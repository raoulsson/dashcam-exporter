"""Writing card trees that look like the real thing.

The DDPAI simulator is calibrated: a real card's directory skeleton and a
real import's filename grammar were both read off this machine. The
BlackVue and VIOFO simulators are NOT. They are built from manuals and from
open-source parsers, and the adapters that read them were built from the
same documents -- so agreement between the two proves they were written
consistently, and nothing about a real camera.
"""

import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

CLIP_SECONDS = 2
FRAME_SIZE = "320x180"


class CardSimulator(ABC):
    """Writes one camera's idea of a card to an empty directory."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Matches the adapter name this card should be detected as."""

    @abstractmethod
    def write(self, card_root: Path, clips: int) -> None:
        """Create the tree, the videos and the telemetry."""

    def _write_clip(self, path: Path, seed: int) -> None:
        """A real, playable MP4 -- short, small, and visibly numbered.

        Real files rather than touched empty ones because an adapter that
        reads telemetry out of a container cannot be exercised by a file
        with no container in it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi",
             "-i", "testsrc=size=%s:rate=10:duration=%d"
                   % (FRAME_SIZE, CLIP_SECONDS),
             "-metadata", "comment=simulated clip %d" % seed,
             "-pix_fmt", "yuv420p", str(path)],
            check=True)

    @staticmethod
    def _clip_times(clips: int, start: datetime,
                    step_seconds: int = 60) -> list[datetime]:
        return [start + timedelta(seconds=step_seconds * index)
                for index in range(clips)]
