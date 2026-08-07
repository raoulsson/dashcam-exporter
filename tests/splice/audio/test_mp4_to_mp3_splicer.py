import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from dashcam_exporter.splice.audio.mp4_to_mp3_splicer import Mp4AudioSplicer


class Mp4AudioSplicerTest(unittest.TestCase):
    def test_splice_mp3_off_mp4_returns_a_readable_mp3_filehandle(self) -> None:
        runner = Mock()

        def create_mp3(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            Path(command[-1]).write_bytes(b"fake mp3 audio")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        runner.side_effect = create_mp3
        splicer = Mp4AudioSplicer(command_runner=runner)

        with tempfile.NamedTemporaryFile(suffix=".mp4") as input_file:
            input_file.write(b"fake mp4 video")
            input_file.flush()

            mp3_file = splicer.spliceMp3OffMp4(input_file)

        self.assertEqual(b"fake mp3 audio", mp3_file.read())
        self.assertFalse(mp3_file.closed)
        self.assertIn("-vn", runner.call_args.args[0])
        self.assertIn("libmp3lame", runner.call_args.args[0])
        mp3_file.close()

    def test_splice_mp3_off_mp4_accepts_an_unnamed_binary_filehandle(self) -> None:
        runner = Mock()

        def create_mp3(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            Path(command[-1]).write_bytes(b"mp3 from stream")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        runner.side_effect = create_mp3

        mp3_file = Mp4AudioSplicer(command_runner=runner).spliceMp3OffMp4(
            io.BytesIO(b"mp4 bytes")
        )

        self.assertEqual(b"mp3 from stream", mp3_file.read())
        mp3_file.close()

    def test_splice_mp3_off_mp4_reports_progress_to_a_callback(self) -> None:
        command_runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, "10.0\n", "")
        )
        progress_updates: list[float] = []

        class FakeProcess:
            def __init__(self, command: list[str], **_: object) -> None:
                Path(command[-1]).write_bytes(b"mp3 with progress")
                self.stdout = io.StringIO(
                    "out_time_us=2500000\nprogress=continue\nout_time_us=10000000\nprogress=end\n"
                )
                self.stderr = io.StringIO()

            def wait(self) -> int:
                return 0

        with tempfile.NamedTemporaryFile(suffix=".mp4") as input_file:
            input_file.write(b"fake mp4 video")
            input_file.flush()
            mp3_file = Mp4AudioSplicer(
                command_runner=command_runner,
                process_factory=FakeProcess,
            ).spliceMp3OffMp4(input_file, progress_updates.append)

        self.assertEqual([0.0, 25.0, 99.0, 100.0], progress_updates)
        self.assertEqual(b"mp3 with progress", mp3_file.read())
        mp3_file.close()


if __name__ == "__main__":
    unittest.main()
