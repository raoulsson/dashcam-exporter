import calendar
import re
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Clip

from .clip_repository import ClipRepository


class DdpaiClipRepository(ClipRepository):
    """DDPAI filename adapter; it tolerates a small front/rear clock skew."""

    _front_pattern = re.compile(r"^(\d{14})_(\d+)\.mp4$")
    _rear_pattern = re.compile(r"^(\d{14})_(\d+)_A\.mp4$")

    def __init__(self, rear_pair_tolerance_seconds: int = 2) -> None:
        self._rear_pair_tolerance_seconds = rear_pair_tolerance_seconds

    def find(self, front_directory: Path, rear_directory: Path | None) -> list[Clip]:
        front_files = self._front_files(front_directory)
        rear_files = self._rear_files(rear_directory)
        return [self._to_clip(timestamp, path, duration, rear_files)
                for timestamp, (path, duration) in sorted(front_files.items())]

    def _front_files(self, directory: Path) -> dict[str, tuple[Path, int]]:
        return {
            match.group(1): (file, int(match.group(2)))
            for file in sorted(directory.iterdir())
            if (match := self._front_pattern.match(file.name))
        }

    def _rear_files(self, directory: Path | None) -> dict[int, Path]:
        if directory is None or not directory.is_dir():
            return {}
        return {
            self._epoch(match.group(1)): file
            for file in sorted(directory.iterdir())
            if (match := self._rear_pattern.match(file.name))
        }

    def _to_clip(self, timestamp: str, front: Path, duration: int, rear_files: dict[int, Path]) -> Clip:
        epoch = self._epoch(timestamp)
        rear = self._closest_rear(epoch, rear_files)
        return Clip.paired(timestamp, epoch, duration, front, rear)

    def _closest_rear(self, epoch: int, rear_files: dict[int, Path]) -> Path | None:
        if not rear_files:
            return None
        candidate = min(rear_files, key=lambda value: abs(value - epoch))
        return rear_files[candidate] if abs(candidate - epoch) <= self._rear_pair_tolerance_seconds else None

    @staticmethod
    def _epoch(timestamp: str) -> int:
        parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        return calendar.timegm(parsed.timetuple())
