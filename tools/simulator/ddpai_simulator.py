import io
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from .card_simulator import CLIP_SECONDS, CardSimulator

# The skeleton was read off a real card, including the directories this tool
# never opens. They are written because a simulator that only creates what
# the adapter reads cannot catch an import that copies too much.
SKELETON = ("DCIM/200video/front", "DCIM/200video/rear",
            "DCIM/201photo/front", "DCIM/201photo/rear", "DCIM/201photo/tmp",
            "DCIM/202thumb/front", "DCIM/202thumb/rear", "DCIM/202thumb/tmp",
            "DCIM/203gps/tar", "DCIM/203gps/tmp", "DCIM/207log/tmp")

# Local wall clock in the filenames, UTC in the telemetry. Eight hours on
# the card this was calibrated against, and reproducing it is the point:
# a simulator that used one clock for both would have passed the bug that
# calibration caught.
UTC_OFFSET_HOURS = 8
ARCHIVE_CLIPS = 9


class DdpaiSimulator(CardSimulator):
    """A DDPAI card, calibrated against a real one."""

    @property
    def name(self) -> str:
        return "ddpai"

    def write(self, card_root: Path, clips: int) -> None:
        for relative in SKELETON:
            (card_root / relative).mkdir(parents=True, exist_ok=True)
        start = datetime(2026, 8, 6, 17, 5, 29)
        times = self._clip_times(clips, start)
        for index, at in enumerate(times):
            stamp = at.strftime("%Y%m%d%H%M%S")
            self._write_clip(
                card_root / "DCIM/200video/front" / ("%s_%04d.mp4"
                                                     % (stamp, 60)), index)
            # The rear camera runs a second behind, as the real one does.
            rear_stamp = (at + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
            self._write_clip(
                card_root / "DCIM/200video/rear" / ("%s_%04d_A.mp4"
                                                    % (rear_stamp, 60)), index)
        self._write_archives(card_root, times)
        self._write_placeholders(card_root)

    def _write_archives(self, card_root: Path, times) -> None:
        tar_directory = card_root / "DCIM/203gps/tar"
        for offset in range(0, len(times), ARCHIVE_CLIPS):
            batch = times[offset:offset + ARCHIVE_CLIPS]
            span = 60 * len(batch)
            name = "%s_%04d.git" % (batch[0].strftime("%Y%m%d%H%M%S"), span)
            with tarfile.open(tar_directory / name, "w") as handle:
                for at in batch:
                    stamp = at.strftime("%Y%m%d%H%M%S")
                    payload = self._nmea(at).encode()
                    info = tarfile.TarInfo("%s_%04d.gpx" % (stamp, 60))
                    info.size = len(payload)
                    handle.addfile(info, io.BytesIO(payload))

    def _nmea(self, at) -> str:
        utc = at - timedelta(hours=UTC_OFFSET_HOURS)
        lines = []
        for second in range(CLIP_SECONDS * 5):
            moment = utc + timedelta(seconds=second)
            lines.append(
                "$GPRMC,%s.000,A,1424.%05d,N,12102.%05d,E,7.12,30.23,%s,,,A,V*3D"
                % (moment.strftime("%H%M%S"), 76532 + second,
                   62468 + second, moment.strftime("%d%m%y")))
        return "\n".join(lines) + "\n"

    def _write_placeholders(self, card_root: Path) -> None:
        """The pre-allocated slots a freshly formatted card carries.

        Identical size, one mtime, stamps counting up from an unset clock.
        Written because an adapter that opens them produces a warning per
        file, and nothing but a real card would have taught us they exist.
        """
        tmp = card_root / "DCIM/203gps/tar/tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        base = datetime(1970, 1, 1, 0, 45, 10)
        for index in range(3):
            at = base + timedelta(seconds=100 * index)
            name = "%s_0100_T.git" % at.strftime("%Y%m%d%H%M%S")
            (tmp / name).write_bytes(b"\x00" * 4096)
