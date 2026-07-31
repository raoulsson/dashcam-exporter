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
"""

from __future__ import annotations

from typing import Optional, Tuple

from menu import Evidence, Verdict, blocked, go


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
                   % len(world.card.new_stamps))


def copy_lost(world) -> Optional[Verdict]:
    """Per clip, with no gaps.

    The ledger records that a verified copy WAS made; it cannot notice that
    the copy was later deleted, moved to a disk that is not plugged in, or
    swept. Every clip on THIS card must be accounted for by something you can
    go and look at — a rendered trip whose meta span contains it, or the clip
    itself still in the workspace. Approving on any single accounted clip is
    how a wipe erased clips whose only copy was the card.
    """
    if not world.card.owed_stamps:
        return None
    return blocked("card: %d clips exist nowhere but this card"
                   % len(world.card.owed_stamps))


CARD_GUARDS = (never_imported, clips_never_copied, copy_lost)


def card_is_expendable(world) -> Verdict:
    """Every clip on this card is provably somewhere else."""
    return _first_blocking(world, CARD_GUARDS) or go()


# ---------------------------------------------------------------------------
# The workspace — item 8, Clean Workspace. Two gates and one sentence.
# ---------------------------------------------------------------------------

def rendered_locally(world) -> Evidence:
    """Did every renderable trip in this import actually get encoded?

    The hard floor: never NA, which is what keeps the rule below it total.
    UNKNOWN means the grouping could not be read, which is not the same as
    "the count is fine".
    """
    if not world.renders_here:
        return Evidence.NO
    return _enough_renders(world)


def _enough_renders(world) -> Evidence:
    if world.expected_trips is None:
        return Evidence.UNKNOWN
    return _at_least(len(world.renders_here), world.expected_trips)


def _at_least(have: int, want: int) -> Evidence:
    return Evidence.YES if have >= want else Evidence.NO


def complete_at_the_destination(world) -> Evidence:
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
    return world.target.complete


WORKSPACE_GATES = (("rendered locally", rendered_locally),
                   ("complete at the destination", complete_at_the_destination))


def nothing_was_rendered_here(world) -> Optional[Verdict]:
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
    line above the CLEAN prompt, and a gate the operator can read must be a
    gate.
    """
    if world.renders_here:
        return None
    return blocked("%s answered %s — nothing from this import was rendered"
                   % (WORKSPACE_GATES[0][0], Evidence.NO.value))


def workspace_is_expendable(world) -> Verdict:
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
    return _floor_refusal(world) or _decided_by_the_destination(world)


def _floor_refusal(world) -> Optional[Verdict]:
    """The refusals no answer from a destination can lift, first one wins."""
    return next(filter(None, map(lambda ask: ask(world), _FLOORS)), None)


def _local_count_unproven(world) -> Optional[Verdict]:
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
    reading = rendered_locally(world)
    if reading is Evidence.YES:
        return None
    return _verdict_of(WORKSPACE_GATES[0][0], reading)


_FLOORS = (nothing_was_rendered_here, _local_count_unproven)


def _decided_by_the_destination(world) -> Verdict:
    """NA drops this gate; anything else has to be a YES.

    A dropped gate is not a passed one — it is a gate that was never asked, and
    what is left standing is said out loud by unproven_lines rather than passed
    over in silence.
    """
    reading = complete_at_the_destination(world)
    if reading is Evidence.NA:
        return go()
    return _verdict_of(WORKSPACE_GATES[1][0], reading)


def _verdict_of(label: str, e: Evidence) -> Verdict:
    if e is Evidence.YES:
        return go()
    return blocked("%s answered %s" % (label, e.value))


def gate_readings(world) -> Tuple[Tuple[str, Evidence], ...]:
    """(label, evidence) per gate, for the item to print before it asks."""
    return tuple((label, fn(world)) for label, fn in WORKSPACE_GATES)


def destination_proof(world) -> str:
    """The answer a go rests on, or "" when the destination gave none.

    Named rather than assumed, because the banner above the CLEAN prompt says
    which answer the erase is proceeding on and a plugin that declined the
    question never gave one. Attribution to an answer that was not given is
    worse than none: it is the last sentence before the footage goes, and a
    reader checking it afterwards is checking a sentence the plugin can
    truthfully deny.
    """
    if complete_at_the_destination(world).applicable:
        return WORKSPACE_GATES[1][0]
    return ""


def unproven_lines(world) -> Tuple[str, ...]:
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
    return tuple(filter(None, (_no_target_line(world), _declined_line(world))))


def _no_target_line(world) -> str:
    if world.target.configured:
        return ""
    return ("no website_uploader configured, so no copy off this machine was"
            " checked")


def _declined_line(world) -> str:
    """A configured plugin that answered NA to the destination question.

    Not a complaint about the plugin: declining a question it genuinely cannot
    answer is what NA is for, and whoever configured it owns that. It is a
    statement about what this erase then rests on, which is the local render
    count and nothing else.
    """
    if destination_proof(world):
        return ""
    return _declined_by(world.target)


def _declined_by(target) -> str:
    if not target.configured:
        return ""               # already said, by _no_target_line
    return ("%s answered 'not applicable' to whether these trips are at the"
            " destination, so no copy off this machine was checked" % target.name)


# ---------------------------------------------------------------------------
# Evidence the other items ask for. Each is about what is on disk, never
# about what ran before.
# ---------------------------------------------------------------------------

def _sidecar_debt_settled(world) -> bool:
    """No import awaiting a look, or the look has happened."""
    if not world.imports:
        return True
    return bool(world.metas)


def sidecars_missing(world) -> Optional[str]:
    """An import is sitting there and nobody has written its sidecars.

    Survives an operator deleting them in Finder, which is why it is not the
    ordering check it resembles: an empty workspace settles the debt, an
    import without metas does not.
    """
    if _sidecar_debt_settled(world):
        return None
    return "no sidecars on disk for the import"


def nothing_to_clean_up(world) -> Optional[str]:
    """Item 8's cheap half: is there anything here it could be asked about.

    Sidecars are the usual answer -- an import with sidecars is an import the
    cycle has started on, and the heavy gates below decide whether it may go.
    But an import with none is not automatically untouchable, and treating it
    so is what made an interrupted first import a dead end: item 1 refused to
    import on top of two stray clips and pointed at item 8, and item 8 refused
    them for having no sidecars. Nothing had been made from those clips and
    the card still held every one; there was nothing to protect.

    So a disposable import passes here, and its own evidence is checked again
    where it counts, in the plan and after the typed word.
    """
    if import_is_disposable(world):
        return None
    return sidecars_missing(world)


def import_is_disposable(world) -> bool:
    """This import produced nothing and the source still holds all of it.

    Both halves are required and neither is a formality. "Produced nothing"
    means the footage has not been described, encoded or published, so no
    later step is relying on it and no copy of it exists anywhere downstream.
    "The source still holds it" is per FILE against the card in the slot, not
    a count and not the ledger: the ledger records that a copy was made once,
    which is exactly the claim that cannot notice the card has since been
    wiped or swapped.

    An empty import is not disposable by this route. It is the settled case
    sidecars_missing already lets through, and an empty set of files would
    otherwise satisfy "the source holds them all" vacuously.
    """
    if _something_was_made_from_it(world):
        return False
    return _every_file_is_on_the_card(world)


def _something_was_made_from_it(world) -> bool:
    return bool(world.metas or world.renders or world.final_folders)


def _every_file_is_on_the_card(world) -> bool:
    """A card that IS the import cannot vouch for it, whatever the sets say."""
    if world.card_shares_the_import:
        return False
    return bool(world.import_files) and not world.unsourced_files


def clean_is_allowed(world) -> Verdict:
    """Item 8's heavy gate, either way in.

    Two acts wear one number. Sweeping a finished cycle erases footage whose
    renders are published, and is decided by workspace_is_expendable. Throwing
    away an import nothing was made from erases footage the card still has,
    and is decided by import_is_disposable. The second is not a weakening of
    the first: it is available only when every gate the first would ask about
    has nothing to be asked about, and it refuses the moment one clip of that
    import is missing from the source.
    """
    if import_is_disposable(world):
        return go()
    return workspace_is_expendable(world)


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
    manifest that describes no drives — a live site that comes up empty rather
    than one that refuses. An empty workspace does not settle this the way it
    settles sidecars_missing, which is exactly the difference between the two.
    """
    if world.metas:
        return None
    return "no sidecars anywhere — publishing them would put an empty index live"


def track_missing(world) -> Optional[str]:
    """Sidecars are built from the GPS track; no track, nothing to build."""
    if world.has_track:
        return None
    return "no GPS track in the import (no .gpx under DCIM) — sidecars need it"


def renders_exist(world) -> bool:
    """Renders to build a page from. A gathered final_ folder counts.

    The rebuild case: once item 6 has gathered, the loose renders are gone and
    the page must still be rebuildable from what is in final_.
    """
    return bool(world.renders or world.final_folders)


def nothing_to_build_from(world) -> Optional[str]:
    """The exporter's OWN question in front of item 6's delegation.

    A gathered final_ folder counts — the rebuild case, where the loose
    renders are gone because an earlier build moved them. Asked here rather
    than of the target because it is a fact about this machine: a target that
    answers yes to everything still cannot get a page built out of an empty
    tree.
    """
    if renders_exist(world):
        return None
    return "no meta or renders"
