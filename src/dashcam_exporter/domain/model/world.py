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

  * `TargetFacts` holds a FROZEN ANSWER, not a live handle on the configured
    plugin. A guard that could call out to the network answers differently
    on two reads of the same World, and the destructive re-check is built on
    exactly that not happening.
  * `Card.new_stamps` and `Card.owed_stamps` are FIELDS, derived once at
    capture. They used to be a function reading a module global that four call
    sites remembered to refresh first; a global four places remember is a
    global the fifth forgets.
  * `expected_trips` stays Optional[int] and the destination's answer stays
    three-valued-plus-NA. Collapsing either to a bool weakens a guard without
    the diff looking like it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

from dashcam_exporter.domain.menu.menu import Evidence, Scope, Strategy


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
    that is not verified-imported (its stamp in the import manifest), not
    excluded, not inside a rendered trip's span, and not sitting in the
    workspace. Non-empty means some clip exists nowhere else. It is a set
    rather than a boolean because one rendered trip vouching for a whole card
    is exactly how a wipe erased footage whose only copy was the card.
    """

    path: Optional[Path] = None
    dcim: bool = False                  # a DCIM directory is there at all
    present: bool = False               # ...and it holds files
    stamps: FrozenSet[str] = frozenset()
    new_stamps: FrozenSet[str] = frozenset()      # never imported
    owed_stamps: FrozenSet[str] = frozenset()     # accounted for by nothing
    # The files behind new_stamps, so a refusal can name what it is refusing
    # about. Carried rather than looked up: a guard that goes to the disk
    # answers differently on two reads of one world.
    new_files: Tuple[str, ...] = ()
    # Files an import would still fetch: the clips above, and the GPS beside
    # them. Separate from new_stamps because a card can hold tracks for clips
    # that came over before the tracks did — nothing new to import by the
    # clip count, and a trip that cannot be described until they arrive.
    to_fetch: int = 0
    note: str = ""                                # how the accounting was met


@dataclass(frozen=True)
class TargetFacts:
    """What the configured plugin said about this import's trips, as of world.at.

    One answer, all or nothing: is EVERY trip of this import at the
    destination. That is the only shape anything here acts on — Clean Workspace
    erases the whole working area, never a chosen trip — and a per-trip map
    would be a finer answer to a question nobody asks.

    An answer, not a handle: nothing downstream may ask the plugin a NEW
    question while judging a world. The whole point of freezing it is that the
    destructive re-check captures a second world and gets a second answer, and
    the two are then judged by the same guard.

    The defaults are the local edition: nothing configured, so the question
    about a destination is NA rather than unanswered. An unreachable CONFIGURED
    plugin is a different thing entirely and says UNKNOWN, which fails closed.

    `note` is the EXPORTER's own words about how the answer was arrived at —
    not asked at this scope, or the plugin raised and what it said. Printed
    under a refusal so an operator can tell "the destination said no" from
    "nobody could ask it".

    `namespace` is WHICH IMPORT the answer is about, and it is not optional
    bookkeeping. The question asked is "are THESE trips complete", and the
    trips are one import's — but several imports can sit under one <out>, each
    in its own namespace, and the working area is swept across all of them.
    Without the scope written down beside the answer, a yes about one import
    reads as a yes about material nobody was asked about. Empty means the
    answer covers nothing: no plugin, or no import was settled on, in which
    case the trip list was empty and a yes to it is vacuous.
    """

    configured: bool = False
    name: str = ""
    origin: str = ""                    # who answered, composed by the loader
    complete: Evidence = Evidence.NA
    namespace: str = ""                 # the import the answer is about
    note: str = ""


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
    # The operator says this machine is on a bad connection. Nothing in the
    # exporter reads it — there is no networked code left here to skip. It is
    # carried so a target can honour it without inventing its own setting name,
    # which is how two spellings of one question end up disagreeing.
    offline: bool = False

    # the workspace
    out_dir: Optional[Path] = None
    out_dir_owner: Optional[str] = None          # another checkout's claim
    imports: Tuple[Path, ...] = ()
    selected_import: Optional[Path] = None
    metas: Tuple[TripMeta, ...] = ()
    renders: Tuple[Render, ...] = ()             # the whole output tree
    renders_here: Tuple[Render, ...] = ()        # this import's namespace only
    # The trips THIS IMPORT contains, sidecar-derived, including any that
    # produced no render — which is what makes the destination's all-or-nothing
    # answer safe: a trip nobody encoded is still in the list it is asked about.
    trip_ids: Tuple[str, ...] = ()
    # Trips deleted on purpose, ever, in this workspace. A dropped trip and a
    # published-then-cleaned-up trip are indistinguishable afterwards, so this
    # is recorded at the only moment that knows which it was.
    dropped_ids: Tuple[str, ...] = ()
    # The same act counted honestly. dropped_ids holds out_base names, which a
    # trip too short to render never has -- so it is short by exactly the
    # fragments, and the progress row that counted it said 1 where the operator
    # had excluded 3. This is one key per excluded trip, fragments included.
    dropped_trips: Tuple[str, ...] = ()
    # Every FILE in the working import, by path relative to it, and the ones
    # the source in the slot does not have at the same path and size.
    #
    # Files rather than clip stamps, and that is not a detail. A stamp names a
    # front clip; the import also holds the rear camera, the GPS tars and the
    # camera's event log, and the rear folder rotates independently of the
    # front one -- so "every stamp is on the card" was true of an import whose
    # rear clip existed nowhere else. Two fields rather than one flag because
    # an empty import and a fully-sourced one both leave "unsourced" empty,
    # and only one of them is footage worth keeping.
    import_files: FrozenSet[str] = frozenset()
    unsourced_files: FrozenSet[str] = frozenset()
    # The configured card path resolving to the import itself, or to something
    # containing it. Then the comparison above is a directory against itself
    # and its "all of it is on the card" means nothing at all.
    card_shares_the_import: bool = False
    # Clips in this import that NOTHING else accounts for: not on the card,
    # not inside a rendered trip, not excluded on purpose. A trip too short to
    # render gets no sidecar, so it reaches neither trip_ids nor
    # expected_trips and is invisible to both of item 8's gates -- the local
    # floor is met by the renders that do exist, and the destination answers
    # YES honestly about the trips it was asked about.
    orphan_clips: Tuple[str, ...] = ()
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
