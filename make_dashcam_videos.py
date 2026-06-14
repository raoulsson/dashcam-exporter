#!/usr/bin/env python3
"""
make_dashcam_videos.py
----------------------
Turn the raw front/rear clips from your dashcam SD card into one polished
1080p MP4 per drive, with:
  - the front camera as the main view
  - the rear camera as a small picture-in-picture in the top-right corner
  - a burned-in wall-clock timestamp in the bottom-left corner
  - all 60-second clips that belong to the same drive concatenated together

Designed to run on macOS (uses the VideoToolbox hardware H.264 encoder for
speed). Falls back to software libx264 if VideoToolbox isn't available.

USAGE
-----
    python3 make_dashcam_videos.py
        # processes every drive on /Volumes/NO NAME into ~/Desktop/Dashcam_Videos

    python3 make_dashcam_videos.py --drives 10 12 13
        # only drives #10, #12, #13

    python3 make_dashcam_videos.py --root /Volumes/MYCAM --out ~/dashcam
    python3 make_dashcam_videos.py --software         # use libx264 instead of VideoToolbox
    python3 make_dashcam_videos.py --keep-intermediates

REQUIREMENTS
------------
    brew install ffmpeg

The script is restartable: if a drive's final .mp4 already exists in the
output folder, it is skipped. Delete the file to force a re-encode.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_ROOT = "/Volumes/NO NAME"
DEFAULT_OUT  = "~/Desktop/Dashcam_Videos"
DEFAULT_GAP  = 90                              # seconds between clips => new drive
DEFAULT_FONT = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"
FALLBACK_FONT = "/System/Library/Fonts/Menlo.ttc"

# Output video parameters
OUT_W, OUT_H = 1920, 1080                      # 1080p
OUT_FPS      = 30
PIP_W, PIP_H = 662, 372                        # rear inset (was 576x324; +15%)
PIP_MARGIN   = 24
TS_FONT_SIZE = 36
SPEED_FONT_SIZE = 24
SPEED_MARGIN_V  = 24                           # bottom-right corner with small margin
SPEED_MARGIN_R  = 24

# Hardware encoder settings (VideoToolbox uses bitrate, not CRF)
VT_BITRATE   = "8M"
VT_MAXRATE   = "10M"

# Software encoder settings
X264_PRESET  = "veryfast"
X264_CRF     = "23"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FRONT_RE = re.compile(r"^(\d{14})_(\d+)\.mp4$")
REAR_RE  = re.compile(r"^(\d{14})_(\d+)_A\.mp4$")
GPX_RE   = re.compile(r"^(\d{14})_(\d+)_D\.gpx$")

KNOTS_TO_KMH = 1.852


@dataclass
class Clip:
    timestamp: str           # e.g. "20260511121158"
    epoch_utc: int           # filename time treated as UTC -> for drawtext gmtime
    duration: int            # clip duration in seconds (from filename)
    front: Path
    rear: Path

    @property
    def dt(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S")


def find_clips(front_dir: Path, rear_dir: Path) -> list[Clip]:
    front_map: dict[str, tuple[Path, int]] = {}
    for f in sorted(os.listdir(front_dir)):
        m = FRONT_RE.match(f)
        if m:
            front_map[m.group(1)] = (front_dir / f, int(m.group(2)))

    rear_map: dict[str, Path] = {}
    for f in sorted(os.listdir(rear_dir)):
        m = REAR_RE.match(f)
        if m:
            rear_map[m.group(1)] = rear_dir / f

    clips: list[Clip] = []
    for ts in sorted(front_map):
        if ts not in rear_map:
            print(f"  ! no rear pair for {ts}, skipping", file=sys.stderr)
            continue
        path_f, dur = front_map[ts]
        epoch = calendar.timegm(datetime.strptime(ts, "%Y%m%d%H%M%S").timetuple())
        clips.append(Clip(ts, epoch, dur, path_f, rear_map[ts]))
    return clips


def group_into_drives(clips: list[Clip], gap_seconds: int) -> list[list[Clip]]:
    drives: list[list[Clip]] = []
    cur: list[Clip] = []
    for c in clips:
        if cur:
            prev_end = cur[-1].dt + timedelta(seconds=cur[-1].duration)
            if (c.dt - prev_end).total_seconds() > gap_seconds:
                drives.append(cur)
                cur = []
        cur.append(c)
    if cur:
        drives.append(cur)
    return drives


def has_videotoolbox() -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT, text=True,
        )
        return "h264_videotoolbox" in out
    except Exception:
        return False


def has_filter(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-filters"],
            stderr=subprocess.STDOUT, text=True,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == name:
                return True
        return False
    except Exception:
        return False


def _nmea_to_decimal(value: str, hemi: str) -> float | None:
    """Convert NMEA latitude/longitude (ddmm.mmmmm / dddmm.mmmmm) to decimal degrees."""
    try:
        if not value or "." not in value:
            return None
        dot = value.index(".")
        deg = int(value[: dot - 2])
        minutes = float(value[dot - 2 :])
        result = deg + minutes / 60.0
        if hemi in ("S", "W"):
            result = -result
        return result
    except (ValueError, IndexError):
        return None


def parse_gpx_speeds(gpx_path: Path) -> list[float]:
    """Return per-second km/h values parsed from the NMEA $GPRMC lines in a GPX file."""
    return [pt[2] for pt in parse_gpx_track(gpx_path)]


def parse_gpx_track(gpx_path: Path) -> list[tuple[float, float, float, datetime]]:
    """
    Return a list of (lat, lon, kmh, utc_datetime) tuples parsed from $GPRMC lines.
    Skips fixes marked invalid (status != 'A').
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
    return points


def gather_track(clips: list[Clip], gps_dirs: tuple[Path | None, ...]) -> list[tuple[float, float, float, datetime]]:
    """Concatenate all parsed track points for the clips in a group, in clip order."""
    out: list[tuple[float, float, float, datetime]] = []
    for c in clips:
        gpx = find_gpx_for(c.timestamp, *gps_dirs)
        if gpx is not None:
            out.extend(parse_gpx_track(gpx))
    return out


def find_gpx_for(timestamp: str, *dirs: Path) -> Path | None:
    """Match a clip timestamp like '20260511180649' to a GPX in any of the given dirs."""
    for d in dirs:
        if d is None or not d.is_dir():
            continue
        for f in os.listdir(d):
            m = GPX_RE.match(f)
            if m and m.group(1) == timestamp:
                return d / f
            # Some tarred members lack the trailing _D, e.g. 20260506122637_0060.gpx
            m2 = re.match(r"^(\d{14})_\d+\.gpx$", f)
            if m2 and m2.group(1) == timestamp:
                return d / f
    return None


import tarfile  # noqa: E402  (kept near use site for clarity)


def harvest_tarred_gpx(tar_dir: Path, cache_dir: Path) -> tuple[int, int]:
    """
    Extract every *.gpx member from every '*.git' tar archive in tar_dir into cache_dir.
    The dashcam mis-labels these archives with a .git extension but they're standard
    POSIX tar files containing the same NMEA-style .gpx logs.
    Returns (n_archives_processed, n_gpx_extracted).
    """
    if not tar_dir.is_dir():
        return (0, 0)
    cache_dir.mkdir(parents=True, exist_ok=True)
    n_arch = 0
    n_gpx = 0
    for name in sorted(os.listdir(tar_dir)):
        if not name.endswith(".git") or name.startswith("._"):
            continue
        path = tar_dir / name
        try:
            with tarfile.open(path, "r") as tf:
                n_arch += 1
                for member in tf.getmembers():
                    base = os.path.basename(member.name)
                    if not base.endswith(".gpx") or base.startswith("._"):
                        continue
                    dest = cache_dir / base
                    if dest.exists() and dest.stat().st_size == member.size:
                        continue  # already extracted
                    try:
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        dest.write_bytes(f.read())
                        n_gpx += 1
                    except Exception:
                        pass
        except (tarfile.TarError, OSError):
            continue
    return (n_arch, n_gpx)


def write_speed_srt(speeds: list[float], srt_path: Path) -> bool:
    """Write a 1-second-per-cue SRT file with km/h values. Returns False if no speeds."""
    if not speeds:
        return False
    with srt_path.open("w") as fh:
        for i, kmh in enumerate(speeds):
            s, e = i, i + 1
            sh, sm, ss = s // 3600, (s % 3600) // 60, s % 60
            eh, em, es = e // 3600, (e % 3600) // 60, e % 60
            fh.write(f"{i+1}\n")
            fh.write(f"{sh:02d}:{sm:02d}:{ss:02d},000 --> {eh:02d}:{em:02d}:{es:02d},000\n")
            fh.write(f"{kmh:.0f} km/h\n\n")
    return True


# ---------------------------------------------------------------------------
# Map / link outputs (per drive or per day)
# ---------------------------------------------------------------------------

import json
import math

# A "real" driving sample should be within these gaps of the previous one.
# Larger gaps indicate engine-off intervals, tunnels, or signal loss — we don't
# want to draw a straight line across town through buildings.
SEGMENT_GAP_SECONDS = 30
SEGMENT_GAP_METERS  = 200


def _haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dl = math.radians(b_lon - a_lon)
    dp = math.radians(b_lat - a_lat)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def segment_track(points: list[tuple[float, float, float, datetime]]
                  ) -> list[list[tuple[float, float, float, datetime]]]:
    """
    Split the flat list of GPS fixes into contiguous-driving segments. Any
    consecutive pair that is more than SEGMENT_GAP_SECONDS apart in time OR
    more than SEGMENT_GAP_METERS apart in distance starts a new segment.
    """
    if not points:
        return []
    segments: list[list[tuple[float, float, float, datetime]]] = [[points[0]]]
    for prev, cur in zip(points, points[1:]):
        time_gap = (cur[3] - prev[3]).total_seconds()
        dist_m = _haversine_km(prev[0], prev[1], cur[0], cur[1]) * 1000
        if time_gap > SEGMENT_GAP_SECONDS or dist_m > SEGMENT_GAP_METERS:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


def _track_stats(points: list[tuple[float, float, float, datetime]]) -> dict:
    if not points:
        return {"n": 0, "distance_km": 0.0, "max_kmh": 0.0, "avg_kmh": 0.0,
                "duration_min": 0.0, "moving_min": 0.0, "n_segments": 0,
                "start": None, "end": None}
    segs = segment_track(points)
    # Distance: only sum within segments (skips engine-off jumps)
    dist = 0.0
    moving_secs = 0.0
    for seg in segs:
        for i in range(1, len(seg)):
            dist += _haversine_km(seg[i-1][0], seg[i-1][1], seg[i][0], seg[i][1])
        if len(seg) >= 2:
            moving_secs += (seg[-1][3] - seg[0][3]).total_seconds()
    speeds = [p[2] for p in points if p[2] > 0]
    return {
        "n": len(points),
        "n_segments": len(segs),
        "distance_km": dist,
        "max_kmh": max((p[2] for p in points), default=0.0),
        "avg_kmh": (sum(speeds) / len(speeds)) if speeds else 0.0,
        "duration_min": ((points[-1][3] - points[0][3]).total_seconds() / 60.0),
        "moving_min": moving_secs / 60.0,
        "start": points[0],
        "end": points[-1],
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  html,body{{margin:0;padding:0;height:100%;font-family:-apple-system,sans-serif}}
  #wrap{{display:flex;flex-direction:column;height:100%}}
  #title{{padding:10px 16px;background:#222;color:#eee;font-size:14px}}
  #title b{{font-size:16px}}
  #title span{{margin-right:18px;color:#bbb}}
  #map{{flex:1}}
  .legend{{background:#fff;padding:8px;border-radius:4px;box-shadow:0 0 5px rgba(0,0,0,.3);font-size:12px}}
  .legend .row{{display:flex;align-items:center;margin:2px 0}}
  .legend .swatch{{width:18px;height:6px;margin-right:6px}}
</style>
</head>
<body>
<div id="wrap">
  <div id="title"><b>{title}</b> &nbsp; {subtitle}</div>
  <div id="map"></div>
</div>
<script>
// `segments` is an array of segments; each segment is an array of [lat, lon, kmh, "time"] tuples.
// Segments are visually disconnected: GPS gaps (engine off / tunnels) are NOT bridged
// by straight lines across the city.
var segments = {segments_json};
var map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
}}).addTo(map);

var COLORS = ['#6baed6', '#2171b5', '#08519c', '#08306b', '#031432'];
function speedBucket(kmh) {{
  if (kmh < 20) return 0;
  if (kmh < 40) return 1;
  if (kmh < 60) return 2;
  if (kmh < 80) return 3;
  return 4;
}}
function drawRun(latlngs, bucket) {{
  if (latlngs.length < 2) return;
  // White halo underneath for contrast over arterials and beige residentials
  L.polyline(latlngs, {{color: '#ffffff', weight: 8, opacity: 0.85,
                       lineJoin: 'round', lineCap: 'round'}}).addTo(map);
  L.polyline(latlngs, {{color: COLORS[bucket], weight: 5, opacity: 1.0,
                       lineJoin: 'round', lineCap: 'round'}}).addTo(map);
}}

var bounds = L.latLngBounds([]);
for (var s = 0; s < segments.length; s++) {{
  var seg = segments[s];
  if (seg.length < 2) continue;
  // Walk the segment, grouping consecutive points of the same speed bucket
  // into one continuous polyline.
  var runPoints = [[seg[0][0], seg[0][1]]];
  var runBucket = speedBucket(seg[1][2]);
  for (var i = 1; i < seg.length; i++) {{
    var pb = speedBucket(seg[i][2]);
    if (pb === runBucket) {{
      runPoints.push([seg[i][0], seg[i][1]]);
    }} else {{
      drawRun(runPoints, runBucket);
      runPoints = [[seg[i-1][0], seg[i-1][1]], [seg[i][0], seg[i][1]]];
      runBucket = pb;
    }}
    bounds.extend([seg[i][0], seg[i][1]]);
  }}
  drawRun(runPoints, runBucket);
  // Mark the seam between segments with a small grey dot so the gap is obvious
  if (seg.length) {{
    var last = seg[seg.length - 1];
    if (s < segments.length - 1) {{
      L.circleMarker([last[0], last[1]], {{
        radius: 4, color: '#666', fillColor: '#fff', fillOpacity: 1, weight: 1
      }}).addTo(map).bindPopup('Segment break<br>last fix: ' + last[3]);
    }}
  }}
}}
if (segments.length && segments[0].length) {{
  var first = segments[0][0];
  var last  = segments[segments.length - 1][segments[segments.length - 1].length - 1];
  L.marker([first[0], first[1]]).addTo(map).bindPopup('<b>Start</b><br>' + first[3]);
  L.marker([last[0], last[1]]).addTo(map).bindPopup('<b>End</b><br>' + last[3]);
  map.fitBounds(bounds, {{padding: [30, 30]}});
}} else {{
  map.setView([0,0], 2);
}}

var legend = L.control({{position: 'bottomright'}});
legend.onAdd = function() {{
  var div = L.DomUtil.create('div', 'legend');
  div.innerHTML =
    '<div><b>Speed</b></div>' +
    '<div class="row"><div class="swatch" style="background:#6baed6"></div>&lt; 20 km/h</div>' +
    '<div class="row"><div class="swatch" style="background:#2171b5"></div>20–40</div>' +
    '<div class="row"><div class="swatch" style="background:#08519c"></div>40–60</div>' +
    '<div class="row"><div class="swatch" style="background:#08306b"></div>60–80</div>' +
    '<div class="row"><div class="swatch" style="background:#031432"></div>&gt; 80</div>';
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>
"""


def write_html_map(out_path: Path, points: list[tuple[float, float, float, datetime]], title: str) -> None:
    if not points:
        return
    stats = _track_stats(points)
    subtitle = (
        f"<span>{stats['distance_km']:.1f} km driven</span>"
        f"<span>{stats['moving_min']:.0f} min moving</span>"
        f"<span>max {stats['max_kmh']:.0f} km/h</span>"
        f"<span>avg {stats['avg_kmh']:.0f} km/h</span>"
        f"<span>{stats['n_segments']} segments / {stats['n']} fixes</span>"
    )
    segments = segment_track(points)
    js_segments = [
        [[round(lat, 6), round(lon, 6), round(kmh, 1), dt.strftime("%Y-%m-%d %H:%M:%S UTC")]
         for (lat, lon, kmh, dt) in seg]
        for seg in segments
    ]
    html = HTML_TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        segments_json=json.dumps(js_segments, separators=(",", ":")),
    )
    out_path.write_text(html, encoding="utf-8")


def write_gpx_export(out_path: Path, points: list[tuple[float, float, float, datetime]], title: str) -> None:
    if not points:
        return
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="make_dashcam_videos.py" '
             'xmlns="http://www.topografix.com/GPX/1/1">',
             f'  <trk><name>{title}</name>']
    # One <trkseg> per contiguous-driving segment, so consumers like Google Earth
    # and Strava don't bridge engine-off gaps with straight lines.
    for seg in segment_track(points):
        lines.append('    <trkseg>')
        for lat, lon, kmh, dt in seg:
            lines.append(
                f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
                f'<time>{dt.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
                f'<extensions><speed>{kmh / 3.6:.2f}</speed></extensions></trkpt>'
            )
        lines.append('    </trkseg>')
    lines.append('  </trk>')
    lines.append('</gpx>')
    out_path.write_text("\n".join(lines), encoding="utf-8")


# --- Burn-in mini-map (per-second PNG frames composed into a side panel) ----

MAP_PANEL_SIZE = 480           # square panel size in output pixels (480x480)
MAP_BG_COLOR   = (245, 243, 235)
MAP_TRACK_PAD  = 28            # px padding around bounding box of the route


def _speed_color(kmh: float) -> tuple[int, int, int]:
    # Matches the Leaflet COLORS[] blue ramp (darker palette for OSM contrast).
    if kmh < 20:  return (107, 174, 214)   # #6baed6
    if kmh < 40:  return ( 33, 113, 181)   # #2171b5
    if kmh < 60:  return (  8,  81, 156)   # #08519c
    if kmh < 80:  return (  8,  48, 107)   # #08306b
    return (  3,  20,  50)                 # #031432


def _project_track(points: list[tuple[float, float, float, datetime]],
                   size: int, pad: int) -> tuple[list[tuple[int, int]], tuple[float, float, float, float]]:
    """Equirectangular projection of (lat,lon) -> pixel; returns list of (px,py) and the bounding box."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    if min_lat == max_lat: max_lat += 1e-5
    if min_lon == max_lon: max_lon += 1e-5
    # Use cosine-of-mid-latitude to keep aspect ratio reasonable
    mid_lat = (min_lat + max_lat) / 2
    aspect = math.cos(math.radians(mid_lat))
    dlat = max_lat - min_lat
    dlon = (max_lon - min_lon) * aspect
    inner = size - 2 * pad
    scale = inner / max(dlat, dlon)
    # Center the smaller dimension
    w = dlon * scale
    h = dlat * scale
    ox = pad + (inner - w) / 2
    oy = pad + (inner - h) / 2
    px_list = []
    for lat, lon, _, _ in points:
        x = ox + (lon - min_lon) * aspect * scale
        y = oy + (max_lat - lat) * scale          # invert: north up
        px_list.append((int(round(x)), int(round(y))))
    return px_list, (min_lat, max_lat, min_lon, max_lon)


def render_base_route_panel(points: list[tuple[float, float, float, datetime]],
                            size: int = MAP_PANEL_SIZE) -> tuple[object, list[tuple[int, int]]] | None:
    """
    Render the full route as a polyline on a neutral background using PIL.
    Returns (PIL.Image base panel, pixel coords per GPS point) or None if PIL is unavailable.
    Tries to upgrade with OSM tiles via the optional `staticmap` package when available
    (gives geographic context); falls back to plain polyline otherwise.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    if not points:
        return None

    px_list, _bbox = _project_track(points, size, MAP_TRACK_PAD)

    # Try the nicer OSM-tile background first
    # Pre-compute segments and the per-point index → segment_index mapping for
    # the per-segment polyline draws below.
    segments = segment_track(points)
    seg_index_of_point: list[int] = []
    for seg_i, seg in enumerate(segments):
        seg_index_of_point.extend([seg_i] * len(seg))

    img = None
    try:
        from staticmap import StaticMap, Line as SMLine, CircleMarker as SMMarker
        m = StaticMap(size, size, padding_x=MAP_TRACK_PAD, padding_y=MAP_TRACK_PAD,
                      url_template="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")
        # Draw one Line per segment so gaps stay visually broken.
        # Navy chosen to pop against OSM's beige/yellow/orange road palette.
        for seg in segments:
            if len(seg) < 2:
                continue
            coords = [(p[1], p[0]) for p in seg]
            m.add_line(SMLine(coords, "#084594", 5))
        if segments and segments[0]:
            m.add_marker(SMMarker((segments[0][0][1], segments[0][0][0]), "#1a9850", 9))
        if segments and segments[-1]:
            m.add_marker(SMMarker((segments[-1][-1][1], segments[-1][-1][0]), "#2b6cb0", 9))
        img = m.render()
        # Re-project on top of staticmap's projection so the marker lands precisely
        zoom = m._calculate_zoom()
        ext = m._calculate_extent(zoom)
        x0, y0, x1, y1 = ext
        px_list = []
        for lat, lon, _, _ in points:
            xt = m._lon_to_x(lon, zoom)
            yt = m._lat_to_y(lat, zoom)
            px = int(round((xt - x0) / (x1 - x0) * size))
            py = int(round((yt - y0) / (y1 - y0) * size))
            px_list.append((px, py))
    except Exception:
        img = None

    if img is None:
        # Offline fallback: plain background + per-segment colored polylines + start/end dots
        img = Image.new("RGB", (size, size), MAP_BG_COLOR)
        draw = ImageDraw.Draw(img)
        # Soft grid for scale reference
        for g in range(0, size, 40):
            draw.line([(g, 0), (g, size)], fill=(225, 220, 210), width=1)
            draw.line([(0, g), (size, g)], fill=(225, 220, 210), width=1)
        # Draw polylines per segment, never across segment boundaries
        for i in range(1, len(points)):
            if seg_index_of_point[i] != seg_index_of_point[i-1]:
                continue
            draw.line([px_list[i-1], px_list[i]], fill=_speed_color(points[i][2]), width=5)
        # Start dot from first segment, end dot from last segment
        sx, sy = px_list[0]
        ex, ey = px_list[-1]
        draw.ellipse([sx-9, sy-9, sx+9, sy+9], fill=(26, 152, 80), outline=(255, 255, 255), width=2)
        draw.ellipse([ex-9, ey-9, ex+9, ey+9], fill=(43, 108, 178), outline=(255, 255, 255), width=2)

    return img, px_list


def render_clip_marker_video(
    clip: Clip,
    base_panel: object,           # PIL.Image
    drive_points: list[tuple[float, float, float, datetime]],
    drive_pixels: list[tuple[int, int]],
    gps_dirs: tuple[Path | None, ...],
    out_video: Path,
) -> bool:
    """
    For one clip: render N=duration PNG frames (base panel + marker at current position)
    and assemble into a 1-fps MP4. Returns False if the clip has no GPS coverage.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    # Per-clip points
    gpx = find_gpx_for(clip.timestamp, *gps_dirs)
    if gpx is None:
        return False
    clip_points = parse_gpx_track(gpx)
    if not clip_points:
        return False

    # Map each clip second to the nearest pixel on the drive map
    n = clip.duration
    # Build a per-second pixel sequence (clip GPX may have fewer entries than `n`)
    per_second = []
    if len(clip_points) >= n:
        for i in range(n):
            lat = clip_points[i][0]; lon = clip_points[i][1]
            # find this point in drive_points and take its pixel; fallback: nearest
            per_second.append(_nearest_pixel(lat, lon, drive_points, drive_pixels))
    else:
        # Stretch the available points across n seconds
        for i in range(n):
            j = min(int(i * len(clip_points) / n), len(clip_points) - 1)
            lat = clip_points[j][0]; lon = clip_points[j][1]
            per_second.append(_nearest_pixel(lat, lon, drive_points, drive_pixels))

    # Render frames to a temp dir, then ffmpeg the sequence
    work = out_video.with_suffix(".frames")
    work.mkdir(parents=True, exist_ok=True)
    base = base_panel.convert("RGB")
    for i, (px, py) in enumerate(per_second):
        frame = base.copy()
        d = ImageDraw.Draw(frame)
        r = 11
        d.ellipse([px-r, py-r, px+r, py+r], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        d.ellipse([px-6, py-6, px+6, py+6], fill=(214, 39, 40))
        frame.save(work / f"f_{i:04d}.png", "PNG")

    # PNG sequence -> mp4 at 1fps
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", "1",
        "-i", str(work / "f_%04d.png"),
        "-vf", f"fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_video),
    ]
    run_ffmpeg(cmd)

    # Clean up the PNGs (best-effort; tolerate sandboxed / non-removable files)
    for png in work.glob("*.png"):
        try:
            png.unlink()
        except OSError:
            pass
    try:
        work.rmdir()
    except OSError:
        pass
    return True


def _render_static_panel_video(base_panel: object, duration: int, out_video: Path) -> bool:
    """Make a video of just the base panel (no marker) for clips with no GPS data."""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    tmp_png = out_video.with_suffix(".still.png")
    base_panel.convert("RGB").save(tmp_png, "PNG")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-loop", "1", "-framerate", "1", "-t", str(duration),
        "-i", str(tmp_png),
        "-vf", "fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_video),
    ]
    run_ffmpeg(cmd)
    tmp_png.unlink(missing_ok=True)
    return True


def _nearest_pixel(lat: float, lon: float,
                   drive_points: list[tuple[float, float, float, datetime]],
                   drive_pixels: list[tuple[int, int]]) -> tuple[int, int]:
    best_i = 0
    best_d = float("inf")
    for i, (dlat, dlon, _, _) in enumerate(drive_points):
        d = (lat - dlat) ** 2 + (lon - dlon) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return drive_pixels[best_i]


def write_links_sidecar(out_path: Path, points: list[tuple[float, float, float, datetime]], title: str) -> None:
    if not points:
        return
    # Pick start, end, and up to 8 evenly spaced waypoints for a /maps/dir/ URL
    if len(points) <= 10:
        waypoints = points
    else:
        step = (len(points) - 1) / 9.0
        waypoints = [points[int(round(i * step))] for i in range(10)]
    coords = [f"{p[0]:.6f},{p[1]:.6f}" for p in waypoints]
    dir_url   = "https://www.google.com/maps/dir/" + "/".join(coords)
    start_url = f"https://www.google.com/maps?q={points[0][0]:.6f},{points[0][1]:.6f}"
    end_url   = f"https://www.google.com/maps?q={points[-1][0]:.6f},{points[-1][1]:.6f}"
    apple_url = f"https://maps.apple.com/?ll={points[0][0]:.6f},{points[0][1]:.6f}"
    stats = _track_stats(points)
    body = (
        f"{title}\n"
        f"{'=' * len(title)}\n\n"
        f"Distance: {stats['distance_km']:.2f} km\n"
        f"Duration: {stats['duration_min']:.1f} minutes\n"
        f"Max speed: {stats['max_kmh']:.1f} km/h\n"
        f"Avg moving speed: {stats['avg_kmh']:.1f} km/h\n"
        f"GPS fixes: {stats['n']}\n\n"
        f"Open in Google Maps (start):\n  {start_url}\n\n"
        f"Open in Google Maps (end):\n  {end_url}\n\n"
        f"Open in Apple Maps (start):\n  {apple_url}\n\n"
        f"Google Maps directions across waypoints (limited to ~10 stops):\n  {dir_url}\n\n"
        f"Tip: open the .html sidecar for the full interactive route, or\n"
        f"     open the .gpx sidecar in Google Earth, Strava, Maps.me, Komoot, etc.\n"
    )
    out_path.write_text(body, encoding="utf-8")


def has_drawtext() -> bool:
    return has_filter("drawtext")


def has_subtitles() -> bool:
    return has_filter("subtitles")


def resolve_font() -> str:
    for f in (DEFAULT_FONT, FALLBACK_FONT):
        if Path(f).exists():
            return f
    # Last-ditch: let ffmpeg find a font by family name (drawtext supports `font=`)
    return DEFAULT_FONT


def fmt_secs(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}h{m:02d}m{sec:02d}s" if h else f"{m:d}m{sec:02d}s"


# ---------------------------------------------------------------------------
# Per-clip encode
# ---------------------------------------------------------------------------

def build_filter_complex(
    font_path: str,
    start_epoch: int,
    with_timestamp: bool,
    speed_srt: Path | None,
    with_map_widget: bool = False,
) -> str:
    """
    Front 2560x1600 -> crop to 16:9 (lose 80 px top/bottom) -> scale 1920x1080
    Rear  -> scale to 576x324 with a thin white border
    Overlay rear at bottom-center with a 24 px margin (covers the bonnet area)
    Optionally:
      - burn 'YYYY-MM-DD HH:MM:SS' in bottom-left
      - render a per-second 'NN km/h' subtitle on the left, above the timestamp
    """
    base = (
        f"[0:v]crop=2560:1440:0:80,scale={OUT_W}:{OUT_H},setsar=1,fps={OUT_FPS}[front];"
        f"[1:v]scale={PIP_W}:{PIP_H},setsar=1,fps={OUT_FPS},"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.9:t=3[rear];"
        f"[front][rear]overlay=(W-w)/2:H-h-{PIP_MARGIN}"
    )

    chain = base
    last_label = ""  # currently the output of `base` is unnamed

    if with_timestamp:
        font_escaped = font_path.replace(":", r"\:")
        chain += (
            f",drawtext=fontfile={font_escaped}:"
            f"text='%{{pts\\:gmtime\\:{start_epoch}\\:%Y-%m-%d %T}}':"
            f"fontcolor=white:fontsize={TS_FONT_SIZE}:"
            f"box=1:boxcolor=black@0.55:boxborderw=10:"
            f"x=24:y=h-th-24"
        )

    if speed_srt is not None:
        # libass force_style: bottom-right speed readout
        style = (
            f"Alignment=3,FontName=Courier New,FontSize={SPEED_FONT_SIZE},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
            "BackColour=&H80000000,BorderStyle=4,Outline=2,Shadow=0,"
            f"MarginV={SPEED_MARGIN_V},MarginR={SPEED_MARGIN_R}"
        )
        # Single-quote the path so colons inside it don't get parsed as option separators
        chain += f",subtitles=filename='{speed_srt.as_posix()}':force_style='{style}'"

    if with_map_widget:
        # Tag the main composed video, then build a side panel from input [2:v] and hstack
        chain += "[video_part];"
        chain += (
            f"[2:v]scale={MAP_PANEL_SIZE}:{MAP_PANEL_SIZE},setsar=1,fps={OUT_FPS},"
            f"pad={MAP_PANEL_SIZE}:{OUT_H}:0:(oh-ih)/2:color=black[map_part];"
        )
        chain += "[video_part][map_part]hstack[out]"
        return chain
    return chain + "[out]"


_NOISY_FFMPEG_PATTERNS = (
    # The DDPAI custom telemetry track triggers this on every clip; the stream
    # is auto-discarded anyway, the message is purely informational.
    "have zero duration",
    "stream set to be discarded by default",
)


def run_ffmpeg(cmd: list[str]) -> None:
    """
    Run an ffmpeg command, streaming stderr through a line filter that drops
    known harmless DDPAI-metadata noise. Real warnings still pass through.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stderr is not None
    for line in proc.stderr:
        if any(p in line for p in _NOISY_FFMPEG_PATTERNS):
            continue
        sys.stderr.write(line)
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def encode_clip(
    clip: Clip,
    out_path: Path,
    font_path: str,
    use_vt: bool,
    with_timestamp: bool,
    gps_dirs: tuple[Path | None, ...],
    with_speed: bool,
    map_video: Path | None = None,
) -> None:
    # If GPS data exists for this clip, write a sidecar SRT and pass it to the filter
    speed_srt: Path | None = None
    if with_speed:
        gpx = find_gpx_for(clip.timestamp, *gps_dirs)
        if gpx is not None:
            speeds = parse_gpx_speeds(gpx)
            srt_path = out_path.with_suffix(".speed.srt")
            if write_speed_srt(speeds, srt_path):
                speed_srt = srt_path

    with_map_widget = map_video is not None
    filt = build_filter_complex(font_path, clip.epoch_utc, with_timestamp, speed_srt, with_map_widget)
    if use_vt:
        venc = [
            "-c:v", "h264_videotoolbox",
            "-b:v", VT_BITRATE,
            "-maxrate", VT_MAXRATE,
            "-profile:v", "high",
        ]
    else:
        venc = [
            "-c:v", "libx264",
            "-preset", X264_PRESET,
            "-crf", X264_CRF,
        ]

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(clip.front),
        "-i", str(clip.rear),
    ]
    if with_map_widget:
        cmd += ["-i", str(map_video)]
    cmd += [
        "-filter_complex", filt,
        "-map", "[out]", "-map", "0:a?",
        *venc,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k",
        str(out_path),
    ]
    run_ffmpeg(cmd)


def concat_clips(intermediate_paths: list[Path], out_path: Path) -> None:
    list_file = out_path.with_suffix(".concat.txt")
    with list_file.open("w") as f:
        for p in intermediate_paths:
            f.write(f"file '{p.as_posix()}'\n")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(cmd)
    list_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root",  default=DEFAULT_ROOT, help=f"Dashcam volume root (default: {DEFAULT_ROOT})")
    ap.add_argument("--out",   default=DEFAULT_OUT,  help=f"Output folder (default: {DEFAULT_OUT})")
    ap.add_argument("--gap",   type=int, default=DEFAULT_GAP, help="Seconds between clips to consider a new drive")
    ap.add_argument("--drives", nargs="+", type=int, help="Only process specific drive numbers (1-based)")
    ap.add_argument("--software", action="store_true", help="Use libx264 instead of VideoToolbox")
    ap.add_argument("--keep-intermediates", action="store_true", help="Keep per-clip processed files")
    ap.add_argument("--dry-run", action="store_true", help="List drives and exit without encoding")
    ap.add_argument("--no-timestamp", action="store_true",
                    help="Skip the burned-in date/time overlay (use if your ffmpeg lacks the drawtext filter)")
    ap.add_argument("--no-speed", action="store_true",
                    help="Skip the GPS speed overlay even when GPX data is available")
    ap.add_argument("--daily", action="store_true",
                    help="Group clips by calendar date instead of by gap, producing one MP4 per day")
    ap.add_argument("--no-map-sidecars", action="store_true",
                    help="Skip the per-group .html / .gpx / _links.txt map sidecars")
    ap.add_argument("--no-map-widget", action="store_true",
                    help="Skip the burned-in mini-map panel on the right of the video frame")
    ap.add_argument("--sidecars-only", action="store_true",
                    help="Only (re-)generate the .html / .gpx / _links.txt sidecars, skip video encoding")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    front_dir = root / "DCIM" / "200video" / "front"
    rear_dir  = root / "DCIM" / "200video" / "rear"
    gps_dir   = root / "DCIM" / "203gps"
    tar_dir   = gps_dir / "tar"
    if not front_dir.is_dir() or not rear_dir.is_dir():
        print(f"ERROR: expected {front_dir} and {rear_dir}", file=sys.stderr)
        return 1
    gps_dir = gps_dir if gps_dir.is_dir() else None
    tar_dir = tar_dir if (tar_dir and tar_dir.is_dir()) else None

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install with:  brew install ffmpeg", file=sys.stderr)
        return 1

    use_vt = not args.software and has_videotoolbox()
    encoder_name = "h264_videotoolbox (hardware)" if use_vt else "libx264 (software)"

    # Decide whether we can / should burn in the timestamp
    with_timestamp = not args.no_timestamp
    if with_timestamp and not has_drawtext():
        print(
            "ERROR: your ffmpeg build does not include the 'drawtext' filter "
            "(libfreetype is missing), so the timestamp overlay can't be drawn.\n"
            "\n"
            "Fix it one of two ways:\n"
            "  1) Install the full ffmpeg build (keeps timestamps):\n"
            "         brew install ffmpeg-full\n"
            "         brew unlink ffmpeg && brew link --overwrite ffmpeg-full\n"
            "     ...then re-run this script.\n"
            "  2) Or skip the timestamp overlay and re-run with --no-timestamp.",
            file=sys.stderr,
        )
        return 1

    font_path = resolve_font() if with_timestamp else ""

    # Decide whether we can / should burn in the GPS speed
    with_speed = not args.no_speed and (gps_dir is not None or tar_dir is not None)
    if with_speed and not has_subtitles():
        print("WARNING: ffmpeg lacks the 'subtitles' filter (libass missing); speed overlay disabled.",
              file=sys.stderr)
        with_speed = False

    # Harvest GPX from tarred archives into a cache (one-time per run)
    tar_cache_dir: Path | None = None
    if with_speed and tar_dir is not None:
        tar_cache_dir = out_dir / ".gpx_cache"
        n_arch, n_new = harvest_tarred_gpx(tar_dir, tar_cache_dir)
        if n_arch:
            print(f"Tarred GPS: extracted {n_new} new .gpx files from {n_arch} archives "
                  f"into {tar_cache_dir}")

    gps_dirs = (gps_dir, tar_cache_dir)

    n_gpx_loose = sum(1 for f in os.listdir(gps_dir) if GPX_RE.match(f)) if gps_dir else 0
    n_gpx_tar   = sum(1 for f in os.listdir(tar_cache_dir) if f.endswith(".gpx")) if tar_cache_dir else 0

    print(f"Encoder:   {encoder_name}")
    print(f"Timestamp: {'on (' + font_path + ')' if with_timestamp else 'off'}")
    if with_speed:
        print(f"Speed:     on ({n_gpx_loose} loose .gpx + {n_gpx_tar} from tar archives)")
    elif gps_dir is None and tar_dir is None:
        print(f"Speed:     off (no DCIM/203gps folder)")
    elif args.no_speed:
        print(f"Speed:     off (--no-speed)")
    else:
        print(f"Speed:     off")
    print(f"Grouping:  {'by day (--daily)' if args.daily else 'by drive (gap-based)'}")
    print(f"Output:    {out_dir}")
    print(f"Scanning:  {front_dir}")

    clips = find_clips(front_dir, rear_dir)

    # Build groups depending on --daily
    if args.daily:
        by_date: dict[str, list[Clip]] = {}
        for c in clips:
            by_date.setdefault(c.timestamp[:8], []).append(c)
        groups = [by_date[k] for k in sorted(by_date)]
        group_kind, group_word = "day", "Day"
    else:
        groups = group_into_drives(clips, args.gap)
        group_kind, group_word = "drive", "Drive"

    print(f"\nFound {len(clips)} clip pairs grouped into {len(groups)} {group_kind}s:")
    total_secs = 0
    for i, g in enumerate(groups, 1):
        start = g[0].dt
        end   = g[-1].dt + timedelta(seconds=g[-1].duration)
        secs  = (end - start).total_seconds()
        total_secs += secs
        print(f"  {group_word} {i:2d}  {start:%Y-%m-%d %H:%M}  -> {end:%H:%M}   "
              f"{len(g):3d} clips  ~{fmt_secs(secs)}")
    print(f"\nTotal: ~{fmt_secs(total_secs)} of footage")

    if args.dry_run:
        return 0

    wanted = set(args.drives) if args.drives else set(range(1, len(groups) + 1))

    work_dir = out_dir / ".intermediates"
    work_dir.mkdir(exist_ok=True)

    for idx, group in enumerate(groups, 1):
        if idx not in wanted:
            continue

        start = group[0].dt
        end   = group[-1].dt + timedelta(seconds=group[-1].duration)
        secs  = (end - start).total_seconds()
        if args.daily:
            label = start.strftime("%Y-%m-%d")
            final = out_dir / f"day_{label}.mp4"
        else:
            label = start.strftime("%Y-%m-%d_%H-%M")
            final = out_dir / f"drive_{idx:02d}_{label}.mp4"

        print(f"\n[{group_word} {idx}/{len(groups)}] {start:%Y-%m-%d %H:%M} → {end:%H:%M}  "
              f"({len(group)} clips, ~{fmt_secs(secs)})")

        # Emit map sidecars (HTML / GPX / links.txt) using whatever GPS data is available.
        # Done unconditionally — even when the final .mp4 already exists — so the
        # user can refresh the sidecars after segmentation/render fixes without
        # re-encoding 1.9 hours of video.
        group_track = gather_track(group, gps_dirs) if with_speed else []
        if not args.no_map_sidecars and group_track:
            title = (f"Drive {idx} — {start:%Y-%m-%d %H:%M}" if not args.daily
                     else f"Day — {start:%Y-%m-%d}")
            html_path  = final.with_suffix(".html")
            gpx_path   = final.with_suffix(".gpx")
            links_path = final.with_name(final.stem + "_links.txt")
            write_html_map(html_path, group_track, title)
            write_gpx_export(gpx_path, group_track, title)
            write_links_sidecar(links_path, group_track, title)
            stats = _track_stats(group_track)
            print(f"  map: {stats['distance_km']:.1f} km in {stats['n_segments']} segments, "
                  f"{stats['n']} fixes → {html_path.name}, {gpx_path.name}, {links_path.name}")
        elif not args.no_map_sidecars:
            print(f"  map: (no GPS data for this {group_kind})")

        if args.sidecars_only:
            continue

        if final.exists():
            print(f"  video: {final.name} already exists — skipping (delete to re-encode)")
            continue

        # Pre-render the burn-in base panel (one per group)
        base_panel = None
        group_pixels: list[tuple[int, int]] = []
        if not args.no_map_widget and group_track:
            rendered = render_base_route_panel(group_track)
            if rendered is None:
                print("  ! map widget skipped: PIL/Pillow not installed."
                      " Run: pip3 install -r requirements.txt")
            else:
                base_panel, group_pixels = rendered

        intermediates: list[Path] = []
        for ci, clip in enumerate(group, 1):
            inter = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_{clip.timestamp}.mp4"

            # Per-clip map widget video (1fps marker animation on the base panel)
            map_video: Path | None = None
            if base_panel is not None:
                map_video = inter.with_suffix(".map.mp4")
                if not map_video.exists():
                    ok = render_clip_marker_video(
                        clip, base_panel, group_track, group_pixels, gps_dirs, map_video
                    )
                    if not ok:
                        # Render base-only panel so all clips share the same output size
                        ok = _render_static_panel_video(base_panel, clip.duration, map_video)
                        if not ok:
                            map_video = None

            if not inter.exists():
                print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}  encoding ...")
                encode_clip(clip, inter, font_path, use_vt, with_timestamp,
                            gps_dirs, with_speed, map_video=map_video)
            else:
                print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}  (cached)")
            intermediates.append(inter)

        print(f"  concatenating {len(intermediates)} clips -> {final.name}")
        concat_clips(intermediates, final)
        print(f"  ✓ {final}")

        if not args.keep_intermediates:
            for p in intermediates:
                p.unlink(missing_ok=True)
                p.with_suffix(".speed.srt").unlink(missing_ok=True)
                p.with_suffix(".map.mp4").unlink(missing_ok=True)

    # Tidy up empty intermediate dir
    if not args.keep_intermediates:
        try:
            next(work_dir.iterdir())
        except StopIteration:
            work_dir.rmdir()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
