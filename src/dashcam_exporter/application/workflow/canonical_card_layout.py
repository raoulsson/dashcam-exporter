from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.adapters import CardLayout

from .canonical_workspace import CanonicalWorkspace

CANONICAL = "canonical"


class CanonicalCardLayout(CardLayout):
    """The same five questions, answered by a workspace nobody's camera wrote.

    Once an import has been normalised there is no adapter left to consult:
    the names are ours, the track files are ours, and the manifest holds what
    the names could not. Implementing the contract over that shape means
    every caller keeps asking the same questions and never learns whether it
    is looking at a card or at what a card became.
    """

    def __init__(self, workspace: CanonicalWorkspace) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return CANONICAL

    def clips(self) -> list[Clip]:
        return sorted(self._workspace.clips(), key=lambda clip: clip.timestamp)

    def stamp_of(self, path: Path) -> str | None:
        stem = Path(path).stem
        head = stem.split("_")[0]
        return head if len(head) == 14 and head.isdigit() else None

    def track_for(self, clip: Clip) -> Track | None:
        return self._workspace.track_for(clip)

    def import_roots(self) -> tuple[Path, ...]:
        return (self._workspace.clips_dir, self._workspace.tracks_dir,
                self._workspace.images_dir, self._workspace.logs_dir)

    def is_track_artifact(self, path: Path) -> bool:
        return Path(path).suffix.lower() == ".json" and \
            Path(path).parent.name == self._workspace.tracks_dir.name
