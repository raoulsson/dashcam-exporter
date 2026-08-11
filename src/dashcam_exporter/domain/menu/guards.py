"""Every guard, as a pure question about a passed-in world.

Not one guard was dropped in the move off the filesystem; what changed is that
each is now handed the evidence instead of going to find it. That makes them
mockable, testable without a fixture tree, and — the part that matters —
answerable twice against two worlds captured at two instants, which is how a
destructive item re-checks itself after the operator has typed the word.

The distinction that decides whether a check survives at all:

    a check about ORDER  ("has the earlier step run yet")  -> deleted; the
        graph says it, and an item that is not offered cannot be selected.
    a check about EVIDENCE ("what is on disk right now")   -> kept; the
        operator can delete a sidecar in Finder between the draw and the
        keypress, and a card can be swapped while the prompt is on screen.

The workspace gates are a class over ONE world rather than functions each
handed a world of their own. See Gates below for why that is not decoration.
"""

from __future__ import annotations

from typing import Optional, Tuple

from dashcam_exporter.domain.menu.menu import (
    Evidence, Verdict, blocked, go, satisfied, IMPORT)


def _is_blocking(verdict) -> bool:
    return verdict is not None and verdict.blocked


def _first_blocking(world, checks) -> Optional[Verdict]:
    verdicts = map(lambda check: check(world), checks)
    return next(filter(_is_blocking, verdicts), None)


# ---------------------------------------------------------------------------
# The card — item 9, Delete SIM Data. The only target with no second copy.
# ---------------------------------------------------------------------------

def never_imported(world) -> Optional[Verdict]:
    """No high-water mark at all: nothing says this footage exists elsewhere."""
    if world.ledger_mark:
        return None
    return blocked("card: nothing was ever imported — no record this footage "
                   "exists anywhere else")


def clips_never_copied(world) -> Optional[Verdict]:
    """Clips newer than the mark, and not excluded on purpose.

    Phrased as what they ARE rather than what has not happened to them. "were
    never imported" reads as a fault, and this is the ordinary state of a card
    you have just put in: footage waiting for the next round. It is greying out
    the erase, which is the whole point — there is nothing wrong here, and the
    line should not sound like there is.
    """
    if not world.card.new_stamps:
        return None
    return blocked("card: %d new clips ready for next session"
                   % len(world.card.new_stamps), world.card.new_files)


def copy_lost(world) -> Optional[Verdict]:
    """Per clip, with no gaps.

    The high-water mark records that a verified copy WAS made; it cannot notice
    that the copy was later deleted, moved to a disk that is not plugged in, or
    swept, and a cleanup can lift it from a render it never copied. Every clip
    on THIS card must instead be accounted for by something with per-clip
    authority — its stamp in the import manifest a verified copy actually wrote,
    a rendered trip whose meta span contains it, or the clip itself still in the
    workspace. Approving on the mark, or on any single accounted clip standing
    for the rest, is how a wipe erased clips whose only copy was the card.
    """
    if not world.card.owed_stamps:
        return None
    return blocked("card: %d clips exist nowhere but this card"
                   % len(world.card.owed_stamps), world.card.new_files)


CARD_GUARDS = (never_imported, clips_never_copied, copy_lost)


def card_is_expendable(world) -> Verdict:
    """Every clip on this card is provably somewhere else."""
    return _first_blocking(world, CARD_GUARDS) or go()


# ---------------------------------------------------------------------------
# The workspace — item 8, Clean Workspace. Two gates and one sentence.
# ---------------------------------------------------------------------------

class Gates:
    """Every gate item 8's sweep passes, asked of ONE world.

    A class for one reason, and it is the reason the module docstring above
    gives: a guard is a pure function of a FROZEN world, and two reads of one
    world must give one answer. As free functions that was left to discipline
    -- nothing stopped one screen asking clean_is_allowed about a world
    captured before the prompt and unproven_lines about one captured after,
    and the sentence printed over the delete would then be about a different
    instant from the verdict it explains. Bound once at construction, that
    cannot be written.

    Holds the world and nothing else. No caching, no state: the answers are
    re-derived per call as before, because a Gates is cheap and a second
    capture is the only honest way to get a second answer.
    """

    def __init__(self, world):
        self.world = world

    def rendered_locally(self) -> Evidence:
        """Did every renderable trip in this import actually get encoded?

        The hard floor: never NA, which is what keeps the rule below it total.
        UNKNOWN means the grouping could not be read, which is not the same as
        "the count is fine".
        """
        if not self.world.renders_here:
            return Evidence.NO
        return self._enough_renders()

    def _enough_renders(self) -> Evidence:
        if self.world.expected_trips is None:
            return Evidence.UNKNOWN
        return self._at_least(len(self.world.renders_here),
                              self.world.expected_trips)

    def _at_least(self, have: int, want: int) -> Evidence:
        return Evidence.YES if have >= want else Evidence.NO

    def complete_at_the_destination(self) -> Evidence:
        """Is EVERY trip of this import at the destination — held and served.

        The plugin's answer, frozen at capture. It used to be two questions asked
        per render (is the file there, is it being served) and folded together
        here; which destination it is, how it was asked and what "complete" means
        there is the implementation's business, and the one reading it produces is
        the guard's.

        All or nothing on purpose. This erases the WHOLE working area, so "which
        trips are there" is a finer answer than anything acts on — and asking about
        TRIPS rather than renders is what lets a trip that produced no render at
        all be covered by the question.
        """
        return self.world.target.complete

    # The two gates, in the order they are asked and printed. Unbound methods:
    # each entry is called against the Gates iterating it, so the labels and the
    # readings can never come from two different worlds.
    WORKSPACE_GATES = (
        ("rendered locally", rendered_locally),
        ("complete at the destination", complete_at_the_destination))

    def nothing_was_rendered_here(self) -> Optional[Verdict]:
        """The floor under the whole rule: not ONE mp4 from this import exists.

        Zero renders is the absence of the thing every later gate reasons about,
        and it is the state where a destination's YES is most likely to be about
        something else: sidecars under <out> outlive every sweep, so a machine that
        published last month can be asked about trips it published then while THIS
        import has not been encoded at all.

        So this is asked BEFORE the destination, and no answer overrides it —
        including an implementation that answers YES to everything, which is
        requirement A of the trust model and the reason this question is never
        delegated. The gate table on screen says "rendered locally .... no" one
        line above the confirmation prompt, and a gate the operator can read must
        be a gate.
        """
        if self.world.renders_here:
            return None
        return blocked("%s answered %s — nothing from this import was rendered"
                       % (self.WORKSPACE_GATES[0][0], Evidence.NO.value))

    def workspace_is_expendable(self) -> Verdict:
        """The floors first, and then the destination's answer decides.

        The floors never leave this machine, and that is the point: an
        implementation that answers YES to everything still cannot talk this into
        erasing an import that produced no renders, or one whose local render count
        is short, because the exporter does not delegate a question it can already
        answer.

        Below them there is one question left and one answer to it. NA — no plugin
        configured, or one that genuinely cannot speak about a destination — drops
        that gate rather than passing it, and the erase then rests on the local
        render count alone, which unproven_lines says out loud.
        """
        return self._floor_refusal() or self._decided_by_the_destination()

    def _floor_refusal(self) -> Optional[Verdict]:
        """The refusals no answer from a destination can lift, first one wins."""
        return next(filter(None, map(lambda ask: ask(self), self._FLOORS)), None)

    def _local_count_unproven(self) -> Optional[Verdict]:
        """A short or unreadable local render count is a floor too, not one vote.

        The destination is now asked about TRIP IDS, including trips that produced
        no render, so its answer does reach the trips a short count leaves at risk
        — an honest implementation says NO about a trip it never received. This
        floor costs nothing in that case and is not there for it.

        It is there for the case no answer from any destination can catch: the trip
        LIST itself being incomplete. The ids are read off the sidecars, so a trip
        whose sidecar was never written is in nobody's question, and an unreadable
        grouping means the count cannot be compared against anything at all. Both
        are states where the destination can answer YES truthfully while footage
        exists only in the import this step erases.

        The cost is a refusal when the grouping is not cached this session. That is
        a step to re-run, not footage to lose.
        """
        reading = self.rendered_locally()
        if reading is Evidence.YES:
            return None
        return self._verdict_of(self.WORKSPACE_GATES[0][0], reading)

    def _only_copy_is_here(self) -> Optional[Verdict]:
        """A clip this sweep would delete that exists nowhere else.

        The third floor, and the one the other two cannot see. They reason about
        TRIPS: how many were rendered, and whether the destination has them. A trip
        too short to render gets no sidecar, so it is in no trip_ids and counts
        toward no expected_trips — the local count is met by the renders that do
        exist, and the destination answers YES honestly about the trips it was
        asked about. The fragment's footage then goes with the sweep, having been
        accounted for by nothing but sitting in the folder about to be erased.

        Reachable in the order the graph calls SAFE: erase the card while its clips
        are provably in the workspace, then clean the workspace once the renders
        are published. That reasoning is sound for a trip that was rendered and
        silently false for one that never will be.

        The remedy is named rather than left to be worked out. Excluding is what
        this tool already calls "this footage never happened, deliberately": it
        records the decision, every guard honours it afterwards, and the delta
        import stops offering the clips back every cycle.
        """
        if not self.world.orphan_clips:
            return None
        n = len(self.world.orphan_clips)
        plural = "" if n == 1 else "s"
        # Two different facts, and only one of them is about the footage. With no
        # card in the slot nothing has been ruled out -- the clips may be sitting
        # safely on a card in the car -- and saying they exist nowhere else would
        # be asserting something nobody checked.
        if not self.world.card.dcim:
            return blocked(
                "%d clip%s here %s in no rendered trip, and there is no card to "
                "vouch for %s" % (n, plural, "is" if n == 1 else "are",
                                  "it" if n == 1 else "them"),
                evidence=self.world.orphan_clips)
        return blocked(
            "%d clip%s here exist%s nowhere else — not on the card, in no rendered "
            "trip, not excluded" % (n, plural, "s" if n == 1 else ""),
            evidence=self.world.orphan_clips)

    _FLOORS = (nothing_was_rendered_here, _local_count_unproven,
               _only_copy_is_here)

    def _decided_by_the_destination(self) -> Verdict:
        """NA drops this gate; anything else has to be a YES.

        A dropped gate is not a passed one — it is a gate that was never asked, and
        what is left standing is said out loud by unproven_lines rather than passed
        over in silence.
        """
        reading = self.complete_at_the_destination()
        if reading is Evidence.NA:
            return go()
        return self._verdict_of(self.WORKSPACE_GATES[1][0], reading)

    def _verdict_of(self, label: str, e: Evidence) -> Verdict:
        if e is Evidence.YES:
            return go()
        return blocked("%s answered %s" % (label, e.value))

    def gate_readings(self) -> Tuple[Tuple[str, Evidence], ...]:
        """(label, evidence) per gate, for the item to print before it asks."""
        return tuple((label, fn(self)) for label, fn in self.WORKSPACE_GATES)

    def destination_proof(self) -> str:
        """The answer a go rests on, or "" when the destination gave none.

        Named rather than assumed, because the banner above the prompt says
        which answer the erase is proceeding on and a plugin that declined the
        question never gave one. Attribution to an answer that was not given is
        worse than none: it is the last sentence before the footage goes, and a
        reader checking it afterwards is checking a sentence the plugin can
        truthfully deny.
        """
        if self.complete_at_the_destination().applicable:
            return self.WORKSPACE_GATES[1][0]
        return ""

    def unproven_lines(self) -> Tuple[str, ...]:
        """What could NOT be checked here, stated rather than passed over.

        Not decoration: with nothing configured to publish to, there is no proof of
        publication at all, so the renders under <out> are the only copy of that
        footage in the world. A guard that could not run says so.

        Two conditions, not the two this used to have. "No bucket" and "no site
        repo" were one operator's two settings for one condition and collapsed
        correctly. What did NOT exist before the interface is the second condition
        below: a plugin that IS configured and answers "not applicable" to whether
        the trips are complete. It leaves exactly the same hole as having no plugin
        at all — nothing off this machine was checked — and it used to say nothing,
        which is the one state where silence reads as proof.
        """
        return tuple(filter(None, (self._no_target_line(), self._declined_line())))

    def _no_target_line(self) -> str:
        if self.world.target.configured:
            return ""
        return ("no upload_plugin configured, so no copy off this machine was"
                " checked")

    def _declined_line(self) -> str:
        """A configured plugin that answered NA to the destination question.

        Not a complaint about the plugin: declining a question it genuinely cannot
        answer is what NA is for, and whoever configured it owns that. It is a
        statement about what this erase then rests on, which is the local render
        count and nothing else.
        """
        if self.destination_proof():
            return ""
        return self._declined_by(self.world.target)

    def _declined_by(self, target) -> str:
        if not target.configured:
            return ""               # already said, by _no_target_line
        return ("%s answered 'not applicable' to whether these trips are at the"
                " destination, so no copy off this machine was checked"
                % target.name)

    # -----------------------------------------------------------------------
    # Evidence the other items ask for. Each is about what is on disk, never
    # about what ran before.
    # -----------------------------------------------------------------------

    def _sidecar_debt_settled(self) -> bool:
        """No import awaiting a look, or the look has happened."""
        if not self.world.imports:
            return True
        return bool(self.world.metas)

    def sidecars_missing(self) -> Optional[str]:
        """An import is sitting there and nobody has written its sidecars.

        Survives an operator deleting them in Finder, which is why it is not the
        ordering check it resembles: an empty workspace settles the debt, an
        import without metas does not.
        """
        if self._sidecar_debt_settled():
            return None
        return "no sidecars on disk for the import"

    def nothing_to_clean_up(self) -> Optional[str]:
        """Item 8's cheap half: is there anything here it could be asked about.

        Sidecars are the usual answer -- an import with sidecars is an import the
        cycle has started on, and the heavy gates below decide whether it may go.
        But an import with none is not automatically untouchable, and treating it
        so is what made an interrupted first import a dead end: item 1 refused to
        import on top of two stray clips and pointed at item 8, and item 8 refused
        them for having no sidecars. Nothing had been made from those clips and
        the card still held every one; there was nothing to protect.

        The same is true of an import holding no clips at all, and for the same
        reason: no sidecars will ever be written for trips that no longer exist,
        so demanding them is demanding something nobody can supply.

        So an import with nothing to protect passes here, and its own evidence is
        checked again where it counts, in the plan and after the typed word.
        """
        if self.nothing_here_to_protect():
            return None
        return self.sidecars_missing()

    def nothing_here_to_protect(self) -> bool:
        """The two states where item 8 is not guarding anything.

        Either the source still holds every file of this import, or the import
        holds no footage at all. Both are asked in two places -- the cheap check
        that decides whether the entry is offered, and the heavy gate behind the
        typed word -- and they have to agree, or the menu offers a step that then
        refuses, or worse, hides one that would have been allowed. One predicate,
        so the two cannot drift apart.
        """
        return self.import_is_disposable() or self.import_holds_no_footage()

    def import_is_disposable(self) -> bool:
        """The source still holds every file of this import.

        That is the whole question, and it is asked per FILE against the card in
        the slot -- not a count, and never the ledger, which records that a copy
        was made once and cannot notice the card has since been wiped or swapped.

        It used to require, as well, that nothing had been MADE from the import:
        no sidecars, no renders, no gathered folder. That was the wrong second
        half. Sidecars and stills are derived and cost seconds; renders cost hours
        but are reproducible from the same footage. None of them is what the guard
        exists to protect, which is footage that exists in one place only. Once a
        step had run, the workspace could not be cleared at all without publishing
        first -- a scan of a card that turned out to be fragments left the operator
        holding a workspace he could neither use nor empty.

        So: the card has it, or it does not. Nothing downstream is weakened,
        because item 9 asks the same question from the other side -- wipe this
        workspace and its clips are owed again, so the card cannot be erased until
        they are somewhere else.

        An empty import is still not disposable by this route: no files means no
        file is missing from the card, and "all of it is safe" would be a yes
        about nothing.
        """
        return self._every_file_is_on_the_card()

    def _every_file_is_on_the_card(self) -> bool:
        """A card that IS the import cannot vouch for it, whatever the sets say."""
        if self.world.card_shares_the_import:
            return False
        return bool(self.world.import_files) and not self.world.unsourced_files

    def import_holds_no_footage(self) -> bool:
        """There is no clip left here, and nothing was made from one either.

        The state item 4 leaves behind when every trip of an import is excluded.
        Excluding DELETES the trip's clips, so an operator who drops all of them
        is left with a workspace of sidecars, GPS logs and tar archives, and not
        one frame of video.

        Every floor below is then asking about something that is not there.
        "Nothing from this import was rendered" is true and is no longer a reason
        to keep anything: what it protects is footage that was never encoded, and
        there is no footage. Same for a short local count and for clips whose only
        copy is here -- vacuous, both, over an import holding no clips.

        Deliberately BOTH halves. Renders empty is exactly the case the first
        floor blocks, so this adds a way through only there and nowhere else; and
        were a render present, the sweep would be throwing away hours of encoding
        on the strength of an empty import, which is a different question and one
        this does not answer.
        """
        if not self.world.imports:
            return False
        if self.world.renders_here:
            return False
        return not any(name.lower().endswith(".mp4")
                       for name in self.world.import_files)

    def clean_is_allowed(self) -> Verdict:
        """Item 8's heavy gate, three ways in.

        Two acts wear one number. Sweeping a finished cycle erases footage whose
        renders are published, and is decided by workspace_is_expendable. Throwing
        away an import nothing was made from erases footage the card still has,
        and is decided by import_is_disposable. The second is not a weakening of
        the first: it is available only when every gate the first would ask about
        has nothing to be asked about, and it refuses the moment one clip of that
        import is missing from the source.

        The third is neither: an import with no clips in it at all. Exclude every
        trip and that is what is left, and the operator could then clear nothing —
        the card had been erased on the strength of the exclusions, so the discard
        path was shut, and the sweep refused because an import that produced no
        footage had produced no renders either. A dead end reached by using item 4
        exactly as intended.
        """
        if self.nothing_here_to_protect():
            return go()
        return self.workspace_is_expendable()


# ---------------------------------------------------------------------------
# The questions that stand alone: one read of the world each, called from one
# place, calling nothing. They take the world directly because there is no
# second question to keep in step with — bind a world for one read and the
# binding is ceremony, not a guarantee.
# ---------------------------------------------------------------------------

def unsourced_lines(world) -> Tuple[str, ...]:
    """Why a discard is refused, per file rather than as a headcount."""
    if world.card_shares_the_import:
        return ("the configured card IS this import, so it vouches for"
                " nothing — check the card setting in config.txt",)
    missing = sorted(world.unsourced_files)
    if not missing:
        return ()
    return ("%d of the %d files in the import are not on the card: %s"
            % (len(missing), len(world.import_files), ", ".join(missing[:4])),)


def no_sidecars_at_all(world) -> Optional[str]:
    """Stricter than sidecars_missing: NOTHING has been written, anywhere.

    Publishing is the act of putting the trips' metadata online. With no
    sidecar in the tree there is nothing to say, and a deploy would push a
    manifest that describes no trips — a live site that comes up empty rather
    than one that refuses. An empty workspace does not settle this the way it
    settles sidecars_missing, which is exactly the difference between the two.
    """
    if world.metas:
        return None
    return "no sidecars anywhere — publishing them would put an empty index live"


def nothing_to_send(world):
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
    upload — run Build Website first", and only the second half of that was an
    ordering claim. The fact survives an operator deleting them in Finder.
    """
    if world.renders or world.trip_ids:
        return None
    return "nothing on disk to publish"


def nothing_left_to_do(world) -> Verdict:
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


def no_import(world, reason: str):
    """`reason` when there is no import, else None.

    The reasons say "no import" rather than "the workspace holds no import".
    Which container is empty is the machine's framing; the operator is looking
    at a greyed entry and wants to know what is missing.
    """
    if world.imports:
        return None
    return reason


def card_awaiting_import(world) -> Optional[str]:
    """A card in the slot with footage not yet imported takes precedence.

    The workspace can hold a PREVIOUS card's import -- the manifest lets a card
    be wiped and its laptop copy processed later -- so an import folder being
    present does not mean the card in the slot is the one it describes. When the
    slot card has clips to fetch, the operator's next move is to import it, and
    building meta now would describe the earlier card's footage under a screen
    that shows a new card in. Block the processing step and point at Import; the
    same condition Import uses to offer real work, so the two never disagree.

    Silent when there is no card, or when the card in the slot holds nothing new
    -- then the workspace import is the thing to work on and meta is available,
    exactly as before.
    """
    card = world.card
    if card.dcim and (card.new_stamps or card.to_fetch):
        n = len(card.new_stamps) or card.to_fetch
        return ("a card with %d clip%s not yet imported is in the slot — "
                "import it first (%d)" % (n, "" if n == 1 else "s", IMPORT))
    return None


def track_missing(world) -> Optional[str]:
    """Sidecars are built from the GPS track; no track, nothing to build."""
    if world.has_track:
        return None
    return "no GPS track in the import (no .gpx under DCIM) — sidecars need it"


def renders_exist(world) -> bool:
    """Renders to build a page from. A gathered final_ folder counts.

    The rebuild case: once item 5 has gathered, the loose renders are gone and
    the page must still be rebuildable from what is in final_.
    """
    return bool(world.renders or world.final_folders)


def nothing_to_build_from(world) -> Optional[str]:
    """The exporter's OWN question in front of item 5's delegation.

    A DESCRIBED trip is enough. Everything a page says about a drive — its
    route, its distance, its places, its map — comes from the sidecars item 2
    writes, and only the player needs the mp4. Demanding a render here is what
    made the site wait hours on an encode it does not read, and the page has
    always had a state for a drive whose video is not up yet.

    A gathered final_ folder counts too — the rebuild case, where the loose
    renders are gone because an earlier build moved them. Asked here rather
    than of the target because it is a fact about this machine: a target that
    answers yes to everything still cannot get a page built out of an empty
    tree.

    This is NOT the floor for the local edition, whose item 5 also gathers —
    see pipeline.LocalPage, which keeps the renders floor of its own.
    """
    if renders_exist(world) or world.metas:
        return None
    return "nothing described or rendered yet"
