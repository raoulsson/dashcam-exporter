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
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import items                     # noqa: E402  (registers the ten)
import menu as M                 # noqa: E402
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

def _aborted(item, note):
    """What the real MenuItem.aborted() amounts to for the position: an
    interruption is simply not completing."""
    item.completed.return_value = False
    return M.stopped(note)


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


def drive(menu_items, position, keys):
    """Run the real Runner loop over mock items, answering the prompt with
    `keys`. Nothing touches the disk: the world is a sentinel, the painter is
    a mock, and the only I/O is the prompt, which is a mock too."""
    ctx = mock.Mock()
    ctx.results = []
    runner = P.Runner(ctx, menu_items, position)
    captures = Captures()
    buf = io.StringIO()
    with mock.patch.object(P, "capture_world", side_effect=captures) as captured, \
            mock.patch.object(P, "print_menu") as painter, \
            mock.patch.object(P, "ask", side_effect=list(keys)):
        with redirect_stdout(buf):
            runner.loop()
    return Drive(runner, ctx, captured, painter, buf.getvalue())


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
        ctx = mock.Mock()
        ctx.results = []
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
        ctx = mock.Mock()
        ctx.results = []
        ctx.plugin.reset.side_effect = RuntimeError("shelf fell over")
        with redirect_stdout(io.StringIO()) as out:
            P._tell_the_plugin(ctx, fake_item(RENDER), M.did("done"))
        self.assertIn("shelf fell over", out.getvalue())


# ---------------------------------------------------------------------------
# How long it took, measured where the operator waited
# ---------------------------------------------------------------------------

class TheClockRunsFromTheMenusSideOfTheCall(unittest.TestCase):
    """Each body used to time itself from its own first line.

    That leaves out everything the operator sat through which the body did not
    do — above all the world capture, which at FULL scope shells out over ssh
    and lists a bucket before the body is even entered. The menu knows when it
    dispatched and when it got control back, and that is the number being
    asked for.
    """

    def _dispatch(self, body):
        ctx = mock.Mock()
        ctx.results = []
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
            time.sleep(0.05)
            ctx.results.append(P.StepResult("Upload Website", P.RAN, 0.0, "deployed"))
            return M.did("deployed")
        results = self._dispatch(slow)
        self.assertGreater(results[0].seconds, 0.0)

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

    def test_deciding_to_leave_still_leaves(self):
        """Ctrl-C is the operator deciding, not the tool failing. It must not
        be swallowed by the catch that keeps the session alive."""
        menu_items, position = self.raising(META, boom=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            drive(menu_items, position, [str(RENDER), "q"])


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
                         [PROGRESS, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                          CLEAN_WS, ERASE_CARD])

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
        built, 2,3,4,5,6,8,9 are selectable; once a trip has actually been
        dropped, only 4,2,8,9 are, because the meta now describes trips that
        no longer exist."""
        menu_items, position = machine(at=META)
        position.advance(menu_items[PREVIEW])
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                          CLEAN_WS, ERASE_CARD])
        position.advance(menu_items[EXCLUDE])
        self.assertEqual(offered(menu_items, position),
                         [PROGRESS, META, EXCLUDE, CLEAN_WS, ERASE_CARD])

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
        erased the evidence and then refused the card is now unsayable."""
        menu_items, position = machine(at=RENDER)
        position.advance(menu_items[CLEAN_WS])
        self.assertEqual(offered(menu_items, position), [PROGRESS, IMPORT])
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
        """Clean Workspace offers only Import, so from there the pipeline
        stands somewhere it could not select again. A refused Import must
        still step back onto it — the position is a place we are, not a
        choice we could re-make."""
        menu_items, position = machine(at=CLEAN_WS)
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
        run = drive(menu_items, position, ["5", "q"])
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
        stays — the same thing a declined prompt means."""
        menu_items, position = machine(at=M.NOWHERE)
        menu_items[IMPORT].execute.side_effect = P.Aborted()
        run = drive(menu_items, position, ["1", "q"])
        menu_items[IMPORT].aborted.assert_called_once_with("interrupted")
        self.assertEqual(run.position.current, M.NOWHERE)
        self.assertEqual([r.status for r in run.ctx.results], [P.FAILED])

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
        the position past Render Videos on the strength of a declined prompt —
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


if __name__ == "__main__":
    unittest.main()
