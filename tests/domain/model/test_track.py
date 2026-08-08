"""The track a camera recorded, once it has become one of our own types."""

import unittest
from datetime import datetime

from dashcam_exporter.domain import Track, TrackPoint


class TrackTest(unittest.TestCase):
    def test_empty_track_has_no_span(self):
        track = Track(points=())

        self.assertTrue(track.is_empty)
        self.assertIsNone(track.started_at)
        self.assertIsNone(track.ended_at)

    def test_span_comes_from_first_and_last_point(self):
        first = TrackPoint(47.1, 8.2, 31.0, datetime(2026, 8, 6, 17, 5, 29))
        last = TrackPoint(47.2, 8.3, 44.0, datetime(2026, 8, 6, 17, 6, 29))

        track = Track(points=(first, last))

        self.assertFalse(track.is_empty)
        self.assertEqual(track.started_at, datetime(2026, 8, 6, 17, 5, 29))
        self.assertEqual(track.ended_at, datetime(2026, 8, 6, 17, 6, 29))


if __name__ == "__main__":
    unittest.main()
