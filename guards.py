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

from itertools import filterfalse
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
    """Clips newer than the mark, and not excluded on purpose."""
    if not world.card.new_stamps:
        return None
    return blocked("card: %d clip(s) were never imported" % len(world.card.new_stamps))


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
    return blocked("card: %d clip(s) exist nowhere but this card"
                   % len(world.card.owed_stamps))


CARD_GUARDS = (never_imported, clips_never_copied, copy_lost)


def card_is_expendable(world) -> Verdict:
    """Every clip on this card is provably somewhere else."""
    return _first_blocking(world, CARD_GUARDS) or go()


# ---------------------------------------------------------------------------
# The workspace — item 8, Clean Workspace. Three gates and one sentence.
# ---------------------------------------------------------------------------

def rendered_locally(world) -> Evidence:
    """Did every renderable trip in this import actually get encoded?

    The hard floor: never NA, which is what makes the unanimity rule total.
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


def on_the_bucket(world) -> Evidence:
    """Are those renders in the bucket, at a matching size?"""
    return world.bucket.covers(world.renders_here)


def published_on_the_site(world) -> Evidence:
    """What is-complete.py says the live site actually serves."""
    return world.site.published


WORKSPACE_GATES = (("rendered locally", rendered_locally),
                   ("present on the bucket", on_the_bucket),
                   ("published on the site (is-complete.py)", published_on_the_site))

WORKSPACE_GUARDS = tuple(fn for _label, fn in WORKSPACE_GATES)


def workspace_is_expendable(world) -> Verdict:
    """The site decides when it can be asked; otherwise every check that can
    answer must say yes.

    Today this is three gates, each carrying a hand-written "and there is no
    site to ask instead — refusing" branch. That shape says two things at
    once: is-complete.py is the authority on whether the raw footage may go,
    and without it nothing may be waved through as commentary. Written as one
    sentence it cannot be applied to two gates and forgotten on the third.

    Deliberately NOT "the last applicable check wins". That reading approves
    the wipe when the local render count is short or unreadable, there is no
    site_repo, and the renders that DO exist are on the bucket — and the trips
    that were never encoded exist in no render, no bucket, nowhere. Their
    footage would go. tests/test_guards.py enumerates all 48 combinations of
    the three gates against the branches this replaces.
    """
    if world.site.published.applicable:
        return _verdict_of(world.site.published)
    return _unanimous(world)


def _applicable(e: Evidence) -> bool:
    return e.applicable


def _not_yes(e: Evidence) -> bool:
    return e is not Evidence.YES


def _unanimous(world) -> Verdict:
    answers = map(lambda g: g(world), WORKSPACE_GUARDS)
    can_answer = filter(_applicable, answers)
    return _verdict_of(next(filter(_not_yes, can_answer), Evidence.YES))


def _verdict_of(e: Evidence) -> Verdict:
    if e is Evidence.YES:
        return go()
    return blocked("the check that decides answered %s" % e.value)


def gate_readings(world) -> Tuple[Tuple[str, Evidence], ...]:
    """(label, evidence) per gate, for the item to print before it asks."""
    return tuple((label, fn(world)) for label, fn in WORKSPACE_GATES)


def unproven_lines(world) -> Tuple[str, ...]:
    """What could NOT be checked here, stated rather than passed over.

    Not decoration: with neither bucket nor site there is no proof of
    publication at all, so the renders under <out> are the only copy of that
    footage in the world. A guard that could not run says so.
    """
    return tuple(filter(None, (_no_bucket_line(world), _no_site_line(world))))


def _no_bucket_line(world) -> str:
    if world.bucket.covers(()) is not Evidence.NA:
        return ""
    return "no s3_bucket in config.txt, so no copy off this machine was checked"


def _no_site_line(world) -> str:
    if world.site.published.applicable:
        return ""
    return "no site_repo in config.txt, so no published copy was checked"


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


def videos_link_wrong(world) -> Optional[str]:
    """public_html/videos must resolve to the tree we are about to verify.

    upload-videos-s3.sh derives its object keys from whatever that symlink
    points at, so verifying a different tree than the one uploaded is worse
    than not verifying.
    """
    if world.site.videos_link == world.out_dir:
        return None
    return ("public_html/videos -> %s but the render output is %s"
            % (world.site.videos_link, world.out_dir))


def uploads_outstanding(world):
    """Renders not yet in the bucket at a matching size.

    What makes an interrupted upload resumable, and what says whether item 7
    would do anything at all. A listing that failed proves nothing, so
    everything stays outstanding; trips the curation flagged mode=delete are
    deliberately absent and owe nothing.
    """
    owed = filterfalse(lambda r: _curation_deleted(world, r), world.renders)
    return tuple(filterfalse(lambda r: _in_bucket(world, r), owed))


def _curation_deleted(world, render) -> bool:
    return any(map(lambda i: i in render.name, world.site.curation_deleted))


def _in_bucket(world, render) -> bool:
    return world.bucket.holds(render.name, render.size) is Evidence.YES


def deploy_outstanding(world):
    """Renders the deploy record does not cover yet."""
    return tuple(filterfalse(lambda r: r.name in world.site.deployed, world.renders))
