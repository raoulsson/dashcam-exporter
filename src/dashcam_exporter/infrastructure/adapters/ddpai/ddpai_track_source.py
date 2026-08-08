import logging
import os
import re
import tarfile
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint

KNOTS_TO_KMH = 1.852

# The optional _T is real. A card carried 73 archives named 19700101...._T.git
# -- the camera booted with no clock -- and not one of them is a readable tar.
_ARCHIVE_NAME = re.compile(r"^(\d{14})_(\d+)(_T)?\.git$", re.IGNORECASE)
_MEMBER_STAMP = re.compile(r"^(\d{14})_\d+\.gpx$", re.IGNORECASE)

# A stamp before this is the camera's uninitialised clock, not a recording.
_CLOCK_EPOCH = datetime(2000, 1, 1)


class DdpaiTrackSource:
    """DDPAI's GPS: NMEA inside tar archives the camera calls '.git'.

    Selection is by member name, not by archive name and not by time. The
    archive is stamped with its own start and a span, which is why matching
    archives to clips by stamp once lost a whole drive's route -- but the
    .gpx MEMBERS inside are named for the clips they belong to, exactly.

    That distinction matters more than it looks: the clip stamp is the
    camera's local wall clock and the NMEA inside is UTC, eight hours apart
    on the card this was calibrated against. Anything that compares the two
    finds nothing, silently. Matching names never asks the question.
    """

    def __init__(self, tar_directory: Path,
                 logger: logging.Logger | None = None) -> None:
        self._tar_directory = tar_directory
        self._logger = logger or logging.getLogger(__name__)
        self._index: dict[str, tuple[Path, str]] | None = None

    def is_track_artifact(self, path: Path) -> bool:
        return path.suffix.lower() in (".gpx", ".git")

    def track_for_stamp(self, stamp: str) -> Track:
        """The fixes recorded during the clip with this canonical stamp."""
        located = self._member_index().get(stamp)
        if located is None:
            return Track(points=())
        archive, member = located
        points = self._points_in(archive, member)
        return Track(points=tuple(sorted(points, key=lambda p: p.at_utc)))

    def _member_index(self) -> dict[str, tuple[Path, str]]:
        """Clip stamp to the archive and member holding its fixes.

        Built once: a card holds a hundred archives and several hundred
        clips, and re-opening every archive per clip would read the same
        indexes hundreds of times over.
        """
        if self._index is not None:
            return self._index
        index: dict[str, tuple[Path, str]] = {}
        for archive in self._archives():
            try:
                with tarfile.open(archive, "r") as handle:
                    for member in handle.getnames():
                        match = _MEMBER_STAMP.match(os.path.basename(member))
                        if match:
                            index.setdefault(match.group(1), (archive, member))
            except (tarfile.TarError, OSError) as error:
                self._logger.warning("Cannot read DDPAI GPS archive %s: %s",
                                     archive, error)
        self._index = index
        return index

    def _archives(self) -> list[Path]:
        # rglob, not iterdir: the camera keeps recent archives directly under
        # tar and moves older ones into tar/tmp.
        if not self._tar_directory.is_dir():
            return []
        return [archive for archive in sorted(self._tar_directory.rglob("*.git"))
                if not archive.name.startswith("._")
                and not self._is_uninitialised_clock(archive)]

    def _is_uninitialised_clock(self, archive: Path) -> bool:
        match = _ARCHIVE_NAME.match(archive.name)
        if not match:
            return False
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S") < _CLOCK_EPOCH
        except ValueError:
            return False

    def _points_in(self, archive: Path, member: str) -> list[TrackPoint]:
        try:
            with tarfile.open(archive, "r") as handle:
                stream = handle.extractfile(member)
                if stream is None:
                    return []
                return self._parse(stream.read())
        except (tarfile.TarError, OSError) as error:
            self._logger.warning("Cannot read DDPAI GPS archive %s: %s",
                                 archive, error)
            return []

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
