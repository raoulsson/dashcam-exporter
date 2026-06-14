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
# Where the rear PiP sits inside the main video frame.
# Choose from: bottom-middle (default), top-left, top-middle, top-right.
# (The bottom-left/right corners are reserved for the timestamp + speed +
# watermark overlays.)
REAR_PIP_POSITION = "bottom-middle"
REAR_PIP_ENABLED  = True   # auto-disabled when no rear/ folder is present
TS_FONT_SIZE = 36
SPEED_FONT_SIZE = 24
# Speed sits ABOVE the watermark in the bottom-right corner, so this margin
# also needs to leave room for the watermark below it.
SPEED_MARGIN_V  = 32                           # px from bottom edge
SPEED_MARGIN_R  = 12                           # px from right edge

# Hardware encoder settings (VideoToolbox uses bitrate, not CRF)
VT_BITRATE   = "8M"
VT_MAXRATE   = "10M"

# Software encoder settings
X264_PRESET  = "veryfast"
X264_CRF     = "23"

# Default config.txt template, dumped by `--write-config PATH`
CONFIG_TEMPLATE = """# dashcam-exporter — config.txt
#
# Every setting here is OPTIONAL. Uncomment the lines you want to change.
# Precedence: command-line flag  >  this file  >  built-in default.
# Booleans accept: true / false / yes / no / 1 / 0.
#
# Pass --config /path/to/this.txt to use a non-default location, or run
#   python3 make_dashcam_videos.py --write-config ./config.txt
# to regenerate this template anytime.


# ============================================================================
# INPUT / OUTPUT
# ============================================================================

# Where the dashcam SD card (or a local copy of it) lives. The script expects
# DCIM/200video/{front,rear} and (optionally) DCIM/203gps inside this folder.
# When the SD card is in the car, point this at a local backup directory
# you've copied the DCIM tree into.
#root = /Volumes/NO NAME

# Where the rendered videos and sidecars get written.
#out = ~/Desktop/Dashcam_Videos


# ============================================================================
# GROUPING
# ============================================================================

# false (default): each gap-separated drive becomes its own .mp4
# true:            all clips on the same calendar date go into one .mp4
#daily = false

# In drive-mode, clips farther than this many seconds apart start a new drive.
#gap = 90


# ============================================================================
# OVERLAYS
# ============================================================================

# Burn date/time into the bottom-left of the main video frame.
#timestamp = true

# Burn GPS speed into the bottom-right corner of the main video frame.
#speed = true
# Display unit on the overlay + stats panel + HTML legend + links.txt.
#   kmh  -> "NN km/h"
#   mph  -> "NN mph"  (with distance shown in miles, max/avg in mph)
# GPX export always stays in m/s per the spec.
#speed_unit = kmh
#speed_font_size = 24
# Distance from the bottom edge / right edge in pixels. Lower = closer to corner.
# Default 32 leaves room for the watermark BELOW the speed in the same corner.
#speed_margin_v = 32
#speed_margin_r = 12

# Render the per-day side panel (stats + map widget with moving marker).
#map_widget = true

# Burn the stats block (Day title, Distance, Driven, Max speed, Avg, segments
# / GPS points) into the top of the side panel. When false, the panel just
# shows the map widget centred vertically.
#panel_stats = true

# Save .html (Leaflet), .gpx (standard GPX), and _links.txt next to each video.
#map_sidecars = true

# Small watermark on the main video.
# Leave watermark_text empty to disable. Position one of:
#   bottom-right (default), bottom-left, top-right, top-left
# Bottom-right sits BELOW the speed readout in the same corner.
# NOTE: font_size is in literal pixels (unlike the speed overlay, which is
# libass-scaled ~3.75x at 1080p). On 1080p: 16 is barely visible, 28 is
# comfortably readable, 36-40 is prominent.
#watermark_text = https://github.com/raoulsson/dashcam-exporter
#watermark_font_size = 28
#watermark_position = bottom-right
# Distance from the closest horizontal / vertical edge in pixels.
#watermark_margin_h = 8
#watermark_margin_v = 6


# ============================================================================
# AUDIO
# ============================================================================

# audio=false strips audio from the output. Useful when passenger conversation
# is on the recording and you don't want it shared.
#audio = true


# ============================================================================
# FRONT CAMERA CROP
# ============================================================================

# Source clips are 2560x1600. We crop pixels off the top and bottom before
# scaling to 1080p so the bonnet doesn't dominate. If you mount your dashcam
# higher or lower, tune these. (Effective height: 1600 - top - bottom)
#front_crop_top    = 80
#front_crop_bottom = 80


# ============================================================================
# REAR PiP (picture-in-picture)
# ============================================================================

# Set to false if your dashcam has no rear camera (also auto-disabled when the
# DCIM/200video/rear folder is missing or empty).
#rear_pip = true

# Where the PiP sits inside the main video frame:
#   bottom-middle (default), top-left, top-middle, top-right
# (the bottom-left/right corners are reserved for timestamp + speed/watermark
# so those positions aren't offered)
#rear_pip_position = bottom-middle

# Set both, or set just one and the other auto-computes from the rear
# camera's 16:9 native aspect (so you don't accidentally squash the picture).
#rear_pip_w      = 662
#rear_pip_h      = 372
#rear_pip_margin = 24


# ============================================================================
# MAP WIDGET PANEL
# ============================================================================

# Side panel width in pixels. The map itself is square at this width.
#map_panel_w = 480

# Where the panel sits relative to the main video: 'right' (default) or 'left'.
#map_panel_position = right

# Black gutter between the main video and the panel.
#map_panel_gutter_px = 2


# ============================================================================
# PARKING SKIP — drop long standstills, replace with a 'Fast forwarding…' slide
# ============================================================================

# Default true: when the car is parked for a long time AND the dashcam keeps
# recording, the script keeps 10s at each end and slides through the middle.
#skip_parking = true

# Minimum length (s) of a parked run before we trigger the skip.
# 300 = 5 minutes. Shorter values are more aggressive.
#parking_min_secs = 300

# How long the entry slice is (BEFORE the Fast-forwarding slide).
# This is the bit where you see yourself parking / getting out.
#parking_entry_pad = 5

# How much pre-drive padding to keep AFTER the Fast-forwarding slide.
# Larger = you see more "about to drive" before the actual drive-away.
#parking_exit_pad = 10

# Legacy combined knob — kept for back-compat. If parking_entry_pad /
# parking_exit_pad above aren't set, this value is used for both.
#parking_pad_secs = 5

# Exit-clip handling after a parking gap.
#
# Drive-resume detection looks for N consecutive seconds of GPS motion
# (>5 km/h, see drive_resume_sustain_secs). When found, the exit slice
# anchors `parking_exit_pad` seconds before that moment.
#
# Most parking-mode dashcams produce GPS that's too scrambled to detect
# reliably (random motion-triggered "passing car / pedestrian" wakeups that
# get flushed into the first new clip), so the script falls through to the
# value below and seeks this many seconds into the exit clip. Set to 0 to
# play exit clips from second 0; 45 is the default and tends to land within
# ~10 seconds of real drive-away on a 60-second clip.
#exit_skip_secs = 45

# How long a moving GPS block must be to count as "real drive" rather than
# parking-mode jitter. Higher = stricter (more often falls through to
# exit_skip_secs). Try 60 if 30 still fires on passing traffic.
#drive_resume_sustain_secs = 30

# Inter-clip gap detection: insert a 'Fast forwarding…' slide whenever the
# wall-clock distance between consecutive clips is longer than this. Catches
# engine-off intervals that AREN'T preceded by parked footage, so the
# parking-run detector doesn't fire for them. Default 60 (1 minute).
#inter_clip_gap_secs = 60

# Auto-skip groups that have fewer than this many clips. These are typically
# loop-recording fragments left over after the SD card rolled around — a
# minute or two of footage from a session whose first portion has been
# overwritten. To force-encode a fragment, pass its index via --drives (the
# min-clips check is bypassed when --drives is explicit).
#min_clips_per_group = 4


# ============================================================================
# OUTPUT SIZE / QUALITY
# ============================================================================

# Optional final downscale of the composite output for web/mobile delivery.
# 0 = keep native (1080p + map panel = 2400x1080).
# 720 gives 720p high. 540 gives 540p mobile-friendly. Aspect ratio is preserved.
#output_height = 0

# Encoder selection.
# software = true forces libx264 even if VideoToolbox (Mac hardware) is available.
#software = false

# Keep the per-clip intermediate .mp4 files after concat.
#keep_intermediates = false

# Hardware H.264 (VideoToolbox) bitrates.
#vt_bitrate = 8M
#vt_maxrate = 10M

# Software H.264 (libx264) tuning.
#x264_preset = veryfast
#x264_crf    = 23
"""

# Parking detection / "Fast forwarding..." transition defaults
PARKING_SPEED_THRESHOLD_KMH = 3.0    # below this we consider the car stationary
PARKING_CLIP_FRACTION       = 0.75   # fraction of seconds-in-clip below threshold
DEFAULT_PARKING_MIN_SECS    = 300    # minimum run length (s) before we skip (5 min)
DEFAULT_PARKING_PAD_SECS    = 5      # legacy alias — kept for back-compat
DEFAULT_PARKING_ENTRY_PAD   = 5      # entry slice length (before the FF)
DEFAULT_PARKING_EXIT_PAD    = 10     # how many seconds of footage precede drive-resume after the FF
# Standard exit-slice skip after a parking gap. The exit slice trims this
# many seconds off the head of the first clip. Drive-resume detection
# refines it when GPS clearly shows >=30s of continuous motion; otherwise
# this value is used as-is. Most parking-mode dashcams produce GPS data
# that's too scrambled to detect reliably (random motion-triggered bursts
# of "passing car / walking pedestrian" wakeups), so this is the value
# you'll see most of the time.
DEFAULT_EXIT_SKIP_SECS      = 45
# Minimum time gap (in wall-clock seconds) between consecutive clips before
# a "Fast forwarding…" slide is auto-inserted between them. This catches
# engine-off intervals that DIDN'T leave parked-but-recording footage on
# either side (so the parking-run detector doesn't fire for them).
DEFAULT_INTER_CLIP_GAP_SECS = 60
# Minimum number of clips a group must have before it gets auto-encoded.
# Smaller groups are typically loop-recording fragments left over after the
# SD card rolled around. The user can still force-encode them by naming the
# group index explicitly via --drives.
DEFAULT_MIN_CLIPS_PER_GROUP = 4
TRANSITION_SECS             = 2      # length of the "Fast forwarding..." slide
TRANSITION_TEXT             = "Fast forwarding..."
TRANSITION_FONT_SIZE        = 72

# Right-side stats panel + copyright watermark
PANEL_STATS_TOP_PX = 30      # px from top of right panel to start drawing stats
PANEL_MAP_TOP_PX   = 340     # y offset of the map block within the 480x1080 right panel
COPYRIGHT_TEXT     = "(c) Raoul Marc Schmidiger"
COPYRIGHT_FONT_SIZE = 28
# Where the watermark sits inside the main video frame:
#   bottom-right (default), bottom-left, top-right, top-left
COPYRIGHT_POSITION = "bottom-right"
# Distance from the edges. Smaller = tighter to the corner.
COPYRIGHT_MARGIN_H = 8     # px from left/right edge depending on position
COPYRIGHT_MARGIN_V = 6     # px from top/bottom edge depending on position

# Front camera default crop (top + bottom rows removed before scale to 1080p).
# Different dashcam mounts show more / less of the bonnet — tune in config.txt.
FRONT_CROP_TOP    = 80
FRONT_CROP_BOTTOM = 80
FRONT_W           = 2560
FRONT_H           = 1600

# Side of the main video the map panel is hstacked on: "right" (default) or "left".
MAP_PANEL_POSITION = "right"
MAP_PANEL_GUTTER_PX = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FRONT_RE = re.compile(r"^(\d{14})_(\d+)\.mp4$")
REAR_RE  = re.compile(r"^(\d{14})_(\d+)_A\.mp4$")
GPX_RE   = re.compile(r"^(\d{14})_(\d+)_D\.gpx$")

KNOTS_TO_KMH = 1.852
KMH_TO_MPH   = 0.621371

# Speed unit displayed on the speed overlay, stats panel, HTML legend, and
# links sidecar. GPX export always stays in m/s (the spec).
SPEED_UNIT = "kmh"             # "kmh" or "mph"


def _kmh_to_display(kmh: float) -> float:
    return kmh * KMH_TO_MPH if SPEED_UNIT == "mph" else kmh


def _speed_unit_label() -> str:
    return "mph" if SPEED_UNIT == "mph" else "km/h"


@dataclass
class Clip:
    timestamp: str           # e.g. "20260511121158"
    epoch_utc: int           # filename time treated as UTC -> for drawtext gmtime
    duration: int            # clip duration in seconds (from filename)
    front: Path
    rear: Path | None        # None if the dashcam has no rear camera

    @property
    def dt(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S")


def find_clips(front_dir: Path, rear_dir: Path | None) -> list[Clip]:
    front_map: dict[str, tuple[Path, int]] = {}
    for f in sorted(os.listdir(front_dir)):
        m = FRONT_RE.match(f)
        if m:
            front_map[m.group(1)] = (front_dir / f, int(m.group(2)))

    rear_map: dict[str, Path] = {}
    if rear_dir is not None and rear_dir.is_dir():
        for f in sorted(os.listdir(rear_dir)):
            m = REAR_RE.match(f)
            if m:
                rear_map[m.group(1)] = rear_dir / f

    clips: list[Clip] = []
    for ts in sorted(front_map):
        path_f, dur = front_map[ts]
        epoch = calendar.timegm(datetime.strptime(ts, "%Y%m%d%H%M%S").timetuple())
        rear_path: Path | None = rear_map.get(ts)
        if REAR_PIP_ENABLED and rear_dir is not None and rear_path is None:
            # Rear cam expected but this clip is missing its rear pair.
            print(f"  ! no rear pair for {ts}, skipping", file=sys.stderr)
            continue
        clips.append(Clip(ts, epoch, dur, path_f, rear_path))
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


def file_has_audio(path: Path) -> bool:
    """True if the file has at least one audio stream (via ffprobe)."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            text=True, stderr=subprocess.STDOUT,
        )
        return bool(out.strip())
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


DRIVE_RESUME_THRESHOLD_KMH = 5.0   # higher than parking threshold to reject GPS jitter
DRIVE_RESUME_SUSTAIN_SECS  = 30    # require N consecutive moving samples = real drive

def find_drive_resume_second(
    clip: Clip, gps_dirs: tuple[Path | None, ...],
    sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
    threshold_kmh: float = DRIVE_RESUME_THRESHOLD_KMH,
) -> int | None:
    """
    Best-effort detection of when the car actually starts moving in this clip.
    Returns the clip-second at which a sustained 30-second-long moving window
    begins, or None if no such window exists (in which case the GPS data is
    too noisy / scrambled to trust — caller falls back to a configurable
    skip).

    The 30-second sustain is intentional: parking-mode dashcams record short
    bursts of motion-triggered video around a parked car (a passing car, a
    pedestrian, dashcam reboot self-tests) which produce brief GPS spikes
    that don't represent real driving. 30 seconds of continuous motion is a
    solid indicator that the drive has actually started.
    """
    gpx = find_gpx_for(clip.timestamp, *gps_dirs)
    if gpx is None:
        return None
    speeds = parse_gpx_speeds(gpx)
    if not speeds:
        return None
    if len(speeds) < sustain_secs:
        return None
    for i in range(len(speeds) - sustain_secs + 1):
        if all(speeds[i + j] > threshold_kmh for j in range(sustain_secs)):
            return i
    return None


def clip_is_parked(clip: Clip, gps_dirs: tuple[Path | None, ...]) -> bool:
    """
    Decide whether a clip is stationary. Three signals all count as "parked":
      1) GPX exists and >=75% of seconds are below 3 km/h (textbook standstill)
      2) GPX exists but holds no valid fixes (indoor parking, lost lock)
      3) No GPX file at all for this clip
    Cases (2) and (3) cover the most common pattern: the dashcam keeps
    recording while parked in a garage but loses GPS. find_parking_runs only
    triggers a skip when the *total* run length is long enough, so brief
    mid-drive GPS dropouts (a few clips through a tunnel) won't trip this.
    """
    gpx = find_gpx_for(clip.timestamp, *gps_dirs)
    if gpx is None:
        return True
    speeds = parse_gpx_speeds(gpx)
    if not speeds:
        return True
    slow = sum(1 for s in speeds if s < PARKING_SPEED_THRESHOLD_KMH)
    return (slow / len(speeds)) >= PARKING_CLIP_FRACTION


def find_parking_runs(
    group: list[Clip],
    gps_dirs: tuple[Path | None, ...],
    min_run_secs: int,
) -> list[tuple[int, int]]:
    """
    Find runs of consecutive parked clips where the total duration is at
    least min_run_secs. Returns list of (first_idx, last_idx) inclusive
    indices into `group`.
    """
    runs: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_secs = 0
    for i, c in enumerate(group):
        if clip_is_parked(c, gps_dirs):
            if cur_start is None:
                cur_start = i
                cur_secs = 0
            cur_secs += c.duration
        else:
            if cur_start is not None and cur_secs >= min_run_secs:
                runs.append((cur_start, i - 1))
            cur_start = None
            cur_secs = 0
    if cur_start is not None and cur_secs >= min_run_secs:
        runs.append((cur_start, len(group) - 1))
    return runs


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
    """Write a 1-second-per-cue SRT file with speed values. Returns False if no speeds."""
    if not speeds:
        return False
    unit = _speed_unit_label()
    with srt_path.open("w") as fh:
        for i, kmh in enumerate(speeds):
            s, e = i, i + 1
            sh, sm, ss = s // 3600, (s % 3600) // 60, s % 60
            eh, em, es = e // 3600, (e % 3600) // 60, e % 60
            value = _kmh_to_display(kmh)
            fh.write(f"{i+1}\n")
            fh.write(f"{sh:02d}:{sm:02d}:{ss:02d},000 --> {eh:02d}:{em:02d}:{es:02d},000\n")
            fh.write(f"{value:.0f} {unit}\n\n")
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
                "max_display": 0.0, "avg_display": 0.0,
                "duration_min": 0.0, "moving_min": 0.0, "n_segments": 0,
                "distance_display": 0.0, "distance_unit": "km",
                "speed_unit": _speed_unit_label(),
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
    max_kmh = max((p[2] for p in points), default=0.0)
    avg_kmh = (sum(speeds) / len(speeds)) if speeds else 0.0
    mph = SPEED_UNIT == "mph"
    return {
        "n": len(points),
        "n_segments": len(segs),
        "distance_km": dist,
        "max_kmh": max_kmh,
        "avg_kmh": avg_kmh,
        "max_display": max_kmh * KMH_TO_MPH if mph else max_kmh,
        "avg_display": avg_kmh * KMH_TO_MPH if mph else avg_kmh,
        "distance_display": dist * KMH_TO_MPH if mph else dist,
        "distance_unit": "mi" if mph else "km",
        "speed_unit": _speed_unit_label(),
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
    '<div><b>Speed (km/h)</b></div>' +
    '<div class="row"><div class="swatch" style="background:#6baed6"></div>&lt; 20</div>' +
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
    dunit, sunit = stats["distance_unit"], stats["speed_unit"]
    subtitle = (
        f"<span>{stats['distance_display']:.1f} {dunit} driven</span>"
        f"<span>{stats['moving_min']:.0f} min moving</span>"
        f"<span>max {stats['max_display']:.0f} {sunit}</span>"
        f"<span>avg {stats['avg_display']:.0f} {sunit}</span>"
        f"<span>{stats['n_segments']} segments / {stats['n']} points</span>"
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


def render_base_right_panel(
    points: list[tuple[float, float, float, datetime]],
    title: str,
    font_path: str,
    include_stats: bool = True,
) -> tuple[object, list[tuple[int, int]]] | None:
    """
    Render the full 480x1080 right-side panel:
      - Title + stats on top when include_stats=True (PIL ImageDraw)
      - Map widget below (480x480, OSM-tiled or PIL-fallback polyline)
    With include_stats=False, the title and stats block are omitted and the
    map is centred vertically inside the panel.
    Returns (PIL.Image full panel, pixel coords per GPS point in PANEL-local
    coordinates already offset for the map's vertical position) or None if PIL
    is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    if not points:
        return None

    # Render the 480x480 map block
    map_result = render_base_route_panel(points)
    if map_result is None:
        return None
    map_img, map_pixels = map_result

    panel_w, panel_h = MAP_PANEL_SIZE, OUT_H
    panel = Image.new("RGB", (panel_w, panel_h), (0, 0, 0))
    draw = ImageDraw.Draw(panel)

    def _load_font(size: int):
        try:
            return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    f_title = _load_font(26)
    f_value = _load_font(22)
    f_small = _load_font(15)

    stats = _track_stats(points)

    if include_stats:
        y = PANEL_STATS_TOP_PX
        draw.text((24, y), title, fill=(255, 255, 255), font=f_title)
        y += 38
        draw.line([(24, y), (panel_w - 24, y)], fill=(90, 90, 90), width=1)
        y += 18

        dunit = stats["distance_unit"]
        sunit = stats["speed_unit"]
        rows = [
            ("Distance",  f"{stats['distance_display']:.1f} {dunit}"),
            ("Driven",    f"{stats['moving_min']:.0f} min"),
            ("Max speed", f"{stats['max_display']:.0f} {sunit}"),
            ("Avg speed", f"{stats['avg_display']:.0f} {sunit}"),
        ]
        for label, value in rows:
            draw.text((24, y), label, fill=(170, 170, 170), font=f_value)
            bbox = draw.textbbox((0, 0), value, font=f_value)
            value_w = bbox[2] - bbox[0]
            draw.text((panel_w - 24 - value_w, y), value, fill=(255, 255, 255), font=f_value)
            y += 30

        y += 6
        draw.text(
            (24, y),
            f"{stats['n_segments']} segments / {stats['n']} points",
            fill=(140, 140, 140), font=f_small,
        )
        map_top = PANEL_MAP_TOP_PX
    else:
        # Stats hidden — centre the 480×480 map vertically inside the 480×1080
        # panel so the panel doesn't look top-heavy.
        map_top = (panel_h - MAP_PANEL_SIZE) // 2

    # Paste the map at its chosen vertical position
    panel.paste(map_img, (0, map_top))

    # Marker pixel coordinates in panel-local space (offset for the map position)
    adjusted = [(px, py + map_top) for (px, py) in map_pixels]
    return panel, adjusted


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
        # OSM's tile usage policy requires a custom User-Agent identifying the
        # app; without it the request gets a 429/403 and tile fetch returns no
        # tiles, which in turn makes staticmap raise. Install a global opener
        # with a sensible UA before any tile fetch.
        import urllib.request
        opener = urllib.request.build_opener()
        opener.addheaders = [
            ("User-Agent",
             "dashcam-exporter/0.1 (+https://github.com/raoulsson/dashcam-exporter)")
        ]
        urllib.request.install_opener(opener)

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

        # Re-project on top of staticmap's projection so the marker lands
        # precisely. staticmap's private API has shifted between versions
        # (0.5.7 dropped _calculate_extent), so do the web-mercator math
        # ourselves using only the public-ish post-render attributes
        # `m.zoom`, `m.x_center`, `m.y_center`. Each tile is 256 px wide.
        TILE_PX = 256

        def _ll_to_tile(lat: float, lon: float, z: int) -> tuple[float, float]:
            n = 2 ** z
            xt = (lon + 180.0) / 360.0 * n
            lat_rad = math.radians(lat)
            yt = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
            return xt, yt

        # Pull zoom and center from the rendered StaticMap; fall back to
        # computing them if the attribute names ever change again.
        zoom = getattr(m, "zoom", None)
        if zoom is None:
            zoom = m._calculate_zoom() if hasattr(m, "_calculate_zoom") else 14
        cx = getattr(m, "x_center", None)
        cy = getattr(m, "y_center", None)
        if cx is None or cy is None:
            lats = [p[0] for p in points]
            lons = [p[1] for p in points]
            cx, cy = _ll_to_tile((min(lats) + max(lats)) / 2,
                                 (min(lons) + max(lons)) / 2, zoom)

        px_list = []
        for lat, lon, _, _ in points:
            xt, yt = _ll_to_tile(lat, lon, zoom)
            px = int(round(size / 2 + (xt - cx) * TILE_PX))
            py = int(round(size / 2 + (yt - cy) * TILE_PX))
            px_list.append((px, py))
    except ImportError:
        # staticmap not installed — fall through to PIL fallback silently
        img = None
    except Exception as e:
        # Network / OSM error: log it so the user sees WHY we fell back, not
        # just an unexpected beige grid in the burn-in widget.
        print(f"  ! map widget: OSM tile fetch failed ({type(e).__name__}: {e});"
              f" using plain polyline background", file=sys.stderr)
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


def _gpx_is_scrambled(points: list[tuple[float, float, float, datetime]]) -> bool:
    """
    True if consecutive GPS fixes jump by more than 60s in either direction.
    The dashcam's parking-mode buffer dumps stale fixes into the first clip
    of a new drive — those files have multiple disjoint time chunks and the
    per-second marker positions become meaningless.
    """
    if len(points) < 2:
        return False
    for a, b in zip(points, points[1:]):
        if abs((b[3] - a[3]).total_seconds()) > 60:
            return True
    return False


def render_clip_marker_video(
    clip: Clip,
    base_panel: object,           # PIL.Image
    drive_points: list[tuple[float, float, float, datetime]],
    drive_pixels: list[tuple[int, int]],
    gps_dirs: tuple[Path | None, ...],
    out_video: Path,
    trim_start: int = 0,
    trim_seconds: int | None = None,
) -> tuple[bool, tuple[int, int] | None]:
    """
    For one clip: render PNG frames (base panel + marker at current position)
    and assemble into a 1-fps MP4. trim_start/trim_seconds restrict to a slice
    of the clip's duration so the map matches the trimmed video.
    Returns False if the clip has no GPS coverage.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False, None

    # Per-clip points
    gpx = find_gpx_for(clip.timestamp, *gps_dirs)
    if gpx is None:
        return False, None
    clip_points = parse_gpx_track(gpx)
    if not clip_points:
        return False, None
    # Reject scrambled buffers (parking-mode stale-data dumps)
    if _gpx_is_scrambled(clip_points):
        return False, None

    # Map each clip second to the nearest pixel on the drive map
    n_full = clip.duration
    per_second_full: list[tuple[int, int]] = []
    if len(clip_points) >= n_full:
        for i in range(n_full):
            lat = clip_points[i][0]; lon = clip_points[i][1]
            per_second_full.append(_nearest_pixel(lat, lon, drive_points, drive_pixels))
    else:
        for i in range(n_full):
            j = min(int(i * len(clip_points) / n_full), len(clip_points) - 1)
            lat = clip_points[j][0]; lon = clip_points[j][1]
            per_second_full.append(_nearest_pixel(lat, lon, drive_points, drive_pixels))

    duration = trim_seconds if trim_seconds is not None else (n_full - trim_start)
    per_second = per_second_full[trim_start:trim_start + duration]
    if not per_second:
        return False, None

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

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", "1",
        "-i", str(work / "f_%04d.png"),
        "-vf", f"fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_video),
    ]
    run_ffmpeg(cmd)

    for png in work.glob("*.png"):
        try: png.unlink()
        except OSError: pass
    try: work.rmdir()
    except OSError: pass

    last_pixel = per_second[-1] if per_second else None
    return True, last_pixel


def _render_static_panel_video(
    base_panel: object,
    duration: int,
    out_video: Path,
    marker_pixel: tuple[int, int] | None = None,
) -> bool:
    """
    Make a video of just the base panel for clips with no GPS data.
    If marker_pixel is given, paint a frozen marker dot at that pixel so the
    map widget shows "we're still here" instead of an empty panel.
    """
    try:
        from PIL import Image, ImageDraw   # noqa: F401
    except ImportError:
        return False
    tmp_png = out_video.with_suffix(".still.png")
    img = base_panel.convert("RGB").copy()
    if marker_pixel is not None:
        px, py = marker_pixel
        d = ImageDraw.Draw(img)
        r = 11
        d.ellipse([px-r, py-r, px+r, py+r], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        d.ellipse([px-6, py-6, px+6, py+6], fill=(214, 39, 40))
    img.save(tmp_png, "PNG")
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
    dunit = stats["distance_unit"]
    sunit = stats["speed_unit"]
    body = (
        f"{title}\n"
        f"{'=' * len(title)}\n\n"
        f"Distance: {stats['distance_display']:.2f} {dunit}\n"
        f"Duration: {stats['duration_min']:.1f} minutes\n"
        f"Max speed: {stats['max_display']:.1f} {sunit}\n"
        f"Avg moving speed: {stats['avg_display']:.1f} {sunit}\n"
        f"GPS points: {stats['n']}\n\n"
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
    # macOS, then common Linux distros, then Windows. First one that exists wins.
    candidates = [
        DEFAULT_FONT,
        FALLBACK_FONT,
        # Common Linux fonts (Debian/Ubuntu defaults)
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        # Windows — best-effort, script is untested there
        r"C:\Windows\Fonts\courbd.ttf",      # Courier New Bold
        r"C:\Windows\Fonts\cour.ttf",        # Courier New Regular
        r"C:\Windows\Fonts\consola.ttf",     # Consolas
        r"C:\Windows\Fonts\arial.ttf",       # Arial Regular
    ]
    for f in candidates:
        if Path(f).exists():
            return f
    # Last-ditch: hand the macOS default back; ffmpeg will error if it's not
    # there, which is at least a clear actionable message.
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
    with_rear: bool = True,
) -> str:
    """
    Front 2560x1600 -> crop to 16:9 (lose 80 px top/bottom) -> scale 1920x1080
    Rear  -> scale to 576x324 with a thin white border
    Overlay rear at bottom-center with a 24 px margin (covers the bonnet area)
    Optionally:
      - burn 'YYYY-MM-DD HH:MM:SS' in bottom-left
      - render a per-second 'NN km/h' subtitle on the left, above the timestamp
    """
    # Front always — crop the bonnet rows, then scale to 1080p.
    base = (
        f"[0:v]crop={FRONT_W}:{FRONT_H - FRONT_CROP_TOP - FRONT_CROP_BOTTOM}:0:{FRONT_CROP_TOP},"
        f"scale={OUT_W}:{OUT_H},setsar=1,fps={OUT_FPS}[front];"
    )
    if REAR_PIP_ENABLED:
        # Overlay coords by position. Only positions that don't collide with
        # the speed readout (bottom-right) or timestamp (bottom-left) are
        # offered — bottom-middle plus the three top corners.
        pos = (REAR_PIP_POSITION or "bottom-middle").lower()
        m = PIP_MARGIN
        ov_x = {
            "bottom-middle": "(W-w)/2",
            "top-left":      f"{m}",
            "top-middle":    "(W-w)/2",
            "top-right":     f"W-w-{m}",
        }.get(pos, "(W-w)/2")
        ov_y = {
            "bottom-middle": f"H-h-{m}",
            "top-left":      f"{m}",
            "top-middle":    f"{m}",
            "top-right":     f"{m}",
        }.get(pos, f"H-h-{m}")
        base += (
            f"[1:v]scale={PIP_W}:{PIP_H},setsar=1,fps={OUT_FPS},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.9:t=3[rear];"
            f"[front][rear]overlay={ov_x}:{ov_y}"
        )
    else:
        base += "[front]null"   # front already labelled; just pass it through

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

    # Watermark — drawn on the main video stream BEFORE the hstack so x/y
    # use the 1920x1080 frame's own coordinate system (no need to know which
    # side the map panel ends up on).
    font_escaped = font_path.replace(":", r"\:") if font_path else ""
    if font_escaped and COPYRIGHT_TEXT:
        pos = (COPYRIGHT_POSITION or "bottom-right").lower()
        mh, mv = COPYRIGHT_MARGIN_H, COPYRIGHT_MARGIN_V
        # drawtext's `tw` and `th` are evaluated per-frame from the actual
        # rendered glyph metrics, so `x = w - tw - mh` right-anchors the text
        # regardless of character count.
        if pos == "bottom-left":
            wm_x, wm_y = f"{mh}", f"h-th-{mv}"
        elif pos == "top-right":
            wm_x, wm_y = f"w-tw-{mh}", f"{mv}"
        elif pos == "top-left":
            wm_x, wm_y = f"{mh}", f"{mv}"
        else:  # bottom-right (default) — tight to both edges, under the speed
            wm_x, wm_y = f"w-tw-{mh}", f"h-th-{mv}"
        chain += (
            f",drawtext=fontfile={font_escaped}:"
            f"text='{_escape_drawtext(COPYRIGHT_TEXT)}':"
            f"fontcolor=white@0.85:fontsize={COPYRIGHT_FONT_SIZE}:"
            f"borderw=2:bordercolor=black@0.6:"
            f"x={wm_x}:y={wm_y}"
        )

    if with_map_widget:
        chain += "[video_part];"
        gutter = MAP_PANEL_GUTTER_PX
        on_left = (MAP_PANEL_POSITION or "right").lower() == "left"
        pad_x = gutter if not on_left else 0
        map_in = "[2:v]" if with_rear else "[1:v]"
        chain += (
            f"{map_in}scale={MAP_PANEL_SIZE}:{OUT_H},setsar=1,fps={OUT_FPS},"
            f"pad={MAP_PANEL_SIZE + gutter}:{OUT_H}:{pad_x}:0:color=black[map_part];"
        )
        if on_left:
            chain += "[map_part][video_part]hstack[out]"
        else:
            chain += "[video_part][map_part]hstack[out]"
        return chain
    return chain + "[out]"


def _escape_drawtext(text: str) -> str:
    """Escape special characters in a drawtext text= value."""
    return (text
            .replace("\\", r"\\")
            .replace(":", r"\:")
            .replace("'", r"\'"))


_NOISY_FFMPEG_PATTERNS = (
    # The DDPAI custom telemetry track triggers this on every clip; the stream
    # is auto-discarded anyway, the message is purely informational.
    "have zero duration",
    "stream set to be discarded by default",
    # Concat-demuxer prints one of these per audio packet at segment boundaries
    # when DTS doesn't perfectly line up across re-encoded segments. ffmpeg
    # auto-corrects (you'd see the warning even on a clean file). Cosmetic.
    "Non-monotonic DTS",
    "Non-monotonous DTS",
    # The harmless VideoToolbox note we already see on every hardware encode
    "Color range not set for yuv420p",
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
    trim_start: int = 0,
    trim_seconds: int | None = None,
    no_audio: bool = False,
    output_height: int = 0,
) -> None:
    """
    Encode one clip (or one trimmed slice of it) to `out_path`.
    trim_start / trim_seconds are in source-clip seconds. If trim_seconds is
    None, encode to the end of the clip.
    """
    duration = trim_seconds if trim_seconds is not None else (clip.duration - trim_start)
    actual_epoch = clip.epoch_utc + trim_start

    # If GPS data exists for this clip, write a sidecar SRT (sliced to the trim
    # window) and pass it to the filter.
    speed_srt: Path | None = None
    if with_speed:
        gpx = find_gpx_for(clip.timestamp, *gps_dirs)
        if gpx is not None:
            all_speeds = parse_gpx_speeds(gpx)
            window = all_speeds[trim_start:trim_start + duration]
            srt_path = out_path.with_suffix(".speed.srt")
            if write_speed_srt(window, srt_path):
                speed_srt = srt_path

    with_map_widget = map_video is not None
    with_rear = REAR_PIP_ENABLED and clip.rear is not None
    filt = build_filter_complex(font_path, actual_epoch, with_timestamp, speed_srt,
                                with_map_widget, with_rear=with_rear)
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

    # When we want audio in the output AND the source clip has no audio
    # stream, we must inject a silent anullsrc input — otherwise the
    # resulting intermediate would have no audio track, and the concat
    # demuxer silently drops audio across the WHOLE output for every clip
    # past the first audio-less one.
    use_rear = REAR_PIP_ENABLED and clip.rear is not None
    source_has_audio = (not no_audio) and file_has_audio(clip.front)
    need_silent_audio = (not no_audio) and not source_has_audio

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
    # -ss before -i seeks to the trim_start; pts is rebased to 0 in the output,
    # which is what drawtext (now using actual_epoch) expects.
    if trim_start:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", str(clip.front)]
    if use_rear:
        if trim_start:
            cmd += ["-ss", str(trim_start)]
        cmd += ["-i", str(clip.rear)]
    if with_map_widget:
        cmd += ["-i", str(map_video)]
    if need_silent_audio:
        # Tracks the input index of the silent audio: appended after all video inputs.
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000"]
    if trim_seconds is not None:
        cmd += ["-t", str(trim_seconds)]
    # Optional final downscale (output_height != 0) and audio strip
    if output_height and output_height != OUT_H:
        filt = filt.replace("[out]", "[pre_scaled];[pre_scaled]scale=-2:" +
                            str(output_height) + "[out]", 1)
    cmd += ["-filter_complex", filt, "-map", "[out]"]
    if no_audio:
        cmd += ["-an"]
    else:
        # Normalize audio to a consistent 48 kHz stereo AAC across every
        # intermediate (sources are typically 16 kHz mono, the transition
        # slide is 48 kHz stereo from anullsrc). Without this, the concat
        # demuxer hits a layout mismatch at the first transition and silently
        # drops audio for the rest of the output.
        if source_has_audio:
            cmd += ["-map", "0:a:0"]
        else:
            n_inputs = 1 + (1 if use_rear else 0) + (1 if with_map_widget else 0) + 1
            silent_idx = n_inputs - 1
            cmd += ["-map", f"{silent_idx}:a", "-shortest"]
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2"]
    cmd += [
        *venc,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(cmd)


def _fmt_skip_duration(secs: float) -> str:
    m, s = divmod(int(round(secs)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m skipped"
    if m:
        return f"{m}m {s:02d}s skipped"
    return f"{s}s skipped"


def generate_transition_slide(
    out_video: Path,
    duration: int,
    font_path: str,
    with_map_widget: bool,
    use_vt: bool,
    skipped_secs: float | None = None,
    output_height: int = 0,
    no_audio: bool = False,
) -> None:
    """
    Render a `duration`-second black slide with the 'Fast forwarding...' text
    centered, matching the dimensions and codec params of the regular per-clip
    intermediates so it can be concat-demuxed alongside them. If skipped_secs
    is given, the elapsed time is shown beneath the headline.
    """
    # +2 for the gutter that build_filter_complex adds between video and map.
    width = OUT_W + (MAP_PANEL_SIZE + 2 if with_map_widget else 0)
    height = OUT_H
    # When the main encode is downscaled, the transition slide must match,
    # otherwise concat-demuxer will refuse to splice them together.
    if output_height and output_height != OUT_H:
        scale = output_height / OUT_H
        width = int(round(width * scale)) & ~1   # keep even
        height = output_height
    font_escaped = font_path.replace(":", r"\:")
    if use_vt:
        venc = ["-c:v", "h264_videotoolbox", "-b:v", VT_BITRATE,
                "-maxrate", VT_MAXRATE, "-profile:v", "high"]
    else:
        venc = ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", X264_CRF]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i",
        f"color=c=black:s={width}x{height}:r={OUT_FPS}:d={duration}",
    ]
    if not no_audio:
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}"]
    cmd += [
        "-vf",
        (
            f"drawtext=fontfile={font_escaped}:text='{TRANSITION_TEXT}':"
            f"fontcolor=white:fontsize={TRANSITION_FONT_SIZE}:"
            f"x=(w-tw)/2:y=(h-th)/2-30"
            + (
                f",drawtext=fontfile={font_escaped}:"
                f"text='{_fmt_skip_duration(skipped_secs)}':"
                f"fontcolor=white@0.7:fontsize=32:"
                f"x=(w-tw)/2:y=(h-th)/2+40"
                if skipped_secs
                else ""
            )
        ),
        "-map", "0:v",
    ]
    if not no_audio:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2"]
    cmd += [*venc, "-pix_fmt", "yuv420p", "-shortest", str(out_video)]
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

def load_config_file(path: Path) -> dict[str, str]:
    """Parse a key=value config file. # introduces a comment to end of line."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _cfg_bool(s: str) -> bool:
    return s.strip().lower() in ("true", "yes", "1", "on")


def _resolve_config_path(argv: list[str]) -> Path:
    """Pre-parse argv to find --config PATH (so we can use it as defaults source)."""
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
        if a.startswith("--config="):
            return Path(a.split("=", 1)[1]).expanduser()
    # Defaults: look next to the script
    return Path(__file__).resolve().parent / "config.txt"


def main() -> int:
    # --- Config file loading (CLI > config.txt > built-in defaults) ----------
    config_path = _resolve_config_path(sys.argv[1:])
    cfg = load_config_file(config_path)
    cs = lambda k, d: cfg.get(k, d)
    ci = lambda k, d: int(cfg[k]) if k in cfg and cfg[k] != "" else d
    cb = lambda k, d: _cfg_bool(cfg[k]) if k in cfg else d

    # Boolean knobs are stored POSITIVELY in config (timestamp=true rather than
    # no_timestamp=false) — easier to read. Translate to the existing --no-* CLI.
    default_no_timestamp     = not cb("timestamp",     True)
    default_no_speed         = not cb("speed",         True)
    default_no_map_widget    = not cb("map_widget",    True)
    default_no_map_sidecars  = not cb("map_sidecars",  True)
    default_no_skip_parking  = not cb("skip_parking",  True)
    default_no_audio         = not cb("audio",         True)
    default_daily            =     cb("daily",         False)
    default_software         =     cb("software",      False)
    default_keep_inter       =     cb("keep_intermediates", False)

    # Override the structural module-level constants from config (these are read
    # by build_filter_complex et al. at call-time, so updating here is sufficient).
    global PIP_W, PIP_H, PIP_MARGIN, REAR_PIP_POSITION, REAR_PIP_ENABLED
    global MAP_PANEL_SIZE, MAP_PANEL_POSITION, MAP_PANEL_GUTTER_PX
    global FRONT_CROP_TOP, FRONT_CROP_BOTTOM
    global COPYRIGHT_TEXT, COPYRIGHT_FONT_SIZE, COPYRIGHT_POSITION
    global COPYRIGHT_MARGIN_H, COPYRIGHT_MARGIN_V
    global SPEED_MARGIN_V, SPEED_MARGIN_R, SPEED_FONT_SIZE
    global VT_BITRATE, VT_MAXRATE, X264_PRESET, X264_CRF
    global SPEED_UNIT
    # Rear PiP dimensions. If only one of width/height is set in config, the
    # other is computed from the rear camera's native 16:9 aspect ratio so
    # the user doesn't accidentally squash the picture.
    REAR_SRC_RATIO = 16 / 9            # DDPAI rear is 1920x1080
    has_w = "rear_pip_w" in cfg
    has_h = "rear_pip_h" in cfg
    if has_w and not has_h:
        PIP_W = ci("rear_pip_w", PIP_W)
        PIP_H = int(round(PIP_W / REAR_SRC_RATIO)) & ~1   # keep even
    elif has_h and not has_w:
        PIP_H = ci("rear_pip_h", PIP_H)
        PIP_W = int(round(PIP_H * REAR_SRC_RATIO)) & ~1
    else:
        PIP_W = ci("rear_pip_w", PIP_W)
        PIP_H = ci("rear_pip_h", PIP_H)
    PIP_MARGIN         = ci("rear_pip_margin",    PIP_MARGIN)
    REAR_PIP_POSITION  = cs("rear_pip_position",  REAR_PIP_POSITION).lower()
    REAR_PIP_ENABLED   = cb("rear_pip",           REAR_PIP_ENABLED)
    MAP_PANEL_SIZE     = ci("map_panel_w",        MAP_PANEL_SIZE)
    MAP_PANEL_POSITION = cs("map_panel_position", MAP_PANEL_POSITION).lower()
    MAP_PANEL_GUTTER_PX = ci("map_panel_gutter_px", MAP_PANEL_GUTTER_PX)
    FRONT_CROP_TOP     = ci("front_crop_top",     FRONT_CROP_TOP)
    FRONT_CROP_BOTTOM  = ci("front_crop_bottom",  FRONT_CROP_BOTTOM)
    COPYRIGHT_TEXT     = cs("watermark_text",     COPYRIGHT_TEXT)
    COPYRIGHT_FONT_SIZE = ci("watermark_font_size", COPYRIGHT_FONT_SIZE)
    COPYRIGHT_POSITION = cs("watermark_position", COPYRIGHT_POSITION).lower()
    COPYRIGHT_MARGIN_H = ci("watermark_margin_h", COPYRIGHT_MARGIN_H)
    COPYRIGHT_MARGIN_V = ci("watermark_margin_v", COPYRIGHT_MARGIN_V)
    SPEED_FONT_SIZE    = ci("speed_font_size",    SPEED_FONT_SIZE)
    SPEED_MARGIN_V     = ci("speed_margin_v",     SPEED_MARGIN_V)
    SPEED_MARGIN_R     = ci("speed_margin_r",     SPEED_MARGIN_R)
    SPEED_UNIT         = cs("speed_unit",         SPEED_UNIT).lower()
    if SPEED_UNIT not in ("kmh", "mph"):
        print(f"WARNING: unknown speed_unit='{SPEED_UNIT}'; using 'kmh'", file=sys.stderr)
        SPEED_UNIT = "kmh"

    panel_stats_enabled = cb("panel_stats", True)
    VT_BITRATE         = cs("vt_bitrate",         VT_BITRATE)
    VT_MAXRATE         = cs("vt_maxrate",         VT_MAXRATE)
    X264_PRESET        = cs("x264_preset",        X264_PRESET)
    X264_CRF           = cs("x264_crf",           X264_CRF)
    if MAP_PANEL_POSITION not in ("right", "left"):
        print(f"WARNING: map_panel_position='{MAP_PANEL_POSITION}' not recognised; "
              "using 'right'.", file=sys.stderr)
        MAP_PANEL_POSITION = "right"

    # Final-output downscaling (e.g. for web/mobile delivery).
    output_height_cfg = ci("output_height", 0)        # 0 = no downscale

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config_path),
                    help=f"Path to config.txt (default: {config_path})")
    ap.add_argument("--write-config", metavar="PATH",
                    help="Write a fully-commented config.txt template to PATH and exit.")
    ap.add_argument("--root",  default=cs("root", DEFAULT_ROOT),
                    help=f"Dashcam volume root (default: {cs('root', DEFAULT_ROOT)})")
    ap.add_argument("--out",   default=cs("out", DEFAULT_OUT),
                    help=f"Output folder (default: {cs('out', DEFAULT_OUT)})")
    ap.add_argument("--gap",   type=int, default=ci("gap", DEFAULT_GAP),
                    help="Seconds between clips to consider a new drive")
    ap.add_argument("--drives", nargs="+", type=int,
                    help="Only process specific drive numbers (1-based)")
    ap.add_argument("--software", action="store_true", default=default_software,
                    help="Use libx264 instead of VideoToolbox")
    ap.add_argument("--keep-intermediates", action="store_true", default=default_keep_inter,
                    help="Keep per-clip processed files")
    ap.add_argument("--dry-run", action="store_true", help="List drives and exit without encoding")
    ap.add_argument("--no-timestamp", action="store_true", default=default_no_timestamp,
                    help="Skip the burned-in date/time overlay")
    ap.add_argument("--no-speed", action="store_true", default=default_no_speed,
                    help="Skip the GPS speed overlay even when GPX data is available")
    ap.add_argument("--no-audio", action="store_true", default=default_no_audio,
                    help="Strip audio from the output (useful if passenger talk shouldn't be shared)")
    ap.add_argument("--daily", action="store_true", default=default_daily,
                    help="Group clips by calendar date, producing one MP4 per day")
    ap.add_argument("--no-map-sidecars", action="store_true", default=default_no_map_sidecars,
                    help="Skip the per-group .html / .gpx / _links.txt map sidecars")
    ap.add_argument("--no-map-widget", action="store_true", default=default_no_map_widget,
                    help="Skip the burned-in mini-map panel on the right of the video frame")
    ap.add_argument("--sidecars-only", action="store_true",
                    help="Only (re-)generate the .html / .gpx / _links.txt sidecars, skip video encoding")
    ap.add_argument("--no-skip-parking", action="store_true", default=default_no_skip_parking,
                    help="Disable the parking-skip")
    ap.add_argument("--parking-min-secs", type=int,
                    default=ci("parking_min_secs", DEFAULT_PARKING_MIN_SECS),
                    help=f"Minimum length (s) of a parked run before we skip it")
    # Legacy combined knob (still accepted) plus per-side overrides.
    ap.add_argument("--parking-pad-secs", type=int,
                    default=ci("parking_pad_secs", DEFAULT_PARKING_PAD_SECS),
                    help="DEPRECATED: shorthand for both --parking-entry-pad and "
                         "--parking-exit-pad. Prefer the explicit pair.")
    ap.add_argument("--parking-entry-pad", type=int,
                    default=ci("parking_entry_pad",
                               ci("parking_pad_secs", DEFAULT_PARKING_ENTRY_PAD)),
                    help="Seconds of footage BEFORE the Fast-forwarding slide "
                         "(entry slice length). Default 5.")
    ap.add_argument("--parking-exit-pad", type=int,
                    default=ci("parking_exit_pad",
                               ci("parking_pad_secs", DEFAULT_PARKING_EXIT_PAD)),
                    help="Seconds of footage AFTER the FF slide before "
                         "drive-resume (exit slice leading padding). Default 10.")
    ap.add_argument("--min-clips-per-group", type=int,
                    default=ci("min_clips_per_group", DEFAULT_MIN_CLIPS_PER_GROUP),
                    help="Auto-skip groups with fewer than this many clips "
                         "(typical loop-recording fragments). Force-encode "
                         "anyway by naming the group via --drives. Default 4.")
    ap.add_argument("--inter-clip-gap-secs", type=int,
                    default=ci("inter_clip_gap_secs", DEFAULT_INTER_CLIP_GAP_SECS),
                    help="Insert a 'Fast forwarding…' transition slide "
                         "whenever the wall-clock gap between consecutive "
                         "clips exceeds this many seconds (default 60).")
    ap.add_argument("--exit-skip-secs", type=int,
                    default=ci("exit_skip_secs", DEFAULT_EXIT_SKIP_SECS),
                    help="Standard seek into the exit clip after a parking "
                         "gap. Drive-resume detection refines this when GPS "
                         "clearly shows continuous motion; otherwise this "
                         "value is used as-is (default 30).")
    ap.add_argument("--drive-resume-sustain-secs", type=int,
                    default=ci("drive_resume_sustain_secs", DRIVE_RESUME_SUSTAIN_SECS),
                    help="Seconds of continuous GPS motion required to "
                         "consider a moving block as 'real drive' for "
                         "exit-slice anchoring (default 30).")
    ap.add_argument("--output-height", type=int, default=output_height_cfg,
                    help="Downscale the final composite to this height in px (0 = native)")
    args = ap.parse_args()

    # Handle --write-config and exit
    if args.write_config:
        target = Path(args.write_config).expanduser()
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"wrote {target}")
        return 0

    if cfg:
        print(f"config:    loaded {len(cfg)} setting(s) from {config_path}")

    root = Path(args.root).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    front_dir = root / "DCIM" / "200video" / "front"
    rear_dir  = root / "DCIM" / "200video" / "rear"
    gps_dir   = root / "DCIM" / "203gps"
    tar_dir   = gps_dir / "tar"
    if not front_dir.is_dir():
        print(f"ERROR: expected front folder at {front_dir}", file=sys.stderr)
        return 1
    rear_present = rear_dir.is_dir() and any(REAR_RE.match(f) for f in os.listdir(rear_dir))
    if REAR_PIP_ENABLED and not rear_present:
        print(f"  note: no rear clips found at {rear_dir} — rear PiP auto-disabled",
              file=sys.stderr)
        REAR_PIP_ENABLED = False
    rear_dir = rear_dir if rear_present else None
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

    # When --drives is given, ONLY those groups run (and the min-clips filter
    # is bypassed for them — user explicitly asked). Otherwise, every group
    # that has at least min_clips_per_group clips runs.
    explicit_set = set(args.drives) if args.drives else None
    if explicit_set is not None:
        wanted = explicit_set
    else:
        wanted = {i for i, g in enumerate(groups, 1)
                  if len(g) >= args.min_clips_per_group}
        skipped_small = [(i, len(g)) for i, g in enumerate(groups, 1)
                         if len(g) < args.min_clips_per_group]
        if skipped_small:
            note = ", ".join(f"#{i} ({n} clip{'s' if n != 1 else ''})"
                             for i, n in skipped_small)
            print(f"\nAuto-skipping {len(skipped_small)} fragment {group_kind}(s): "
                  f"{note}\n(force-encode by naming the index via --drives.)")

    if args.dry_run:
        return 0

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
                  f"{stats['n']} points → {html_path.name}, {gpx_path.name}, {links_path.name}")
        elif not args.no_map_sidecars:
            print(f"  map: (no GPS data for this {group_kind})")

        if args.sidecars_only:
            continue

        if final.exists():
            print(f"  video: {final.name} already exists — skipping (delete to re-encode)")
            continue

        # Pre-render the burn-in right panel (stats on top + map + optional QR)
        base_panel = None
        group_pixels: list[tuple[int, int]] = []
        if not args.no_map_widget and group_track:
            panel_title = (f"Drive {idx} — {start:%Y-%m-%d}" if not args.daily
                           else f"Day — {start:%Y-%m-%d}")
            rendered = render_base_right_panel(
                group_track,
                title=panel_title,
                font_path=font_path,
                include_stats=panel_stats_enabled,
            )
            if rendered is None:
                print("  ! map widget skipped: PIL/Pillow not installed."
                      " Run: pip3 install -r requirements.txt")
            else:
                base_panel, group_pixels = rendered
        with_map_widget = base_panel is not None

        # Identify long parking runs we should skip past.
        parking_runs: list[tuple[int, int]] = []
        if not args.no_skip_parking and with_speed:
            parking_runs = find_parking_runs(group, gps_dirs, args.parking_min_secs)

        # Map clip-index → action.
        #   entry  = first pad seconds of the FIRST parked clip
        #   skip   = drop entirely (every clip in the parked run, including the last)
        #   exit   = first pad seconds of the NEXT MOVING clip after the run
        # This means the Fast-forwarding slide covers both the remaining parked
        # footage AND any engine-off gap until the next drive resumes.
        action_for: dict[int, str] = {}
        skipped_secs_for: dict[int, float] = {}
        for run_start, run_end in parking_runs:
            action_for[run_start] = "entry"
            for k in range(run_start + 1, run_end + 1):
                action_for[k] = "skip"
            next_idx = run_end + 1
            if next_idx < len(group) and next_idx not in action_for:
                action_for[next_idx] = "exit"
                # Wall-clock seconds elapsed between the entry's last frame and
                # the exit's first frame.
                entry_end = group[run_start].dt + timedelta(seconds=args.parking_entry_pad)
                exit_start = group[next_idx].dt
                skipped_secs_for[run_start] = max(
                    0.0, (exit_start - entry_end).total_seconds()
                )

        if parking_runs:
            saved = 0
            for s, e in parking_runs:
                next_idx = e + 1
                if next_idx < len(group):
                    span = (group[next_idx].dt - group[s].dt).total_seconds() \
                        + args.parking_exit_pad
                else:
                    span = (e - s + 1) * group[s].duration
                saved += int(span - args.parking_entry_pad
                             - args.parking_exit_pad - TRANSITION_SECS)
            print(f"  parking: {len(parking_runs)} run(s) skipped, "
                  f"~{fmt_secs(max(saved, 0))} cut from the output")

        entry_pad = args.parking_entry_pad
        exit_pad  = args.parking_exit_pad

        intermediates: list[Path] = []
        prev_emitted_clip: Clip | None = None
        # Last known good marker pixel — persisted across clips so parked /
        # scrambled-GPS clips show a frozen dot at the last real location
        # instead of disappearing or jumping to the route's start.
        last_marker_pixel: tuple[int, int] | None = None
        for ci, clip in enumerate(group, 1):
            ci0 = ci - 1
            action = action_for.get(ci0)

            # Anywhere inside a parked run (including its last clip) — drop entirely.
            if action == "skip":
                continue

            # Inter-clip gap detection: insert a 'Fast forwarding…' slide when
            # the wall-clock distance from the previous emitted clip exceeds
            # the threshold. Parking-run exits ALREADY have their own FF
            # inserted by the entry side, so skip in that case.
            if prev_emitted_clip is not None and action != "exit":
                prev_end = prev_emitted_clip.dt + timedelta(seconds=prev_emitted_clip.duration)
                gap_secs = (clip.dt - prev_end).total_seconds()
                if gap_secs > args.inter_clip_gap_secs:
                    gap_trans = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_gap_transition.mp4"
                    if not gap_trans.exists():
                        print(f"        + gap transition slide ({TRANSITION_SECS}s, "
                              f"~{_fmt_skip_duration(gap_secs)})")
                        generate_transition_slide(
                            gap_trans, TRANSITION_SECS, font_path, with_map_widget, use_vt,
                            skipped_secs=gap_secs,
                            output_height=args.output_height,
                            no_audio=args.no_audio,
                        )
                    intermediates.append(gap_trans)

            # Entry slice: first `pad` seconds of the first parked clip.
            # Exit slice: anchored to the actual drive-resume moment within
            # the next moving clip. When sustained motion is detected, back
            # up by `pad` seconds so the slice ends just as the wheels start
            # turning. When the GPS for the first-clip-after-parking is
            # corrupted (the dashcam often flushes stale buffered data
            # there), fall back to skipping a configurable amount of clip
            # head so we land closer to the actual drive moment.
            trim_start = 0
            trim_seconds: int | None = None
            if action == "entry":
                trim_seconds = entry_pad
            elif action == "exit":
                drive_sec = find_drive_resume_second(
                    clip, gps_dirs,
                    sustain_secs=args.drive_resume_sustain_secs,
                )
                if drive_sec is None:
                    trim_start = min(args.exit_skip_secs, max(0, clip.duration - exit_pad))
                else:
                    trim_start = max(0, drive_sec - exit_pad)
                trim_seconds = None     # run to end of clip

            # Per-slice intermediate filename. Suffix the action so re-runs
            # can find / cache them correctly.
            suffix = f"_{action}" if action else ""
            inter = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_{clip.timestamp}{suffix}.mp4"

            # Per-clip map widget video (trimmed if we're trimming the video).
            map_video: Path | None = None
            if with_map_widget:
                map_video = inter.with_suffix(".map.mp4")
                if not map_video.exists():
                    ok, last_pixel = render_clip_marker_video(
                        clip, base_panel, group_track, group_pixels, gps_dirs, map_video,
                        trim_start=trim_start, trim_seconds=trim_seconds,
                    )
                    if ok and last_pixel is not None:
                        last_marker_pixel = last_pixel
                    if not ok:
                        # IMPORTANT: when trim_seconds is None the video runs
                        # to the end of the clip, which is (duration - trim_start)
                        # seconds long. The static map panel must match that
                        # length exactly, otherwise hstack waits for the longer
                        # stream and the front/rear frame freezes for the
                        # remainder. (The bug used to show as a ~minute pause
                        # right after the GPS-fix transition.)
                        actual_dur = (trim_seconds if trim_seconds is not None
                                      else (clip.duration - trim_start))
                        ok = _render_static_panel_video(
                            base_panel, actual_dur, map_video,
                            marker_pixel=last_marker_pixel,
                        )
                        if not ok:
                            map_video = None

            if not inter.exists():
                tag = f" ({action} slice, {trim_seconds}s)" if action else ""
                print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}{tag}  encoding ...")
                encode_clip(
                    clip, inter, font_path, use_vt, with_timestamp,
                    gps_dirs, with_speed, map_video=map_video,
                    trim_start=trim_start, trim_seconds=trim_seconds,
                    no_audio=args.no_audio, output_height=args.output_height,
                )
            else:
                print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}  (cached)")
            intermediates.append(inter)

            # After the entry slice of a parking run, splice in the transition.
            if action == "entry":
                trans = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_transition.mp4"
                skipped = skipped_secs_for.get(ci0)
                if not trans.exists():
                    note = (f", ~{_fmt_skip_duration(skipped).replace(' skipped','')} ahead"
                            if skipped else "")
                    print(f"        + transition slide ({TRANSITION_SECS}s{note})")
                    generate_transition_slide(
                        trans, TRANSITION_SECS, font_path, with_map_widget, use_vt,
                        skipped_secs=skipped,
                        output_height=args.output_height,
                        no_audio=args.no_audio,
                    )
                intermediates.append(trans)

            # Remember this clip so the next iteration can measure the gap.
            prev_emitted_clip = clip

        print(f"  concatenating {len(intermediates)} clips -> {final.name}")
        concat_clips(intermediates, final)
        print(f"  ✓ {final}")

        if not args.keep_intermediates:
            for p in intermediates:
                p.unlink(missing_ok=True)
                p.with_suffix(".speed.srt").unlink(missing_ok=True)
                p.with_suffix(".map.mp4").unlink(missing_ok=True)
            # Also clean any *_gap_transition.mp4 files
            for p in work_dir.glob(f"{group_kind}{idx:02d}_clip*_gap_transition.mp4"):
                p.unlink(missing_ok=True)

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
