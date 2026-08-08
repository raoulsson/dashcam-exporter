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
build the graph; MenuGraph.inbound() computes the real one and
MenuGraph.disagreements() diffs the two.
"""

from __future__ import annotations

from dashcam_exporter.domain.menu.menu import (Anywhere, Destructive, Edges, Evidence, MenuItem, Plan,
                  Ruling, Scope, StartNode, StepBack, Strategy, Verdict,
                  blocked, go, satisfied,
                  PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                  BUILD, TRANSCRIBE, UPLOAD, CLEAN_WS, ERASE_CARD)
from dashcam_exporter.domain.menu.handover import Handover
from dashcam_exporter.domain.menu import guards


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
    ABOUT = (
        "Counts what is on disk and what has become of it: clips in the "
        "workspace, trips described, trips excluded, trips rendered. It shows "
        "work still owed rather than an inventory, so a line reading zero "
        "means there is nothing left to do, not that nothing is there.\n\n"
        "Looking is free and never moves the pipeline — this entry is a view, "
        "not a step, which is why it can be reached from anywhere and why "
        "standing on it is impossible. On an installation that publishes it "
        "also asks the destination what it already holds, so it can take a few "
        "seconds; the answer is what the later steps are judged against."
    )
    INBOUND_KIND = Anywhere
    OUT = Strategy.both(Anywhere())
    IN_AUTHORED = Strategy.both(None)

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
    ABOUT = (
        "Copies the DCIM tree into a dated folder in the workspace and then "
        "reads it back file for file. The source is any directory holding a "
        "DCIM tree, so a mounted card and a card already copied onto an "
        "external disk work the same way. Nothing is deleted here: the card "
        "keeps every clip until item 9 is run deliberately.\n\n"
        "An unfinished session refuses a new import. Importing on top of one "
        "mixes two cards into a single grouping with no record of which clip "
        "came from which card, and there is no way to unpick that afterwards. "
        "Clear the previous round with item 8 first; this entry offers no "
        "shortcut past it, because a wipe hidden inside a copy is a wipe "
        "nobody chose."
    )
    START = True
    INBOUND_KIND = StartNode
    # IMPORT leads to itself, exactly as GenerateMeta does. A card holds more
    # than one session's worth and the copy can be interrupted, so "import
    # again" is an ordinary next move rather than a restart -- the delta
    # decides how much, which is what evaluate() below already says. Without
    # the self-edge, orienting onto item 1 with footage in the workspace took
    # item 1 off the menu, and every entry it led to was blocked by its own
    # guard for want of a GPS track.
    OUT = Strategy.both(Edges.of(IMPORT, META, CLEAN_WS, ERASE_CARD))
    IN_AUTHORED = Strategy.both(None)

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
    ABOUT = (
        "Decides where one drive ends and the next begins, then writes a "
        "sidecar per trip: the day, the span, the track, the distance. A trip "
        "is the publishing unit and it is not an engine-on session and not a "
        "calendar day — it runs from pulling out to parking back at the "
        "anchor, so a stop somewhere else in the middle stays inside one "
        "trip however long it lasts — until 04:00, where a trip is closed "
        "whatever it was doing and the rest becomes the new day's. Nothing is "
        "lost at that boundary; it is a split, and it is what stops a drive "
        "that never comes home from growing until the card runs out. "
        "Departure and arrival are read from the "
        "footage itself, by optical flow, because GPS in a car park is late "
        "or absent.\n\n"
        "Run it again whenever the grouping should change — after excluding a "
        "trip, or after adding clips. It rebuilds rather than skipping what "
        "exists, so the sidecars always describe the footage that is there "
        "now. It reads clips and writes small text files; a card takes about "
        "a minute, and nothing it does is irreversible."
    )
    OUT = Edges.and_upload(META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE, BUILD, CLEAN_WS,
                           ERASE_CARD)
    IN_AUTHORED = Strategy.both(frozenset((META, IMPORT, EXCLUDE)))

    def evaluate(self, world) -> Verdict:
        """Evidence only.

        "Has the import run" is not asked as an ORDER question — item 2 is
        reachable from itself and from Exclude Trip, so an emptied workspace
        is genuinely reachable here and the answer is that there is no work,
        not that a step was skipped. The GPS track check is pure evidence:
        sidecars are built from the track, and a source without one produces
        nothing however many times it is run.
        """
        return Verdict.first_block(
            guards.no_import(world, "no import — nothing to build meta from"),
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
    ABOUT = (
        "Pulls one frame out of each trip and lays them out as a contact "
        "sheet, so a card can be judged by eye in a few seconds instead of by "
        "reading timestamps. It lets you answer the question, which trips you "
        "want to keep or exclude from the current SIM card export.\n\n"
        "It writes stills and nothing else — no sidecar, no video, nothing "
        "sent anywhere — so it cannot change what the later steps see and it "
        "is safe to run at any point. Running it again rebuilds the stills "
        "from scratch rather than adding to them, which is what you want after "
        "the grouping has moved."
    )
    # Stills and a contact sheet. It writes no sidecar and sends nothing
    # anywhere, so neither the trip list nor the destination has moved.
    CHANGES_THE_QUESTION = False
    OUT = Edges.and_upload(PREVIEW, META, EXCLUDE, RENDER, TRANSCRIBE, BUILD, CLEAN_WS,
                           ERASE_CARD)
    IN_AUTHORED = Strategy.both(frozenset((PREVIEW, META, EXCLUDE)))

    def evaluate(self, world) -> Verdict:
        """The sidecars have to be ON DISK, not merely to have been written.

        This looks like the ordering check it replaced and is not: nothing
        stops the operator deleting a sidecar in Finder between the menu draw
        and the keypress, and an empty workspace settles the question rather
        than failing it.
        """
        return Verdict.first_block(
            guards.no_import(world, "no import — nothing to preview"),
            guards.Gates(world).sidecars_missing())

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
    ABOUT = (
        "Deletes the original clips of the trips you name, so nothing "
        "downstream ever sees them again. This is the one place footage is "
        "dropped on purpose, and it asks you to type EXCLUDE before anything "
        "goes. A blank selection, an index that is not on the list, or "
        "anything but the word at the prompt leaves everything exactly as it "
        "was.\n\n"
        "The decision is recorded against the trip rather than against the "
        "files, so when the same footage turns up on a later card it is "
        "already known to be unwanted and does not have to be judged twice."
    )
    DESTR = True
    # DELETE, like item 8. The two erase different things and both erase from
    # the WORKSPACE; the card keeps its own word, because that is the one with
    # no second copy behind it.
    # EXCLUDE, the entry's own verb. This asked for DELETE, and delete is what
    # item 8 and item 9 do -- they remove footage and record nothing about it.
    # This one records a DECISION, against the trip, honoured for good: the
    # delta import stops offering those clips and a rebuild downstream knows
    # the trip was dropped rather than merely cleaned up. Asking for the word
    # that names the other act taught the wrong idea of what was happening.
    WORD = "EXCLUDE"
    SCOPE = Scope.FULL          # the only-copy warning has to ask the target
    OUT = Edges.and_upload(EXCLUDE, META, PREVIEW, TRANSCRIBE, CLEAN_WS, ERASE_CARD)
    IN_AUTHORED = Strategy.both(frozenset((EXCLUDE, META, PREVIEW)))

    def evaluate(self, world) -> Verdict:
        return Verdict.first_block(
            guards.no_import(world, "no import — nothing to exclude"))

    def _plan(self, world) -> Plan:
        return self._work.exclude_plan(world)


# ---------------------------------------------------------------------------
# 6 — Render Trips
# ---------------------------------------------------------------------------

class RenderVideos(MenuItem):
    number = RENDER
    NAME = "Render Trips"
    DESCRIPTION = "Encode the trips into watchable videos. Hours for a full card."
    ABOUT = (
        "Encodes one video per trip with the timestamp, speed, map and stats "
        "burned in. Parking inside a trip is cut out and replaced by a "
        "fast-forward slide, and the cuts are placed off the footage rather "
        "than off GPS — the slide arrives a few seconds after the wheels stop, "
        "and the picture comes back a few seconds before they turn again — so "
        "an hour parked at a supermarket costs the viewer three seconds. The "
        "trip therefore plays continuously even though the camera stopped and "
        "started several times inside it.\n\n"
        "You can interrupt the rendering with ctrl-c and restart it later. It "
        "will pick up where it was left from."
    )
    # An encode produces mp4s. The trips were already described, so the list
    # is_complete() is asked about is the same list, and nothing has been
    # published — which is the question it answers.
    CHANGES_THE_QUESTION = False
    # RESTORED TO THE OWNER'S TABLE: he gave item 6 an outbound edge to 7 and
    # it was taken away, on the reasoning that item 7 uploads the BUILT site so
    # reaching it from Render skips the building. That reasoning was wrong
    # about what a render produces. An encode makes an mp4 and no metadata at
    # all, so the manifest built before it is still correct after it, and
    # sending the videos is the whole of what is left to do. The removed edge
    # bought nothing and cost a rebuild after every render.
    OUT = Edges.and_upload(RENDER, META, PREVIEW, EXCLUDE, TRANSCRIBE, BUILD, CLEAN_WS,
                           ERASE_CARD)
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({RENDER, META, PREVIEW, EXCLUDE, BUILD}),
    }

    def evaluate(self, world) -> Verdict:
        """Render reads the COPIED import, never the source itself.

        A mounted card is not a workspace: that distinction is evidence about
        two different directories, not a statement about which step ran first.
        """
        return Verdict.first_block(
            guards.no_import(world,
                       "nothing to render"),
            guards.Gates(world).sidecars_missing())

    def _perform(self, world):
        return self._work.render(world)


# ---------------------------------------------------------------------------
# 7 — Transcribe Trips
# ---------------------------------------------------------------------------

class TranscribeTrips(MenuItem):
    """Generate admin transcript sidecars from whatever rendered MP4s exist."""

    number = TRANSCRIBE
    NAME = "Transcribe Trips"
    DESCRIPTION = "Generate a transcript from the rendered videos."
    ABOUT = ("Extracts the audio from every rendered MP4 and writes a text "
             "transcript plus paragraph timeline beside it. You can choose "
             "plain transcription or speaker diarization each run. Existing "
             "and partial renders are both eligible.")
    # Transcription is repeatable, but it must not become a dead-end: after it
    # finishes the operator can rebuild, upload, clean, or erase just as after
    # rendering. The uploader strategy adds Upload Website to the same edge.
    OUT = Edges.and_upload(IMPORT, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE,
                           BUILD, CLEAN_WS, ERASE_CARD)
    IN_AUTHORED = Strategy.both(frozenset({META, PREVIEW, EXCLUDE, RENDER, BUILD, UPLOAD}))

    def evaluate(self, world) -> Verdict:
        return go() if world.renders else blocked("no rendered MP4s to transcribe")

    def _perform(self, world):
        return self._work.transcribe(world)


# ---------------------------------------------------------------------------
# 5 — Build Website
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
    DESCRIPTION = "Build the website from the described trips."
    # DEVIATION FROM THE OWNER'S TABLE: 7 added to the outbound under the
    # publishing edition. His inbound column for item 7 says {7,5} — build the
    # site, then put it online — but no outbound set anywhere offered 7, so
    # Upload Website was unreachable by its own natural route. This is the edge
    # that makes publishing work.
    OUT = {
        Strategy.UPLOADER: Edges.of(BUILD, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE,
                                    UPLOAD, CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_PAGE: Edges.of(BUILD, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE,
                                      CLEAN_WS, ERASE_CARD),
    }
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE, UPLOAD}),
        Strategy.LOCAL_PAGE: frozenset({BUILD, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE}),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        # The strategy branch, resolved once. It must not reappear as an `if`
        # in _perform.
        self._builder = work.builder(strategy)
        self._handover = Handover(work, self._builder)

    def description(self) -> str:
        """What THIS installation's build does, asked of the thing that does it.

        Not a statement about how the tool is installed — that is the session's
        to say, once, at startup. It is this entry answering for its own job,
        which genuinely differs between the two products.
        """
        return self._builder.describe()

    def about(self) -> str:
        """The two modes, then whatever the installed plugin says about its own.

        The two sentences are fixed, because the CHOICE is the same on every
        installation. What follows them is not ours to write: where a site ends
        up, what is public, what a second run does differently — a plugin that
        publishes somewhere we have never heard of is the only thing that can
        say, so its answer goes in verbatim.

        Where the local edition writes is named as a real path rather than as
        <dir>: the operator is being told where to go and look.
        """
        where = self._work.site_dir()
        return self._handover.about(
            "This step builds the website out of the trips that have been "
            "described. In local mode, a simple website gets generated and "
            "placed in:\n\t%s\n\nIf you configured a plugin, the operational "
            "details of the process are delegated to the plugin."
            % (where or "<export_dir>"))

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
        verdict = self._builder.evaluate(world)
        # Build Website is an explicit command, not an idempotent
        # postcondition.  A plugin may reasonably report SATISFIED when its
        # manifest already matches the workspace, but pressing menu 5 still
        # means “rebuild it now” (for example after changing a template or
        # curation on the plugin side).  Preserve refusals, while making a
        # satisfied builder selectable so execute() is reached every time.
        return go() if verdict.ruling is Ruling.SATISFIED else verdict

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
    # publishing edition. He wrote 7 into item 5's INBOUND column — after
    # uploading you fix a caption and rebuild — and left it out of item 7's
    # outbound. Deriving the inbound would have silently deleted an edge he
    # authored.
    OUT = {
        Strategy.UPLOADER: Edges.of(UPLOAD, META, PREVIEW, EXCLUDE, RENDER, TRANSCRIBE,
                                    BUILD, CLEAN_WS, ERASE_CARD),
        Strategy.LOCAL_PAGE: Edges.of(),
    }
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({UPLOAD, BUILD, TRANSCRIBE}),
        Strategy.LOCAL_PAGE: frozenset(),
    }

    def __init__(self, strategy, work, inbound):
        super().__init__(strategy, work, inbound)
        self._publish = work.publisher(strategy)
        self._handover = Handover(work, self._publish)

    def description(self) -> str:
        """What THIS installation's upload does, asked of the thing that does it.

        The same rule as item 5's row: the entry answers for its own job rather
        than restating how the tool is installed.
        """
        return self._publish.describe()

    def about(self) -> str:
        """The two modes, then whatever the installed plugin says about its own.

        The same shape as item 5, and for the same reason: which mode you are
        in is the exporter's to explain, and where the files actually go is
        not. A destination we have never heard of can only be described by the
        thing that speaks to it.
        """
        where = self._work.website_export_dir()
        return self._handover.about(
            "This step uploads the built website to its destination. In local "
            "mode, the website is copied to the specified directory:\n\t%s\n\n"
            "If you configured a plugin, the operational details of the "
            "process are delegated to the plugin."
            % (where or "<default_website_export_dir>"))

    def evaluate(self, world) -> Verdict:
        """The exporter's own evidence first, then the uploader, then the
        frozen answer about the destination.

        Both local checks are evidence, not order: something to send, and a
        sidecar somewhere to describe it. The sidecar one came across from the
        folded-in deploy step — publishing is putting the trips' metadata
        online, and with no sidecar anywhere the deploy pushes an index
        describing no trips.
        """
        stop = Verdict.first_reason(guards.nothing_to_send(world),
                                    guards.no_sidecars_at_all(world))
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
        return guards.nothing_left_to_do(world)

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
    ABOUT = (
        "Empties the working area: the imported clips, the sidecars and the "
        "renders made from them. It asks you to type CLEAN first. Whether "
        "this is cheap or serious depends on where the originals are — if the "
        "card still holds them it removes a second copy, and if the card has "
        "already been erased it removes the only one. The screen says which "
        "of the two you are looking at before it asks.\n\n"
        "It refuses while any trip is neither rendered nor excluded, and names "
        "the trips and the remedy. That refusal is the point of the step: "
        "footage nobody accounted for is footage about to vanish on no one's "
        "decision, and the way past is item 4, where dropping a trip is a "
        "decision that gets recorded. Once the workspace is gone the only way "
        "on is a new import, so this genuinely ends a cycle."
    )
    END = True
    DESTR = True
    # CLEAN, the entry's own verb, as at item 4. What this does is empty the
    # working area of copies whose originals are published or still on the
    # card; DELETE is item 9's word, and it names an act with nothing behind
    # it.
    WORD = "CLEAN"
    # NO way past. Item 4 is the only place a trip is dropped on purpose, and
    # it is where this sends him: a decision made there is recorded against the
    # trip and honoured for good, so the same footage arriving on a later card
    # is known to be unwanted. A word typed here would drop the same clips
    # without any of that, and the next import would offer them back.
    SCOPE = Scope.FULL
    OUT = Strategy.both(Edges.of(IMPORT, CLEAN_WS))
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD, TRANSCRIBE}),
        Strategy.LOCAL_PAGE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
                                                   RENDER, BUILD, TRANSCRIBE}),
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
        return Verdict.first_block(
            guards.no_import(world, "nothing imported — nothing to clean up"),
            guards.Gates(world).nothing_to_clean_up())

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
    # ERASE and not DELETE, which is this item's own word. Stepping past a
    # guard should not be reachable by the muscle memory of the erase it is
    # guarding: typing the usual word out of habit gets you nowhere here.
    #
    # THE TWO WERE THE OTHER WAY ROUND. The entry is called Delete SIM Data and
    # then asked for ERASE, which is a name and a password for the same act
    # disagreeing on the screen where the operator is being asked to be sure.
    # DELETE is now the word, matching the name and matching items 4 and 8 —
    # what is given up is that the card no longer has a word of its own, and
    # what that was worth was never the safety here. The guard is the per-clip
    # accounting; the word only confirms he meant it. The property that DOES
    # matter is the one below, and it survives the swap intact.
    OVERRIDE_WORD = "ERASE"
    DESCRIPTION = "Erase the card, once every clip is accounted for elsewhere."
    ABOUT = (
        "Erases the card's clips and keeps its folder tree, so the camera can "
        "record again without rebuilding anything — remove the folders too and "
        "it sits on \"loading card\" instead. It asks you to type DELETE. The "
        "guard is per clip, not per card: every file has to be provably "
        "somewhere else before any of them go, and the check is the accounting "
        "rather than the word.\n\n"
        "This is the one refusal in the tool with a way past it, because "
        "\"these clips exist nowhere else\" is true and is sometimes not a "
        "reason to keep them — old strays, a fragment nothing would ever "
        "render, footage you have looked at and do not want. You are shown "
        "every such path and the file they were written to before being asked, "
        "and the way past asks for ERASE rather than DELETE, so the habit of "
        "typing the usual word cannot carry you through the guard. Freeing the "
        "card does not end the cycle: it hands the position back to wherever "
        "you came from, and cleaning the workspace is still ahead of you."
    )
    # The card is not the workspace and not the destination. Erasing it leaves
    # both the trip list and what is published exactly as they were.
    CHANGES_THE_QUESTION = False
    END = True
    DESTR = True
    WORD = "DELETE"
    # FULL, for the ADVISORY rather than for the guard. Every card guard is
    # local and would be answered identically at either scope; what needs the
    # target is _card_advisory, which says whether the workspace copy — the one
    # copy this erase is allowed on the strength of — is published yet. At
    # LOCAL scope a configured target reads UNKNOWN, so that line fired on every
    # card erase on every publishing install and told the operator nothing. A
    # warning that is always on is a warning nobody reads.
    SCOPE = Scope.FULL
    OUT = Strategy.both(StepBack())
    IN_AUTHORED = {
        Strategy.UPLOADER: frozenset({IMPORT, META, PREVIEW, EXCLUDE, RENDER,
                                          BUILD, UPLOAD, TRANSCRIBE}),
        Strategy.LOCAL_PAGE: frozenset({IMPORT, META, PREVIEW, EXCLUDE,
                                                   RENDER, BUILD, TRANSCRIBE}),
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
             RenderVideos, TranscribeTrips, BuildWebsite, UploadWebsite,
             CleanWorkspace, DeleteSimData)

NAMES = {cls.number: cls.NAME for cls in ALL_ITEMS}
