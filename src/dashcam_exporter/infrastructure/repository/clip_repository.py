from abc import ABC, abstractmethod
from pathlib import Path

from dashcam_exporter.domain import Clip


class ClipRepository(ABC):
    """Port for retrieving chronological clips from a footage source."""

    @abstractmethod
    def find(self, front_directory: Path, rear_directory: Path | None) -> list[Clip]:
        """Return source clips in ascending recording-time order."""
