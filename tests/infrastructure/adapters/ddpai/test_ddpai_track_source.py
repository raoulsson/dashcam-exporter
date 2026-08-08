"""Turning DDPAI's mislabeled tar archives into our Track."""

import io
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiTrackSource

NMEA = (
    "$GPRMC,170530.00,A,4712.3456,N,00832.1234,E,20.0,35.3,060826,,,A*52\n"
    "$GPRMC,170531.00,A,4712.4000,N,00832.2000,E,30.0,35.3,060826,,,A*52\n"
    "$GPRMC,170532.00,V,4712.5000,N,00832.3000,E,99.0,35.3,060826,,,A*52\n"
)


def write_archive(directory: Path, name: str, member: str,
                  payload: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    info = tarfile.TarInfo(member)
    info.size = len(payload)
    with tarfile.open(directory / name, "w") as handle:
        handle.addfile(info, io.BytesIO(payload))


class DdpaiTrackSourceTest(unittest.TestCase):
    def test_reads_nmea_from_a_nested_archive_into_our_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory / "tmp", "20260806170529_0540.git",
                          "nested/20260806170529_0060.gpx", NMEA.encode())

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 6, 17, 5, 29),
                datetime(2026, 8, 6, 17, 6, 29))

        self.assertEqual(len(track.points), 2)
        self.assertAlmostEqual(track.points[0].lat, 47.205760, places=5)
        self.assertAlmostEqual(track.points[0].lon, 8.535390, places=5)
        self.assertAlmostEqual(track.points[0].kmh, 37.04, places=2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 8, 6, 17, 5, 30))

    def test_a_window_the_archives_do_not_cover_yields_an_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory, "20260806170529_0540.git",
                          "20260806170529_0060.gpx", NMEA.encode())

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 7, 9, 0, 0),
                datetime(2026, 8, 7, 9, 1, 0))

        self.assertTrue(track.is_empty)

    def test_a_corrupt_archive_is_survived_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "20260806170529_0540.git").write_bytes(b"not a tar")

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 6, 17, 5, 29),
                datetime(2026, 8, 6, 17, 6, 29))

        self.assertTrue(track.is_empty)

    def test_recognises_both_archive_and_plain_gpx_as_track_artifacts(self):
        source = DdpaiTrackSource(Path("/nowhere"))

        self.assertTrue(source.is_track_artifact(Path("a/20260806_0540.git")))
        self.assertTrue(source.is_track_artifact(Path("a/20260806.GPX")))
        self.assertFalse(source.is_track_artifact(Path("a/20260806_0060.mp4")))


if __name__ == "__main__":
    unittest.main()
