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

`blocked_because(ctx)` answers the other question — not "may this follow that"
but "would it do anything right now" — and returns the reason it would not, or
None. Each step implements its own: the rule that greys a step in the menu is
the step's rule, written where the step is declared, not registered from
elsewhere. The filesystem machinery the rules consult still lives in
pipeline.py and is reached through the ctx (see _machinery below).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from itertools import chain
from typing import Dict, Iterable, Optional, Protocol, Set


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


# ---------------------------------------------------------------------------
# Reaching the machinery. The predicates need pipeline.py's filesystem helpers
# (import_candidates, rendered_mp4s, copy_still_exists, ...), and pipeline.py
# imports this module — the classic cycle. It is broken by resolving pipeline
# through the ctx OBJECT rather than by name: the ctx was built by exactly the
# pipeline instance whose helpers apply to it. That matters because the tests
# load pipeline.py under private module names and patch attributes on those
# instances; `import pipeline` here would construct a second, unpatched pipeline
# (and re-execute the whole file when the CLI runs as __main__). A method's
# __globals__ IS the defining module's namespace, so patches are seen live.
# ---------------------------------------------------------------------------

class _Machinery:
    """Attribute-style live view over a pipeline module's namespace."""

    def __init__(self, namespace: dict):
        self._ns = namespace

    def __getattr__(self, name):
        return self._ns[name]


def _machinery(ctx: "Ctxish") -> _Machinery:
    """The pipeline instance that built this ctx, found via its class."""
    return _Machinery(type(ctx).cfg_opt.__globals__)


# ---------------------------------------------------------------------------
# Shared predicate pieces. Each is a single question with a single branch, so
# the availability rules stay measurably simple; blocked_because composes them
# with _first_reason, which stops at the first reason found.
# ---------------------------------------------------------------------------

def _first_reason(ctx: "Ctxish", checks: Iterable) -> Optional[str]:
    """The first reason any check returns, or None. Lazy: later checks do not
    run once one has answered — the same short-circuit the old if-chains had."""
    reasons = (check(ctx) for check in checks)
    return next(filter(None, reasons), None)


def _safe_rglob(root, pattern):
    """Recursive glob that treats an unreadable root as empty."""
    try:
        return list(root.rglob(pattern))
    except OSError:
        return []


def _safe_glob(root, pattern):
    """Non-recursive glob that treats an unreadable root as empty."""
    try:
        return list(root.glob(pattern))
    except OSError:
        return []


def _has_metas(ctx) -> bool:
    """Has the sidecar pass (Preview) left its evidence in the working area?"""
    return bool(_safe_rglob(ctx.out_dir, "*_meta.json"))


_SIDECAR_REASON = "create sidecars first — run %d) %s"


def _sidecars_missing(ctx) -> Optional[str]:
    """Reason when an import exists but no sidecars do, or None.

    The sidecar pass (Preview) is where the trips are looked at before anything
    slow or destructive happens to them. Every step that consumes the trips —
    render, upload, delete, wipe — waits for it, so the decision about what to
    keep is made from evidence rather than from memory.
    """
    if _sidecar_debt_settled(ctx):
        return None
    return _SIDECAR_REASON % (PREVIEW, ALL_STEPS[PREVIEW].title)


def _sidecar_debt_settled(ctx) -> bool:
    """No import awaiting a look, or the look has happened."""
    if not _machinery(ctx).import_candidates(ctx):
        return True
    return _has_metas(ctx)


def _sidecars_absent(ctx) -> Optional[str]:
    """Reason when NO sidecars exist at all, or None.

    Stricter than _sidecars_missing: an empty workspace passes that one (there
    is no import owing a look), but a step that publishes the trips' metadata —
    Deploy — has nothing to say until the sidecar pass has run at least once.
    """
    if _has_metas(ctx):
        return None
    return _SIDECAR_REASON % (PREVIEW, ALL_STEPS[PREVIEW].title)


def _nothing_imported(ctx, reason: str) -> Optional[str]:
    """The given reason when the workspace holds no import, else None."""
    if _machinery(ctx).import_candidates(ctx):
        return None
    return reason


# GPS-track evidence for the sidecar pass. The track lives in DCIM/<NNN>gps/ as
# .gpx files, or as '*.git' tar archives the renderer harvests .gpx members
# from. Either counts; a source with neither has no track and the sidecar pass
# would produce nothing.

def _is_track_file(f) -> bool:
    return f.suffix.lower() in (".gpx", ".git")


def _is_gps_dir(p) -> bool:
    return p.is_dir() and "gps" in p.name.lower()


def _subdirs(dcim):
    if not dcim.is_dir():
        return []
    return list(dcim.iterdir())


def _gps_dirs(cands):
    subs = chain.from_iterable(_subdirs(c / "DCIM") for c in cands)
    return filter(_is_gps_dir, subs)


def _has_track(sub) -> bool:
    return any(map(_is_track_file, sub.iterdir()))


# The publishing configuration, asked for the same way by Upload and Deploy.
# The reasons name the config key, because this line is where someone who
# cloned the repo finds out that publishing exists at all. "not configured"
# would tell them nothing they can act on.

def _no_site_key(ctx) -> Optional[str]:
    if ctx.site is None:
        return "needs site_repo in config.txt"
    return None


def _site_dir_missing(ctx) -> Optional[str]:
    if ctx.site.is_dir():
        return None
    return "site_repo not found: %s" % _machinery(ctx).tilde(ctx.site)


def _site_script_missing(ctx, script: str) -> Optional[str]:
    if ctx.site_script("deploy", script):
        return None
    return "no deploy/%s in %s" % (script, _machinery(ctx).tilde(ctx.site))


def _site_repo_missing(ctx, script: str) -> Optional[str]:
    """Why the site repo cannot serve this step, or None."""
    checks = (_no_site_key, _site_dir_missing,
              lambda c: _site_script_missing(c, script))
    return _first_reason(ctx, checks)


def _final_folder_exists(ctx) -> bool:
    """A gathered final_ folder counts as renders too — the rebuild case."""
    froot = getattr(ctx, "final_root", ctx.out_dir)
    dirs = _safe_glob(froot, _machinery(ctx).FINAL_PREFIX + "*")
    return any(d.is_dir() for d in dirs)


class Step(ABC):
    """One menu action, and where it sits in the order."""

    number: int
    title: str

    @abstractmethod
    def reachable_from(self, strategy: Strategy) -> Set[int]:
        """Steps that can sensibly precede this one. Empty = an entry point."""

    @abstractmethod
    def reaches_to(self, strategy: Strategy) -> Set[int]:
        """Steps this one unlocks. Empty = a leaf."""

    def is_start_node(self, ctx: "Ctxish") -> bool:
        """Can this be the FIRST thing you do, with nothing done yet?

        Not the same as having no inbound edges: Import has two — you come back
        to it after clearing the workspace or the card — and is still an entry
        point. It takes the state because the answer could depend on it; today
        Import is the ONLY way in under both strategies, because every other
        step consumes something a previous step produced. Deploy looked like a
        second way in on a publishing install, but deploying with no sidecars
        publishes nothing.

        A test holds this to is_enabled() on a cold workspace, so a step cannot
        advertise itself as an entry point while the menu greys it out.
        """
        return False

    def is_destructive(self) -> bool:
        """Does this step erase something that will not come back?

        The menu paints these red from here, so "destructive" is a property the
        step states about itself rather than a marker someone remembers to put
        in front of it. Steps 4, 9 and 10 are the same kind of thing and now say
        so in the same way.
        """
        return False

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Why this step would do nothing right now, or None if it would.

        Each step subclass overrides this with its own rule — the rule lives
        with the step it governs, so the menu, the tests and the step body all
        read the same answer from the same place. The default is unconditional:
        a step with no override has nothing that can be in its way.
        """
        return None

    def is_enabled(self, ctx: Ctxish) -> bool:
        """Would this step do something, given the state?

        Deliberately derived from blocked_because rather than answered
        separately: two independent answers to "can I run this" is how a menu
        ends up offering a step that then refuses, or greying one out with no
        reason underneath.
        """
        return self.blocked_because(ctx) is None

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

    def is_start_node(self, ctx: "Ctxish") -> bool:
        # The way in for footage: everything downstream reads the copy it makes.
        return True

    def reachable_from(self, s: Strategy) -> Set[int]:
        # An entry point, and also where you come back to after a cycle ends.
        return {DELETE_WS, WIPE_SIM}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {LIST, PREVIEW, WIPE_SIM}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Import has nothing to do when there is no source but footage is in.

        Resolved through import_candidates(), the same way every step does,
        rather than looking only at import_root: the folder actually in use
        comes from config.txt's `root`, and checking only the fixed default
        meant renaming the import directory silently re-enabled this step.
        """
        return _first_reason(ctx, (self._unfinished_session,
                                   self._already_imported))

    def _unfinished_session(self, ctx) -> Optional[str]:
        # An unfinished session blocks a new import outright. Importing over it
        # mixes two sources into one grouping with no record of which clip came
        # from which — the rule is that it does not happen at all.
        # working_area_is_expendable is the same proof the sweeps demand; it
        # only reaches for S3 when loose renders exist AND a bucket is
        # configured, so the common menu draw stays local and instant.
        ok, why, _stragglers = _machinery(ctx).working_area_is_expendable(ctx)
        if ok:
            return None
        return ("unfinished session (%s) — finish it (%d/%d) or delete it (%d) first"
                % (why, SITE, UPLOAD, DELETE_WS))

    def _already_imported(self, ctx) -> Optional[str]:
        if (ctx.card / "DCIM").is_dir():
            return None            # a source with footage always has work to offer
        return self._clips_already_in(ctx)

    def _clips_already_in(self, ctx) -> Optional[str]:
        pl = _machinery(ctx)
        cands = pl.import_candidates(ctx)
        if not cands:
            return None
        # Terse on purpose: this sits under the menu on every draw, so a long
        # sentence there costs a line of a narrow screen every time.
        return ("already have %s clips — select %d or %d"
                % (pl.clip_count(cands[0]), PREVIEW, RENDER))


class ListTrips(Step):
    number, title = LIST, "List trips"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {IMPORT}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {PREVIEW}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Nothing to list before anything is imported."""
        return _nothing_imported(ctx, "nothing imported — run %d) first" % IMPORT)


class PreviewTrips(Step):
    number, title = PREVIEW, "Preview trips"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {IMPORT, LIST}

    def reaches_to(self, s: Strategy) -> Set[int]:
        # Preview writes the sidecars, which is what everything downstream
        # reads. On a publishing install they also unlock Deploy directly:
        # publishing the page from sidecars alone exercises the real publish
        # path — curation pull, manifest rebuild, asset split — hours before a
        # render finishes, which is when a broken pipeline is cheap to catch.
        if s is Strategy.WEBSITE_REPO:
            return {EXCLUDE, RENDER, DEPLOY}
        return {EXCLUDE, RENDER}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Sidecars are built from the GPS track; no track, nothing to build."""
        cands = _machinery(ctx).import_candidates(ctx)
        if not cands:
            return "nothing imported — run %d) first" % IMPORT
        return self._no_track(cands)

    def _no_track(self, cands) -> Optional[str]:
        tracked = map(_has_track, _gps_dirs(cands))
        if any(tracked):
            return None
        return "no GPS track in the import (no .gpx under DCIM) — sidecars need it"


class ExcludeTrip(Step):
    number, title = EXCLUDE, "Exclude trip"

    def is_destructive(self) -> bool:
        return True

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {PREVIEW}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {RENDER}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Nothing imported means nothing to exclude."""
        return _nothing_imported(ctx, "nothing imported — nothing to exclude")


class RenderVideos(Step):
    number, title = RENDER, "Render videos"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {PREVIEW, EXCLUDE}

    def reaches_to(self, s: Strategy) -> Set[int]:
        if s is Strategy.WEBSITE_REPO:
            return {SITE, UPLOAD}
        return {SITE}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Render reads the copied import, never the source itself."""
        reason = _nothing_imported(
            ctx, "nothing imported — a mounted card is not a workspace; run %d) first"
            % IMPORT)
        return reason or _sidecars_missing(ctx)


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

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """The local site is built FROM the renders. A gathered final_ folder
        counts too — the rebuild case."""
        if self._renders_exist(ctx):
            return None
        return "no renders to build from — run %d) first" % RENDER

    def _renders_exist(self, ctx) -> bool:
        if _machinery(ctx).rendered_mp4s(ctx.out_dir):
            return True
        return _final_folder_exists(ctx)


class UploadToSite(Step):
    number, title = UPLOAD, "Upload to site"

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {RENDER} if s is Strategy.WEBSITE_REPO else set()

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {DEPLOY} if s is Strategy.WEBSITE_REPO else set()

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Why Upload cannot run, or None.

        The bucket is named first when both are missing: it is the setting that
        distinguishes this step from Deploy, and the site repo is asked for on
        the line below anyway. Without the bucket the sync could still run, but
        its result could not be verified — and `aws s3 sync` exits 0 on failed
        objects, so an unverifiable upload is one this CLI has no business
        reporting on. Config alone is not enough: upload sends renders, so
        there have to BE renders, and they have to have been looked at
        (sidecars) first.
        """
        checks = (self._bucket_missing,
                  lambda c: _site_repo_missing(c, "upload-videos-s3.sh"),
                  self._nothing_rendered,
                  _sidecars_missing)
        return _first_reason(ctx, checks)

    def _bucket_missing(self, ctx) -> Optional[str]:
        if ctx.s3_bucket:
            return None
        return "needs s3_bucket in config.txt"

    def _nothing_rendered(self, ctx) -> Optional[str]:
        if _machinery(ctx).rendered_mp4s(ctx.out_dir):
            return None
        return "nothing rendered to upload — run %d) first" % RENDER


class UpdateSite(Step):
    number, title = DEPLOY, "Update site"

    def reachable_from(self, s: Strategy) -> Set[int]:
        # From Preview as well as Upload: the sidecars are what the manifest is
        # built from, so deploy can (and should) run before any render — to the
        # REAL site, to catch a broken publish path early. See PreviewTrips.
        return {PREVIEW, UPLOAD} if s is Strategy.WEBSITE_REPO else set()

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {DELETE_WS} if s is Strategy.WEBSITE_REPO else set()

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """Why Deploy cannot run, or None.

        Deploy is allowed WITHOUT the render step and WITHOUT an upload — it
        publishes the page, the manifest and the curation, not video — but
        never without sidecars: the manifest is built from them, so before the
        sidecar pass has run at least once there is nothing to deploy, and
        deploying with an import sitting there unlooked-at would publish a
        manifest that says nothing about the trips on disk.
        """
        checks = (lambda c: _site_repo_missing(c, "deploy-site.sh"),
                  _sidecars_absent)
        return _first_reason(ctx, checks)


class DeleteSimData(Step):
    number, title = DELETE_WS, "Delete SIM data"

    def is_destructive(self) -> bool:
        return True

    def reachable_from(self, s: Strategy) -> Set[int]:
        return {DEPLOY} if s is Strategy.WEBSITE_REPO else {SITE}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {IMPORT}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """The delete has its own heavy gates; the menu-level ones are that
        something exists to delete at all, and that the trips were looked at
        (sidecars) before the footage is put beyond looking."""
        return _first_reason(ctx, (self._nothing_to_delete, _sidecars_missing))

    def _nothing_to_delete(self, ctx) -> Optional[str]:
        if self._holdings(ctx):
            return None
        return "nothing imported — nothing to delete"

    def _holdings(self, ctx):
        pl = _machinery(ctx)
        return pl.import_candidates(ctx) or pl.rendered_mp4s(ctx.out_dir)


class CleanSim(Step):
    number, title = WIPE_SIM, "Clean SIM"

    def is_destructive(self) -> bool:
        return True

    def reachable_from(self, s: Strategy) -> Set[int]:
        # As soon as the import has landed and verified, the card is free.
        return {IMPORT}

    def reaches_to(self, s: Strategy) -> Set[int]:
        return {IMPORT}

    def blocked_because(self, ctx: Ctxish) -> Optional[str]:
        """The same guards wipe_card enforces at run time, asked up front.

        This is the one step whose target may have no copy, so the menu must
        not offer it on evidence the step itself would refuse: the ledger has
        to show an import, no clip on the card may be newer than it, and the
        copy the ledger claims has to still be findable on this machine.
        """
        checks = (self._no_source, self._already_clean, self._never_imported,
                  self._unimported_clips, self._copy_lost, _sidecars_missing)
        return _first_reason(ctx, checks)

    def _no_source(self, ctx) -> Optional[str]:
        if (ctx.card / "DCIM").is_dir():
            return None
        return "no card at %s" % _machinery(ctx).tilde(ctx.card)

    def _already_clean(self, ctx) -> Optional[str]:
        files = filter(_is_file, _safe_rglob(ctx.card / "DCIM", "*"))
        if next(iter(files), None) is None:
            return "card is already clean — nothing to wipe"
        return None

    def _never_imported(self, ctx) -> Optional[str]:
        if _machinery(ctx).last_imported_stamp(ctx):
            return None
        return "nothing ever imported — no record this footage exists anywhere else"

    def _unimported_clips(self, ctx) -> Optional[str]:
        pl = _machinery(ctx)
        pl.excluded_stamps(ctx)          # refresh the cache card_split reads
        n_new, _n_old = pl.card_split(ctx.card, pl.last_imported_stamp(ctx))
        if n_new:
            return "%d clip(s) on the card were never imported" % n_new
        return None

    def _copy_lost(self, ctx) -> Optional[str]:
        have, _what = _machinery(ctx).copy_still_exists(ctx)
        if have:
            return None
        return "the ledger claims imported, but no copy is still on this machine"


def _is_file(p) -> bool:
    return p.is_file()


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
