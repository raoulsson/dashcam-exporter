from abc import ABC, abstractmethod
from pathlib import Path

from dashcam_exporter.domain import Clip


class ExporterAdapter(ABC):
    """Camera-data adapter contract; renderers remain outside this interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable adapter name used in logs and configuration."""

    @abstractmethod
    def discover_clips(self, front_directory: Path, rear_directory: Path | None) -> list[Clip]:
        """Discover and pair source video clips."""

    @abstractmethod
    def prepare_gps(self, tar_directory: Path, cache_directory: Path) -> tuple[int, int]:
        """Extract camera-specific GPS archives into the generic cache."""
