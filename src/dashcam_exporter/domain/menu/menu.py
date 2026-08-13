"""The menu: eleven items, the graph they sit in, and the position we are at.

Each menu item is one class. It declares what it is (name, description, start,
end, destr), where it may lead (outbound), and answers two questions about the
world it is handed: `evaluate` — would this do anything, and may it — and
`execute` — do it, once, idempotently.

Two rules shape everything here.

ORDER IS THE GRAPH'S JOB. An item is not offered unless the current position
offers it, so an item never asks "has the earlier step run yet". What an item
still asks about is EVIDENCE: what is on disk right now, which an operator can
change in Finder between the menu being drawn and the key being pressed.

OUTBOUND IS AUTHORED, INBOUND IS DERIVED. They are two views of one relation,
not two opinions. Written twice, they disagreed sixteen times in a ten-row
table authored in one sitting — a cross-check its own author cannot pass is a
second place to be wrong, not a safety net. So each item writes its outbound,
and `MenuGraph.inbound()` computes every inbound from every other item's
outbound. The hand-written inbound column survives as IN_AUTHORED and is
DIFFED against the derivation by `MenuGraph.disagreements()`, which reports
rather than reconciles: a difference is a finding for the person who wrote the
table, printed by tests/print_step_graph.py.

WHAT IS A CLASS AND WHAT IS A NAME. Every behaviour here belongs to a type:
Verdict and Outcome build and read themselves, Neighbours answers about edges,
Registry admits an item class, MenuGraph reads the eleven classes as a graph for
one strategy, Position holds where we are. The short block of functions at the
foot of the file is the module's SURFACE -- the names pipeline.py, guards.py,
items.py and the published plugin interface in uploader.py already say -- and
each is one call into the type that does the work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from dashcam_exporter.domain.model.values import (Evidence, NotRun, Outcome, Ruling, Scope,
                                     Strategy, Verdict)


# Item numbers, named once so the graph reads as sentences rather than integers.
PROGRESS, IMPORT, META, PREVIEW, EXCLUDE = 0, 1, 2, 3, 4
BUILD, RENDER, TRANSCRIBE, UPLOAD, CLEAN_WS, ERASE_CARD = 5, 6, 7, 8, 9, 10


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Neighbours. Three of the eleven items name something that is not a set of
# numbers, so the kinds are types rather than numbers on an exemption list.
# ---------------------------------------------------------------------------

class Neighbours(ABC):
    @abstractmethod
    def offers(self, universe: FrozenSet[int]) -> FrozenSet[int]:
        """What may be selected next."""

    @abstractmethod
    def edges(self) -> Optional[FrozenSet[int]]:
        """The numbers this side contributes to the graph, or None when this
        side is not an edge set at all."""

    @abstractmethod
    def settles_at(self, number: int, current: int) -> int:
        """Where the position lands after this item completes."""

    def reaches(self, number: int) -> bool:
        """Is that number one of the edges this side contributes.

        Concrete on the interface because the three kinds that answer None
        would each have to write the same `edges is not None and n in edges`
        otherwise, and the derivation of the inbound column and the sentence
        the menu prints about an unreachable entry both ask it. Two readers of
        one relation, asking through one method: None and the empty set both
        answer no, and neither reader has to know which it got.
        """
        numbers = self.edges()
        return bool(numbers) and number in numbers

    def is_view(self) -> bool:
        """A view neighbours everything and is never a position of its own.

        Answered by the kinds rather than by an isinstance at the call site, so
        adding a second view-like kind does not mean finding every place that
        named this one.
        """
        return False

    def no_edges(self) -> bool:
        """This side declares an edge set and it is empty.

        Not the same as declaring none: a view and a start node answer None
        here and are emphatically still in the graph. Only a side that named
        its numbers and named none of them is a side nothing can cross.
        """
        return self.edges() == frozenset()


@dataclass(frozen=True)
class Edges(Neighbours):
    numbers: FrozenSet[int]

    def offers(self, universe):
        return self.numbers

    def edges(self):
        return self.numbers

    def settles_at(self, number, current):
        return number

    @classmethod
    def of(cls, *numbers) -> "Edges":
        """An outbound row, written as the numbers it names.

        Was the module-level `e()`: short because an outbound row is a dense
        line and a longer word buried the numbers. As a classmethod it keeps
        the row readable and now says which type it is building.
        """
        return cls(frozenset(numbers))

    @classmethod
    def and_upload(cls, *numbers) -> Dict[Strategy, "Edges"]:
        """These neighbours, plus item 8 wherever item 8 is a real entry.

        Item 8 is offered from everywhere in the cycle, and the graph is
        deliberately not the thing that decides whether it may run. Publishing
        needs a built site, which is a FACT about the destination's material and
        is the publisher's to answer -- it says "nothing has been built to
        upload yet" and means it. Encoded as edges instead, the same fact became
        an ordering rule that hid the entry, and hiding it is how the operator
        who just rendered an mp4 is told to go and rebuild an index the encode
        did not change.

        Under the local edition item 8 exists but publishes nothing, so it stays
        out of every outbound there, exactly as before.
        """
        return {Strategy.UPLOADER: cls.of(*numbers, UPLOAD),
                Strategy.LOCAL_PAGE: cls.of(*numbers)}


class Anywhere(Neighbours):
    """A view: neighbours everything, is never a position, declares no edge.

    edges() is None rather than "every number", which is what keeps the other
    ten items from having to declare the view back — and what keeps the view
    off the disagreement report by definition instead of by a skip list.
    """

    def offers(self, universe):
        return universe

    def edges(self):
        return None

    def settles_at(self, number, current):
        return current

    def is_view(self) -> bool:
        return True

    def __repr__(self):                       # pragma: no cover - debug aid
        return "*"


class StartNode(Neighbours):
    """Being entered is not something an entry point consents to.

    Import SIM has an arriving edge — Clean Workspace offers it — and still
    declares no inbound: it is where footage comes in, reachable with nothing
    done at all. The owner's table says `-` here and it is right.
    """

    def offers(self, universe):
        return frozenset()

    def edges(self):
        return None

    def settles_at(self, number, current):
        return number

    def __repr__(self):                       # pragma: no cover - debug aid
        return "-"


class StepBack(Neighbours):
    """Completing hands the position back to whoever offered it.

    Freeing the card does not interrupt the cycle, so its successors are its
    callers and there is nothing separate for it to name.
    """

    def offers(self, universe):
        return frozenset()

    def edges(self):
        return None

    def settles_at(self, number, current):
        return current

    def __repr__(self):                       # pragma: no cover - debug aid
        return "step back by 1"


# The three shapes an outbound row takes now live on the types that answer for
# them: Strategy.both for the row that does not branch, Edges.of for the row
# that names its numbers, and Edges.and_upload for the one branch there is.


# ---------------------------------------------------------------------------
# The interface every item implements, and the register it lands in
# ---------------------------------------------------------------------------

class Registry:
    """The eleven item classes by number, and the gate they pass to get in.

    One instance, filled by __init_subclass__ as items.py imports. It exists as
    a type rather than a module dict because admitting a class and vetting it
    are one act: a class that skipped the vetting and still landed in the dict
    would be found by the first menu draw that indexed its table under the
    strategy it forgot, which is the wrong end of the session to find it.

    The numeric order is canonical: the menu is printed in item-number order,
    even when a new item is declared beside the step it follows.
    """

    def __init__(self):
        self._classes: Dict[int, type] = {}

    def declare(self, cls) -> None:
        """Import-time checks. A malformed item fails to load, not to run."""
        self._require(set(cls.OUT) == set(Strategy),
                      "%s: OUT must cover every Strategy" % cls)
        self._require(set(cls.IN_AUTHORED) == set(Strategy),
                      "%s: IN_AUTHORED must cover every Strategy" % cls)
        self._require(cls.DESTR == bool(cls.WORD),
                      "%s: DESTR and WORD must agree" % cls)
        self._classes[cls.number] = cls

    def classes(self) -> Dict[int, type]:
        """A copy: what the caller does with it is not the registry's problem."""
        return dict(sorted(self._classes.items()))

    @staticmethod
    def _require(ok: bool, message: str) -> None:
        if not ok:
            raise TypeError(message)


REGISTRY = Registry()


class MenuItem(ABC):
    """One menu item: one job, and where it sits in the order."""

    number: int
    NAME: str
    DESCRIPTION: str
    # DESCRIPTION is the menu row: one line, present tense, what the step does.
    # ABOUT is what `h <n>` prints above the graph — the part a one-liner has no
    # room for, and the part an operator needs BEFORE running something for the
    # first time: what it costs, what it assumes has already happened, and what
    # it will not do for you. Blank lines separate paragraphs; the help screen
    # wraps it to the terminal.
    ABOUT = ""
    START = False
    END = False
    DESTR = False
    WORD = ""                       # the word typed before an irreversible act
    SCOPE = Scope.LOCAL
    # Does running this change WHAT THE DESTINATION IS ASKED? is_complete() is
    # asked about a list of trips, so the answer goes stale when the list moves
    # (an import, sidecars written, a trip dropped, the workspace erased) or
    # when the destination itself moves (an upload). Nothing else invalidates
    # it, and invalidating it costs a full round trip -- ssh, a bucket listing
    # and a fetch of the live index -- on the next menu draw.
    #
    # Defaults TRUE so that forgetting to think about it is the SAFE mistake:
    # a needless refresh is nine seconds, and a stale YES is what stands
    # between Clean Workspace and footage whose only copy is here.
    CHANGES_THE_QUESTION = True
    INBOUND_KIND: Optional[type] = None       # None = derive from the outbounds
    OUT: Dict[Strategy, Neighbours] = {}
    IN_AUTHORED: Dict[Strategy, Optional[FrozenSet[int]]] = {}

    def __init_subclass__(cls, abstract: bool = False, **kw):
        super().__init_subclass__(**kw)
        if abstract:
            return
        REGISTRY.declare(cls)

    def __init__(self, strategy: Strategy, work, inbound: Neighbours):
        # The strategy is resolved ONCE, here. Items 5, 6 and 7 differ between
        # the two products; that difference lives in the edges this item
        # reports and in which collaborator the constructor installs, never in
        # an `if` inside a method the menu calls forty times a session.
        self._strategy = strategy
        self._out = self.OUT[strategy]
        self._in = inbound
        self._work = work
        self._outcome: Optional[Outcome] = None

    # -- what it is --------------------------------------------------------
    def name(self) -> str:
        return self.NAME

    def description(self) -> str:
        return self.DESCRIPTION

    def about(self) -> str:
        return self.ABOUT

    def start(self) -> bool:
        return self.START

    def end(self) -> bool:
        return self.END

    def destr(self) -> bool:
        return self.DESTR

    def word(self) -> str:
        return self.WORD

    def strategy(self) -> Strategy:
        return self._strategy

    # -- where it sits -----------------------------------------------------
    def inbound(self) -> Neighbours:
        return self._in

    def outbound(self) -> Neighbours:
        return self._out

    def settles_at(self, current: int) -> int:
        return self._out.settles_at(self.number, current)

    # -- what it did -------------------------------------------------------
    def completed(self) -> bool:
        """Transient: the answer from the most recent execute(), or an error.

        Never a default. A stale read here is the difference between a report
        saying the card was erased and one saying it was refused.
        """
        if self._outcome is None:
            raise NotRun("%d) %s has not run" % (self.number, self.NAME))
        return self._outcome.completed

    def outcome(self) -> Optional[Outcome]:
        return self._outcome

    # A refusal this item lets the operator step past, by typing this word.
    # Empty means there is no way past: most refusals are facts about the
    # world, and the answer to them is to change the world.
    OVERRIDE_WORD = ""

    def override(self, world) -> Optional[Outcome]:
        """Take the way past, having been shown exactly what it costs.

        Not a bypass. The item that offers one has to make the refusal FALSE
        -- by recording the decision the guard was waiting for -- and then go
        through its own gates unchanged. A door around the guard would be a
        second erase path with its own bugs.
        """
        return None

    def aborted(self, note: str, performed: bool = False) -> Outcome:
        """Record an interruption as this item's answer.

        Ctrl-C and a bare q leave execute() part-way through, so the outcome
        would otherwise stay unset and completed() would raise at the point the
        runner asks it. An abort is simply not completing: the position stays
        where it was, which is the same thing a declined prompt means.
        """
        self._outcome = Outcome.stopped(note, performed)
        return self._outcome

    # -- doing it ----------------------------------------------------------
    def execute(self, world) -> Outcome:
        """Do the one job, once.

        Final on purpose: an item cannot forget to consult its own guard,
        because it never gets to write this method. Idempotent by the same
        route — an item whose postcondition already holds answers SATISFIED
        from evaluate() and does nothing.
        """
        self._outcome = None
        self._outcome = self._guarded(world)
        return self._outcome

    def _guarded(self, world) -> Outcome:
        verdict = self.evaluate(world)
        if verdict.ruling is Ruling.GO:
            return self._perform(world)
        return Outcome.not_doing(verdict)

    @abstractmethod
    def evaluate(self, world) -> Verdict:
        """Would this do anything, and may it? Pure over the passed-in world."""

    @abstractmethod
    def _perform(self, world) -> Outcome:
        """The work. Never called without evaluate() having passed."""

    def __repr__(self) -> str:                # pragma: no cover - debug aid
        return "<%d %s>" % (self.number, self.NAME)


# ---------------------------------------------------------------------------
# Destructive items: the sequence that puts a fresh world under the guard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """What would be erased, which guard says so, and what does it.

    `guard` and `act` have NO defaults on purpose. They are the two halves of
    the re-check that stands between a typed word and an irreversible call, and
    a plan that forgot one used to be constructible — and crashed at the moment
    it mattered, after the operator had typed DROP.
    """

    guard: object                  # world -> Verdict; the SAME callable evaluate uses
    act: object                    # world -> Outcome
    banner: Tuple[str, ...] = ()
    nothing: str = ""              # set when there turned out to be nothing to do

    @classmethod
    def nothing_to_do(cls, reason: str) -> "Plan":
        """No target, so no word is asked for and neither half is ever called."""
        return cls(cls._never, cls._never, nothing=reason)

    @staticmethod
    def nothing_to_recheck(world) -> Verdict:
        """A deliberate no-op re-check, for the one item whose evidence cannot
        change between the prompt and the act.

        Exclude Trip deletes the clips the operator just picked off a list. A
        clip that vanished while the prompt was on screen makes the delete a
        no-op rather than a hazard, so there is nothing a second look could
        refuse on. It is a NAMED guard passed explicitly rather than a default,
        because "this one needs no re-check" has to be a decision somebody
        wrote down and not a field nobody filled in.
        """
        return Verdict.go()

    @staticmethod
    def _never(world) -> Verdict:        # pragma: no cover - never reached
        """Both halves of a plan there is nothing to do. Never reached: the
        item returns on `nothing` before either is called. It refuses rather
        than passes so that a route that did reach it stops."""
        return Verdict.refuse("this plan found nothing to do")


class Destructive(MenuItem, abstract=True):
    """Show, ask for the word, RE-DERIVE the world, re-ask the guard, act.

    The re-derivation point is early enough by construction rather than by
    inspection: every irreversible statement lives in plan.act, plan.act only
    ever receives the freshly captured world, and a subclass supplies only
    WHAT and WHICH GUARD. It never gets to destroy anything on its own, so it
    cannot have invalidated its own evidence before the guard sees it. The
    shipped defect this replaces reasoned the other way — the trees do not
    overlap, so the evidence must still hold — and the trees did overlap.
    """

    def _perform(self, world) -> Outcome:
        plan = self._plan(world)
        if plan.nothing:
            return Outcome.stopped(plan.nothing)
        return self._confirm(plan)

    def _confirm(self, plan: Plan) -> Outcome:
        self._work.show(plan.banner)
        if self._work.ask_word(self.WORD) != self.WORD:
            return Outcome.stopped("Aborted by user pre-run.")
        return self._commit(plan)

    def _commit(self, plan: Plan) -> Outcome:
        fresh = self._work.recapture(self.SCOPE)
        verdict = plan.guard(fresh)
        if verdict.blocked:
            return self._work.refuse(self.name(), verdict.reason)
        return plan.act(fresh)

    @abstractmethod
    def _plan(self, world) -> Plan:
        """What would be erased, which guard says so, and what does it."""


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Disagreement:
    """One row of the owner's inbound column against the derivation."""

    number: int
    authored: FrozenSet[int]
    derived: FrozenSet[int]

    @property
    def extra(self) -> FrozenSet[int]:
        return self.derived - self.authored

    @property
    def missing(self) -> FrozenSet[int]:
        return self.authored - self.derived

    @classmethod
    def between(cls, number, authored, derived) -> Optional["Disagreement"]:
        """The two columns for one item, or None when they agree.

        Returning None for agreement rather than an "empty" Disagreement keeps
        the report a list of findings: a row that is on it is a difference, and
        nothing has to ask a Disagreement whether it disagrees.
        """
        if authored == derived:
            return None
        return cls(number, authored, derived)


class MenuGraph:
    """The eleven classes read as a graph, for ONE strategy.

    The strategy is bound once, here, instead of being handed to five
    functions in turn. It was the second argument of everything in this
    section, and every one of them had to be trusted to pass on the same one:
    the derivation, the authored column and the construction all read
    OUT[strategy] and IN_AUTHORED[strategy], and a mismatch between any two of
    them would have built a menu whose edges came from one product and whose
    items came from the other.

    It holds classes, never instances. Nothing here needs a built item, and
    keeping it that way is what lets the graph be printed and diffed without a
    `work` to hand -- tests/print_step_graph.py has none worth speaking of.
    """

    def __init__(self, classes, strategy: Strategy):
        self._classes = REGISTRY.classes() if classes is None else classes
        self._strategy = strategy

    def build(self, work) -> Dict[int, MenuItem]:
        """Construct the eleven items, each handed the inbound derived for it."""
        inbound = self.inbound()
        return {n: cls(self._strategy, work, inbound[n])
                for n, cls in self._classes.items()}

    def inbound(self) -> Dict[int, Neighbours]:
        """Every item's inbound, computed from every other item's outbound."""
        out = self._outbound()
        return {n: self._inbound_for(n, cls, out)
                for n, cls in self._classes.items()}

    def disagreements(self) -> List[Disagreement]:
        """Where the owner's hand-written inbound column and the derivation part.

        Reported, never reconciled: the derivation is what the tool runs on, and
        each difference is a line of the table for its author to confirm or fix.
        """
        derived = self.inbound()
        found = map(lambda kv: self._disagreement(kv[0], kv[1], derived),
                    self._classes.items())
        return sorted(filter(None, found), key=lambda d: d.number)

    def _outbound(self) -> Dict[int, Neighbours]:
        return {n: cls.OUT[self._strategy] for n, cls in self._classes.items()}

    def _inbound_for(self, n, cls, out) -> Neighbours:
        if cls.INBOUND_KIND is not None:
            return cls.INBOUND_KIND()
        return Edges(frozenset(filter(lambda a: out[a].reaches(n), out)))

    def _disagreement(self, n, cls, derived) -> Optional[Disagreement]:
        authored = self._authored_edges(cls)
        if authored is None:
            return None
        return Disagreement.between(n, authored, derived[n].edges())

    def _authored_edges(self, cls) -> Optional[FrozenSet[int]]:
        """The owner's inbound column for this item, or None when it is a KIND.

        Two items are exempt BY DEFINITION, not by a number on a skip list:
        Import SIM declares StartNode (its inbound is empty because it is where
        footage comes in, even though Clean Workspace offers it back), and
        Progress declares Anywhere (it neighbours everything and must not force
        the other ten to declare it back).
        """
        if cls.INBOUND_KIND is not None:
            return None
        return cls.IN_AUTHORED[self._strategy]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

NOWHERE = -1


@dataclass
class Position:
    """Where the pipeline is. One integer, and no history stack.

    "Step back by 1" is the move NOT taking effect: the position is only ever
    written on a completing advance, so there is nothing to pop and no
    empty-stack case. A stack is state that can disagree with the graph; an
    integer cannot.

    The position can OFFER an item. It can never AUTHORISE one — evaluate()
    runs again against a fresh world inside execute(), and a third time after
    the typed word. That is what lets it be a bare integer.
    """

    universe: FrozenSet[int]
    views: FrozenSet[int]
    starts: FrozenSet[int]
    current: int = NOWHERE

    @classmethod
    def for_menu(cls, menu: Dict[int, MenuItem]) -> "Position":
        """A Position that knows this menu's universe, views and entry points.

        Read off the built menu rather than passed in, so the three sets cannot
        describe a different menu from the one they will be asked about.
        """
        views = frozenset(filter(lambda n: menu[n].outbound().is_view(), menu))
        starts = frozenset(filter(lambda n: menu[n].start(), menu))
        return cls(frozenset(menu), views, starts)

    def selectable(self, menu: Dict[int, MenuItem]) -> FrozenSet[int]:
        return self._reachable(menu) | self.views

    def _reachable(self, menu) -> FrozenSet[int]:
        if self.current == NOWHERE:
            return self.starts
        return menu[self.current].outbound().offers(self.universe)

    def advance(self, item: MenuItem) -> int:
        if item.completed():
            self.current = item.settles_at(self.current)
        return self.current

    def orient(self, world, rules) -> int:
        """Derive where we are from the disk, on a cold start.

        Not persisted: a remembered position is a file that can lie about a
        world which moved while the tool was off; a derived one cannot be
        older than the disk. Correctness does not depend on it — an item whose
        postcondition holds completes and advances, so a cold start walks
        forward to the same place. It saves the keypresses, nothing more.
        """
        hit = next(filter(lambda rule: rule[1](world), rules), None)
        self.current = NOWHERE if hit is None else hit[0]
        return self.current


# ---------------------------------------------------------------------------
# The module surface
#
# Everything above answers for itself. What follows is the set of names the
# rest of the tool -- and one published plugin interface -- says out loud, each
# one word for word what it has always been and now a single call into the type
# that owns the behaviour.
#
# `go`, `satisfied`, `blocked`, `did` and `stopped` are re-exported by
# uploader.py and are how an outside plugin builds its answers: `from uploader
# import Verdict, go, blocked, did`. They are somebody else's source code, not
# ours, so they do not get renamed, moved onto a class, or given a keyword
# argument -- there is no deprecation window for a file we cannot see.
# ---------------------------------------------------------------------------

def go() -> Verdict:
    return Verdict.go()


def satisfied(reason: str) -> Verdict:
    return Verdict.satisfied(reason)


def blocked(reason: str, evidence: Tuple[str, ...] = ()) -> Verdict:
    return Verdict.refuse(reason, evidence)


def did(note: str) -> Outcome:
    return Outcome.did(note)


def stopped(note: str, performed: bool = False) -> Outcome:
    return Outcome.stopped(note, performed)


def nothing_to_recheck(world) -> Verdict:
    """The named no-op re-check, passed as a Plan guard. See Plan."""
    return Plan.nothing_to_recheck(world)


def registry() -> Dict[int, type]:
    """The item classes, by number, in declaration order."""
    return REGISTRY.classes()


def build_menu(strategy: Strategy, work, classes=None) -> Dict[int, MenuItem]:
    """Construct the eleven items for ONE strategy.

    The only place the strategy branch is read. `classes` is injectable so a
    test can drive the runner with mocks instead of the real eleven.
    """
    return MenuGraph(classes, strategy).build(work)


def disagreements(classes, strategy) -> List[Disagreement]:
    """The step table's two columns, diffed. See MenuGraph.disagreements."""
    return MenuGraph(classes, strategy).disagreements()


def is_view(item: MenuItem) -> bool:
    """A view neighbours everything and is never a position of its own."""
    return item.outbound().is_view()


def switched_off(item: MenuItem) -> bool:
    """No way in and no way out: this strategy does not have this item at all.

    Read off the edges rather than asked of the strategy, so there is no second
    list of which items a product includes that could disagree with the graph
    the tool actually walks. An item nothing leads to and which leads nowhere
    is unreachable by construction, and that is the whole definition.

    Both sides must be empty. A start node has an empty inbound because it is
    where a cycle begins, and it still leads somewhere; a view declares None on
    both sides rather than an empty set, which is not the same thing as having
    no edges.

    Asked of the item's two sides rather than of the item, and that is
    deliberate: the painter is handed doubles whose whole interface is
    outbound() and inbound(), and every one of these three would otherwise be
    a method the drawing code demands of anything it is shown.
    """
    return item.inbound().no_edges() and item.outbound().no_edges()


def leads_to(menu: Dict[int, MenuItem], number: int) -> List[int]:
    """The entries that offer this one, asked of the entries themselves.

    So the sentence the menu says about an unavailable entry is derived from
    the same edges the machine walks. Written down anywhere else it would be a
    second copy to fall out of date, and it would fall out of date silently,
    because prose does not fail a test.

    Views are not answers here: one neighbours everything by definition, so
    naming it would tell the operator nothing about what to do next -- and
    Anywhere answers None to reaches(), so it is excluded without being named.

    Stays a function rather than a method: it is a query over a menu the CALLER
    owns -- build_menu hands back a plain dict, by contract with pipeline.py --
    and the only object it could hang off is one nothing else would hold.
    """
    return sorted(filter(lambda a: menu[a].outbound().reaches(number), menu))


def position_for(menu: Dict[int, MenuItem]) -> Position:
    return Position.for_menu(menu)
