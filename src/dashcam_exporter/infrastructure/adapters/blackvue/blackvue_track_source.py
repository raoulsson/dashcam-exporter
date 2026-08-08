import logging
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint
from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)

KNOTS_TO_KMH = 1.852
_GPS_BOX = "gps "


class BlackvueTrackSource:
    """BlackVue telemetry, from a sidecar or from inside the video.

    Two regimes, because the camera changed between model generations.
    Legacy models write <base>_<type>.gps beside the video -- carrying the
    mode letter but no direction letter, so one file serves both cameras.
    The DR900X writes no sidecar at all and the same text lives in an MP4
    box with FourCC 'gps ', so the sidecar is tried first and the container
    second.

    Provenance: this format is taken from BlackVue's manuals and from the
    blackvuesync and blackclue parsers. No BlackVue card was available, so
    it is UNVERIFIED against a real file. The payload is a unix timestamp in
    milliseconds in square brackets followed by an NMEA sentence, and the
    talker is GN rather than GP -- which is exactly why this tool's old
    '$GPRMC' filter would have read nothing at all from this camera.
    """

    def __init__(self, record_directory: Path,
                 logger: logging.Logger | None = None) -> None:
        self._record_directory = record_directory
        self._logger = logger or logging.getLogger(__name__)

    def is_track_artifact(self, path: Path) -> bool:
        return path.suffix.lower() in (".gps", ".3gf")

    def track_for(self, base_filename: str, mode_letter: str,
                  video: Path) -> Track:
        payload = self._sidecar_payload(base_filename, mode_letter)
        if payload is None:
            payload = self._embedded_payload(video)
        if payload is None:
            return Track(points=())
        return Track(points=tuple(self._parse(payload)))

    def _sidecar_payload(self, base_filename: str,
                         mode_letter: str) -> bytes | None:
        sidecar = self._record_directory / ("%s_%s.gps"
                                            % (base_filename, mode_letter))
        try:
            return sidecar.read_bytes() if sidecar.is_file() else None
        except OSError as error:
            self._logger.warning("Cannot read BlackVue sidecar %s: %s",
                                 sidecar, error)
            return None

    def _embedded_payload(self, video: Path) -> bytes | None:
        try:
            if not video.is_file():
                return None
            for fourcc, offset, size in iter_top_level_boxes(video):
                if fourcc == _GPS_BOX:
                    return read_box_payload(video, offset, size)
        except OSError as error:
            self._logger.warning("Cannot read BlackVue video %s: %s",
                                 video, error)
        return None

    def _parse(self, payload: bytes):
        for line in payload.decode("utf-8", errors="ignore").splitlines():
            point = self._point_from(line)
            if point is not None:
                yield point

    def _point_from(self, line: str) -> TrackPoint | None:
        sentence = line[line.index("$"):] if "$" in line else ""
        fields = sentence.split(",")
        # Any talker, RMC only: GGA carries no speed and this tool needs it.
        if len(fields) < 10 or not fields[0].endswith("RMC"):
            return None
        if fields[2] != "A":
            return None
        lat = _to_decimal(fields[3], fields[4])
        lon = _to_decimal(fields[5], fields[6])
        at_utc = _to_utc(fields[9], fields[1])
        if lat is None or lon is None or at_utc is None:
            return None
        try:
            kmh = float(fields[7]) * KNOTS_TO_KMH
        except ValueError:
            kmh = 0.0
        return TrackPoint(lat, lon, kmh, at_utc)


def _to_decimal(value: str, hemisphere: str) -> float | None:
    try:
        if not value or "." not in value:
            return None
        dot = value.index(".")
        degrees = int(value[: dot - 2])
        minutes = float(value[dot - 2:])
        result = degrees + minutes / 60.0
        return -result if hemisphere in ("S", "W") else result
    except (ValueError, IndexError):
        return None


def _to_utc(date_field: str, time_field: str) -> datetime | None:
    try:
        return datetime.strptime(date_field + time_field.split(".")[0],
                                 "%d%m%y%H%M%S")
    except ValueError:
        return None
