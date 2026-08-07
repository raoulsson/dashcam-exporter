from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.domain import Clip

KNOTS_TO_KMH = 1.852
CLIP_GPX_WINDOW_SECONDS = 60
PARKING_SPEED_THRESHOLD_KMH = 3.0
PARKING_CLIP_FRACTION = 0.75
_TZ_QUANTUM = 900
_SPAN_MARGIN = timedelta(0)


def _nmea_to_decimal(value: str, hemi: str) -> float | None:
    """Convert NMEA ddmm.mmmm / dddmm.mmmm coordinates to decimal degrees."""
    try:
        if not value or "." not in value:
            return None
        dot = value.index(".")
        deg = int(value[: dot - 2])
        minutes = float(value[dot - 2 :])
        result = deg + minutes / 60.0
        return -result if hemi in ("S", "W") else result
    except (ValueError, IndexError):
        return None

def parse_gpx_track(gpx_path: Path,
                    window_seconds: int | None = None,
                    ) -> list[tuple[float, float, float, datetime]]:
    """
    Return a list of (lat, lon, kmh, utc_datetime) tuples parsed from $GPRMC lines.
    Skips fixes marked invalid (status != 'A').

    If the parsed points span much more than `window_seconds`, only the
    densest window of that length is returned — discarding stale fixes the
    dashcam firmware bundled in from a previous drive (a real-world failure
    mode where drive N's clip GPX contains data from drive N-1's last
    location, which would otherwise blow up the drive's bounding box and
    make the marker animation jump across town).
    """
    points: list[tuple[float, float, float, datetime]] = []
    try:
        with gpx_path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.startswith("$GPRMC"):
                    continue
                fields = line.split(",")
                # $GPRMC,time,status,lat,N,lon,E,speed_knots,heading,date,...
                if len(fields) < 10 or fields[2] != "A":
                    continue
                lat = _nmea_to_decimal(fields[3], fields[4])
                lon = _nmea_to_decimal(fields[5], fields[6])
                if lat is None or lon is None:
                    continue
                try:
                    kmh = float(fields[7]) * KNOTS_TO_KMH
                except ValueError:
                    kmh = 0.0
                ts_str = fields[1]            # e.g. 101005.000
                date_str = fields[9]          # e.g. 110526 (ddmmyy)
                try:
                    hh = int(ts_str[0:2]); mm = int(ts_str[2:4]); ss = int(ts_str[4:6])
                    dd = int(date_str[0:2]); mo = int(date_str[2:4]); yr = 2000 + int(date_str[4:6])
                    dt = datetime(yr, mo, dd, hh, mm, ss)
                except (ValueError, IndexError):
                    dt = datetime(1970, 1, 1)
                points.append((lat, lon, kmh, dt))
    except OSError:
        pass
    # Resolve default at call time so config-loaded changes to the global
    # take effect (default-arg values would freeze at import time).
    if window_seconds is None:
        window_seconds = CLIP_GPX_WINDOW_SECONDS
    if not points or window_seconds <= 0:
        return points
    # If everything already fits within 1.5x the expected clip window, the
    # GPX is correctly scoped — return as-is. Common case, fast-path.
    points.sort(key=lambda p: p[3])
    if (points[-1][3] - points[0][3]).total_seconds() <= window_seconds * 1.5:
        return points
    # File contains more than one clip's worth of data. Two known failure modes:
    #   1. Cross-drive stale data: DDPAI parking-mode buffer dumps points
    #      from a previous drive (hours earlier) into the current clip's
    #      GPX. These are time-disjoint and easy to drop.
    #   2. Multi-clip bundle: the GPX file for clip N actually contains BOTH
    #      clip N-1's and clip N's data, time-contiguous (no gap between
    #      17:24:59 and 17:25:00). The speeds[] array then starts with the
    #      WRONG clip's data, so the speed overlay shows the previous clip's
    #      acceleration ramp burned onto the current clip's still-parked
    #      footage (visible as "speed already 15 km/h when wheels haven't
    #      moved yet").
    # Both cases are handled by keeping only the points within
    # `window_seconds` of the LATEST fix (= the actual clip's data, since
    # DDPAI always writes the live recording last and any extra junk earlier).
    latest = points[-1][3]
    return [p for p in points if (latest - p[3]).total_seconds() <= window_seconds]


# Real timezones are whole quarter-hours, so the offset is rounded to one.
# That turns a per-file reading that can be a second or two out into the same
# exact value for every file, which is what makes the median stable.
_TZ_QUANTUM = 900

# NO MARGIN, and the upper bound is exclusive. Clips are CONTIGUOUS: one ends
# on the second the next begins, so any margin at all hands a clip its
# neighbours' fixes. At 90 seconds on a 60-second clip it handed over both of
# them whole, and Track.endpoints -- which reads the first and last fix to
# decide where the car was when a clip started and stopped -- then answered
# with a position from the clip before and one from the clip after. Trip
# boundaries are found from exactly those two readings.
#
# Half-open rather than merely zero-width, because a fix landing exactly on the
# boundary second would otherwise belong to both clips.
_SPAN_MARGIN = timedelta(0)


def _camera_utc_offset(paths: list[Path]) -> timedelta:
    """UTC minus camera-local, read from the camera's own statement.

    Every healthy NMEA file opens with `$GPSCAMTIME <local yyyymmddHHMMSS>`
    and then carries `$GPRMC,<utc hhmmss>` lines. The pair gives the camera's
    timezone directly, per file, with no setting to configure and nothing to
    infer from filenames.

    The median across files, because a file whose GPS never got a fix replays
    an old sentence and reads wildly off -- which is the very defect this
    exists to survive. One such file cannot move a median.
    """
    readings = []
    for path in paths:
        local = utc = None
        try:
            with path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("$GPSCAMTIME") and local is None:
                        try:
                            local = datetime.strptime(line.split()[1][:14], "%Y%m%d%H%M%S")
                        except (IndexError, ValueError):
                            pass
                    elif line.startswith("$GPRMC") and utc is None:
                        f = line.split(",")
                        if len(f) > 2 and f[2] == "A" and len(f[1]) >= 6:
                            utc = f[1][:6]
                    if local is not None and utc is not None:
                        break
        except OSError:
            continue
        if local is None or utc is None:
            continue
        secs = ((int(utc[:2]) * 3600 + int(utc[2:4]) * 60 + int(utc[4:6]))
                - (local.hour * 3600 + local.minute * 60 + local.second))
        # Fold into (-12h, +12h]: the time-of-day difference wraps at midnight.
        secs = (secs + 43200) % 86400 - 43200
        readings.append(round(secs / _TZ_QUANTUM) * _TZ_QUANTUM)
    if not readings:
        return timedelta(0)
    return timedelta(seconds=sorted(readings)[len(readings) // 2])


class Track:
    """The card's GPS: one time series, and every question asked of it.

    ONE TIME SERIES, NOT A FILE PER CLIP. The camera writes GPS continuously
    and its writer rolls into a new file whenever it decides to, so a filename
    says when a file was opened and nothing about whose fixes are inside it.
    Matching a clip to the file that shares its name was reading a coincidence
    as a promise, and it fails in both directions: a file can hold a replay from
    an earlier drive, and a minute's fixes can sit in a file named for a
    different minute.

    That is why this is an object rather than a pair of directories passed
    around: reading the pool, knowing the camera's UTC offset, cutting a clip's
    window out of it and turning that window into per-second speeds are four
    steps of ONE piece of knowledge, and a caller holding only the directories
    could — and did — do any of them by filename instead.

    Public: files, utc_offset, during, endpoints, speeds, is_parked. `dirs`
    is private: handing the directories back out is exactly how a caller
    re-acquires the ability to resolve a clip's GPS by filename, the mistake
    this class exists to make impossible. `files` stays public against that
    grain because the render's cache key is built from the NMEA listing — a
    track that gained a file must invalidate the key — and it is a read-only
    property, so a caller can read the listing but never install its own.
    """

    #: (pool, utc offset) per directory set, shared by every Track. Building it
    #: reads several hundred NMEA files; gather is called once per clip while
    #: trip boundaries are found, and re-reading the card each time would
    #: dominate the scan. Shared rather than per-instance because the render
    #: builds a Track wherever it needs one and must not decode the card twice.
    _POOL: "dict[tuple, tuple[list, timedelta]]" = {}

    def __init__(self, gps_dirs: "tuple[Path | None, ...]"):
        self._dirs = tuple(gps_dirs)

    def __repr__(self) -> str:
        return "Track(%s)" % ", ".join(str(d) for d in self._dirs)

    @property
    def files(self) -> list[Path]:
        """Every NMEA file under these directories, at any depth.

        rglob, not listdir. The camera moves older files down into a `tmp`
        subdirectory as it rolls, so the top level holds only what it happens to
        be writing now.
        """
        out: list[Path] = []
        for d in self._dirs:
            if d is not None and d.is_dir():
                out.extend(sorted(f for f in d.rglob("*.gpx") if f.is_file()))
        return out

    def _pooled(self) -> "tuple[list, timedelta]":
        """(every fix under these dirs sorted by time, the camera's UTC offset).

        Deduplicated by the second, because the camera writes the same minute
        twice -- once loose and once into the tar archives -- and both are read.
        """
        key = tuple(str(d) for d in self._dirs if d is not None)
        if key not in Track._POOL:
            files = self.files
            seen: set[datetime] = set()
            pool: list[tuple[float, float, float, datetime]] = []
            for f in files:
                for pt in parse_gpx_track(f):
                    if pt[3] not in seen:
                        seen.add(pt[3])
                        pool.append(pt)
            pool.sort(key=lambda pt: pt[3])
            Track._POOL[key] = (pool, _camera_utc_offset(files))
        return Track._POOL[key]

    @property
    def utc_offset(self) -> timedelta:
        """UTC minus camera-local. Zero means no DDPAI header anywhere."""
        return self._pooled()[1]

    def during(self, clips: "list[Clip]") -> list[tuple[float, float, float, datetime]]:
        """The fixes recorded while these clips were being filmed.

        Selected by TIME rather than by filename. A fix belongs to a clip when
        the camera recorded it while that clip was rolling; nothing else is
        evidence of that, and the filename in particular is not.
        """
        if not clips:
            return []
        pool, offset = self._pooled()
        lo = min(c.dt for c in clips) + offset - _SPAN_MARGIN
        hi = max(c.end for c in clips) + offset + _SPAN_MARGIN
        return [pt for pt in pool if lo <= pt[3] < hi]

    def endpoints(self, clip: "Clip"):
        """(first fix, last fix) of a clip as (lat, lon), or (None, None).

        Trip detection asks where the car was when a clip started and stopped,
        and every boundary test is built on these two readings — which is why
        the window they come out of has no margin at all."""
        pts = self.during([clip])
        if not pts:
            return (None, None)
        return ((pts[0][0], pts[0][1]), (pts[-1][0], pts[-1][1]))

    def speeds(self, clip: "Clip") -> list[float]:
        """
        Per-second km/h aligned to the clip's VIDEO timeline (not the GPS-fix
        index). Three real-world wrinkles handled here:

        1) GPS-lock-acquisition lag. Dashcam often starts recording a few
           seconds before GPS reports its first fix. Those leading video
           seconds get a 0 km/h placeholder.
        2) Mid-clip GPS dropouts. GPS can lose lock briefly (tunnel, urban
           canyon, parking ceiling). Affected seconds get the previous-known
           speed forward-filled — collapsing the gap instead would shift every
           subsequent speed earlier.
        3) DDPAI dashcam clock drift. The wall-clock burned into the video can
           be offset from GPS UTC by a non-integer-hour amount, so mod-3600
           lag detection isn't enough. The `$GPSCAMTIME` header at the top of
           the GPX file gives the exact LOCAL↔UTC offset for the device.

        Mishandle any of the three and speeds run ~10+ seconds AHEAD of the
        video — the visible "speed already 14 km/h while the wheels haven't
        moved yet" symptom. Handled, speeds[i] is the GPS reading at the SAME
        video-second the user sees burned-in on the timestamp watermark.
        """
        # The clip's own fixes, selected by time. Resolving the file by name
        # gave this the same wrong answer it gave the track: a clip whose
        # receiver never fixed got a replayed sentence from an earlier drive,
        # and its speeds with it.
        points = self.during([clip])
        if not points:
            return []
        try:
            clip_dt = datetime.strptime(clip.timestamp, "%Y%m%d%H%M%S")
        except ValueError:
            return [p[2] for p in points]

        # The camera's own LOCAL<->UTC offset, read from $GPSCAMTIME across the
        # card rather than from one file's header: the file a clip is named for
        # may have no header at all, and that is exactly the file whose fixes
        # cannot be trusted to be its own. Zero means no DDPAI header anywhere,
        # which is the non-DDPAI card the fallback below is for.
        utc_off = self.utc_offset
        if utc_off:
            offset = -utc_off             # local = utc + offset
            # Place each point at its true clip-second via its UTC timestamp.
            raw: list[float | None] = [None] * clip.duration
            for p in points:
                local_time = p[3] + offset
                clip_sec = int(round((local_time - clip_dt).total_seconds()))
                if 0 <= clip_sec < clip.duration:
                    raw[clip_sec] = p[2]
            # Forward-fill: missing seconds use the previous-known speed (or 0
            # before the first reading). That way a mid-clip GPS dropout shows
            # the last sensed speed rather than collapsing time.
            speeds: list[float] = [0.0] * clip.duration
            last = 0.0
            for i in range(clip.duration):
                if raw[i] is not None:
                    last = raw[i]
                speeds[i] = last
            return speeds

        # Fallback for non-DDPAI files (no $GPSCAMTIME header).
        speeds = [p[2] for p in points]
        gps_dt = points[0][3]
        clip_sih = clip_dt.minute * 60 + clip_dt.second
        gps_sih = gps_dt.minute * 60 + gps_dt.second
        lag = (gps_sih - clip_sih) % 3600
        if 0 < lag <= clip.duration:
            speeds = [0.0] * lag + speeds
        return speeds

    def is_parked(self, clip: "Clip") -> bool:
        """
        Decide whether a clip is stationary. Three signals all count as
        "parked":
          1) GPX exists and >=75% of seconds are below 3 km/h (textbook
             standstill)
          2) GPX exists but holds no valid fixes (indoor parking, lost lock)
          3) No GPX file at all for this clip
        Cases (2) and (3) cover the most common pattern: the dashcam keeps
        recording while parked in a garage but loses GPS. find_parking_runs
        only triggers a skip when the *total* run length is long enough, so
        brief mid-drive GPS dropouts (a few clips through a tunnel) won't trip
        this.
        """
        # By time, not by filename: a clip whose receiver never fixed is named
        # for a file full of an earlier drive's sentences, and judging whether
        # THIS clip was parked from THOSE speeds is judging the wrong minute.
        speeds = [pt[2] for pt in self.during([clip])]
        if not speeds:
            return True
        # Sparse-coverage + fast-speed check: if the GPX file holds far fewer
        # samples than the clip's duration would suggest (1 Hz nominal) AND
        # those samples are all at highway-ish speeds, they're stale parking-
        # buffer data from a previous drive that just happens to be all the
        # GPS info this clip has. Real cars don't go from a parking-mode wake
        # to 80 km/h, so this combination is a reliable "parked, GPS missing"
        # signal. (A clip with SLOW sparse samples — e.g., GPS still acquiring
        # at the start of a drive — falls through to the normal slow-ratio
        # check, which handles both directions correctly.)
        if len(speeds) < clip.duration * 0.2:
            avg = sum(speeds) / len(speeds)
            if avg > 40:
                return True
        slow = sum(1 for s in speeds if s < PARKING_SPEED_THRESHOLD_KMH)
        return (slow / len(speeds)) >= PARKING_CLIP_FRACTION
