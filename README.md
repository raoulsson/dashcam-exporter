# dashcam-exporter

Turn a dashcam SD card into watchable drives.

A DDPAI camera writes one-minute clips, thousands of them, with the GPS track in
a separate file and no idea where one journey ends and the next begins. This
reads the card, works out where you actually drove, and renders each journey as
a single video — moving map, speed readout, timestamp burned in, parking cut.
Then, if you want, it publishes them to a private website only the people you
invite can see.

It is one interactive tool. You pick a numbered step, and it tells you what it
is about to do and what that will cost before it does it.

> **DDPAI only.** The card layout, GPS log format and clip naming are specific to
> DDPAI cameras. Developed against a **Mola N3 Pro**, on macOS. Linux should
> work. Windows is untested.

---

## Start here

```bash
git clone git@github.com:raoulsson/dashcam-exporter.git
cd dashcam-exporter
./INSTALLER.sh              # builds .venv, installs everything, checks ffmpeg
./RUN-DASHCAM-EXPORTER.sh   # the tool
```

`INSTALLER.sh` needs ffmpeg on the system (`brew install ffmpeg`, `apt install
ffmpeg`) — the one thing it cannot install for you. Everything else it handles,
and it is safe to run again.

`RUN-DASHCAM-EXPORTER.sh` takes no arguments. Neither does the tool behind it:
every setting lives in `config.txt`, and the private ones in `.env`. That is
deliberate — see [One source of truth](#one-source-of-truth).

---

## What it looks like

```
  dashcam pipeline   card -> render -> S3 -> site

-- status ---------------------------------------------------------------------
  SD card      mounted  /Volumes/NO NAME  (222 clips)
  Import       ~/dashcam-data/import/2026-07-28  612 clips, 116.6 GB
  Rendered     6 mp4  8.3 GB in ~/dashcam-data/output
  Local site   ~/dashcam-data/output/dashcam_import_data_site.html  built 22 min ago
  Prepared     29 trips  updated 2 h ago
  Live site    23 trips
  Repos        ~/dev/dashcam-exporter | ~/dev/goodnight-drives
--------------------------------------------------------------------------------
  disk: 238.8 GB free of 917.0 GB
--------------------------------------------------------------------------------
    1) Import from SIM    ! 4) Exclude trip         7) Upload to site     ! 10) Clean SIM
    2) List trips           5) Render videos        8) Update site
    3) Preview trips        6) Create website     ! 9) Delete SIM data
   0) status    all = 1-3,5-8    q = quit    (4,9,10 destructive, alone only)

Select>
```

Steps you have not configured are greyed out with the reason underneath, rather
than hidden. The numbering never moves, so "run 5" means the same thing on every
machine.

### The ten steps

| | Step | What it does |
|---|---|---|
| 1 | **Import from SIM** | Copies the card's `DCIM` tree into the workspace and verifies it file-for-file. Takes only clips newer than the last import. |
| 2 | **List trips** | Scans and prints the trip table. Reads nothing else, changes nothing. |
| 3 | **Preview trips** | Sidecars, one still per trip, and a local contact sheet. No encoding. |
| 4 | **Exclude trip** &#9888; | Deletes one trip's source clips, its render and its site entry. |
| 5 | **Render videos** | Encodes the chosen trips. The slow step — hours for a full card. |
| 6 | **Create website** | One self-contained HTML page built from the renders. Nothing leaves the machine. |
| 7 | **Upload to site** | Syncs the mp4s to your bucket, then verifies they arrived. |
| 8 | **Update site** | Runs the site repo's deploy. |
| 9 | **Delete SIM data** &#9888; | Erases the imported footage and the renders — once the site serves every trip. The card is step 10's job. |
| 10 | **Clean SIM** &#9888; | Erases the card's clips, keeping its folder structure. |

Steps 4, 9 and 10 destroy footage. They run alone, never in a batch, and each
needs a word typed — `DROP`, `DELETE`, `ERASE` — not an Enter pressed.

---

## The normal cycle

```
1   import          card -> workspace, only the new clips
10  clean SIM       the card is now free — put it back in the car
2   list            what is on it
3   preview         look at each trip before spending hours encoding
4   exclude         drop the ones not worth keeping          (optional)
5   render          the long one
6   create website  a local page you can open and check
7   upload          mp4s to the bucket
8   update site     publish
9   delete SIM data the disk is freed
```

**10 comes second, not last.** As soon as the import has landed and verified,
the card has served its purpose — everything after that reads from the
workspace, and the encode alone is hours. Running 10 here is the whole reason
the import copies only new clips: the card goes back in the car while the slow
half runs. Step 9 at the end frees the disk; it does not touch the card,
because by then the card is not here.

Steps 2 and 3 exist because encoding is hours and uploading is days. Deciding
what to keep has to be possible before either, and a still plus a map is enough
to make the call.

You will not always run all of them. With nothing configured the cycle is 1→6:
import, render, and a local website. See [Two editions](#two-editions).

---

## Example output

Default layout — front camera, rear picture-in-picture bottom-centre, timestamp,
speed and watermark in the corners, stats panel and map on the right:

![Default frame layout](examples/dash-default-view.jpg)

The same composition with the rear PiP moved to the top-left
(`rear_pip_position = top-left`):

![Rear PiP in top-left](examples/dash-rear-view-top-left.jpg)

The interactive HTML map sidecar — Leaflet and OSM tiles, route coloured by
speed, a dot at each segment break:

![Interactive HTML map sidecar](examples/gps-data-on-map.jpg)

The `.gpx` sidecar in [gpx.studio](https://gpx.studio). One `<trkseg>` per
contiguous driving segment, so each engine-on leg is its own polyline:

![Per-trip GPX in gpx.studio](examples/gps-data-single-drives-on-gpx.studio.jpg)

---

## Where things live

```
~/dashcam-data/                     <- the workspace root
|-- import/                         <- import_dir: footage being worked on
|   `-- 2026-07-28/DCIM/...         <- one folder per import
|-- final_2026-07-28/               <- the finished deliverable, BESIDE output/
|                                      (output/ is swept on every import)
`-- output/                         <- derived from import_dir; renders land here
    |-- 2026-07-28/                 <- rendered trips, namespaced by the import
    |   |-- trip_2026-07-28_08-57_01_h1080.mp4
    |   |-- trip_2026-07-28_08-57_01.gpx
    |   |-- trip_2026-07-28_08-57_01.html       <- interactive map
    |   |-- trip_2026-07-28_08-57_01_meta.json  <- the state that outlives the video
    |   `-- trip_2026-07-28_08-57_01_links.txt
    |-- previews/preview_2026-07-28.html        <- the contact sheet
    |-- logs/run-20260728-192417.log
    |-- .imported.json                          <- the ledger
    `-- .owned-by                               <- which checkout owns this dir
```

Two settings decide all of it:

- **`import_dir`** — the workspace. Imports land here; renders, scans and the
  drop step read from it. Defaults to `~/dashcam-data/import`.
- **`card`** — where the SD card mounts. Read only by the import step. Defaults
  to `/Volumes/NO NAME`.

`out` is derived from `import_dir` (`<parent>/output`) unless you set it. That
matters more than it looks: two checkouts pointed at one output directory means
step 1 in either sweeps the other's work. That is not hypothetical — it happened
here, and it killed a render in progress. Hence `.owned-by`, which makes a
checkout refuse to sweep a directory another one has claimed.

### Only metadata is kept

The working area is swept on every import: everything that is not `logs/`, the
ledger, the owner marker, a `_meta.json`, or a `final_*` folder. Renders are
~1.4 GB each and live on S3 once uploaded; source clips are ~400 MB each and
live on the card until you erase it. What survives is a few KB of metadata —
and that is the point. `_meta.json` and `.imported.json` answer "have I already
imported this card" long after the footage is gone, and they are what Clean SIM
reads to decide whether a card's clips are inside a rendered trip.

The sweep only runs when the round is **finished**, and finished means one of
two things: every render is inside a `final_<date>` folder, or every render is
in the bucket. Either way the workspace holds nothing that exists only there.
If neither is true it deletes nothing, names the files, and says which of the
two would settle it.

Anything you park in the working area goes with it. It is the tool's workspace,
not a shelf.

---

## Trips, and why the grouping is the whole game

A trip is one journey: from leaving somewhere until you come back, or a long
engine-off gap, or the 04:00 day rollover. Short stops inside a trip — fuel,
lunch, a hike — do not split it; they become a "fast forwarding" slide, so the
trip plays as one continuous video instead of forty minutes of stationary
bumper.

Boundaries are found by **video ego-motion**: actually looking at the frames to
see when the car starts moving. There is a GPS-radius fallback, and the two
disagree badly. On one card the fallback found 9 trips over 15h12m where
ego-motion found 6 over 10h48m — inventing a 3-second trip and folding 4.5 hours
of parked recording into a "drive".

Everything downstream inherits that grouping: the previews you judge from, the
renders, and which files the delete step removes. So the tool **refuses to
start** without numpy and opencv rather than quietly producing a different
answer. That is what `RUN-DASHCAM-EXPORTER.sh` checks before handing over — on a
machine with four Pythons on PATH, only one of them has opencv.

---

## Two editions

Nothing about publishing is required. The tool works out what it can do from
what you have configured:

| Configured | You get |
|---|---|
| nothing | Import, render, and `dashcam_import_data_site.html` — one self-contained page, every still embedded, every route drawn from its GPX. Opens from `file://` with no network. |
| `site_repo`, `s3_bucket` | Steps 7 and 8 wake up: upload to your bucket, deploy to your site. |

Unconfigured, steps 7 and 8 stay in the menu, greyed out, with the setting that
would enable them printed underneath. Run the cycle and the result is gathered
into `final_<date>/` — page, videos and sidecars together, ready to move
wherever you keep things.

The publishing half is one person's setup, so it lives in the **gitignored
`.env`**, never in `config.txt`:

```
SET_SITE_REPO      SET_S3_BUCKET      SET_S3_REGION      SET_LIVE_TRIPS_URL
SET_HOME_LAT       SET_HOME_LON
```

`config.txt` is tracked, so a value written there gets committed and pushed.
Also not theoretical: it happened, and the history had to be rewritten to remove
it. `.env.example` shows the shape.

---

## One source of truth

`pipeline.py` has no command line. Not one flag.

It used to have six, and each was a second answer to a question `config.txt`
already answered. They disagreed exactly once, and once was enough: a
compiled-in default outlived the config meant to replace it, a fresh clone
inherited another checkout's output directory believing it was its own, and the
sweep followed the constant into a running render.

Everything a person sets is in **[config.txt](config.txt)** — 70 settings, each
documented where it lives. That is why this README does not list them: the file
is the reference, and it cannot drift from itself.

The handful most people touch:

| Setting | Default | |
|---|---|---|
| `import_dir` | `~/dashcam-data/import` | the workspace |
| `card` | `/Volumes/NO NAME` | where the SD card mounts |
| `out` | next to `import_dir` | where renders land |
| `output_height` | `1080` | 720 and 540 are much smaller files |
| `x264_crf` | `26` | quality; lower is bigger and better |
| `speed_colour` | `true` | colour the route by speed |
| `final_dir` | beside the output dir | where the finished folder is gathered |

Two environment variables still apply, because they are conventions rather than
settings of this tool: `NO_COLOR` disables colour, and `DASHCAM_IMPORT_ROOT`
overrides the workspace for a single run.

---

## Getting the card back in the car

The import copies **only what is new**. It reads the high-water mark from the
ledger and the rendered metadata — both of which survive deleting the footage —
and takes the clips stamped after it. 612 new clips out of 1039 means copying
116 GB instead of 198.

Then step 10 frees the card, without waiting for the encode:

```
  Card:  /Volumes/NO NAME
  Holds: 1039 file(s), 198.4 GB
  Imported through 20260728155513

  All 1039 clip(s) on the card were imported and verified.
  Still here: 612 source clip(s) in the workspace.
  Not yet published (nothing from it was rendered).
  Fine for the card - the copy above is verified - but it does
  mean that copy is now the only one, so do not lose it.

  Type ERASE to clean the card, anything else to cancel:
```

It does **not** require the trips to be rendered or uploaded. The import is a
verified copy on a disk you control; holding the card hostage until an encode
and an upload finish would leave the car without a camera for exactly as long as
the slow half takes.

It refuses in two cases, and refuses rather than asks:

- the card holds clips newer than anything imported — those exist nowhere else
- the ledger claims an import that nothing on this machine still holds

The second one is subtle and worth stating. A ledger records that a copy was
*made*. It cannot see that the copy was deleted afterwards, and both look
identical from where it sits. So the guard demands evidence the copy still
exists: published, or rendered, or the source clips present. No confirmation
prompt makes deleting the only copy recoverable, which is why that case is a
refusal and not a question.

---

## Running the pieces directly

The CLI drives three scripts. Run them yourself when you want something it does
not offer:

```bash
./import-sd-card.sh 2026-07-28                  # copy the card, verify file-for-file
./list-trips-data.sh                            # the trip table, nothing else
./make-trips-rendered.sh                        # render every trip
./make-trips-rendered.sh 6 8                    # only trips 6 and 8
./make-trips-rendered.sh --sidecars-only        # maps and metadata, no encoding
./make-trips-rendered.sh 8 --output-height 720  # one trip, smaller
```

`make_dashcam_videos.py` is the renderer underneath; `--import-dir` (formerly
`--root`) names the tree to render, and `--help` lists the rest.

Renders are restartable — a finished clip is not re-encoded, so an interrupted
run picks up where it stopped. Every run logs to `<out>/logs/run-<stamp>.log`,
and a copy lands beside each finished mp4.

Re-running step 5 renders only the trips that have no video. Naming trips
explicitly re-encodes those, and only those.

---

## The card itself

Expected layout — the DDPAI writes this, you do not create it:

```
/Volumes/NO NAME/DCIM/
|-- 200video/front/20260728141441_0060.mp4
|-- 200video/rear/20260728141441_0060_A.mp4
`-- 203gps/...                      <- the GPS track
```

A clip's name carries its timestamp (`YYYYMMDDHHMMSS`), which is what makes the
delta import a string comparison rather than a guess.

If your card came formatted so macOS will not mount it, see
[docs/sd-card-formatting/](docs/sd-card-formatting/FORMATTING_SD_HACK.md).

---

## Publishing

The other half is [goodnight-drives](https://github.com/raoulsson/goodnight-drives):
a private website with invite-only access, videos in a private S3 bucket served
through CloudFront as short-lived signed URLs. Point `site_repo` at it in `.env`
and steps 7 and 8 wake up.

You do not need it. The local website is a real deliverable on its own.

---

## Requirements

- **ffmpeg** and **ffprobe** — every render, still and duration goes through them
- **Python 3.9+** with numpy, opencv, staticmap, Pillow — `INSTALLER.sh` handles these
- **rsync** — for the import; macOS ships `openrsync`, which works
- optional: **awscli**, configured, for the upload step

---

## Docs

- **[config.txt](config.txt)** — every setting, documented where it lives
- **[docs/public-edition.md](docs/public-edition.md)** — notes on the
  configured/unconfigured split
- **[docs/sd-card-formatting/](docs/sd-card-formatting/FORMATTING_SD_HACK.md)** — patching the MBR when the card will not mount
- **[CLAUDE.md](CLAUDE.md)** — architecture notes

## Licence

MIT. See [LICENSE](LICENSE).
