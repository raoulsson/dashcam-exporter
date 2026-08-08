import json
from datetime import datetime
from pathlib import Path

from .track import Track, TrackPoint

VERSION = 1


class DexGpsFile:
    """A Track on disk, in the one GPS format this tool owns.

    JSON rather than GPX or NMEA because nothing but this tool ever reads it.
    Writing GPX would mean parsing XML back into numbers we already had, and
    writing NMEA would mean re-encoding decimal degrees into the ddmm.mmmm a
    camera happens to use -- a round trip through somebody else's format for
    no reader's benefit.

    A point is [lat, lon, kmh, iso8601] rather than an object per fix: a
    drive is tens of thousands of fixes, and four keys repeated that many
    times is most of the file.
    """

    @staticmethod
    def write(path, track: Track) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "points": [[p.lat, p.lon, p.kmh, p.at_utc.isoformat()]
                       for p in track.points],
        }
        destination.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def read(path) -> Track:
        """The track, or an empty one if the file is missing or unreadable.

        Empty rather than raising: a drive with no route is an ordinary
        outcome this tool already renders, and a cache file damaged by a
        power cut should cost that drive its route, not the whole run.
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Track(points=())
        points = []
        for row in raw.get("points", []):
            try:
                points.append(TrackPoint(float(row[0]), float(row[1]),
                                         float(row[2]),
                                         datetime.fromisoformat(row[3])))
            except (TypeError, ValueError, IndexError):
                continue
        return Track(points=tuple(points))
