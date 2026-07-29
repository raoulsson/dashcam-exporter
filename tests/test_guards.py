#!/usr/bin/env python3
"""What the destructive items delete, and what they refuse to.

Every test builds a throwaway workspace under a temp dir. Nothing here reads the
real card, the real import or the real output tree — a test that needed those
would be a test you cannot run.

Run with:  ./run-tests.sh          (or: python3 -m unittest discover -s tests)

THREE paths erase things — 4) Exclude Trip, 8) Clean Workspace and 9) Delete
SIM Data — and each is guarded by a predicate. It used to be four: the import
step swept the previous round from inside itself, which was item 8's job run
from item 1, and that arc is gone. These lock the predicates down: what makes
something expendable, what counts as evidence a copy survives, and what the
sweep keeps when it does run.
"""

import json
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import guards                    # noqa: E402
import items                     # noqa: E402,F401  (registers the ten)
import menu as M                 # noqa: E402
import world as W                # noqa: E402


def load_pipeline():
    """Import pipeline.py as a module without running its CLI."""
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()


class Workspace:
    """A disposable workspace, shaped like the real one.

    root/
      import/            the footage being worked on   (ctx.render_root)
      out/               renders, previews, caches     (ctx.out_dir)
      final_<day>/       the gathered deliverable      (beside out/, as in life)
      card/DCIM/...      a fake SD card                (ctx.card)
    """

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-test-"))
        (self.root / "out").mkdir()
        (self.root / "import").mkdir()
        self.ctx = P.Ctx.__new__(P.Ctx)
        self.ctx.exporter = P.EXPORTER_DIR
        self.ctx.cfg = {}
        self.ctx.out_dir = self.root / "out"
        self.ctx.final_root = self.root
        self.ctx.render_root = self.root / "import"
        self.ctx.import_root = self.root / "import"
        self.ctx.card = self.root / "card"
        self.ctx.uploader = None
        self.ctx.site = None
        self.ctx.s3_bucket = None
        self.ctx.selected_import = None
        self.ctx.last_scan = None
        self.ctx.last_groups = None

    # -- builders ----------------------------------------------------------
    def render(self, trip_id, day="2026-07-28", ns="import", size=1024, meta=True,
               start="2026-07-28 08:00:00", end="2026-07-28 09:00:00"):
        d = self.ctx.out_dir / ns / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip_id + "_h1080.mp4")).write_bytes(b"x" * size)
        if meta:
            (d / (trip_id + "_meta.json")).write_text(json.dumps(
                {"day": day, "start": start, "end": end,
                 "video": trip_id + "_h1080.mp4"}))
        return d

    def gathered(self, trip_id, day="2026-07-28", size=1024):
        d = self.root / (P.FINAL_PREFIX + day) / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip_id + "_h1080.mp4")).write_bytes(b"x" * size)
        return d

    def clips(self, stamps, where="card"):
        base = (self.ctx.card if where == "card" else self.ctx.render_root)
        front = base / "DCIM" / "200video" / "front"
        front.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (front / ("%s_0060.mp4" % s)).write_text("clip")
        return front

    def ledger(self, through):
        (self.ctx.out_dir / P.LEDGER_FILE).write_text(json.dumps({"through": through}))

    def bucket(self, mapping):
        """Pretend the configured bucket holds {key: size}."""
        self.ctx.cfg["s3_bucket"] = "test-bucket"
        P.s3_objects = lambda ctx: mapping

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._real_s3 = P.s3_objects
        self.w = Workspace()

    def tearDown(self):
        P.s3_objects = self._real_s3
        self.w.cleanup()


# ---------------------------------------------------------------------------
# working_area_is_expendable — the predicate 8) Clean Workspace obeys
# ---------------------------------------------------------------------------

class TestWorkingAreaIsExpendable(GuardTest):

    def test_empty_workspace_is_expendable(self):
        ok, why, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertTrue(ok, why)
        self.assertEqual(strag, [])

    def test_render_neither_uploaded_nor_gathered_blocks(self):
        self.w.render("trip_2026-07-28_08-57_01")
        ok, why, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)
        self.assertIn("neither uploaded nor gathered", why)

    def test_gathered_render_is_expendable(self):
        self.w.render("trip_A", size=2048)
        self.w.gathered("trip_A", size=2048)
        ok, why, _ = P.working_area_is_expendable(self.w.ctx)
        self.assertTrue(ok, why)

    def test_gathered_copy_of_a_different_size_does_not_count(self):
        """A re-render has the same NAME as the stale copy in final_.

        Matching on name alone declared it expendable and deleted the new file —
        the one gather_into_final refuses to overwrite so it can be looked at.
        """
        self.w.render("trip_A", size=4096)          # re-rendered, new bytes
        self.w.gathered("trip_A", size=1024)        # the older, smaller copy
        ok, _, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_uploaded_render_is_expendable(self):
        self.w.render("trip_A", size=777)
        self.w.bucket({"videos/trip_A_h1080.mp4": 777})
        ok, why, _ = P.working_area_is_expendable(self.w.ctx)
        self.assertTrue(ok, why)

    def test_bucket_size_mismatch_blocks(self):
        self.w.render("trip_A", size=777)
        self.w.bucket({"videos/trip_A_h1080.mp4": 999})
        ok, _, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_unlistable_bucket_fails_closed(self):
        self.w.render("trip_A")
        self.w.ctx.cfg["s3_bucket"] = "test-bucket"
        P.s3_objects = lambda ctx: None            # cannot reach S3
        ok, _, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertFalse(ok, "an unreachable bucket must not read as 'uploaded'")
        self.assertEqual(len(strag), 1)

    def test_key_suffix_needs_a_path_boundary(self):
        """sometrip_A_h1080.mp4 in the bucket must not vouch for trip_A_h1080.mp4."""
        self.w.render("trip_A", size=500)
        self.w.bucket({"videos/sometrip_A_h1080.mp4": 500})
        ok, _, strag = P.working_area_is_expendable(self.w.ctx)
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_previews_and_caches_never_block(self):
        (self.w.ctx.out_dir / "previews").mkdir()
        (self.w.ctx.out_dir / "previews" / "still.jpg").write_text("jpg")
        (self.w.ctx.out_dir / ".scan_cache.json").write_text("{}")
        ok, why, _ = P.working_area_is_expendable(self.w.ctx)
        self.assertTrue(ok, why)


# ---------------------------------------------------------------------------
# purge_published_renders — what Clean Workspace keeps when it is allowed to run
# ---------------------------------------------------------------------------

class TestPurgeKeepsState(GuardTest):

    def test_keeps_meta_ledger_logs_and_empties_the_rest(self):
        d = self.w.render("trip_A", size=4096)
        (d / "trip_A.gpx").write_text("gpx")
        (d / "trip_A.html").write_text("html")
        (self.w.ctx.out_dir / "previews").mkdir()
        (self.w.ctx.out_dir / "previews" / "s.jpg").write_text("jpg")
        (self.w.ctx.out_dir / "logs").mkdir()
        (self.w.ctx.out_dir / "logs" / "run.log").write_text("log")
        self.w.ledger("20260728000000")

        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)

        self.assertTrue((self.w.ctx.out_dir / P.LEDGER_FILE).is_file(), "ledger must survive")
        self.assertTrue((self.w.ctx.out_dir / "logs" / "run.log").is_file(), "logs must survive")
        self.assertTrue((d / "trip_A_meta.json").is_file(), "metadata must survive")
        self.assertFalse((d / "trip_A_h1080.mp4").exists(), "the render must go")
        self.assertFalse((d / "trip_A.gpx").exists())
        self.assertFalse((self.w.ctx.out_dir / "previews").exists())

    def test_keeps_meta_in_the_import_namespace_too(self):
        """The render namespace is named after the import dir, so it hits the
        keep-branch — the branch that empties the import folder."""
        d = self.w.render("trip_A", ns=self.w.ctx.render_root.name)
        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)
        self.assertTrue((d / "trip_A_meta.json").is_file())
        self.assertFalse((d / "trip_A_h1080.mp4").exists())

    def test_final_folders_are_untouched(self):
        self.w.gathered("trip_A")
        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)
        finals = list(self.w.root.glob(P.FINAL_PREFIX + "*"))
        self.assertEqual(len(finals), 1)
        self.assertTrue(any(finals[0].rglob("*.mp4")))


# ---------------------------------------------------------------------------
# The ledger and the delta import
# ---------------------------------------------------------------------------

class TestLedgerAndDelta(GuardTest):

    def test_ledger_never_moves_backwards(self):
        """An excluded trip's meta is deleted with its render. If the mark could
        fall back, the next delta import would re-copy the clips of the trip you
        just decided to remove."""
        self.w.ledger("20260728155513")
        P.write_ledger(self.w.ctx, "20260101000000", "older")
        self.assertEqual(P.read_ledger(self.w.ctx)["through"], "20260728155513")

    def test_ledger_advances_on_a_newer_stamp(self):
        self.w.ledger("20260101000000")
        P.write_ledger(self.w.ctx, "20260728155513", "newer")
        self.assertEqual(P.read_ledger(self.w.ctx)["through"], "20260728155513")

    def test_meta_end_raises_the_high_water_mark(self):
        """Between import and render there are no metas; after a render the
        trip's end time is the better mark."""
        self.w.ledger("20260725120000")
        self.w.render("trip_A", day="2026-07-25", end="2026-07-25 22:17:05")
        self.assertEqual(P.last_imported_stamp(self.w.ctx), "20260725221705")

    def test_card_split_counts_new_against_the_mark(self):
        self.w.clips(["20260725160655", "20260726080000", "20260726081000"])
        new, old = P.card_split(self.w.ctx.card, "20260725221705")
        self.assertEqual((new, old), (2, 1))

    def test_no_mark_means_everything_is_new(self):
        self.w.clips(["20260725160655", "20260726080000"])
        new, old = P.card_split(self.w.ctx.card, None)
        self.assertEqual((new, old), (2, 0))


# ---------------------------------------------------------------------------
# copy_still_exists — the guard on 9) Delete SIM Data, the only item whose
# target has no second copy
# ---------------------------------------------------------------------------

class TestCleanSimEvidence(GuardTest):

    def test_no_card_means_no_evidence(self):
        ok, why = P.copy_still_exists(self.w.ctx)
        self.assertFalse(ok)

    def test_this_cards_clips_in_the_workspace_count(self):
        self.w.clips(["20260728090000"])
        self.w.clips(["20260728090000"], where="import")
        ok, why = P.copy_still_exists(self.w.ctx)
        self.assertTrue(ok, why)

    def test_another_cards_clips_do_not_count(self):
        self.w.clips(["20260728090000"])                      # in the slot
        self.w.clips(["20260101000000"], where="import")      # a different card
        ok, _ = P.copy_still_exists(self.w.ctx)
        self.assertFalse(ok, "footage from another card is not evidence for this one")

    def test_a_rendered_trip_covering_the_clips_counts(self):
        self.w.clips(["20260728090000"])
        self.w.render("trip_A", day="2026-07-28",
                      start="2026-07-28 08:57:56", end="2026-07-28 14:13:41")
        ok, why = P.copy_still_exists(self.w.ctx)
        self.assertTrue(ok, why)

    def test_a_render_from_another_month_does_not_count(self):
        """final_ folders survive every sweep, so 'any render on disk' would be
        permanently true once one exists."""
        self.w.clips(["20260901120000"])                       # September card
        self.w.render("trip_A", day="2026-07-28",
                      start="2026-07-28 08:57:56", end="2026-07-28 14:13:41")
        ok, _ = P.copy_still_exists(self.w.ctx)
        self.assertFalse(ok)

    def test_one_covered_clip_does_not_vouch_for_the_whole_card(self):
        """The guard is per clip. One clip inside a rendered trip used to
        return True for the card as a whole, and the wipe then erased clips
        whose only copy WAS the card."""
        self.w.clips(["20260728090000", "20260728100000"])
        self.w.render("trip_A", day="2026-07-28",
                      start="2026-07-28 08:55:00", end="2026-07-28 09:05:00")
        ok, _ = P.copy_still_exists(self.w.ctx)
        self.assertFalse(ok, "the 10:00 clip is accounted for by nothing")

    def test_mixed_evidence_covering_every_clip_counts(self):
        """The kinds of evidence may mix; the accounting may not have gaps."""
        self.w.clips(["20260728090000", "20260728100000"])
        self.w.render("trip_A", day="2026-07-28",
                      start="2026-07-28 08:55:00", end="2026-07-28 09:05:00")
        self.w.clips(["20260728100000"], where="import")       # the other clip
        ok, why = P.copy_still_exists(self.w.ctx)
        self.assertTrue(ok, why)


# ---------------------------------------------------------------------------
# 8) Clean Workspace: the site decides when it can be asked, and otherwise
# every check that CAN answer must say yes
# ---------------------------------------------------------------------------

def _world(renders_here=1, expected=3, bucket=None, published=M.Evidence.NA):
    """A world built by hand, with no filesystem anywhere near it.

    This is what the move off the disk bought: the guard is a pure function, so
    a test states the evidence directly instead of arranging a fixture tree and
    hoping it produces the state it meant.
    """
    return W.World(
        renders_here=tuple(W.Render("trip_%d.mp4" % i, 64) for i in range(renders_here)),
        expected_trips=expected,
        bucket=bucket if bucket is not None else W.NoBucket(),
        site=W.SiteFacts(published=published))


class TestWorkspaceRefusesWhenNothingElseCanDecide(GuardTest):
    """'Noted, not blocking — is-complete.py decides' is only honest when
    is-complete.py will actually run. With no site repo it never does, so the
    under-rendered branches must refuse rather than defer to nothing — the
    rmtree would erase footage of trips that were never encoded.

    Deliberately NOT "the last applicable check wins". Under that reading the
    bucket has the last word when there is no site, and it says yes about the
    renders that exist while saying nothing at all about the trips that were
    never encoded — whose footage exists in no render, no bucket, nowhere.
    """

    def test_under_rendered_import_refuses_without_a_site(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=1, expected=3))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("rendered locally", verdict.reason)

    def test_unreadable_grouping_refuses_without_a_site(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=1, expected=None))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("unknown", verdict.reason)

    def test_a_full_bucket_does_not_excuse_an_under_rendered_import(self):
        """The divergence that made the fold worth checking by enumeration.

        One mp4 exists, three trips were expected, there is no site_repo, and
        that one mp4 IS on the bucket at a matching size. The two trips that
        were never encoded exist nowhere. This must refuse.
        """
        listed = W.Listed({"v/trip_0.mp4": 64})
        verdict = guards.workspace_is_expendable(
            _world(renders_here=1, expected=3, bucket=listed))
        self.assertTrue(verdict.blocked, verdict.reason)

    def test_the_site_has_the_last_word_when_it_can_be_asked(self):
        """is-complete.py asks what the live site actually serves, which is the
        only question that matters — so a short local count is commentary once
        it can run, and its NO is decisive even when everything else says yes."""
        yes = guards.workspace_is_expendable(
            _world(renders_here=1, expected=3, published=M.Evidence.YES))
        self.assertFalse(yes.blocked, yes.reason)
        no = guards.workspace_is_expendable(
            _world(renders_here=3, expected=3, bucket=W.Listed({}),
                   published=M.Evidence.NO))
        self.assertTrue(no.blocked, no.reason)

    def test_an_unlistable_bucket_is_not_a_yes(self):
        """Fails closed: "could not find out" is not "it is there"."""
        verdict = guards.workspace_is_expendable(
            _world(renders_here=3, expected=3, bucket=W.Unlistable()))
        self.assertTrue(verdict.blocked, verdict.reason)

    def test_everything_proven_locally_is_expendable(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=3, expected=3))
        self.assertFalse(verdict.blocked, verdict.reason)


class TestNothingRenderedIsBelowTheSitesWordEntirely(GuardTest):
    """A yes from is-complete.py cannot vouch for an import it never saw.

    is-complete.py walks the trip metas under <out>, and those outlive every
    sweep — so on a machine that published last month it reports last month's
    trips all live while today's import has not been encoded at all. The
    site's word is decisive about SHORT counts, which is the case the deferral
    was written for; it is not evidence about footage that produced no render
    for it to look at.
    """

    def test_a_published_site_does_not_excuse_an_import_with_no_renders(self):
        """Import today, sidecars, no render, then Clean Workspace. Every gate
        above is vacuously happy: the bucket "covers" an empty render list and
        is-complete.py is talking about a previous round. The rmtree would
        take footage that exists in no render, no bucket and on no site."""
        verdict = guards.workspace_is_expendable(
            _world(renders_here=0, expected=3, bucket=W.Listed({}),
                   published=M.Evidence.YES))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("nothing from this import was rendered", verdict.reason)

    def test_it_refuses_before_the_site_is_consulted_at_all(self):
        """Not "the site said no" — the site is not what decided. Asserted on
        an UNKNOWN grouping too, so the refusal cannot be read as the count
        comparison happening to fail."""
        for expected in (None, 0, 3):
            with self.subTest(expected=expected):
                verdict = guards.workspace_is_expendable(
                    _world(renders_here=0, expected=expected,
                           published=M.Evidence.YES))
                self.assertTrue(verdict.blocked, verdict.reason)

    def test_the_gate_table_and_the_verdict_agree(self):
        """The screen printed "rendered locally .... no" and then proceeded to
        the CLEAN prompt. A gate the operator can read must be a gate."""
        world = _world(renders_here=0, expected=3, published=M.Evidence.YES)
        readings = dict(guards.gate_readings(world))
        self.assertIs(readings["rendered locally"], M.Evidence.NO)
        self.assertTrue(guards.workspace_is_expendable(world).blocked)


# ---------------------------------------------------------------------------
# import_is_expendable — freed by the arc removal and kept as 9) Delete SIM
# Data's advisory: erasing the card is allowed on the strength of a copy being
# here, and if that copy is not published it becomes the only one.
# ---------------------------------------------------------------------------

class TestImportIsExpendable(GuardTest):

    def test_nothing_rendered_is_not_expendable(self):
        ok, why = P.import_is_expendable(self.w.ctx, self.w.ctx.render_root)
        self.assertFalse(ok)
        self.assertIn("nothing from it was rendered", why)

    def test_rendered_and_uploaded_is_expendable(self):
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.bucket({"x/trip_A_h1080.mp4": 321})
        ok, why = P.import_is_expendable(self.w.ctx, self.w.ctx.render_root)
        self.assertTrue(ok, why)

    def test_rendered_but_missing_from_the_bucket_is_not(self):
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.bucket({})
        ok, why = P.import_is_expendable(self.w.ctx, self.w.ctx.render_root)
        self.assertFalse(ok)
        self.assertIn("not on S3", why)


# ---------------------------------------------------------------------------
# The sequence every destructive item inherits: show, ask for the word,
# RE-DERIVE the world, re-ask the guard, act. Driven end to end against the
# card, because it is the one target with no second copy.
# ---------------------------------------------------------------------------

class TestTheDestructiveSequence(GuardTest):

    def setUp(self):
        super().setUp()
        self.w.ctx.results = []
        # One clip, on the card AND in the workspace, so erasing the card is
        # allowed: its footage is provably somewhere else.
        for base in (self.w.ctx.card, self.w.ctx.render_root):
            d = base / "DCIM" / "200video" / "front"
            d.mkdir(parents=True, exist_ok=True)
            (d / "20260728090000_0060.mp4").write_text("clip")
        P.write_ledger(self.w.ctx, "20260728090000")
        self.clip = (self.w.ctx.card / "DCIM" / "200video" / "front"
                     / "20260728090000_0060.mp4")
        self.item = M.build_menu(M.Strategy.of(self.w.ctx.uploader),
                                 P.Work(self.w.ctx))[M.ERASE_CARD]

    def _run(self, answer):
        with mock.patch.object(P, "ask", return_value=answer):
            return self.item.execute(P.capture_world(self.w.ctx, M.Scope.FULL))

    def test_the_wrong_word_erases_nothing_and_does_not_complete(self):
        """Not completing is what leaves the position where it was — the
        owner's rule 3, and the only thing that makes a cancel a cancel."""
        outcome = self._run("yes")
        self.assertFalse(outcome.completed)
        self.assertFalse(self.item.completed())
        self.assertTrue(self.clip.is_file(), "the footage must survive a cancel")

    def test_the_word_erases_the_files_and_keeps_the_folders(self):
        outcome = self._run("ERASE")
        self.assertTrue(outcome.completed, outcome.note)
        self.assertFalse(self.clip.exists())
        self.assertTrue((self.w.ctx.card / "DCIM" / "200video" / "front").is_dir(),
                        "the camera writes into these and expects them to exist")

    def test_a_second_run_does_not_reach_the_prompt(self):
        """The idempotence invariant, as behaviour rather than as intent.

        evaluate() answers SATISFIED on an already-empty card, and SATISFIED
        must mean the body never runs — otherwise the operator is asked to
        type ERASE to find out there is nothing behind it.
        """
        self._run("ERASE")
        with mock.patch.object(P, "ask", side_effect=AssertionError(
                "an already-satisfied item must not prompt")):
            outcome = self.item.execute(P.capture_world(self.w.ctx, M.Scope.FULL))
        self.assertTrue(outcome.completed)
        self.assertIn("nothing to erase", outcome.note)

    def test_the_guard_is_re_asked_against_a_world_captured_after_the_word(self):
        """The refresh point, proven rather than asserted in a comment.

        The evidence here is "the clip is in the workspace". Something removes
        the workspace while the prompt is on screen; the re-check must see
        that and refuse, having erased nothing.
        """
        import shutil as _sh

        def answer_and_move_the_world(_prompt, *a, **k):
            _sh.rmtree(str(self.w.ctx.render_root / "DCIM"))
            return "ERASE"

        with mock.patch.object(P, "ask", side_effect=answer_and_move_the_world):
            outcome = self.item.execute(P.capture_world(self.w.ctx, M.Scope.FULL))
        self.assertFalse(outcome.completed)
        self.assertIn("refused after re-check", outcome.note)
        self.assertTrue(self.clip.is_file(), "nothing may be erased after a refusal")

    def test_every_destructive_plan_carries_a_callable_re_check(self):
        """A plan with no guard used to be constructible, and crashed at the
        moment it mattered — after the operator had typed the word."""
        plan = M.Plan.nothing_to_do("nothing")
        self.assertTrue(callable(plan.guard))
        self.assertTrue(callable(plan.act))


# ---------------------------------------------------------------------------
# The bucket listing, as a type. Everything above reaches it through
# working_area_is_expendable, which has its own copy of the matching rule in
# pipeline.py; these ask world.Listed directly, because it is what the ITEMS
# judge and a rule that holds in one implementation and not the other is the
# gap that lets a wipe through.
# ---------------------------------------------------------------------------

class TestWhatTheBucketVouchesFor(unittest.TestCase):
    """An object in the bucket stands in for footage on disk, so what counts
    as "the same object" is the whole strength of that promise."""

    def test_a_key_only_matches_on_a_path_boundary(self):
        """sometrip_A.mp4 must not vouch for trip_A.mp4.

        A bare endswith let a longer name satisfy a shorter one, and the
        render whose only other copy was the workspace then read as published.
        """
        listed = W.Listed({"videos/sometrip_A.mp4": 500})
        self.assertIs(listed.holds("trip_A.mp4", 500), M.Evidence.NO)

    def test_the_same_name_under_a_prefix_does_match(self):
        """The other half of the rule: a real key is prefixed by its folder,
        so the boundary must admit videos/trip_A.mp4 for trip_A.mp4."""
        listed = W.Listed({"videos/trip_A.mp4": 500})
        self.assertIs(listed.holds("trip_A.mp4", 500), M.Evidence.YES)

    def test_a_copy_of_a_different_size_is_not_the_same_object(self):
        """Size is part of identity here. A truncated or half-uploaded object
        carries the right name and not the right footage, and name alone would
        declare the local copy expendable."""
        listed = W.Listed({"videos/trip_A.mp4": 499})
        self.assertIs(listed.holds("trip_A.mp4", 500), M.Evidence.NO)

    def test_covering_a_set_of_renders_needs_every_one_of_them(self):
        listed = W.Listed({"videos/trip_A.mp4": 1})
        both = (W.Render("trip_A.mp4", 1), W.Render("trip_B.mp4", 1))
        self.assertIs(listed.covers(both), M.Evidence.NO)
        self.assertIs(listed.covers(both[:1]), M.Evidence.YES)


class TestWhatIsStillOwedToTheBucket(unittest.TestCase):
    """uploads_outstanding is what makes an interrupted upload resumable, and
    what decides whether 7) Upload Website has anything left to do."""

    def test_a_render_the_bucket_holds_at_the_wrong_size_is_still_owed(self):
        """The resume case. An upload cut off half way leaves a short object
        under the right key; calling that done strands the rest of the file
        and leaves 8) Clean Workspace looking at proof that is not there.
        """
        world = W.World(renders=(W.Render("trip_A.mp4", 500),),
                        bucket=W.Listed({"videos/trip_A.mp4": 250}))
        self.assertEqual(len(guards.uploads_outstanding(world)), 1)

    def test_a_render_the_bucket_holds_whole_is_settled(self):
        world = W.World(renders=(W.Render("trip_A.mp4", 500),),
                        bucket=W.Listed({"videos/trip_A.mp4": 500}))
        self.assertEqual(guards.uploads_outstanding(world), ())

    def test_a_bucket_that_could_not_be_listed_owes_everything(self):
        """Fails closed: "could not find out" is not "it is already there"."""
        world = W.World(renders=(W.Render("trip_A.mp4", 500),),
                        bucket=W.Unlistable())
        self.assertEqual(len(guards.uploads_outstanding(world)), 1)


class TestTheLocalRenderGateHasNoWayToAbstain(unittest.TestCase):
    """rendered_locally is the hard floor under the unanimity rule.

    workspace_is_expendable defers to the site when the site can answer, and
    otherwise every gate that CAN answer must say yes. That sentence is only
    total because this gate never answers "not applicable" — if it could
    abstain, an import with nothing rendered would leave the bucket gate
    talking about the renders that exist while saying nothing about the trips
    that were never encoded, whose footage is in the workspace and nowhere
    else.
    """

    def test_an_import_with_no_renders_at_all_answers_no(self):
        self.assertIs(guards.rendered_locally(_world(renders_here=0)),
                      M.Evidence.NO)

    def test_a_grouping_that_could_not_be_read_answers_unknown_not_yes(self):
        self.assertIs(guards.rendered_locally(_world(renders_here=1, expected=None)),
                      M.Evidence.UNKNOWN)

    def test_it_never_answers_not_applicable(self):
        """Asserted over every shape of world the capture can produce, because
        one abstention is all the unanimity rule needs to be waved through."""
        for renders_here in (0, 1, 3):
            for expected in (None, 0, 3):
                with self.subTest(renders=renders_here, expected=expected):
                    reading = guards.rendered_locally(
                        _world(renders_here=renders_here, expected=expected))
                    self.assertTrue(reading.applicable,
                                    "the floor abstained, so nothing holds the rule up")


if __name__ == "__main__":
    unittest.main(verbosity=2)
