import logging
import os
import tarfile
from pathlib import Path

from dashcam_exporter.domain import Clip
from dashcam_exporter.infrastructure.repository import DdpaiClipRepository

from .exporter_adapter import ExporterAdapter


class DdpaiDataAdapter(ExporterAdapter):
    """DDPAI source adapter; no video rendering belongs here."""

    def __init__(self, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._clips = DdpaiClipRepository(rear_pair_tolerance_seconds)
        self._logger = logger or logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return "ddpai"

    def discover_clips(self, front_directory: Path, rear_directory: Path | None) -> list[Clip]:
        return self._clips.find(front_directory, rear_directory)

    def prepare_gps(self, tar_directory: Path, cache_directory: Path) -> tuple[int, int]:
        """Extract `.gpx` members from DDPAI's mislabeled `.git` tar files.

        The camera stores recent archives directly under `tar` and older ones
        below `tar/tmp`, hence recursive discovery. Existing same-size members
        are retained so repeated renders do not re-extract the cache.
        """
        if not tar_directory.is_dir():
            return 0, 0
        cache_directory.mkdir(parents=True, exist_ok=True)
        archives = extracted = 0
        for archive in sorted(tar_directory.rglob("*.git")):
            if archive.name.startswith("._"):
                continue
            try:
                with tarfile.open(archive, "r") as handle:
                    archives += 1
                    for member in handle.getmembers():
                        name = os.path.basename(member.name)
                        if not name.endswith(".gpx") or name.startswith("._"):
                            continue
                        destination = cache_directory / name
                        if destination.exists() and destination.stat().st_size == member.size:
                            continue
                        stream = handle.extractfile(member)
                        if stream is None:
                            continue
                        destination.write_bytes(stream.read())
                        extracted += 1
            except (tarfile.TarError, OSError) as error:
                self._logger.warning("Cannot read DDPAI GPS archive %s: %s", archive, error)
        return archives, extracted
