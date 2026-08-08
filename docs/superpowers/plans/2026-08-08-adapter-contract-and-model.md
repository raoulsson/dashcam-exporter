# Adapter Contract and Canonical Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the DDPAI-shaped `ExporterAdapter` with a camera-agnostic contract and a canonical domain model, and reimplement the DDPAI adapter on top of it without breaking any existing caller.

**Architecture:** Nothing crosses the adapter boundary except our own types. An `ExporterAdapter` detects a card and produces a `CardLayout`; the layout answers every question the pipeline has about that card, including returning parsed `Track` objects rather than file paths. The DDPAI adapter becomes the first implementation, and the existing `DdpaiDataAdapter` survives as a thin shim so `renderer.py` keeps working until plan 3 rewires it.

**Tech Stack:** Python 3, stdlib only (`abc`, `dataclasses`, `enum`, `tarfile`, `re`, `datetime`, `subprocess`). Tests are `unittest`. No new dependencies.

## Global Constraints

- Test runner: `./run-tests.sh`, which runs `python3 -m unittest discover -s tests -q` with `PYTHONPATH=src`. There is no pytest in this repo — write `unittest.TestCase` classes.
- Tests must be fixture-only. `run-tests.sh` states verbatim that nothing in the suite reads the SD card, the import workspace or the output tree, so it is safe to run mid-render. Never read `/Volumes/...` or `~/dashcam-data/...` from a test.
- Every new test package directory needs an `__init__.py`, or `unittest discover` will skip it. Follow `tests/splice/` which already does this.
- Source lives under `src/dashcam_exporter/`, tests mirror it under `tests/`.
- One class per file, named for the class.
- No emojis anywhere — code, comments, commit messages, docstrings.
- Commit messages say WHY in prose, not a diff summary. Sign off with:
  `Co-Authored-By: Claude <noreply@anthropic.com>`
- Work on branch `adapter-interface` in `~/dev/dashcam-exporter-adapter-change`. Do not touch `~/dev/dashcam-exporter` — another process is editing it.

## File Structure

| File | Responsibility |
|---|---|
| `src/dashcam_exporter/domain/model/track.py` | `TrackPoint` and `Track` value objects |
| `src/dashcam_exporter/domain/model/channel.py` | `Channel` enum |
| `src/dashcam_exporter/domain/model/clip_mode.py` | `ClipMode` enum |
| `src/dashcam_exporter/domain/model/clip.py` | `Clip`, reshaped (modify) |
| `src/dashcam_exporter/infrastructure/adapters/card_layout.py` | `CardLayout` ABC |
| `src/dashcam_exporter/infrastructure/adapters/exporter_adapter.py` | `ExporterAdapter` ABC, rewritten (modify) |
| `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py` | `AdapterRegistry` — detection and override |
| `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_adapter.py` | DDPAI `ExporterAdapter` |
| `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_card_layout.py` | DDPAI `CardLayout` |
| `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_track_source.py` | `.git` tar reading and NMEA parsing into `Track` |
| `src/dashcam_exporter/infrastructure/adapters/ddpai_data_adapter.py` | Shim keeping the old two-method API alive (modify) |
| `tools/calibrate_ddpai.py` | Standalone script comparing the adapter against a real import. Not part of the suite. |

---

### Task 1: Track value objects

**Files:**
- Create: `src/dashcam_exporter/domain/model/track.py`
- Test: `tests/domain/model/test_track.py`
- Create: `tests/domain/__init__.py`, `tests/domain/model/__init__.py` (empty files)

**Interfaces:**
- Consumes: nothing.
- Produces: `TrackPoint(lat: float, lon: float, kmh: float, at_utc: datetime)` and `Track(points: tuple[TrackPoint, ...])` with `Track.is_empty -> bool`, `Track.started_at -> datetime | None`, `Track.ended_at -> datetime | None`. Both frozen dataclasses with `slots=True`.

- [ ] **Step 1: Create the empty test package files**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p tests/domain/model
touch tests/domain/__init__.py tests/domain/model/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/domain/model/test_track.py`:

```python
"""The track a camera recorded, once it has become one of our own types."""

import unittest
from datetime import datetime

from dashcam_exporter.domain import Track, TrackPoint


class TrackTest(unittest.TestCase):
    def test_empty_track_has_no_span(self):
        track = Track(points=())

        self.assertTrue(track.is_empty)
        self.assertIsNone(track.started_at)
        self.assertIsNone(track.ended_at)

    def test_span_comes_from_first_and_last_point(self):
        first = TrackPoint(47.1, 8.2, 31.0, datetime(2026, 8, 6, 17, 5, 29))
        last = TrackPoint(47.2, 8.3, 44.0, datetime(2026, 8, 6, 17, 6, 29))

        track = Track(points=(first, last))

        self.assertFalse(track.is_empty)
        self.assertEqual(track.started_at, datetime(2026, 8, 6, 17, 5, 29))
        self.assertEqual(track.ended_at, datetime(2026, 8, 6, 17, 6, 29))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_track -v`
Expected: FAIL with `ImportError: cannot import name 'Track'`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/domain/model/track.py`:

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One GPS fix, in our units: decimal degrees, km/h, UTC.

    Cameras disagree about all three -- NMEA ddmm.mmmm, knots, local time,
    epoch milliseconds -- and every one of those disagreements is an adapter's
    problem, settled before a point reaches here.
    """

    lat: float
    lon: float
    kmh: float
    at_utc: datetime


@dataclass(frozen=True, slots=True)
class Track:
    """The fixes belonging to one clip, in ascending time order."""

    points: tuple[TrackPoint, ...]

    @property
    def is_empty(self) -> bool:
        return not self.points

    @property
    def started_at(self) -> datetime | None:
        return self.points[0].at_utc if self.points else None

    @property
    def ended_at(self) -> datetime | None:
        return self.points[-1].at_utc if self.points else None
```

- [ ] **Step 5: Export from the domain package**

Modify `src/dashcam_exporter/domain/__init__.py` to read:

```python
from .model.clip import Clip
from .model.render_options import Cut, RenderOptions
from .model.track import Track, TrackPoint

__all__ = ["Clip", "Cut", "RenderOptions", "Track", "TrackPoint"]
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_track -v`
Expected: PASS, 2 tests.

- [ ] **Step 7: Run the whole suite**

Run: `./run-tests.sh`
Expected: `all green`.

- [ ] **Step 8: Commit**

```bash
git add src/dashcam_exporter/domain/model/track.py src/dashcam_exporter/domain/__init__.py tests/domain
git commit -m "$(cat <<'EOF'
Give the track a type so no camera's bytes travel past the adapter

Today a track is a file path and every consumer parses it, which is why
tracking.py filters on "$GPRMC" and would silently read nothing from a
BlackVue camera writing "[epoch]$GNGGA" or a Thinkware camera writing RMC
into a timed-text track. A parsed value object moves that decision to the
one class that knows which camera it is talking to.

Units are fixed here rather than negotiated: decimal degrees, km/h, UTC.
Cameras disagree about all three and the disagreement is not interesting
to anything downstream.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Channel and ClipMode enums

**Files:**
- Create: `src/dashcam_exporter/domain/model/channel.py`
- Create: `src/dashcam_exporter/domain/model/clip_mode.py`
- Test: `tests/domain/model/test_clip_mode.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Channel.FRONT | REAR | INTERIOR | TELEPHOTO` and `ClipMode.NORMAL | EVENT | PARKING | MANUAL | TIMELAPSE | OTHER`. Both plain `Enum` with lowercase string values (`Channel.FRONT.value == "front"`), because the values are written into canonical filenames in plan 3.

- [ ] **Step 1: Write the failing test**

Create `tests/domain/model/test_clip_mode.py`:

```python
"""The two closed vocabularies a clip is classified by."""

import unittest

from dashcam_exporter.domain import Channel, ClipMode


class VocabularyTest(unittest.TestCase):
    def test_channel_values_are_the_names_used_in_canonical_filenames(self):
        self.assertEqual(Channel.FRONT.value, "front")
        self.assertEqual(Channel.REAR.value, "rear")
        self.assertEqual(Channel.INTERIOR.value, "interior")
        self.assertEqual(Channel.TELEPHOTO.value, "telephoto")

    def test_modes_cover_the_four_cameras_researched(self):
        self.assertEqual(
            {mode.value for mode in ClipMode},
            {"normal", "event", "parking", "manual", "timelapse", "other"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_clip_mode -v`
Expected: FAIL with `ImportError: cannot import name 'Channel'`.

- [ ] **Step 3: Write the implementations**

Create `src/dashcam_exporter/domain/model/channel.py`:

```python
from enum import Enum


class Channel(Enum):
    """Which camera a video file came from.

    Front and rear were enough until the VIOFO A139 Pro, which is three
    channels sharing one timestamp, and the A329, which adds a telephoto.
    """

    FRONT = "front"
    REAR = "rear"
    INTERIOR = "interior"
    TELEPHOTO = "telephoto"
```

Create `src/dashcam_exporter/domain/model/clip_mode.py`:

```python
from enum import Enum


class ClipMode(Enum):
    """Why the camera was recording, collapsed to what this tool decides on.

    BlackVue defines sixteen mode letters, VIOFO puts its marker in the
    filename, Thinkware puts it in the folder name and nowhere else. Clip
    keeps the vendor's own token alongside this, so collapsing here does not
    destroy evidence a later grouping rule may want.
    """

    NORMAL = "normal"
    EVENT = "event"
    PARKING = "parking"
    MANUAL = "manual"
    TIMELAPSE = "timelapse"
    OTHER = "other"
```

- [ ] **Step 4: Export from the domain package**

Modify `src/dashcam_exporter/domain/__init__.py` to read:

```python
from .model.channel import Channel
from .model.clip import Clip
from .model.clip_mode import ClipMode
from .model.render_options import Cut, RenderOptions
from .model.track import Track, TrackPoint

__all__ = ["Channel", "Clip", "ClipMode", "Cut", "RenderOptions", "Track",
           "TrackPoint"]
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_clip_mode -v`
Expected: PASS, 2 tests.

- [ ] **Step 6: Commit**

```bash
git add src/dashcam_exporter/domain/model/channel.py src/dashcam_exporter/domain/model/clip_mode.py src/dashcam_exporter/domain/__init__.py tests/domain/model/test_clip_mode.py
git commit -m "$(cat <<'EOF'
Name the two vocabularies before Clip has to hold them

Channel exists because front-plus-rear stopped being enough: the VIOFO
A139 Pro records three channels against one timestamp and the A329 adds a
telephoto, so a clip holds a map rather than two fields.

ClipMode is small on purpose. BlackVue has sixteen mode letters and this
tool acts on about five distinctions, so the enum is the decision and the
vendor's own token is carried beside it rather than thrown away.

Values are the lowercase strings deliberately -- they are written into
canonical workspace filenames later, and an enum whose value is an
implementation detail would make that a translation table.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Reshape Clip

**Files:**
- Modify: `src/dashcam_exporter/domain/model/clip.py` (whole file)
- Modify: `src/dashcam_exporter/infrastructure/repository/ddpai_clip_repository.py:45`
- Modify: `tests/test_grouping.py:83`, `tests/test_parking.py:78`, `tests/test_track.py:57`
- Test: `tests/domain/model/test_clip.py`

**Interfaces:**
- Consumes: `Channel`, `ClipMode` from task 2.
- Produces:

```python
Clip(timestamp: str, epoch_utc: int, playback_seconds: float,
     wall_seconds: float, videos: Mapping[Channel, Path],
     mode: ClipMode = ClipMode.NORMAL, source_mode: str = "",
     protected: bool = False)
```

  plus the classmethod
  `Clip.paired(timestamp, epoch_utc, duration, front, rear=None, mode=ClipMode.NORMAL, source_mode="", protected=False) -> Clip`
  and read-only properties `front -> Path`, `rear -> Path | None`,
  `duration -> float`, `started_at`, `ended_at`, `dt`, `end`,
  `gap_after(previous)`, `gap_before(previous)`.

**Why two durations:** `ended_at` drives trip grouping and is wall-clock. Thinkware's timelapse modes record ten minutes of real time into a two-minute file, so wall span and playback length differ by the timelapse ratio. For every other clip they are equal, which is exactly why one adapter hid this.

- [ ] **Step 1: Write the failing test**

Create `tests/domain/model/test_clip.py`:

```python
"""A clip, once both durations and more than two channels have to fit."""

import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode


class ClipTest(unittest.TestCase):
    def test_paired_builds_the_common_two_channel_case(self):
        clip = Clip.paired("20260806170529", 1785000329, 60,
                           Path("front.mp4"), Path("rear.mp4"))

        self.assertEqual(clip.front, Path("front.mp4"))
        self.assertEqual(clip.rear, Path("rear.mp4"))
        self.assertEqual(clip.duration, 60)
        self.assertEqual(clip.mode, ClipMode.NORMAL)
        self.assertFalse(clip.protected)

    def test_rear_is_none_when_the_camera_recorded_only_front(self):
        clip = Clip.paired("20260806170529", 0, 60, Path("front.mp4"))

        self.assertIsNone(clip.rear)

    def test_three_channels_share_one_timestamp(self):
        clip = Clip(timestamp="20201018170010", epoch_utc=0,
                    playback_seconds=60, wall_seconds=60,
                    videos={Channel.FRONT: Path("062PF.MP4"),
                            Channel.INTERIOR: Path("063PI.MP4"),
                            Channel.REAR: Path("064PR.MP4")},
                    mode=ClipMode.PARKING, source_mode="P")

        self.assertEqual(clip.front, Path("062PF.MP4"))
        self.assertEqual(clip.rear, Path("064PR.MP4"))
        self.assertEqual(clip.videos[Channel.INTERIOR], Path("063PI.MP4"))
        self.assertEqual(clip.source_mode, "P")

    def test_timelapse_ends_by_wall_clock_not_by_playback_length(self):
        # Thinkware records ten minutes of real time into a two-minute file.
        clip = Clip(timestamp="20260806170529", epoch_utc=0,
                    playback_seconds=120, wall_seconds=600,
                    videos={Channel.FRONT: Path("f.mp4")},
                    mode=ClipMode.TIMELAPSE, source_mode="motion_timelapse_rec")

        self.assertEqual(clip.ended_at, datetime(2026, 8, 6, 17, 15, 29))
        self.assertEqual(clip.duration, 120)

    def test_gap_after_measures_wall_clock_between_clips(self):
        first = Clip.paired("20260806170529", 0, 60, Path("a.mp4"))
        second = Clip.paired("20260806170729", 0, 60, Path("b.mp4"))

        self.assertEqual(second.gap_after(first), 60.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_clip -v`
Expected: FAIL with `AttributeError: type object 'Clip' has no attribute 'paired'`.

- [ ] **Step 3: Rewrite Clip**

Replace the whole of `src/dashcam_exporter/domain/model/clip.py` with:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .channel import Channel
from .clip_mode import ClipMode


@dataclass(frozen=True, slots=True)
class Clip:
    """An immutable recording discovered on a card, in our own terms.

    Two durations, because they are two different questions. wall_seconds is
    how much of the world this clip covers and is what trip grouping reads;
    playback_seconds is how long ffmpeg will play it. They are equal for
    every ordinary clip, and differ by the timelapse ratio for the modes that
    compress ten minutes of road into two minutes of file -- which is a
    distinction one camera could never have taught us.

    videos is a map rather than a front/rear pair because a three-channel
    camera records front, interior and rear against one timestamp.
    """

    timestamp: str
    epoch_utc: int
    playback_seconds: float
    wall_seconds: float
    videos: Mapping[Channel, Path]
    mode: ClipMode = ClipMode.NORMAL
    source_mode: str = ""
    protected: bool = False

    @classmethod
    def paired(cls, timestamp: str, epoch_utc: int, duration: float,
               front: Path, rear: Path | None = None,
               mode: ClipMode = ClipMode.NORMAL, source_mode: str = "",
               protected: bool = False) -> "Clip":
        """The two-channel case, where wall clock and playback agree."""
        videos = {Channel.FRONT: front}
        if rear is not None:
            videos[Channel.REAR] = rear
        return cls(timestamp, epoch_utc, duration, duration,
                   MappingProxyType(videos), mode, source_mode, protected)

    @property
    def front(self) -> Path:
        return self.videos[Channel.FRONT]

    @property
    def rear(self) -> Path | None:
        return self.videos.get(Channel.REAR)

    @property
    def duration(self) -> float:
        """Playback length. Ask wall_seconds for the span it covers."""
        return self.playback_seconds

    @property
    def started_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S")

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.wall_seconds)

    # Compatibility aliases let the legacy orchestration consume the extracted
    # value object while it is migrated incrementally.
    @property
    def dt(self) -> datetime:
        return self.started_at

    @property
    def end(self) -> datetime:
        return self.ended_at

    def gap_after(self, previous: "Clip") -> float:
        return max(0.0, (self.started_at - previous.ended_at).total_seconds())

    def gap_before(self, previous: "Clip") -> float:
        return self.gap_after(previous)
```

Note: `MappingProxyType` is deliberate — without it the map inside a frozen dataclass is still mutable, which makes the immutability a comment rather than a fact.

- [ ] **Step 4: Run the new test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.domain.model.test_clip -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Update the one production constructor**

In `src/dashcam_exporter/infrastructure/repository/ddpai_clip_repository.py`, change line 45 from:

```python
        return Clip(timestamp, epoch, duration, front, rear)
```

to:

```python
        return Clip.paired(timestamp, epoch, duration, front, rear)
```

- [ ] **Step 6: Update the three test constructors**

In `tests/test_grouping.py:83`, `tests/test_parking.py:78` and `tests/test_track.py:57`, replace `Clip(` with `Clip.paired(` and convert the keyword arguments to positional in the order `timestamp, epoch_utc, duration, front, rear`. For example `tests/test_track.py:57` becomes:

```python
    return Clip.paired(ts, 0, secs, front, rear)
```

Read each call site first — the existing keyword names are `timestamp=`, `epoch_utc=`, `duration=`, `front=`, `rear=`, so the mapping is direct.

- [ ] **Step 7: Run the whole suite**

Run: `./run-tests.sh`
Expected: `all green`. If a test fails on `.duration` returning a float where an int was asserted, that is a real behaviour change — fix the assertion, not the model.

- [ ] **Step 8: Commit**

```bash
git add src/dashcam_exporter/domain/model/clip.py src/dashcam_exporter/infrastructure/repository/ddpai_clip_repository.py tests/domain/model/test_clip.py tests/test_grouping.py tests/test_parking.py tests/test_track.py
git commit -m "$(cat <<'EOF'
Split the two durations a clip was answering with one number

ended_at drives trip grouping and is a wall-clock question; ffmpeg asks a
playback question. Every clip this tool has ever handled answered both
with the same number, so one field was indistinguishable from correct.
Thinkware's timelapse modes record ten minutes of road into a two-minute
file, and a clip like that would have been grouped as ending eight
minutes early -- silently, since nothing would raise.

The channel map lands in the same change because it is the same lesson
from the same research: the VIOFO A139 Pro records front, interior and
rear against one timestamp, so front-plus-rear cannot hold a clip.

front, rear and duration stay as properties. Forty-three readers in src
do not need to know any of this happened.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: The CardLayout contract

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/card_layout.py`
- Test: `tests/infrastructure/adapters/test_card_layout.py`
- Create: `tests/infrastructure/__init__.py`, `tests/infrastructure/adapters/__init__.py`

**Interfaces:**
- Consumes: `Clip`, `Track` from tasks 1 and 3.
- Produces: `CardLayout` ABC with abstract methods `clips() -> list[Clip]`, `stamp_of(path: Path) -> str | None`, `track_for(clip: Clip) -> Track | None`, `import_roots() -> tuple[Path, ...]`, `is_track_artifact(path: Path) -> bool`.

- [ ] **Step 1: Create the test package files**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p tests/infrastructure/adapters
touch tests/infrastructure/__init__.py tests/infrastructure/adapters/__init__.py
```

- [ ] **Step 2: Write the failing test**

The point of this test is that the ABC actually refuses a partial implementation — an interface someone else implements is declared as one, not left as a duck-typed hope.

Create `tests/infrastructure/adapters/test_card_layout.py`:

```python
"""The contract a third party implements to support a new camera."""

import unittest
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.adapters import CardLayout


class HalfBuiltLayout(CardLayout):
    def clips(self) -> list[Clip]:
        return []


class CompleteLayout(CardLayout):
    def clips(self) -> list[Clip]:
        return []

    def stamp_of(self, path: Path) -> str | None:
        return None

    def track_for(self, clip: Clip) -> Track | None:
        return None

    def import_roots(self) -> tuple[Path, ...]:
        return ()

    def is_track_artifact(self, path: Path) -> bool:
        return False


class CardLayoutTest(unittest.TestCase):
    def test_a_partial_implementation_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            HalfBuiltLayout()

    def test_a_complete_implementation_can(self):
        self.assertEqual(CompleteLayout().clips(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_card_layout -v`
Expected: FAIL with `ImportError: cannot import name 'CardLayout'`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/infrastructure/adapters/card_layout.py`:

```python
from abc import ABC, abstractmethod
from pathlib import Path

from dashcam_exporter.domain import Clip, Track


class CardLayout(ABC):
    """Everything this tool asks about one specific card.

    Five questions, and they are the pipeline's questions rather than any
    camera's answers. A camera that stores its track as an s-expression, in a
    mislabeled tar, or inside the video container satisfies track_for the same
    way: by returning our Track. Nothing camera-shaped travels past here.
    """

    @abstractmethod
    def clips(self) -> list[Clip]:
        """Every clip on the card, in ascending recording-time order.

        Channels are paired here, by whatever rule the camera requires --
        exact timestamps for Thinkware, a tolerance window for DDPAI and
        VIOFO, whose front and rear clocks drift apart.
        """

    @abstractmethod
    def stamp_of(self, path: Path) -> str | None:
        """This file's canonical YYYYMMDDHHMMSS stamp, or None if it has none.

        The canonical form is ours. A camera writing 2020_1018_170010 or
        REC_2019_07_01_10_25_30_F translates here and nowhere else.
        """

    @abstractmethod
    def track_for(self, clip: Clip) -> Track | None:
        """The parsed track covering this clip, or None if it recorded none."""

    @abstractmethod
    def import_roots(self) -> tuple[Path, ...]:
        """Directories the importer copies, as absolute paths.

        Cards hoard: DDPAI keeps photos, thumbnails and logs this tool never
        reads, and copying them costs gigabytes per import.
        """

    @abstractmethod
    def is_track_artifact(self, path: Path) -> bool:
        """Whether this file carries GPS, however the camera stores it.

        The destructive paths ask before they erase, so a wrong answer here
        deletes a drive's route.
        """
```

- [ ] **Step 5: Export it**

Modify `src/dashcam_exporter/infrastructure/adapters/__init__.py` to read:

```python
from .card_layout import CardLayout
from .exporter_adapter import ExporterAdapter
from .ddpai_data_adapter import DdpaiDataAdapter

__all__ = ["CardLayout", "DdpaiDataAdapter", "ExporterAdapter"]
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_card_layout -v`
Expected: PASS, 2 tests.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/card_layout.py src/dashcam_exporter/infrastructure/adapters/__init__.py tests/infrastructure
git commit -m "$(cat <<'EOF'
Declare the five questions a card has to answer

The old contract took a front directory, a rear directory and a tar
directory, which is DDPAI's filing system with an ABC wrapped round it.
BlackVue puts both cameras in one folder and the newer models keep GPS
inside the MP4, so there is nothing to pass for two of those three
arguments.

These five are the pipeline's questions instead, and the test asserts the
part that makes it a contract rather than a suggestion: a partial
implementation raises TypeError. Someone adding a camera should find out
at construction, not when a destructive path calls a method nobody wrote.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The ExporterAdapter contract and registry

**Files:**
- Modify: `src/dashcam_exporter/infrastructure/adapters/exporter_adapter.py` (whole file)
- Create: `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py`
- Test: `tests/infrastructure/adapters/test_adapter_registry.py`

**Interfaces:**
- Consumes: `CardLayout` from task 4.
- Produces:
  - `ExporterAdapter` ABC: property `name -> str`, `detect(card_root: Path) -> bool`, `layout_for(card_root: Path) -> CardLayout`.
  - `AdapterRegistry(adapters: Sequence[ExporterAdapter])` with `detect(card_root: Path, forced: str | None = None) -> ExporterAdapter`, raising `NoAdapterFound` or `AmbiguousCard`. Both exception classes live in `adapter_registry.py`.

**Note on the old file:** `exporter_adapter.py` currently declares `discover_clips` and `prepare_gps`. Those move to `CardLayout` (already done) and out of this ABC. `DdpaiDataAdapter` still declares them and will stop inheriting them in task 8 — until then it simply has extra methods, which is fine.

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/test_adapter_registry.py`:

```python
"""Choosing an adapter for a card, and refusing to guess."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.adapters import (
    AdapterRegistry, AmbiguousCard, CardLayout, ExporterAdapter,
    NoAdapterFound)


class NullLayout(CardLayout):
    def clips(self) -> list[Clip]:
        return []

    def stamp_of(self, path: Path) -> str | None:
        return None

    def track_for(self, clip: Clip) -> Track | None:
        return None

    def import_roots(self) -> tuple[Path, ...]:
        return ()

    def is_track_artifact(self, path: Path) -> bool:
        return False


class MarkerAdapter(ExporterAdapter):
    """Claims any card holding a directory it was named after."""

    def __init__(self, marker: str) -> None:
        self._marker = marker

    @property
    def name(self) -> str:
        return self._marker

    def detect(self, card_root: Path) -> bool:
        return (card_root / self._marker).is_dir()

    def layout_for(self, card_root: Path) -> CardLayout:
        return NullLayout()


class AdapterRegistryTest(unittest.TestCase):
    def test_picks_the_single_adapter_that_claims_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "alpha").mkdir()
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            self.assertEqual(registry.detect(card).name, "alpha")

    def test_refuses_to_guess_when_two_adapters_claim_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "alpha").mkdir()
            (card / "beta").mkdir()
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            with self.assertRaises(AmbiguousCard) as raised:
                registry.detect(card)

            self.assertIn("alpha", str(raised.exception))
            self.assertIn("beta", str(raised.exception))

    def test_an_override_wins_without_consulting_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha"),
                                        MarkerAdapter("beta")])

            self.assertEqual(registry.detect(card, forced="beta").name, "beta")

    def test_an_unknown_override_names_what_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha")])

            with self.assertRaises(NoAdapterFound) as raised:
                registry.detect(card, forced="nosuch")

            self.assertIn("alpha", str(raised.exception))

    def test_no_claim_at_all_is_an_error_naming_the_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            registry = AdapterRegistry([MarkerAdapter("alpha")])

            with self.assertRaises(NoAdapterFound) as raised:
                registry.detect(card)

            self.assertIn(str(card), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_adapter_registry -v`
Expected: FAIL with `ImportError: cannot import name 'AdapterRegistry'`.

- [ ] **Step 3: Rewrite the ExporterAdapter ABC**

Replace the whole of `src/dashcam_exporter/infrastructure/adapters/exporter_adapter.py` with:

```python
from abc import ABC, abstractmethod
from pathlib import Path

from .card_layout import CardLayout


class ExporterAdapter(ABC):
    """Support for one camera's way of filing footage.

    An adapter is keyed to a layout, not to a company. BlackVue changed GPS
    regimes between model generations and VIOFO's A119 V3 uses a different
    filename grammar from the rest of the range, so two adapters for one
    brand is a normal outcome rather than a design failure.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier used in logs, configuration and the override."""

    @abstractmethod
    def detect(self, card_root: Path) -> bool:
        """Whether this adapter recognises the tree at card_root.

        Inspect structure rather than a single marker: DDPAI and VIOFO both
        live under DCIM, so the presence of DCIM decides nothing.
        """

    @abstractmethod
    def layout_for(self, card_root: Path) -> CardLayout:
        """The layout answering questions about this particular card."""
```

- [ ] **Step 4: Write the registry**

Create `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py`:

```python
from pathlib import Path
from typing import Sequence

from .exporter_adapter import ExporterAdapter


class NoAdapterFound(Exception):
    """No registered adapter recognised the card, or the override is unknown."""


class AmbiguousCard(Exception):
    """More than one adapter claimed the card, so the tool refuses to guess."""


class AdapterRegistry:
    """Resolves a card to exactly one adapter, or says why it cannot.

    Ambiguity raises rather than picking a winner. Two adapters claiming one
    card means a detect() is too loose, and a silent first-match would hide
    that until someone's footage came out wrong.
    """

    def __init__(self, adapters: Sequence[ExporterAdapter]) -> None:
        self._adapters = tuple(adapters)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self._adapters)

    def detect(self, card_root: Path,
               forced: str | None = None) -> ExporterAdapter:
        if forced is not None:
            return self._named(forced)
        claimed = [a for a in self._adapters if a.detect(card_root)]
        if len(claimed) == 1:
            return claimed[0]
        if not claimed:
            raise NoAdapterFound(
                "No adapter recognises %s. Registered: %s"
                % (card_root, ", ".join(self.names) or "none"))
        raise AmbiguousCard(
            "%s is claimed by %s. Force one with the adapter setting."
            % (card_root, ", ".join(a.name for a in claimed)))

    def _named(self, name: str) -> ExporterAdapter:
        for adapter in self._adapters:
            if adapter.name == name:
                return adapter
        raise NoAdapterFound(
            "No adapter named %r. Registered: %s"
            % (name, ", ".join(self.names) or "none"))
```

- [ ] **Step 5: Export the new names**

Modify `src/dashcam_exporter/infrastructure/adapters/__init__.py` to read:

```python
from .adapter_registry import AdapterRegistry, AmbiguousCard, NoAdapterFound
from .card_layout import CardLayout
from .exporter_adapter import ExporterAdapter
from .ddpai_data_adapter import DdpaiDataAdapter

__all__ = ["AdapterRegistry", "AmbiguousCard", "CardLayout",
           "DdpaiDataAdapter", "ExporterAdapter", "NoAdapterFound"]
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_adapter_registry -v`
Expected: PASS, 5 tests.

- [ ] **Step 7: Run the whole suite**

Run: `./run-tests.sh`

Expected: FAIL. `DdpaiDataAdapter` still inherits from `ExporterAdapter` and no longer implements the new abstract methods, so `tests/test_ddpai_adapter.py` raises `TypeError` on instantiation. Fix it now by making the shim stop inheriting — change line 12 of `src/dashcam_exporter/infrastructure/adapters/ddpai_data_adapter.py` from:

```python
class DdpaiDataAdapter(ExporterAdapter):
```

to:

```python
class DdpaiDataAdapter:
```

and delete the now-unused `from .exporter_adapter import ExporterAdapter` import on line 9. Task 8 replaces this class properly; for now it is a plain class with the old two methods, and `renderer.py` keeps working unchanged.

- [ ] **Step 8: Run the whole suite again**

Run: `./run-tests.sh`
Expected: `all green`.

- [ ] **Step 9: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/ tests/infrastructure/adapters/test_adapter_registry.py
git commit -m "$(cat <<'EOF'
Give a card one way in, and make ambiguity loud

Nothing selected an adapter before -- renderer.py constructed DdpaiDataAdapter
directly, so a third party's implementation had no route into the tool no
matter how correctly it was written. The registry is that route.

It raises on ambiguity rather than taking the first match. Two adapters
claiming one card means somebody's detect() is too loose, and first-match
would bury that until a drive rendered from the wrong grammar. DDPAI and
VIOFO both live under DCIM, so this is a near case rather than a
hypothetical one.

DdpaiDataAdapter stops inheriting the ABC here and becomes a plain shim so
renderer.py keeps working. It is replaced two commits from now.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: DDPAI track source

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/ddpai/__init__.py`
- Create: `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_track_source.py`
- Test: `tests/infrastructure/adapters/ddpai/test_ddpai_track_source.py`
- Create: `tests/infrastructure/adapters/ddpai/__init__.py`

**Interfaces:**
- Consumes: `Track`, `TrackPoint`.
- Produces: `DdpaiTrackSource(tar_directory: Path, logger: logging.Logger | None = None)` with `track_covering(started_at: datetime, ended_at: datetime) -> Track` and `is_track_artifact(path: Path) -> bool`.

**Background this task must honour:** DDPAI stores GPS as `.gpx` members inside tar archives mislabeled `.git`, recent ones directly under `203gps/tar` and older ones under `203gps/tar/tmp`, hence recursive discovery. Archive names carry their own start and a span (`20260806170529_0540.git`), not a clip stamp, so archives are selected by time overlap and never by name matching. macOS resource forks (`._` prefix) must be skipped. The payload is NMEA: `$GPRMC` lines, `ddmm.mmmm` coordinates, speed in knots. Fixes with status other than `A` are invalid and dropped.

- [ ] **Step 1: Create the package directories**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p src/dashcam_exporter/infrastructure/adapters/ddpai
mkdir -p tests/infrastructure/adapters/ddpai
touch src/dashcam_exporter/infrastructure/adapters/ddpai/__init__.py
touch tests/infrastructure/adapters/ddpai/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/infrastructure/adapters/ddpai/test_ddpai_track_source.py`:

```python
"""Turning DDPAI's mislabeled tar archives into our Track."""

import io
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiTrackSource

NMEA = (
    "$GPRMC,170530.00,A,4712.3456,N,00832.1234,E,20.0,35.3,060826,,,A*52\n"
    "$GPRMC,170531.00,A,4712.4000,N,00832.2000,E,30.0,35.3,060826,,,A*52\n"
    "$GPRMC,170532.00,V,4712.5000,N,00832.3000,E,99.0,35.3,060826,,,A*52\n"
)


def write_archive(directory: Path, name: str, member: str,
                  payload: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    info = tarfile.TarInfo(member)
    info.size = len(payload)
    with tarfile.open(directory / name, "w") as handle:
        handle.addfile(info, io.BytesIO(payload))


class DdpaiTrackSourceTest(unittest.TestCase):
    def test_reads_nmea_from_a_nested_archive_into_our_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory / "tmp", "20260806170529_0540.git",
                          "nested/20260806170529_0060.gpx", NMEA.encode())

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 6, 17, 5, 29),
                datetime(2026, 8, 6, 17, 6, 29))

        self.assertEqual(len(track.points), 2)
        self.assertAlmostEqual(track.points[0].lat, 47.205760, places=5)
        self.assertAlmostEqual(track.points[0].lon, 8.535390, places=5)
        self.assertAlmostEqual(track.points[0].kmh, 37.04, places=2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 8, 6, 17, 5, 30))

    def test_a_window_the_archives_do_not_cover_yields_an_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            write_archive(tar_directory, "20260806170529_0540.git",
                          "20260806170529_0060.gpx", NMEA.encode())

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 7, 9, 0, 0),
                datetime(2026, 8, 7, 9, 1, 0))

        self.assertTrue(track.is_empty)

    def test_a_corrupt_archive_is_survived_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as temporary:
            tar_directory = Path(temporary) / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "20260806170529_0540.git").write_bytes(b"not a tar")

            track = DdpaiTrackSource(tar_directory).track_covering(
                datetime(2026, 8, 6, 17, 5, 29),
                datetime(2026, 8, 6, 17, 6, 29))

        self.assertTrue(track.is_empty)

    def test_recognises_both_archive_and_plain_gpx_as_track_artifacts(self):
        source = DdpaiTrackSource(Path("/nowhere"))

        self.assertTrue(source.is_track_artifact(Path("a/20260806_0540.git")))
        self.assertTrue(source.is_track_artifact(Path("a/20260806.GPX")))
        self.assertFalse(source.is_track_artifact(Path("a/20260806_0060.mp4")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_track_source -v`
Expected: FAIL with `ImportError: cannot import name 'DdpaiTrackSource'`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_track_source.py`:

```python
import logging
import os
import re
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint

KNOTS_TO_KMH = 1.852

_ARCHIVE_NAME = re.compile(r"^(\d{14})_(\d+)\.git$", re.IGNORECASE)


class DdpaiTrackSource:
    """DDPAI's GPS: NMEA inside tar archives the camera calls '.git'.

    Archives are named with their OWN start and a span, not with a clip's
    stamp, so they are selected by time overlap. Matching them by stamp once
    took two files off a card that held thirty for that day and produced a
    drive with no route at all, from footage whose track was sitting right
    there.
    """

    def __init__(self, tar_directory: Path,
                 logger: logging.Logger | None = None) -> None:
        self._tar_directory = tar_directory
        self._logger = logger or logging.getLogger(__name__)

    def is_track_artifact(self, path: Path) -> bool:
        return path.suffix.lower() in (".gpx", ".git")

    def track_covering(self, started_at: datetime,
                       ended_at: datetime) -> Track:
        points: list[TrackPoint] = []
        for archive in self._archives_overlapping(started_at, ended_at):
            points.extend(self._points_in(archive))
        inside = [p for p in points if started_at <= p.at_utc <= ended_at]
        return Track(points=tuple(sorted(inside, key=lambda p: p.at_utc)))

    def _archives_overlapping(self, started_at: datetime,
                              ended_at: datetime) -> list[Path]:
        # rglob, not iterdir: the camera keeps recent archives directly under
        # tar and moves older ones into tar/tmp.
        found = []
        if not self._tar_directory.is_dir():
            return found
        for archive in sorted(self._tar_directory.rglob("*.git")):
            if archive.name.startswith("._"):
                continue
            span = self._span_of(archive)
            if span is None:
                found.append(archive)      # unreadable name: read it anyway
                continue
            archive_start, archive_end = span
            if archive_start <= ended_at and archive_end >= started_at:
                found.append(archive)
        return found

    def _span_of(self, archive: Path) -> tuple[datetime, datetime] | None:
        match = _ARCHIVE_NAME.match(archive.name)
        if not match:
            return None
        try:
            start = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return start, start + timedelta(seconds=int(match.group(2)))

    def _points_in(self, archive: Path) -> list[TrackPoint]:
        points: list[TrackPoint] = []
        try:
            with tarfile.open(archive, "r") as handle:
                for member in handle.getmembers():
                    name = os.path.basename(member.name)
                    if not name.lower().endswith(".gpx") or name.startswith("._"):
                        continue
                    stream = handle.extractfile(member)
                    if stream is None:
                        continue
                    points.extend(self._parse(stream.read()))
        except (tarfile.TarError, OSError) as error:
            self._logger.warning("Cannot read DDPAI GPS archive %s: %s",
                                 archive, error)
        return points

    def _parse(self, payload: bytes) -> list[TrackPoint]:
        points = []
        for line in payload.decode("utf-8", errors="ignore").splitlines():
            point = self._point_from(line)
            if point is not None:
                points.append(point)
        return points

    def _point_from(self, line: str) -> TrackPoint | None:
        if not line.startswith("$GPRMC"):
            return None
        fields = line.split(",")
        # $GPRMC,time,status,lat,N,lon,E,speed_knots,heading,date,...
        if len(fields) < 10 or fields[2] != "A":
            return None
        lat = _to_decimal(fields[3], fields[4])
        lon = _to_decimal(fields[5], fields[6])
        at_utc = _to_utc(fields[9], fields[1])
        if lat is None or lon is None or at_utc is None:
            return None
        try:
            kmh = float(fields[7]) * KNOTS_TO_KMH
        except ValueError:
            kmh = 0.0
        return TrackPoint(lat, lon, kmh, at_utc)


def _to_decimal(value: str, hemisphere: str) -> float | None:
    """NMEA ddmm.mmmm / dddmm.mmmm to decimal degrees."""
    try:
        if not value or "." not in value:
            return None
        dot = value.index(".")
        degrees = int(value[: dot - 2])
        minutes = float(value[dot - 2:])
        result = degrees + minutes / 60.0
        return -result if hemisphere in ("S", "W") else result
    except (ValueError, IndexError):
        return None


def _to_utc(date_field: str, time_field: str) -> datetime | None:
    """NMEA ddmmyy plus hhmmss.ss to a UTC datetime."""
    try:
        return datetime.strptime(date_field + time_field.split(".")[0],
                                 "%d%m%y%H%M%S")
    except ValueError:
        return None
```

- [ ] **Step 5: Export it**

Write `src/dashcam_exporter/infrastructure/adapters/ddpai/__init__.py`:

```python
from .ddpai_track_source import DdpaiTrackSource

__all__ = ["DdpaiTrackSource"]
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_track_source -v`
Expected: PASS, 4 tests. If the coordinate assertions fail, check `_to_decimal` against the expected values by hand: `4712.3456` is 47 degrees plus 12.3456 minutes, which is 47.20576.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/ddpai tests/infrastructure/adapters/ddpai
git commit -m "$(cat <<'EOF'
Parse DDPAI's GPS where the camera's oddities are already known

The archives are tars the firmware labels .git, recent ones directly under
203gps/tar and older ones moved into tar/tmp, and each is named with its
own start and span rather than any clip's stamp. Every one of those facts
was previously spread between renderer.py, pipeline.py and the adapter,
and the extraction itself existed twice.

Selecting archives by time overlap rather than by name is the load-bearing
part: matching on stamp once took two files off a card holding thirty for
that day, and the drive rendered with no route from footage whose track
was sitting right there.

The NMEA parsing moves in here too. It is the reason a Track is worth
having -- the '$GPRMC' filter that lives in tracking.py today would read
nothing at all from BlackVue's '[epoch]$GNGGA' or Thinkware's RMC in a
timed-text track, and now it never has to.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: DDPAI card layout

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_card_layout.py`
- Test: `tests/infrastructure/adapters/ddpai/test_ddpai_card_layout.py`

**Interfaces:**
- Consumes: `CardLayout`, `Clip`, `Track`, `DdpaiTrackSource`, and the existing `DdpaiClipRepository`.
- Produces: `DdpaiCardLayout(card_root: Path, rear_pair_tolerance_seconds: int = 2, logger=None)` implementing all five `CardLayout` methods.

**Layout facts, verified against a real card:** video under `DCIM/200video/front` and `DCIM/200video/rear`, GPS under `DCIM/203gps/tar`, and the card also carries `201photo`, `202thumb` and `207log` which this tool never reads. Front files are `<stamp>_<duration>.mp4`, rear files `<stamp>_<duration>_A.mp4`. A real 235-clip import had 234 rear files: unpaired front clips are normal.

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/ddpai/test_ddpai_card_layout.py`:

```python
"""The DDPAI card, answered through the contract a third party implements."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiCardLayout


def build_card(root: Path) -> Path:
    for relative in ("DCIM/200video/front", "DCIM/200video/rear",
                     "DCIM/203gps/tar", "DCIM/201photo/front",
                     "DCIM/202thumb/front", "DCIM/207log/tmp"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


class DdpaiCardLayoutTest(unittest.TestCase):
    def test_pairs_rear_across_a_one_second_camera_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()
            (card / "DCIM/200video/rear/20260806170530_0060_A.mp4").touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].duration, 60)
        self.assertEqual(clips[0].wall_seconds, 60)
        self.assertEqual(clips[0].mode, ClipMode.NORMAL)
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "20260806170530_0060_A.mp4")

    def test_a_front_clip_with_no_partner_is_normal_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertIsNone(clips[0].rear)

    def test_clips_come_back_in_recording_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for stamp in ("20260806170729", "20260806170529", "20260806170629"):
                (card / ("DCIM/200video/front/%s_0060.mp4" % stamp)).touch()

            clips = DdpaiCardLayout(card).clips()

        self.assertEqual([c.timestamp for c in clips],
                         ["20260806170529", "20260806170629", "20260806170729"])

    def test_stamp_of_reads_the_canonical_form_from_either_camera(self):
        layout = DdpaiCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("20260806170529_0060.mp4")),
                         "20260806170529")
        self.assertEqual(layout.stamp_of(Path("20260806170529_0060_A.mp4")),
                         "20260806170529")
        self.assertIsNone(layout.stamp_of(Path("notes.txt")))

    def test_import_roots_leave_the_photos_thumbnails_and_logs_behind(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            roots = DdpaiCardLayout(card).import_roots()

        self.assertEqual(
            sorted(r.relative_to(card).as_posix() for r in roots),
            ["DCIM/200video", "DCIM/203gps"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_card_layout -v`
Expected: FAIL with `ImportError: cannot import name 'DdpaiCardLayout'`.

- [ ] **Step 3: Write the implementation**

Create `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_card_layout.py`:

```python
import logging
import re
from pathlib import Path

from dashcam_exporter.domain import Clip, Track
from dashcam_exporter.infrastructure.repository import DdpaiClipRepository

from ..card_layout import CardLayout
from .ddpai_track_source import DdpaiTrackSource

VIDEO_ROOT = "DCIM/200video"
GPS_ROOT = "DCIM/203gps"

_STAMP = re.compile(r"^(\d{14})_\d+(_A)?\.mp4$", re.IGNORECASE)


class DdpaiCardLayout(CardLayout):
    """A DDPAI card: two video directories, and GPS in mislabeled tars.

    The card also carries 201photo, 202thumb and 207log -- several hundred
    megabytes this tool never reads, which is why import_roots names two
    directories rather than returning DCIM.
    """

    def __init__(self, card_root: Path, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._card_root = card_root
        self._clips = DdpaiClipRepository(rear_pair_tolerance_seconds)
        self._tracks = DdpaiTrackSource(card_root / GPS_ROOT / "tar", logger)

    def clips(self) -> list[Clip]:
        front = self._card_root / VIDEO_ROOT / "front"
        rear = self._card_root / VIDEO_ROOT / "rear"
        if not front.is_dir():
            return []
        return self._clips.find(front, rear)

    def stamp_of(self, path: Path) -> str | None:
        match = _STAMP.match(path.name)
        return match.group(1) if match else None

    def track_for(self, clip: Clip) -> Track | None:
        track = self._tracks.track_covering(clip.started_at, clip.ended_at)
        return None if track.is_empty else track

    def import_roots(self) -> tuple[Path, ...]:
        return (self._card_root / VIDEO_ROOT, self._card_root / GPS_ROOT)

    def is_track_artifact(self, path: Path) -> bool:
        return self._tracks.is_track_artifact(path)
```

- [ ] **Step 4: Export it**

Update `src/dashcam_exporter/infrastructure/adapters/ddpai/__init__.py` to read:

```python
from .ddpai_card_layout import DdpaiCardLayout
from .ddpai_track_source import DdpaiTrackSource

__all__ = ["DdpaiCardLayout", "DdpaiTrackSource"]
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_card_layout -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Run the whole suite**

Run: `./run-tests.sh`
Expected: `all green`.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/ddpai tests/infrastructure/adapters/ddpai/test_ddpai_card_layout.py
git commit -m "$(cat <<'EOF'
Let the DDPAI card answer for itself instead of being described by callers

The directory names were the caller's business before this: eight sites in
pipeline.py spelled out DCIM/200video/front, and the front and rear
directories were arguments to a method on the adapter. Both are facts about
one camera's filing system and belong to the one class that is allowed to
know them.

import_roots names two directories rather than DCIM because the card
hoards -- 201photo, 202thumb and 207log run to hundreds of megabytes this
tool never opens, and an import that copied them would cost that on every
card.

Unpaired front clips are asserted as normal rather than tolerated. A real
card imported here held 235 front files against 234 rear, so a layout that
assumed symmetry would be wrong on the first card it ever saw.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: DDPAI adapter and detection

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_adapter.py`
- Modify: `src/dashcam_exporter/infrastructure/adapters/ddpai_data_adapter.py` (rewrite as a shim over the layout)
- Modify: `src/dashcam_exporter/infrastructure/adapters/__init__.py`
- Test: `tests/infrastructure/adapters/ddpai/test_ddpai_adapter.py`
- Modify: `tests/test_ddpai_adapter.py`

**Interfaces:**
- Consumes: `ExporterAdapter`, `DdpaiCardLayout`.
- Produces: `DdpaiAdapter(rear_pair_tolerance_seconds: int = 2, logger=None)` with `name == "ddpai"`, `detect(card_root)`, `layout_for(card_root) -> DdpaiCardLayout`. Also `default_registry() -> AdapterRegistry` in `adapter_registry.py`, returning a registry holding `DdpaiAdapter()`.

**Detection rule:** a DDPAI card has `DCIM/200video/front`. Testing for `DCIM` alone would also claim every VIOFO card, which is the ambiguity the registry raises on.

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/ddpai/test_ddpai_adapter.py`:

```python
"""Recognising a DDPAI card, and declining the ones that only look like it."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import (
    DdpaiAdapter, DdpaiCardLayout)


class DdpaiAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_numbered_video_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertTrue(DdpaiAdapter().detect(card))

    def test_declines_a_card_that_merely_has_dcim(self):
        # A VIOFO card is DCIM/Movie. Claiming it would make the registry
        # ambiguous the moment a second adapter is registered.
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/Movie").mkdir(parents=True)

            self.assertFalse(DdpaiAdapter().detect(card))

    def test_declines_an_empty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(DdpaiAdapter().detect(Path(temporary)))

    def test_produces_a_layout_bound_to_that_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            layout = DdpaiAdapter().layout_for(card)

        self.assertIsInstance(layout, DdpaiCardLayout)
        self.assertEqual(layout.import_roots()[0], card / "DCIM/200video")

    def test_is_named_for_the_camera(self):
        self.assertEqual(DdpaiAdapter().name, "ddpai")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_adapter -v`
Expected: FAIL with `ImportError: cannot import name 'DdpaiAdapter'`.

- [ ] **Step 3: Write the adapter**

Create `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_adapter.py`:

```python
import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .ddpai_card_layout import VIDEO_ROOT, DdpaiCardLayout


class DdpaiAdapter(ExporterAdapter):
    """DDPAI cards, recognised by their numbered video directory."""

    def __init__(self, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._rear_pair_tolerance_seconds = rear_pair_tolerance_seconds
        self._logger = logger

    @property
    def name(self) -> str:
        return "ddpai"

    def detect(self, card_root: Path) -> bool:
        # DCIM alone decides nothing -- VIOFO cards have it too, and a
        # detect() loose enough to claim theirs makes the registry raise.
        return (card_root / VIDEO_ROOT / "front").is_dir()

    def layout_for(self, card_root: Path) -> DdpaiCardLayout:
        return DdpaiCardLayout(card_root, self._rear_pair_tolerance_seconds,
                               self._logger)
```

- [ ] **Step 4: Export it and add the default registry**

Update `src/dashcam_exporter/infrastructure/adapters/ddpai/__init__.py`:

```python
from .ddpai_adapter import DdpaiAdapter
from .ddpai_card_layout import DdpaiCardLayout
from .ddpai_track_source import DdpaiTrackSource

__all__ = ["DdpaiAdapter", "DdpaiCardLayout", "DdpaiTrackSource"]
```

Append to `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py`:

```python
def default_registry() -> AdapterRegistry:
    """Every adapter shipped with the tool.

    Imported here rather than at module scope so the registry module stays
    free of any particular camera.
    """
    from .ddpai.ddpai_adapter import DdpaiAdapter
    return AdapterRegistry([DdpaiAdapter()])
```

Update `src/dashcam_exporter/infrastructure/adapters/__init__.py`:

```python
from .adapter_registry import (AdapterRegistry, AmbiguousCard, NoAdapterFound,
                               default_registry)
from .card_layout import CardLayout
from .exporter_adapter import ExporterAdapter
from .ddpai.ddpai_adapter import DdpaiAdapter
from .ddpai_data_adapter import DdpaiDataAdapter

__all__ = ["AdapterRegistry", "AmbiguousCard", "CardLayout", "DdpaiAdapter",
           "DdpaiDataAdapter", "ExporterAdapter", "NoAdapterFound",
           "default_registry"]
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_adapter -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Rewrite the shim over the layout**

`renderer.py:314` calls `DdpaiDataAdapter(REAR_PAIR_TOLERANCE_S).discover_clips(front_dir, rear_dir)` and `renderer.py:766` has its own copy of the tar extraction. Plan 3 removes both. Until then the shim must keep working, but it should stop being a second implementation.

Replace the whole of `src/dashcam_exporter/infrastructure/adapters/ddpai_data_adapter.py` with:

```python
import logging
from pathlib import Path

from dashcam_exporter.domain import Clip
from dashcam_exporter.infrastructure.repository import DdpaiClipRepository


class DdpaiDataAdapter:
    """The pre-contract two-method API, kept alive for renderer.py.

    Deprecated. DdpaiAdapter and DdpaiCardLayout are the real implementation;
    this exists so the renderer keeps working until it is rewired, and it
    delegates rather than duplicating so there is still one definition of
    how DDPAI files its footage.
    """

    def __init__(self, rear_pair_tolerance_seconds: int = 2,
                 logger: logging.Logger | None = None) -> None:
        self._clips = DdpaiClipRepository(rear_pair_tolerance_seconds)
        self._logger = logger or logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return "ddpai"

    def discover_clips(self, front_directory: Path,
                       rear_directory: Path | None) -> list[Clip]:
        return self._clips.find(front_directory, rear_directory)
```

Note that `prepare_gps` is gone from the shim. Check whether anything still calls it:

Run: `grep -rn "prepare_gps" --include="*.py" src tests`

Expected: only `tests/test_ddpai_adapter.py`. `renderer.py:766` has its own private copy and does not call the adapter's. If the grep shows a caller in `src/`, stop and report rather than deleting.

- [ ] **Step 7: Replace the old adapter test**

The GPS half of `tests/test_ddpai_adapter.py` now belongs to `test_ddpai_track_source.py`, which asserts the same behaviour in our own types. Replace the whole of `tests/test_ddpai_adapter.py` with:

```python
"""The deprecated two-method shim renderer.py still calls."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters import DdpaiDataAdapter


class DdpaiDataAdapterTest(unittest.TestCase):
    def test_pairs_rear_by_clock_when_camera_is_one_second_late(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            front = root / "front"
            rear = root / "rear"
            front.mkdir()
            rear.mkdir()
            (front / "20260728141441_0060.mp4").touch()
            (rear / "20260728141442_0060_A.mp4").touch()

            clips = DdpaiDataAdapter().discover_clips(front, rear)

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].duration, 60)
        self.assertEqual(clips[0].rear.name, "20260728141442_0060_A.mp4")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 8: Run the whole suite**

Run: `./run-tests.sh`
Expected: `all green`.

- [ ] **Step 9: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters tests/infrastructure/adapters tests/test_ddpai_adapter.py
git commit -m "$(cat <<'EOF'
Recognise a DDPAI card by what makes it one, not by having a DCIM folder

detect() tests for DCIM/200video/front. DCIM alone is the wrong question:
VIOFO cards have one too, and an adapter loose enough to claim theirs would
make the registry raise on ambiguity the day a second adapter ships --
correctly, but for a reason that was ours to avoid.

DdpaiDataAdapter survives as a delegating shim because renderer.py still
calls it, and it loses prepare_gps entirely: the renderer has its own
private copy of that extraction, so removing the adapter's leaves one
definition rather than none. The two copies were the clearest evidence that
the old contract was not carrying the camera knowledge it claimed to.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Calibrate against a real card import

**Files:**
- Create: `tools/calibrate_ddpai.py`

**Interfaces:**
- Consumes: `DdpaiAdapter`.
- Produces: a standalone script, deliberately **not** part of the test suite. `run-tests.sh` guarantees the suite reads neither the card nor the import workspace, and that guarantee is what makes it safe to run mid-render. This script breaks that rule on purpose and is therefore run by hand.

**Why this task exists:** every fixture so far was written by the same person who wrote the parser, so all of them agree with it by construction. A real import at `~/dashcam-data/import/2026-08-08` holds 235 front files, 234 rear and 28 archives from an actual card. It is the only evidence available that is not self-confirming.

- [ ] **Step 1: Write the script**

Create `tools/calibrate_ddpai.py`:

```python
#!/usr/bin/env python3
"""Run the DDPAI adapter over a real import and report what it found.

Not a test. The suite is fixture-only by design so it stays safe to run
mid-render, and every fixture in it was written by whoever wrote the parser
-- so the fixtures agree with the parser by construction. This reads actual
footage instead, which is the only evidence available that is not
self-confirming.

Usage:
    PYTHONPATH=src python3 tools/calibrate_ddpai.py ~/dashcam-data/import/2026-08-08
"""

import sys
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiAdapter


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    card = Path(argv[1]).expanduser()

    adapter = DdpaiAdapter()
    print("card:      %s" % card)
    print("detected:  %s" % adapter.detect(card))
    if not adapter.detect(card):
        print("Nothing further to report -- the adapter does not claim this tree.")
        return 1

    layout = adapter.layout_for(card)
    clips = layout.clips()
    paired = [c for c in clips if c.rear is not None]
    print("clips:     %d" % len(clips))
    print("paired:    %d" % len(paired))
    print("unpaired:  %d" % (len(clips) - len(paired)))
    if clips:
        print("first:     %s  %s" % (clips[0].timestamp, clips[0].front.name))
        print("last:      %s  %s" % (clips[-1].timestamp, clips[-1].front.name))

    with_track = 0
    points = 0
    for clip in clips:
        track = layout.track_for(clip)
        if track is not None:
            with_track += 1
            points += len(track.points)
    print("with gps:  %d of %d" % (with_track, len(clips)))
    print("points:    %d" % points)
    if clips and with_track == 0:
        print("WARNING: not one clip resolved a track. Either the archives "
              "were not copied into this import, or archive selection by "
              "time overlap is wrong.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 2: Run it against the real import**

Run: `cd ~/dev/dashcam-exporter-adapter-change && PYTHONPATH=src python3 tools/calibrate_ddpai.py ~/dashcam-data/import/2026-08-08`

Expected, from the listing taken when this plan was written: `detected: True`, `clips: 235`, `paired: 234`, `unpaired: 1`, and a non-zero count for `with gps` and `points`.

- [ ] **Step 3: Judge the result honestly**

If the counts match, say so and record the actual `with gps` and `points` numbers in the commit message — they are the first evidence the track selection works on footage nobody wrote a fixture for.

If they do not match, **stop and report rather than adjusting the script until it agrees**. A mismatch here is the finding, not an obstacle: 235 front files producing fewer than 235 clips means the filename pattern is wrong, and zero tracks means archive selection by time overlap is wrong. Either is worth more than a green run.

- [ ] **Step 4: Confirm the suite is unaffected**

Run: `./run-tests.sh`
Expected: `all green`, and the script must not appear in the run — `unittest discover -s tests` never looks in `tools/`.

- [ ] **Step 5: Commit**

```bash
git add tools/calibrate_ddpai.py
git commit -m "$(cat <<'EOF'
Check the adapter against footage nobody wrote a fixture for

Every fixture in this plan was written by whoever wrote the parser, so all
of them agree with it by construction -- which is exactly the property that
makes a suite green while a camera goes unread. A real import of 235 front
files, 234 rear and 28 archives is the one piece of evidence available that
was not authored to pass.

It stays out of the suite deliberately. run-tests.sh promises that nothing
in it reads the card, the import workspace or the output tree, and that
promise is what makes the suite safe to run mid-render. Breaking it for one
check would cost more than the check is worth, so this is run by hand.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## What this plan deliberately leaves undone

- `pipeline.py` still hardcodes `DCIM/200video/front` in eight places and `renderer.py` still constructs `DdpaiDataAdapter` directly. Plan 3 rewires them. Nothing in this plan changes observable behaviour of the running tool.
- `renderer.py:766` still holds a second copy of the tar extraction. It is now the only copy outside the adapter, and plan 3 deletes it.
- `tracking.py:parse_gpx_track` still filters on `$GPRMC`. It is unreachable from the new adapter path and stays for the renderer until plan 3.
- No canonical workspace, no `DexGpsFile`, no importer transformation. Plan 3.
- BlackVue and VIOFO adapters, and the card simulator. Plan 2.

## Self-review notes

Checked against the spec: the model section, the five-method contract, the registry with override and loud ambiguity, and the DDPAI evidence are all implemented. The spec's simulator, migration and two-zone sections are explicitly deferred to plans 2 and 3 above rather than left implicit.

One spec item has no task here and is called out instead: the spec's open question about whether `tracking.parse_gpx_track` also serves the per-trip `.gpx` sidecars written as output. It cannot be answered without reading the renderer's output path, and it only matters once plan 3 rewires that path. It stays an open item.
