#!/usr/bin/env python3
"""The step graph, checked as a whole rather than one rule at a time.

test_spec.py states individual rules; this walks every step under both
strategies in one loop, so a rule changed in one place and not the other is
caught without anyone remembering to add a test for it.

Mocks come from unittest.mock (stdlib — no new dependency): the expensive and
destructive parts (upload, render, the S3 listing) are patched out, so the
graph can be exercised without a card, a bucket or a render.
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import steps as S            # noqa: E402  (after sys.path)


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_graph", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()


class MockState:
    """A workspace in whatever condition a test needs, without doing the work.

    Each `with_*` marks one milestone as reached — the file-level evidence the
    real steps leave behind — so a test can say "as if rendered but not
    uploaded" in one line.
    """

    def __init__(self, strategy=S.Strategy.LOCAL_DEFAULT_WEBSITE):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-graph-"))
        (self.root / "out").mkdir()
        (self.root / "import").mkdir()
        c = P.Ctx.__new__(P.Ctx)
        c.exporter = P.EXPORTER_DIR
        c.cfg = {}
        c.out_dir = self.root / "out"
        c.final_root = self.root
        c.render_root = self.root / "import"
        c.import_root = self.root / "import"
        c.card = self.root / "card"
        c.site = None
        c.s3_bucket = None
        c.selected_import = None
        c.last_scan = None
        c.last_groups = None
        c.config_args = []
        c.scan_args = []
        self.ctx = c
        if strategy is S.Strategy.WEBSITE_REPO:
            self.with_site_repo().with_bucket()

    # -- milestones --------------------------------------------------------
    def with_card(self, stamps=("20260728090000",)):
        d = self.ctx.card / "DCIM" / "200video" / "front"
        d.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (d / ("%s_0060.mp4" % s)).write_text("clip")
        return self

    def with_import(self, stamps=("20260728090000",)):
        d = self.ctx.render_root / "DCIM" / "200video" / "front"
        d.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (d / ("%s_0060.mp4" % s)).write_text("clip")
        (self.ctx.render_root / "DCIM" / "203gps").mkdir(parents=True, exist_ok=True)
        (self.ctx.render_root / "DCIM" / "203gps" / "t.gpx").write_text("<gpx/>")
        return self

    def with_sidecars(self, trip="trip_2026-07-28_08-57_01", day="2026-07-28"):
        d = self.ctx.out_dir / self.ctx.render_root.name / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_meta.json")).write_text(
            '{"day":"%s","start":"%s 08:00:00","end":"%s 09:00:00"}' % (day, day, day))
        (d / (trip + ".gpx")).write_text("<gpx/>")
        (d / (trip + ".html")).write_text("<html/>")
        return self

    def with_render(self, trip="trip_2026-07-28_08-57_01", day="2026-07-28", size=64):
        d = self.ctx.out_dir / self.ctx.render_root.name / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_h1080.mp4")).write_bytes(b"x" * size)
        return self

    def with_site_repo(self):
        s = self.root / "site"
        (s / "deploy").mkdir(parents=True, exist_ok=True)
        for name in ("deploy-site.sh", "upload-videos-s3.sh"):
            (s / "deploy" / name).write_text("#!/bin/sh\n")
        (s / "build_manifest.py").write_text("")
        self.ctx.site = s
        return self

    def with_bucket(self, contents=None):
        self.ctx.cfg["s3_bucket"] = "bucket"
        self.ctx.s3_bucket = "bucket"
        self._bucket = contents or {}
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class GraphTest(unittest.TestCase):
    def setUp(self):
        # Nothing in these tests may reach the network or the renderer.
        self.patches = [
            mock.patch.object(P, "s3_objects", side_effect=lambda ctx: getattr(
                self, "_bucket_contents", {})),
            mock.patch.object(P, "run_stream", side_effect=AssertionError(
                "a graph test must not run a subprocess")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class TestGraphConsistency(GraphTest):
    """Every edge is declared from both ends, under both strategies."""

    def test_edges_agree_in_both_directions(self):
        for strategy in S.Strategy:
            with self.subTest(strategy=strategy.value):
                problems = S.inconsistent_edges(strategy)
                self.assertEqual(problems, [], "one-sided edges: %r" % (problems,))

    def test_every_step_is_reachable_or_an_entry_point(self):
        for strategy in S.Strategy:
            for step in S.ALL_STEPS.values():
                with self.subTest(strategy=strategy.value, step=step.number):
                    incoming = step.reachable_from(strategy)
                    if strategy is S.Strategy.LOCAL_DEFAULT_WEBSITE and step.number in (
                            S.UPLOAD, S.DEPLOY):
                        self.assertEqual(incoming, set(),
                                         "publishing steps have no place in the local product")
                    else:
                        self.assertTrue(incoming,
                                        "step %d is unreachable" % step.number)

    def test_the_cycle_closes(self):
        """Every strategy has a path from import back to import."""
        for strategy in S.Strategy:
            with self.subTest(strategy=strategy.value):
                seen, frontier = set(), {S.IMPORT}
                while frontier:
                    n = frontier.pop()
                    if n in seen:
                        continue
                    seen.add(n)
                    frontier |= S.ALL_STEPS[n].reaches_to(strategy)
                self.assertIn(S.CLEANUP, seen, "no route to clearing the workspace")
                self.assertIn(S.IMPORT, S.ALL_STEPS[S.CLEANUP].reaches_to(strategy))


class TestInterfaceMatchesBehaviour(GraphTest):
    """What a step SAYS about itself has to be what the tool DOES."""

    def test_start_nodes_are_exactly_the_steps_enabled_on_a_cold_start(self):
        """is_start_node() must equal is_enabled() on an untouched workspace.

        A step advertising itself as an entry point while the menu greys it out
        is the same class of drift the two-sided edges exist to catch.
        """
        for strategy in S.Strategy:
            m = MockState(strategy)
            try:
                m.with_card()          # "nothing happened" = card in, nothing done
                self._bucket_contents = {}
                for step in S.ALL_STEPS.values():
                    with self.subTest(strategy=strategy.value, step=step.number):
                        self.assertEqual(
                            step.is_start_node(m.ctx), step.is_enabled(m.ctx),
                            "step %d: is_start_node=%s but is_enabled=%s on a cold start"
                            % (step.number, step.is_start_node(m.ctx), step.is_enabled(m.ctx)))
            finally:
                m.cleanup()

    def test_the_ways_in(self):
        """Footage always enters through Import — the only ACTING start node
        under both strategies. Deploy looked like a second way in on a
        publishing install, but deploying with no sidecars publishes nothing.
        Progress is a start node too, because looking at an empty workspace is
        legitimate — it is a view, not a transition."""
        local = MockState(S.Strategy.LOCAL_DEFAULT_WEBSITE)
        pub = MockState(S.Strategy.WEBSITE_REPO)
        try:
            self.assertEqual(
                [s.number for s in S.ALL_STEPS.values() if s.is_start_node(local.ctx)],
                [S.IMPORT, S.PROGRESS])
            self.assertEqual(
                [s.number for s in S.ALL_STEPS.values() if s.is_start_node(pub.ctx)],
                [S.IMPORT, S.PROGRESS])
        finally:
            local.cleanup(); pub.cleanup()

    def test_destructive_steps_are_the_ones_that_erase(self):
        destructive = {s.number for s in S.ALL_STEPS.values() if s.is_destructive()}
        self.assertEqual(destructive, {S.EXCLUDE, S.CLEANUP})

    def test_is_enabled_is_the_negation_of_a_reason(self):
        """One answer, not two: enabled iff nothing is in the way."""
        m = MockState()
        try:
            self._bucket_contents = {}
            for step in S.ALL_STEPS.values():
                with self.subTest(step=step.number):
                    self.assertEqual(step.is_enabled(m.ctx),
                                     step.blocked_because(m.ctx) is None)
        finally:
            m.cleanup()


class TestStrategySplit(GraphTest):
    """The two products differ where they should and nowhere else."""

    def test_strategy_is_decided_by_configuration(self):
        local = MockState()
        self.assertIs(S.Strategy.of(local.ctx), S.Strategy.LOCAL_DEFAULT_WEBSITE)
        local.cleanup()
        pub = MockState(S.Strategy.WEBSITE_REPO)
        self.assertIs(S.Strategy.of(pub.ctx), S.Strategy.WEBSITE_REPO)
        pub.cleanup()

    def test_only_the_publishing_steps_differ(self):
        differing = set()
        for step in S.ALL_STEPS.values():
            a = (step.reachable_from(S.Strategy.WEBSITE_REPO),
                 step.reaches_to(S.Strategy.WEBSITE_REPO))
            b = (step.reachable_from(S.Strategy.LOCAL_DEFAULT_WEBSITE),
                 step.reaches_to(S.Strategy.LOCAL_DEFAULT_WEBSITE))
            if a != b:
                differing.add(step.number)
        # GENERATE_META is in the set because on a publishing install the
        # sidecars unlock Deploy — publish early, catch a broken pipeline
        # before the render — an edge the local product does not have.
        self.assertEqual(differing,
                         {S.GENERATE_META, S.RENDER, S.SITE, S.UPLOAD, S.DEPLOY, S.CLEANUP},
                         "the split should touch publishing and what settles the workspace")

    def test_local_product_settles_the_workspace_by_gathering(self):
        s = S.Strategy.LOCAL_DEFAULT_WEBSITE
        self.assertIn(S.CLEANUP, S.ALL_STEPS[S.SITE].reaches_to(s))

    def test_publishing_product_settles_the_workspace_by_deploying(self):
        s = S.Strategy.WEBSITE_REPO
        self.assertIn(S.CLEANUP, S.ALL_STEPS[S.DEPLOY].reaches_to(s))
        self.assertNotIn(S.CLEANUP, S.ALL_STEPS[S.SITE].reaches_to(s))


class TestAvailabilityMatchesTheGraph(GraphTest):
    """Walk every step in one loop: with its predecessors done it is offered,
    and from an empty workspace it is not."""

    MILESTONES = {
        S.GENERATE_META: lambda m: m.with_import(),
        S.PREVIEW:  lambda m: m.with_import().with_sidecars(),
        S.EXCLUDE:  lambda m: m.with_import().with_sidecars(),
        S.RENDER:   lambda m: m.with_import().with_sidecars(),
        S.SITE:     lambda m: m.with_import().with_sidecars().with_render(),
        S.UPLOAD:   lambda m: m.with_import().with_sidecars().with_render(),
        # No card: the clean-up's card half is only asked when a card with
        # footage is in; the workspace half alone is a legitimate run.
        S.CLEANUP:  lambda m: m.with_import().with_sidecars(),
    }

    def test_each_step_is_unavailable_from_an_empty_workspace(self):
        for number in (S.GENERATE_META, S.PREVIEW, S.EXCLUDE, S.RENDER, S.SITE, S.CLEANUP):
            m = MockState()
            try:
                self._bucket_contents = {}
                blocked = P.unavailable_steps(m.ctx)
                with self.subTest(step=number):
                    self.assertIn(number, blocked,
                                  "step %d should not be offered on an empty workspace" % number)
            finally:
                m.cleanup()

    def test_each_step_is_available_once_its_predecessors_are_done(self):
        for number, prepare in self.MILESTONES.items():
            strategy = (S.Strategy.WEBSITE_REPO if number in (S.UPLOAD,)
                        else S.Strategy.LOCAL_DEFAULT_WEBSITE)
            m = MockState(strategy)
            try:
                prepare(m)
                self._bucket_contents = {}
                blocked = P.unavailable_steps(m.ctx)
                with self.subTest(step=number, strategy=strategy.value):
                    self.assertNotIn(number, blocked,
                                     "step %d should be offered: %s"
                                     % (number, blocked.get(number)))
            finally:
                m.cleanup()

    def test_publishing_steps_are_unavailable_in_the_local_product(self):
        m = MockState(S.Strategy.LOCAL_DEFAULT_WEBSITE)
        try:
            m.with_import().with_sidecars().with_render()
            blocked = P.unavailable_steps(m.ctx)
            for number in (S.UPLOAD, S.DEPLOY):
                with self.subTest(step=number):
                    self.assertIn(number, blocked)
        finally:
            m.cleanup()


class TestMockedWork(GraphTest):
    """The expensive steps are mocked: the graph is exercised, nothing runs."""

    def test_a_mocked_render_moves_the_state_forward(self):
        m = MockState()
        try:
            m.with_import().with_sidecars()
            self._bucket_contents = {}
            self.assertIn(S.SITE, P.unavailable_steps(m.ctx), "no renders yet")
            m.with_render()                       # as if step 5 had run
            self.assertNotIn(S.SITE, P.unavailable_steps(m.ctx))
        finally:
            m.cleanup()

    def test_a_mocked_upload_makes_the_workspace_expendable(self):
        m = MockState(S.Strategy.WEBSITE_REPO)
        try:
            m.with_import().with_sidecars().with_render(size=64)
            self._bucket_contents = {}
            ok, why, _ = P.working_area_is_expendable(m.ctx)
            self.assertFalse(ok, "nothing uploaded yet")
            self._bucket_contents = {"v/trip_2026-07-28_08-57_01_h1080.mp4": 64}
            (m.ctx.out_dir / P.DEPLOYED_FILE).write_text(
                '{"videos":["trip_2026-07-28_08-57_01_h1080.mp4"]}')
            ok, why, _ = P.working_area_is_expendable(m.ctx)
            self.assertTrue(ok, why)
        finally:
            m.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
