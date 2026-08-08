"""Walking the top level of an ISO base media file."""

import struct
import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)


def box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


class Mp4BoxesTest(unittest.TestCase):
    def test_walks_every_top_level_box_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom")
                             + box(b"free", b"GPS payload")
                             + box(b"mdat", b"\x00" * 16))

            found = [(name, size) for name, _, size
                     in iter_top_level_boxes(path)]

        self.assertEqual(found, [("ftyp", 4), ("free", 11), ("mdat", 16)])

    def test_reads_one_box_payload_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom") + box(b"free", b"GPS x"))

            boxes = {name: (offset, size)
                     for name, offset, size in iter_top_level_boxes(path)}
            offset, size = boxes["free"]

            self.assertEqual(read_box_payload(path, offset, size), b"GPS x")

    def test_a_truncated_box_header_ends_the_walk_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom") + b"\x00\x00")

            found = [name for name, _, _ in iter_top_level_boxes(path)]

        self.assertEqual(found, ["ftyp"])

    def test_a_size_of_one_means_a_64_bit_length_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            large = (struct.pack(">I", 1) + b"mdat"
                     + struct.pack(">Q", 16 + 4) + b"data")
            path.write_bytes(box(b"ftyp", b"isom") + large)

            found = [(name, size) for name, _, size
                     in iter_top_level_boxes(path)]

        self.assertEqual(found, [("ftyp", 4), ("mdat", 4)])


if __name__ == "__main__":
    unittest.main()
