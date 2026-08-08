from abc import ABC, abstractmethod
from pathlib import Path

from .card_layout import CardLayout


class ExporterAdapter(ABC):
    """Support for one camera's way of filing footage.

    An adapter is keyed to a layout, not to a company. BlackVue changed GPS
    regimes between model generations and VIOFO's A119 V3 uses a different
    filename grammar from the rest of the range, so two adapters for one
    brand is a normal outcome rather than a design failure.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier used in logs, configuration and the override."""

    @abstractmethod
    def detect(self, card_root: Path) -> bool:
        """Whether this adapter recognises the tree at card_root.

        Inspect structure rather than a single marker: DDPAI and VIOFO both
        live under DCIM, so the presence of DCIM decides nothing.
        """

    @abstractmethod
    def layout_for(self, card_root: Path) -> CardLayout:
        """The layout answering questions about this particular card."""
