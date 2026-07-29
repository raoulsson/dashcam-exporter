"""The world the pipeline hands to a menu item: plain facts, no filesystem.

Nothing in this module goes and looks at anything. It is the shape of what
pipeline.capture_world() found, so every guard over it is a pure function a
test can build an argument for by hand — no fixture tree, no module patching,
no ordering between the guard and the disk.

Frozen, because a guard that can mutate what it is judging is a guard nobody
can reason about. Re-derived rather than updated, because an update is a
second way to be wrong: pipeline.capture_world is called again whenever the
answer could have changed, and always immediately before anything irreversible.

Three shapes here carry weight and must not be simplified:

  * `TargetFacts` holds FROZEN ANSWERS, not a live handle on the configured
    uploader. A guard that could call out to the network answers differently
    on two reads of the same World, and the destructive re-check is built on
    exactly that not happening.
  * `Card.new_stamps` and `Card.owed_stamps` are FIELDS, derived once at
    capture. They used to be a function reading a module global that four call
    sites remembered to refresh first; a global four places remember is a
    global the fifth forgets.
  * `expected_trips` stays Optional[int] and the target's answers stay
    three-valued-plus-NA. Collapsing either to a bool weakens a guard without
    the diff looking like it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

from menu import Scope, Strategy
from uploader import Answers, Owed


@dataclass(frozen=True)
class Render:
    """One rendered mp4 as the guards see it: a name and a size."""

    name: str
    size: int
    path: Optional[Path] = None


@dataclass(frozen=True)
class TripMeta:
    """A sidecar's span, in the 14-digit form a clip filename carries."""

    id: str
    start: str = ""
    end: str = ""
    path: Optional[Path] = None

    def covers(self, stamp: str) -> bool:
        if not self.start:
            return False
        return self.start <= stamp <= self.end


@dataclass(frozen=True)
class Card:
    """The source in the slot, and the per-clip accounting for it.

    `owed_stamps` is the whole guard on erasing it: every clip on THIS card
    that is not excluded, not inside a rendered trip's span, and not sitting
    in the workspace. Non-empty means some clip exists nowhere else. It is a
    set rather than a boolean because one rendered trip vouching for a whole
    card is exactly how a wipe erased footage whose only copy was the card.
    """

    path: Optional[Path] = None
    dcim: bool = False                  # a DCIM directory is there at all
    present: bool = False               # ...and it holds files
    stamps: FrozenSet[str] = frozenset()
    new_stamps: FrozenSet[str] = frozenset()      # never imported
    owed_stamps: FrozenSet[str] = frozenset()     # accounted for by nothing
    note: str = ""                                # how the accounting was met


@dataclass(frozen=True)
class TargetFacts:
    """What the configured publishing target said about these renders, as of
    world.at.

    Answers, not a handle: nothing downstream may ask the target a NEW question
    while judging a world. The whole point of freezing them is that the
    destructive re-check captures a second world and gets a second set of
    answers, and the two can then be compared by the same guard.

    The defaults are the local edition: nothing configured, so every question
    about a destination is NA rather than unanswered. An unreachable CONFIGURED
    target is a different thing entirely and says UNKNOWN, which fails closed.
    """

    configured: bool = False
    name: str = ""
    origin: str = ""                    # who answered, composed by the loader
    holds: Answers = field(default_factory=Answers.not_applicable)
    published: Answers = field(default_factory=Answers.not_applicable)
    owed: Owed = field(default_factory=Owed.nothing)
    carried: Answers = field(default_factory=Answers.not_applicable)


@dataclass(frozen=True)
class World:
    """Everything a menu item is allowed to look at, as of `at`.

    Every field has a default, so a test writes World(renders=(...), card=...)
    and nothing else. That is the point: the guards became testable when they
    stopped going to disk themselves.
    """

    at: float = 0.0
    scope: Scope = Scope.LOCAL
    strategy: Strategy = Strategy.LOCAL_PAGE

    # the workspace
    out_dir: Optional[Path] = None
    out_dir_owner: Optional[str] = None          # another checkout's claim
    imports: Tuple[Path, ...] = ()
    selected_import: Optional[Path] = None
    metas: Tuple[TripMeta, ...] = ()
    renders: Tuple[Render, ...] = ()             # the whole output tree
    renders_here: Tuple[Render, ...] = ()        # this import's namespace only
    final_folders: Tuple[Path, ...] = ()
    expected_trips: Optional[int] = None         # None = grouping unreadable
    has_track: bool = False
    stills_current: bool = False
    local_page: bool = False                     # a built local result page exists
    ledger_mark: Optional[str] = None
    excluded: FrozenSet[str] = frozenset()
    excluded_at: float = 0.0
    newest_meta_at: float = 0.0

    # is the working area a second copy of everything in it?
    workspace_settled: bool = True
    workspace_note: str = ""
    stragglers: Tuple[Path, ...] = ()

    # the outside
    card: Card = field(default_factory=Card)
    target: TargetFacts = field(default_factory=TargetFacts)
