"""The Novatek freeGPS blob, read back from what pack_record writes.

Provenance warning: no VIOFO file was available. The layout is transcribed
from Sergei's nvtk_mp42gpx and EgorKin's fork, and reader and writer here
share one definition of it -- so these tests prove self-consistency, not
that the layout matches a real camera.
"""

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.viofo import (
    NovatekGpsReader, pack_record)


def free_box(payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + b"free" + payload


def other_box(fourcc: bytes, size: int) -> bytes:
    return struct.pack(">I", 8 + size) + fourcc + b"\x00" * size


class NovatekGpsReaderTest(unittest.TestCase):
    def test_reads_back_a_record_this_project_wrote(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.412755, 121.043745, 20.0, 30.2))
                + other_box(b"mdat", 8))

            track = NovatekGpsReader().read(video)

        self.assertEqual(len(track.points), 1)
        self.assertAlmostEqual(track.points[0].lat, 14.412755, places=4)
        self.assertAlmostEqual(track.points[0].lon, 121.043745, places=4)
        self.assertAlmostEqual(track.points[0].kmh, 37.04, places=2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 5, 16, 12, 0, 1))

    def test_every_free_box_in_the_file_contributes_its_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.4, 121.0, 10.0, 0.0))
                + other_box(b"mdat", 8)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 2),
                                       14.5, 121.1, 12.0, 0.0)))

            track = NovatekGpsReader().read(video)

        self.assertEqual(len(track.points), 2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 5, 16, 12, 0, 1))

    def test_a_free_box_without_the_magic_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(other_box(b"ftyp", 4)
                              + free_box(b"padding, not telemetry"))

            track = NovatekGpsReader().read(video)

        self.assertTrue(track.is_empty)

    def test_a_record_with_no_fix_is_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.4, 121.0, 10.0, 0.0, active=False)))

            track = NovatekGpsReader().read(video)

        self.assertTrue(track.is_empty)

    def test_a_southern_and_western_fix_comes_back_negative(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       -33.868820, -151.209290, 5.0, 0.0)))

            track = NovatekGpsReader().read(video)

        self.assertAlmostEqual(track.points[0].lat, -33.868820, places=4)
        self.assertAlmostEqual(track.points[0].lon, -151.209290, places=4)


if __name__ == "__main__":
    unittest.main()
