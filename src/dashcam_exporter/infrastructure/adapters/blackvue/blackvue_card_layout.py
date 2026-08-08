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
