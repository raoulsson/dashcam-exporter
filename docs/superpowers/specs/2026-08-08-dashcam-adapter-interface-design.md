# Camera adapter interface — design

Date: 2026-08-08
Branch: `adapter-interface` (clone at `~/dev/dashcam-exporter-adapter-change`)
Status: design approved, VIOFO/Thinkware findings pending

## Why

`ExporterAdapter` already exists, but it is DDPAI's shape wearing an ABC's
clothes: `discover_clips(front_directory, rear_directory)` assumes two
directories, and `prepare_gps(tar_directory, cache_directory)` assumes GPS
lives in archives in a sibling folder. Neither assumption survives contact
with a second camera.

Worse, the adapter is not the only place that knows the camera. `pipeline.py`
hardcodes `DCIM/200video/front` in eight places, `VIDEO_DIR, GPS_DIR =
"200video", "203gps"` at line 2483, `.git`-archive recognition at 6785, and a
14-digit stamp regex used fifteen times. `renderer.py:766` holds a second copy
of the `.git`-tar GPX extraction that the adapter already implements. A
BlackVue card would be rejected as "not a card" long before any adapter saw
it.

One implementation always hides holes. The goal of this work is a contract a
third party can implement for an arbitrary camera, proven by writing two more
adapters against it.

## Principle

The interface is derived from what the pipeline needs, never from what a
camera offers. Nothing crosses the adapter boundary except our own types. If a
camera stores its track as an s-expression, the adapter parses the
s-expression and returns `Track`. Downstream code contains no camera
branching, because nothing camera-shaped ever reaches it.

## Two zones

**Vendor-facing zone** — card detection, clip discovery, track parsing, and
the import copy. The adapter runs here, once per card.

**Workspace zone** — everything after import. It reads a canonical layout we
define, with our stamp grammar and typed artifacts. No adapter, no camera, no
branching.

The importer is the transformation between them. This is what keeps the change
to `pipeline.py` shallow: because the workspace is named with our canonical
`YYYYMMDDHHMMSS` stamp, `STAMP_RE` and its fifteen call sites remain correct
by construction and need no edit. A hardcoded path constant in the workspace
zone is legitimate — we wrote those directories.

The eight hardcoded card-path sites split as follows:

| Site | Function | Zone |
|---|---|---|
| 1085 | `clip_count` | workspace |
| 1909 | new-vs-already clip counter | vendor |
| 2401 | card guard | vendor |
| 3870 | card guard | vendor |
| 6453 | `card_stamps` | vendor |
| 6563 | still-in-workspace check | workspace |
| 6949 | per-clip front path list | workspace |

Vendor-zone sites ask the registry for an adapter. Workspace-zone sites read
the canonical layout constant.

## Model

```python
@dataclass(frozen=True, slots=True)
class TrackPoint:
    lat: float
    lon: float
    kmh: float
    at_utc: datetime

@dataclass(frozen=True, slots=True)
class Track:
    points: tuple[TrackPoint, ...]

class ClipMode(Enum):
    NORMAL, EVENT, PARKING, MANUAL, TIMELAPSE, OTHER

class Channel(Enum):
    FRONT, REAR, INTERIOR, TELEPHOTO

@dataclass(frozen=True, slots=True)
class Clip:
    timestamp: str                    # canonical YYYYMMDDHHMMSS
    epoch_utc: int
    playback_seconds: float           # what ffmpeg will play
    wall_seconds: float               # real-world span the footage covers
    videos: Mapping[Channel, Path]
    mode: ClipMode
    source_mode: str                  # the vendor's own token, verbatim
    protected: bool                   # locked/event-protected on the card
```

`front`, `rear` and `duration` remain as compatibility properties so the
migration is incremental; `Clip` already carries `dt` and `end` aliases for
the same reason.

Three changes, each forced by a specific camera rather than anticipated:

**Channel map instead of front/rear.** The VIOFO A139 Pro is three-channel and
the A329 adds a telephoto, so two fields cannot hold a clip. The manual's own
example shows three channels sharing one timestamp and consuming three
consecutive sequence numbers.

**Two durations.** Thinkware's timelapse modes record ten minutes of real time
at 2 fps and store a two-minute file. `ended_at = started_at + duration` is
wall-clock semantics and drives trip grouping; ffmpeg needs playback length.
For every clip handled so far these were the same number, which is precisely
why one implementation hid it.

**Mode enum plus raw token plus a protected flag.** BlackVue defines sixteen
mode letters and collapsing them to six discards evidence a future grouping
rule may want, so the vendor token is retained alongside the decision the code
acts on. Protection is orthogonal to mode: VIOFO encodes it *only* by placing
the file in `DCIM/Movie/RO/`, with no filename marker, so it is unrecoverable
once files are copied off the card. That is an argument for transforming at
import rather than copying verbatim — today's `import-sd-card.sh` would
silently destroy it.

## Interface

```python
class ExporterAdapter(ABC):
    name: str
    def detect(self, card_root: Path) -> bool
    def layout_for(self, card_root: Path) -> CardLayout

class CardLayout(ABC):
    def clips(self) -> list[Clip]                    # time-ordered, rear paired or None
    def stamp_of(self, path: Path) -> str | None     # canonical YYYYMMDDHHMMSS
    def track_for(self, clip: Clip) -> Track | None  # parsed, not a file path
    def import_roots(self) -> tuple[Path, ...]       # what the importer copies
    def is_track_artifact(self, path: Path) -> bool  # sweep safety
```

Gone from the old contract: `front_directory`/`rear_directory`,
`tar_directory`, and `prepare_gps`'s `(archives, extracted)` return, which
described DDPAI's extraction mechanics rather than a result.

GPS extraction is a separate axis from folder layout: two brands can share a
GPS format while sharing nothing else, and one brand can change GPS format
across firmware while keeping its folders. `CardLayout` therefore *holds* a
GPS source rather than *being* one, so a shared reader (for instance the
Novatek freeGPS blob common to many chipset-sibling cameras) is written once.

## Selection

An adapter registry resolves a card: each adapter's `detect(card_root)`
inspects the tree, the registry picks the single match, errors loudly on
ambiguity, and a config key or CLI flag forces a specific adapter. Detection
is itself an interface test — a camera whose layout cannot be recognised is
one that has not really been modelled.

## Simulated cards

A generator builds card trees from published layout grammars, with 1–2 second
ffmpeg-encoded MP4s and genuine GPS payloads (sidecar files, or GPS actually
muxed into the container for embedded-GPS cameras), so the whole pipeline runs
end to end including render and track overlay.

The DDPAI generated card is calibrated against real bytes: a real card's
directory skeleton and a real 235-clip import both exist on this machine. If
the generator can reproduce a card as irregular as DDPAI's faithfully, the
cards it generates from published grammars for other brands are worth
something.

## Evidence

### DDPAI — verified from real bytes

Card skeleton (`/Volumes/NO NAME`, files swept, structure intact):

```
DCIM/200video/{front,rear}
DCIM/201photo/{front,rear,tmp}
DCIM/202thumb/{front,rear,tmp}
DCIM/203gps/{tar,tmp}
DCIM/207log/tmp
```

Filenames (`~/dashcam-data/import/2026-08-08`, 235 front, 234 rear, 28
archives):

```
200video/front  20260806170529_0060.mp4        stamp + duration seconds
200video/rear   20260806170529_0060_A.mp4      same stamp, _A suffix
203gps/tar      20260806170529_0540.git        own start + span, tar mislabeled .git
```

Two facts only real bytes gave us:

- 235 front against 234 rear. One clip has no partner. Asymmetric pairing is
  the normal case, not an edge case.
- The comment at pipeline.py:2492 cites `20260712191931_0120_T.git` with a
  trailing `_T`. No file on this card carries it. The comment teaches a
  grammar the camera no longer writes; recognition is by suffix so nothing is
  broken, but the example should be corrected.

### BlackVue — from manuals and open-source parsers, not from a card

Layout: everything in `/BlackVue/Record/`, named
`YYYYMMDD_HHMMSS_[type][direction][flag].mp4`, e.g. `20141017_163635_NF.mp4`
and `..._NR.mp4`. Sixteen type letters (N normal, E event, P parking motion,
M manual, I parking impact, and others); direction F/R/I/O.

What it breaks in the current interface:

1. Front and rear share one directory, so a two-directory signature has
   nothing to receive.
2. Duration is not in the filename. Segments are fixed at one minute per the
   manual, and rear files are trimmed to about 59 s to absorb the rear
   camera's start delay. The layout must supply duration by other means.
3. Mode is part of clip identity and `Clip` had no field for it.
4. GPS has two regimes. Legacy models write a per-clip `.gps` sidecar named
   `<stamp>_<type>.gps` — mode letter but no direction letter, so one per
   clip-pair. The DR900X writes no sidecars at all: GPS lives in the MP4 under
   FourCC `gps ` (trailing space, payload at offset 4, NUL-terminated). Its
   payload is a bracketed epoch followed by an NMEA sentence,
   `[1611723852888]$GNGGA,...`.

Point 4 also exposes a fifth leak: `tracking.py:parse_gpx_track` filters on
`line.startswith("$GPRMC")`, which matches neither the bracketed prefix nor
the `GN` talker ID. Under this design that is moot — the adapter returns
`Track`, so no camera's bytes reach the parser.

Unverified and deliberately not asserted: whether DR750X/DR970 write sidecars;
`.3gf` axis order and G-scale.

### VIOFO — from manuals and open-source extractors, not from a card

Layout: `DCIM/Movie/` for loop recording, `DCIM/Movie/RO/` for locked/event,
`DCIM/Movie/Parking/` for parking, `DCIM/Photo/` for snapshots. Older A129
Plus has no `Parking` subfolder; A119 V3 puts `RO` as a sibling of `Movie`;
some A119-era units use a `CARDV/` root instead of `DCIM/`. A parser should
accept both roots.

Filenames: `YYYY_MMDD_HHMMSS_<seq>[P|E]?[F|R|I|T].MP4`, sequence width
varying 3–8 digits by firmware. `P` parking, `E` impact/event, absent normal.
A119 V3 uses a different grammar entirely, `YYYYMMDDHHMMSS_NNNNNN.MP4`, with
no channel letter. Same brand, incompatible grammars.

Front and rear pairing is **fuzzy**: timestamps skew by a second or more and
sequence numbers drift independently when one camera is unplugged. The
commercial reference player searches ±6 seconds and ±4 file numbers. Never
require equality; never reconstruct the sibling filename from the stem.

GPS: no sidecar. A non-standard `gps ` table inside `moov` points at `free`
boxes beginning with the ASCII magic `GPS `, holding little-endian binary
records with coordinates in `DDDmm.mmmm` hybrid degrees-minutes. **The payload
offset within the box varies by model and firmware** — extractors discriminate
on a uint32 at offset 12 (`0x58` implies unpack at `0x30`; `0x3F0` and `0x2C`
imply `0x10`). This is the strongest argument for holding a GPS source rather
than being one: the same reader serves many chipset-sibling brands, with
firmware quirks isolated in one place.

Duration is a menu setting (1/2/3/5/10 minutes), invisible to any parser.

### Thinkware — from manuals and ExifTool's parser, not from a card

Layout: no `DCIM` at all. Mode folders sit at the card root — `cont_rec`,
`evt_rec`, `manual_rec`, `motion_timelapse_rec`, `parking_rec`. The F770
differs again: `motion_rec` and no `parking_rec`. A hidden `.TWSYS` folder
holds pre-allocated `.TMP` files and must be skipped when scanning.

Filenames: `REC_YYYY_MM_DD_HH_MM_SS_<CH>.MP4` with underscore-separated date
parts, channel `F`/`R`. **Mode appears nowhere in the name** — every file is
`REC_...` and the containing folder is the only encoding. This is the
structural opposite of VIOFO, where the marker is in the name.

GPS: NMEA RMC inside a timed-text track at roughly 1 Hz, each sample carrying
a G-sensor prefix, an RMC sentence and a `CAR,...` telemetry suffix. ExifTool
has an explicit Thinkware branch with a captured sample.

Duration is fixed per mode, not per camera: `cont_rec` 1 minute, `evt_rec` 20
seconds, `manual_rec` 1 minute, and the timelapse modes record ten minutes of
real time at 2 fps compressed into a two-minute file.

### What the third and fourth cameras broke

The five-method contract survived unchanged. The model did not, in two places:
the channel map (VIOFO three- and four-channel models) and the split between
playback and wall-clock duration (Thinkware timelapse). Both are recorded
above. This is the result the exercise was for — one implementation had hidden
both.

Unverified and deliberately not asserted: the VIOFO A139 Pro manual examples
were decoded from subset-font glyph IDs with no `/ToUnicode` map, so the exact
digits are inference, though they independently reproduce the manual's own
prose; which firmware ships the `CARDV/` root; the meaning of Thinkware's
`_GS` suffix; whether Thinkware's timed-text handler is literally `sbtl`.

### There is no dashcam standard

DCF (JEITA) explains `DCIM/` and numbered subdirectories, and nothing else —
it stops where this problem starts. Below that, layout is per-vendor and
changes between firmware generations, as BlackVue's two GPS regimes show. What
convergence exists comes from shared SoCs (Novatek, Ambarella) shipping
reference firmware, not from any agreement between vendors. Adapters are
therefore keyed to a layout, not to a company.

## Migration

Existing import folders are vendor-shaped, because `import-sd-card.sh` copies
`DCIM` verbatim. A one-time converter walks such a folder, runs it through the
DDPAI adapter, and writes the canonical workspace beside it. Existing imports
keep their value, and the converter doubles as an end-to-end test of the
adapter against 235 real clips — the best validation available without a
populated card.

The `.gpx_cache` becomes ours rather than the camera's: today it holds GPX
members extracted verbatim from DDPAI tars, and under this design the pipeline
serialises `Track` objects in a format we define. Existing caches are stale
and rebuild on first run.

`prepare_gps` was the progress-reporting hook — the UI prints archives-read
and members-extracted. `track_for(clip)` gives per-clip granularity, a better
signal, but the screen printing those two numbers needs rewording.

## Duration source

Measured with ffprobe, with a layout free to shortcut when it genuinely knows.
DDPAI is the only camera of the four that publishes duration in the filename.
VIOFO's is a menu setting no parser can see, Thinkware's varies per mode, and
BlackVue's is a fixed minute with rear files trimmed to about 59 seconds.
Wall-clock span equals playback length except for timelapse modes, where the
layout supplies the ratio it knows from the mode.

## Open items

- Whether `tracking.parse_gpx_track` also serves the per-trip `.gpx` sidecars
  we write as output. The output side is our own format and stays unchanged;
  this needs checking rather than assuming.
- Which adapters ship in the first pass. BlackVue and VIOFO exercise the
  contract hardest (single-directory pairing, fuzzy pairing, embedded GPS,
  multi-channel); Thinkware is documented here because it broke the duration
  model, but implementing it is optional.
- Detection ambiguity rules once more than one adapter is registered — VIOFO
  and DDPAI both live under `DCIM/`, so detection must inspect subdirectory
  names rather than the presence of `DCIM` alone.
