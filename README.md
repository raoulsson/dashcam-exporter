# dashcam-exporter (for DDPAI dashcams)

Turn the raw front + rear clips from a DDPAI dashcam SD card into one polished
MP4 per **trip** — with a moving GPS map widget, speed overlay, date/time
burn-in, automatic parking-skip, and per-trip HTML / GPX / Google Maps /
metadata sidecars.

A **trip** is the publishing unit: everything from leaving an anchor until the
car returns to it and parks — or the 04:00 day rollover ends it. Short interior
stops (fuel, lunch, a hike) don't split a trip; they become "Fast forwarding…"
slides so the trip plays as one continuous video.

> **DDPAI only.** The card layout, GPS log format, and clip naming convention
> are specific to DDPAI cameras. Tested with **DDPAI Mola N3 Pro**. Should work
> with any DDPAI variant that uses the layout shown below.
>
> **Developed and tested on macOS.** It should work on Linux. **It has not
> been tested on Windows** — the basics (Python, ffmpeg, paths) are all
> cross-platform via `pathlib`, but font detection and default file paths are
> tuned for macOS. If you try it on Windows please open an issue.


## Contents

This manual is in three tiers. Read tier 1, skim tier 2 when something looks
wrong, and read tier 3 only if you want to know why it works.

| Tier | For | Section |
|------|-----|---------|
| 1 | Everyone | [🚗 Quick start / everyday use](#tier-1--quick-start--everyday-use) |
| 2 | Tuning the look and the cuts | [🎛 All the options](#tier-2--all-the-options) |
| 3 | How it actually works | [🔬 For the nerds — how it works internally](#tier-3--for-the-nerds--how-it-works-internally) |

Then: [Troubleshooting](#troubleshooting) · [Repo layout](#repo-layout) ·
[Funding](#funding) · [License](#license)


## Example output

Default layout — front camera, rear PiP bottom-centre, timestamp + speed +
watermark in corners, stats panel + map widget on the right:

![Default frame layout](examples/dash-default-view.png)

Same composition with the rear PiP repositioned to the top-left corner
(`rear_pip_position = top-left` in config.txt):

![Rear PiP in top-left](examples/dash-rear-view-top-left.png)

The interactive HTML map sidecar (Leaflet + OSM tiles, route coloured by
speed, segment-break dots, opens in any browser):

![Interactive HTML map sidecar](examples/gps-data-on-map.png)

The standard `.gpx` sidecar opened in [gpx.studio](https://gpx.studio) —
because the script emits one `<trkseg>` per contiguous-driving segment, each
engine-on leg of the trip shows up as its own colored polyline so you can see
the whole trip at a glance:

![Per-trip GPX in gpx.studio](examples/gps-data-single-drives-on-gpx.studio.png)


---

# TIER 1 — Quick start / everyday use

## What this tool is

You drive with a DDPAI dashcam. The card fills up with hundreds of 1-minute
front clips, matching rear clips, and NMEA GPS logs. This tool reads that mess
and produces, **per trip**, one watchable MP4 plus sidecars:

- front camera full-frame, rear camera as a picture-in-picture inset,
- burned-in date/time and GPS speed,
- a side panel with trip stats and a real OSM map with a marker that moves,
- parked stretches collapsed into a `Fast forwarding… 46m 15s skipped` slide,
- `.html` / `.gpx` / `_links.txt` / `_meta.json` sidecars next to the video.

### Expected SD-card layout

    /Volumes/NO NAME/DCIM/
        200video/front/   YYYYMMDDhhmmss_NNNN.mp4
        200video/rear/    YYYYMMDDhhmmss_NNNN_A.mp4
        203gps/           YYYYMMDDhhmmss_NNNN_D.gpx        # loose NMEA logs
        203gps/tar/       YYYYMMDDhhmmss_NNNN.git          # tarred NMEA logs

(Both mislabeled by the firmware: the `.gpx` files are NMEA, and the `.git`
files are plain POSIX tar archives, not Git data.)


## Install

### macOS

The script needs **ffmpeg** with the `drawtext` (libfreetype) and `subtitles`
(libass) filters. The plain Homebrew `ffmpeg` doesn't include those — use
`ffmpeg-full`:

```sh
brew install ffmpeg-full
brew unlink ffmpeg 2>/dev/null
brew link --overwrite ffmpeg-full
```

Then the Python dependencies, in a virtualenv:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The venv matters because Homebrew Python 3.12+ is *externally managed* and
blocks system-wide `pip install` per PEP 668. Re-activate the venv
(`source .venv/bin/activate`) at the start of every new terminal session.

What the dependencies buy you:

| Package | Needed for | Without it |
|---------|-----------|------------|
| `Pillow` | map-widget marker compositing | burn-in map panel skipped (pass `--no-map-widget` to silence the warning) |
| `staticmap` | OSM tile background in the map panel | PIL polyline fallback / no map |
| `numpy` + `opencv-python-headless` | **video ego-motion** — the good trip boundary and drive-away detection | falls back to GPS-radius grouping and GPS-only drive-away (faster, less accurate) |

numpy and opencv are optional but strongly recommended: they are what make the
trip boundaries and the parking cuts land on the real pull-out and pull-in.

### Linux / WSL

```sh
sudo apt install ffmpeg                    # usually includes drawtext + subtitles
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Windows (untested)

Best-effort guesses based on cross-platform behaviour. **The script has not
been tested on Windows.**

```powershell
winget install Gyan.FFmpeg          # or: choco install ffmpeg-full / scoop install ffmpeg
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Watch out for: `root = /Volumes/NO NAME` won't exist (set `root = E:\`);
VideoToolbox doesn't exist so encoding falls back to software libx264;
font detection tries macOS fonts first then `courbd.ttf` / `cour.ttf` /
`arial.ttf` — if none are found, run with `--no-timestamp` and an empty
`watermark_text`.


## The everyday workflow

Three steps, in order.

### 1. Import the card

```sh
./import-sd-card.sh                  # -> ~/dashcam-data/import_sink/<today>
./import-sd-card.sh 2026-07-20       # name the day folder explicitly
./import-sd-card.sh --keep           # copy + verify, but DON'T delete the card
./import-sd-card.sh --checksum       # rigorous byte-for-byte verify (slow)
./import-sd-card.sh --src "/Volumes/OTHER" 2026-07-20
```

It rsyncs the card's whole `DCIM` tree into `import_sink/<day>/`, **verifies
the copy**, and only then deletes the card's **files** — keeping the `DCIM/…`
folder tree in place so the dashcam can keep recording into it. Nothing on the
card is deleted until the copy has been verified. Re-importing the same day
merges into the existing folder. Override the import root with the
`DASHCAM_IMPORT_ROOT` environment variable.

### 2. See what's on it (dry run)

```sh
./list-trips-data.sh --root ~/dashcam-data/import_sink/2026-05-11 \
                     --out  ~/dashcam-data/output
```

Lists every trip with its 1-based index, 04:00-day label, start → end, clip
count and duration — and encodes nothing:

```
Trip  1  day 2026-04-02  2026-04-02 12:30 -> 04-02 12:31     1 clips  ~1m
Trip  2  day 2026-04-11  2026-04-11 21:16 -> 04-11 21:24     3 clips  ~8m
…
Trip  8  day 2026-05-11  2026-05-11 12:11 -> 05-11 19:07   104 clips  ~1h45m
```

### 3. Render

```sh
./make-trips-rendered.sh --root ~/dashcam-data/import_sink/2026-05-11 \
                         --out  ~/dashcam-data/output        # all trips
./make-trips-rendered.sh 8 --root … --out …                      # only trip 8
./make-trips-rendered.sh 1 2 --root … --out …                    # trips 1 and 2
```

Leading bare integers are trip indices (they become `--drives N N …`). The
first non-integer argument ends index parsing; everything from there on is
passed straight through to the Python script. With no integers, every trip is
encoded.

Both shell scripts also have a commented `OPTS+=(…)` block near the top — put
the `--root` / `--out` / `--output-height` you always use there once and stop
typing them. Anything in `config.txt` is loaded automatically and doesn't need
to be in the scripts at all.

Indices are stable within a run but **shift if you change a trip threshold**
(`--trip-return-m`, `--trip-leave-m`, `--trip-day-rollover`), because the
grouping itself changes. Always `--dry-run` again after a change.

### Or call the script directly

```sh
source .venv/bin/activate
python3 make_dashcam_videos.py --dry-run                  # list trips, encode nothing
python3 make_dashcam_videos.py                            # encode everything
python3 make_dashcam_videos.py --drives 8                 # --trips 8 is an alias
python3 make_dashcam_videos.py --root ~/backup --out ~/Movies/Dashcam
python3 make_dashcam_videos.py --sidecars-only            # refresh sidecars only
python3 make_dashcam_videos.py --output-height 540        # phone-sized file
```


## Where the output lands

One subfolder **per extract day** (the 04:00-rollover day a trip belongs to,
which need not match the import folder's name since one card can span several
days). Each day folder holds its trips plus an `info.txt` naming the source
import folder:

```
<--out>/                              # e.g. ~/dashcam-data/output/
├── 2026-05-11/
│   ├── info.txt                      # "source import folder: …/import_sink/2026-05-11"
│   ├── trip_2026-05-11_12-11_08_h1080.mp4
│   ├── trip_2026-05-11_12-11_08.html
│   ├── trip_2026-05-11_12-11_08.gpx
│   ├── trip_2026-05-11_12-11_08_links.txt
│   ├── trip_2026-05-11_12-11_08_meta.json
│   └── trip_2026-05-11_19-40_09_…    # a second trip on the same day
├── 2026-07-15/
│   └── …                             # trips extracted from an import that spanned days
├── .gpx_cache/                       # harvested tar contents, reused across runs
└── .intermediates/                   # scratch — wiped at the start of every run
```

### What you get per trip

All filenames lead with the trip's **day label** (the 04:00-rollover date), so
a publishing UI can glob a whole day's trips by prefix.

| File | What it is |
|------|------------|
| `trip_YYYY-MM-DD_HH-MM_NN_h1080.mp4` | Final video. Name is `trip_<day>_<start-time>_<global-index>_<size-tag>`, e.g. `trip_2026-05-11_12-11_08_h1080.mp4`. Composed at 2402×1080 with map widget (or 1920×1080 without) and downscaled to the chosen height. The size tag is always present and reflects `output_height` (1080 default = native; 720 for half the size, 540 for phone-sized). VT bitrate auto-scales to match, so smaller heights mean proportionally smaller files. Rendering at multiple heights produces side-by-side files instead of overwriting. |
| `trip_….html` | Self-contained Leaflet/OSM interactive map. Un-tagged — one per trip regardless of video size. |
| `trip_….gpx` | Standards-compliant GPX. Opens in Google Earth, Strava, Maps.me, Komoot. |
| `trip_…_links.txt` | Google Maps + Apple Maps URLs and trip stats. |
| `trip_…_meta.json` | Machine-readable trip sidecar: `trip_index`, `day` (04:00 label), `start`, `end`, `duration_secs`, `n_clips`, `video`, `round_trip` (bool — `false` means a one-way relocation), `start_fix`, `end_fix`, `distance_km`. Un-tagged — one per trip. Meant for a publishing UI to group a day's trips and know each trip's shape without re-parsing GPS. |

### The video frame

```
+----------------------------------------+----------+
|                                        | Trip 8   |
|                                        | 2026-…   |
|              FRONT CAMERA              | Distance |
|                                        | Driven   |
|                                        | Max …    |
|                                        | Avg …    |
|                                        |          |
|         +------------------+           | +------+ |
|         |   REAR CAMERA    |           | | MAP  | |
|         +------------------+           | |      | |
|                                        | +------+ |
|                                        |          |
| 2026-05-11 18:07:52         19 km/h    |          |
|                      (c) Watermark …   |          |
+----------------------------------------+----------+
       1920 px main video                  480 px panel
                       2402 × 1080
```

When a trip has no GPS at all, the script falls back to plain 1920×1080
output (no map widget, no speed overlay) so per-trip output sizes stay
consistent within a run.


## The handful of flags a normal person needs

| Flag | Why you'd use it |
|------|------------------|
| `--dry-run` | List trips and their indices; encode nothing. Always do this first. |
| `--force` | Re-encode a trip whose `.mp4` already exists (otherwise it's skipped). |
| `--sidecars-only` | Regenerate `.html` / `.gpx` / `_links.txt` / `_meta.json` without touching video. Fast. |
| `--output-height 540` | Smaller file for phone/messaging. `1080` (native) is the default; `720` roughly halves the size. |
| `--no-audio` | Strip audio — passenger conversation privacy. |
| `--drives N [N …]` (alias `--trips`) | Encode only these trip indices. Also bypasses the fragment auto-skip. |
| `--debug-cuts [SECS]` | The fast "did the cuts land right?" preview — see below. |

### `--debug-cuts` — check the cuts in 20 seconds

```sh
./make-trips-rendered.sh 8 --debug-cuts        # bare = 5 s of context per cut
./make-trips-rendered.sh 8 --debug-cuts 8
```

Renders **only** the trip's cut points — the start, each parking / fast-forward
pause with N seconds of context either side of the slide, and the stop —
dropping all the driving in between. A ~20 second clip instead of a full
render, written as a separate `*_debugcuts*.mp4` so it never overwrites a real
output. This is the right tool when a parking cut lands somewhere odd and you
want to iterate on `parking_entry_pad` / `parking_exit_pad`.


## Restartability and logs

- **A full render starts fresh — but only for the days it writes.** Before
  encoding, each destination day folder is cleared, so a re-render (whose trip
  indices may have shifted after a re-group) leaves no stale `trip_*` behind.
  Only the day folders **this run renders** are touched: rendering the
  `2026-07-19` import clears its `2026-07-15..18` days and never disturbs an
  unrelated `2026-05-11` from another import sharing the same `--out`. Hidden
  entries inside a day folder, and the `--out`-root caches (`.gpx_cache`,
  `.geocode_cache.json`), are always kept. The reset is **skipped** for a
  `--drives` subset (targeted / resume-like) and for `--sidecars-only`; pass
  `--no-clean-days` to skip it on a full render too. Because a full render
  resets its days, re-running it redoes those trips rather than resuming — to
  resume (keep completed trips, add the rest), use `--no-clean-days` or a
  `--drives` subset, where an existing `.mp4` is skipped unless you pass
  `--force`.
- Sidecars are emitted unconditionally, even when the `.mp4` exists, so a
  palette / unit / segmentation tweak lands via a quick `--sidecars-only` run.
- `make-trips-rendered.sh` tees stdout+stderr into a `render-YYYYMMDD-HHMMSS.log`
  **inside the output dir**, so the paper trail ships with the render. At the
  end of the run a copy of that log also lands next to every `.mp4` the run
  produced — e.g. `trip_2026-05-11_12-11_08_h1080.log` — so it's beside its
  data. Override the location with the `LOG_DIR` environment variable.


## Loop-recording fragments

Dashcams loop-record: old footage gets overwritten when the card fills. Those
half-overwritten sessions show up as small "fragments" (1–3 clips) at the head
of the timeline. By default trips smaller than `min_clips_per_group` clips
(default **4**) are auto-skipped with a one-line notice:

```
Auto-skipping 7 fragment trip(s): #1 (1 clip), #2 (3 clips), #3 (1 clip), …
(force-encode by naming the index via --drives.)
```

Force-encode one anyway by naming its index: `./make-trips-rendered.sh 1 2`.

> The bundled example dataset is exactly this case: only trip 8 (May 11, 104
> clips, ~1h45m of driving) is a complete trip. The other 7 are loop-overwrite
> fragments.


## Performance

Hardware-accelerated `h264_videotoolbox` on an Apple-silicon Mac runs roughly
5–10× realtime, so ~2 hours of source footage encodes in 15–25 minutes. On
Linux/Windows you fall back to software libx264 — fine, just slower.

Video ego-motion analysis adds a pass over the front clips near trip
boundaries. For quick iteration on grouping thresholds, `--no-video-drive-detect`
falls back to the GPS-radius method, which is dramatically faster.


---

# TIER 2 — All the options

Everything below is settable in `config.txt`. Generate a fully-commented
template with:

```sh
python3 make_dashcam_videos.py --write-config ./config.txt
```

Precedence is **CLI flag > config.txt > built-in default**. Booleans accept
`true / false / yes / no / 1 / 0`. Every line in the template is commented out;
uncomment what you want to change.


## Rear picture-in-picture

The rear camera is inset into the main frame with a thin white border.

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `rear_pip` | — | `true` | Master on/off. Auto-disabled when `DCIM/200video/rear` is missing or empty. |
| `rear_pip_position` | — | `bottom-middle` | `bottom-middle`, `top-left`, `top-middle`, `top-right`. The bottom-left and bottom-right corners are **reserved** for the timestamp and the speed + watermark, so they aren't offered. |
| `rear_pip_w` | — | `662` | PiP width in px. |
| `rear_pip_h` | — | `372` | PiP height in px. |
| `rear_pip_margin` | — | `24` | Gap in px between the PiP and the frame edge. |

Set **both** `rear_pip_w` and `rear_pip_h`, or set **just one** and the other
is auto-computed from the rear camera's native 16:9 aspect — so you can't
accidentally squash the picture by changing only the width.

A front clip with no matching rear clip is dropped with a
`! no rear pair for …, skipping` warning. For a front-only camera set
`rear_pip = false` and those clips are kept.


## Watermark

| Config key | Default | What it does |
|------------|---------|--------------|
| `watermark_text` | `https://github.com/raoulsson/dashcam-exporter` | The text. **Leave empty to disable the watermark entirely.** |
| `watermark_font_size` | `28` | **Literal pixels** — unlike the speed overlay, which libass scales ~3.75× at 1080p. At 1080p: `16` is barely visible, `28` is comfortably readable, `36`–`40` is prominent. |
| `watermark_position` | `bottom-right` | `bottom-right`, `bottom-left`, `top-right`, `top-left`. |
| `watermark_margin_h` | `8` | px from the nearest horizontal edge. |
| `watermark_margin_v` | `6` | px from the nearest vertical edge. |

In the default `bottom-right` position the watermark sits **below** the speed
readout in the same corner — which is why `speed_margin_v` defaults to 32,
leaving room underneath.


## Timestamp and speed overlays

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `timestamp` | `--no-timestamp` | `true` | Burn `YYYY-MM-DD HH:MM:SS` into the bottom-left, advancing per frame from the clip's filename timestamp. |
| `speed` | `--no-speed` | `true` | Burn GPS speed into the bottom-right. Only where GPS data exists for the clip. |
| `speed_unit` | — | `kmh` | `kmh` or `mph`. Affects the overlay, the stats panel, the HTML legend and `_links.txt` (with distance in miles for `mph`). **GPX export always stays m/s per the spec.** |
| `speed_font_size` | — | `24` | libass-scaled, so the on-screen size is roughly 3.75× this at 1080p. |
| `speed_margin_v` | — | `32` | px from the bottom edge. The default leaves room for the watermark below. |
| `speed_margin_r` | — | `12` | px from the right edge. |


## Map widget and side panel

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `map_widget` | `--no-map-widget` | `true` | Render the side panel at all. Off = composite is a plain 1920×1080, ~20 % smaller files. |
| `panel_stats` | — | `true` | Burn the stats block (trip title, distance, moving time, max/avg speed, segments / GPS points) into the top of the panel. `false` = map only, centred vertically. |
| `map_panel_w` | — | `480` | Panel width in px. The map itself is square at this width. |
| `map_panel_position` | — | `right` | `right` or `left`. |
| `map_panel_gutter_px` | — | `2` | Black gutter between main video and panel. |
| `map_track_pad` | — | `12` | px reserved around the route's bounding box inside the panel. Smaller = tighter frame. |
| `map_zoom_boost` | — | `0` | Bump the auto-chosen OSM tile zoom by N integer steps. staticmap's auto-zoom rounds *down* to fit the bbox. `1` ≈ 2× detail (endpoints may sit at the panel edge); `2` is very tight (endpoints may clip). |
| `map_sidecars` | `--no-map-sidecars` | `true` | Write the `.html` / `.gpx` / `_links.txt` sidecars next to each video. |


## Front-camera crop

| Config key | Default | What it does |
|------------|---------|--------------|
| `front_crop_top` | *auto* | px cropped off the top of the source before scaling. |
| `front_crop_bottom` | *auto* | px cropped off the bottom. |

**The default is auto-detected.** The script probes the first front clip's real
resolution with `ffprobe` (`probe_video_size`) instead of assuming 2560×1600,
and derives the crop as the height *beyond* the 16:9 output aspect, split
evenly top and bottom:

| Source | Derived crop | Why |
|--------|--------------|-----|
| 2560×1600 (16:10) | `80` / `80` | trims to 2560×1440 = 16:9, no stretch |
| 1920×1080 (16:9) | `0` / `0` | already 16:9 — no crop, no stretch |

An explicit `front_crop_top` / `front_crop_bottom` in `config.txt` overrides the
derived value — use that if your camera is mounted high or low and the bonnet
dominates the frame. Effective height is `source_height − top − bottom`.


## Output size, quality and encoder

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `output_height` | `--output-height N` | `1080` | Downscale the finished composite to this height. `1080` (or `0`) keeps native 1080 — no downscale filter at all. |
| `vt_bitrate` | — | `8M` | VideoToolbox target bitrate, tuned for 1080p. |
| `vt_maxrate` | — | `10M` | VideoToolbox max bitrate. |
| `software` | `--software` | `false` | Force libx264 even where VideoToolbox is available. |
| `x264_preset` | — | `veryfast` | libx264 preset. |
| `x264_crf` | — | `23` | libx264 quality (lower = better/bigger). |
| `keep_intermediates` | `--keep-intermediates` | `false` | Don't delete per-clip intermediates after concat. |
| `audio` | `--no-audio` | `true` | `false` strips audio entirely. |

Both VT bitrates are **auto-scaled by `(output_height/1080)²`** — e.g. 540p
gets 2M / 2.5M — so a smaller frame produces a proportionally smaller file
instead of being over-encoded at 1080p-tier bitrate. libx264 uses CRF and
self-adjusts.

| Setting | Composite size | Typical file size, 1 h source | Best for |
|---------|----------------|--------------------------------|----------|
| `output_height = 1080` (default) | 2402 × 1080 | ~3.5 – 4 GB | Archive, big-screen viewing |
| `output_height = 720` | 1601 × 720 | ~1.5 – 2 GB | Detail-rich sharing — plates, signs |
| `output_height = 540` | 1201 × 540 | ~400 – 500 MB | Phone-sized messaging / streaming |

The pipeline always composes at the native 2402×1080 so overlays stay crisp,
then scales the finished frame down **once** at the end. The chosen height is
baked into the filename (`…_h540.mp4`, `…_h1080.mp4` — the tag is always
present), so rendering the same trip at several heights gives
side-by-side files rather than overwrites. Sidecars stay un-tagged — they only
depend on the GPS track, not the video resolution.


## Parking-skip

By default a long standstill collapses to: a short beat as you park, a
`Fast forwarding… 46m 15s skipped` slide, then a **clean cut to the moment you
drive away**. If you'd rather keep the parking maneuvering (backing out,
jockeying around a lot), turn it off with `--no-skip-parking`.

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `skip_parking` | `--no-skip-parking` | `true` | Master on/off for the whole feature. |
| `parking_min_secs` | `--parking-min-secs N` | `300` | Minimum total length of a parked run before the skip fires. 300 = 5 min; lower is more aggressive. |
| `parking_entry_pad` | `--parking-entry-pad N` | `3` | Seconds of stopped footage kept AFTER the detected park onset, before the FF slide. Keeps a brief "you've parked" beat so the cut doesn't feel jarring. |
| `parking_exit_pad` | `--parking-exit-pad N` | `10` | Seconds of pre-drive footage kept AFTER the FF slide, before the detected drive-away. Larger = more "about to drive". |
| `exit_skip_secs` | `--exit-skip-secs N` | `15` | Fallback: seek this far into the exit clip when neither video nor GPS gives a conclusive drive-resume. `0` plays the exit clip from second 0. |
| `drive_resume_sustain_secs` | `--drive-resume-sustain-secs N` | `30` | Consecutive seconds of GPS motion (>5 km/h) required to count as a real drive rather than parking-mode jitter. Try 60 if 30 still fires on passing traffic. |
| `drive_first_clip_pad_secs` | `--drive-first-clip-pad-secs N` | `8` | Head-trim pad at a trip's start: begin this many seconds before the detected motion. GPS only reports reliably ≥5 km/h, so the car is typically visibly rolling ~3 s before GPS notices; 8 s lands you with a few parked seconds before the rollout. |
| `inter_clip_gap_secs` | `--inter-clip-gap-secs N` | `60` | Insert a FF slide whenever the wall-clock distance between consecutive clips exceeds this. Catches engine-off intervals not preceded by parked footage. |
| `parking_pad_secs` | `--parking-pad-secs N` | `5` | **Deprecated** legacy combined knob. Used for both entry and exit pads only when neither of those is set. |
| `video_drive_detect` | `--no-video-drive-detect` | `true` | **Advanced.** `true` = find the drive-away by video ego-motion (robust to passing people/cars; needs numpy + opencv). `false` = GPS speed only. Most people should touch `skip_parking` instead — but this has a real effect, so the knob exists. |


## Trip grouping

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `trip_return_m` | `--trip-return-m M` | `100` | Getting back within M metres of the trip's anchor lets a park close the trip. |
| `trip_leave_m` | `--trip-leave-m M` | `150` | How far the car must first travel from the anchor before a return can close the trip — stops it closing on the driveway. |
| `trip_day_rollover` | `--trip-day-rollover H` | `4` | Hour of day the trip/day label rolls over instead of midnight. A trip starting before this hour carries the previous date. |
| `trip_min_m` | `--trip-min-m M` | `500` | A group only counts as a trip if its **noise-pruned** GPS track reaches at least M metres from the anchor. Closer clusters are auto-skipped as stationary. |
| `min_clips_per_group` | `--min-clips-per-group N` | `4` | Auto-skip groups smaller than N clips (loop-recording fragments). Bypassed for indices named explicitly via `--drives`. |

### Setting your home location

Home is **optional** but makes grouping far more robust. Because a home address
is personal, it is read from a **gitignored `.env`**, never from the committed
`config.txt`:

```bash
cp .env.example .env
# then edit .env:
#   SET_HOME_LAT=<your latitude>
#   SET_HOME_LON=<your longitude>
#   SET_HOME_RADIUS_M=100      # metres counted as "at home"
```

Grab the coordinates from Google Maps — right-click your home and the first
menu line is `lat, lon`. A real environment variable of the same name overrides
the `.env` value. If no home is set, the anchor is just the carried park
location (still works; home mainly helps when the carried anchor is unreliable,
e.g. loop-recording ate the departure so a trip's footage starts already out on
the highway).

Bump `SET_HOME_RADIUS_M` if you park on the street near home rather than in a
fixed spot — 100 m can be a touch tight.

> **Your home coordinates must never go into `config.txt`.** That file is
> committed to the repository. `.env` is gitignored precisely so this cannot
> happen by accident.


## GPS parsing knobs

| Config key | Default | What it does |
|------------|---------|--------------|
| `gps_segment_min_points` | `5` | Drop GPS segments shorter than this many consecutive fixes (= seconds at the dashcam's 1 Hz rate). Tiny segments are usually phantom fixes — the GPS briefly reports a position kilometres away then snaps back — which would otherwise bloat the map's bounding box into a regional view. Lower to `1` to keep every fix. |
| `clip_gpx_window_seconds` | `60` | Per-clip GPX time window. When a clip's parsed points span much more than this, only the densest window of this length is kept — see [Clip pairing & GPS](#1-clip-pairing--gps) for why. `0` disables. |


## Housekeeping

| Config key | CLI | Default | What it does |
|------------|-----|---------|--------------|
| `cache_max_age_days` | `--cache-max-age-days N` | `20` | Files in `.gpx_cache/` older than this are deleted at the start of each run. `0` disables. |
| `root` | `--root PATH` | `/Volumes/NO NAME` | SD card or local backup root; expects `DCIM/200video/{front,rear}` and optionally `DCIM/203gps` inside. |
| `out` | `--out PATH` | `~/Desktop/Dashcam_Videos` | Where videos and sidecars are written. |
| — | `--config PATH` | `./config.txt` | Use a config file at a non-default location. |
| — | `--write-config PATH` | — | Dump the fully-commented template and exit. Pass a directory (e.g. `.`) to write `config.txt` inside it. |

`.gpx_cache/` **persists** across runs — harvesting tar archives is expensive
and its result is unaffected by encoding config. `.intermediates/` is **wiped at
the start of every run**: it's scratch, and reusing it across runs would
silently produce stale output whenever a config knob changed (head-trim pad,
`output_height`, `speed_unit`, audio, …). Because it's wiped, **any config
change always takes effect on the next render, no flag needed.**


## Full CLI flag table

| Flag | Effect |
|------|--------|
| `--config PATH` | Use a config.txt at a non-default location. |
| `--write-config PATH` | Dump the fully-commented config template and exit. Pass `.` to write `./config.txt`. |
| `--root PATH` | Dashcam SD-card / backup root (default: `/Volumes/NO NAME`). |
| `--out PATH` | Output folder (default: `~/Desktop/Dashcam_Videos`). |
| `--drives N [N …]` (alias `--trips`) | Only process specific trip numbers (1-based). Bypasses min-clips skip for those trips. |
| `--trip-return-m M` | Back within M metres of the trip's anchor lets a park close the trip (default 100). |
| `--trip-leave-m M` | How far (m) the car must travel from the anchor before a return can close the trip (default 150). |
| `--trip-day-rollover H` | Hour of day the trip/day label rolls over instead of midnight (default 4 = 04:00). |
| `--trip-min-m M` | A group is only kept as a trip if its noise-pruned GPS track reaches at least M metres from the anchor (default 500). |
| `--no-video-drive-detect` | Force GPS-only boundary / drive-away detection (skips the video ego-motion pass; much faster). |
| `--debug-cuts [SECS]` | DEBUG PREVIEW (not a normal render): a short clip of **only** a trip's cut points — start, each parking/FF pause, stop — with the driving dropped. SECS = context kept around each event. Writes a separate `*_debugcuts*.mp4`. Bare = 5 s. Default 0 (off). |
| `--min-clips-per-group N` | Auto-skip trips smaller than N clips (default 4). Loop-recording fragments. |
| `--inter-clip-gap-secs N` | Insert a "Fast forwarding…" slide whenever consecutive clips are >N s apart (default 60). |
| `--force` | Re-encode trips whose `.mp4` already exists (default: skipped). |
| `--sidecars-only` | Only (re-)generate `.html` / `.gpx` / `_links.txt` / `_meta.json`; skip video encoding. |
| `--no-map-sidecars` | Don't generate the sidecars either. |
| `--no-map-widget` | Skip the burn-in side panel (output stays 1920×1080). |
| `--no-timestamp` | Skip the date/time overlay. |
| `--no-speed` | Skip the GPS speed overlay even when GPX data exists. |
| `--no-audio` | Strip audio (passenger-conversation privacy). |
| `--no-skip-parking` | Disable parking-skip altogether. |
| `--parking-min-secs N` | Minimum parked-run length (s) before parking-skip fires (default 300). |
| `--parking-entry-pad N` | Seconds of footage kept after park onset, before the FF slide (default 3). |
| `--parking-exit-pad N` | Seconds of footage kept after the FF slide before drive-resume (default 10). |
| `--parking-pad-secs N` | Deprecated shorthand for both pads (default 5). |
| `--exit-skip-secs N` | Seek N seconds into the exit clip when drive-resume isn't conclusive (default 15). |
| `--drive-resume-sustain-secs N` | Consecutive seconds of GPS motion required to count as "real drive" (default 30). |
| `--drive-first-clip-pad-secs N` | Head-trim pad at a trip's start (default 8). |
| `--cache-max-age-days N` | TTL for `.gpx_cache/` entries (default 20; 0 disables). |
| `--output-height N` | Downscale final composite to this height. **Default 1080 (native).** 720 for roughly half the size, 540 for phone-sized. |
| `--software` | Force libx264 instead of macOS VideoToolbox. |
| `--keep-intermediates` | Don't delete per-clip intermediates after concat. |
| `--dry-run` | List trips and exit without encoding. |


---

# TIER 3 — For the nerds: how it works internally

## 1. Clip pairing & GPS

`find_clips` builds two maps keyed by the filename timestamp: front clips
matching `YYYYMMDDhhmmss_NNNN.mp4` and rear clips matching
`YYYYMMDDhhmmss_NNNN_A.mp4`. The duration comes out of the filename, and the
timestamp is converted to an epoch **treated as UTC** so ffmpeg's `drawtext`
`gmtime` renders the wall-clock time the camera recorded. A front clip whose
rear pair is missing is dropped (unless `rear_pip` is off).

GPS lives in `DCIM/203gps/` as NMEA sentences in files with a `.gpx`
extension, and older sessions are rolled up into POSIX tar archives in
`203gps/tar/` with a `.git` extension. Both are firmware mislabelings.
`harvest_tarred_gpx` extracts every `.gpx` member of every tar into
`OUT_DIR/.gpx_cache/`, cached across runs. `find_gpx_for` then matches a clip
timestamp against either location. On the demo card this expanded GPS coverage
from **25 → 90 clips out of 117**.

`parse_gpx_track` reads only `$GPRMC` sentences, skips fixes whose status field
isn't `A` (valid), converts NMEA ddmm.mmmm coordinates to decimal degrees via
`_nmea_to_decimal`, and converts the speed-in-knots field to km/h. It returns
`(lat, lon, kmh, utc_datetime)` tuples.

### The DDPAI stale-buffer problem

DDPAI firmware sometimes writes a **previous drive's GPS fixes** into the start
of a new clip's GPX file — parking-mode buffer leftovers. There are two
observed shapes:

1. **Cross-drive stale data.** Clip N's GPX contains points from hours earlier
   at a location across town. Left in, they blow up the map's bounding box and
   make the marker animation jump.
2. **Multi-clip bundle.** Clip N's GPX contains clip N−1's *and* clip N's data,
   time-contiguous. The speed array then starts with the wrong clip's data, so
   the overlay shows the previous clip's acceleration ramp burned onto footage
   where the wheels haven't moved yet.

Both are handled the same way: if the parsed points span more than 1.5 ×
`clip_gpx_window_seconds` (default 60, i.e. one clip), keep only the points
within that window of the **latest** fix. DDPAI always writes the live
recording last, so the newest window is the actual clip's data.

### Segmentation

`segment_track` splits the flat fix list into contiguous-driving segments: any
consecutive pair more than `SEGMENT_GAP_SECONDS` (30 s) apart in time **or**
`SEGMENT_GAP_METERS` (200 m) apart in distance starts a new segment. Segments
shorter than `gps_segment_min_points` (default 5) fixes are pruned as phantom
fixes — the GPS briefly reporting a position kilometres away, then snapping
back. If pruning would remove *every* segment the unfiltered list is returned
so the caller still has something to work with.

Segmentation is why the map doesn't draw phantom straight lines across town
between engine-off intervals, and why the exported `.gpx` has one `<trkseg>`
per driving leg.


## 2. Trip grouping = a park-to-park state machine

`group_into_trips` is the heart of it. **A trip boundary is the car actually
PARKING at its anchor — not a GPS radius crossing.**

The **anchor** is where the car last parked, carried forward from trip to trip.
A configured `home` (from `.env`) is an extra, always-valid park target. The
shape of a trip is:

> **DEPART** (start driving away from the anchor) → **drive** → **ARRIVE +
> PARK** (return to the anchor/home and come to a stop).

Between trips the car is **IDLE** at the anchor, and those clips belong to **no
trip** — the loop skips forward through them until `departs_here` fires.

The two boundaries are found by video ego-motion, not by geometry:

- `departs_here(i, anchor)` → `find_drive_away_by_video(clips[i])` is not None.
- `parks_here(i, anchor)` → the clip's GPS is within `trip_return_m` of the
  anchor (or `SET_HOME_RADIUS_M` of home) **and** `find_park_second_by_video`
  finds a sustained stop. GPS position only gates *which* clips get the
  (expensive) video check; the video makes the call.

`trip_leave_m` gates the whole thing: the car must first get that far from the
anchor before any return is allowed to close the trip, so you can't close on
the driveway.

### Why park-detection beats a radius

The old radius method was wrong in both directions, visibly:

- It **closed trips ~10–15 s early** — the radius fired while the car was still
  rolling home at 27 km/h, so the actual pull-in and park got orphaned into the
  *next* group.
- It **split near-home departure maneuvering** into throwaway fragments —
  backing out and turning around re-crossed the radius without ever parking.

Ego-motion lands on the real pull-out and the real pull-in, so a trip contains
its own complete departure and arrival.

### Interior stops never close a trip

Because only a park **at the anchor** closes a trip, a stop *elsewhere* never
does. Drive **A → B, hang out for any length of time, B → A** and that is
**one trip**, with the stop at B cut out as a Fast-forwarding slide. There is
deliberately **no engine-off-duration rule**.

### Rollover

Crossing `trip_day_rollover`:00 (default **04:00, not midnight**) force-closes
a trip. This is what bounds a **one-way relocation**: drive to a holiday base,
sleep, drive back three days later = two trips, not one enormous one. The same
rollover defines the **day label** (`trip_day_label`) that leads every output
filename — a trip starting at 02:00 belongs to the previous date; one starting
at 17:00 keeps its date even if it runs to 03:32.

### The `moved` flag

`is_moved` gathers the group's track, prunes it through `segment_track`, and
asks whether **any pruned point** is more than `trip_min_m` (default 500 m)
from the anchor. A group that never gets that far is near-home puttering,
parking-mode motion events, or a lone phantom fix — auto-skipped as stationary.
Pruning first is what stops a single phantom fix from faking a trip.

### Fallback

Without numpy + opencv (or with `--no-video-drive-detect` / `video_drive_detect
= false`), `video` is False and grouping degrades to the old radius-entry
behaviour: `departs_here` becomes "min distance from anchor > `trip_leave_m`",
`parks_here` becomes plain radius entry. Much faster — genuinely useful for
quick `--dry-run` iteration on thresholds.


## 3. Video ego-motion detection

Functions: `_ego_extract_frames`, `_ego_median_flow`, `_ego_drive_onset`,
`_ego_park_onset`, `find_drive_away_by_video`, `find_park_second_by_video`,
`find_drive_away_in_group_video`.

**Sampling.** `_ego_extract_frames` runs ffmpeg on the front clip with
`fps=4,scale=640:400,format=gray` into `-f rawvideo -pix_fmt gray` on stdout,
capped at `EGO_MAX_ANALYZE_SECS` (120), and reshapes the bytes into an
`(n, 400, 640)` uint8 numpy array. Greyscale, small, 4 fps — cheap.

**Flow.** `_ego_median_flow` walks consecutive frame pairs. For each pair it
picks up to 300 corners with `goodFeaturesToTrack`, tracks them into the next
frame with `calcOpticalFlowPyrLK` (Lucas–Kanade, 21×21 window, 3 pyramid
levels), and takes the **median** magnitude of the displacement vectors of the
successfully-tracked features.

**Why the median is the whole trick.** Parking-mode clips are event snippets —
the camera wakes up because *something* moved nearby, so the footage is full of
people walking past and cars driving by **while your car sits still**. Those
are a handful of features against a static background: a few outliers the
median discards. When the car itself is rolling, the entire frame sweeps, so
*most* features move and the median moves with them. Measured, the separation
is about two orders of magnitude — **~0.01–0.05 px parked versus 4–16 px
driving** at 640×400. That gap is why the thresholds can be flat constants:
`EGO_THR_SUSTAIN = 1.0` for "driving", `EGO_THR_BASELINE = 0.15` for the
parked-noise floor.

**Drive onset.** `_ego_drive_onset` finds the first index where median flow
stays above `EGO_THR_SUSTAIN` for `EGO_SUSTAIN_SECS` (1.5 s = 6 frames), then
**walks backward** while flow is still above `EGO_THR_BASELINE`. The sustain
test rejects a single passing truck; the walk-back recovers the first
millimetre of actual rollout, which is the frame you want to cut to.

**Park onset.** `_ego_park_onset` is the mirror. The clip must contain real
driving first (`max(med) > EGO_THR_SUSTAIN`), then: scan backwards from the end
for the last frame still above baseline; the stop begins right after it, and it
only counts if that stillness lasts at least `sustain` frames to the clip's
end. A clip still driving at its end is not an arrival.

**Across clips.** `find_drive_away_in_group_video` concatenates the sampled
frames of the first few clips of a trip (stopping at the first coverage gap so
it never fuses non-contiguous footage), runs one onset detection over the whole
thing, and maps the result back to `(clip_index, second_within_clip)` via the
recorded frame bounds. That is what lets a trip's head-trim drop entire parked
leading clips and then trim into the one where motion starts.

**Why GPS failed here.** Consumer GPS only reports speed reliably above roughly
5 km/h, and parking-mode fixes are stale and jittery on top of that. Ego-motion
catches the car **creeping out of a spot below the GPS speed floor** — exactly
the moments GPS cannot see, and exactly the frames you want the cut on.

**Why not phase correlation?** A translation-only estimator is insufficient for
a forward-facing camera. Driving straight ahead produces scene **expansion**
about a focus of expansion — features stream outward from a point — with
essentially **zero net translation**. A global-translation method reads that as
"not moving". Per-feature flow magnitudes, medianed, see it correctly, because
every feature has a large magnitude even though the mean direction cancels.


## 4. Parking-skip rendering

`find_parking_runs` walks the trip's clips looking for a park onset via
`find_park_second` (smoothed GPS speed, with a two-clip lookahead so a 30 s
sustain window straddling a clip boundary still counts). If that misses, it
falls back to the whole-clip heuristic `clip_is_parked`, which calls a clip
parked when any of: ≥75 % of its GPS seconds are below 3 km/h; the GPX exists
but holds no valid fixes (indoor parking, lost lock); or there is no GPX at
all. There is also a sparse-and-fast guard: a clip whose GPX holds far fewer
samples than its duration suggests *and* whose samples average above 40 km/h is
stale parking-buffer data from a previous drive — real cars don't go from a
parking-mode wake to 80 km/h.

From that onset the run is followed forward through subsequent parked clips.
Total stopped time = the partial entry-clip tail + the fully-parked clips + the
wall-clock engine-off gap to the next moving clip. The run is only recorded if
that total ≥ `parking_min_secs`.

The render then classifies each clip index into an action:

| Action | Meaning |
|--------|---------|
| `entry` | The clip where the park onset happens. Emitted trimmed to `park_sec + parking_entry_pad`, then followed by the FF slide. |
| `entry_end` | Same, but the parking run runs to the end of the trip. **No trailing FF slide** — there's nothing to skip to, and a dangling slide as the final frames looks broken. Typical for a one-way relocation where the engine stays on after arrival. |
| `skip` | Fully-parked clip; not emitted at all. |
| `exit` | The clip after the run. Anchored on the video drive-away, falling back to GPS `find_drive_resume_second`, falling back to `exit_skip_secs`. |
| `head_skip` | Leading parked clip at the trip start, dropped by head-trim. |

The FF slide (`generate_transition_slide`, `TRANSITION_SECS = 3`) reads
`Fast forwarding… 46m 15s skipped`, computed from the entry clip's last emitted
frame (park onset + entry pad) to the exit clip's start.

Head-trim and parking runs are deliberately kept from fighting: a run that
overlaps the head-trimmed region is ignored, so a parking run can't resurrect
footage the head-trim meant to cut.

Separately from parked runs, **inter-clip wall-clock gaps** longer than
`inter_clip_gap_secs` (default 60 s) get their own FF slide. This catches
engine-off intervals that aren't preceded by parked footage — the camera simply
wasn't recording, so `find_parking_runs` has nothing to detect.


## 5. Composition

Per clip, one ffmpeg `filter_complex` (`build_filter_complex`) does everything
in a single pass:

```
front.mp4 ─┐
           ├─► ffmpeg filter_complex:
rear.mp4  ─┤    crop + scale front  →  [front]
           │    scale + border rear  →  [rear]
           │    overlay [rear] on [front] at chosen position
           │    drawtext timestamp (bottom-left)
           │    subtitles speed.srt (bottom-right)
           │    drawtext watermark (chosen corner)        → [video_part]
map.mp4   ─┤    [2:v] scale to 480×1080 + 2-px gutter pad → [map_part]
           │    hstack [video_part][map_part]              → [out]
           ▼
       clip_NN.mp4  (intermediate, 2402×1080)
           ▼
       concat-demuxer  (stream-copy, no re-encode)
           ▼
       final trip_….mp4
```

1. **Front** — cropped to the 16:9 output aspect (auto-derived, see
   [Front-camera crop](#front-camera-crop)) then scaled to 1920×1080.
2. **Rear PiP** — scaled to `rear_pip_w`×`rear_pip_h` with a thin white border
   and overlaid at the configured position.
3. **Timestamp** — `drawtext` with `gmtime` on the clip's filename epoch, so it
   advances per frame.
4. **Speed** — `write_speed_srt` emits one 1-second SRT cue per GPS sample and
   the `subtitles` filter burns them in. libass scales the font ~3.75× at
   1080p, which is why `speed_font_size` and `watermark_font_size` behave
   differently.
5. **Watermark** — `drawtext` at literal pixel size in the chosen corner.
6. **Side panel** — 480 px wide, hstacked with a 2 px gutter, giving 2402×1080.

The panel video is built by PIL/staticmap:

```
all GPX points for the trip   →  base panel PNG (stats + route + start/end markers)
                                 +
each second of clip            →  marker dot composited on base PNG
                                 ↓
                             PNG sequence
                                 ↓
                         ffmpeg 1-fps mp4  →  map.mp4
```

The base panel (`render_base_right_panel` / `render_base_route_panel`) draws
the full route coloured by speed via `_speed_color` — a blue→navy palette
chosen to pop against OSM's yellow/orange roads — with `_project_track` mapping
lat/lon to panel pixels. `render_clip_marker_video` composites the per-second
marker via `_nearest_pixel`. If OSM tiles are unreachable, staticmap is skipped
and a plain PIL polyline is drawn instead, so the render never blocks on the
network. The 1-fps map is upsampled to 30 fps by the main filter chain, so the
marker visibly steps once per second.

Finally the 2402×1080 composite is downscaled once to `output_height`, with the
VideoToolbox bitrate scaled by `(h/1080)²` (`_scale_bitrate_string`).


## 6. Output & metadata

Videos are written into **per-extract-day folders** with an `info.txt` naming
the source import folder, because one card can span several days and the
trip's day label (04:00 rollover) is what should organise the output, not the
import date.

Filenames are `trip_<day>_<HH-MM>_<NN>[_hHHH].mp4` — day label, start time,
global 1-based trip index, and the output-height tag (omitted for native
1080). Rendering the same trip at several heights therefore produces
side-by-side files rather than overwrites, and the format on disk is obvious
from the name.

`_meta.json` carries `trip_index`, `day`, `start`, `end`, `duration_secs`,
`n_clips`, `video`, `round_trip`, `start_fix`, `end_fix`, `distance_km`. A
publishing UI can group a day's trips and know each trip's shape without
re-parsing a single GPS file. `round_trip = false` means the trip was closed by
rollover or end-of-clips rather than by a park at the anchor — a one-way
relocation.

Sidecars are **un-tagged** by size because they depend only on the GPS track,
which is resolution-independent; one set covers every rendered height.

Caching, restated as policy:

- Final `.mp4` exists → trip skipped. Delete it or pass `--force`.
- `.intermediates/` is wiped at the start of every run. It's scratch, and its
  filename can't encode every relevant knob, so reuse would silently produce
  stale output. The wipe is what makes "change config, re-render, it just
  works" true.
- `.gpx_cache/` persists — expensive to redo, unaffected by encoding config —
  and is TTL-evicted via `cache_max_age_days`.
- Sidecars are written unconditionally, even when the `.mp4` exists.


## 7. `--debug-cuts`

Rendering a full trip to check whether one cut landed right is a terrible
feedback loop. `--debug-cuts SECS` renders **only** the cut points: the trip's
start, each parking / gap pause with `SECS` of context on either side of the FF
slide, and the trip's stop — dropping every driving middle. ~20 seconds instead
of ~1.5 GB, written to a separate `*_debugcuts<N>s*.mp4`.

Two implementation subtleties worth knowing:

1. **Dropped middles still advance `prev_emitted_clip`.** Gap detection compares
   each emitted clip's start against the previous clip's end. If a dropped
   driving middle didn't update that pointer, every dropped stretch would look
   like a huge inter-clip gap and the preview would sprout FF slides that the
   real render doesn't have. So the loop updates the pointer even on the drop
   path — the preview's gap decisions match the real render's exactly.
2. **A precomputed `gap_pre_pause` set.** For an *inter-clip gap* pause (as
   opposed to a parking pause) the "before FF" context is the **tail of the
   preceding clip**, which the loop only discovers once it's already past it.
   So before rendering, the emitted-clip sequence is walked with the same gap
   rule the render loop uses, and the clips whose tail precedes a genuine gap
   are collected into `gap_pre_pause`. Computing it over the emitted sequence
   (not the raw clip list) is what stops it mistaking a dropped middle for a
   gap.


---

## Troubleshooting

**`No such filter: 'drawtext'`** — ffmpeg without libfreetype. Install
`ffmpeg-full` (see Install), or run with `--no-timestamp`.

**`Unable to open … speed.srt` / `subtitles` errors** — ffmpeg without
libass. Install `ffmpeg-full`, or run with `--no-speed`.

**`no rear pair for YYYYMMDDhhmmss, skipping`** — A front clip exists with no
matching rear. The script drops it. To use a front-only setup set
`rear_pip = false` in `config.txt`.

**`map: (no GPS data for this trip)`** — Clip filenames in that trip don't
match any GPX (loose or tarred). Normal for trips without GPS lock.

**`Output looks squashed horizontally`** — Player isn't honouring SAR. The
video is yuv420p with square pixels; QuickTime / VLC / IINA all handle it.

**`OSM tile fetch failed (…)`** — Network problem during burn-in widget
render. The HTML map (Leaflet) still works fine since OSM tiles load in your
browser at view time.

**`error: externally-managed-environment`** — Homebrew Python blocks
system-wide `pip install` (PEP 668). Use the venv recipe in Install.

**`! map widget skipped: PIL/Pillow not installed`** — venv not activated,
or `pip install -r requirements.txt` was never run. Video still encodes at
1920×1080 with all other overlays.

**Trip boundaries look wrong / trips are merging or splitting oddly** — check
that `numpy` and `opencv-python-headless` are installed; without them grouping
silently falls back to the GPS-radius method. Then set a home location in
`.env`, and `--dry-run` after every threshold change since indices shift.

**A parking cut lands in a strange place** — use `--debug-cuts` to iterate
quickly, and tune `parking_entry_pad` / `parking_exit_pad`. Set
`PARKING_DEBUG=1` in the environment for per-clip park-detection tracing.


## Repo layout

```
dashcam-exporter/
├── make_dashcam_videos.py        # the single-file script (entry point)
├── config.txt                    # generated by --write-config; edit to taste
├── .env / .env.example           # HOME coordinates (gitignored)
├── requirements.txt              # Pillow + staticmap (+ numpy/opencv optional)
├── import-sd-card.sh             # copy the card into a dated import folder, verify, wipe
├── list-trips-data.sh            # dry-run: list every trip with its index
├── make-trips-rendered.sh        # encode a chosen trip (or all)
├── examples/                     # screenshots used in this README
├── logs/                         # per-run render logs
├── .gitignore
├── LICENSE
└── README.md                     # this file
```


## Funding

- 🏅 https://github.com/sponsors/raoulsson
- 🪙 https://www.buymeacoffee.com/raoulsson


## License

MIT — see [LICENSE](LICENSE).

Copyright © 2026 Raoul Marc Schmidiger ([hello@raoulsson.com](mailto:hello@raoulsson.com)).

---

**Happy Driving! 🎉**
