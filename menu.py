"""The menu: ten items, the graph they sit in, and the position we are at.

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
and `derive_inbound` computes every inbound from every other item's outbound.
The hand-written inbound column survives as IN_AUTHORED and is DIFFED against
the derivation by `disagreements()`, which reports rather than reconciles: a
difference is a finding for the person who wrote the table, printed by
tests/print_step_graph.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple


class Strategy(Enum):
    """Which of the two products this installation is."""

    UPLOADER = "uploader"
    LOCAL_PAGE = "local page"

    @classmethod
    def of(cls, plugin) -> "Strategy":
        """Was a publishing plugin supplied. That is the whole question.

        It takes the plugin itself, not the ctx it came from: asked of a ctx
        this would read config keys, and the keys it read named one operator's
        arrangement. Which product this is has to be answerable without knowing
        how anybody publishes.
        """
        return cls.UPLOADER if plugin is not None else cls.LOCAL_PAGE


# Item numbers, named once so the graph reads as sentences rather than integers.
PROGRESS, IMPORT, META, PREVIEW, EXCLUDE = 0, 1, 2, 3, 4
RENDER, BUILD, UPLOAD, CLEAN_WS, ERASE_CARD = 5, 6, 7, 8, 9


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

class Ruling(Enum):
    GO = "go"
    SATISFIED = "satisfied"        # the postcondition already holds
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Verdict:
    ruling: Ruling
    reason: str = ""
    # What the reason is talking about, when naming it is worth more than
    # counting it. "13 new clips ready for next session" tells the operator
    # the number; these tell him WHICH, so he can see for himself whether they
    # are footage he wants or the trash he suspects. Printed under the refusal
    # and nowhere else.
    evidence: Tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.ruling is Ruling.BLOCKED

    @property
    def selectable(self) -> bool:
        return not self.blocked


def go() -> Verdict:
    return Verdict(Ruling.GO)


def satisfied(reason: str) -> Verdict:
    return Verdict(Ruling.SATISFIED, reason)


def blocked(reason: str, evidence: Tuple[str, ...] = ()) -> Verdict:
    return Verdict(Ruling.BLOCKED, reason, tuple(evidence))


@dataclass(frozen=True)
class Outcome:
    """What one run of an item amounted to.

    `completed` is the owner's signal: the item's postcondition holds and the
    operator did not abort. The pipeline advances the position on true and
    leaves it where it was on false, which is what "steps back by one" means
    for a move that did not take effect.
    """

    completed: bool
    note: str
    performed: bool = True      # False when the postcondition already held


def did(note: str) -> Outcome:
    return Outcome(True, note)


def stopped(note: str) -> Outcome:
    return Outcome(False, note)


def _not_doing(verdict: Verdict) -> Outcome:
    """The two ways an item does not run, which are not the same answer.

    BLOCKED did not happen and does not complete: the pipeline stays where it
    was. SATISFIED did not happen either and DOES complete: the postcondition
    already holds, nothing is owed, and the pipeline may move on. That is the
    whole reason evaluate() is three-valued while completed() is two-valued.

    This is also what makes execute() idempotent rather than merely re-runnable.
    A second Delete SIM Data on a card it has just emptied must not reach the
    ERASE prompt to find out there is nothing behind it.
    """
    if verdict.blocked:
        return stopped(verdict.reason)
    return Outcome(True, verdict.reason, performed=False)


class NotRun(Exception):
    """completed() read before execute() ran. Never a default answer."""


class Evidence(Enum):
    """Three-valued, plus 'this check could not apply here at all'.

    Collapsing UNKNOWN into NO or YES is how a guard gets weakened without the
    diff looking like it: "could not ask the destination" is not "not at the
    destination", and neither is "there is no destination".
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    NA = "not applicable"

    @property
    def applicable(self) -> bool:
        return self is not Evidence.NA


# ---------------------------------------------------------------------------
# Neighbours. Three of the ten items name something that is not a set of
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


@dataclass(frozen=True)
class Edges(Neighbours):
    numbers: FrozenSet[int]

    def offers(self, universe):
        return self.numbers

    def edges(self):
        return self.numbers

    def settles_at(self, number, current):
        return number


class Anywhere(Neighbours):
    """A view: neighbours everything, is never a position, declares no edge.

    edges() is None rather than "every number", which is what keeps the other
    nine items from having to declare the view back — and what keeps the view
    off the disagreement report by definition instead of by a skip list.
    """

    def offers(self, universe):
        return universe

    def edges(self):
        return None

    def settles_at(self, number, current):
        return current

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


class Scope(Enum):
    """How much of the world an item's guard needs to see.

    LOCAL is the filesystem and is what the menu draws on every loop. FULL
    also asks the configured publishing target what it holds and serves, which
    may go to the network or shell out — fine once per dispatch, not once per
    keystroke. At LOCAL scope a configured target reads UNKNOWN everywhere,
    which every guard already treats as not proven.
    """

    LOCAL = "local"
    FULL = "full"


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

_REGISTRY: Dict[int, type] = {}


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise TypeError(message)


def _declare(cls) -> None:
    """Import-time checks. A malformed item fails to load, not to run."""
    _check(set(cls.OUT) == set(Strategy), "%s: OUT must cover every Strategy" % cls)
    _check(set(cls.IN_AUTHORED) == set(Strategy),
           "%s: IN_AUTHORED must cover every Strategy" % cls)
    _check(cls.DESTR == bool(cls.WORD), "%s: DESTR and WORD must agree" % cls)
    _REGISTRY[cls.number] = cls


class MenuItem(ABC):
    """One menu item: one job, and where it sits in the order."""

    number: int
    NAME: str
    DESCRIPTION: str
    START = False
    END = False
    DESTR = False
    WORD = ""                       # the word typed before an irreversible act
    SCOPE = Scope.LOCAL
    INBOUND_KIND: Optional[type] = None       # None = derive from the outbounds
    OUT: Dict[Strategy, Neighbours] = {}
    IN_AUTHORED: Dict[Strategy, Optional[FrozenSet[int]]] = {}

    def __init_subclass__(cls, abstract: bool = False, **kw):
        super().__init_subclass__(**kw)
        if abstract:
            return
        _declare(cls)

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

    def aborted(self, note: str) -> Outcome:
        """Record an interruption as this item's answer.

        Ctrl-C and a bare q leave execute() part-way through, so the outcome
        would otherwise stay unset and completed() would raise at the point the
        runner asks it. An abort is simply not completing: the position stays
        where it was, which is the same thing a declined prompt means.
        """
        self._outcome = stopped(note)
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
        return _not_doing(verdict)

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

def nothing_to_recheck(world) -> Verdict:
    """A deliberate no-op re-check, for the one item whose evidence cannot
    change between the prompt and the act.

    Exclude Trip deletes the clips the operator just picked off a list. A clip
    that vanished while the prompt was on screen makes the delete a no-op
    rather than a hazard, so there is nothing a second look could refuse on.
    It is a NAMED function passed explicitly rather than a default, because
    "this one needs no re-check" has to be a decision somebody wrote down and
    not a field nobody filled in.
    """
    return go()


def _no_plan(world) -> Verdict:          # pragma: no cover - never reached
    return blocked("this plan found nothing to do")


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
        return cls(_no_plan, _no_plan, nothing=reason)


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
            return stopped(plan.nothing)
        return self._confirm(plan)

    def _confirm(self, plan: Plan) -> Outcome:
        self._work.show(plan.banner)
        if self._work.ask_word(self.WORD) != self.WORD:
            return stopped("Aborted by user pre-run.")
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

def _out_sets(classes, strategy) -> Dict[int, Optional[FrozenSet[int]]]:
    return {n: cls.OUT[strategy].edges() for n, cls in classes.items()}


def _offers(numbers, n) -> bool:
    return bool(numbers) and n in numbers


def _inbound_for(n, cls, out) -> Neighbours:
    if cls.INBOUND_KIND is not None:
        return cls.INBOUND_KIND()
    return Edges(frozenset(filter(lambda a: _offers(out[a], n), out)))


def derive_inbound(classes, strategy) -> Dict[int, Neighbours]:
    """Every item's inbound, computed from every other item's outbound."""
    out = _out_sets(classes, strategy)
    return {n: _inbound_for(n, cls, out) for n, cls in classes.items()}


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


def _authored_edges(cls, strategy) -> Optional[FrozenSet[int]]:
    """The owner's inbound column for this item, or None when it is a KIND.

    Two items are exempt BY DEFINITION, not by a number on a skip list:
    Import SIM declares StartNode (its inbound is empty because it is where
    footage comes in, even though Clean Workspace offers it back), and
    Progress declares Anywhere (it neighbours everything and must not force
    the other nine to declare it back).
    """
    if cls.INBOUND_KIND is not None:
        return None
    return cls.IN_AUTHORED[strategy]


def _diff(n, authored, derived) -> Optional[Disagreement]:
    if authored == derived:
        return None
    return Disagreement(n, authored, derived)


def _disagreement(n, cls, strategy, derived) -> Optional[Disagreement]:
    authored = _authored_edges(cls, strategy)
    if authored is None:
        return None
    return _diff(n, authored, derived[n].edges())


def _number_of(d: Disagreement) -> int:
    return d.number


def disagreements(classes, strategy) -> List[Disagreement]:
    """Where the owner's hand-written inbound column and the derivation part.

    Reported, never reconciled: the derivation is what the tool runs on, and
    each difference is a line of the table for its author to confirm or fix.
    """
    derived = derive_inbound(classes, strategy)
    found = map(lambda kv: _disagreement(kv[0], kv[1], strategy, derived),
                classes.items())
    return sorted(filter(None, found), key=_number_of)


def registry() -> Dict[int, type]:
    """The item classes, by number, in declaration order."""
    return dict(_REGISTRY)


def _or_registry(classes) -> Dict[int, type]:
    if classes is None:
        return registry()
    return classes


def build_menu(strategy: Strategy, work, classes=None) -> Dict[int, MenuItem]:
    """Construct the ten items for ONE strategy.

    The only place the strategy branch is read. `classes` is injectable so a
    test can drive the runner with mocks instead of the real ten.
    """
    chosen = _or_registry(classes)
    inbound = derive_inbound(chosen, strategy)
    return {n: cls(strategy, work, inbound[n]) for n, cls in chosen.items()}


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
        self.current = _rule_number(hit)
        return self.current


def _rule_number(rule) -> int:
    if rule is None:
        return NOWHERE
    return rule[0]


def is_view(item: MenuItem) -> bool:
    """A view neighbours everything and is never a position of its own."""
    return isinstance(item.outbound(), Anywhere)


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
    """
    return (item.inbound().edges() == frozenset()
            and item.outbound().edges() == frozenset())


def leads_to(menu: Dict[int, MenuItem], number: int) -> List[int]:
    """The entries that offer this one, asked of the entries themselves.

    So the sentence the menu says about an unavailable entry is derived from
    the same edges the machine walks. Written down anywhere else it would be a
    second copy to fall out of date, and it would fall out of date silently,
    because prose does not fail a test.

    Views are not answers here: one neighbours everything by definition, so
    naming it would tell the operator nothing about what to do next.
    """
    return sorted(filter(lambda a: _reaches(menu[a], number), menu))


def _reaches(item: MenuItem, number: int) -> bool:
    return _offers(item.outbound().edges(), number)


def position_for(menu: Dict[int, MenuItem]) -> Position:
    """A Position that knows this menu's universe, views and entry points."""
    views = frozenset(filter(lambda n: is_view(menu[n]), menu))
    starts = frozenset(filter(lambda n: menu[n].start(), menu))
    return Position(frozenset(menu), views, starts)
