import logging
import subprocess
from collections.abc import Sequence

from .command_runner import CommandRunner


class FfmpegRunner(CommandRunner):
    """FFmpeg executor that suppresses only known harmless camera metadata noise."""

    _ignored_messages = (
        "have zero duration", "stream set to be discarded by default",
        "Non-monotonic DTS", "Non-monotonous DTS", "Color range not set for yuv420p",
    )

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def run(self, command: Sequence[str]) -> None:
        self._logger.debug("Executing FFmpeg: %s", subprocess.list2cmdline(command))
        process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True, bufsize=1)
        assert process.stderr is not None
        for line in process.stderr:
            if not any(message in line for message in self._ignored_messages):
                self._logger.warning("ffmpeg: %s", line.rstrip())
        process.wait()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
