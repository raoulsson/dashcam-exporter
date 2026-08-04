#!/usr/bin/env python3
"""The paths through the ten items, walked for real, and the menu they draw.

Two halves, one subject: what the state machine PERMITS, and what the operator
is SHOWN.

The first half drives pipeline.Runner along the routes the owner's table
describes — the full cycle, the early loop he actually works in, the two
destructive items — and asserts at every prompt that the set on offer is
exactly what the table says. It also asserts the routes that must NOT exist:
publishing under the local product, a number the position does not offer,
and a destructive item reached without its evidence.

The second half paints the menu from a machine made of items the painter has
never heard of — numbers outside 0-9, names no step has ever had. Every entry
it draws, greys, reddens and names has to come from that machine, so a step
number, a label or an ordering written into the drawing code fails here
rather than drifting quietly from the items.

Both halves use MOCK items and a mock Work: nothing renders, uploads, reads a
card or touches the network. The world each item judges is handed to it, so
these tests state what the GRAPH does, while test_guards.py states what the
evidence does.
"""

import contextlib
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import guards                    # noqa: E402
import items                     # noqa: E402  (registers the ten)
import menu as M                 # noqa: E402
import world as W                # noqa: E402
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)      # noqa: E402


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_paths", REPO / "src" / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()

UPLOADER, LOCAL = M.Strategy.UPLOADER, M.Strategy.LOCAL_PAGE


@contextlib.contextmanager
def quiet():
    """The runner and the painter print. A test reads the machine, not the ink."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


# ---------------------------------------------------------------------------
# The mocks: one world the items judge, one Work that records instead of doing
# ---------------------------------------------------------------------------

CLIP = "20260728090000"
RENDERED = W.Render("trip_2026-07-28_08-57_01_h1080.mp4", 64)
SIDECAR = W.TripMeta("trip_2026-07-28_08-57_01", "20260728080000", "20260728093000")


def _target(strategy):
    """A publishing install has a plugin that answers; a local one has no
    plugin at all, and NA is not the same answer as no.

    Which destination it is stopped being this file's business when the second
    repo became an implementation: what a path test needs is a destination that
    is not in the way, so nothing here refuses for a reason about evidence when
    the subject is order.

    NA, and that is the only answer that works here now that there is one.
    YES would make item 7 SATISFIED — a legitimate answer and the wrong one to
    hold constant in a file about which paths EXIST, because a satisfied item
    completes without running. NO would refuse item 8. NA is the plugin
    declining the destination question, which neither satisfies the upload nor
    blocks the erase: it is the state where nothing about the destination is in
    the way of anything, which is what a file about ORDER needs.
    """
    if strategy is UPLOADER:
        return W.TargetFacts(configured=True, name="a target",
                             origin="a target (a test)", complete=M.Evidence.NA)
    return W.TargetFacts()


def every_item_can_run(strategy, **override):
    """A world in which all ten items have their evidence.

    The point of these tests is the ORDER, so the evidence is held constant:
    what refuses an item here is the position it is asked from, never a
    missing file. The one exception is stated in its own test, which takes
    this world and removes exactly one fact.

    One pair cannot both be satisfied by a single frozen world, and that is a
    real rule rather than a gap in the fixture: item 1 wants clips accounted
    for by nothing, item 9 wants every clip accounted for by something. In a
    live run the import itself moves a clip from the first set to the second;
    here the world does not move, so item 1 answers SATISFIED — completing
    without performing, which is what a step with nothing to do means and
    what these walks then have to allow for.
    """
    facts = dict(
        strategy=strategy, out_dir=Path("/w/out"),
        imports=(Path("/w/import"),), selected_import=Path("/w/import"),
        metas=(SIDECAR,), renders=(RENDERED,), renders_here=(RENDERED,),
        expected_trips=1, has_track=True, stills_current=True,
        ledger_mark=CLIP, workspace_settled=True, local_page=True,
        card=W.Card(path=Path("/w/card"), dcim=True, present=True,
                    stamps=frozenset({CLIP})),
        target=_target(strategy))
    facts.update(override)
    return W.World(**facts)


class FakeBuilder:
    """Item 5's collaborator. Both products build; what differs is what gets
    built, which is the implementation's business and not this file's."""

    def __init__(self, done):
        self._done = done

    def describe(self):
        return "Build whatever this product publishes."

    def evaluate(self, world):
        return M.go()

    def execute(self, world):
        self._done.append(BUILD)
        return M.did("built")


class FakePublisher:
    """Item 7's collaborator, installed by the constructor and never branched
    on afterwards. Under the local product it refuses by configuration."""

    def __init__(self, strategy, done):
        self._local = strategy is LOCAL
        self._done = done

    def describe(self):
        return "Put what was built online."

    def evaluate(self, world):
        if self._local:
            return M.blocked("no upload_plugin is configured")
        return M.go()

    def execute(self, world):
        self._done.append(UPLOAD)
        return M.did("uploaded")


def _workspace_gate(world):
    """The sweep's gate in the shape menu.Plan takes: world -> Verdict."""
    return guards.Gates(world).workspace_is_expendable()


class FakeWork:
    """Every body an item can reach, replaced by a recorder.

    items.py imports nothing but menu and guards, which is what makes this
    possible: the ten real classes, their real evaluate(), their real edges,
    and no pipeline behind them.
    """

    def __init__(self, strategy):
        self.strategy = strategy
        self.done = []            # the numbers whose work actually ran
        self.shown = []           # the banners a destructive item printed
        self.asked = []           # the words it asked for
        self.refused = []
        self.answer = None        # what the operator types; None = the word
        self.fresh = None         # what recapture() finds; None = unchanged

    # -- the plain bodies --------------------------------------------------
    def progress(self, world):
        return self._ran(PROGRESS)

    def import_footage(self, world):
        return self._ran(IMPORT)

    def generate_meta(self, world):
        return self._ran(META)

    def build_preview(self, world):
        return self._ran(PREVIEW)

    def render(self, world):
        return self._ran(RENDER)

    def _ran(self, number):
        self.done.append(number)
        return M.did("mocked")

    # -- the collaborators the constructor installs ------------------------
    def builder(self, strategy):
        return FakeBuilder(self.done)

    def publisher(self, strategy):
        return FakePublisher(strategy, self.done)

    # -- the destructive plans ---------------------------------------------
    def exclude_plan(self, world):
        return M.Plan(M.nothing_to_recheck, self._drop, ("would drop trip_a",))

    def clean_workspace_plan(self, world):
        return M.Plan(_workspace_gate, self._clean,
                      ("would erase the import and its renders",))

    def erase_card_plan(self, world):
        return M.Plan(guards.card_is_expendable, self._erase,
                      ("would erase 1 clip from the card",))

    def _drop(self, world):
        return self._ran(EXCLUDE)

    def _clean(self, world):
        return self._ran(CLEAN_WS)

    def _erase(self, world):
        return self._ran(ERASE_CARD)

    # -- what Destructive needs between the plan and the act ---------------
    def show(self, banner):
        self.shown.append(tuple(banner))

    def ask_word(self, word):
        self.asked.append(word)
        if self.answer is None:
            return word
        return self.answer

    def recapture(self, scope):
        """The refresh point, under the test's control: `fresh` is the world
        that turned up AFTER the operator typed the word."""
        return self.fresh or self.world

    def refuse(self, name, reason):
        self.refused.append(reason)
        return M.stopped("refused after re-check: %s" % reason)


class FakeCtx:
    """As much ctx as the runner reads once capture_world is mocked out.

    It needs an out_dir because the runner writes where it left off beside the
    ledger, and a temp dir it owns is the answer -- the same discipline the
    archive uses: no attribute means the fixture has not said where, which is
    loud, rather than a default that writes somewhere real.
    """

    def __init__(self):
        self.results = []
        self.out_dir = Path(tempfile.mkdtemp(prefix="dashcam-paths-"))
        self.state_dir = self.out_dir / "state"
        self.workspace = self.out_dir
        self.plugin = None


# ---------------------------------------------------------------------------
# The bench: the real ten items, the real Runner, a scripted operator
# ---------------------------------------------------------------------------

class Bench:
    """A pipeline standing at a position, driven by a list of keystrokes."""

    def __init__(self, strategy=UPLOADER, world=None, current=M.NOWHERE):
        self.strategy = strategy
        self.work = FakeWork(strategy)
        self.menu = M.build_menu(strategy, self.work)
        self.position = M.position_for(self.menu)
        self.position.current = current
        self.world = world or every_item_can_run(strategy)
        self.work.world = self.world
        self.runner = P.Runner(FakeCtx(), self.menu, self.position)
        self.offered_at = []

    def selectable(self):
        return sorted(self.position.selectable(self.menu))

    def type(self, *keys):
        """Feed the real menu loop, recording what was on offer at each prompt.

        The record is taken where the operator sees it — after the menu is
        painted, before the key is read — so a hop asserted here is a hop the
        machine offered, not one a test computed for itself.
        """
        pressed = iter(keys + ("q",))
        with quiet() as out, self._patched(pressed):
            self.runner.loop()
        return out.getvalue()

    def _patched(self, pressed):
        return _patches(mock.patch.object(P, "ask", side_effect=self._ask(pressed)),
                     mock.patch.object(P, "capture_world",
                                       side_effect=lambda ctx, scope=None: self.world))

    def _ask(self, pressed):
        def answer(prompt, default="", quits=True):
            self.offered_at.append(self.selectable())
            return next(pressed)
        return answer


@contextlib.contextmanager
def _patches(*applied):
    with contextlib.ExitStack() as stack:
        for patch in applied:
            stack.enter_context(patch)
        yield


# The offers, in the owner's own numbers. Written out rather than read off the
# items, because a test that derives its expectation from the thing under test
# agrees with it whatever it says.
START = [PROGRESS, IMPORT]                                     # nothing done yet
# After a clean-up: a new cycle, or cleaning the next import in the sink.
# 9 is deliberately absent — that is the order the unfold exists to forbid.
AFTER_CLEAN = [PROGRESS, IMPORT, CLEAN_WS]
AFTER_IMPORT = [PROGRESS, IMPORT, META, CLEAN_WS, ERASE_CARD]   # item 1's {1,2,8,9}
# RESTATED: 1 is in its own outbound. A card holds more than one session's
# worth and a copy can be interrupted, so importing again is an ordinary next
# move -- the delta decides how much. Without it, landing on item 1 with
# footage in the workspace took item 1 off the menu.
MID_CYCLE = [PROGRESS, META, PREVIEW, EXCLUDE, BUILD, RENDER, CLEAN_WS, ERASE_CARD]
# Under the publishing edition item 7 is in EVERY mid-cycle offer, not just
# Build's. Whether it may run is the publisher's answer about built material;
# the graph does not spell that as an ordering rule.
MID_CYCLE_PUBLISHING = sorted(MID_CYCLE + [UPLOAD])
# RESTATED: rule 6's {4,2,8,9} plus 3. After a drop the sidecars described
# trips that no longer existed, so the only way on was to write them again;
# item 4 removes those sidecars with the footage now, so looking at what is
# left needs no regeneration first.
AFTER_EXCLUDE = [PROGRESS, META, PREVIEW, EXCLUDE, CLEAN_WS, ERASE_CARD]
AFTER_EXCLUDE_PUBLISHING = sorted(AFTER_EXCLUDE + [UPLOAD])


# ---------------------------------------------------------------------------
# Half one: the paths
# ---------------------------------------------------------------------------

class TestTheNormalCycle(unittest.TestCase):
    """One card from the slot to the site and back to an empty workspace."""

    def test_the_full_publishing_cycle_offers_the_owners_table_at_every_hop(self):
        """Import, meta, preview, build, upload, render, build, upload, clean.

        The pages-first cycle, and the site goes up twice on purpose: once
        while the trips are only described, once more when their videos exist.
        Rendering does NOT offer Upload -- the manifest a render invalidates is
        the one that says those videos are not there, so the way from an encode
        to the site is back through Build. That is the graph enforcing it, not
        an ordering rule written anywhere.

        Each expected set is the item's own outbound column plus Progress,
        which neighbours everything. Cleaning the workspace ends the cycle:
        its outbound is {1}, so what is left is a new import and nothing else.
        """
        b = Bench(UPLOADER)
        b.type("1", "2", "3", "5", "7", "6", "5", "7", "8")
        # No IMPORT: this world's card is fully accounted for, so item 1
        # completes without performing. It still advances the position, which
        # is what the offer table below is asserting.
        self.assertEqual(b.work.done,
                         [META, PREVIEW, BUILD, UPLOAD, RENDER,
                          BUILD, UPLOAD, CLEAN_WS])
        self.assertEqual(b.offered_at,
                         [START, AFTER_IMPORT] +
                         [MID_CYCLE_PUBLISHING] * 7 + [AFTER_CLEAN])

    def test_rendering_offers_the_upload_straight_away(self):
        """A render produces an mp4 and no metadata at all, so the manifest
        built before it is still correct after it and sending the videos is
        the whole of what is left to do. Making the operator rebuild first
        would be the graph asking for work the data does not justify."""
        b = Bench(UPLOADER)
        b.type("1", "2", "3", "5", "7", "6")
        self.assertIn(UPLOAD, b.offered_at[-1])

    def test_the_early_loop_the_owner_works_in(self):
        """Import, meta, build, upload, look, exclude a trip, regenerate.

        The loop that catches a bad trip before the hours of encoding. Two
        things it pins: looking at Progress does not move the pipeline, and
        the moment a trip is dropped the offer narrows to {4,2,8,9} — the
        sidecars now describe a trip that no longer exists, so the only way
        forward is to write them again.
        """
        b = Bench(UPLOADER)
        b.type("1", "2", "5", "7", "0", "4", "2")
        self.assertEqual(b.work.done, [META, BUILD, UPLOAD, PROGRESS,
                                       EXCLUDE, META])
        self.assertEqual(b.offered_at[4], b.offered_at[5],
                         "looking at Progress moved the pipeline")
        self.assertEqual(b.offered_at[6], AFTER_EXCLUDE_PUBLISHING)
        self.assertEqual(b.offered_at[7], MID_CYCLE_PUBLISHING)


class TestThePathsThatMustNotExist(unittest.TestCase):
    """What the graph refuses, asserted by trying it."""

    def test_a_number_the_position_does_not_offer_runs_nothing(self):
        """Rendering is not reachable with nothing imported. The refusal is
        the graph's, before any guard is asked."""
        b = Bench(UPLOADER)
        out = b.type("5")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.offered_at, [START, START])
        self.assertIn("is not available", out)

    def test_publishing_is_on_no_path_at_all_under_the_local_product(self):
        """Not by an `if` in a body: nothing anywhere offers it.

        The reachable set is walked from every position in turn, so the claim
        is about the whole graph and not about one route through it.
        """
        built = M.build_menu(LOCAL, FakeWork(LOCAL))
        self.assertEqual(_reachable(built), set(built) - {UPLOAD})

    def test_publishing_is_reachable_under_the_publishing_product(self):
        built = M.build_menu(UPLOADER, FakeWork(UPLOADER))
        self.assertEqual(_reachable(built), set(built))

    def test_freeing_the_card_can_never_follow_erasing_the_workspace(self):
        """The defect the unfold closes, expressed as a path that does not
        exist. The folded step gathered the card's evidence from the
        workspace, erased the workspace, then asked about the card — refusing
        after the irreversible half had run. Clean Workspace's outbound is
        {1,8} — itself and a new cycle, never 9 — so there is no position from
        which that order can be typed.
        """
        b = Bench(UPLOADER)
        b.type("1", "2", "8", "9")
        self.assertEqual(b.work.done, [META, CLEAN_WS])
        self.assertEqual(b.offered_at[3], AFTER_CLEAN)
        self.assertNotIn(ERASE_CARD, AFTER_CLEAN)

    def test_freeing_the_card_first_leaves_the_workspace_still_cleanable(self):
        """The safe order is the permitted one. Erasing the card steps back by
        one, so wherever we were is where we still are, and 8 is still on
        offer from there."""
        b = Bench(UPLOADER)
        b.type("1", "2", "9", "8")
        self.assertEqual(b.work.done, [META, ERASE_CARD, CLEAN_WS])
        self.assertEqual(b.offered_at[2], b.offered_at[3],
                         "erasing the card moved the pipeline")


class TestDestructiveItemsOnThePath(unittest.TestCase):
    """Reaching one is not being allowed to run it."""

    def test_the_card_is_not_erased_without_the_evidence_for_it(self):
        """A clip that exists nowhere but the card refuses the erase, and the
        refusal comes BEFORE the banner and the word — an operator who has
        typed ERASE has been told the erase was going to happen."""
        stranded = every_item_can_run(UPLOADER,
                                      card=W.Card(path=Path("/w/card"), dcim=True,
                                                  present=True,
                                                  stamps=frozenset({CLIP}),
                                                  owed_stamps=frozenset({CLIP})))
        b = Bench(UPLOADER, world=stranded, current=RENDER)
        b.type("9")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.work.shown, [])
        self.assertEqual(b.work.asked, [])

    def test_anything_but_the_word_erases_nothing_and_stays_put(self):
        """Rule 3: an aborted step does not complete, so the pipeline is
        exactly where it was and the wide offer survives."""
        b = Bench(UPLOADER, current=PREVIEW)
        b.work.answer = "yes"
        b.type("4")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.work.asked, ["EXCLUDE"])
        self.assertEqual(b.offered_at[1], MID_CYCLE_PUBLISHING)

    def test_a_world_that_changes_while_the_prompt_is_on_screen_stops_the_act(self):
        """The freshness rule. The guard is asked again against a world
        re-derived AFTER the word is typed, and it is the same callable that
        let the item be selected — so a card swapped between the banner and
        the return key refuses, and nothing is touched.
        """
        b = Bench(UPLOADER, current=RENDER)
        b.work.fresh = every_item_can_run(
            UPLOADER, card=W.Card(path=Path("/w/card"), dcim=True, present=True,
                                 stamps=frozenset({"20260729120000"}),
                                 new_stamps=frozenset({"20260729120000"})))
        b.type("9")
        self.assertEqual(b.work.asked, ["DELETE"], "the word was never asked for")
        self.assertEqual(b.work.done, [], "the card was erased on stale evidence")
        self.assertEqual(len(b.work.refused), 1)
        self.assertIn("new clips", b.work.refused[0])

    def test_each_word_on_the_path_names_its_own_act(self):
        """Item 4 excludes, items 8 and 9 delete. The word names the act, so a
        prompt cannot teach the wrong idea of what is about to happen."""
        b = Bench(UPLOADER, current=PREVIEW)
        b.type("4", "2", "9", "8")
        self.assertEqual(b.work.asked, ["EXCLUDE", "DELETE", "CLEAN"])


class TestIdempotence(unittest.TestCase):
    """Rule 6: an item may be run twice, and the second run is not a hazard."""

    def test_an_item_that_reaches_itself_may_be_run_again_and_again(self):
        """Generate Meta's outbound contains Generate Meta, so this is a path
        the graph offers rather than one it tolerates. Each run is answered by
        the world it is handed and not by a remembered flag, so the third one
        is worth exactly what the first was and the position never drifts.
        """
        b = Bench(UPLOADER)
        b.type("1", "2", "2", "2")
        self.assertEqual(b.work.done, [META, META, META])
        self.assertEqual(b.offered_at[2], b.offered_at[3])

    def test_a_second_erase_of_an_empty_card_never_reaches_the_prompt(self):
        """SATISFIED is not GO. The card holds no clips, so the postcondition
        already holds: the item completes, the position moves as if it had
        run, and no banner and no word ever appear. An item that asks for
        ERASE to discover there is nothing behind it is teaching the operator
        to type it without reading.
        """
        emptied = every_item_can_run(UPLOADER,
                                     card=W.Card(path=Path("/w/card"), dcim=True))
        b = Bench(UPLOADER, world=emptied, current=RENDER)
        b.type("9")
        self.assertEqual(b.work.asked, [])
        self.assertEqual(b.work.done, [])
        self.assertTrue(b.menu[ERASE_CARD].completed(),
                        "an already-empty card is a completed erase")

    # completed() raising NotRun before an item has run is pinned once, in
    # test_menuitems.py, where the item's own answers live. Driving a Bench to
    # build a menu and then never walking a path asserted nothing this file is
    # for.


class TestEveryShortPath(unittest.TestCase):
    """Every route the graph offers, driven, not reasoned about.

    Enumerating them is what catches an edge that only misbehaves in company:
    a position reached two ways, an item that completes on one route and not
    the other. Bounded at five selections, which is long enough to leave the
    start, loop back on itself, and reach both destructive items by more than
    one route — about 1700 walks across the two products.
    """

    DEPTH = 5

    def test_every_path_the_graph_offers_lands_where_the_graph_says(self):
        for strategy in M.Strategy:
            self._walk_all(strategy)

    def _walk_all(self, strategy):
        for path in _paths(M.build_menu(strategy, FakeWork(strategy)), self.DEPTH):
            with self.subTest(strategy=strategy.value, path=path):
                self._drive(strategy, path)

    def _drive(self, strategy, path):
        b = Bench(strategy)
        b.type(*map(str, path))
        # Item 1 is SATISFIED against this frozen world -- its clips are
        # already accounted for -- so it completes without performing and
        # never reaches the recorder. It still advances the position, which is
        # what the walk is about.
        self.assertEqual(b.work.done, [n for n in path if n != IMPORT],
                         "a path the graph offers did not run")
        self.assertEqual(b.position.current, _lands_at(b.menu, path))

    def test_no_path_runs_an_item_that_was_not_on_offer(self):
        """The offer recorded at each prompt is what the operator saw. Every
        key pressed on every enumerated path has to have been in it."""
        for path in _paths(M.build_menu(UPLOADER, FakeWork(UPLOADER)), self.DEPTH):
            self._all_were_offered(path)

    def _all_were_offered(self, path):
        b = Bench(UPLOADER)
        b.type(*map(str, path))
        with self.subTest(path=path):
            self.assertEqual(_unoffered(path, b.offered_at), [])


class TestTheMenuSaysWhichProductItIs(unittest.TestCase):
    """An item this product does not have is a different sentence from an item
    the pipeline has not reached yet.

    Both are greyed and neither is selectable, so lumping them together reads
    as "keep going and it will turn up". Upload Website never turns up on a
    local install however far you walk, and the only useful thing the menu can
    say about it is which product you are running. The distinction is read off
    the edges — nothing leads in, nothing leads out — rather than from a list
    of which items each product includes, because a second list is a second
    place to disagree with the graph the tool actually walks.
    """

    def test_the_item_with_no_edges_names_the_strategy(self):
        """Asked for with `p` rather than printed under every draw, along with
        every other reason an entry is greyed."""
        b = Bench(LOCAL)
        b.type()
        said = "\n".join(P._why_lines(b.menu, P._verdicts(b.menu, b.world),
                                       b.position.selectable(b.menu)))
        self.assertIn("not available for %s" % LOCAL.value, said)

    def test_it_is_not_reported_as_merely_not_reached_yet(self):
        b = Bench(LOCAL)
        line = _line_holding(b.type(), "not available from here")
        self.assertNotIn(str(UPLOAD), _numbers_in(line),
                         "the item this product lacks was filed under 'not yet'")

    def test_the_publishing_product_says_it_of_nobody(self):
        """Every item exists there, so the sentence has no subject and must not
        be printed at all."""
        self.assertNotIn("not available for", Bench(UPLOADER).type())

    def test_the_switched_off_test_does_not_catch_a_start_node(self):
        """Import SIM also declares an empty inbound, because a cycle has to
        begin somewhere. It leads on, so it is not switched off."""
        for strategy in M.Strategy:
            built = M.build_menu(strategy, FakeWork(strategy))
            with self.subTest(strategy=strategy.value):
                self.assertFalse(M.switched_off(built[IMPORT]))
                self.assertFalse(M.switched_off(built[PROGRESS]))


def _line_holding(out, phrase):
    found = filter(lambda l: phrase in l, out.splitlines())
    return next(found, "")


def _numbers_in(line):
    return re.findall(r"\d+", line)


def _offer_from(built, current):
    """What the machine itself offers from that position — never a set this
    file computed for itself."""
    position = M.position_for(built)
    position.current = current
    return position.selectable(built)


def _paths(built, depth, current=M.NOWHERE, prefix=()):
    """Every sequence of selections of at most `depth` the graph permits."""
    if len(prefix) >= depth:
        return [prefix]
    return [prefix] + _branches(built, depth, current, prefix)


def _branches(built, depth, current, prefix):
    found = []
    for number in sorted(_offer_from(built, current)):
        found += _paths(built, depth, built[number].settles_at(current),
                        prefix + (number,))
    return found


def _lands_at(built, path):
    current = M.NOWHERE
    for number in path:
        current = built[number].settles_at(current)
    return current


def _unoffered(path, offers):
    """The keys pressed that were not on the menu when they were pressed."""
    return [pressed[0] for pressed in filter(_was_not_offered, zip(path, offers))]


def _was_not_offered(pressed):
    return pressed[0] not in pressed[1]


def _reachable(built):
    """Every item reachable from the entry point, by any route.

    The frontier holds POSITIONS, not items, which is the distinction that
    matters: Progress and Delete SIM Data are selectable without becoming the
    position, so following them as if they were would offer whatever the
    machine offers from everywhere.
    """
    seen, frontier, been = set(), {M.NOWHERE}, set()
    while frontier:
        current = frontier.pop()
        been.add(current)
        offer = set(_offer_from(built, current))
        seen |= offer
        frontier |= _settling_at(built, current, offer) - been
    return seen


def _settling_at(built, current, offer):
    """The positions those selections would leave the pipeline in."""
    return {built[number].settles_at(current) for number in offer}


# ---------------------------------------------------------------------------
# Half two: the menu is drawn from the machine and from nothing else
# ---------------------------------------------------------------------------

class Painted:
    """A menu item as the PAINTER sees it: a number, a name, a flag, an answer.

    Not a MenuItem, and not one of the ten. It is the whole interface the
    drawing code is allowed to use, so anything the painter needs that is not
    here is something it went and got from somewhere else.
    """

    def __init__(self, number, name, verdict, out, destr=False):
        self.number = number
        self._name = name
        self._verdict = verdict
        self._out = out
        self._destr = destr

    def name(self):
        return self._name

    def destr(self):
        return self._destr

    def start(self):
        return False

    def description(self):
        return "an item the painter has never heard of"

    def evaluate(self, world):
        return self._verdict

    def outbound(self):
        return self._out

    def inbound(self):
        return self._out


# Numbers outside 0-9 and names no step has ever had. If the painter knows any
# of this without being told, these tests are the ones that notice.
ZETA, YANKEE, XRAY, WHISKEY = 11, 12, 13, 14
YAK = "the yak is out"


def invented_menu():
    """A machine of four items: one current, one blocked, one destructive, one
    the position does not offer."""
    return {
        ZETA: Painted(ZETA, "Zeta", M.go(), M.Edges(frozenset({YANKEE, XRAY}))),
        YANKEE: Painted(YANKEE, "Yankee", M.blocked(YAK), M.Edges(frozenset({ZETA}))),
        XRAY: Painted(XRAY, "Xray", M.go(), M.Edges(frozenset({ZETA})), destr=True),
        WHISKEY: Painted(WHISKEY, "Whiskey", M.go(), M.Edges(frozenset({ZETA}))),
    }


def invented_position(built, current=ZETA):
    return M.Position(frozenset(built), frozenset(), frozenset({ZETA}), current)


class PainterTest(unittest.TestCase):
    """Colour on, width fixed: the ink is the assertion here."""

    def setUp(self):
        self.was = P.C.enabled
        P.C.enabled = True
        self.width = mock.patch.object(P, "term_width", return_value=100)
        self.width.start()

    def tearDown(self):
        P.C.enabled = self.was
        self.width.stop()

    def paint(self, built, position, world=None):
        with quiet() as out:
            P.print_menu(FakeCtx(), built, position, world or W.World())
        return out.getvalue()

    def reasons(self, built, position, world=None):
        """What `s` says about the greyed entries.

        They used to print under every menu draw. Eight lines that are the same
        on every draw stop being read, which is a problem when the one that
        changed is among them — so they are asked for now, and this is where
        the tests ask.
        """
        w = world or W.World()
        return "\n".join(P._why_lines(built, P._verdicts(built, w),
                                       position.selectable(built)))


DIM, RED, BOLD = "\x1b[2m%s\x1b[0m", "\x1b[31m%s\x1b[0m", "\x1b[1m%s\x1b[0m"


class TestTheMenuIsTheMachine(PainterTest):
    """Everything drawn is something an item said about itself."""

    def test_the_menu_shows_exactly_the_entries_the_machine_holds(self):
        """Four items with invented numbers and invented names, drawn whole.

        A step number or a label written into the drawing code cannot survive
        this: there is no item 6 here, and nothing is called Render Trips.
        """
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        for number, item in built.items():
            with self.subTest(item=number):
                self.assertIn("%d) " % number, out)
                self.assertIn(item.name(), out)

    def test_no_real_step_label_leaks_into_a_menu_that_has_none(self):
        """The negative half of the same claim, and the one that fails loudly
        if anyone reaches for the ten by name again."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        for name in items.NAMES.values():
            self.assertNotIn(name, out, "a label the machine never mentioned")

    def test_no_real_step_number_leaks_into_a_menu_that_has_none(self):
        """The same, for the numbers. This machine's items are 11 to 14, so a
        0-9 anywhere in the grid came from the painter and not from an item."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        for number in range(10):
            self.assertEqual(_entries_for(out, number), [],
                             "a step number the machine never mentioned")

    def test_the_invented_items_never_became_part_of_the_machine(self):
        """These four exist to prove the painter asks the interface and
        nothing else. They are not MenuItems and nothing registered them, so
        the product still ships exactly ten."""
        self.assertEqual(sorted(M.registry()), list(range(10)))

    def test_grey_is_exactly_the_entries_that_cannot_be_picked(self):
        """Two gates, one colour: the position not offering it, or its own
        guard blocking it. Zeta is where we are and offers only 12 and 13, so
        Zeta and Whiskey are out by the graph and Yankee by its guard."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertEqual(_names_in(out, DIM, built), {"Zeta", "Yankee", "Whiskey"})

    def test_bold_is_every_entry_that_can_be_picked_destructive_or_not(self):
        """REVERSED: Xray was red for being destructive. Two states is all the
        grid has now — grey cannot be picked, bold can — and unpickable still
        outranks everything, so an entry the position does not offer is grey
        whatever its flag says."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertEqual(_names_in(out, BOLD, built), {"Xray"})
        self.assertEqual(_names_in(out, RED, built), set())

    def test_a_blocked_entry_gives_the_reason_the_item_itself_gave(self):
        """Verbatim, and attached to its own number. The painter has no
        wording of its own for why an item cannot run."""
        built = invented_menu()
        out = self.reasons(built, invented_position(built))
        self.assertIn("%d) %s" % (YANKEE, YAK), out)

    def test_the_entries_the_position_does_not_offer_are_named_together(self):
        """One line for the graph's refusal, naming them all: it is the same
        sentence for every entry it applies to, and printed once per entry it
        taught the eye to skip the block that also holds the actionable ones.
        """
        built = invented_menu()
        out = self.reasons(built, invented_position(built))
        self.assertIn("%d,%d) not available from here" % (ZETA, WHISKEY), out)
        self.assertNotIn("%d) not available from here" % YANKEE, out)

    def test_where_we_are_is_said_in_the_items_own_words(self):
        """RESTATED TWICE: first the position moved off the menu footer onto
        the progress screen -- under the grid it was one more thing identical
        on every draw. Then it stopped being "Position: 8) Clean Workspace"
        and became "Last: Clean Workspace". The number is already beside that
        entry in the grid, and the reader is asking what he last did, not
        where a machine is."""
        built = invented_menu()
        said = "\n".join(P._where_lines(built, invented_position(built)))
        self.assertIn("Last: Zeta", said)
        self.assertNotIn("%d)" % ZETA, said)

    def test_at_the_start_it_says_so_rather_than_naming_an_item(self):
        built = invented_menu()
        said = "\n".join(P._where_lines(built, invented_position(built, M.NOWHERE)))
        self.assertIn("nothing yet", said)

    def test_nothing_in_the_grid_is_red(self):
        """RESTATED TWICE, and the second time undoes the first. The footer
        used to name the destructive entries as well; that was the third
        telling of one fact, so the line went and the colour stayed. The colour
        has now gone after it, for the same reason it was always the weakest of
        the three: Exclude Trip, Clean Workspace and Delete SIM Data say what
        they do in their own names, and each asks for a typed word first.

        Red on a resting menu is red that has stopped meaning anything. It is
        kept for the only-copy banner and for a step that failed, neither of
        which appears unless something is genuinely wrong."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertNotIn("\x1b[31m", out)
        self.assertNotIn("destroy footage", out)

    def test_a_guard_that_raises_greys_its_entry_instead_of_taking_the_menu_down(self):
        """A menu that cannot be drawn is a tool that cannot be used, and the
        item whose guard fell over is the only one that loses anything."""
        built = invented_menu()
        built[YANKEE].evaluate = _explode
        position = invented_position(built)
        out = self.paint(built, position)
        self.assertIn("Yankee", out, "the entry is still drawn")
        self.assertIn("Xray", out, "and so is everything after it")
        self.assertIn("guard error: the guard fell over",
                      self.reasons(built, position))


def _explode(world):
    raise RuntimeError("the guard fell over")


def _names_in(out, colour, built):
    """Which item names were painted in this colour."""
    painted = filter(lambda item: colour % item.name() in out, built.values())
    return {item.name() for item in painted}


def _entries_for(out, number):
    """The menu entries drawn for this number, if any.

    A lookbehind, because "11) Zeta" contains "1) Zeta" and a plain substring
    test would say the painter had drawn an item 1 that does not exist.
    """
    return re.findall(r"(?<![0-9])%d\) " % number, out)


class TestThePaintedMenuMatchesTheRealMachine(PainterTest):
    """The same claim, against the ten items and the world they judge."""

    def test_every_greyed_entry_is_one_the_machine_refuses(self):
        """Painted grey iff unselectable: the position does not offer it, or
        its own evaluate() blocks. Asserted for the real ten under both
        products, from a position in the middle of the cycle."""
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                self._check(strategy)

    def _check(self, strategy):
        b = Bench(strategy, current=RENDER)
        out = self.paint(b.menu, b.position, b.world)
        self.assertEqual(_names_in(out, DIM, b.menu),
                         {b.menu[n].name() for n in _refused(b)})

    def test_the_menu_draws_one_entry_per_step_and_no_others(self):
        """Every STEP gets an entry, in the machine's own order, and a number
        the menu draws that no item holds is a number written into the painter.

        Progress is not drawn: it is a view, it changes nothing, and it is
        reached with `p` like the other keys that only show you something. It
        is still in the machine and still reachable -- what changed is that the
        grid lists steps, and looking at the workspace is not one."""
        b = Bench(UPLOADER, current=RENDER)
        out = self.paint(b.menu, b.position, b.world)
        steps = sorted(n for n in b.menu if not M.is_view(b.menu[n]))
        self.assertEqual(_numbers_drawn(out), steps)
        self.assertNotIn(PROGRESS, _numbers_drawn(out))


def _numbers_drawn(out):
    return list(filter(lambda n: _entries_for(out, n), range(20)))


def _refused(bench):
    """The two gates that grey an entry, as one set of numbers."""
    offered = set(bench.position.selectable(bench.menu))
    return (set(bench.menu) - offered) | (_blocked_in(bench) & offered)


def _blocked_in(bench):
    stopped = filter(lambda i: i.evaluate(bench.world).blocked, bench.menu.values())
    return {item.number for item in stopped}


if __name__ == "__main__":
    unittest.main(verbosity=2)
