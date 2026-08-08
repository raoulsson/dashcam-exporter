#!/usr/bin/env python3
"""A clip's own fixes are its own, and trip boundaries depend on it.

Trip detection asks where the car was when a clip started and when it stopped —
`Track.endpoints` reads the first and last fix of that clip, and every boundary
test (`within_target`, `min_dist`, `parks_here`, `departs_here`) is built on
those two readings.

Clips are CONTIGUOUS: one ends on the second the next begins. So a window with
any margin at all hands a clip its neighbours' fixes, and the two readings stop
being about that clip. Selecting a track by time made that mistake with a
90-second margin on 60-second clips — each clip received both neighbours whole,
positions smeared together, and an afternoon of short hops near home came back
as `14:14:41 -> 14:16:41` and `14:16:41 -> 14:20:41`: two trips whose boundary
shared a second, both below the fragment threshold and both offered up for
exclusion.

The video ego-motion path needs OpenCV, so these drive the radius fallback
(`use_video=False`). The windowing under test is the same on both paths.

Run with:  ./run-tests.sh          (or: python3 -m unittest discover -s tests)
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.argv = ["dashcam_exporter.infrastructure.media.renderer"]
from dashcam_exporter import renderer as M  # noqa: E402
from dashcam_exporter.domain import Clip  # noqa: E402

HOME = (14.4200, 121.0200)
CLIP_SECS = 60


def _away(metres):
    """A point `metres` north of home. 1e-5 degrees of latitude is ~1.11 m."""
    return (HOME[0] + metres / 111_000.0, HOME[1])


def _ddmm(deg, width):
    d = int(deg)
    return "%0*d%08.5f" % (width, d, (deg - d) * 60)


def _metres(a, b):
    return M._haversine_km(a[0], a[1], b[0], b[1]) * 1000.0


class GroupingTest(unittest.TestCase):
    """A card built clip by clip, each with its position written as NMEA."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gps = self.root / "203gps"
        self.gps.mkdir()
        (self.root / "front").mkdir()
        self.clips = []
        M.Track._POOL.clear()
        self.addCleanup(M.Track._POOL.clear)

    def add(self, at, where, secs=CLIP_SECS, every=10):
        """One clip starting at local `at`, sitting still at `where`."""
        stamp = at.strftime("%Y%m%d%H%M%S")
        front = self.root / "front" / ("%s_%04d.mp4" % (stamp, secs))
        front.write_bytes(b"")
        utc = at - timedelta(hours=8)          # the camera's own offset
        lines = ["$GPSCAMTIME %s" % stamp]
        for s in range(0, secs, every):
            t = utc + timedelta(seconds=s)
            lines.append("$GPRMC,%s.000,A,%s,N,%s,E,0.50,0.00,280726,,,A,V*00"
                         % (t.strftime("%H%M%S"), _ddmm(where[0], 2),
                            _ddmm(where[1], 3)))
        (self.gps / ("%s_%04d.gpx" % (stamp, secs))).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        self.clips.append(Clip.paired(stamp, 0, secs, front, None))
        return at + timedelta(seconds=secs)

    def endpoints(self):
        return [M.Track((self.gps, None)).endpoints(c) for c in self.clips]

    def group(self, **kw):
        opts = dict(home=HOME, home_radius_m=100, return_m=100, leave_m=150,
                    min_trip_m=200, use_video=False)
        opts.update(kw)
        trips, _moved = M.group_into_trips(self.clips, M.Track((self.gps, None)), **opts)
        return trips


class TestAClipsFixesAreItsOwn(GroupingTest):
    """The defect, at the level it happened."""

    def setUp(self):
        super().setUp()
        t = datetime(2026, 7, 28, 14, 14, 41)
        for where in (HOME, _away(400), HOME):
            t = self.add(t, where)

    def test_the_middle_clip_reads_where_it_actually_was(self):
        """Its neighbours are both at home and it is 400 m away. Given their
        fixes as well, it read as home — and a clip that never left cannot
        depart, which is how a trip boundary lands in the wrong place."""
        first, last = self.endpoints()[1]
        self.assertAlmostEqual(_metres(HOME, first), 400, delta=5)
        self.assertAlmostEqual(_metres(HOME, last), 400, delta=5)

    def test_neither_neighbour_borrows_from_it(self):
        eps = self.endpoints()
        for i in (0, 2):
            with self.subTest(clip=i):
                for fix in eps[i]:
                    self.assertLess(_metres(HOME, fix), 5,
                                    "a clip at home reported the one away")

    def test_a_clip_gets_exactly_its_own_fixes(self):
        """Six fixes per clip, and no clip may hold seven."""
        for i, clip in enumerate(self.clips):
            with self.subTest(clip=i):
                self.assertEqual(len(M.Track((self.gps, None)).during([clip])),
                                 CLIP_SECS // 10)

    def test_the_boundary_second_belongs_to_one_clip_only(self):
        """A fix landing exactly where one clip ends and the next begins is
        the next clip's. Inclusive at both ends it was both clips'."""
        owners = [len(M.Track((self.gps, None)).during([c])) for c in self.clips]
        pool, _off = M.Track((self.gps, None))._pooled()
        self.assertEqual(sum(owners), len(pool), "a fix was counted twice")


class TestBoundariesFallWhereTheDrivingSays(GroupingTest):
    """The model itself is unchanged: a park at the anchor ends a trip."""

    def test_out_and_back_twice_is_two_trips(self):
        t = datetime(2026, 7, 28, 14, 14, 41)
        for where in (HOME, _away(400), HOME, _away(400), HOME):
            t = self.add(t, where)
        self.assertEqual([len(x) for x in self.group()], [3, 2])

    def test_a_boundary_never_shares_a_second(self):
        """The symptom that started this: one trip ending and the next
        beginning at the same moment, with no idle between them."""
        t = datetime(2026, 7, 28, 14, 14, 41)
        for where in (HOME, _away(400), HOME, _away(400), HOME):
            t = self.add(t, where)
        trips = self.group()
        for before, after in zip(trips, trips[1:]):
            end = before[-1].dt + timedelta(seconds=before[-1].duration)
            self.assertGreaterEqual((after[0].dt - end).total_seconds(), 0)

    def test_one_continuous_drive_away_is_one_trip(self):
        """Never returning to the anchor: nothing closes it."""
        t = datetime(2026, 7, 28, 14, 14, 41)
        for metres in (0, 400, 900, 1500, 2200):
            t = self.add(t, _away(metres))
        self.assertEqual(len(self.group()), 1)


class TestTheRolloverSplitsRatherThanDiscards(GroupingTest):
    """A drive running through 04:00 is force-closed there, and what follows
    is the next trip — not footage that falls off the end.

    The rollover exists to bound a one-way relocation: without it a drive that
    never returns to the anchor has nothing to close it and grows until the
    clips run out. Closing a trip and continuing are two different acts,
    though, and the one that loses footage looks exactly like the one that
    does not until you count the clips on both sides.
    """

    def setUp(self):
        super().setUp()
        t = datetime(2026, 7, 28, 3, 57, 0)
        # Driving away and never coming back: nothing but the rollover can
        # close this.
        for metres in (0, 600, 1200, 1800, 2400, 3000, 3600):
            t = self.add(t, _away(metres))

    def group(self, **kw):
        kw.setdefault("rollover_h", 4)
        return super().group(**kw)

    def test_it_becomes_two_trips(self):
        self.assertEqual(len(self.group()), 2)

    def test_not_one_clip_is_dropped(self):
        kept = sum(len(trip) for trip in self.group())
        self.assertEqual(kept, len(self.clips),
                         "clips after the rollover went nowhere")

    def test_the_split_falls_on_the_rollover(self):
        first, second = self.group()
        self.assertLess(first[-1].dt.hour, 4)
        self.assertGreaterEqual(second[0].dt.hour, 4)

    def test_the_two_carry_different_day_labels(self):
        first, second = self.group()
        self.assertNotEqual(M.trip_day_label(first[0].dt, 4),
                            M.trip_day_label(second[0].dt, 4))

    def test_a_drive_that_clears_the_hour_is_left_whole(self):
        """The same footage an hour later is one trip. The split is the
        rollover doing its job, not the length of the drive."""
        self.clips = []
        self.gps = self.root / "203gps_late"
        self.gps.mkdir()
        M.Track._POOL.clear()
        t = datetime(2026, 7, 28, 4, 57, 0)
        for metres in (0, 600, 1200, 1800, 2400, 3000, 3600):
            t = self.add(t, _away(metres))
        self.assertEqual(len(self.group()), 1)


if __name__ == "__main__":
    unittest.main()
