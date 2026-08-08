import struct
from datetime import datetime, timedelta
from pathlib import Path

from .card_simulator import CLIP_SECONDS, CardSimulator

RECORD = "BlackVue/Record"


class BlackvueSimulator(CardSimulator):
    """A BlackVue card. UNVERIFIED -- built from manuals, not from a card.

    Half the clips get a .gps sidecar and half get the telemetry appended as
    a 'gps ' box, because both regimes exist in the wild and an adapter that
    only ever meets one of them has not been tested.
    """

    @property
    def name(self) -> str:
        return "blackvue"

    def write(self, card_root: Path, clips: int) -> None:
        record = card_root / RECORD
        record.mkdir(parents=True, exist_ok=True)
        (card_root / "BlackVue/Config").mkdir(parents=True, exist_ok=True)
        for index, at in enumerate(self._clip_times(
                clips, datetime(2021, 1, 27, 15, 50, 52))):
            base = at.strftime("%Y%m%d_%H%M%S")
            mode = "N" if index % 3 else "P"
            front = record / ("%s_%sF.mp4" % (base, mode))
            self._write_clip(front, index)
            self._write_clip(record / ("%s_%sR.mp4" % (base, mode)), index)
            payload = self._sidecar(at).encode()
            if index % 2:
                (record / ("%s_%s.gps" % (base, mode))).write_bytes(payload)
            else:
                self._append_box(front, b"gps ", payload + b"\x00")

    def _sidecar(self, at: datetime) -> str:
        lines = []
        for second in range(CLIP_SECONDS * 5):
            moment = at + timedelta(seconds=second)
            epoch_ms = int(moment.timestamp() * 1000)
            lines.append(
                "[%d]$GNRMC,%s.00,A,4529.%05d,N,07337.%05d,W,"
                "6.225,35.34,%s,,,A*52"
                % (epoch_ms, moment.strftime("%H%M%S"), 87489 + second,
                   1215 + second, moment.strftime("%d%m%y")))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _append_box(video: Path, fourcc: bytes, payload: bytes) -> None:
        with video.open("ab") as handle:
            handle.write(struct.pack(">I", 8 + len(payload)) + fourcc + payload)
