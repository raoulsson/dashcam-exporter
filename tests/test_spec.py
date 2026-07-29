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
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_spec", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()

IMPORT, PROGRESS, GENERATE_META, PREVIEW, EXCLUDE, RENDER, SITE, UPLOAD, DEPLOY, CLEANUP = range(1, 11)


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

    def site_repo(self, with_deploy=True):
        s = self.root / "site"
        (s / "deploy").mkdir(parents=True, exist_ok=True)
        if with_deploy:
            (s / "deploy" / "deploy-site.sh").write_text("#!/bin/sh\n")
            (s / "deploy" / "upload-videos-s3.sh").write_text("#!/bin/sh\n")
            (s / "build_manifest.py").write_text("")
        self.ctx.site = s
        return self

    def bucket(self, mapping):
        self.ctx.cfg["s3_bucket"] = "b"
        self.ctx.s3_bucket = "b"
        P.s3_objects = lambda ctx: mapping
        return self

    def blocked(self):
        return P.unavailable_steps(self.ctx)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class SpecTest(unittest.TestCase):
    def setUp(self):
        self._s3 = P.s3_objects
        self.b = Bench()

    def tearDown(self):
        P.s3_objects = self._s3
        self.b.cleanup()

    def assertBlocked(self, step, msg=""):
        self.assertIn(step, self.b.blocked(), msg or "step %d should be unavailable" % step)

    def assertAvailable(self, step, msg=""):
        self.assertNotIn(step, self.b.blocked(), msg or "step %d should be available" % step)


# ---------------------------------------------------------------------------
# Availability: what the menu offers, given the state
# ---------------------------------------------------------------------------

class TestAvailability(SpecTest):

    def test_cannot_clean_up_before_anything_happened(self):
        """The clean-up (workspace + SIM, folded into one step) has nothing to
        do on an untouched bench — and with no card in, that includes the SIM
        half."""
        self.assertBlocked(CLEANUP)

    def test_can_clean_up_once_the_card_is_accounted_for(self):
        # Mounted AND accounted for: the clean-up's own runtime guard refuses
        # while nothing was ever imported, so the menu offering it on a bare
        # card was the drift the step graph exists to catch. The rule this file
        # already states — sidecars before the clean-up — applies here too.
        self.b.card_in().imported().sidecars()
        self.assertAvailable(CLEANUP)

    def test_cannot_upload_without_the_render_step(self):
        """You cannot upload without the render step."""
        self.b.site_repo().bucket({}).imported().sidecars()
        self.assertBlocked(UPLOAD, "no renders exist, upload must be unavailable")

    def test_can_upload_once_rendered(self):
        self.b.site_repo().bucket({}).imported().sidecars().render()
        self.assertAvailable(UPLOAD)

    def test_can_deploy_the_site_without_the_render_step(self):
        """You CAN deploy the site without the render step — but not without
        sidecars: deploying with nothing to publish is a no-op, and this file's
        own rule lists deploy-site among the steps that wait for sidecars."""
        self.b.imported().sidecars().site_repo()
        self.assertAvailable(DEPLOY, "deploy publishes curation; renders are not required")

    def test_cannot_upload_without_sidecars(self):
        """You cannot upload a site without the sidecars created."""
        self.b.site_repo().bucket({}).imported().render()      # render, no sidecars
        self.assertBlocked(UPLOAD)

    def test_sidecars_are_required_before_the_steps_that_consume_them(self):
        """You have to create sidecars before preview / deploy site / render /
        upload / delete / wipe sim / wipe WS."""
        self.b.card_in().imported().site_repo().bucket({})
        blocked = self.b.blocked()
        for step in (PREVIEW, RENDER, UPLOAD, DEPLOY, CLEANUP):
            self.assertIn(step, blocked, "step %d must wait for sidecars" % step)

    def test_cannot_create_sidecars_without_gpx(self):
        """You cannot create sidecars without gpx. The step that CREATES them
        is Generate meta now — Preview only looks at what it wrote."""
        self.b.imported()                                       # clips, no 203gps
        self.assertBlocked(GENERATE_META, "no GPX means no sidecars can be built")


# ---------------------------------------------------------------------------
# Preconditions on the destructive and publishing steps
# ---------------------------------------------------------------------------

class TestPreconditions(SpecTest):

    def test_cannot_delete_the_workspace_without_upload_and_deploy(self):
        """You cannot delete the WS without a verified upload of S3 AND site
        deploy."""
        self.b.imported().sidecars().render(size=99).site_repo()
        self.b.bucket({"x/trip_2026-07-28_08-57_01_h1080.mp4": 99})   # uploaded...
        ok, why, _ = P.working_area_is_expendable(self.b.ctx)
        self.assertFalse(ok, "upload alone must not authorise deleting the workspace; "
                             "the site deploy has to have happened too")

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

    def test_an_interrupted_upload_resumes_rather_than_restarts(self):
        """Uploads can be interrupted, stopped, cancelled, resumed."""
        self.b.site_repo().imported().sidecars()
        self.b.render("trip_A", size=100).render("trip_B", size=100)
        self.b.bucket({"x/trip_A_h1080.mp4": 100})        # A landed, B did not
        todo = P.uploads_outstanding(self.b.ctx)          # expected entry point
        self.assertEqual([p.name for p in todo], ["trip_B_h1080.mp4"],
                         "an interrupted upload resumes with what is missing")


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
        """Nothing to build sidecars from before anything is imported. (These
        two were List trips' availability tests; the generative half of step 2
        moved into Generate meta, and Progress — the view — is never blocked.)"""
        self.assertBlocked(GENERATE_META)

    def test_generate_meta_is_available_once_clips_are_in(self):
        self.b.imported().gpx()
        self.assertAvailable(GENERATE_META)

    def test_render_needs_an_import_not_just_a_card(self):
        """A mounted card is not a workspace: render reads the copy."""
        self.b.card_in()
        self.assertBlocked(RENDER)

    def test_exclude_needs_something_to_exclude(self):
        self.assertBlocked(EXCLUDE)

    def test_site_step_needs_renders(self):
        """The local site is built FROM the renders."""
        self.b.imported().gpx().sidecars()
        self.assertBlocked(SITE, "no renders means no site to build")

    def test_site_step_available_once_rendered(self):
        self.b.imported().gpx().sidecars().render()
        self.assertAvailable(SITE)


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

    def test_wiping_the_sim_keeps_the_folder_tree(self):
        """The camera writes into DCIM/200video/{front,rear} and expects them."""
        self.b.card_in().imported().sidecars()
        P.wipe_card(self.b.ctx)                             # expected entry point
        front = self.b.ctx.card / "DCIM" / "200video" / "front"
        self.assertTrue(front.is_dir(), "folders must survive the wipe")
        self.assertEqual(list(front.glob("*.mp4")), [])

    def test_deleting_the_workspace_keeps_logs_ledger_and_meta(self):
        """What survives is the state, not the payload."""
        self.b.imported().sidecars().render(size=10)
        (self.b.ctx.out_dir / "logs").mkdir(exist_ok=True)
        (self.b.ctx.out_dir / "logs" / "run.log").write_text("log")
        (self.b.ctx.out_dir / P.LEDGER_FILE).write_text('{"through":"20260728090000"}')
        P.purge_published_renders(self.b.ctx, self.b.ctx.render_root)
        out = self.b.ctx.out_dir
        self.assertTrue((out / "logs" / "run.log").is_file())
        self.assertTrue((out / P.LEDGER_FILE).is_file())
        self.assertTrue(any(out.rglob("*_meta.json")))
        self.assertFalse(any(out.rglob("*.mp4")))


class TestIdempotence(SpecTest):

    def test_running_the_sidecar_pass_twice_changes_nothing(self):
        """Steps that can be re-run must be safe to re-run."""
        self.b.imported().gpx().sidecars()
        before = sorted(p.name for p in self.b.ctx.out_dir.rglob("*") if p.is_file())
        P.build_sidecars(self.b.ctx)                        # expected entry point
        after = sorted(p.name for p in self.b.ctx.out_dir.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_a_second_wipe_of_an_empty_card_is_a_no_op(self):
        self.b.card_in()
        (self.b.ctx.card / "DCIM" / "200video" / "front" / "20260728090000_0060.mp4").unlink()
        self.assertBlocked(CLEANUP, "an empty card and an empty workspace offer nothing to clean")


if __name__ == "__main__":
    unittest.main(verbosity=2)
