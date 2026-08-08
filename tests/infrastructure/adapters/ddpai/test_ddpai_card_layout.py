"""The DDPAI card, answered through the contract a third party implements."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiCardLayout


def build_card(root: Path) -> Path:
    for relative in ("DCIM/200video/front", "DCIM/200video/rear",
                     "DCIM/203gps/tar", "DCIM/201photo/front",
                     "DCIM/202thumb/front", "DCIM/207log/tmp"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


class DdpaiCardLayoutTest(unittest.TestCase):
    def test_pairs_rear_across_a_one_second_camera_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()
            (card / "DCIM/200video/rear/20260806170530_0060_A.mp4").touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].duration, 60)
        self.assertEqual(clips[0].wall_seconds, 60)
        self.assertEqual(clips[0].mode, ClipMode.NORMAL)
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "20260806170530_0060_A.mp4")

    def test_a_front_clip_with_no_partner_is_normal_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertIsNone(clips[0].rear)

    def test_clips_come_back_in_recording_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for stamp in ("20260806170729", "20260806170529", "20260806170629"):
                (card / ("DCIM/200video/front/%s_0060.mp4" % stamp)).touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual([c.timestamp for c in clips],
                         ["20260806170529", "20260806170629", "20260806170729"])

    def test_stamp_of_reads_the_canonical_form_from_either_camera(self):
        layout = DdpaiCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("20260806170529_0060.mp4")),
                         "20260806170529")
        self.assertEqual(layout.stamp_of(Path("20260806170529_0060_A.mp4")),
                         "20260806170529")
        self.assertIsNone(layout.stamp_of(Path("notes.txt")))

    def test_import_roots_leave_the_photos_thumbnails_and_logs_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            roots = DdpaiCardLayout(card).import_roots()

        self.assertEqual(
            sorted(r.relative_to(card).as_posix() for r in roots),
            ["DCIM/200video", "DCIM/203gps"])


if __name__ == "__main__":
    unittest.main()
