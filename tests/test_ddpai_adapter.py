"""Semantic fixtures for the canonical DDPAI source adapter."""

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters import DdpaiDataAdapter


class DdpaiAdapterTest(unittest.TestCase):
    def test_pairs_rear_by_clock_when_camera_is_one_second_late(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            front = root / "front"
            rear = root / "rear"
            front.mkdir()
            rear.mkdir()
            (front / "20260728141441_0060.mp4").touch()
            (rear / "20260728141442_0060_A.mp4").touch()

            clips = DdpaiDataAdapter().discover_clips(front, rear)

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].duration, 60)
        self.assertEqual(clips[0].rear.name, "20260728141442_0060_A.mp4")

    def test_extracts_gpx_from_nested_camera_tar_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archives = root / "tar" / "tmp"
            cache = root / "cache"
            archives.mkdir(parents=True)
            archive = archives / "20260728.git"
            payload = b"$GPSCAMTIME 20260728141441\n"
            info = tarfile.TarInfo("nested/20260728141441_0060.gpx")
            info.size = len(payload)
            with tarfile.open(archive, "w") as handle:
                handle.addfile(info, io.BytesIO(payload))

            result = DdpaiDataAdapter().prepare_gps(root / "tar", cache)

            self.assertEqual(result, (1, 1))
            self.assertEqual((cache / "20260728141441_0060.gpx").read_bytes(), payload)
            self.assertEqual(DdpaiDataAdapter().prepare_gps(root / "tar", cache), (1, 0))


if __name__ == "__main__":
    unittest.main()
