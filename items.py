"""The ten menu items, 0-9, and the graph they declare.

One class per item. Each states what it is, where it may lead, and answers
`evaluate(world)` — would this do anything, and may it — before `execute`
lets it near `_perform`. The bodies themselves live in pipeline.py and arrive
through the injected `work` collaborator, so nothing here imports pipeline and
a test can drive the whole menu with a mock.

The outbound column is authored here from the owner's table. THREE EDGES
DIFFER from what he wrote, each marked at its declaration site and reported by
tests/print_step_graph.py — see the notes on items 5, 6 and 7. The inbound
column he wrote is transcribed verbatim into IN_AUTHORED and is never used to
build the graph; menu.derive_inbound computes the real one and
menu.disagreements diffs the two.
"""

from __future__ import annotations

from menu import (Anywhere, Destructive, Edges, MenuItem, Plan, Scope, StartNode,
                  StepBack, Strategy, Verdict, blocked, did, go, satisfied,
                  stopped, PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                  BUILD, UPLOAD, CLEAN_WS, ERASE_CARD)
import guards


def _first_block(*reasons) -> Verdict:
    """The first reason anything is in the way, or GO."""
    hit = next(filter(None, reasons), None)
    return _blocked_or_go(hit)


def _blocked_or_go(reason) -> Verdict:
    if reason:
        return blocked(reason)
    return go()


def _no_import(world, reason: str):
    """`reason` when the workspace holds no import, else None."""
    if world.imports:
        return None
    return reason


def _e(*numbers) -> Edges:
    return Edges(frozenset(numbers))


def _both(neighbours):
    return {Strategy.WEBSITE_REPO: neighbours,
            Strategy.LOCAL_DEFAULT_WEBSITE: neighbours}


def _both_sets(*numbers):
    return {Strategy.WEBSITE_REPO: frozenset(numbers),
            Strategy.LOCAL_DEFAULT_WEBSITE: frozenset(numbers)}


# ---------------------------------------------------------------------------
# 0 — Progress
# ---------------------------------------------------------------------------

class Progress(MenuItem):
    """The read-only view: what is on disk and what has been done to it.

    Not a transition — an observation of one. It neighbours everything and is
    never a position, so looking never moves the pipeline and never blocks:
    an empty workspace is a legitimate thing to report.
    """

    number = PROGRESS
    NAME = "Progress"
    DESCRIPTION = ("Show what exists and what has been done to it. "
                   "Reads only; changes nothing.")
    INBOUND_KIND = Anywhere
    OUT = _both(Anywhere())
    IN_AUTHORED = _both(None)

    def evaluate(self, world) -> Verdict:
        return go()

    def _perform(self, world):
        return self._work.progress(world)


# ---------------------------------------------------------------------------
# 1 — Import SIM
# ---------------------------------------------------------------------------

class ImportSim(MenuItem):
    """Copy the source's DCIM tree into the workspace and verify it.

    The source is ANY directory holding a DCIM tree — a mounted card is the
    common case, a card copied onto an external disk works the same.

    It no longer offers to clear the previous round. That was item 8's job run
    from inside item 1, silently in one branch and behind a y/n in the other;
    under this graph item 1 reaches item 8 directly, so the offer has nowhere
    to earn its place. What survives is the QUESTION it was attached to — an
    unfinished session still refuses a new import, because importing on top
    mixes two cards into one grouping with no record of which clip came from
    which.
    """

    number = IMPORT
    NAME = "Import SIM"
    DESCRIPTION = ("Copy the SIM's DCIM tree into the workspace and verify it "
                   "file-for-file.")
    START = True
    INBOUND_KIND = StartNode
    OUT = _both(_e(META, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both(None)

    def evaluate(self, world) -> Verdict:
        if not world.workspace_settled:
            return blocked("unfinished session (%s) — finish it (%d/%d) or clean up (%d)"
                           % (world.workspace_note, BUILD, UPLOAD, CLEAN_WS))
        return self._source(world)

    def _source(self, world) -> Verdict:
        # A source with footage always has work to offer, even over an import
        # that is already there: the delta decides how much.
        if world.card.dcim:
            return go()
        return self._already_in(world)

    def _already_in(self, world) -> Verdict:
        if world.imports:
            return satisfied("footage is already in the workspace")
        return blocked("no source with a DCIM tree, and nothing imported")

    def _perform(self, world):
        return self._work.import_footage(world)


# ---------------------------------------------------------------------------
# 2 — Generate Meta
# ---------------------------------------------------------------------------

class GenerateMeta(MenuItem):
    number = META
    NAME = "Generate Meta"
    DESCRIPTION = ("Write each trip's sidecars: _meta.json, .gpx and .html map. "
                   "No stills, no encoding.")
    OUT = _both(_e(META, PREVIEW, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both_sets(META, IMPORT, EXCLUDE)

    def evaluate(self, world) -> Verdict:
        """Evidence only.

        "Has the import run" is not asked as an ORDER question — item 2 is
        reachable from itself and from Exclude Trip, so an emptied workspace
        is genuinely reachable here and the answer is that there is no work,
        not that a step was skipped. The GPS track check is pure evidence:
        sidecars are built from the track, and a source without one produces
        nothing however many times it is run.
        """
        return _first_block(
            _no_import(world, "the workspace holds no import — nothing to build sidecars from"),
            guards.track_missing(world))

    def _perform(self, world):
        return self._work.generate_meta(world)


# ---------------------------------------------------------------------------
# 3 — Build Preview
# ---------------------------------------------------------------------------

class BuildPreview(MenuItem):
    number = PREVIEW
    NAME = "Build Preview"
    DESCRIPTION = ("A still per trip and a local contact sheet, from the sidecars. "
                   "No encoding, nothing leaves this machine.")
    OUT = _both(_e(PREVIEW, META, EXCLUDE, RENDER, BUILD, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both_sets(PREVIEW, META, EXCLUDE)

    def evaluate(self, world) -> Verdict:
        """The sidecars have to be ON DISK, not merely to have been written.

        This looks like the ordering check it replaced and is not: nothing
        stops the operator deleting a sidecar in Finder between the menu draw
        and the keypress, and an empty workspace settles the question rather
        than failing it.
        """
        return _first_block(
            _no_import(world, "the workspace holds no import — nothing to preview"),
            guards.sidecars_missing(world))

    def _perform(self, world):
        return self._work.build_preview(world)


# ---------------------------------------------------------------------------
# 4 — Exclude Trip (DESTRUCTIVE)
# ---------------------------------------------------------------------------

class ExcludeTrip(Destructive):
    """Delete one trip's original clips so nothing downstream ever sees them.

    Completed IFF a trip was removed — the owner's own worked example. Every
    cancel path (a blank selection, an index that is not listed, anything but
    DROP at the prompt) leaves the position where it was, and the narrow
    outbound {4,2,8,9} only takes effect when clips actually went.
    """

    number = EXCLUDE
    NAME = "Exclude Trip"
    DESCRIPTION = ("Delete a trip's source clips so it is never rendered, "
                   "uploaded or published.")
    DESTR = True
    WORD = "DROP"
    SCOPE = Scope.FULL          # the only-copy warning needs the bucket listing
    OUT = _both(_e(EXCLUDE, META, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both_sets(EXCLUDE, META, PREVIEW)

    def evaluate(self, world) -> Verdict:
        return _first_block(
            _no_import(world, "the workspace holds no import — nothing to exclude"))

    def _plan(self, world) -> Plan:
        return self._work.exclude_plan(world)


# ---------------------------------------------------------------------------
# 5 — Render Videos
# ---------------------------------------------------------------------------

class RenderVideos(MenuItem):
    number = RENDER
    NAME = "Render Videos"
    DESCRIPTION = "Encode the chosen trips. The slow step: hours for a full card."
    # DEVIATION FROM THE OWNER'S TABLE: he gave item 5 an outbound edge to 7
    # under website_repo. Removed. Item 7 uploads the BUILT site, and reaching
    # it from Render skips the building — item 7's own inbound column says
    # {7,6}, so the edge was one-sided in his table too. Item 6 gained 7 in
    # exchange (see BuildWebsite), and the two changes only make sense
    # together: 5 was the sole route to 7 before them.
    OUT = _both(_e(RENDER, META, PREVIEW, EXCLUDE, BUILD, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = {
        Strategy.WEBSITE_REPO: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD, UPLOAD}),
        Strategy.LOCAL_DEFAULT_WEBSITE: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """Render reads the COPIED import, never the source itself.

        A mounted card is not a workspace: that distinction is evidence about
        two different directories, not a statement about which step ran first.
        """
        return _first_block(
            _no_import(world,
                       "the workspace holds no import — a mounted source is not a workspace"),
            guards.sidecars_missing(world))

    def _perform(self, world):
        return self._work.render(world)


# ---------------------------------------------------------------------------
# 6 — Build Website
# ---------------------------------------------------------------------------

class BuildWebsite(MenuItem):
    """Build the page from the renders.

    Under the local product this also GATHERS the render tree into
    final_<day>_<import>, which is what makes the workspace expendable — there
    is no separate gather item, so it lives here or nowhere. Which gatherer is
    installed is settled by the constructor, not by an `if ctx.site_ready`
    inside the body: under website_repo the trips.json uids embed the import
    folder name, so moving the tree would orphan every published trip.
    """

    number = BUILD
    NAME = "Build Website"
    DESCRIPTION = ("Build the local result page from the renders. Nothing leaves "
                   "this machine.")
    # DEVIATION FROM THE OWNER'S TABLE: 7 added to the outbound under
    # website_repo. His inbound column for item 7 says {7,6} — build the site,
    # then put it online — but no outbound set anywhere offered 7, so Upload
    # Website was unreachable by its own natural route. This is the edge that
    # makes publishing work.
    OUT = {
        Strategy.WEBSITE_REPO: _e(BUILD, META, PREVIEW, EXCLUDE, RENDER, UPLOAD,
                                  CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_DEFAULT_WEBSITE: _e(BUILD, META, PREVIEW, EXCLUDE, RENDER,
                                           CLEAN_WS, ERASE_CARD),
    }
    IN_AUTHORED = {
        Strategy.WEBSITE_REPO: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER, UPLOAD}),
        Strategy.LOCAL_DEFAULT_WEBSITE: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER}),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        # The strategy branch, resolved once. It must not reappear as an `if`
        # in _perform.
        self._gather = work.gatherer(strategy)

    def evaluate(self, world) -> Verdict:
        """Built FROM the renders. A gathered final_ folder counts — the
        rebuild case, where the loose renders are gone because this item moved
        them and the page must still be rebuildable."""
        if guards.renders_exist(world):
            return go()
        return blocked("no renders and no gathered folder to build a page from")

    def _perform(self, world):
        return self._work.build_website(world, self._gather)


# ---------------------------------------------------------------------------
# 7 — Upload Website
# ---------------------------------------------------------------------------

class UploadWebsite(MenuItem):
    """Getting the built site online. One job, two transports.

    The assets go to the bucket and the pages go to the server; they were two
    menu items and are one now, in that order, because the deploy record and
    the bucket listing are two halves of the same proof — Clean Workspace
    needs both facts about the same file before it will erase anything.
    """

    number = UPLOAD
    NAME = "Upload Website"
    DESCRIPTION = ("Sync the renders to the bucket, then deploy the site with "
                   "SIGNED_VIDEOS=1. Resumes where it left off.")
    SCOPE = Scope.FULL
    # DEVIATION FROM THE OWNER'S TABLE: 6 added to the outbound under
    # website_repo. He wrote 7 into item 6's INBOUND column — after uploading
    # you fix a caption and rebuild — and left it out of item 7's outbound.
    # Deriving the inbound would have silently deleted an edge he authored.
    OUT = {
        Strategy.WEBSITE_REPO: _e(UPLOAD, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                                  CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_DEFAULT_WEBSITE: _e(),
    }
    IN_AUTHORED = {
        Strategy.WEBSITE_REPO: frozenset({UPLOAD, BUILD}),
        Strategy.LOCAL_DEFAULT_WEBSITE: frozenset(),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        self._publish = work.publisher(strategy)

    def evaluate(self, world) -> Verdict:
        why = self._publish.why_not(world)
        if why:
            return blocked(why)
        return self._outstanding(world)

    def _outstanding(self, world) -> Verdict:
        todo = guards.uploads_outstanding(world) + guards.deploy_outstanding(world)
        if todo:
            return go()
        return satisfied("every render is on the bucket and covered by a deploy")

    def _perform(self, world):
        return self._publish.run(world)


# ---------------------------------------------------------------------------
# 8 — Clean Workspace (DESTRUCTIVE, ends the cycle)
# ---------------------------------------------------------------------------

class CleanWorkspace(Destructive):
    """Erase the imported footage and the renders it produced.

    The WORKING AREA half of what used to be one folded "Clean up". Its
    outbound is {1}: once the workspace is gone only a new cycle remains — so
    it can never precede Delete SIM Data, and the folded step's defect (gather
    the card's evidence from the workspace, erase the workspace, then refuse
    the card half after the irreversible half has already run and printed that
    the card was verified) cannot be expressed in this graph at all.
    """

    number = CLEAN_WS
    NAME = "Clean Workspace"
    DESCRIPTION = ("Erase the imported footage and the renders it produced. "
                   "Refuses unless the site says they are published.")
    END = True
    DESTR = True
    WORD = "CLEAN"
    SCOPE = Scope.FULL
    OUT = _both(_e(IMPORT))
    IN_AUTHORED = {
        Strategy.WEBSITE_REPO: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD}),
        Strategy.LOCAL_DEFAULT_WEBSITE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
                                                   RENDER, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """The cheap half only.

        The three heavy gates — rendered locally, on the bucket, published on
        the site — are plan.guard, asked against a world captured at dispatch
        and again after the word is typed. Asking them on every menu draw
        would shell out to is-complete.py forty times a session, and a menu
        that is not instant stops being recomputed and starts being remembered.
        """
        return _first_block(
            _no_import(world, "nothing imported — nothing to clean up"),
            guards.sidecars_missing(world))

    def _plan(self, world) -> Plan:
        return self._work.clean_workspace_plan(world)


# ---------------------------------------------------------------------------
# 9 — Delete SIM Data (DESTRUCTIVE, ends the cycle)
# ---------------------------------------------------------------------------

class DeleteSimData(Destructive):
    """Erase the card's clips, keeping its folders so the camera can record.

    The CARD half, unfolded back out of "Clean up". Its outbound is StepBack:
    freeing the card does not interrupt the cycle, so completing hands the
    position back to whoever offered it — from which Clean Workspace is still
    reachable. The graph therefore permits the safe order (erase the card
    while its clips are provably in the workspace, clean the workspace once
    the renders are published) and forbids the dangerous one.
    """

    number = ERASE_CARD
    NAME = "Delete SIM Data"
    DESCRIPTION = ("Erase the card's clips, keeping its folders so the camera can "
                   "record. Refuses unless every clip is accounted for.")
    END = True
    DESTR = True
    WORD = "ERASE"
    OUT = _both(StepBack())
    IN_AUTHORED = {
        Strategy.WEBSITE_REPO: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD}),
        Strategy.LOCAL_DEFAULT_WEBSITE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
                                                   RENDER, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """An already-clean card is SATISFIED, not refused.

        Folded into the clean-up this case was masked; standing alone it used
        to answer "the ledger claims imported, but no copy is still on this
        machine" — a lost-footage warning fired at a card that is simply
        empty. A guard that cries wolf is how an operator learns to stop
        reading guards.
        """
        if not world.card.dcim:
            return blocked("no card at %s" % world.card.path)
        return self._card_state(world)

    def _card_state(self, world) -> Verdict:
        if not world.card.stamps:
            return satisfied("the card holds no clips — nothing to erase")
        return guards.card_is_expendable(world)

    def _plan(self, world) -> Plan:
        return self._work.erase_card_plan(world)


# The cold-start orientation: the first row that matches names where we are.
# Order is priority, and the FIRST row is a correctness rule rather than a
# progress marker — an exclusion record newer than the newest sidecar means
# the sidecars describe trips that no longer exist, so Exclude Trip's narrow
# outbound is restored across a session boundary and a restart cannot render
# stale meta.
COLD_START_RULES = (
    (EXCLUDE, lambda w: bool(w.excluded) and w.excluded_at > w.newest_meta_at),
    (UPLOAD, lambda w: bool(w.site.deployed)),
    (BUILD, lambda w: w.site.page or bool(w.final_folders)),
    (RENDER, lambda w: bool(w.renders)),
    (PREVIEW, lambda w: w.stills_current),
    (META, lambda w: bool(w.metas)),
    (IMPORT, lambda w: bool(w.imports)),
)

ALL_ITEMS = (Progress, ImportSim, GenerateMeta, BuildPreview, ExcludeTrip,
             RenderVideos, BuildWebsite, UploadWebsite, CleanWorkspace,
             DeleteSimData)

NAMES = {cls.number: cls.NAME for cls in ALL_ITEMS}
