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
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import guards                    # noqa: E402
import items                     # noqa: E402  (registers the ten)
import menu as M                 # noqa: E402
import world as W                # noqa: E402
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)      # noqa: E402


def load_pipeline():
    sys.argv = ["pipeline.py"]
    spec = importlib.util.spec_from_file_location("pipeline_paths", REPO / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = load_pipeline()

WEBSITE, LOCAL = M.Strategy.WEBSITE_REPO, M.Strategy.LOCAL_DEFAULT_WEBSITE


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


def _site(strategy):
    """A publishing install has a second repo and a live answer; a local one
    has neither, and NA is not the same answer as no."""
    if strategy is WEBSITE:
        return W.SiteFacts(configured=True, on_disk=True, published=M.Evidence.YES,
                           scripts=frozenset({"upload-videos-s3.sh", "deploy-site.sh"}),
                           videos_link=Path("/w/out"), page=True)
    return W.SiteFacts(page=True)


def _bucket(strategy):
    if strategy is WEBSITE:
        return W.Listed({"videos/" + RENDERED.name: RENDERED.size})
    return W.NoBucket()


def every_item_can_run(strategy, **override):
    """A world in which all ten items have their evidence.

    The point of these tests is the ORDER, so the evidence is held constant:
    what refuses an item here is the position it is asked from, never a
    missing file. The one exception is stated in its own test, which takes
    this world and removes exactly one fact.
    """
    facts = dict(
        strategy=strategy, out_dir=Path("/w/out"),
        imports=(Path("/w/import"),), selected_import=Path("/w/import"),
        metas=(SIDECAR,), renders=(RENDERED,), renders_here=(RENDERED,),
        expected_trips=1, has_track=True, stills_current=True,
        ledger_mark=CLIP, workspace_settled=True, awscli=True,
        card=W.Card(path=Path("/w/card"), dcim=True, present=True,
                    stamps=frozenset({CLIP})),
        bucket=_bucket(strategy), site=_site(strategy))
    facts.update(override)
    return W.World(**facts)


class FakePublisher:
    """Item 7's collaborator, installed by the constructor and never branched
    on afterwards. Under the local product it refuses by configuration."""

    def __init__(self, strategy, done):
        self._local = strategy is LOCAL
        self._done = done

    def why_not(self, world):
        if self._local:
            return "needs site_repo and s3_bucket in config.txt"
        return None

    def run(self, world):
        self._done.append(UPLOAD)
        return M.did("uploaded")


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

    def build_website(self, world, gather):
        return self._ran(BUILD)

    def _ran(self, number):
        self.done.append(number)
        return M.did("mocked")

    # -- the collaborators the constructor installs ------------------------
    def gatherer(self, strategy):
        return "gather-under-%s" % strategy.value

    def publisher(self, strategy):
        return FakePublisher(strategy, self.done)

    # -- the destructive plans ---------------------------------------------
    def exclude_plan(self, world):
        return M.Plan(M.nothing_to_recheck, self._drop, ("would drop trip_a",))

    def clean_workspace_plan(self, world):
        return M.Plan(guards.workspace_is_expendable, self._clean,
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

    def refuse(self, reason):
        self.refused.append(reason)
        return M.stopped("refused after re-check: %s" % reason)


class FakeCtx:
    """As much ctx as the runner reads once capture_world is mocked out."""

    def __init__(self):
        self.results = []


# ---------------------------------------------------------------------------
# The bench: the real ten items, the real Runner, a scripted operator
# ---------------------------------------------------------------------------

class Bench:
    """A pipeline standing at a position, driven by a list of keystrokes."""

    def __init__(self, strategy=WEBSITE, world=None, current=M.NOWHERE):
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
AFTER_IMPORT = [PROGRESS, META, CLEAN_WS, ERASE_CARD]          # item 1's {2,8,9}
MID_CYCLE = [PROGRESS, META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD]
MID_CYCLE_PUBLISHING = sorted(MID_CYCLE + [UPLOAD])            # from 6 and 7 only
AFTER_EXCLUDE = [PROGRESS, META, EXCLUDE, CLEAN_WS, ERASE_CARD]  # rule 6's {4,2,8,9}


# ---------------------------------------------------------------------------
# Half one: the paths
# ---------------------------------------------------------------------------

class TestTheNormalCycle(unittest.TestCase):
    """One card from the slot to the site and back to an empty workspace."""

    def test_the_full_publishing_cycle_offers_the_owners_table_at_every_hop(self):
        """Import, meta, preview, render, build, upload, clean.

        Each expected set is the item's own outbound column plus Progress,
        which neighbours everything. Cleaning the workspace ends the cycle:
        its outbound is {1}, so what is left is a new import and nothing else.
        """
        b = Bench(WEBSITE)
        b.type("1", "2", "3", "5", "6", "7", "8")
        self.assertEqual(b.work.done,
                         [IMPORT, META, PREVIEW, RENDER, BUILD, UPLOAD, CLEAN_WS])
        self.assertEqual(b.offered_at,
                         [START, AFTER_IMPORT,
                          MID_CYCLE, MID_CYCLE, MID_CYCLE,
                          MID_CYCLE_PUBLISHING, MID_CYCLE_PUBLISHING,
                          START])

    def test_the_local_cycle_ends_at_the_built_page(self):
        """The same walk without the publishing item, which the local product
        does not have. Building is what settles the workspace there, so 8
        follows 6 directly."""
        b = Bench(LOCAL)
        b.type("1", "2", "3", "5", "6", "8")
        self.assertEqual(b.work.done, [IMPORT, META, PREVIEW, RENDER, BUILD, CLEAN_WS])
        self.assertEqual(b.offered_at[-1], START)
        self.assertNotIn(UPLOAD, set().union(*map(set, b.offered_at)))

    def test_the_early_loop_the_owner_works_in(self):
        """Import, meta, build, upload, look, exclude a trip, regenerate.

        The loop that catches a bad trip before the hours of encoding. Two
        things it pins: looking at Progress does not move the pipeline, and
        the moment a trip is dropped the offer narrows to {4,2,8,9} — the
        sidecars now describe a trip that no longer exists, so the only way
        forward is to write them again.
        """
        b = Bench(WEBSITE)
        b.type("1", "2", "6", "7", "0", "4", "2")
        self.assertEqual(b.work.done, [IMPORT, META, BUILD, UPLOAD, PROGRESS,
                                       EXCLUDE, META])
        self.assertEqual(b.offered_at[4], b.offered_at[5],
                         "looking at Progress moved the pipeline")
        self.assertEqual(b.offered_at[6], AFTER_EXCLUDE)
        self.assertEqual(b.offered_at[7], MID_CYCLE)

    def test_deploying_straight_from_the_sidecars_is_not_on_any_path(self):
        """Item 2 offers no route to item 7, by the owner's own table.

        His early loop as he describes it goes import, meta, UPLOAD — pushing
        the page from the sidecars alone, hours before a render, to catch a
        broken publish path early. The table he wrote does not contain that
        edge, and nothing about the world blocks it: this is an edge to add or
        a habit to change, and the decision is his. Pinned here so the answer
        is a decision rather than a discovery.
        """
        b = Bench(WEBSITE)
        b.type("1", "2", "7")
        self.assertEqual(b.work.done, [IMPORT, META])
        self.assertNotIn(UPLOAD, b.offered_at[2])


class TestThePathsThatMustNotExist(unittest.TestCase):
    """What the graph refuses, asserted by trying it."""

    def test_a_number_the_position_does_not_offer_runs_nothing(self):
        """Rendering is not reachable with nothing imported. The refusal is
        the graph's, before any guard is asked."""
        b = Bench(WEBSITE)
        out = b.type("5")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.offered_at, [START, START])
        self.assertIn("does not follow", out)

    def test_publishing_is_on_no_path_at_all_under_the_local_product(self):
        """Not by an `if` in a body: nothing anywhere offers it.

        The reachable set is walked from every position in turn, so the claim
        is about the whole graph and not about one route through it.
        """
        built = M.build_menu(LOCAL, FakeWork(LOCAL))
        self.assertEqual(_reachable(built), set(built) - {UPLOAD})

    def test_publishing_is_reachable_under_the_publishing_product(self):
        built = M.build_menu(WEBSITE, FakeWork(WEBSITE))
        self.assertEqual(_reachable(built), set(built))

    def test_freeing_the_card_can_never_follow_erasing_the_workspace(self):
        """The defect the unfold closes, expressed as a path that does not
        exist. The folded step gathered the card's evidence from the
        workspace, erased the workspace, then asked about the card — refusing
        after the irreversible half had run. Clean Workspace's outbound is {1},
        so there is no position from which that order can be typed.
        """
        b = Bench(WEBSITE)
        b.type("1", "2", "8", "9")
        self.assertEqual(b.work.done, [IMPORT, META, CLEAN_WS])
        self.assertEqual(b.offered_at[3], START)

    def test_freeing_the_card_first_leaves_the_workspace_still_cleanable(self):
        """The safe order is the permitted one. Erasing the card steps back by
        one, so wherever we were is where we still are, and 8 is still on
        offer from there."""
        b = Bench(WEBSITE)
        b.type("1", "2", "9", "8")
        self.assertEqual(b.work.done, [IMPORT, META, ERASE_CARD, CLEAN_WS])
        self.assertEqual(b.offered_at[2], b.offered_at[3],
                         "erasing the card moved the pipeline")


class TestDestructiveItemsOnThePath(unittest.TestCase):
    """Reaching one is not being allowed to run it."""

    def test_the_card_is_not_erased_without_the_evidence_for_it(self):
        """A clip that exists nowhere but the card refuses the erase, and the
        refusal comes BEFORE the banner and the word — an operator who has
        typed ERASE has been told the erase was going to happen."""
        stranded = every_item_can_run(WEBSITE,
                                      card=W.Card(path=Path("/w/card"), dcim=True,
                                                  present=True,
                                                  stamps=frozenset({CLIP}),
                                                  owed_stamps=frozenset({CLIP})))
        b = Bench(WEBSITE, world=stranded, current=RENDER)
        b.type("9")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.work.shown, [])
        self.assertEqual(b.work.asked, [])

    def test_anything_but_the_word_erases_nothing_and_stays_put(self):
        """Rule 3: an aborted step does not complete, so the pipeline is
        exactly where it was and the wide offer survives."""
        b = Bench(WEBSITE, current=PREVIEW)
        b.work.answer = "yes"
        b.type("4")
        self.assertEqual(b.work.done, [])
        self.assertEqual(b.work.asked, ["DROP"])
        self.assertEqual(b.offered_at[1], MID_CYCLE)

    def test_a_world_that_changes_while_the_prompt_is_on_screen_stops_the_act(self):
        """The freshness rule. The guard is asked again against a world
        re-derived AFTER the word is typed, and it is the same callable that
        let the item be selected — so a card swapped between the banner and
        the return key refuses, and nothing is touched.
        """
        b = Bench(WEBSITE, current=RENDER)
        b.work.fresh = every_item_can_run(
            WEBSITE, card=W.Card(path=Path("/w/card"), dcim=True, present=True,
                                 stamps=frozenset({"20260729120000"}),
                                 new_stamps=frozenset({"20260729120000"})))
        b.type("9")
        self.assertEqual(b.work.asked, ["ERASE"], "the word was never asked for")
        self.assertEqual(b.work.done, [], "the card was erased on stale evidence")
        self.assertEqual(len(b.work.refused), 1)
        self.assertIn("never imported", b.work.refused[0])

    def test_the_three_destructive_items_ask_three_different_words(self):
        """Two identical prompts is how the second one gets typed from muscle
        memory, and 4, 8 and 9 are now three prompts on one path."""
        b = Bench(WEBSITE, current=PREVIEW)
        b.type("4", "2", "9", "8")
        self.assertEqual(b.work.asked, ["DROP", "ERASE", "CLEAN"])


class TestIdempotence(unittest.TestCase):
    """Rule 6: an item may be run twice, and the second run is not a hazard."""

    def test_an_item_that_reaches_itself_may_be_run_again_and_again(self):
        """Generate Meta's outbound contains Generate Meta, so this is a path
        the graph offers rather than one it tolerates. Each run is answered by
        the world it is handed and not by a remembered flag, so the third one
        is worth exactly what the first was and the position never drifts.
        """
        b = Bench(WEBSITE)
        b.type("1", "2", "2", "2")
        self.assertEqual(b.work.done, [IMPORT, META, META, META])
        self.assertEqual(b.offered_at[2], b.offered_at[3])

    def test_a_second_erase_of_an_empty_card_never_reaches_the_prompt(self):
        """SATISFIED is not GO. The card holds no clips, so the postcondition
        already holds: the item completes, the position moves as if it had
        run, and no banner and no word ever appear. An item that asks for
        ERASE to discover there is nothing behind it is teaching the operator
        to type it without reading.
        """
        emptied = every_item_can_run(WEBSITE,
                                     card=W.Card(path=Path("/w/card"), dcim=True))
        b = Bench(WEBSITE, world=emptied, current=RENDER)
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
        self.assertEqual(b.work.done, list(path),
                         "a path the graph offers did not run")
        self.assertEqual(b.position.current, _lands_at(b.menu, path))

    def test_no_path_runs_an_item_that_was_not_on_offer(self):
        """The offer recorded at each prompt is what the operator saw. Every
        key pressed on every enumerated path has to have been in it."""
        for path in _paths(M.build_menu(WEBSITE, FakeWork(WEBSITE)), self.DEPTH):
            self._all_were_offered(path)

    def _all_were_offered(self, path):
        b = Bench(WEBSITE)
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
        b = Bench(LOCAL)
        out = b.type()
        self.assertIn("not available for %s" % LOCAL.value, out)

    def test_it_is_not_reported_as_merely_not_reached_yet(self):
        b = Bench(LOCAL)
        line = _line_holding(b.type(), "not from here")
        self.assertNotIn(str(UPLOAD), _numbers_in(line),
                         "the item this product lacks was filed under 'not yet'")

    def test_the_publishing_product_says_it_of_nobody(self):
        """Every item exists there, so the sentence has no subject and must not
        be printed at all."""
        self.assertNotIn("not available for", Bench(WEBSITE).type())

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


DIM, RED, BOLD = "\x1b[2m%s\x1b[0m", "\x1b[31m%s\x1b[0m", "\x1b[1m%s\x1b[0m"


class TestTheMenuIsTheMachine(PainterTest):
    """Everything drawn is something an item said about itself."""

    def test_the_menu_shows_exactly_the_entries_the_machine_holds(self):
        """Four items with invented numbers and invented names, drawn whole.

        A step number or a label written into the drawing code cannot survive
        this: there is no item 5 here, and nothing is called Render Videos.
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

    def test_red_is_exactly_the_items_whose_destr_is_true(self):
        """And only while they can be picked: an entry the position does not
        offer is grey, because unpickable outranks dangerous."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertEqual(_names_in(out, RED, built), {"Xray"})
        self.assertEqual(_names_in(out, BOLD, built), set())

    def test_a_blocked_entry_gives_the_reason_the_item_itself_gave(self):
        """Verbatim, and attached to its own number. The painter has no
        wording of its own for why an item cannot run."""
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertIn("%d) %s" % (YANKEE, YAK), out)

    def test_the_entries_the_position_does_not_offer_are_named_together(self):
        """One line for the graph's refusal, naming them all: it is the same
        sentence for every entry it applies to, and printed once per entry it
        taught the eye to skip the block that also holds the actionable ones.
        """
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertIn("%d,%d) not from here" % (ZETA, WHISKEY), out)
        self.assertNotIn("%d) not from here" % YANKEE, out)

    def test_where_we_are_is_said_in_the_items_own_words(self):
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertIn("at: %d) Zeta" % ZETA, out)

    def test_at_the_start_the_footer_says_so_rather_than_naming_an_item(self):
        built = invented_menu()
        out = self.paint(built, invented_position(built, M.NOWHERE))
        self.assertIn("at: the start", out)

    def test_the_footer_lists_the_destructive_items_from_their_own_flag(self):
        built = invented_menu()
        out = self.paint(built, invented_position(built))
        self.assertIn("(%d destroy footage)" % XRAY, out)

    def test_a_guard_that_raises_greys_its_entry_instead_of_taking_the_menu_down(self):
        """A menu that cannot be drawn is a tool that cannot be used, and the
        item whose guard fell over is the only one that loses anything."""
        built = invented_menu()
        built[YANKEE].evaluate = _explode
        out = self.paint(built, invented_position(built))
        self.assertIn("Yankee", out)
        self.assertIn("guard error: the guard fell over", out)
        self.assertIn("Xray", out)


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

    def test_the_menu_draws_one_entry_per_item_and_no_others(self):
        """Ten items, ten entries, in the machine's own order. A number the
        menu draws that no item holds is a number written into the painter."""
        b = Bench(WEBSITE, current=RENDER)
        out = self.paint(b.menu, b.position, b.world)
        self.assertEqual(_numbers_drawn(out), sorted(b.menu))


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
