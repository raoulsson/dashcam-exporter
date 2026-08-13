#!/usr/bin/env python3
"""The built-in export's one deviation from every other uploader: it answers
"did WE copy this out" from a record, and never by going back to look.

The destination is a directory the operator named, and the ordinary reason to
name one is a memory stick or an external drive -- exported at lunchtime,
unplugged, carried off. An is_complete that listed the destination would then
read the empty slot, or a DIFFERENT disk mounted at the same path, as though the
copy had never happened, and 9) Clean Workspace would refuse for good on footage
that is demonstrably on a shelf.

So the tests below state the record as the source of truth, including the case
that looks wrong at first glance and is the whole point: no destination
directory at all, and still YES.

A real plugin does NOT work this way and must not be made to. It owns a
destination it can re-inspect -- a bucket, a server -- so it answers by looking,
and what it says is true because only it can know. tests/test_uploader.py pins
that half against the shipped example.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.application.ports.uploader import Evidence, Ruling, Workspace
from dashcam_exporter.infrastructure.plugin.directory_export import DirectoryExport

# A name Render.trip_id actually parses. Made-up ids read fine in a test and
# then record nothing at all, because execute() derives the trip from the FILE
# NAME -- so a fixture that does not obey the naming rule would pass the
# recording tests by asserting an empty set against an empty set.
TRIP = "trip_2026-07-28_08-57_01"
VIDEO = TRIP + "_h1080.mp4"


class Record:
    """The durable "we copied these out" record, in memory.

    Stands in for pipeline.ExportRecord, whose own union/re-read behaviour is
    pinned in tests/test_uploader.py against the real ledger. What this class
    is for is the OTHER side of the seam: DirectoryExport is handed a record
    rather than reaching for one, which is what lets these tests state an
    export that happened last week without performing it.
    """

    def __init__(self, exported=()):
        self.marks = {str(t) for t in exported}

    def exported(self):
        return set(self.marks)

    def mark(self, trip_ids):
        self.marks |= {str(t) for t in trip_ids}
        return set(self.marks)


class UnreadableRecord(Record):
    """The ledger will not open. Not the same thing as an empty ledger."""

    def exported(self):
        raise OSError("the ledger would not open")


class ExportTest(unittest.TestCase):
    """A gathered deliverable and somewhere to copy it to, on real files.

      root/final_2026-07-28/2026-07-28/index.html      the browsable part
      root/final_2026-07-28/2026-07-28/<TRIP>_h1080.mp4  the footage
      root/stick/                                      the destination
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dashcam-export-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.final = self.root / "final_2026-07-28" / "2026-07-28"
        self.final.mkdir(parents=True)
        (self.final / "index.html").write_text("<p>a trip</p>", encoding="utf-8")
        (self.final / VIDEO).write_bytes(b"x" * 64)
        self.destination = self.root / "stick"
        self.record = Record()

    def export(self, destination=None, finals=None, record=None):
        return DirectoryExport(destination or self.destination,
                               finals or (lambda: (self.final.parent,)),
                               self.record if record is None else record)

    def workspace(self):
        return Workspace(out_dir=self.root / "out")


# ---------------------------------------------------------------------------
# is_complete -- the answer 9) Clean Workspace erases footage on
# ---------------------------------------------------------------------------

class TestWhatCountsIsThatTheCopyHappened(ExportTest):
    """Six readings, and only one of them looks at anything on disk.

    Each is the difference between erasing the only local copy of a drive and
    keeping it, so they are stated one by one rather than as a table: NO is a
    refusal that costs disk, UNKNOWN is a question nobody could answer and
    fails closed, and YES is the one that lets footage go.
    """

    def test_a_trip_we_never_copied_out_is_no(self):
        self.assertIs(self.export().is_complete((TRIP,)), Evidence.NO)

    def test_a_trip_the_record_says_we_copied_is_yes(self):
        self.record.mark((TRIP,))
        self.assertIs(self.export().is_complete((TRIP,)), Evidence.YES)

    def test_a_destination_that_is_not_there_at_all_is_still_yes(self):
        """The reason this class exists.

        The stick was written at lunchtime and is in a drawer, or a different
        disk is mounted where it used to be. Nothing about that changes whether
        the copy took place, and a gate that went back to look would answer NO
        forever on footage that is on a shelf. The record is what is read, so
        the destination is allowed to be missing entirely.
        """
        self.record.mark((TRIP,))
        gone = self.export(destination=self.root / "no" / "such" / "mount")
        self.assertFalse((self.root / "no").exists())
        self.assertIs(gone.is_complete((TRIP,)), Evidence.YES)

    def test_one_trip_we_never_copied_makes_the_whole_answer_no(self):
        """All or nothing, and this is what makes that safe.

        The exporter asks about every trip of the import, including one that
        produced no render and therefore no file to copy. It is not in the
        record, so the honest answer about the set is NO and none of that
        import's footage is swept.
        """
        self.record.mark((TRIP,))
        self.assertIs(self.export().is_complete((TRIP, "trip_never_rendered")),
                      Evidence.NO)

    def test_an_empty_trip_list_is_no_and_never_a_vacuous_yes(self):
        """Nothing was asked about, so nothing was vouched for.

        Set arithmetic says the empty set is a subset of anything, which would
        make "have we copied out no trips" answer YES and clear a gate that is
        about footage. An import with no trips settled on hands over an empty
        list, so this is reachable rather than theoretical.
        """
        self.record.mark((TRIP,))
        self.assertIs(self.export().is_complete(()), Evidence.NO)

    def test_a_record_that_cannot_be_read_is_unknown_not_no(self):
        """Our own record failed. That is not permission and it is not a
        denial: UNKNOWN fails closed at the gate, while NO merely refuses this
        one sweep and reads as "the destination has nothing", which nobody
        established."""
        self.assertIs(self.export(record=UnreadableRecord()).is_complete((TRIP,)),
                      Evidence.UNKNOWN)


# ---------------------------------------------------------------------------
# execute -- what a run writes down, and what it refuses to write down
# ---------------------------------------------------------------------------

class TestOnlyACopiedVideoIsRecorded(ExportTest):
    """The record means "this drive was written out", not "this run ran".

    Two runs are legitimate and only one of them may move the erase gate: the
    pages-only run makes a destination browsable in a few megabytes, and the
    full run carries the hours of footage. If attempting the step were enough
    to satisfy is_complete, a pages-only run would authorise erasing footage
    that never left the machine.
    """

    def test_a_run_with_the_videos_records_the_trips_they_belong_to(self):
        outcome = self.export().execute(self.workspace(), includeVideos=True)
        self.assertTrue(outcome.completed)
        self.assertEqual(self.record.exported(), {TRIP})

    def test_a_pages_only_run_records_nothing(self):
        outcome = self.export().execute(self.workspace())
        self.assertTrue(outcome.completed)
        self.assertEqual(self.record.exported(), set())
        self.assertTrue((self.destination / "final_2026-07-28" / "2026-07-28"
                         / "index.html").is_file())
        self.assertFalse((self.destination / "final_2026-07-28" / "2026-07-28"
                          / VIDEO).exists())

    def test_a_second_run_overwrites_what_is_already_at_the_destination(self):
        """The destination is never listed to work out what is missing, and
        that is deliberate: copying the lot is how a re-rendered trip replaces
        the older copy on the stick rather than being skipped as present."""
        self.export().execute(self.workspace(), includeVideos=True)
        (self.final / VIDEO).write_bytes(b"y" * 128)          # re-rendered
        self.export().execute(self.workspace(), includeVideos=True)
        copied = self.destination / "final_2026-07-28" / "2026-07-28" / VIDEO
        self.assertEqual(copied.read_bytes(), b"y" * 128)

    def test_a_destination_that_cannot_be_written_stops_and_records_nothing(self):
        """A disk that is not there. The step reports not completing rather
        than raising, so the position stays where it was and item 8 is still
        owed -- and nothing is recorded, because a failed copy that marked the
        trips would hand Clean Workspace a yes for files that never landed."""
        blocked_by_a_file = self.root / "not-a-directory"
        blocked_by_a_file.write_text("in the way", encoding="utf-8")
        outcome = self.export(destination=blocked_by_a_file / "stick").execute(
            self.workspace(), includeVideos=True)
        self.assertFalse(outcome.completed)
        self.assertEqual(self.record.exported(), set())

    def test_nothing_gathered_is_a_stopped_run_rather_than_an_empty_success(self):
        outcome = self.export(finals=lambda: ()).execute(self.workspace(),
                                                         includeVideos=True)
        self.assertFalse(outcome.completed)
        self.assertEqual(self.record.exported(), set())


# ---------------------------------------------------------------------------
# evaluate -- asked on every menu draw, and never of the destination
# ---------------------------------------------------------------------------

class TestWhetherItMayRunIsAskedOfTheSourceOnly(ExportTest):
    """The destination's ordinary state is unplugged, so it is not consulted.

    Consulting it would do two wrong things at once: grey out item 8 whenever
    the stick is not in, and -- once it IS in and holds last week's copy --
    report the step as SATISFIED, which is how a re-rendered trip never reaches
    the disk.
    """

    def test_there_is_something_to_do_even_though_the_destination_is_absent(self):
        self.assertFalse(self.destination.exists())
        self.assertIs(self.export().evaluate(self.workspace()).ruling, Ruling.GO)

    def test_with_nothing_gathered_it_is_blocked(self):
        verdict = self.export(finals=lambda: ()).evaluate(self.workspace())
        self.assertIs(verdict.ruling, Ruling.BLOCKED)
        self.assertIn("gathered", verdict.reason)

    def test_a_second_export_is_still_offered_rather_than_satisfied(self):
        """Running it again copies again, overwriting. SATISFIED would complete
        the item without doing the work, which is right for a plugin that can
        see its own destination and wrong for one that cannot."""
        self.export().execute(self.workspace(), includeVideos=True)
        self.assertIs(self.export().evaluate(self.workspace()).ruling, Ruling.GO)


if __name__ == "__main__":
    unittest.main()
