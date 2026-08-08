import calendar
import re
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Clip, ClipMode

from .clip_repository import ClipRepository

# Two grammars, both taken off a real card. The ordinary one is
# <stamp>_<duration>.mp4 with its rear partner suffixed _A. Alongside it sit
# S_ front files paired with Q_ rear files, carrying an extra field before
# the duration: S_20260807142001_0870_0030.mp4 runs 29.97 seconds, so the
# LAST field is the duration and 0870 is something this tool does not know.
# What S and Q mean is equally unknown, which is why the mode is OTHER with
# the letter kept verbatim rather than a guess dressed up as EVENT.
_FRONT_PATTERNS = (
    (re.compile(r"^(\d{14})_(\d+)\.mp4$", re.IGNORECASE), ClipMode.NORMAL, ""),
    (re.compile(r"^S_(\d{14})_\d+_(\d+)\.mp4$", re.IGNORECASE), ClipMode.OTHER, "S"),
)
_REAR_PATTERNS = (
    re.compile(r"^(\d{14})_\d+_A\.mp4$", re.IGNORECASE),
    re.compile(r"^Q_(\d{14})_\d+_\d+\.mp4$", re.IGNORECASE),
)


class DdpaiClipRepository(ClipRepository):
    """DDPAI filename adapter; it tolerates a small front/rear clock skew."""

    def __init__(self, rear_pair_tolerance_seconds: int = 2) -> None:
        self._rear_pair_tolerance_seconds = rear_pair_tolerance_seconds

    def find(self, front_directory: Path, rear_directory: Path | None) -> list[Clip]:
        front_files = self._front_files(front_directory)
        rear_files = self._rear_files(rear_directory)
        return [self._to_clip(timestamp, found, rear_files)
                for timestamp, found in sorted(front_files.items())]

    def _front_files(self, directory: Path) -> dict[str, tuple[Path, int, ClipMode, str]]:
        found: dict[str, tuple[Path, int, ClipMode, str]] = {}
        for file in sorted(directory.iterdir()):
            for pattern, mode, source_mode in _FRONT_PATTERNS:
                match = pattern.match(file.name)
                if match:
                    found[match.group(1)] = (file, int(match.group(2)), mode,
                                             source_mode)
                    break
        return found

    def _rear_files(self, directory: Path | None) -> dict[int, Path]:
        if directory is None or not directory.is_dir():
            return {}
        found: dict[int, Path] = {}
        for file in sorted(directory.iterdir()):
            for pattern in _REAR_PATTERNS:
                match = pattern.match(file.name)
                if match:
                    found[self._epoch(match.group(1))] = file
                    break
        return found

    def _to_clip(self, timestamp: str,
                 found: tuple[Path, int, ClipMode, str],
                 rear_files: dict[int, Path]) -> Clip:
        front, duration, mode, source_mode = found
        epoch = self._epoch(timestamp)
        rear = self._closest_rear(epoch, rear_files)
        return Clip.paired(timestamp, epoch, duration, front, rear, mode,
                           source_mode)

    def _closest_rear(self, epoch: int, rear_files: dict[int, Path]) -> Path | None:
        if not rear_files:
            return None
        candidate = min(rear_files, key=lambda value: abs(value - epoch))
        return rear_files[candidate] if abs(candidate - epoch) <= self._rear_pair_tolerance_seconds else None

    @staticmethod
    def _epoch(timestamp: str) -> int:
        parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        return calendar.timegm(parsed.timetuple())
