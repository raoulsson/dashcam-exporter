"""Two ways the renderer used to write the wrong file.

Both are about the NAME on disk, which is the only thing the rest of the tool
reads back: `final.exists()` decides whether a trip is re-rendered, and
pipeline.rendered_mp4s() counts trip_*.mp4 as the evidence that footage has
been rendered and may therefore be deleted.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.infrastructure.media import renderer as R


class PublishNumberingTest(unittest.TestCase):
    """The _NN in a trip's filename belongs to the day, not to the selection."""

    def test_it_restarts_at_one_each_day(self):
        days = ["2026-05-11", "2026-05-11", "2026-05-12"]
        self.assertEqual(R.publish_numbers(days, {1, 2, 3}, set()),
                         {1: 1, 2: 2, 3: 1})

    def test_the_unpublished_groups_do_not_take_numbers(self):
        # Group 2 is a fragment a full render skips: the day publishes two
        # trips, numbered 01 and 02, and group 2 is not one of them.
        days = ["2026-05-11"] * 3
        self.assertEqual(R.publish_numbers(days, {1, 3}, set()), {1: 1, 3: 2})

    def test_a_forced_fragment_lands_after_the_days_real_trips(self):
        # --drives 2 on that fragment must not push trip 3 off _02.
        days = ["2026-05-11"] * 3
        numbers = R.publish_numbers(days, {1, 3}, {2})
        self.assertEqual(numbers[1], 1)
        self.assertEqual(numbers[3], 2)
        self.assertEqual(numbers[2], 3)


class DrivesSubsetNamesTheSameFileTest(unittest.TestCase):
    """End to end through main(): --drives must compute the full render's name.

    Grouping is stubbed because real boundaries need GPS; everything the test
    is about -- the publish numbering and the output basename -- is the real
    code path, read out of --print-groups, which reports exactly the paths a
    render would write.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.card = base / "card-A"
        self.out = base / "out"
        front = self.card / "DCIM" / "200video" / "front"
        front.mkdir(parents=True)
        # DDPAI names carry the start stamp and the duration, so no encoding
        # is needed for a scan: 15 one-minute clips in three bursts on one day.
        for burst in ("080000", "120000", "180000"):
            t = datetime.strptime("20260511" + burst, "%Y%m%d%H%M%S")
            for _ in range(5):
                (front / f"{t:%Y%m%d%H%M%S}_0060.mp4").write_bytes(b"")
                t += timedelta(seconds=60)
        self._real_group = R.group_into_trips
        R.group_into_trips = lambda clips, track, **kw: (
            [clips[0:5], clips[5:10], clips[10:15]], [True] * 3)

    def tearDown(self):
        R.group_into_trips = self._real_group
        self._tmp.cleanup()

    def _basenames(self, extra):
        # The scan cache keys on the boundaries, and a second run in the same
        # out_dir would answer from it instead of the stub.
        (self.out / ".scan_cache.json").unlink(missing_ok=True)
        argv = sys.argv
        sys.argv = ["renderer", "--root", str(self.card), "--out", str(self.out),
                    "--print-groups"] + extra
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                R.main()
        finally:
            sys.argv = argv
        text = buffer.getvalue()
        payload = json.loads(text[text.index("{"):])
        return {t["index"]: (Path(t["out_base"]).name if t["out_base"] else None)
                for t in payload["trips"]}

    def test_a_subset_render_writes_the_filename_a_full_render_would(self):
        full = self._basenames([])
        self.assertEqual(full[2], "trip_2026-05-11_12-00_02")
        subset = self._basenames(["--drives", "2"])
        self.assertEqual(subset[2], full[2],
                         "--drives renumbered the day and wrote a second _01")


class AtomicConcatTest(unittest.TestCase):
    """An unfinished encode must never hold the finished name."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.day = Path(self._tmp.name)
        self.final = self.day / "trip_2026-05-11_08-00_01_h1080.mp4"
        self._real_ffmpeg = R.run_ffmpeg

    def tearDown(self):
        R.run_ffmpeg = self._real_ffmpeg
        self._tmp.cleanup()

    def _ffmpeg(self, write_bytes, then=None):
        def fake(cmd):
            Path(cmd[-1]).write_bytes(write_bytes)
            if then is not None:
                raise then
        return fake

    def test_an_interrupted_encode_leaves_nothing_behind(self):
        # ffmpeg opens the output, writes a header and dies (Ctrl-C: rc 255).
        R.run_ffmpeg = self._ffmpeg(
            b"half a movie", subprocess.CalledProcessError(255, ["ffmpeg"]))
        with self.assertRaises(subprocess.CalledProcessError):
            R.concat_clips([self.day / "a.mp4"], self.final)
        self.assertFalse(self.final.exists(),
                         "a truncated file under the final name reads as done")
        self.assertEqual(sorted(p.name for p in self.day.iterdir()), [])

    def test_ctrl_c_is_cleaned_up_too(self):
        # KeyboardInterrupt is not an Exception; an `except Exception` here
        # would leave the partial behind for the commonest interruption there
        # is.
        R.run_ffmpeg = self._ffmpeg(b"half a movie", KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            R.concat_clips([self.day / "a.mp4"], self.final)
        self.assertEqual(sorted(p.name for p in self.day.iterdir()), [])

    def test_a_finished_encode_takes_the_final_name_and_no_scratch(self):
        R.run_ffmpeg = self._ffmpeg(b"a whole movie")
        R.concat_clips([self.day / "a.mp4"], self.final)
        self.assertEqual(self.final.read_bytes(), b"a whole movie")
        self.assertEqual([p.name for p in self.day.iterdir()], [self.final.name])

    def test_a_partial_cannot_pass_for_a_render(self):
        # The two names the rest of the tool sweeps by. rendered_mp4s() globs
        # trip_*.mp4 and skips dotted paths; the day reset keeps hidden files.
        partial = R._partial_of(self.final)
        self.assertTrue(partial.name.startswith("."))
        self.assertFalse(partial.name.endswith(".mp4"))
        partial.write_bytes(b"leftover from a crash")
        self.assertEqual(list(self.day.glob("trip_*.mp4")), [])

    def test_a_stale_partial_is_cleared_rather_than_accumulating(self):
        # Nothing else deletes these: they are hidden, so the fresh-output
        # reset walks past them.
        stale = R._partial_of(self.day / "trip_2026-05-11_07-00_09_h1080.mp4")
        stale.write_bytes(b"leftover from a crash")
        R.run_ffmpeg = self._ffmpeg(b"a whole movie")
        R.concat_clips([self.day / "a.mp4"], self.final)
        self.assertFalse(stale.exists())


class TheZeroExitFailureIsNotTakenForSuccess(unittest.TestCase):
    """ffmpeg can report an error and still exit 0.

    Reproduced against the real binary: give the concat demuxer a list whose
    FIRST clip opens and whose second is missing, and it writes the first
    clip's frames, prints "Error during demuxing", and returns 0. (With the
    first clip missing it exits non-zero, so the return code alone looks
    sufficient until a later input is the one that fails.)

    Nothing downstream can tell that short file from a finished trip: it gets
    the final name, the next run skips it as already rendered, and the delete
    gates count it as proof the footage was encoded. Atomicity does not help --
    the encode "succeeded".
    """

    def _ffmpeg_saying(self, line, rc=0):
        """Stand in for the binary: emit `line` on stderr and exit `rc`."""
        script = ("import sys; sys.stderr.write(%r); sys.exit(%d)"
                  % (line, rc))
        return [sys.executable, "-c", script]

    def test_a_demuxing_error_raises_even_though_ffmpeg_exited_zero(self):
        with self.assertRaises(subprocess.CalledProcessError):
            R.run_ffmpeg(self._ffmpeg_saying(
                "[in#0/concat] Error during demuxing: No such file or directory\n"))

    def test_an_ordinary_run_still_passes(self):
        R.run_ffmpeg(self._ffmpeg_saying("frame= 120 fps=30 time=00:00:04.00\n"))

    def test_known_ddpai_noise_is_still_not_an_error(self):
        for noisy in R._NOISY_FFMPEG_PATTERNS:
            with self.subTest(noisy=noisy):
                R.run_ffmpeg(self._ffmpeg_saying(str(noisy) + "\n"))


if __name__ == "__main__":
    unittest.main()
