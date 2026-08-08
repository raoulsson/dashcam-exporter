# Canonical Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a local, vendor-shaped import into a canonical workspace the rest of the tool can read without knowing which camera wrote it.

**Architecture:** Import stays what it always was — a verbatim `DCIM` copy, no adapter involved. Normalisation is a separate, local step: the adapter reads the copy and rewrites it into our own shape. Videos are **moved** (a rename within one filesystem, so 46 GB is not duplicated), images and logs are **copied and renamed**, GPS is **transformed** into our own track files.

**Tech Stack:** Python 3 stdlib. Tests are `unittest`.

## The rule

| On the card | In the canonical workspace | How |
|---|---|---|
| `.mp4` | `clips/<stamp>_<channel>.mp4` | moved (rename, no copy) |
| `.jpg` / `.jpeg` / `.png` | `images/<stamp>_<channel>.<ext>`, or its own name when unstamped | copied |
| `.txt` | `logs/<stamp>.txt`, or its own name when unstamped | copied |
| anything carrying GPS | `tracks/<stamp>.json` | transformed into our TrackPoints |
| everything else | left in `DCIM/` | untouched |

Plus `clips.json` at the workspace root, because a canonical filename cannot carry mode, `protected`, or the two durations — and VIOFO's locked flag exists ONLY as a directory name that normalisation discards.

**The one-time converter is not a separate tool.** Normalising a local import IS the conversion, and the existing `2026-08-08` import is exactly its input.

## Global Constraints

- Test runner: `./run-tests.sh`. Gate: no failures beyond the known `test_checkout` checkout-name one. Script at `scratchpad/gate.sh`.
- Tests must not read the SD card, the import workspace or the output tree, and must not invoke ffmpeg.
- **Never normalise a real import without explicit approval.** It renames files in place. Every run takes `--dry-run` by default in the CLI; writing requires `--apply`.
- One class per file. No emojis. Commit messages say WHY. Sign off `Co-Authored-By: Claude <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `src/dashcam_exporter/domain/model/dex_gps_file.py` | `DexGpsFile` — our track on disk, read and written |
| `src/dashcam_exporter/application/workflow/canonical_workspace.py` | `CanonicalWorkspace` — the layout's names and readers |
| `src/dashcam_exporter/application/workflow/normalizer.py` | `Normalizer` — turns a vendor import into that workspace |
| `tools/normalize_import.py` | CLI, dry-run by default |

---

### Task 1: DexGpsFile

**Files:**
- Create: `src/dashcam_exporter/domain/model/dex_gps_file.py`
- Test: `tests/domain/model/test_dex_gps_file.py`

**Interfaces:**
- Produces: `DexGpsFile.write(path, track)` and `DexGpsFile.read(path) -> Track`. JSON, one object with a `points` array of `[lat, lon, kmh, iso8601]`.

**Why JSON and not GPX:** the file is ours, read only by us. GPX would mean re-parsing XML to recover numbers we already had, and NMEA would mean re-encoding decimal degrees back into `ddmm.mmmm` — both are round trips through a camera's format for no reader's benefit.

- [ ] **Step 1: Write the failing test**

Create `tests/domain/model/test_dex_gps_file.py`:

```python
"""Our own track on disk: written once, read without a camera in sight."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import DexGpsFile, Track, TrackPoint

TRACK = Track(points=(
    TrackPoint(14.412755, 121.043745, 37.04, datetime(2026, 8, 6, 9, 5, 30)),
    TrackPoint(14.413000, 121.044000, 41.20, datetime(2026, 8, 6, 9, 5, 31)),
))


class DexGpsFileTest(unittest.TestCase):
    def test_a_track_survives_the_round_trip_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "20260806170529.json"
            DexGpsFile.write(path, TRACK)
            back = DexGpsFile.read(path)

        self.assertEqual(len(back.points), 2)
        self.assertEqual(back.points[0].at_utc, datetime(2026, 8, 6, 9, 5, 30))
        self.assertAlmostEqual(back.points[0].lat, 14.412755, places=6)
        self.assertAlmostEqual(back.points[1].kmh, 41.20, places=2)

    def test_the_file_is_readable_without_this_code(self):
        # It is our format, but it should not need our parser to inspect.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "t.json"
            DexGpsFile.write(path, TRACK)
            raw = json.loads(path.read_text())

        self.assertEqual(raw["points"][0][3], "2026-08-06T09:05:30")
        self.assertEqual(len(raw["points"]), 2)

    def test_an_empty_track_writes_and_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "t.json"
            DexGpsFile.write(path, Track(points=()))

            self.assertTrue(DexGpsFile.read(path).is_empty)

    def test_a_missing_or_unreadable_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "nope.json"
            broken = Path(temporary) / "broken.json"
            broken.write_text("{not json")

            self.assertTrue(DexGpsFile.read(missing).is_empty)
            self.assertTrue(DexGpsFile.read(broken).is_empty)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_dex_gps_file -v`

- [ ] **Step 3: Write the implementation**

Create `src/dashcam_exporter/domain/model/dex_gps_file.py`:

```python
import json
from datetime import datetime
from pathlib import Path

from .track import Track, TrackPoint

VERSION = 1


class DexGpsFile:
    """A Track on disk, in the one GPS format this tool owns.

    JSON rather than GPX or NMEA because nothing but this tool ever reads it.
    Writing GPX would mean parsing XML back into numbers we already had, and
    writing NMEA would mean re-encoding decimal degrees into the ddmm.mmmm a
    camera happens to use -- a round trip through somebody else's format for
    no reader's benefit.

    A point is [lat, lon, kmh, iso8601] rather than an object per fix: a
    drive is tens of thousands of fixes, and four keys repeated that many
    times is most of the file.
    """

    @staticmethod
    def write(path: Path, track: Track) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "points": [[p.lat, p.lon, p.kmh, p.at_utc.isoformat()]
                       for p in track.points],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def read(path: Path) -> Track:
        """The track, or an empty one if the file is missing or unreadable.

        Empty rather than raising: a drive with no route is an ordinary
        outcome this tool already renders, and a cache file damaged by a
        power cut should cost that drive its route, not the whole run.
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Track(points=())
        points = []
        for row in raw.get("points", []):
            try:
                points.append(TrackPoint(float(row[0]), float(row[1]),
                                         float(row[2]),
                                         datetime.fromisoformat(row[3])))
            except (TypeError, ValueError, IndexError):
                continue
        return Track(points=tuple(points))
```

- [ ] **Step 4: Export it from the domain package**

Add `from .model.dex_gps_file import DexGpsFile` to `src/dashcam_exporter/domain/__init__.py` and to `__all__`.

- [ ] **Step 5: Run the tests, then the gate, then commit**

```bash
git add src/dashcam_exporter/domain tests/domain/model/test_dex_gps_file.py
git commit -m "$(cat <<'EOF'
Give the track a file format this tool owns

Every GPS format the adapters read belongs to a camera: NMEA in mislabeled
tars, a bracketed epoch in a sidecar, a binary blob whose offset moves with
firmware. Writing any of those back out would mean a round trip through
somebody else's format for the benefit of no reader -- the only thing that
ever reads this is us.

So it is JSON, and a point is a four-element array rather than an object: a
drive is tens of thousands of fixes, and four repeated keys would be most
of the file.

A missing or damaged file reads as an empty track rather than raising. A
drive with no route is something this tool already renders; a power cut
mid-write should cost that drive its route, not the run.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: CanonicalWorkspace

**Files:**
- Create: `src/dashcam_exporter/application/workflow/canonical_workspace.py`
- Test: `tests/application/workflow/test_canonical_workspace.py`

**Interfaces:**
- Produces: `CanonicalWorkspace(root)` with `clips_dir`, `images_dir`, `logs_dir`, `tracks_dir`, `manifest_path`; `video_name(stamp, channel) -> str`; `is_normalized -> bool`; `clips() -> list[Clip]`; `track_for(clip) -> Track | None`; `write_manifest(clips)`.

**Naming:** `<stamp>_<channel>.<ext>`, with `channel` the lowercase `Channel` value. `Channel`'s values were chosen as lowercase strings in plan 1 for exactly this, so no translation table exists anywhere.

- [ ] **Step 1: Write the failing test**

Create `tests/application/workflow/test_canonical_workspace.py`:

```python
"""The shape the rest of the tool reads, with no camera in it."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode
from dashcam_exporter.application.workflow.canonical_workspace import (
    CanonicalWorkspace)


def a_clip(stamp="20260806170529", mode=ClipMode.NORMAL, protected=False):
    return Clip(timestamp=stamp, epoch_utc=0, playback_seconds=60,
                wall_seconds=60,
                videos={Channel.FRONT: Path("clips/%s_front.mp4" % stamp),
                        Channel.REAR: Path("clips/%s_rear.mp4" % stamp)},
                mode=mode, source_mode="N", protected=protected)


class CanonicalWorkspaceTest(unittest.TestCase):
    def test_a_video_is_named_by_its_stamp_and_channel(self):
        workspace = CanonicalWorkspace(Path("/nowhere"))

        self.assertEqual(workspace.video_name("20260806170529", Channel.FRONT),
                         "20260806170529_front.mp4")
        self.assertEqual(workspace.video_name("20260806170529",
                                              Channel.INTERIOR),
                         "20260806170529_interior.mp4")

    def test_an_untouched_import_is_not_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "DCIM/200video/front").mkdir(parents=True)

            self.assertFalse(CanonicalWorkspace(root).is_normalized)

    def test_the_manifest_carries_what_a_filename_cannot(self):
        # mode, protected and the two durations have nowhere to live in a
        # canonical name -- and VIOFO's locked flag exists only as a
        # directory this normalisation throws away.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = CanonicalWorkspace(root)
            workspace.write_manifest([a_clip(mode=ClipMode.PARKING,
                                             protected=True)])

            self.assertTrue(workspace.is_normalized)
            back = workspace.clips()

        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].mode, ClipMode.PARKING)
        self.assertTrue(back[0].protected)
        self.assertEqual(back[0].source_mode, "N")
        self.assertEqual(back[0].wall_seconds, 60)

    def test_a_timelapse_clip_keeps_both_of_its_durations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = CanonicalWorkspace(root)
            clip = Clip(timestamp="20260806170529", epoch_utc=0,
                        playback_seconds=120, wall_seconds=600,
                        videos={Channel.FRONT: Path("clips/x_front.mp4")},
                        mode=ClipMode.TIMELAPSE, source_mode="t")
            workspace.write_manifest([clip])

            back = workspace.clips()[0]

        self.assertEqual(back.playback_seconds, 120)
        self.assertEqual(back.wall_seconds, 600)

    def test_video_paths_come_back_absolute_under_the_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = CanonicalWorkspace(root)
            workspace.write_manifest([a_clip()])

            front = workspace.clips()[0].front

        self.assertTrue(front.is_absolute())
        self.assertEqual(front.name, "20260806170529_front.mp4")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails**

- [ ] **Step 3: Write the implementation**

Create `src/dashcam_exporter/application/workflow/canonical_workspace.py`:

```python
import json
from pathlib import Path

from dashcam_exporter.domain import (Channel, Clip, ClipMode, DexGpsFile,
                                     Track)

MANIFEST = "clips.json"
CLIPS = "clips"
IMAGES = "images"
LOGS = "logs"
TRACKS = "tracks"
VERSION = 1


class CanonicalWorkspace:
    """A normalised import: our names, our track format, our manifest.

    The videos are not copied into this shape, they are MOVED into it -- a
    rename inside one filesystem. An import is 46 GB on the machine this was
    built against, and a second copy of that to gain a better filename is
    not a trade worth making.

    The manifest exists because a filename cannot carry everything a clip
    is. Mode, the protected flag and the two durations have nowhere to live
    in <stamp>_<channel>.mp4, and VIOFO records "this clip is locked" ONLY
    by the directory it sits in -- a fact that would be destroyed by the
    very rename that makes the workspace canonical.
    """

    def __init__(self, root) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def clips_dir(self) -> Path:
        return self._root / CLIPS

    @property
    def images_dir(self) -> Path:
        return self._root / IMAGES

    @property
    def logs_dir(self) -> Path:
        return self._root / LOGS

    @property
    def tracks_dir(self) -> Path:
        return self._root / TRACKS

    @property
    def manifest_path(self) -> Path:
        return self._root / MANIFEST

    @property
    def is_normalized(self) -> bool:
        return self.manifest_path.is_file()

    @staticmethod
    def video_name(stamp: str, channel: Channel, suffix: str = ".mp4") -> str:
        return "%s_%s%s" % (stamp, channel.value, suffix)

    def track_name(self, stamp: str) -> str:
        return "%s.json" % stamp

    def write_manifest(self, clips) -> None:
        rows = []
        for clip in clips:
            rows.append({
                "stamp": clip.timestamp,
                "epoch_utc": clip.epoch_utc,
                "playback_seconds": clip.playback_seconds,
                "wall_seconds": clip.wall_seconds,
                "mode": clip.mode.value,
                "source_mode": clip.source_mode,
                "protected": clip.protected,
                "videos": {channel.value: Path(path).name
                           for channel, path in clip.videos.items()},
            })
        self._root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps({"version": VERSION, "clips": rows}, indent=1),
            encoding="utf-8")

    def clips(self) -> list[Clip]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [self._to_clip(row) for row in raw.get("clips", [])]

    def track_for(self, clip: Clip) -> Track | None:
        track = DexGpsFile.read(self.tracks_dir / self.track_name(clip.timestamp))
        return None if track.is_empty else track

    def _to_clip(self, row) -> Clip:
        videos = {Channel(name): self.clips_dir / filename
                  for name, filename in row.get("videos", {}).items()}
        return Clip(timestamp=row["stamp"],
                    epoch_utc=int(row.get("epoch_utc", 0)),
                    playback_seconds=float(row.get("playback_seconds", 0)),
                    wall_seconds=float(row.get("wall_seconds", 0)),
                    videos=videos,
                    mode=ClipMode(row.get("mode", "normal")),
                    source_mode=row.get("source_mode", ""),
                    protected=bool(row.get("protected", False)))
```

- [ ] **Step 4: Run the tests, the gate, and commit**

```bash
git add src/dashcam_exporter/application/workflow/canonical_workspace.py tests/application/workflow/test_canonical_workspace.py
git commit -m "$(cat <<'EOF'
Name the shape the tool reads once the camera is behind us

Videos are MOVED into this shape rather than copied. The import this was
built against is 46 GB, and duplicating that to gain a better filename is
not a trade worth making.

The manifest earns its place by holding what a filename cannot. Mode, the
protected flag and the two durations have nowhere to live in
<stamp>_<channel>.mp4 -- and VIOFO records "locked" only by which directory
a clip sits in, so the very rename that makes the workspace canonical is
what would destroy it.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The Normalizer

**Files:**
- Create: `src/dashcam_exporter/application/workflow/normalizer.py`
- Test: `tests/application/workflow/test_normalizer.py`

**Interfaces:**
- Produces: `Normalizer(source_root, workspace=None, logger=None)` with `plan() -> NormalizationPlan` and `apply() -> NormalizationPlan`. The plan is a frozen dataclass counting moves, copies, tracks and skips, so a dry run and a real run describe themselves the same way.

**Rules, exactly:** `.mp4` moved; `.jpg`/`.jpeg`/`.png` copied into `images/`; `.txt` copied into `logs/`; every clip's track transformed into `tracks/<stamp>.json`; everything else left where it is.

**Idempotence matters more than it looks:** the operator will run this twice. A second run must be a no-op, not a second set of moves.

- [ ] **Step 1: Write the failing test**

Create `tests/application/workflow/test_normalizer.py` covering: a DDPAI tree normalises to canonical names; the videos are gone from their old location and present in `clips/`; a `.txt` is copied and the original still exists; a track file is written and reads back non-empty; a second `apply()` reports zero moves; and `plan()` writes nothing at all.

- [ ] **Step 2 onward:** implement, run, gate, commit. The implementation walks `layout.clips()` for videos and `layout.track_for()` for GPS, then sweeps the source tree for images and logs.

---

### Task 4: The CLI, and a real dry run

**Files:**
- Create: `tools/normalize_import.py`

Dry run by default; `--apply` required to write. Run it against the real `2026-08-08` import in dry-run mode and show the plan before anything is applied.

## Scope note

Making the renderer and pipeline READ the canonical workspace is deliberately not in this plan. Normalisation must exist and be trusted first — on real footage, with a dry run inspected — before anything depends on its output.
