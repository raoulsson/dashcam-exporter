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

from menu import (Anywhere, Destructive, Edges, Evidence, MenuItem, Plan, Ruling,
                  Scope, StartNode, StepBack, Strategy, Verdict, blocked, go,
                  satisfied,
                  PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                  BUILD, UPLOAD, CLEAN_WS, ERASE_CARD)
import guards


def _reason(*reasons):
    """The first reason anything is in the way, or None."""
    return next(filter(None, reasons), None)


def _first_block(*reasons) -> Verdict:
    """The first reason anything is in the way, or GO."""
    return _blocked_or_go(_reason(*reasons))


def _blocked_or_go(reason) -> Verdict:
    if reason:
        return blocked(reason)
    return go()


def _nothing_to_send(world):
    """Nothing to put online: no render, and no trip in this import either.

    It used to demand a render, which is the thing publishing no longer waits
    for — the pages of a described trip go up while its video is still hours
    away. But the floor itself cannot go, and the reason is exact: is_complete
    answers YES to an EMPTY trip list, deliberately, and that yes is only safe
    because something refuses before it is ever consulted. Take the refusal
    away and an import with nothing in it reads as "the destination has
    everything", which is the one sentence that must never appear in front of
    someone who has published nothing.

    So the floor moves from renders to trip_ids, and it must be trip_ids
    rather than metas: metas is the whole output tree, trip_ids is THIS
    import. A fresh import with no sidecars yet, beside an old import whose
    sidecars are still on disk, passes a metas test and then asks the
    destination about no trips at all.

    Evidence, not order, as before: the old wording was "nothing rendered to
    upload — run 6 first", and only the second half of that was an ordering
    claim. The fact survives an operator deleting the renders in Finder.
    """
    if world.renders or world.trip_ids:
        return None
    return "nothing on disk to publish"


def _nothing_left_to_do(world) -> Verdict:
    """Is there anything for the upload to do, off the frozen answer.

    Asked of the world rather than of the uploader, because the uploader's
    evaluate() is asked forty times a session and this question goes to the
    destination. capture_world asks it once per dispatch, at FULL scope, and
    freezes what came back; at a menu draw it reads UNKNOWN, which is not YES,
    so the offer stands. That is the same rule the old owes() carried — a
    failed listing proves nothing, so everything stays outstanding and the
    UPLOAD is what discovers the destination is down.
    """
    if world.target.complete is Evidence.YES:
        return satisfied("%s has everything" % world.target.name)
    return go()


def _no_import(world, reason: str):
    """`reason` when there is no import, else None.

    The reasons say "no import" rather than "the workspace holds no import".
    Which container is empty is the machine's framing; the operator is looking
    at a greyed entry and wants to know what is missing.
    """
    if world.imports:
        return None
    return reason


def _e(*numbers) -> Edges:
    return Edges(frozenset(numbers))


def _both(neighbours):
    return {Strategy.UPLOADER: neighbours,
            Strategy.LOCAL_PAGE: neighbours}


def _both_sets(*numbers):
    return {Strategy.UPLOADER: frozenset(numbers),
            Strategy.LOCAL_PAGE: frozenset(numbers)}


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
    DESCRIPTION = "Show what is here and what has been done to it."
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
    DESCRIPTION = "Copy the card into the workspace and verify it file-for-file."
    START = True
    INBOUND_KIND = StartNode
    # IMPORT leads to itself, exactly as GenerateMeta does. A card holds more
    # than one session's worth and the copy can be interrupted, so "import
    # again" is an ordinary next move rather than a restart -- the delta
    # decides how much, which is what evaluate() below already says. Without
    # the self-edge, orienting onto item 1 with footage in the workspace took
    # item 1 off the menu, and every entry it led to was blocked by its own
    # guard for want of a GPS track.
    OUT = _both(_e(IMPORT, META, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both(None)

    def evaluate(self, world) -> Verdict:
        if not world.workspace_settled:
            return blocked("unfinished session (%s) — finish it (%d/%d) or clean up (%d)"
                           % (world.workspace_note, BUILD, UPLOAD, CLEAN_WS))
        return self._source(world)

    def _source(self, world) -> Verdict:
        """A card with nothing left to fetch is not work.

        It used to be: "a source with footage always has work to offer, even
        over an import that is already there -- the delta decides how much."
        That was true while the delta was only decided inside the step. It is
        decided at capture now: card.new_stamps is the set accounted for by
        nothing, which is exactly what an import would copy. Empty means
        pressing 1 can only report "nothing new at the source", and a menu
        entry whose whole outcome is that sentence should have said it while
        it was still a greyed name.
        """
        if world.card.dcim and (world.card.new_stamps or world.card.to_fetch):
            return go()
        return self._already_in(world)

    def _already_in(self, world) -> Verdict:
        if world.imports:
            return satisfied("everything on the card is already here")
        if world.card.dcim:
            # A card whose every clip is accounted for elsewhere: rendered and
            # published, or dropped on purpose. Nothing to fetch and nothing
            # missing -- which is the finished state, not a fault.
            return satisfied("nothing on the card needs importing")
        return blocked("no source with a DCIM tree, and nothing imported")

    def _perform(self, world):
        return self._work.import_footage(world)


# ---------------------------------------------------------------------------
# 2 — Generate Meta
# ---------------------------------------------------------------------------

class GenerateMeta(MenuItem):
    number = META
    NAME = "Generate Meta"
    DESCRIPTION = "Work out where each trip begins and ends, and describe it."
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
            _no_import(world, "no import — nothing to build meta from"),
            guards.track_missing(world))

    def _perform(self, world):
        return self._work.generate_meta(world)


# ---------------------------------------------------------------------------
# 3 — Build Preview
# ---------------------------------------------------------------------------

class BuildPreview(MenuItem):
    number = PREVIEW
    NAME = "Build Preview"
    DESCRIPTION = "Make one still per trip so you can see what you have."
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
            _no_import(world, "no import — nothing to preview"),
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
    the word at the prompt) leaves the position where it was, and the outbound
    only takes effect when clips actually went.

    That outbound was {4,2,8,9}: after a drop the sidecars described trips
    that no longer existed, so the only way on was to write them again. It
    takes 3 now as well, because the reason has gone — the orphaned sidecars
    are removed with the footage rather than left behind for the next pass to
    trip over, so the workspace after an exclude is consistent and looking at
    what is left is a reasonable next move.
    """

    number = EXCLUDE
    NAME = "Exclude Trip"
    DESCRIPTION = "Select which trips to exclude from meta and render."
    DESTR = True
    # DELETE, like item 8. The two erase different things and both erase from
    # the WORKSPACE; the card keeps its own word, because that is the one with
    # no second copy behind it.
    WORD = "DELETE"
    SCOPE = Scope.FULL          # the only-copy warning has to ask the target
    OUT = _both(_e(EXCLUDE, META, PREVIEW, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = _both_sets(EXCLUDE, META, PREVIEW)

    def evaluate(self, world) -> Verdict:
        return _first_block(
            _no_import(world, "no import — nothing to exclude"))

    def _plan(self, world) -> Plan:
        return self._work.exclude_plan(world)


# ---------------------------------------------------------------------------
# 5 — Render Trips
# ---------------------------------------------------------------------------

class RenderVideos(MenuItem):
    number = RENDER
    NAME = "Render Trips"
    DESCRIPTION = "Encode the trips into watchable videos. Hours for a full card."
    # DEVIATION FROM THE OWNER'S TABLE: he gave item 5 an outbound edge to 7
    # under the uploader edition. Removed. Item 7 uploads the BUILT site, and reaching
    # it from Render skips the building — item 7's own inbound column says
    # {7,6}, so the edge was one-sided in his table too. Item 6 gained 7 in
    # exchange (see BuildWebsite), and the two changes only make sense
    # together: 5 was the sole route to 7 before them.
    OUT = _both(_e(RENDER, META, PREVIEW, EXCLUDE, BUILD, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """Render reads the COPIED import, never the source itself.

        A mounted card is not a workspace: that distinction is evidence about
        two different directories, not a statement about which step ran first.
        """
        return _first_block(
            _no_import(world,
                       "nothing to render"),
            guards.sidecars_missing(world))

    def _perform(self, world):
        return self._work.render(world)


# ---------------------------------------------------------------------------
# 6 — Build Website
# ---------------------------------------------------------------------------

class BuildWebsite(MenuItem):
    """Build what this installation publishes, from the renders.

    Which builder is installed is settled by the constructor, not by an `if`
    in the body. Under the local edition it writes the one-file page AND
    GATHERS the render tree into final_<day>_<import> — there is no separate
    gather item, and gathering is what makes the workspace expendable, so it
    lives here or nowhere. With an uploader configured it is the uploader's
    build, and the local page is not written at all: that page is the local
    edition's deliverable, and "Nothing leaves this machine" is a sentence
    about the other product.

    That last part is the bug this shape fixes. Only the MOVER used to be the
    strategy branch; the page writer ran either way, so a publishing install
    got a local page it never asked for, announcing that nothing had left the
    machine while item 7 was about to send it all.
    """

    number = BUILD
    NAME = "Build Website"
    DESCRIPTION = "Build the website from the rendered trips."
    # DEVIATION FROM THE OWNER'S TABLE: 7 added to the outbound under the
    # publishing edition. His inbound column for item 7 says {7,6} — build the
    # site, then put it online — but no outbound set anywhere offered 7, so
    # Upload Website was unreachable by its own natural route. This is the edge
    # that makes publishing work.
    OUT = {
        Strategy.UPLOADER: _e(BUILD, META, PREVIEW, EXCLUDE, RENDER, UPLOAD,
                                  CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_PAGE: _e(BUILD, META, PREVIEW, EXCLUDE, RENDER,
                                           CLEAN_WS, ERASE_CARD),
    }
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER}),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        # The strategy branch, resolved once. It must not reappear as an `if`
        # in _perform.
        self._builder = work.builder(strategy)

    def description(self) -> str:
        """What THIS installation's build does, asked of the thing that does it.

        Not a statement about how the tool is installed — that is the session's
        to say, once, at startup. It is this entry answering for its own job,
        which genuinely differs between the two products.
        """
        return self._builder.describe()

    def evaluate(self, world) -> Verdict:
        """The exporter's own question first, then the builder's own verdict.

        The renders check never leaves home and can block on its own: a builder
        that says yes to everything still cannot get a page built out of an
        empty tree. Past that the answer is the builder's, verbatim — including
        SATISFIED, which is how an act that has nothing left to do says so.
        """
        stop = guards.nothing_to_build_from(world)
        if stop:
            return blocked(stop)
        return self._builder.evaluate(world)

    def _perform(self, world):
        return self._builder.execute(world)


# ---------------------------------------------------------------------------
# 7 — Upload Website
# ---------------------------------------------------------------------------

class UploadWebsite(MenuItem):
    """Getting what was built online. One job.

    How many transports that takes — a bucket and then a server, one rsync, a
    copy into a folder — is the implementation's business. It was two menu
    items once and folding them was right: what Clean Workspace needs is one
    answer about one file, and an item that can leave half of that true is an
    item that publishes nothing while reporting success.
    """

    number = UPLOAD
    NAME = "Upload Website"
    DESCRIPTION = "Put the website online. Resumes where it left off."
    SCOPE = Scope.FULL
    # DEVIATION FROM THE OWNER'S TABLE: 6 added to the outbound under the
    # publishing edition. He wrote 7 into item 6's INBOUND column — after
    # uploading you fix a caption and rebuild — and left it out of item 7's
    # outbound. Deriving the inbound would have silently deleted an edge he
    # authored.
    OUT = {
        Strategy.UPLOADER: _e(UPLOAD, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                                  CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_PAGE: _e(),
    }
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({UPLOAD, BUILD}),
        Strategy.LOCAL_PAGE: frozenset(),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        self._publish = work.publisher(strategy)

    def description(self) -> str:
        """What THIS installation's upload does, asked of the thing that does it.

        The same rule as item 6's row: the entry answers for its own job rather
        than restating how the tool is installed.
        """
        return self._publish.describe()

    def evaluate(self, world) -> Verdict:
        """The exporter's own evidence first, then the uploader, then the
        frozen answer about the destination.

        Both local checks are evidence, not order: something to send, and a
        sidecar somewhere to describe it. The sidecar one came across from the
        folded-in deploy step — publishing is putting the trips' metadata
        online, and with no sidecar anywhere the deploy pushes an index
        describing no trips.
        """
        stop = _reason(_nothing_to_send(world), guards.no_sidecars_at_all(world))
        if stop:
            return blocked(stop)
        return self._still_owed(world)

    def _still_owed(self, world) -> Verdict:
        """The uploader's own word stands unless it says GO.

        It is asked on every menu draw, so it may not go and look; what it can
        answer is whether anything about THIS installation is in the way.
        """
        verdict = self._publish.evaluate(world)
        if verdict.ruling is not Ruling.GO:
            return verdict
        return _nothing_left_to_do(world)

    def _perform(self, world):
        return self._publish.execute(world)


# ---------------------------------------------------------------------------
# 8 — Clean Workspace (DESTRUCTIVE, ends the cycle)
# ---------------------------------------------------------------------------

class CleanWorkspace(Destructive):
    """Erase the imported footage and the renders it produced.

    Separate from Delete SIM Data because the two erase different things on
    different evidence, and because folding them is a defect rather than a
    tidy-up: the card's evidence is partly "the clips are in the workspace",
    so one step that gathers that evidence, erases the workspace and then
    checks the card refuses AFTER the irreversible half has run, having
    already printed that the card was verified.

    Its outbound is {1,8}: once the workspace is gone only a new cycle
    remains, so it can never precede Delete SIM Data and that sequence cannot
    be expressed in this graph at all. The 8 is itself, and it does not weaken
    that -- 8 leading to 8 still reaches nothing but 1.

    It is there because standing on 8 does not mean the workspace is empty. It
    means 8 was the last step that COMPLETED, and an import that ran after it
    and did not finish -- interrupted, or declined at the prompt -- leaves the
    position where it was with footage on disk that nobody can now clear. That
    is a dead end reached by pressing n. A sink holding several dated imports
    is the same shape: the erase narrows to one of them, and cleaning the next
    is an ordinary move.
    """

    number = CLEAN_WS
    NAME = "Clean Workspace"
    DESCRIPTION = "Delete the imported footage and its renders."
    END = True
    DESTR = True
    WORD = "DELETE"
    SCOPE = Scope.FULL
    OUT = _both(_e(IMPORT, CLEAN_WS))
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
                                                   RENDER, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """The cheap half only.

        The heavy gates — rendered locally, and complete at the destination —
        are plan.guard, asked against a world captured at dispatch and again
        after the word is typed. The second goes to whatever was configured to
        publish, and asking that forty times a session makes the menu slow; a
        menu that is not instant stops being recomputed and starts being
        remembered.
        """
        return _first_block(
            _no_import(world, "nothing imported — nothing to clean up"),
            guards.nothing_to_clean_up(world))

    def _plan(self, world) -> Plan:
        return self._work.clean_workspace_plan(world)


# ---------------------------------------------------------------------------
# 9 — Delete SIM Data (DESTRUCTIVE, ends the cycle)
# ---------------------------------------------------------------------------

class DeleteSimData(Destructive):
    """Erase the card's clips, keeping its folders so the camera can record.

    Its outbound is StepBack: freeing the card does not interrupt the cycle,
    so completing hands the position back to whoever offered it — from which
    Clean Workspace is still reachable. The graph therefore permits the safe
    order (erase the card while its clips are provably in the workspace, clean
    the workspace once the renders are published) and forbids the dangerous
    one.
    """

    number = ERASE_CARD
    NAME = "Delete SIM Data"
    # The one refusal in the tool with a way past it. "These clips exist
    # nowhere else" is true and is sometimes not a reason to keep them: old
    # strays, a fragment the scanner would never render, footage the operator
    # has looked at and does not want. He is shown every path and the file
    # they were written to before he is asked.
    #
    # DELETE and not ERASE, which is this item's own word. Stepping past a
    # guard should not be reachable by the muscle memory of the erase it is
    # guarding: typing the usual word out of habit gets you nowhere here.
    OVERRIDE_WORD = "DELETE"
    DESCRIPTION = "Erase the card, once every clip is accounted for elsewhere."
    END = True
    DESTR = True
    WORD = "ERASE"
    # FULL, for the ADVISORY rather than for the guard. Every card guard is
    # local and would be answered identically at either scope; what needs the
    # target is _card_advisory, which says whether the workspace copy — the one
    # copy this erase is allowed on the strength of — is published yet. At
    # LOCAL scope a configured target reads UNKNOWN, so that line fired on every
    # card erase on every publishing install and told the operator nothing. A
    # warning that is always on is a warning nobody reads.
    SCOPE = Scope.FULL
    OUT = _both(StepBack())
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
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

    def override(self, world):
        """Drop the unaccounted clips ON PURPOSE, then erase normally.

        Excluding is what this tool already calls "this footage never
        happened, deliberately" -- item 4 does it per trip and every guard
        honours it. So the way past is not a flag that skips card_is_expendable:
        it records the decision, which makes the refusal false, and the erase
        then passes the same gates any erase passes and asks for the same word.
        """
        if not world.card.new_stamps:
            return None
        return self._work.drop_unaccounted_then_erase(world)


# The cold-start orientation: the first row that matches names where we are.
# Order is priority, and the FIRST row is a correctness rule rather than a
# progress marker — an exclusion record newer than the newest sidecar means
# the sidecars describe trips that no longer exist, so Exclude Trip's narrow
# outbound is restored across a session boundary and a restart cannot render
# stale meta.
#
# There is no row for Upload Website any more. It read a local deploy record
# that only one publishing arrangement wrote, and orientation runs at LOCAL
# scope precisely so a cold start never reaches the network — so the fact is
# now the target's and cannot be had here. The cost is one keypress: a restart
# mid-round on a publishing install lands on Build Website, whose outbound
# offers Upload Website. Position.orient says correctness does not depend on
# it, and it does not.
COLD_START_RULES = (
    (EXCLUDE, lambda w: bool(w.excluded) and w.excluded_at > w.newest_meta_at),
    # The destination's own answer comes first, because it is the only one that
    # knows anything about the half of the cycle this machine does not do. The
    # two rules under it read local artefacts that a configured install never
    # creates -- the self-contained page and the gather are the local edition's
    # deliverables -- so without this, publishing left no trace orientation
    # could see and every restart landed back at the renders.
    # The renders have to still be HERE. The destination answering yes is about
    # trips, not about this machine, and after a clean-up it goes on saying yes
    # about trips whose local copies are gone -- which put a swept, empty
    # workspace at "7) Upload Website" with nothing to upload. Published AND
    # still present is the state this rule is for: uploaded, not yet cleaned.
    (UPLOAD, lambda w: bool(w.renders) and w.target.complete is Evidence.YES),
    (BUILD, lambda w: w.local_page or bool(w.final_folders)),
    (RENDER, lambda w: bool(w.renders)),
    (PREVIEW, lambda w: w.stills_current),
    (META, lambda w: bool(w.metas)),
    (IMPORT, lambda w: bool(w.imports)),
)

ALL_ITEMS = (Progress, ImportSim, GenerateMeta, BuildPreview, ExcludeTrip,
             RenderVideos, BuildWebsite, UploadWebsite, CleanWorkspace,
             DeleteSimData)

NAMES = {cls.number: cls.NAME for cls in ALL_ITEMS}
