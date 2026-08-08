import struct
from datetime import datetime, timedelta
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.viofo import pack_record

from .card_simulator import CLIP_SECONDS, CardSimulator

MOVIE = "DCIM/Movie"


class ViofoSimulator(CardSimulator):
    """A VIOFO card. UNVERIFIED -- built from manuals and extractor source.

    The telemetry is written with the adapter's own pack_record, so this
    proves reader and writer agree and nothing more. That is stated here
    because it is the kind of thing a green test run makes easy to forget.
    """

    @property
    def name(self) -> str:
        return "viofo"

    def write(self, card_root: Path, clips: int) -> None:
        movie = card_root / MOVIE
        for relative in (movie, movie / "RO", movie / "Parking",
                         card_root / "DCIM/Photo"):
            relative.mkdir(parents=True, exist_ok=True)
        for index, at in enumerate(self._clip_times(
                clips, datetime(2026, 5, 16, 12, 0, 0))):
            directory, marker = self._destination(movie, index)
            base = at.strftime("%Y_%m%d_%H%M%S")
            front = directory / ("%s_%03d%sF.MP4" % (base, index + 1, marker))
            self._write_clip(front, index)
            # The rear camera saves two seconds later and counts its own
            # sequence numbers, exactly as owners report.
            rear_at = at + timedelta(seconds=2)
            self._write_clip(
                directory / ("%s_%03d%sR.MP4"
                             % (rear_at.strftime("%Y_%m%d_%H%M%S"),
                                index + 4, marker)), index)
            self._append_gps(front, at)

    @staticmethod
    def _destination(movie: Path, index: int) -> tuple[Path, str]:
        if index % 5 == 4:
            return movie / "RO", "E"
        if index % 3 == 2:
            return movie / "Parking", "P"
        return movie, ""

    @staticmethod
    def _append_gps(video: Path, at: datetime) -> None:
        with video.open("ab") as handle:
            for second in range(CLIP_SECONDS * 5):
                moment = at + timedelta(seconds=second)
                payload = pack_record(moment, 14.412755 + second / 10000.0,
                                      121.043745 + second / 10000.0,
                                      20.0, 30.2)
                handle.write(struct.pack(">I", 8 + len(payload))
                             + b"free" + payload)
