#!/usr/bin/env python3
"""The rules the steps are supposed to obey — as executable statements.

These are written from the SPEC, not from the code, so a failure here is a gap
in the tool rather than a broken test. Run them and the failures are the to-do
list; when a rule is implemented its test turns green and stays green.

Each test names the rule it encodes in its docstring, in the owner's words.

Run:  ./run-tests.sh          (or: python3 -m unittest tests.test_spec -v)
"""

import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dashcam_exporter import items, menu as M, uploader as U  # noqa: E402,F401


def load_pipeline():
    sys.argv = ["pipeline.py"]
    from dashcam_exporter import pipeline
    return pipeline


P = load_pipeline()

# The numbering is imported, never restated. Written out here it was a second
# declaration of the same thing, and it would have gone on passing against the
# old numbers while the tool moved to the new ones.
from dashcam_exporter.domain.menu.menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER,   # noqa: E402
                  BUILD, UPLOAD, CLEAN_WS, ERASE_CARD)


class Bench:
    """A workspace plus the knobs each rule needs to set."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-spec-"))
        (self.root / "out").mkdir()
        (self.root / "import").mkdir()
        self.ctx = P.Ctx.__new__(P.Ctx)
        c = self.ctx
        c.exporter = P.EXPORTER_DIR
        c.cfg = {}
        c.out_dir = self.root / "out"
        c.final_root = self.root
        # Its own, never the real one under $HOME: a clean-up test MOVES
        # receipts there, and the next test would read them as evidence
        # about a real card.
        c.archive_dir = self.root / "archive"
        c.state_dir = self.root / "state"
        c.lock_file = self.root / P.LOCK_FILE
        c.workspace = self.root
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

    def card_in(self, stamps=("20260728090000",)):
        f = self.ctx.card / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (f / ("%s_0060.mp4" % s)).write_text("clip")
        return self

    def imported(self, stamps=("20260728090000",)):
        f = self.ctx.render_root / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (f / ("%s_0060.mp4" % s)).write_text("clip")
        return self

    def gpx(self):
        d = self.ctx.render_root / "DCIM" / "203gps"
        d.mkdir(parents=True, exist_ok=True)
        (d / "track.gpx").write_text("<gpx/>")
        return self

    def sidecars(self, trip="trip_2026-07-28_08-57_01", day="2026-07-28"):
        d = self.ctx.out_dir / self.ctx.render_root.name / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_meta.json")).write_text(json.dumps(
            {"day": day, "start": "2026-07-28 08:57:56", "end": "2026-07-28 14:13:41"}))
        (d / (trip + ".gpx")).write_text("<gpx/>")
        (d / (trip + ".html")).write_text("<html/>")
        return self

    def render(self, trip="trip_2026-07-28_08-57_01", day="2026-07-28", size=1024):
        d = self.ctx.out_dir / self.ctx.render_root.name / day
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_h1080.mp4")).write_bytes(b"x" * size)
        return self

    def publishes(self):
        """A configured publishing plugin — which is what decides the edition.

        The shipped example, loaded the way a real install loads one, rather
        than a stub: these tests are about the wiring between the menu and
        whatever was configured, and a stub lets that wiring drift from the
        one implementation anybody reads. Its destination is a directory, so
        "already published" is a real file a test can put there.
        """
        self.published_dir = self.root / "published"
        os.environ["DASHCAM_LOCAL_SITE_STAGING"] = str(self.root / "staging")
        os.environ["DASHCAM_LOCAL_SITE_DEST"] = str(self.published_dir)
        spec = ("%s:LocalWebSiteBuilderPlugin:LocalWebSiteUploader"
                % (REPO / "examples" / "local_website.py"))
        self.ctx.plugin = U.load_plugin(spec, REPO)
        return self

    def built(self):
        """As if item 5 had run: the plugin has staged a site of its own.

        Written through the plugin rather than by hand, because where it stages
        and what it stages are its business — the bench only knows that the
        build ran.
        """
        world = P.capture_world(self.ctx, M.Scope.LOCAL)
        self.ctx.plugin.builder.execute(P._handed_over(self.ctx, world))
        return self

    def already_there(self, trip="trip_2026-07-28_08-57_01", size=1024):
        """This trip is at the destination already: its page is there."""
        self.published_dir.mkdir(parents=True, exist_ok=True)
        (self.published_dir / (trip + ".html")).write_text("<html/>")
        return self

    def verdicts(self, scope=None):
        """What each of the ten items says about this bench, right now.

        One world, captured once, and ten pure questions over it — which is
        the whole point of the move: a guard no longer decides when it goes
        and looks.
        """
        world = P.capture_world(self.ctx, scope or M.Scope.FULL)
        menu_items = M.build_menu(M.Strategy.of(self.ctx.plugin), P.Work(self.ctx))
        return {n: item.evaluate(world) for n, item in menu_items.items()}

    def blocked(self):
        return {n: v.reason for n, v in self.verdicts().items() if v.blocked}

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.b = Bench()

    def tearDown(self):
        self.b.cleanup()

    def assertBlocked(self, item, msg=""):
        self.assertIn(item, self.b.blocked(), msg or "item %d should be unavailable" % item)

    def assertAvailable(self, item, msg=""):
        self.assertNotIn(item, self.b.blocked(), msg or "item %d should be available" % item)


# ---------------------------------------------------------------------------
# Availability: what the menu offers, given the state
# ---------------------------------------------------------------------------

class TestAvailability(SpecTest):

    def test_cannot_clean_the_workspace_before_anything_happened(self):
        """Clean Workspace has nothing to do on an untouched bench."""
        self.assertBlocked(CLEAN_WS)

    def test_cannot_clean_the_workspace_with_only_leftover_renders(self):
        """Renders without an import folder: the item's first act (picking an
        import) has nothing to pick, so it must not be offered. The leftovers'
        designed path is to publish or gather them."""
        self.b.render()                     # a render, but no DCIM tree anywhere
        self.assertBlocked(CLEAN_WS)

    def test_can_clean_the_workspace_once_there_is_an_import_with_sidecars(self):
        # The cheap half only. Whether the footage may actually GO is the three
        # heavy gates, asked at dispatch and again after CLEAN is typed —
        # putting them here would shell out to is-complete.py on every draw.
        self.b.card_in().imported().sidecars()
        self.assertAvailable(CLEAN_WS)

    def test_erasing_the_card_is_a_separate_item_from_clearing_the_workspace(self):
        """The unfold, as a rule rather than as a comment.

        Folded, one step gathered the card's evidence from the workspace, erased
        the workspace, and then refused the card half after the irreversible half
        had run — having already printed that the card was verified. Two items
        cannot do that, and Clean Workspace's outbound of {1} means it can never
        even precede the other.
        """
        built = M.build_menu(M.Strategy.of(self.b.ctx.plugin), P.Work(self.b.ctx))
        self.assertNotIn(ERASE_CARD,
                         built[CLEAN_WS].outbound().offers(frozenset(built)))

    def test_cannot_upload_without_the_render_step(self):
        """You cannot upload without a render to upload."""
        self.b.publishes().imported().sidecars()
        self.assertBlocked(UPLOAD, "no renders exist, upload must be unavailable")

    def test_can_upload_once_the_site_has_been_built(self):
        """RESTATED: this used to say "once rendered", and it passed because
        the old shipped example published the render files themselves and so
        had nothing of its own to be missing.

        The graph has always been 6 then 7 — item 7 puts the BUILT site online
        — and the example now has a build step with an artefact, so "nothing
        staged yet" is a real answer about the destination's material rather
        than about ordering. The exporter still answers its own two questions
        (something rendered, a sidecar somewhere) before asking the plugin at
        all, and those are what the two tests around this one pin.
        """
        self.b.publishes().imported().sidecars().render()
        self.assertBlocked(UPLOAD, "nothing has been built to upload yet")
        self.b.built()
        self.assertAvailable(UPLOAD)

    def test_publishing_is_reachable_before_a_render(self):
        """REVERSED, and the reversal is the product. His table removed item
        2's route to item 7 — "publish the page hours early and find out then
        that the publish path is broken" — but publishing a described trip
        before it is encoded is now the point rather than the hazard: the
        route, the distance, the places and the map all come from the sidecars,
        and only the player waits for the mp4.

        The refusal that remains is about material, not order. With nothing
        built the publisher says so, and it says it from wherever the operator
        is standing rather than by being absent from the menu.
        """
        self.b.imported().sidecars().publishes()
        built = M.build_menu(M.Strategy.of(self.b.ctx.plugin), P.Work(self.b.ctx))
        self.assertIn(UPLOAD, built[META].outbound().offers(frozenset(built)))

    def test_cannot_upload_without_sidecars(self):
        """You cannot upload a site without the sidecars created."""
        self.b.publishes().imported().render()      # render, no sidecars
        self.assertBlocked(UPLOAD)

    def test_sidecars_are_required_before_the_items_that_consume_them(self):
        """You have to create sidecars before preview / publish / render.
        Upload Website carries the rule the folded-in deploy step used to: with
        no sidecar anywhere there is no metadata to put online.

        RESTATED: Clean Workspace was in this list and is not any more. It
        consumed sidecars in the sense that its gates are about renders, so
        with none it refused -- and an interrupted first import, which leaves
        clips and nothing else, could then neither go forward nor be cleared.
        It now has a second way in that the others do not have and cannot:
        an import nothing was made from, every clip of which is still on the
        card. The test below holds that line, and the one after it shows the
        refusal is per clip and still there.
        """
        self.b.card_in().imported().publishes()
        blocked = self.b.blocked()
        for item in (PREVIEW, RENDER, UPLOAD):
            self.assertIn(item, blocked, "item %d must wait for sidecars" % item)

    def test_an_import_nothing_was_made_from_can_be_discarded(self):
        """The clips are on the card, so this deletes a copy, not footage."""
        self.b.card_in().imported().publishes()
        self.assertAvailable(CLEAN_WS)

    def test_but_not_when_the_card_no_longer_holds_it(self):
        """Same import, no card in the slot: this copy is the only one, and
        nothing downstream vouches for it either. A discard here would be the
        footage-loss path the sidecar rule was standing in for."""
        self.b.imported().publishes()               # imported, card gone
        self.assertBlocked(CLEAN_WS)

    def test_nor_when_the_cards_copy_is_a_different_size(self):
        """The card still has the NAME. A clip rotated away and replaced, or
        truncated by a bad eject, keeps its filename and loses its footage, so
        name equality alone approves deleting the good copy."""
        self.b.card_in().imported().publishes()
        clip = (self.b.ctx.card / "DCIM" / "200video" / "front"
                / "20260728090000_0060.mp4")
        clip.write_text("truncated-differently")
        self.assertBlocked(CLEAN_WS)

    def test_nor_when_a_file_that_is_not_a_clip_is_missing(self):
        """The delete is an rmtree of the folder, so everything in it has to
        be on the card -- the rear camera, the GPS tars, the event log. A
        check that knew only about front clips deleted the rest unexamined."""
        self.b.card_in().imported().gpx().publishes()
        self.assertBlocked(CLEAN_WS)

    def test_nor_when_the_configured_card_IS_the_import(self):
        """card_root() searches down for a DCIM tree and an import holds one,
        so a card path pointing into the workspace resolves to the very folder
        item 8 would delete. Every file is then "on the card" by identity."""
        self.b.imported().publishes()
        self.b.ctx.card = self.b.ctx.render_root
        self.assertBlocked(CLEAN_WS)

    def test_nor_when_one_clip_of_it_is_missing_from_the_card(self):
        """Per clip, not a headcount: a card holding two of the three still
        leaves one whose only copy is the import."""
        self.b.card_in(stamps=("20260728090000", "20260728100000"))
        self.b.imported(stamps=("20260728090000", "20260728100000",
                                "20260728110000")).publishes()
        self.assertBlocked(CLEAN_WS)

    def test_cannot_create_sidecars_without_gpx(self):
        """You cannot create sidecars without gpx. Generate Meta is what
        CREATES them; Build Preview only looks at what it wrote."""
        self.b.imported()                                       # clips, no 203gps
        self.assertBlocked(META, "no GPX means no sidecars can be built")


# ---------------------------------------------------------------------------
# Preconditions on the destructive and publishing steps
# ---------------------------------------------------------------------------

class TestPreconditions(SpecTest):

    def test_cannot_delete_the_workspace_until_the_target_says_it_has_it(self):
        """You cannot delete the WS without the copy being provably elsewhere.

        The owner's rule was "a verified S3 upload AND the site deploy". Both
        halves are one arrangement's definition of "at the destination" and
        live with the arrangement now; what the exporter asks is the target,
        and only a YES clears a render. Here the target has not been given the
        file, so it says no and the workspace stays.
        """
        self.b.imported().sidecars().render(size=99).publishes()
        world = P.capture_world(self.b.ctx, M.Scope.FULL)
        ok, why, _ = P.working_area_is_expendable(self.b.ctx, world.target)
        self.assertFalse(ok, "a render the target does not have must not authorise"
                             " deleting the workspace")

    def test_once_the_target_has_it_the_workspace_may_go(self):
        self.b.imported().sidecars().render(size=99).publishes()
        self.b.already_there(size=99)
        world = P.capture_world(self.b.ctx, M.Scope.FULL)
        ok, why, _ = P.working_area_is_expendable(self.b.ctx, world.target)
        self.assertTrue(ok, why)

    def test_cannot_import_over_an_unfinished_session(self):
        """You cannot import over an unfinished session."""
        self.b.card_in().imported().sidecars().render()   # a round still in progress
        self.assertBlocked(IMPORT, "an unfinished session must block a new import")

    def test_a_new_import_clears_the_cached_gpx(self):
        """You have to delete cached gpx on new import or name clash."""
        cache = self.b.ctx.out_dir / ".gpx_cache"
        cache.mkdir()
        (cache / "old.gpx").write_text("stale")
        self.b.card_in()
        P.prepare_for_import(self.b.ctx)                  # expected entry point
        self.assertFalse((cache / "old.gpx").exists(),
                         "a new import must drop the cached gpx")

    def test_only_one_instance_of_the_cli_may_run(self):
        """You cannot run two instances of the cli app at once."""
        first = P.acquire_single_instance_lock(self.b.ctx)
        self.assertTrue(first)
        second = P.acquire_single_instance_lock(self.b.ctx)
        self.assertFalse(second, "a second instance must be refused")


# ---------------------------------------------------------------------------
# Interrupted work
# ---------------------------------------------------------------------------

class TestInterruptions(SpecTest):

    def test_an_aborted_render_is_detected_and_cleaned_up(self):
        """Aborting a render is detected and cleaned up."""
        d = self.b.imported().sidecars().ctx.out_dir / self.b.ctx.render_root.name / "2026-07-28"
        (d / "trip_2026-07-28_08-57_01_h1080.mp4.part").write_bytes(b"half")
        inter = self.b.ctx.out_dir / ".intermediates"
        inter.mkdir(exist_ok=True)
        (inter / "clip001.png").write_text("frame")
        P.recover_aborted_render(self.b.ctx)              # expected entry point
        self.assertFalse((d / "trip_2026-07-28_08-57_01_h1080.mp4.part").exists())
        self.assertFalse(any(inter.iterdir()), "scratch frames must be cleared")

    def test_an_interrupted_upload_is_offered_again_rather_than_reported_done(self):
        """Uploads can be interrupted, stopped, cancelled, resumed.

        RESTATED: this used to read the set of owed render names off the world,
        and owes() is gone with the per-name map it filled. What decides
        whether item 7 has anything left to do is now one answer — is every
        trip of this import at the destination — and half-landed must not read
        as done. What a re-run then re-sends is the plugin's own business, and
        the shipped example only sends what is missing.
        """
        self.b.publishes().imported().sidecars()
        self.b.render("trip_A", size=100).render("trip_B", size=100)
        self.b.already_there("trip_A", size=100)          # A landed, B did not
        world = P.capture_world(self.b.ctx, M.Scope.FULL)
        self.assertIs(world.target.complete, M.Evidence.NO,
                      "a half-landed upload must not read as complete")


# ---------------------------------------------------------------------------
# The local (unconfigured) setup
# ---------------------------------------------------------------------------

class TestLocalSetup(SpecTest):

    def test_every_import_gets_its_own_final_folder(self):
        """A local setup builds a new final_<id> for every import."""
        self.b.imported().sidecars().render()
        first = P.final_dir_for(self.b.ctx.final_root, ["2026-07-28"])
        first.mkdir(parents=True, exist_ok=True)
        second = P.final_dir_for(self.b.ctx.final_root, ["2026-07-28"])
        self.assertNotEqual(first, second,
                            "a second import of the same day must not reuse the folder")


# ---------------------------------------------------------------------------
# Excluded trips
# ---------------------------------------------------------------------------

class TestExcludedTrips(SpecTest):

    def test_an_excluded_trip_is_not_imported_again(self):
        """Excluded trips are not imported."""
        self.b.card_in(["20260728090000", "20260728100000"])
        P.record_excluded_stamps(self.b.ctx, ["20260728100000"])   # expected entry point
        new, old = P.card_split(self.b.ctx.card, P.last_imported_stamp(self.b.ctx))
        self.assertEqual(new, 1, "an excluded trip's clips must not count as new")

    def test_wiping_a_sim_does_not_warn_about_excluded_trips(self):
        """Excluded trips ... can be wiped, they are treated as if imported, no
        warning needed at wipe, only at exclude time."""
        self.b.card_in(["20260728090000"])
        P.record_excluded_stamps(self.b.ctx, ["20260728090000"])
        ok, why = P.copy_still_exists(self.b.ctx)
        self.assertTrue(ok, "an excluded clip counts as accounted for: %s" % why)

    def test_an_excluded_trip_is_absent_from_later_listings(self):
        """After a trip is excluded, it doesn't show up as excluded later on in
        the listing."""
        self.b.imported().sidecars()
        P.record_excluded_stamps(self.b.ctx, ["20260728090000"])
        listed = P.listed_trips(self.b.ctx)                        # expected entry point
        self.assertFalse(any(t.get("excluded") for t in listed),
                         "an excluded trip should be gone, not shown as excluded")


# ---------------------------------------------------------------------------
# More of the same shape: what each step needs before it, and what it leaves
# behind. Written from the spec; a failure is a gap in the tool.
# ---------------------------------------------------------------------------

class TestStepOrder(SpecTest):

    def test_generate_meta_needs_an_import(self):
        """Nothing to build sidecars from before anything is imported.

        Not an ordering check even though it reads like one: items 2 and 4
        both lead back to item 2, so an emptied workspace is genuinely
        reachable here, and the honest answer is that there is no work rather
        than that a step was skipped."""
        self.assertBlocked(META)

    def test_generate_meta_is_available_once_clips_are_in(self):
        self.b.imported().gpx()
        self.assertAvailable(META)

    def test_render_needs_an_import_not_just_a_card(self):
        """A mounted card is not a workspace: render reads the copy."""
        self.b.card_in()
        self.assertBlocked(RENDER)

    def test_exclude_needs_something_to_exclude(self):
        self.assertBlocked(EXCLUDE)

    def test_build_website_needs_renders(self):
        """The local page is built FROM the renders."""
        self.b.imported().gpx().sidecars()
        self.assertBlocked(BUILD, "no renders means no page to build")

    def test_build_website_available_once_rendered(self):
        self.b.imported().gpx().sidecars().render()
        self.assertAvailable(BUILD)


class TestPostState(SpecTest):

    def test_import_leaves_the_ledger_advanced(self):
        """After an import the high-water mark covers what came in."""
        self.b.card_in(["20260728090000", "20260728100000"])
        P.record_import(self.b.ctx, self.b.ctx.card)        # expected entry point
        self.assertEqual(P.last_imported_stamp(self.b.ctx), "20260728100000")

    def test_render_leaves_no_scratch_behind(self):
        """A finished render leaves renders and sidecars, not intermediates."""
        self.b.imported().gpx().sidecars().render()
        inter = self.b.ctx.out_dir / ".intermediates"
        inter.mkdir(exist_ok=True)
        (inter / "f.png").write_text("frame")
        P.after_render(self.b.ctx)                          # expected entry point
        self.assertFalse(any(inter.iterdir()) if inter.exists() else False)

    def test_wiping_the_sim_keeps_every_folder_and_takes_every_file(self):
        """The camera does not rebuild its own tree. Handed a card with DCIM
        there but 200video/front missing it sits on "loading card" and records
        nothing — a failure discovered in the car, hours later, with nothing
        recorded in between."""
        self.b.card_in().imported().sidecars()
        dcim = self.b.ctx.card / "DCIM"
        before = sorted(str(d.relative_to(dcim)) for d in dcim.rglob("*") if d.is_dir())
        P.wipe_card(self.b.ctx)                             # expected entry point
        after = sorted(str(d.relative_to(dcim)) for d in dcim.rglob("*") if d.is_dir())
        self.assertTrue(dcim.is_dir(), "DCIM is the card-is-here marker")
        self.assertEqual(after, before, "the camera writes into these")
        self.assertEqual([f for f in dcim.rglob("*") if f.is_file()], [])

    def test_an_erased_card_still_reads_as_a_card(self):
        self.b.card_in().imported().sidecars()
        P.wipe_card(self.b.ctx)
        card = P.capture_world(self.b.ctx, M.Scope.LOCAL).card
        self.assertTrue(card.dcim, "the card is still in the slot")
        self.assertFalse(card.present, "and it holds nothing")

    def test_deleting_the_workspace_keeps_the_state_and_moves_the_receipts(self):
        """What survives is the state, not the payload — and the receipts now
        survive somewhere else. RESTATED: this asserted a _meta.json was still
        under out_dir afterwards. They are archived outside every working area,
        so the working area is left genuinely empty and the state is intact."""
        self.b.imported().sidecars().render(size=10)
        P.state_path(self.b.ctx, P.LEDGER_FILE).write_text('{"through":"20260728090000"}')
        P.purge_published_renders(self.b.ctx, self.b.ctx.render_root)
        out = self.b.ctx.out_dir
        self.assertTrue((self.b.ctx.state_dir / P.LEDGER_FILE).is_file())
        self.assertFalse(any(out.rglob("*_meta.json")), "no receipt stays here")
        self.assertEqual(P.archived_trips(self.b.ctx), 1, "it is archived")
        self.assertFalse(any(out.rglob("*.mp4")))


class TestIdempotence(SpecTest):

    def test_running_the_sidecar_pass_twice_changes_nothing(self):
        """Steps that can be re-run must be safe to re-run."""
        self.b.imported().gpx().sidecars()
        before = sorted(p.name for p in self.b.ctx.out_dir.rglob("*") if p.is_file())
        P.build_sidecars(self.b.ctx)                        # expected entry point
        after = sorted(p.name for p in self.b.ctx.out_dir.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_a_second_wipe_of_an_empty_card_is_a_no_op_not_a_warning(self):
        """An already-clean card is SATISFIED, and must not cry wolf.

        Standing alone, this case used to answer "the ledger claims imported,
        but no copy is still on this machine" — a lost-footage warning fired at
        a card that is simply empty. A guard that cries wolf is how an operator
        learns to stop reading guards.
        """
        self.b.card_in()
        (self.b.ctx.card / "DCIM" / "200video" / "front" / "20260728090000_0060.mp4").unlink()
        verdict = self.b.verdicts()[ERASE_CARD]
        self.assertFalse(verdict.blocked, verdict.reason)
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)
        self.assertIn("nothing to erase", verdict.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# The two copies of config.txt
# ---------------------------------------------------------------------------

class TestWriteConfigHandsOutTheRealFile(unittest.TestCase):
    """--write-config copies config.txt itself, so there is one document.

    An abridged second copy lived in the script and rotted: the file everybody
    edits stayed right while the file everybody is GIVEN went on describing
    confirmation words the tool does not ask for. A test can pin setting names
    across two documents; it cannot pin the prose, which is most of what a
    commented config file is.
    """

    def test_it_writes_config_txt_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "config.new.txt"
            subprocess.run(
                [sys.executable, "-m", "dashcam_exporter.infrastructure.media.renderer",
                 "--write-config", str(out)],
                check=True, capture_output=True, cwd=str(REPO),
                env={**os.environ, "PYTHONPATH": str(REPO / "src")})
            self.assertEqual(out.read_text(encoding="utf-8"),
                             (REPO / "config.txt").read_text(encoding="utf-8"))

    def test_a_directory_argument_lands_on_config_txt_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, "-m", "dashcam_exporter.infrastructure.media.renderer",
                 "--write-config", tmp],
                check=True, capture_output=True, cwd=str(REPO),
                env={**os.environ, "PYTHONPATH": str(REPO / "src")})
            self.assertTrue((Path(tmp) / "config.txt").is_file())
