"""Our own track on disk: written once, read without a camera in sight."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import DexGpsFile, Track, TrackPoint

TRACK = Track(points=(
    TrackPoint(14.412755, 121.043745, 37.04, datetime(2026, 8, 6, 9, 5, 30)),
    TrackPoint(14.413000, 121.044000, 41.20, datetime(2026, 8, 6, 9, 5, 31)),
))


class DexGpsFileTest(unittest.TestCase):
    def test_a_track_survives_the_round_trip_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "20260806170529.json"
            DexGpsFile.write(path, TRACK)
            back = DexGpsFile.read(path)

        self.assertEqual(len(back.points), 2)
        self.assertEqual(back.points[0].at_utc, datetime(2026, 8, 6, 9, 5, 30))
        self.assertAlmostEqual(back.points[0].lat, 14.412755, places=6)
        self.assertAlmostEqual(back.points[1].kmh, 41.20, places=2)

    def test_the_file_is_readable_without_this_code(self):
        # It is our format, but it should not need our parser to inspect.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "t.json"
            DexGpsFile.write(path, TRACK)
            raw = json.loads(path.read_text())

        self.assertEqual(raw["points"][0][3], "2026-08-06T09:05:30")
        self.assertEqual(len(raw["points"]), 2)

    def test_an_empty_track_writes_and_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "t.json"
            DexGpsFile.write(path, Track(points=()))

            self.assertTrue(DexGpsFile.read(path).is_empty)

    def test_a_missing_or_unreadable_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "nope.json"
            broken = Path(temporary) / "broken.json"
            broken.write_text("{not json")

            self.assertTrue(DexGpsFile.read(missing).is_empty)
            self.assertTrue(DexGpsFile.read(broken).is_empty)


if __name__ == "__main__":
    unittest.main()
