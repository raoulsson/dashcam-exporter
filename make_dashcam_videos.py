#!/usr/bin/env python3
"""
make_dashcam_videos.py
----------------------
DDPAI dashcam SD card -> one polished MP4 per trip (a trip = leave an anchor,
return to it — or run until a long engine-off gap / the 04:00 day rollover;
see group_into_trips), with all of the following composed in one pass:

  - front camera filling the main 1920x1080 frame (configurable crop)
  - rear camera picture-in-picture (bottom-middle default; top-left/middle/
    right also supported); auto-disabled if no rear footage exists
  - burned-in `YYYY-MM-DD HH:MM:SS` timestamp in the bottom-left
  - per-second GPS speed (km/h or mph) in the bottom-right corner
  - small ©-watermark in any chosen corner (text + size configurable)
  - 480-wide side panel (right or left) with stats + a moving-marker map
    widget that uses real OSM tiles when reachable, PIL fallback otherwise
  - automatic parking-skip: long stationary runs collapse to entry slice +
    "Fast forwarding…" slide + exit slice anchored on actual drive-resume
  - automatic inter-clip-gap detection: engine-off intervals between clips
    get their own "Fast forwarding…" slide with the elapsed time
  - per-trip sidecars: interactive Leaflet .html map, standard .gpx,
    _links.txt with Google/Apple Maps URLs and trip stats, and a _meta.json
    carrying the trip's 04:00-rollover day label for the publishing UI

Runs on macOS (with hardware-accelerated VideoToolbox encoding) and Linux
(with software libx264). Tested on macOS; should work on Linux. Untested
on Windows but should largely work — see the README for caveats.

USAGE
-----
    python3 make_dashcam_videos.py                     # encode every trip on the card
    python3 make_dashcam_videos.py --drives 8          # only trip 8
    python3 make_dashcam_videos.py --dry-run           # list trips without encoding
    python3 make_dashcam_videos.py --sidecars-only     # refresh sidecars without encoding
    python3 make_dashcam_videos.py --print-groups      # same grouping, as JSON on stdout
    python3 make_dashcam_videos.py --force             # overwrite existing .mp4s
    python3 make_dashcam_videos.py --write-config .    # dump a fully commented config.txt

`config.txt` (next to the script, or at --config PATH) overrides built-in
defaults; CLI flags override config. Run with --help for the full flag list.

REQUIREMENTS
------------
    brew install ffmpeg-full   # macOS — needs drawtext + subtitles filters
    pip install -r requirements.txt   # Pillow + staticmap (for the map widget)

The script is restartable: a group whose final .mp4 already exists is
skipped unless --force is passed. Per-clip intermediates in .intermediates/
are SCRATCH — wiped at the start of every run and regenerated against the
current config, so a tweak to head-trim pad / output_height / etc. always
takes effect the next time you re-render. To re-render, delete the final
.mp4 (or pass --force, which does that for you). Harvested GPX in
.gpx_cache/ DOES persist (expensive to redo and unaffected by encoding
config); it's TTL-evicted via --cache-max-age-days.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import atexit
import shutil
import subprocess
import time
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_ROOT = "/Volumes/NO NAME"
DEFAULT_OUT  = "~/dashcam-data/output"       # where the site's `videos` symlink points
                                             # (created on first run, parents included)
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
#out = ~/dashcam-data/output


# ============================================================================
# TRIP GROUPING
# ============================================================================
#
# The publishing unit is a "trip": a park-to-park unit anchored on where the car
# last parked (a configured home is an extra park target).
#   DEPART (drive away from the anchor) -> drive -> ARRIVE + PARK (return and
#   come to a stop). The departure and arrival are found by VIDEO ego-motion, so
#   they land on the real pull-out / pull-in, not on a GPS radius crossing.
# A stop ELSEWHERE (not the anchor) never closes a trip, so A -> B -> hang out
# ANY length of time -> A is ONE trip with the stop at B cut out. Between trips
# the car sits parked at the anchor; those idle clips belong to no trip. A
# ROLLOVER across trip_day_rollover:00 (04:00, not midnight) also force-closes,
# bounding a one-way relocation (drive to a base, sleep, drive back = two trips).
# Every interior stop becomes a 'Fast forwarding...' slide (no duration split).
# Video needs numpy + opencv; without them it falls back to the GPS radius.
#
# Each trip is written as trip_<day>_<HH-MM>_<idx> with a _meta.json sidecar
# carrying its day label, so a publishing UI can group a day's trips together.
#
# HOME LOCATION lives in a gitignored .env (SET_HOME_LAT / SET_HOME_LON /
# SET_HOME_RADIUS_M), NOT here — this file is committed and your home address
# should not be. Copy .env.example to .env and fill it in.

# Return-to-anchor distance in metres. Back within this of where the trip
# started closes the trip. Default 100.
#trip_return_m = 100

# How far (metres) the car must travel from the anchor before a return can
# close the trip — stops it closing on the driveway. Default 150.
#trip_leave_m = 150

# Hour of day the trip/day label rolls over instead of midnight. A trip
# starting before this hour is labelled the previous date. Default 4 (04:00).
#trip_day_rollover = 4

# Minimum distance (metres) the noise-pruned GPS track must reach from the
# anchor for a group to count as a real trip. Closer clusters are near-home
# puttering or parking-mode motion events (and a single phantom GPS fix can't
# fake it — it's dropped as noise first) and are auto-skipped. Default 500.
#trip_min_m = 500


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

# Burn the stats block (Trip title, Distance, Moving, Max speed, Avg, segments
# / GPS points) into the top of the side panel. When false, the panel just
# shows the map widget centred vertically.
#panel_stats = true

# Save .html (Leaflet), .gpx (standard GPX), and _links.txt next to each video.
#map_sidecars = true

# OPT-IN: look up place names for each trip's start/end (OSM Nominatim) and
# add start_place/end_place to _meta.json. Off by default because it calls a
# public, rate-limited service; results are cached in .geocode_cache.json and
# it fails silently offline. All other metadata is computed locally.
#geocode = false

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

# The front camera's resolution is DETECTED from the clips, and the default
# crop is derived from it: whatever height exceeds the 16:9 output aspect is
# removed, split top/bottom (2560x1600 -> 80/80; 1920x1080 -> 0/0, i.e. no
# crop and no vertical stretch). Set these only to override — e.g. to crop
# more bonnet away. Cropping beyond the 16:9 excess WILL stretch the image.
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

# Padding (px) reserved around the route's bounding box inside the map panel.
# Smaller values produce a tighter frame around the route. Default 12.
#map_track_pad = 12

# Bump the auto-chosen OSM tile zoom by this many integer steps. staticmap's
# auto-zoom rounds DOWN to fit the bbox; the smaller map_track_pad already
# tightens the frame, so 0 (default) keeps a comfortable amount of context.
# Set to 1 for ~2x detail (route endpoints may sit right at the panel edges),
# 2 for very tight crops (endpoints may clip outside the panel).
#map_zoom_boost = 0

# Drop GPS segments shorter than this many consecutive fixes (= seconds at the
# dashcam's 1 Hz sample rate). Tiny segments are usually phantom fixes — the
# GPS briefly reports a position kilometers away, then snaps back — and would
# otherwise bloat the map's bounding box so the auto-zoom shows a regional
# view rather than the actual drive. Default 5. Raise to be stricter, lower
# to 1 to keep every fix.
#gps_segment_min_points = 5

# Per-clip GPX time-window. The DDPAI dashcam sometimes dumps stale GPS data
# from a previous drive into the start of a new clip's GPX file (parking-mode
# buffer leftovers). When the parsed points span much more than this many
# seconds, only the densest window of this length is kept — which is the
# actual clip's data. Default 60 (one clip duration). Set to 0 to disable.
#clip_gpx_window_seconds = 60


# ============================================================================
# PARKING SKIP — drop long standstills, replace with a 'Fast forwarding…' slide
# ============================================================================

# Default true: when the car is parked for a long time AND the dashcam keeps
# recording, the script keeps 10s at each end and slides through the middle.
#skip_parking = true

# ADVANCED: how the drive-away after a parking slide is found. Default true =
# video ego-motion (median optical flow; robust to passing people/cars, needs
# numpy + opencv). false = GPS speed only. Most people never touch this; the
# real choice is skip_parking above. Has a real effect, so the knob exists.
#video_drive_detect = true

# Minimum length (s) of a parked run before we trigger the skip.
# 300 = 5 minutes. Shorter values are more aggressive.
#parking_min_secs = 300

# Seconds of "stopped" footage to keep AFTER park onset before the FF slide
# kicks in. Park onset = first sustained drop below ~3 km/h in the smoothed
# GPS speed (detected per-clip via find_park_second). Default 3 keeps a
# brief "you've parked" beat so the FF doesn't feel jarring.
#parking_entry_pad = 3

# How much pre-drive padding to keep AFTER the Fast-forwarding slide.
# Larger = you see more "about to drive" before the actual drive-away.
#parking_exit_pad = 10

# Drive-mode head-trim padding (in seconds). When the first clip of a drive
# has GPS that proves the car only started moving partway in, the video is
# trimmed to start this many seconds BEFORE the detected motion. Default 8 —
# GPS only reports speeds reliably ≥5 km/h, so the car is typically visibly
# rolling for ~3 seconds before GPS catches up; 8s of pad lands you with a
# few visibly-parked seconds before the rollout, which reads as a natural
# "about to drive" intro. Lower for a tighter drop-in, higher for more
# pre-drive context.
#drive_first_clip_pad_secs = 8

# Legacy combined knob — kept for back-compat. If parking_entry_pad /
# parking_exit_pad above aren't set, this value is used for both.
#parking_pad_secs = 5

# Exit-clip handling after a parking gap.
#
# Drive-resume detection looks for N consecutive seconds of GPS motion
# (>5 km/h, see drive_resume_sustain_secs). When found, the exit slice
# anchors `parking_exit_pad` seconds before that moment.
#
# When the exit clip's GPS is too sparse / noisy for drive-resume detection
# to find a sustained moving window (e.g., GPS still acquiring lock at the
# start of a drive), we fall back to seeking this many seconds in. Set to 0
# to play the exit clip from second 0. Default 15 — lands you 10-20 seconds
# after the actual drive-away on a typical 60-second clip, which matches
# what feels natural after the FF transition.
#exit_skip_secs = 15

# How long a moving GPS block must be to count as "real drive" rather than
# parking-mode jitter. Higher = stricter (more often falls through to
# exit_skip_secs). Try 60 if 30 still fires on passing traffic.
#drive_resume_sustain_secs = 30

# Inter-clip gap detection: insert a 'Fast forwarding…' slide whenever the
# wall-clock distance between consecutive clips is longer than this. Catches
# engine-off intervals that AREN'T preceded by parked footage, so the
# parking-run detector doesn't fire for them. Default 60 (1 minute).
#inter_clip_gap_secs = 60

# How long the 'Fast forwarding...' slide stays on screen, in seconds.
# It also reports how much wall-clock time was elided. Default 3.
#transition_secs = 3

# Auto-skip groups that have fewer than this many clips. These are typically
# loop-recording fragments left over after the SD card rolled around — a
# minute or two of footage from a session whose first portion has been
# overwritten. To force-encode a fragment, pass its index via --drives (the
# min-clips check is bypassed when --drives is explicit).
#min_clips_per_group = 4

# Cache housekeeping: files in .gpx_cache/ older than this many days are
# auto-deleted at the start of each run. Stops the disk from filling up
# silently over weeks of usage. Set to 0 to disable. (.intermediates/ is
# always wiped at the start of every run; it's scratch, not cache.)
#cache_max_age_days = 20


# ============================================================================
# OUTPUT SIZE / QUALITY
# ============================================================================

# Final-output downscale of the composite for web/mobile delivery.
# 1080 (default) is native — no downscale at all, the full 2402x1080
# composite (~3.5–4 GB per hour of source, archive size). 720 is the
# quality / size sweet spot, still detail-rich enough to read plates and
# signs (~1.5–2 GB per hour); 540 is web/phone-friendly (~400–500 MB per
# hour). Aspect ratio is preserved; VT bitrate auto-scales.
#output_height = 1080

# Encoder selection. libx264 (software) is the DEFAULT: ~2-3x smaller files than
# hardware VideoToolbox at the same quality (far better bits-per-pixel via CRF),
# and portable. Trade-off: slower to encode. Set false to use Mac hardware (bigger
# files, much faster). For a further ~2x, also set output_height = 720 above.
software = true

# Keep the per-clip intermediate .mp4 files after concat.
#keep_intermediates = false

# Hardware H.264 (VideoToolbox) bitrates. These are tuned for 1080p; when
# output_height is lower, both values are scaled down by (output_height/1080)^2
# automatically (e.g. 540p → 2M / 2.5M) so smaller frames produce smaller
# files instead of over-allocating bits. Libx264 uses CRF and self-adjusts.
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
DEFAULT_PARKING_ENTRY_PAD   = 3      # seconds AFTER park onset, before FF
DEFAULT_PARKING_EXIT_PAD    = 10     # how many seconds of footage precede drive-resume after the FF
# Standard exit-slice skip after a parking gap. The exit slice trims this
# many seconds off the head of the first clip. Drive-resume detection
# refines it when GPS clearly shows >=30s of continuous motion; otherwise
# this value is used as-is. Most parking-mode dashcams produce GPS data
# that's too scrambled to detect reliably (random motion-triggered bursts
# of "passing car / walking pedestrian" wakeups), so this is the value
# you'll see most of the time.
DEFAULT_EXIT_SKIP_SECS      = 15
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
# Cache housekeeping: files in .gpx_cache/ and .intermediates/ older than this
# many days get auto-deleted at the start of each run. Stops the user's disk
# from filling up silently when they encode many days over weeks of usage.
# Set to 0 to disable the TTL eviction.
DEFAULT_CACHE_MAX_AGE_DAYS  = 20
TRANSITION_SECS             = 3      # length of the "Fast forwarding..." slide
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
REAR_PAIR_TOLERANCE_S = 2   # pair front↔rear when their timestamps differ by ≤ this
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


def probe_video_size(path: Path) -> "tuple[int, int] | None":
    """(width, height) of a video's first stream via ffprobe, or None."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            text=True, stderr=subprocess.STDOUT,
        ).strip().splitlines()[0]
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


_FFMPEG_VERSION: "str | None" = None


def ffmpeg_version() -> str:
    """Short ffmpeg version string (e.g. '7.1'), probed once per run."""
    global _FFMPEG_VERSION
    if _FFMPEG_VERSION is None:
        try:
            first = subprocess.check_output(
                ["ffmpeg", "-version"], text=True, stderr=subprocess.STDOUT
            ).splitlines()[0]
            # "ffmpeg version 7.1 Copyright (c) ..." -> "7.1"
            parts = first.split()
            _FFMPEG_VERSION = parts[2] if len(parts) > 2 else "unknown"
        except Exception:
            _FFMPEG_VERSION = "unknown"
    return _FFMPEG_VERSION


def reverse_geocode(lat: float, lon: float, cache_path: Path) -> "str | None":
    """Best-effort place name for a coordinate via OSM Nominatim. OPT-IN only
    (--geocode): it calls a public service, so it must never be on the critical
    path of a render. Results are cached on disk (coordinates rounded to ~11 m)
    because Nominatim's usage policy asks for caching and max 1 req/sec, and a
    day of trips would otherwise re-ask for the same driveway repeatedly.
    Returns None on any failure — no network, rate limit, malformed reply."""
    key = f"{lat:.4f},{lon:.4f}"
    cache: dict = {}
    try:
        if cache_path.is_file():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if key in cache:
        return cache[key]
    try:
        import urllib.request
        import urllib.parse
        qs = urllib.parse.urlencode({
            "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
            "format": "jsonv2", "zoom": "16", "addressdetails": "1",
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{qs}",
            headers={"User-Agent": "dashcam-exporter/1.0 (github.com/raoulsson/dashcam-exporter)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        a = data.get("address", {}) or {}
        # Build a short "neighbourhood, city" style label rather than the full
        # postal address, which is long and mostly noise for a trip label.
        near = (a.get("neighbourhood") or a.get("suburb") or a.get("village")
                or a.get("hamlet") or a.get("road"))
        city = (a.get("city") or a.get("town") or a.get("municipality")
                or a.get("county"))
        name = ", ".join(x for x in (near, city) if x) or data.get("display_name")
    except Exception:
        return None
    cache[key] = name
    try:
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    time.sleep(1.0)          # Nominatim asks for <= 1 request/second
    return name


def find_clips(front_dir: Path, rear_dir: Path | None) -> list[Clip]:
    front_map: dict[str, tuple[Path, int]] = {}
    for f in sorted(os.listdir(front_dir)):
        m = FRONT_RE.match(f)
        if m:
            front_map[m.group(1)] = (front_dir / f, int(m.group(2)))

    rear_map: dict[str, Path] = {}
    rear_by_epoch: dict[int, Path] = {}
    if rear_dir is not None and rear_dir.is_dir():
        for f in sorted(os.listdir(rear_dir)):
            m = REAR_RE.match(f)
            if m:
                rts = m.group(1)
                rear_map[rts] = rear_dir / f
                rear_by_epoch[calendar.timegm(
                    datetime.strptime(rts, "%Y%m%d%H%M%S").timetuple())] = rear_dir / f

    clips: list[Clip] = []
    n_no_rear = 0
    for ts in sorted(front_map):
        path_f, dur = front_map[ts]
        epoch = calendar.timegm(datetime.strptime(ts, "%Y%m%d%H%M%S").timetuple())
        rear_path: Path | None = rear_map.get(ts)
        if rear_path is None and rear_by_epoch:
            # DDPAI sometimes writes the rear file 1-2s off the front's second
            # (e.g. front 18:17:55, rear 18:17:56). Exact-timestamp matching then
            # reads a real rear clip as "missing", dropping its PiP. Pair with the
            # nearest rear within REAR_PAIR_TOLERANCE_S before giving up. (Clips
            # are ~60s apart, so this window can't mis-pair adjacent clips.)
            _best = min(rear_by_epoch, key=lambda e: abs(e - epoch))
            if abs(_best - epoch) <= REAR_PAIR_TOLERANCE_S:
                rear_path = rear_by_epoch[_best]
        if REAR_PIP_ENABLED and rear_dir is not None and rear_path is None:
            # The rear cam is on for the rest of the card, but THIS clip has no
            # rear file (rear disconnected, or its file was loop-overwritten).
            # Keep the clip and render it front-only — dropping it would throw
            # away real footage (a whole day, on some cards). build_filter_complex
            # branches on the per-clip `with_rear`, so the PiP is simply omitted.
            n_no_rear += 1
        clips.append(Clip(ts, epoch, dur, path_f, rear_path))
    if n_no_rear:
        print(f"  note: {n_no_rear} clip(s) have no rear pair — rendered "
              f"front-only (no PiP)")
    return clips


# ---------------------------------------------------------------------------
# Trip grouping — the publishing unit
# ---------------------------------------------------------------------------
# A "trip" is not a single engine-on session (that's a gap-based drive). A trip
# boundary is a PARK at the anchor — the car actually coming to rest where it
# started — NOT a mere radius crossing. The anchor is where the car last parked
# (carried forward; a configured `home` is an extra always-valid park target).
# A trip is:
#
#     DEPART (start driving away from the anchor)
#       → drive — any interior stop (fuel, lunch, a 4-hour hangout at B, a hike)
#                 stays in the trip as a 'Fast forwarding…' slide, no matter how
#                 long, because B is not the anchor. So A → B → hang out → A is
#                 ONE trip with the stop at B cut out.
#       → ARRIVE + PARK (return to the anchor/home and come to a stop)
#
#   * Departure and arrival are found by VIDEO ego-motion (drive-away = flow
#     rises; park = flow falls to baseline and stays). That's why boundaries
#     land on the real pull-away / pull-in, not 10-15s early (radius entry while
#     still rolling) and not split by near-home maneuvering (which crosses the
#     radius without parking). Between trips the car sits IDLE at the anchor —
#     those clips belong to no trip.
#   * ROLLOVER (crossing `rollover_h`:00, default 04:00 not midnight) still
#     force-closes a trip, bounding a ONE-WAY relocation (drive to a holiday
#     base, sleep, drive back days later = two trips).
#   * Without OpenCV/numpy it degrades to the old radius-entry boundary.
DEFAULT_TRIP_RETURN_M     = 100     # back within this of anchor => trip closes
DEFAULT_TRIP_LEAVE_M      = 150     # must get this far out before a return counts
DEFAULT_TRIP_DAY_ROLLOVER = 4       # trips/days roll over at 04:00, not midnight
DEFAULT_HOME_RADIUS_M     = 100     # parking within this of `home` => hard boundary
DEFAULT_TRIP_MIN_M        = 500     # clean track must reach this far => real trip


def trip_day_label(dt: datetime, rollover_h: int = DEFAULT_TRIP_DAY_ROLLOVER) -> str:
    """The calendar day a trip belongs to, with the rollover at `rollover_h`:00
    instead of midnight. A trip starting 02:00 belongs to the PREVIOUS date; one
    starting 17:00 keeps its date even if it runs past midnight to 03:32."""
    return (dt - timedelta(hours=rollover_h)).strftime("%Y-%m-%d")


def _crosses_rollover(a: datetime, b: datetime, rollover_h: int) -> bool:
    """True if a `rollover_h`:00 day boundary falls in the interval (a, b]."""
    return trip_day_label(a, rollover_h) != trip_day_label(b, rollover_h)


def _clip_endpoints(clip: Clip, gps_dirs: "tuple[Path | None, ...]"):
    """(first_fix, last_fix) each as (lat, lon), or (None, None) if the clip has
    no GPS. Used only for trip boundary detection, so raw first/last valid fixes
    are precise enough (parse_gpx_track already strips stale cross-drive data)."""
    pts = gather_track([clip], gps_dirs)
    if not pts:
        return None, None
    return (pts[0][0], pts[0][1]), (pts[-1][0], pts[-1][1])


def group_into_trips(
    clips: list[Clip],
    gps_dirs: "tuple[Path | None, ...]",
    *,
    return_m: float = DEFAULT_TRIP_RETURN_M,
    leave_m: float = DEFAULT_TRIP_LEAVE_M,
    rollover_h: int = DEFAULT_TRIP_DAY_ROLLOVER,
    home: "tuple[float, float] | None" = None,
    home_radius_m: float = DEFAULT_HOME_RADIUS_M,
    min_trip_m: float = DEFAULT_TRIP_MIN_M,
    use_video: bool = True,
) -> "tuple[list[list[Clip]], list[bool]]":
    """Segment chronologically-ordered clips into trips.

    A trip boundary is a PARK at the anchor, not a mere radius crossing. The
    anchor is where the car was last parked (carried forward; `home` is an extra
    always-valid park target). Between trips the car sits IDLE at the anchor —
    those clips belong to no trip. A trip is:

        DEPART (car starts driving away from the anchor)
          → drive (interior stops elsewhere stay in the trip as FF slides)
          → ARRIVE+PARK (car returns to the anchor/home and comes to a stop)

    Departure and arrival are found by VIDEO ego-motion (find_drive_away_by_video
    / find_park_second_by_video), which is why the boundaries land on the real
    pull-away and pull-in rather than 10-15s early (radius entry while still
    rolling) or split by near-home maneuvering. GPS position only gates WHICH
    clips get the (cheap-ish) video check — those near the anchor. ROLLOVER
    (crossing `rollover_h`:00) still force-closes a trip, bounding one-way
    relocations. Without OpenCV/numpy (or use_video=False) it degrades to the
    old radius-entry behaviour.

    Returns (trips, moved); moved[i] is False for a trip whose NOISE-PRUNED track
    never reaches `min_trip_m` from its anchor (near-home puttering / parking-mode
    events / a lone phantom GPS jump) — the caller auto-skips those."""
    def dist_m(a, b) -> float:
        return _haversine_km(a[0], a[1], b[0], b[1]) * 1000.0

    n = len(clips)
    if n == 0:
        return [], []
    # Announce each clip as it is read. This loop is the long silent stretch of a
    # scan — minutes on a full card — and without a line per clip any progress
    # display upstream has nothing to show but the directory it started on, which
    # is indistinguishable from being wedged.
    eps = []
    _tty = sys.stdout.isatty()
    for i, c in enumerate(clips, 1):
        # On a terminal, redraw one line: 239 clips scrolling past is noise that
        # buries whatever was printed before the scan started. When piped (the
        # pipeline CLI, a log file) emit real lines instead — a consumer needs
        # them separable, and a log wants the history.
        line = f"[scan {i:4d}/{len(clips)}] {c.front.name}"
        print(line + ("\r" if _tty else "\n"), end="", flush=True)
        eps.append(_clip_endpoints(c, gps_dirs))
    if _tty and clips:
        # leave the line clean so the next print does not land on a stale tail
        print(" " * 78 + "\r", end="", flush=True)
    video = use_video and _HAVE_EGO

    def min_dist(idx, anchor):
        if anchor is None:
            return None
        ds = [dist_m(anchor, f) for f in eps[idx] if f is not None]
        return min(ds) if ds else None

    def last_fix(idx):
        s, e = eps[idx]
        return e if e is not None else s

    def rollover_before(idx):
        if idx <= 0:
            return False
        pe = clips[idx - 1].dt + timedelta(seconds=clips[idx - 1].duration)
        return _crosses_rollover(pe, clips[idx].dt, rollover_h)

    def within_target(idx, anchor):
        for t, r in ((anchor, return_m), (home, home_radius_m)):
            if t is None:
                continue
            for f in eps[idx]:
                if f is not None and dist_m(t, f) <= r:
                    return True
        return False

    def parks_here(idx, anchor):
        # Car parks at the anchor/home within this clip? Requires GPS within the
        # radius AND (video) a sustained stop. No video -> radius entry counts.
        if not within_target(idx, anchor):
            return False
        if not video:
            return True
        return find_park_second_by_video(clips[idx]) is not None

    def departs_here(idx, anchor):
        # Car starts driving away in this clip (ends the IDLE gap between trips)?
        if video:
            return find_drive_away_by_video(clips[idx]) is not None
        d = min_dist(idx, anchor)
        return d is None or d > leave_m

    def is_moved(lo, hi, anc):
        pts = gather_track(clips[lo:hi], gps_dirs)
        if not pts or anc is None:
            return True
        pruned = [p for seg in segment_track(pts) for p in seg]
        if not pruned:
            return True
        return any(dist_m(anc, (p[0], p[1])) > min_trip_m for p in pruned)

    trips: list[list[Clip]] = []
    moved: list[bool] = []
    carry: "tuple[float, float] | None" = None   # where the car last parked
    i = 0
    while i < n:
        anchor = carry
        # --- IDLE: skip clips parked at the anchor until the next departure. ---
        # The first trip (carry is None) starts at clip 0 — the import begins at
        # a departure — and any leading parked footage is trimmed by the render
        # head-trim. A rollover during idle doesn't matter: parked is parked.
        if anchor is not None:
            while i < n and not departs_here(i, anchor):
                i += 1
            if i >= n:
                break
        start = i
        if anchor is None:
            anchor = eps[start][0] if eps[start][0] is not None else last_fix(start)
        # --- DRIVING: accumulate until arrival-park at the anchor, or rollover. -
        left = False
        end = None
        j = start
        while j < n:
            if j > start and rollover_before(j):
                break
            d = min_dist(j, anchor)
            if not left and d is not None and d > leave_m:
                left = True
            if left and parks_here(j, anchor):
                lf = last_fix(j)
                if lf is not None:
                    carry = lf
                end = j + 1
                break
            j += 1
        if end is None:                  # rollover / end of clips (one-way trip)
            end = j
            lf = last_fix(end - 1) if end > start else None
            if lf is not None:
                carry = lf
        moved.append(is_moved(start, end, anchor))
        trips.append(clips[start:end])
        i = end
    return trips, moved


def has_videotoolbox() -> bool:
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT, text=True,
        )
        return "h264_videotoolbox" in out
    except Exception:
        return False


def _scale_bitrate_string(bitrate: str, output_height: int) -> str:
    """
    Scale a VideoToolbox-style bitrate string (e.g. '8M', '500k') to match the
    output resolution. 8 Mbps is right for 1080p, but at 540p it over-encodes
    by ~4x — pixel count scales with height squared (16:9 aspect).
    Returns the original string if scaling isn't applicable.
    """
    if not output_height or output_height == OUT_H:
        return bitrate
    m = re.match(r"^(\d+(?:\.\d+)?)([KkMm]?)$", bitrate.strip())
    if not m:
        return bitrate
    val = float(m.group(1))
    unit = m.group(2).upper()
    kbps = val * (1000 if unit == "M" else 1)
    scaled = max(500, int(round(kbps * (output_height / OUT_H) ** 2)))
    if scaled >= 1000:
        whole = scaled // 1000
        return f"{whole}M" if scaled % 1000 == 0 else f"{scaled / 1000:.1f}M"
    return f"{scaled}k"


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


def _parse_camtime_header(gpx_path: Path) -> datetime | None:
    """
    Read the DDPAI-specific `$GPSCAMTIME YYYYMMDDhhmmss` header at the top of
    a GPX file. The value is the dashcam's LOCAL wall-clock at the moment GPS
    first reported a fix in this clip — which lets us compute the exact
    LOCAL↔UTC offset for this device even when the dashcam's display clock
    has drifted (a real-world case: the file we tested had a 7:56:46 offset
    rather than a clean +8h timezone).
    """
    try:
        with gpx_path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("$GPSCAMTIME"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return datetime.strptime(parts[1], "%Y%m%d%H%M%S")
                        except ValueError:
                            return None
                # Header is right at the top; bail as soon as we hit GPS data.
                if line.startswith("$GPRMC") or line.startswith("$GPGGA"):
                    return None
    except OSError:
        pass
    return None


def parse_clip_speeds(clip: "Clip", gps_dirs: tuple[Path | None, ...]) -> list[float]:
    """
    Per-second km/h aligned to the clip's VIDEO timeline (not the GPS-fix
    index). Three real-world wrinkles handled here:

    1) GPS-lock-acquisition lag. Dashcam often starts recording a few seconds
       before GPS reports its first fix. Those leading video seconds get a
       0 km/h placeholder.
    2) Mid-clip GPS dropouts. GPS can lose lock briefly (tunnel, urban
       canyon, parking ceiling). Affected seconds get the previous-known
       speed forward-filled, rather than the old behaviour of collapsing
       the gap and shifting every subsequent speed earlier.
    3) DDPAI dashcam clock drift. The wall-clock burned into the video can
       be offset from GPS UTC by a non-integer-hour amount, so mod-3600
       lag detection isn't enough. The `$GPSCAMTIME` header at the top of
       the GPX file gives the exact LOCAL↔UTC offset for the device.

    The combination of all three was producing speeds that ran ~10+ seconds
    AHEAD of the video — the visible "speed already 14 km/h while wheels
    haven't moved yet" symptom. After this fix, speeds[i] is the GPS reading
    at the SAME video-second the user sees burned-in on the timestamp watermark.
    """
    gpx = find_gpx_for(clip.timestamp, *gps_dirs)
    if gpx is None:
        return []
    points = parse_gpx_track(gpx)
    if not points:
        return []
    try:
        clip_dt = datetime.strptime(clip.timestamp, "%Y%m%d%H%M%S")
    except ValueError:
        return [p[2] for p in points]

    # Prefer the exact `$GPSCAMTIME` offset when DDPAI writes it; fall back
    # to the old mod-3600 lag-prepend behaviour for non-DDPAI files.
    camtime_local = _parse_camtime_header(gpx)
    if camtime_local is not None:
        # offset = LOCAL_at_first_fix - UTC_at_first_fix
        offset = camtime_local - points[0][3]
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


# How long (seconds) a single clip's GPX file is expected to span. The DDPAI
# dashcam occasionally dumps stale GPS data from a previous drive into the
# first clip of a new drive (parking-mode buffer leftovers). When the parsed
# points span much more than this, parse_gpx_track keeps only the densest
# window of this many seconds — which is the actual clip's data.
CLIP_GPX_WINDOW_SECONDS = 60


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

def find_drive_resume_in_group(
    head_clips: list[Clip],
    gps_dirs: tuple[Path | None, ...],
    sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
    threshold_kmh: float = DRIVE_RESUME_THRESHOLD_KMH,
) -> tuple[int, int] | None:
    """
    Scan the speeds of the first few clips of a group, concatenated, to find
    the first index at which `sustain_secs` consecutive samples are all above
    `threshold_kmh`. Returns (clip_index, offset_within_clip) where the
    sustained motion begins, or None if no such window exists.

    The speeds are sourced via parse_clip_speeds so they're already aligned
    to each clip's VIDEO timeline (with leading zeros prepended for any
    GPS-acquisition lag at the start of a clip). That means the returned
    offset_within_clip can be used directly as a trim_start for that clip.

    Use case: drive-mode head-trim where the car may start moving in clip 0,
    clip 1, or clip 2 of a drive. The caller can drop earlier clips entirely
    via action_for[k] = "head_skip", then trim the clip containing motion to
    start `pad` seconds before offset_within_clip.
    """
    speeds: list[float] = []
    boundaries: list[int] = [0]    # cumulative speed-count after each clip
    for c in head_clips:
        clip_speeds = parse_clip_speeds(c, gps_dirs)
        if not clip_speeds:
            # Gap in GPS coverage — can't reliably scan past this point.
            break
        speeds.extend(clip_speeds)
        boundaries.append(len(speeds))
    if len(speeds) < sustain_secs:
        return None
    for i in range(len(speeds) - sustain_secs + 1):
        if all(speeds[i + j] > threshold_kmh for j in range(sustain_secs)):
            # Map global index i back to (clip_index, offset_within_clip).
            for ci in range(len(boundaries) - 1):
                if boundaries[ci] <= i < boundaries[ci + 1]:
                    return ci, i - boundaries[ci]
            return None
    return None


def find_drive_resume_second(
    clip: Clip, gps_dirs: tuple[Path | None, ...],
    sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
    threshold_kmh: float = DRIVE_RESUME_THRESHOLD_KMH,
    next_clips: list[Clip] | None = None,
) -> int | None:
    """
    Best-effort detection of when the car actually starts moving in `clip`.
    Returns the clip-second at which a sustained `sustain_secs`-long moving
    window begins, or None if no such window exists (in which case the GPS
    data is too noisy / scrambled to trust — caller falls back to a
    configurable skip).

    The 30-second default sustain is intentional: parking-mode dashcams
    record short bursts of motion-triggered video around a parked car (a
    passing car, a pedestrian, dashcam reboot self-tests) which produce
    brief GPS spikes that don't represent real driving. 30 seconds of
    continuous motion is a solid indicator that the drive has actually
    started.

    If `next_clips` is provided, their speeds are concatenated onto this
    clip's, so a sustain window that STARTS in this clip and continues into
    the next one(s) still counts. The returned index stays in this clip's
    timeline — clamped to clip.duration - 1 if motion starts at the very
    end of the clip — so the caller can still trim from second N onward.
    Without this, a drive that starts at e.g. second 40 of a 60-second clip
    can never satisfy a 30-second-within-one-clip rule and the head-trim
    silently fails open (showing the whole pre-drive pause).
    """
    # Use parse_clip_speeds so the returned index is already in VIDEO-second
    # space (with leading 0s prepended for any GPS-lock acquisition lag).
    # That way the caller's trim_start = max(0, drive_sec - pad) lands on
    # the correct video frame, not on the GPS-fix index — which would
    # otherwise drop into action ~10 seconds before the wheels actually move.
    speeds: list[float] = parse_clip_speeds(clip, gps_dirs)
    if not speeds:
        return None
    clip_len = len(speeds)
    if next_clips:
        for nc in next_clips:
            nspeeds = parse_clip_speeds(nc, gps_dirs)
            if not nspeeds:
                break  # gap — don't pretend the next-next clip is contiguous
            speeds.extend(nspeeds)
    if len(speeds) < sustain_secs:
        return None
    for i in range(len(speeds) - sustain_secs + 1):
        if all(speeds[i + j] > threshold_kmh for j in range(sustain_secs)):
            # Clamp to this clip's timeline so the caller's trim_start
            # stays a valid offset inside this clip's source video.
            return min(i, max(0, clip_len - 1))
    return None


# --- Video-based drive-away detection (parking exit) -------------------------
# GPS speed is unreliable for finding the moment a parked car starts driving:
# parking-mode clips are event snippets (the cam records when a person or car
# passes), so the footage is full of OTHER things moving while the car sits
# still, and the stale/jittery GPS can't tell that apart from real driving.
# Instead, measure EGO-motion from the front video: track many features frame to
# frame (Lucas-Kanade optical flow) and take the MEDIAN flow magnitude. When the
# car is parked, most features are on the static scene (median ~0) and a passing
# car/person is just a handful of outliers the median ignores. When the car
# actually drives, the WHOLE scene sweeps (features flow outward even driving
# straight, translate/rotate when maneuvering out of a spot), so the median
# jumps by ~two orders of magnitude. We find the first sustained jump, then walk
# back to where the motion first left the parked baseline = the drive-away.
EGO_FPS            = 4        # frames/sec sampled from the clip for analysis
EGO_W, EGO_H       = 640, 400  # downscaled analysis resolution (speed)
EGO_SUSTAIN_SECS   = 1.5     # motion must persist this long to count as driving
EGO_THR_SUSTAIN    = 1.0     # median flow (px at EGO_W×EGO_H) => "driving"
EGO_THR_BASELINE   = 0.15    # walk-back stops below this (parked-noise floor)
EGO_CONTEXT_PAD    = 2       # seconds of "about to move" kept before drive-away
EGO_END_PAD        = 10      # seconds kept after the car finally comes to rest
EGO_MAX_ANALYZE_SECS = 120   # cap analysis (a clip is ≤60s, but be safe)

try:
    import numpy as _np
    import cv2 as _cv2
    _HAVE_EGO = True
except Exception:
    _HAVE_EGO = False


def _ego_extract_frames(clip: Clip):
    """Sampled greyscale frames of a clip as an (n, H, W) uint8 array, or None."""
    if not _HAVE_EGO:
        return None
    cmd = ["ffmpeg", "-v", "error", "-i", str(clip.front),
           "-t", str(EGO_MAX_ANALYZE_SECS),
           "-vf", f"fps={EGO_FPS},scale={EGO_W}:{EGO_H},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True).stdout
    except Exception:
        return None
    fsz = EGO_W * EGO_H
    n = len(raw) // fsz
    if n < 1:
        return None
    return _np.frombuffer(raw[:n * fsz], dtype=_np.uint8).reshape(n, EGO_H, EGO_W)


def _ego_median_flow(frames) -> "list[float]":
    """Median Lucas-Kanade optical-flow magnitude between consecutive frames.
    Index i is the motion from frame i-1 to i; index 0 is 0. Median rejects the
    handful of features on passing objects, so it tracks WHOLE-frame ego-motion:
    ~0 parked, large when the car actually drives."""
    lk = dict(winSize=(21, 21), maxLevel=3,
              criteria=(_cv2.TERM_CRITERIA_EPS | _cv2.TERM_CRITERIA_COUNT, 20, 0.03))
    n = len(frames)
    med = [0.0] * n
    prev = frames[0]
    for i in range(1, n):
        cur = frames[i]
        p0 = _cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01, minDistance=8)
        if p0 is not None:
            p1, stt, _err = _cv2.calcOpticalFlowPyrLK(prev, cur, p0, None, **lk)
            g = stt.ravel() == 1
            if g.sum() >= 5:
                d = (p1[g] - p0[g]).reshape(-1, 2)
                med[i] = float(_np.median(_np.hypot(d[:, 0], d[:, 1])))
        prev = cur
    return med


def _ego_drive_onset(med: "list[float]") -> "int | None":
    """First frame index of sustained driving in a median-flow signal, walked
    back to where motion left the parked baseline. None if never driving."""
    n = len(med)
    sustain = max(1, int(round(EGO_SUSTAIN_SECS * EGO_FPS)))
    run_start = None
    for i in range(1, n - sustain + 1):
        if all(med[i + j] > EGO_THR_SUSTAIN for j in range(sustain)):
            run_start = i
            break
    if run_start is None:
        return None
    onset = run_start
    while onset > 1 and med[onset - 1] > EGO_THR_BASELINE:
        onset -= 1
    return onset


def find_drive_away_by_video(clip: Clip) -> float | None:
    """Video-second within `clip` at which the car starts driving (ego-motion),
    robust to passing people/cars; None if unavailable or no motion found."""
    frames = _ego_extract_frames(clip)
    if frames is None or len(frames) < 4:
        return None
    onset = _ego_drive_onset(_ego_median_flow(frames))
    return None if onset is None else max(0.0, onset / EGO_FPS)


def find_drive_away_in_group_video(clips: "list[Clip]") -> "tuple[int, float] | None":
    """Like find_drive_away_by_video but across the first few clips of a trip:
    returns (clip_index, second_within_that_clip) of the departure, so a
    head-trim can drop earlier parked clips and trim into the motion clip.
    Used for the trip START (mirror of the parking-exit case)."""
    if not _HAVE_EGO:
        return None
    allf = []
    bounds = [0]
    for c in clips:
        f = _ego_extract_frames(c)
        if f is None or len(f) == 0:
            break  # gap in coverage — don't fuse a non-contiguous clip
        allf.append(f)
        bounds.append(bounds[-1] + len(f))
    if not allf:
        return None
    frames = _np.concatenate(allf, axis=0)
    if len(frames) < 4:
        return None
    onset = _ego_drive_onset(_ego_median_flow(frames))
    if onset is None:
        return None
    for ci in range(len(bounds) - 1):
        if bounds[ci] <= onset < bounds[ci + 1]:
            return ci, (onset - bounds[ci]) / EGO_FPS
    return None


def _ego_park_onset(med: "list[float]") -> "int | None":
    """Frame index at which the car comes to a sustained STOP that lasts to the
    end of the clip (the mirror of _ego_drive_onset): the clip must contain real
    driving first, then motion drops to the parked baseline and stays there.
    None if the clip never drove, or is still driving at its end (no arrival)."""
    n = len(med)
    sustain = max(1, int(round(EGO_SUSTAIN_SECS * EGO_FPS)))
    if n < sustain + 1 or max(med) <= EGO_THR_SUSTAIN:
        return None                       # never really drove -> not an arrival
    # Anchor on the end of the last SUSTAINED driving run, not on the last
    # sample above a low baseline. Once parked, the flow signal still twitches:
    # a pedestrian crossing in front of the car produced blips of 0.15-0.44 for
    # ~15s after a full stop, and a single-frame spike of 26.9 appeared at the
    # very end of another clip (door/headlights). Walking back on the baseline
    # let any one of those veto the stop, reporting the park up to 15 seconds
    # late — or, with a trailing spike, not at all. Requiring `sustain`
    # consecutive frames of real motion ignores both.
    last_drive_end = None
    for i in range(n - 1, sustain - 2, -1):
        if all(med[i - j] > EGO_THR_SUSTAIN for j in range(sustain)):
            last_drive_end = i
            break
    if last_drive_end is None:
        return None
    park = last_drive_end + 1
    if park >= n or (n - park) < sustain:
        return None                       # still driving at the end (no park)
    return park


def find_park_second_by_gps(
    clip: Clip, gps_dirs: "tuple[Path | None, ...]",
    threshold_kmh: float = PARKING_SPEED_THRESHOLD_KMH,
) -> float | None:
    """GPS-speed fallback for park detection: the video-second at which speed
    drops below `threshold_kmh` and STAYS there through the end of the clip.

    Video ego-motion is the primary detector, but it needs trackable features —
    a night scene can be too dark to yield any, so it reports "still moving"
    for a car that has plainly stopped. GPS has the opposite bias: it is poor at
    spotting the START of motion (passing traffic, slow creep below the speed
    floor) but perfectly good at confirming a sustained standstill. Same shape
    as _ego_park_onset: the clip must contain real motion first, then settle."""
    speeds = parse_clip_speeds(clip, gps_dirs)
    if not speeds:
        return None
    n = len(speeds)
    sustain = max(1, int(round(EGO_SUSTAIN_SECS)))
    if n < sustain + 1 or max(speeds) <= threshold_kmh:
        return None                       # never really drove -> not an arrival
    k = n - 1
    while k >= 1 and speeds[k] < threshold_kmh:
        k -= 1
    park = k + 1
    if park >= n or (n - park) < sustain:
        return None                       # still moving at the end (no park)
    return float(park)


def find_park_second_by_video(clip: Clip) -> float | None:
    """Video-second within `clip` at which the car parks (drives in, then comes
    to a sustained stop through the end of the clip). None if it doesn't park
    here (still moving) or video is unavailable. Used to close a trip at the
    real arrival, not merely on entering the anchor radius."""
    frames = _ego_extract_frames(clip)
    if frames is None or len(frames) < 4:
        return None
    onset = _ego_park_onset(_ego_median_flow(frames))
    return None if onset is None else max(0.0, onset / EGO_FPS)


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


def _filter_gps_outliers(speeds: list[float], max_delta_kmh: float = 30.0) -> list[float]:
    """
    Replace single-sample GPS noise spikes — a sample that differs from BOTH
    immediate neighbours by more than `max_delta_kmh` — with the mean of its
    neighbours. Catches the classic "parked car suddenly reports 80 km/h for
    one second" failure mode that breaks park-onset detection.
    """
    if len(speeds) < 3:
        return list(speeds)
    out = list(speeds)
    for i in range(1, len(speeds) - 1):
        prev, cur, nxt = speeds[i - 1], speeds[i], speeds[i + 1]
        if abs(cur - prev) > max_delta_kmh and abs(cur - nxt) > max_delta_kmh:
            out[i] = (prev + nxt) / 2.0
    return out


def _smooth_speeds(speeds: list[float], window: int = 5) -> list[float]:
    """
    Centre-aligned moving-average smoothing. Returns a same-length list. At
    the edges the window is truncated to the available samples (no padding).
    Used to extract the "middle line" from noisy GPS, so park-onset
    detection isn't tripped by transient overshoots and undershoots.
    """
    n = len(speeds)
    if n < 2:
        return list(speeds)
    half = window // 2
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(speeds[lo:hi]) / (hi - lo))
    return out


def find_park_second(
    clip: Clip,
    gps_dirs: tuple[Path | None, ...],
    sustain_secs: int = 30,
    threshold_kmh: float = PARKING_SPEED_THRESHOLD_KMH,
    next_clips: list[Clip] | None = None,
) -> int | None:
    """
    First clip-second at which the smoothed GPS speed drops below
    `threshold_kmh` and STAYS below for `sustain_secs` consecutive seconds.
    Returns the clip-second of the park onset (relative to this clip's
    video timeline), or None if no sustained drop exists across the clip
    + any lookahead clips.

    Outliers are filtered and the series is moving-averaged before the
    sustain check, so a single noise spike up to 80 km/h on a parked car
    doesn't push park onset 5 minutes later than it really happened.
    """
    speeds = parse_clip_speeds(clip, gps_dirs)
    if not speeds:
        return None
    clip_len = len(speeds)
    if next_clips:
        for nc in next_clips:
            nspeeds = parse_clip_speeds(nc, gps_dirs)
            if not nspeeds:
                break
            speeds.extend(nspeeds)
    filtered = _filter_gps_outliers(speeds)
    smoothed = _smooth_speeds(filtered)
    if len(smoothed) < sustain_secs:
        return None
    for i in range(len(smoothed) - sustain_secs + 1):
        if all(smoothed[i + j] < threshold_kmh for j in range(sustain_secs)):
            # Clamp the returned index to THIS clip's timeline — the caller
            # uses it as a trim_seconds offset within the entry clip's source.
            return min(i, max(0, clip_len - 1))
    return None


def find_parking_runs(
    group: list[Clip],
    gps_dirs: tuple[Path | None, ...],
    min_run_secs: int,
) -> list[tuple[int, int, int]]:
    """
    Find runs of parking and return a list of
    `(entry_idx, entry_park_sec, end_idx)` tuples.

    `entry_idx` is the group-index of the clip in which the park onset is
    detected (via find_park_second). `entry_park_sec` is the clip-second
    at which sustained low-speed begins inside that clip — could be 0
    (clip was already parked from frame 1) or up to clip.duration - 1
    (parking onset late in the clip). `end_idx` is the last clip of the
    parking run (subsequent clip = parking exit).

    Total stopped duration includes the partial entry-clip tail, the fully
    parked clips, AND the wall-clock engine-off gap to the next moving
    clip. Threshold passed only if `total >= min_run_secs`.
    """
    runs: list[tuple[int, int, int]] = []
    verbose = os.environ.get("PARKING_DEBUG") == "1"
    i = 0
    while i < len(group):
        c = group[i]
        # Look for park onset in clip i (with TWO-clip lookahead so a 30s
        # sustain window straddling clip boundaries still counts, even when
        # the immediately following clip has a few seconds of crawl-creep
        # before settling.)
        park_sec = find_park_second(
            c, gps_dirs,
            next_clips=group[i + 1: i + 3],
        )
        # Fallback: if smoothing-based detection misses, fall back to the
        # whole-clip parked heuristic. This catches the case where the GPS
        # data is too noisy / sparse for sustained-window detection but
        # >=75% of seconds are still below 3 km/h. Park onset is treated
        # as clip-second 0 in that case (we don't know more precisely).
        if park_sec is None and clip_is_parked(c, gps_dirs):
            park_sec = 0
        if verbose:
            print(f"    [park-debug] clip[{i}] {c.timestamp} "
                  f"park_sec={park_sec}")
        if park_sec is None:
            i += 1
            continue
        # Park starts in clip i at second `park_sec`. Now follow the parked
        # run forward by checking subsequent clips with clip_is_parked.
        end_idx = i
        for j in range(i + 1, len(group)):
            if clip_is_parked(group[j], gps_dirs):
                end_idx = j
            else:
                break
        # Total stopped time = partial entry tail + fully-parked clips +
        # wall-clock engine-off gap to next moving clip.
        partial_entry = max(0, group[i].duration - park_sec)
        skipped_clips_secs = sum(
            group[k].duration for k in range(i + 1, end_idx + 1)
        )
        if end_idx + 1 < len(group):
            last_end = group[end_idx].dt + timedelta(seconds=group[end_idx].duration)
            gap = max(0.0, (group[end_idx + 1].dt - last_end).total_seconds())
        else:
            gap = 0.0
        total = partial_entry + skipped_clips_secs + gap
        if total >= min_run_secs:
            runs.append((i, park_sec, end_idx))
        i = end_idx + 1
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

# A "real" driving segment should contain at least this many consecutive 1 Hz
# GPS fixes (= seconds). Anything shorter is treated as GPS noise — a phantom
# fix that landed kilometers away from the real position, broke segmentation,
# but didn't trigger enough follow-up jumps to become its own segment chain.
# These tiny outlier segments would otherwise blow up the route's bounding box
# and pull the map widget's auto-zoom out to a regional view that has nothing
# to do with the actual drive.
SEGMENT_MIN_POINTS  = 5


def _haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dl = math.radians(b_lon - a_lon)
    dp = math.radians(b_lat - a_lat)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _nearest_track_fix(track: list[tuple[float, float, float, datetime]],
                       target: datetime) -> tuple[float, float] | None:
    """Return (lat, lon) of the track fix whose datetime is nearest `target`,
    or None if the track is empty. Used to place each interior stop (the fix
    right before a 'Fast forwarding…' slide) in the per-trip meta."""
    if not track:
        return None
    best = min(track, key=lambda p: abs((p[3] - target).total_seconds()))
    return (best[0], best[1])


def segment_track(points: list[tuple[float, float, float, datetime]],
                  min_points: int = SEGMENT_MIN_POINTS,
                  ) -> list[list[tuple[float, float, float, datetime]]]:
    """
    Split the flat list of GPS fixes into contiguous-driving segments. Any
    consecutive pair that is more than SEGMENT_GAP_SECONDS apart in time OR
    more than SEGMENT_GAP_METERS apart in distance starts a new segment.

    Segments shorter than `min_points` fixes are pruned as GPS noise (a few
    isolated phantom fixes that happen to fall outside the SEGMENT_GAP
    threshold from their neighbours but don't represent real driving). Pass
    `min_points=0` to keep every segment regardless of length. If pruning
    would remove every segment, the unfiltered list is returned so the
    caller still has something to work with.
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
    if min_points > 1:
        pruned = [s for s in segments if len(s) >= min_points]
        if pruned:
            return pruned
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
  #title{{padding:10px 16px;background:#222;color:#eee;font-size:15px}}
  #title b{{font-size:17px}}
  #title span{{margin-right:18px;color:#bbb}}
  #map{{flex:1}}
  .legend{{background:#fff;padding:8px;border-radius:4px;box-shadow:0 0 5px rgba(0,0,0,.3);font-size:13px}}
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

// One cycling colour per LEG (each segment = a drive between stops / FFs).
var COLORS = ['#3b82f6', '#ec4899', '#06b6d4', '#22c55e', '#f59e0b',
              '#a855f7', '#ef4444', '#14b8a6'];
function drawRun(latlngs, color) {{
  if (latlngs.length < 2) return;
  // White halo underneath for contrast over arterials and beige residentials
  L.polyline(latlngs, {{color: '#ffffff', weight: 8, opacity: 0.85,
                       lineJoin: 'round', lineCap: 'round'}}).addTo(map);
  L.polyline(latlngs, {{color: color, weight: 5, opacity: 1.0,
                       lineJoin: 'round', lineCap: 'round'}}).addTo(map);
}}

var bounds = L.latLngBounds([]);
for (var s = 0; s < segments.length; s++) {{
  var seg = segments[s];
  if (seg.length < 2) continue;
  // Each segment (a drive between stops) is drawn as one solid, cycling colour.
  var pts = [];
  for (var i = 0; i < seg.length; i++) {{
    pts.push([seg[i][0], seg[i][1]]);
    bounds.extend([seg[i][0], seg[i][1]]);
  }}
  drawRun(pts, COLORS[s % COLORS.length]);
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
    '<div><b>Route</b></div>' +
    '<div class="row">each colour = one leg between stops</div>';
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>
"""


def segment_by_gap(points, gap_secs):
    """Split the track ONLY where consecutive fixes are more than gap_secs apart
    in TIME — i.e. at engine-off stops, where a Fast-Forward is inserted — not at
    brief GPS dropouts. This is what the map legs colour by, so a signal loss on
    the highway no longer looks like a stop (unlike segment_track's 30s/200m)."""
    if not points:
        return []
    segs = [[points[0]]]
    for p in points[1:]:
        if (p[3] - segs[-1][-1][3]).total_seconds() > gap_secs:
            segs.append([p])
        else:
            segs[-1].append(p)
    return segs

def write_html_map(out_path: Path, points: list[tuple[float, float, float, datetime]],
                   title: str, leg_gap_secs: float = 60) -> None:
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
    segments = segment_by_gap(points, leg_gap_secs)   # legs = between engine-off stops (FFs)
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
MAP_TRACK_PAD  = 12            # px padding around bounding box of the route
# Bump the auto-chosen OSM tile zoom by this many integer steps. staticmap's
# auto-zoom rounds DOWN to fit the bbox; the smaller MAP_TRACK_PAD above
# already tightens the frame, so 0 (default) keeps a comfortable amount of
# context around the route. Set to 1 for ~2x detail (route endpoints may sit
# right at the panel edges on elongated routes), 2 for very tight (endpoints
# may clip outside the panel).
MAP_ZOOM_BOOST = 0


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
    tech_lines: "list[str] | None" = None,
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
            # "Moving" not "Driven": it's moving-time (parked time excluded), and
            # Courier's thin lowercase r rendered "Driven" as an ambiguous
            # "Diiven" at 720p. "Moving" also matches the sidecar's "moving speed".
            ("Moving",    f"{stats['moving_min']:.0f} min"),
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

    # A couple of compact technical lines under the map. Deliberately terse and
    # dim: the panel is only 480px wide and this is burned into every frame, so
    # the FULL technical detail lives in _meta.json instead — this is just the
    # at-a-glance provenance of the file you're watching.
    if tech_lines:
        ty = map_top + MAP_PANEL_SIZE + 14
        for line in tech_lines[:3]:
            if ty > panel_h - 18:
                break
            draw.text((24, ty), line, fill=(120, 120, 120), font=f_small)
            ty += 20

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
        # staticmap's auto-zoom rounds DOWN to fit, leaving dead space on
        # short routes. Compute its choice and bump by MAP_ZOOM_BOOST for a
        # tighter frame. Clamp to OSM's max zoom 19.
        zoom_override = None
        if MAP_ZOOM_BOOST and hasattr(m, "_calculate_zoom"):
            try:
                auto_z = m._calculate_zoom()
                zoom_override = min(19, auto_z + MAP_ZOOM_BOOST)
            except Exception:
                zoom_override = None
        img = m.render(zoom=zoom_override) if zoom_override else m.render()

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
    # NOTE: branch on `with_rear`, NOT the global REAR_PIP_ENABLED. A single
    # clip can be missing its rear file even when the rear cam is on for the
    # rest of the trip; that clip has no rear INPUT, so referencing [1:v] here
    # would grab the map-panel input instead. with_rear is set per clip by the
    # caller (REAR_PIP_ENABLED and clip.rear is not None).
    if with_rear:
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
        # parse_clip_speeds aligns the GPS time-series to the clip's VIDEO
        # timeline by prepending 0-km/h placeholders if the dashcam started
        # recording before GPS reacquired lock. Without this, the speed
        # overlay can be ~10 seconds AHEAD of what's actually visible.
        all_speeds = parse_clip_speeds(clip, gps_dirs)
        if all_speeds:
            window = all_speeds[trim_start:trim_start + duration]
            srt_path = out_path.with_suffix(".speed.srt")
            if write_speed_srt(window, srt_path):
                speed_srt = srt_path

    with_map_widget = map_video is not None
    with_rear = REAR_PIP_ENABLED and clip.rear is not None
    filt = build_filter_complex(font_path, actual_epoch, with_timestamp, speed_srt,
                                with_map_widget, with_rear=with_rear)
    if use_vt:
        # Scale the hardware bitrate to the downscaled resolution so 540p
        # output doesn't get 8 Mbps of bitrate (=tiny gain, big file).
        # libx264 uses CRF, which auto-adjusts with resolution, so no scaling needed there.
        vt_b = _scale_bitrate_string(VT_BITRATE, output_height)
        vt_m = _scale_bitrate_string(VT_MAXRATE, output_height)
        venc = [
            "-c:v", "h264_videotoolbox",
            "-b:v", vt_b,
            "-maxrate", vt_m,
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
    # When the main encode is downscaled, the transition slide must match it
    # EXACTLY. The clips are scaled by ffmpeg's `scale=-2:H`, which rounds the
    # derived width to the NEAREST even number. Rounding differently here makes
    # the slide a couple of pixels narrower than the clips (2402 -> 1600 instead
    # of 1602 at 720p). concat -c copy then produces an MP4 whose `avcC`
    # declares one resolution while the slide's frames carry an SPS with
    # another — software decoders cope, hardware ones (VideoToolbox: QuickTime,
    # Preview, Safari) blank until the next IDR, which is the black gap around
    # the slide. So mirror ffmpeg: round-half-up to the nearest multiple of 2.
    if output_height and output_height != OUT_H:
        scale = output_height / OUT_H
        width = int(round(width * scale / 2)) * 2
        height = output_height
    font_escaped = font_path.replace(":", r"\:")
    if use_vt:
        # Match the main encode's scaled bitrate so the slide and clips share
        # comparable encoder parameters (helps concat-demuxer keep the stream).
        vt_b = _scale_bitrate_string(VT_BITRATE, output_height)
        vt_m = _scale_bitrate_string(VT_MAXRATE, output_height)
        venc = ["-c:v", "h264_videotoolbox", "-b:v", vt_b,
                "-maxrate", vt_m, "-profile:v", "high"]
    else:
        venc = ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", X264_CRF]
    # Disable B-frames so there is no frame reordering across the concat splice.
    #
    # Do NOT add "-g 1" here. It was once added to fix "black around the slide",
    # but it is what CAUSES that black: GOP-size-1 makes VideoToolbox emit an
    # all-intra SPS that differs from the one the regular clips are coded with.
    # Since concat_clips joins everything with `-c copy`, the resulting MP4 then
    # contains two distinct SPS variants while its `avcC` declares only the
    # clips' one. Software decoders (ffmpeg) read the in-band SPS and cope, so
    # extracted frames look fine — but HARDWARE decoders (VideoToolbox, i.e.
    # QuickTime/Preview/Safari) configure once from `avcC`, refuse to
    # reconfigure mid-track, and blank until they resync on a later IDR. That is
    # the 1-2 seconds of black users see between the slide and the footage.
    # Verified: dropping -g 1 collapses the file to a single SPS and the gap
    # disappears. A static black slide also encodes SMALLER with a normal GOP.
    venc += ["-bf", "0"]
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


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines -> os.environ, WITHOUT overriding a
    variable already present in the real environment. No python-dotenv needed.
    Used for PERSONAL settings that must never live in the tracked config —
    currently the home coordinates (SET_HOME_LAT / SET_HOME_LON /
    SET_HOME_RADIUS_M). `.env` is gitignored; `.env.example` shows the format."""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def _env_float(key: str) -> "float | None":
    """Read an env var as float, or None if unset/blank/malformed."""
    val = os.environ.get(key, "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        print(f"  ! ignoring {key}={val!r}: not a number", file=sys.stderr)
        return None


def _resolve_config_path(argv: list[str]) -> Path:
    """Pre-parse argv to find --config PATH (so we can use it as defaults source)."""
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
        if a.startswith("--config="):
            return Path(a.split("=", 1)[1]).expanduser()
    # Defaults: look next to the script
    return Path(__file__).resolve().parent / "config.txt"


def _scan_cache_key(clips, gps_dirs=(), **params):
    """Identity of a scan: everything the grouping depends on.

    The clip set, every knob that changes how it is grouped, AND the GPS files —
    because boundaries are derived from them and a .gpx can change while keeping
    its name (the tar cache re-extracts under the same filenames). Name alone
    would let a changed track reuse an old grouping, so size and mtime go in too.

    Any difference must miss. A stale grouping is worse than a slow one: the drop
    step deletes original footage by it.
    """
    h = hashlib.sha256()
    for c in clips:
        # Identify a clip by its name, duration, timestamp and size — NOT its
        # absolute path. Renaming the import directory moves every file without
        # changing a single frame, and keying on the full path made that look
        # like a completely new card and threw away a cache that was still
        # perfectly valid. Size is in because two cards can reuse filenames.
        try:
            size = c.front.stat().st_size
        except OSError:
            size = -1
        h.update(f"{c.front.name}|{c.duration}|{c.timestamp}|{size}\n".encode())
    for d in gps_dirs:
        if not d:
            continue
        try:
            for f in sorted(Path(d).rglob("*.gpx")):
                st = f.stat()
                # name, not path, for the same reason as the clips above
                h.update(f"{f.name}|{st.st_size}|{int(st.st_mtime)}\n".encode())
        except OSError:
            h.update(f"{d}|unreadable\n".encode())
    for k in sorted(params):
        h.update(f"{k}={params[k]}\n".encode())
    return h.hexdigest()


def _scan_cache_load(path, key, clips):
    """Rebuild groups from a cache written by this same session, or None."""
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None
    if d.get("key") != key:
        # Anything changed -> the whole cache is suspect, not just this entry.
        # Leaving it would tempt a later run into a near-miss reuse.
        try:
            Path(path).unlink()
        except OSError:
            pass
        return None
    # Match on basename: the cache may have been written before the import
    # directory was renamed, and the clips are the same clips.
    by_name = {Path(c.front).name: c for c in clips}
    groups = []
    for g in d.get("groups", []):
        try:
            groups.append([by_name[Path(fp).name] for fp in g])
        except KeyError:
            return None                     # clip set really did change
    return groups, d.get("trip_moved", [])


def _scan_cache_store(path, key, groups, trip_moved):
    try:
        Path(path).write_text(json.dumps({
            "key": key,
            "groups": [[str(c.front) for c in g] for g in groups],
            "trip_moved": list(trip_moved),
        }))
    except Exception:
        pass                                # a cache that cannot be written is
                                            # not a reason to fail the run


class _LogTee:
    """Write to the terminal verbatim, and to a log file line-by-line.

    The wrapper used to pipe everything through `tee`, which meant stdout was
    never a terminal — so per-clip progress could not redraw one line with \r,
    because doing that would also have written \r into the log and collapsed it
    into a single unreadable line. Owning the log here separates the two: the
    terminal keeps the carriage returns, and the file gets the newline the
    carriage return was standing in for.
    """

    def __init__(self, stream, path):
        self._s = stream
        self._f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        self._pending = ""

    def write(self, s):
        self._s.write(s)
        # \r means "redraw this line"; in a file that is a completed line.
        for ch in s:
            if ch in "\r\n":
                if self._pending.strip():
                    self._f.write(self._pending.rstrip() + "\n")
                self._pending = ""
            else:
                self._pending += ch
        return len(s)

    def flush(self):
        self._s.flush()
        self._f.flush()

    def isatty(self):
        # The terminal is still the terminal; the log is a side effect.
        return self._s.isatty()

    def close(self):
        if self._pending.strip():
            self._f.write(self._pending.rstrip() + "\n")
        self._f.close()


def main() -> int:
    # --- Personal settings via .env (never the tracked config) ---------------
    # Home coordinates identify where you live, so they live in a gitignored
    # .env as SET_HOME_LAT / SET_HOME_LON / SET_HOME_RADIUS_M, loaded here.
    # A real environment variable of the same name wins over the .env file.
    script_dir = Path(__file__).resolve().parent
    for env_path in (script_dir / ".env", Path.cwd() / ".env"):
        load_dotenv(env_path)
    env_home_lat = _env_float("SET_HOME_LAT")
    env_home_lon = _env_float("SET_HOME_LON")
    # The RADIUS is not private — it is a tuning knob like trip_return_m, and it
    # belongs in config.txt with the others. Only the coordinates identify where
    # you live. Read below, once config is loaded; SET_HOME_RADIUS_M still works
    # as a fallback so an existing .env keeps behaving as it did.

    # --- Config file loading (CLI > config.txt > built-in defaults) ----------
    config_path = _resolve_config_path(sys.argv[1:])
    cfg = load_config_file(config_path)
    cs = lambda k, d: cfg.get(k, d)
    ci = lambda k, d: int(cfg[k]) if k in cfg and cfg[k] != "" else d
    cflt = lambda k, d: float(cfg[k]) if k in cfg and cfg[k] != "" else d
    cb = lambda k, d: _cfg_bool(cfg[k]) if k in cfg else d

    # home_radius_m: config.txt first, then the legacy .env key, then the default.
    home_radius_m = cflt("home_radius_m",
                         _env_float("SET_HOME_RADIUS_M") or DEFAULT_HOME_RADIUS_M)
    home_radius_src = ("config.txt" if "home_radius_m" in cfg
                       else (".env" if _env_float("SET_HOME_RADIUS_M") else "default"))

    # Home COORDINATES resolve the other way round: .env WINS over config.txt.
    # config.txt is committed to a public repo, so whatever sits in it is an
    # example, not a home — the real position belongs in the gitignored .env and
    # must never be overridden by the file everyone can read. (The radius goes
    # the usual way because it discloses nothing.)
    home_lat = env_home_lat if env_home_lat is not None else cflt("home_lat", None)
    home_lon = env_home_lon if env_home_lon is not None else cflt("home_lon", None)
    home = (home_lat, home_lon) if home_lat is not None and home_lon is not None else None
    home_src = (".env" if env_home_lat is not None and env_home_lon is not None
                else ("config.txt example" if home is not None else "unset"))

    # Boolean knobs are stored POSITIVELY in config (timestamp=true rather than
    # no_timestamp=false) — easier to read. Translate to the existing --no-* CLI.
    default_no_timestamp     = not cb("timestamp",     True)
    default_no_speed         = not cb("speed",         True)
    default_no_map_widget    = not cb("map_widget",    True)
    default_no_map_sidecars  = not cb("map_sidecars",  True)
    default_no_skip_parking  = not cb("skip_parking",  True)
    default_no_audio         = not cb("audio",         True)
    default_software         =     cb("software",      True)   # libx264: smaller files
    default_keep_inter       =     cb("keep_intermediates", False)

    # Override the structural module-level constants from config (these are read
    # by build_filter_complex et al. at call-time, so updating here is sufficient).
    global PIP_W, PIP_H, PIP_MARGIN, REAR_PIP_POSITION, REAR_PIP_ENABLED
    global MAP_PANEL_SIZE, MAP_PANEL_POSITION, MAP_PANEL_GUTTER_PX
    global MAP_TRACK_PAD, MAP_ZOOM_BOOST, SEGMENT_MIN_POINTS, CLIP_GPX_WINDOW_SECONDS
    global FRONT_CROP_TOP, FRONT_CROP_BOTTOM, FRONT_W, FRONT_H
    global TRANSITION_SECS
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
    MAP_TRACK_PAD      = ci("map_track_pad",      MAP_TRACK_PAD)
    MAP_ZOOM_BOOST     = ci("map_zoom_boost",     MAP_ZOOM_BOOST)
    SEGMENT_MIN_POINTS = ci("gps_segment_min_points", SEGMENT_MIN_POINTS)
    CLIP_GPX_WINDOW_SECONDS = ci("clip_gpx_window_seconds", CLIP_GPX_WINDOW_SECONDS)
    FRONT_CROP_TOP     = ci("front_crop_top",     FRONT_CROP_TOP)
    FRONT_CROP_BOTTOM  = ci("front_crop_bottom",  FRONT_CROP_BOTTOM)
    TRANSITION_SECS    = ci("transition_secs",    TRANSITION_SECS)
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
    # Default 1080 = OUT_H, i.e. native: the downscale filter is skipped
    # entirely and the composite ships at full 2402x1080. Set 720 in
    # config.txt for ~half the bitrate (the (h/1080)^2 VT auto-scale), or
    # 540 for a phone-friendly file. 0 is still accepted and means native.
    output_height_cfg = ci("output_height", 1080)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config_path),
                    help=f"Path to config.txt (default: {config_path})")
    ap.add_argument("--write-config", metavar="PATH",
                    help="Write a fully-commented config.txt template to PATH and exit.")
    ap.add_argument("--root",  default=cs("root", DEFAULT_ROOT),
                    help=f"Dashcam volume root (default: {cs('root', DEFAULT_ROOT)})")
    ap.add_argument("--out",   default=cs("out", DEFAULT_OUT),
                    help=f"Output folder (default: {cs('out', DEFAULT_OUT)})")
    ap.add_argument("--drives", "--trips", nargs="+", type=int, dest="drives",
                    help="Only process specific trip numbers (1-based). "
                         "--trips is an alias.")
    ap.add_argument("--software", action="store_true", default=default_software,
                    help="Use libx264 instead of VideoToolbox")
    ap.add_argument("--keep-intermediates", action="store_true", default=default_keep_inter,
                    help="Keep per-clip processed files")
    ap.add_argument("--force", action="store_true", default=cb("force", False),
                    help="Re-encode groups even when the final .mp4 already exists "
                         "(default: existing files are skipped).")
    ap.add_argument("--cache-max-age-days", type=int,
                    default=ci("cache_max_age_days", DEFAULT_CACHE_MAX_AGE_DAYS),
                    help="Auto-delete cached .gpx_cache/ entries older than "
                         "this many days at the start of each run. Set to 0 "
                         "to disable. Default 20. (Per-clip intermediates in "
                         ".intermediates/ are always wiped at the start of a "
                         "run regardless — they're scratch, not cache.)")
    ap.add_argument("--dry-run", action="store_true", help="List drives and exit without encoding")
    ap.add_argument("--scan-cache", metavar="PATH",
                    help="Where to cache trip boundaries. Defaults to "
                         "<out>/.scan_cache.json; reused when the clips, their GPX and "
                         "the grouping options are all unchanged, recomputed otherwise. "
                         "Pass an empty string to force a fresh scan.")
    ap.add_argument("--log-file", metavar="PATH",
                    help="Also append output to PATH. Keeps stdout a real terminal so\n"
                         "per-clip progress can redraw in place, while the file still\n"
                         "gets one line per event (replaces piping through tee).")
    ap.add_argument("--print-groups", action="store_true",
                    help="Machine-readable --dry-run: print the trip grouping as "
                         "JSON on stdout (and nothing else — every human-readable "
                         "line goes to stderr) and exit without encoding. The JSON "
                         "lists, per trip, the exact front/rear source files the "
                         "scanner assigned to it, so a caller can act on a trip's "
                         "clips (e.g. drop them from the import) without having to "
                         "re-derive the boundaries from filenames.")
    ap.add_argument("--no-timestamp", action="store_true", default=default_no_timestamp,
                    help="Skip the burned-in date/time overlay")
    ap.add_argument("--no-speed", action="store_true", default=default_no_speed,
                    help="Skip the GPS speed overlay even when GPX data is available")
    ap.add_argument("--no-audio", action="store_true", default=default_no_audio,
                    help="Strip audio from the output (useful if passenger talk shouldn't be shared)")
    ap.add_argument("--trip-return-m", type=float,
                    default=cflt("trip_return_m", DEFAULT_TRIP_RETURN_M),
                    help="A trip closes when the car returns within this many "
                         "metres of where the trip began (after first leaving). "
                         f"Default {DEFAULT_TRIP_RETURN_M}.")
    ap.add_argument("--trip-leave-m", type=float,
                    default=cflt("trip_leave_m", DEFAULT_TRIP_LEAVE_M),
                    help="How far the car must travel from the anchor before a "
                         "return can close the trip (stops it closing on the "
                         f"driveway). Default {DEFAULT_TRIP_LEAVE_M}.")
    ap.add_argument("--trip-day-rollover", type=int,
                    default=ci("trip_day_rollover", DEFAULT_TRIP_DAY_ROLLOVER),
                    help="Hour of day the trip/day label rolls over, instead of "
                         "midnight. A trip starting before this hour is labelled "
                         f"the previous date. Default {DEFAULT_TRIP_DAY_ROLLOVER} (04:00).")
    ap.add_argument("--trip-min-m", type=float,
                    default=cflt("trip_min_m", DEFAULT_TRIP_MIN_M),
                    help="A trip is only kept if its noise-pruned GPS track "
                         "reaches at least this many metres from its anchor; "
                         "closer clusters are near-home puttering / parking-mode "
                         f"events and are auto-skipped. Default {DEFAULT_TRIP_MIN_M}.")
    # Advanced / implementation knob: force GPS-only drive-away detection at
    # parking exits instead of the video ego-motion default. Rarely wanted (the
    # end-user choice is clean-cut vs --no-skip-parking), but the method has a
    # real-life effect so it stays available.
    ap.add_argument("--no-video-drive-detect", action="store_true",
                    default=not cb("video_drive_detect", True),
                    help="ADVANCED: force GPS-only detection of the parking-exit "
                         "drive-away instead of the default video ego-motion "
                         "(needs numpy + opencv-python, used automatically when "
                         "installed).")
    ap.add_argument("--geocode", action="store_true", default=cb("geocode", False),
                    help="Look up place names for each trip's start/end via OSM "
                         "Nominatim and add start_place/end_place to _meta.json. "
                         "OPT-IN: it calls a public service (rate-limited, cached "
                         "in .geocode_cache.json) and fails silently offline. "
                         "Everything else in the metadata is computed locally.")
    ap.add_argument("--no-clean-days", action="store_true",
                    help="By DEFAULT a full render (no --drives) first clears the "
                         "day folders it is about to write, so shifted trip "
                         "indices don't leave stale duplicates behind — but ONLY "
                         "those days; other imports' day folders in the same "
                         "--out are never touched. Pass this to skip that reset "
                         "and keep existing files (e.g. to resume a full render). "
                         "Hidden entries and the --out-root caches are always kept.")
    ap.add_argument("--debug-cuts", type=int, default=0, nargs="?", const=5,
                    dest="debug_cuts", metavar="SECS",
                    help="DEBUG PREVIEW, not a normal render: produce a short clip "
                         "containing ONLY the cut points of a trip — the start, "
                         "each parking/FF pause, and the stop — with all the "
                         "driving in between dropped. SECS is how much context to "
                         "keep around each event (before the 'Fast forwarding' "
                         "slide and after the car moves again); it is NOT overall "
                         "padding. Lets you eyeball where the cuts land in ~20s "
                         "instead of a full render. Writes a separate "
                         "*_debugcuts*.mp4 that never overwrites a real render. "
                         "Bare --debug-cuts uses 5s. Default 0 (off).")
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
    ap.add_argument("--drive-first-clip-pad-secs", type=int,
                    default=ci("drive_first_clip_pad_secs", 8),
                    help="Drive-mode head-trim: when the first clip of a "
                         "drive has GPS that proves the car only started "
                         "moving partway in, anchor the video to start this "
                         "many seconds BEFORE the detected motion. Default 8 "
                         "— GPS only reports speeds reliably ≥5 km/h, so the "
                         "car is typically visibly rolling for ~3 seconds "
                         "before GPS catches up; 8s of pad gives a few "
                         "visibly-parked seconds before the rollout for a "
                         "natural 'about to drive' intro. Separate from "
                         "--parking-exit-pad which controls the exit-from-parking "
                         "slice inside a trip.")
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
                    help="Last-resort seek into the exit clip after a parking "
                         "gap, used only when BOTH video ego-motion and GPS "
                         "drive-resume detection fail to find the drive-away. "
                         f"Default {DEFAULT_EXIT_SKIP_SECS}.")
    ap.add_argument("--drive-resume-sustain-secs", type=int,
                    default=ci("drive_resume_sustain_secs", DRIVE_RESUME_SUSTAIN_SECS),
                    help="Seconds of continuous GPS motion required to "
                         "consider a moving block as 'real drive' for "
                         "exit-slice anchoring (default 30).")
    ap.add_argument("--output-height", type=int, default=output_height_cfg,
                    help="Downscale the final composite to this height in px "
                         "(0 or 1080 = keep native 1080). Default 1080 "
                         "(native). Use 720 for roughly half the size, or "
                         "540 for a smaller phone-sized "
                         "file.")
    args = ap.parse_args()

    # Install the log tee before anything prints, so the file is a complete
    # record of the run rather than whatever happened after setup.
    if getattr(args, "log_file", None):
        _tee = _LogTee(sys.stdout, args.log_file)
        # stderr goes through the same tee, reproducing what `2>&1 | tee` did:
        # errors belong in the log, and an OS-level 2>&1 would bypass this
        # object entirely and never reach the file.
        sys.stdout = sys.stderr = _tee

    # Handle --write-config and exit
    if args.write_config:
        target = Path(args.write_config).expanduser()
        # Convenience: if the user passes a directory (e.g. "." or "~/"), write
        # the template into config.txt inside it instead of refusing to write
        # a file into a directory path.
        if target.is_dir():
            target = target / "config.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"wrote {target.resolve()}")
        return 0

    # --print-groups promises "JSON on stdout and nothing else". The scan prints a
    # page of progress on its way to the grouping, and chasing every one of those
    # prints with a file= argument would be a permanent maintenance tax that a
    # future print would silently break. Move the whole conversational stream to
    # stderr instead and keep the real stdout aside for the one JSON document —
    # then stdout is machine-clean by construction, and the scan's chatter is
    # still there for a human watching the run.
    _json_stdout = sys.stdout
    if args.print_groups:
        sys.stdout = sys.stderr

    if cfg:
        print(f"config:      loaded {len(cfg)} setting(s) from {config_path}")

    root = Path(args.root).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Default the boundary cache to <out>/.scan_cache.json rather than making
    # every caller pass it. The ego-motion pass costs minutes and produces the
    # same answer for the same inputs, so paying it again is never what anyone
    # wants — and only the CLI was passing the flag, so a render started from
    # the wrapper or by hand rescanned the whole card. The key already refuses
    # to hit when anything it depends on changed, so defaulting it on is safe.
    # Pass --scan-cache "" to force a fresh scan.
    if args.scan_cache is None:
        args.scan_cache = str(out_dir / ".scan_cache.json")

    # Always wipe per-clip intermediates at the start of a run. Reusing them
    # across runs is dangerous — config changes (head-trim pad, output_height,
    # speed_unit, audio settings, etc.) silently produce stale outputs because
    # the intermediate filename can't encode every relevant knob. The cleaner
    # model: intermediates are scratch space for ONE run, finals persist and
    # the user controls regeneration by deleting a final .mp4 (or passing
    # --force, which deletes it for you).
    encoding_run = not (args.dry_run or args.sidecars_only or args.print_groups)

    # Two encoders sharing one --out would delete each other's scratch files
    # mid-encode (.intermediates is wiped at start). Take a PID lock so the
    # second one refuses instead of corrupting the first.
    lock_path = out_dir / ".render.lock"
    if encoding_run:
        if lock_path.exists():
            try:
                other_pid = int(lock_path.read_text().split()[0])
            except Exception:
                other_pid = 0
            alive = False
            if other_pid:
                try:
                    os.kill(other_pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                print(f"ERROR: another render (pid {other_pid}) is already writing to\n"
                      f"       {out_dir}\n"
                      f"       Wait for it to finish, or use a different --out.",
                      file=sys.stderr)
                return 1
            lock_path.unlink(missing_ok=True)      # stale lock from a killed run
        lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        atexit.register(lambda: lock_path.unlink(missing_ok=True))

    # ONLY when we are actually going to encode. --dry-run and --sidecars-only
    # must be read-only: wiping here would destroy the scratch files of a render
    # already running against the same --out (which is exactly how a dry-run
    # once killed a live 283-clip encode at clip 50).
    inter_dir = out_dir / ".intermediates"
    if encoding_run and inter_dir.is_dir():
        n = sum(1 for _ in inter_dir.rglob("*"))
        if n:
            shutil.rmtree(inter_dir, ignore_errors=True)
            print(f"cleared {inter_dir}  ({n} entries removed)")

    # TTL eviction for the GPX cache (harvested from tar archives — expensive
    # to redo and unaffected by encoding config, so this one DOES cache across
    # runs). Disk-friendly cleanup of stale entries.
    if args.cache_max_age_days > 0:
        cutoff = datetime.now().timestamp() - args.cache_max_age_days * 86400
        target = out_dir / ".gpx_cache"
        if target.is_dir():
            removed = 0
            for p in target.rglob("*"):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed:
                print(f"  cache TTL: removed {removed} file(s) older than "
                      f"{args.cache_max_age_days}d from {target}")

    front_dir = root / "DCIM" / "200video" / "front"
    rear_dir  = root / "DCIM" / "200video" / "rear"
    gps_dir   = root / "DCIM" / "203gps"
    tar_dir   = gps_dir / "tar"
    if not front_dir.is_dir():
        print(f"ERROR: expected front folder at {front_dir}", file=sys.stderr)
        return 1

    # Detect the actual front-camera resolution instead of assuming 2560x1600.
    # DDPAI cams record at a few resolutions (e.g. 2560x1600 16:10, 1920x1080
    # 16:9). We crop the source to the 16:9 output aspect BEFORE scaling so a
    # 1080p clip isn't vertically stretched, and the crop default is derived
    # from the real size (2560x1600 -> 80/80 as before; 1920x1080 -> 0/0). An
    # explicit front_crop_top/bottom in config still overrides.
    _first_front = next(iter(sorted(front_dir.glob("*.mp4"))), None)
    _sz = probe_video_size(_first_front) if _first_front else None
    if _sz:
        FRONT_W, FRONT_H = _sz
        print(f"Source:      front {FRONT_W}x{FRONT_H}")
    _aspect_crop = max(0, FRONT_H - round(FRONT_W * OUT_H / OUT_W)) // 2
    FRONT_CROP_TOP    = ci("front_crop_top",    _aspect_crop)
    FRONT_CROP_BOTTOM = ci("front_crop_bottom", _aspect_crop)
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
            print(f"Tarred GPS:  extracted {n_new} new .gpx files from {n_arch} archives "
                  f"into {tar_cache_dir}")

    gps_dirs = (gps_dir, tar_cache_dir)

    n_gpx_loose = sum(1 for f in os.listdir(gps_dir) if GPX_RE.match(f)) if gps_dir else 0
    n_gpx_tar   = sum(1 for f in os.listdir(tar_cache_dir) if f.endswith(".gpx")) if tar_cache_dir else 0

    print(f"Encoder:     {encoder_name}")
    print(f"Timestamp:   {'on (' + font_path + ')' if with_timestamp else 'off'}")
    if with_speed:
        print(f"Speed:       on ({n_gpx_loose} loose .gpx + {n_gpx_tar} from tar archives)")
    elif gps_dir is None and tar_dir is None:
        print(f"Speed:       off (no DCIM/203gps folder)")
    elif args.no_speed:
        print(f"Speed:       off (--no-speed)")
    else:
        print(f"Speed:       off")
    # Don't print the actual home coordinates (they'd land in logs) — just note
    # that a home boundary is active, sourced from .env.
    # Same shape as "return 100m": both are radii in metres, so both read the
    # same way. The stray "r" made two identical concepts look like two things.
    # Coordinates come from .env (private); the radius from config.txt (not).
    home_note = (f" / home {home_radius_m:.0f}m ({home_radius_src}, coords {home_src})"
                 if home is not None else "")
    print(f"Grouping:    return {args.trip_return_m:.0f}m / "
          f"rollover {args.trip_day_rollover:02d}:00{home_note}")
    print(f"Output:      {out_dir}")
    print(f"Scanning:    {front_dir}")

    clips = find_clips(front_dir, rear_dir)

    # A "trip" is the publishing unit: leave an anchor, return to it — or run
    # until a long engine-off gap / the 04:00 rollover. See group_into_trips.
    # `moved` flags trips that had GPS but never actually went anywhere
    # (parking-mode motion events clustered at a standstill) so we auto-skip them.
    _gparams = dict(
        return_m=args.trip_return_m,
        leave_m=args.trip_leave_m,
        rollover_h=args.trip_day_rollover,
        home=home,
        home_radius_m=home_radius_m,
        min_trip_m=args.trip_min_m,
        use_video=not args.no_video_drive_detect,
    )
    # Finding drive-away/park by ego-motion is the slowest part of a scan and it
    # produces the same answer every time for the same clips. --scan-cache lets
    # one session compute it once and reuse it across steps; the caller owns the
    # file and deletes it on exit, so a restart always recomputes.
    _ck = _scan_cache_key(clips, gps_dirs=gps_dirs, **_gparams) if args.scan_cache else None
    _hit = _scan_cache_load(args.scan_cache, _ck, clips) if _ck else None
    if _hit:
        groups, trip_moved = _hit
        print(f"Grouping:    reusing cached boundaries ({len(groups)} trips)")
    else:
        groups, trip_moved = group_into_trips(clips, gps_dirs, **_gparams)
        if _ck:
            _scan_cache_store(args.scan_cache, _ck, groups, trip_moved)
    group_kind, group_word = "trip", "Trip"
    # Day label (04:00 rollover) each trip belongs to — the UI groups on this.
    day_labels = [trip_day_label(g[0].dt, args.trip_day_rollover) for g in groups]

    print(f"\nFound {len(clips)} clip pairs grouped into {len(groups)} {group_kind}s:")
    total_secs = 0
    for i, g in enumerate(groups, 1):
        start = g[0].dt
        end   = g[-1].dt + timedelta(seconds=g[-1].duration)
        secs  = (end - start).total_seconds()
        total_secs += secs
        print(f"  {group_word} {i:2d}  day {day_labels[i-1]}  "
              f"{start:%Y-%m-%d %H:%M} -> {end:%m-%d %H:%M}   "
              f"{len(g):3d} clips  ~{fmt_secs(secs)} span")
    print(f"\nTotal: ~{fmt_secs(total_secs)} of wall-clock span "
          f"(start to end per trip)")
    print("Parking inside a trip is cut and replaced with a Fast-forwarding "
          "slide,\nso the encoded result is shorter — often far shorter. "
          "Preview (step 3)\nwrites the real moving time per trip into "
          "each _meta.json.")

    # When --drives is given, ONLY those groups run (and the min-clips filter
    # is bypassed for them — user explicitly asked). Otherwise, every group
    # that has at least min_clips_per_group clips runs.
    explicit_set = set(args.drives) if args.drives else None
    if explicit_set is not None:
        wanted = explicit_set
    else:
        # Auto-skip fragments (too few clips) AND stationary trips (had GPS but
        # never left the anchor — parking-mode junk). Both are force-encodable
        # by naming the index via --drives.
        wanted = {i for i, g in enumerate(groups, 1)
                  if len(g) >= args.min_clips_per_group and trip_moved[i - 1]}
        skipped_small = [(i, len(g)) for i, g in enumerate(groups, 1)
                         if len(g) < args.min_clips_per_group]
        skipped_still = [i for i, g in enumerate(groups, 1)
                         if len(g) >= args.min_clips_per_group and not trip_moved[i - 1]]
        if skipped_small:
            note = ", ".join(f"#{i} ({n} clip{'s' if n != 1 else ''})"
                             for i, n in skipped_small)
            print(f"\nAuto-skipping {len(skipped_small)} fragment {group_kind}(s): "
                  f"{note}\n(force-encode by naming the index via --drives.)")
        if skipped_still:
            note = ", ".join(f"#{i}" for i in skipped_still)
            print(f"\nAuto-skipping {len(skipped_still)} stationary {group_kind}(s) "
                  f"(GPS shows no real drive): {note}\n"
                  f"(force-encode by naming the index via --drives.)")

    # PUBLISHED trip numbers. `idx` is the internal group index and counts the
    # fragments/stationary groups that never get rendered, so a day whose only
    # two real trips are groups 8 and 9 would publish "Trip 8"/"Trip 9". Number
    # the trips that actually ship 1..N within their own day instead — that's
    # what ends up in the filename, the panel title and _meta.json.
    #
    # Computed BEFORE the --print-groups / --dry-run exits (it is pure
    # arithmetic, no side effects) so those two modes can report the same
    # publish numbers and output paths a real render would produce, from this
    # one copy of the rule rather than a second one that could drift.
    pub_no: dict[int, int] = {}
    _per_day: dict[str, int] = {}
    # An out-of-range --drives index is silently ignored by the render loop
    # below (it walks the groups and skips what isn't wanted); without this
    # bound it would blow up here on day_labels instead. Say so rather than
    # filtering it away in silence — a typo'd index would otherwise scan the
    # whole card, render nothing and exit 0, which reads as "nothing to do".
    _oob = sorted(w for w in wanted if not (1 <= w <= len(groups)))
    if _oob:
        print(f"WARNING: ignoring --drives index/indices "
              f"{', '.join(str(w) for w in _oob)}: only {len(groups)} "
              f"{group_kind}s were found.", file=sys.stderr)
    for i in sorted(w for w in wanted if 1 <= w <= len(groups)):
        d = day_labels[i - 1]
        _per_day[d] = _per_day.get(d, 0) + 1
        pub_no[i] = _per_day[d]

    # Output is NAMESPACED BY IMPORT: everything from one import lives under
    # out_dir/<import-name>/<day>/. This makes cross-card clobbering impossible —
    # two different cards that both contain, say, May 11 footage land in
    # out_dir/2026-05-11/2026-05-11/ and out_dir/import-3904994/2026-05-11/,
    # separate subtrees that can never overwrite each other. (DDPAI cards hoard
    # old event clips, so a single import routinely spans many historical days —
    # that is exactly how a "06-22" card once reset the day back to April and
    # wiped a 2026-05-11 rendered from a different, already-deleted card.)
    import_ns = out_dir / root.name

    if args.print_groups:
        # Serialise the grouping the scan just produced — `groups` straight out of
        # group_into_trips(), with `wanted`/`trip_moved` for the skip decision and
        # `pub_no`/`import_ns` for the output naming. Nothing here re-derives a
        # boundary; every file listed is a Clip object the grouper itself put in
        # that trip. That matters because the caller (pipeline.py's drop step)
        # DELETES the files in this list: inferring a trip's extent from filename
        # timestamps would eventually cut one clip either side of a real boundary
        # and destroy original footage.
        payload = {"root": str(root), "out": str(out_dir), "trips": []}
        for i, g in enumerate(groups, 1):
            start = g[0].dt
            end = g[-1].dt + timedelta(seconds=g[-1].duration)
            renderable = i in wanted
            # Say WHY a trip will not be rendered, in the same terms (and from
            # the same conditions) as the "Auto-skipping …" lines above.
            if renderable:
                reason = None
            elif len(g) < args.min_clips_per_group:
                reason = (f"fragment: {len(g)} clip(s), fewer than "
                          f"--min-clips-per-group {args.min_clips_per_group}")
            elif not trip_moved[i - 1]:
                reason = "stationary: GPS shows no real drive away from the anchor"
            else:
                reason = "not named by --drives"
            entry = {
                "index": i,
                "day": day_labels[i - 1],
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "clips": len(g),
                "duration_secs": int((end - start).total_seconds()),
                "renderable": renderable,
                "reason": reason,
                # Publish number and output basename, but only for a trip that
                # would actually be rendered — the others get no output at all.
                "pub_index": pub_no.get(i),
                "out_base": (str(import_ns / day_labels[i - 1] /
                                 f"trip_{day_labels[i - 1]}_{start:%H-%M}_{pub_no[i]:02d}")
                             if renderable and i in pub_no else None),
                "front": [str(c.front) for c in g],
                # A clip can legitimately have no rear file (rear cam absent or
                # its file loop-overwritten), so this list is often shorter than
                # `front` and must not be zipped with it positionally.
                "rear": [str(c.rear) for c in g if c.rear is not None],
            }
            payload["trips"].append(entry)
        json.dump(payload, _json_stdout, indent=1)
        _json_stdout.write("\n")
        _json_stdout.flush()
        return 0

    if args.dry_run:
        return 0

    # FRESH OUTPUT for the days THIS run renders — but ONLY inside this import's
    # own namespace. A re-render clears its own stale trip_* (indices shift on a
    # re-group); every OTHER import is physically in a different folder and is
    # never touched. Gated to FULL renders (a --drives subset is resume-like);
    # --no-clean-days opts out. Hidden entries and the out-root caches are kept.
    #
    # ALSO gated to an ENCODING run, which it was not until now — and that was a
    # data-loss bug, not a nuance. A bare `--sidecars-only` (no --drives, so
    # full_render is True) walked the day folders and unlinked everything in
    # them, including finished .mp4 renders it had no intention of replacing:
    # verified on a fixture, an existing trip_*.mp4 plus its .html and .gpx were
    # deleted and only the rewritten _meta.json came back. The wrapper's own
    # comment already claimed the reset was "skipped for the read-only
    # --sidecars-only mode"; it was not. A metadata refresh must never remove a
    # render, and pipeline.py's preview step runs exactly that command over an
    # import that may already be partly rendered.
    full_render = explicit_set is None
    if full_render and encoding_run and not args.no_clean_days:
        target_days = sorted({day_labels[i - 1] for i in wanted})
        removed = 0
        for d in target_days:
            dd = import_ns / d
            if dd.is_dir():
                for p in dd.iterdir():
                    if p.name.startswith("."):
                        continue                       # keep hidden content
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                    removed += 1
        if target_days:
            print(f"fresh output: cleared {removed} file(s) under "
                  f"{import_ns.name}/ for day(s) {', '.join(target_days)} "
                  f"(only this import's namespace; other imports untouched)")

    work_dir = out_dir / ".intermediates"
    work_dir.mkdir(exist_ok=True)

    for idx, group in enumerate(groups, 1):
        if idx not in wanted:
            continue

        start = group[0].dt
        end   = group[-1].dt + timedelta(seconds=group[-1].duration)
        secs  = (end - start).total_seconds()
        # Final filename: bake the chosen output height into the name (when
        # downscaling) so re-rendering at a different height doesn't overwrite
        # the previous file and the format is obvious from the name on disk
        # (e.g. trip_2026-05-11_08-00_01_h540.mp4). Sidecars stay un-tagged
        # because GPS data is the same regardless of video resolution — one
        # .html/.gpx per trip, even if the user renders multiple sizes.
        #
        # The DAY LABEL (04:00 rollover) leads the name so the publishing UI can
        # group a day's trips by globbing the prefix; the start time and global
        # index disambiguate multiple trips on the same day.
        # ALWAYS record the resolution in the filename, including a native
        # render (output_height=0), where the composite is OUT_H tall — the
        # name should say what you are looking at without probing the file.
        size_tag = f"_h{args.output_height or OUT_H}"
        # Debug-cuts previews get their own suffix so they never overwrite the
        # real full-length render of the same trip.
        fringe_tag = f"_debugcuts{args.debug_cuts}s" if args.debug_cuts > 0 else ""
        day_label = day_labels[idx - 1]
        label = f"{day_label}_{start:%H-%M}_{pub_no[idx]:02d}"
        # Output is namespaced by import: out_dir/<import-name>/<extract-day>/.
        # The extract day (04:00-rollover) groups a card's trips; the import name
        # above it keeps two cards that share a calendar day in separate subtrees
        # so they can never overwrite each other. info.txt records the source.
        day_dir = import_ns / day_label
        day_dir.mkdir(parents=True, exist_ok=True)
        info_txt = day_dir / "info.txt"
        if not info_txt.exists():
            info_txt.write_text(
                f"source import folder: {root}\n"
                f"extract day (trips grouped with a 04:00 rollover): {day_label}\n",
                encoding="utf-8",
            )
        sidecar_base = day_dir / f"trip_{label}"
        final = sidecar_base.with_name(sidecar_base.name + f"{fringe_tag}{size_tag}.mp4")

        print(f"\n[{group_word} {pub_no[idx]}/{len(wanted)}] {start:%Y-%m-%d %H:%M} → {end:%H:%M}  "
              f"({len(group)} clips, ~{fmt_secs(secs)})")
        if args.debug_cuts > 0:
            print(f"  DEBUG-CUTS preview (not a real render): start + pauses "
                  f"(±{args.debug_cuts}s context) + stop only, driving dropped "
                  f"→ {final.name}")
        if size_tag:
            # Make the resize visible in the log so it's clear which format
            # this run produced (and which bitrate the encoder ended up using).
            vt_b = _scale_bitrate_string(VT_BITRATE, args.output_height)
            vt_m = _scale_bitrate_string(VT_MAXRATE, args.output_height)
            print(f"  output: downscaled to {args.output_height}p "
                  f"(VT bitrate {VT_BITRATE} → {vt_b}, maxrate {VT_MAXRATE} → {vt_m})")
        else:
            print(f"  output: native 1080p (no downscale)")

        # Emit map sidecars (HTML / GPX / links.txt) using whatever GPS data is available.
        # Done unconditionally — even when the final .mp4 already exists — so the
        # user can refresh the sidecars after segmentation/render fixes without
        # re-encoding 1.9 hours of video.
        group_track_raw = gather_track(group, gps_dirs) if with_speed else []
        # Prune GPS noise once, at the top of the pipeline. segment_track
        # drops segments shorter than SEGMENT_MIN_POINTS — those are usually
        # a few isolated phantom fixes that fall outside the gap thresholds
        # but don't represent real driving. Flatten back to a single list so
        # the rest of the pipeline (sidecars, stats, burn-in panel, per-second
        # marker animation) all see the same set of points; otherwise the
        # burn-in's auto-zoomed bbox gets pulled wide by stray outlier fixes
        # that the leaflet sidecar happens to render less prominently.
        if group_track_raw:
            pruned_pts: list[tuple[float, float, float, datetime]] = []
            for seg in segment_track(group_track_raw):
                pruned_pts.extend(seg)
            n_pruned = len(group_track_raw) - len(pruned_pts)
            group_track = pruned_pts
            if n_pruned:
                print(f"  gps: pruned {n_pruned} noise-segment "
                      f"fix{'es' if n_pruned != 1 else ''} from {len(group_track_raw)} raw")
        else:
            group_track = []
        # GPS <time> is UTC, but a clip's filename time is the camera's LOCAL
        # clock; they differ by the camera timezone. Compute that offset once
        # (first fix vs first clip) so a stop's local time can be matched to the
        # right group_track fix — without it, every stop resolves to the last
        # fix (the trip's end) because a local time is hours past all UTC fixes.
        _gps_time_offset = (group_track[0][3] - group[0].dt) if group_track else timedelta(0)
        if not args.no_map_sidecars and group_track:
            title = f"Trip {pub_no[idx]} — {day_label} — {start:%H:%M}"
            html_path  = sidecar_base.with_suffix(".html")
            gpx_path   = sidecar_base.with_suffix(".gpx")
            links_path = sidecar_base.with_name(sidecar_base.name + "_links.txt")
            write_html_map(html_path, group_track, title, args.inter_clip_gap_secs)
            write_gpx_export(gpx_path, group_track, title)
            write_links_sidecar(links_path, group_track, title)
            stats = _track_stats(group_track)
            print(f"  map: {stats['distance_km']:.1f} km in {stats['n_segments']} segments, "
                  f"{stats['n']} points → {html_path.name}, {gpx_path.name}, {links_path.name}")
        elif not args.no_map_sidecars:
            print(f"  map: (no GPS data for this {group_kind})")

        # Per-trip machine-readable metadata — this is the "day metadata" the
        # publishing UI reads to group a day's trips together and to know each
        # trip's shape (round-trip vs one-way relocation) without re-parsing GPS.
        # Written independently of --no-map-sidecars: it's the UI contract, not
        # a map artifact. `video` is the PLANNED output filename (this runs
        # before the encode), so it names the file the UI should expect.
        first_fix = (group_track[0][0], group_track[0][1]) if group_track else None
        last_fix  = (group_track[-1][0], group_track[-1][1]) if group_track else None
        round_trip = (
            first_fix is not None and last_fix is not None
            and _haversine_km(first_fix[0], first_fix[1],
                              last_fix[0], last_fix[1]) * 1000.0 <= args.trip_return_m
        )
        st = _track_stats(group_track) if group_track else None
        bbox = None
        if group_track:
            lats = [p[0] for p in group_track]
            lons = [p[1] for p in group_track]
            bbox = {"min_lat": round(min(lats), 6), "min_lon": round(min(lons), 6),
                    "max_lat": round(max(lats), 6), "max_lon": round(max(lons), 6)}

        def _gmaps(fix):
            return (f"https://www.google.com/maps/search/?api=1&query={fix[0]:.6f},{fix[1]:.6f}"
                    if fix else None)

        # Interior stops (one per 'Fast forwarding…' slide). Populated by the
        # per-clip render loop below and written into the meta AFTER that loop
        # (see the post-render meta write). Same list object referenced here.
        stops: list[dict] = []

        meta = {
            "trip_index": pub_no[idx],
            "group_index": idx,   # internal grouping index, for traceability
            "day": day_label,               # 04:00-rollover day the UI groups on
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end":   end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_secs": int(secs),
            "n_clips": len(group),
            "video": final.name,
            "round_trip": round_trip,       # False => one-way relocation
            "source_import": str(root),
            # --- where -------------------------------------------------------
            "start_fix": first_fix,
            "end_fix": last_fix,
            "bbox": bbox,
            "start_map_url": _gmaps(first_fix),
            "end_map_url": _gmaps(last_fix),
            # --- how far / how fast ------------------------------------------
            "distance_km": round(st["distance_km"], 2) if st else 0.0,
            "moving_min": round(st["moving_min"], 1) if st else 0.0,
            "duration_min": round(st["duration_min"], 1) if st else 0.0,
            "max_kmh": round(st["max_kmh"], 1) if st else 0.0,
            "avg_kmh": round(st["avg_kmh"], 1) if st else 0.0,
            "gps_points": st["n"] if st else 0,
            "gps_segments": st["n_segments"] if st else 0,
            # --- interior stops (filled in after the render loop) ------------
            "stops": stops,
            # --- how it was made ---------------------------------------------
            "technical": {
                "resolution": f"{FRONT_W}x{FRONT_H} source",
                "output_height": args.output_height or OUT_H,
                "encoder": "h264_videotoolbox" if use_vt else "libx264",
                "ffmpeg": ffmpeg_version(),
                "exporter": "dashcam-exporter",
                "trip_boundaries": ("video ego-motion (park-to-park)"
                                    if (_HAVE_EGO and not args.no_video_drive_detect)
                                    else "GPS radius (fallback)"),
                "cut_detection": ("Lucas-Kanade median optical flow"
                                  if (_HAVE_EGO and not args.no_video_drive_detect)
                                  else "GPS speed"),
            },
        }
        # Opt-in place names. Never on by default: it calls a public geocoder.
        if args.geocode:
            gc_cache = out_dir / ".geocode_cache.json"
            if first_fix:
                meta["start_place"] = reverse_geocode(*first_fix, gc_cache)
            if last_fix:
                meta["end_place"] = reverse_geocode(*last_fix, gc_cache)
        meta_path = sidecar_base.with_name(sidecar_base.name + "_meta.json")

        if args.sidecars_only:
            # Metadata-only pass: no render happens, so `stops` can't be
            # recomputed here — PRESERVE any a prior render already wrote rather
            # than clobbering them with an empty list.
            if meta_path.exists():
                try:
                    meta["stops"] = json.loads(meta_path.read_text()).get("stops", [])
                except Exception:
                    pass
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            continue

        # NOTE: for a real render the meta is written AFTER the clip loop, once
        # `stops` is populated (see below). A skipped-because-already-rendered
        # trip therefore does NOT rewrite its meta here, which preserves the
        # `stops` a previous successful render wrote (they can't be recomputed
        # without re-rendering; re-run with --force to refresh).
        if final.exists():
            if args.force:
                print(f"  video: {final.name} already exists — re-encoding (--force)")
                try:
                    final.unlink()
                except OSError as e:
                    print(f"  ! could not remove {final}: {e}", file=sys.stderr)
                    continue
            else:
                print(f"  video: {final.name} already exists — skipping "
                      "(re-run with --force to overwrite)")
                continue

        # Pre-render the burn-in right panel (stats on top + map + optional QR)
        base_panel = None
        group_pixels: list[tuple[int, int]] = []
        if not args.no_map_widget and group_track:
            panel_title = f"Trip {pub_no[idx]} — {day_label}"
            # Compact provenance under the map. Full detail is in _meta.json —
            # this is only what fits legibly in a 480px column.
            _out_h = args.output_height or OUT_H
            _comp_w = int(round((OUT_W + MAP_PANEL_SIZE + 2) * (_out_h / OUT_H) / 2)) * 2
            tech_lines = [
                f"{_comp_w}x{_out_h} · {'h264_vt' if use_vt else 'libx264'} · ffmpeg {ffmpeg_version()}",
                (f"cuts: optical-flow ego-motion"
                 if (_HAVE_EGO and not args.no_video_drive_detect)
                 else "cuts: GPS speed"),
            ]
            rendered = render_base_right_panel(
                group_track,
                title=panel_title,
                font_path=font_path,
                include_stats=panel_stats_enabled,
                tech_lines=tech_lines,
            )
            if rendered is None:
                print("  ! map widget skipped: PIL/Pillow not installed."
                      " Run: pip3 install -r requirements.txt")
            else:
                base_panel, group_pixels = rendered
        with_map_widget = base_panel is not None

        # Identify long parking runs we should skip past. A trip spans multiple
        # engine-on sessions (drive out, park, drive back), so interior parking
        # is expected and gets a 'Fast forwarding…' slide — the same machinery
        # the old --daily path used. (A trip whose entire body is one continuous
        # drive simply yields no parking runs here.)
        parking_runs: list[tuple[int, int, int]] = []
        if not args.no_skip_parking and with_speed:
            parking_runs = find_parking_runs(group, gps_dirs, args.parking_min_secs)

        # Map clip-index → action.
        #   entry      = first pad seconds of the FIRST parked clip
        #   skip       = drop entirely (every clip in the parked run, including the last)
        #   exit       = first pad seconds of the NEXT MOVING clip after the run
        #   head_skip  = drop entirely (drive-mode head trim before motion clip)
        # This means the Fast-forwarding slide covers both the remaining parked
        # footage AND any engine-off gap until the next drive resumes.
        action_for: dict[int, str] = {}
        skipped_secs_for: dict[int, float] = {}
        # Drive-mode head trim: scan the first few clips for the moment the
        # car actually starts moving (drive_resume_sustain_secs of sustained
        # GPS motion). If motion starts in clip N, mark clips 0..N-1 as
        # head_skip (drop entirely) and trim clip N to start `pad` seconds
        # before motion. This handles drives where the engine-on→wheels-turn
        # gap spans into the second or third clip (e.g., long warm-up,
        # waiting at a light right after starting).
        head_skip_count = 0
        head_trim_for_motion_clip = 0
        if with_speed and len(group) >= 1:
            # Prefer VIDEO ego-motion to find the departure, same reason as the
            # parking exit: GPS misses the low-speed pull-away amid passing
            # traffic and lags several seconds, so it starts the trip late.
            # Video pinpoints it → small EGO_CONTEXT_PAD; GPS needs its bigger
            # drive_first_clip_pad_secs to compensate for the lag. Examine up to
            # the first 3 clips (motion may start late in clip 0 and sustain
            # into clip 2). Falls back to GPS when video is off/unavailable.
            src = offset = None
            vid = (None if args.no_video_drive_detect
                   else find_drive_away_in_group_video(group[:3]))
            if vid is not None:
                motion_clip_idx, vsec = vid
                head_skip_count = motion_clip_idx
                head_trim_for_motion_clip = max(0, int(vsec) - EGO_CONTEXT_PAD)
                src, offset = "video", int(vsec)
            else:
                resume = find_drive_resume_in_group(
                    group[:3], gps_dirs,
                    sustain_secs=args.drive_resume_sustain_secs,
                )
                if resume is not None:
                    motion_clip_idx, offset = resume
                    head_skip_count = motion_clip_idx
                    head_trim_for_motion_clip = max(0, offset - args.drive_first_clip_pad_secs)
                    src = "gps"
            if src is not None:
                if head_skip_count > 0:
                    for k in range(head_skip_count):
                        action_for[k] = "head_skip"
                    print(f"  head-trim ({src}): skipping {head_skip_count} pre-drive "
                          f"clip{'s' if head_skip_count != 1 else ''}, motion in "
                          f"clip {head_skip_count + 1} at second {offset}")
                elif head_trim_for_motion_clip > 0:
                    print(f"  head-trim ({src}): dropping first "
                          f"{head_trim_for_motion_clip}s of clip 1, motion at second {offset}")
        # Map entry clip-index → park-onset second within that clip (so the
        # main loop can compute trim_seconds = park_sec + entry_pad).
        park_sec_for_entry: dict[int, int] = {}
        for run_start, park_sec, run_end in parking_runs:
            # In trip mode head-trim and parking detection both run. Head-trim
            # already drops the pre-departure clips at the very start of a trip;
            # don't let a parking run that overlaps that head reclassify them
            # (which would resurrect footage head-trim meant to cut). Also
            # protect the motion/departure clip itself (index head_skip_count)
            # when head-trim acted, so a run that mis-classifies its parked head
            # can't overwrite the head-trim with a parking "entry".
            head_trim_acted = head_skip_count > 0 or head_trim_for_motion_clip > 0
            if action_for.get(run_start) == "head_skip":
                continue
            if head_trim_acted and run_start == head_skip_count:
                continue
            next_idx = run_end + 1
            trailing = next_idx >= len(group)
            # A trailing run = the trip ends parked (typical for a one-way
            # relocation: arrive, engine stays on). Keep the arrival slice, but
            # emit NO 'Fast forwarding…' after it — there's nothing to skip to,
            # and a dangling FF slide as the final frames looks broken.
            action_for[run_start] = "entry_end" if trailing else "entry"
            park_sec_for_entry[run_start] = park_sec
            for k in range(run_start + 1, run_end + 1):
                if action_for.get(k) != "head_skip":
                    action_for[k] = "skip"
            if next_idx < len(group) and next_idx not in action_for:
                action_for[next_idx] = "exit"
                # Wall-clock seconds elapsed between the entry's last frame
                # (= park onset + entry_pad seconds into the entry clip) and
                # the exit clip's start.
                entry_end = (group[run_start].dt +
                             timedelta(seconds=park_sec + args.parking_entry_pad))
                exit_start = group[next_idx].dt
                skipped_secs_for[run_start] = max(
                    0.0, (exit_start - entry_end).total_seconds()
                )

        if parking_runs:
            saved = 0
            for run_start, park_sec, run_end in parking_runs:
                next_idx = run_end + 1
                if next_idx < len(group):
                    # From park onset in entry clip to start of exit clip.
                    span = ((group[next_idx].dt - group[run_start].dt).total_seconds()
                            - park_sec + args.parking_exit_pad)
                else:
                    span = (run_end - run_start + 1) * group[run_start].duration
                saved += int(span - args.parking_entry_pad
                             - args.parking_exit_pad - TRANSITION_SECS)
            print(f"  parking: {len(parking_runs)} run(s) skipped, "
                  f"~{fmt_secs(max(saved, 0))} cut from the output")

        entry_pad = args.parking_entry_pad
        exit_pad  = args.parking_exit_pad

        # For --debug-cuts preview: the first and last clips that actually get
        # emitted (skips/head-skips excluded) = the trip's "start" and "stop".
        _emit_idx = [k for k in range(len(group))
                     if action_for.get(k) not in ("skip", "head_skip")]
        first_emit_idx = _emit_idx[0] if _emit_idx else -1
        last_emit_idx  = _emit_idx[-1] if _emit_idx else -1
        # Clips whose END precedes a genuine inter-clip gap (a gap-FF pause):
        # their tail is the "5s before FF". Computed over the emitted sequence
        # with the same rule the render loop uses, so it doesn't mistake a
        # dropped middle for a gap.
        gap_pre_pause: set[int] = set()
        if args.debug_cuts > 0:
            _pk = None
            for k in _emit_idx:
                if _pk is not None and action_for.get(k) != "exit":
                    g = (group[k].dt - (group[_pk].dt
                         + timedelta(seconds=group[_pk].duration))).total_seconds()
                    if g > args.inter_clip_gap_secs:
                        gap_pre_pause.add(_pk)
                _pk = k

        intermediates: list[Path] = []
        # Running position in the OUTPUT video (playable seconds from the
        # start). Advanced by each emitted clip's played length and by each
        # 'Fast forwarding…' slide (TRANSITION_SECS). Used to stamp each
        # interior stop with where its FF slide lands in the final video.
        video_secs = 0.0
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
            # Drive-mode head trim: pre-motion clips dropped wholesale.
            if action == "head_skip":
                continue

            # Inter-clip gap detection: insert a 'Fast forwarding…' slide when
            # the wall-clock distance from the previous emitted clip exceeds
            # the threshold. Parking-run exits ALREADY have their own FF
            # inserted by the entry side, so skip in that case. A trip contains
            # engine-off gaps of any length (all interior stops stay in the
            # trip), so this fires whenever a within-trip gap is long enough.
            if (prev_emitted_clip is not None and action != "exit"):
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
                    # Record the stop: its FF slide lands at the current output
                    # position; the fix nearest the pre-gap moment gives lat/lon.
                    _fix = _nearest_track_fix(group_track, prev_end + _gps_time_offset)
                    if _fix is not None:
                        stops.append({
                            "video_secs": round(video_secs, 2),
                            "lat": _fix[0], "lon": _fix[1],
                            "park_secs": round(float(gap_secs or 0), 1),
                        })
                    intermediates.append(gap_trans)
                    video_secs += TRANSITION_SECS
                    # After a gap-FF, treat this clip as a parking-exit so the
                    # engine-on-but-not-moving head gets trimmed (the same way
                    # parking-detected exits do). Without this, the next clip
                    # plays from second 0 = engine-just-on, showing 20+s of
                    # parked footage before the wheels turn even though we
                    # just told the viewer we "fast-forwarded" past it.
                    if action is None:
                        action = "exit"

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
            if action in ("entry", "entry_end"):
                # Entry slice anchors on within-clip park onset (when GPS speed
                # first sustains below threshold) + entry_pad, so the slice ends
                # entry_pad seconds AFTER the car actually stopped. "entry_end"
                # is a trailing park that closes the trip: same arrival slice,
                # but the render loop emits no FF transition after it.
                park_sec = park_sec_for_entry.get(ci0, 0)
                trim_seconds = park_sec + entry_pad
            elif action == "exit":
                # Prefer VIDEO ego-motion to anchor the drive-away — GPS speed is
                # unreliable here (parking-mode snippets are full of passing
                # people/cars). find_drive_away_by_video pinpoints the wheels
                # turning; keep a small EGO_CONTEXT_PAD before it. Silently fall
                # back to GPS drive-resume, then a fixed skip, when video isn't
                # available (no numpy/opencv) or finds nothing. The user only
                # cares that the cut is clean, not how it's found; the choice to
                # KEEP the parking movements instead is --no-skip-parking. The
                # --no-video-drive-detect knob forces GPS-only (advanced).
                vid_sec = (None if args.no_video_drive_detect
                           else find_drive_away_by_video(clip))
                if vid_sec is not None:
                    trim_start = max(0, int(vid_sec) - EGO_CONTEXT_PAD)
                    print(f"        exit: video drive-away at {vid_sec:.1f}s "
                          f"→ trim from {trim_start}s")
                else:
                    drive_sec = find_drive_resume_second(
                        clip, gps_dirs,
                        sustain_secs=args.drive_resume_sustain_secs,
                    )
                    if drive_sec is None:
                        trim_start = min(args.exit_skip_secs, max(0, clip.duration - exit_pad))
                    else:
                        trim_start = max(0, drive_sec - exit_pad)
                trim_seconds = None     # run to end of clip
            elif ci0 == head_skip_count and with_speed:
                # The first NON-SKIPPED clip of the trip = the departure/motion
                # clip. head_trim_for_motion_clip was computed in the pre-pass
                # above. Use it directly as the trim_start so the trip opens
                # just before the wheels start turning.
                trim_start = head_trim_for_motion_clip

            # END-TRIM: the trip's final clip otherwise plays out to the end of
            # its minute, leaving up to ~a minute of already-parked footage after
            # the car has come to rest. Find the park with the same video
            # ego-motion detector used for drive-away and stop EGO_END_PAD
            # seconds after it. (A trailing parking RUN is already handled by
            # `entry_end`; this covers the common case where the trip simply ends
            # with the car pulling in.)
            if action is None and ci0 == last_emit_idx and trim_seconds is None:
                park_sec = (None if args.no_video_drive_detect
                            else find_park_second_by_video(clip))
                how = "video"
                if park_sec is None:
                    # Video needs trackable features; a night arrival can be too
                    # dark to give any. GPS is reliable for a sustained stop.
                    park_sec = find_park_second_by_gps(clip, gps_dirs)
                    how = "gps"
                if park_sec is not None and int(park_sec) >= trim_start:
                    trim_seconds = max(1, int(park_sec) - trim_start + EGO_END_PAD)
                    print(f"        end-trim ({how}): parks at {park_sec:.1f}s → "
                          f"stop after {trim_seconds}s")

            # --debug-cuts preview: keep only the transition moments with `N`
            # secs of context each, and drop the driving middles. This runs AFTER
            # the normal trims are computed (start/exit trim_starts, entry slice
            # length) so it just narrows them to a short window.
            if args.debug_cuts > 0:
                N = args.debug_cuts
                if action in ("entry", "entry_end"):
                    # last N secs of the entry (pre-FF) slice = the car stopping
                    full = park_sec_for_entry.get(ci0, 0) + entry_pad
                    trim_start = max(0, full - N)
                    trim_seconds = min(N, full)
                elif action == "exit":
                    # first N secs after drive-resume (trim_start already set)
                    trim_seconds = N
                elif ci0 in gap_pre_pause:
                    # last N secs before an inter-clip gap-FF = the "before FF" side
                    trim_start = max(0, clip.duration - N)
                    trim_seconds = N
                elif ci0 == first_emit_idx:
                    # start: first N secs of the departure (trim_start = head-trim)
                    trim_seconds = N
                elif ci0 == last_emit_idx:
                    # stop: last N secs of the final clip
                    trim_start = max(0, clip.duration - N)
                    trim_seconds = N
                else:
                    # a driving middle — drop it, but keep prev_emitted_clip
                    # honest so the NEXT clip's gap detection sees the true
                    # wall-clock gap (not an inflated one from dropped clips).
                    prev_emitted_clip = clip
                    continue

            # Per-slice intermediate filename. Suffix the action so re-runs
            # can find / cache them correctly. The requested output_height is
            # also baked into the filename, otherwise switching between
            # 1080p / 720p / 540p in config would silently reuse old cached
            # MP4s at the previous size (which is why "tweaking
            # output_height in config does nothing" felt broken before).
            suffix = f"_{action}" if action else ""
            size_tag = f"_h{args.output_height or OUT_H}"
            inter = work_dir / (
                f"{group_kind}{idx:02d}_clip{ci:03d}"
                f"_{clip.timestamp}{suffix}{size_tag}.mp4"
            )

            # Per-clip map widget video (trimmed if we're trimming the video).
            # Like the main intermediate, always regenerated — caching was the
            # source of stale-trim bugs when head-trim params changed between
            # runs.
            map_video: Path | None = None
            if with_map_widget:
                map_video = inter.with_suffix(".map.mp4")
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

            # Intermediates are scratch space, always re-encoded. The directory
            # was wiped at the start of the run, so inter.exists() is False
            # except in the rare case where two drives in the same run share a
            # clip (the same one re-targeted) — and even then, re-encoding is
            # cheap and safe.
            if action:
                secs_part = f", {trim_seconds}s" if trim_seconds is not None else ""
                tag = f" ({action} slice{secs_part})"
            else:
                tag = ""
            print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}{tag}  encoding ...")
            encode_clip(
                clip, inter, font_path, use_vt, with_timestamp,
                gps_dirs, with_speed, map_video=map_video,
                trim_start=trim_start, trim_seconds=trim_seconds,
                no_audio=args.no_audio, output_height=args.output_height,
            )
            intermediates.append(inter)
            # Advance the output-position clock by this clip's played length.
            # Runs for every emitted clip exactly once (skips/head-skips and the
            # debug-cuts middle-drop all `continue` before reaching here).
            video_secs += (trim_seconds if trim_seconds is not None
                           else (clip.duration - trim_start))

            # After the entry slice of a parking run, splice in the transition.
            if action == "entry":
                trans = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_transition.mp4"
                skipped = skipped_secs_for.get(ci0)
                # Record the stop: the FF slide plays right after this entry
                # slice (video_secs already includes it). Park onset time =
                # clip start + park_sec (this entry clip IS the run's start).
                _stop_dt = clip.dt + timedelta(seconds=park_sec)
                _fix = _nearest_track_fix(group_track, _stop_dt + _gps_time_offset)
                if _fix is not None:
                    stops.append({
                        "video_secs": round(video_secs, 2),
                        "lat": _fix[0], "lon": _fix[1],
                        "park_secs": round(float(skipped or 0), 1),
                    })
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
                video_secs += TRANSITION_SECS

            # Remember this clip so the next iteration can measure the gap.
            prev_emitted_clip = clip

        print(f"  concatenating {len(intermediates)} clips -> {final.name}")
        concat_clips(intermediates, final)
        print(f"  ✓ {final}")

        # Now that the render loop has populated `stops`, write the meta. This
        # is the meta write for a real render (the pre-render write only happens
        # in --sidecars-only mode); `stops` reflects the actual output video.
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

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
