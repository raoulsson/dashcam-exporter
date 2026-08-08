import logging
import struct
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint
from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)

KNOTS_TO_KMH = 1.852

MAGIC = b"GPS "

# A uint32 at offset 12 tells the firmware variant apart. Transcribed from
# EgorKin's fork of Sergei's extractor, whose comments date each addition:
# 0x58 arrived with the A229, 0x2C with a later A229 firmware, 0x3F0 with
# the A129 Plus Duo. Only the 0x58 path is exercised by any test here,
# because it is the one this project's own writer produces. The other two
# are UNTESTED.
_VARIANT_OFFSETS = {0x58: 0x30, 0x2C: 0x10, 0x3F0: 0x10}
_DEFAULT_OFFSET = 0x30
_DISCRIMINATOR_AT = 12
_RECORD = struct.Struct("<6I4c4f")


def pack_record(at_utc: datetime, lat: float, lon: float, knots: float,
                course: float, active: bool = True) -> bytes:
    """Build one freeGPS payload, so writer and reader share one layout.

    Exported for the card simulator deliberately. Two transcriptions of a
    binary format drift; one definition used from both ends cannot. It also
    means a mistake here is invisible to every test -- which is stated in
    the module docstring of those tests rather than left to be discovered.
    """
    body = _RECORD.pack(
        at_utc.hour, at_utc.minute, at_utc.second,
        at_utc.year, at_utc.month, at_utc.day,
        b"A" if active else b"V",
        b"N" if lat >= 0 else b"S",
        b"E" if lon >= 0 else b"W",
        b"\x00",
        _to_hybrid(lat), _to_hybrid(lon), knots, course)
    head = bytearray(MAGIC + b"\x00" * (_DEFAULT_OFFSET - len(MAGIC)))
    struct.pack_into("<I", head, _DISCRIMINATOR_AT, 0x58)
    return bytes(head) + body


class NovatekGpsReader:
    """GPS from the free boxes Novatek-based cameras interleave in the MP4.

    Provenance: transcribed from Sergei's nvtk_mp42gpx and EgorKin's fork.
    No VIOFO file was available, so this is UNVERIFIED against a real
    camera; it is verified only against pack_record above, which was written
    from the same source.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def read(self, video: Path) -> Track:
        points: list[TrackPoint] = []
        try:
            if not video.is_file():
                return Track(points=())
            for fourcc, offset, size in iter_top_level_boxes(video):
                if fourcc != "free" or size < _DEFAULT_OFFSET:
                    continue
                payload = read_box_payload(video, offset, size)
                if not payload.startswith(MAGIC):
                    continue
                point = self._point_from(payload)
                if point is not None:
                    points.append(point)
        except OSError as error:
            self._logger.warning("Cannot read VIOFO video %s: %s", video, error)
        return Track(points=tuple(sorted(points, key=lambda p: p.at_utc)))

    def _point_from(self, payload: bytes) -> TrackPoint | None:
        start = self._record_offset(payload)
        if start + _RECORD.size > len(payload):
            return None
        (hour, minute, second, year, month, day,
         active, lat_hemisphere, lon_hemisphere, _unknown,
         lat, lon, knots, _course) = _RECORD.unpack_from(payload, start)
        if active != b"A":
            return None
        try:
            at_utc = datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
        return TrackPoint(_from_hybrid(lat, lat_hemisphere),
                          _from_hybrid(lon, lon_hemisphere),
                          knots * KNOTS_TO_KMH, at_utc)

    def _record_offset(self, payload: bytes) -> int:
        if len(payload) < _DISCRIMINATOR_AT + 4:
            return _DEFAULT_OFFSET
        variant = struct.unpack_from("<I", payload, _DISCRIMINATOR_AT)[0]
        return _VARIANT_OFFSETS.get(variant, _DEFAULT_OFFSET)


def _to_hybrid(degrees: float) -> float:
    """Decimal degrees to the DDDmm.mmmm the format stores."""
    value = abs(degrees)
    whole = int(value)
    return whole * 100 + (value - whole) * 60


def _from_hybrid(value: float, hemisphere: bytes) -> float:
    whole = int(abs(value) // 100)
    minutes = abs(value) - whole * 100
    result = whole + minutes / 60.0
    return -result if hemisphere in (b"S", b"W") else result
