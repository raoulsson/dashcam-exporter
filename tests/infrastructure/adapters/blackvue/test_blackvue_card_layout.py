"""A BlackVue card: one directory, mode and direction in the filename."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.blackvue import (
    BlackvueAdapter, BlackvueCardLayout)


def build_card(root: Path) -> Path:
    (root / "BlackVue/Record").mkdir(parents=True, exist_ok=True)
    (root / "BlackVue/Config").mkdir(parents=True, exist_ok=True)
    return root


def record(root: Path) -> Path:
    return root / "BlackVue/Record"


class BlackvueCardLayoutTest(unittest.TestCase):
    def test_pairs_front_and_rear_that_share_one_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_NF.mp4").touch()
            (record(card) / "20210127_155052_NR.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].timestamp, "20210127155052")
        self.assertEqual(clips[0].videos[Channel.FRONT].name,
                         "20210127_155052_NF.mp4")
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "20210127_155052_NR.mp4")

    def test_an_interior_camera_is_a_third_channel_not_a_second_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for name in ("20210127_155052_NF.mp4", "20210127_155052_NR.mp4",
                         "20210127_155052_NI.mp4"):
                (record(card) / name).touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(len(clips[0].videos), 3)
        self.assertEqual(clips[0].videos[Channel.INTERIOR].name,
                         "20210127_155052_NI.mp4")

    def test_parking_and_manual_modes_come_from_the_type_letter(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_PF.mp4").touch()
            (record(card) / "20210127_160000_MF.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual([c.mode for c in clips],
                         [ClipMode.PARKING, ClipMode.MANUAL])
        self.assertEqual([c.source_mode for c in clips], ["P", "M"])
        self.assertTrue(all(c.protected for c in clips))

    def test_an_upload_flagged_file_is_the_same_clip(self):
        # The third character is L or S, a cloud upload flag.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_NFS.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].videos[Channel.FRONT].name,
                         "20210127_155052_NFS.mp4")

    def test_stamp_of_translates_the_filename_into_our_canonical_form(self):
        layout = BlackvueCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("20210127_155052_NF.mp4")),
                         "20210127155052")
        self.assertIsNone(layout.stamp_of(Path("readme.txt")))

    def test_the_import_root_is_the_record_directory_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            roots = BlackvueCardLayout(card).import_roots()

        self.assertEqual([r.relative_to(card).as_posix() for r in roots],
                         ["BlackVue/Record"])


class BlackvueAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_record_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            self.assertTrue(BlackvueAdapter().detect(card))

    def test_declines_a_dcim_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertFalse(BlackvueAdapter().detect(card))

    def test_is_named_for_the_camera(self):
        self.assertEqual(BlackvueAdapter().name, "blackvue")


if __name__ == "__main__":
    unittest.main()
