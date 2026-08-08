"""The contract a third party implements to support a new camera."""

import unittest
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.adapters import CardLayout


class HalfBuiltLayout(CardLayout):
    def clips(self) -> list[Clip]:
        return []


class CompleteLayout(CardLayout):
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


class CardLayoutTest(unittest.TestCase):
    def test_a_partial_implementation_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            HalfBuiltLayout()

    def test_a_complete_implementation_can(self):
        self.assertEqual(CompleteLayout().clips(), [])


if __name__ == "__main__":
    unittest.main()
