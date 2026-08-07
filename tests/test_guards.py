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
sys.path.insert(0, str(REPO / "src"))

from dashcam_exporter import guards, items, menu as M, world as W  # noqa: E402


def target(complete=M.Evidence.YES, configured=True, note="", namespace="import"):
    """What the configured plugin said, as the world carries it.

    Written down rather than arranged, which is the whole point of freezing the
    answer at capture: what the guard sees is data a test can state.

    `namespace` defaults to the fixture's own import directory, because an
    answer with no import behind it is about no trips at all — that is a state
    worth stating on purpose (see the tests that pass namespace="") and a poor
    default for tests about a destination that answered.
    """
    return W.TargetFacts(configured=configured, name="target",
                         origin="target (/a/test/plugin.py:B:U)",
                         complete=complete, namespace=namespace, note=note)


def load_pipeline():
    """Import pipeline.py as a module without running its CLI."""
    sys.argv = ["pipeline.py"]
    from dashcam_exporter import pipeline
    return pipeline


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
        # Its own, never the real one under $HOME: a clean-up test MOVES
        # receipts there, and the next test would read them as evidence
        # about a real card.
        self.ctx.archive_dir = self.root / "archive"
        self.ctx.state_dir = self.root / "state"
        self.ctx.lock_file = self.root / P.LOCK_FILE
        self.ctx.workspace = self.root
        self.ctx.render_root = self.root / "import"
        self.ctx.import_root = self.root / "import"
        self.ctx.card = self.root / "card"
        self.ctx.plugin = None
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
        P.state_path(self.ctx, P.LEDGER_FILE).write_text(
            json.dumps({"through": through}))

    def target_has_everything(self):
        """The configured plugin says every trip of this import is at the
        destination.

        What "at the destination" means — an object in a bucket at a matching
        size, a page on a server, a file in a folder — is the implementation's
        rule and is tested where it lives. What the exporter's guard reads is
        the one reading, and that is what a test states here.
        """
        self.target = target(complete=M.Evidence.YES)
        return self.target

    def target_says_no(self):
        self.target = target(complete=M.Evidence.NO)
        return self.target

    def target_unreachable(self):
        self.target = target(complete=M.Evidence.UNKNOWN,
                             note="could not reach the destination")
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

    def test_a_render_the_destination_vouches_for_is_expendable(self):
        self.w.render("trip_A", size=777)
        self.w.target_has_everything()
        ok, why, _ = self.expendable()
        self.assertTrue(ok, why)

    def test_a_render_no_answer_covers_blocks(self):
        """RESTATED: the plugin used to answer per render and this stated the
        case where it named a different one. It answers about the whole import
        now, so "not covered" is a NO rather than a silence — and the rule is
        the same one: anything short of YES keeps the only local copy."""
        self.w.render("trip_A", size=777)
        self.w.target_says_no()
        ok, _, strag = self.expendable()
        self.assertFalse(ok)
        self.assertEqual(len(strag), 1)

    def test_an_unreachable_target_fails_closed(self):
        self.w.render("trip_A")
        self.w.target_unreachable()
        ok, _, strag = self.expendable()
        self.assertFalse(ok, "an unreachable target must not read as 'published'")
        self.assertEqual(len(strag), 1)

    def test_an_answer_about_another_import_does_not_clear_these_renders(self):
        """<out> holds a NAMESPACE PER IMPORT and this sweep walks all of them,
        while the plugin is asked about the trips of ONE.

        Read without its scope, a yes about this round authorises deleting last
        round's renders — footage that was never in the question and, once the
        card has been erased, may exist nowhere else. The renders of an import
        nobody was asked about are stragglers, which costs disk and keeps the
        only copy.
        """
        self.w.render("trip_B", ns="last-month")
        self.w.target = target(complete=M.Evidence.YES, namespace="import")
        ok, why, strag = self.expendable()
        self.assertFalse(ok, why)
        self.assertEqual([p.name for p in strag], ["trip_B_h1080.mp4"])

    def test_an_answer_about_no_import_at_all_clears_nothing(self):
        """With several imports and none picked, the world is about no import,
        the trip list handed over is empty, and an implementation that folds an
        empty list to YES is saying nothing was missing from nothing. That must
        not clear a render."""
        self.w.render("trip_A")
        self.w.target = target(complete=M.Evidence.YES, namespace="")
        ok, why, strag = self.expendable()
        self.assertFalse(ok, why)
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

class TestAClipWithNoSecondCopyStopsTheSweep(unittest.TestCase):
    """The floor the other two cannot see.

    They reason about TRIPS: how many were rendered, and whether the
    destination has them. A trip too short to render gets no sidecar, so it is
    in no trip_ids and counts toward no expected_trips — the local count is met
    by the renders that do exist, and the destination answers YES honestly
    about the trips it was asked about. Its footage then went with the sweep.

    Reachable in the order the graph calls SAFE: erase the card while its clips
    are provably in the workspace, then clean once the renders are published.
    Sound for a trip that was rendered, silently false for one that never will
    be.
    """

    def _world(self, orphans=()):
        return W.World(
            out_dir=Path("/ws"), imports=(Path("/ws/import"),),
            selected_import=Path("/ws/import"),
            renders=tuple(W.Render("t%d.mp4" % i, 10) for i in range(4)),
            renders_here=tuple(W.Render("t%d.mp4" % i, 10) for i in range(4)),
            expected_trips=4, trip_ids=("t0", "t1", "t2", "t3"),
            card=W.Card(path=Path("/card"), dcim=True, present=False),
            target=W.TargetFacts(configured=True, name="site",
                                 complete=M.Evidence.YES, namespace="import"),
            orphan_clips=orphans)

    def test_the_sweep_is_refused(self):
        verdict = guards.Gates(self._world(("20260728155513",))).clean_is_allowed()
        self.assertTrue(verdict.blocked)
        self.assertIn("nowhere else", verdict.reason)

    def test_the_clips_are_named_rather_than_counted(self):
        """He decides whether footage matters by looking at it, and a headcount
        is not something anyone can act on."""
        verdict = guards.Gates(self._world(("20260728155513",))).clean_is_allowed()
        self.assertEqual(verdict.evidence, ("20260728155513",))

    def test_everything_else_unchanged_still_goes_through(self):
        self.assertFalse(guards.Gates(self._world()).clean_is_allowed().blocked)

    def test_excluding_them_is_what_lifts_it(self):
        """Not a flag that skips the gate: excluding records the decision, and
        orphan_clips is derived with the exclusions subtracted."""
        self.assertFalse(guards.Gates(self._world(())).clean_is_allowed().blocked)


class TestPurgeKeepsState(GuardTest):
    """RESTATED. Two of these asserted the receipts stay where they were.

    They move now, to the archive outside every working area, and the sweep
    then takes everything. Sparing them in place left the output tree holding
    files that read as trips waiting to be rendered when what they record is
    that the work is done — and the state they carry is unchanged, so what is
    pinned here is where it went, not whether it survived.
    """

    def test_moves_the_receipts_out_and_keeps_the_ledger(self):
        d = self.w.render("trip_A", size=4096)
        (d / "trip_A.gpx").write_text("gpx")
        (d / "trip_A.html").write_text("html")
        (self.w.ctx.out_dir / "previews").mkdir()
        (self.w.ctx.out_dir / "previews" / "s.jpg").write_text("jpg")
        self.w.ledger("20260728000000")

        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)

        self.assertTrue((self.w.ctx.state_dir / P.LEDGER_FILE).is_file(),
                        "the ledger lives outside the swept tree now")
        left = sorted(p.name for p in self.w.ctx.out_dir.iterdir())
        self.assertEqual(left, [],
                         "the render namespace of a swept import goes with it")
        self.assertFalse((d / "trip_A_meta.json").exists(), "the receipt must not stay here")
        self.assertEqual(P.archived_trips(self.w.ctx), 1, "the receipt must be archived")
        self.assertFalse((d / "trip_A_h1080.mp4").exists(), "the render must go")
        self.assertFalse((d / "trip_A.gpx").exists())
        self.assertFalse((self.w.ctx.out_dir / "previews").exists())

    def test_the_import_namespace_is_emptied_too(self):
        """The render namespace is named after the import dir, so it hits the
        branch that empties the import folder. Its receipt is archived like any
        other — that branch used to need its own exemption to spare them, and
        the exemption is gone with the reason for it."""
        d = self.w.render("trip_A", ns=self.w.ctx.render_root.name)
        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)
        self.assertFalse((d / "trip_A_meta.json").exists())
        self.assertEqual(P.archived_trips(self.w.ctx), 1)
        self.assertFalse((d / "trip_A_h1080.mp4").exists())

    def test_a_symlink_is_unlinked_and_never_walked(self):
        """The tool makes no symlinks. Someone else's may be in the tree --
        pointing the output at a folder that is really somewhere else is an
        ordinary thing to do -- and the sweep deletes file by file, so walking
        one would delete THROUGH it into a tree this tool was never pointed at.

        The link is removed as the name it is. What it pointed at is not ours.
        """
        elsewhere = self.w.root / "not-ours"
        (elsewhere / "keep").mkdir(parents=True)
        (elsewhere / "keep" / "family.jpg").write_text("irreplaceable")
        (self.w.ctx.out_dir / "shortcut").symlink_to(elsewhere)

        P.purge_published_renders(self.w.ctx, self.w.ctx.render_root)

        self.assertTrue((elsewhere / "keep" / "family.jpg").is_file(),
                        "the sweep deleted through a symlink")
        self.assertFalse((self.w.ctx.out_dir / "shortcut").exists(),
                         "the link itself is a name in the working area")

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


def _world(renders_here=1, expected=3, complete=M.Evidence.NA):
    """A world built by hand, with no filesystem anywhere near it.

    This is what the move off the disk bought — and what freezing the plugin's
    answer at capture preserves: the guard is a pure function, so a test states
    the evidence directly instead of arranging a fixture tree, or a fake
    network, and hoping it produces the state it meant.
    """
    return W.World(renders_here=_renders(renders_here), expected_trips=expected,
                   target=target(complete=complete,
                                 configured=complete is not M.Evidence.NA))


class TestTheTripCountIsSomethingWeKnow(unittest.TestCase):
    """WE do the rendering, so how many trips there are is not a question that
    needs asking twice.

    It used to come only from this session's cached grouping — a boundary scan
    that walks the video and costs minutes, so a capture is not allowed to
    start one. A fresh launch therefore had no number, the local render gate
    answered UNKNOWN, and Clean Workspace refused until a scan had been run
    again. That is the exporter failing to know something entirely its own.

    Generate Meta writes one sidecar per trip. They are on disk and already
    listed, so they answer it.
    """

    def _metas(self, n):
        return tuple(W.TripMeta("trip_%d" % i, "2026", "2026") for i in range(n))

    def test_the_sidecars_answer_when_no_scan_has_run_this_session(self):
        ctx = mock.Mock()
        ctx.last_groups = None
        self.assertEqual(P._expected_trips(ctx, Path("/w/import"), self._metas(6)), 6)

    def test_a_grouping_from_this_session_still_wins(self):
        """It is the authority the sidecars were written from; its absence is
        what stopped being a reason to refuse, not its answer."""
        root = Path("/w/import")
        ctx = mock.Mock()
        ctx.last_groups = (root, {"trips": [{"renderable": True}] * 4})
        self.assertEqual(P._expected_trips(ctx, root, self._metas(6)), 4)

    def test_the_gate_no_longer_abstains_on_a_fresh_session(self):
        """The symptom, stated as the gate's answer: six sidecars, six renders,
        no scan — yes, not unknown."""
        world = W.World(renders_here=_renders(6), expected_trips=6)
        self.assertIs(guards.Gates(world).rendered_locally(), M.Evidence.YES)


class TestWorkspaceRefusesWhenNothingElseCanDecide(GuardTest):
    """'Noted, not blocking — the destination decides' is only honest when a
    destination will actually answer. With none configured it never does, so
    the under-rendered branches must refuse rather than defer to nothing — the
    rmtree would erase footage of trips that were never encoded.

    RESTATED for the narrowed interface. There used to be two destination gates
    and this class enumerated which of them decided in which state; there is
    one question now, so what is pinned is the shape that survived: the floors
    first, then one answer, and NA drops that answer's gate rather than passing
    it.
    """

    def test_under_rendered_import_refuses_with_no_target(self):
        verdict = guards.Gates(
            _world(renders_here=1, expected=3)).workspace_is_expendable()
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("rendered locally", verdict.reason)

    def test_unreadable_grouping_refuses_with_no_target(self):
        verdict = guards.Gates(
            _world(renders_here=1, expected=None)).workspace_is_expendable()
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("unknown", verdict.reason)

    def test_a_destination_that_has_everything_does_not_excuse_a_short_count(self):
        """The floor, and the reason it stays.

        One mp4 exists where three trips were expected, and the destination
        says every trip of this import is there. It still refuses: the trip
        LIST is read off the sidecars, so a trip whose sidecar was never
        written is in nobody's question, and an unreadable grouping means the
        count cannot be compared against anything at all.
        """
        world = _world(renders_here=1, expected=3, complete=M.Evidence.YES)
        verdict = guards.Gates(world).workspace_is_expendable()
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("rendered locally", verdict.reason)

    def test_a_no_from_the_destination_is_decisive_against_the_erase(self):
        """A NO stops it even when everything local agrees. That half of the
        rule is unchanged."""
        world = _world(renders_here=3, expected=3, complete=M.Evidence.NO)
        no = guards.Gates(world).workspace_is_expendable()
        self.assertTrue(no.blocked, no.reason)

    def test_a_destination_that_could_not_answer_is_not_a_yes(self):
        """Fails closed: "could not find out" is not "it is there"."""
        world = _world(renders_here=3, expected=3, complete=M.Evidence.UNKNOWN)
        verdict = guards.Gates(world).workspace_is_expendable()
        self.assertTrue(verdict.blocked, verdict.reason)

    def test_not_applicable_drops_the_gate_rather_than_failing_it(self):
        """A plugin may genuinely have no notion of a destination to check —
        an archive disk stores footage and does not publish it. Then the erase
        rests on the local render count alone, and unproven_lines says so."""
        world = _world(renders_here=3, expected=3, complete=M.Evidence.NA)
        self.assertFalse(guards.Gates(world).workspace_is_expendable().blocked)

    def test_everything_proven_locally_is_expendable(self):
        verdict = guards.Gates(
            _world(renders_here=3, expected=3)).workspace_is_expendable()
        self.assertFalse(verdict.blocked, verdict.reason)


class TestNothingRenderedIsBelowTheTargetsWordEntirely(GuardTest):
    """A yes from the destination cannot vouch for an import it never saw.

    Sidecars under <out> outlive every sweep, so a machine that published last
    month can answer confidently about trips it published then while today's
    import has not been encoded at all. Its word is decisive about a
    destination; it is not evidence about footage that produced no render.

    This is also requirement A of the trust model, as a test: an implementation
    is believed about what it says, and it still cannot talk this into erasing
    an import that produced no renders, because the question is never
    delegated.
    """

    def test_a_destination_saying_yes_does_not_excuse_no_renders(self):
        """Import today, sidecars, no render, then Clean Workspace. The gate
        below the floor is vacuously happy — the destination is talking about a
        previous round. The rmtree would take footage that exists in no render
        and at no destination."""
        world = _world(renders_here=0, expected=3, complete=M.Evidence.YES)
        verdict = guards.Gates(world).workspace_is_expendable()
        self.assertTrue(verdict.blocked, verdict.reason)
        self.assertIn("nothing from this import was rendered", verdict.reason)

    def test_it_refuses_before_the_destination_is_consulted_at_all(self):
        """Not "the destination said no" — it is not what decided. Asserted on
        an UNKNOWN grouping too, so the refusal cannot be read as the count
        comparison happening to fail."""
        for expected in (None, 0, 3):
            with self.subTest(expected=expected):
                verdict = guards.Gates(
                    _world(renders_here=0, expected=expected,
                           complete=M.Evidence.YES)).workspace_is_expendable()
                self.assertTrue(verdict.blocked, verdict.reason)

    def test_the_gate_table_and_the_verdict_agree(self):
        """The screen printed "rendered locally .... no" and then proceeded to
        the prompt. A gate the operator can read must be a gate."""
        world = _world(renders_here=0, expected=3, complete=M.Evidence.YES)
        readings = dict(guards.Gates(world).gate_readings())
        self.assertIs(readings["rendered locally"], M.Evidence.NO)
        self.assertTrue(guards.Gates(world).workspace_is_expendable().blocked)


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

    def test_rendered_and_at_the_destination_is_expendable(self):
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.target_has_everything()
        ok, why = self.expendable()
        self.assertTrue(ok, why)

    def test_rendered_but_not_confirmed_there_is_not(self):
        """RESTATED: the advisory used to count renders the plugin had not
        vouched for by name. There is one answer now, so anything short of YES
        makes the whole import unconfirmed — which is what the sentence it
        feeds already said."""
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.target_says_no()
        ok, why = self.expendable()
        self.assertFalse(ok)
        self.assertIn("not confirmed", why)

    def test_an_answer_about_another_import_confirms_nothing_here(self):
        """Item 9's advisory walks EVERY import in the workspace; the plugin was
        asked about one of them.

        Without this the one import that is published silences the warning
        about the one that is not — and that warning is the sentence saying the
        copy on this machine becomes the only one the moment the card goes.
        """
        self.w.render("trip_A", ns=self.w.ctx.render_root.name, size=321)
        self.w.target = target(complete=M.Evidence.YES, namespace="last-month")
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
# 4) Exclude Trip's two sentences about the destination. Neither is a gate;
# both are the last thing an operator reads before an irreversible delete, and
# the world they are read from was captured BEFORE the operator was asked which
# import to drop from. With several in the workspace those differ.
# ---------------------------------------------------------------------------

class TestWhatExcludeTripSaysAboutTheDestination(GuardTest):

    def _world(self, complete, namespace, trip_ids=("trip_A",)):
        return W.World(out_dir=self.w.ctx.out_dir, trip_ids=trip_ids,
                       target=target(complete=complete, namespace=namespace))

    def _banner(self, world, trip_id, ns):
        """The only-copy panel for one picked trip, sitting in namespace `ns`.

        Built by hand rather than driven through the prompt because the point
        is the MISMATCH: the trip on screen belongs to one import and the
        answer in the world is about another, which no single-import fixture
        can express.
        """
        trip = {"day": "2026-06-01", "start": "2026-06-01T08:00:00",
                "end": "2026-06-01T09:00:00", "clips": 1, "files": [],
                "out_base": str(self.w.ctx.out_dir / ns / "2026-06-01" / trip_id)}
        return "\n".join(P._only_copy_lines(self.w.ctx, world,
                                            {"trips": [trip]},
                                            P.Picked({1: trip}, [1])))

    def test_a_trip_of_the_import_that_was_asked_about_is_not_the_only_copy(self):
        """The baseline the mismatch tests are read against: inside the answer's
        own namespace a yes still suppresses the panel, which is what keeps it
        from firing over every published trip anyone cleans up."""
        world = self._world(M.Evidence.YES, "import")
        self.assertNotIn("ONLY copy", self._banner(world, "trip_A", "import"))

    def test_a_trip_of_another_import_still_gets_the_only_copy_panel(self):
        """The answer is about ONE import's trips. Read across that boundary it
        turns "nobody was asked about this" into "it is safely published", in
        the one panel that tells the operator this is the last copy."""
        world = self._world(M.Evidence.YES, "import")
        printed = self._banner(world, "trip_B", "last-month")
        self.assertIn("ONLY copy", printed)
        self.assertIn("no answer covering these trips", printed)

    def test_the_stays_behind_note_needs_the_answer_to_name_these_trips(self):
        """The other sentence, and the other direction of the same mistake:
        telling the operator a copy survives the drop sends him looking for one
        that was never there. world.trip_ids IS the list the plugin was handed,
        so containment in it is the exact test."""
        world = self._world(M.Evidence.YES, "import", trip_ids=("trip_A",))
        self.assertTrue(P._all_at_the_destination(world, ["trip_A"]))
        self.assertFalse(P._all_at_the_destination(world, ["trip_B"]))
        self.assertFalse(P._all_at_the_destination(world, ["trip_A", "trip_B"]))


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
        self.item = M.build_menu(M.Strategy.of(self.w.ctx.plugin),
                                 P.Work(self.w.ctx))[M.ERASE_CARD]

    def _run(self, answer):
        with mock.patch.object(P.prompt, "ask", return_value=answer):
            return self.item.execute(P.capture_world(self.w.ctx, M.Scope.FULL))

    def test_the_wrong_word_erases_nothing_and_does_not_complete(self):
        """Not completing is what leaves the position where it was — the
        owner's rule 3, and the only thing that makes a cancel a cancel."""
        outcome = self._run("yes")
        self.assertFalse(outcome.completed)
        self.assertFalse(self.item.completed())
        self.assertTrue(self.clip.is_file(), "the footage must survive a cancel")

    def test_the_word_takes_the_files_and_leaves_the_folders(self):
        outcome = self._run("DELETE")
        self.assertTrue(outcome.completed, outcome.note)
        self.assertFalse(self.clip.exists())
        dcim = self.w.ctx.card / "DCIM"
        self.assertTrue(dcim.is_dir(), "DCIM is the card-is-here marker")
        self.assertTrue((dcim / "200video" / "front").is_dir(),
                        "the camera writes here and does not create it itself")
        self.assertEqual([f for f in dcim.rglob("*") if f.is_file()], [])

    def test_a_second_run_does_not_reach_the_prompt(self):
        """The idempotence invariant, as behaviour rather than as intent.

        evaluate() answers SATISFIED on an already-empty card, and SATISFIED
        must mean the body never runs — otherwise the operator is asked to
        type DELETE to find out there is nothing behind it.
        """
        self._run("DELETE")
        with mock.patch.object(P.prompt, "ask", side_effect=AssertionError(
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
            return "DELETE"

        with mock.patch.object(P.prompt, "ask", side_effect=answer_and_move_the_world):
            outcome = self.item.execute(P.capture_world(self.w.ctx, M.Scope.FULL))
        self.assertFalse(outcome.completed)
        self.assertIn("Refused after the re-check", outcome.note)
        self.assertTrue(self.clip.is_file(), "nothing may be erased after a refusal")

    def test_every_destructive_plan_carries_a_callable_re_check(self):
        """A plan with no guard used to be constructible, and crashed at the
        moment it mattered — after the operator had typed the word."""
        plan = M.Plan.nothing_to_do("nothing")
        self.assertTrue(callable(plan.guard))
        self.assertTrue(callable(plan.act))


# ---------------------------------------------------------------------------
# What the plugin's answer means where the exporter reads it. The rule for
# deciding YES — a whole-name match, a size comparison, whatever "there" means
# for that destination — belongs to the implementation and is tested where it
# lives (tests/test_uploader.py drives the shipped example). What is pinned
# here is the exporter's side: which reading permits what.
# ---------------------------------------------------------------------------

class TestTheLocalRenderGateHasNoWayToAbstain(unittest.TestCase):
    """rendered_locally is the hard floor under the erase rule.

    Below it there is one question left and the destination's answer decides
    it. That is only safe because this gate never answers "not applicable" — if
    it could abstain, an import with nothing rendered would be judged by a
    destination talking about trips it published last month while today's
    footage is in the workspace and nowhere else.
    """

    def test_an_import_with_no_renders_at_all_answers_no(self):
        self.assertIs(guards.Gates(_world(renders_here=0)).rendered_locally(),
                      M.Evidence.NO)

    def test_a_grouping_that_could_not_be_read_answers_unknown_not_yes(self):
        reading = guards.Gates(
            _world(renders_here=1, expected=None)).rendered_locally()
        self.assertIs(reading, M.Evidence.UNKNOWN)

    def test_it_never_answers_not_applicable(self):
        """Asserted over every shape of world the capture can produce, because
        one abstention is all the floor needs to be waved through."""
        for renders_here in (0, 1, 3):
            for expected in (None, 0, 3):
                with self.subTest(renders=renders_here, expected=expected):
                    world = _world(renders_here=renders_here,
                                   expected=expected)
                    reading = guards.Gates(world).rendered_locally()
                    self.assertTrue(reading.applicable,
                                    "the floor abstained, so nothing holds the rule up")


class TestARefusalNamesWhatItIsAbout(unittest.TestCase):
    """A count is something to take on trust; the files are something to look
    at. "13 new clips ready for next session" says how many; the operator's
    next question is which, because May footage and this morning's footage are
    the same number and a different decision."""

    def _card(self, **kw):
        return W.World(card=W.Card(path=Path("/Volumes/SIM"), dcim=True,
                                   present=True, **kw))

    def test_the_blocking_clips_are_carried_on_the_verdict(self):
        files = ("/Volumes/SIM/DCIM/200video/front/20260502102459_0060.mp4",)
        v = guards.clips_never_copied(
            self._card(new_stamps=frozenset({"20260502102459"}), new_files=files))
        self.assertTrue(v.blocked)
        self.assertEqual(v.evidence, files)

    def test_a_verdict_with_nothing_to_show_carries_nothing(self):
        self.assertEqual(M.blocked("plain").evidence, ())
        self.assertEqual(M.go().evidence, ())


class TestOldFootageIsNotADeadEnd(unittest.TestCase):
    """A card carrying clips older than the last import must stay actionable.

    Thirteen clips from May and July sat below a high-water mark set in
    August. The delta skipped them as already imported; no rendered trip's
    span contained them, so item 9 refused to erase the card because they
    existed nowhere else. Item 1 would not take them and item 9 would not let
    them go, and each was right on its own terms.
    """

    def test_a_clip_below_the_mark_that_nothing_accounts_for_is_still_offered(self):
        old, recent = "20260502102459", "20260731061615"
        stamps = frozenset({old, recent})
        # The mark is past both, so the mark alone offers nothing.
        self.assertEqual(P._never_imported_stamps(stamps, recent, frozenset()),
                         frozenset())
        # Owed says the old one is on the card and nowhere else.
        offered = P.to_import(None, stamps, recent, frozenset(), {old})
        self.assertIn(old, offered)
        self.assertNotIn(recent, offered, "a clip already accounted for was re-offered")

    def test_a_published_clip_is_not_offered_when_the_mark_goes_backwards(self):
        """A discard winds the mark back. With it at zero, every clip on the
        card counted as never imported — including the ones already rendered,
        published and swept, whose receipts are in the archive. 25 GB offered
        where 2.5 GB was wanted."""
        published, stray = "20260730135403", "20260502102459"
        offered = P.to_import(None, frozenset({published, stray}), "",
                              frozenset(), {stray})
        self.assertEqual(offered, frozenset({stray}))

    def test_an_excluded_clip_is_never_re_offered(self):
        """Dropped on purpose is accounted FOR, not owed -- card_accounting
        removes it before owed is computed, so it cannot come back this way."""
        old = "20260502102459"
        offered = P.to_import(None, frozenset({old}), "20260731061615",
                              frozenset({old}), set())
        self.assertEqual(offered, frozenset())


class TestThrowingAwayAnImportNothingWasMadeFrom(unittest.TestCase):
    """The second way into item 8, and the only one that does not go through
    the publish gates.

    It exists because the first way cannot cover the state it is for: an
    import that was interrupted before anything read it has no sidecars, no
    renders and nothing at any destination, so every gate about publishing
    answers "no" and the footage can neither move forward nor be cleared. The
    evidence it stands on instead is that the card in the slot still holds the
    clips, per clip, so what is being deleted is a copy.
    """

    def _import(self, mine=("a.mp4", "b.mp4"), on_card=("a.mp4", "b.mp4"), **rest):
        return W.World(imports=(Path("/w/import"),),
                       import_files=frozenset(mine),
                       unsourced_files=frozenset(mine) - frozenset(on_card),
                       **rest)

    def test_a_bare_import_whose_clips_are_all_on_the_card(self):
        self.assertTrue(guards.Gates(self._import()).import_is_disposable())
        self.assertFalse(guards.Gates(self._import()).clean_is_allowed().blocked)

    def test_one_file_short_and_it_is_not_disposable(self):
        """Per file, because a headcount cannot tell a card holding two of the
        three from a card holding three others -- and because the file that is
        short is as likely to be a rear clip or a GPS tar as a front clip."""
        world = self._import(mine=("a.mp4", "b.mp4", "c.mp4"),
                             on_card=("a.mp4", "b.mp4"))
        self.assertFalse(guards.Gates(world).import_is_disposable())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)

    def test_no_card_at_all_and_it_is_not_disposable(self):
        world = self._import(on_card=())
        self.assertFalse(guards.Gates(world).import_is_disposable())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)

    def test_an_empty_import_is_not_disposable_by_this_route(self):
        """Vacuous truth is the failure mode: no files means no file is
        missing from the card, and "all of them are safe" would be a yes about
        nothing. sidecars_missing already settles the empty case."""
        self.assertFalse(
            guards.Gates(self._import(mine=())).import_is_disposable())

    def test_what_was_made_from_it_does_not_take_this_route_away(self):
        """RESTATED: a sidecar, a render or a gathered folder used to block it.

        The reasoning was that something downstream is built on this footage.
        True, and not what the guard protects: sidecars and stills are derived
        and cost seconds, renders cost hours but come back from the same
        clips. What must never go is footage that exists in one place only,
        and the card holding every file answers exactly that.

        The old rule made a workspace unclearable the moment any step had run
        — scan a card that turns out to be fragments and you could neither use
        it nor empty it without publishing first.
        """
        for made in ({"metas": (W.TripMeta("t", "2026", "2026"),)},
                     {"renders": (W.Render("t.mp4", 10),)},
                     {"final_folders": (Path("/w/out/final_2026-07-28"),)}):
            with self.subTest(**made):
                world = self._import(**made)
                self.assertTrue(guards.Gates(world).import_is_disposable())
                self.assertFalse(guards.Gates(world).clean_is_allowed().blocked)

    def test_but_the_card_must_still_hold_every_file(self):
        """The half that stayed. One file short and no amount of derived work
        makes the workspace expendable."""
        world = self._import(mine=("a.mp4", "b.mp4"), on_card=("a.mp4",),
                             renders=(W.Render("t.mp4", 10),))
        self.assertFalse(guards.Gates(world).import_is_disposable())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)

    def test_the_cheap_half_lets_a_disposable_import_through(self):
        """Item 8's evaluate, which is what decides whether the entry is even
        offered. Blocked there, the operator never reaches the evidence."""
        self.assertIsNone(guards.Gates(self._import()).nothing_to_clean_up())
        self.assertIsNotNone(
            guards.Gates(self._import(on_card=())).nothing_to_clean_up())

    def test_the_refusal_names_the_files(self):
        said = guards.unsourced_lines(self._import(mine=("a.mp4", "b.mp4", "c.mp4"),
                                                   on_card=("a.mp4",)))
        self.assertIn("b.mp4", said[0])
        self.assertIn("c.mp4", said[0])

    def test_a_card_that_is_the_import_vouches_for_nothing(self):
        """The sets say every file is on the card because they were read from
        the same directory twice. Point the card setting at the workspace and
        card_root() walks down to the import\'s own DCIM; the comparison then
        cannot fail, and the folder it approves deleting is the only copy."""
        world = self._import(card_shares_the_import=True)
        self.assertFalse(guards.Gates(world).import_is_disposable())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)
        self.assertIn("card", guards.unsourced_lines(world)[0])


class TestAnImportWithNoClipsLeftCanBeCleared(unittest.TestCase):
    """The dead end item 4 could walk you into.

    Exclude every trip of an import and its clips are deleted, which is what
    excluding means. The card was erased on the strength of those exclusions,
    so the discard path is shut -- and the sweep refused, because an import
    that produced no footage produced no renders either, and "nothing from
    this import was rendered" is the first floor. Nothing left to lose, and no
    way to say so.
    """

    def _world(self, files, renders=0, imports=(Path("/w/import/2026-08-04"),)):
        # unsourced == every file: the card was erased on the strength of the
        # exclusions, which is what shuts the discard path and is the whole
        # reason this state is a dead end. A world without it would answer
        # through import_is_disposable and prove nothing about this guard.
        return W.World(imports=imports,
                       import_files=frozenset(files),
                       unsourced_files=frozenset(files),
                       renders_here=_renders(renders))

    def test_the_entry_is_offered_and_then_allowed(self):
        """Two checks stand in front of item 8: the cheap one that decides
        whether the menu offers it, and the heavy one behind the typed word.
        An entry that is offered and then refuses is a worse screen than one
        that was never offered, and an entry hidden from a state it would have
        allowed is the dead end this whole change is about. They read one
        predicate so they cannot drift."""
        world = self._world(["DCIM/203gps/x.gpx"])
        self.assertIsNone(guards.Gates(world).nothing_to_clean_up())
        self.assertFalse(guards.Gates(world).clean_is_allowed().blocked)

    def test_footage_here_shuts_both_of_them(self):
        world = self._world(["DCIM/200video/front/a.mp4"])
        self.assertIsNotNone(guards.Gates(world).nothing_to_clean_up())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)

    def test_the_card_route_is_genuinely_shut_in_these_worlds(self):
        world = self._world(["DCIM/203gps/x.gpx"])
        self.assertFalse(guards.Gates(world).import_is_disposable(),
                         "these tests would pass through the card path")

    def test_a_workspace_of_sidecars_and_logs_may_go(self):
        world = self._world(["DCIM/203gps/20260731061515_0060.gpx",
                             "DCIM/203gps/tar/0731.git",
                             "DCIM/IPSRecord.txt"])
        self.assertTrue(guards.Gates(world).import_holds_no_footage())
        self.assertFalse(guards.Gates(world).clean_is_allowed().blocked)

    def test_one_clip_still_here_is_still_a_refusal(self):
        """The floor is doing its job the moment there is footage to protect,
        and one clip is enough -- this is the only copy, the card is gone."""
        world = self._world(["DCIM/203gps/20260731061515_0060.gpx",
                             "DCIM/200video/front/20260731061515_0060.mp4"])
        self.assertFalse(guards.Gates(world).import_holds_no_footage())
        self.assertTrue(guards.Gates(world).clean_is_allowed().blocked)

    def test_the_suffix_is_read_whatever_its_case(self):
        world = self._world(["DCIM/200video/front/20260731061515_0060.MP4"])
        self.assertFalse(guards.Gates(world).import_holds_no_footage())

    def test_a_render_on_disk_takes_this_way_out_away(self):
        """Hours of encoding is not sidecars. An empty import is no reason to
        throw a render away, and that is a different question from this one."""
        world = self._world(["DCIM/203gps/20260731061515_0060.gpx"], renders=2)
        self.assertFalse(guards.Gates(world).import_holds_no_footage())

    def test_no_import_at_all_is_not_an_empty_import(self):
        """Nothing selected is not the same fact, and must not read as a yes."""
        world = self._world(["DCIM/203gps/x.gpx"], imports=())
        self.assertFalse(guards.Gates(world).import_holds_no_footage())


if __name__ == "__main__":
    unittest.main(verbosity=2)
