import logging
from pathlib import Path

from dashcam_exporter.domain import Clip
from dashcam_exporter.infrastructure.repository import DdpaiClipRepository


class DdpaiDataAdapter:
    """The pre-contract two-method API, kept alive for renderer.py.

    Deprecated. DdpaiAdapter and DdpaiCardLayout are the real implementation;
    this exists so the renderer keeps working until it is rewired, and it
    delegates rather than duplicating so there is still one definition of
    how DDPAI files its footage.
    """

    def __init__(self, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._clips = DdpaiClipRepository(rear_pair_tolerance_seconds)
        self._logger = logger or logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return "ddpai"

    def discover_clips(self, front_directory: Path,
                       rear_directory: Path | None) -> list[Clip]:
        return self._clips.find(front_directory, rear_directory)
