import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .blackvue_card_layout import RECORD_ROOT, BlackvueCardLayout


class BlackvueAdapter(ExporterAdapter):
    """BlackVue cards, recognised by their own top-level directory."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        return "blackvue"

    def detect(self, card_root: Path) -> bool:
        return (card_root / RECORD_ROOT).is_dir()

    def layout_for(self, card_root: Path) -> BlackvueCardLayout:
        return BlackvueCardLayout(card_root, self._logger)
