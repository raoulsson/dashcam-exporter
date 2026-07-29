"""The one thing an outside publishing target implements, and the value types
its answers come back in.

Nothing here knows about a bucket, a host, a deploy script or a second repo.
The exporter asks four kinds of question through this module and nothing else:
do the work, may it run, what is already at the destination, and — once — tell
the target something it cannot find out for itself.

An implementation is INSIDE the trust boundary. Whoever configured it owns what
it does, exactly as with any library, and the exporter believes what it says.
What the exporter does NOT delegate is a question it can answer itself: "were
these trips rendered on this machine" never leaves home, so an implementation
that answers yes to everything still cannot talk Clean Workspace into erasing
an import that produced no renders.

The one answer that must never be inventable is "could not find out". UNKNOWN
is what an unreachable target says, and unreachable is not permission — so
Answers has no dict-like accessor with a caller-supplied default: a name nobody
answered about reads UNKNOWN, never YES and never NA.
"""

from __future__ import annotations

import importlib.util
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from menu import Evidence


# ---------------------------------------------------------------------------
# What an answer looks like
# ---------------------------------------------------------------------------

# Least permissive first. An aggregate is the weakest reading in it, so one
# render nobody could speak for sinks the whole set — which is what "could not
# ask" has to mean when the next step erases the only local copy.
_ORDER = (Evidence.UNKNOWN, Evidence.NO, Evidence.YES)


def _rank(e: Evidence) -> int:
    return _ORDER.index(e)


def _weakest(readings) -> Evidence:
    """The least permissive reading, YES over an empty set.

    Vacuous YES is safe here and only here: Clean Workspace refuses outright on
    zero renders_here (guards.nothing_was_rendered_here) before any of this is
    consulted, so an empty set never reaches a gate that could act on it.
    """
    return min(readings, key=_rank, default=Evidence.YES)


@dataclass(frozen=True)
class Answers:
    """Per-render evidence, and how it was found out.

    Built through the three named constructors rather than by hand, so "I could
    not ask" cannot be spelled the same way as "everything is fine". `readings`
    is the storage; `about` and `covers` are the way out, and `about` defaults
    to UNKNOWN with no way for a caller to supply a kinder default. That is the
    same discipline world.Listing enforced by being a type instead of an
    Optional[dict] — `answers.readings.get(name, YES)` is writable, so nothing
    in the exporter may reach for `readings`, and tests pin that.
    """

    readings: Dict[str, Evidence] = field(default_factory=dict)
    applicable: bool = True
    detail: Tuple[str, ...] = ()

    @classmethod
    def of(cls, readings, detail=()) -> "Answers":
        """The target answered. Say what you found, per render name."""
        return cls(dict(readings), True, tuple(detail))

    @classmethod
    def unknown(cls, why: str) -> "Answers":
        """The target could not be reached or could not say. Every name UNKNOWN."""
        return cls({}, True, (why,))

    @classmethod
    def not_applicable(cls, why: str = "") -> "Answers":
        """This question does not arise for this target at all.

        For a target with no notion of 'served' — an archive disk holds, it does
        not publish. NA removes the gate rather than failing it, so use it only
        when the question is genuinely meaningless here, never when it merely
        could not be answered today. That case is unknown().
        """
        return cls({}, False, tuple(filter(None, (why,))))

    def about(self, name: str) -> Evidence:
        if not self.applicable:
            return Evidence.NA
        return self.readings.get(name, Evidence.UNKNOWN)

    def covers(self, renders) -> Evidence:
        if not self.applicable:
            return Evidence.NA
        return _weakest(map(self._about, renders))

    def _about(self, render) -> Evidence:
        return self.about(render.name)


@dataclass(frozen=True)
class Owed:
    """What upload() would still do, and whether the target could say.

    `certain` is False when the target could not be asked, and then EVERYTHING
    is owed: a listing that failed proves nothing. That rule lived in
    pipeline.uploads_outstanding ("a failed listing proves nothing, so
    everything stays outstanding") and is stated once here instead of per call
    site.
    """

    names: FrozenSet[str] = frozenset()
    certain: bool = True
    detail: Tuple[str, ...] = ()

    @classmethod
    def everything(cls, renders, why: str) -> "Owed":
        return cls(frozenset(r.name for r in renders), False, (why,))

    @classmethod
    def just(cls, names, detail=()) -> "Owed":
        return cls(frozenset(names), True, tuple(detail))

    @classmethod
    def nothing(cls) -> "Owed":
        return cls()

    @property
    def any(self) -> bool:
        return bool(self.names)


@dataclass(frozen=True)
class Report:
    """What one call to build() or upload() amounted to.

    Not a StepResult: that is the exporter's own logging type and an
    implementation must not have to construct one. `ok` is the only thing the
    menu reads — it becomes the item's `completed`, which decides whether the
    position advances.
    """

    ok: bool
    note: str = ""


class Ui(ABC):
    """The exporter's output, lent to an implementation for the duration of a
    build or an upload.

    So a target's work looks like the rest of the tool — same progress bar,
    same colours — without it importing pipeline internals that will move. An
    implementation is free to print() instead; this exists to make the nice
    thing the easy thing.
    """

    @abstractmethod
    def say(self, line: str) -> None:
        """One dimmed line of explanation."""

    @abstractmethod
    def warn(self, line: str) -> None:
        """One line the operator should not skim past."""

    @abstractmethod
    def run(self, cmd, cwd, label: str, env=None, parser=None) -> int:
        """Run a child process with a live progress line. Returns its exit code.

        `parser` is a callable given each output line, returning either None or
        (fraction_done, note) — that is what turns the spinner into a real bar.
        Your tool's output format is yours to parse, which is why the hook is
        here rather than a table of formats this repo would have to maintain.
        """


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class WebsiteUploaderInterface(ABC):
    """Everything the exporter asks a publishing target. Nothing else.

    WHAT AN IMPLEMENTATION MAY ASSUME

      * `world` is frozen and describes THIS machine as of `world.at`. Read
        `renders`, `renders_here`, `metas`, `out_dir`, `final_folders`,
        `excluded`, `ledger_mark`. Do not write to it; it is shared.
      * `renders` passed to holds/published/owes are the local mp4s, each with
        its bare filename and its exact byte size.
      * Ordering is the graph's job. Never ask "has build run" or "has render
        run" — those items are unreachable out of order by construction. If
        upload needs an artefact and there is none, that is a why_not_upload
        answer ABOUT THE ARTEFACT, not about a step.
      * why_not_build / why_not_upload are asked on EVERY MENU DRAW. They must
        be cheap and local: no network, no subprocess. The questions that go to
        the network are holds/published/owes/carries, and those are asked at
        most a few times per dispatch.
      * holds/published/owes must NOT memoise. They are asked again after the
        operator has typed CLEAN, precisely so a target that changed under the
        prompt is noticed. A cached answer silently defeats that, and the
        exporter cannot detect it — this is documentation, not a check.

    WHAT YES MEANS

      holds(renders) YES means: this exact content is at the destination, at
      this exact size. Size equality and a whole-name match are the
      implementation's to enforce now. They used to be enforced here: a bare
      endswith let `sometrip_X.mp4` at the destination vouch for `trip_X.mp4`
      on disk, which is how a render read as published while its only other
      copy was the workspace about to be erased. Match on a path boundary, and
      compare sizes.
    """

    # -- who answered ------------------------------------------------------
    def name(self) -> str:
        """A label for the operator, on the menu row and in the erase banner.

        Defaults to the class name so attribution works even when the
        implementer never thought about it.
        """
        return type(self).__name__

    def describe(self) -> str:
        """One line for item 6's menu row: what building means for this target."""
        return "Build what %s publishes, from the renders." % self.name()

    # -- 1. do the work ----------------------------------------------------
    @abstractmethod
    def build(self, world, ui: Ui) -> Report:
        """Item 6: produce whatever this target publishes, from the renders."""

    @abstractmethod
    def upload(self, world, ui: Ui) -> Report:
        """Item 7: put it online. One job; how many transports it takes is
        yours, not the menu's."""

    # -- 2. may it run, and would it do anything ---------------------------
    @abstractmethod
    def why_not_build(self, world) -> Optional[str]:
        """None, or one reason in the operator's terms.

        About the TARGET only. Not about ordering, and not about the local
        workspace — the exporter answers those itself and blocks on its own.
        """

    @abstractmethod
    def why_not_upload(self, world) -> Optional[str]:
        """None, or one reason in the operator's terms."""

    @abstractmethod
    def owes(self, renders) -> Owed:
        """Which of these would upload() still act on.

        Distinct from holds() on purpose: a render the target deliberately does
        NOT carry (curated away, excluded) is neither held nor owed. Collapsing
        the two makes a deliberately-absent trip look like an unfinished upload
        forever, or makes an absent trip vouch for itself. Both have happened.
        """

    # -- 3. what is already at the destination -----------------------------
    @abstractmethod
    def holds(self, renders) -> Answers:
        """Is this exact render present at the destination, at this size?"""

    @abstractmethod
    def published(self, renders) -> Answers:
        """Is this render actually being SERVED — not merely stored?"""

    @abstractmethod
    def carries(self, trip_ids) -> Answers:
        """Is there anything at the destination for these trips, by trip id?

        Asked by Exclude Trip about a trip whose render may not exist locally
        at all — the published-then-cleaned-up case. holds() cannot answer it:
        there is no local Render to key on, and no size to compare. Keyed on
        the trip's base id, e.g. "trip_2026-07-24_16-16_02".
        """

    # -- 4. telling the target something -----------------------------------
    def dropped(self, trip_ids, ui: Ui) -> None:
        """These trips were removed ON PURPOSE — not cleaned up after publishing.

        The two are indistinguishable from the outside: id in the previous
        index, nothing on disk. Only the moment of dropping knows which it was,
        so it is said here or never. Default: this target has nothing to record.
        """
        return None

    def status_lines(self) -> Tuple[str, ...]:
        """Rows for the startup status screen. May touch the network — called
        once at launch, never in the menu loop. Default: say nothing."""
        return ()


# ---------------------------------------------------------------------------
# Loading one
# ---------------------------------------------------------------------------

class UploaderNotLoaded(Exception):
    """A configured uploader that will not load stops the tool.

    Never caught into a fallback. Silently becoming the local edition is how
    someone's renders quietly stop being published — the menu would look
    normal, item 6 would write a local page, and item 8 would go on refusing
    for a reason that reads like a network problem.
    """


def load_uploader(spec: str, exporter_dir: Path):
    """"<file>:<Class>" -> a ready instance. Raises, or returns; never None.

    A FILE PATH rather than a dotted module name, because the implementation
    lives in a repo that is not installed and must not have to be. A path is
    what the operator already types for every other location this tool knows,
    and when it is wrong the error names his file instead of Python's search
    path. A module name plus a PYTHONPATH-shaped second key would be two
    settings for one answer, which is two ways to disagree.
    """
    path, _, class_name = spec.partition(":")
    module = _import_file(Path(path).expanduser(), exporter_dir)
    return _instantiate(_checked(_class_from(module, class_name), spec), spec)


def _import_file(path: Path, exporter_dir: Path):
    found = importlib.util.spec_from_file_location("dashcam_uploader", path)
    return _executed(_found(found, path), exporter_dir)


def _found(found, path):
    # Both halves in one test: spec_from_file_location returns None for a
    # missing file and a spec with no loader for something that is not a module.
    if None in (found, getattr(found, "loader", None)):
        raise UploaderNotLoaded("no Python module at %s" % path)
    return found


def _executed(found, exporter_dir: Path):
    """Run the implementation's module with this repo importable.

    It has to import `uploader` to subclass the interface, and it lives in a
    directory Python has never heard of. Prepended explicitly rather than
    relying on sys.path[0] being the exporter, so it stays true however the
    tool was started.
    """
    module = importlib.util.module_from_spec(found)
    sys.path.insert(0, str(exporter_dir))
    try:
        found.loader.exec_module(module)
    except Exception as e:
        raise UploaderNotLoaded("%s failed to import: %s" % (found.origin, e))
    finally:
        _unshadow(sys.path, str(exporter_dir))
    return module


def _unshadow(paths, entry):
    if paths[:1] == [entry]:
        paths.pop(0)


def _class_from(module, class_name: str):
    found = getattr(module, class_name, None)
    if found is None:
        raise UploaderNotLoaded("%s has no class %s" % (module.__file__, class_name))
    return found


def _checked(cls, spec: str):
    """A SHAPE check, not a trust check.

    What the implementation does is its own business — that is settled. What it
    can ANSWER is the exporter's business, because an implementation missing
    published() would otherwise raise at the moment item 8 asks: after the
    operator has typed CLEAN, mid-way through a destructive item. Fail at
    startup instead.
    """
    if not _implements(cls):
        raise UploaderNotLoaded("%s is not a WebsiteUploaderInterface" % spec)
    return cls


def _implements(cls) -> bool:
    return isinstance(cls, type) and issubclass(cls, WebsiteUploaderInterface)


def _instantiate(cls, spec: str):
    # ABC refuses to instantiate a subclass with an abstractmethod left
    # unimplemented. That TypeError names the missing methods, which is exactly
    # the message the operator needs, so it is re-raised rather than described.
    try:
        return cls()
    except Exception as e:
        raise UploaderNotLoaded("%s could not be constructed: %s" % (spec, e))


def origin_of(spec: str, instance) -> str:
    """Who answered, and where he came from — for the erase banner.

    Composed from the SPEC, not from the instance, so it cannot go stale the
    way a copy-pasted NAME constant does. This is attribution, not a safeguard:
    if footage is gone and the answer was wrong, the operator reads which
    implementation gave the answer he acted on from the tool rather than from
    memory.
    """
    return "%s (%s)" % (instance.name(), spec)
