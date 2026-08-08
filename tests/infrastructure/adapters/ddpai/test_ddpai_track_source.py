"""Turning DDPAI's mislabeled tar archives into our Track."""

import io
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiTrackSource

# Recorded at 09:05 UTC. The clip it belongs to is stamped 17:05:29, because
# the camera writes local wall clock and the fix is UTC -- eight hours apart
# on the card this was calibrated against.
NMEA = (
    "$GPRMC,090530.00,A,4712.3456,N,00832.1234,E,20.0,35.3,060826,,,A*52\n"
    "$GPRMC,090531.00,A,4712.4000,N,00832.2000,E,30.0,35.3,060826,,,A*52\n"
    "$GPRMC,090532.00,V,4712.5000,N,00832.3000,E,99.0,35.3,060826,,,A*52\n"
)


def write_archive(directory: Path, name: str, members: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(directory / name, "w") as handle:
        for member, payload in members.items():
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))


class DdpaiTrackSourceTest(unittest.TestCase):
    def test_finds_the_member_named_for_the_clip_and_converts_its_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory / "tmp", "20260806170529_0540.git",
                          {"nested/20260806170529_0060.gpx": NMEA.encode()})

            track = DdpaiTrackSource(tar_directory).track_for_stamp(
                "20260806170529")

        self.assertEqual(len(track.points), 2)
        self.assertAlmostEqual(track.points[0].lat, 47.205760, places=5)
        self.assertAlmostEqual(track.points[0].lon, 8.535390, places=5)
        self.assertAlmostEqual(track.points[0].kmh, 37.04, places=2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 8, 6, 9, 5, 30))

    def test_the_local_clip_stamp_is_never_compared_against_the_utc_fix(self):
        # The regression this was written for: comparing a 17:05 local stamp
        # against a 09:05 UTC fix found nothing on a card holding 9,868 of
        # them, and reported no route rather than an error.
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory, "20260806170529_0540.git",
                          {"20260806170529_0060.gpx": NMEA.encode()})

            track = DdpaiTrackSource(tar_directory).track_for_stamp(
                "20260806170529")

        self.assertFalse(track.is_empty)

    def test_a_stamp_no_archive_carries_yields_an_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory, "20260806170529_0540.git",
                          {"20260806170529_0060.gpx": NMEA.encode()})

            track = DdpaiTrackSource(tar_directory).track_for_stamp(
                "20260807090000")

        self.assertTrue(track.is_empty)

    def test_archives_stamped_before_the_camera_had_a_clock_are_never_opened(self):
        # 73 of these sat in tar/tmp on a real card, none of them readable
        # tars. Skipping them by name keeps a hundred warnings out of a run.
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "19700101004510_0100_T.git").write_bytes(b"not a tar")

            with self.assertNoLogs(
                    "dashcam_exporter.infrastructure.adapters.ddpai"
                    ".ddpai_track_source", level="WARNING"):
                track = DdpaiTrackSource(tar_directory).track_for_stamp(
                    "20260806170529")

        self.assertTrue(track.is_empty)

    def test_a_corrupt_but_plausibly_stamped_archive_is_survived(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "20260806170529_0540.git").write_bytes(b"not a tar")

            track = DdpaiTrackSource(tar_directory).track_for_stamp(
                "20260806170529")

        self.assertTrue(track.is_empty)

    def test_recognises_both_archive_and_plain_gpx_as_track_artifacts(self):
        source = DdpaiTrackSource(Path("/nowhere"))

        self.assertTrue(source.is_track_artifact(Path("a/20260806_0540.git")))
        self.assertTrue(source.is_track_artifact(Path("a/20260806.GPX")))
        self.assertFalse(source.is_track_artifact(Path("a/20260806_0060.mp4")))



class DdpaiExtractionTest(unittest.TestCase):
    """The file-shaped cache the renderer still keeps, filled from one place."""

    def test_writes_members_out_once_and_skips_what_is_already_there(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar_directory = root / "tar"
            cache = root / "cache"
            write_archive(tar_directory, "20260806170529_0540.git",
                          {"nested/20260806170529_0060.gpx": NMEA.encode()})

            first = DdpaiTrackSource(tar_directory).extract_members_into(cache)
            second = DdpaiTrackSource(tar_directory).extract_members_into(cache)
            written = (cache / "20260806170529_0060.gpx").read_bytes()

        self.assertEqual(first, (1, 1))
        self.assertEqual(second, (1, 0))
        self.assertEqual(written, NMEA.encode())

    def test_placeholder_archives_are_never_opened_or_counted(self):
        # The renderer's own copy of this walk opened all 73 of them on a
        # real card and logged a failure for each.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar_directory = root / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "19700101004510_0100_T.git").write_bytes(b"\x00" * 64)

            result = DdpaiTrackSource(tar_directory).extract_members_into(
                root / "cache")

        self.assertEqual(result, (0, 0))

    def test_a_missing_tar_directory_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = DdpaiTrackSource(root / "absent").extract_members_into(
                root / "cache")

        self.assertEqual(result, (0, 0))

if __name__ == "__main__":
    unittest.main()
