import logging
import os
import re
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint

KNOTS_TO_KMH = 1.852

_ARCHIVE_NAME = re.compile(r"^(\d{14})_(\d+)\.git$", re.IGNORECASE)


class DdpaiTrackSource:
    """DDPAI's GPS: NMEA inside tar archives the camera calls '.git'.

    Archives are named with their OWN start and a span, not with a clip's
    stamp, so they are selected by time overlap. Matching them by stamp once
    took two files off a card that held thirty for that day and produced a
    drive with no route at all, from footage whose track was sitting right
    there.
    """

    def __init__(self, tar_directory: Path,
                 logger: logging.Logger | None = None) -> None:
        self._tar_directory = tar_directory
        self._logger = logger or logging.getLogger(__name__)

    def is_track_artifact(self, path: Path) -> bool:
        return path.suffix.lower() in (".gpx", ".git")

    def track_covering(self, started_at: datetime,
                       ended_at: datetime) -> Track:
        points: list[TrackPoint] = []
        for archive in self._archives_overlapping(started_at, ended_at):
            points.extend(self._points_in(archive))
        inside = [p for p in points if started_at <= p.at_utc <= ended_at]
        return Track(points=tuple(sorted(inside, key=lambda p: p.at_utc)))

    def _archives_overlapping(self, started_at: datetime,
                              ended_at: datetime) -> list[Path]:
        # rglob, not iterdir: the camera keeps recent archives directly under
        # tar and moves older ones into tar/tmp.
        found = []
        if not self._tar_directory.is_dir():
            return found
        for archive in sorted(self._tar_directory.rglob("*.git")):
            if archive.name.startswith("._"):
                continue
            span = self._span_of(archive)
            if span is None:
                found.append(archive)      # unreadable name: read it anyway
                continue
            archive_start, archive_end = span
            if archive_start <= ended_at and archive_end >= started_at:
                found.append(archive)
        return found

    def _span_of(self, archive: Path) -> tuple[datetime, datetime] | None:
        match = _ARCHIVE_NAME.match(archive.name)
        if not match:
            return None
        try:
            start = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return start, start + timedelta(seconds=int(match.group(2)))

    def _points_in(self, archive: Path) -> list[TrackPoint]:
        points: list[TrackPoint] = []
        try:
            with tarfile.open(archive, "r") as handle:
                for member in handle.getmembers():
                    name = os.path.basename(member.name)
                    if not name.lower().endswith(".gpx") or name.startswith("._"):
                        continue
                    stream = handle.extractfile(member)
                    if stream is None:
                        continue
                    points.extend(self._parse(stream.read()))
        except (tarfile.TarError, OSError) as error:
            self._logger.warning("Cannot read DDPAI GPS archive %s: %s",
                                 archive, error)
        return points

    def _parse(self, payload: bytes) -> list[TrackPoint]:
        points = []
        for line in payload.decode("utf-8", errors="ignore").splitlines():
            point = self._point_from(line)
            if point is not None:
                points.append(point)
        return points

    def _point_from(self, line: str) -> TrackPoint | None:
        if not line.startswith("$GPRMC"):
            return None
        fields = line.split(",")
        # $GPRMC,time,status,lat,N,lon,E,speed_knots,heading,date,...
        if len(fields) < 10 or fields[2] != "A":
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
    """NMEA ddmm.mmmm / dddmm.mmmm to decimal degrees."""
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
    """NMEA ddmmyy plus hhmmss.ss to a UTC datetime."""
    try:
        return datetime.strptime(date_field + time_field.split(".")[0],
                                 "%d%m%y%H%M%S")
    except ValueError:
        return None
