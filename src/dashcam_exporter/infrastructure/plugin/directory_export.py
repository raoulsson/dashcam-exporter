"""Item 8 with no publisher configured: copy the finished folder somewhere.

An install with no `upload_plugin` still has somewhere it wants the result to
end up -- an external disk, a NAS share, a synced folder. That is not a
different KIND of publishing and it does not get a path of its own through the
menu: it is an ordinary implementation of the uploader seam, so the erase gates
reason about it with the same three questions they ask any plugin, and nothing
in guards.py had to learn a second way of being satisfied.

The destination is a SECOND COPY. Files are copied, never moved: the workspace
copy is the one items 4, 9 and 10 reason about, and a move would leave those
gates judging a tree that shifted under them. That is the same condition every
outside implementation is held to, and being shipped in this repo does not earn
an exemption from it.

WHAT IS COPIED is the gathered deliverable -- the final_<day>_<import> folder
item 5 builds -- and not the loose renders. The folder holds the page, the
videos, the maps and the tracks together, with every link inside it resolving,
which is the whole reason Build Website gathers. Copying renders alone would
put an unopenable pile of mp4s on the disk and call it published.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dashcam_exporter.application.ports.uploader import (Evidence, Outcome, Uploader,
                                                         Verdict, blocked, did, go,
                                                         stopped)
from dashcam_exporter.domain.model.world import Render


def _raise(error):
    """os.walk swallows every error it hits unless told otherwise.

    Which is exactly wrong here: a directory that could not be read has to
    reach is_complete as UNKNOWN, and a walk that quietly skipped it would
    report a short listing that reads as "the file is not there" -- NO, on
    evidence nobody actually gathered.
    """
    raise error


def _walk_files(base: Path):
    """Every file under `base`, or OSError. Never a partial answer."""
    found = []
    for parent, _dirs, names in os.walk(str(base), onerror=_raise):
        found += [Path(parent) / name for name in names]
    return found


def _sized(path: Path):
    return (path.name, path.stat().st_size)


def _is_video(rel: Path) -> bool:
    return rel.suffix.lower() == ".mp4"


class DirectoryExport(Uploader):
    """Item 8: copy the gathered result folders to a destination directory.

    Constructed with what it needs rather than with the exporter's ctx: where
    to copy TO, and a callable answering which folders are the deliverable.
    The callable is the exporter's own `_final_folders`, so "what a finished
    result is" is defined in one place and this cannot drift into a second
    opinion about it.
    """

    def __init__(self, destination: Path, finals, record):
        self.destination = Path(destination)
        self._finals = finals
        # Where "we copied this out" is written down and read back. Handed in
        # for the same reason `finals` is: the record is durable state the
        # application owns, and this package must not reach into it.
        self._record = record

    # -- the menu row ------------------------------------------------------

    def describe(self) -> str:
        return "Copy the finished result folder to %s." % self.destination

    def get_website_upload_description(self) -> str:
        return ("The finished folder that 5) Build Website gathers is copied to:"
                "\n\t%s\n\n"
                "It is a copy and not a move, so the working area still holds "
                "everything after it runs -- which is what 9) Clean Workspace "
                "then erases, once every trip of this import has been copied "
                "out at least once.\n\n"
                "Running it again copies again, overwriting: that is how a "
                "re-rendered trip replaces the older one already on the disk.\n\n"
                "What counts is that the copy HAPPENED, not that the files are "
                "still there. The destination is usually a stick or an external "
                "drive, so it is expected to be unplugged and carried off, and "
                "a gate that went back to look would read an empty slot -- or a "
                "different disk mounted at the same path -- as though the export "
                "never took place.\n\n"
                "A run that leaves the videos out is a perfectly good run; it "
                "copies the browsable part and leaves the erase gate exactly "
                "where it was, because a folder without its videos is not a "
                "copy of the footage." % self.destination)

    # -- may it run --------------------------------------------------------

    def evaluate(self, workspace) -> Verdict:
        """Local stat calls only: this is asked on every menu draw."""
        if not self._sources():
            return blocked("no gathered result folder to copy from")
        # Never SATISFIED on the strength of what is at the destination. This
        # act is always willing to run: exporting again copies again and
        # overwrites, which is the only way a re-rendered trip reaches a stick
        # that already holds the old one. It also means the destination is
        # never consulted to decide whether to offer the step -- and it must
        # not be, because the ordinary state of a memory stick is unplugged.
        return go()

    # -- do it -------------------------------------------------------------

    def execute(self, workspace, includeVideos: bool = False) -> Outcome:
        """Pages first, media after, and only when asked for.

        The order is the seam's rule and it is a real one here too: the page
        and its sidecars are a few megabytes and make the destination
        browsable, while the videos are the hours. A run that copies only the
        pages has done exactly what was asked of it -- and it will not satisfy
        the erase gate, because is_complete looks for the videos.
        """
        if not self._sources():
            return stopped("nothing gathered to copy")
        try:
            copied, trips = self._copy(workspace, self._everything(includeVideos))
        except OSError as error:
            # A disk that is not there, or a directory that will not be
            # written. It is reported as the step not completing rather than as
            # a traceback: the position stays where it was, and the next thing
            # the operator sees is the menu with item 8 still owed.
            return stopped("could not copy to %s: %s" % (self.destination, error))
        # Recorded only for trips whose VIDEO landed, and only after the copy
        # returned without error. That is what the erase gate reads later, so
        # it has to mean "this drive was written out", not "this run was
        # attempted": a pages-only run copies the browsable part and leaves the
        # gate exactly where it was.
        if trips:
            self._record.mark(trips)
        return did("%d files copied to %s" % (copied, self.destination))

    def _copy(self, workspace, outstanding):
        """Copy, overwriting, and report which trips got their video across."""
        copied, trips = 0, set()
        for source, rel in outstanding:
            target = self.destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))       # overwrites by design
            copied += 1
            if _is_video(rel):
                trip = Render(source.name, 0).trip_id
                if trip:
                    trips.add(trip)
            self._say(workspace, copied, len(outstanding), rel)
        return copied, trips

    def _sources(self):
        """The gathered result folders, which are the whole of what is copied.

        Asked of the callable handed in rather than worked out here, so "what a
        finished result is" has one definition and this cannot form a second
        opinion about it. Empty means item 5 has not gathered anything yet,
        which is the one state that blocks the step.
        """
        return [Path(base) for base in self._finals()]

    def _everything(self, with_videos: bool):
        """Every file in the gathered folders, pages always, media on request.

        The destination is never listed to work out what is missing. It cannot
        be trusted to be there, and "copy the lot" is also what makes a second
        export overwrite a stale render rather than skip it.
        """
        out = []
        for base in self._sources():
            for path in _walk_files(base):
                rel = path.relative_to(base.parent)
                if _is_video(rel) and not with_videos:
                    continue
                out.append((path, rel))
        return out

    def _say(self, workspace, done, total, rel) -> None:
        ui = getattr(workspace, "ui", None)
        if ui is None:
            return
        ui.debug("  copied %d/%d  %s" % (done, total, rel))

    # -- the question the erase gate rests on ------------------------------

    def is_complete(self, trip_ids, progress=None) -> Evidence:
        """Did WE copy every one of these trips out. Not: is it still there.

        This is the one place the built-in export differs sharply from a real
        plugin, and the difference is the whole point of it. A plugin owns a
        destination it can go back and look at -- a bucket, a server -- so it
        answers by looking, and whatever it says is true because only it can
        know. Nothing here may second-guess that.

        The built-in destination is a directory the operator named, and the
        ordinary reason to name one is a memory stick or an external disk. So
        it is EXPECTED to be gone: exported at lunchtime, unplugged, taken
        away. Answering by looking would then read the empty slot -- or, worse,
        a DIFFERENT disk mounted at the same path -- as though the copy had
        never happened, and Clean Workspace would refuse for good on a card
        whose footage is demonstrably on a shelf.

        So the answer is the copy itself, recorded when it succeeded, and it
        stays true afterwards. That is the same shape as the import manifest,
        which exists because the high-water mark could not tell "was copied"
        from "is still present" either.

        NO for a trip we never copied -- including one that never rendered,
        which is what keeps its footage from being erased. NO, never YES, for
        an empty list: nothing was asked about, and a vacuous yes is how a
        question about no trips clears a gate about footage.
        """
        wanted = {str(t) for t in trip_ids}
        if not wanted:
            return Evidence.NO
        try:
            return self._yes_no(wanted <= self._record.exported())
        except Exception:
            # Our own record could not be read. That is not permission.
            return Evidence.UNKNOWN

    def _yes_no(self, ok: bool) -> Evidence:
        return Evidence.YES if ok else Evidence.NO
