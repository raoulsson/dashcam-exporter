"""A VIOFO card: mode in the folder and the name, and clocks that drift."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.viofo import (
    ViofoAdapter, ViofoCardLayout)


def build_card(root: Path) -> Path:
    for relative in ("DCIM/Movie", "DCIM/Movie/RO", "DCIM/Movie/Parking",
                     "DCIM/Photo"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


class ViofoCardLayoutTest(unittest.TestCase):
    def test_pairs_cameras_whose_clocks_drifted_apart(self):
        # Owners report the rear file saving a second later, and the
        # sequence numbers running independently once a camera is unplugged.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/2026_0516_120000_001F.MP4").touch()
            (card / "DCIM/Movie/2026_0516_120002_004R.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "2026_0516_120002_004R.MP4")

    def test_a_rear_file_beyond_the_tolerance_is_its_own_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/2026_0516_120000_001F.MP4").touch()
            (card / "DCIM/Movie/2026_0516_120030_004R.MP4").touch()

            clips = ViofoCardLayout(card, pair_tolerance_seconds=6).clips()

        self.assertEqual(len(clips), 2)

    def test_a_three_channel_clip_keeps_all_of_its_cameras(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for name in ("2020_1018_170010_062PF.MP4",
                         "2020_1018_170010_063PI.MP4",
                         "2020_1018_170010_064PR.MP4"):
                (card / "DCIM/Movie/Parking" / name).touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(len(clips[0].videos), 3)
        self.assertEqual(clips[0].mode, ClipMode.PARKING)
        self.assertEqual(clips[0].source_mode, "P")

    def test_locked_clips_are_marked_from_the_folder_they_sit_in(self):
        # RO has no filename marker at all. The flag is the only place this
        # survives being copied off the card.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/RO/2026_0516_120000_001F.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].protected)

    def test_an_event_marker_in_the_name_is_read_as_an_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/RO/2026_0618_203643_0001EF.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(clips[0].mode, ClipMode.EVENT)
        self.assertEqual(clips[0].source_mode, "E")

    def test_stamp_of_translates_the_filename_into_our_canonical_form(self):
        layout = ViofoCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("2026_0516_120000_001F.MP4")),
                         "20260516120000")
        self.assertEqual(layout.stamp_of(Path("2020_1018_170010_062PF.MP4")),
                         "20201018170010")
        self.assertIsNone(layout.stamp_of(Path("readme.txt")))

    def test_a_sequence_number_of_any_documented_width_is_accepted(self):
        layout = ViofoCardLayout(Path("/nowhere"))

        for name in ("2026_0516_120000_001F.MP4",
                     "2026_0618_200300_0001F.MP4",
                     "2026_0508_104020_001234F.MP4"):
            self.assertIsNotNone(layout.stamp_of(Path(name)), name)


class ViofoAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_movie_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            self.assertTrue(ViofoAdapter().detect(card))

    def test_declines_a_ddpai_card_that_also_has_dcim(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertFalse(ViofoAdapter().detect(card))

    def test_is_named_for_the_camera(self):
        self.assertEqual(ViofoAdapter().name, "viofo")


if __name__ == "__main__":
    unittest.main()
