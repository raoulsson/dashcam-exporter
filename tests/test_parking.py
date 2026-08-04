#!/usr/bin/env python3
"""Where a parked run begins and ends decides what the viewer never sees.

A parking run is cut out of the render and replaced by a "Fast forwarding…"
slide. So its two boundaries are the two cuts the viewer actually watches: the
arrival (drive in, come to rest, slide) and the departure (slide, pull out,
drive off). Put either boundary on the wrong clip and the render either dwells
on a motionless car or drops the pull-out entirely.

Both went wrong on the 2026-08-03 supermarket stop, and for one reason: the run
boundaries were decided by GPS, which is dead in a parking lot, while the
accurate video ego-motion detector was consulted only for the trip's final trim.

  arrival    the car stopped at clip 13 +29s. The receiver kept reporting
             18 km/h for another ten seconds and only reached 0.0 at +39, so
             the smoothing detector found no onset at all and the run began at
             the NEXT clip instead — the slide landed 34s after the wheels
             stopped, on half a minute of a parked car.
  departure  the car reversed out of the slot at clip 21 +38s. That clip had
             no fix at all (n_speeds=0), which reads as "parked", so it was
             swallowed by the run and skipped whole. The render resumed on the
             clip after it, 22s into a pull-out already in progress.

The fixtures below reproduce both, faithfully: the arrival clip carries GPS
that lags ten seconds behind the frames, and the parked clips carry no GPS at
all. The flow signal is seeded directly, which is the honest seam — it is what
the camera saw — and it lets these run without OpenCV.

Run with:  ./run-tests.sh          (or: python3 -m unittest discover -s tests)
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.argv = ["make_dashcam_videos.py"]
import make_dashcam_videos as M            # noqa: E402

CLIP_SECS = 60
PARKED_FLOW = 0.01      # a still frame still twitches a hundredth of a pixel
DRIVING_FLOW = 45.0     # the whole frame sweeping past
KMH_PER_KNOT = 1.852


class ParkingTest(unittest.TestCase):
    """A card built clip by clip: frames as a flow signal, GPS as NMEA."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gps = self.root / "203gps"
        self.gps.mkdir()
        (self.root / "front").mkdir()
        self.clips = []
        self.at = datetime(2026, 8, 3, 16, 0, 0)
        for cache in (M._TRACK_POOL, M._EGO_FLOW_CACHE):
            cache.clear()
            self.addCleanup(cache.clear)

    # -- building a card -------------------------------------------------

    def add(self, flow, speeds=None):
        """One clip. `flow` is a per-second list turned into the 4 fps signal
        the detectors read; `speeds` is per-second km/h written as NMEA, or
        None for a clip whose receiver never fixed (the parking-lot case)."""
        stamp = self.at.strftime("%Y%m%d%H%M%S")
        front = self.root / "front" / ("%s_%04d.mp4" % (stamp, CLIP_SECS))
        front.write_bytes(b"")
        if speeds is not None:
            self._write_nmea(stamp, speeds)
        clip = M.Clip(timestamp=stamp, epoch_utc=0, duration=CLIP_SECS,
                      front=front, rear=None)
        M._EGO_FLOW_CACHE[front] = [v for v in flow for _ in range(M.EGO_FPS)]
        self.clips.append(clip)
        self.at += timedelta(seconds=CLIP_SECS)
        return clip

    def _write_nmea(self, stamp, speeds):
        utc = datetime.strptime(stamp, "%Y%m%d%H%M%S") - timedelta(hours=8)
        lines = ["$GPSCAMTIME %s" % stamp]
        for s, kmh in enumerate(speeds):
            t = utc + timedelta(seconds=s)
            lines.append(
                "$GPRMC,%s.000,A,1425.20000,N,12101.20000,E,%.2f,0.00,030826,,,A,V*00"
                % (t.strftime("%H%M%S"), kmh / KMH_PER_KNOT))
        (self.gps / ("%s_%04d.gpx" % (stamp, CLIP_SECS))).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    # -- the four kinds of clip on this card -----------------------------

    def driving(self):
        return self.add([DRIVING_FLOW] * CLIP_SECS, [30.0] * CLIP_SECS)

    def parked(self):
        """No fix at all — what a supermarket lot actually produces."""
        return self.add([PARKED_FLOW] * CLIP_SECS)

    def arriving(self, at_sec, gps_lag=10):
        """Drives in, comes to rest at `at_sec`. The receiver keeps reporting
        speed for `gps_lag` seconds after the frames have gone still."""
        flow = [DRIVING_FLOW if s < at_sec else PARKED_FLOW
                for s in range(CLIP_SECS)]
        speeds = [20.0 if s < at_sec + gps_lag else 0.0
                  for s in range(CLIP_SECS)]
        return self.add(flow, speeds)

    def departing(self, at_sec):
        """Sits still, then pulls out at `at_sec`. No fix — the receiver has
        not reacquired yet, so by GPS alone this clip reads as parked."""
        flow = [PARKED_FLOW if s < at_sec else DRIVING_FLOW
                for s in range(CLIP_SECS)]
        return self.add(flow)

    def runs(self, min_run_secs=300):
        return M.find_parking_runs(self.clips, (self.gps, None), min_run_secs)


class TestTheRunBeginsWhereTheWheelsStopped(ParkingTest):
    """The arrival cut."""

    def setUp(self):
        super().setUp()
        self.driving()                 # 0
        self.arriving(at_sec=29)       # 1  <- the car comes to rest here
        for _ in range(6):             # 2..7
            self.parked()

    def test_the_run_starts_in_the_clip_that_came_to_rest(self):
        self.assertEqual(len(self.runs()), 1)
        start, _park_sec, _end = self.runs()[0]
        self.assertEqual(start, 1, "the run began on a clip that was already "
                                   "parked, so the arrival plays out in full")

    def test_the_onset_is_the_frames_not_the_lagging_receiver(self):
        _start, park_sec, _end = self.runs()[0]
        self.assertAlmostEqual(park_sec, 29, delta=1)

    def test_the_onset_is_a_whole_clip_second(self):
        """The render slices footage with it (`per_second[trim:trim + n]`), so a
        fractional onset is a TypeError mid-encode, not a rounding question.
        Both video and GPS answer in fractional seconds."""
        _start, park_sec, _end = self.runs()[0]
        self.assertIsInstance(park_sec, int)

    def test_gps_alone_would_have_placed_it_ten_seconds_late(self):
        """Naming the lag this fixture encodes: GPS is not merely absent here,
        it is confidently wrong, which is why it cannot be the first answer."""
        self.assertAlmostEqual(
            M.find_park_second_by_gps(self.clips[1], (self.gps, None)),
            39, delta=1)

    def test_the_slide_is_entry_pad_seconds_after_the_stop(self):
        """What the boundary is FOR: the render keeps park_sec + entry_pad of
        the entry clip. Off-by-a-clip here is 34 seconds of a motionless car."""
        _start, park_sec, _end = self.runs()[0]
        self.assertLess(park_sec + 3, 35)


class TestTheRunEndsBeforeTheCarPullsOut(ParkingTest):
    """The departure cut."""

    def setUp(self):
        super().setUp()
        self.driving()                 # 0
        self.arriving(at_sec=29)       # 1
        for _ in range(5):             # 2..6
            self.parked()
        self.departing(at_sec=38)      # 7  <- reverses out of the slot here
        self.driving()                 # 8

    def test_the_pull_out_clip_is_not_swallowed_by_the_run(self):
        _start, _park, end = self.runs()[0]
        self.assertEqual(end, 6, "the clip holding the pull-out was skipped, "
                                 "so the render resumes already driving")

    def test_the_pull_out_clip_becomes_the_exit(self):
        """The render puts its "exit" action on run_end + 1. That has to be the
        clip the car actually leaves in, or the slice starts too late."""
        _start, _park, end = self.runs()[0]
        self.assertIsNotNone(M.find_drive_away_by_video(self.clips[end + 1]))

    def test_a_run_is_never_walked_back_past_its_own_start(self):
        """A degenerate run — the car stops and leaves inside one clip — must
        not produce an end before its start."""
        self.clips = [self.clips[0], self.clips[7], self.clips[8]]
        for start, _park, end in self.runs(min_run_secs=1):
            self.assertGreaterEqual(end, start)


class TestAnOrdinaryRunIsLeftAlone(ParkingTest):
    """The corrections must not fire where nothing is wrong."""

    def test_a_run_that_ends_at_the_last_clip_keeps_every_parked_clip(self):
        self.driving()
        self.arriving(at_sec=29)
        for _ in range(6):
            self.parked()
        _start, _park, end = self.runs()[0]
        self.assertEqual(end, len(self.clips) - 1)

    def test_a_stop_too_short_to_matter_is_still_no_run(self):
        self.driving()
        self.arriving(at_sec=29)
        self.parked()
        self.driving()
        self.assertEqual(self.runs(), [])


class TestTheExitSliceShowsTheRunUp(ParkingTest):
    """`parking_exit_pad` is the documented knob for how much footage precedes
    the pull-out. On the video path it was ignored in favour of a 2-second
    constant, so the knob did nothing whenever OpenCV was installed."""

    def test_the_slice_starts_a_full_pad_before_the_drive_away(self):
        self.assertEqual(M.exit_trim_start(38.0, exit_pad=10), 28)

    def test_a_drive_away_at_the_very_start_cannot_trim_below_zero(self):
        self.assertEqual(M.exit_trim_start(0.2, exit_pad=10), 0)

    def test_the_default_pad_is_a_beat_not_a_wait(self):
        """Ten seconds of a motionless frame immediately after a slide that
        says "43m skipped" is the dead air the slide exists to remove."""
        self.assertLessEqual(M.DEFAULT_PARKING_EXIT_PAD, 5)


class TestAStationaryCarIsNeverCaptionedWithASpeed(ParkingTest):
    """The receiver decays rather than dropping to zero. Cutting the arrival at
    the true stop exposed it: three seconds of a motionless frame captioned
    18, 17, 14 km/h, right before the slide."""

    def test_the_overlay_follows_the_frames_from_the_park_onset(self):
        lagging = [20.0] * 29 + [18.0, 17.0, 14.9] + [0.0] * 28
        settled = M.settle_speeds_after(lagging, 29)
        self.assertEqual(settled[26:32], [20.0, 20.0, 20.0, 0.0, 0.0, 0.0])

    def test_the_approach_is_left_alone(self):
        lagging = [20.0] * 29 + [18.0] * 31
        self.assertEqual(M.settle_speeds_after(lagging, 29)[:29], [20.0] * 29)

    def test_a_clip_parked_from_its_first_frame_shows_no_speed_at_all(self):
        self.assertEqual(M.settle_speeds_after([80.0] * 60, 0), [0.0] * 60)


if __name__ == "__main__":
    unittest.main()
