import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from dashcam_exporter.domain import DexGpsFile

from . import card_access
from .canonical_workspace import CanonicalWorkspace

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
LOG_SUFFIXES = (".txt",)


@dataclass(frozen=True, slots=True)
class NormalizationPlan:
    """What normalising this import would do, or did.

    The same type describes a dry run and a real one, so what the operator
    was shown and what happened cannot drift into two different vocabularies.
    """

    adapter: str
    clips: int
    moves: int
    copies: int
    tracks: int
    already: int

    @property
    def is_noop(self) -> bool:
        return not (self.moves or self.copies or self.tracks)

    def describe(self) -> str:
        return ("adapter %s: %d clips, %d videos to move, %d files to copy, "
                "%d tracks to write, %d already in place"
                % (self.adapter, self.clips, self.moves, self.copies,
                   self.tracks, self.already))


class Normalizer:
    """Turns a local vendor-shaped import into a canonical workspace.

    The card is never touched. Importing is a verbatim rsync onto the disk
    named by import_dir, the card comes out, and only then does this run --
    so a camera's filing system is undone with the footage safely on a disk
    rather than while it is the only copy.

    Videos MOVE. Images and logs are COPIED, because they are small and
    leaving the originals costs nothing while a half-finished rename of the
    footage would cost everything. GPS is TRANSFORMED: whatever the camera
    wrote becomes our own track file, which is the whole point.
    """

    def __init__(self, source_root, workspace=None,
                 logger: logging.Logger | None = None) -> None:
        self._source = Path(source_root)
        self._workspace = CanonicalWorkspace(workspace or source_root)
        self._logger = logger or logging.getLogger(__name__)

    @property
    def workspace(self) -> CanonicalWorkspace:
        return self._workspace

    def plan(self) -> NormalizationPlan:
        return self._run(apply=False)

    def apply(self) -> NormalizationPlan:
        return self._run(apply=True)

    def _run(self, apply: bool) -> NormalizationPlan:
        layout = card_access.layout_for(self._source)
        if layout is None:
            return NormalizationPlan("none", 0, 0, 0, 0, 0)

        clips = layout.clips()
        moves = copies = tracks = already = 0
        normalised = []

        for clip in clips:
            moved = {}
            for channel, source in clip.videos.items():
                target = self._workspace.clips_dir / self._workspace.video_name(
                    clip.timestamp, channel, Path(source).suffix.lower())
                moved[channel] = target
                if target.exists():
                    already += 1
                    continue
                moves += 1
                if apply:
                    self._move(Path(source), target)
            normalised.append(self._with_videos(clip, moved))

            target = self._workspace.tracks_dir / self._workspace.track_name(
                clip.timestamp)
            if target.exists():
                already += 1
            else:
                track = layout.track_for(clip)
                if track is not None:
                    tracks += 1
                    if apply:
                        DexGpsFile.write(target, track)

        copies = self._sidecars(apply)
        if apply:
            self._workspace.write_manifest(normalised)
        return NormalizationPlan(self._adapter_name(), len(clips), moves,
                                 copies, tracks, already)

    def _adapter_name(self) -> str:
        from dashcam_exporter.infrastructure.adapters import (
            AmbiguousCard, NoAdapterFound, default_registry)
        try:
            return default_registry().detect(self._source).name
        except (NoAdapterFound, AmbiguousCard):
            return "none"

    @staticmethod
    def _with_videos(clip, videos):
        return replace(clip, videos=videos)

    def _sidecars(self, apply: bool) -> int:
        """Images and logs, copied under canonical names where they have one.

        Copied rather than moved: they are small, and an interrupted run that
        left the footage half-renamed and the photos gone would be the worst
        of both. An unstamped file keeps its own name -- IPSRecord.txt is the
        camera's drive log and belongs to the card, not to any one clip.
        """
        copied = 0
        layout = card_access.layout_for(self._source)
        for path in sorted(self._source.rglob("*")):
            if not path.is_file():
                continue
            if self._inside_workspace(path):
                continue
            suffix = path.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                destination = self._workspace.images_dir
            elif suffix in LOG_SUFFIXES:
                destination = self._workspace.logs_dir
            else:
                continue
            stamp = layout.stamp_of(path) if layout is not None else None
            name = ("%s%s" % (stamp, suffix)) if stamp else path.name
            target = destination / name
            if target.exists():
                continue
            copied += 1
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        return copied

    def _inside_workspace(self, path: Path) -> bool:
        for directory in (self._workspace.clips_dir, self._workspace.images_dir,
                          self._workspace.logs_dir, self._workspace.tracks_dir):
            if directory in path.parents:
                return True
        return False

    def _move(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.replace(target)
        except OSError:
            # Across filesystems replace() cannot rename. Falls back to a
            # copy and unlink, which is what shutil.move does anyway.
            shutil.move(str(source), str(target))
