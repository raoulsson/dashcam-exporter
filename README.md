# dashcam-exporter (for "DDPAI Mola N3 Pro" model)

Turn the raw front + rear clips from a DDPAI dashcam SD card into one polished
1080p/1440p (with side map) MP4 per drive or per day, with burned-in date/time,
GPS speed, and an animated route map.

Tested with **DDPAI Mola N3 Pro**. Should work with any
DDPAI variant that lays out the card as:

    /Volumes/NO NAME/DCIM/
        200video/front/   YYYYMMDDhhmmss_NNNN.mp4
        200video/rear/    YYYYMMDDhhmmss_NNNN_A.mp4
        203gps/           YYYYMMDDhhmmss_NNNN_D.gpx        # loose NMEA logs
        203gps/tar/       YYYYMMDDhhmmss_NNNN.git          # tarred NMEA logs


## What it does

For each "drive" (a run of consecutive clips, default ≤90s gap) — or each
calendar day in `--daily` mode — the script produces:

| File | What it is |
|------|------------|
| `drive_NN_YYYY-MM-DD_HH-MM.mp4` | Final video. 2400×1080 (with map widget) or 1920×1080. |
| `drive_NN_…html`                | Self-contained Leaflet/OSM interactive map. |
| `drive_NN_…gpx`                 | Standards-compliant GPX. Opens in Google Earth, Strava, Maps.me, Komoot. |
| `drive_NN_…_links.txt`          | Google Maps + Apple Maps URLs and trip stats. |

The video frame layout:

```
+---------------------------------------+----------+
|                                       |          |
|                                       |          |
|              FRONT CAMERA             | (black)  |
|                                       |          |
|                                       |  +----+  |
|                                       |  |MAP |  |
|        +------------------+           |  |    |  |
|        |   REAR CAMERA    |           |  +----+  |
|        +------------------+           |          |
|                                       |          |
| 2026-05-11 18:07:52     18 km/h       | (black)  |
+---------------------------------------+----------+
       1920 px (video)                    480 px
                       2400 × 1080
```

Overlays, in order of how the filter graph composes them:

1. **Front camera** — cropped from 2560×1600 to 16:9, scaled to 1920×1080.
2. **Rear PiP** — 662×372 with thin white border, sitting centered along the
   bottom edge (covers the bonnet reflection nicely).
3. **Timestamp** — `YYYY-MM-DD HH:MM:SS` burned in the bottom-left corner,
   advancing per frame from the clip's filename timestamp.
4. **Speed** — `NN km/h` rendered as 1-second SRT subtitles in the bottom-right
   corner. Only present when GPS data exists for the clip.
5. **Map widget** — 480×480 panel hstacked on the right side. Shows the full
   drive's route coloured by speed, with a red marker dot moving once per
   second to the current GPS position. Only present when the drive has any
   GPS data.

If the drive has no GPS at all, the script falls back to plain 1920×1080 output
(no map widget, no speed overlay) so the per-drive output sizes stay
consistent within a single run.


## Install

The script needs **ffmpeg** with `drawtext` (libfreetype) and `subtitles`
(libass) filters built in. The plain Homebrew `ffmpeg` formula doesn't include
those — you need `ffmpeg-full`:

```sh
brew install ffmpeg-full
brew unlink ffmpeg 2>/dev/null
brew link --overwrite ffmpeg-full
```

Then the Python dependencies (only needed for the burn-in map widget):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The venv step matters because Homebrew's Python 3.12+ refuses system-wide
`pip install` per PEP 668 (you'll see *"externally-managed-environment"* if
you skip the venv). Re-activate the venv (`source .venv/bin/activate`) at the
start of every new terminal session before running the script.

The deps are `staticmap` for OSM-tile background fetching and `Pillow` for
marker compositing. If you don't install them, the script still runs but
skips the burn-in map widget — pass `--no-map-widget` to suppress the
warning.


## Quick start

```sh
cd ~/dev/dashcam-exporter

# Dry-run to see what would be encoded
python3 make_dashcam_videos.py --dry-run

# Encode every drive on the card with full overlays
python3 make_dashcam_videos.py

# Same but merge by date (one MP4 per calendar day)
python3 make_dashcam_videos.py --daily

# Only specific drives
python3 make_dashcam_videos.py --drives 13 14

# Send outputs elsewhere
python3 make_dashcam_videos.py --daily --out ~/Movies/Dashcam
```

The default input is `/Volumes/NO NAME` and the default output is
`~/Desktop/Dashcam_Videos/`. Override with `--root` and `--out`.


## CLI flags

| Flag | Effect |
|------|--------|
| `--root PATH`           | Dashcam volume root. Default `/Volumes/NO NAME`. |
| `--out PATH`            | Output folder. Default `~/Desktop/Dashcam_Videos`. |
| `--daily`               | Group clips by calendar date instead of by gap. |
| `--gap N`               | Seconds-between-clips threshold for grouping (default 90, drives-mode only). |
| `--drives N [N …]`      | Only process specific group numbers (1-based; works for both modes). |
| `--software`            | Force libx264 software encode instead of VideoToolbox hardware. |
| `--no-timestamp`        | Skip the date/time overlay. Required if your ffmpeg lacks `drawtext`. |
| `--no-speed`            | Skip the GPS speed overlay even when GPX data exists. |
| `--no-map-sidecars`     | Skip the `.html`, `.gpx`, and `_links.txt` sidecars. |
| `--no-map-widget`       | Skip the burn-in map panel (output stays 1920×1080). |
| `--keep-intermediates`  | Don't delete per-clip processed files after concat. |
| `--dry-run`             | List drives/days and exit without encoding. |


## How grouping works

The script reads every front clip filename, pairs it with the matching rear
clip, and orders them by timestamp.

In **drive mode** (default), a new drive starts whenever the gap between the
end of one clip and the start of the next exceeds `--gap` seconds (default 90).
This usually corresponds to engine-off events.

In **`--daily` mode**, all clips on the same calendar date go into one group,
regardless of how long the engine was off between them.


## How GPS speed is sourced

The dashcam writes GPS logs to `DCIM/203gps/` in NMEA format (mislabeled as
`.gpx`). For older sessions it rolls them up into POSIX tar archives in
`203gps/tar/` mislabeled as `.git`.

On startup the script:

1. Lists every loose `.gpx` in `203gps/`.
2. Extracts every `.gpx` member from every `.git` tar in `203gps/tar/` into
   `OUT_DIR/.gpx_cache/` (cached across runs).
3. For each clip, looks for a matching `.gpx` in either location.

The speed overlay is rendered per second from the `$GPRMC` speed-in-knots
field, converted to km/h.

For the demo card this expanded GPS coverage from **25 clips → 90 clips**
(out of 117).


## Output layout

After a typical `--daily` run:

```
~/Desktop/Dashcam_Videos/
├── day_2026-04-02.mp4
├── day_2026-04-11.mp4
├── day_2026-04-22.mp4
├── …
├── day_2026-05-11.mp4
├── day_2026-05-11.html
├── day_2026-05-11.gpx
├── day_2026-05-11_links.txt
├── .gpx_cache/              # harvested tar contents, reused across runs
└── .intermediates/          # per-clip work, cleaned up unless --keep-intermediates
```


## Architecture

Pipeline per clip:

```
front.mp4  ─┐
            ├─► ffmpeg filter_complex:
rear.mp4   ─┤    crop + scale front  →  [front]
            │    scale + border rear  →  [rear]
            │    overlay [rear] on [front] at bottom-center
            │    drawtext timestamp (bottom-left)
            │    subtitles speed.srt (bottom-right) ─── from per-clip SRT
            │    [video_part]
map.mp4    ─┤    [2:v] scale to 480×480 + pad to 480×1080  →  [map_part]
            │    hstack [video_part][map_part]  →  [out]
            ▼
        clip_NN.mp4  (intermediate, 2400×1080)
            ▼
        concat-demuxer  (stream-copy, no re-encode)
            ▼
        final drive_NN.mp4
```

The map.mp4 is itself produced by PIL/staticmap:

```
all GPX points for the drive  →  base panel PNG (route + start/end markers)
                                  +
each second of clip            →  marker dot composited on base PNG
                                  ↓
                              PNG sequence
                                  ↓
                          ffmpeg 1-fps mp4  →  map.mp4
```

The 1-fps map gets upsampled to 30 fps by the main filter chain, so the marker
visibly jumps once per second.


## Performance

Hardware-accelerated encoding via `h264_videotoolbox` on an Apple-silicon Mac
gets you roughly 5–10× realtime, so ~2 hours of source footage encodes in
15–25 minutes. Map widget rendering adds a couple seconds per clip for PIL
work plus tile fetches (cached locally inside `staticmap`).

The script is **restartable**:

- If a final `.mp4` already exists in `--out`, that drive/day is skipped.
- Per-clip intermediates in `.intermediates/` are reused if present.
- Harvested GPX in `.gpx_cache/` is reused across runs.

To force a re-encode of one drive: delete its final `.mp4` (and the matching
intermediates if you want fresh per-clip work too).


## Troubleshooting

**`No such filter: 'drawtext'`** — Your ffmpeg was built without libfreetype.
Install `ffmpeg-full` (see Install above), or re-run with `--no-timestamp`.

**`Unable to open … speed.srt` / `subtitles` errors** — Your ffmpeg lacks
libass. Install `ffmpeg-full`, or re-run with `--no-speed`.

**`no rear pair for YYYYMMDDhhmmss, skipping`** — A front clip exists with no
matching rear file. The script silently drops it.

**`map: (no GPS data for this day)`** — The clip filenames in that group don't
match any GPX (loose or tarred). For the demo card this is normal for the
early-April / early-May days; the dashcam either didn't have GPS lock or those
logs were overwritten by loop recording.

**`All samples in data stream … have zero duration`** — Harmless. That's the
DDPAI custom telemetry track inside the source MP4; ffmpeg ignores it.

**Output looks squashed horizontally** — Your player isn't honouring SAR.
The video is yuv420p with square pixels; reload it or try a different player
(QuickTime, VLC, IINA all handle it correctly).

**OSM tile fetch failures during map widget render** — Check the user's
internet, or pass `--no-map-widget`. The HTML map (Leaflet) still works
fine without the widget since OSM tiles are loaded in your browser at view
time.

**`error: externally-managed-environment`** — Homebrew's Python blocks
system-wide `pip install` (PEP 668). Use the venv recipe in Install.

**`! map widget skipped: PIL/Pillow not installed`** — You forgot to
activate the venv before running, or never ran `pip install -r
requirements.txt`. The video will still encode at 1920×1080 with all other
overlays — only the right-side map panel is missing.


## Repo layout

```
dashcam-exporter/
├── make_dashcam_videos.py     # the single-file script (entry point)
├── requirements.txt           # Pillow + staticmap (only for burn-in widget)
├── .gitignore
└── README.md                  # this file
```


## License

MIT — see [LICENSE](LICENSE).

Copyright © 2026 Raoul Marc Schmidiger ([hello@raoulsson.com](mailto:hello@raoulsson.com)).
