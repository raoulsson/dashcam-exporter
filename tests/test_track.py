#!/usr/bin/env python3
"""How a trip's GPS track is selected out of the card's NMEA files.

The camera writes GPS continuously and its writer rolls into a new file
whenever it decides to. A filename therefore records when a file was OPENED
and says nothing about whose fixes are inside it, which fails in both
directions: a file can hold a replay from an earlier drive, and a minute's
fixes can sit in a file named for a different minute.

So a fix is selected by TIME. These tests pin that, and the two real failures
that made it necessary — both taken from one card:

  * 20260728142342_0045.gpx held seven sentences replayed from 26 minutes
    earlier, at a position 11 km away. The receiver had no fix during that
    clip, so it re-emitted its last known one. Seven points cleared the
    five-point noise floor, and the trip was drawn as a straight line 12 km
    long out of an area 1.5 km across.
  * 203gps/tmp held twenty files no listdir ever saw.

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


def nmea(local: str, fixes) -> str:
    """One camera file: its local-clock header, then $GPRMC sentences.

    `fixes` is (utc_hhmmss, lat_decimal, lon_decimal). Written back out in
    the DDMM.MMMM the receiver actually emits, so the parser under test does
    the same conversion it does on a real card.
    """
    lines = ["$GPSCAMTIME %s" % local]
    for hhmmss, lat, lon in fixes:
        lines.append("$GPRMC,%s.000,A,%s,N,%s,E,0.50,0.00,280726,,,A,V*00"
                     % (hhmmss, _ddmm(lat, 2), _ddmm(lon, 3)))
    return "\n".join(lines) + "\n"


def _ddmm(deg: float, width: int) -> str:
    d = int(deg)
    return "%0*d%08.5f" % (width, d, (deg - d) * 60)


#: UTC of the first fix of clip 20260728141441, whose camera-local header says
#: 14:14:41 -- so this card sits at UTC-8, as everywhere else in this file.
_UTC_BASE = datetime(2026, 7, 28, 6, 14, 41)


def utc(seconds: int) -> str:
    """hhmmss that many seconds after the reference clip's first fix."""
    return (_UTC_BASE + timedelta(seconds=seconds)).strftime("%H%M%S")


def clip(ts: str, secs: int = 60) -> Clip:
    return Clip.paired(ts, 0, secs, Path("/tmp/%s.mp4" % ts), None)


class TrackTest(unittest.TestCase):
    """A card directory built per test, with the pool cache cleared.

    The pool is memoised per directory set — Track.during is called once per
    clip while boundaries are found, and re-reading several hundred files each
    time would dominate the scan — so a test that skipped this would read the
    previous test's card.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gps = Path(self.tmp.name) / "203gps"
        self.gps.mkdir(parents=True)
        M.Track._POOL.clear()

    def tearDown(self):
        self.tmp.cleanup()
        M.Track._POOL.clear()

    def write(self, name: str, text: str, sub: str = "") -> Path:
        d = self.gps / sub if sub else self.gps
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p

    def track(self, clips):
        return M.Track((self.gps, None)).during(clips)


class TestTheOffsetComesFromTheCamera(TrackTest):
    """$GPSCAMTIME is the camera's local clock and $GPRMC is UTC, in the same
    file. The pair states the timezone, so nothing has to be configured and
    nothing is inferred from a filename."""

    def test_it_reads_the_offset_off_a_healthy_file(self):
        self.write("20260728141441_0060.gpx",
                   nmea("20260728141441", [("061441", 14.42, 121.02)]))
        off = M.Track((self.gps, None)).utc_offset
        self.assertEqual(off, timedelta(hours=-8))

    def test_a_file_whose_gps_never_fixed_cannot_move_it(self):
        """The median, not the first reading. A replayed sentence gives a wild
        offset, and that file is exactly the one this has to survive."""
        for i, mm in enumerate(("41", "42", "43")):
            self.write("202607281414%s_0060.gpx" % mm,
                       nmea("202607281414%s" % mm, [("06144%d" % i, 14.42, 121.02)]))
        self.write("20260728142342_0045.gpx",                   # the liar
                   nmea("20260728142342", [("055742", 14.5177, 121.0296)]))
        off = M.Track((self.gps, None)).utc_offset
        self.assertEqual(off, timedelta(hours=-8))

    def test_no_camera_time_anywhere_is_zero_rather_than_a_guess(self):
        self.write("20260728141441_0060.gpx",
                   "$GPRMC,061441.000,A,1425.20000,N,12101.20000,E,0.5,0,280726,,,A,V*00\n")
        off = M.Track((self.gps, None)).utc_offset
        self.assertEqual(off, timedelta(0))


class TestAReplayedFixIsNotThisTripsFix(TrackTest):
    """The failure this was written for, in the shape it really had."""

    def setUp(self):
        super().setUp()
        # Three real clips at 14:14-14:16 local, which is 06:14-06:16 UTC.
        for i in range(3):
            self.write("2026072814%02d41_0060.gpx" % (14 + i),
                       nmea("2026072814%02d41" % (14 + i),
                            [("06%02d41" % (14 + i), 14.4206 + i * 0.0002, 121.0256)]))
        # A fourth file, correctly named and correctly rolled, whose receiver
        # had no fix: it replayed its last known sentence from 26 minutes back
        # and 11 km north.
        self.write("20260728142342_0045.gpx",
                   nmea("20260728142342",
                        [("0557%02d" % (42 + i), 14.5177 - i * 0.0002, 121.0296)
                         for i in range(7)]))

    def test_the_replay_is_left_out(self):
        got = self.track([clip("20260728141441"), clip("20260728141541"),
                          clip("20260728141641"), clip("20260728142342", 45)])
        self.assertTrue(got, "the real fixes must still be there")
        self.assertFalse([p for p in got if p[0] > 14.50],
                         "a fix from before the trip began is not the trip's")

    def test_the_real_fixes_survive_it(self):
        got = self.track([clip("20260728141441"), clip("20260728141541"),
                          clip("20260728141641")])
        self.assertEqual(len(got), 3)

    def test_the_bounding_box_stays_the_size_of_the_drive(self):
        """Seven points cleared the five-point noise floor, so counting was
        never going to separate them. 12 km tall over a drive around the
        block is what the operator actually saw."""
        got = self.track([clip("20260728141441"), clip("20260728142342", 45)])
        tall_km = (max(p[0] for p in got) - min(p[0] for p in got)) * 111
        self.assertLess(tall_km, 1.0)


class TestEveryFileIsRead(TrackTest):
    """The camera moves older files down into tmp/ as it rolls, so the top
    level holds only what it is writing now."""

    def test_a_file_in_tmp_is_found(self):
        self.write("20260728141441_0060.gpx",
                   nmea("20260728141441", [("061441", 14.42, 121.02)]))
        self.write("20260728141541_0060_T.gpx",
                   nmea("20260728141541", [("061541", 14.4202, 121.0202)]), sub="tmp")
        got = self.track([clip("20260728141441"), clip("20260728141541")])
        self.assertEqual(len(got), 2, "the fix in tmp/ was not read")

    def test_the_same_minute_written_twice_is_counted_once(self):
        """The camera writes a minute loose AND into the tar archives, and
        both are read. Pooling without dedup would double every point."""
        fix = [("061441", 14.42, 121.02)]
        self.write("20260728141441_0060.gpx", nmea("20260728141441", fix))
        self.write("20260728141441_0060_T.gpx", nmea("20260728141441", fix), sub="tmp")
        self.assertEqual(len(self.track([clip("20260728141441")])), 1)


class TestSelectionIsByTimeNotByName(TrackTest):
    """The point of the rewrite: a filename is where a file was opened."""

    def test_a_clip_with_no_file_of_its_own_still_gets_its_fixes(self):
        """One file rolled long, covering three clips' worth of driving. Two
        of those clips have no file named for them, and name-matching gave
        them nothing at all."""
        # One file opened at 14:14:41 and left open for three minutes. The only
        # fixes in it are from the MIDDLE minute, so what comes back can only
        # have been selected by time.
        self.write("20260728141441_0180.gpx",
                   nmea("20260728141441",
                        [("061550", 14.43, 121.02), ("061600", 14.4301, 121.02)]))
        got = self.track([clip("20260728141541")])
        self.assertEqual(len(got), 2,
                         "the middle clip's fixes are in a file named for another minute")

    def test_a_neighbouring_drive_is_not_borrowed(self):
        """Selection by time has to exclude as well as include: the next
        drive's fixes are in the pool and must stay out."""
        self.write("20260728141441_0060.gpx",
                   nmea("20260728141441", [("061441", 14.42, 121.02)]))
        self.write("20260728180000_0060.gpx",
                   nmea("20260728180000", [("100000", 14.60, 121.10)]))
        got = self.track([clip("20260728141441")])
        self.assertEqual(len(got), 1)
        self.assertLess(got[0][0], 14.50)


class TestAnOverScopedFileKeepsTheRecordingNotTheStrays(TrackTest):
    """Which window survives when one clip's GPX holds more than one clip.

    The rule is the NEWEST window of the clip length, because the camera
    writes the live recording last -- unless an earlier window is several
    times denser, in which case the newest "window" is not a recording at all
    but a handful of strays that happen to carry the biggest clock reading.

    Both halves matter and they pull against each other, so both are pinned
    here: drop the density escape and one forward-stamped phantom becomes the
    whole track; drop the newest-first preference and a couple of missing
    fixes hand a clip its predecessor's data.
    """

    def gpx(self, name: str, fixes) -> Path:
        """Fixes as (utc_hhmmss, lat, lon), no camera header needed."""
        return self.write(name, nmea("20260728141441", fixes))

    def test_one_fix_stamped_ahead_does_not_become_the_track(self):
        """The reproduction. A single phantom an hour in the future made
        itself the latest fix, and all sixty genuine ones fell outside its
        window -- so the clip's track WAS the phantom, and every distance,
        boundary and map frame came off it."""
        real = [(utc(i), 14.4206 + i * 0.00005, 121.0256) for i in range(60)]
        path = self.gpx("20260728141441_0060.gpx",
                        real + [(utc(3600), 14.5177, 121.0296)])
        got = M.parse_gpx_track(path, window_seconds=60)
        self.assertEqual(len(got), 60, "the drive's own fixes are the track")
        self.assertFalse([p for p in got if p[0] > 14.50],
                         "the forward-stamped stray is not the drive")

    def test_a_fix_stamped_behind_is_still_left_out(self):
        """The original failure mode, which the newest-window rule already
        handled. Fixing the forward case must not cost the backward one."""
        real = [(utc(i), 14.4206 + i * 0.00005, 121.0256) for i in range(60)]
        path = self.gpx("20260728141441_0060.gpx",
                        [(utc(-3600), 14.5177, 121.0296)] + real)
        got = M.parse_gpx_track(path, window_seconds=60)
        self.assertEqual(len(got), 60)
        self.assertFalse([p for p in got if p[0] > 14.50])

    def test_a_bundled_neighbour_clip_does_not_win_on_a_dropped_fix(self):
        """Two clips bundled contiguously, the live one missing two fixes to a
        dropout. Counting alone would prefer the fuller EARLIER window and
        hand this clip its predecessor's speeds -- the "already 15 km/h while
        the wheels haven't moved" symptom. The newest window keeps priority
        until an earlier one is several times denser, so it does not."""
        prev = [(utc(i - 60), 14.40, 121.00) for i in range(60)]
        live = [(utc(i), 14.4206 + i * 0.00005, 121.0256)
                for i in range(60) if i not in (20, 40)]
        path = self.gpx("20260728141441_0060.gpx", prev + live)
        got = M.parse_gpx_track(path, window_seconds=60)
        self.assertTrue(all(p[0] > 14.41 for p in got[1:]),
                        "the live clip's own fixes, not the bundled neighbour's")

    def test_the_phantom_costs_the_trip_its_track_end_to_end(self):
        """Not just the parser: the same file read through the pool, which is
        where trip boundaries and the map actually get their points."""
        real = [(utc(i), 14.4206 + i * 0.00005, 121.0256) for i in range(60)]
        self.write("20260728141441_0060.gpx",
                   nmea("20260728141441", real + [(utc(3600), 14.5177, 121.0296)]))
        got = self.track([clip("20260728141441")])
        self.assertEqual(len(got), 60)
        tall_km = (max(p[0] for p in got) - min(p[0] for p in got)) * 111
        self.assertLess(tall_km, 1.0, "one stray must not stretch the bounding box")


if __name__ == "__main__":
    unittest.main()
