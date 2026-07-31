#!/usr/bin/env python3
"""The menu graph, checked as a whole rather than one rule at a time.

test_spec.py states individual rules; this walks every item under both
strategies in one loop, so a rule changed in one place and not the other is
caught without anyone remembering to add a test for it.

Mocks come from unittest.mock (stdlib — no new dependency): the expensive and
destructive parts (upload, render, the S3 listing) are patched out, so the
graph can be exercised without a card, a bucket or a render.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import items                     # noqa: E402,F401  (registers the ten)
import menu as M                 # noqa: E402
import uploader as U             # noqa: E402
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)      # noqa: E402
from print_step_graph import NullWork                # noqa: E402


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_graph", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()


# The owner's inbound column, transcribed. It is NOT what the tool runs on —
# menu.derive_inbound computes that from every item's outbound — but it is not
# thrown away either: every difference between the two is a finding for the
# person who wrote the table, and this is where they are pinned so a NEW one
# fails the suite instead of passing unnoticed.
#
# Items 0 and 1 are absent by definition rather than by exemption: their
# inbound is a KIND (Anywhere, StartNode), not a set of numbers.
AUTHORED_INBOUND = {
    M.Strategy.UPLOADER: {
        META: {META, IMPORT, EXCLUDE},
        PREVIEW: {PREVIEW, META, EXCLUDE},
        EXCLUDE: {EXCLUDE, META, PREVIEW},
        RENDER: {RENDER, META, PREVIEW, EXCLUDE, BUILD, UPLOAD},
        BUILD: {BUILD, META, PREVIEW, EXCLUDE, RENDER, UPLOAD},
        UPLOAD: {UPLOAD, BUILD},
        CLEAN_WS: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
        ERASE_CARD: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
    },
    M.Strategy.LOCAL_PAGE: {
        META: {META, IMPORT, EXCLUDE},
        PREVIEW: {PREVIEW, META, EXCLUDE},
        EXCLUDE: {EXCLUDE, META, PREVIEW},
        RENDER: {RENDER, META, PREVIEW, EXCLUDE, BUILD},
        BUILD: {BUILD, META, PREVIEW, EXCLUDE, RENDER},
        UPLOAD: set(),
        CLEAN_WS: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD},
        ERASE_CARD: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD},
    },
}

# Which items the derivation legitimately parts company with, and why. Every
# entry is a decision, not a waiver: a difference NOT listed here fails.
EXPLAINED = {
    META: "back-edges: 3, 5, 6 and 7 all offer 2, which he wrote into their "
          "outbound and not into 2's inbound",
    PREVIEW: "adds the same back-edges; drops 4, because Exclude Trip's "
             "outbound is the narrow {4,2,8,9} of the owner's rule 6",
    EXCLUDE: "adds the back-edges from 5, 6 and 7",
    RENDER: "drops 4, for the same rule-6 reason as 3",
    BUILD: "drops 4, for the same rule-6 reason as 3",
}


class MockState:
    """A workspace in whatever condition a test needs, without doing the work.

    Each `with_*` marks one milestone as reached — the file-level evidence the
    real items leave behind — so a test can say "as if rendered but not
    uploaded" in one line.
    """

    def __init__(self, strategy=M.Strategy.LOCAL_PAGE):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-graph-"))
        (self.root / "out").mkdir()
        (self.root / "import").mkdir()
        c = P.Ctx.__new__(P.Ctx)
        c.exporter = P.EXPORTER_DIR
        c.cfg = {}
        c.out_dir = self.root / "out"
        c.final_root = self.root
        # Its own, never the real one under $HOME: a clean-up test MOVES
        # receipts there, and the next test would read them as evidence
        # about a real card.
        c.archive_dir = self.root / "archive"
        c.state_dir = self.root / "state"
        c.lock_file = self.root / "import" / P.LOCK_FILE
        c.render_root = self.root / "import"
        c.import_root = self.root / "import"
        c.card = self.root / "card"
        c.plugin = None
        c.offline = False
        c.selected_import = None
        c.last_scan = None
        c.last_groups = None
        c.config_args = []
        c.scan_args = []
        self.ctx = c
        if strategy is M.Strategy.UPLOADER:
            self.with_uploader()

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

    def published(self, trip="trip_2026-07-28_08-57_01", size=64):
        """This trip is at the destination already.

        The shipped example publishes a page per trip, so "already published"
        is a real file in a real directory rather than a patched-out listing.
        What the guards then read is a genuine answer from a genuine
        implementation.
        """
        d = self.root / "published"
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + ".html")).write_text("<html/>")
        return self

    def with_uploader(self):
        """A configured publishing plugin, loaded the way a real install loads
        one.

        The shipped example rather than a stub written here: what these tests
        exercise is the wiring between the menu and whatever was configured,
        and a stub would let that wiring drift from the one implementation
        anybody actually reads.
        """
        os.environ["DASHCAM_LOCAL_SITE_STAGING"] = str(self.root / "staging")
        os.environ["DASHCAM_LOCAL_SITE_DEST"] = str(self.root / "published")
        spec = ("%s:LocalWebSiteBuilderPlugin:LocalWebSiteUploader"
                % (REPO / "examples" / "local_website.py"))
        self.ctx.plugin = U.load_plugin(spec, REPO)
        return self

    def menu(self):
        """The ten items, built for whatever this ctx configures."""
        return M.build_menu(M.Strategy.of(self.ctx.plugin), P.Work(self.ctx))

    def verdicts(self, scope=None):
        """{number: Verdict} — what each item says about this world."""
        world = P.capture_world(self.ctx, scope or M.Scope.LOCAL)
        return {n: item.evaluate(world) for n, item in self.menu().items()}

    def blocked(self, scope=None):
        """{number: reason} for the items that would do nothing right now."""
        return {n: v.reason for n, v in self.verdicts(scope).items() if v.blocked}

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class GraphTest(unittest.TestCase):
    def setUp(self):
        # Nothing in these tests may reach the network or the renderer.
        self.patches = [
            mock.patch.object(P, "run_stream", side_effect=AssertionError(
                "a graph test must not run a subprocess")),
            mock.patch.object(P, "load_groups", side_effect=lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class TestGraphConsistency(GraphTest):
    """The graph holds together, under both strategies."""

    def test_the_authored_inbound_column_differs_only_where_explained(self):
        """The owner's hand-written inbound against the derivation.

        Reported, never silently reconciled. A one-sided edge cannot exist any
        more — there is only one side, the outbound — so what this catches is
        a NEW divergence between what he wrote and what the items declare.
        """
        for strategy in M.Strategy:
            derived = M.derive_inbound(M.registry(), strategy)
            for number, authored in AUTHORED_INBOUND[strategy].items():
                with self.subTest(strategy=strategy.value, item=number):
                    if derived[number].edges() == authored:
                        continue
                    self.assertIn(number, EXPLAINED,
                                  "item %d: authored %s, derived %s — an unexplained "
                                  "difference from the owner's table"
                                  % (number, sorted(authored),
                                     sorted(derived[number].edges())))

    def test_only_items_0_and_1_declare_a_kind_instead_of_edges(self):
        """The two exemptions are by definition, not by a skip list.

        Progress neighbours everything and must not force the other nine to
        declare it back; Import SIM is where footage comes in and declares no
        inbound even though Clean Workspace offers it.
        """
        for strategy in M.Strategy:
            built = M.build_menu(strategy, NullWork())
            kinds = {n for n, i in built.items() if i.inbound().edges() is None}
            self.assertEqual(kinds, {PROGRESS, IMPORT})

    def test_every_item_is_reachable_or_declares_why_not(self):
        for strategy in M.Strategy:
            built = M.build_menu(strategy, NullWork())
            for number, item in built.items():
                with self.subTest(strategy=strategy.value, item=number):
                    self._reachable_or_exempt(strategy, number, item)

    def _reachable_or_exempt(self, strategy, number, item):
        incoming = item.inbound().edges()
        if number in (PROGRESS, IMPORT):
            self.assertIsNone(incoming, "a kind, not an edge set")
        elif strategy is M.Strategy.LOCAL_PAGE and number == UPLOAD:
            self.assertEqual(incoming, frozenset(),
                             "publishing has no place in the local product")
        else:
            self.assertTrue(incoming, "item %d is unreachable" % number)

    def test_the_cycle_closes(self):
        """Every strategy has a path from import back to import."""
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                built = M.build_menu(strategy, NullWork())
                seen = self._walk(built, IMPORT)
                self.assertIn(CLEAN_WS, seen, "no route to clearing the workspace")
                self.assertIn(IMPORT,
                              built[CLEAN_WS].outbound().offers(frozenset(built)))

    def _walk(self, built, start):
        seen, frontier = set(), {start}
        universe = frozenset(built)
        while frontier:
            n = frontier.pop()
            if n in seen:
                continue
            seen.add(n)
            frontier |= set(built[n].outbound().offers(universe))
        return seen

    def test_publishing_is_reached_from_building_and_nowhere_else(self):
        """The edge the owner's table was missing.

        He wrote 6 into item 7's inbound and never put 7 into any outbound, so
        Upload Website was unreachable by its own natural route. Item 5 offered
        it instead, which skips building the site item 7 uploads.
        """
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        universe = frozenset(built)
        self.assertIn(UPLOAD, built[BUILD].outbound().offers(universe))
        self.assertNotIn(UPLOAD, built[RENDER].outbound().offers(universe))


class TestTheUnfoldIsStructural(GraphTest):
    """8 cannot precede 9 in one cycle, and 9 can precede 8.

    The folded clean-up gathered the card's evidence from the workspace, erased
    the workspace, then refused the card half after the irreversible half had
    already run — having printed that the card was verified. With the halves
    unfolded, item 8's outbound is {1}, so that sequence cannot be expressed.
    """

    def test_clean_workspace_offers_only_a_new_cycle(self):
        for strategy in M.Strategy:
            built = M.build_menu(strategy, NullWork())
            offered = built[CLEAN_WS].outbound().offers(frozenset(built))
            with self.subTest(strategy=strategy.value):
                self.assertEqual(set(offered), {IMPORT})
                self.assertNotIn(ERASE_CARD, offered)

    def test_erasing_the_card_hands_the_position_back(self):
        """Freeing the card does not interrupt the cycle, so Clean Workspace
        is still reachable afterwards — the safe order is the permitted one."""
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        position = M.position_for(built)
        position.current = RENDER
        self.assertEqual(built[ERASE_CARD].settles_at(RENDER), RENDER)
        self.assertIn(CLEAN_WS, position.selectable(built))


class TestTheOwnersWorkedExample(GraphTest):
    """Rule 6, asserted literally against the graph."""

    def test_after_a_preview_and_then_an_exclusion(self):
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        position = M.position_for(built)
        position.current = PREVIEW
        self.assertEqual(sorted(position.selectable(built)),
                         [PROGRESS, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                          CLEAN_WS, ERASE_CARD])
        position.current = EXCLUDE
        self.assertEqual(sorted(position.selectable(built)),
                         [PROGRESS, META, EXCLUDE, CLEAN_WS, ERASE_CARD])


class TestInterfaceMatchesBehaviour(GraphTest):
    """What an item SAYS about itself has to be what the tool DOES."""

    def test_the_ways_in(self):
        """Footage enters through Import, the only start node.

        Progress is NOT one, and the owner's `start = -` for it is right: a
        view is not a transition. It reaches the menu through its neighbour
        KIND instead, which is why it is selectable from anywhere including
        nowhere.
        """
        for strategy in M.Strategy:
            built = M.build_menu(strategy, NullWork())
            with self.subTest(strategy=strategy.value):
                self.assertEqual([n for n, i in sorted(built.items()) if i.start()],
                                 [IMPORT])
                position = M.position_for(built)
                self.assertEqual(sorted(position.selectable(built)),
                                 [PROGRESS, IMPORT])

    def test_destructive_items_are_the_ones_that_erase(self):
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        self.assertEqual({n for n, i in built.items() if i.destr()},
                         {EXCLUDE, CLEAN_WS, ERASE_CARD})

    def test_every_destructive_item_asks_for_a_distinct_word(self):
        """Two identical prompts is how the second one gets typed from muscle
        memory, and items 8 and 9 are now two prompts."""
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        words = [i.word() for i in built.values() if i.destr()]
        self.assertEqual(sorted(words), ["CLEAN", "DROP", "ERASE"])

    def test_the_items_that_end_the_cycle_say_so(self):
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        self.assertEqual({n for n, i in built.items() if i.end()},
                         {CLEAN_WS, ERASE_CARD})

    def test_a_blocked_verdict_always_carries_a_reason(self):
        """One answer, not two: blocked iff there is something to say."""
        m = MockState()
        try:
            for number, verdict in m.verdicts().items():
                with self.subTest(item=number):
                    self.assertEqual(verdict.blocked, bool(verdict.reason)
                                     and verdict.blocked)
                    if verdict.blocked:
                        self.assertTrue(verdict.reason, "item %d blocked in silence"
                                        % number)
        finally:
            m.cleanup()

    def test_completed_is_an_error_before_the_item_has_run(self):
        """Never a default answer. A stale read here is the difference between
        a report saying the card was erased and one saying it was refused."""
        built = M.build_menu(M.Strategy.UPLOADER, NullWork())
        with self.assertRaises(M.NotRun):
            built[ERASE_CARD].completed()


class TestStrategySplit(GraphTest):
    """The two products differ where they should and nowhere else."""

    def test_strategy_is_whether_an_implementation_was_supplied(self):
        local = MockState()
        self.assertIs(M.Strategy.of(local.ctx.plugin), M.Strategy.LOCAL_PAGE)
        local.cleanup()
        pub = MockState(M.Strategy.UPLOADER)
        self.assertIs(M.Strategy.of(pub.ctx.plugin), M.Strategy.UPLOADER)
        pub.cleanup()

    def test_the_strategy_cannot_see_a_ctx_to_read_config_keys_off(self):
        """It used to resolve on one operator's two settings — a site repo and
        a bucket name — which is the same question as "was an uploader
        supplied" expressed in his terms. Taking the uploader itself is what
        stops that coming back."""
        self.assertIs(M.Strategy.of(None), M.Strategy.LOCAL_PAGE)
        self.assertIs(M.Strategy.of(object()), M.Strategy.UPLOADER)

    def test_only_the_publishing_items_declare_different_edges(self):
        """The AUTHORED column — outbound — differs for exactly two items.

        Item 6 gains the edge to publishing, and item 7 has no edges at all
        under the local product. Every other difference in the table is in the
        DERIVED inbound and follows from these two.
        """
        a = M.build_menu(M.Strategy.UPLOADER, NullWork())
        b = M.build_menu(M.Strategy.LOCAL_PAGE, NullWork())
        differing = {n for n in a
                     if a[n].outbound().edges() != b[n].outbound().edges()}
        self.assertEqual(differing, {BUILD, UPLOAD})

    def test_local_product_settles_the_workspace_by_gathering(self):
        b = M.build_menu(M.Strategy.LOCAL_PAGE, NullWork())
        self.assertIn(CLEAN_WS, b[BUILD].outbound().offers(frozenset(b)))

    def test_publishing_product_settles_the_workspace_by_deploying(self):
        a = M.build_menu(M.Strategy.UPLOADER, NullWork())
        self.assertIn(CLEAN_WS, a[UPLOAD].outbound().offers(frozenset(a)))

    def test_publishing_is_unavailable_in_the_local_product(self):
        """Twice over, and neither is an `if` in a body: nothing offers it,
        and its own guard blocks it."""
        m = MockState(M.Strategy.LOCAL_PAGE)
        try:
            m.with_import().with_sidecars().with_render()
            self.assertIn(UPLOAD, m.blocked())
            built = m.menu()
            self.assertEqual(set(built[UPLOAD].outbound().offers(frozenset(built))),
                             set())
        finally:
            m.cleanup()


class TestGuardsSeeTheWorld(GraphTest):
    """With its evidence absent an item blocks; with it present it does not."""

    MILESTONES = {
        META: lambda m: m.with_import(),
        PREVIEW: lambda m: m.with_import().with_sidecars(),
        EXCLUDE: lambda m: m.with_import().with_sidecars(),
        RENDER: lambda m: m.with_import().with_sidecars(),
        BUILD: lambda m: m.with_import().with_sidecars().with_render(),
        # No card: item 9 is the card's own item now, so the workspace half
        # never has to reason about one.
        CLEAN_WS: lambda m: m.with_import().with_sidecars(),
    }

    def test_each_item_is_blocked_on_an_empty_workspace(self):
        for number in self.MILESTONES:
            m = MockState()
            try:
                with self.subTest(item=number):
                    self.assertIn(number, m.blocked(),
                                  "item %d has no evidence to work from" % number)
            finally:
                m.cleanup()

    def test_each_item_is_offered_once_its_evidence_is_there(self):
        for number, prepare in self.MILESTONES.items():
            m = MockState()
            try:
                prepare(m)
                blocked = m.blocked()
                with self.subTest(item=number):
                    self.assertNotIn(number, blocked,
                                     "item %d should be offered: %s"
                                     % (number, blocked.get(number)))
            finally:
                m.cleanup()

    def test_progress_is_never_blocked(self):
        """An empty workspace is a legitimate thing to report."""
        m = MockState()
        try:
            self.assertNotIn(PROGRESS, m.blocked())
        finally:
            m.cleanup()


class TestMockedWork(GraphTest):
    """The expensive items are mocked: the graph is exercised, nothing runs."""

    def test_a_mocked_render_moves_the_state_forward(self):
        m = MockState()
        try:
            m.with_import().with_sidecars()
            self.assertIn(BUILD, m.blocked(), "no renders yet")
            m.with_render()                       # as if Render Videos had run
            self.assertNotIn(BUILD, m.blocked())
        finally:
            m.cleanup()

    def test_publishing_makes_the_workspace_expendable(self):
        """Driven through a real implementation rather than a patched listing,
        so what this proves is the whole path: the plugin is asked at capture,
        its answer is frozen into the world, and the guard reads it there."""
        m = MockState(M.Strategy.UPLOADER)
        try:
            m.with_import().with_sidecars().with_render(size=64)
            target = P.capture_world(m.ctx, M.Scope.FULL).target
            ok, why, _ = P.working_area_is_expendable(m.ctx, target)
            self.assertFalse(ok, "nothing published yet")
            m.published(size=64)
            target = P.capture_world(m.ctx, M.Scope.FULL).target
            ok, why, _ = P.working_area_is_expendable(m.ctx, target)
            self.assertTrue(ok, why)
        finally:
            m.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
