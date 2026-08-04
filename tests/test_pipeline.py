#!/usr/bin/env python3
"""The pipeline, driven by mock menu items instead of by the real ten.

test_step_graph.py asks whether the ten items declare the right edges.
test_guards.py asks what the destructive predicates allow. This file asks a
third thing, which neither of those can: given items that answer whatever the
test tells them to answer, does the PIPELINE do the right thing with those
answers. Nothing real runs here — no import, no render, no upload, no card, no
filesystem. Every item is a mock, and the world handed to it is a sentinel.

The mocks are autospecced from the real item classes, so a mock that answers a
method the interface does not have, or answers one with the wrong number of
arguments, fails the test rather than passing it. What the mock supplies is
only "what did this item answer": whether it completed, and where it may lead.
What that ANSWER MEANS is left to the production code — `settles_at` on the
fake is the real MenuItem.settles_at bound to the fake, reading a real
Neighbours object — because the meaning is the thing under test.

The rules pinned here, in the owner's words:

  * "completed means the step was not aborted" — the position moves onto an
    item that completed, and onto nothing else.
  * "If it is false, the pipeline steps back by one" — a refusal leaves the
    position exactly where it was. Not two back, not at the start, and not on
    the item that just refused.
  * "Progress" neighbours everything and is never a position of its own.
  * "step back by 1" is item 9's whole outbound, and it is not an empty set of
    successors — freeing the card does not interrupt the cycle.
  * The graph decides what may be selected. An item outside the current
    outbound set is refused before its guard is ever asked.
"""

import functools
import importlib.util
import re
import io
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import items                     # noqa: E402  (registers the ten)
import menu as M                 # noqa: E402
import world as W                # noqa: E402
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)      # noqa: E402


def load_pipeline():
    """Import pipeline.py as a module without running its CLI."""
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_mocked",
                                                  REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()

REAL = {cls.number: cls for cls in items.ALL_ITEMS}
UPLOADER = M.Strategy.UPLOADER
LOCAL = M.Strategy.LOCAL_PAGE

# A position is somewhere the pipeline can actually stand. Progress is not one
# of them — it is a view — and NOWHERE is the cold start.
STANDABLE = tuple(sorted(set(REAL) - {PROGRESS}))


# ---------------------------------------------------------------------------
# The fakes
# ---------------------------------------------------------------------------

def _aborted(item, note, performed=False):
    """What the real MenuItem.aborted() amounts to for the position: an
    interruption is simply not completing. performed rides along because it
    decides whether the plugin is told its input moved, and a prompt declined
    moved nothing."""
    item.completed.return_value = False
    return M.stopped(note, performed)


def fake_item(number, strategy=UPLOADER):
    """A mock standing in for one real item, autospecced from its class.

    It carries the real class's number, name and declared edges — so a test
    reads as the owner's table — and nothing else of the real one. Its
    `settles_at` is the production method bound to the mock, because where a
    completed item leaves the position is exactly what these tests are about
    and must not be re-implemented in the fake.

    It completes and its guard passes, so a test states only the answer it
    cares about: `item.completed.return_value = False` is a refusal and
    nothing else has to be said.
    """
    real = REAL[number]
    edges = real.OUT[strategy]
    item = mock.create_autospec(real, instance=True)
    item.number = number
    item.NAME = real.NAME
    item.SCOPE = real.SCOPE
    item._out = edges
    item.name.return_value = real.NAME
    item.description.return_value = real.DESCRIPTION
    item.start.return_value = real.START
    item.end.return_value = real.END
    item.destr.return_value = real.DESTR
    item.outbound.return_value = edges
    item.inbound.return_value = M.Edges(frozenset())
    item.completed.return_value = True
    item.evaluate.return_value = M.go()
    item.execute.return_value = M.did("mock")
    item.settles_at.side_effect = functools.partial(M.MenuItem.settles_at, item)
    item.aborted.side_effect = functools.partial(_aborted, item)
    return item


def fake_menu(strategy=UPLOADER):
    return {n: fake_item(n, strategy) for n in sorted(REAL)}


def machine(strategy=UPLOADER, at=M.NOWHERE):
    """A menu of mocks and a Position built from it by the production code.

    The mocks are reset afterwards: position_for() asks every item where it
    leads and whether it starts, and a test asserting "the pipeline asked
    nobody" must not be reading those questions.
    """
    menu_items = fake_menu(strategy)
    position = M.position_for(menu_items)
    position.current = at
    for item in menu_items.values():
        item.reset_mock()
    return menu_items, position


def offered(menu_items, position):
    """What the menu would light up, as plain sorted numbers."""
    return sorted(position.selectable(menu_items))


class Captures:
    """Stands in for capture_world: hands out a fresh, distinguishable world
    each time it is called, so a test can tell WHICH capture reached the item."""

    def __init__(self):
        self.worlds = []

    def __call__(self, ctx, scope=M.Scope.LOCAL):
        world = ("world-%d" % len(self.worlds), scope)
        self.worlds.append(world)
        return world


class Drive:
    """The result of driving the runner: the mocks, and what was printed."""

    def __init__(self, runner, ctx, captured, painter, printed):
        self.ctx = ctx
        self.captured = captured
        self.painter = painter
        self.printed = printed
        self.position = runner.position

    def worlds(self):
        """The worlds handed out, in the order they were captured."""
        return self.captured.side_effect.worlds


def a_ctx():
    """A Mock, except for out_dir: the runner writes where it left off beside
    the ledger, and `Mock() / "x"` is a TypeError rather than a write."""
    ctx = mock.Mock()
    ctx.results = []
    ctx.out_dir = Path(tempfile.mkdtemp(prefix="dashcam-runner-"))
    ctx.state_dir = ctx.out_dir / "state"
    ctx.workspace = ctx.out_dir
    return ctx


def drive(menu_items, position, keys, plugin=None):
    """Run the real Runner loop over mock items, answering the prompt with
    `keys`. Nothing touches the disk but a temp ledger: the world is a
    sentinel, the painter is a mock, and the prompt is a mock too."""
    ctx = a_ctx()
    ctx.plugin = plugin
    runner = P.Runner(ctx, menu_items, position)
    captures = Captures()
    buf = io.StringIO()
    with mock.patch.object(P, "capture_world", side_effect=captures) as captured, \
            mock.patch.object(P, "print_menu") as painter, \
            mock.patch.object(P, "ask", side_effect=list(keys)):
        with redirect_stdout(buf):
            runner.loop()
    return Drive(runner, ctx, captured, painter, buf.getvalue())


class WhereWeLeftOffSurvivesARestart(unittest.TestCase):
    """The position is the last STEP the operator took, not an inference.

    Deriving it from disk went wrong in both directions in one evening: a swept
    workspace read as Upload Website because the destination still said the
    trips were published, then as Generate Meta because six receipts had not
    been archived. He knows where he is; the tool should ask him by remembering.

    A remembered position cannot lie its way past anything -- it decides what
    is OFFERED, and every item still asks its own guard before it runs.
    """

    def test_a_step_is_written_down_and_read_back(self):
        menu_items, position = machine(at=META)
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual(P.remembered_step(run.ctx), RENDER)

    def test_looking_at_progress_does_not_move_it(self):
        """A view answers a question without changing anything, so it is not
        where you are. Nor are h, i and s, which never dispatch at all."""
        menu_items, position = machine(at=RENDER)
        run = drive(menu_items, position, [str(RENDER), str(PROGRESS), "q"])
        self.assertEqual(P.remembered_step(run.ctx), RENDER)

    def test_a_refused_step_leaves_it_where_it_was(self):
        menu_items, position = machine(at=META)
        menu_items[RENDER].completed.return_value = False
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual(P.remembered_step(run.ctx), META)

    def test_nowhere_is_never_remembered(self):
        """Because it is not a position -- it is the position not being known.

        Remembered, it outranks orient() forever: an import that was started
        and interrupted put two clips in the workspace, and the menu went on
        offering only the start entries, so item 1 refused to import on top of
        them while pointing at an item 8 it would not offer.
        """
        menu_items, position = machine(at=M.NOWHERE)
        menu_items[RENDER].completed.return_value = False
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertIsNone(P.remembered_step(run.ctx))

    def test_a_remembered_clean_that_the_disk_contradicts_is_dropped(self):
        """8 completing means the working area was emptied. Footage in it now
        arrived after — an import that was interrupted or declined does not
        complete, so the position stays on 8 while the disk fills up behind
        it, and from 8 the menu offers 1 and 8 but never 2."""
        ctx = a_ctx()
        P.remember_step(ctx, CLEAN_WS)
        position = M.position_for(fake_menu())
        with mock.patch.object(P.items, "COLD_START_RULES",
                               ((IMPORT, lambda w: bool(w.imports)),)):
            P._resume(ctx, position, W.World(imports=(Path("/w/import"),)))
        self.assertEqual(position.current, IMPORT)

    def test_a_remembered_clean_with_an_empty_workspace_stands(self):
        """Nothing contradicts it, so it is where he is."""
        ctx = a_ctx()
        P.remember_step(ctx, CLEAN_WS)
        position = M.position_for(fake_menu())
        P._resume(ctx, position, W.World())
        self.assertEqual(position.current, CLEAN_WS)

    def test_a_nowhere_already_on_disk_is_ignored(self):
        """The ledgers written before that fix still say -1."""
        ctx = a_ctx()
        P.remember_step(ctx, M.NOWHERE)
        self.assertIsNone(P.remembered_step(ctx))


class DecliningTellsThePluginNothing(unittest.TestCase):
    """The plugin is told its input moved so it can drop what it cached. An
    abort before anything ran moved nothing.

    It was told anyway: stopped() defaulted to performed=True, so typing
    anything but DELETE, answering n, or pressing q dropped the plugin's
    answer and the next menu draw paid for a fresh one — eight seconds of
    "Reading the workspace..." for a keypress that touched nothing.
    """

    def _drove(self, boom):
        menu_items, position = machine(at=META)
        menu_items[RENDER].execute.side_effect = boom
        plugin = mock.Mock()
        run = drive(menu_items, position, [str(RENDER), "q"], plugin=plugin)
        return plugin, run

    def test_a_prompt_abort_does_not_reset_it(self):
        plugin, _run = self._drove(P.Aborted())
        plugin.reset.assert_not_called()

    def test_an_abort_part_way_through_does(self):
        """That one really did change the workspace: a copy or an encode was
        running when it stopped."""
        plugin, _run = self._drove(P.Aborted(mid_run=True))
        plugin.reset.assert_called_once()


class AYesNoQuestionTakesOneKey(unittest.TestCase):
    """One bit of information, one keypress. And nothing else counted as an
    answer: these prompts sit in front of copies and erases, and a
    fat-fingered r read as "no" is a silent wrong answer to a question the
    operator believed he had answered."""

    def _confirm(self, keys, default):
        with mock.patch.object(P, "_raw_capable", return_value=True), \
                mock.patch.object(P, "_one_char_at", side_effect=list(keys)):
            with redirect_stdout(io.StringIO()):
                return P.confirm("  Go?", default)

    def test_y_and_n(self):
        self.assertIs(self._confirm(["y"], False), True)
        self.assertIs(self._confirm(["n"], True), False)

    def test_enter_takes_the_default(self):
        self.assertIs(self._confirm(["\r"], True), True)
        self.assertIs(self._confirm(["\r"], False), False)

    def test_anything_else_asks_again_rather_than_meaning_no(self):
        self.assertIs(self._confirm(["r", "x", "y"], False), True)

    def test_q_aborts_the_step(self):
        with self.assertRaises(P.Aborted):
            self._confirm(["q"], True)

    def test_a_pipe_answers_on_one_line_and_does_not_loop(self):
        """Not a terminal is every test and every piped run. It must not wait
        for a second key that is never coming."""
        with mock.patch.object(P, "_raw_capable", return_value=False), \
                mock.patch.object(P, "ask", return_value="r"):
            with redirect_stdout(io.StringIO()):
                self.assertIs(P.confirm("  Go?", False), False)


class TheLiveProgressLineIsActuallyDrawn(unittest.TestCase):
    """The one path the suite could not see, and it shipped a crash.

    run_stream only renders when there IS a live area, and there is none when
    stdout is piped -- which is every test. So the render closure was never
    executed by anything but a real terminal, and an assignment inside it made
    a name it reads from the enclosing scope local: UnboundLocalError on the
    first parsed line of a real import.

    Forcing the live area on is what makes it testable at all.
    """

    def _run(self, note_first):
        cmd = ["/bin/sh", "-c", "printf 'one.mp4\\n  1024 100%% 9MB/s 0:00:01\\n'"]
        buf = io.StringIO()
        with mock.patch.object(P.C, "enabled", True):
            with redirect_stdout(buf):
                rc, lines = P.run_stream(cmd, str(REPO), "Import",
                                         parser=P.make_import_parser(4096),
                                         note_first=note_first)
        return rc, buf.getvalue()

    def test_the_note_goes_in_front_of_the_bar(self):
        rc, out = self._run(True)
        self.assertEqual(rc, 0)
        self.assertIn("one.mp4", out)
        self.assertIn("9MB/s", out)
        drawn = [l for l in out.replace("\r", "\n").split("\n") if "9MB/s" in l]
        self.assertTrue(drawn, "nothing was drawn")
        for line in drawn:
            self.assertEqual(line.count("9MB/s"), 1,
                             "the note was appended a second time after the bar")

    def test_the_label_leads_when_the_note_does_not(self):
        rc, out = self._run(False)
        self.assertEqual(rc, 0)
        self.assertIn("Import", out)

    def _sweep(self, script):
        """A child with nothing countable in its output, the deploy's case."""
        buf = io.StringIO()
        with mock.patch.object(P.C, "enabled", True):
            with redirect_stdout(buf):
                rc, _ = P.run_stream(["/bin/sh", "-c", script], str(REPO),
                                     "Deploy", quiet_finish=True)
        return rc, buf.getvalue()

    def test_a_step_with_no_denominator_draws_the_bar_not_a_spinner(self):
        """It drew a bare |/-\\, so the deploy read as a different program
        from the render three lines above it. Both say "still working"; only
        one of them says it in the tool's own language."""
        rc, out = self._sweep("echo '=== pulling live curation ==='; sleep 0.4")
        self.assertEqual(rc, 0)
        self.assertIn("###", out)
        for ch in "|/-\\":
            self.assertNotIn("Deploy %s" % ch, out)

    def test_the_block_moves_while_the_child_says_nothing(self):
        """The whole point of it. A deploy is ssh round trips, so the child is
        silent for seconds at a time and the line has to keep saying it is
        alive on the timeout tick rather than on output."""
        _rc, out = self._sweep("sleep 0.8")
        positions = {line.index("###") for line in out.split("\n")
                     if "###" in line}
        self.assertGreater(len(positions), 2,
                           "the block never moved without child output")

    def test_it_never_shows_a_percentage_it_does_not_have(self):
        """run_stream does not synthesise one from a guess, and a deploy cannot
        say how many phases it has until it has run them."""
        _rc, out = self._sweep("echo '=== rsync ==='; sleep 0.3")
        self.assertNotIn("%", out)


class TheChildsLineKeepsBothEnds(unittest.TestCase):
    """What is happening, and what it is happening to.

    The trim kept only the start, on the reasoning that an encoder puts what
    identifies a line at the front. True of "[ 4/6] 2026-07-28 encoding" and
    false of "concatenating 99 clips -> trip_..._h1080.mp4", where the front is
    the same words every time and the name at the end is the only part saying
    which trip is being written.
    """

    LINE = "concatenating 99 clips -> trip_2026-07-28_14-14_01_h1080.mp4"

    def test_the_name_at_the_end_survives(self):
        got = P._fit(self.LINE, 34)
        self.assertIn("h1080.mp4", got, "cut from the front this said 'trip_2026-'")
        self.assertTrue(got.startswith("concatenating"))

    def test_it_fits_exactly_the_room_it_was_given(self):
        for room in range(12, 70):
            with self.subTest(room=room):
                self.assertLessEqual(len(P._fit(self.LINE, room)), room)

    def test_a_line_that_fits_is_untouched(self):
        self.assertEqual(P._fit("short", 40), "short")

    def test_no_room_at_all_still_returns_something_printable(self):
        self.assertEqual(len(P._fit(self.LINE, 5)), 5)


class DiscardingAnImportUnclaimsIt(unittest.TestCase):
    """After throwing away the local copy, the ledger must stop saying it has
    it.

    The mark answers "have I already taken this card in", and item 1 acts on
    it: a card whose clips are all at or below the mark offers nothing and
    returns satisfied. Left standing after a discard, the banner\'s promise
    that item 1 brings the footage back is a lie, and the operator is left
    with one copy and item 9 as the next thing on offer.
    """

    def setUp(self):
        self.ctx = a_ctx()

    def _world(self, files, card_stamps):
        return W.World(import_files=frozenset(files),
                       card=W.Card(stamps=frozenset(card_stamps)))

    def test_the_mark_comes_back_to_before_the_discarded_clips(self):
        P.write_ledger(self.ctx, "20260728120000")
        P._unclaim_the_discarded(self.ctx, self._world(
            ["DCIM/200video/front/20260728110000_0060.mp4"],
            ["20260728090000", "20260728110000"]))
        self.assertEqual(P.read_ledger(self.ctx).get("through"), "20260728090000")

    def test_an_older_round_keeps_its_mark(self):
        """Only the discarded span is unclaimed. Winding the mark to nothing
        would re-copy a whole card whose earlier rounds were published and
        swept -- hours, for footage that is not coming back."""
        P.write_ledger(self.ctx, "20260728120000")
        P._unclaim_the_discarded(self.ctx, self._world(
            ["DCIM/200video/front/20260728110000_0060.mp4"],
            ["20260728090000", "20260728110000"]))
        self.assertGreater(P.read_ledger(self.ctx).get("through"), "")

    def test_a_mark_that_never_covered_them_is_left_alone(self):
        P.write_ledger(self.ctx, "20260728090000")
        P._unclaim_the_discarded(self.ctx, self._world(
            ["DCIM/200video/front/20260728110000_0060.mp4"], []))
        self.assertEqual(P.read_ledger(self.ctx).get("through"), "20260728090000")


class TheDeleteTargetIsRecheckedAfterTheWord(unittest.TestCase):
    """The narrowing is decided when the plan is drawn and the prompt then
    sits on screen for as long as it takes to answer.

    A second terminal running an import in that window creates a dated folder
    under the sink, which turns "delete the sink" into "delete the sink and
    the import that just landed in it". The guard cannot object: it is handed
    the world, and the target is not in it.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-target-"))
        (self.root / "DCIM").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_unchanged_is_still_the_same_target(self):
        self.assertTrue(P._target_still(self.root, self.root))

    def test_a_sibling_landing_during_the_prompt_changes_it(self):
        (self.root / "2026-07-31" / "DCIM").mkdir(parents=True)
        self.assertFalse(P._target_still(self.root, self.root))
        self.assertTrue(P._target_still(self.root, self.root / "DCIM"))


# ---------------------------------------------------------------------------
# Telling the plugin its input moved
# ---------------------------------------------------------------------------

class ThePluginIsToldWhenItsInputChanged(unittest.TestCase):
    """A plugin cannot see the workspace change, so the dispatcher tells it.

    An import landing, a trip being dropped, the working area being erased —
    none of that goes anywhere near it, and every one changes which trips it
    would be asked about. Whether it caches is its own decision, made where the
    cost is known; knowing when to stop is not something it could arrange for
    itself.

    The trigger is derived, not listed: a step that performed work and is not a
    view. A hand-kept list of the steps that matter is a list to forget to add
    to, and the forgotten one would be the one that erases something.
    """

    def _dispatch(self, number, outcome=None):
        ctx = a_ctx()
        item = fake_item(number)
        item.execute.return_value = outcome or M.did("done")
        position = mock.Mock()
        position.current = number
        runner = P.Runner.__new__(P.Runner)
        runner.ctx, runner.menu, runner.position = ctx, {number: item}, position
        with mock.patch.object(P, "capture_world", lambda c, s=None: object()), \
                redirect_stdout(io.StringIO()):
            runner.run_one(number)
        return ctx.plugin

    def test_a_step_that_did_work_tells_it(self):
        self.assertTrue(self._dispatch(RENDER).reset.called)

    def test_looking_at_progress_tells_it_nothing(self):
        """A view changes nothing by definition, so it invalidates nothing."""
        self.assertFalse(self._dispatch(PROGRESS).reset.called)

    def test_a_step_that_was_already_satisfied_tells_it_nothing(self):
        """Nothing happened, so nothing the plugin holds went stale."""
        settled = M.Outcome(True, "already done", performed=False)
        self.assertFalse(self._dispatch(RENDER, settled).reset.called)

    def test_a_plugin_that_raises_on_reset_does_not_fail_the_step(self):
        """Its cache is its own problem. A step that just finished must not be
        reported as failed because a notification about it went wrong."""
        ctx = a_ctx()
        ctx.plugin.reset.side_effect = RuntimeError("shelf fell over")
        with redirect_stdout(io.StringIO()) as out:
            P._tell_the_plugin(ctx, fake_item(RENDER), M.did("done"))
        self.assertIn("shelf fell over", out.getvalue())


# ---------------------------------------------------------------------------
# How long it took, measured where the operator waited
# ---------------------------------------------------------------------------

SLEPT = 0.05


class TheClockRunsFromTheMenusSideOfTheCall(unittest.TestCase):
    """Each body used to time itself from its own first line.

    That leaves out everything the operator sat through which the body did not
    do — above all the world capture, which at FULL scope shells out over ssh
    and lists a bucket before the body is even entered. The menu knows when it
    dispatched and when it got control back, and that is the number being
    asked for.
    """

    def _dispatch(self, body):
        ctx = a_ctx()
        item = fake_item(UPLOAD)
        item.execute.side_effect = lambda world: body(ctx)
        position = mock.Mock()
        position.current = UPLOAD
        runner = P.Runner.__new__(P.Runner)
        runner.ctx, runner.menu, runner.position = ctx, {UPLOAD: item}, position
        with mock.patch.object(P, "capture_world", lambda c, s=None: object()), \
                redirect_stdout(io.StringIO()):
            runner.run_one(UPLOAD)
        return ctx.results

    def test_the_wait_is_measured_even_when_the_body_does_not_measure_it(self):
        """The body logs a duration of zero, as one that times itself after the
        work has finished would. The dispatch is what decides."""
        def slow(ctx):
            time.sleep(SLEPT)
            ctx.results.append(P.StepResult("Upload Website", P.RAN, 0.0, "deployed"))
            return M.did("deployed")
        results = self._dispatch(slow)
        # Not "> 0". A body that RAISES also produces a small positive
        # duration, so that assertion passed while this test's sleep was
        # never reached — an undefined name, swallowed by the runner's
        # catch-all. It has to be at least as long as the sleep.
        self.assertGreaterEqual(results[0].seconds, SLEPT)
        self.assertTrue(results[0].status is not P.FAILED, "the body did not run")

    def test_a_body_that_logs_twice_gets_one_wait_on_both(self):
        """They are one dispatch, so they are one wait — not a duration split
        between them by a rule nobody wrote down."""
        def twice(ctx):
            ctx.results.append(P.StepResult("Upload Website", P.RAN, 0.0, "sent"))
            ctx.results.append(P.StepResult("Upload Website", P.RAN, 0.0, "deployed"))
            return M.did("deployed")
        results = self._dispatch(twice)
        self.assertEqual(results[0].seconds, results[1].seconds)

    def test_results_from_earlier_dispatches_are_left_alone(self):
        """Only what this dispatch appended is stamped. An earlier item's
        duration is a fact about an earlier wait."""
        def one(ctx):
            ctx.results.append(P.StepResult("Upload Website", P.RAN, 0.0, "deployed"))
            return M.did("deployed")
        ctx_results = self._dispatch(one)
        self.assertEqual(len(ctx_results), 1)


# ---------------------------------------------------------------------------
# When an item raises instead of answering
# ---------------------------------------------------------------------------

class ARuntimeFailureIsOneItemsFailure(unittest.TestCase):
    """"runtime can always happen, should throw exception and pipeline catches
    it and does step -1, no complete."

    A disk fills, the source is unplugged mid-copy, a call is wired wrong.
    Before this, the exception reached the top and took the session with it —
    and the position went too, which is the worst moment to lose both. An item
    that raises is an item that did not complete, and not completing already
    has a defined meaning: the position does not move.
    """

    def raising(self, at, boom=RuntimeError("disk full")):
        menu_items, position = machine(UPLOADER, at=at)
        menu_items[RENDER].execute.side_effect = boom
        return menu_items, position

    def test_the_session_survives_it(self):
        """The menu comes back. Proven by the prompt being answered again
        after the failure rather than the loop unwinding."""
        menu_items, position = self.raising(META)
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual(run.painter.call_count, 2)

    def test_the_position_does_not_move(self):
        menu_items, position = self.raising(META)
        drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual(position.current, META)

    def test_the_item_did_not_complete(self):
        """And says so through the same channel an abort uses, because they
        mean the same thing to the machine."""
        menu_items, position = self.raising(META)
        drive(menu_items, position, [str(RENDER), "q"])
        menu_items[RENDER].aborted.assert_called_once()

    def test_the_operator_is_told_what_raised(self):
        menu_items, position = self.raising(META)
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertIn("RuntimeError", run.printed)
        self.assertIn("disk full", run.printed)

    def test_the_failure_is_recorded_for_the_summary(self):
        menu_items, position = self.raising(META)
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertTrue(run.ctx.results, "a crash left no trace in the results")

    def test_ctrl_c_inside_a_step_stops_the_step(self):
        """RESTATED: it used to end the SESSION.

        Ctrl-C is the operator deciding, not the tool failing — that part
        stands. What was wrong is where the decision landed: inside a child
        process run_stream already caught it and aborted the STEP, so the same
        keypress ended the session or ended one step depending on whether the
        step happened to be shelling out at that moment. A stills loop is
        Python and a render is a subprocess, and the operator cannot see the
        difference.

        Leaving is still one keypress away: the menu's own prompt raises
        Aborted, which _run_menu catches and exits on.
        """
        menu_items, position = self.raising(META, boom=KeyboardInterrupt())
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual([r.status for r in run.ctx.results], [P.ABORTED])
        self.assertEqual([r.detail for r in run.ctx.results],
                         ["Aborted by user mid-run."])


# ---------------------------------------------------------------------------
# Where the position goes when an item completes
# ---------------------------------------------------------------------------

class Advancing(unittest.TestCase):
    """"completed means the step was not aborted" — and a step that was not
    aborted is where the pipeline now stands."""

    def test_the_item_that_completed_becomes_where_we_are(self):
        """A completing item is the new position, and what may be selected
        next is that item's outbound set and nothing else."""
        menu_items, position = machine(at=IMPORT)
        self.assertEqual(position.advance(menu_items[META]), META)
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, META, PREVIEW, EXCLUDE, BUILD, RENDER,
                          UPLOAD, CLEAN_WS, ERASE_CARD])

    def test_the_pipeline_asks_the_item_itself_whether_it_completed(self):
        """The answer comes from the item that ran, not from a status the
        pipeline kept about it, and no other item is consulted."""
        menu_items, position = machine(at=IMPORT)
        position.advance(menu_items[META])
        menu_items[META].completed.assert_called_once_with()
        menu_items[META].settles_at.assert_called_once_with(IMPORT)
        for n in sorted(set(REAL) - {META}):
            menu_items[n].completed.assert_not_called()
            menu_items[n].settles_at.assert_not_called()

    def test_the_owners_worked_example(self):
        """His rule 6, run through the position machine: with the preview
        built, 2,3,4,5,6,8,9 are selectable.

        RESTATED: after a drop it was 4,2,8,9 — the meta then described trips
        that no longer existed. Item 4 removes those sidecars with the footage
        now, so 3 comes back: what is left can be looked at without writing
        the metadata again first.
        """
        menu_items, position = machine(at=META)
        position.advance(menu_items[PREVIEW])
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, META, PREVIEW, EXCLUDE, BUILD, RENDER,
                          UPLOAD, CLEAN_WS, ERASE_CARD])
        position.advance(menu_items[EXCLUDE])
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, META, PREVIEW, EXCLUDE, UPLOAD,
                          CLEAN_WS, ERASE_CARD])

    def test_running_the_same_item_twice_leaves_the_position_alone(self):
        """The position is idempotent in the same sense the items are: a
        second run of an item that completes again lands in the same place."""
        menu_items, position = machine(at=IMPORT)
        first = position.advance(menu_items[META])
        self.assertEqual(position.advance(menu_items[META]), first)
        self.assertEqual(position.current, META)

    def test_clean_workspace_ends_the_cycle_at_import(self):
        """Once the workspace is gone only a new cycle remains, so Delete SIM
        Data cannot follow Clean Workspace in the same round — the order that
        erased the evidence and then refused the card is now unsayable.

        RESTATED: it also offers itself, because a sink can hold several dated
        imports and the erase narrows to one of them. What this test is for is
        the absence of 9, and that is now asserted directly rather than by an
        exact set that happened to exclude it.
        """
        menu_items, position = machine(at=RENDER)
        position.advance(menu_items[CLEAN_WS])
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, IMPORT, CLEAN_WS])
        self.assertNotIn(ERASE_CARD, position.selectable(menu_items))


# ---------------------------------------------------------------------------
# Rule 3 and rule 6: "the pipeline steps back by one"
# ---------------------------------------------------------------------------

class SteppingBack(unittest.TestCase):
    """"If the user aborted, the pipeline steps back one step, the rest is
    noise from the UI."

    Stepping back by one is the move NOT taking effect: the position was never
    written, so it is still the item that offered the refused one. These cases
    exist because "by one" is the part that is easy to get wrong — one
    implementation too many and a refusal walks the operator back to the start.
    """

    def _refuses(self, menu_items, number):
        item = menu_items[number]
        item.completed.return_value = False
        return item

    def test_a_refusal_from_every_position_in_turn(self):
        """From wherever the pipeline stands, an item that does not complete
        leaves it standing exactly there, and leaves the same list on offer."""
        for where in STANDABLE:
            menu_items, position = machine(at=where)
            before = offered(menu_items, position)
            for number in sorted(position.selectable(menu_items)):
                with self.subTest(at=where, selected=number):
                    position.current = where
                    item = self._refuses(menu_items, number)
                    self.assertEqual(position.advance(item), where)
                    self.assertEqual(offered(menu_items, position), before)

    def test_a_refusal_never_asks_where_the_item_would_have_settled(self):
        """An item that did not complete is not asked where it leads. The
        question only makes sense for a move that took effect, and asking it
        anyway is how "step back" quietly becomes "step forward"."""
        menu_items, position = machine(at=META)
        item = self._refuses(menu_items, RENDER)
        position.advance(item)
        item.completed.assert_called_once_with()
        item.settles_at.assert_not_called()

    def test_the_refused_item_does_not_become_the_position(self):
        """The obvious wrong answer: treating "it ran" as "we are now there".
        The pipeline is still at the item that offered it."""
        menu_items, position = machine(at=PREVIEW)
        position.advance(self._refuses(menu_items, RENDER))
        self.assertEqual(position.current, PREVIEW)
        self.assertNotEqual(position.current, RENDER)

    def test_stepping_back_onto_a_position_that_is_no_longer_reachable(self):
        """The position is a place we are, not a choice we could re-make.

        RESTATED: every standable entry now leads to itself, so the real graph
        no longer produces this shape — which is why the outbound is narrowed
        here by hand rather than borrowed from an item. The rule outlives the
        graph that first showed it: a refusal steps back onto where we were
        even when the menu would not offer to go there.
        """
        menu_items, position = machine(at=CLEAN_WS)
        menu_items[CLEAN_WS].outbound.return_value = M.Edges(frozenset({IMPORT}))
        self.assertNotIn(CLEAN_WS, position.selectable(menu_items))
        position.advance(self._refuses(menu_items, IMPORT))
        self.assertEqual(position.current, CLEAN_WS)
        self.assertEqual(offered(menu_items, position), [PROGRESS, IMPORT])

    def test_stepping_back_at_the_first_item(self):
        """At the entry point there is nothing behind it to fall to. A refusal
        stays at Import rather than dropping to the cold start."""
        menu_items, position = machine(at=IMPORT)
        position.advance(self._refuses(menu_items, META))
        self.assertEqual(position.current, IMPORT)
        self.assertNotEqual(position.current, M.NOWHERE)

    def test_stepping_back_before_the_first_item(self):
        """And at the cold start there is no such thing as further back: a
        refused Import leaves the pipeline at the start, still offering the
        entry points. "Nowhere to step back to" is not a case anyone has to
        handle, because the position is only ever written on success."""
        menu_items, position = machine(at=M.NOWHERE)
        position.advance(self._refuses(menu_items, IMPORT))
        self.assertEqual(position.current, M.NOWHERE)
        self.assertEqual(offered(menu_items, position), [PROGRESS, IMPORT])

    def test_stepping_back_twice_in_a_row(self):
        """Two refusals are not two steps back. The position was never written
        either time, so it is still where the last COMPLETED item put it."""
        menu_items, position = machine(at=IMPORT)
        position.advance(menu_items[META])
        position.advance(self._refuses(menu_items, RENDER))
        position.advance(self._refuses(menu_items, BUILD))
        self.assertEqual(position.current, META)

    def test_a_refusal_after_a_success_returns_to_the_success(self):
        """The full shape of the owner's sentence: complete, refuse, and the
        selectable list is the completed item's again, unchanged."""
        menu_items, position = machine(at=IMPORT)
        position.advance(menu_items[META])
        after_meta = offered(menu_items, position)
        position.advance(self._refuses(menu_items, EXCLUDE))
        self.assertEqual(offered(menu_items, position), after_meta)

    def test_a_refusal_does_not_narrow_the_menu_the_way_a_drop_would(self):
        """Exclude Trip narrows the pipeline to 4,2,8,9 IFF a trip was
        actually removed. Cancelled at the prompt, the wider list stays —
        this is the owner's "always complete IFF a trip was removed"."""
        menu_items, position = machine(at=PREVIEW)
        wide = offered(menu_items, position)
        position.advance(self._refuses(menu_items, EXCLUDE))
        self.assertEqual(offered(menu_items, position), wide)
        self.assertIn(RENDER, position.selectable(menu_items))


# ---------------------------------------------------------------------------
# The cold start
# ---------------------------------------------------------------------------

class ColdStart(unittest.TestCase):
    """Before anything has completed there is no position, and the only way
    in is an entry point."""

    def test_only_the_entry_points_are_on_offer(self):
        """With nothing done, Import is the one item that may be selected —
        plus the view, which is not work."""
        menu_items, position = machine(at=M.NOWHERE)
        self.assertEqual(offered(menu_items, position), [PROGRESS, IMPORT])

    def test_the_entry_points_come_from_the_items_not_from_a_constant(self):
        """Which items are entry points is each item's own declaration, asked
        when the menu is built. A hardcoded {1} elsewhere would be a second
        place to be wrong."""
        menu_items = fake_menu()
        position = M.position_for(menu_items)
        for n in sorted(REAL):
            menu_items[n].start.assert_called_once_with()
        self.assertEqual(sorted(position.starts), [IMPORT])

    def test_at_the_start_no_item_is_asked_where_it_leads(self):
        """There is nowhere to lead from yet. The cold start reads the entry
        points and consults nobody's outbound."""
        menu_items, position = machine(at=M.NOWHERE)
        position.selectable(menu_items)
        for n in sorted(REAL):
            menu_items[n].outbound.assert_not_called()

    def test_orientation_reads_the_world_and_stops_at_the_first_match(self):
        """Where we are on a cold start is DERIVED from the world, first rule
        wins. The rules after the hit are not even asked, which is what makes
        the order of that table a priority and not a list."""
        world = mock.sentinel.world
        hit = mock.Mock(return_value=True)
        later = mock.Mock(return_value=True)
        _, position = machine(at=M.NOWHERE)
        self.assertEqual(position.orient(world, ((RENDER, hit), (META, later))),
                         RENDER)
        hit.assert_called_once_with(world)
        later.assert_not_called()

    def test_orientation_of_an_empty_world_is_the_start(self):
        """Nothing on disk means nothing was done, and the pipeline says so
        rather than guessing a position it cannot support."""
        rules = tuple((n, mock.Mock(return_value=False)) for n in STANDABLE)
        _, position = machine(at=RENDER)
        self.assertEqual(position.orient(mock.sentinel.world, rules), M.NOWHERE)
        for _, rule in rules:
            rule.assert_called_once_with(mock.sentinel.world)


# ---------------------------------------------------------------------------
# 0) Progress — the view
# ---------------------------------------------------------------------------

class TheView(unittest.TestCase):
    """Progress neighbours everything and changes nothing. It is a way of
    looking, not a step, and the position machine needs no special case to
    keep it from becoming a position."""

    def test_progress_is_offered_from_everywhere_including_the_start(self):
        for where in (M.NOWHERE,) + STANDABLE:
            with self.subTest(at=where):
                menu_items, position = machine(at=where)
                self.assertIn(PROGRESS, position.selectable(menu_items))

    def test_progress_does_not_move_the_position(self):
        """It completes — looking at the state always succeeds — and the
        pipeline stays exactly where it was."""
        for where in (M.NOWHERE,) + STANDABLE:
            with self.subTest(at=where):
                menu_items, position = machine(at=where)
                self.assertEqual(position.advance(menu_items[PROGRESS]), where)
                menu_items[PROGRESS].settles_at.assert_called_once_with(where)

    def test_progress_does_not_change_what_may_be_selected_next(self):
        menu_items, position = machine(at=RENDER)
        before = offered(menu_items, position)
        position.advance(menu_items[PROGRESS])
        self.assertEqual(offered(menu_items, position), before)

    def test_the_view_is_recognised_by_what_it_declares(self):
        """A view is an item whose neighbours are "everything", asked of the
        item — not item number 0 on an exemption list somewhere."""
        menu_items = fake_menu()
        position = M.position_for(menu_items)
        self.assertEqual(sorted(position.views), [PROGRESS])
        menu_items[PROGRESS].outbound.assert_called()


# ---------------------------------------------------------------------------
# 9) Delete SIM Data — "step back by 1"
# ---------------------------------------------------------------------------

class StepBackByOne(unittest.TestCase):
    """Item 9's outbound is not a set of successors, and must not be read as
    an empty one. Freeing the card does not interrupt the cycle: it hands the
    position back to whoever offered it."""

    def test_erasing_the_card_returns_to_wherever_it_was_offered_from(self):
        for where in (META, PREVIEW, RENDER, BUILD, UPLOAD):
            with self.subTest(offered_from=where):
                menu_items, position = machine(at=where)
                before = offered(menu_items, position)
                self.assertIn(ERASE_CARD, before)
                self.assertEqual(position.advance(menu_items[ERASE_CARD]), where)
                self.assertEqual(offered(menu_items, position), before)

    def test_it_is_a_step_back_and_not_an_empty_outbound_set(self):
        """Read as a normal outbound set it offers nothing, which would strand
        the operator with only the view. The distinction is the whole point of
        the kind: what it offers is empty, where it settles is where we were."""
        menu_items, position = machine(at=RENDER)
        erase = menu_items[ERASE_CARD]
        self.assertEqual(erase.outbound().offers(position.universe), frozenset())
        position.advance(erase)
        self.assertNotEqual(offered(menu_items, position), [PROGRESS])
        self.assertIn(BUILD, position.selectable(menu_items))

    def test_the_card_can_still_be_erased_before_the_workspace_is_cleaned(self):
        """The safe order survives: erase the card while its clips are
        provably in the workspace, then clean the workspace. Stepping back
        leaves Clean Workspace on offer."""
        menu_items, position = machine(at=RENDER)
        position.advance(menu_items[ERASE_CARD])
        self.assertIn(CLEAN_WS, position.selectable(menu_items))

    def test_a_refused_erase_is_indistinguishable_from_a_completed_one_here(self):
        """Deliberately so: both leave the position where it was. The
        difference is in the report, not in the graph — which is why the
        outcome is the item's own answer and not the position's."""
        menu_items, position = machine(at=BUILD)
        menu_items[ERASE_CARD].completed.return_value = False
        self.assertEqual(position.advance(menu_items[ERASE_CARD]), BUILD)


# ---------------------------------------------------------------------------
# The graph is the gate
# ---------------------------------------------------------------------------

class TheGraphRefusesFirst(unittest.TestCase):
    """Order is the graph's job. An item that does not follow where we are is
    refused before its own guard is consulted — which is what lets the items
    be atomic and stop asking "has the earlier step run yet"."""

    def test_an_item_outside_the_outbound_set_cannot_be_selected(self):
        """Its guard would pass. It is still not offered, because Upload does
        not follow Import."""
        menu_items, position = machine(at=IMPORT)
        menu_items[UPLOAD].evaluate.return_value = M.go()
        run = drive(menu_items, position, ["7", "q"])
        menu_items[UPLOAD].execute.assert_not_called()
        self.assertEqual(run.position.current, IMPORT)

    def test_the_guard_is_not_even_asked_when_the_graph_says_no(self):
        """Two gates, in order: the graph answers "may this follow where we
        are" and only then does anything ask "would it do anything". A guard
        consulted here would be a guard that could wave through an item the
        position never offered."""
        menu_items, position = machine(at=IMPORT)
        drive(menu_items, position, ["7", "q"])
        menu_items[UPLOAD].evaluate.assert_not_called()

    def test_the_refusal_names_the_item_and_says_plainly_it_cannot_run(self):
        """Named, and in the operator's terms rather than the machine's.

        It used to also recite where we are -- "does not follow 1) Import SIM"
        -- which described the graph to someone who only wanted to know whether
        the key would work. The position is on the menu's own footer; the
        refusal answers the question that was asked.
        """
        menu_items, position = machine(at=IMPORT)
        run = drive(menu_items, position, ["7", "q"])
        self.assertIn("Upload Website", run.printed)
        self.assertIn("is not available", run.printed)

    def test_an_item_the_graph_offers_is_dispatched(self):
        """The other half of the same rule: what the position offers, runs."""
        menu_items, position = machine(at=M.NOWHERE)
        run = drive(menu_items, position, ["1", "q"])
        menu_items[IMPORT].execute.assert_called_once()
        self.assertEqual(run.position.current, IMPORT)

    def test_only_the_selected_item_is_run(self):
        """One selection, one item. Batch selection is gone: the second
        number's legality would depend on the first one's outcome."""
        menu_items, position = machine(at=M.NOWHERE)
        drive(menu_items, position, ["1", "q"])
        for n in sorted(set(REAL) - {IMPORT}):
            menu_items[n].execute.assert_not_called()

    def test_something_that_is_not_an_item_runs_nothing(self):
        menu_items, position = machine(at=M.NOWHERE)
        run = drive(menu_items, position, ["1 2", "banana", "q"])
        for item in menu_items.values():
            item.execute.assert_not_called()
        self.assertEqual(run.position.current, M.NOWHERE)


# ---------------------------------------------------------------------------
# The runner: what it hands the item, and what it does with the answer
# ---------------------------------------------------------------------------

class TheRunner(unittest.TestCase):
    """One selection, one item, one world captured for that item, right now."""

    def test_the_item_is_handed_a_world_captured_after_the_prompt(self):
        """Not the world the menu was drawn with. That one is a prompt old and
        the card can be swapped, or a sidecar deleted in Finder, while the
        prompt is on screen."""
        menu_items, position = machine(at=M.NOWHERE)
        run = drive(menu_items, position, ["1", "q"])
        drawn_with, handed_over = run.worlds()[:2]
        menu_items[IMPORT].execute.assert_called_once_with(handed_over)
        self.assertNotEqual(drawn_with, handed_over)

    def test_the_world_is_captured_for_the_items_own_scope(self):
        """The menu draws on the local scope every loop; an item that needs
        the bucket says so itself, and the runner reads that declaration
        rather than keeping a list of which numbers are expensive. The mock
        declares a scope its real counterpart does not, so this can only pass
        by the runner having read the declaration."""
        menu_items, position = machine(at=PREVIEW)
        menu_items[RENDER].SCOPE = M.Scope.FULL
        run = drive(menu_items, position, [str(RENDER), "q"])
        self.assertEqual(run.captured.call_args_list[0].args[1], M.Scope.LOCAL)
        self.assertEqual(run.captured.call_args_list[1].args[1], M.Scope.FULL)

    def test_a_completing_item_moves_the_position_and_a_refusing_one_does_not(self):
        """The runner takes the item's own answer for it, both ways round, in
        one session."""
        menu_items, position = machine(at=M.NOWHERE)
        menu_items[CLEAN_WS].completed.return_value = False
        run = drive(menu_items, position, ["1", "8", "q"])
        menu_items[CLEAN_WS].execute.assert_called_once()
        self.assertEqual(run.position.current, IMPORT)

    def test_an_interruption_is_the_items_own_answer_and_holds_the_position(self):
        """Ctrl-C leaves execute() part-way through, so the item is told to
        record the abort as its outcome. It did not complete, so the position
        stays — the same thing a declined prompt means.

        RESTATED: it is recorded ABORTED, not FAILED. Ctrl-C is the operator
        deciding, and the summary said FAILED in red beside a step he stopped
        on purpose. Nothing else changes: it still does not complete and the
        position still holds.
        """
        menu_items, position = machine(at=M.NOWHERE)
        menu_items[IMPORT].execute.side_effect = P.Aborted(mid_run=True)
        run = drive(menu_items, position, ["1", "q"])
        menu_items[IMPORT].aborted.assert_called_once_with("Aborted by user mid-run.",
                                                    performed=True)
        self.assertEqual(run.position.current, M.NOWHERE)
        self.assertEqual([r.status for r in run.ctx.results], [P.ABORTED])
        self.assertEqual(P._exit_code(run.ctx), 0, "stopping on purpose is not a failure")

    def test_a_prompt_abort_is_pre_run_not_mid_run(self):
        """q at a typed-word prompt stops a step that never started. Reported
        mid-run it claims something was part way through, which is the one
        thing the two words exist to tell apart."""
        menu_items, position = machine(at=M.NOWHERE)
        menu_items[IMPORT].execute.side_effect = P.Aborted()
        run = drive(menu_items, position, ["1", "q"])
        menu_items[IMPORT].aborted.assert_called_once_with("Aborted by user pre-run.",
                                                    performed=False)
        self.assertEqual([r.detail for r in run.ctx.results],
                         ["Aborted by user pre-run."])

    def test_the_menu_is_repainted_from_the_position_every_loop(self):
        """The greying is recomputed, never remembered: the painter is handed
        the live position and a freshly captured world on each turn."""
        menu_items, position = machine(at=M.NOWHERE)
        run = drive(menu_items, position, ["1", "q"])
        self.assertEqual(run.painter.call_count, 2)
        for call, world in zip(run.painter.call_args_list,
                               [run.worlds()[0], run.worlds()[2]]):
            self.assertIs(call.args[2], run.position)
            self.assertEqual(call.args[3], world)

    def test_quitting_runs_nothing(self):
        menu_items, position = machine(at=RENDER)
        run = drive(menu_items, position, ["q"])
        for item in menu_items.values():
            item.execute.assert_not_called()
        self.assertEqual(run.position.current, RENDER)


# ---------------------------------------------------------------------------
# What a step body's answer becomes
# ---------------------------------------------------------------------------

class WhatCountsAsHavingDoneIt(unittest.TestCase):
    """The bodies still answer in the four words they always did; the Work
    facade turns that answer into `completed`, and which words count is the
    owner's rule 3 — "completed means the step was not aborted".

    This is the seam where a body that declined to do anything could start
    looking like one that did. It is asserted on `_outcome` directly because
    every non-destructive item reaches it and none of them can see it.
    """

    def outcome_of(self, status):
        return P._outcome(P.StepResult("a step", status, 0, "detail"))

    def test_a_step_that_ran_completes(self):
        self.assertTrue(self.outcome_of(P.RAN).completed)

    def test_a_step_whose_postcondition_already_held_completes(self):
        """SATISFIED is not GO and it is not failure either: nothing was owed,
        so the pipeline may move on. That is what makes re-running an item
        harmless rather than merely tolerated."""
        self.assertTrue(self.outcome_of(P.SATISFIED).completed)

    def test_a_step_that_failed_does_not_complete(self):
        self.assertFalse(self.outcome_of(P.FAILED).completed)

    def test_a_step_that_skipped_does_not_complete(self):
        """The one that is easy to get wrong, because SKIPPED is not an error.

        A render where the operator answered no to "delete and re-render?"
        reports SKIPPED, and so does one given a bad height. Nothing was
        encoded in either case, so treating them as completing would advance
        the position past Render Trips on the strength of a declined prompt —
        and the old `status != FAILED` convention did exactly that.
        """
        self.assertFalse(self.outcome_of(P.SKIPPED).completed)

    def test_the_bodys_own_words_survive_into_the_outcome(self):
        """Whatever the step said about itself is what the report prints; the
        facade decides completion, not wording."""
        self.assertEqual(self.outcome_of(P.SKIPPED).note, "detail")


# ---------------------------------------------------------------------------
# No dead ends
# ---------------------------------------------------------------------------

def _next_positions(menu_items, position, here):
    position.current = here
    return set(map(lambda n: menu_items[n].settles_at(here),
                   position.selectable(menu_items)))


def walk(menu_items, position, start=M.NOWHERE):
    """Every position reachable from `start` by any sequence of COMPLETING
    moves.

    Refusals need no walking: they do not write the position, so they add no
    state — which is itself the reason this walk is finite.
    """
    seen, queue = set(), {start}
    while queue:
        here = queue.pop()
        seen.add(here)
        queue |= _next_positions(menu_items, position, here) - seen
    return seen


def offered_from(menu_items, position, start=M.NOWHERE):
    """Every item selectable from any position reachable from `start`."""
    at = functools.partial(_selectable_at, menu_items, position)
    return set().union(*map(at, walk(menu_items, position, start)))


def _selectable_at(menu_items, position, here):
    position.current = here
    return position.selectable(menu_items)


class NoDeadEnds(unittest.TestCase):
    """No sequence of moves may leave the operator with nothing to do. A menu
    where everything is greyed is a tool that has to be restarted, and the
    position is derived, so a restart would land in the same place."""

    def test_every_reachable_position_still_offers_work(self):
        for strategy in M.Strategy:
            menu_items, position = machine(strategy)
            for here in sorted(walk(menu_items, position)):
                with self.subTest(strategy=strategy.value, at=here):
                    position.current = here
                    work = position.selectable(menu_items) - position.views
                    self.assertTrue(work, "nothing to do at %d" % here)

    def test_the_cycle_can_always_be_started_again(self):
        """From every reachable position there is a route back to Import, so a
        session never ends in a place a new round cannot begin from."""
        for strategy in M.Strategy:
            menu_items, position = machine(strategy)
            for here in sorted(walk(menu_items, position)):
                with self.subTest(strategy=strategy.value, at=here):
                    self.assertIn(IMPORT, offered_from(menu_items, position, here))

    def test_the_item_with_no_edges_is_never_offered(self):
        """Under the local product Upload Website has no edges at all. It must
        be unreachable rather than a place to get stuck in — nothing offers
        it, and that is settled when the menu is built, not by an if in a
        method body."""
        menu_items, position = machine(LOCAL)
        self.assertEqual(menu_items[UPLOAD].outbound().edges(), frozenset())
        self.assertNotIn(UPLOAD, offered_from(menu_items, position))

    def test_the_publishing_product_offers_every_item_somewhere(self):
        """And under the publishing product every item can be selected from
        somewhere, so no step of the owner's table is dead code — including
        Upload Website, which the table as written could not reach at all
        until Build Website was given the edge to it."""
        menu_items, position = machine(UPLOADER)
        reachable = offered_from(menu_items, position)
        for n in sorted(REAL):
            with self.subTest(item=n):
                self.assertIn(n, reachable)


class TheHelpScreen(unittest.TestCase):
    """`h <n>` answers what the menu row has no room for."""

    def setUp(self):
        # Items 5 and 7 ask the installed plugin to describe its own job, so
        # the fake has to answer that rather than hand back None.
        class FakePlugin:
            def describe(self):
                return "Build the website from the described trips."

            def get_website_upload_description(self):
                return "What this publisher does, in its own words."

        class FakeWork:
            # site_dir answers a path; everything else hands back the plugin.
            def site_dir(self):
                return "~/dashcam-data/export"

            def __getattr__(self, name):
                return lambda *a, **kw: FakePlugin()
        self.items = M.build_menu(M.Strategy.UPLOADER, FakeWork())

    def _plain(self, number):
        return [re.sub(r"\x1b\[[0-9;]*m", "", ln)
                for ln in P._about(self.items, None, number)]

    def _prose(self, number):
        """The wrapped paragraphs: after the description, before the graph."""
        lines = self._plain(number)
        stop = next(i for i, ln in enumerate(lines) if ln.startswith("    leads to"))
        return [ln for ln in lines[2:stop] if ln.strip()]

    def test_every_entry_says_more_than_its_menu_row(self):
        for n in self.items:
            with self.subTest(entry=n):
                self.assertTrue(self.items[n].about().strip(),
                                "%d) %s has no help text"
                                % (n, self.items[n].name()))

    def test_the_graph_comes_after_the_prose_not_before_it(self):
        lines = self._plain(9)
        prose = next(i for i, ln in enumerate(lines) if "keeps its folder tree" in ln)
        graph = next(i for i, ln in enumerate(lines) if ln.startswith("    leads to"))
        self.assertLess(prose, graph)

    def test_the_graph_rows_are_dim(self):
        with mock.patch.object(P.C, "enabled", True):
            raw = P._about(self.items, None, 9)
        for row in raw:
            if "leads to" in row or "reached from" in row or "erases" in row:
                self.assertIn("\x1b[2m", row, "graph row is not dimmed")

    def test_the_prose_does_not_run_off_the_terminal(self):
        """The graph rows name every entry they reach and are as long as they
        are; the paragraphs are wrapped, and a paragraph that is not is a wall
        of text on a wide window."""
        for n in self.items:
            for line in self._prose(n):
                with self.subTest(entry=n):
                    self.assertLessEqual(len(line), 100)

    def test_a_step_back_is_not_described_as_a_view(self):
        """All three edgeless shapes answer edges() with None. Reading them the
        same way told the operator that the entry which erases a card was a
        view of one."""
        leads = next(ln for ln in self._plain(9) if ln.startswith("    leads to"))
        self.assertNotIn("view", leads)

    def test_the_entry_point_is_reached_from_nothing_not_from_anywhere(self):
        came = next(ln for ln in self._plain(1) if ln.startswith("    reached from"))
        self.assertNotIn("view", came)

    def test_a_path_on_its_own_line_is_not_reflowed(self):
        """An entry can lay out a line itself by indenting it. Wrapped into
        the prose a path breaks across two lines and stops being something you
        can select and paste."""
        lines = P._about_paragraphs("Written to:\n\t/very/long/path/that/would/"
                                    "otherwise/be/wrapped/into/the/prose/export")
        self.assertIn("        /very/long/path/that/would/otherwise/be/"
                      "wrapped/into/the/prose/export", lines)

    def test_a_single_newline_is_a_line_break_not_a_space(self):
        out = [ln.strip() for ln in P._about_paragraphs("first\nsecond") if ln.strip()]
        self.assertEqual(out, ["first", "second"])

    def test_item_five_prints_what_the_publisher_says_verbatim(self):
        said = "What this publisher does, in its own words."
        self.assertIn(said, "\n".join(self._plain(5)))

    def test_item_five_names_the_directory_when_it_knows_it(self):
        self.assertIn("~/dashcam-data/export", "\n".join(self._plain(5)))

    def test_the_handover_sentence_closes_when_nothing_answers(self):
        """A publisher predating this interface returns "", and a colon with
        nothing after it reads as a page that lost its last paragraph."""
        item = self.items[5]
        with mock.patch.object(item._builder, "get_website_upload_description",
                               return_value=""):
            text = item.about()
        self.assertTrue(text.rstrip().endswith("handed over to the plugin."))

    def test_the_view_still_says_it_is_one(self):
        for row in ("    leads to", "    reached from"):
            line = next(ln for ln in self._plain(0) if ln.startswith(row))
            self.assertIn("view", line)


class TheVersionIsTheCommitCount(unittest.TestCase):
    """major is set by hand; minor and patch are arithmetic on the history.

    The old scheme sliced the count's DIGITS apart — 249 became 2.4.9 — which
    made the major number advance every hundred commits and capped the whole
    thing at 999. It also could not represent a count of fewer than three
    digits at all.
    """

    def test_the_count_splits_into_hundreds_and_remainder(self):
        self.assertEqual(P._version_of("418"), "3.4.18")

    def test_the_major_is_not_taken_from_the_history(self):
        for count in ("418", "912", "1500"):
            with self.subTest(count=count):
                self.assertTrue(P._version_of(count).startswith("3."))

    def test_a_hundredth_commit_rolls_the_minor_and_zeroes_the_patch(self):
        self.assertEqual(P._version_of("499"), "3.4.99")
        self.assertEqual(P._version_of("500"), "3.5.0")

    def test_the_count_is_no_longer_capped_at_three_digits(self):
        """1000 commits used to be unrepresentable, and 99 equally so."""
        self.assertEqual(P._version_of("1042"), "3.10.42")
        self.assertEqual(P._version_of("7"), "3.0.7")

    def test_no_history_to_count_says_so_rather_than_inventing_one(self):
        for count in (None, "", "abc", "4.1.8"):
            with self.subTest(count=count):
                self.assertEqual(P._version_of(count), P.VERSION_FALLBACK)


if __name__ == "__main__":
    unittest.main()
