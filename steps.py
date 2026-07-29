"""The step graph: what each menu action needs before it, and what it unlocks.

pipeline.py knows how to DO each step. This module states how they fit together,
in one place and in types, so the ordering can be checked instead of trusted.
Until now the order lived in a scatter of `if` conditions across 4,600 lines, and
the rules drifted from each other because nothing could compare them.

Two things every step declares:

    reachable_from(strategy) -> the steps that can sensibly precede it
    reaches_to(strategy)     -> the steps it unlocks

They are two views of the same edge, so they must agree: if 5 reaches to 7, then
7 is reachable from 5. A test walks every pair and fails on any edge declared
from one side only — the cheapest way to catch a rule that was changed in one
place and not the other.

Both are CONDITIONAL on the strategy, because the tool is two products:

    WEBSITE_REPO           a site repo and a bucket are configured; the cycle
                           ends by uploading and deploying, and the workspace is
                           expendable once both have happened.
    LOCAL_DEFAULT_WEBSITE  neither is configured; the cycle ends by building a
                           self-contained page and gathering it into final_<id>,
                           which is what makes the workspace expendable.

`available(ctx)` answers the other question — not "may this follow that" but
"would it do anything right now" — and returns the reason it would not, or None.
That is the same contract as pipeline.py's NOOP_CHECK, which is where these are
wired in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, Dict, Optional, Protocol, Set


class Strategy(Enum):
    """Which of the two products this installation is."""

    WEBSITE_REPO = "website_repo"
    LOCAL_DEFAULT_WEBSITE = "local_default_website"

    @classmethod
    def of(cls, ctx: "Ctxish") -> "Strategy":
        """A configured site repo AND bucket is the publishing product."""
        configured = (getattr(ctx, "site", None) is not None,
                      bool(getattr(ctx, "s3_bucket", None)))
        return cls.WEBSITE_REPO if all(configured) else cls.LOCAL_DEFAULT_WEBSITE


class Ctxish(Protocol):
    """The parts of pipeline.Ctx a step declaration is allowed to look at.

    Deliberately narrow: a step may ask about configuration and paths, not reach
    into the pipeline's internals. Anything more is a sign the rule belongs in
    the step body rather than in its declaration.
    """

    site: object
    s3_bucket: object
    card: object
    out_dir: object
    render_root: object


# Step numbers, named once so the graph reads as sentences rather than integers.
IMPORT = 1
LIST = 2
PREVIEW = 3
EXCLUDE = 4
RENDER = 5
SITE = 6
UPLOAD = 7
DEPLOY = 8
DELETE_WS = 9
WIPE_SIM = 10


class Step(ABC):
    """One menu action, and where it sits in the order."""

    number: int
    title: str
    destructive: bool = False

    @abstractmethod
    def reachable_from(self, strategy: Strategy) -> Set[int]:
        """Steps that can sensibly precede this one. Empty = an entry point."""

    @abstractmethod
    def reaches_to(self, strategy: Strategy) -> Set[int]:
        """Steps this one unlocks. Empty = a leaf."""

    def available(self, ctx: Ctxish) -> Optional[str]:
        """Why this step would do nothing right now, or None if it would.

        Default: always available. Steps with a precondition override it, and
        pipeline.py's NOOP_CHECK is the single place these are consulted, so a
        rule stated here is the rule the menu enforces.
        """
        return None

    # -- plumbing ----------------------------------------------------------
    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return "<Step %d %s>" % (self.number, self.title)


# ---------------------------------------------------------------------------
# The ten steps. Edges are declared from BOTH sides on purpose: stating each one
# twice is what makes the consistency check possible, and a graph that can only
# be wrong in one place is worth the duplication.
# ---------------------------------------------------------------------------

class ImportFromSim(Step):
    number, title = IMPORT, "Import from SIM"

    def reachable_from(self, s: Strategy) -> Set[int]:
        # An entry point, and also where you come back to after a cycle ends.
        return {DELETE_WS, WIPE_SIM}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {LIST, PREVIEW, WIPE_SIM}


class ListTrips(Step):
    number, title = LIST, "List trips"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {IMPORT}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {PREVIEW}


class PreviewTrips(Step):
    number, title = PREVIEW, "Preview trips"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {IMPORT, LIST}

    def reaches_to(self, s: Strategy) -> Set[int]:
        # Preview writes the sidecars, which is what everything downstream reads.
        return {EXCLUDE, RENDER}


class ExcludeTrip(Step):
    number, title, destructive = EXCLUDE, "Exclude trip", True

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {PREVIEW}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {RENDER}


class RenderVideos(Step):
    number, title = RENDER, "Render videos"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {PREVIEW, EXCLUDE}

    def reaches_to(self, s: Strategy) -> Set[int]:
        if s is Strategy.WEBSITE_REPO:
            return {SITE, UPLOAD}
        return {SITE}


class CreateWebsite(Step):
    number, title = SITE, "Create website"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {RENDER}

    def reaches_to(self, s: Strategy) -> Set[int]:
        # Local edition: gathering into final_<id> is what settles the workspace.
        # Publishing edition: the local page is a check, not a milestone.
        if s is Strategy.LOCAL_DEFAULT_WEBSITE:
            return {DELETE_WS}
        return set()


class UploadToSite(Step):
    number, title = UPLOAD, "Upload to site"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {RENDER} if s is Strategy.WEBSITE_REPO else set()

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {DEPLOY} if s is Strategy.WEBSITE_REPO else set()


class UpdateSite(Step):
    number, title = DEPLOY, "Update site"

    def reachable_from(self, s: Strategy) -> Set[int]:
        # Deploy publishes curation, so it stands alone as well as following an
        # upload — you can deploy without having rendered anything.
        return {UPLOAD} if s is Strategy.WEBSITE_REPO else set()

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {DELETE_WS} if s is Strategy.WEBSITE_REPO else set()


class DeleteSimData(Step):
    number, title, destructive = DELETE_WS, "Delete SIM data", True

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {DEPLOY} if s is Strategy.WEBSITE_REPO else {SITE}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {IMPORT}


class CleanSim(Step):
    number, title, destructive = WIPE_SIM, "Clean SIM", True

    def reachable_from(self, s: Strategy) -> Set[int]:
        # As soon as the import has landed and verified, the card is free.
        return {IMPORT}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {IMPORT}


ALL_STEPS: Dict[int, Step] = {
    s.number: s for s in (
        ImportFromSim(), ListTrips(), PreviewTrips(), ExcludeTrip(), RenderVideos(),
        CreateWebsite(), UploadToSite(), UpdateSite(), DeleteSimData(), CleanSim(),
    )
}


def _out_of(step: Step, strategy: Strategy):
    """The edges one step declares on its own behalf."""
    return {(step.number, b) for b in step.reaches_to(strategy)}


def _into(step: Step, strategy: Strategy):
    """The edges one step claims arrive at it."""
    return {(a, step.number) for a in step.reachable_from(strategy)}


def _forward_edges(strategy: Strategy):
    return set().union(*(_out_of(s, strategy) for s in ALL_STEPS.values()))


def _backward_edges(strategy: Strategy):
    return set().union(*(_into(s, strategy) for s in ALL_STEPS.values()))


def inconsistent_edges(strategy: Strategy):
    """Edges declared from one side only.

    An edge a->b must appear in a.reaches_to AND b.reachable_from, so anything
    in the symmetric difference is a rule that was changed in one place and not
    the other. That is the entire reason both directions are written down.
    """
    return sorted(_forward_edges(strategy) ^ _backward_edges(strategy))
