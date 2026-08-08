# Route the Pipeline Through the Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tool reachable by any adapter — replace the hardcoded DDPAI paths in `pipeline.py` and `renderer.py` with registry lookups, and delete the duplicated GPS extraction.

**Architecture:** `pipeline.py` never imports an adapter. It calls a small `card_access` module which resolves a tree through `default_registry()` and answers the questions the pipeline actually asks: is this a card, how many clips, which stamps, does it carry a track. `renderer.py` resolves its adapter the same way, and its private tar extractor delegates to the DDPAI track source so there is one definition of where DDPAI keeps GPS.

**Tech Stack:** Python 3 stdlib. Tests are `unittest`.

## Scope: what this plan is NOT

The canonical workspace, `DexGpsFile`, the import transformation and the one-time converter are **plan 4**. They change the on-disk shape of import folders and touch real footage, so they get their own plan and their own approval. This plan changes no file the tool writes — only how it decides what a card is.

Consequently `STAMP_RE` and its 15 call sites stay exactly as they are. Import folders remain vendor-shaped until plan 4, so those sites are still reading DDPAI names and are still correct.

## Global Constraints

- Test runner: `./run-tests.sh` (`python3 -m unittest discover -s tests -q`, `PYTHONPATH=src`). No pytest.
- **Baseline:** the suite is green except one environmental failure — `test_checkout.test_the_memory_is_named_after_the_checkout` asserts `HOME_DIR.name == ".dashcam-exporter"`, and `HOME_DIR` derives from the checkout directory name, so it fails in this clone and would fail on clean master here too. Gate: no failures beyond that one. Script at `scratchpad/gate.sh`.
- Tests must not read the SD card, the import workspace or the output tree.
- `pipeline.py` must not import from `infrastructure.adapters`. It talks to `card_access` only.
- One class per file. No emojis. Commit messages say WHY. Sign off `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Branch `adapter-interface` in `~/dev/dashcam-exporter-adapter-change`, now merged with master.

## The call sites this plan replaces

| Location | What it does | Replacement |
|---|---|---|
| pipeline.py:1084 `clip_count` | counts files in `DCIM/200video/front` | `card_access.clip_count(root)` |
| pipeline.py:1908 | new-vs-already clip counter | `card_access.stamps_on(card)` |
| pipeline.py:2400 | card guard | `card_access.is_card(card)` |
| pipeline.py:3869 | candidate guard | `card_access.is_card(cand)` |
| pipeline.py:6452 `card_stamps` | globs `*.mp4`, extracts stamps | `card_access.stamps_on(ctx.card)` |
| pipeline.py:6547 | front dir via `VIDEO_DIR` | `card_access.is_card(root)` |
| pipeline.py:6562 | candidate guard | `card_access.is_card(cand)` |
| pipeline.py:6948 | per-clip front paths | `card_access.front_videos(card)` |
| pipeline.py:6781 `_is_track_file` | `.gpx`/`.git` suffix test | `card_access.is_track_artifact(root, f)` |
| renderer.py:314 | constructs `DdpaiDataAdapter` | registry lookup |
| renderer.py:764 `harvest_tarred_gpx` | second copy of tar extraction | delegates to `DdpaiTrackSource` |

---

### Task 1: The card_access module

**Files:**
- Create: `src/dashcam_exporter/application/workflow/card_access.py`
- Test: `tests/application/workflow/test_card_access.py`
- Create: `tests/application/__init__.py`, `tests/application/workflow/__init__.py`

**Interfaces:**
- Produces: `layout_for(root) -> CardLayout | None`, `is_card(root) -> bool`, `clip_count(root) -> int | None`, `stamps_on(root) -> set[str]`, `front_videos(root) -> list[Path]`, `is_track_artifact(root, path) -> bool`, `carries_track(root) -> bool`.

**Why a module of functions rather than a class:** every one of these is a pure question about a path, with no state to hold between calls beyond a cache. A class here would be a namespace wearing a constructor.

**Caching matters:** `clips()` parses every filename on the card, and the pipeline asks these questions repeatedly while painting a menu. `layout_for` memoises per resolved path.

- [ ] **Step 1: Create the test packages**

```bash
cd ~/dev/dashcam-exporter-adapter-change
mkdir -p tests/application/workflow
touch tests/application/__init__.py tests/application/workflow/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/application/workflow/test_card_access.py`:

```python
"""The questions the pipeline asks about a tree, answered by whoever wrote it."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.application.workflow import card_access


def ddpai_card(root: Path) -> Path:
    (root / "DCIM/200video/front").mkdir(parents=True)
    (root / "DCIM/200video/rear").mkdir(parents=True)
    (root / "DCIM/203gps/tar").mkdir(parents=True)
    return root


def blackvue_card(root: Path) -> Path:
    (root / "BlackVue/Record").mkdir(parents=True)
    return root


class CardAccessTest(unittest.TestCase):
    def setUp(self):
        card_access.forget()

    def test_recognises_a_card_from_any_supported_camera(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ddpai_card(root / "a")
            blackvue_card(root / "b")

            self.assertTrue(card_access.is_card(root / "a"))
            self.assertTrue(card_access.is_card(root / "b"))

    def test_a_tree_no_adapter_claims_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "DCIM/100CANON").mkdir(parents=True)

            self.assertFalse(card_access.is_card(root))
            self.assertIsNone(card_access.clip_count(root))
            self.assertEqual(card_access.stamps_on(root), set())

    def test_counts_clips_by_parsing_rather_than_by_listing(self):
        # A directory listing counted two files this tool cannot read as
        # clips on a real card. Parsing is the honest count.
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()
            (card / "DCIM/200video/front/20260806170629_0060.mp4").touch()
            (card / "DCIM/200video/front/notes.txt").touch()

            self.assertEqual(card_access.clip_count(card), 2)

    def test_stamps_come_back_in_our_canonical_form(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = blackvue_card(Path(temporary))
            (card / "BlackVue/Record/20210127_155052_NF.mp4").touch()

            self.assertEqual(card_access.stamps_on(card), {"20210127155052"})

    def test_front_videos_are_the_clips_main_camera_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))
            for stamp in ("20260806170629", "20260806170529"):
                (card / ("DCIM/200video/front/%s_0060.mp4" % stamp)).touch()

            names = [p.name for p in card_access.front_videos(card)]

        self.assertEqual(names, ["20260806170529_0060.mp4",
                                 "20260806170629_0060.mp4"])

    def test_a_track_artifact_is_whatever_that_camera_calls_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))

            self.assertTrue(card_access.is_track_artifact(
                card, Path("20260806170529_0540.git")))
            self.assertFalse(card_access.is_track_artifact(
                card, Path("20260806170529_0060.mp4")))

    def test_carries_track_sees_the_archive_a_ddpai_card_keeps(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))

            self.assertFalse(card_access.carries_track(card))
            (card / "DCIM/203gps/tar/20260806170529_0540.git").touch()

            card_access.forget()
            self.assertTrue(card_access.carries_track(card))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.application.workflow.test_card_access -v`
Expected: FAIL with `ImportError: cannot import name 'card_access'`.

- [ ] **Step 4: Write the implementation**

Create `src/dashcam_exporter/application/workflow/card_access.py`:

```python
"""What the pipeline is allowed to know about a card: nothing.

Every question here used to be answered by spelling out DCIM/200video/front,
in nine places, which is why a card from any other camera was rejected as
"not a card" before an adapter ever saw it. The pipeline now asks these
questions instead, and the answers come from whichever adapter recognises
the tree.

This module is the ONLY route from the pipeline to the adapters. Nothing in
application/ imports infrastructure.adapters directly.
"""

from pathlib import Path

from dashcam_exporter.domain import Channel
from dashcam_exporter.infrastructure.adapters import (
    AmbiguousCard, CardLayout, NoAdapterFound, default_registry)

_LAYOUTS: dict[Path, CardLayout | None] = {}


def forget() -> None:
    """Drop the memoised layouts. For tests, and for a card being swapped."""
    _LAYOUTS.clear()


def layout_for(root) -> CardLayout | None:
    """The layout for whatever camera wrote this tree, or None if unknown.

    Memoised: clips() parses every filename on the card, and the menu asks
    these questions on every repaint.
    """
    if root is None:
        return None
    key = Path(root)
    if key in _LAYOUTS:
        return _LAYOUTS[key]
    layout: CardLayout | None
    try:
        layout = default_registry().detect(key).layout_for(key)
    except (NoAdapterFound, AmbiguousCard):
        layout = None
    _LAYOUTS[key] = layout
    return layout


def is_card(root) -> bool:
    return layout_for(root) is not None


def clip_count(root) -> int | None:
    """How many clips this tree holds, or None if it is not a card at all.

    None rather than zero is load-bearing: a folder that is not a card and a
    card that has been emptied are different answers, and the destructive
    paths read this one to decide whether erasing is safe.
    """
    layout = layout_for(root)
    return None if layout is None else len(layout.clips())


def stamps_on(root) -> set[str]:
    layout = layout_for(root)
    return set() if layout is None else {clip.timestamp for clip in layout.clips()}


def front_videos(root) -> list[Path]:
    """One path per clip, main camera, in recording order."""
    layout = layout_for(root)
    if layout is None:
        return []
    return [clip.videos.get(Channel.FRONT, clip.front) for clip in layout.clips()]


def is_track_artifact(root, path) -> bool:
    layout = layout_for(root)
    return False if layout is None else layout.is_track_artifact(Path(path))


def carries_track(root) -> bool:
    """Whether this tree holds any GPS at all, however the camera stores it."""
    layout = layout_for(root)
    if layout is None:
        return False
    for directory in layout.import_roots():
        if not directory.is_dir():
            continue
        for candidate in directory.rglob("*"):
            if candidate.is_file() and layout.is_track_artifact(candidate):
                return True
    return False
```

- [ ] **Step 5: Run it and confirm it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.application.workflow.test_card_access -v`
Expected: PASS, 7 tests.

- [ ] **Step 6: Run the gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 7: Commit**

```bash
git add src/dashcam_exporter/application/workflow/card_access.py tests/application
git commit -m "$(cat <<'EOF'
Give the pipeline one place to ask what a card is

Nine sites spelled out DCIM/200video/front and fifteen more read a DDPAI
stamp, so the tool decided what counted as a card before any adapter was
consulted -- which is why a correctly written BlackVue adapter still could
not have been reached.

One module, and it is the only route from application/ to the adapters.
clip_count answering None rather than zero is the load-bearing part: a
folder that is not a card and a card that has been emptied are different
answers, and the destructive paths read exactly this to decide whether
erasing is safe.

Counting by parsing rather than by listing is a real change, and the right
one: on a real card, listing counted two files this tool cannot read as
clips.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Route pipeline.py's card questions

**Files:**
- Modify: `src/dashcam_exporter/application/workflow/pipeline.py` at the sites tabled above.

**Method — read each site before editing it.** The line numbers move as you edit; re-grep rather than trusting them. The edits are mechanical but the surrounding logic is not, so change only the expression that builds or reads the path.

- [ ] **Step 1: Add the import**

At the top of `pipeline.py`, alongside the other intra-package imports, add:

```python
from dashcam_exporter.application.workflow import card_access
```

- [ ] **Step 2: Replace `clip_count`**

Find `def clip_count(dcim_parent):` and replace the whole function with:

```python
def clip_count(dcim_parent):
    """Clips in an import folder, or None when it is not a card at all."""
    return card_access.clip_count(dcim_parent)
```

- [ ] **Step 3: Replace `card_stamps`**

Find `def card_stamps(ctx):`. Keep its docstring — it explains why the stamps come from the CARD rather than from the workspace, which is still true — and replace the body's globbing with:

```python
    return card_access.stamps_on(ctx.card)
```

- [ ] **Step 4: Replace the four card guards**

At each site that reads `front = <root> / "DCIM" / "200video" / "front"` purely to test `front.is_dir()`, replace the pair of lines with `card_access.is_card(<root>)`. Re-grep for `200video` after each edit; four such guards exist plus the one built from `VIDEO_DIR`.

- [ ] **Step 5: Replace the per-clip front path list**

Find the function whose docstring is `"""One path per clip, front camera, in time order."""` and replace its body with:

```python
    return card_access.front_videos(card)
```

- [ ] **Step 6: Replace the track-artifact predicates**

`_is_track_file(f)` currently tests the suffix against `.gpx`/`.git`. It is called from `_gps_dirs`-driven code that already has a candidate root in hand. Rewrite `_is_track_file` to take the root:

```python
def _is_track_file(root, f):
    """Whether this file carries GPS, as the camera that wrote it defines it."""
    return card_access.is_track_artifact(root, f)
```

and update its one call site to pass the candidate root. `_is_gps_dir` and `_gps_dirs` stay: they narrow the search cheaply before the layout is asked.

- [ ] **Step 7: Confirm the pipeline no longer names a camera**

Run: `grep -n '200video\|203gps\|VIDEO_DIR\|GPS_DIR' src/dashcam_exporter/application/workflow/pipeline.py`

Expected: only prose in docstrings and comments. Any remaining code construction is an unconverted site — convert it. Update the docstrings that describe the old behaviour in the same pass; a comment naming `200video/front` as the definition of a card is now wrong, and a wrong comment is worse than none.

- [ ] **Step 8: Run the gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`. If a destructive-path test fails, STOP — those tests guard erasing a card, and a failure there means the None-versus-zero distinction moved. Report rather than adjusting the test.

- [ ] **Step 9: Commit**

```bash
git add src/dashcam_exporter/application/workflow/pipeline.py
git commit -m "$(cat <<'EOF'
Stop the pipeline deciding what a card is by its directory names

Nine sites built DCIM/200video/front by hand -- card detection, clip
counting, the guards the destructive paths consult. Every one of them was a
fact about one camera's filing system sitting in the layer that is supposed
to be about trips and renders, and together they were the reason a third
party's adapter had no way into the tool.

pipeline.py now imports card_access and nothing else from below it. The
comments that described a card as "a folder with DCIM/200video/front" are
updated in the same pass, because a comment teaching the old rule is worse
than no comment.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Route the renderer, and delete the duplicated extraction

**Files:**
- Modify: `src/dashcam_exporter/infrastructure/adapters/ddpai/ddpai_track_source.py` (add `extract_members_into`)
- Modify: `src/dashcam_exporter/infrastructure/media/renderer.py:314` and `:764`
- Test: `tests/infrastructure/adapters/ddpai/test_ddpai_track_source.py` (add one test)

**The duplication being removed:** `renderer.py:harvest_tarred_gpx` walks `*.git` archives, skips `._` forks, extracts `.gpx` members and writes them into `.gpx_cache`. `DdpaiTrackSource` already knows all of that, plus two things the renderer's copy does not: that `19700101` archives are pre-allocated placeholders rather than damaged files, and that member names carry clip stamps. Rather than delete the renderer's cache — the renderer's own `Track` class reads `.gpx` files out of it, and changing that is plan 4's job — the extraction moves behind one definition and the renderer keeps its cache.

- [ ] **Step 1: Write the failing test**

Append to `tests/infrastructure/adapters/ddpai/test_ddpai_track_source.py`:

```python
class DdpaiExtractionTest(unittest.TestCase):
    def test_writes_members_out_once_and_skips_what_is_already_there(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar_directory = root / "tar"
            cache = root / "cache"
            write_archive(tar_directory, "20260806170529_0540.git",
                          {"nested/20260806170529_0060.gpx": NMEA.encode()})

            source = DdpaiTrackSource(tar_directory)
            first = source.extract_members_into(cache)
            second = DdpaiTrackSource(tar_directory).extract_members_into(cache)

        self.assertEqual(first, (1, 1))
        self.assertEqual(second, (1, 0))

    def test_placeholder_archives_are_not_counted_as_archives_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar_directory = root / "tar"
            tar_directory.mkdir(parents=True)
            (tar_directory / "19700101004510_0100_T.git").write_bytes(b"\x00" * 64)

            result = DdpaiTrackSource(tar_directory).extract_members_into(
                root / "cache")

        self.assertEqual(result, (0, 0))
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_track_source -v`
Expected: FAIL with `AttributeError: 'DdpaiTrackSource' object has no attribute 'extract_members_into'`.

- [ ] **Step 3: Add the method**

Add to `DdpaiTrackSource`:

```python
    def extract_members_into(self, cache_directory: Path) -> tuple[int, int]:
        """Write every .gpx member out to a cache, skipping what is there.

        The renderer keeps a .gpx_cache of files rather than Track objects,
        and converting it is a later job. Until then this exists so the walk
        over DDPAI's archives has one definition instead of two -- the
        renderer's private copy predates the adapter and did not know that
        19700101 archives are pre-allocated placeholders, so it opened all
        73 of them and logged a failure for each.
        """
        archives = extracted = 0
        for archive in self._archives():
            try:
                with tarfile.open(archive, "r") as handle:
                    archives += 1
                    for member in handle.getmembers():
                        name = os.path.basename(member.name)
                        if not name.lower().endswith(".gpx") or name.startswith("._"):
                            continue
                        cache_directory.mkdir(parents=True, exist_ok=True)
                        destination = cache_directory / name
                        if (destination.exists()
                                and destination.stat().st_size == member.size):
                            continue
                        stream = handle.extractfile(member)
                        if stream is None:
                            continue
                        destination.write_bytes(stream.read())
                        extracted += 1
            except (tarfile.TarError, OSError) as error:
                self._logger.warning("Cannot read DDPAI GPS archive %s: %s",
                                     archive, error)
        return archives, extracted
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.infrastructure.adapters.ddpai.test_ddpai_track_source -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Delegate the renderer's copy**

Replace the body of `harvest_tarred_gpx` in `renderer.py` with a delegation, keeping its signature so its caller is untouched:

```python
def harvest_tarred_gpx(tar_dir: Path, cache_dir: Path) -> tuple[int, int]:
    """Extract the camera's GPS archives into cache_dir.

    Delegates: the DDPAI adapter owns the knowledge of where those archives
    live and which of them are real. This function is the renderer's cache
    contract and stays until the cache itself becomes ours.
    """
    from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiTrackSource
    return DdpaiTrackSource(tar_dir).extract_members_into(cache_dir)
```

Delete the old body entirely — the walk, the `._` skip, the size comparison. They now exist once, in the adapter.

- [ ] **Step 6: Route the renderer's clip discovery through the registry**

At `renderer.py:314`, replace the direct construction. Read the surrounding function first: it receives `front_dir` and `rear_dir`, which are workspace paths under a card root. Change it to resolve the card root through the registry, falling back to the shim if no adapter claims the tree (a bare pair of directories with no card structure around them is exactly what the old call supported, and tests rely on it):

```python
    layout = card_access.layout_for(front_dir.parent.parent.parent)
    if layout is not None:
        clips = layout.clips()
    else:
        clips = DdpaiDataAdapter(REAR_PAIR_TOLERANCE_S).discover_clips(front_dir, rear_dir)
```

Import `card_access` at the top of `renderer.py`. Verify the parent count against the real layout — `DCIM/200video/front` is three levels below the card root — and if the caller passes something else, prefer changing the caller to pass the card root over guessing with `.parent` chains.

- [ ] **Step 7: Run the gate**

Run: `scratchpad/gate.sh`
Expected: `GATE OK`.

- [ ] **Step 8: Commit**

```bash
git add src/dashcam_exporter/infrastructure src/dashcam_exporter/application tests
git commit -m "$(cat <<'EOF'
Leave one definition of where DDPAI keeps its GPS

The renderer had its own copy of the archive walk -- the *.git glob, the
._ skip, the same-size check -- written before the adapter existed. Two
copies of a camera's filing system is exactly the thing this whole change
set is about, and the renderer's copy had already fallen behind: it did not
know that 19700101 archives are pre-allocated placeholders, so it opened
all 73 of them and logged a failure for each.

The cache of .gpx files stays for now because the renderer's own Track
class reads it, and converting that is plan 4's job. What moves is the
knowledge, not the file format.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Prove it end to end on real and simulated cards

**Files:** none created. This task runs what exists and judges the result.

- [ ] **Step 1: Real DDPAI import**

Run: `PYTHONPATH=src python3 tools/calibrate_ddpai.py ~/dashcam-data/import/2026-08-08`
Expected, unchanged: 235 clips, 234 paired, 202 with GPS, 9,868 points.

- [ ] **Step 2: All three simulated cards**

Run: `PYTHONPATH=src python3 tools/calibrate_adapters.py 6`
Expected: each camera detected as itself, 6 clips, non-zero points.

- [ ] **Step 3: The question this plan set out to answer**

Confirm by hand that a BlackVue card is now visible to the pipeline, which it demonstrably was not before:

```bash
S=$(mktemp -d)
PYTHONPATH=src python3 tools/simulate_card.py blackvue "$S/bv" 4
PYTHONPATH=src python3 -c "
import sys; sys.path.insert(0,'src')
from dashcam_exporter.application.workflow import card_access
from pathlib import Path
card = Path('$S/bv')
print('is_card   ', card_access.is_card(card))
print('clip_count', card_access.clip_count(card))
print('stamps    ', len(card_access.stamps_on(card)))
"
rm -rf "$S"
```

Expected: `is_card True`, `clip_count 4`, `stamps 4`. Before this plan every one of those would have been False, None and 0, because the pipeline asked for `DCIM/200video/front`.

- [ ] **Step 4: Run the gate one more time and push**

Run: `scratchpad/gate.sh` then `git push origin adapter-interface`.

## Self-review notes

Scope: this plan converts detection and counting only. The canonical workspace, `DexGpsFile`, the importer transformation and the converter are plan 4, stated at the top rather than left implicit.

Known soft spot: task 3 step 6 reaches for the card root with a `.parent` chain, which is fragile. The step says so and prefers changing the caller. If the caller cannot be changed cheaply, leaving the shim path in place is acceptable — the renderer keeps working either way, and plan 4 rewires it properly.
