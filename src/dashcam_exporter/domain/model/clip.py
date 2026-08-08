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
