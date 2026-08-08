"""The seam an outside publisher implements: one act of publishing work, twice.

The exporter builds a website at item 5 and puts it online at item 7. What
those two mean — a static page in a folder, an S3 sync and an rsync, a Docker
push, an FTP session — is nobody's business here. So this module declares the
shape of ONE act, and a plugin supplies two of them: a builder for item 5 and
an uploader for item 7.

An act answers what a menu item already answers, and nothing more:

    describe()            what the menu row says about this act
    evaluate(workspace)   may it run — GO, SATISFIED, or BLOCKED with a reason
    execute(workspace, includeVideos=False)
                          do it, optionally including heavyweight media

The uploader answers one question beyond that, because it is the half that
owns the connection to the destination:

    is_complete(trip_ids) -> Evidence   are ALL of these trips at the far end

NOTHING IS REGISTERED AND NOTHING CALLS BACK. The exporter calls; the plugin
returns. Both acts are offered from everywhere in the cycle and each one
answers for itself through evaluate(), so a readiness callback would have no
work to do.

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
import subprocess
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
from dashcam_exporter.domain.menu.menu import (Evidence, Outcome, Ruling, Verdict, blocked,   # noqa: F401
                  did, go, satisfied, stopped)
from dashcam_exporter.domain.model.world import Render, TripMeta              # noqa: F401


# ---------------------------------------------------------------------------
# What the exporter lends an implementation while it works
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The one shape a progress line's left-hand side takes
# ---------------------------------------------------------------------------

# Fixed columns, so the figures sit still while they change. A field that grows
# from "9 MB" to "10 MB" and shifts everything after it is a line the eye has
# to re-read on every redraw.
NAME_W, RATE_W, SIZE_W = 23, 10, 9


def human_bytes(n) -> str:
    """Decimal, the way df and the label on the disk report it.

    Powers of ten, not two. These figures are compared against what the Finder
    and df say about the same card, and 1024-based units read seven per cent
    small at gigabyte scale.
    """
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "?"


def progress_note(name="", rate="", done=None, total=None, tail="") -> str:
    """`<what>   <rate>   <done>/<total>   <tail>` — every bar's left-hand side.

    What is moving, how fast, how far through. The bar and the percentage go
    on the right of it, so a render, a copy and an upload all read the same
    way and the eye learns one line rather than three.

    Anything absent is left out rather than blanked, so a step with no rate to
    report does not print a hole where one would be.
    """
    parts = []
    if name:
        parts.append(_fitted(str(name), NAME_W))
    if rate:
        parts.append(_right(str(rate), RATE_W))
    if done is not None and total is not None:
        parts.append("%s/%-*s" % (_right(human_bytes(done), SIZE_W),
                                  SIZE_W, human_bytes(total)))
    if tail:
        parts.append(str(tail))
    return "   ".join(parts)


_ORDINALS = {1: "st", 2: "nd", 3: "rd"}


def last_change(where) -> str:
    """When a checkout last changed, as "4th of August 2026". "" without git.

    Here rather than in the exporter because a plugin's `i` rows want the same
    sentence about ITS repository, and a second implementation of "spell the
    ordinal" is a second thing to get wrong on the 11th, 12th and 13th.
    """
    try:
        p = subprocess.run(["git", "log", "-1", "--format=%cd",
                            "--date=format:%d %B %Y"],
                           cwd=str(where), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return _spelled(p.stdout.strip()) if p.returncode == 0 else ""


def checkout_version(where, major: int) -> str:
    """`major`.hundreds.remainder off the commit count. "" without git.

    The major is a decision and stays yours; the other two are the history, so
    they cannot drift from what is actually installed the way a hand-kept
    constant does.
    """
    try:
        p = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(where),
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    count = p.stdout.strip()
    if p.returncode != 0 or not count.isdigit():
        return ""
    return "%d.%d.%d" % (major, int(count) // 100, int(count) % 100)


def _spelled(stamp: str) -> str:
    """"04 August 2026" -> "4th of August 2026"."""
    parts = stamp.split(" ", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return stamp
    day = int(parts[0])
    # 11th, 12th and 13th, the three the last digit gets wrong.
    suffix = "th" if 11 <= day % 100 <= 13 else _ORDINALS.get(day % 10, "th")
    return "%d%s of %s" % (day, suffix, parts[1])


def _fitted(text, width):
    if len(text) > width:
        return text[:width - 1] + "\u2026"
    return "%-*s" % (width, text)


def _right(text, width):
    return "%*s" % (width, text)


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

    def info(self, line: str) -> None:
        """An informational callback, recorded without requiring a print."""
        self.say(line)

    def debug(self, line: str) -> None:
        """A detailed progress callback, normally kept out of the display."""
        return None

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


    def reset(self) -> None:
        """The workspace changed under you. Anything you were holding is stale.

        Called after any menu item that actually did something — an import
        brought a new card in, a trip was dropped, the working area was erased.
        You cannot see those: they are this machine's business and they happen
        without going anywhere near you. But they change what your destination
        should be asked about, and an answer cached across one of them is about
        trips that may no longer exist.

        The default does nothing, because a plugin that goes and looks every
        time has nothing to forget. Implement it if you cache — which you may
        well want to, since the exporter asks on every capture and does not
        presume to know what that costs you. Your own build and upload are
        yours to notice; this is for everything else.

        It must not raise and must not be slow: it is called on the way past a
        step the operator is watching finish.
        """


    def get_website_upload_description(self) -> str:
        """What handing the context over to you amounts to, in your own words.

        Printed verbatim under `h 5`, after the two sentences that explain the
        choice between building locally and handing over. Those two are the
        exporter's to write because the choice is the same everywhere; this is
        not. Where the site ends up, what it is called, which of it is public,
        what a second run does differently — the exporter cannot know any of
        it, and an operator reading the help for the step that publishes their
        driving deserves better than "it is handed to a plugin".

        Paragraphs separated by a blank line, wrapped to the terminal by the
        caller. A line beginning with whitespace is left exactly as written, so
        a path stays on one line and stays copy-pasteable.

        Not abstract, for the same reason as holds() above: an implementation
        written before this existed must keep working, and "" simply means the
        sentence ends there.
        """
        return ""

    def get_plugin_info(self) -> "Tuple[Tuple[str, str], ...]":
        """Label/value rows for the `i` screen, under a section of your own.

        Whatever an operator would need in order to report a bug against you
        rather than against the exporter: a name, where the file is, who wrote
        it, a licence, a version, when it last changed. Rows rather than a
        block of prose because they are printed beside the exporter's own,
        aligned in the same two columns.

        Order is yours and is preserved. Rows are printed as given, so leave
        out what you have no answer for rather than filling it in with a dash
        -- an absent row reads as "not applicable", a dash reads as "broken".

        Asked of the UPLOADER first and the builder only if that says nothing,
        the same way the plugin's name is taken: they are two halves of one
        arrangement and describing it twice is two places to go stale.

        Not abstract, for the same reason as everything else here.
        """
        return ()


class Builder(Act):
    """Item 5: produce whatever this installation publishes, from the trips
    the exporter has described.

    It stages a site; it never speaks to the far end. That is why it is not
    the half that answers is_complete().
    """

    def holds(self) -> Optional[int]:
        """How many trips the built result contains, or None if you cannot say.

        Asked immediately BEFORE and AFTER execute(), so the exporter can
        report what the build changed rather than only what it now amounts to.
        "35 trips" says nothing about whether this run added one or rebuilt the
        same thirty-five; "1 new, 35 total" is the sentence an operator reads
        to know the run did something.

        A count of the RESULT, not of the workspace. What the exporter handed
        over is already known to it; what it cannot see is how much of that
        reached the thing you build, and whether anything you were carrying
        forward from a previous build is still in there.

        Not abstract, and deliberately so: this is a published interface, and
        an implementation written before the question existed must keep
        working. None means "no answer", and the exporter then says what it
        always said.
        """
        return None


class Uploader(Act):
    """Item 7: put what was built online. One job, however many transports.

    How many it takes — a bucket and then a server, one rsync, a copy into a
    folder — is yours, not the menu's. What the exporter needs is one answer
    about one destination, and an act that can leave half of that true is an
    act that publishes nothing while reporting success.

    TWO RULES ABOUT THE ORDER, IF YOUR DESTINATION HAS PAGES AND MEDIA.

    PAGES FIRST, MEDIA AFTER. A trip is publishable as soon as it is
    DESCRIBED: its route, its distance, its places and its map all come from
    the sidecars item 2 writes, and only playback waits on the encode. Sending
    the media first means that for the whole of that upload — which is hours
    on a card's worth of video — the destination shows nothing at all, and
    everything appears at once when the last byte of the last file lands.
    Reversed, the pages are up in a minute and each drive starts playing as its
    own media arrives behind it.

    AND MISSING MEDIA IS NOT A FAILURE. Item 5 is reachable before item 6 by
    design, so a publish with nothing encoded yet is the ORDINARY first move
    and not an odd one. It completes, and it says what it did: pages sent, no
    media to send. An act that errors there fails the whole publish after the
    pages have already gone up, and reports a failure for a run that did
    exactly what was asked of it.

    That is what makes the exporter's own gate safe to leave alone. Publishing
    pages moves nothing at the destination as far as is_complete() is
    concerned: a trip with no media there is still NO, so Clean Workspace stays
    shut over footage whose only copy is local. Nothing about publishing early
    may soften that answer.
    """

    @abstractmethod
    def execute(self, workspace: Workspace, includeVideos: bool = False) -> Outcome:
        """Deploy the website, optionally uploading video media as well.

        ``includeVideos`` defaults to false so a page-only deploy is cheap and
        safe; callers that explicitly want the media transport pass ``True``.
        """

    @abstractmethod
    def is_complete(self, trip_ids, progress=None) -> Evidence:
        """Are ALL of these trips at the destination, published and reachable?

        ``progress`` is an optional callback accepting a short status string;
        call it while doing a slow remote check so the exporter can keep its
        waiting bar informative. Implementations may ignore it.

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

        THE SAME STATE MUST GIVE THE SAME ANSWER, however many times it is
        asked. That is the whole contract, and it is what lets the exporter ask
        whenever it needs to know instead of rationing the question.

        Which means you may cache — you are expected to, if going and looking
        is expensive, because nothing out here presumes to know what it costs
        you. It means the cache must be wrong for no longer than the state is.
        Your own build and upload you can see. Everything else that changes
        what you would be asked about — an import landing, a trip dropped, the
        working area erased — arrives as reset(), because none of it goes
        anywhere near you.

        What the exporter will NOT do is reach past your cache. It asks a
        second time before an erase, after the confirmation word, and takes
        that answer as the state's. If your destination changed under you and
        you said yes from memory, that is a wrong answer rather than a question
        asked badly.

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
    normal, item 5 would write a local page, and item 8 would go on refusing
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

    def reset(self) -> None:
        """Tell both acts. One arrangement, one notification."""
        self.builder.reset()
        self.uploader.reset()

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

    @property
    def info(self) -> "Tuple[Tuple[str, str], ...]":
        """The rows this plugin wants on the `i` screen, or empty.

        The uploader answers, and the builder only if it said nothing — one
        arrangement, described once. Never raises: `i` is a screen that prints
        who to complain to, and it failing is the one thing that would leave
        nobody to complain to.
        """
        for act in (self.uploader, self.builder):
            try:
                rows = tuple(act.get_plugin_info())
            except Exception:
                continue
            if rows:
                return rows
        return ()


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
            "upload_plugin reads <file.py>:<BuilderClass>:<UploaderClass>,"
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
    # Existing third-party plugins may still spell the public interface as
    # ``from uploader import ...``. Keep that import working at load time
    # without maintaining a source-level shim in this project.
    previous_uploader = sys.modules.get("uploader")
    sys.modules["uploader"] = sys.modules[__name__]
    sys.path.insert(0, str(exporter_dir))
    try:
        found.loader.exec_module(module)
    except Exception as e:
        raise UploaderNotLoaded("%s failed to import: %s" % (found.origin, e))
    finally:
        _unshadow(sys.path, str(exporter_dir))
        if previous_uploader is None:
            sys.modules.pop("uploader", None)
        else:
            sys.modules["uploader"] = previous_uploader
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
