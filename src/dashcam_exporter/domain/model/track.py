from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One GPS fix, in our units: decimal degrees, km/h, UTC.

    Cameras disagree about all three -- NMEA ddmm.mmmm, knots, local time,
    epoch milliseconds -- and every one of those disagreements is an adapter's
    problem, settled before a point reaches here.
    """

    lat: float
    lon: float
    kmh: float
    at_utc: datetime


@dataclass(frozen=True, slots=True)
class Track:
    """The fixes belonging to one clip, in ascending time order."""

    points: tuple[TrackPoint, ...]

    @property
    def is_empty(self) -> bool:
        return not self.points

    @property
    def started_at(self) -> datetime | None:
        return self.points[0].at_utc if self.points else None

    @property
    def ended_at(self) -> datetime | None:
        return self.points[-1].at_utc if self.points else None
