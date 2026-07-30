"""The seam an outside publisher implements: one act of publishing work, twice.

The exporter builds a website at item 6 and puts it online at item 7. What
those two mean — a static page in a folder, an S3 sync and an rsync, a Docker
push, an FTP session — is nobody's business here. So this module declares the
shape of ONE act, and a plugin supplies two of them: a builder for item 6 and
an uploader for item 7.

An act answers what a menu item already answers, and nothing more:

    describe()            what the menu row says about this act
    evaluate(workspace)   may it run — GO, SATISFIED, or BLOCKED with a reason
    execute(workspace)    do it, and say what happened

The uploader answers one question beyond that, because it is the half that
owns the connection to the destination:

    is_complete(trip_ids) -> Evidence   are ALL of these trips at the far end

NOTHING IS REGISTERED AND NOTHING CALLS BACK. The exporter calls; the plugin
returns. The graph in menu.py already decides when each act may run — item 7 is
unreachable until item 6 has run, and item 6 is unreachable until there is
something to build from — so a readiness callback would have no work to do.

THE ONE CONDITION OF TRUST: AN IMPLEMENTATION READS THE WORKSPACE AND NEVER
MODIFIES IT. No move, no rename, no delete, no writing into the output tree.
Copy what you need somewhere of your own and work there. This is not policed
and will not be: whoever installs an implementation owns what it does. The
reason it matters is mechanical rather than moral — the renders and sidecars
handed over are the same files the exporter's own guards then reason about, and
items 4, 8 and 9 erase footage on the strength of that reasoning. A mv would
leave those gates judging a workspace that moved under them.

An implementation is otherwise INSIDE the trust boundary and the exporter
believes what it says. What it does NOT delegate is a question it can answer
itself: "were these trips rendered on this machine" never leaves home, so an
implementation that answers YES to everything still cannot talk Clean Workspace
into erasing an import that produced no renders.

The answer that must never be inventable is "could not find out". UNKNOWN is
what an unreachable destination says, and unreachable is not permission.
"""

from __future__ import annotations

import importlib.util
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# Re-exported on purpose. An implementation has to say GO / SATISFIED / BLOCKED
# and "this ran, here is what happened", and the exporter already has one
# vocabulary for both. A second set of types meaning the same three states is
# two names for one thing, which is exactly the mess these are here to avoid —
# so `from uploader import Verdict, go, blocked, did` gets the exporter's own
# types, not copies of them.
from menu import (Evidence, Outcome, Ruling, Verdict, blocked,   # noqa: F401
                  did, go, satisfied, stopped)
from world import Render, TripMeta                               # noqa: F401


# ---------------------------------------------------------------------------
# What the exporter lends an implementation while it works
# ---------------------------------------------------------------------------

class Ui(ABC):
    """The exporter's output, lent to an implementation for one call.

    So an act's work looks like the rest of the tool — same progress bar, same
    colours — without importing pipeline internals that will move. An
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
# The reduced state an act is handed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Workspace:
    """Where the material is. Everything an act needs, and nothing else.

    Deliberately NOT the exporter's World: that carries card facts, ledger
    marks and the destination's own answers, none of which an implementation
    has any business reading, and all of which move as this repo changes.

    READ THIS, NEVER CHANGE IT. Copy out what you need and build somewhere of
    your own. The files named here are the same files the exporter's erase
    gates reason about; moving or renaming one leaves those gates judging a
    workspace that shifted under them.

      out_dir      the output tree: renders and sidecars live under here
      import_dir   the import being worked on, or None when none is picked
      renders      every rendered mp4 in the tree, by bare name and exact size
      metas        the sidecars, each with its trip id and its span
      trip_ids     the trips THIS IMPORT contains — including any that produced
                   no render, which is the point of it: a trip nobody encoded
                   is still a trip the destination must be asked about
      dropped_ids  trips deleted ON PURPOSE, ever, in this workspace. An
                   implementation that rebuilds an index from a previous one
                   must not carry these forward: a dropped trip and a
                   published-then-cleaned-up trip look identical from the
                   outside, and this is the only record of which is which
      offline      the operator says this machine is on a bad connection
      ui           the exporter's output for the duration of this call
    """

    out_dir: Optional[Path] = None
    import_dir: Optional[Path] = None
    renders: Tuple[Render, ...] = ()
    metas: Tuple[TripMeta, ...] = ()
    trip_ids: Tuple[str, ...] = ()
    dropped_ids: Tuple[str, ...] = ()
    offline: bool = False
    ui: Optional[Ui] = None


# ---------------------------------------------------------------------------
# The interface: one act, and the uploader's extra question
# ---------------------------------------------------------------------------

class Act(ABC):
    """One unit of publishing work — the shape a menu item already has.

    WHAT AN IMPLEMENTATION MAY ASSUME

      * Ordering is the graph's job. Never ask "has the build run yet" — an
        act is unreachable out of order by construction. If your upload needs
        an artefact and there is none, that is a BLOCKED verdict ABOUT THE
        ARTEFACT, not about a step.
      * evaluate() is asked on EVERY MENU DRAW. It must be cheap and local: no
        network, no subprocess. is_complete() is the question that may leave
        the machine, and it is asked at most a few times per dispatch.
      * evaluate() must not print. Its answer is what the menu row is drawn
        from, forty times a session.
      * execute() may take as long as it takes and may print through
        `workspace.ui`.
    """

    @abstractmethod
    def describe(self) -> str:
        """One line for the menu row: what this act does, on this machine."""

    @abstractmethod
    def evaluate(self, workspace: Workspace) -> Verdict:
        """May this run, and would it do anything.

            go()               there is work and nothing is in the way
            satisfied(reason)  the postcondition already holds; the exporter
                               moves on without calling execute()
            blocked(reason)    one reason, in the operator's terms, about THIS
                               act — never about ordering, and never about the
                               local workspace, which the exporter judges itself
        """

    @abstractmethod
    def execute(self, workspace: Workspace) -> Outcome:
        """Do it, once, and say what happened.

            did(note)      it ran and the postcondition now holds
            stopped(note)  it did not; the pipeline stays where it was

        `completed` is all the menu reads: it decides whether the position
        advances.
        """


class Builder(Act):
    """Item 6: produce whatever this installation publishes, from the renders.

    It stages a site; it never speaks to the far end. That is why it is not
    the half that answers is_complete().
    """


class Uploader(Act):
    """Item 7: put what was built online. One job, however many transports.

    How many it takes — a bucket and then a server, one rsync, a copy into a
    folder — is yours, not the menu's. What the exporter needs is one answer
    about one destination, and an act that can leave half of that true is an
    act that publishes nothing while reporting success.
    """

    @abstractmethod
    def is_complete(self, trip_ids) -> Evidence:
        """Are ALL of these trips at the destination, published and reachable?

        ALL OR NOTHING, and that is deliberate. The one thing that acts on this
        answer — Clean Workspace — erases the WHOLE working area, never a
        chosen trip, so a per-trip map would answer a question nobody asks and
        every caller would fold it down to this anyway.

        The list is the EXPORTER'S idea of the import, not yours. A trip that
        was never encoded is still in it, you do not have it, and the honest
        answer is therefore NO — which is exactly the case that keeps its
        footage from being erased. "Do you have everything you were given"
        would not close that, because you were never given the trip that was
        never rendered.

            YES      every one of them is there, and being served
            NO       at least one is not
            UNKNOWN  the destination could not be reached, or could not say.
                     Not "no", and never "yes" — the next thing the operator
                     does is erase the only local copy of the footage
            NA       this question does not arise for this destination at all.
                     NA REMOVES the erase gate rather than failing it, so use
                     it only when the question is genuinely meaningless here
                     (an archive disk that stores and does not serve). Never
                     for something that merely could not be checked today —
                     that is UNKNOWN, and it fails closed.

        Do not memoise. It is asked again after the operator has typed CLEAN,
        precisely so a destination that changed under the prompt is noticed. A
        cached answer silently defeats that, and the exporter cannot detect it.

        AND THE STATE IS YOURS ACROSS SESSIONS. The exporter persists nothing
        about your destination: not what was built, not what was uploaded, not
        this answer. The four files it keeps in the working area are its own
        business — where the card import reached, which trips the operator
        dropped, the owner marker and the lock — and none of them describes a
        destination. So every session starts by asking you, with no memory of
        the last one, and a plugin that only knows what it did since it was
        constructed will answer wrongly the moment the tool is restarted.

        Best is not to remember at all: go and look, the way the shipped
        example lists its destination directory on every call. Then a site
        someone changed while the tool was closed is seen, and there is no
        record to fall out of step with reality. If your destination is too
        expensive to interrogate and you must keep a record, keep it in YOUR
        storage — beside the destination, or wherever the arrangement belongs.
        Never in the exporter's working area: writing there breaks the one
        condition asked of an implementation, and the erase gates reason about
        those very files.

        Nor is execute()'s return value a substitute for being asked: it says
        what happened at the time it ran, and the world moves between then and
        the irreversible act.
        """


# ---------------------------------------------------------------------------
# Loading one
# ---------------------------------------------------------------------------

class UploaderNotLoaded(Exception):
    """A configured plugin that will not load stops the tool.

    Never caught into a fallback. Silently becoming the local edition is how
    someone's renders quietly stop being published — the menu would look
    normal, item 6 would write a local page, and item 8 would go on refusing
    for a reason that reads like a network problem.
    """


@dataclass(frozen=True)
class Plugin:
    """One configured plugin: the two acts it supplies, and where it came from.

    Two classes rather than one with build_/upload_ pairs, because they are two
    menu items with two verdicts and two outcomes. One plugin rather than two
    settings, because they are one arrangement: a builder that stages for a
    destination the uploader does not send to is not a thing anyone wants.
    """

    builder: Builder
    uploader: Uploader
    spec: str

    @property
    def name(self) -> str:
        """The label the operator reads on the erase banner.

        The uploader's class name, because that is the half that answers about
        the destination the erase rests on. Derived rather than asked for: a
        NAME the implementation supplies is a second place to say what the
        class already says, and it goes stale after a rename.
        """
        return type(self.uploader).__name__

    @property
    def origin(self) -> str:
        """Who answered, and where he came from — for the erase banner.

        Composed from the SPEC, so it cannot go stale the way a copy-pasted
        constant does. This is attribution, not a safeguard: if footage is gone
        and the answer was wrong, the operator reads which implementation gave
        the answer he acted on off the tool rather than out of memory.
        """
        return "%s (%s)" % (self.name, self.spec)


def load_plugin(spec: str, exporter_dir: Path) -> Plugin:
    """"<file>:<BuilderClass>:<UploaderClass>" -> both, ready. Raises, or returns.

    A FILE PATH rather than a dotted module name, because the implementation
    lives in a repo that is not installed and must not have to be. A path is
    what the operator already types for every other location this tool knows,
    and when it is wrong the error names his file instead of Python's search
    path.

    Both class names in one setting, because one plugin is one arrangement.
    Two settings would be two places to disagree about which pair is installed.
    """
    path, builder_name, uploader_name = _parts(spec)
    module = _import_file(Path(path).expanduser(), exporter_dir)
    return Plugin(_ready(module, builder_name, Builder, spec),
                  _ready(module, uploader_name, Uploader, spec), spec)


def _parts(spec: str):
    parts = spec.split(":")
    if len(parts) != 3:
        raise UploaderNotLoaded(
            "website_uploader reads <file.py>:<BuilderClass>:<UploaderClass>,"
            " not %r" % spec)
    return parts


def _ready(module, class_name: str, base, spec: str):
    return _instantiate(_checked(_class_from(module, class_name), base, spec),
                        class_name, spec)


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


def _checked(cls, base, spec: str):
    """A SHAPE check, not a trust check.

    What the implementation does is its own business — that is settled. What it
    can ANSWER is the exporter's business, because a plugin whose uploader has
    no is_complete() would otherwise raise at the moment item 8 asks: after the
    operator has typed CLEAN, mid-way through a destructive item. Fail at
    startup instead.
    """
    if not _implements(cls, base):
        raise UploaderNotLoaded("%s: %s is not a %s"
                                % (spec, getattr(cls, "__name__", cls), base.__name__))
    return cls


def _implements(cls, base) -> bool:
    return isinstance(cls, type) and issubclass(cls, base)


def _instantiate(cls, class_name: str, spec: str):
    # ABC refuses to instantiate a subclass with an abstractmethod left
    # unimplemented. That TypeError names the missing methods, which is exactly
    # the message the operator needs, so it is re-raised rather than described.
    try:
        return cls()
    except Exception as e:
        raise UploaderNotLoaded("%s: %s could not be constructed: %s"
                                % (spec, class_name, e))
