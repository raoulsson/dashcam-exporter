# Card Simulator and Two More Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the `CardLayout` contract generalises by implementing BlackVue and VIOFO adapters against it, and build a card simulator that writes real MP4s and real GPS payloads so those adapters can be run end to end.

**Architecture:** A `CardSimulator` per camera writes a card tree to disk using ffmpeg for video and a per-camera GPS writer for telemetry. Each adapter is the mirror image: detection, layout, and a GPS source that returns our `Track`. The registry then holds three adapters and must still resolve each card unambiguously.

**Tech Stack:** Python 3 stdlib plus ffmpeg on PATH (8.1.1 verified present). Tests are `unittest`.

## The honesty constraint that shapes this whole plan

For DDPAI, plan 1's calibration was real evidence: a card existed, and it caught two false claims and a silent timezone bug within one run.

**No BlackVue or VIOFO card exists here.** Every byte those adapters will ever be tested against is a byte this same work wrote, from the same reading of the same documents. A misreading of the Novatek record layout would be encoded identically in writer and reader, and they would agree perfectly. This is circular and it is the exact failure mode plan 1 caught.

The decision taken is to build it anyway, because the goal is proving the *contract* generalises — which circular fixtures can do — while labelling the circularity everywhere it could mislead. Therefore:

- Every simulator-backed adapter test name says what it is testing against. Prefer `test_reads_back_what_our_own_writer_produced` over `test_parses_viofo_gps`.
- Every GPS reader docstring states the provenance of the format and that it is unverified against a real file.
- The spec gets a section naming exactly what evidence would settle it: one `find` listing plus one sample MP4 per brand.
- No commit message may claim these adapters read real cards.

**The circle gets closed after this plan, not inside it.** Real card data is
being sourced. When it arrives the check is the same one that worked for
DDPAI: run the adapter over it with a calibration script and believe the
disagreement, not the code. A brand nobody has written an adapter for is
equally welcome — writing a fourth adapter against real footage would test
the contract harder than the two written from documents, because the card
gets to disagree.

## Global Constraints

- Test runner: `./run-tests.sh` (`python3 -m unittest discover -s tests -q`, `PYTHONPATH=src`). No pytest.
- **The suite is currently red at master for unrelated reasons** — 57 tests fail with `Bench._patched.<locals>.<lambda>() got an unexpected keyword argument 'progress'`. Gate on the delta, not on green: no test that passed before may fail after. The comparison script is at `scratchpad/gate.sh`.
- Tests must not read the SD card, the import workspace or the output tree.
- Tests must not invoke ffmpeg. It is slow and it is an external dependency; the simulator is a tool, and adapter unit tests use `touch()`-created files plus small binary payloads written inline.
- Every new test package directory needs an `__init__.py`.
- One class per file, named for the class. No emojis anywhere.
- Commit messages say WHY in prose. Sign off `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Branch `adapter-interface` in `~/dev/dashcam-exporter-adapter-change`.

## File Structure

| File | Responsibility |
|---|---|
| `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_adapter.py` | BlackVue `ExporterAdapter` |
| `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_card_layout.py` | BlackVue `CardLayout` |
| `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_track_source.py` | `.gps` sidecar and embedded-box reading |
| `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_adapter.py` | VIOFO `ExporterAdapter` |
| `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_card_layout.py` | VIOFO `CardLayout`, fuzzy pairing |
| `src/dashcam_exporter/infrastructure/adapters/viofo/novatek_gps_reader.py` | Novatek freeGPS binary reader |
| `src/dashcam_exporter/infrastructure/media/mp4_boxes.py` | Reading top-level MP4 boxes, shared by both |
| `tools/simulator/card_simulator.py` | `CardSimulator` ABC and the ffmpeg clip writer |
| `tools/simulator/ddpai_simulator.py` | DDPAI card writer, calibrated against the real skeleton |
| `tools/simulator/blackvue_simulator.py` | BlackVue card writer |
| `tools/simulator/viofo_simulator.py` | VIOFO card writer |
| `tools/simulate_card.py` | CLI entry point |
| `tools/calibrate_adapters.py` | Generates all three cards, runs the registry over them, reports |

---

### Task 1: MP4 box reader

**Files:**
- Create: `src/dashcam_exporter/infrastructure/media/mp4_boxes.py`
- Test: `tests/infrastructure/media/test_mp4_boxes.py`
- Create: `tests/infrastructure/media/__init__.py`

**Interfaces:**
- Produces: `iter_top_level_boxes(path: Path) -> Iterator[tuple[str, int, int]]` yielding `(fourcc, payload_offset, payload_size)`, and `read_box_payload(path: Path, offset: int, size: int) -> bytes`.

**Why shared:** BlackVue keeps GPS in a box called `gps ` and VIOFO in `free` boxes carrying a `GPS ` magic. Both need the same walk, and writing it twice would be two definitions of the ISO base media container.

- [ ] **Step 1: Create the test package**

```bash
cd ~/dev/dashcam-exporter-adapter-change && mkdir -p tests/infrastructure/media && touch tests/infrastructure/media/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/infrastructure/media/test_mp4_boxes.py`:

```python
"""Walking the top level of an ISO base media file."""

import struct
import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)


def box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


class Mp4BoxesTest(unittest.TestCase):
    def test_walks_every_top_level_box_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom")
                             + box(b"free", b"GPS payload")
                             + box(b"mdat", b"\x00" * 16))

            found = [(name, size) for name, _, size
                     in iter_top_level_boxes(path)]

        self.assertEqual(found, [("ftyp", 4), ("free", 11), ("mdat", 16)])

    def test_reads_one_box_payload_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom") + box(b"free", b"GPS x"))

            boxes = {name: (offset, size)
                     for name, offset, size in iter_top_level_boxes(path)}
            offset, size = boxes["free"]

            self.assertEqual(read_box_payload(path, offset, size), b"GPS x")

    def test_a_truncated_box_header_ends_the_walk_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            path.write_bytes(box(b"ftyp", b"isom") + b"\x00\x00")

            found = [name for name, _, _ in iter_top_level_boxes(path)]

        self.assertEqual(found, ["ftyp"])

    def test_a_size_of_one_means_a_64_bit_length_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.mp4"
            large = (struct.pack(">I", 1) + b"mdat"
                     + struct.pack(">Q", 16 + 4) + b"data")
            path.write_bytes(box(b"ftyp", b"isom") + large)

            found = [(name, size) for name, _, size
                     in iter_top_level_boxes(path)]

        self.assertEqual(found, [("ftyp", 4), ("mdat", 4)])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.media.test_mp4_boxes -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/infrastructure/media/mp4_boxes.py`:

```python
import struct
from pathlib import Path
from typing import Iterator

_HEADER = 8
_LARGE_HEADER = 16


def iter_top_level_boxes(path: Path) -> Iterator[tuple[str, int, int]]:
    """Yield (fourcc, payload offset, payload size) for each top-level box.

    Stops at the first malformed header rather than raising. A dashcam file
    truncated by a power cut mid-write is an ordinary thing to meet on a
    card, and a walk that raises would turn one bad file into a failed
    import.
    """
    size = path.stat().st_size
    offset = 0
    with path.open("rb") as handle:
        while offset + _HEADER <= size:
            handle.seek(offset)
            header = handle.read(_HEADER)
            if len(header) < _HEADER:
                return
            box_size = struct.unpack(">I", header[:4])[0]
            fourcc = header[4:8].decode("latin-1")
            payload_offset = offset + _HEADER
            if box_size == 1:
                extra = handle.read(8)
                if len(extra) < 8:
                    return
                box_size = struct.unpack(">Q", extra)[0]
                payload_offset = offset + _LARGE_HEADER
            elif box_size == 0:
                box_size = size - offset
            if box_size < (payload_offset - offset) or offset + box_size > size:
                return
            yield fourcc, payload_offset, offset + box_size - payload_offset
            offset += box_size


def read_box_payload(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)
```

- [ ] **Step 5: Run it and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.media.test_mp4_boxes -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Commit**

```bash
git add src/dashcam_exporter/infrastructure/media/mp4_boxes.py tests/infrastructure/media
git commit -m "$(cat <<'EOF'
Walk MP4 boxes once, for the two cameras that hide GPS in them

BlackVue's newer models write no sidecar at all and keep telemetry in a box
called 'gps ', and VIOFO keeps it in free boxes carrying a 'GPS ' magic.
Both need the same walk over the same container, and two copies of it would
be two definitions of the ISO base media format that could drift apart.

It stops at a malformed header instead of raising. A file truncated by a
power cut mid-write is an ordinary thing to find on a card, and a walk that
raised would turn one bad clip into a failed import.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: BlackVue track source

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/blackvue/__init__.py`
- Create: `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_track_source.py`
- Test: `tests/infrastructure/adapters/blackvue/test_blackvue_track_source.py`
- Create: `tests/infrastructure/adapters/blackvue/__init__.py`

**Interfaces:**
- Produces: `BlackvueTrackSource(record_directory: Path, logger=None)` with `track_for(base_filename: str, mode_letter: str, video: Path) -> Track` and `is_track_artifact(path: Path) -> bool`.

**Format facts, from BlackVue manuals and the blackvuesync and blackclue parsers — not from a card:**
- Legacy sidecar is `<base>_<type>.gps`, carrying the mode letter but **no** direction letter, so one per clip pair, not per camera.
- Sidecar content is one fix per line: a unix timestamp in milliseconds in square brackets, then an NMEA sentence — `[1611723852888]$GNGGA,...`. The talker is `GN`, not `GP`.
- Newer models write no sidecar; the same text sits in an MP4 box with FourCC `gps ` (trailing space), NUL-terminated.
- `.3gf` holds accelerometer data and is not read by this tool, but counts as a track artifact for sweep safety.

Position is taken from RMC sentences. GGA carries no speed, and this tool needs speed for its parking detection, so a file of GGA only yields points with `kmh` of 0.0 — which the test asserts rather than leaves implicit.

- [ ] **Step 1: Create the packages**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p src/dashcam_exporter/infrastructure/adapters/blackvue tests/infrastructure/adapters/blackvue
touch src/dashcam_exporter/infrastructure/adapters/blackvue/__init__.py tests/infrastructure/adapters/blackvue/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/infrastructure/adapters/blackvue/test_blackvue_track_source.py`:

```python
"""BlackVue telemetry, read back from payloads this project also writes.

Provenance warning: no BlackVue card was available. Every byte here was
written from the same reading of the same documents as the reader, so these
tests prove the two halves agree -- not that either matches a real camera.
"""

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.blackvue import BlackvueTrackSource

SIDECAR = (
    "[1611723852888]$GNRMC,155052.00,A,4529.87489,N,07337.01215,W,"
    "6.225,35.34,270121,,,A*52\n"
    "[1611723853888]$GNRMC,155053.00,A,4529.88000,N,07337.02000,W,"
    "12.000,35.34,270121,,,A*52\n"
    "[1611723854888]$GNRMC,155054.00,V,4529.89000,N,07337.03000,W,"
    "99.000,35.34,270121,,,A*52\n"
)


def box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


class BlackvueTrackSourceTest(unittest.TestCase):
    def test_reads_a_sidecar_named_without_the_direction_letter(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            (record / "20210127_155052_N.gps").write_text(SIDECAR)
            video = record / "20210127_155052_NF.mp4"
            video.touch()

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertEqual(len(track.points), 2)
        self.assertAlmostEqual(track.points[0].lat, 45.4979148, places=5)
        self.assertAlmostEqual(track.points[0].lon, -73.6168692, places=5)
        self.assertAlmostEqual(track.points[0].kmh, 11.529, places=3)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2021, 1, 27, 15, 50, 52))

    def test_falls_back_to_the_box_inside_the_video_when_no_sidecar_exists(self):
        # The DR900X writes no sidecar at all; this is its only copy.
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            video = record / "20210127_155052_NF.mp4"
            video.write_bytes(box(b"ftyp", b"isom")
                              + box(b"gps ", SIDECAR.encode() + b"\x00")
                              + box(b"mdat", b"\x00" * 8))

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertEqual(len(track.points), 2)

    def test_a_clip_that_recorded_nothing_yields_an_empty_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            video = record / "20210127_155052_NF.mp4"
            video.write_bytes(box(b"ftyp", b"isom"))

            track = BlackvueTrackSource(record).track_for(
                "20210127_155052", "N", video)

        self.assertTrue(track.is_empty)

    def test_accelerometer_and_gps_sidecars_both_count_as_track_artifacts(self):
        source = BlackvueTrackSource(Path("/nowhere"))

        self.assertTrue(source.is_track_artifact(Path("a/x_N.gps")))
        self.assertTrue(source.is_track_artifact(Path("a/x_N.3gf")))
        self.assertFalse(source.is_track_artifact(Path("a/x_NF.mp4")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.blackvue.test_blackvue_track_source -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_track_source.py`:

```python
import logging
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint
from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)

KNOTS_TO_KMH = 1.852
_GPS_BOX = "gps "


class BlackvueTrackSource:
    """BlackVue telemetry, from a sidecar or from inside the video.

    Two regimes, because the camera changed between model generations.
    Legacy models write <base>_<type>.gps beside the video -- carrying the
    mode letter but no direction letter, so one file serves both cameras.
    The DR900X writes no sidecar at all and the same text lives in an MP4
    box with FourCC 'gps ', so the sidecar is tried first and the container
    second.

    Provenance: this format is taken from BlackVue's manuals and from the
    blackvuesync and blackclue parsers. No BlackVue card was available, so
    it is UNVERIFIED against a real file. The payload is a unix timestamp in
    milliseconds in square brackets followed by an NMEA sentence, and the
    talker is GN rather than GP -- which is exactly why this tool's old
    '$GPRMC' filter would have read nothing at all from this camera.
    """

    def __init__(self, record_directory: Path,
                 logger: logging.Logger | None = None) -> None:
        self._record_directory = record_directory
        self._logger = logger or logging.getLogger(__name__)

    def is_track_artifact(self, path: Path) -> bool:
        return path.suffix.lower() in (".gps", ".3gf")

    def track_for(self, base_filename: str, mode_letter: str,
                  video: Path) -> Track:
        payload = self._sidecar_payload(base_filename, mode_letter)
        if payload is None:
            payload = self._embedded_payload(video)
        if payload is None:
            return Track(points=())
        return Track(points=tuple(self._parse(payload)))

    def _sidecar_payload(self, base_filename: str,
                         mode_letter: str) -> bytes | None:
        sidecar = self._record_directory / ("%s_%s.gps"
                                            % (base_filename, mode_letter))
        try:
            return sidecar.read_bytes() if sidecar.is_file() else None
        except OSError as error:
            self._logger.warning("Cannot read BlackVue sidecar %s: %s",
                                 sidecar, error)
            return None

    def _embedded_payload(self, video: Path) -> bytes | None:
        try:
            if not video.is_file():
                return None
            for fourcc, offset, size in iter_top_level_boxes(video):
                if fourcc == _GPS_BOX:
                    return read_box_payload(video, offset, size)
        except OSError as error:
            self._logger.warning("Cannot read BlackVue video %s: %s",
                                 video, error)
        return None

    def _parse(self, payload: bytes):
        for line in payload.decode("utf-8", errors="ignore").splitlines():
            point = self._point_from(line)
            if point is not None:
                yield point

    def _point_from(self, line: str) -> TrackPoint | None:
        sentence = line[line.index("$"):] if "$" in line else ""
        fields = sentence.split(",")
        # Any talker, RMC only: GGA carries no speed and this tool needs it.
        if len(fields) < 10 or not fields[0].endswith("RMC"):
            return None
        if fields[2] != "A":
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


def _to_utc(date_field: str, time_field: str):
    from datetime import datetime
    try:
        return datetime.strptime(date_field + time_field.split(".")[0],
                                 "%d%m%y%H%M%S")
    except ValueError:
        return None
```

Move the `datetime` import to the top of the file rather than inside `_to_utc` before committing — it is written inline here only to keep the helper self-contained while reading.

- [ ] **Step 5: Export it**

Write `src/dashcam_exporter/infrastructure/adapters/blackvue/__init__.py`:

```python
from .blackvue_track_source import BlackvueTrackSource

__all__ = ["BlackvueTrackSource"]
```

- [ ] **Step 6: Run it and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.blackvue.test_blackvue_track_source -v`
Expected: PASS, 4 tests.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/blackvue tests/infrastructure/adapters/blackvue
git commit -m "$(cat <<'EOF'
Read BlackVue telemetry from a sidecar, or from inside the video

The camera changed regimes between generations: legacy models write
<base>_<type>.gps beside the clip -- mode letter, no direction letter, so
one file for both cameras -- and the DR900X writes no sidecar at all and
keeps the same text in a 'gps ' box. Sidecar first, container second.

The payload is a bracketed epoch followed by an NMEA sentence with a GN
talker. That single detail is the argument for the whole Track type: this
tool's old "$GPRMC" filter matches neither the bracket nor the talker, so
it would have read a card full of telemetry as a drive with no route.

Provenance is stated in the docstring and in the test module header rather
than implied: no BlackVue card was available, and these tests prove the
reader agrees with a writer built from the same documents. They do not
prove either matches a real camera.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: BlackVue layout and adapter

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_card_layout.py`
- Create: `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_adapter.py`
- Test: `tests/infrastructure/adapters/blackvue/test_blackvue_card_layout.py`

**Interfaces:**
- Produces: `BlackvueCardLayout(card_root: Path, logger=None)` implementing the five `CardLayout` methods, and `BlackvueAdapter(logger=None)` with `name == "blackvue"`.

**Layout facts:** everything in `/BlackVue/Record/`; filenames `YYYYMMDD_HHMMSS_[type][direction][flag].mp4`; direction `F`/`R`/`I`/`O`; sixteen type letters. Duration is not in the filename — segments are a fixed minute and rear files are trimmed to about 59 s. Front and rear share the timestamp exactly.

**Mode mapping** — the six-value enum against BlackVue's sixteen letters, with the letter kept in `source_mode`:

| Letters | `ClipMode` |
|---|---|
| `N` | `NORMAL` |
| `E`, `I`, `O`, `A`, `T`, `B` | `EVENT` |
| `P` | `PARKING` |
| `M` | `MANUAL` |
| anything else (`R`, `X`, `G`, `D`, `L`, `Y`, `F`) | `OTHER` |

`protected` is `True` for every mode except `N`, since those are the recordings the camera locks.

**Duration:** measured with ffprobe would mean invoking it from library code on every clip. Instead the layout declares 60 seconds, matching the manual's fixed segment length, and the `wall_seconds` and `playback_seconds` are equal. This is a documented approximation, not a measurement, and the docstring must say so — a rear file trimmed to 59 s will be described as 60.

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/blackvue/test_blackvue_card_layout.py`:

```python
"""A BlackVue card: one directory, mode and direction in the filename."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.blackvue import (
    BlackvueAdapter, BlackvueCardLayout)


def build_card(root: Path) -> Path:
    (root / "BlackVue/Record").mkdir(parents=True, exist_ok=True)
    (root / "BlackVue/Config").mkdir(parents=True, exist_ok=True)
    return root


def record(root: Path) -> Path:
    return root / "BlackVue/Record"


class BlackvueCardLayoutTest(unittest.TestCase):
    def test_pairs_front_and_rear_that_share_one_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_NF.mp4").touch()
            (record(card) / "20210127_155052_NR.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].timestamp, "20210127155052")
        self.assertEqual(clips[0].videos[Channel.FRONT].name,
                         "20210127_155052_NF.mp4")
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "20210127_155052_NR.mp4")

    def test_an_interior_camera_is_a_third_channel_not_a_second_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for name in ("20210127_155052_NF.mp4", "20210127_155052_NR.mp4",
                         "20210127_155052_NI.mp4"):
                (record(card) / name).touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(len(clips[0].videos), 3)
        self.assertEqual(clips[0].videos[Channel.INTERIOR].name,
                         "20210127_155052_NI.mp4")

    def test_parking_and_manual_modes_come_from_the_type_letter(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_PF.mp4").touch()
            (record(card) / "20210127_160000_MF.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual([c.mode for c in clips],
                         [ClipMode.PARKING, ClipMode.MANUAL])
        self.assertEqual([c.source_mode for c in clips], ["P", "M"])
        self.assertTrue(all(c.protected for c in clips))

    def test_an_upload_flagged_file_is_the_same_clip(self):
        # The third character is L or S, a cloud upload flag.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (record(card) / "20210127_155052_NFS.mp4").touch()

            clips = BlackvueCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].videos[Channel.FRONT].name,
                         "20210127_155052_NFS.mp4")

    def test_stamp_of_translates_the_filename_into_our_canonical_form(self):
        layout = BlackvueCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("20210127_155052_NF.mp4")),
                         "20210127155052")
        self.assertIsNone(layout.stamp_of(Path("readme.txt")))

    def test_the_import_root_is_the_record_directory_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            roots = BlackvueCardLayout(card).import_roots()

        self.assertEqual([r.relative_to(card).as_posix() for r in roots],
                         ["BlackVue/Record"])


class BlackvueAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_record_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            self.assertTrue(BlackvueAdapter().detect(card))

    def test_declines_a_dcim_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertFalse(BlackvueAdapter().detect(card))

    def test_is_named_for_the_camera(self):
        self.assertEqual(BlackvueAdapter().name, "blackvue")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.blackvue.test_blackvue_card_layout -v`
Expected: FAIL with `ImportError: cannot import name 'BlackvueCardLayout'`.

- [ ] **Step 3: Write the layout**

Create `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_card_layout.py`:

```python
import calendar
import logging
import re
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode, Track

from ..card_layout import CardLayout
from .blackvue_track_source import BlackvueTrackSource

RECORD_ROOT = "BlackVue/Record"

# YYYYMMDD_HHMMSS_[type][direction][upload flag].mp4
_NAME = re.compile(
    r"^(\d{8})_(\d{6})_([A-Z])([FRIO])([LS]?)\.mp4$", re.IGNORECASE)

_CHANNELS = {"F": Channel.FRONT, "R": Channel.REAR,
             "I": Channel.INTERIOR, "O": Channel.TELEPHOTO}

# Sixteen letters against six values. The letter is kept in source_mode, so
# collapsing here loses nothing a later rule could want back.
_MODES = {"N": ClipMode.NORMAL, "P": ClipMode.PARKING, "M": ClipMode.MANUAL,
          "E": ClipMode.EVENT, "I": ClipMode.EVENT, "O": ClipMode.EVENT,
          "A": ClipMode.EVENT, "T": ClipMode.EVENT, "B": ClipMode.EVENT}

# The manual is explicit: "Video segment length is fixed at 1 minute." It is
# not in the filename, so this is a declaration rather than a measurement --
# a rear file trimmed to 59 seconds to absorb the rear camera's start delay
# is described here as 60.
SEGMENT_SECONDS = 60


class BlackvueCardLayout(CardLayout):
    """A BlackVue card: every clip in one directory, distinguished by suffix.

    Front, rear and interior share a timestamp exactly, so pairing is by
    equality rather than by tolerance -- unlike DDPAI and VIOFO, whose
    cameras keep separate clocks.
    """

    def __init__(self, card_root: Path,
                 logger: logging.Logger | None = None) -> None:
        self._card_root = card_root
        self._record = card_root / RECORD_ROOT
        self._tracks = BlackvueTrackSource(self._record, logger)

    def clips(self) -> list[Clip]:
        if not self._record.is_dir():
            return []
        grouped: dict[tuple[str, str], dict[Channel, Path]] = {}
        for file in sorted(self._record.iterdir()):
            match = _NAME.match(file.name)
            if not match:
                continue
            stamp = match.group(1) + match.group(2)
            mode_letter = match.group(3).upper()
            channel = _CHANNELS[match.group(4).upper()]
            grouped.setdefault((stamp, mode_letter), {})[channel] = file
        return [self._to_clip(stamp, mode_letter, videos)
                for (stamp, mode_letter), videos in sorted(grouped.items())]

    def stamp_of(self, path: Path) -> str | None:
        match = _NAME.match(path.name)
        return match.group(1) + match.group(2) if match else None

    def track_for(self, clip: Clip) -> Track | None:
        track = self._tracks.track_for(
            "%s_%s" % (clip.timestamp[:8], clip.timestamp[8:]),
            clip.source_mode, clip.front)
        return None if track.is_empty else track

    def import_roots(self) -> tuple[Path, ...]:
        return (self._record,)

    def is_track_artifact(self, path: Path) -> bool:
        return self._tracks.is_track_artifact(path)

    def _to_clip(self, stamp: str, mode_letter: str,
                 videos: dict[Channel, Path]) -> Clip:
        parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        mode = _MODES.get(mode_letter, ClipMode.OTHER)
        return Clip(timestamp=stamp,
                    epoch_utc=calendar.timegm(parsed.timetuple()),
                    playback_seconds=SEGMENT_SECONDS,
                    wall_seconds=SEGMENT_SECONDS,
                    videos=dict(videos),
                    mode=mode,
                    source_mode=mode_letter,
                    protected=mode_letter != "N")
```

- [ ] **Step 4: Write the adapter**

Create `src/dashcam_exporter/infrastructure/adapters/blackvue/blackvue_adapter.py`:

```python
import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .blackvue_card_layout import RECORD_ROOT, BlackvueCardLayout


class BlackvueAdapter(ExporterAdapter):
    """BlackVue cards, recognised by their own top-level directory."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        return "blackvue"

    def detect(self, card_root: Path) -> bool:
        return (card_root / RECORD_ROOT).is_dir()

    def layout_for(self, card_root: Path) -> BlackvueCardLayout:
        return BlackvueCardLayout(card_root, self._logger)
```

- [ ] **Step 5: Export both**

Write `src/dashcam_exporter/infrastructure/adapters/blackvue/__init__.py`:

```python
from .blackvue_adapter import BlackvueAdapter
from .blackvue_card_layout import BlackvueCardLayout
from .blackvue_track_source import BlackvueTrackSource

__all__ = ["BlackvueAdapter", "BlackvueCardLayout", "BlackvueTrackSource"]
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.blackvue.test_blackvue_card_layout -v`
Expected: PASS, 9 tests.

- [ ] **Step 7: Run the delta gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 8: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/blackvue tests/infrastructure/adapters/blackvue
git commit -m "$(cat <<'EOF'
Support a camera that files every clip in one directory

BlackVue was the camera the old contract could not have been given: front,
rear and interior share a directory and a timestamp, distinguished only by
a letter in the name, so discover_clips(front_directory, rear_directory)
had nothing to receive. Under the new contract it is an ordinary layout.

Pairing is by equality here, deliberately. DDPAI and VIOFO need a tolerance
because their two cameras keep separate clocks; BlackVue writes one
timestamp for every channel, and a tolerance would invent matches between
adjacent minutes.

Duration is declared, not measured: the manual fixes segments at one minute
and the filename does not carry it. The docstring says so plainly, because
a rear file trimmed to 59 seconds is described here as 60 and someone will
eventually need to know why.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Novatek GPS reader

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/viofo/__init__.py`
- Create: `src/dashcam_exporter/infrastructure/adapters/viofo/novatek_gps_reader.py`
- Test: `tests/infrastructure/adapters/viofo/test_novatek_gps_reader.py`
- Create: `tests/infrastructure/adapters/viofo/__init__.py`

**Interfaces:**
- Produces: `NovatekGpsReader(logger=None)` with `read(video: Path) -> Track`, and module-level `pack_record(...)` used by the simulator so writer and reader share one definition of the layout.

**Format facts, from Sergei's extractor and EgorKin's fork — no VIOFO file was available:**
- Payload sits in top-level `free` boxes whose first four bytes are the ASCII magic `GPS `.
- A uint32 at offset 12 of the payload discriminates firmware variants: `0x58` implies the record starts at `0x30`; `0x3F0` and `0x2C` imply `0x10`; default `0x30`.
- Record, little-endian from that offset: six uint32 `hour, minute, second, year, month, day`; then `active` (one byte, `A` for a fix), latitude hemisphere, longitude hemisphere, one unknown byte; then four float32 `latitude, longitude, speed_knots, course`.
- Coordinates are `DDDmm.mmmm` hybrid degrees-and-minutes, not decimal degrees.

The reader implements all three offsets. Only the `0x58`/`0x30` variant is exercised, because that is the one the simulator writes — the others are transcribed from the extractor's source and are **untested**, which the docstring must say.

- [ ] **Step 1: Create the packages**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p src/dashcam_exporter/infrastructure/adapters/viofo tests/infrastructure/adapters/viofo
touch src/dashcam_exporter/infrastructure/adapters/viofo/__init__.py tests/infrastructure/adapters/viofo/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/infrastructure/adapters/viofo/test_novatek_gps_reader.py`:

```python
"""The Novatek freeGPS blob, read back from what pack_record writes.

Provenance warning: no VIOFO file was available. The layout is transcribed
from Sergei's nvtk_mp42gpx and EgorKin's fork, and reader and writer here
share one definition of it -- so these tests prove self-consistency, not
that the layout matches a real camera.
"""

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.viofo import (
    NovatekGpsReader, pack_record)


def free_box(payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + b"free" + payload


def other_box(fourcc: bytes, size: int) -> bytes:
    return struct.pack(">I", 8 + size) + fourcc + b"\x00" * size


class NovatekGpsReaderTest(unittest.TestCase):
    def test_reads_back_a_record_this_project_wrote(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.412755, 121.043745, 20.0, 30.2))
                + other_box(b"mdat", 8))

            track = NovatekGpsReader().read(video)

        self.assertEqual(len(track.points), 1)
        self.assertAlmostEqual(track.points[0].lat, 14.412755, places=4)
        self.assertAlmostEqual(track.points[0].lon, 121.043745, places=4)
        self.assertAlmostEqual(track.points[0].kmh, 37.04, places=2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 5, 16, 12, 0, 1))

    def test_every_free_box_in_the_file_contributes_its_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.4, 121.0, 10.0, 0.0))
                + other_box(b"mdat", 8)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 2),
                                       14.5, 121.1, 12.0, 0.0)))

            track = NovatekGpsReader().read(video)

        self.assertEqual(len(track.points), 2)
        self.assertEqual(track.points[0].at_utc,
                         datetime(2026, 5, 16, 12, 0, 1))

    def test_a_free_box_without_the_magic_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(other_box(b"ftyp", 4)
                              + free_box(b"padding, not telemetry"))

            track = NovatekGpsReader().read(video)

        self.assertTrue(track.is_empty)

    def test_a_record_with_no_fix_is_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       14.4, 121.0, 10.0, 0.0, active=False)))

            track = NovatekGpsReader().read(video)

        self.assertTrue(track.is_empty)

    def test_a_southern_and_western_fix_comes_back_negative(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "2026_0516_120000_001F.MP4"
            video.write_bytes(
                other_box(b"ftyp", 4)
                + free_box(pack_record(datetime(2026, 5, 16, 12, 0, 1),
                                       -33.868820, -151.209290, 5.0, 0.0)))

            track = NovatekGpsReader().read(video)

        self.assertAlmostEqual(track.points[0].lat, -33.868820, places=4)
        self.assertAlmostEqual(track.points[0].lon, -151.209290, places=4)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.viofo.test_novatek_gps_reader -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/infrastructure/adapters/viofo/novatek_gps_reader.py`:

```python
import logging
import struct
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Track, TrackPoint
from dashcam_exporter.infrastructure.media.mp4_boxes import (
    iter_top_level_boxes, read_box_payload)

KNOTS_TO_KMH = 1.852

MAGIC = b"GPS "

# A uint32 at offset 12 tells the firmware variant apart. Transcribed from
# EgorKin's fork of Sergei's extractor, whose comments date each addition:
# 0x58 arrived with the A229, 0x2C with a later A229 firmware, 0x3F0 with
# the A129 Plus Duo. Only the 0x58 path is exercised by any test here,
# because it is the one this project's own writer produces. The other two
# are UNTESTED.
_VARIANT_OFFSETS = {0x58: 0x30, 0x2C: 0x10, 0x3F0: 0x10}
_DEFAULT_OFFSET = 0x30
_DISCRIMINATOR_AT = 12
_RECORD = struct.Struct("<6I4c4f")


def pack_record(at_utc: datetime, lat: float, lon: float, knots: float,
                course: float, active: bool = True) -> bytes:
    """Build one freeGPS payload, so writer and reader share one layout.

    Exported for the card simulator deliberately. Two transcriptions of a
    binary format drift; one definition used from both ends cannot. It also
    means a mistake here is invisible to every test -- which is stated in
    the module docstring of those tests rather than left to be discovered.
    """
    body = _RECORD.pack(
        at_utc.hour, at_utc.minute, at_utc.second,
        at_utc.year, at_utc.month, at_utc.day,
        b"A" if active else b"V",
        b"N" if lat >= 0 else b"S",
        b"E" if lon >= 0 else b"W",
        b"\x00",
        _to_hybrid(lat), _to_hybrid(lon), knots, course)
    head = bytearray(MAGIC + b"\x00" * (_DEFAULT_OFFSET - len(MAGIC)))
    struct.pack_into("<I", head, _DISCRIMINATOR_AT, 0x58)
    return bytes(head) + body


class NovatekGpsReader:
    """GPS from the free boxes Novatek-based cameras interleave in the MP4.

    Provenance: transcribed from Sergei's nvtk_mp42gpx and EgorKin's fork.
    No VIOFO file was available, so this is UNVERIFIED against a real
    camera; it is verified only against pack_record above, which was written
    from the same source.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def read(self, video: Path) -> Track:
        points: list[TrackPoint] = []
        try:
            if not video.is_file():
                return Track(points=())
            for fourcc, offset, size in iter_top_level_boxes(video):
                if fourcc != "free" or size < _DEFAULT_OFFSET:
                    continue
                payload = read_box_payload(video, offset, size)
                if not payload.startswith(MAGIC):
                    continue
                point = self._point_from(payload)
                if point is not None:
                    points.append(point)
        except OSError as error:
            self._logger.warning("Cannot read VIOFO video %s: %s", video, error)
        return Track(points=tuple(sorted(points, key=lambda p: p.at_utc)))

    def _point_from(self, payload: bytes) -> TrackPoint | None:
        start = self._record_offset(payload)
        if start + _RECORD.size > len(payload):
            return None
        (hour, minute, second, year, month, day,
         active, lat_hemisphere, lon_hemisphere, _unknown,
         lat, lon, knots, _course) = _RECORD.unpack_from(payload, start)
        if active != b"A":
            return None
        try:
            at_utc = datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
        return TrackPoint(_from_hybrid(lat, lat_hemisphere),
                          _from_hybrid(lon, lon_hemisphere),
                          knots * KNOTS_TO_KMH, at_utc)

    def _record_offset(self, payload: bytes) -> int:
        if len(payload) < _DISCRIMINATOR_AT + 4:
            return _DEFAULT_OFFSET
        variant = struct.unpack_from("<I", payload, _DISCRIMINATOR_AT)[0]
        return _VARIANT_OFFSETS.get(variant, _DEFAULT_OFFSET)


def _to_hybrid(degrees: float) -> float:
    """Decimal degrees to the DDDmm.mmmm the format stores."""
    value = abs(degrees)
    whole = int(value)
    return whole * 100 + (value - whole) * 60


def _from_hybrid(value: float, hemisphere: bytes) -> float:
    whole = int(abs(value) // 100)
    minutes = abs(value) - whole * 100
    result = whole + minutes / 60.0
    return -result if hemisphere in (b"S", b"W") else result
```

- [ ] **Step 5: Export it**

Write `src/dashcam_exporter/infrastructure/adapters/viofo/__init__.py`:

```python
from .novatek_gps_reader import NovatekGpsReader, pack_record

__all__ = ["NovatekGpsReader", "pack_record"]
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.viofo.test_novatek_gps_reader -v`
Expected: PASS, 5 tests.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/viofo tests/infrastructure/adapters/viofo
git commit -m "$(cat <<'EOF'
Read the Novatek telemetry blob, and say plainly it is unverified

This is the format that convergence on shared silicon produced: many
unrelated brands ship Novatek reference firmware and therefore the same
free-box blob, which is the strongest argument for a layout HOLDING a GPS
source rather than being one. The reader can serve any of them.

The payload offset moves with firmware -- a uint32 at offset 12 tells the
variants apart -- and all three documented offsets are implemented while
only one is exercised, because only one is what our writer produces.

pack_record is exported for the simulator on purpose. Two transcriptions of
a binary layout drift apart; one definition used from both ends cannot. The
cost is that a misreading of the format is invisible to every test, and
that cost is written into the test module header rather than left for
somebody to work out later. No VIOFO file was available to check against.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: VIOFO layout and adapter

**Files:**
- Create: `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_card_layout.py`
- Create: `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_adapter.py`
- Test: `tests/infrastructure/adapters/viofo/test_viofo_card_layout.py`

**Interfaces:**
- Produces: `ViofoCardLayout(card_root: Path, pair_tolerance_seconds: int = 6, logger=None)` and `ViofoAdapter(pair_tolerance_seconds: int = 6, logger=None)` with `name == "viofo"`.

**Layout facts:** `DCIM/Movie/` for loop recording, `DCIM/Movie/RO/` for locked, `DCIM/Movie/Parking/` for parking. Filenames `YYYY_MMDD_HHMMSS_<seq>[P|E]?[F|R|I|T].MP4` with the sequence 3 to 8 digits. Duration is a menu setting and appears nowhere.

**Pairing is fuzzy and that is the point.** Front and rear timestamps drift by a second or more and sequence numbers drift independently. The commercial reference player searches ±6 seconds, which is where the default tolerance comes from. Pairing is nearest-timestamp within tolerance, never filename reconstruction.

**Protection comes from the directory**, not the name — `RO` has no filename marker, so a clip's `protected` flag is the only place that survives the copy off the card.

**Duration:** declared 60 seconds as a documented default, same approximation as BlackVue, since the menu setting is invisible. `source_mode` carries the filename marker (`P`, `E`, or empty).

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/viofo/test_viofo_card_layout.py`:

```python
"""A VIOFO card: mode in the folder and the name, and clocks that drift."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.domain import Channel, ClipMode
from dashcam_exporter.infrastructure.adapters.viofo import (
    ViofoAdapter, ViofoCardLayout)


def build_card(root: Path) -> Path:
    for relative in ("DCIM/Movie", "DCIM/Movie/RO", "DCIM/Movie/Parking",
                     "DCIM/Photo"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


class ViofoCardLayoutTest(unittest.TestCase):
    def test_pairs_cameras_whose_clocks_drifted_apart(self):
        # Owners report the rear file saving a second later, and the
        # sequence numbers running independently once a camera is unplugged.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/2026_0516_120000_001F.MP4").touch()
            (card / "DCIM/Movie/2026_0516_120002_004R.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].videos[Channel.REAR].name,
                         "2026_0516_120002_004R.MP4")

    def test_a_rear_file_beyond_the_tolerance_is_its_own_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/2026_0516_120000_001F.MP4").touch()
            (card / "DCIM/Movie/2026_0516_120030_004R.MP4").touch()

            clips = ViofoCardLayout(card, pair_tolerance_seconds=6).clips()

        self.assertEqual(len(clips), 2)

    def test_a_three_channel_clip_keeps_all_of_its_cameras(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            for name in ("2020_1018_170010_062PF.MP4",
                         "2020_1018_170010_063PI.MP4",
                         "2020_1018_170010_064PR.MP4"):
                (card / "DCIM/Movie/Parking" / name).touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertEqual(len(clips[0].videos), 3)
        self.assertEqual(clips[0].mode, ClipMode.PARKING)
        self.assertEqual(clips[0].source_mode, "P")

    def test_locked_clips_are_marked_from_the_folder_they_sit_in(self):
        # RO has no filename marker at all. The flag is the only place this
        # survives being copied off the card.
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/RO/2026_0516_120000_001F.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].protected)

    def test_an_event_marker_in_the_name_is_read_as_an_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))
            (card / "DCIM/Movie/RO/2026_0618_203643_0001EF.MP4").touch()

            clips = ViofoCardLayout(card).clips()

        self.assertEqual(clips[0].mode, ClipMode.EVENT)
        self.assertEqual(clips[0].source_mode, "E")

    def test_stamp_of_translates_the_filename_into_our_canonical_form(self):
        layout = ViofoCardLayout(Path("/nowhere"))

        self.assertEqual(layout.stamp_of(Path("2026_0516_120000_001F.MP4")),
                         "20260516120000")
        self.assertEqual(layout.stamp_of(Path("2020_1018_170010_062PF.MP4")),
                         "20201018170010")
        self.assertIsNone(layout.stamp_of(Path("readme.txt")))

    def test_a_sequence_number_of_any_documented_width_is_accepted(self):
        layout = ViofoCardLayout(Path("/nowhere"))

        for name in ("2026_0516_120000_001F.MP4",
                     "2026_0618_200300_0001F.MP4",
                     "2026_0508_104020_001234F.MP4"):
            self.assertIsNotNone(layout.stamp_of(Path(name)), name)


class ViofoAdapterTest(unittest.TestCase):
    def test_claims_a_card_with_the_movie_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = build_card(Path(temporary))

            self.assertTrue(ViofoAdapter().detect(card))

    def test_declines_a_ddpai_card_that_also_has_dcim(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/200video/front").mkdir(parents=True)

            self.assertFalse(ViofoAdapter().detect(card))

    def test_is_named_for_the_camera(self):
        self.assertEqual(ViofoAdapter().name, "viofo")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.viofo.test_viofo_card_layout -v`
Expected: FAIL with `ImportError: cannot import name 'ViofoCardLayout'`.

- [ ] **Step 3: Write the layout**

Create `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_card_layout.py`:

```python
import calendar
import logging
import re
from datetime import datetime
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode, Track

from ..card_layout import CardLayout
from .novatek_gps_reader import NovatekGpsReader

MOVIE_ROOT = "DCIM/Movie"
LOCKED_DIRECTORY = "RO"
PARKING_DIRECTORY = "Parking"

# YYYY_MMDD_HHMMSS_<seq 3-8>[P|E]?[F|R|I|T].MP4
_NAME = re.compile(
    r"^(\d{4})_(\d{2})(\d{2})_(\d{6})_(\d{3,8})([PE]?)([FRIT])\.MP4$",
    re.IGNORECASE)

_CHANNELS = {"F": Channel.FRONT, "R": Channel.REAR,
             "I": Channel.INTERIOR, "T": Channel.TELEPHOTO}
_MARKER_MODES = {"P": ClipMode.PARKING, "E": ClipMode.EVENT}

# The menu offers 1, 2, 3, 5 or 10 minutes and the filename says nothing, so
# this is a declared default rather than a measurement.
SEGMENT_SECONDS = 60


class ViofoCardLayout(CardLayout):
    """A VIOFO card: mode in the folder AND the name, and drifting clocks.

    Pairing is nearest-timestamp within a tolerance, never reconstruction of
    a sibling filename. Owners report the rear file saving a second later
    than the front, and the sequence numbers running independently once one
    camera has been unplugged, so the two names for one moment agree about
    nothing except roughly when it was.
    """

    def __init__(self, card_root: Path, pair_tolerance_seconds: int = 6,
                 logger: logging.Logger | None = None) -> None:
        self._card_root = card_root
        self._movie = card_root / MOVIE_ROOT
        self._tolerance = pair_tolerance_seconds
        self._gps = NovatekGpsReader(logger)

    def clips(self) -> list[Clip]:
        found = [self._describe(path) for path in self._video_files()]
        fronts = sorted((f for f in found if f[1] is Channel.FRONT),
                        key=lambda f: f[0])
        others = [f for f in found if f[1] is not Channel.FRONT]
        return [self._to_clip(front, others) for front in fronts]

    def stamp_of(self, path: Path) -> str | None:
        match = _NAME.match(path.name)
        if not match:
            return None
        return "%s%s%s%s" % (match.group(1), match.group(2), match.group(3),
                             match.group(4))

    def track_for(self, clip: Clip) -> Track | None:
        track = self._gps.read(clip.front)
        return None if track.is_empty else track

    def import_roots(self) -> tuple[Path, ...]:
        return (self._movie,)

    def is_track_artifact(self, path: Path) -> bool:
        # There is no sidecar: the telemetry is inside the video, so the
        # videos themselves are the only artifacts carrying a route.
        return _NAME.match(path.name) is not None

    def _video_files(self) -> list[Path]:
        if not self._movie.is_dir():
            return []
        return [path for path in sorted(self._movie.rglob("*"))
                if path.is_file() and _NAME.match(path.name)]

    def _describe(self, path: Path):
        """(stamp, channel, marker, protected, path) for one video file."""
        match = _NAME.match(path.name)
        stamp = "%s%s%s%s" % (match.group(1), match.group(2), match.group(3),
                              match.group(4))
        marker = match.group(6).upper()
        parents = {parent.name for parent in path.parents}
        protected = LOCKED_DIRECTORY in parents
        if PARKING_DIRECTORY in parents and not marker:
            marker = "P"
        return stamp, _CHANNELS[match.group(7).upper()], marker, protected, path

    def _to_clip(self, front, others) -> Clip:
        stamp, _channel, marker, protected, path = front
        epoch = self._epoch(stamp)
        videos = {Channel.FRONT: path}
        for other in others:
            if abs(self._epoch(other[0]) - epoch) <= self._tolerance:
                videos.setdefault(other[1], other[4])
        return Clip(timestamp=stamp, epoch_utc=epoch,
                    playback_seconds=SEGMENT_SECONDS,
                    wall_seconds=SEGMENT_SECONDS,
                    videos=videos,
                    mode=_MARKER_MODES.get(marker, ClipMode.NORMAL),
                    source_mode=marker,
                    protected=protected)

    @staticmethod
    def _epoch(stamp: str) -> int:
        parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        return calendar.timegm(parsed.timetuple())
```

Note the second test in this task: a rear file 30 seconds away becomes its own clip. That works because `clips()` builds one clip per **front** file, and a rear file that pairs with nothing is not currently emitted at all. Before finishing this task, verify that assertion actually passes; if it does not, the fix is to emit unpaired non-front files as their own clips rather than to weaken the test.

- [ ] **Step 4: Write the adapter**

Create `src/dashcam_exporter/infrastructure/adapters/viofo/viofo_adapter.py`:

```python
import logging
from pathlib import Path

from ..exporter_adapter import ExporterAdapter
from .viofo_card_layout import MOVIE_ROOT, ViofoCardLayout


class ViofoAdapter(ExporterAdapter):
    """VIOFO cards, recognised by DCIM/Movie rather than by DCIM.

    DDPAI lives under DCIM too. Detecting on DCIM alone would make the
    registry raise on every card in the house, correctly and uselessly.
    """

    def __init__(self, pair_tolerance_seconds: int = 6,
                 logger: logging.Logger | None = None) -> None:
        self._pair_tolerance_seconds = pair_tolerance_seconds
        self._logger = logger

    @property
    def name(self) -> str:
        return "viofo"

    def detect(self, card_root: Path) -> bool:
        return (card_root / MOVIE_ROOT).is_dir()

    def layout_for(self, card_root: Path) -> ViofoCardLayout:
        return ViofoCardLayout(card_root, self._pair_tolerance_seconds,
                               self._logger)
```

- [ ] **Step 5: Export both**

Write `src/dashcam_exporter/infrastructure/adapters/viofo/__init__.py`:

```python
from .novatek_gps_reader import NovatekGpsReader, pack_record
from .viofo_adapter import ViofoAdapter
from .viofo_card_layout import ViofoCardLayout

__all__ = ["NovatekGpsReader", "ViofoAdapter", "ViofoCardLayout",
           "pack_record"]
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.viofo.test_viofo_card_layout -v`
Expected: PASS, 10 tests.

- [ ] **Step 7: Run the delta gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 8: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters/viofo tests/infrastructure/adapters/viofo
git commit -m "$(cat <<'EOF'
Pair VIOFO cameras by when they recorded, never by what they are called

The two files for one moment agree about nothing except roughly the time:
owners report the rear saving a second later, and the sequence numbers
running independently once a camera has been unplugged. The commercial
reference player searches six seconds and four file numbers, which is where
this tolerance comes from. Reconstructing the sibling name from the stem
would have looked correct and silently dropped half the rear footage.

Locked recordings are the other lesson. VIOFO marks them by putting the
file in RO and by nothing else -- no filename marker at all -- so the flag
this layout sets is the only place that fact survives being copied off the
card. It is the clearest argument in the whole exercise for transforming at
import rather than copying verbatim.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Register all three and prove they stay distinguishable

**Files:**
- Modify: `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py` (the `default_registry` function)
- Modify: `src/dashcam_exporter/infrastructure/adapters/__init__.py`
- Test: `tests/infrastructure/adapters/test_default_registry.py`

**Interfaces:**
- Produces: `default_registry()` returning a registry of `DdpaiAdapter`, `BlackvueAdapter`, `ViofoAdapter`.

**Why this is its own task:** the registry raising on ambiguity is only a real guarantee once more than one adapter exists. DDPAI and VIOFO both live under `DCIM/`, so this is the test that would have caught a `detect()` written as "has a DCIM directory".

- [ ] **Step 1: Write the failing test**

Create `tests/infrastructure/adapters/test_default_registry.py`:

```python
"""Three cameras, three layouts, and no card claimed twice."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.infrastructure.adapters import (
    NoAdapterFound, default_registry)

CARDS = {
    "ddpai": "DCIM/200video/front",
    "blackvue": "BlackVue/Record",
    "viofo": "DCIM/Movie",
}


class DefaultRegistryTest(unittest.TestCase):
    def test_every_shipped_adapter_is_registered(self):
        self.assertEqual(sorted(default_registry().names),
                         ["blackvue", "ddpai", "viofo"])

    def test_each_card_resolves_to_exactly_one_adapter(self):
        for expected, marker in CARDS.items():
            with self.subTest(camera=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    card = Path(temporary)
                    (card / marker).mkdir(parents=True)

                    self.assertEqual(
                        default_registry().detect(card).name, expected)

    def test_a_dcim_card_belonging_to_neither_is_claimed_by_neither(self):
        # The test that catches a detect() written as "has a DCIM folder":
        # DDPAI and VIOFO share that parent and nothing else.
        with tempfile.TemporaryDirectory() as temporary:
            card = Path(temporary)
            (card / "DCIM/100CANON").mkdir(parents=True)

            with self.assertRaises(NoAdapterFound):
                default_registry().detect(card)

    def test_an_empty_card_is_claimed_by_nobody(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(NoAdapterFound):
                default_registry().detect(Path(temporary))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_default_registry -v`
Expected: FAIL — `test_every_shipped_adapter_is_registered` reports only `["ddpai"]`.

- [ ] **Step 3: Register the two new adapters**

In `src/dashcam_exporter/infrastructure/adapters/adapter_registry.py`, replace the body of `default_registry` with:

```python
def default_registry() -> AdapterRegistry:
    """Every adapter shipped with the tool.

    Imported inside the function rather than at module scope so the registry
    module stays free of any particular camera.
    """
    from .blackvue.blackvue_adapter import BlackvueAdapter
    from .ddpai.ddpai_adapter import DdpaiAdapter
    from .viofo.viofo_adapter import ViofoAdapter
    return AdapterRegistry([DdpaiAdapter(), BlackvueAdapter(), ViofoAdapter()])
```

- [ ] **Step 4: Export the new adapters**

Replace `src/dashcam_exporter/infrastructure/adapters/__init__.py` with:

```python
from .adapter_registry import (AdapterRegistry, AmbiguousCard, NoAdapterFound,
                               default_registry)
from .card_layout import CardLayout
from .exporter_adapter import ExporterAdapter
from .blackvue.blackvue_adapter import BlackvueAdapter
from .ddpai.ddpai_adapter import DdpaiAdapter
from .viofo.viofo_adapter import ViofoAdapter
from .ddpai_data_adapter import DdpaiDataAdapter

__all__ = ["AdapterRegistry", "AmbiguousCard", "BlackvueAdapter", "CardLayout",
           "DdpaiAdapter", "DdpaiDataAdapter", "ExporterAdapter",
           "NoAdapterFound", "ViofoAdapter", "default_registry"]
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.test_default_registry -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Run the delta gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/infrastructure/adapters tests/infrastructure/adapters/test_default_registry.py
git commit -m "$(cat <<'EOF'
Ship three adapters, and make the ambiguity rule mean something

A registry that raises when two adapters claim one card is a promise about
a situation that could not arise while only one adapter existed. Now it
can: DDPAI and VIOFO are both DCIM cards and differ one level down, so the
test that a Canon DCIM folder is claimed by neither is the test that would
have caught either detect() written as "has a DCIM directory".

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The card simulator

**Files:**
- Create: `tools/simulator/__init__.py`
- Create: `tools/simulator/card_simulator.py`
- Create: `tools/simulator/ddpai_simulator.py`
- Create: `tools/simulator/blackvue_simulator.py`
- Create: `tools/simulator/viofo_simulator.py`
- Create: `tools/simulate_card.py`

**Interfaces:**
- Produces: `CardSimulator` ABC with `name`, `write(card_root: Path, clips: int) -> None`; three implementations; and a CLI `python3 tools/simulate_card.py <camera> <destination> [clips]`.

**Not tested by the suite.** It shells out to ffmpeg, which is slow and external. It is exercised by task 8's calibration script instead. This mirrors plan 1's decision about `calibrate_ddpai.py` and for the same reason.

- [ ] **Step 1: Write the base and the clip writer**

```bash
cd ~/dev/dashcam-exporter-adapter-change && mkdir -p tools/simulator && touch tools/simulator/__init__.py
```

Create `tools/simulator/card_simulator.py`:

```python
"""Writing card trees that look like the real thing.

The DDPAI simulator is calibrated: a real card's directory skeleton and a
real import's filename grammar were both read off this machine. The
BlackVue and VIOFO simulators are NOT. They are built from manuals and from
open-source parsers, and the adapters that read them were built from the
same documents -- so agreement between the two proves they were written
consistently, and nothing about a real camera.
"""

import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

CLIP_SECONDS = 2
FRAME_SIZE = "320x180"


class CardSimulator(ABC):
    """Writes one camera's idea of a card to an empty directory."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Matches the adapter name this card should be detected as."""

    @abstractmethod
    def write(self, card_root: Path, clips: int) -> None:
        """Create the tree, the videos and the telemetry."""

    def _write_clip(self, path: Path, seed: int) -> None:
        """A real, playable MP4 -- short, small, and visibly numbered.

        Real files rather than touched empty ones because an adapter that
        reads telemetry out of a container cannot be exercised by a file
        with no container in it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi",
             "-i", "testsrc=size=%s:rate=10:duration=%d"
                   % (FRAME_SIZE, CLIP_SECONDS),
             "-metadata", "comment=simulated clip %d" % seed,
             "-pix_fmt", "yuv420p", str(path)],
            check=True)

    @staticmethod
    def _clip_times(clips: int, start: datetime,
                    step_seconds: int = 60) -> list[datetime]:
        return [start + timedelta(seconds=step_seconds * index)
                for index in range(clips)]
```

- [ ] **Step 2: Write the DDPAI simulator**

Create `tools/simulator/ddpai_simulator.py`:

```python
import io
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from .card_simulator import CLIP_SECONDS, CardSimulator

# The skeleton was read off a real card, including the directories this tool
# never opens. They are written because a simulator that only creates what
# the adapter reads cannot catch an import that copies too much.
SKELETON = ("DCIM/200video/front", "DCIM/200video/rear",
            "DCIM/201photo/front", "DCIM/201photo/rear", "DCIM/201photo/tmp",
            "DCIM/202thumb/front", "DCIM/202thumb/rear", "DCIM/202thumb/tmp",
            "DCIM/203gps/tar", "DCIM/203gps/tmp", "DCIM/207log/tmp")

# Local wall clock in the filenames, UTC in the telemetry. Eight hours on
# the card this was calibrated against, and reproducing it is the point:
# a simulator that used one clock for both would have passed the bug that
# calibration caught.
UTC_OFFSET_HOURS = 8
ARCHIVE_CLIPS = 9


class DdpaiSimulator(CardSimulator):
    """A DDPAI card, calibrated against a real one."""

    @property
    def name(self) -> str:
        return "ddpai"

    def write(self, card_root: Path, clips: int) -> None:
        for relative in SKELETON:
            (card_root / relative).mkdir(parents=True, exist_ok=True)
        start = datetime(2026, 8, 6, 17, 5, 29)
        times = self._clip_times(clips, start)
        for index, at in enumerate(times):
            stamp = at.strftime("%Y%m%d%H%M%S")
            self._write_clip(
                card_root / "DCIM/200video/front" / ("%s_%04d.mp4"
                                                     % (stamp, 60)), index)
            # The rear camera runs a second behind, as the real one does.
            rear_stamp = (at + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
            self._write_clip(
                card_root / "DCIM/200video/rear" / ("%s_%04d_A.mp4"
                                                    % (rear_stamp, 60)), index)
        self._write_archives(card_root, times)
        self._write_placeholders(card_root)

    def _write_archives(self, card_root: Path, times) -> None:
        tar_directory = card_root / "DCIM/203gps/tar"
        for offset in range(0, len(times), ARCHIVE_CLIPS):
            batch = times[offset:offset + ARCHIVE_CLIPS]
            span = 60 * len(batch)
            name = "%s_%04d.git" % (batch[0].strftime("%Y%m%d%H%M%S"), span)
            with tarfile.open(tar_directory / name, "w") as handle:
                for at in batch:
                    stamp = at.strftime("%Y%m%d%H%M%S")
                    payload = self._nmea(at).encode()
                    info = tarfile.TarInfo("%s_%04d.gpx" % (stamp, 60))
                    info.size = len(payload)
                    handle.addfile(info, io.BytesIO(payload))

    def _nmea(self, at) -> str:
        utc = at - timedelta(hours=UTC_OFFSET_HOURS)
        lines = []
        for second in range(CLIP_SECONDS * 5):
            moment = utc + timedelta(seconds=second)
            lines.append(
                "$GPRMC,%s.000,A,1424.%05d,N,12102.%05d,E,7.12,30.23,%s,,,A,V*3D"
                % (moment.strftime("%H%M%S"), 76532 + second,
                   62468 + second, moment.strftime("%d%m%y")))
        return "\n".join(lines) + "\n"

    def _write_placeholders(self, card_root: Path) -> None:
        """The 73 pre-allocated slots a freshly formatted card carries.

        Identical size, one mtime, stamps counting up from an unset clock.
        Written because an adapter that opens them produces a warning per
        file, and nothing but a real card would have taught us they exist.
        """
        tmp = card_root / "DCIM/203gps/tar/tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        base = datetime(1970, 1, 1, 0, 45, 10)
        for index in range(3):
            at = base + timedelta(seconds=100 * index)
            name = "%s_0100_T.git" % at.strftime("%Y%m%d%H%M%S")
            (tmp / name).write_bytes(b"\x00" * 4096)
```

- [ ] **Step 3: Write the BlackVue simulator**

Create `tools/simulator/blackvue_simulator.py`:

```python
import struct
from datetime import datetime, timedelta
from pathlib import Path

from .card_simulator import CLIP_SECONDS, CardSimulator

RECORD = "BlackVue/Record"


class BlackvueSimulator(CardSimulator):
    """A BlackVue card. UNVERIFIED -- built from manuals, not from a card.

    Half the clips get a .gps sidecar and half get the telemetry appended as
    a 'gps ' box, because both regimes exist in the wild and an adapter that
    only ever meets one of them has not been tested.
    """

    @property
    def name(self) -> str:
        return "blackvue"

    def write(self, card_root: Path, clips: int) -> None:
        record = card_root / RECORD
        record.mkdir(parents=True, exist_ok=True)
        (card_root / "BlackVue/Config").mkdir(parents=True, exist_ok=True)
        for index, at in enumerate(self._clip_times(
                clips, datetime(2021, 1, 27, 15, 50, 52))):
            base = at.strftime("%Y%m%d_%H%M%S")
            mode = "N" if index % 3 else "P"
            front = record / ("%s_%sF.mp4" % (base, mode))
            self._write_clip(front, index)
            self._write_clip(record / ("%s_%sR.mp4" % (base, mode)), index)
            payload = self._sidecar(at).encode()
            if index % 2:
                (record / ("%s_%s.gps" % (base, mode))).write_bytes(payload)
            else:
                self._append_box(front, b"gps ", payload + b"\x00")

    def _sidecar(self, at: datetime) -> str:
        lines = []
        for second in range(CLIP_SECONDS * 5):
            moment = at + timedelta(seconds=second)
            epoch_ms = int(moment.timestamp() * 1000)
            lines.append(
                "[%d]$GNRMC,%s.00,A,4529.%05d,N,07337.%05d,W,"
                "6.225,35.34,%s,,,A*52"
                % (epoch_ms, moment.strftime("%H%M%S"), 87489 + second,
                   1215 + second, moment.strftime("%d%m%y")))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _append_box(video: Path, fourcc: bytes, payload: bytes) -> None:
        with video.open("ab") as handle:
            handle.write(struct.pack(">I", 8 + len(payload)) + fourcc + payload)
```

- [ ] **Step 4: Write the VIOFO simulator**

Create `tools/simulator/viofo_simulator.py`:

```python
import struct
from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.viofo import pack_record

from .card_simulator import CLIP_SECONDS, CardSimulator

MOVIE = "DCIM/Movie"


class ViofoSimulator(CardSimulator):
    """A VIOFO card. UNVERIFIED -- built from manuals and extractor source.

    The telemetry is written with the adapter's own pack_record, so this
    proves reader and writer agree and nothing more. That is stated here
    because it is the kind of thing a green test run makes easy to forget.
    """

    @property
    def name(self) -> str:
        return "viofo"

    def write(self, card_root: Path, clips: int) -> None:
        movie = card_root / MOVIE
        for relative in (movie, movie / "RO", movie / "Parking",
                         card_root / "DCIM/Photo"):
            relative.mkdir(parents=True, exist_ok=True)
        for index, at in enumerate(self._clip_times(
                clips, datetime(2026, 5, 16, 12, 0, 0))):
            directory, marker = self._destination(movie, index)
            base = at.strftime("%Y_%m%d_%H%M%S")
            front = directory / ("%s_%03d%sF.MP4" % (base, index + 1, marker))
            self._write_clip(front, index)
            # The rear camera saves two seconds later and counts its own
            # sequence numbers, exactly as owners report.
            rear_at = at + timedelta(seconds=2)
            self._write_clip(
                directory / ("%s_%03d%sR.MP4"
                             % (rear_at.strftime("%Y_%m%d_%H%M%S"),
                                index + 4, marker)), index)
            self._append_gps(front, at)

    @staticmethod
    def _destination(movie: Path, index: int) -> tuple[Path, str]:
        if index % 5 == 4:
            return movie / "RO", "E"
        if index % 3 == 2:
            return movie / "Parking", "P"
        return movie, ""

    @staticmethod
    def _append_gps(video: Path, at: datetime) -> None:
        with video.open("ab") as handle:
            for second in range(CLIP_SECONDS * 5):
                moment = at + timedelta(seconds=second)
                payload = pack_record(moment, 14.412755 + second / 10000.0,
                                      121.043745 + second / 10000.0,
                                      20.0, 30.2)
                handle.write(struct.pack(">I", 8 + len(payload))
                             + b"free" + payload)
```

- [ ] **Step 5: Write the CLI**

Create `tools/simulate_card.py`:

```python
#!/usr/bin/env python3
"""Write a simulated dashcam card.

Usage:
    PYTHONPATH=src python3 tools/simulate_card.py <camera> <destination> [clips]

Cameras: ddpai, blackvue, viofo

The DDPAI card is calibrated against a real one. The other two are built
from manuals and open-source parsers, and the adapters that read them come
from the same documents -- so they demonstrate the contract, not the camera.
"""

import sys
from pathlib import Path

from simulator.blackvue_simulator import BlackvueSimulator
from simulator.ddpai_simulator import DdpaiSimulator
from simulator.viofo_simulator import ViofoSimulator

SIMULATORS = {simulator.name: simulator
              for simulator in (DdpaiSimulator(), BlackvueSimulator(),
                                ViofoSimulator())}


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    camera = argv[1]
    if camera not in SIMULATORS:
        print("Unknown camera %r. Known: %s"
              % (camera, ", ".join(sorted(SIMULATORS))))
        return 2
    destination = Path(argv[2]).expanduser()
    clips = int(argv[3]) if len(argv) == 4 else 6
    destination.mkdir(parents=True, exist_ok=True)
    SIMULATORS[camera].write(destination, clips)
    print("wrote a %s card of %d clips to %s" % (camera, clips, destination))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv))
```

- [ ] **Step 6: Generate one card of each and look at it**

```bash
cd ~/dev/dashcam-exporter-adapter-change
for camera in ddpai blackvue viofo; do
  PYTHONPATH=src python3 tools/simulate_card.py $camera /tmp/sim-$camera 6
  find /tmp/sim-$camera -type f | head -8
done
```

Expected: three trees, real MP4 files, and DDPAI's tree matching the skeleton recorded in the spec.

- [ ] **Step 7: Commit**

```bash
git add tools/simulator tools/simulate_card.py
git commit -m "$(cat <<'EOF'
Write cards to read back, and say which of them is calibrated

The DDPAI simulator reproduces a real card: the full skeleton including the
photo, thumbnail and log directories this tool never opens, the rear camera
running a second behind the front, local wall clock in the filenames
against UTC in the telemetry, and three of the pre-allocated placeholder
archives a freshly formatted card carries. Every one of those details came
from the card rather than from a document, and the timezone one is there
specifically because a simulator using one clock for both would have passed
the bug that calibration caught.

The BlackVue and VIOFO simulators are not calibrated and their docstrings
say so. VIOFO's writes telemetry with the adapter's own pack_record, which
makes reader and writer provably consistent and proves nothing whatever
about a camera.

Not in the test suite: it shells out to ffmpeg, and the suite is fast and
dependency-free by design.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Run every adapter over its own simulated card

**Files:**
- Create: `tools/calibrate_adapters.py`

**Interfaces:**
- Consumes: `default_registry()`, the three simulators.
- Produces: a standalone script that writes three cards to a temporary directory, resolves each through the registry, and reports clips, channels, modes and GPS points per camera.

**What this proves and what it does not.** It proves the contract carries three genuinely different filing systems: two directories against one, sidecar against embedded telemetry, exact pairing against fuzzy, mode in the name against mode in the folder. It does not prove any adapter reads a real BlackVue or VIOFO card, and the script prints that caveat itself so nobody reads its output as more than it is.

- [ ] **Step 1: Write the script**

Create `tools/calibrate_adapters.py`:

```python
#!/usr/bin/env python3
"""Generate a card per camera, then read each one back through the registry.

Usage:
    PYTHONPATH=src python3 tools/calibrate_adapters.py [clips]

Not a test: it shells out to ffmpeg and writes hundreds of files.

What it shows: that one contract carries three unrelated filing systems.
What it does not show: that the BlackVue and VIOFO adapters read real
cards. Those cards were written by this project from the same documents the
adapters were written from, so agreement between them is self-consistency
and nothing more. Only the DDPAI adapter has been run against real footage.
"""

import sys
import tempfile
from pathlib import Path

from dashcam_exporter.domain import Channel
from dashcam_exporter.infrastructure.adapters import default_registry

from simulator.blackvue_simulator import BlackvueSimulator
from simulator.ddpai_simulator import DdpaiSimulator
from simulator.viofo_simulator import ViofoSimulator

SIMULATORS = (DdpaiSimulator(), BlackvueSimulator(), ViofoSimulator())


def report(simulator, card: Path, clips: int) -> bool:
    simulator.write(card, clips)
    adapter = default_registry().detect(card)
    layout = adapter.layout_for(card)
    found = layout.clips()
    channels = sorted({channel.value for clip in found
                       for channel in clip.videos})
    modes = sorted({clip.mode.value for clip in found})
    tracked = [layout.track_for(clip) for clip in found]
    points = sum(len(track.points) for track in tracked if track is not None)
    with_track = sum(1 for track in tracked if track is not None)

    print("%-9s detected as %-9s clips %-3d channels %-22s modes %-24s "
          "gps %d/%d, %d points"
          % (simulator.name, adapter.name, len(found), ",".join(channels),
             ",".join(modes), with_track, len(found), points))

    ok = adapter.name == simulator.name and len(found) == clips and points > 0
    if not ok:
        print("           MISMATCH -- expected %d clips detected as %s with "
              "telemetry" % (clips, simulator.name))
    return ok


def main(argv: list[str]) -> int:
    clips = int(argv[1]) if len(argv) > 1 else 6
    every = True
    with tempfile.TemporaryDirectory() as temporary:
        for simulator in SIMULATORS:
            card = Path(temporary) / simulator.name
            card.mkdir(parents=True)
            every = report(simulator, card, clips) and every
    print()
    print("Only the DDPAI result is calibrated against a real card. The other "
          "two read back what this project wrote.")
    return 0 if every else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv))
```

- [ ] **Step 2: Run it**

Run: `cd ~/dev/dashcam-exporter-adapter-change && PYTHONPATH=src python3 tools/calibrate_adapters.py 6`

Expected: each camera detected as itself, 6 clips each, DDPAI showing front and rear channels, VIOFO showing front and rear across normal, parking and event modes, BlackVue showing front and rear across normal and parking, and a non-zero point count for all three.

- [ ] **Step 3: Judge the result honestly**

If a camera mismatches, **report it rather than adjusting the simulator until the adapter agrees**. The two halves were written from one reading, so making them agree is trivial and worthless; a disagreement means one half has a bug the other does not, which is the only signal this arrangement can produce.

- [ ] **Step 4: Re-run the DDPAI calibration against the real import**

Run: `PYTHONPATH=src python3 tools/calibrate_ddpai.py ~/dashcam-data/import/2026-08-08`

Expected, unchanged from plan 1: 235 clips, 234 paired, 202 with GPS, 9,868 points. This is the check that the simulator work did not disturb the one adapter with real evidence behind it.

- [ ] **Step 5: Run the delta gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 6: Commit**

```bash
git add tools/calibrate_adapters.py
git commit -m "$(cat <<'EOF'
Read three simulated cards back, and print what the result is not worth

One contract now carries two directories against one, sidecar telemetry
against telemetry inside the container, exact pairing against a six-second
tolerance, and mode in the filename against mode in the folder name. That
was the question the exercise set out to answer, and this script is the
answer in one line per camera.

It prints its own caveat because the output looks like evidence and mostly
is not: the BlackVue and VIOFO cards were written by this project from the
same documents its adapters were written from. Only the DDPAI line has a
real card behind it, and that one is re-checked against the real import in
the same run.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

Spec coverage: the simulator, the BlackVue adapter, the VIOFO adapter and the registry ambiguity rule all have tasks. The spec's plan-3 items — pipeline rewiring, import normalisation, `DexGpsFile`, the one-time converter — are not in this plan and remain plan 3's.

Type consistency: `CardLayout`'s five methods are implemented identically in `BlackvueCardLayout` and `ViofoCardLayout`; `pack_record` and `NovatekGpsReader` share `_RECORD` and the `0x58` discriminator; `default_registry()` is extended rather than redefined.

One known soft spot, recorded rather than hidden: `ViofoCardLayout.clips()` emits one clip per front file, so a rear file that pairs with nothing is dropped entirely. Task 5 step 3 flags this and instructs verification of the assertion rather than weakening it. If it fails, the fix is to emit unpaired non-front files as their own clips.
