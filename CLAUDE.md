# dashcam-exporter — project notes

Turns raw DDPAI dashcam SD-card footage (front + optional rear + NMEA GPS) into
one polished MP4 per **trip**, with burned-in overlays (timestamp, speed,
watermark), a moving-marker map widget + stats panel, automatic parking-skip,
and per-trip HTML / GPX / links / meta sidecars. DDPAI-specific (tested on Mola
N3 Pro). macOS-first (hardware VideoToolbox); Linux works via libx264.

Almost everything lives in one file: `make_dashcam_videos.py` (~4500 lines).
There is no package, no tests — it's a prototype script. Reshaping it is fine;
less code is better.

## The trip model (core concept)

A **trip** is the publishing unit (NOT an engine-on session, NOT a calendar
day). `group_into_trips` is a **park-to-park state machine**: a boundary is the
car actually PARKING at the anchor, not a radius crossing.

- **Anchor** = where the car last parked, carried forward. `home_lat` /
`home_lon` / `home_radius_m` seed it from config.txt; the gitignored .env WINS
for the coordinates (SET_HOME_LAT / SET_HOME_LON), which is where a real
address belongs. config.txt ships a Zurich example.
- **DEPART → drive → ARRIVE+PARK.** Departure found by `find_drive_away_by_video`
  (median optical flow rises), arrival by `find_park_second_by_video` (flow
  falls to baseline and stays). GPS position only gates WHICH clips get the
  video check (near the anchor). Between trips the car is IDLE at the anchor →
  those clips belong to no trip.
- Boundaries land on the real pull-out / pull-in. A radius-entry close ends
  trips ~15 s early (still rolling in) and splits near-home departure
  maneuvering into skipped fragments; park-detection avoids both — which is
  why the radius fallback below is a fallback, not an equal alternative.
- An interior stop **elsewhere** (not the anchor) never closes → A→B→hangout→A
  is one trip, any duration. **ROLLOVER** (`trip_day_rollover`:00, default 04:00)
  force-closes, bounding one-way relocations.
- Falls back to the radius-entry boundary without OpenCV/numpy (or
  `--no-video-drive-detect`): ~1.5 s vs ~70 s, useful for quick dry-runs.
- `moved` flag: a group whose NOISE-PRUNED track never reaches `trip_min_m` from
  the anchor (puttering / parking-mode events / lone phantom fix) is auto-skipped.

Every interior stop renders as a "Fast forwarding…" slide, so a trip spans
multiple engine-on sessions and plays continuously. Trip rendering runs BOTH
parking-run detection + inter-clip gap slides AND the drive-mode head-trim at
the trip's start. A trip that ends parked uses `entry_end` (arrival slice, no
trailing FF).

"Day" is only a **label** (the 04:00-rollover date) each trip carries, for a
future publishing UI to group a day's trips side by side. Not a render mode.

### Tuning note

Knobs: `trip_return_m`, `trip_leave_m`, `trip_day_rollover`, `trip_min_m`
(config/CLI) and `SET_HOME_RADIUS_M` (.env). `trip_min_m` (default 500) is the
min pruned-track distance from anchor for a group to count as a real trip —
raise it to drop more near-home puttering, lower it to keep short trips. A day
where you drive out and never return home
before 04:00 (one-way, or messy/incomplete data) stays one trip by design. 100 m
home radius can be tight if you street-park near home. Always
`./list-trips-data.sh` (dry-run) before encoding — indices shift if you change
anything.

### Debug-cuts preview

`--debug-cuts N` (dest `args.debug_cuts`, bare = 5) produces a preview clip of
only the transition moments — trip start, each pause (N s before the FF slide +
the slide + N s after drive-resume) and the stop — dropping the driving middles.
A 34-clip / 25-min trip becomes a ~5-clip / ~23 s clip, so parking / FF /
drive-away behaviour is eyeballed fast. Writes a separate `*_debugcutsNs*.mp4`
(never clobbers a real render). Two impl subtleties: dropped middles still
update `prev_emitted_clip` so gap detection stays honest, and `gap_pre_pause`
(precomputed over the emitted sequence) supplies the "before FF" tail for
inter-clip gaps. It is the fastest way to check that the parking exit slice
lands on actual drive-away footage rather than still-parked frames (that cut is
anchored by `find_drive_away_by_video` optical flow; see below).

### Parking-exit drive-away = video ego-motion, not GPS

`find_drive_away_by_video` anchors the parking exit slice. GPS speed is useless
here (parking-mode clips are event snippets full of passing people/cars, and GPS
is stale/jittery). Instead: sample the front clip at 4 fps (ffmpeg → gray
640×400 rawvideo), LK optical flow, take the MEDIAN flow magnitude — passing
objects are outliers the median rejects; the car actually rolling sweeps the
whole frame (median jumps ~80×). Find the first sustained jump, walk back to the
baseline departure = drive-away. Needs numpy + opencv (venv); silently falls
back to GPS `find_drive_resume_second` otherwise. There is intentionally NO knob
to pick GPS-vs-video (the user only wants a clean cut); the real alternative —
keep the parking movements — is `--no-skip-parking`. Same technique could later
replace the GPS head-trim at trip start. Constants: `EGO_*`.

### Env / venv

Homebrew's `python3` is externally-managed (PEP 668), so Pillow/staticmap (map
widget) can't install into it. Use `.venv` (`python3 -m venv .venv && .venv/bin/
pip install -r requirements.txt`); the wrapper scripts auto-prefer `.venv/bin/
python` when it exists. Without Pillow the render still works but silently drops
the map-widget panel (prints a warning). `.venv/` and `.env` are gitignored.

## Data layout (outside the repo)

- Inputs:  `~/dashcam-data/import/<label>/DCIM/{200video/{front,rear},203gps,...}`
  — one folder per import (folder name is arbitrary, usually the import date;
  grouping is timestamp-driven and one folder can span several days of clips).
- Outputs: `~/dashcam-data/output/` — the `--out` target. Output is
  **namespaced by import**: `output/<import-name>/<extract-day>/`, e.g.
  `output/2026-07-19/2026-07-15/`. The import folder (`root.name`) is the
  top namespace; the extract day (04:00-rollover) groups a card's trips beneath
  it. This is what makes cross-card clobbering **impossible**: DDPAI cards hoard
  old event clips, so two different cards routinely contain the same calendar day
  — but they land in `<cardA>/<day>/` and `<cardB>/<day>/`, separate subtrees. The
  fresh-output reset only ever clears inside the running import's own namespace,
  so rendering one card never touches another's output. Each day folder holds
  its trips + an `info.txt` naming the source import. `.gpx_cache/.intermediates`
  stay at the `--out` root, shared. A publishing target walks this with
  `rglob` and regroups by day for display.

Pass `--root <import-folder>` and `--out <output-dir>`. Do NOT bake these
absolute personal paths into the tracked `config.txt` (shared template) — set
them per-run, in the wrapper-script OPTS, or a local uncommitted config.

Importing a card: `./import-sd-card.sh [YYYY-MM-DD] [--delete] [--checksum] [--src PATH]` copies
the card's `DCIM` into `<import_dir>/<day>/`, verifies file-for-file, and KEEPS
the card — deletion is opt-in via `--delete`, and `--keep` survives only as a
back-compat no-op. With `--delete` it removes the card's files and keeps the
folder tree so the camera can record. Nothing is deleted until the verify pass
succeeds, and the verify refuses if rsync itself failed. `AFTER_STAMP` in the
environment makes it a delta copy; `--delete` is refused after one, because the
skipped clips were verified by an earlier run this script cannot see.

## Running

- `./list-trips-data.sh` — dry-run; list trips with index, day label, span, clips.
- `./make-trips-rendered.sh [N …]` — encode; leading ints select trip indices
  (via `--drives`, alias `--trips`), rest passes through. A FULL render (no
  `--drives`) first clears ONLY the day folders it is about to write, so shifted
  trip indices leave no stale duplicates — but other imports' day folders in the
  same `--out` are never touched (rendering the 07-19 import clears 07-15..18,
  leaves an unrelated 05-11 alone). Hidden entries and the root
  `.gpx_cache`/`.geocode_cache.json` are always kept. The reset lives in
  `make_dashcam_videos.py` (it knows which days it'll write), is skipped for a
  `--drives` subset and `--sidecars-only`, and is disabled by `--no-clean-days`.
  Logs to `run-<ts>.log` inside `--out/logs/`. Keep the reset in the renderer,
  never the wrapper: a wrapper-level wipe cannot know which days a run writes,
  so it would take other imports' output with it.
- Direct: `python3 make_dashcam_videos.py --root … --out … [--dry-run|--sidecars-only|--force]`.
- `--write-config PATH` copies `config.txt` itself to PATH, so a config change
  is two places: that file and the argparse defaults.

## Output files per trip

Under `out_dir/<extract-day>/`: `trip_<day>_<HH-MM>_<NN>_hHHH.mp4` (NN = the per-DAY
publish index, restarting at 01 each day; the _h<height> tag is always written,
native 1080 included), plus un-tagged `.html`, `.gpx`,
`_links.txt`, and `_meta.json` (day, start/end, round_trip bool, fixes,
distance). Each day folder also holds `info.txt` (source import folder).

## Gotchas

- DDPAI dumps stale GPS from a previous drive into a clip's GPX (parking-mode
  buffer). `parse_gpx_track` keeps only the densest time-window; there's a lot
  of code fighting phantom fixes — respect it when touching GPS.
- `.intermediates/` is scratch, wiped every run. `.gpx_cache/` persists (harvested
  from tarred NMEA), TTL-evicted via `--cache-max-age-days`.
- Restartable: a trip whose final `.mp4` exists is skipped unless `--force`.
- `*.gpx` / `*.mp4` / `*.html` / `*_links.txt` are gitignored — running with
  `--out .` litters the repo with them; keep outputs pointed at the working dir.
