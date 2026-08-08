import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .ddpai_card_layout import VIDEO_ROOT, DdpaiCardLayout


class DdpaiAdapter(ExporterAdapter):
    """DDPAI cards, recognised by their numbered video directory."""

    def __init__(self, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._rear_pair_tolerance_seconds = rear_pair_tolerance_seconds
        self._logger = logger

    @property
    def name(self) -> str:
        return "ddpai"

    def detect(self, card_root: Path) -> bool:
        # DCIM alone decides nothing -- VIOFO cards have it too, and a
        # detect() loose enough to claim theirs makes the registry raise.
        return (card_root / VIDEO_ROOT / "front").is_dir()

    def layout_for(self, card_root: Path) -> DdpaiCardLayout:
        return DdpaiCardLayout(card_root, self._rear_pair_tolerance_seconds,
                               self._logger)
