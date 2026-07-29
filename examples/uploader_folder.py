"""A complete, working WebsiteUploaderInterface: publish to a folder.

The point of this file is to be READ. It is the shortest thing that is still a
real implementation — no network, no credentials, no second repo — so what is
left is the shape every target has to have, and nothing else.

Point `website_uploader` at it in config.txt (or `SET_WEBSITE_UPLOADER` in
.env, which is the same setting and stays out of git):

    website_uploader = ~/dev/dashcam-exporter/examples/uploader_folder.py:FolderTarget

The destination is this implementation's own business, not the exporter's:
DASHCAM_FOLDER_TARGET, defaulting to ~/dashcam-published. An implementation is
constructed with no arguments and reads its own settings from wherever it
likes — the exporter has exactly one setting about publishing, which is which
class to load.

WHAT A FOLDER CAN AND CANNOT ANSWER

  holds()     yes. The file is there at that size, or it is not.
  published() NO — as in "the question does not arise". A folder holds files;
              it does not serve them. That is what Answers.not_applicable is
              for, and it is NOT the same as "I could not check today", which
              is Answers.unknown and which fails closed. Getting these two the
              wrong way round is how Clean Workspace erases footage on the
              strength of a check that never ran.
  carries()   yes, by trip id — the published-then-cleaned-up case, where
              there is no local render left to key on.

A real web target answers published() with Answers.of(...) after asking
whatever actually serves the videos.
"""

from __future__ import annotations

import itertools
import os
import shutil
from pathlib import Path

from menu import Evidence
from uploader import Answers, Owed, Report, WebsiteUploaderInterface


def _destination() -> Path:
    return Path(os.environ.get("DASHCAM_FOLDER_TARGET")
                or "~/dashcam-published").expanduser()


def _name_of(path) -> str:
    return path.name


class FolderTarget(WebsiteUploaderInterface):
    """Copy the renders into a folder, and answer honestly about what is in it."""

    def __init__(self):
        self._dir = _destination()

    def name(self) -> str:
        return "folder(%s)" % self._dir

    def describe(self) -> str:
        return "Stage the renders for %s. Nothing is sent anywhere." % self._dir

    # -- 1. do the work ----------------------------------------------------
    def build(self, world, ui) -> Report:
        """Nothing to build: what this target publishes IS the renders.

        A target with an index to generate does it here — this is item 6, and
        it runs before upload() by construction, so build() may assume nothing
        about ordering and does not have to check it.
        """
        ui.say("  %s publishes the renders as they are; nothing to build."
               % self.name())
        return Report(True, "nothing to build")

    def upload(self, world, ui) -> Report:
        """Copy across whatever is not there yet, and say what happened."""
        self._dir.mkdir(parents=True, exist_ok=True)
        copied = [self._copy(r, ui) for r in self._owed_renders(world.renders)]
        return Report(True, "%d render(s) copied" % sum(copied))

    def _copy(self, render, ui) -> int:
        ui.say("  -> %s" % render.name)
        shutil.copy2(str(render.path), str(self._dir / render.name))
        return 1

    # -- 2. may it run, and would it do anything ---------------------------
    def why_not_build(self, world):
        """Cheap and local: this is asked on every menu draw."""
        return None

    def why_not_upload(self, world):
        parent = self._dir.parent
        if parent.is_dir():
            return None
        return "%s does not exist, so %s cannot be written" % (parent, self._dir)

    def owes(self, renders) -> Owed:
        return Owed.just(r.name for r in self._owed_renders(renders))

    def _owed_renders(self, renders):
        return list(itertools.filterfalse(self._same_file, renders))

    # -- 3. what is already at the destination -----------------------------
    def holds(self, renders) -> Answers:
        """Name AND size. A name-only match let a re-render at a different size
        read as already published while the local copy was about to go."""
        return Answers.of({r.name: self._yes_no(self._same_file(r)) for r in renders},
                          detail=("read from %s" % self._dir,))

    def published(self, renders) -> Answers:
        return Answers.not_applicable("a folder holds files; it does not serve them")

    def carries(self, trip_ids) -> Answers:
        there = self._names()
        return Answers.of(map(lambda t: (t, self._carries_one(t, there)), trip_ids))

    def _carries_one(self, trip_id, names) -> Evidence:
        return self._yes_no(any(map(lambda n: n.startswith(trip_id), names)))

    # -- the folder itself -------------------------------------------------
    def _same_file(self, render) -> bool:
        return self._size_of(self._dir / render.name) == render.size

    def _size_of(self, path):
        try:
            return path.stat().st_size
        except OSError:
            return None

    def _names(self):
        try:
            return sorted(map(_name_of, self._dir.iterdir()))
        except OSError:
            return []

    def _yes_no(self, ok) -> Evidence:
        return Evidence.YES if ok else Evidence.NO

    def status_lines(self):
        return ("  Folder       %s  %d file(s)" % (self._dir, len(self._names())),)
