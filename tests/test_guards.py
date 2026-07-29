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
import uploader as U             # noqa: E402
import world as W                # noqa: E402


def target(holds=(), published=None, unreachable=False, configured=True):
    """What a publishing target said, as the world carries it.

    Written down rather than arranged, which is the whole point of freezing the
    answers at capture: what the guard sees is data a test can state.
    """
    if unreachable:
        answers = U.Answers.unknown("could not reach the target")
        return W.TargetFacts(configured=True, name="target", holds=answers,
                             published=answers, carried=answers,
                             owed=U.Owed.everything((), "could not reach the target"))
    return W.TargetFacts(
        configured=configured, name="target",
        holds=U.Answers.of({n: M.Evidence.YES for n in holds}),
        published=(U.Answers.of({}) if published is None
                   else U.Answers.of(published)))


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
        self.ctx.offline = False
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

    def target_holds(self, *names):
        """The configured target says it holds these render names.

        Whether "holds" means an object in a bucket at a matching size, a file
        in a folder, or something else entirely is the implementation's rule
        and is tested where it lives. What the exporter's guard reads is the
        reading, and that is what a test states here.
        """
        self.target = target(holds=names)
        return self.target

    def target_unreachable(self):
        self.target = target(unreachable=True)
        return self.target

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()
        self.w.target = W.TargetFacts()      # the local edition unless a test says otherwise

    def tearDown(self):
        self.w.cleanup()


# ---------------------------------------------------------------------------
# working_area_is_expendable — the predicate 8) Clean Workspace obeys
# ---------------------------------------------------------------------------

class TestWorkingAreaIsExpendable(GuardTest):

    def expendable(self):
        return P.working_area_is_expendable(self.w.ctx, self.w.target)

    def test_empty_workspace_is_expendable(self):
        ok, why, strag = self.expendable()
        self.assertTrue(ok, why)
        self.assertEqual(strag, [])

    def test_render_neither_published_nor_gathered_blocks(self):
        self.w.render("trip_2026-07-28_08-57_01")
        ok, why, strag = self.expendable()
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)
        self.assertIn("neither published nor gathered", why)

    def test_gathered_render_is_expendable(self):
        self.w.render("trip_A", size=2048)
        self.w.gathered("trip_A", size=2048)
        ok, why, _ = self.expendable()
        self.assertTrue(ok, why)

    def test_gathered_copy_of_a_different_size_does_not_count(self):
        """A re-render has the same NAME as the stale copy in final_.

        Matching on name alone declared it expendable and deleted the new file —
        the one gather_into_final refuses to overwrite so it can be looked at.
        This is the half of the size rule that stayed here, because a final_
        folder is this machine's and no target is asked about it.
        """
        self.w.render("trip_A", size=4096)          # re-rendered, new bytes
        self.w.gathered("trip_A", size=1024)        # the older, smaller copy
        ok, _, strag = self.expendable()
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_a_render_the_target_holds_is_expendable(self):
        self.w.render("trip_A", size=777)
        self.w.target_holds("trip_A_h1080.mp4")
        ok, why, _ = self.expendable()
        self.assertTrue(ok, why)

    def test_a_render_the_target_does_not_answer_about_blocks(self):
        """The target spoke, and said nothing about this one. Not published —
        unknown, which is not permission to sweep the only local copy."""
        self.w.render("trip_A", size=777)
        self.w.target_holds("trip_B_h1080.mp4")
        ok, _, strag = self.expendable()
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_an_unreachable_target_fails_closed(self):
        self.w.render("trip_A")
        self.w.target_unreachable()
        ok, _, strag = self.expendable()
        self.assertFalse(ok, "an unreachable target must not read as 'published'")
        self.assertEqual(len(strag), 1)

    def test_previews_and_caches_never_block(self):
        (self.w.ctx.out_dir / "previews").mkdir()
        (self.w.ctx.out_dir / "previews" / "still.jpg").write_text("jpg")
        (self.w.ctx.out_dir / ".scan_cache.json").write_text("{}")
        ok, why, _ = self.expendable()
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

def _renders(n):
    return tuple(W.Render("trip_%d.mp4" % i, 64) for i in range(n))


def _answers(renders, evidence):
    if evidence is M.Evidence.NA:
        return U.Answers.not_applicable("this target does not answer that")
    return U.Answers.of({r.name: evidence for r in renders})


def _world(renders_here=1, expected=3, held=M.Evidence.NA,
           published=M.Evidence.NA, unreachable=False):
    """A world built by hand, with no filesystem anywhere near it.

    This is what the move off the disk bought — and what freezing the target's
    answers at capture preserves: the guard is a pure function, so a test
    states the evidence directly instead of arranging a fixture tree, or a
    fake network, and hoping it produces the state it meant.
    """
    renders = _renders(renders_here)
    if unreachable:
        unknown = U.Answers.unknown("could not reach the target")
        facts = W.TargetFacts(configured=True, name="target", holds=unknown,
                              published=unknown, carried=unknown)
    else:
        facts = W.TargetFacts(configured=held is not M.Evidence.NA
                              or published is not M.Evidence.NA,
                              name="target",
                              holds=_answers(renders, held),
                              published=_answers(renders, published))
    return W.World(renders_here=renders, expected_trips=expected, target=facts)


class TestWorkspaceRefusesWhenNothingElseCanDecide(GuardTest):
    """'Noted, not blocking — the target decides' is only honest when a target
    will actually answer. With none configured it never does, so the
    under-rendered branches must refuse rather than defer to nothing — the
    rmtree would erase footage of trips that were never encoded.

    Deliberately NOT "the last applicable check wins". Under that reading the
    held-at-the-destination gate has the last word when nothing can say what
    is served, and it says yes about the renders that exist while saying
    nothing at all about the trips that were never encoded — whose footage
    exists in no render, at no destination, nowhere.
    """

    def test_under_rendered_import_refuses_with_no_target(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=1, expected=3))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("rendered locally", verdict.reason)

    def test_unreadable_grouping_refuses_with_no_target(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=1, expected=None))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("unknown", verdict.reason)

    def test_a_target_holding_everything_does_not_excuse_an_under_rendered_import(self):
        """The divergence that made the fold worth checking by enumeration.

        One mp4 exists, three trips were expected, nothing can say what is
        served, and that one mp4 IS at the destination. The two trips that
        were never encoded exist nowhere. This must refuse.
        """
        verdict = guards.workspace_is_expendable(
            _world(renders_here=1, expected=3, held=M.Evidence.YES))
        self.assertTrue(verdict.blocked, verdict.reason)

    def test_being_served_has_the_last_word_when_it_can_be_asked(self):
        """What the destination actually SERVES is the only question that
        matters — so a short local count is commentary once it can be
        answered, and a NO is decisive even when everything else says yes."""
        yes = guards.workspace_is_expendable(
            _world(renders_here=1, expected=3, published=M.Evidence.YES))
        self.assertFalse(yes.blocked, yes.reason)
        no = guards.workspace_is_expendable(
            _world(renders_here=3, expected=3, held=M.Evidence.YES,
                   published=M.Evidence.NO))
        self.assertTrue(no.blocked, no.reason)

    def test_a_target_that_could_not_answer_is_not_a_yes(self):
        """Fails closed: "could not find out" is not "it is there"."""
        verdict = guards.workspace_is_expendable(
            _world(renders_here=3, expected=3, unreachable=True))
        self.assertTrue(verdict.blocked, verdict.reason)

    def test_everything_proven_locally_is_expendable(self):
        verdict = guards.workspace_is_expendable(_world(renders_here=3, expected=3))
        self.assertFalse(verdict.blocked, verdict.reason)


class TestNothingRenderedIsBelowTheTargetsWordEntirely(GuardTest):
    """A yes from the target cannot vouch for an import it never saw.

    A target answers about the trips it knows, and the local metas under <out>
    outlive every sweep — so on a machine that published last month it reports
    last month's trips all live while today's import has not been encoded at
    all. Its word is decisive about SHORT counts, which is the case the
    deferral was written for; it is not evidence about footage that produced
    no render for it to look at.

    This is also requirement A of the trust model, as a test: an
    implementation is believed about what it says, and it still cannot talk
    this into erasing an import that produced no renders, because the question
    is never delegated.
    """

    def test_a_target_saying_everything_is_served_does_not_excuse_no_renders(self):
        """Import today, sidecars, no render, then Clean Workspace. Every gate
        below the floor is vacuously happy: an empty render list is "covered"
        by anything, and the target is talking about a previous round. The
        rmtree would take footage that exists in no render and at no
        destination."""
        verdict = guards.workspace_is_expendable(
            _world(renders_here=0, expected=3, held=M.Evidence.YES,
                   published=M.Evidence.YES))
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("nothing from this import was rendered", verdict.reason)

    def test_it_refuses_before_the_target_is_consulted_at_all(self):
        """Not "the target said no" — the target is not what decided. Asserted
        on an UNKNOWN grouping too, so the refusal cannot be read as the count
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

    def expendable(self):
        return P.import_is_expendable(self.w.ctx, self.w.ctx.render_root, self.w.target)

    def test_nothing_rendered_is_not_expendable(self):
        ok, why = self.expendable()
        self.assertFalse(ok)
        self.assertIn("nothing from it was rendered", why)

    def test_rendered_and_held_at_the_destination_is_expendable(self):
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.target_holds("trip_A_h1080.mp4")
        ok, why = self.expendable()
        self.assertTrue(ok, why)

    def test_rendered_but_not_confirmed_there_is_not(self):
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.target_holds()                  # the target answered about nothing
        ok, why = self.expendable()
        self.assertFalse(ok)
        self.assertIn("not confirmed", why)

    def test_with_no_target_the_render_alone_settles_it(self):
        """The local edition has no destination to confirm anything, and this
        is an advisory rather than a gate — so it says what it can see, which
        is that the trip was rendered."""
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        ok, why = self.expendable()
        self.assertTrue(ok, why)


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
# What the target's answers mean where the exporter reads them. The rule for
# deciding YES — a whole-name match, a size comparison, whatever "there" means
# for that destination — belongs to the implementation and is tested where it
# lives (tests/test_uploader.py drives the shipped example). What is pinned
# here is the exporter's side: which reading permits what.
# ---------------------------------------------------------------------------

class TestWhatIsStillOwed(unittest.TestCase):
    """Owed is what makes an interrupted upload resumable, and what decides
    whether 7) Upload Website has anything left to do."""

    def test_a_target_that_could_not_be_asked_owes_everything(self):
        """Fails closed, and it is the interface that says so rather than each
        call site: a listing that failed proves nothing, so the offer stands
        and the upload itself is what discovers the target is down."""
        renders = (W.Render("trip_A.mp4", 500),)
        owed = U.Owed.everything(renders, "could not reach the target")
        self.assertTrue(owed.any)
        self.assertFalse(owed.certain)

    def test_a_target_that_holds_everything_owes_nothing(self):
        self.assertFalse(U.Owed.nothing().any)


class TestTheLocalRenderGateHasNoWayToAbstain(unittest.TestCase):
    """rendered_locally is the hard floor under the unanimity rule.

    workspace_is_expendable defers to the target when the target can say what
    is served, and otherwise every gate that CAN answer must say yes. That
    sentence is only total because this gate never answers "not applicable" —
    if it could abstain, an import with nothing rendered would leave the
    held-at-the-destination gate talking about the renders that exist while
    saying nothing about the trips that were never encoded, whose footage is
    in the workspace and nowhere else.
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
