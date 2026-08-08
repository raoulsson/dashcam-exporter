"""Choosing an adapter for a card, and refusing to guess."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.adapters import (
    AdapterRegistry, AmbiguousCard, CardLayout, ExporterAdapter,
    NoAdapterFound)


class NullLayout(CardLayout):
    def clips(self) -> list[Clip]:
        return []

    def stamp_of(self, path: Path) -> str | None:
        return None

    def track_for(self, clip: Clip) -> Track | None:
        return None

    def import_roots(self) -> tuple[Path, ...]:
        return ()

    def is_track_artifact(self, path: Path) -> bool:
        return False


class MarkerAdapter(ExporterAdapter):
    """Claims any card holding a directory it was named after."""

    def __init__(self, marker: str) -> None:
        self._marker = marker

    @property
    def name(self) -> str:
        return self._marker

    def detect(self, card_root: Path) -> bool:
        return (card_root / self._marker).is_dir()

    def layout_for(self, card_root: Path) -> CardLayout:
        return NullLayout()


class AdapterRegistryTest(unittest.TestCase):
    def test_picks_the_single_adapter_that_claims_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "alpha").mkdir()
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            self.assertEqual(registry.detect(card).name, "alpha")

    def test_refuses_to_guess_when_two_adapters_claim_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "alpha").mkdir()
            (card / "beta").mkdir()
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            with self.assertRaises(AmbiguousCard) as raised:
                registry.detect(card)

            self.assertIn("alpha", str(raised.exception))
            self.assertIn("beta", str(raised.exception))

    def test_an_override_wins_without_consulting_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            self.assertEqual(registry.detect(card, forced="beta").name, "beta")

    def test_an_unknown_override_names_what_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha")])

            with self.assertRaises(NoAdapterFound) as raised:
                registry.detect(card, forced="nosuch")

            self.assertIn("alpha", str(raised.exception))

    def test_no_claim_at_all_is_an_error_naming_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha")])

            with self.assertRaises(NoAdapterFound) as raised:
                registry.detect(card)

            self.assertIn(str(card), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
