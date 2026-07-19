# dashcam-exporter — project notes

Turns raw DDPAI dashcam SD-card footage (front + optional rear + NMEA GPS) into
one polished MP4 per **trip**, with burned-in overlays (timestamp, speed,
watermark), a moving-marker map widget + stats panel, automatic parking-skip,
and per-trip HTML / GPX / links / meta sidecars. DDPAI-specific (tested on Mola
N3 Pro). macOS-first (hardware VideoToolbox); Linux works via libx264.

Almost everything lives in one file: `make_dashcam_videos.py` (~3000 lines).
There is no package, no tests — it's a prototype script. Reshaping it is fine;
less code is better.

## The trip model (core concept)

A **trip** is the publishing unit. It is NOT a single engine-on session and it
is NOT a calendar day — both of those older models are gone. `group_into_trips`
walks clips chronologically and closes a trip at whichever fires FIRST:

1. **Return** — back within `trip_return_m` (100 m) of the trip's anchor, after
   first leaving by `trip_leave_m` (150 m). A → B, hang out ANY length of time,
   B → A = one trip with the stop at B cut out. Duration is irrelevant (10 min
   or 20 h). The **anchor is carried forward**: a trip anchors on where the car
   last parked (previous trip's last good fix), NOT its own first fix — that
   survives stale/garage-start GPS and a homeward leg re-crossing a mid-route
   point.
2. **Home** (optional) — if `home` is configured, parking within `home_radius_m`
   is a HARD boundary independent of the anchor: arriving home ends a trip, next
   departure starts a new one. Ground-truth version of Return; fires even when
   the carried anchor is wrong (loop-recording ate the home departure so a trip
   starts out on the highway). Home coords come from a **gitignored `.env`**
   (`SET_HOME_LAT` / `SET_HOME_LON` / `SET_HOME_RADIUS_M`), loaded by
   `load_dotenv` — NEVER config.txt (that's committed; public repo). See
   `.env.example`.
3. **Rollover** — the clock crosses `trip_day_rollover`:00 (default 04:00, not
   midnight) between two clips. Import-folder name/date is irrelevant. This also
   bounds a one-way relocation (drive to a base, sleep, drive back days later =
   two trips, because a 04:00 boundary falls between arrival and return).

There is **deliberately no long-gap / engine-off-duration split** — a stop of
any length stays inside the trip. (Removed on purpose: it would split exactly
the A→B→A round trips that must stay whole.)

Every interior stop renders as a "Fast forwarding…" slide, so a trip spans
multiple engine-on sessions and plays continuously. Trip rendering reuses the
machinery the old `--daily` path had (parking-run detection + inter-clip gap
slides), PLUS the old drive-mode head-trim at the trip's start. Both run now;
they used to be mutually exclusive via `--daily`. A trip that ends parked uses
`entry_end` (arrival slice, no trailing FF). `group_into_trips` also returns a
`moved` flag; a group whose NOISE-PRUNED track never reaches `trip_min_m` from
the anchor (near-home puttering, parking-mode events, or a lone phantom GPS
jump that got pruned) is auto-skipped as stationary.

"Day" is now only a **label** (the 04:00-rollover date) each trip carries, for a
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

### Env / venv

Homebrew's `python3` is externally-managed (PEP 668), so Pillow/staticmap (map
widget) can't install into it. Use `.venv` (`python3 -m venv .venv && .venv/bin/
pip install -r requirements.txt`); the wrapper scripts auto-prefer `.venv/bin/
python` when it exists. Without Pillow the render still works but silently drops
the map-widget panel (prints a warning). `.venv/` and `.env` are gitignored.

## Data layout (outside the repo)

- Inputs:  `~/rsc-data/Import_Dashcam/<label>/DCIM/{200video/{front,rear},203gps,...}`
  — one folder per import (folder name is arbitrary; grouping is timestamp-driven,
  and a single folder can contain several days of clips).
- Outputs: `~/rsc-data/Dashcam_Videos_working/` — the `--out` target.

Pass `--root <import-folder>` and `--out <working-dir>`. Do NOT bake these
absolute personal paths into the tracked `config.txt` (shared template) — set
them per-run, in the wrapper-script OPTS, or a local uncommitted config.

## Running

- `./list-trips-data.sh` — dry-run; list trips with index, day label, span, clips.
- `./make-trips-rendered.sh [N …]` — encode; leading ints select trip indices
  (via `--drives`, alias `--trips`), rest passes through. Logs to `./logs/`.
- Direct: `python3 make_dashcam_videos.py --root … --out … [--dry-run|--sidecars-only|--force]`.
- `--write-config PATH` dumps the fully-commented config template. The template
  is the `CONFIG_TEMPLATE` string near the top of the script — keep it in sync
  with `config.txt` and the argparse defaults (a config change is three places).

## Output files per trip

`trip_<day>_<HH-MM>_<NN>[_hHHH].mp4` (day label leads so a UI can glob a day;
NN = global 1-based index; size tag omitted at native 1080), plus un-tagged
`.html`, `.gpx`, `_links.txt`, and `_meta.json` (day, start/end, round_trip
bool, fixes, distance) — the machine-readable day metadata.

## Gotchas

- DDPAI dumps stale GPS from a previous drive into a clip's GPX (parking-mode
  buffer). `parse_gpx_track` keeps only the densest time-window; there's a lot
  of code fighting phantom fixes — respect it when touching GPS.
- `.intermediates/` is scratch, wiped every run. `.gpx_cache/` persists (harvested
  from tarred NMEA), TTL-evicted via `--cache-max-age-days`.
- Restartable: a trip whose final `.mp4` exists is skipped unless `--force`.
- `*.gpx` / `*.mp4` / `*.html` / `*_links.txt` are gitignored — running with
  `--out .` litters the repo with them; keep outputs pointed at the working dir.
