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
_NAMES: dict[Path, str | None] = {}
_FORCED: str | None = None


def forget() -> None:
    """Drop the memoised layouts. For tests, and for a card being swapped."""
    _LAYOUTS.clear()
    _NAMES.clear()


def use_adapter(name: str | None) -> None:
    """Force one adapter by name, from config.txt's `adapter` key.

    Detection is normally enough. This exists for the case detection cannot
    settle -- a card whose layout two adapters both recognise, or a camera
    whose firmware changed under a name we already know -- and for saying
    plainly in the status which one is at work.
    """
    global _FORCED
    _FORCED = name or None
    forget()


def forced_adapter() -> str | None:
    return _FORCED


def layout_for(root) -> CardLayout | None:
    """The layout for this tree, or None when nothing recognises it.

    A normalised workspace answers for itself and no adapter is consulted:
    once the names and the track files are ours, there is no camera left in
    the tree to detect. Otherwise the registry decides, or the operator has
    already decided for it.

    Memoised: clips() parses every filename, and the menu asks these
    questions on every repaint.
    """
    if root is None:
        return None
    key = Path(root)
    if key not in _LAYOUTS:
        _LAYOUTS[key], _NAMES[key] = _resolve(key)
    return _LAYOUTS[key]


def _resolve(key: Path):
    """(layout, adapter name). The name is the adapter's, not the layout's."""
    from .canonical_card_layout import CANONICAL, CanonicalCardLayout
    from .canonical_workspace import CanonicalWorkspace

    workspace = CanonicalWorkspace(key)
    if workspace.is_normalized:
        return CanonicalCardLayout(workspace), CANONICAL
    try:
        adapter = default_registry().detect(key, forced=_FORCED)
    except (NoAdapterFound, AmbiguousCard):
        return None, None
    return adapter.layout_for(key), adapter.name


def adapter_name(root) -> str | None:
    """Which adapter is at work on this tree, for the operator to read.

    "canonical" is an honest answer rather than a missing one: it means the
    import has been normalised and no camera's grammar is in use at all.
    """
    layout_for(root)
    return _NAMES.get(Path(root)) if root is not None else None


def is_card(root) -> bool:
    return layout_for(root) is not None


def card_root_of(path):
    """The nearest ancestor of this path that some adapter recognises.

    For callers handed a directory INSIDE a card -- the renderer is given a
    video directory on the command line -- rather than the card itself.
    Walking up beats counting parents: DCIM/200video/front is three levels
    down and BlackVue/Record is two, so any fixed number is one camera's
    number.
    """
    if path is None:
        return None
    start = Path(path)
    for candidate in [start, *start.parents]:
        if is_card(candidate):
            return candidate
    return None


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
