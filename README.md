# dashcam-exporter

Turn a dashcam SD card into watchable trips.

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
-- configuration (read from ~/dev/dashcam-exporter/config.txt) ------------------
  SIM Card             /Volumes/NO NAME
  Workspace Directory  ~/dashcam-data
  Import Directory     ~/dashcam-data/import
  Export Directory     ~/dashcam-data/export
  Running in           ~/dev/dashcam-exporter
  Export-Mode          uploader: User plugin handles build and upload of website
  Plugin               MyBuilder, MyUploader
    Location           ~/dev/my-site/exporter_uploader.py
    Description        Send the videos to S3 and deploy the pages.
  Python               3.14.6 (~/dev/dashcam-exporter/.venv/bin/python)
--------------------------------------------------------------------------------

-- status ----------------------------------------------------------------------
    SIM Card     mounted  222 clips         (/Volumes/NO NAME)
    Imported     612 clips, 116.6 GB       (~/dashcam-data/import/2026-07-28)
    Rendered     6 mp4, 8.3 GB             (~/dashcam-data/export)
    Published    all 6 trips are live      (goodnight-drives.com)
    Disk         238.8 GB free of 917.0 GB (/Volumes/Macintosh HD)
--------------------------------------------------------------------------------
================================================================================
  1) Import SIM           4) Exclude Trip         7) Upload Website
  2) Generate Meta        5) Build Website        8) Clean Workspace
  3) Build Preview        6) Render Trips         9) Delete SIM Data
  p = progress   h = help   i = info   q = quit

Select>
```

The menu is drawn from the state machine, so everything on it is an answer
rather than a label. An item you cannot pick is greyed out, and `p` says why —
which of the two gates refused: the **graph** (this does not follow where the
pipeline is) or the item's own **guard** ("no card at ...", "no GPS track in
the import"). The numbering never moves, so "run 5" means the same thing on
every machine.

### The nine steps

Plus `p` — progress: which trips exist, what has been done to them, what can
be done next and why the rest cannot. It is not numbered, because looking at
the workspace is not a step in working through it.

| | Item | What it does |
|---|---|---|
| 1 | **Import SIM** | Copies the source's `DCIM` tree into the workspace and verifies it file-for-file. Takes only clips newer than the last import. |
| 2 | **Generate Meta** | Writes each trip's sidecars — `_meta.json`, `.gpx`, `.html` map. The metadata everything downstream reads. |
| 3 | **Build Preview** | One still per trip and a local contact sheet, from the sidecars. No encoding. |
| 4 | **Exclude Trip** &#9888; | Deletes the chosen trips' source clips, their renders and their sidecars. Deleting locally does not unpublish: a trip already at the publishing target stays there. |
| 5 | **Build Website** | Builds what this installation publishes, from the trips that have been described. With nothing configured: one self-contained HTML page from the renders, and nothing leaves the machine. |
| 6 | **Render Trips** | Encodes the chosen trips. The slow step — hours for a full card. |
| 7 | **Upload Website** | Puts it online, through whatever you configured. One job — how many transports it takes is the implementation's business, not the menu's. |
| 8 | **Clean Workspace** &#9888; | Erases the imported footage and the renders it produced, once this machine and the publishing target both say they are safe. |
| 9 | **Delete SIM Data** &#9888; | Erases the card's clips, keeping its folders so the camera can record, once every clip is accounted for elsewhere. |

Items 4, 8 and 9 destroy footage, and each asks for a word to be typed rather
than an Enter pressed. The word is the entry's own verb: `EXCLUDE` at Exclude
Trip, `DELETE` at Clean Workspace and Delete SIM Data. Excluding records a
decision against the trip and the other two do not, so a prompt that named them
alike taught the wrong idea of what was about to happen.

Delete SIM Data has one refusal with a way past it — dropping clips that exist
nowhere else, deliberately — and that way past asks for `ERASE`. Stepping over
a guard is not reachable by the muscle memory of the word that guard asks for.

One item runs at a time. There is no batch selection, because what may follow
depends on what the last item actually did — an Exclude Trip that the operator
cancelled leaves the pipeline exactly where it was.

---

## The normal cycle

```
1   import sim        card -> workspace, only the new clips
2   generate meta     sidecars: the map, the stats, the places
3   build preview     look at each trip before spending hours encoding
4   exclude trip      drop the ones not worth keeping          (optional)
5   build website     what your target publishes; a local page if there is none
6   render videos     the long one
7   upload website    put it online, through whatever you configured
8   clean workspace   erase the footage and the renders — the cycle is closed
9   delete sim data   free the card, at any point once its clips are safe
```

With a publishing target configured the cycle does not run straight down. A trip
is publishable as soon as it is described: its route, its distance, its places
and its map all come out of the sidecars item 2 writes, and only playback waits
on the encode. So the usual pass is 1, 2, 3, 4, then 5 and 7 — the site is up in
a minute, with each drive reading "This drive isn't available to play yet" —
then 6 for the long encode, then 5 and 7 again to bring the videos. Item 7 is
offered from every step in the cycle; nothing in the graph makes you build
first, because the publisher answers for itself when there is nothing built.

Progress (`p`) is not in the sequence because it is not a transition — it is
the view you run at any point to see where things stand, including on an empty
workspace. It neighbours everything and never becomes the position.

Items 2 and 3 exist because encoding is hours and uploading is days. Deciding
what to keep has to be possible before either, and a still plus a map is enough
to make the call.

**Clean Workspace (8) and Delete SIM Data (9) are two items, not one.** They
erase two different things and the evidence for each is different: 8 wants the
renders published, 9 wants every clip on the card accounted for somewhere you
can go and look. Folded together they are a defect rather than a tidy-up: a
single step gathers the card's evidence from the workspace, erases the
workspace, and only then checks the card — so it refuses after the irreversible
half has run, having already printed that the card was verified. Split, that
cannot happen: Clean Workspace's outbound is Import SIM and itself, and cleaning
a second import reaches nothing else, so it can never precede Delete SIM Data.

9 hands the position back to wherever you were, because freeing the card does
not interrupt the cycle. So the safe order — erase the card while its clips are
provably in the workspace, then clean the workspace once the renders are
published — is the one the graph permits, and the dangerous order is the one it
forbids.

You will not always run all of them. With nothing configured you run 1, 2, 3 and
4, then 6 to render and 5 to write the local page and gather the result, then 8
and 9. The local build needs the renders, because gathering them is half of what
it does. See [Two editions](#two-editions).

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
|-- export/                         <- export_dir: sidecars and renders land here
|   |-- 2026-07-28/                 <- namespaced by the import
|   |   `-- 2026-07-28/             <- and by the trip's day
|   |       |-- trip_2026-07-28_08-57_01_h1080.mp4
|   |       |-- trip_2026-07-28_08-57_01.gpx
|   |       |-- trip_2026-07-28_08-57_01.html       <- interactive map
|   |       |-- trip_2026-07-28_08-57_01_meta.json  <- the state that outlives the video
|   |       `-- trip_2026-07-28_08-57_01_links.txt
|   `-- previews/preview_2026-07-28.html            <- the contact sheet
|-- final_2026-07-28_1924/          <- the finished deliverable, BESIDE export/
|                                      (the export tree is swept by Clean Workspace)
|-- logs/run-20260728-192417.log
`-- pid.lock                        <- this workspace is busy right now

~/.dashcam-exporter/                <- what this checkout remembers between cycles
|-- state/imported.json             <- the ledger
|-- state/excluded.json             <- the trips dropped on purpose
|-- state/owned-by                  <- which checkout owns the export dir
`-- processed/2026-07-28/           <- the archived receipts of finished cycles
```

Three settings decide the layout, and a fourth names the source. None of them is
guessed from another, so the three directories can sit on three different disks:

- **`workspace`** — where the session's own files live: `pid.lock`, which says
  this workspace is busy, and `logs/`. Defaults to `~/dashcam-data`.
- **`import_dir`** — where a copy of the card is kept. Renders, scans and
  Exclude Trip read from it. Defaults to `<workspace>/import`.
- **`export_dir`** — where the sidecars and the rendered trips are written, and
  what a publishing target reads. Defaults to `<workspace>/export`.
- **`card`** — the footage source: any directory holding a `DCIM` tree. The
  mounted SD card is the common case, but a card copied onto an external disk
  or a folder someone handed over works the same. Read only by Import SIM.
  Defaults to `/Volumes/NO NAME`.

`export_dir` defaults to `<workspace>/export` and is never worked out from
`import_dir`. That matters more than it looks: two checkouts pointed at one
export directory means Clean Workspace in either erases the other's work —
including a render in progress. `owned-by` is the guard: a checkout refuses to
sweep a directory another one has claimed.

### Only metadata is kept

Clean Workspace erases everything in the export tree that is not a `final_*`
folder. The trip receipts are archived to `~/.dashcam-exporter/processed/` on
the way past, and the ledger, the exclusion record and the owner marker are not
in the export tree at all — they live in `~/.dashcam-exporter/state/`, which is
why the sweep has no keep-list to get wrong. Renders are ~1.4 GB each and
live at the publishing target once uploaded; source clips are ~400 MB each and
live on the card until you erase it. What survives is a few KB of metadata — and that is the
point. `_meta.json` and `imported.json` answer "have I already imported this
card" long after the footage is gone, and they are what Delete SIM Data reads
to decide whether a card's clips are inside a rendered trip. That is also why
it is a separate item: the evidence it needs must not be erased by the item
that would otherwise have run first.

Import SIM sweeps nothing. It refuses over an unfinished session instead —
importing on top of one mixes two cards into a single grouping with no record of
which clip came from which — and points at Clean Workspace, which it reaches
directly.

The sweep only runs when the round is **finished**, and finished means one of
two things: every render is inside a `final_*` folder, or the configured
plugin says every trip of this import is complete at the destination. Either way the workspace holds nothing that
exists only there. If neither is true it deletes nothing, names the files, and
says which of the two would settle it.

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
disagree badly. On one card the fallback finds 9 trips over 15h12m where
ego-motion finds 6 over 10h48m — inventing a 3-second trip and folding 4.5 hours
of parked recording into a "drive".

Everything downstream inherits that grouping: the previews you judge from, the
renders, and which files Exclude Trip removes. So the tool **refuses to
start** without numpy and opencv rather than quietly producing a different
answer. That is what `RUN-DASHCAM-EXPORTER.sh` checks before handing over — on a
machine with four Pythons on PATH, only one of them has opencv.

---

## Two editions

Nothing about publishing is required. The tool works out what it can do from
one setting:

| Configured | You get |
|---|---|
| nothing | Import, render, and `dashcam_export_data_site.html` — one self-contained page, every still embedded, every route drawn from its GPX. Opens from `file://` with no network. |
| `upload_plugin` | Item 5 builds what YOUR plugin publishes instead of the local page, and item 7, Upload Website, wakes up. |

Unconfigured, item 7 stays in the menu, greyed out, and `p` gives the reason.
Run the cycle and the result is gathered into `final_<day>_<import>/` — page,
videos and sidecars together, ready to move wherever you keep things.

There is no second edition of this repo and no fork. There is one program, and
whether it publishes depends on whether you supplied something that publishes.

The value lives in the **gitignored `.env`**, never in `config.txt`:

```
SET_UPLOAD_PLUGIN      SET_HOME_LAT      SET_HOME_LON
```

`config.txt` is tracked, so a value written there gets committed and pushed —
and undoing that means rewriting history, not just deleting the line.
`.env.example` shows the shape.

---

## One source of truth

`pipeline.py` has no command line. Not one flag.

A flag is a second answer to a question `config.txt` already answers, and when
the two disagree the compiled-in default wins silently: a fresh clone inherits
another checkout's export directory believing it is its own, and the sweep
follows the constant into that checkout's renders. One place to set a value
means one place for it to be wrong.

Everything a person sets is in **[config.txt](config.txt)**, each setting
documented where it lives. That is why this README does not list them: the file
is the reference, and it cannot drift from itself.

The handful most people touch:

| Setting | Default | |
|---|---|---|
| `workspace` | `~/dashcam-data` | the root the other two default inside |
| `import_dir` | `<workspace>/import` | where a copy of the card is kept |
| `export_dir` | `<workspace>/export` | where the sidecars and renders land |
| `card` | `/Volumes/NO NAME` | the footage source — any folder with a `DCIM` tree |
| `output_height` | `1080` | 720 and 540 are much smaller files |
| `x264_crf` | `26` | quality; lower is bigger and better |
| `speed_colour` | `true` | colour the route by speed |
| `final_dir` | beside the export dir | where the finished folder is gathered |

Two environment variables apply, because they are conventions rather than
settings of this tool: `NO_COLOR` disables colour, and `DASHCAM_IMPORT_ROOT`
overrides the workspace for a single run.

---

## Getting the card back in the car

The import copies **only what is new**. It reads the high-water mark from the
ledger and the rendered metadata — both of which survive deleting the footage —
and takes the clips stamped after it. 612 new clips out of 1039 means copying
116 GB instead of 198.

The card itself is freed by Delete SIM Data (9), which is its own item and can
run at any point in the cycle — freeing the card does not interrupt anything,
so completing it hands the position straight back. It refuses in two cases,
and refuses rather than asks:

- the card holds clips newer than anything imported — those exist nowhere else
- the ledger claims an import that nothing on this machine still holds

The second one is subtle and worth stating. A ledger records that a copy was
*made*. It cannot see that the copy was deleted afterwards, and both look
identical from where it sits. So the guard demands evidence the copy still
exists: published, or rendered, or the source clips present. No confirmation
prompt makes deleting the only copy recoverable, which is why that case is a
refusal and not a question. The accounting is per clip, with no gaps —
approving on any single accounted clip lets one rendered trip vouch for a whole
card, and the wipe then erases clips whose only copy is the card.

A card that is already empty is not a refusal either. It answers "the card
holds no clips — nothing to erase" and completes, because saying "no copy is
still on this machine" at a card that is simply empty is a guard crying wolf,
and that is how an operator learns to stop reading guards.

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

`make_dashcam_videos.py` is the renderer underneath; `--import-dir` (or its
alias `--root`) names the tree to render, and `--help` lists the rest.

Renders are restartable — a finished clip is not re-encoded, so an interrupted
run picks up where it stopped. Every run logs to
`<workspace>/logs/run-<stamp>.log`, and a copy lands beside each finished mp4.

Re-running Render Trips (6) renders only the trips that have no video. Naming trips
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

## Publishing — plugging in your own

This repo does not know where your videos go, and it is not supposed to. It
knows one interface — an ACT of publishing work, in [uploader.py](uploader.py) —
and it calls whatever classes you point it at.

Set one thing, in the gitignored `.env`:

```
SET_UPLOAD_PLUGIN=~/dev/my-site/my_plugin.py:MySiteBuilder:MySiteUploader
```

`<path to a .py>:<BuilderClass>:<UploaderClass>`. One plugin, two classes: the
builder is item 5, the uploader is item 7. A file path rather than a dotted
module name, because your implementation lives somewhere that is not installed
and must not have to be — and when the path is wrong, the error names your file
instead of Python's search path.

**Start from [examples/local_website.py](examples/local_website.py).** It is a
complete plugin in under three hundred lines that copies the renders into a
staging directory under `/tmp`, builds a small HTML site there, and then sends
it to another directory, so the example runs and is tested without a network. A
clearly-marked pseudo-code FTP session stands where a real transport goes. The
test suite drives the real menu through it, twice, which is the point: an
example nobody runs rots into a lie.

### The interface

Both classes answer the same three questions — the same three a menu item
answers — and the uploader answers one more:

| | |
|---|---|
| `describe()` | One line for the menu row: what this act does on this machine. |
| `evaluate(workspace)` | May it run? `go()`, `satisfied(reason)` when it is already done, or `blocked(reason)` with one reason in the operator's words. Asked on **every menu draw**, so keep it cheap and local — no network, no subprocess. |
| `execute(workspace)` | Do it, and say what happened: `did(note)` or `stopped(note)`. |
| `is_complete(trip_ids)` — uploader only | Are **all** of these trips at the destination? `YES` / `NO` / `UNKNOWN` / `NA`. |

### Two rules about the order, if your destination has pages and media

**Pages first, media after.** A trip is publishable as soon as it is described:
its route, its distance, its places and its map all come from the sidecars item
2 writes, and only playback waits on the encode. Send the media first and the
destination shows nothing for the hours that upload takes, then everything at
once. Reversed, the pages are up in a minute and each drive starts playing as
its own media arrives behind it.

**Missing media is not a failure.** Item 5 is reachable before item 6 by design,
so a publish with nothing encoded yet is the ordinary first move. Complete, and
say what you did: pages sent, no media to send. None of this softens the erase
gate — a trip with no media at the destination is still `NO` from
`is_complete`, so Clean Workspace stays shut over footage whose only copy is
local.

Nothing is registered and nothing calls back. The exporter calls; you return.
The graph already decides when each act may run, so a readiness callback would
have no work to do.

`workspace` is a small frozen value object: `out_dir`, `import_dir`, `renders`
(name, size and path of each mp4), `metas`, `trip_ids`, `dropped_ids`,
`offline`, and `ui` — the exporter's own output, so your progress looks like
the rest of the tool.

### The one condition of trust

**Read the workspace. Never modify it.** No move, no rename, no delete, no
writing into the export tree: copy what you need somewhere of your own and work
there. This is not policed and will not be. It matters because the renders and
sidecars you are handed are the same files the exporter's erase gates then
reason about — a `mv` leaves items 4, 8 and 9 judging a workspace that shifted
under them.

### What the exporter will and will not believe

Your classes are **inside the trust boundary**. Whoever installed them owns what
they do, exactly as with any library — the exporter runs your code and believes
what it says. There is no sandbox and no output validation, because there is no
threat model in which those would help: you chose the classes.

Two things follow, and they are the whole design:

**"I could not find out" must be unwritable as "yes".** An uploader whose
destination is unreachable answers `Evidence.UNKNOWN`, and unreachable is not
permission. `Evidence.NA` is a different thing — the question does not *arise*
for your destination (an archive disk stores footage; it does not publish it) —
and it removes the erase gate rather than failing it. Getting those two the
wrong way round is how Clean Workspace erases footage on the strength of a
check that never ran.

**`is_complete` is all or nothing, and the list is ours.** Clean Workspace
erases the whole working area, never a chosen trip, so a per-trip answer would
be finer than anything acts on. The exporter names every trip of the import —
*including one that produced no render at all*, which is exactly the trip whose
footage exists nowhere else. You do not have it, so the honest answer is NO, and
its footage stays.

**The exporter still answers its own questions.** "Were these trips rendered on
this machine" never leaves home. An implementation that answers yes to
everything still cannot get an import that produced no renders erased, because
that gate is not delegated — not out of distrust, but because the tool already
knows the answer.

And when the erase does go ahead on your answer, the gate table, the banner
above the prompt and the recorded step all name your implementation. Not
a safeguard; attribution. If footage is gone and the answer was wrong, whose
answer it was should be readable off the tool rather than out of memory.

You do not need any of this. The local page is a real deliverable on its own.

---

## Tests

```bash
./run-tests.sh
```

Fixtures only — it never reads the card, the workspace or the export tree, so it
is safe to run mid-render. It covers the graph the menu is built from and the
predicates the destructive items obey:
what makes the working area expendable, what counts as evidence a copy of the
card survives, what the sweep keeps when it runs, that the ledger never moves
backwards, that the owner's own worked example holds against the live items,
and that `import-sd-card.sh` refuses to erase the card when its verify pass
cannot run or reports files still pending.

506 Python checks plus 7 shell ones, in about a dozen seconds.

`python3 tests/print_step_graph.py` prints the graph from the live item
objects, per strategy, with the start/end/destr columns and both neighbour
sets — and diffs the hand-written inbound column against the one derived from
the outbounds, reporting every difference rather than reconciling it.

---

## Requirements

- **ffmpeg** and **ffprobe** — every render, still and duration goes through them
- **Python 3.9+** with numpy, opencv, staticmap, Pillow — `INSTALLER.sh` handles these
- **rsync** — for the import; macOS ships `openrsync`, which works
- optional: **awscli**, configured, for Upload Website

---

## Docs

- **[config.txt](config.txt)** — every setting, documented where it lives
- **[docs/public-edition.md](docs/public-edition.md)** — notes on the
  configured/unconfigured split
- **[docs/sd-card-formatting/](docs/sd-card-formatting/FORMATTING_SD_HACK.md)** — patching the MBR when the card will not mount
- **[CLAUDE.md](CLAUDE.md)** — architecture notes

## 💚 Funding

- 🏅 https://github.com/sponsors/raoulsson
- 🪙 https://www.buymeacoffee.com/raoulsson

## Licence

MIT. See [LICENSE](LICENSE).

---

Designed by Raoul Marc Schmidiger. Implemented by Claude.
