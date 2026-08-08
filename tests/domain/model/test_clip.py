"""A clip, once both durations and more than two channels have to fit."""

import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode


class ClipTest(unittest.TestCase):
    def test_paired_builds_the_common_two_channel_case(self):
        clip = Clip.paired("20260806170529", 1785000329, 60,
                           Path("front.mp4"), Path("rear.mp4"))

        self.assertEqual(clip.front, Path("front.mp4"))
        self.assertEqual(clip.rear, Path("rear.mp4"))
        self.assertEqual(clip.duration, 60)
        self.assertEqual(clip.mode, ClipMode.NORMAL)
        self.assertFalse(clip.protected)

    def test_rear_is_none_when_the_camera_recorded_only_front(self):
        clip = Clip.paired("20260806170529", 0, 60, Path("front.mp4"))

        self.assertIsNone(clip.rear)

    def test_three_channels_share_one_timestamp(self):
        clip = Clip(timestamp="20201018170010", epoch_utc=0,
                    playback_seconds=60, wall_seconds=60,
                    videos={Channel.FRONT: Path("062PF.MP4"),
                            Channel.INTERIOR: Path("063PI.MP4"),
                            Channel.REAR: Path("064PR.MP4")},
                    mode=ClipMode.PARKING, source_mode="P")

        self.assertEqual(clip.front, Path("062PF.MP4"))
        self.assertEqual(clip.rear, Path("064PR.MP4"))
        self.assertEqual(clip.videos[Channel.INTERIOR], Path("063PI.MP4"))
        self.assertEqual(clip.source_mode, "P")

    def test_timelapse_ends_by_wall_clock_not_by_playback_length(self):
        # Thinkware records ten minutes of real time into a two-minute file.
        clip = Clip(timestamp="20260806170529", epoch_utc=0,
                    playback_seconds=120, wall_seconds=600,
                    videos={Channel.FRONT: Path("f.mp4")},
                    mode=ClipMode.TIMELAPSE, source_mode="motion_timelapse_rec")

        self.assertEqual(clip.ended_at, datetime(2026, 8, 6, 17, 15, 29))
        self.assertEqual(clip.duration, 120)

    def test_gap_after_measures_wall_clock_between_clips(self):
        first = Clip.paired("20260806170529", 0, 60, Path("a.mp4"))
        second = Clip.paired("20260806170729", 0, 60, Path("b.mp4"))

        self.assertEqual(second.gap_after(first), 60.0)


if __name__ == "__main__":
    unittest.main()
