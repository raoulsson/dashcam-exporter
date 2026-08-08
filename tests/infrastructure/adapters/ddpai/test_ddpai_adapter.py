"""Recognising a DDPAI card, and declining the ones that only look like it."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import (
    DdpaiAdapter, DdpaiCardLayout)


class DdpaiAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_numbered_video_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertTrue(DdpaiAdapter().detect(card))

    def test_declines_a_card_that_merely_has_dcim(self):
        # A VIOFO card is DCIM/Movie. Claiming it would make the registry
        # ambiguous the moment a second adapter is registered.
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/Movie").mkdir(parents=True)

            self.assertFalse(DdpaiAdapter().detect(card))

    def test_declines_an_empty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(DdpaiAdapter().detect(Path(temporary)))

    def test_produces_a_layout_bound_to_that_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            layout = DdpaiAdapter().layout_for(card)

        self.assertIsInstance(layout, DdpaiCardLayout)
        self.assertEqual(layout.import_roots()[0], card / "DCIM/200video")

    def test_is_named_for_the_camera(self):
        self.assertEqual(DdpaiAdapter().name, "ddpai")


if __name__ == "__main__":
    unittest.main()
