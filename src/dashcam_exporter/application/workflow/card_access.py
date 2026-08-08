"""What the pipeline is allowed to know about a card: nothing.

Every question here used to be answered by spelling out DCIM/200video/front,
in nine places, which is why a card from any other camera was rejected as
"not a card" before an adapter ever saw it. The pipeline now asks these
questions instead, and the answers come from whichever adapter recognises
the tree.

This module is the ONLY route from the pipeline to the adapters. Nothing in
application/ imports infrastructure.adapters directly.
"""

from pathlib import Path

from dashcam_exporter.domain import Channel
from dashcam_exporter.infrastructure.adapters import (
    AmbiguousCard, CardLayout, NoAdapterFound, default_registry)

_LAYOUTS: dict[Path, CardLayout | None] = {}


def forget() -> None:
    """Drop the memoised layouts. For tests, and for a card being swapped."""
    _LAYOUTS.clear()


def layout_for(root) -> CardLayout | None:
    """The layout for whatever camera wrote this tree, or None if unknown.

    Memoised: clips() parses every filename on the card, and the menu asks
    these questions on every repaint.
    """
    if root is None:
        return None
    key = Path(root)
    if key in _LAYOUTS:
        return _LAYOUTS[key]
    layout: CardLayout | None
    try:
        layout = default_registry().detect(key).layout_for(key)
    except (NoAdapterFound, AmbiguousCard):
        layout = None
    _LAYOUTS[key] = layout
    return layout


def is_card(root) -> bool:
    return layout_for(root) is not None


def clip_count(root) -> int | None:
    """How many clips this tree holds, or None if it is not a card at all.

    None rather than zero is load-bearing: a folder that is not a card and a
    card that has been emptied are different answers, and the destructive
    paths read this one to decide whether erasing is safe.
    """
    layout = layout_for(root)
    return None if layout is None else len(layout.clips())


def stamps_on(root) -> set[str]:
    layout = layout_for(root)
    return set() if layout is None else {clip.timestamp for clip in layout.clips()}


def front_videos(root) -> list[Path]:
    """One path per clip, main camera, in recording order."""
    layout = layout_for(root)
    if layout is None:
        return []
    return [clip.videos.get(Channel.FRONT, clip.front) for clip in layout.clips()]


def is_track_artifact(root, path) -> bool:
    layout = layout_for(root)
    return False if layout is None else layout.is_track_artifact(Path(path))


def carries_track(root) -> bool:
    """Whether this tree holds any GPS at all, however the camera stores it."""
    layout = layout_for(root)
    if layout is None:
        return False
    for directory in layout.import_roots():
        if not directory.is_dir():
            continue
        for candidate in directory.rglob("*"):
            if candidate.is_file() and layout.is_track_artifact(candidate):
                return True
    return False
