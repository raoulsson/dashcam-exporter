#!/usr/bin/env python3
"""The seam itself, driven: the real items 6, 7 and 8 against a target this
repo does not own.

The other files stop short of this on purpose. test_uploader.py states what an
Answer MEANS and what the loader refuses; test_wiring.py binds the signatures;
test_paths.py drives the graph with every body mocked; test_spec.py asks the
real ten what they think of a real workspace but only ever LOOKS at their
verdicts. None of them executes an item whose work is a stranger's code.

That is the one thing the interface exists to make possible, so it is the thing
this file does: a target under the test's control is handed to a real
pipeline.Work over a real temporary workspace, and the ten real items are then
run at it. What it pins, that nothing pinned before:

  * item 6 under a configured target writes NO local page and gathers nothing
    — the reported bug, as a property of a run rather than of the wiring;
  * item 6 and item 7 take their reasons, their descriptions and their
    "is there anything left to do" from the target and not from the disk;
  * the local edition still builds its page, and item 7 is unreachable there
    by the edges rather than by a refusal in a body;
  * a target that raises, a target that cannot say, and a target that will not
    load all fail CLOSED at item 8 — and a target that answers yes to
    everything still cannot erase an import that produced no renders;
  * the destructive re-check really does ask the target a SECOND time, and a
    target that changes its mind between the banner and the typed word stops
    the erase;
  * a menu draw never asks the questions that leave the machine;
  * both items are idempotent, counted in calls made to the target.

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
from menu import (EXCLUDE, BUILD, UPLOAD, CLEAN_WS)              # noqa: E402


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
EXAMPLE = REPO / "examples" / "uploader_folder.py"


@contextlib.contextmanager
def quiet():
    """The items print. A test reads what happened, not the ink."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


# ---------------------------------------------------------------------------
# The target under the test's control
# ---------------------------------------------------------------------------

def _scripted(answers):
    """One answer per ask, the last one repeating.

    So a test can make a target CHANGE ITS MIND between the world the banner
    was drawn from and the world captured after CLEAN was typed. That is the
    only way to tell an exporter that re-asks from one that remembers, and
    remembering is precisely what the destructive re-check exists to defeat.
    """
    if len(answers) > 1:
        return answers.pop(0)
    return answers[0]


_ANSWER = {
    UNKNOWN: lambda names: U.Answers.unknown("the target could not look"),
    NA: lambda names: U.Answers.not_applicable("the question does not arise here"),
}


def _answer(reading, names):
    """An Evidence, as the Answers a real target would build.

    UNKNOWN and NA go through their own constructors rather than being written
    into the per-name dict, because that is the discipline the interface
    imposes on a real implementation: "could not find out" is not a reading you
    can spell the same way as a reading.
    """
    return _ANSWER.get(reading, lambda ns: U.Answers.of(dict.fromkeys(ns, reading)))(names)


class Recorder(U.WebsiteUploaderInterface):
    """A publishing target that writes down every question it is asked.

    Everything about it is the test's: what it answers, whether it raises,
    whether its answer changes on the second ask. That is the whole point of
    the seam — before it, a test could not supply a destination at all, and the
    exporter's questions about one were bucket listings inside its own guards.
    """

    ALL = "everything"

    def __init__(self, holds=YES, published=YES, owed=ALL, raises=None,
                 builds=True, uploads=True, no_build=None, no_upload=None,
                 settles=False, carries=None,
                 describes="Push the drives onto the test's own shelf."):
        self.calls = []            # every question asked, in order
        self.uis = []              # what was handed over as a Ui
        self.dropped_ids = []
        self._holds = _as_list(holds)
        # carries() is a LOOSER question than holds() — "is there anything
        # there for this trip", not "this exact file at this exact size, fully
        # published" — so a real target answers them differently and the
        # default here follows holds only so the tests that predate the knob
        # keep meaning what they meant.
        self._carries = _as_list(holds if carries is None else carries)
        self._published = _as_list(published)
        self._owed = owed
        self._raises = raises
        self._builds = builds
        self._uploads = uploads
        self._no_build = no_build
        self._no_upload = no_upload
        self._settles = settles
        self._describes = describes

    # -- what the test reads afterwards ------------------------------------
    def times(self, question):
        return self.calls.count(question)

    def asked(self, *questions):
        return [c for c in self.calls if c in questions]

    # -- who answered ------------------------------------------------------
    def describe(self):
        return self._describes

    # -- the work ----------------------------------------------------------
    def build(self, world, ui):
        self.calls.append("build")
        self.uis.append(ui)
        return U.Report(self._builds, "built by the test's target")

    def upload(self, world, ui):
        self.calls.append("upload")
        self.uis.append(ui)
        return self._uploaded()

    def _uploaded(self):
        if self._settles:
            self._owed = ()
        return U.Report(self._uploads, "uploaded by the test's target")

    # -- may it run --------------------------------------------------------
    def why_not_build(self, world):
        self.calls.append("why_not_build")
        return self._no_build

    def why_not_upload(self, world):
        self.calls.append("why_not_upload")
        return self._no_upload

    def owes(self, renders):
        self.calls.append("owes")
        self._maybe_raise()
        return self._owes(tuple(r.name for r in renders))

    def _owes(self, names):
        if self._owed == self.ALL:
            return U.Owed.just(names)
        return U.Owed.just(self._owed)

    # -- what is already at the destination --------------------------------
    def holds(self, renders):
        self.calls.append("holds")
        self._maybe_raise()
        return _answer(_scripted(self._holds), [r.name for r in renders])

    def published(self, renders):
        self.calls.append("published")
        self._maybe_raise()
        return _answer(_scripted(self._published), [r.name for r in renders])

    def carries(self, trip_ids):
        self.calls.append("carries")
        self._maybe_raise()
        return _answer(_scripted(self._carries), list(trip_ids))

    def dropped(self, trip_ids, ui):
        self.calls.append("dropped")
        self.dropped_ids.extend(trip_ids)

    def _maybe_raise(self):
        """A target that falls over while being asked.

        An implementation is trusted about what it SAYS, and an exception is
        not a thing it said. The exporter has to read this as unreachable, not
        as consent.
        """
        if self._raises:
            raise RuntimeError(self._raises)


def _as_list(reading):
    if isinstance(reading, list):
        return list(reading)
    return [reading]


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

    def __init__(self, target=None):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-seam-"))
        self.target = target
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
        c.uploader = target
        c.uploader_origin = _origin(target)
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
        return M.build_menu(M.Strategy.of(self.ctx.uploader), P.Work(self.ctx))

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


def _origin(target):
    if target is None:
        return ""
    return U.origin_of("/a/test/target.py:Recorder", target)


class SeamTest(unittest.TestCase):
    def bench(self, target=None):
        b = Bench(target)
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
        """An implementation is lent the tool's Ui so its work looks like the
        rest of the session. If that ever stops being a real Ui, every target
        that used it breaks at once, and it breaks inside somebody else's code.
        """
        target = Recorder()
        self.bench(target).complete().run(BUILD)
        self.assertTrue(all(isinstance(ui, U.Ui) for ui in target.uis))

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
        self.assertIn("no renders", ran.note)
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
        target = Recorder()
        ran = self.bench(target).complete().run(UPLOAD)
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(target.times("upload"), 1)

    def test_whether_anything_is_left_to_do_is_the_targets_answer(self):
        """Not the disk's. Nothing has been uploaded anywhere in this test and
        the renders are all sitting in the workspace — the only thing that can
        make this SATISFIED is the target saying it owes nothing."""
        target = Recorder(owed=())
        b = self.bench(target).complete()
        verdict = b.evaluate(UPLOAD)
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)
        self.assertEqual(target.times("upload"), 0)

    def test_a_target_that_could_not_be_asked_still_offers_the_upload(self):
        """A failed listing proves nothing, so everything stays outstanding and
        the offer stands — the UPLOAD is what discovers the target is down. The
        opposite reading would report an install as fully published because its
        destination was unreachable."""
        b = self.bench(Recorder(raises="no route to the host")).complete()
        world = b.world()
        self.assertTrue(world.target.owed.any)
        self.assertFalse(world.target.owed.certain)
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
        and a sidecar somewhere to describe it. Neither is asked of the target,
        and the target answering yes to everything does not move them."""
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
        """NA, not UNKNOWN. The two destination gates are dropped rather than
        failed, so the erase falls back to unanimity over the gate that never
        leaves this machine — and the banner says publication could not be
        verified rather than that it failed."""
        world = self.bench().complete().world()
        self.assertFalse(world.target.configured)
        self.assertIs(world.target.holds.covers(world.renders_here), NA)
        self.assertIs(world.target.published.covers(world.renders_here), NA)
        self.assertTrue(P.guards.unproven_lines(world))


# ---------------------------------------------------------------------------
# Item 8 — no answer, and no absence of one, may read as permission
# ---------------------------------------------------------------------------

class TestNothingUnprovenErasesFootage(SeamTest):

    def _erased(self, target, typed=None):
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS, typed=typed)
        return b, ran

    def test_a_target_that_says_yes_erases_the_footage(self):
        """The positive control. Without it every refusal below could be
        passing for some reason that has nothing to do with the answer."""
        b, ran = self._erased(Recorder(holds=YES, published=YES))
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
        b, ran = self._erased(Recorder(holds=UNKNOWN, published=UNKNOWN))
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())

    def test_a_target_that_says_no_refuses(self):
        b, ran = self._erased(Recorder(holds=NO, published=NO))
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())

    def test_a_yes_to_everything_cannot_erase_an_import_that_produced_nothing(self):
        """Requirement A, at the item that erases footage.

        "Were these trips rendered on this machine" never leaves home, so a
        target answering yes to every question it is asked still cannot talk
        this into erasing an import with no renders — it is not asked, and the
        answer it would have given is not consulted.
        """
        target = Recorder(holds=YES, published=YES)
        b = self.bench(target).imported().sidecars().grouped()   # nothing rendered
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())
        self.assertIn("nothing from this import was rendered", ran.note)

    def test_the_renders_stay_when_the_target_does_not_hold_them(self):
        """The second, separately-proven half of the erase. Even with the
        footage gone, a render the target does not hold is the only copy of
        that drive and is kept — the sweep is not part of the same permission.
        """
        b = self.bench(Recorder(holds=NO, published=YES)).complete()
        b.run(CLEAN_WS)
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME],
                         "a render nobody holds was swept")

    def test_only_a_yes_clears_a_render_and_an_unknown_is_not_one(self):
        """The sweep's own rule, which is stricter than the gate above it.

        The erase proceeded on "served", so the footage is gone — and a render
        the target could not speak for is now the only copy of that drive.
        UNKNOWN keeps it. Anything looser sweeps a render on the strength of a
        question nobody answered, which is the expensive direction of the same
        mistake the gates are built to avoid.
        """
        b = self.bench(Recorder(holds=UNKNOWN, published=YES)).complete()
        b.run(CLEAN_WS)
        self.assertFalse(b.footage_on_disk(), "the erase itself did not happen")
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME],
                         "a render nobody could vouch for was swept")

    def test_the_erase_says_whose_answer_it_acted_on(self):
        """Attribution, in the record that outlives the screen. Not a
        safeguard — a component inside the trust boundary needs none — but the
        question "who said the footage was safe" gets asked weeks later."""
        b, ran = self._erased(Recorder())
        cleaned = [r for r in b.ctx.results if r.name == items.NAMES[CLEAN_WS]]
        self.assertTrue(cleaned)
        self.assertIn("/a/test/target.py:Recorder", cleaned[-1].detail)

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

        The target says published when the banner is drawn and cannot say when
        the world is captured after CLEAN is typed — a destination that went
        down while the prompt was on screen. The second answer is the one that
        counts, and it refuses.
        """
        target = Recorder(published=[YES, UNKNOWN], holds=[YES, UNKNOWN])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "footage went on a pre-prompt answer")
        self.assertIn("refused after re-check", ran.note)
        self.assertGreaterEqual(target.times("published"), 2,
                                "the target was asked once and remembered")

    def test_the_erase_that_proceeds_was_asked_twice_and_not_remembered(self):
        """The same path when the answer holds up. Two asks, not one — the
        first drew the banner, the second is what the act was permitted on."""
        target = Recorder(published=[YES, YES], holds=[YES, YES])
        b = self.bench(target).complete()
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertGreaterEqual(target.times("published"), 2)

    def test_a_menu_draw_never_asks_the_questions_that_leave_the_machine(self):
        """The scope rule, stated in calls made rather than in a comment.

        The menu is redrawn on every keystroke. If holds/published/owes/carries
        were asked there, every target that goes to the network would make the
        menu unusable — and a menu that is not instant stops being recomputed
        and starts being remembered, which is the one thing a greying rule must
        never be. At LOCAL scope a configured target reads UNKNOWN everywhere,
        which no guard treats as proven.
        """
        target = Recorder()
        b = self.bench(target).complete()
        local = b.world(M.Scope.LOCAL)
        self.assertEqual(target.asked("holds", "published", "owes", "carries"), [])
        self.assertIs(local.target.holds.covers(local.renders_here), UNKNOWN)
        self.assertTrue(local.target.configured)

        b.world(M.Scope.FULL)
        self.assertEqual(sorted(set(target.asked("holds", "published", "owes",
                                                 "carries"))),
                         ["carries", "holds", "owes", "published"])


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
        seam: once the target says it owes nothing the item completes without
        the target doing the work again. An exporter that re-ran it would push
        the whole set on every keypress."""
        target = Recorder(settles=True)
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
# Item 4 — the two questions the interface grew for it
# ---------------------------------------------------------------------------

class TestExcludeTripAsksTheTarget(SeamTest):
    """The half of the seam that feeds a WARNING rather than a gate.

    Both questions here exist because holds() cannot answer them, and both are
    easy to lose quietly. carries() is asked about a trip that may have no
    local render at all — there is no Render to key on and no size to compare —
    and dropped() is the one thing the exporter TELLS a target rather than asks
    it. A warning that silently stops firing is worse than one that never
    existed, and a drop the target is never told about leaves the trip live
    forever.
    """

    def _published_then_cleaned_up(self, target):
        """The case the id-keyed question was written for: the trip is at the
        destination and its local render is long gone."""
        return self.bench(target).imported().sidecars().grouped()

    def test_a_trip_the_target_carries_is_not_called_the_only_copy(self):
        """No render on disk and nothing but the target's word for it. If this
        panel fires here it fires over every published trip anyone ever cleans
        up, and an operator who sees it wrongly once stops reading it."""
        target = Recorder(holds=YES)          # carries answers off the same script
        b = self._published_then_cleaned_up(target)
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        # Twice, not once: item 4 captures a full world to plan with and
        # another after the word, because Destructive re-derives whatever the
        # guard it was given happens to read.
        self.assertGreaterEqual(target.times("carries"), 1,
                                "the target was never asked")
        self.assertNotIn("ONLY copy", ran.printed)
        self.assertIn("also exist", ran.printed)

    def test_a_trip_nobody_can_vouch_for_gets_the_full_warning(self):
        """The other half, which is what makes the first half meaningful: a
        target that cannot say does NOT suppress the panel, and the caveat
        says which of the three silences this was."""
        b = self._published_then_cleaned_up(Recorder(holds=UNKNOWN))
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("ONLY copy", ran.printed)
        self.assertIn("could not answer", ran.printed)

    def test_with_no_target_the_warning_says_nothing_is_off_this_machine(self):
        """Three silences, three sentences. No target at all is not the same as
        a target that timed out, and saying so wrongly in a delete prompt is a
        lie the operator acts on."""
        b = self.bench().imported().sidecars().grouped()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("ONLY copy", ran.printed)
        self.assertIn("No website_uploader is configured", ran.printed)

    def test_a_copy_that_stays_behind_is_named_even_when_holds_says_no(self):
        """The note above the file list is the loose question too.

        holds() means "this exact file, at this exact size, and fully
        published". A trip re-rendered since it was uploaded fails it on the
        size alone; a machine whose deploy record is gone fails it outright.
        In both states the copy at the destination is still there and still
        serving, and a drop does not touch it — so a note driven off holds()
        goes silent in exactly the states where the operator most needs it,
        and he deletes locally believing the trip is gone everywhere.
        """
        target = Recorder(holds=NO, carries=YES)
        b = self.bench(target).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertIn("stay there", ran.printed)
        self.assertIn("Deleting locally does not remove them", ran.printed)

    def test_a_copy_the_target_does_not_carry_is_not_claimed_to_stay(self):
        """The control. The note is about a copy that survives the drop, so a
        destination with nothing for this trip must not produce it — the
        operator would go looking for a copy that was never there."""
        b = self.bench(Recorder(holds=YES, carries=NO)).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertNotIn("stay there", ran.printed)

    def test_dropping_a_rendered_trip_tells_the_target_it_was_on_purpose(self):
        """A dropped trip and a cleaned-up published trip are indistinguishable
        afterwards — id in the previous index, nothing on disk. Only the moment
        of dropping knows which it is, so it is said here or never."""
        target = Recorder()
        b = self.bench(target).complete()
        ran = b.run(EXCLUDE, typed=["1", "DROP"])
        self.assertTrue(ran.completed, ran.note)
        self.assertEqual(target.dropped_ids, [TRIP])

    def test_cancelling_tells_the_target_nothing(self):
        """Nothing was dropped, so there is nothing to record — and a target
        told about a drop that did not happen would curate away a trip that is
        still there."""
        target = Recorder()
        b = self.bench(target).complete()
        ran = b.run(EXCLUDE, typed=["1", "no"])
        self.assertFalse(ran.completed)
        self.assertEqual(target.dropped_ids, [])
        self.assertEqual(target.times("dropped"), 0)


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
# NA is a real answer, and the two destination gates are not interchangeable
# ---------------------------------------------------------------------------

class TestATargetThatDeclinesADestinationQuestion(SeamTest):
    """"The question does not arise here" is not "I could not check".

    This is not a corner: the implementation this repo SHIPS answers it. A
    folder holds files and does not serve them, so examples/uploader_folder.py
    returns not_applicable from published(), and every erase against it is
    therefore decided by holds() instead. An archive disk is the same shape.

    Two things have to be true in that state and neither was pinned. The hold
    answer must actually decide — NA on the serving question drops that gate,
    it does not pass it — and the tool must not describe the decision as
    resting on an answer the target never gave.
    """

    def _cleaned(self, **answers):
        b = self.bench(Recorder(**answers)).complete()
        return b, b.run(CLEAN_WS)

    def test_the_hold_decides_when_the_serving_question_does_not_arise(self):
        """The positive control for a target of the shipped example's shape."""
        b, ran = self._cleaned(holds=YES, published=NA)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())

    def test_a_hold_of_no_refuses_even_though_serving_does_not_arise(self):
        """A dropped gate must not be a passed one. If NA on the serving
        question let the erase through, every target that cannot serve would
        erase footage on the local render count alone."""
        b, ran = self._cleaned(holds=NO, published=NA)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())
        self.assertIn("held at the destination", ran.note)

    def test_a_hold_nobody_could_answer_refuses_too(self):
        """The unreachable-archive case, which is the one that costs footage:
        UNKNOWN from the only gate that can still speak is not permission."""
        b, ran = self._cleaned(holds=UNKNOWN, published=NA)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())

    def test_the_banner_names_the_answer_the_erase_actually_rested_on(self):
        """Attribution is worthless if it names an answer that was not given.

        The last line before the footage goes used to say "proceeding on X's
        answer that these renders are published" whatever had been asked —
        including to a target that had just said publishing does not arise
        here. A reader checking that sentence afterwards would be checking one
        the target can truthfully deny.
        """
        _b, ran = self._cleaned(holds=YES, published=NA)
        self.assertIn("are held at the destination", ran.printed)
        self.assertNotIn("are published", ran.printed)

    def test_declining_both_questions_erases_on_the_local_count_alone(self):
        """Pinned as the trust model's consequence, not as a good state.

        A configured target may answer NA to both, and then no gate but the
        local render count applies and the footage goes. That is the settled
        trust model — whoever installed the implementation owns what it says,
        and an implementation is entitled to decline a question it genuinely
        cannot answer. What must not happen is that it goes QUIETLY, which is
        the next test.
        """
        b, ran = self._cleaned(holds=NA, published=NA)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())

    def test_and_says_that_nothing_off_this_machine_was_checked(self):
        """The same sentence an unconfigured install gets, because it is the
        same hole: no copy off this machine was checked. A configured target
        used to buy silence here purely by being configured, and silence in
        front of an erase reads as proof."""
        _b, ran = self._cleaned(holds=NA, published=NA)
        self.assertIn("Publication was NOT verified", ran.printed)
        self.assertIn("not applicable", ran.printed)
        self.assertIn("no copy off this machine was checked", ran.printed)

    def test_the_renders_are_kept_when_nobody_holds_them(self):
        """Belt and braces on the same state: the footage went on the local
        count, so the renders are now the only copy of those drives and the
        sweep leaves them where they are."""
        b, _ran = self._cleaned(holds=NA, published=NA)
        self.assertEqual(b.renders_on_disk(), [RENDER_NAME])


# ---------------------------------------------------------------------------
# An answer that does not mention every render
# ---------------------------------------------------------------------------

class Selective(Recorder):
    """Answers about the names it was told to, and stays silent on the rest.

    The silence is the point. A target that answers about four of five renders
    has not said the fifth is missing and has not said it is there; the
    exporter has to read that name as UNKNOWN, because the alternative is that
    a render nobody mentioned gets erased on the strength of its neighbours.
    """

    def __init__(self, speaks_for, **kw):
        super().__init__(**kw)
        self._speaks_for = list(speaks_for)

    def holds(self, renders):
        self.calls.append("holds")
        return U.Answers.of(dict.fromkeys(self._speaks_for, YES))

    def published(self, renders):
        self.calls.append("published")
        return U.Answers.of(dict.fromkeys(self._speaks_for, YES))


class TestAnAnswerThatDoesNotCoverEveryRender(SeamTest):
    """Aggregation is the exporter's half of the fail-closed rule.

    Whether `sometrip_X.mp4` may vouch for `trip_X.mp4` is the implementation's
    problem now, and the interface says so. What stayed here is what a set of
    per-render answers ADDS UP TO, and a missing name is where that goes wrong
    silently: the erase is one decision over many renders, and the weakest
    reading in it has to win.
    """

    def _two_renders(self, target):
        return self.bench(target).imported().sidecars().render().render(
            trip=SECOND_TRIP).grouped()

    def test_a_render_the_target_never_mentioned_refuses_the_erase(self):
        target = Selective([RENDER_NAME])          # the second render: silence
        b = self._two_renders(target)
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk(), "footage went on a partial answer")
        self.assertIn("unknown", ran.note)

    def test_the_same_two_renders_go_once_both_are_spoken_for(self):
        """The control that makes the test above mean something: nothing else
        about this workspace refuses it."""
        b = self._two_renders(Selective([RENDER_NAME, SECOND_RENDER]))
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())

    def test_names_the_target_volunteers_beyond_the_question_change_nothing(self):
        """A target is free to answer about more than it was handed — another
        machine's renders, last year's. Those names are not on this disk and
        cannot make a decision about it."""
        b = self._two_renders(Selective([RENDER_NAME, SECOND_RENDER,
                                         "trip_from_another_machine_h1080.mp4"]))
        self.assertTrue(b.run(CLEAN_WS).completed)


# ---------------------------------------------------------------------------
# A target that falls over somewhere other than the four questions
# ---------------------------------------------------------------------------

class RaisesWhenDrawn(Recorder):
    def why_not_build(self, world):
        raise RuntimeError("fell over answering a menu draw")


class RaisesMidBuild(Recorder):
    def build(self, world, ui):
        self.calls.append("build")
        raise RuntimeError("fell over half way through the build")


class RaisesMidUpload(Recorder):
    def upload(self, world, ui):
        self.calls.append("upload")
        raise RuntimeError("fell over half way through the upload")


class TestATargetThatFallsOverAwayFromTheAskPath(SeamTest):
    """capture_world already reads an exception from holds/published/owes as
    "unreachable". The three places an implementation can raise that are NOT on
    that path had nothing holding them.

    They must not be equivalent. A build that dies is one item failing, which
    is what a menu is for. A menu DRAW that dies is the tool gone — and the
    draw is where why_not_build is asked, forty times a session, in a stranger's
    code.
    """

    def test_a_menu_draw_survives_a_target_that_raises_while_being_drawn(self):
        b = self.bench(RaisesWhenDrawn()).complete()
        built, world = b.menu(), b.world(M.Scope.LOCAL)
        with self.assertRaises(RuntimeError):
            built[BUILD].evaluate(world)           # it really does raise
        with quiet():
            P.print_menu(b.ctx, built, M.position_for(built), world)
        self.assertTrue(P._safe_verdict(built[BUILD], world).blocked,
                        "an item whose target raised was still offered")

    def test_a_target_that_raises_mid_build_fails_the_item_not_the_session(self):
        """The position stays where it was, the failure is logged under this
        session's results, and the next menu offers the same choices."""
        b = self.bench(RaisesMidBuild()).complete()
        runner = P.build_runner(b.ctx)
        with quiet():
            outcome = runner._execute(runner.menu[BUILD])
        self.assertFalse(outcome.completed)
        self.assertEqual([r.status for r in b.ctx.results], [P.FAILED])

    def test_a_target_that_raises_mid_upload_fails_the_item_not_the_session(self):
        b = self.bench(RaisesMidUpload()).complete()
        runner = P.build_runner(b.ctx)
        with quiet():
            outcome = runner._execute(runner.menu[UPLOAD])
        self.assertFalse(outcome.completed)
        self.assertEqual([r.status for r in b.ctx.results], [P.FAILED])


# ---------------------------------------------------------------------------
# The shipped example, driven by the suite so it cannot rot into a lie
# ---------------------------------------------------------------------------

class TestTheShippedExampleIsRunAndNotJustRead(SeamTest):
    """examples/uploader_folder.py is what an implementer copies.

    test_uploader.py calls its methods; nothing drove the real items through
    it. An example that is only READ drifts from the interface the moment the
    interface moves, and it drifts in the direction of the person copying it.
    This runs the documented arc — 6, 7, 8 — through the REAL loader, from the
    same spec string the file's own docstring tells you to write.
    """

    def _example(self):
        dest = Path(tempfile.mkdtemp(prefix="dashcam-seam-folder-"))
        self.addCleanup(shutil.rmtree, str(dest), True)
        patched = mock.patch.dict(os.environ, {"DASHCAM_FOLDER_TARGET": str(dest)})
        patched.start()
        self.addCleanup(patched.stop)
        spec = "%s:FolderTarget" % EXAMPLE
        target = U.load_uploader(spec, REPO)
        b = self.bench(target)
        b.ctx.uploader_origin = U.origin_of(spec, target)
        return b.complete(), dest

    def test_the_documented_arc_runs_end_to_end(self):
        b, dest = self._example()
        self.assertTrue(b.run(BUILD).completed)
        self.assertEqual(b.pages(), [], "the example wrote the other product's page")
        self.assertTrue(b.run(UPLOAD).completed)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), [RENDER_NAME])

    def test_the_erase_rests_on_the_folder_being_able_to_answer_holds(self):
        """And says so. The example answers NA to published() on purpose, so
        this arc is the one where naming "published" would be a sentence its
        own author documented as false."""
        b, _dest = self._example()
        b.run(BUILD)
        b.run(UPLOAD)
        ran = b.run(CLEAN_WS)
        self.assertTrue(ran.completed, ran.note)
        self.assertFalse(b.footage_on_disk())
        self.assertIn("are held at the destination", ran.printed)

    def test_an_upload_that_never_happened_keeps_the_footage(self):
        """The folder is empty, so it holds nothing, so it says NO — and the
        erase is refused by a real implementation rather than by a stub."""
        b, _dest = self._example()
        ran = b.run(CLEAN_WS)
        self.assertFalse(ran.completed)
        self.assertTrue(b.footage_on_disk())


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
