"""Three cameras, three layouts, and no card claimed twice."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters import (
    NoAdapterFound, default_registry)

CARDS = {
    "ddpai": "DCIM/200video/front",
    "blackvue": "BlackVue/Record",
    "viofo": "DCIM/Movie",
}


class DefaultRegistryTest(unittest.TestCase):
    def test_every_shipped_adapter_is_registered(self):
        self.assertEqual(sorted(default_registry().names),
                         ["blackvue", "ddpai", "viofo"])

    def test_each_card_resolves_to_exactly_one_adapter(self):
        for expected, marker in CARDS.items():
            with self.subTest(camera=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    card = Path(temporary)
                    (card / marker).mkdir(parents=True)

                    self.assertEqual(
                        default_registry().detect(card).name, expected)

    def test_a_dcim_card_belonging_to_neither_is_claimed_by_neither(self):
        # The test that catches a detect() written as "has a DCIM folder":
        # DDPAI and VIOFO share that parent and nothing else.
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/100CANON").mkdir(parents=True)

            with self.assertRaises(NoAdapterFound):
                default_registry().detect(card)

    def test_an_empty_card_is_claimed_by_nobody(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(NoAdapterFound):
                default_registry().detect(Path(temporary))


if __name__ == "__main__":
    unittest.main()
