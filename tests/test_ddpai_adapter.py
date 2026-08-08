"""The deprecated two-method shim renderer.py still calls."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters import DdpaiDataAdapter


class DdpaiDataAdapterTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
