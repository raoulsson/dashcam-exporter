#!/usr/bin/env python3
"""The ten menu items, one class at a time, against a hand-built world.

Nothing here touches the disk. Every item is handed a `world` object built in
the test body and a `work` collaborator that records what it was asked to do,
which is the whole point of the shape: a guard that goes and looks at the
filesystem can only be tested with a fixture tree, and a guard handed its
evidence can be tested by writing the evidence down.

What this file pins, in the order it appears:

  * WHAT AN ITEM IS. Name and description, and the start/end/destr flags
    exactly as the owner's table declares them.
  * WHERE IT SITS. Inbound and outbound under BOTH strategies. Outbound is
    authored on the class, inbound is derived from every other item's
    outbound, so what is asserted here is the graph the tool actually runs on.
  * THAT THE STRATEGY IS SETTLED IN THE CONSTRUCTOR. The website branch is
    resolved once, when the menu is built. It must not reappear as an `if`
    inside a method the menu calls forty times a session.
  * THE EVIDENCE GUARDS. For each one: a world where the evidence is there and
    the item is offered, and a world where it is not and the item refuses.
    Ordering questions ("has the earlier step run") are the graph's job and are
    deliberately absent; what remains is what an operator can change in Finder
    between the menu being drawn and the key being pressed.
  * IDEMPOTENCE. Every item executed twice in a row: the second run neither
    doubles the effect nor lies about what it did.
  * COMPLETION. `completed()` is true only when a step was actually done —
    with Exclude Trip as the sharp case the owner named, completed IFF a trip
    was really removed and false on every abort path.

Run with:  ./run-tests.sh          (or: python3 -m unittest discover -s tests)
"""

import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dashcam_exporter import guards, items, menu as M, world as W  # noqa: E402
from dashcam_exporter.domain.menu.menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)      # noqa: E402

UPLOADER = M.Strategy.UPLOADER
LOCAL = M.Strategy.LOCAL_PAGE


# ---------------------------------------------------------------------------
# The stand-ins. An item's job is delegated to `work`; these record what was
# asked rather than doing it, so a test can say what happened without a disk.
# ---------------------------------------------------------------------------

class Act:
    """The irreversible half of a plan, which records instead of erasing.

    It keeps the world it was handed, because WHICH world reached it is the
    question: a destructive item must act on the world re-captured after the
    operator typed the word, never on the one the menu was drawn with.
    """

    def __init__(self, note="erased"):
        self.worlds = []
        self.note = note

    def __call__(self, world):
        self.worlds.append(world)
        return M.did(self.note)

    @property
    def ran(self):
        return bool(self.worlds)


class FakeBuilder:
    """Item 5's collaborator: the one the constructor installs.

    Item 5's WHOLE body is the strategy branch now, not merely its mover, so
    what a test checks is that the item asks whichever builder it was given —
    for its menu row, for its refusal, and for the work.
    """

    def __init__(self, strategy, reason=None):
        self.strategy = strategy
        self.reason = reason
        self.worlds = []

    def describe(self):
        return "Build under %s." % self.strategy.value

    def evaluate(self, world):
        if self.reason:
            return M.blocked(self.reason)
        return M.go()

    def execute(self, world):
        self.worlds.append(world)
        return M.did("built")

    @property
    def ran(self):
        return bool(self.worlds)


class FakePublisher:
    """Item 7's collaborator: the one the constructor installs.

    Mirrors the pipeline's split — a real Publisher under the publishing
    product, an unavailable one under the local product — so a test can check
    that the item ASKS its collaborator rather than testing the strategy
    itself.
    """

    def __init__(self, reason):
        self.reason = reason
        self.worlds = []

    def describe(self):
        return "Publish."

    def evaluate(self, world):
        if self.reason:
            return M.blocked(self.reason)
        return M.go()

    def execute(self, world):
        self.worlds.append(world)
        return M.did("published")

    @property
    def ran(self):
        return bool(self.worlds)


class FakeWork:
    """Enough of pipeline.Work to drive all ten items with no filesystem."""

    def __init__(self, plan=None, publish_reason=None, build_reason=None,
                 word="", fresh=None):
        self.calls = []                 # (what was asked, the world it got)
        self.plan = plan
        self.publish_reason = publish_reason
        self.build_reason = build_reason
        self.word_typed = word
        self.fresh = fresh              # what recapture() hands back
        self.banners = []
        self.refusals = []
        self.scopes = []
        self.builder_asked = []
        self.publisher_asked = []
        self.builders = []
        self.publishers = []
        self.last_world = None

    # -- the bodies --------------------------------------------------------
    def _body(self, what, world):
        self.calls.append((what, world))
        self.last_world = world
        return M.did(what + " ran")

    def progress(self, world):
        return self._body("progress", world)

    def import_footage(self, world):
        return self._body("import", world)

    def generate_meta(self, world):
        return self._body("meta", world)

    def build_preview(self, world):
        return self._body("preview", world)

    def render(self, world):
        return self._body("render", world)

    # -- paint, lent to the items ------------------------------------------
    def yellow(self, text):
        """The paint items 5 and 7 lend their Handover, for the plugin's name.

        Identity rather than an escape sequence: what a test asserts about a
        help screen is which words are there and whose they are, and an ANSI
        code around the name would only be something every such assertion has
        to spell out.
        """
        return text

    # -- the collaborators the constructor installs ------------------------
    def builder(self, strategy):
        self.builder_asked.append(strategy)
        made = FakeBuilder(strategy, self._build_reason(strategy))
        self.builders.append(made)
        return made

    def _build_reason(self, strategy):
        if strategy is UPLOADER:
            return self.build_reason
        return None

    def publisher(self, strategy):
        self.publisher_asked.append(strategy)
        made = FakePublisher(self._publish_reason(strategy))
        self.publishers.append(made)
        return made

    def _publish_reason(self, strategy):
        if strategy is UPLOADER:
            return self.publish_reason
        return "not part of this edition"

    # -- the destructive plans ---------------------------------------------
    def exclude_plan(self, world):
        return self._plan("exclude_plan", world)

    def clean_workspace_plan(self, world):
        return self._plan("clean_plan", world)

    def erase_card_plan(self, world):
        return self._plan("erase_plan", world)

    def _plan(self, what, world):
        self.calls.append((what, world))
        self.last_world = world
        return self.plan

    # -- what Destructive needs between the plan and the act ---------------
    def show(self, banner):
        self.banners.append(banner)

    def ask_word(self, word):
        self.calls.append(("ask_word", word))
        return self.word_typed

    def recapture(self, scope):
        self.scopes.append(scope)
        if self.fresh is None:
            return self.last_world
        return self.fresh

    def refuse(self, name, reason):
        self.refusals.append(reason)
        return M.stopped("refused after re-check: %s" % reason)

    # -- what a test asks it -----------------------------------------------
    def asked(self):
        return [what for what, _ in self.calls]

    def times(self, what):
        return self.asked().count(what)


def a_plan(guard=M.nothing_to_recheck, act=None, banner=("would erase two clips",)):
    """A plan with both halves filled in, which is the only kind that exists."""
    return M.Plan(guard=guard, act=act or Act(), banner=banner)


def menu_for(strategy=UPLOADER, work=None):
    """The ten items as the pipeline builds them, sharing one `work`."""
    return M.build_menu(strategy, work or FakeWork())


def item_for(number, strategy=UPLOADER, work=None):
    return menu_for(strategy, work)[number]


# ---------------------------------------------------------------------------
# Worlds. Each helper writes down one situation; a test names the situation
# rather than assembling it, so what a test is about is its first line.
# ---------------------------------------------------------------------------

IMPORTS = (Path("/ws/import/20260101"),)
TRIP = W.TripMeta(id="t1", start="20260101120000", end="20260101130000")
MP4 = W.Render(name="trip_t1.mp4", size=1000)


def world(**kw):
    return W.World(**kw)


def imported(**kw):
    """An import in the workspace and its sidecars written."""
    base = dict(imports=IMPORTS, metas=(TRIP,), has_track=True,
                selected_import=IMPORTS[0])
    base.update(kw)
    return world(**base)


def a_card(**kw):
    return W.Card(path=Path("/card"), **kw)


def full_card(**kw):
    """A card with two clips on it, imported, and accounted for elsewhere.

    So: nothing left to FETCH. That is what item 9 wants to see and the
    opposite of what item 1 does — a card with something to import passes
    new_stamps, which is the set accounted for by nothing.
    """
    base = dict(dcim=True, present=True,
                stamps=frozenset({"20260101120000", "20260101121000"}))
    base.update(kw)
    return a_card(**base)


def a_target(complete=M.Evidence.NA, configured=True, name="target"):
    """What the configured plugin said about this world, written down.

    One frozen answer, because that is how the world carries it: the plugin is
    asked once at capture and every guard reads the same reading, which is what
    lets the destructive re-check compare two instants rather than two moods.

    NA is the default — the question does not arise for this destination at all
    — so a test that says nothing about it gets a dropped gate rather than an
    accidental UNKNOWN that would refuse for a reason the test never wrote.
    """
    return W.TargetFacts(configured=configured, name=name,
                         origin="%s (a test)" % name, complete=complete)


def evaluate(item, w):
    return item.evaluate(w)


def ruling(item, w):
    return item.evaluate(w).ruling


# ---------------------------------------------------------------------------
# What each item IS
# ---------------------------------------------------------------------------

# The owner's table, transcribed: number -> (name, start, end, destr). This is
# the declaration half of his spec and it is asserted verbatim, because a flag
# that quietly flips is a step that quietly stops ending the cycle or stops
# asking for a word.
OWNERS_TABLE = {
    PROGRESS:   ("Progress",        False, False, False),
    IMPORT:     ("Import SIM",      True,  False, False),
    META:       ("Generate Meta",   False, False, False),
    PREVIEW:    ("Build Preview",   False, False, False),
    EXCLUDE:    ("Exclude Trip",    False, False, True),
    RENDER:     ("Render Trips",   False, False, False),
    BUILD:      ("Build Website",   False, False, False),
    UPLOAD:     ("Upload Website",  False, False, False),
    CLEAN_WS:   ("Clean Workspace", False, True,  True),
    ERASE_CARD: ("Delete SIM Data", False, True,  True),
}


class TestWhatEachItemDeclares(unittest.TestCase):
    """Every item names itself and its flags exactly as the owner's table."""

    def setUp(self):
        self.menu = menu_for()

    def test_every_item_carries_the_name_the_table_gives_it(self):
        """The number and the name mean the same thing everywhere: the table,
        the docs and the menu are one declaration, not three that can drift."""
        for number, (name, _s, _e, _d) in OWNERS_TABLE.items():
            with self.subTest(item=number):
                self.assertEqual(self.menu[number].name(), name)

    def test_the_ten_numbers_are_0_through_9(self):
        """Ten items, numbered 0-9 with no gaps. A missing number is an item
        that failed to register at import and would be silently absent."""
        self.assertEqual(sorted(self.menu), list(range(10)))

    def test_start_end_and_destr_are_the_flags_the_table_declares(self):
        """Import is the only entry point; Clean Workspace and Delete SIM Data
        are the only two that end a cycle; Exclude Trip, Clean Workspace and
        Delete SIM Data are the three that erase."""
        for number, (_n, start, end, destr) in OWNERS_TABLE.items():
            with self.subTest(item=number):
                got = (self.menu[number].start(), self.menu[number].end(),
                       self.menu[number].destr())
                self.assertEqual(got, (start, end, destr))

    def test_every_item_describes_itself_in_its_own_words(self):
        """The description is what a first-time reader is told the item does,
        so it is a sentence, and no two items may claim the same one."""
        said = list(map(self._a_sentence, sorted(self.menu)))
        self.assertEqual(len(set(said)), len(said))

    def _a_sentence(self, number):
        text = self.menu[number].description()
        self.assertTrue(text.endswith("."), text)
        return text

    def test_only_the_destructive_items_ask_for_a_word(self):
        """A typed word is what stands between an operator and an irreversible
        act, so having one and being destructive are the same statement."""
        for number in sorted(self.menu):
            with self.subTest(item=number):
                item = self.menu[number]
                self.assertEqual(item.destr(), bool(item.word()))

    def test_each_destructive_item_asks_for_its_own_verb(self):
        """RESTATED THREE TIMES, and this is the shape that holds. It was DROP,
        DELETE, ERASE — three arbitrary words. Then all three converged on
        DELETE, because an entry called Delete SIM Data asking for ERASE is a
        name and a password disagreeing where it matters most.

        The same argument then applies to item 4, in the other direction. It is
        called Exclude Trip and it records a DECISION: the delta import stops
        offering those clips, and a rebuild downstream knows the trip was
        dropped rather than merely cleaned up. Items 8 and 9 delete and record
        nothing about it. Asking for DELETE there taught the wrong idea of
        what was about to happen.

        The distinctness that earns its place is item 9's way PAST its own
        guard, which asks a word its own prompt never does.
        """
        words = {n: self.menu[n].word() for n in (EXCLUDE, CLEAN_WS, ERASE_CARD)}
        self.assertEqual(words[EXCLUDE], "EXCLUDE")
        self.assertEqual(words[CLEAN_WS], "CLEAN")
        self.assertEqual(words[ERASE_CARD], "DELETE")
        self.assertNotEqual(self.menu[ERASE_CARD].OVERRIDE_WORD,
                            self.menu[ERASE_CARD].word())


# ---------------------------------------------------------------------------
# Where each item SITS
# ---------------------------------------------------------------------------

# The graph the tool runs on, per strategy: outbound is authored on the class,
# inbound is derived from every other item's outbound. Items 0, 1 and 9 name a
# KIND rather than a set of numbers and are asserted separately.
OUTBOUND = {
    UPLOADER: {
        # Item 7 is in every mid-cycle outbound: publishing is offered from
        # wherever you are, and whether it MAY run is the publisher's answer
        # about built material, not an ordering rule spelled as edges.
        IMPORT:   {IMPORT, META, CLEAN_WS, ERASE_CARD},
        META:     {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD, CLEAN_WS,
                   ERASE_CARD},
        PREVIEW:  {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD, CLEAN_WS,
                   ERASE_CARD},
        EXCLUDE:  {META, PREVIEW, EXCLUDE, UPLOAD, CLEAN_WS, ERASE_CARD},
        RENDER:   {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD, CLEAN_WS,
                   ERASE_CARD},
        BUILD:    {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD, CLEAN_WS,
                   ERASE_CARD},
        UPLOAD:   {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD, CLEAN_WS,
                   ERASE_CARD},
        CLEAN_WS: {IMPORT, CLEAN_WS},
    },
    LOCAL: {
        IMPORT:   {IMPORT, META, CLEAN_WS, ERASE_CARD},
        META:     {META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD},
        PREVIEW:  {META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD},
        EXCLUDE:  {META, PREVIEW, EXCLUDE, CLEAN_WS, ERASE_CARD},
        RENDER:   {META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD},
        BUILD:    {META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD},
        UPLOAD:   set(),
        CLEAN_WS: {IMPORT, CLEAN_WS},
    },
}

INBOUND = {
    UPLOADER: {
        META:     {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
        PREVIEW:  {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
        EXCLUDE:  {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
        RENDER:   {META, PREVIEW, RENDER, BUILD, UPLOAD},
        BUILD:    {META, PREVIEW, RENDER, BUILD, UPLOAD},
        UPLOAD:   {META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
        CLEAN_WS: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD,
                   CLEAN_WS},
        ERASE_CARD: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD},
    },
    LOCAL: {
        META:     {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD},
        PREVIEW:  {META, PREVIEW, EXCLUDE, RENDER, BUILD},
        EXCLUDE:  {META, PREVIEW, EXCLUDE, RENDER, BUILD},
        RENDER:   {META, PREVIEW, RENDER, BUILD},
        BUILD:    {META, PREVIEW, RENDER, BUILD},
        UPLOAD:   set(),
        CLEAN_WS: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS},
        ERASE_CARD: {IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD},
    },
}


class TestWhereEachItemSits(unittest.TestCase):
    """The edges each item reports, under both products."""

    def test_outbound_is_what_the_item_offers_next(self):
        """Read off the owner's table, with the three item-7 corrections he was
        told about: 5 offers 7 (publishing was otherwise unreachable), 6 does
        not (uploading from Render skips the build it uploads), and 7 offers 6
        back (fix a caption, rebuild)."""
        self._both_products(M.MenuItem.outbound, OUTBOUND)

    def test_inbound_is_derived_from_every_other_items_outbound(self):
        """One relation, one place it is written. Inbound is not a second
        opinion an item holds about who may precede it — it is computed, so an
        edge cannot exist on one side only."""
        self._both_products(M.MenuItem.inbound, INBOUND)

    def _both_products(self, side, expected_per_strategy):
        for strategy, expected in expected_per_strategy.items():
            self._edges(menu_for(strategy), strategy, side, expected)

    def _edges(self, built, strategy, side, expected):
        for number, numbers in expected.items():
            with self.subTest(strategy=strategy.value, item=number):
                self.assertEqual(side(built[number]).edges(), frozenset(numbers))

    def test_progress_neighbours_everything_and_is_never_a_position(self):
        """A view is an observation of a transition, not one. Looking must not
        move the pipeline, so it settles wherever the pipeline already was."""
        item = item_for(PROGRESS)
        universe = frozenset(range(10))
        self.assertEqual(item.outbound().offers(universe), universe)
        self.assertIsNone(item.outbound().edges())
        self.assertEqual(item.settles_at(RENDER), RENDER)

    def test_import_declares_no_inbound_because_it_is_where_footage_comes_in(self):
        """Clean Workspace offers Import, so Import does have an arriving edge
        — and still declares none, because being entered is not something an
        entry point consents to. It is reachable with nothing done at all."""
        item = item_for(IMPORT)
        self.assertIsInstance(item.inbound(), M.StartNode)
        self.assertIsNone(item.inbound().edges())
        self.assertTrue(item.start())

    def test_erasing_the_card_hands_the_position_back(self):
        """Freeing the card does not interrupt the cycle, so its successors
        are its callers: completing steps back to wherever it was selected
        from, from which Clean Workspace is still offered."""
        item = item_for(ERASE_CARD)
        self.assertIsInstance(item.outbound(), M.StepBack)
        self.assertEqual(item.settles_at(RENDER), RENDER)
        self.assertEqual(item.outbound().offers(frozenset(range(10))),
                         frozenset())

    def test_cleaning_the_workspace_offers_only_a_new_cycle(self):
        """Once the workspace is gone only an import remains — which is what
        makes "clean up, then erase the card" impossible to express: the card's
        evidence is partly that its clips are in the workspace.

        RESTATED: the outbound is {1,8}, not {1}. The 8 is itself, and it does
        not weaken the rule this test is for — 8 leading to 8 still reaches
        nothing but 1, and the assertion below says so directly rather than by
        the proxy of an exact set. A sink can hold several dated imports and
        the erase narrows to one, so cleaning the next is an ordinary move;
        and declining the import that 8 offers used to leave the position on 8
        with 8 off the menu.
        """
        item = item_for(CLEAN_WS)
        self.assertEqual(item.outbound().edges(), frozenset({IMPORT, CLEAN_WS}))
        self.assertNotIn(ERASE_CARD, item.outbound().edges())
        self.assertEqual(item.settles_at(RENDER), CLEAN_WS)

    def test_publishing_has_no_edges_at_all_under_the_local_product(self):
        """Item 7 exists on every installation so the numbers mean the same
        thing everywhere, and under the local product nothing reaches it and it
        offers nothing."""
        item = item_for(UPLOAD, LOCAL)
        self.assertEqual(item.outbound().edges(), frozenset())
        self.assertEqual(item.inbound().edges(), frozenset())


# ---------------------------------------------------------------------------
# The strategy is a constructor argument, not a branch in a body
# ---------------------------------------------------------------------------

class TestTheStrategyIsSettledWhenTheMenuIsBuilt(unittest.TestCase):
    """The website branch is resolved once, at construction."""

    def test_the_builder_and_the_publisher_are_chosen_at_construction(self):
        """Items 6 and 7 differ between the two products. They ask for their
        collaborator in the constructor and are handed one; the choosing
        happens once per session, not once per keypress."""
        work = FakeWork()
        menu_for(UPLOADER, work)
        self.assertEqual(work.builder_asked, [UPLOADER])
        self.assertEqual(work.publisher_asked, [UPLOADER])

    def test_executing_never_asks_which_product_this_is(self):
        """Running item 5 forty times must not re-decide which builder it
        uses: the constructor settled it, and asking again is the `if` this
        design removed coming back through the side door."""
        work = FakeWork()
        item = menu_for(UPLOADER, work)[BUILD]
        item.execute(imported(renders=(MP4,)))
        item.execute(imported(renders=(MP4,)))
        self.assertEqual(work.builder_asked, [UPLOADER])
        self.assertEqual(len(work.builders[0].worlds), 2)

    def test_the_local_product_installs_a_different_builder(self):
        """Under the local product item 5 writes the one-file page and gathers
        the render tree into final_; with an uploader configured neither
        happens here, and the page is not written at all."""
        work = FakeWork()
        menu_for(LOCAL, work)
        self.assertEqual(work.builder_asked, [LOCAL])

    def test_the_menu_row_is_the_builders_own_words(self):
        """What building means genuinely differs between the two products, and
        an entry answers for its own job. How the tool is installed is the
        session's to state, once, at startup."""
        work = FakeWork()
        self.assertEqual(menu_for(UPLOADER, work)[BUILD].description(),
                         "Build under uploader.")
        self.assertEqual(menu_for(LOCAL, work)[BUILD].description(),
                         "Build under local page.")

    def test_no_item_body_reads_the_strategy(self):
        """The rule stated as a property of the source: the constructor is the
        only method allowed to mention the strategy. A conditional the
        constructor already settled must not reappear as a branch in a body,
        which is also what keeps the bodies simple enough to read."""
        for cls in items.ALL_ITEMS:
            self._bodies_of(cls)

    def _bodies_of(self, cls):
        for name, fn in _own_methods(cls):
            with self.subTest(item=cls.number, method=name):
                self.assertNotIn("strategy", inspect.getsource(fn))


def _own_methods(cls):
    """The methods this class writes itself, minus the constructor."""
    defined = filter(lambda kv: inspect.isfunction(kv[1]), vars(cls).items())
    return filter(lambda kv: kv[0] != "__init__", defined)


# ---------------------------------------------------------------------------
# 0 — Progress
# ---------------------------------------------------------------------------

class TestProgress(unittest.TestCase):
    """The read-only view: it never refuses and never moves anything."""

    def test_an_empty_workspace_is_a_legitimate_thing_to_report(self):
        """Nothing to show is itself the answer. A view that can be blocked is
        a view you cannot use to find out why you are blocked."""
        self.assertIs(ruling(item_for(PROGRESS), world()), M.Ruling.GO)

    def test_it_reports_whatever_state_the_world_is_in(self):
        """Every world, not merely the interesting ones: mid-cycle, finished,
        empty."""
        item = item_for(PROGRESS)
        for w in (world(), imported(), imported(renders=(MP4,))):
            with self.subTest(world=w):
                self.assertIs(ruling(item, w), M.Ruling.GO)


# ---------------------------------------------------------------------------
# 1 — Import SIM
# ---------------------------------------------------------------------------

class TestImportSim(unittest.TestCase):
    """Copy the source's DCIM tree in. The one entry point."""

    def test_a_source_with_something_to_copy_is_all_it_needs(self):
        """No ordering question is asked at all: importing is where a cycle
        begins, so what is asked is whether there is something to copy.

        RESTATED: "a source with footage" used to be enough, and the delta
        decided how much once the step was running. It is decided at capture
        now — new_stamps is what an import would take — so a card whose every
        clip is accounted for elsewhere leaves item 1 with nothing to do, and
        it says so as a greyed name rather than as the step's only output.
        """
        w = world(card=full_card(new_stamps=frozenset({"20260101120000"})))
        self.assertIs(ruling(item_for(IMPORT), w), M.Ruling.GO)

    def test_a_card_with_nothing_left_to_fetch_is_satisfied(self):
        w = world(card=full_card(), imports=(Path("/w/import"),))
        self.assertIs(ruling(item_for(IMPORT), w), M.Ruling.SATISFIED)

    def test_an_unfinished_session_refuses_a_second_card(self):
        """Importing on top mixes two cards into one grouping with no record of
        which clip came from which. The refusal names the way out."""
        w = world(card=full_card(), workspace_settled=False,
                  workspace_note="renders not published")
        verdict = evaluate(item_for(IMPORT), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("unfinished session", verdict.reason)

    def test_no_source_and_nothing_imported_is_a_refusal(self):
        """There is nothing to do and no evidence that anything was done."""
        verdict = evaluate(item_for(IMPORT), world())
        self.assertTrue(verdict.blocked)
        self.assertIn("no source", verdict.reason)

    def test_no_source_but_footage_already_here_is_settled_not_refused(self):
        """The card was pulled out after the copy. The postcondition holds —
        the footage is in the workspace — so the item completes and the
        pipeline may move on, rather than refusing a step already taken."""
        w = world(imports=IMPORTS)
        verdict = evaluate(item_for(IMPORT), w)
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)

    def test_the_body_never_runs_when_the_session_is_unfinished(self):
        """A blocked verdict is not advice. Nothing behind it may run."""
        work = FakeWork()
        item = menu_for(UPLOADER, work)[IMPORT]
        item.execute(world(card=full_card(), workspace_settled=False))
        self.assertEqual(work.times("import"), 0)


# ---------------------------------------------------------------------------
# 2 — Generate Meta
# ---------------------------------------------------------------------------

class TestGenerateMeta(unittest.TestCase):
    """Write the sidecars. Everything downstream reads them."""

    def test_an_import_with_a_track_is_all_it_needs(self):
        w = world(imports=IMPORTS, has_track=True)
        self.assertIs(ruling(item_for(META), w), M.Ruling.GO)

    def test_an_empty_workspace_is_refused_as_evidence_not_as_order(self):
        """Item 2 is reachable from itself and from Exclude Trip, so an emptied
        workspace is genuinely reachable here. The answer is that there is
        nothing to build sidecars from, not that a step was skipped."""
        verdict = evaluate(item_for(META), world(has_track=True))
        self.assertTrue(verdict.blocked)
        self.assertIn("no import", verdict.reason)

    def test_an_import_without_a_gps_track_is_refused(self):
        """Sidecars are built from the track. Without one the pass produces
        nothing however many times it is run, so it is refused rather than run
        to no effect."""
        verdict = evaluate(item_for(META), world(imports=IMPORTS, has_track=False))
        self.assertTrue(verdict.blocked)
        self.assertIn("GPS track", verdict.reason)


# ---------------------------------------------------------------------------
# 3 — Build Preview
# ---------------------------------------------------------------------------

class TestBuildPreview(unittest.TestCase):
    """Stills and a contact sheet, from the sidecars."""

    def test_sidecars_on_disk_are_what_it_needs(self):
        self.assertIs(ruling(item_for(PREVIEW), imported()), M.Ruling.GO)

    def test_an_import_whose_sidecars_are_gone_is_refused(self):
        """This looks like the ordering check it replaced and is not: nothing
        stops the operator deleting a sidecar in Finder between the menu being
        drawn and the key being pressed, and the item is handed a world
        captured at the keypress."""
        w = world(imports=IMPORTS, metas=(), has_track=True)
        verdict = evaluate(item_for(PREVIEW), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("sidecars", verdict.reason)

    def test_an_empty_workspace_is_refused(self):
        verdict = evaluate(item_for(PREVIEW), world())
        self.assertTrue(verdict.blocked)
        self.assertIn("no import", verdict.reason)


# ---------------------------------------------------------------------------
# 4 — Exclude Trip. The owner's worked example, and the sharp completion case.
# ---------------------------------------------------------------------------

class TestExcludeTrip(unittest.TestCase):
    """Delete a trip's clips so nothing downstream ever sees them."""

    def test_an_import_is_all_it_needs(self):
        self.assertIs(ruling(item_for(EXCLUDE), imported()), M.Ruling.GO)

    def test_an_empty_workspace_has_nothing_to_exclude(self):
        verdict = evaluate(item_for(EXCLUDE), world())
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing to exclude", verdict.reason)

    def test_it_lists_the_bucket_before_it_warns(self):
        """The only-copy warning is the point of the prompt, and it cannot be
        written without knowing what is published — so this item is dispatched
        against the full world, not the cheap one the menu is drawn with."""
        self.assertIs(item_for(EXCLUDE).SCOPE, M.Scope.FULL)

    def test_completed_when_a_trip_was_really_removed(self):
        """The owner's own sentence: Exclude Trip is complete IFF a trip was
        removed."""
        act = Act("dropped trip t1")
        work = FakeWork(plan=a_plan(act=act), word="EXCLUDE")
        item = menu_for(UPLOADER, work)[EXCLUDE]
        outcome = item.execute(imported())
        self.assertTrue(outcome.completed)
        self.assertTrue(item.completed())
        self.assertTrue(act.ran)

    def test_not_completed_when_the_operator_aborts_at_the_prompt(self):
        """Anything but the word cancels, and a cancel is not a step. The
        pipeline stays where it was, which is what "steps back by one" means
        for a move that never took effect."""
        act = Act()
        work = FakeWork(plan=a_plan(act=act), word="drop please")
        item = menu_for(UPLOADER, work)[EXCLUDE]
        outcome = item.execute(imported())
        self.assertFalse(outcome.completed)
        self.assertFalse(act.ran)

    def test_not_completed_when_nothing_was_selected(self):
        """A blank or unlisted index leaves the plan with no target, so no word
        is asked for and neither half of the plan is ever called."""
        work = FakeWork(plan=M.Plan.nothing_to_do("no trip selected"))
        item = menu_for(UPLOADER, work)[EXCLUDE]
        outcome = item.execute(imported())
        self.assertFalse(outcome.completed)
        self.assertEqual(work.times("ask_word"), 0)
        self.assertEqual(outcome.note, "no trip selected")

    def test_an_interruption_is_the_items_own_answer(self):
        """Ctrl-C leaves execute() part-way through, so the outcome would
        otherwise stay unset and completed() would raise where the runner asks
        it. An abort is simply not completing."""
        item = item_for(EXCLUDE)
        item.aborted("interrupted")
        self.assertFalse(item.completed())

    def test_completed_raises_before_the_item_has_ever_run(self):
        """Never a default answer. A stale or invented `False` here is the
        difference between a report saying a trip was dropped and one saying it
        was not."""
        with self.assertRaises(M.NotRun):
            item_for(EXCLUDE).completed()


# ---------------------------------------------------------------------------
# 6 — Render Trips
# ---------------------------------------------------------------------------

class TestRenderVideos(unittest.TestCase):
    """Encode the chosen trips. The slow step."""

    def test_an_import_with_sidecars_is_what_it_needs(self):
        self.assertIs(ruling(item_for(RENDER), imported()), M.Ruling.GO)

    def test_a_mounted_card_is_not_a_workspace(self):
        """Rendering reads the COPIED import, never the source itself. That is
        evidence about two different directories, not a claim about which step
        ran first — a card in the slot with nothing copied off it yet is a
        world in which there is genuinely nothing to render."""
        verdict = evaluate(item_for(RENDER), world(card=full_card(), metas=(TRIP,)))
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing to render", verdict.reason)

    def test_an_import_without_sidecars_is_refused(self):
        w = world(imports=IMPORTS, metas=())
        self.assertTrue(evaluate(item_for(RENDER), w).blocked)


# ---------------------------------------------------------------------------
# 5 — Build Website
# ---------------------------------------------------------------------------

class TestBuildWebsite(unittest.TestCase):
    """Build the page from the renders."""

    def test_renders_on_disk_are_what_it_builds_from(self):
        w = imported(renders=(MP4,))
        self.assertIs(ruling(item_for(BUILD), w), M.Ruling.GO)

    def test_a_gathered_folder_counts_because_the_page_must_be_rebuildable(self):
        """Once this item has gathered, the loose renders are gone — it moved
        them. The page must still be rebuildable from what is in final_, so a
        gathered folder is evidence just as loose renders are."""
        w = imported(renders=(), final_folders=(Path("/ws/final_2026-01-01"),))
        self.assertIs(ruling(item_for(BUILD), w), M.Ruling.GO)

    def test_a_described_trip_is_enough_to_build_from(self):
        """Sidecars, no render anywhere. Everything a page says about a drive —
        its route, its distance, its places, its map — comes from what item 2
        writes; only the player needs the mp4. Demanding a render here made the
        site wait hours on an encode it does not read."""
        self.assertIs(ruling(item_for(BUILD), imported(renders=())), M.Ruling.GO)

    def test_nothing_described_and_nothing_rendered_is_refused(self):
        """Refused on the evidence, not on the order: the wording stays true
        whether the sidecars were never made or were deleted in Finder."""
        verdict = evaluate(item_for(BUILD), imported(metas=(), renders=()))
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing described or rendered", verdict.reason)

    def test_it_hands_the_work_to_the_builder_it_was_constructed_with(self):
        """The whole body is passed in, not chosen. That is the strategy
        branch living in the constructor where it belongs — and it is the fix
        for the local page being written under the publishing edition, which
        happened because only the MOVER used to be the branch."""
        work = FakeWork()
        item = menu_for(LOCAL, work)[BUILD]
        item.execute(imported(renders=(MP4,)))
        self.assertTrue(work.builders[0].ran)

    def test_the_targets_refusal_comes_after_the_exporters_own_question(self):
        """An empty tree is refused by the exporter itself, before the target
        is asked anything. A target that answers yes to everything still
        cannot get a page built out of nothing."""
        work = FakeWork(build_reason="the target is not reachable")
        item = menu_for(UPLOADER, work)[BUILD]
        self.assertIn("nothing described or rendered",
                      evaluate(item, imported(metas=(), renders=())).reason)
        self.assertIn("not reachable",
                      evaluate(item, imported(renders=(MP4,))).reason)

    def test_a_satisfied_builder_is_still_rebuilt_when_menu_five_is_pressed(self):
        """Build Website is a command: unchanged inputs must not turn it into
        a disabled menu row.  A plugin may use SATISFIED to report that its
        current manifest matches, but the operator explicitly asked for a
        fresh build and the builder must run again."""
        work = FakeWork()
        item = menu_for(UPLOADER, work)[BUILD]
        item._builder.evaluate = lambda world: M.satisfied("already current")
        world = imported(renders=(MP4,))

        self.assertIs(ruling(item, world), M.Ruling.GO)
        item.execute(world)
        self.assertTrue(work.builders[0].ran)


# ---------------------------------------------------------------------------
# 7 — Upload Website
# ---------------------------------------------------------------------------

def a_published_world(**kw):
    """Renders on disk, and the destination has every trip of this import."""
    base = dict(renders=(MP4,), renders_here=(MP4,),
                target=a_target(complete=M.Evidence.YES))
    base.update(kw)
    return imported(**base)


class TestUploadWebsite(unittest.TestCase):
    """Getting the built site online. One job, two transports."""

    def test_a_destination_that_does_not_have_them_is_work_to_do(self):
        """RESTATED: this used to read a set of owed render names, and owes()
        is gone with the per-name map it filled. The question it answered is
        the same one — is there anything left for the upload to do — and it is
        now the one all-or-nothing answer. A destination that could not be
        asked reads UNKNOWN, which is not YES, so the offer stands and the
        upload is what discovers it is down."""
        for answer in (M.Evidence.NO, M.Evidence.UNKNOWN, M.Evidence.NA):
            with self.subTest(answer=answer.value):
                w = imported(renders=(MP4,), renders_here=(MP4,),
                             target=a_target(complete=answer))
                self.assertIs(ruling(item_for(UPLOAD), w), M.Ruling.GO)

    def test_everything_already_online_is_settled_not_refused(self):
        """The postcondition holds: the destination has every trip. The item
        completes without touching the network."""
        verdict = evaluate(item_for(UPLOAD), a_published_world())
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)

    def test_nothing_rendered_is_refused_rather_than_vacuously_settled(self):
        """"The destination has everything" is true of no renders at all, and it
        is the wrong sentence to put in front of someone who has published
        nothing. Evidence, not order: it survives the renders being deleted."""
        verdict = evaluate(item_for(UPLOAD), world(imports=IMPORTS))
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing on disk to publish", verdict.reason)

    def test_a_described_trip_may_be_published_before_it_is_encoded(self):
        """The pages of a described trip go up while its video is still hours
        away — that is the whole point of the floor moving off renders."""
        w = imported(renders=(), trip_ids=("trip_2026-07-12_17-46_01",))
        self.assertFalse(evaluate(item_for(UPLOAD), w).blocked)

    def test_the_floor_is_this_import_not_the_whole_tree(self):
        """metas is the whole output tree; trip_ids is THIS import. A fresh
        import with no sidecars yet, beside an old import whose sidecars are
        still on disk, would pass a metas test and then ask the destination
        about no trips at all — and is_complete answers YES to an empty list."""
        w = imported(renders=(), trip_ids=())
        self.assertTrue(evaluate(item_for(UPLOAD), w).blocked)

    def test_no_sidecars_anywhere_is_refused_because_the_index_would_be_empty(self):
        """Publishing is putting the trips' metadata online. With no sidecar in
        the tree the deploy pushes an index describing no drives — a live site
        that comes up empty rather than one that refuses."""
        w = world(imports=IMPORTS, metas=(), renders=(MP4,), renders_here=(MP4,))
        verdict = evaluate(item_for(UPLOAD), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("empty index", verdict.reason)

    def test_the_collaborators_refusal_comes_first(self):
        """The target's own reason, in the operator's terms, before anything
        the world could settle. What it says is the implementation's to
        choose — the exporter has no vocabulary for another machine."""
        work = FakeWork(publish_reason="the destination is not writable")
        item = menu_for(UPLOADER, work)[UPLOAD]
        verdict = evaluate(item, a_published_world())
        self.assertTrue(verdict.blocked)
        self.assertIn("not writable", verdict.reason)

    def test_the_local_product_refuses_through_its_installed_collaborator(self):
        """Item 7 is unreachable under the local product for two independent
        reasons — nothing offers it, and it refuses — and neither one is an
        `if` inside this item. It asks whichever publisher it was given."""
        item = item_for(UPLOAD, LOCAL)
        verdict = evaluate(item, a_published_world())
        self.assertTrue(verdict.blocked)
        self.assertIn("not part of this edition", verdict.reason)

    def test_it_never_publishes_behind_a_refusal(self):
        work = FakeWork(publish_reason="the destination is not writable")
        item = menu_for(UPLOADER, work)[UPLOAD]
        item.execute(a_published_world())
        self.assertFalse(work.publishers[0].ran)


# ---------------------------------------------------------------------------
# 8 — Clean Workspace, and the rule that decides whether the footage may go
# ---------------------------------------------------------------------------

class TestCleanWorkspaceIsOffered(unittest.TestCase):
    """The cheap half, asked on every menu draw."""

    def test_an_import_with_sidecars_may_be_considered(self):
        """Considered, not approved: the three heavy gates are the plan's
        guard, asked at dispatch and again after the word is typed."""
        self.assertIs(ruling(item_for(CLEAN_WS), imported()), M.Ruling.GO)

    def test_nothing_imported_means_nothing_to_clean(self):
        verdict = evaluate(item_for(CLEAN_WS), world())
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing to clean", verdict.reason)

    def test_the_heavy_gates_are_not_asked_on_every_draw(self):
        """A menu that is not instant stops being recomputed and starts being
        remembered, so is-complete.py is shelled out to at dispatch — which is
        what declaring the full scope means."""
        self.assertIs(item_for(CLEAN_WS).SCOPE, M.Scope.FULL)


def workspace_gate(world):
    """The sweep's gate in the shape menu.Plan takes: world -> Verdict.

    A Gates is built per world rather than per question, so the callable can
    be handed the world captured after the word is typed and answer about that
    one.
    """
    return guards.Gates(world).workspace_is_expendable()


def clean_with(guard, fresh, word="CLEAN"):
    """Drive item 8 all the way to its re-check, with `fresh` as the world the
    guard is re-asked against after the word is typed."""
    act = Act("workspace erased")
    work = FakeWork(plan=a_plan(guard=guard, act=act), word=word, fresh=fresh)
    item = menu_for(UPLOADER, work)[CLEAN_WS]
    outcome = item.execute(imported())
    return work, act, outcome


def rendered_world(**kw):
    """Two renderable trips, both encoded. The local half of the evidence."""
    second = W.Render(name="trip_t2.mp4", size=2000)
    base = dict(renders=(MP4, second), renders_here=(MP4, second),
                expected_trips=2)
    base.update(kw)
    return imported(**base)


class TestTheWorkspaceIsExpendableRule(unittest.TestCase):
    """What has to be true before the imported footage may be erased.

    One sentence, applied through the item that acts on it: THE FLOORS THAT
    NEVER LEAVE THIS MACHINE FIRST, AND THEN THE DESTINATION'S ANSWER DECIDES.

    RESTATED for the narrowed interface. Two of the three gates used to be the
    plugin's answers — is the file there, is it being served — and this class
    enumerated how they combined. They are one question now, asked about the
    import's TRIPS rather than about the renders that happen to exist, so what
    is left to pin is which reading permits what. The floors are unchanged and
    so are the readings; only the number of ways to spell them is smaller.
    """

    def test_a_local_shortfall_refuses_even_when_the_destination_says_yes(self):
        """The floor, and why it stays.

        Nine trips expected and only the rendered ones present. The trip ids
        are read off the sidecars, so a trip whose sidecar was never written is
        in nobody's question — and its footage is only in the import this step
        erases.
        """
        fresh = rendered_world(expected_trips=9,
                               target=a_target(complete=M.Evidence.YES))
        _work, act, outcome = clean_with(workspace_gate, fresh)
        self.assertFalse(act.ran)
        self.assertFalse(outcome.completed)

    def test_a_destination_that_says_no_refuses_however_good_the_local_evidence(self):
        fresh = rendered_world(target=a_target(complete=M.Evidence.NO))
        work, act, outcome = clean_with(workspace_gate, fresh)
        self.assertFalse(act.ran)
        self.assertFalse(outcome.completed)
        self.assertTrue(work.refusals)

    def test_a_grouping_that_could_not_be_read_refuses(self):
        """"Could not find out how many trips there should be" is not "the
        count is fine"."""
        fresh = rendered_world(expected_trips=None,
                               target=a_target(complete=M.Evidence.YES))
        _work, act, _outcome = clean_with(workspace_gate, fresh)
        self.assertFalse(act.ran)

    def test_a_destination_that_could_not_be_asked_fails_closed(self):
        """The rule the whole interface is shaped around. An implementation
        that cannot reach its destination says UNKNOWN, and unreachable is not
        permission — turning "could not find out" into "yes" is what this
        forbids, and one negation is all it would take."""
        fresh = rendered_world(target=a_target(complete=M.Evidence.UNKNOWN))
        _work, act, _outcome = clean_with(workspace_gate, fresh)
        self.assertFalse(act.ran)

    def test_not_applicable_drops_the_gate_and_unknown_fails_it(self):
        """The two answers that are easiest to confuse, side by side. NA is a
        destination the question does not arise for — an archive disk stores
        and does not publish — and the erase then rests on the local render
        count. UNKNOWN is a question that could not be answered, and it
        refuses."""
        for answer, expected in ((M.Evidence.NA, True), (M.Evidence.UNKNOWN, False)):
            with self.subTest(answer=answer.value):
                fresh = rendered_world(target=a_target(complete=answer))
                _work, act, _outcome = clean_with(workspace_gate,
                                                  fresh)
                self.assertIs(act.ran, expected)

    def test_everything_proven_lets_the_workspace_go(self):
        """Every trip encoded, and the destination has all of them. This is the
        case the whole rule exists to permit."""
        fresh = rendered_world(target=a_target(complete=M.Evidence.YES))
        _work, act, outcome = clean_with(workspace_gate, fresh)
        self.assertTrue(act.ran)
        self.assertTrue(outcome.completed)

    def test_what_could_not_be_checked_is_stated_rather_than_passed_over(self):
        """With nothing configured to publish to there is no proof of
        publication at all, so the renders are the only copy of that footage in
        the world. A guard that could not run says so instead of staying quiet.

        One sentence where there used to be two. Not a weakened check — the
        two lines were one operator's two config keys for one condition, and
        with one implementation it is genuinely one condition."""
        lines = guards.Gates(rendered_world()).unproven_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("upload_plugin", lines[0])

    def test_a_configured_plugin_that_answers_does_not_get_that_sentence(self):
        world = rendered_world(target=a_target(complete=M.Evidence.YES))
        lines = guards.Gates(world).unproven_lines()
        self.assertEqual(lines, ())


class TestTheRecheckHappensAfterTheWordIsTyped(unittest.TestCase):
    """The world a destructive item acts on is captured after the prompt."""

    def test_the_guard_is_re_asked_against_a_freshly_captured_world(self):
        """The world the menu was drawn with is a prompt old, and a card can be
        swapped or a folder deleted while the prompt is on screen. The same
        callable is asked twice, against two worlds captured at two instants."""
        fresh = rendered_world(target=a_target(complete=M.Evidence.NO))
        work, act, _outcome = clean_with(workspace_gate, fresh)
        self.assertEqual(work.scopes, [M.Scope.FULL])
        self.assertFalse(act.ran)

    def test_the_irreversible_half_only_ever_sees_the_fresh_world(self):
        """Not "the trees do not overlap so the evidence must still hold" —
        that reasoning produced the defect this replaces. The act is handed the
        re-captured world by construction, so it cannot be handed a stale one."""
        fresh = rendered_world(target=a_target(complete=M.Evidence.YES))
        _work, act, _outcome = clean_with(workspace_gate, fresh)
        self.assertEqual(act.worlds, [fresh])

    def test_the_wrong_word_stops_before_the_re_check(self):
        fresh = rendered_world(target=a_target(complete=M.Evidence.YES))
        work, act, outcome = clean_with(workspace_gate, fresh,
                                        word="clean")
        self.assertFalse(act.ran)
        self.assertFalse(outcome.completed)
        self.assertEqual(work.scopes, [])

    def test_every_destructive_item_carries_both_halves_of_the_re_check(self):
        """A plan without a guard used to be constructible, and crashed at the
        moment it mattered: after the operator had typed the word. Both halves
        are required fields now, and this is the statement of why."""
        for number in (EXCLUDE, CLEAN_WS, ERASE_CARD):
            with self.subTest(item=number):
                plan = a_plan()
                self.assertTrue(callable(plan.guard))
                self.assertTrue(callable(plan.act))
                self.assertIsInstance(item_for(number), M.Destructive)


# ---------------------------------------------------------------------------
# 9 — Delete SIM Data, and the per-clip accounting that guards it
# ---------------------------------------------------------------------------

class TestDeleteSimData(unittest.TestCase):
    """The card is the one target whose contents may have no second copy."""

    def test_no_card_in_the_slot_is_refused(self):
        verdict = evaluate(item_for(ERASE_CARD), imported())
        self.assertTrue(verdict.blocked)
        self.assertIn("no card", verdict.reason)

    def test_an_already_empty_card_is_settled_not_warned_about(self):
        """It used to answer "the ledger claims imported, but no copy is still
        on this machine" — a lost-footage warning fired at a card that is
        simply empty. A guard that cries wolf is how an operator learns to stop
        reading guards."""
        w = imported(card=a_card(dcim=True, stamps=frozenset()))
        verdict = evaluate(item_for(ERASE_CARD), w)
        self.assertIs(verdict.ruling, M.Ruling.SATISFIED)
        self.assertIn("nothing to erase", verdict.reason)

    def test_it_asks_the_target_before_it_warns(self):
        """The card's own guards are all local, so this scope buys nothing for
        them. It buys the ADVISORY: erasing the card is allowed on the strength
        of the workspace copy, and whether that copy is published yet is the
        target's answer. At LOCAL scope a configured target reads UNKNOWN, so
        the line fired on every card erase on every publishing install and said
        nothing — the same cry-wolf the empty-card case above was fixed for."""
        self.assertIs(item_for(ERASE_CARD).SCOPE, M.Scope.FULL)

    def test_a_card_whose_every_clip_is_accounted_for_may_go(self):
        w = imported(ledger_mark="20260101130000",
                     card=full_card(new_stamps=frozenset(),
                                    owed_stamps=frozenset()))
        self.assertIs(ruling(item_for(ERASE_CARD), w), M.Ruling.GO)

    def test_nothing_ever_imported_means_nothing_says_it_exists_elsewhere(self):
        """No high-water mark at all. The card may be the only copy of every
        clip on it, and there is no record that says otherwise."""
        w = imported(ledger_mark=None, card=full_card())
        verdict = evaluate(item_for(ERASE_CARD), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("nothing was ever imported", verdict.reason)

    def test_clips_newer_than_the_mark_were_never_copied(self):
        """The camera kept recording after the import. Those clips are on this
        card and nowhere else, and they are not excluded on purpose."""
        w = imported(ledger_mark="20260101120500",
                     card=full_card(new_stamps=frozenset({"20260101121000"})))
        verdict = evaluate(item_for(ERASE_CARD), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("new clips", verdict.reason)

    def test_one_accounted_clip_does_not_vouch_for_the_whole_card(self):
        """The per-clip accounting, and the reason it is a SET and not a
        boolean. A card of two clips where the first is inside a rendered
        trip's span and the second is accounted for by nothing: an hour before
        this was written, a version that approved on the first clip approved
        wiping a card whose second clip existed nowhere else."""
        w = imported(ledger_mark="20260101130000",
                     card=full_card(owed_stamps=frozenset({"20260101121000"})))
        verdict = evaluate(item_for(ERASE_CARD), w)
        self.assertTrue(verdict.blocked)
        self.assertIn("nowhere but this card", verdict.reason)

    def test_the_ledger_alone_does_not_prove_the_copy_still_exists(self):
        """The ledger records that a verified copy WAS made. It cannot notice
        that the copy was later deleted, moved to a disk that is not plugged
        in, or swept — so a mark plus an owed clip is still a refusal."""
        w = imported(ledger_mark="20260101130000", imports=(),
                     card=full_card(owed_stamps=frozenset({"20260101120000",
                                                           "20260101121000"})))
        self.assertTrue(evaluate(item_for(ERASE_CARD), w).blocked)

    def test_the_card_is_never_touched_behind_a_refusal(self):
        """No plan is even built, so no banner is printed and no word is asked
        for: a refusal is not a prompt with a discouraging message."""
        act = Act()
        work = FakeWork(plan=a_plan(act=act), word="DELETE")
        item = menu_for(UPLOADER, work)[ERASE_CARD]
        item.execute(imported(ledger_mark=None, card=full_card()))
        self.assertFalse(act.ran)
        self.assertEqual(work.times("erase_plan"), 0)


# ---------------------------------------------------------------------------
# Idempotence: every item, executed twice
# ---------------------------------------------------------------------------

# Per item: the world the first run sees, the world as it is AFTER that run,
# and what the second run must answer. The three that matter most are the
# destructive ones, and they are asserted individually below as well.
SECOND_RUN = {
    PROGRESS: (world(), world(), M.Ruling.GO),
    # The card was pulled out after the copy. The footage is in the workspace,
    # so there is nothing owed and nothing to copy again.
    IMPORT: (world(card=full_card()), world(imports=IMPORTS),
             M.Ruling.SATISFIED),
    # Rewriting the sidecars produces the same bytes; the pass is idempotent in
    # itself, so the item delegates rather than gating.
    META: (world(imports=IMPORTS, has_track=True),
           imported(), M.Ruling.GO),
    PREVIEW: (imported(), imported(stills_current=True), M.Ruling.GO),
    RENDER: (imported(), imported(renders=(MP4,)), M.Ruling.GO),
    BUILD: (imported(renders=(MP4,)),
            imported(renders=(MP4,), local_page=True), M.Ruling.GO),
    # The target says it holds and serves every render, so there is nothing
    # left for a second upload to do.
    UPLOAD: (imported(renders=(MP4,), renders_here=(MP4,)),
             a_published_world(), M.Ruling.SATISFIED),
    # The import tree is gone, which is this item's postcondition.
    CLEAN_WS: (imported(), world(), M.Ruling.BLOCKED),
    # The card holds no clips, which is this item's postcondition.
    ERASE_CARD: (imported(ledger_mark="20260101130000", card=full_card()),
                 imported(card=a_card(dcim=True, stamps=frozenset())),
                 M.Ruling.SATISFIED),
}


class TestIdempotence(unittest.TestCase):
    """Executing an item twice in a row never doubles the effect.

    The invariant the owner named: every execute must be idempotent. What that
    means differs by item and both readings are here — an item whose
    postcondition already holds answers SATISFIED and does not run at all, and
    an item whose body is itself idempotent runs again to the same effect.
    What no item may do is act a second time on a world where its work is
    already done, or report an outcome that does not match what happened.
    """

    def test_a_second_run_against_the_world_the_first_left_behind(self):
        for number, (before, after, expected) in SECOND_RUN.items():
            with self.subTest(item=number):
                self._twice(number, before, after, expected)

    def _twice(self, number, before, after, expected):
        work = FakeWork(plan=a_plan(), word=_word_for(number))
        item = menu_for(UPLOADER, work)[number]
        item.execute(before)
        self.assertIs(item.evaluate(after).ruling, expected)

    def test_the_destructive_items_do_not_reach_the_prompt_a_second_time(self):
        """The sharp form: a second Delete SIM Data on the card it has just
        emptied must not print its banner and ask for ERASE only to discover
        there is nothing behind it. Asking for a word implies there is
        something to lose."""
        act = Act()
        work = FakeWork(plan=a_plan(act=act), word="DELETE")
        item = menu_for(UPLOADER, work)[ERASE_CARD]
        item.execute(imported(ledger_mark="20260101130000", card=full_card()))
        item.execute(imported(card=a_card(dcim=True, stamps=frozenset())))
        self.assertEqual(len(act.worlds), 1)
        self.assertEqual(work.times("ask_word"), 1)

    def test_a_second_exclusion_erases_nothing_when_the_trip_is_gone(self):
        """The plan finds no target, so no word is asked for and neither half
        of it is called."""
        act = Act()
        work = FakeWork(plan=a_plan(act=act), word="EXCLUDE")
        item = menu_for(UPLOADER, work)[EXCLUDE]
        item.execute(imported())
        work.plan = M.Plan.nothing_to_do("that trip is already gone")
        second = item.execute(imported())
        self.assertEqual(len(act.worlds), 1)
        self.assertFalse(second.completed)

    def test_a_second_upload_does_not_touch_the_network(self):
        """Resuming is the normal case — an interrupted upload picks up where
        it stopped — and "nothing left to do" is the same question answered
        with an empty list."""
        work = FakeWork()
        item = menu_for(UPLOADER, work)[UPLOAD]
        item.execute(imported(renders=(MP4,), renders_here=(MP4,),
                              target=a_target(complete=M.Evidence.NO)))
        item.execute(a_published_world())
        self.assertEqual(len(work.publishers[0].worlds), 1)

    def test_cleaning_a_workspace_that_is_already_gone_erases_nothing(self):
        """The idempotence rule alone, stated so it survives an open question.

        Item 8 currently REFUSES a second run where item 9 in the same
        situation answers SATISFIED, and the design's postcondition table sides
        with item 9 — item 8's postcondition is "the import tree is gone", and
        it is. That is the owner's call and it is pinned as it stands by
        test_nothing_imported_means_nothing_to_clean, which asserts the refusal
        outright.

        What this test owns is narrower and true under either answer: the
        second run erases nothing, asks for no second word, and does not report
        that an erasure happened.
        """
        act = Act()
        work = FakeWork(plan=a_plan(act=act), word="CLEAN")
        item = menu_for(UPLOADER, work)[CLEAN_WS]
        item.execute(imported())
        second = item.execute(world())
        self.assertEqual(len(act.worlds), 1)
        self.assertEqual(work.times("ask_word"), 1)
        self.assertIn("nothing to clean up", second.note)


def _word_for(number):
    return {EXCLUDE: "EXCLUDE", CLEAN_WS: "CLEAN", ERASE_CARD: "DELETE"}.get(number, "")


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

class TestCompleted(unittest.TestCase):
    """completed() is true only when a step was actually done.

    The owner's definition is "the step was not aborted", which makes it two
    valued while evaluate() is three valued: an item whose postcondition
    already holds did not do work and did not abort either, so it completes and
    the pipeline moves on. Every way of NOT doing the work — a refusal, a
    cancelled prompt, a failed body, an interruption — reports false, and the
    position stays where it was.
    """

    def test_work_that_was_done_completes(self):
        work = FakeWork()
        item = menu_for(UPLOADER, work)[META]
        self.assertTrue(item.execute(imported()).completed)

    def test_a_refusal_does_not_complete(self):
        item = item_for(META)
        self.assertFalse(item.execute(world()).completed)

    def test_a_postcondition_that_already_holds_completes(self):
        """Not the same as having done work, and deliberately so: an item that
        refused to settle a world it agrees is already settled would deadlock
        the pipeline at the step before it."""
        item = item_for(IMPORT)
        outcome = item.execute(world(imports=IMPORTS))
        self.assertTrue(outcome.completed)
        self.assertTrue(item.completed())

    def test_a_body_that_failed_does_not_complete(self):
        """The item consulted its guard and ran; the work itself did not
        finish. Completion is about the postcondition, not about permission."""
        work = FakeWork()
        work.generate_meta = lambda w: M.stopped("the sidecar pass exited 1")
        item = menu_for(UPLOADER, work)[META]
        self.assertFalse(item.execute(imported()).completed)

    def test_a_blocked_verdict_always_says_why(self):
        """A greyed item with no reason is an item the operator cannot act on.
        Every refusal here is a sentence naming what is missing."""
        blocked_worlds = ((META, world()), (PREVIEW, world()),
                          (RENDER, world()), (BUILD, world()),
                          (UPLOAD, world()), (CLEAN_WS, world()),
                          (ERASE_CARD, imported()))
        for number, w in blocked_worlds:
            with self.subTest(item=number):
                verdict = evaluate(item_for(number), w)
                self.assertTrue(verdict.blocked)
                self.assertTrue(verdict.reason.strip())

    def test_the_answer_is_the_most_recent_run_and_nothing_older(self):
        """Reset first, written last. A stale read is the difference between a
        report saying the card was erased and one saying it was refused."""
        item = item_for(META)
        self.assertTrue(item.execute(imported()).completed)
        self.assertFalse(item.execute(world()).completed)
        self.assertFalse(item.completed())


if __name__ == "__main__":
    unittest.main()
