import json
from pathlib import Path

from dashcam_exporter.domain import Channel, Clip, ClipMode, DexGpsFile, Track

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

    @staticmethod
    def track_name(stamp: str) -> str:
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
