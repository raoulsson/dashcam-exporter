#!/usr/bin/env python3
"""The seam itself, driven: the real items 6, 7 and 8 against a plugin this
repo does not own.

The other files stop short of this on purpose. test_uploader.py states what an
answer MEANS and what the loader refuses; test_wiring.py binds the signatures;
test_paths.py drives the graph with every body mocked; test_spec.py asks the
real ten what they think of a real workspace but only ever LOOKS at their
verdicts. None of them executes an item whose work is a stranger's code.

That is the one thing the interface exists to make possible, so it is the thing
this file does: a plugin under the test's control is handed to a real
pipeline.Work over a real temporary workspace, and the ten real items are then
run at it. What it pins, that nothing pinned before:

  * item 6 under a configured plugin writes NO local page and gathers nothing
    — the reported bug, as a property of a run rather than of the wiring;
  * item 6 and item 7 take their reasons, their descriptions and their
    "is there anything left to do" from the plugin and not from the disk;
  * the local edition still builds its page, and item 7 is unreachable there
    by the edges rather than by a refusal in a body;
  * a plugin that raises, one that cannot say, and one that will not load all
    fail CLOSED at item 8 — and one that answers yes to everything still
    cannot erase an import that produced no renders;
  * the destructive re-check really does ask the plugin a SECOND time, and one
    that changes its mind between the banner and the typed word stops the
    erase;
  * the trips it is asked about are THIS import's, including a trip that
    produced no render at all;
  * a menu draw never asks the question that leaves the machine;
  * both items are idempotent, counted in calls made to the plugin.

Fixtures and temp dirs only. Nothing here touches the card, the real workspace,
a network host or a real implementation.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import items                     # noqa: E402,F401  (registers the ten)
import menu as M                 # noqa: E402
import uploader as U             # noqa: E402
from menu import (META, EXCLUDE, BUILD, UPLOAD, CLEAN_WS,
                  ERASE_CARD)                                   # noqa: E402


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_seam", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()

YES, NO, UNKNOWN, NA = (M.Evidence.YES, M.Evidence.NO,
                        M.Evidence.UNKNOWN, M.Evidence.NA)

TRIP = "trip_2026-07-28_08-57_01"
RENDER_NAME = TRIP + "_h1080.mp4"
SECOND_TRIP = "trip_2026-07-28_15-30_02"
SECOND_RENDER = SECOND_TRIP + "_h1080.mp4"
DAY = "2026-07-28"
CLIP = "20260728090000"
EXAMPLE = REPO / "examples" / "local_website.py"
EXAMPLE_SPEC = ("%s:LocalWebSiteBuilderPlugin:LocalWebSiteUploader" % EXAMPLE)


@contextlib.contextmanager
def quiet():
    """The items print. A test reads what happened, not the ink."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


# ---------------------------------------------------------------------------
# The plugin under the test's control
# ---------------------------------------------------------------------------

def _scripted(answers):
    """One answer per ask, the last one repeating.

    So a test can make a plugin CHANGE ITS MIND between the world the banner
    was drawn from and the world captured after CLEAN was typed. That is the
    only way to tell an exporter that re-asks from one that remembers, and
    remembering is precisely what the destructive re-check exists to defeat.
    """
    if len(answers) > 1:
        return answers.pop(0)
    return answers[0]


class Script:
    """What the test's two acts answer, and every question they were asked.

    One object behind both classes, because a test wants to say "this
    destination has everything" once and have the builder and the uploader
    agree about it — which is what a single plugin means.
    """

    def __init__(self, complete=YES, raises=None, builds=True, uploads=True,
                 no_build=None, no_upload=None, settles=False,
                 describes="Push the drives onto the test's own shelf."):
        self.calls = []            # every question asked, in order
        self.workspaces = []       # exactly what was handed to an execute()
        self.trip_asks = []        # the trip ids is_complete() was given
        self.complete = _as_list(complete)
        self.raises = raises
        self.builds = builds
        self.uploads = uploads
        self.no_build = no_build
        self.no_upload = no_upload
        self.settles = settles
        self.describes = describes

    # -- what the test reads afterwards ------------------------------------
    def times(self, question):
        return self.calls.count(question)

    def asked(self, *questions):
        return [c for c in self.calls if c in questions]

    def saw(self, question, workspace):
        self.calls.append(question)
        self.workspaces.append(workspace)


class Recording(U.Act):
    """The half of an act both kinds share: it writes down what it was asked."""

    def __init__(self, script):
        self.script = script

    def describe(self):
        return self.script.describes

    def reset(self):
        self.script.calls.append("reset")


class RecordingBuilder(Recording, U.Builder):

    def evaluate(self, workspace):
        self.script.calls.append("evaluate_build")
        return _verdict(self.script.no_build)

    def execute(self, workspace):
        self.script.saw("build", workspace)
        return _outcome(self.script.builds, "built by the test's plugin")


class RecordingUploader(Recording, U.Uploader):

    def evaluate(self, workspace):
        self.script.calls.append("evaluate_upload")
        return _verdict(self.script.no_upload)

    def execute(self, workspace):
        self.script.saw("upload", workspace)
        return self._uploaded()

    def _uploaded(self):
        if self.script.settles:
            self.script.complete = [YES]
        return _outcome(self.script.uploads, "uploaded by the test's plugin")

    def is_complete(self, trip_ids):
        """The one question that may leave the machine.

        It records the trip ids it was asked about, because WHICH trips the
        exporter names is half the safety of an all-or-nothing answer.
        """
        self.script.calls.append("is_complete")
        self.script.trip_asks.append(tuple(trip_ids))
        self._maybe_raise()
        return _scripted(self.script.complete)

    def _maybe_raise(self):
        """A plugin that falls over while being asked.

        An implementation is trusted about what it SAYS, and an exception is
        not a thing it said. The exporter has to read this as unreachable, not
        as consent.
        """
        if self.script.raises:
            raise RuntimeError(self.script.raises)


class Recorder(U.Plugin):
    """A plugin whose two acts share one script, with the script's readers
    hoisted onto it so a test says what it means: `target.times("build")`.

    It is a real uploader.Plugin — the same shape the loader builds — because
    everything under test reads .builder, .uploader, .name and .origin off it.
    """

    def __init__(self, **kw):
        script = Script(**kw)
        super().__init__(RecordingBuilder(script), RecordingUploader(script),
                         "/a/test/plugin.py:RecordingBuilder:RecordingUploader")

    @property
    def script(self):
        return self.uploader.script

    def times(self, question):
        return self.script.times(question)

    def asked(self, *questions):
        return self.script.asked(*questions)

    @property
    def handed(self):
        """Every Workspace an execute() was given."""
        return self.script.workspaces

    @property
    def trip_asks(self):
        return self.script.trip_asks


def _verdict(reason):
    if reason:
        return M.blocked(reason)
    return M.go()


def _outcome(ok, note):
    if ok:
        return M.did(note)
    return M.stopped(note)


def _as_list(reading):
    if isinstance(reading, list):
        return list(reading)
    return [reading]


def _script(plugin):
    """The knobs behind a plugin the bench was given."""
    return plugin.uploader.script


# ---------------------------------------------------------------------------
# The bench: a real Ctx over a temp workspace, a real Work, the real ten items
# ---------------------------------------------------------------------------

class Bench:
    """A workspace on disk and the items that judge it.

    Deliberately the real Ctx object with its fields filled in rather than a
    stand-in: the collaborators under test are pipeline.TargetBuild,
    pipeline.TargetPublish and pipeline.LocalPage, and each of them reaches
    into the ctx for its work. A fake ctx would prove they were called, not
    that they run.
    """

    def __init__(self, plugin=None):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-seam-"))
        self.plugin = plugin
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
        c.log_dir = self.root / "logs"
        c.render_root = self.root / "import"
        c.import_root = self.root / "import"
        c.card = self.root / "card"
        c.plugin = plugin
        c.offline = False
        c.selected_import = None
        c.last_scan = None
        c.last_groups = None
        c.config_args = []
        c.scan_args = []
        c.results = []
        c.speed_colour = True
        c.site_still_seconds = 2.0

    # -- what is on disk ---------------------------------------------------
    def imported(self, stamps=(CLIP,)):
        f = self.ctx.render_root / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        for s in stamps:
            (f / ("%s_0060.mp4" % s)).write_text("clip")
        return self

    def sidecars(self, trip=TRIP):
        d = self.ctx.out_dir / self.ctx.render_root.name / DAY
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_meta.json")).write_text(json.dumps(
            {"day": DAY, "start": "2026-07-28 08:57:56", "end": "2026-07-28 14:13:41"}))
        (d / (trip + ".gpx")).write_text("<gpx/>")
        return self

    def render(self, trip=TRIP, size=1024):
        d = self.ctx.out_dir / self.ctx.render_root.name / DAY
        d.mkdir(parents=True, exist_ok=True)
        (d / (trip + "_h1080.mp4")).write_bytes(b"x" * size)
        return self

    def grouped(self, trips=1):
        """This session's cached grouping, so "rendered locally" can answer.

        Without it _expected_trips is None and the first gate abstains — an
        ordinary state, but not the one to test the other two gates against.

        It is also what item 4 reads instead of starting a scan, so a real drop
        can be driven here without the renderer: load_groups returns this
        payload untouched when the root matches, which is the same path a
        second drop in one session takes on a real machine.
        """
        self.ctx.last_groups = (self.ctx.import_root, self._grouping(trips))
        return self

    def _grouping(self, trips):
        return {"root": str(self.ctx.import_root),
                "trips": [self._trip(i) for i in range(1, trips + 1)]}

    def _trip(self, index):
        clips = sorted(str(p) for p in
                       (self.ctx.render_root / "DCIM").rglob("*.mp4"))
        return {"index": index, "day": DAY, "renderable": True, "clips": len(clips),
                "start": "2026-07-28 08:57:56", "end": "2026-07-28 14:13:41",
                "front": clips, "rear": [],
                "out_base": str(self.ctx.out_dir / self.ctx.render_root.name
                                / DAY / TRIP)}

    def complete(self):
        return self.imported().sidecars().render().grouped()

    # -- what the machine says ---------------------------------------------
    def world(self, scope=M.Scope.FULL):
        return P.capture_world(self.ctx, scope)

    def menu(self):
        return M.build_menu(M.Strategy.of(self.ctx.plugin), P.Work(self.ctx))

    def evaluate(self, number):
        return self.menu()[number].evaluate(self.world())

    def run(self, number, typed=None):
        """Execute one real item for real, with the operator scripted.

        `typed` is what gets typed; None means the word the item asked for,
        which is the case that reaches the irreversible half. A list is one
        answer per prompt, the last one repeating — item 4 asks which trips
        before it asks for DROP.
        """
        item = self.menu()[number]
        with quiet() as out, self._prompted(item, typed):
            outcome = item.execute(self.world())
        return Ran(item, outcome, out.getvalue())

    def _prompted(self, item, typed):
        self.asked = []
        return mock.patch.object(
            P, "ask", side_effect=self._answering(_as_list(_or_word(item, typed))))

    def _answering(self, answers):
        def ask(prompt, default="", quits=True):
            self.asked.append(prompt)
            return _scripted(answers)
        return ask

    # -- reading the disk back ---------------------------------------------
    def pages(self):
        return sorted(p for p in self.root.rglob(P.RESULT_FILE))

    def finals(self):
        return sorted(p for p in self.root.glob(P.FINAL_PREFIX + "*") if p.is_dir())

    def renders_on_disk(self):
        return sorted(p.name for p in self.ctx.out_dir.rglob("*.mp4"))

    def footage_on_disk(self):
        return (self.ctx.import_root / "DCIM").is_dir()

    def reopen(self, plugin):
        """The same working area, a freshly constructed plugin.

        What a restart amounts to here: the disk is where the last session left
        it, and nothing the plugin knew in memory survives. The menu is rebuilt
        from ctx on every call, so swapping it is the whole of it — which is
        itself the point, since anything the exporter had cached about the
        destination would survive this and be caught by the tests that use it.
        """
        self.plugin = plugin
        self.ctx.plugin = plugin
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Ran:
    """One executed item: what it answered, and what it printed."""

    def __init__(self, item, outcome, printed):
        self.item = item
        self.outcome = outcome
        self.printed = printed

    @property
    def completed(self):
        return self.outcome.completed

    @property
    def note(self):
        return self.outcome.note


def _or_word(item, typed):
    if typed is None:
        return item.word()
    return typed


class SeamTest(unittest.TestCase):
    def bench(self, plugin=None):
        b = Bench(plugin)
        self.addCleanup(b.cleanup)
        return b


# ---------------------------------------------------------------------------
# Item 6 — the work is the target's, and so is the sentence about it
# ---------------------------------------------------------------------------

class TestBuildWebsiteDelegates(SeamTest):

    def test_the_target_builds_and_no_local_page_is_written(self):
        """The reported bug, as a property of a RUN rather than of the wiring.

        Under a configured target the local edition's deliverable must not
        appear at all: the page announces that nothing leaves this machine
        while item 7 is one keypress from sending everything, and the gather it
        used to do would rename every published trip out from under whatever
        index the target keeps.
        """
        target = Recorder()
        b = self.bench(target).complete()
        ran = b.run(BUILD)
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(target.times("build"), 1)
        self.assertEqual(b.pages(), [], "a publishing install got a local page")
        self.assertEqual(b.finals(), [], "a publishing install had its renders gathered")
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME],
                         "the render tree moved under a target that publishes it")

    def test_what_is_handed_over_is_the_exporters_own_output(self):
        """An implementation is lent the tool's Ui, on the workspace it is
        handed, so its work looks like the rest of the session. If that ever
        stops being a real Ui, every plugin that used it breaks at once, and it
        breaks inside somebody else's code.
        """
        target = Recorder()
        self.bench(target).complete().run(BUILD)
        self.assertTrue(all(isinstance(w.ui, U.Ui) for w in target.handed))

    def test_the_menu_row_is_the_targets_own_sentence(self):
        """Item 6's description differs between the two products because the
        job genuinely differs, and the entry asks the thing that does the job
        rather than restating how the tool is installed."""
        b = self.bench(Recorder(describes="Push to the shelf."))
        self.assertEqual(b.menu()[BUILD].description(), "Push to the shelf.")
        self.assertNotIn("Nothing leaves this machine",
                         b.menu()[BUILD].description())

    def test_the_reason_it_will_not_run_comes_from_the_target(self):
        """Verbatim, and about the target only. The workspace is complete here,
        so nothing local is in the way and the refusal can have come from
        nowhere else."""
        target = Recorder(no_build="the index template is missing")
        b = self.bench(target).complete()
        verdict = b.evaluate(BUILD)
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.reason, "the index template is missing")
        self.assertEqual(target.times("build"), 0)

    def test_the_exporter_still_answers_its_own_question_first(self):
        """Requirement A at item 6. A target that says yes to everything still
        cannot get a page built out of an empty tree, because "is there
        anything here to build from" is a fact about this machine and is never
        delegated."""
        target = Recorder()
        b = self.bench(target).imported().sidecars()      # no render anywhere
        ran = b.run(BUILD)
        self.assertFalse(ran.completed)
        self.assertIn("no meta or renders", ran.note)
        self.assertEqual(target.times("build"), 0,
                         "the target was asked to build from nothing")

    def test_a_target_that_reports_failure_does_not_complete(self):
        """`ok` is the only thing the menu reads off a Report, and it decides
        whether the position advances. A target that says the build failed must
        not move the pipeline on."""
        b = self.bench(Recorder(builds=False)).complete()
        ran = b.run(BUILD)
        self.assertFalse(ran.completed)


# ---------------------------------------------------------------------------
# Item 7 — one job, and the target decides whether it is owed
# ---------------------------------------------------------------------------

class TestUploadWebsiteDelegates(SeamTest):

    def test_the_upload_is_the_targets_and_runs_once(self):
        target = Recorder(complete=NO)      # something left to send
        ran = self.bench(target).complete().run(UPLOAD)
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(target.times("upload"), 1)

    def test_whether_anything_is_left_to_do_is_the_destinations_answer(self):
        """RESTATED: owes() is gone, and this is the question it answered.

        Not the disk's: nothing has been uploaded anywhere in this test and the
        renders are all sitting in the workspace. The only thing that can make
        this SATISFIED is the destination saying it has every trip of this
        import — which is one answer now instead of a set of owed names.
        """
        target = Recorder(complete=YES)
        b = self.bench(target).complete()
        verdict = b.evaluate(UPLOAD)
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)
        self.assertEqual(target.times("upload"), 0)

    def test_a_destination_that_could_not_be_asked_still_offers_the_upload(self):
        """A failed listing proves nothing, so the offer stands — the UPLOAD is
        what discovers the destination is down. The opposite reading would
        report an install as fully published because its destination was
        unreachable."""
        b = self.bench(Recorder(raises="no route to the host")).complete()
        self.assertIs(b.world().target.complete, UNKNOWN)
        self.assertIsNot(b.evaluate(UPLOAD).ruling, M.Ruling.SATISFIED)

    def test_the_reason_it_will_not_run_comes_from_the_target(self):
        target = Recorder(no_upload="the credentials expired on Tuesday")
        b = self.bench(target).complete()
        verdict = b.evaluate(UPLOAD)
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.reason, "the credentials expired on Tuesday")
        self.assertEqual(target.times("upload"), 0)

    def test_the_exporter_still_answers_its_own_questions_first(self):
        """Two local facts item 7 keeps for itself: something rendered to send,
        and a sidecar somewhere to describe it. Neither is asked of the plugin,
        and a plugin answering yes to everything does not move them."""
        target = Recorder(no_upload=None)
        b = self.bench(target).imported().grouped()       # no renders, no sidecars
        verdict = b.evaluate(UPLOAD)
        self.assertTrue(verdict.blocked)
        self.assertEqual(target.times("upload"), 0)


# ---------------------------------------------------------------------------
# The local edition — no implementation at all
# ---------------------------------------------------------------------------

class TestTheLocalEdition(SeamTest):
    """Absent means the local edition, exactly as an unconfigured install has
    always behaved. Nothing here has a target to ask, and that is a different
    state from a target that could not answer."""

    def test_item_six_writes_the_page_and_gathers(self):
        """The local product's whole deliverable, and the thing that makes its
        workspace expendable. Frame extraction is stubbed: what is under test
        is which builder ran, not ffmpeg."""
        b = self.bench().complete()
        with mock.patch.object(P, "still_data_uri", return_value=""):
            ran = b.run(BUILD)
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(len(b.pages()), 1, "the local edition built no page")
        self.assertEqual(len(b.finals()), 1, "the local edition gathered nothing")

    def test_item_seven_is_unreachable_by_the_edges_and_not_by_a_refusal(self):
        """Switched off means nothing leads in and nothing leads out. Read off
        the graph the tool actually walks, so there is no second list of which
        product has which item to disagree with it."""
        built = self.bench().menu()
        self.assertTrue(M.switched_off(built[UPLOAD]))
        self.assertEqual(M.leads_to(built, UPLOAD), [])

    def test_item_seven_runs_nothing_even_when_executed_directly(self):
        """Belt as well as braces: the graph never offers it, and if something
        reached past the graph the item still refuses by configuration."""
        b = self.bench().complete()
        ran = b.run(UPLOAD)
        self.assertFalse(ran.completed)
        self.assertEqual(b.ctx.results, [], "the local edition logged an upload")

    def test_nothing_is_asked_of_a_destination_that_does_not_exist(self):
        """NA, not UNKNOWN. The destination gate is dropped rather than failed,
        so the erase falls back to the gate that never leaves this machine —
        and the banner says publication could not be verified rather than that
        it failed."""
        world = self.bench().complete().world()
        self.assertFalse(world.target.configured)
        self.assertIs(world.target.complete, NA)
        self.assertTrue(P.guards.unproven_lines(world))


# ---------------------------------------------------------------------------
# Item 8 — no answer, and no absence of one, may read as permission
# ---------------------------------------------------------------------------

class TestNothingUnprovenErasesFootage(SeamTest):

    def _erased(self, target, typed=None):
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS, typed=typed)
        return b, ran

    def test_a_destination_that_says_yes_erases_the_footage(self):
        """The positive control. Without it every refusal below could be
        passing for some reason that has nothing to do with the answer."""
        b, ran = self._erased(Recorder(complete=YES))
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk(), "the erase did not happen")

    def test_a_target_that_raises_refuses_and_never_reaches_the_prompt(self):
        """An exception is not a thing the target said. It reads as
        unreachable — UNKNOWN everywhere — and the refusal comes before the
        banner, so nobody types CLEAN and then learns it was not going to
        happen."""
        b, ran = self._erased(Recorder(raises="the shelf fell over"))
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "footage went on a target's exception")
        self.assertEqual(b.asked, [], "the word was asked for over a refusal")

    def test_a_target_that_cannot_say_refuses(self):
        """Unreachable is not permission. This is the reading the whole
        fail-closed discipline exists for: the honest answer from a target
        whose destination is down."""
        b, ran = self._erased(Recorder(complete=UNKNOWN))
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())

    def test_a_target_that_says_no_refuses(self):
        b, ran = self._erased(Recorder(complete=NO))
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())

    def test_a_yes_to_everything_cannot_erase_an_import_that_produced_nothing(self):
        """Requirement A, at the item that erases footage.

        "Were these trips rendered on this machine" never leaves home, so a
        target answering yes to every question it is asked still cannot talk
        this into erasing an import with no renders — it is not asked, and the
        answer it would have given is not consulted.
        """
        target = Recorder(complete=YES)
        b = self.bench(target).imported().sidecars().grouped()   # nothing rendered
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())
        self.assertIn("nothing from this import was rendered", ran.note)

    def test_only_a_yes_clears_a_render_and_the_footage_going_does_not(self):
        """RESTATED: the sweep used to read a per-render hold answer, and there
        is one answer now — so this states the same rule with the split that is
        left.

        The erase itself is refused here, and that is the point of the pair
        below: the render sweep is a second, separately-proven permission. A
        render the destination could not speak for is the only copy of that
        drive, and UNKNOWN keeps it. Anything looser sweeps a render on the
        strength of a question nobody answered.
        """
        b = self.bench(Recorder(complete=UNKNOWN)).complete()
        b.run(CLEAN_WS)
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME],
                         "a render nobody could vouch for was swept")

    def test_the_renders_go_only_once_the_destination_vouches_for_them(self):
        """The other half: with a YES the footage goes AND the renders are
        swept, because they are then a second copy rather than the only one."""
        b = self.bench(Recorder(complete=YES)).complete()
        b.run(CLEAN_WS)
        self.assertFalse(b.footage_on_disk(), "the erase itself did not happen")
        self.assertEqual(b.renders_on_disk(), [])

    def test_the_erase_says_whose_answer_it_acted_on(self):
        """Attribution, in the record that outlives the screen. Not a
        safeguard — a component inside the trust boundary needs none — but the
        question "who said the footage was safe" gets asked weeks later.

        RESTATED: it was on the exit summary, which is a screen, and a screen
        is read once and then scrolls. Worse, it was printed on a DISCARD too,
        naming a plugin that was never asked and had no part in the decision.
        It is now the ledger's note for that sweep — a file, beside the
        high-water mark, which is the one thing here designed to outlive
        everything else.
        """
        b, ran = self._erased(Recorder())
        note = P.read_ledger(b.ctx)["history"][-1]["note"]
        self.assertIn("/a/test/plugin.py:RecordingBuilder:RecordingUploader", note)

    def test_anything_but_the_word_erases_nothing(self):
        b, ran = self._erased(Recorder(), typed="yes")
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())


# ---------------------------------------------------------------------------
# The re-check asks again — the freshness rule, across the seam
# ---------------------------------------------------------------------------

class TestTheRecheckAsksTheTargetAgain(SeamTest):

    def test_a_target_that_changes_its_mind_stops_the_erase(self):
        """The rule the destructive machine exists for, now that the deciding
        fact lives outside this repo.

        The plugin says complete when the banner is drawn and cannot say when
        the world is captured after CLEAN is typed — a destination that went
        down while the prompt was on screen. The second answer is the one that
        counts, and it refuses.
        """
        target = Recorder(complete=[YES, UNKNOWN])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "footage went on a pre-prompt answer")
        self.assertIn("Refused after the re-check", ran.note)
        self.assertGreaterEqual(target.times("is_complete"), 2,
                                "the plugin was asked once and remembered")
        # And it is in the log. This path returned an Outcome and recorded
        # nothing, so the one run where the operator typed the word and the
        # tool said no afterwards left no row in the summary at all.
        rows = [r for r in b.ctx.results if r.name == items.NAMES[CLEAN_WS]]
        self.assertTrue(rows, "a refusal after the word left no summary row")
        self.assertIn("Refused after the re-check", rows[-1].detail)

    def test_the_erase_that_proceeds_was_asked_twice_and_not_remembered(self):
        """The same path when the answer holds up. Two asks, not one — the
        first drew the banner, the second is what the act was permitted on."""
        target = Recorder(complete=[YES, YES])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertGreaterEqual(target.times("is_complete"), 2)

    def test_every_capture_asks_including_a_menu_draw(self):
        """RESTATED. This asserted the opposite: that a menu draw must never
        ask, because a plugin going to the network would make the menu
        unusable.

        That was the exporter budgeting on a guess about someone else's code.
        It does not know what is behind the interface — an ssh session, a dict,
        a mock — so it cannot know whether asking is expensive, and deciding
        not to ask cost real accuracy: the menu offered items that refused the
        moment they were picked.

        Whether to cache is the implementation's decision, made where the cost
        is known and where an upload can invalidate it. The exporter asks.
        """
        target = Recorder()
        b = self.bench(target).complete()
        for scope in (M.Scope.LOCAL, M.Scope.FULL):
            with self.subTest(scope=scope.value):
                before = target.times("is_complete")
                world = b.world(scope)
                self.assertGreater(target.times("is_complete"), before,
                                   "the plugin was not asked at %s" % scope.value)
                self.assertTrue(world.target.configured)

    def test_the_trips_it_is_asked_about_are_this_imports_own(self):
        """WHICH trips are named is half the safety of a binary answer.

        They come from the sidecars of the import under judgement, so a trip
        that produced no render is still in the list — the destination does not
        have it, says NO, and its footage is not erased. Read off the renders
        instead, that trip would be invisible to the question and its only copy
        would go.
        """
        target = Recorder()
        b = self.bench(target).imported().sidecars().sidecars(
            trip=SECOND_TRIP).render().grouped()      # only the first is rendered
        b.world(M.Scope.FULL)
        self.assertEqual(target.trip_asks[-1], (TRIP, SECOND_TRIP))


# ---------------------------------------------------------------------------
# Idempotence, counted in calls to the target
# ---------------------------------------------------------------------------

class TestIdempotence(SeamTest):

    def test_building_twice_asks_the_target_twice_and_remembers_nothing(self):
        """Item 6 reaches itself in the graph, so a second run is a path the
        machine offers rather than one it tolerates. Each run is answered by
        the world it is handed, so the second is worth exactly what the first
        was."""
        target = Recorder()
        b = self.bench(target).complete()
        first, second = b.run(BUILD), b.run(BUILD)
        self.assertTrue(first.completed and second.completed)
        self.assertEqual(target.times("build"), 2)
        self.assertEqual(b.pages(), [])

    def test_a_second_upload_of_a_target_that_has_everything_never_runs(self):
        """SATISFIED is not GO, and this is where it earns its keep across the
        seam: once the destination has every trip the item completes without
        the plugin doing the work again. An exporter that re-ran it would push
        the whole set on every keypress."""
        target = Recorder(complete=NO, settles=True)
        b = self.bench(target).complete()
        first, second = b.run(UPLOAD), b.run(UPLOAD)
        self.assertTrue(first.completed and second.completed)
        self.assertEqual(target.times("upload"), 1,
                         "the second upload ran against a settled target")
        self.assertFalse(second.outcome.performed)

    def test_a_second_erase_is_refused_rather_than_repeated(self):
        """Once the footage is gone there is no import to clean, and item 8
        says so instead of asking for CLEAN again to find out."""
        b = self.bench(Recorder()).complete()
        self.assertTrue(b.run(CLEAN_WS).completed)
        second = b.run(CLEAN_WS)
        self.assertFalse(second.completed)
        self.assertEqual(b.asked, [], "CLEAN was asked for with nothing to clean")


# ---------------------------------------------------------------------------
# Item 4 — the questions the interface grew for it
# ---------------------------------------------------------------------------

class TestExcludeTripAsksTheTarget(SeamTest):
    """The half of the seam that feeds a WARNING rather than a gate.

    RESTATED for the narrowed interface. This used to pin two members that are
    gone: carries(), which asked per trip id, and dropped(), which was the one
    thing the exporter TOLD a plugin. The facts they carried both survive —
    is_complete() is keyed on trip ids and so still covers a trip whose local
    render is long gone, and a deliberate drop is now RECORDED in the workspace
    and handed to the next build as Workspace.dropped_ids.

    A warning that silently stops firing is worse than one that never existed,
    and a drop nothing downstream knows about leaves the trip live forever.
    """

    def _published_then_cleaned_up(self, target):
        """The case the id-keyed question was written for: the trip is at the
        destination and its local render is long gone."""
        return self.bench(target).imported().sidecars().grouped()

    def test_a_trip_the_destination_has_is_not_called_the_only_copy(self):
        """No render on disk and nothing but the plugin's word for it. If this
        panel fires here it fires over every published trip anyone ever cleans
        up, and an operator who sees it wrongly once stops reading it."""
        target = Recorder(complete=YES)
        b = self._published_then_cleaned_up(target)
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        # Twice, not once: item 4 captures a full world to plan with and
        # another after the word, because Destructive re-derives whatever the
        # guard it was given happens to read.
        self.assertGreaterEqual(target.times("is_complete"), 1,
                                "the plugin was never asked")
        self.assertNotIn("ONLY copy", ran.printed)
        self.assertIn("also exist", ran.printed)

    def test_a_trip_nobody_can_vouch_for_gets_the_full_warning(self):
        """The other half, which is what makes the first half meaningful: a
        destination that cannot say does NOT suppress the panel, and the caveat
        says which of the three silences this was.

        RESTATED on the caveat's wording. It read "could not answer", which was
        the only way to get here while every answer covered every trip. There
        is a second way now — the answer was about a different import than the
        trips on screen — and both leave the same hole, so the sentence states
        the hole ("no answer covering these trips") rather than guessing which
        of the two produced it.
        """
        b = self._published_then_cleaned_up(Recorder(complete=UNKNOWN))
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("ONLY copy", ran.printed)
        self.assertIn("gave no answer covering these trips", ran.printed)

    def test_with_no_target_the_warning_says_nothing_is_off_this_machine(self):
        """Three silences, three sentences. No plugin at all is not the same as
        one that timed out, and saying so wrongly in a delete prompt is a lie
        the operator acts on."""
        b = self.bench().imported().sidecars().grouped()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("ONLY copy", ran.printed)
        self.assertIn("No website_uploader is configured", ran.printed)

    def test_a_copy_that_stays_behind_is_named(self):
        """The note above the file list: deleting locally does not unpublish.

        RESTATED. It used to be driven off carries() precisely because holds()
        went silent when a trip had been re-rendered or the deploy record was
        lost. There is one answer now and it is keyed on trip ids, which is the
        looser question those states needed.
        """
        b = self.bench(Recorder(complete=YES)).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("stay there", ran.printed)
        self.assertIn("Deleting locally does not remove them", ran.printed)

    def test_a_copy_the_destination_does_not_have_is_not_claimed_to_stay(self):
        """The control. The note is about a copy that survives the drop, so a
        destination with nothing for this trip must not produce it — the
        operator would go looking for a copy that was never there."""
        b = self.bench(Recorder(complete=NO)).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertNotIn("stay there", ran.printed)

    def test_dropping_a_trip_records_that_it_was_on_purpose(self):
        """A dropped trip and a cleaned-up published trip are indistinguishable
        afterwards — id in the previous index, nothing on disk. Only the moment
        of dropping knows which it is, so it is written down there or never.

        Written into the workspace rather than announced to whoever happens to
        be configured: it then survives a restart and reaches a plugin
        installed next week.
        """
        b = self.bench(Recorder()).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(P.dropped_trip_ids(b.ctx), (TRIP,))

    def test_the_next_build_is_handed_what_was_dropped(self):
        """Which is the whole point of recording it: an index-rebuilding plugin
        deliberately carries a previously-published trip forward when its local
        output is gone, and this is the one thing that tells it not to."""
        target = Recorder()
        b = self.bench(target).complete()
        b.run(EXCLUDE, typed=["1", "DROP"])
        b.sidecars().render()               # something to build from again
        b.run(BUILD)
        self.assertEqual(target.handed[-1].dropped_ids, (TRIP,))

    def test_cancelling_records_nothing(self):
        """Nothing was dropped, so there is nothing to record — and a build
        told about a drop that did not happen would curate away a trip that is
        still there."""
        b = self.bench(Recorder()).complete()
        ran = b.run(EXCLUDE, typed=["1", "no"])
        self.assertFalse(ran.completed)
        self.assertEqual(P.dropped_trip_ids(b.ctx), ())


# ---------------------------------------------------------------------------
# A configured target that will not load stops the tool
# ---------------------------------------------------------------------------

class TestAFailureToLoadIsLoud(unittest.TestCase):
    """Silently becoming the local edition is how someone's renders quietly
    stop being published. The menu would look normal, item 6 would write a
    local page, and item 8 would go on refusing for a reason that reads like a
    network problem.

    tests/test_uploader.py pins that the loader raises. What is pinned here is
    the consequence: the TOOL stops, before a menu is drawn and therefore
    before item 8 can be reached at all.
    """

    def setUp(self):
        self.was = os.environ.get("SET_WEBSITE_UPLOADER")
        os.environ["SET_WEBSITE_UPLOADER"] = "/no/such/implementation.py:Nope"
        self.addCleanup(self._restore)

    def _restore(self):
        if self.was is None:
            os.environ.pop("SET_WEBSITE_UPLOADER", None)
            return
        os.environ["SET_WEBSITE_UPLOADER"] = self.was

    def test_the_tool_exits_before_it_takes_the_lock_or_draws_a_menu(self):
        lock = mock.patch.object(P, "acquire_single_instance_lock")
        runner = mock.patch.object(P, "build_runner")
        with quiet() as out, lock as took, runner as built:
            code = P.main()
        self.assertEqual(code, 4)
        self.assertFalse(took.called, "a broken uploader still started a session")
        self.assertFalse(built.called, "a broken uploader still drew a menu")
        self.assertIn("will not load", out.getvalue())

    def test_it_says_which_setting_and_what_was_wrong(self):
        with quiet() as out:
            P.main()
        said = out.getvalue()
        self.assertIn("website_uploader", said)
        self.assertIn("/no/such/implementation.py", said)


# ---------------------------------------------------------------------------
# NA is a real answer, and a dropped gate is not a passed one
# ---------------------------------------------------------------------------

class TestATargetThatDeclinesTheDestinationQuestion(SeamTest):
    """"The question does not arise here" is not "I could not check".

    RESTATED. There used to be two destination gates and this pinned which of
    them decided when a target declined the other — the shipped example
    answered NA to published() because a folder holds files and does not serve
    them. There is one question now, so what is left to pin is the pair that
    always mattered: NA DROPS the gate rather than passing it, and the tool
    does not describe the decision as resting on an answer nobody gave.

    An archive disk is still the general case: it stores footage and has no
    notion of publishing it.
    """

    def _cleaned(self, **answers):
        b = self.bench(Recorder(**answers)).complete()
        return b, b.run(CLEAN_WS)

    def test_declining_the_question_erases_on_the_local_count_alone(self):
        """Pinned as the trust model's consequence, not as a good state.

        A configured plugin may answer NA, and then no gate but the local
        render count applies and the footage goes. That is the settled trust
        model — whoever installed the implementation owns what it says, and an
        implementation is entitled to decline a question it genuinely cannot
        answer. What must not happen is that it goes QUIETLY, which is the next
        test.
        """
        b, ran = self._cleaned(complete=NA)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())

    def test_and_says_that_nothing_off_this_machine_was_checked(self):
        """The same sentence an unconfigured install gets, because it is the
        same hole: no copy off this machine was checked. A configured plugin
        used to buy silence here purely by being configured, and silence in
        front of an erase reads as proof."""
        _b, ran = self._cleaned(complete=NA)
        self.assertIn("Publication was NOT verified", ran.printed)
        self.assertIn("not applicable", ran.printed)
        self.assertIn("no copy off this machine was checked", ran.printed)

    def test_the_renders_are_kept_when_nobody_vouches_for_them(self):
        """Belt and braces on the same state: the footage went on the local
        count, so the renders are now the only copy of those drives and the
        sweep leaves them where they are."""
        b, _ran = self._cleaned(complete=NA)
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME])

    def test_the_gate_that_decided_is_the_gate_on_screen(self):
        """RESTATED: the banner used to name the plugin and the gate together
        — "proceeding on <name> (<loader spec>)'s answer that these renders
        are complete at the destination". The gate still has to be the one
        that actually decided; naming the plugin is what went.

        Three lines above a delete carried its name, and a screen that repeats
        a third party's name over an irreversible act reads as the tool laying
        the decision off on it. The decision is the operator's, on evidence
        this tool gathered. Whose answer it rested on is recorded in the
        ledger, which outlives the session; a screen does not.
        """
        _b, ran = self._cleaned(complete=YES)
        self.assertIn("complete at the destination", ran.printed)
        self.assertNotIn("RecordingUploader", ran.printed)
        self.assertNotIn("/a/test/plugin.py", ran.printed)


# ---------------------------------------------------------------------------
# The answer covers trips, not renders
# ---------------------------------------------------------------------------

class TestTheAnswerIsAboutTripsAndIsAllOrNothing(SeamTest):
    """RESTATED, and this is the rule that replaced an exporter-side fold.

    The exporter used to aggregate per-render answers and take the weakest, so
    the interesting case was a target that answered about four renders out of
    five: the fifth had to read UNKNOWN, because a render nobody mentioned must
    not be erased on the strength of its neighbours.

    There is nothing to aggregate now. The plugin is handed every trip id of
    the import and answers about all of them at once, so a trip it cannot
    speak for is ITS problem to turn into a NO — and the exporter's job is to
    name every trip, including the ones that produced no render.
    """

    def _two_trips(self, target):
        return self.bench(target).imported().sidecars().sidecars(
            trip=SECOND_TRIP).render().render(trip=SECOND_TRIP).grouped(trips=2)

    def test_a_plugin_that_cannot_speak_for_them_all_refuses_the_erase(self):
        b = self._two_trips(Recorder(complete=NO))
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "footage went on a partial answer")
        self.assertIn("complete at the destination", ran.note)

    def test_the_same_two_trips_go_once_it_speaks_for_both(self):
        """The control that makes the test above mean something: nothing else
        about this workspace refuses it."""
        b = self._two_trips(Recorder(complete=YES))
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())

    def test_every_trip_of_the_import_is_named_in_the_question(self):
        target = Recorder(complete=YES)
        b = self._two_trips(target)
        b.run(CLEAN_WS)
        self.assertEqual(target.trip_asks[-1], (TRIP, SECOND_TRIP))

    def test_an_older_imports_trips_are_not_in_the_question(self):
        """Which is what keeps an all-or-nothing answer from jamming shut.

        Sidecars outlive every sweep, so the tree accumulates months of trips —
        and one of them that a destination legitimately no longer serves would
        hold the erase gate closed forever if it were named. The question is
        scoped to the import under judgement.
        """
        target = Recorder(complete=YES)
        b = self.bench(target).complete()
        old = b.ctx.out_dir / "an-older-import" / DAY
        old.mkdir(parents=True)
        (old / ("trip_2026-01-01_09-00_01_meta.json")).write_text(json.dumps(
            {"day": "2026-01-01", "start": "2026-01-01 09:00:00",
             "end": "2026-01-01 10:00:00"}))
        b.world(M.Scope.FULL)
        self.assertEqual(target.trip_asks[-1], (TRIP,))


# ---------------------------------------------------------------------------
# A plugin that falls over somewhere other than the ask path
# ---------------------------------------------------------------------------

class RaisesWhenDrawn(RecordingBuilder):
    def evaluate(self, workspace):
        raise RuntimeError("fell over answering a menu draw")


class RaisesMidBuild(RecordingBuilder):
    def execute(self, workspace):
        self.script.calls.append("build")
        raise RuntimeError("fell over half way through the build")


class RaisesMidUpload(RecordingUploader):
    def execute(self, workspace):
        self.script.calls.append("upload")
        raise RuntimeError("fell over half way through the upload")


def _plugin_with(builder=None, uploader=None, **kw):
    plugin = Recorder(**kw)
    script = plugin.script
    return U.Plugin(builder(script) if builder else plugin.builder,
                    uploader(script) if uploader else plugin.uploader,
                    plugin.spec)


class TestTheDestinationsStateIsThePluginsToKeep(SeamTest):
    """The exporter remembers nothing about a destination, between prompts or
    between sessions.

    It used to: a .deployed.json beside the renders recorded what had been put
    online, and the erase gate read it. That is a second account of a far end
    the exporter cannot see, and it is wrong the moment anyone changes the site
    while the tool is closed — which is the ordinary case, since the site is
    curated from a browser.

    So the answer is asked, never stored, and a plugin that only knows what it
    did since it was constructed is wrong on the next launch. The shipped
    example shows the way out: go and look, every time.
    """

    def _cycled(self, target):
        """A workspace taken all the way to published."""
        b = self.bench(target).imported().sidecars().render().grouped(trips=1)
        b.run(BUILD)
        b.run(UPLOAD)
        return b

    def test_the_working_area_records_nothing_about_the_destination(self):
        """Whatever the exporter keeps beside the renders has to be about the
        card, the operator's own decisions, or itself. A file naming the
        destination would be an answer nobody asked the plugin for."""
        b = self._cycled(Recorder(complete=YES))
        kept = sorted(p.name for p in b.ctx.out_dir.rglob("*")
                      if p.is_file() and p.name.startswith("."))
        for name in kept:
            with self.subTest(file=name):
                self.assertIn(name, (P.LEDGER_FILE, P.EXCLUDED_FILE,
                                     P.OWNER_FILE, P.LOCK_FILE),
                              "an unexpected dotfile in the working area: %s" % name)

    def test_a_second_session_asks_again_rather_than_remembering(self):
        """The plugin is reconstructed and the count starts from zero, which is
        what a restart looks like. The erase must still consult it."""
        b = self._cycled(Recorder(complete=YES))
        fresh = Recorder(complete=YES)
        b.reopen(fresh)
        self.assertEqual(fresh.times("is_complete"), 0, "the bench started dirty")
        b.run(CLEAN_WS)
        self.assertTrue(fresh.times("is_complete"),
                        "the erase acted without asking this session's plugin")

    def test_a_destination_emptied_while_the_tool_was_closed_refuses(self):
        """The reason the rule earns its place. Last session it said yes and
        the footage stayed; this session it says no, and the erase must obey
        the answer it gets NOW rather than the one it got then."""
        b = self._cycled(Recorder(complete=YES))
        b.reopen(Recorder(complete=NO))
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "erased on last session's answer")


class TestTheLogTimesTheWorkAndNotTheLogging(unittest.TestCase):
    """A plugin's act is timed around the CALL, not after it returned.

    _logged used to be handed the outcome, so the earliest clock reading
    available to it was the moment the work had already finished, and every
    act logged 00:00:00 — a fifty-seven second deploy included. That line is
    exactly what an operator reads to find out where a session went, and it
    was the one line that could not be right.

    The clock here steps ten seconds per reading, and the fake act takes two
    readings. Timed around the call that is thirty seconds; timed after it,
    ten. Only the first is the truth about the act.
    """

    def _run_with_a_clock(self):
        ctx = mock.Mock()
        ctx.results = []
        ticks = iter([100.0, 110.0, 120.0, 130.0, 140.0, 150.0])

        def act():
            P.time.time()
            P.time.time()
            return M.did("published")

        with mock.patch.object(P.time, "time", lambda: next(ticks)):
            P._logged(ctx, UPLOAD, act)
        return ctx.results[0]

    def test_the_duration_covers_the_whole_act(self):
        self.assertEqual(self._run_with_a_clock().seconds, 30.0)

    def test_the_act_ran_exactly_once(self):
        """Taking a callable rather than a result is what makes the timing
        possible, so it must not become a way to run the work twice."""
        calls = []
        ctx = mock.Mock()
        ctx.results = []
        P._logged(ctx, UPLOAD, lambda: (calls.append(1), M.did("published"))[1])
        self.assertEqual(len(calls), 1)

    def test_the_log_column_is_always_hours_minutes_seconds(self):
        """Fixed width, so a long step is visible by shape rather than by
        reading every row."""
        self.assertEqual(P._hms(0), "00:00:00")
        self.assertEqual(P._hms(57), "00:00:57")
        self.assertEqual(P._hms(3661), "01:01:01")


class TestATripWithNoFixIsStillDescribed(SeamTest):
    """A trip the camera recorded no position for has no track and no map,
    because there is nothing to draw.

    The completeness test wanted all three sidecars — .gpx, .html and the
    meta — so such a trip could never be done, `need` stayed true, and item 2
    re-ran the whole pass every time it was pressed. Two trips on this card
    are exactly that: the camera wrote no GPS file for those days.
    """

    def _base(self, points):
        b = self.bench()
        folder = b.ctx.out_dir / "2026-07-10"
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / "trip_2026-07-10_16-23_01"
        (folder / "trip_2026-07-10_16-23_01_meta.json").write_text(
            '{"gps_points": %d, "day": "2026-07-10",'
            ' "start": "2026-07-10 16:23:38", "end": "2026-07-10 16:47:40"}' % points)
        return b, str(base)

    def test_the_meta_alone_is_enough_without_a_fix(self):
        _b, base = self._base(points=0)
        self.assertTrue(P._is_described(base))

    def test_but_not_once_the_import_holds_a_track_for_that_day(self):
        """A no-fix answer is only final while there is no track that could
        change it. The import fetches GPS whether or not it fetched clips, so
        one can land after the meta was written — and then the meta is an
        answer reached without evidence that now exists."""
        _b, base = self._base(points=0)
        self.assertFalse(P._is_described(base, frozenset({"20260710"})))
        self.assertTrue(P._is_described(base, frozenset({"20260931"})),
                        "another day's track re-opened this trip")

    def test_the_day_comes_from_the_meta_and_the_gps_from_its_name(self):
        b = self.bench()
        gps = b.ctx.render_root / "DCIM" / "203gps" / "tar" / "tmp"
        gps.mkdir(parents=True, exist_ok=True)
        (gps / "20260712191931_0120_T.git").write_text("track")
        self.assertEqual(P._gps_days(b.ctx.render_root), frozenset({"20260712"}))

    def test_a_trip_with_a_fix_still_wants_its_track_and_map(self):
        b, base = self._base(points=412)
        self.assertFalse(P._is_described(base), "a route was promised and not written")
        for ext in (".gpx", ".html"):
            Path(base + ext).write_text("x")
        self.assertTrue(P._is_described(base))

    def test_no_meta_at_all_is_not_described(self):
        b = self.bench()
        self.assertFalse(P._is_described(str(b.ctx.out_dir / "nothing")))


class TestThePreviewBoxTicksWhenThereIsAPreview(SeamTest):
    """The state of a step is read off what the step WRITES.

    _stills_current asked for previews/index.html, which is what the contact
    sheet was called before it was dated. write_contact_sheet writes
    preview_<day>.html and unlinks any index.html it finds, so the one file
    the check asked about was the one file guaranteed not to exist. The box
    never ticked, and the cold-start rule that orients onto Build Preview read
    the same false answer.
    """

    def test_a_dated_contact_sheet_counts(self):
        b = self.bench()
        previews = b.ctx.out_dir / P.PREVIEW_DIRNAME
        previews.mkdir(parents=True, exist_ok=True)
        self.assertFalse(P._stills_current(b.ctx))
        (previews / "preview_2026-07-31.html").write_text("<html/>")
        self.assertTrue(P._stills_current(b.ctx),
                        "the sheet the writer actually writes did not count")

    def test_the_name_the_writer_uses_is_the_name_that_is_checked(self):
        """Pinned against the writer rather than against a literal, so the two
        cannot drift apart again."""
        b = self.bench().imported().sidecars()
        payload = {"trips": [{"index": 1, "day": "2026-07-31",
                              "start": "2026-07-31 06:05:00",
                              "end": "2026-07-31 06:16:00", "front": []}]}
        previews = b.ctx.out_dir / P.PREVIEW_DIRNAME
        previews.mkdir(parents=True, exist_ok=True)
        with quiet():
            written = P.write_contact_sheet(b.ctx, b.ctx.render_root, payload,
                                            previews, {})
        self.assertTrue(written.is_file())
        self.assertTrue(P._stills_current(b.ctx))


class TestWipingAWorkspaceWhoseSourceIsStillInTheSlot(SeamTest):
    """"If the original data is still around I have to be able to wipe it."

    The guard exists for footage that lives in one place only. Once the card
    still holds every file, the workspace is a second copy — and what was
    MADE from it does not change that: sidecars cost seconds, renders cost
    hours but come back from the same clips.

    Before this, any step having run made the workspace unclearable without
    publishing first. Scan a card that turns out to be fragments and you could
    neither use it nor empty it.
    """

    def test_sidecars_and_renders_do_not_block_it(self):
        b = self.bench().imported().sidecars().render()
        for st in (CLIP,):
            f = b.ctx.card / "DCIM" / "200video" / "front"
            f.mkdir(parents=True, exist_ok=True)
            (f / ("%s_0060.mp4" % st)).write_text("clip")
        w = b.world(M.Scope.LOCAL)
        self.assertTrue(P.guards.import_is_disposable(w),
                        "the card holds it, so it is a second copy")
        self.assertFalse(P.guards.clean_is_allowed(w).blocked)

    def test_it_files_no_receipt_for_a_trip_that_was_never_published(self):
        """A receipt says "this trip finished". covered_stamps reads them, so
        one filed for a discarded trip tells the tool its clips sit inside a
        rendered trip — hiding them from the next import and clearing the card
        to be erased. Ten clips read as safe when they existed in one place."""
        b = self.bench().imported().sidecars().render()
        f = b.ctx.card / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        (f / ("%s_0060.mp4" % CLIP)).write_text("clip")
        before = P.read_ledger(b.ctx).get("through") or ""
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(list(P._safe_rglob(P.archive_dir(b.ctx), "trip_*_meta.json")),
                         [], "a discard filed a receipt for an unpublished trip")
        self.assertLessEqual(P.read_ledger(b.ctx).get("through") or "", before,
                             "a discard advanced the high-water mark")

    def test_the_renders_go_with_it(self):
        """He asked for everything gone. Leaving the renders leaves a
        workspace he asked to be empty half full."""
        b = self.bench().imported().sidecars().render()
        f = b.ctx.card / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        (f / ("%s_0060.mp4" % CLIP)).write_text("clip")
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())
        self.assertEqual(b.renders_on_disk(), [])

    def test_but_not_by_this_route_when_the_card_is_gone(self):
        """The half that stayed. With no card there is no second copy, so the
        discard route is closed and the publish gates decide — which is the
        path that says "this is the ORIGINAL footage" before it asks."""
        b = self.bench().imported().sidecars().render()
        w = b.world(M.Scope.LOCAL)
        self.assertFalse(P.guards.import_is_disposable(w))
        self.assertIn("ORIGINAL footage", "\n".join(P._what_goes_lines(w)))

    def test_and_the_original_warning_is_not_shown_when_it_is_a_copy(self):
        b = self.bench().imported().sidecars().render()
        f = b.ctx.card / "DCIM" / "200video" / "front"
        f.mkdir(parents=True, exist_ok=True)
        (f / ("%s_0060.mp4" % CLIP)).write_text("clip")
        said = "\n".join(P._what_goes_lines(b.world(M.Scope.LOCAL)))
        self.assertNotIn("ORIGINAL", said)
        self.assertIn("removes 1 trips and the metadata", said)
        self.assertIn("still on the SIM card", said)


class TestEveryRendererRunIsToldWhereToLog(SeamTest):
    """make-trips-rendered.sh defaults LOG_DIR to <out>/logs.

    So a call that does not pass it writes its run log into the EXPORT tree —
    beside the sidecars, inside the directory item 8 sweeps, and nowhere near
    the logs/ the workspace keeps at its root. Only the render step passed it,
    so every sidecar pass logged in the wrong place.
    """

    def _envs(self, run):
        return [c.kwargs.get("env_extra") for c in run.call_args_list]

    def test_the_sidecar_pass_carries_it(self):
        b = self.bench().imported()
        payload = {"trips": [{"index": 1, "renderable": True, "day": "2026-07-31",
                              "out_base": str(b.ctx.out_dir / "nope")}]}
        with quiet(), mock.patch.object(P, "load_groups", return_value=payload), \
                mock.patch.object(P, "run_stream", return_value=(0, [])) as run:
            P.step_generate_meta(b.ctx)
        self.assertTrue(run.called)
        for env in self._envs(run):
            self.assertEqual(env, {"LOG_DIR": str(b.ctx.log_dir)})

    def test_and_the_log_dir_is_the_workspace_root(self):
        b = self.bench()
        self.assertEqual(b.ctx.log_dir, b.ctx.workspace / "logs")
        self.assertNotIn(str(b.ctx.out_dir), str(b.ctx.log_dir))


class TestGpsAloneIsStillWorkToDo(SeamTest):
    """A card can hold tracks for clips that came over before the tracks did.

    Nothing new by the clip count, and a trip that cannot be described until
    they arrive — so item 1 has work even though new_stamps is empty, and
    saying "everything on the card is already here" would be false.
    """

    def _imported_without_its_track(self):
        b = self.bench()
        dcim = b.ctx.card / "DCIM"
        (dcim / "200video" / "front").mkdir(parents=True, exist_ok=True)
        (dcim / "200video" / "front" / ("%s_0060.mp4" % CLIP)).write_text("clip")
        (dcim / "203gps").mkdir(parents=True, exist_ok=True)
        (dcim / "203gps" / "20260101000000_0480_T.git").write_text("track")
        # the clip is already in the workspace; its track is not
        imp = b.ctx.render_root / "DCIM" / "200video" / "front"
        imp.mkdir(parents=True, exist_ok=True)
        (imp / ("%s_0060.mp4" % CLIP)).write_text("clip")
        return b

    def test_the_track_alone_counts_as_work(self):
        b = self._imported_without_its_track()
        _here, todo, _size, files = P._delta_counts(b.ctx, "")
        self.assertEqual(todo, 0, "no clip is missing")
        self.assertEqual([f for f in files if "203gps" in f],
                         ["DCIM/203gps/20260101000000_0480_T.git"])

    def test_and_item_one_offers_to_do_it(self):
        b = self._imported_without_its_track()
        built = M.build_menu(M.Strategy.LOCAL_PAGE, P.Work(b.ctx))
        self.assertIs(built[1].evaluate(b.world(M.Scope.LOCAL)).ruling, M.Ruling.GO)

    def test_and_stops_offering_once_it_is_here(self):
        b = self._imported_without_its_track()
        dst = b.ctx.render_root / "DCIM" / "203gps"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "20260101000000_0480_T.git").write_text("track")
        _here, _todo, _size, files = P._delta_counts(b.ctx, "")
        self.assertEqual(files, [], "it offered what the workspace already had")


class TestTheGpsComesWithTheClips(SeamTest):
    """A GPS archive is stamped with ITS OWN start and a span —
    "20260712191931_0120_T.git" — so it almost never carries a clip's stamp.

    Matched by stamp, an import took two GPS files off a card that held thirty
    for that day, and the trip came out with gps_points 0: a drive with no
    route, from footage whose track was sitting right there on the card.
    """

    def _card(self):
        b = self.bench()
        dcim = b.ctx.card / "DCIM"
        (dcim / "200video" / "front").mkdir(parents=True, exist_ok=True)
        (dcim / "200video" / "front" / "20260712192031_0060.mp4").write_text("clip")
        (dcim / "203gps" / "tar").mkdir(parents=True, exist_ok=True)
        (dcim / "203gps" / "tar" / "20260712191931_0120_T.git").write_text("track")
        (dcim / "201photo").mkdir(parents=True, exist_ok=True)
        (dcim / "201photo" / "G_20260507144616_745_0000_X.jpg").write_text("photo")
        (dcim / "IPSRecord.txt").write_text("log")
        return b

    def test_a_track_whose_stamp_matches_no_clip_still_comes(self):
        b = self._card()
        files = P._files_for(b.ctx.card, frozenset({"20260712192031"}))
        self.assertIn("DCIM/203gps/tar/20260712191931_0120_T.git", files)

    def test_the_clip_and_the_camera_log_come_too(self):
        b = self._card()
        files = P._files_for(b.ctx.card, frozenset({"20260712192031"}))
        self.assertIn("DCIM/200video/front/20260712192031_0060.mp4", files)
        self.assertIn("DCIM/IPSRecord.txt", files)

    def test_what_nothing_reads_does_not(self):
        """750 MB of stills and thumbnails on a full card, into a workspace
        whose whole purpose is to be rendered from and thrown away."""
        b = self._card()
        files = P._files_for(b.ctx.card, frozenset({"20260712192031"}))
        self.assertEqual([f for f in files if "201photo" in f], [])

    def test_a_clip_not_asked_for_does_not(self):
        b = self._card()
        self.assertEqual(
            [f for f in P._files_for(b.ctx.card, frozenset()) if "200video" in f], [])


class TestWhatIsOfferedIsWhatIsFetched(SeamTest):
    """The screen's count and the script's filter have to mean one thing.

    to_import() offers a clip that is owed whatever its date. The script skips
    everything at or before AFTER_STAMP. Between them, "14 clips to import
    (2.5 GB)" fetched one untimestamped file: every owed clip sat below the
    mark and the filter dropped all of them.
    """

    def _counts(self, card_stamps, mark, excluded=()):
        b = self.bench()
        front = b.ctx.card / "DCIM" / "200video" / "front"
        front.mkdir(parents=True, exist_ok=True)
        for st in card_stamps:
            (front / ("%s_0060.mp4" % st)).write_text("clip")
        if mark:
            P.write_ledger(b.ctx, mark, "test fixture")
        if excluded:
            P.record_excluded_stamps(b.ctx, set(excluded))
        return P._delta_counts(b.ctx, mark)

    def test_the_list_holds_exactly_what_was_offered(self):
        """The old clip is accounted for — dropped on purpose — so only the
        new one is wanted, and only the new one is in the list."""
        _here, todo, _size, files = self._counts(
            ["20260724185433", "20260801120000"], "20260724185433",
            excluded=["20260724185433"])
        self.assertEqual(todo, 1)
        self.assertEqual([f for f in files if f.endswith(".mp4")],
                         ["DCIM/200video/front/20260801120000_0060.mp4"])

    def test_a_clip_older_than_the_mark_is_in_the_list_too(self):
        """The case that broke both ways. AFTER_STAMP would skip it; no filter
        at all would fetch the whole card. The list names it and nothing
        else."""
        _here, todo, _size, files = self._counts(
            ["20260502102459", "20260801120000"], "20260724185433")
        self.assertEqual(todo, 2, "the old clip was not offered")
        self.assertEqual(sorted(f for f in files if f.endswith(".mp4")),
                         ["DCIM/200video/front/20260502102459_0060.mp4",
                          "DCIM/200video/front/20260801120000_0060.mp4"])

    def test_what_is_weighed_is_what_is_listed(self):
        """The bar's total and the offer are the same bytes, so a copy cannot
        run past 100% — it read "9.8 GB/2.5 GB" when they were computed
        apart."""
        b = self.bench()
        front = b.ctx.card / "DCIM" / "200video" / "front"
        front.mkdir(parents=True, exist_ok=True)
        (front / "20260801120000_0060.mp4").write_text("x" * 500)
        _here, _todo, size, files = P._delta_counts(b.ctx, "")
        self.assertEqual(size, P._weigh(b.ctx.card, files))


class TestTheWayPastTheCardRefusal(SeamTest):
    """The one refusal with a way past it, and what the way past costs.

    "13 clips exist nowhere but this card" is true and is sometimes not a
    reason to keep them. The way past is not a flag that skips the guard: it
    records the clips as dropped on purpose — the same act item 4 performs per
    trip — which makes the refusal FALSE, and the erase then passes the same
    gates any erase passes.
    """

    def _carded(self, stamps=("20260502102459",)):
        b = self.bench()
        front = b.ctx.card / "DCIM" / "200video" / "front"
        front.mkdir(parents=True, exist_ok=True)
        for st in stamps:
            (front / ("%s_0060.mp4" % st)).write_text("clip")
        # A mark, so the refusal under test is the clip accounting and not
        # "nothing was ever imported" — which is a different guard with no way
        # past it, and rightly so.
        P.write_ledger(b.ctx, "20260731061615", "test fixture")
        return b, b.world(M.Scope.LOCAL)

    def test_it_records_the_drop_and_then_erases(self):
        b, w = self._carded()
        self.assertTrue(P.guards.card_is_expendable(w).blocked)
        with quiet(), mock.patch.object(P, "ask", return_value="DELETE"):
            out = P.drop_unaccounted_then_erase(b.ctx, w)
        self.assertTrue(out.completed, out.note)
        self.assertIn("20260502102459", P.excluded_stamps(b.ctx))
        self.assertFalse(any(p.is_file() for p in
                             (b.ctx.card / "DCIM").rglob("*")), "the card kept files")

    def test_the_guard_is_asked_again_and_can_still_refuse(self):
        """Recording the drop is not permission. Anything else standing in the
        way stops it, with the ledger changed and the card untouched."""
        b, w = self._carded()
        import guards as G
        with quiet(), mock.patch.object(G, "card_is_expendable",
                                        return_value=M.blocked("something else")):
            out = P.drop_unaccounted_then_erase(b.ctx, w)
        self.assertFalse(out.completed)
        self.assertIn("something else", out.note)
        self.assertTrue(any(p.is_file() for p in (b.ctx.card / "DCIM").rglob("*")),
                        "the card was erased through a standing refusal")

    def test_only_an_item_that_declares_a_word_offers_one(self):
        built = M.build_menu(M.Strategy.LOCAL_PAGE, P.Work(self.bench().ctx))
        self.assertEqual(built[ERASE_CARD].OVERRIDE_WORD, "DELETE")
        self.assertNotEqual(built[ERASE_CARD].OVERRIDE_WORD,
                            built[ERASE_CARD].WORD,
                            "the way past a guard is reachable by habit")
        self.assertEqual([n for n, i in built.items() if i.OVERRIDE_WORD],
                         [ERASE_CARD], "a second item grew a way past its guard")


class TestARefusalIsAlwaysInTheLog(SeamTest):
    """A step that decided not to act still ran, and the summary is the record
    of what ran.

    Item 8 recorded its plan-time exits through _nothing(); item 4 returned a
    plan and logged nothing, so "no import folder", "the trip scan failed",
    "no trips" and "cancelled" left the summary empty for a step the operator
    had just watched refuse. Item 2 was worse than silent: it ran the whole
    sidecar pass AFTER the scan it reads had failed and said so in red.
    """

    def _rows(self, b, number):
        return [r for r in b.ctx.results if r.name == items.NAMES[number]]

    def test_item_four_records_a_scan_that_failed(self):
        b = self.bench().imported()
        with quiet(), mock.patch.object(P, "load_groups", return_value=None):
            P.drop_plan(b.ctx, b.world())
        rows = self._rows(b, EXCLUDE)
        self.assertTrue(rows, "item 4 refused and left no summary row")
        self.assertEqual(rows[-1].detail, "the trip scan failed")

    def test_item_four_records_an_import_with_no_trips(self):
        b = self.bench().imported()
        with quiet(), mock.patch.object(P, "load_groups", return_value={"trips": []}):
            P.drop_plan(b.ctx, b.world())
        rows = self._rows(b, EXCLUDE)
        self.assertTrue(rows, "item 4 refused and left no summary row")
        self.assertEqual(rows[-1].detail, "no trips")

    def test_item_two_stops_when_the_scan_it_reads_has_failed(self):
        """It used to carry on: minutes of sidecar pass behind a red failure
        the operator had already been shown."""
        b = self.bench().imported()
        # run_stream returns (rc, lines); a bare Mock unpacks with a TypeError
        # rather than a clean assertion, so it is given the shape of a success.
        with quiet(), mock.patch.object(P, "load_groups", return_value=None), \
                mock.patch.object(P, "run_stream", return_value=(0, [])) as ran:
            P.step_generate_meta(b.ctx)
        ran.assert_not_called()
        rows = self._rows(b, META)
        self.assertTrue(rows)
        self.assertEqual(rows[-1].status, P.FAILED)
        self.assertEqual(rows[-1].detail, "the trip scan failed")


class TestTheEraseAsksASecondTime(SeamTest):
    """RESTATED. It asserted that the re-check tells the plugin to forget
    first, so a cached answer could not be handed back.

    That was this module compensating for a contract it should rely on. An act
    answers for the state it is in, so the same state gives the same answer
    however many times it is asked — and reaching past a cache to try to get a
    different answer out of an unchanged state is either pointless or an
    admission the contract is not believed. A plugin that is wrong about its
    own destination is wrong; that is not a question asked badly.

    What survives, and is what the second ask was always for: the world is
    captured again after the word, so anything that moved under the prompt is
    seen, and an act that changes its answer stops the erase.
    """

    def test_a_plugin_that_changes_its_mind_stops_the_erase(self):
        target = Recorder(complete=[YES, NO])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed, ran.note)
        self.assertTrue(b.footage_on_disk(), "erased on the pre-prompt answer")

    def test_it_is_asked_twice(self):
        """Once for the banner, once for the act. One ask would mean acting on
        a world that is a confirmation prompt old."""
        target = Recorder(complete=[YES, YES])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertGreaterEqual(target.times("is_complete"), 2)


class TestATargetThatFallsOverAwayFromTheAskPath(SeamTest):
    """capture_world already reads an exception from is_complete() as
    "unreachable". The three places an implementation can raise that are NOT on
    that path had nothing holding them.

    They must not be equivalent. A build that dies is one item failing, which
    is what a menu is for. A menu DRAW that dies is the tool gone — and the
    draw is where an act's evaluate() is asked, forty times a session, in a
    stranger's code.
    """

    def test_a_menu_draw_survives_a_plugin_that_raises_while_being_drawn(self):
        b = self.bench(_plugin_with(builder=RaisesWhenDrawn)).complete()
        built, world = b.menu(), b.world(M.Scope.LOCAL)
        with self.assertRaises(RuntimeError):
            built[BUILD].evaluate(world)           # it really does raise
        with quiet():
            P.print_menu(b.ctx, built, M.position_for(built), world)
        self.assertTrue(P._safe_verdict(built[BUILD], world).blocked,
                        "an item whose plugin raised was still offered")

    def test_a_plugin_that_raises_mid_build_fails_the_item_not_the_session(self):
        """The position stays where it was, the failure is logged under this
        session's results, and the next menu offers the same choices."""
        b = self.bench(_plugin_with(builder=RaisesMidBuild)).complete()
        runner = P.build_runner(b.ctx)
        with quiet():
            outcome = runner._execute(runner.menu[BUILD],
                                       P.capture_world(b.ctx, M.Scope.FULL))
        self.assertFalse(outcome.completed)
        self.assertEqual([r.status for r in b.ctx.results], [P.FAILED])

    def test_a_plugin_that_raises_mid_upload_fails_the_item_not_the_session(self):
        b = self.bench(_plugin_with(uploader=RaisesMidUpload,
                                    complete=NO)).complete()
        runner = P.build_runner(b.ctx)
        with quiet():
            outcome = runner._execute(runner.menu[UPLOAD],
                                       P.capture_world(b.ctx, M.Scope.FULL))
        self.assertFalse(outcome.completed)
        self.assertEqual([r.status for r in b.ctx.results], [P.FAILED])


# ---------------------------------------------------------------------------
# The shipped example, driven by the suite so it cannot rot into a lie
# ---------------------------------------------------------------------------

class TestTheShippedExampleIsRunAndNotJustRead(SeamTest):
    """examples/local_website.py is what an implementer copies.

    test_uploader.py calls its methods; nothing drove the real items through
    it. An example that is only READ drifts from the interface the moment the
    interface moves, and it drifts in the direction of the person copying it.
    This runs the documented arc — 6, 7, 8, and 6 and 7 again — through the
    REAL loader, from the same spec string the file's own docstring tells you
    to write.
    """

    def _example(self):
        home = Path(tempfile.mkdtemp(prefix="dashcam-seam-example-"))
        self.addCleanup(shutil.rmtree, str(home), True)
        dest = home / "dest"
        patched = mock.patch.dict(os.environ, {
            "DASHCAM_LOCAL_SITE_STAGING": str(home / "staging"),
            "DASHCAM_LOCAL_SITE_DEST": str(dest)})
        patched.start()
        self.addCleanup(patched.stop)
        return self.bench(U.load_plugin(EXAMPLE_SPEC, REPO)).complete(), dest

    def test_the_documented_arc_runs_end_to_end(self):
        b, dest = self._example()
        self.assertTrue(b.run(BUILD).completed)
        self.assertEqual(b.pages(), [], "the example wrote the other product's page")
        self.assertTrue(b.run(UPLOAD).completed)
        self.assertIn(TRIP + ".html", [p.name for p in dest.iterdir()])

    def test_running_the_pair_twice_says_nothing_to_do(self):
        """The path an implementor gets wrong first, driven through the real
        runner: the second upload answers SATISFIED, so the item completes
        without the plugin doing the work again and the menu says so."""
        b, _dest = self._example()
        b.run(BUILD)
        b.run(UPLOAD)
        b.run(BUILD)
        again = b.run(UPLOAD)
        self.assertTrue(again.completed, again.note)
        self.assertFalse(again.outcome.performed)
        self.assertIn("Nothing to do", _runner_said(b, UPLOAD))

    def test_the_erase_rests_on_the_examples_own_answer(self):
        """And says which gate decided, without naming who answered it."""
        b, _dest = self._example()
        b.run(BUILD)
        b.run(UPLOAD)
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())
        self.assertIn("complete at the destination", ran.printed)
        self.assertNotIn("LocalWebSiteUploader", ran.printed)

    def test_an_upload_that_never_happened_keeps_the_footage(self):
        """The destination has nothing, so it says NO — and the erase is
        refused by a real implementation rather than by a stub."""
        b, _dest = self._example()
        b.run(BUILD)
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())


def _runner_said(bench, number):
    """What the real runner prints for an item that had nothing to do.

    run_one rather than _execute: the "Nothing to do" line is the runner's
    reading of an outcome that completed without performing, and that reading
    is the thing under test.
    """
    runner = P.build_runner(bench.ctx)
    with quiet() as out:
        runner.run_one(number)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The local edition's own arc, end to end
# ---------------------------------------------------------------------------

class TestTheLocalEditionsOwnArc(SeamTest):

    def _built(self):
        b = self.bench().complete()
        with mock.patch.object(P, "still_data_uri", return_value=""):
            b.run(BUILD)
        return b

    def test_the_deliverable_is_a_page_and_a_gathered_folder(self):
        b = self._built()
        self.assertEqual(len(b.pages()), 1)
        self.assertEqual(len(b.finals()), 1)
        self.assertEqual(b.renders_on_disk(), [],
                         "the renders were left loose in the working area")

    @unittest.expectedFailure
    def test_cleaning_up_after_a_local_build_is_possible(self):
        """LEFT FAILING ON PURPOSE — a dead end in the local edition's graph,
        and the fix is a design call rather than a test's to assume.

        Item 6's outbound offers item 8 under LOCAL_PAGE, and the owner's own
        inbound column for item 8 lists item 6. But gather_into_final moves the
        whole day folder — renders AND sidecars — into final_<day>, so after a
        local build the working area has no sidecars (item 8's cheap guard
        refuses) and no renders under this import's namespace (the floor,
        nothing_was_rendered_here, refuses). Re-running item 2 clears the first
        and not the second, so the state is terminal: the local operator can
        never clean the workspace after building the thing that was supposed to
        make it expendable.

        Both refusals are FAIL-CLOSED, so nothing is at risk — this costs disk,
        not data, which is why it is documented here rather than patched under
        the erase path. It is also not new with the interface: the old Gatherer
        moved the same files.

        Three ways out, and picking one is the owner's:
          * the floor counts a render inside this import's final_ folder, which
            is the same evidence working_area_is_expendable already accepts;
          * gather leaves the sidecars in the working area, where the card
            guard and the delta import read them from anyway;
          * item 6 stops offering item 8 under the local edition, which admits
            the arc does not exist rather than fixing it.
        When one is taken this test starts passing, and unittest reports an
        unexpected success as a FAILURE — so the tripwire is loud in the
        direction that matters.
        """
        b = self._built()
        b.sidecars()                    # as if item 2 had been re-run
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
