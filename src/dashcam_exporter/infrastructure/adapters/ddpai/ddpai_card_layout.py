import logging
import re
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.repository import DdpaiClipRepository

from ..card_layout import CardLayout
from .ddpai_track_source import DdpaiTrackSource

VIDEO_ROOT = "DCIM/200video"
GPS_ROOT = "DCIM/203gps"

_STAMP = re.compile(r"^[SQ]?_?(\d{14})_\d+(_\d+|_A)?\.mp4$", re.IGNORECASE)


class DdpaiCardLayout(CardLayout):
    """A DDPAI card: two video directories, and GPS in mislabeled tars.

    The card also carries 201photo, 202thumb and 207log -- several hundred
    megabytes this tool never reads, which is why import_roots names two
    directories rather than returning DCIM.
    """

    def __init__(self, card_root: Path, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._card_root = card_root
        self._clips = DdpaiClipRepository(rear_pair_tolerance_seconds)
        self._tracks = DdpaiTrackSource(card_root / GPS_ROOT / "tar", logger)

    def clips(self) -> list[Clip]:
        front = self._card_root / VIDEO_ROOT / "front"
        rear = self._card_root / VIDEO_ROOT / "rear"
        if not front.is_dir():
            return []
        return self._clips.find(front, rear)

    def stamp_of(self, path: Path) -> str | None:
        match = _STAMP.match(path.name)
        return match.group(1) if match else None

    def track_for(self, clip: Clip) -> Track | None:
        track = self._tracks.track_for_stamp(clip.timestamp)
        return None if track.is_empty else track

    def import_roots(self) -> tuple[Path, ...]:
        return (self._card_root / VIDEO_ROOT, self._card_root / GPS_ROOT)

    def is_track_artifact(self, path: Path) -> bool:
        return self._tracks.is_track_artifact(path)
