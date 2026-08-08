from pathlib import Path
from typing import Sequence

from .exporter_adapter import ExporterAdapter


class NoAdapterFound(Exception):
    """No registered adapter recognised the card, or the override is unknown."""


class AmbiguousCard(Exception):
    """More than one adapter claimed the card, so the tool refuses to guess."""


class AdapterRegistry:
    """Resolves a card to exactly one adapter, or says why it cannot.

    Ambiguity raises rather than picking a winner. Two adapters claiming one
    card means a detect() is too loose, and a silent first-match would hide
    that until someone's footage came out wrong.
    """

    def __init__(self, adapters: Sequence[ExporterAdapter]) -> None:
        self._adapters = tuple(adapters)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)

    def detect(self, card_root: Path,
               forced: str | None = None) -> ExporterAdapter:
        if forced is not None:
            return self._named(forced)
        claimed = [a for a in self._adapters if a.detect(card_root)]
        if len(claimed) == 1:
            return claimed[0]
        if not claimed:
            raise NoAdapterFound(
                "No adapter recognises %s. Registered: %s"
                % (card_root, ", ".join(self.names) or "none"))
        raise AmbiguousCard(
            "%s is claimed by %s. Force one with the adapter setting."
            % (card_root, ", ".join(a.name for a in claimed)))

    def _named(self, name: str) -> ExporterAdapter:
        for adapter in self._adapters:
            if adapter.name == name:
                return adapter
        raise NoAdapterFound(
            "No adapter named %r. Registered: %s"
            % (name, ", ".join(self.names) or "none"))
