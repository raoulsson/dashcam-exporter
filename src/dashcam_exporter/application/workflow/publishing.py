"""Publishing collaborators for the dashcam exporter.

This module owns the boundary between the exporter and an optional publishing
plugin.  Local builds, plugin builds, uploads, and the no-publisher edition
share one small collaborator protocol while keeping orchestration out of the
interactive pipeline.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from dashcam_exporter.domain.menu import guards, menu
from dashcam_exporter.application.ports import uploader

from dashcam_exporter.application.workflow.results import ABORTED, RAN, SATISFIED, record
from dashcam_exporter.application.ui.term import C, tilde
from dashcam_exporter.infrastructure.runtime.runtime import Child

def __getattr__(name):
    # Pipeline owns shared orchestration helpers; defer lookup to avoid an
    # import cycle while keeping this module independently importable.
    if name in {"NAME", "BUILD", "UPLOAD", "_outcome", "_handed_over",
                "_holds", "_long_description", "_with_the_count",
                "_delta_words", "_logged", "_closed_on", "_status_of",
                "_did_or_settled", "step_site", "gather_into_final"}:
        from dashcam_exporter.application.workflow import pipeline
        return getattr(pipeline, name)
    raise AttributeError(name)


def _pipeline(name):
    """Resolve shared orchestration helpers lazily, avoiding import cycles."""
    from dashcam_exporter.application.workflow import pipeline
    return getattr(pipeline, name)

class Console(uploader.Ui):
    """The exporter's output, lent to an implementation while it works.

    So a target's build and upload look like the rest of the tool — same
    progress bar, same colours — without it importing pipeline internals that
    will move. Nothing here is a restriction: an implementation is free to
    print() and to run its own subprocesses, and several do. This exists to
    make the nice thing the easy thing.
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def say(self, line):
        print(C.dim(line))

    def warn(self, line):
        print(C.yellow(line))

    def run(self, cmd, cwd, label, env=None, parser=None):
        # quiet_finish: the plugin says what its act achieved through the
        # Outcome it returns. Without it every plugin child left the
        # streamer's own "Deploy [####] 100% 0:34  completed" behind, which is
        # this module announcing somebody else's work in its own words.
        from dashcam_exporter.application.workflow.pipeline import run_stream, Readout
        rc, _lines = run_stream(Child(cmd, cwd, env=env),
                                Readout(label, parser, quiet_finish=True))
        return rc


def _handed_over(ctx, world):
    """The World, reduced to what an act needs to find the material.

    Not the World itself: that carries card facts, ledger marks and the
    destination's own answers, which an implementation has no business reading
    and which would couple it to internals that move.
    """
    return uploader.Workspace(
        out_dir=world.out_dir, import_dir=world.selected_import,
        renders=world.renders, metas=world.metas, trip_ids=world.trip_ids,
        dropped_ids=world.dropped_ids, offline=world.offline, ui=Console(ctx))


class PublishingCollaborator(ABC):
    """What items 5 and 7 install as their body, declared rather than assumed.

    NOT uploader.Act, and the difference is the whole reason this exists. An
    Act is handed a Workspace — the reduced, plugin-facing view — and knows
    nothing about this repo. A collaborator is handed the exporter's own
    World, because two of the four are the exporter doing the job itself and
    the other two have to translate before an Act ever sees anything. Naming
    them Acts would let a LocalPage pass load_plugin's shape check while being
    unable to accept the one argument a plugin is ever given.

    So this is the exporter's INTERNAL seam, and every method on it is
    abstract. That is safe here in a way it is not in uploader.py: the only
    implementations are the four below, all in this file, and a method added
    to the seam should stop the tool at construction rather than at the moment
    an operator presses a key. It already did the other thing once — a long
    description was added to the interface, all four collaborators silently
    lacked it, and `h 5` and `h 7` raised AttributeError on every install.
    """

    @abstractmethod
    def describe(self):
        """One line for the menu row: what THIS installation's step does.

        Read on every draw, so it must be cheap and must not go looking at
        anything. It is the entry answering for its own job rather than the
        session restating how the tool was installed, which is why it differs
        between the two editions instead of being a constant in items.py.
        """

    @abstractmethod
    def evaluate(self, world):
        """May the step run against this World, and would it do anything.

        The exporter's own guards have already had their say by the time this
        is asked; what comes back is about the collaborator's half of the job.
        GO, SATISFIED and BLOCKED all mean here what they mean everywhere else
        in the menu, and the item passes the answer through verbatim.

        Asked on every menu draw, so no network and no subprocess. Anything
        that has to leave the machine belongs behind capture_world's FULL
        scope, which asks once per dispatch and freezes what came back.
        """

    @abstractmethod
    def execute(self, world):
        """Do the step, once, and say what happened as an Outcome.

        `completed` is all the runner reads: it decides whether the position
        advances or the pipeline stays where it was. Whatever this prints is
        the operator's whole picture of the step, since the menu adds nothing
        beyond the outcome's note.
        """

    @abstractmethod
    def get_website_upload_description(self):
        """What this step amounts to on this installation, for `h 5` and `h 7`.

        Printed under the exporter's own two sentences and attributed, so a
        reader can tell which prose this repo is answerable for. Return "" when
        there is nothing to add — the local edition's collaborators do, because
        the exporter has already said everything true about them — and the help
        screen then simply ends where it ended before.

        It must not raise. This is the one screen in the tool that exists to
        tell an operator who to complain to.
        """

    @abstractmethod
    def plugin_name(self):
        """Whose words those were, or None when they are the exporter's own.

        The help screen attributes a quoted paragraph to the thing that said
        it. None means there is nothing to attribute — no plugin is configured
        — and the help screen prints no attribution line rather than crediting
        an empty quotation to "the plugin".
        """


class LocalPage(PublishingCollaborator):
    """Item 5 under the local edition: write the page, and gather.

    Gathering is what makes the local edition's workspace expendable, so the
    two belong to one job. Under a plugin neither happens here.
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def describe(self):
        return ("Build the local result page from the renders. Nothing leaves "
                "this machine.")

    def get_website_upload_description(self):
        return ""      # nothing is handed over under this edition

    def plugin_name(self):
        return None    # there is no plugin to name

    def evaluate(self, world):
        """Its own renders floor, because THIS build also gathers.

        The shared guard stopped demanding renders so a described trip can be
        published before it is encoded — right for a page, wrong here. execute
        moves whole day directories into final_<day>_<import>, sidecars and
        all, and a gather with nothing rendered would carry the sidecars out
        of the import namespace where _is_described looks for them, leaving
        item 2 with a trip it no longer believes it has described.
        """
        if not guards.renders_exist(world):
            return menu.blocked("no renders on disk to gather")
        return menu.go()

    def execute(self, world):
        return _pipeline("_outcome")(
            _pipeline("step_site")(self._ctx, _pipeline("gather_into_final")))


class TargetBuild(PublishingCollaborator):
    """Item 5 with a plugin configured: whatever its builder builds.

    The local page is not written and gather_into_final does not run. Moving
    the render tree would rename every published trip out from under whatever
    index the plugin keeps, and the page is the other product's deliverable.
    """

    def __init__(self, ctx, act):
        self._ctx = ctx
        self._act = act

    def describe(self):
        return self._act.describe()

    def get_website_upload_description(self):
        return _long_description(self._act)

    def plugin_name(self):
        return type(self._act).__name__

    def evaluate(self, world):
        return self._act.evaluate(_handed_over(self._ctx, world))

    def execute(self, world):
        # Counted on both sides of the act, because the interesting number is
        # the difference. The act's own note says what it did; this says what
        # changed, which is the thing a second press cannot fake.
        before = _holds(self._act)
        outcome = _logged(self._ctx, _pipeline("BUILD"),
                          lambda: self._act.execute(_handed_over(self._ctx, world)))
        return _with_the_count(outcome, before, _holds(self._act))


class TargetPublish(TargetBuild):
    """Item 7 with a plugin configured: one job, however many transports.

    The same three calls as item 5's collaborator against a different act,
    which is the whole point of the acts having one shape — the only thing that
    differs is which step the outcome is logged against.

    Subclassing TargetBuild rather than the seam directly, and it stays sound
    now that TargetBuild has a base: everything inherited — describe,
    evaluate, the long description, the plugin's name — is a bare delegation to
    self._act, and self._act here is the uploader. There is nothing about item
    5 left in any of them to be wrong about item 7.

    It is not an uploader.Uploader either, and deliberately: is_complete() is
    the destination's answer and the exporter asks the ACT for it, in
    capture_world at FULL scope. Routing it through here would put a wrapper
    on the one question the erase gates rest on, for no caller.
    """

    def execute(self, world):
        return _logged(self._ctx, _pipeline("UPLOAD"),
                       lambda: self._act.execute(
                           _handed_over(self._ctx, world), includeVideos=False))


def _holds(act):
    """What the builder says its result contains, or None if it will not say."""
    try:
        return act.holds()
    except Exception:
        return None      # a builder's bookkeeping is not this module's problem


def _long_description(act):
    """An act's own long-form help, or "" if it has none.

    Same shape as _holds above and for the same reason: this is a published
    interface and an act written before the question existed has to keep
    working. getattr rather than hasattr-then-call, because a plugin is free to
    answer it dynamically, and a raise here would take down a HELP SCREEN --
    the one place in the tool nothing should ever fail.
    """
    try:
        said = getattr(act, "get_website_upload_description", lambda: "")()
    except Exception:
        return ""
    return said if isinstance(said, str) else ""


def _with_the_count(outcome, before, after):
    """The act's own words, plus what the build changed.

    Only when the answer is a real one and the act succeeded. A builder that
    cannot count, or a build that failed, says exactly what it said before.
    """
    if after is None or not outcome.completed:
        return outcome
    added = after - before if before is not None else None
    return menu.did("%s, %s" % (outcome.note, _delta_words(added, after)))


def _delta_words(added, total):
    if added is None:
        return "%d trip%s total" % (total, "" if total == 1 else "s")
    if added <= 0:
        return "no new trips, %d total" % total
    return "%d new trip%s added, %d total" % (added, "" if added == 1 else "s", total)


class NoPublisher(PublishingCollaborator):
    """Item 7 under the local edition: no edges, and nothing to run.

    Constructed rather than omitted so that every number means the same thing
    on every installation — a menu that renumbers itself makes every sentence
    anyone writes about "item 6" true only locally.

    A collaborator and NOT an uploader.Uploader, which would demand
    is_complete(). There is no destination here to be complete at, and the two
    answers such a stub could give are both wrong: YES and NA each REMOVE the
    erase gate, and UNKNOWN would put a permanent "could not reach the
    destination" in front of an operator who never configured one. Nothing
    asks — the question goes to ctx.plugin.uploader, which under this edition
    is None, and the local edition's Clean Workspace rests on what is on this
    machine instead.
    """

    def describe(self):
        return "Put what was built online. Not part of this edition."

    def get_website_upload_description(self):
        return ""      # nothing is handed over under this edition

    def plugin_name(self):
        return None    # there is no plugin to name

    def evaluate(self, world):
        """What is missing from THIS installation, never how to change it.

        Which product this is and how to turn the other one on is said once at
        startup and belongs in the README; an item that answered it would be
        describing the machine it runs in rather than the job it does.
        """
        return menu.blocked("not part of this edition")

    def execute(self, world):    # pragma: no cover - unreachable by two rules
        return menu.stopped("publishing is not configured")


def _logged(ctx, number, run):
    """Run an act, and write what it amounted to into this session's log.

    It takes the act rather than its result BECAUSE OF THE CLOCK. Handed the
    outcome, the only start time available is the moment the work already
    finished, so every act logged 0:00 — a fifty-seven second deploy included,
    which is exactly the line an operator would read to find out where a
    session went.

    The log is the exporter's, not the plugin's: a StepResult carries a
    duration and a status the summary and the crash log read, and an
    implementation must not have to construct one. The Outcome itself goes
    back to the item untouched — it is the act's answer, not this module's.
    """
    started = time.time()
    outcome = run()
    record(ctx, _pipeline("NAME")[number], _status_of(outcome), started, outcome.note)
    _closed_on(outcome)
    return outcome


def _closed_on(outcome):
    """The one green line every step ends on, for the two that are a plugin's.

    Every other step closes on `100% - <what it did>`, and these two closed on
    nothing at all: the heading, the description, whatever the child streamed,
    and then the menu again. The operator was left inferring success from the
    absence of red.

    The act's own words, not this module's. It is the only thing that knows
    what it achieved -- how many trips it indexed, how many videos went up --
    and quiet_finish exists precisely so the streamer's "completed" does not
    speak over it. Nothing is printed for an outcome that did not complete: the
    failure has already said more than a summary line could.
    """
    if outcome.completed and outcome.performed and outcome.note:
        _pipeline("done_line")(outcome.note)


def _status_of(outcome):
    """Not completing is not failing.

    An act that stops because the operator did not type the word, or answered
    n, has done exactly what he asked. FAILED is for something going wrong,
    and a red line beside a decision he made teaches him to read past red.
    """
    if not outcome.completed:
        return ABORTED
    return _pipeline("_did_or_settled")(outcome)


def _did_or_settled(outcome):
    if outcome.performed:
        return RAN
    return SATISFIED
