from __future__ import annotations

import abc
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dashcam_exporter.domain import Clip
from dashcam_exporter.application.ports.checkout import RealCheckout
from .tracking import Track

PARKING_SPEED_THRESHOLD_KMH = 3.0
DRIVE_RESUME_THRESHOLD_KMH = 5.0
DRIVE_RESUME_SUSTAIN_SECS = 30

def find_drive_resume_in_group(
    head_clips: list[Clip],
    track: "Track",
    sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
    threshold_kmh: float = DRIVE_RESUME_THRESHOLD_KMH,
) -> tuple[int, int] | None:
    """
    Scan the speeds of the first few clips of a group, concatenated, to find
    the first index at which `sustain_secs` consecutive samples are all above
    `threshold_kmh`. Returns (clip_index, offset_within_clip) where the
    sustained motion begins, or None if no such window exists.

    The speeds are sourced via Track.speeds so they're already aligned
    to each clip's VIDEO timeline (with leading zeros prepended for any
    GPS-acquisition lag at the start of a clip). That means the returned
    offset_within_clip can be used directly as a trim_start for that clip.

    Use case: drive-mode head-trim where the car may start moving in clip 0,
    clip 1, or clip 2 of a drive. The caller can drop earlier clips entirely
    via action_for[k] = "head_skip", then trim the clip containing motion to
    start `pad` seconds before offset_within_clip.
    """
    speeds: list[float] = []
    boundaries: list[int] = [0]    # cumulative speed-count after each clip
    for c in head_clips:
        clip_speeds = track.speeds(c)
        if not clip_speeds:
            # Gap in GPS coverage — can't reliably scan past this point.
            break
        speeds.extend(clip_speeds)
        boundaries.append(len(speeds))
    if len(speeds) < sustain_secs:
        return None
    for i in range(len(speeds) - sustain_secs + 1):
        if all(speeds[i + j] > threshold_kmh for j in range(sustain_secs)):
            # Map global index i back to (clip_index, offset_within_clip).
            for ci in range(len(boundaries) - 1):
                if boundaries[ci] <= i < boundaries[ci + 1]:
                    return ci, i - boundaries[ci]
            return None
    return None


def find_drive_resume_second(
    clip: Clip, track: "Track",
    sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
    threshold_kmh: float = DRIVE_RESUME_THRESHOLD_KMH,
    next_clips: list[Clip] | None = None,
) -> int | None:
    """
    Best-effort detection of when the car actually starts moving in `clip`.
    Returns the clip-second at which a sustained `sustain_secs`-long moving
    window begins, or None if no such window exists (in which case the GPS
    data is too noisy / scrambled to trust — caller falls back to a
    configurable skip).

    The 30-second default sustain is intentional: parking-mode dashcams
    record short bursts of motion-triggered video around a parked car (a
    passing car, a pedestrian, dashcam reboot self-tests) which produce
    brief GPS spikes that don't represent real driving. 30 seconds of
    continuous motion is a solid indicator that the drive has actually
    started.

    If `next_clips` is provided, their speeds are concatenated onto this
    clip's, so a sustain window that STARTS in this clip and continues into
    the next one(s) still counts. The returned index stays in this clip's
    timeline — clamped to clip.duration - 1 if motion starts at the very
    end of the clip — so the caller can still trim from second N onward.
    Without this, a drive that starts at e.g. second 40 of a 60-second clip
    can never satisfy a 30-second-within-one-clip rule and the head-trim
    silently fails open (showing the whole pre-drive pause).
    """
    # Use Track.speeds so the returned index is already in VIDEO-second
    # space (with leading 0s prepended for any GPS-lock acquisition lag).
    # That way the caller's trim_start = max(0, drive_sec - pad) lands on
    # the correct video frame, not on the GPS-fix index — which would
    # otherwise drop into action ~10 seconds before the wheels actually move.
    speeds: list[float] = track.speeds(clip)
    if not speeds:
        return None
    clip_len = len(speeds)
    if next_clips:
        for nc in next_clips:
            nspeeds = track.speeds(nc)
            if not nspeeds:
                break  # gap — don't pretend the next-next clip is contiguous
            speeds.extend(nspeeds)
    if len(speeds) < sustain_secs:
        return None
    for i in range(len(speeds) - sustain_secs + 1):
        if all(speeds[i + j] > threshold_kmh for j in range(sustain_secs)):
            # Clamp to this clip's timeline so the caller's trim_start
            # stays a valid offset inside this clip's source video.
            return min(i, max(0, clip_len - 1))
    return None


# --- Video-based drive-away detection (parking exit) -------------------------
# GPS speed is unreliable for finding the moment a parked car starts driving:
# parking-mode clips are event snippets (the cam records when a person or car
# passes), so the footage is full of OTHER things moving while the car sits
# still, and the stale/jittery GPS can't tell that apart from real driving.
# Instead, measure EGO-motion from the front video: track many features frame to
# frame (Lucas-Kanade optical flow) and take the MEDIAN flow magnitude. When the
# car is parked, most features are on the static scene (median ~0) and a passing
# car/person is just a handful of outliers the median ignores. When the car
# actually drives, the WHOLE scene sweeps (features flow outward even driving
# straight, translate/rotate when maneuvering out of a spot), so the median
# jumps by ~two orders of magnitude. We find the first sustained jump, then walk
# back to where the motion first left the parked baseline = the drive-away.
EGO_FPS            = 4        # frames/sec sampled from the clip for analysis
EGO_W, EGO_H       = 640, 400  # downscaled analysis resolution (speed)
EGO_SUSTAIN_SECS   = 1.5     # motion must persist this long to count as driving
EGO_THR_SUSTAIN    = 1.0     # median flow (px at EGO_W×EGO_H) => "driving"
EGO_THR_BASELINE   = 0.15    # walk-back stops below this (parked-noise floor)
EGO_CONTEXT_PAD    = 2       # seconds of "about to move" kept before drive-away
EGO_END_PAD        = 10      # seconds kept after the car finally comes to rest
EGO_MAX_ANALYZE_SECS = 120   # cap analysis (a clip is ≤60s, but be safe)

# Where this installation's files are. config.txt and .env sit at the checkout
# root, one level above this module now that sources live under src/: they are
# what the operator edits, and they are not source. Asked of a Checkout rather
# than walked up to from here, so that the layout is written down in one place
# and this module cannot come to disagree with pipeline.py about it.
CHECKOUT = RealCheckout(__file__)

try:
    import numpy as _np
    import cv2 as _cv2
    _HAVE_EGO = True
except Exception:
    _HAVE_EGO = False


def _ego_extract_frames(clip: Clip):
    """Sampled greyscale frames of a clip as an (n, H, W) uint8 array, or None."""
    if not _HAVE_EGO:
        return None
    cmd = ["ffmpeg", "-v", "error", "-i", str(clip.front),
           "-t", str(EGO_MAX_ANALYZE_SECS),
           "-vf", f"fps={EGO_FPS},scale={EGO_W}:{EGO_H},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True).stdout
    except Exception:
        return None
    fsz = EGO_W * EGO_H
    n = len(raw) // fsz
    if n < 1:
        return None
    return _np.frombuffer(raw[:n * fsz], dtype=_np.uint8).reshape(n, EGO_H, EGO_W)


def _ego_median_flow(frames) -> "list[float]":
    """Median Lucas-Kanade optical-flow magnitude between consecutive frames.
    Index i is the motion from frame i-1 to i; index 0 is 0. Median rejects the
    handful of features on passing objects, so it tracks WHOLE-frame ego-motion:
    ~0 parked, large when the car actually drives."""
    lk = dict(winSize=(21, 21), maxLevel=3,
              criteria=(_cv2.TERM_CRITERIA_EPS | _cv2.TERM_CRITERIA_COUNT, 20, 0.03))
    n = len(frames)
    med = [0.0] * n
    prev = frames[0]
    for i in range(1, n):
        cur = frames[i]
        p0 = _cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01, minDistance=8)
        if p0 is not None:
            p1, stt, _err = _cv2.calcOpticalFlowPyrLK(prev, cur, p0, None, **lk)
            g = stt.ravel() == 1
            if g.sum() >= 5:
                d = (p1[g] - p0[g]).reshape(-1, 2)
                med[i] = float(_np.median(_np.hypot(d[:, 0], d[:, 1])))
        prev = cur
    return med


def _ego_drive_onset(med: "list[float]") -> "int | None":
    """First frame index of sustained driving in a median-flow signal, walked
    back to where motion left the parked baseline. None if never driving."""
    n = len(med)
    sustain = max(1, int(round(EGO_SUSTAIN_SECS * EGO_FPS)))
    run_start = None
    for i in range(1, n - sustain + 1):
        if all(med[i + j] > EGO_THR_SUSTAIN for j in range(sustain)):
            run_start = i
            break
    if run_start is None:
        return None
    onset = run_start
    while onset > 1 and med[onset - 1] > EGO_THR_BASELINE:
        onset -= 1
    return onset


# The video detector's memo, module-level so the whole run shares one. It is
# also the seam the parking tests drive the detector through: they seed the
# signal a clip's frames would have produced and never open OpenCV.
_EGO_FLOW_CACHE: "dict[Path, list[float] | None]" = {}


def find_drive_away_in_group_video(clips: "list[Clip]") -> "tuple[int, float] | None":
    """Like VideoMotionDetector.drive_away_second but across the first few clips
    of a trip:
    returns (clip_index, second_within_that_clip) of the departure, so a
    head-trim can drop earlier parked clips and trim into the motion clip.
    Used for the trip START (mirror of the parking-exit case)."""
    if not _HAVE_EGO:
        return None
    allf = []
    bounds = [0]
    for c in clips:
        f = _ego_extract_frames(c)
        if f is None or len(f) == 0:
            break  # gap in coverage — don't fuse a non-contiguous clip
        allf.append(f)
        bounds.append(bounds[-1] + len(f))
    if not allf:
        return None
    frames = _np.concatenate(allf, axis=0)
    if len(frames) < 4:
        return None
    onset = _ego_drive_onset(_ego_median_flow(frames))
    if onset is None:
        return None
    for ci in range(len(bounds) - 1):
        if bounds[ci] <= onset < bounds[ci + 1]:
            return ci, (onset - bounds[ci]) / EGO_FPS
    return None


def _ego_park_onset(med: "list[float]") -> "int | None":
    """Frame index at which the car comes to a sustained STOP that lasts to the
    end of the clip (the mirror of _ego_drive_onset): the clip must contain real
    driving first, then motion drops to the parked baseline and stays there.
    None if the clip never drove, or is still driving at its end (no arrival)."""
    n = len(med)
    sustain = max(1, int(round(EGO_SUSTAIN_SECS * EGO_FPS)))
    if n < sustain + 1 or max(med) <= EGO_THR_SUSTAIN:
        return None                       # never really drove -> not an arrival
    # Anchor on the end of the last SUSTAINED driving run, not on the last
    # sample above a low baseline. Once parked, the flow signal still twitches:
    # a pedestrian crossing in front of the car produced blips of 0.15-0.44 for
    # ~15s after a full stop, and a single-frame spike of 26.9 appeared at the
    # very end of another clip (door/headlights). Walking back on the baseline
    # let any one of those veto the stop, reporting the park up to 15 seconds
    # late — or, with a trailing spike, not at all. Requiring `sustain`
    # consecutive frames of real motion ignores both.
    last_drive_end = None
    for i in range(n - 1, sustain - 2, -1):
        if all(med[i - j] > EGO_THR_SUSTAIN for j in range(sustain)):
            last_drive_end = i
            break
    if last_drive_end is None:
        return None
    park = last_drive_end + 1
    if park >= n or (n - park) < sustain:
        return None                       # still driving at the end (no park)
    return park


@dataclass(frozen=True)
class Detection:
    """A detector's answer, and which detector gave it.

    The source travels with the second because the caller prints it and, at the
    parking exit, branches on it. Returning a bare float meant every caller
    re-derived "which one answered" from the order it had asked in, and that is
    how the ladder came to be written out three times.

    Public: second, source. Already frozen, and it stays that way: a Detection
    is a reading, and a caller that edited the second while keeping the source
    would produce an answer no detector ever gave."""
    second: float
    source: str


class MotionDetector(abc.ABC):
    """Whether a clip holds the car coming to rest, or setting off again.

    Two implementations answer these, from different evidence and with opposite
    weaknesses (see VideoMotionDetector and GpsMotionDetector), and a third
    composes them into the fallback ladder. Declared as an interface rather than
    left duck-typed because the composite takes implementations it did not
    write: an implementation that answers only one of the two questions would
    otherwise fail at the moment of the render, on the one clip that needed the
    other one.

    Public: source, park_second, drive_away_second, park, drive_away. An
    implementation's evidence — a flow memo, a Track, a list of rungs — is its
    own business and stays private."""

    #: What Detection.source reports for answers from this detector.
    source = "?"

    @abc.abstractmethod
    def park_second(self, clip: Clip) -> "float | None":
        """Video-second within `clip` at which the car comes to rest and STAYS
        at rest through the end of the clip, or None if it does not park here.
        Fractional; callers that slice footage with it must round."""

    @abc.abstractmethod
    def drive_away_second(self, clip: Clip) -> "float | None":
        """Video-second within `clip` at which the car starts moving for real,
        or None if it never does. Fractional, as above."""

    def park(self, clip: Clip) -> "Detection | None":
        got = self.park_second(clip)
        return None if got is None else Detection(got, self.source)

    def drive_away(self, clip: Clip) -> "Detection | None":
        got = self.drive_away_second(clip)
        return None if got is None else Detection(got, self.source)


class VideoMotionDetector(MotionDetector):
    """Ego-motion read off the front camera. The primary detector, because it
    reads the wheels: it sees the car itself move, not a receiver's opinion
    about it. Blind where the scene gives it no features to track — a dark
    arrival under a roof — and that blindness is what the ladder is for.

    Public: the MotionDetector interface, plus seed_flow — a named test seam,
    see there."""

    source = "video"

    def __init__(self, cache: "dict[Path, list[float] | None] | None" = None):
        # Shared across instances by default so the whole run decodes each clip
        # once; see _flow.
        self._cache = _EGO_FLOW_CACHE if cache is None else cache

    def seed_flow(self, clip: Clip, signal: "list[float] | None") -> None:
        """Put a median-flow signal into the memo as if the clip had been
        decoded. EXISTS FOR THE TESTS, and is public for that reason alone.

        The parking fixtures describe a card as what the camera saw — a flow
        signal per clip — and answer the detectors from it without OpenCV and
        without a real .mp4 on disk. That seam has to exist somewhere; naming it
        here is what lets the memo itself be private, so no caller can reach
        past it and come to depend on how the memo is keyed."""
        self._cache[clip.front] = signal

    def _flow(self, clip: Clip) -> "list[float] | None":
        """The median-flow signal for a clip, computed at most once.

        Decoding a clip at EGO_FPS and running Lucas-Kanade over it is the whole
        cost of video ego-motion, and the same clip gets asked more than once:
        the parking-run boundaries ask whether a clip parks and whether it
        drives away, and the render then re-asks the exit clip. Answering from
        the signal makes every question after the first free. Keyed by path
        because a Clip is rebuilt from the scan each time; the file it names
        does not change under us within a run."""
        key = clip.front
        if key not in self._cache:
            frames = _ego_extract_frames(clip)
            self._cache[key] = (None if frames is None or len(frames) < 4
                                else _ego_median_flow(frames))
        return self._cache[key]

    def park_second(self, clip: Clip) -> "float | None":
        """Video-second within `clip` at which the car parks (drives in, then
        comes to a sustained stop through the end of the clip). None if it
        doesn't park here (still moving) or video is unavailable. Used to close
        a trip at the real arrival, not merely on entering the anchor radius."""
        med = self._flow(clip)
        if med is None:
            return None
        onset = _ego_park_onset(med)
        return None if onset is None else max(0.0, onset / EGO_FPS)

    def drive_away_second(self, clip: Clip) -> "float | None":
        """Video-second within `clip` at which the car starts driving
        (ego-motion), robust to passing people/cars; None if unavailable or no
        motion found."""
        med = self._flow(clip)
        if med is None:
            return None
        onset = _ego_drive_onset(med)
        return None if onset is None else max(0.0, onset / EGO_FPS)


class GpsMotionDetector(MotionDetector):
    """The same two questions answered from the receiver's speeds.

    Good at exactly what video is bad at and bad at what video is good at: it
    confirms a sustained standstill through a dark garage roof, and it is poor
    at spotting the START of motion, where passing traffic and a slow creep
    below the speed floor both read as nothing happening. Second on the ladder
    for that reason, never first.

    Public: the MotionDetector interface. The Track and the two thresholds are
    settled at construction and private after it — a caller that re-pointed a
    live detector at another card, or moved its parking floor between the
    arrival question and the departure one, would cut the two ends of the same
    run by different rules."""

    source = "gps"

    def __init__(self, track: "Track",
                 park_threshold_kmh: float = PARKING_SPEED_THRESHOLD_KMH,
                 drive_sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS):
        self._track = track
        self._park_threshold_kmh = park_threshold_kmh
        self._drive_sustain_secs = drive_sustain_secs

    def park_second(self, clip: Clip) -> "float | None":
        """The video-second at which speed drops below the parking threshold and
        STAYS there through the end of the clip. Same shape as _ego_park_onset:
        the clip must contain real motion first, then settle."""
        speeds = self._track.speeds(clip)
        if not speeds:
            return None
        n = len(speeds)
        sustain = max(1, int(round(EGO_SUSTAIN_SECS)))
        if n < sustain + 1 or max(speeds) <= self._park_threshold_kmh:
            return None                   # never really drove -> not an arrival
        k = n - 1
        while k >= 1 and speeds[k] < self._park_threshold_kmh:
            k -= 1
        park = k + 1
        if park >= n or (n - park) < sustain:
            return None                   # still moving at the end (no park)
        return float(park)

    def drive_away_second(self, clip: Clip) -> "float | None":
        """The first sustained moving window in the clip's speeds — see
        find_drive_resume_second for why the sustain is as long as it is."""
        got = find_drive_resume_second(clip, self._track,
                                       sustain_secs=self._drive_sustain_secs)
        return None if got is None else float(got)


class FirstAnswerDetector(MotionDetector):
    """The fallback ladder, in one place: ask each detector in turn, keep the
    first answer.

    Order is the whole content of this class. Video comes first because it reads
    the wheels; GPS answers for the arrival too dark to track. It used to be
    written out as `if x is None: x = other(...)` at the three sites that needed
    it — the parking-run arrival, the trip's end-trim and the parking exit —
    which is why a fix to the order landed at one of them and not the other
    two, and the arrival cut stayed wrong for months after the end-trim was
    right. Building the ladder from an empty or GPS-only list is how
    --no-video-drive-detect is honoured; there is no flag to consult in here.

    Public: the MotionDetector interface, plus `detectors` read-only — the rungs
    ARE the class's answer to "which evidence, in what order", and that is the
    one thing a caller building a ladder from a flag needs to be able to check.
    Read-only because a ladder whose order could be edited after construction is
    the very bug the class was extracted to end."""

    source = "ladder"

    def __init__(self, *detectors: MotionDetector):
        self._detectors = tuple(detectors)

    @property
    def detectors(self) -> "tuple[MotionDetector, ...]":
        """The rungs, in the order they are asked. A tuple, so reading it cannot
        reorder it."""
        return self._detectors

    def park(self, clip: Clip) -> "Detection | None":
        for d in self._detectors:
            got = d.park(clip)
            if got is not None:
                return got
        return None

    def drive_away(self, clip: Clip) -> "Detection | None":
        for d in self._detectors:
            got = d.drive_away(clip)
            if got is not None:
                return got
        return None

    def park_second(self, clip: Clip) -> "float | None":
        got = self.park(clip)
        return None if got is None else got.second

    def drive_away_second(self, clip: Clip) -> "float | None":
        got = self.drive_away(clip)
        return None if got is None else got.second


# The one video detector of a run. Module-level because its cache is: every
# caller must reach the same memo or the render decodes the card twice.
VIDEO_MOTION = VideoMotionDetector()


def motion_ladder(track: "Track",
                  use_video: bool = True,
                  drive_sustain_secs: int = DRIVE_RESUME_SUSTAIN_SECS,
                  ) -> FirstAnswerDetector:
    """The detector every arrival and departure question goes through.

    `use_video=False` is --no-video-drive-detect: the video rung is left out
    rather than disabled inside it, so the flag is read once at the edge and the
    detectors never learn it exists."""
    rungs: list[MotionDetector] = []
    if use_video:
        rungs.append(VIDEO_MOTION)
    rungs.append(GpsMotionDetector(track, drive_sustain_secs=drive_sustain_secs))
    return FirstAnswerDetector(*rungs)
