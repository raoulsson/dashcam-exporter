import struct
from pathlib import Path
from typing import Iterator

_HEADER = 8
_LARGE_HEADER = 16


def iter_top_level_boxes(path: Path) -> Iterator[tuple[str, int, int]]:
    """Yield (fourcc, payload offset, payload size) for each top-level box.

    Stops at the first malformed header rather than raising. A dashcam file
    truncated by a power cut mid-write is an ordinary thing to meet on a
    card, and a walk that raised would turn one bad file into a failed
    import.
    """
    size = path.stat().st_size
    offset = 0
    with path.open("rb") as handle:
        while offset + _HEADER <= size:
            handle.seek(offset)
            header = handle.read(_HEADER)
            if len(header) < _HEADER:
                return
            box_size = struct.unpack(">I", header[:4])[0]
            fourcc = header[4:8].decode("latin-1")
            payload_offset = offset + _HEADER
            if box_size == 1:
                extra = handle.read(8)
                if len(extra) < 8:
                    return
                box_size = struct.unpack(">Q", extra)[0]
                payload_offset = offset + _LARGE_HEADER
            elif box_size == 0:
                box_size = size - offset
            if box_size < (payload_offset - offset) or offset + box_size > size:
                return
            yield fourcc, payload_offset, offset + box_size - payload_offset
            offset += box_size


def read_box_payload(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)
