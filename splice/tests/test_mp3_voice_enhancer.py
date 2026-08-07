import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mp3_voice_enhancer import Mp3VoiceEnhancer


class Mp3VoiceEnhancerTest(unittest.TestCase):
    def test_enhance_mp3_applies_the_requested_filter_chain(self) -> None:
        runner = Mock()

        def create_mp3(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            Path(command[-1]).write_bytes(b"enhanced audio")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        runner.side_effect = create_mp3
        with tempfile.NamedTemporaryFile(suffix=".mp3") as source:
            source.write(b"input audio")
            source.flush()
            enhanced = Mp3VoiceEnhancer(command_runner=runner).enhanceMp3(source)

        command = runner.call_args.args[0]
        filters = command[command.index("-af") + 1]
        self.assertIn("afftdn", filters)
        self.assertIn("acompressor", filters)
        self.assertIn("loudnorm", filters)
        self.assertEqual(b"enhanced audio", enhanced.read())
        enhanced.close()

    def test_enhance_mp3_reports_progress(self) -> None:
        command_runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, "8.0\n", "")
        )
        updates: list[float] = []

        class FakeProcess:
            def __init__(self, command: list[str], **_: object) -> None:
                Path(command[-1]).write_bytes(b"enhanced")
                self.stdout = io.StringIO("out_time_us=4000000\nprogress=end\n")
                self.stderr = io.StringIO()

            def wait(self) -> int:
                return 0

        enhanced = Mp3VoiceEnhancer(
            command_runner=command_runner,
            process_factory=FakeProcess,
        ).enhanceMp3(io.BytesIO(b"input audio"), updates.append)

        self.assertEqual([0.0, 50.0, 100.0], updates)
        enhanced.close()
