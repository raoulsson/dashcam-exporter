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
PIP_W, PIP_H = 576, 324                        # rear inset (was 480x270; +20%)
PIP_MARGIN   = 24
TS_FONT_SIZE = 36
SPEED_FONT_SIZE = 35                           # was 44; -20%

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


def parse_gpx_speeds(gpx_path: Path) -> list[float]:
    """Return per-second km/h values parsed from the NMEA $GPRMC lines in a GPX file."""
    speeds: list[float] = []
    try:
        with gpx_path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("$GPRMC"):
                    fields = line.split(",")
                    # $GPRMC,time,status,lat,N,lon,E,speed_knots,heading,date,...
                    if len(fields) >= 8 and fields[2] == "A":
                        try:
                            knots = float(fields[7])
                            speeds.append(knots * KNOTS_TO_KMH)
                        except ValueError:
                            pass
    except OSError:
        pass
    return speeds


def find_gpx_for(timestamp: str, gps_dir: Path) -> Path | None:
    """Match a clip timestamp like '20260511180649' to its GPX file (any duration suffix)."""
    if not gps_dir.is_dir():
        return None
    for f in os.listdir(gps_dir):
        m = GPX_RE.match(f)
        if m and m.group(1) == timestamp:
            return gps_dir / f
    return None


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
        # libass force_style: bottom-left, sitting above the timestamp box
        # Timestamp box is ~24+TS_FONT_SIZE+2*boxborderw ≈ 80 px from the bottom,
        # so push the speed readout up so its bottom sits ~96 px above the frame bottom.
        style = (
            f"Alignment=1,FontName=Courier New,FontSize={SPEED_FONT_SIZE},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
            "BackColour=&H80000000,BorderStyle=4,Outline=2,Shadow=0,"
            "MarginV=96,MarginL=24"
        )
        # Single-quote the path so colons inside it don't get parsed as option separators
        chain += f",subtitles=filename='{speed_srt.as_posix()}':force_style='{style}'"

    return chain + "[out]"


def encode_clip(
    clip: Clip,
    out_path: Path,
    font_path: str,
    use_vt: bool,
    with_timestamp: bool,
    gps_dir: Path | None,
    with_speed: bool,
) -> None:
    # If GPS data exists for this clip, write a sidecar SRT and pass it to the filter
    speed_srt: Path | None = None
    if with_speed and gps_dir is not None:
        gpx = find_gpx_for(clip.timestamp, gps_dir)
        if gpx is not None:
            speeds = parse_gpx_speeds(gpx)
            srt_path = out_path.with_suffix(".speed.srt")
            if write_speed_srt(speeds, srt_path):
                speed_srt = srt_path

    filt = build_filter_complex(font_path, clip.epoch_utc, with_timestamp, speed_srt)
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
        "-filter_complex", filt,
        "-map", "[out]", "-map", "0:a?",
        *venc,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "96k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


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
    subprocess.run(cmd, check=True)
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
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    front_dir = root / "DCIM" / "200video" / "front"
    rear_dir  = root / "DCIM" / "200video" / "rear"
    gps_dir   = root / "DCIM" / "203gps"
    if not front_dir.is_dir() or not rear_dir.is_dir():
        print(f"ERROR: expected {front_dir} and {rear_dir}", file=sys.stderr)
        return 1
    gps_dir = gps_dir if gps_dir.is_dir() else None

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
    with_speed = not args.no_speed and gps_dir is not None
    if with_speed and not has_subtitles():
        print("WARNING: ffmpeg lacks the 'subtitles' filter (libass missing); speed overlay disabled.",
              file=sys.stderr)
        with_speed = False
    n_gpx = 0
    if gps_dir is not None:
        n_gpx = sum(1 for f in os.listdir(gps_dir) if GPX_RE.match(f))

    print(f"Encoder:   {encoder_name}")
    print(f"Timestamp: {'on (' + font_path + ')' if with_timestamp else 'off'}")
    if with_speed:
        print(f"Speed:     on (GPS data for {n_gpx} clips found in {gps_dir})")
    elif gps_dir is None:
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

        if final.exists():
            print(f"\n[{group_word} {idx}/{len(groups)}] {final.name} already exists — skipping (delete to re-encode)")
            continue

        print(f"\n[{group_word} {idx}/{len(groups)}] {start:%Y-%m-%d %H:%M} → {end:%H:%M}  "
              f"({len(group)} clips, ~{fmt_secs(secs)})")

        intermediates: list[Path] = []
        for ci, clip in enumerate(group, 1):
            inter = work_dir / f"{group_kind}{idx:02d}_clip{ci:03d}_{clip.timestamp}.mp4"
            if not inter.exists():
                print(f"  [{ci:>3}/{len(group)}] {clip.timestamp}  encoding ...")
                encode_clip(clip, inter, font_path, use_vt, with_timestamp, gps_dir, with_speed)
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
