import calendar
import logging
import re
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode, Track

from ..card_layout import CardLayout
from .novatek_gps_reader import NovatekGpsReader

MOVIE_ROOT = "DCIM/Movie"
LOCKED_DIRECTORY = "RO"
PARKING_DIRECTORY = "Parking"

# YYYY_MMDD_HHMMSS_<seq 3-8>[P|E]?[F|R|I|T].MP4
_NAME = re.compile(
    r"^(\d{4})_(\d{2})(\d{2})_(\d{6})_(\d{3,8})([PE]?)([FRIT])\.MP4$",
    re.IGNORECASE)

_CHANNELS = {"F": Channel.FRONT, "R": Channel.REAR,
             "I": Channel.INTERIOR, "T": Channel.TELEPHOTO}
_MARKER_MODES = {"P": ClipMode.PARKING, "E": ClipMode.EVENT}

# The menu offers 1, 2, 3, 5 or 10 minutes and the filename says nothing, so
# this is a declared default rather than a measurement.
SEGMENT_SECONDS = 60


class ViofoCardLayout(CardLayout):
    """A VIOFO card: mode in the folder AND the name, and drifting clocks.

    Pairing is nearest-timestamp within a tolerance, never reconstruction of
    a sibling filename. Owners report the rear file saving a second later
    than the front, and the sequence numbers running independently once one
    camera has been unplugged, so the two names for one moment agree about
    nothing except roughly when it was.

    A file that pairs with nothing becomes its own clip rather than being
    dropped. Footage the tool cannot pair is still footage, and silently
    losing it would be the worst of the available behaviours.
    """

    def __init__(self, card_root: Path, pair_tolerance_seconds: int = 6,
                 logger: logging.Logger | None = None) -> None:
        self._card_root = card_root
        self._movie = card_root / MOVIE_ROOT
        self._tolerance = pair_tolerance_seconds
        self._gps = NovatekGpsReader(logger)

    def clips(self) -> list[Clip]:
        found = [self._describe(path) for path in self._video_files()]
        fronts = sorted((f for f in found if f[1] is Channel.FRONT),
                        key=lambda f: f[0])
        others = [f for f in found if f[1] is not Channel.FRONT]
        claimed: set[Path] = set()
        clips = [self._paired_clip(front, others, claimed) for front in fronts]
        clips.extend(self._orphan_clip(other) for other in others
                     if other[4] not in claimed)
        return sorted(clips, key=lambda clip: clip.timestamp)

    def stamp_of(self, path: Path) -> str | None:
        match = _NAME.match(path.name)
        if not match:
            return None
        return "%s%s%s%s" % (match.group(1), match.group(2), match.group(3),
                             match.group(4))

    def track_for(self, clip: Clip) -> Track | None:
        track = self._gps.read(clip.front)
        return None if track.is_empty else track

    def import_roots(self) -> tuple[Path, ...]:
        return (self._movie,)

    def is_track_artifact(self, path: Path) -> bool:
        # There is no sidecar: the telemetry is inside the video, so the
        # videos themselves are the only artifacts carrying a route.
        return _NAME.match(path.name) is not None

    def _video_files(self) -> list[Path]:
        if not self._movie.is_dir():
            return []
        return [path for path in sorted(self._movie.rglob("*"))
                if path.is_file() and _NAME.match(path.name)]

    def _describe(self, path: Path):
        """(stamp, channel, marker, protected, path) for one video file."""
        match = _NAME.match(path.name)
        stamp = "%s%s%s%s" % (match.group(1), match.group(2), match.group(3),
                              match.group(4))
        marker = match.group(6).upper()
        parents = {parent.name for parent in path.parents}
        protected = LOCKED_DIRECTORY in parents
        if PARKING_DIRECTORY in parents and not marker:
            marker = "P"
        return stamp, _CHANNELS[match.group(7).upper()], marker, protected, path

    def _paired_clip(self, front, others, claimed: set[Path]) -> Clip:
        stamp, _channel, marker, protected, path = front
        epoch = self._epoch(stamp)
        videos = {Channel.FRONT: path}
        for other in others:
            if other[4] in claimed or other[1] in videos:
                continue
            if abs(self._epoch(other[0]) - epoch) <= self._tolerance:
                videos[other[1]] = other[4]
                claimed.add(other[4])
        return self._to_clip(stamp, epoch, marker, protected, videos)

    def _orphan_clip(self, other) -> Clip:
        stamp, channel, marker, protected, path = other
        return self._to_clip(stamp, self._epoch(stamp), marker, protected,
                             {channel: path})

    def _to_clip(self, stamp: str, epoch: int, marker: str, protected: bool,
                 videos: dict[Channel, Path]) -> Clip:
        return Clip(timestamp=stamp, epoch_utc=epoch,
                    playback_seconds=SEGMENT_SECONDS,
                    wall_seconds=SEGMENT_SECONDS,
                    videos=videos,
                    mode=_MARKER_MODES.get(marker, ClipMode.NORMAL),
                    source_mode=marker,
                    protected=protected)

    @staticmethod
    def _epoch(stamp: str) -> int:
        parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        return calendar.timegm(parsed.timetuple())
