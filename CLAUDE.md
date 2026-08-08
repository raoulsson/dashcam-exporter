# dashcam-exporter — project notes

Turns raw DDPAI dashcam SD-card footage (front + optional rear + NMEA GPS) into
one polished MP4 per **trip**, with burned-in overlays (timestamp, speed,
watermark), a moving-marker map widget + stats panel, automatic parking-skip,
and per-trip HTML / GPX / links / meta sidecars. DDPAI-specific (tested on Mola
N3 Pro). macOS-first (hardware VideoToolbox); Linux works via libx264.

The code lives under `src/dashcam_exporter/` in four layers — `domain/`
(the menu machine + frozen world), `application/` (the operator workflow + UI +
ports), `infrastructure/` (the renderer, media, config, adapters), `splice/`
(audio → transcript). `PYTHONPATH=src`; there is NO top-level `make_dashcam_videos.py`
any more (it moved to `infrastructure/media/renderer.py`). Two layers, and they
do not know much about each other.

`infrastructure/media/renderer.py` (~3600 lines) is the RENDERER: scanning, trip
grouping, GPS, sidecars, encoding. It has a full command line, is run as
`python -m dashcam_exporter.infrastructure.media.renderer`, and is what the rest
of this file is mostly about.

The other modules are the OPERATOR TOOL, an eleven-item menu (entry point
`python -m dashcam_exporter.application.workflow.pipeline`) that runs the renderer
as a child process and decides what may run when:

| module | lines | what is in it |
|---|---|---|
| `application/workflow/pipeline.py` | ~8000 | the step bodies, the terminal output, the world capture |
| `domain/menu/menu.py`   | ~770 | the item base classes, the step graph, the position machine |
| `domain/menu/items.py`  | ~800 | one class per menu item: what it is, where it leads, what blocks it |
| `domain/menu/guards.py` | ~600 | pure predicates over a captured world — every refusal lives here |
| `domain/model/world.py` | ~210 | the frozen facts a guard is allowed to look at |
| `application/ports/uploader.py` | ~700 | the seam an outside publisher implements |

The menu items are numbered `0..10`: 0 Progress, 1 Import SIM, 2 Generate Meta,
3 Build Preview, 4 Exclude Trip, 5 Build Website, 6 Render Trips, 7 Transcribe
Trips, 8 Upload Website, 9 Clean Workspace, 10 Delete SIM Data. Inserting item 7
in 2026-07 pushed Upload/Clean/Delete up by one — a renumber is global, and the
path/gate tests that type item numbers had to move with it.

`./run-tests.sh` runs 677 Python tests and 7 shell tests and prints `all green`;
it also fails on pyflakes undefined names and redefinitions. Anything touching
`guards.py`, `items.py` or `menu.py` is expected to come with a test, because
those decide whether footage may be deleted. The renderer itself stays a
prototype: reshaping it is fine, less code is better.

## The trip model (core concept)

A **trip** is the publishing unit (NOT an engine-on session, NOT a calendar
day). `group_into_trips` is a **park-to-park state machine**: a boundary is the
car actually PARKING at the anchor, not a radius crossing.

- **Anchor** = where the car last parked, carried forward. `home_lat` /
`home_lon` / `home_radius_m` seed it from config.txt; the gitignored .env WINS
for the coordinates (SET_HOME_LAT / SET_HOME_LON), which is where a real
address belongs. config.txt ships a Zurich example.
- **DEPART → drive → ARRIVE+PARK.** Departure and arrival are found by
  `VideoMotionDetector` — `drive_away_second` (median optical flow rises) and
  `park_second` (flow falls to baseline and stays), asked directly rather than
  through the ladder, because a GPS answer near the anchor splits trips. GPS position only gates WHICH clips get the
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
anchored by `VideoMotionDetector.drive_away_second` optical flow; see below).

### A parked run's boundaries come from the frames

`find_parking_runs` walks clips with `Track.is_parked` (GPS), which is the right
cheap sweep but wrong at the two ends — and the two ends are the only frames a
viewer sees, since everything between them is replaced by the slide. In a lot
the receiver decays instead of dropping to zero (18 km/h reported ten seconds
after the frame went still) and a car pulling out has no fix at all, which
reads as "parked". So the run's FIRST and LAST clip get the same ladder the
end-trim uses. That ladder is ONE object now: `MotionDetector` is an ABC with
`park_second` and `drive_away_second`; `VideoMotionDetector` and
`GpsMotionDetector` implement it, and `FirstAnswerDetector` asks each in turn
and keeps the first answer — video first, because it reads the wheels. Build it
with `motion_ladder(track, use_video=...)`; `--no-video-drive-detect` drops the
video rung rather than muting it. It used to be open-coded as `if x is None: x =
other(...)` at three call sites, which is how a fix reached one of them and left
the other two answering from the receiver. `_leads_into_parking` bounds the
arrival to the one clip per run that can hold it, and the far end is one
walk-back per run (VIDEO ONLY there — the clips have no fix, or a stale fast
one) — asking every clip would decode the whole trip.
`VideoMotionDetector._flow` caches the median-flow signal per clip, so the
render re-asking the exit clip is free.

`parking_exit_pad` (default 4, mirroring `parking_entry_pad`'s 3) is live on the
video path; it used to be ignored there in favour of a 2-second constant. Note
`--debug-cuts N` shows N seconds from the START of the exit slice, so N must
exceed `parking_exit_pad` for a preview to reach the drive-away at all.

`settle_speeds_after` zeroes the speed overlay from a detected park onset.
`Track.speeds` fixes alignment lag; a decaying receiver is not an alignment
problem, and only the frames can settle it.

### Parking-exit drive-away = video ego-motion, not GPS

`VideoMotionDetector.drive_away_second` anchors the parking exit slice, via the
ladder. GPS speed is useless
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

### Transcription (menu item 7)

`Transcribe Trips` turns rendered MP4s into transcript sidecars. It works ONLY on
already-rendered MP4s (`world.renders`) — `evaluate` returns `blocked("no rendered
MP4s to transcribe")` when none exist, so there is no transcript without a render.
Repeatable, opt-in, never a dead-end (the same edges as after a render). Code is
under `src/dashcam_exporter/splice/` (`transcription/`, `audio/`, `diarization/`,
`cli/`); the step body is `step_transcribe` in `pipeline.py`.

Pipeline per trip: splice the MP4's audio to MP3 (ffmpeg `-vn libmp3lame`) →
voice-enhance (`afftdn` denoise + `highpass=80`/`lowpass=12000` + `loudnorm`) →
transcribe with **faster-whisper** (`small`, int8, beam 5). Diarization is optional
and prompted per run (`Use speaker diarization?`, default no): **pyannote.audio**
`speaker-diarization-3.1`, which needs `hf_token` (config) or `HF_TOKEN` env — a
diarize run with no token aborts before touching audio. Deps `faster-whisper>=1.1`
+ `pyannote.audio>=3.3` (venv only); missing them silently skips.

Two sidecars land beside the MP4, and a trip counts as transcribed only when BOTH
exist: `<video>.transcript.txt` (UTF-8, segments grouped into 350–700-char
paragraphs, speaker-prefixed when diarized) and `<video>.transcript.timeline.json`
(`{"format":"paragraph-timeline/v1","paragraphs":[…]}` mapping each paragraph to
media time + character offsets). The sibling `goodnight-drives` site reads these as
a Map/Transcript tab that follows the video. Noise filtering is ON (drops
`sd card loaded/loading`); repetition dedup (`_dedupe_repetition`) exists but is
DELIBERATELY DISABLED — raw repetitions are kept for now while transcripts are
checked against the recording. Config knobs: `hf_token`, `diarization_model`
(default `pyannote/speaker-diarization-3.1`); whisper model/device are hardcoded.

### Env / venv

Homebrew's `python3` is externally-managed (PEP 668), so Pillow/staticmap (map
widget) can't install into it. Use `.venv` (`python3 -m venv .venv && .venv/bin/
pip install -r requirements.txt`); the wrapper scripts auto-prefer `.venv/bin/
python` when it exists. Without Pillow the render still works but silently drops
the map-widget panel (prints a warning). `.venv/` and `.env` are gitignored.

## The operator tool, and what must not be broken in it

Four rules hold the delete gates together. Everything else in these modules can
be reshaped; break one of these and footage goes.

- **A guard is a pure function of a frozen `World`.** It never touches the disk
  and never calls the plugin. Two reads of one world give one answer, which is
  what makes the re-check after a typed word meaningful.
- **Outbound edges are authored on the item; inbound is derived** by
  `menu.MenuGraph.inbound()` from every other item's outbound. Never hand-write an
  inbound set — `menu.disagreements()` exists to report where the derivation
  parts company with the owner's table, and a hand-edit hides that.
- **Destructive items ask for a typed word, then capture the world AGAIN** and
  re-ask the same guard callable before acting. Each asks for its own verb —
  `EXCLUDE` at 4, `CLEAN` at 9, `DELETE` at 10 — and the way past item 10's own
  refusal asks `ERASE`, so habit cannot carry anyone through a guard.
- **`is_complete()` is three-valued and fails closed.** A destination that
  cannot be reached answers UNKNOWN, never NO and never YES, because the next
  thing the operator does is erase the only local copy.

### The uploader plugin (the publishing seam)

Publishing is optional and lives entirely behind ONE seam — the interface in
`application/ports/uploader.py`. An outside plugin supplies TWO classes in one
file: a `Builder` (item 5, Build Website — builds what THIS install publishes)
and an `Uploader` (item 8, Upload Website — puts it there). Wired by a FILE PATH,
not a module: `SET_UPLOAD_PLUGIN=<path.py>:<BuilderClass>:<UploaderClass>` in the
`.env` (or `upload_plugin` in config.txt). Unconfigured, item 8 is greyed and
Build Website writes the local self-contained page instead. A configured plugin
that will not load stops the tool at startup, loudly — never a silent degrade to
the local edition, because a render that quietly stops reaching the world looks
identical to one that is publishing fine.

The exporter asks the plugin only four things — `evaluate` (may this run / would
it do anything), `execute` (do it), `describe` (one line), and, on the uploader,
`is_complete(trip_ids)` → YES/NO/UNKNOWN/NA. Everything else it answers itself:
which trips are in scope (read off the import's sidecars, so a trip that never
rendered is still on the list), whether they rendered here, whether the operator
typed the word, and which item may follow which. So a plugin that answers yes to
everything STILL cannot talk Clean Workspace (9) into erasing an import that
produced no renders — `is_complete` gates that erase, is all-or-nothing over the
import's trips, and fails closed. To add one: copy `examples/local_website.py`
(the suite drives it through the erase gates twice, so it can't rot). The
method-by-method table is in the README's "Publishing — plugging in your own";
the why-a-type-not-config-keys reasoning is in `docs/public-edition.md`.

## Data layout (outside the repo)

- Inputs:  `~/dashcam-data/import/<label>/DCIM/{200video/{front,rear},203gps,...}`
  — one folder per import (folder name is arbitrary, usually the import date;
  grouping is timestamp-driven and one folder can span several days of clips).
- Outputs: `~/dashcam-data/export/` — the `--out` target. Output is
  **namespaced by import**: `export/<import-name>/<extract-day>/`, e.g.
  `export/2026-07-19/2026-07-15/`. The import folder (`root.name`) is the
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
the card — deletion is opt-in via `--delete`, and `--keep` is a no-op that
means the same thing. With `--delete` it removes the card's files and keeps the
folder tree so the camera can record. Nothing is deleted until the verify pass
succeeds, and the verify refuses if rsync itself failed. `AFTER_STAMP` in the
environment makes it a delta copy; `--delete` is refused after one, because the
skipped clips were verified by an earlier run this script cannot see.

## Running

- `./RUN-DASHCAM-EXPORTER.sh` — the operator tool. Picks an interpreter that has
  cv2, numpy, staticmap and PIL, then execs
  `python -m dashcam_exporter.application.workflow.pipeline`, which takes no
  arguments at all: every value comes from `config.txt` and the gitignored
  `.env`. A flag would be a second answer to a question `config.txt` already
  answers, and the compiled-in default wins silently when the two disagree.
- `./list-trips-data.sh` — dry-run; list trips with index, day label, span, clips.
- `./make-trips-rendered.sh [N …]` — encode; leading ints select trip indices
  (via `--drives`, alias `--trips`), rest passes through. A FULL render (no
  `--drives`) first clears ONLY the day folders it is about to write, so shifted
  trip indices leave no stale duplicates — but other imports' day folders in the
  same `--out` are never touched (rendering the 07-19 import clears 07-15..18,
  leaves an unrelated 05-11 alone). Hidden entries and the root
  `.gpx_cache`/`.geocode_cache.json` are always kept. The reset lives in
  `infrastructure/media/renderer.py` (it knows which days it'll write), is skipped
  for a `--drives` subset and `--sidecars-only`, and is disabled by `--no-clean-days`.
  Logs to `run-<ts>.log` inside `--out/logs/`. Keep the reset in the renderer,
  never the wrapper: a wrapper-level wipe cannot know which days a run writes,
  so it would take other imports' output with it.
- `./list-trips-data.sh` and `./make-trips-rendered.sh` both exec
  `python -m dashcam_exporter.infrastructure.media.renderer` (the first with
  `--dry-run`); flags after the leading trip indices pass straight through.
- Direct: `python -m dashcam_exporter.infrastructure.media.renderer --root … --out … [--dry-run|--sidecars-only|--force]`.
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
