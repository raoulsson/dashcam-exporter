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

  * `Listing` is a TYPE, not Optional[dict]. `Unlistable` has no .get(), so
    `s3_objects(ctx) or {}` — the idiom that turns "could not find out" into
    "not there" — is unwritable against it.
  * `Card.new_stamps` and `Card.owed_stamps` are FIELDS, derived once at
    capture. They used to be a function reading a module global that four call
    sites remembered to refresh first; a global four places remember is a
    global the fifth forgets.
  * `expected_trips` stays Optional[int] and `SiteFacts.published` stays
    three-valued-plus-NA. Collapsing either to a bool weakens a guard without
    the diff looking like it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from menu import Evidence, Scope, Strategy


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


class Listing(ABC):
    """What the bucket said, including that it did not say."""

    @abstractmethod
    def holds(self, name: str, size: int) -> Evidence:
        """Is this exact object there at this exact size?"""

    @abstractmethod
    def covers(self, renders) -> Evidence:
        """Is every one of these there? UNKNOWN is not NO."""


def _same_object(key: str, name: str) -> bool:
    """Key match on a "/" boundary.

    A bare endswith let sometrip_X.mp4 in the bucket vouch for trip_X.mp4 on
    disk. This rule lives here and nowhere else now.
    """
    return key == name or key.endswith("/" + name)


def _yes_no(ok: bool) -> Evidence:
    return Evidence.YES if ok else Evidence.NO


@dataclass(frozen=True)
class Listed(Listing):
    objects: Dict[str, int] = field(default_factory=dict)

    def holds(self, name, size) -> Evidence:
        return _yes_no(self.size_of(name) == size)

    def size_of(self, name) -> Optional[int]:
        """The size of the object with this name, or None if it is not there."""
        hits = filter(lambda kv: _same_object(kv[0], name), self.objects.items())
        return next(map(lambda kv: kv[1], hits), None)

    def covers(self, renders) -> Evidence:
        return _yes_no(all(map(self._holds_render, renders)))

    def _holds_render(self, render) -> bool:
        return self.holds(render.name, render.size) is Evidence.YES

    def names(self) -> FrozenSet[str]:
        return frozenset(self.objects)


class Unlistable(Listing):
    """A bucket is configured and could not be read. Fails closed, always."""

    def holds(self, name, size) -> Evidence:
        return Evidence.UNKNOWN

    def covers(self, renders) -> Evidence:
        return Evidence.UNKNOWN

    def names(self) -> FrozenSet[str]:
        return frozenset()


class NoBucket(Listing):
    """No bucket is configured, so the question does not arise here."""

    def holds(self, name, size) -> Evidence:
        return Evidence.NA

    def covers(self, renders) -> Evidence:
        return Evidence.NA

    def names(self) -> FrozenSet[str]:
        return frozenset()


@dataclass(frozen=True)
class SiteFacts:
    """The second repo, the deploy record, and what the live site serves."""

    configured: bool = False            # site_repo is set in config.txt
    on_disk: bool = False               # and the directory is actually there
    scripts: FrozenSet[str] = frozenset()          # deploy/ scripts found
    has_manifest_builder: bool = False
    videos_link: Optional[Path] = None  # what public_html/videos resolves to
    curation_deleted: FrozenSet[str] = frozenset()  # admin.json mode=delete ids
    deployed: FrozenSet[str] = frozenset()          # render names a deploy covered
    published: Evidence = Evidence.NA   # is-complete.py's verdict, FULL scope only
    published_counts: Tuple[Optional[int], Optional[int]] = (None, None)
    published_lines: Tuple[str, ...] = ()
    page: bool = False                  # a built local result page exists

    def has(self, script: str) -> bool:
        return script in self.scripts


@dataclass(frozen=True)
class World:
    """Everything a menu item is allowed to look at, as of `at`.

    Every field has a default, so a test writes World(renders=(...), card=...)
    and nothing else. That is the point: the guards became testable when they
    stopped going to disk themselves.
    """

    at: float = 0.0
    scope: Scope = Scope.LOCAL
    strategy: Strategy = Strategy.LOCAL_DEFAULT_WEBSITE

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
    bucket: Listing = field(default_factory=NoBucket)
    site: SiteFacts = field(default_factory=SiteFacts)
    awscli: bool = False
