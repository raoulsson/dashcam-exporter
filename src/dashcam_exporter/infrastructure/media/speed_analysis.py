"""GPS speed signal processing used by parking detection.

This module intentionally contains only transformations and onset detection for
per-second speed samples.  It has no knowledge of rendering, filesystem layout,
or trip orchestration; callers provide a Track-like object with ``speeds``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imports are for static type checkers
    from .domain import Clip


PARKING_SPEED_THRESHOLD_KMH = 3.0


def filter_gps_outliers(speeds: list[float], max_delta_kmh: float = 30.0) -> list[float]:
    """Replace isolated GPS spikes with the mean of their neighbours."""
    if len(speeds) < 3:
        return list(speeds)
    out = list(speeds)
    for i in range(1, len(speeds) - 1):
        prev, cur, nxt = speeds[i - 1], speeds[i], speeds[i + 1]
        if abs(cur - prev) > max_delta_kmh and abs(cur - nxt) > max_delta_kmh:
            out[i] = (prev + nxt) / 2.0
    return out


def smooth_speeds(speeds: list[float], window: int = 5) -> list[float]:
    """Return a same-length centre-aligned moving average."""
    n = len(speeds)
    if n < 2:
        return list(speeds)
    half = window // 2
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(speeds[lo:hi]) / (hi - lo))
    return out


def find_park_second(
    clip: "Clip",
    track,
    sustain_secs: int = 30,
    threshold_kmh: float = PARKING_SPEED_THRESHOLD_KMH,
    next_clips: list["Clip"] | None = None,
) -> int | None:
    """Find the first sustained low-speed second in ``clip``.

    ``track`` is deliberately a protocol-by-convention rather than a concrete
    import, keeping this signal layer independent of the renderer's Track
    implementation and avoiding a circular dependency.
    """
    speeds = track.speeds(clip)
    if not speeds:
        return None
    clip_len = len(speeds)
    if next_clips:
        for next_clip in next_clips:
            next_speeds = track.speeds(next_clip)
            if not next_speeds:
                break
            speeds.extend(next_speeds)
    smoothed = smooth_speeds(filter_gps_outliers(speeds))
    if len(smoothed) < sustain_secs:
        return None
    for i in range(len(smoothed) - sustain_secs + 1):
        if all(smoothed[i + j] < threshold_kmh for j in range(sustain_secs)):
            return min(i, max(0, clip_len - 1))
    return None


# Private aliases preserve the old renderer test seams for callers that import
# these helpers by name while the implementation lives in this focused module.
_filter_gps_outliers = filter_gps_outliers
_smooth_speeds = smooth_speeds

