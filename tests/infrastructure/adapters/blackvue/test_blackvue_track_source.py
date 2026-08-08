"""BlackVue telemetry, read back from payloads this project also writes.

Provenance warning: no BlackVue card was available. Every byte here was
written from the same reading of the same documents as the reader, so these
tests prove the two halves agree -- not that either matches a real camera.
"""

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.blackvue import BlackvueTrackSource

SIDECAR = (
    "[1611723852888]$GNRMC,155052.00,A,4529.87489,N,07337.01215,W,"
    "6.225,35.34,270121,,,A*52\n"
    "[1611723853888]$GNRMC,155053.00,A,4529.88000,N,07337.02000,W,"
    "12.000,35.34,270121,,,A*52\n"
    "[1611723854888]$GNRMC,155054.00,V,4529.89000,N,07337.03000,W,"
    "99.000,35.34,270121,,,A*52\n"
)


def box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


class BlackvueTrackSourceTest(unittest.TestCase):
    def test_reads_a_sidecar_named_without_the_direction_letter(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            (record / "20210127_155052_N.gps").write_text(SIDECAR)
            video = record / "20210127_155052_NF.mp4"
            video.touch()

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertEqual(len(track.points), 2)
        self.assertAlmostEqual(track.points[0].lat, 45.4979148, places=5)
        self.assertAlmostEqual(track.points[0].lon, -73.6168692, places=5)
        self.assertAlmostEqual(track.points[0].kmh, 11.529, places=3)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2021, 1, 27, 15, 50, 52))

    def test_falls_back_to_the_box_inside_the_video_when_no_sidecar_exists(self):
        # The DR900X writes no sidecar at all; this is its only copy.
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            video = record / "20210127_155052_NF.mp4"
            video.write_bytes(box(b"ftyp", b"isom")
                              + box(b"gps ", SIDECAR.encode() + b"\x00")
                              + box(b"mdat", b"\x00" * 8))

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertEqual(len(track.points), 2)

    def test_a_clip_that_recorded_nothing_yields_an_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            video = record / "20210127_155052_NF.mp4"
            video.write_bytes(box(b"ftyp", b"isom"))

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertTrue(track.is_empty)

    def test_accelerometer_and_gps_sidecars_both_count_as_track_artifacts(self):
        source = BlackvueTrackSource(Path("/nowhere"))

        self.assertTrue(source.is_track_artifact(Path("a/x_N.gps")))
        self.assertTrue(source.is_track_artifact(Path("a/x_N.3gf")))
        self.assertFalse(source.is_track_artifact(Path("a/x_NF.mp4")))


if __name__ == "__main__":
    unittest.main()
