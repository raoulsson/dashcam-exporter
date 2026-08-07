from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Clip:
    """An immutable front/rear camera recording discovered on the card."""

    timestamp: str
    epoch_utc: int
    duration: int
    front: Path
    rear: Path | None = None

    @property
    def started_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S")

    # Compatibility aliases let the legacy orchestration consume the extracted
    # value object while it is migrated incrementally.
    @property
    def dt(self) -> datetime:
        return self.started_at

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration)

    @property
    def end(self) -> datetime:
        return self.ended_at

    def gap_after(self, previous: "Clip") -> float:
        return max(0.0, (self.started_at - previous.ended_at).total_seconds())

    def gap_before(self, previous: "Clip") -> float:
        return self.gap_after(previous)
