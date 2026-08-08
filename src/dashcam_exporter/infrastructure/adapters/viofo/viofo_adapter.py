import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .viofo_card_layout import MOVIE_ROOT, ViofoCardLayout


class ViofoAdapter(ExporterAdapter):
    """VIOFO cards, recognised by DCIM/Movie rather than by DCIM.

    DDPAI lives under DCIM too. Detecting on DCIM alone would make the
    registry raise on every card in the house, correctly and uselessly.
    """

    def __init__(self, pair_tolerance_seconds: int = 6,
                 logger: logging.Logger | None = None) -> None:
        self._pair_tolerance_seconds = pair_tolerance_seconds
        self._logger = logger

    @property
    def name(self) -> str:
        return "viofo"

    def detect(self, card_root: Path) -> bool:
        return (card_root / MOVIE_ROOT).is_dir()

    def layout_for(self, card_root: Path) -> ViofoCardLayout:
        return ViofoCardLayout(card_root, self._pair_tolerance_seconds,
                               self._logger)
