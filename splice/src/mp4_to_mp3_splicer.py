"""Extract MP3 audio from MP4 file handles."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable


CommandRunner = Callable[..., subprocess.CompletedProcess]
ProgressCallback = Callable[[float], None]
ProcessFactory = Callable[..., subprocess.Popen[str]]


class Mp4AudioSplicer:
    """Converts the audio stream of an MP4 file handle to an MP3 file handle."""

    def __init__(
        self,
        ffmpeg_command: str = "ffmpeg",
        ffprobe_command: str = "ffprobe",
        command_runner: CommandRunner = subprocess.run,
        process_factory: ProcessFactory = subprocess.Popen,
    ) -> None:
        self._ffmpeg_command = ffmpeg_command
        self._ffprobe_command = ffprobe_command
        self._command_runner = command_runner
        self._process_factory = process_factory

    def spliceMp3OffMp4(
        self,
        input: BinaryIO,
        progress_callback: ProgressCallback | None = None,
    ) -> BinaryIO:
        """Return a new readable MP3 handle extracted from ``input``.

        The caller retains ownership of ``input``.  The returned handle is a
        temporary binary stream positioned at its beginning and is owned by
        the caller, who should close it when finished. When supplied,
        ``progress_callback`` receives percentage values from 0.0 to 100.0.
        """
        source_path, source_cleanup = self._source_path(input)
        output_descriptor, output_path = tempfile.mkstemp(suffix=".mp3")
        os.close(output_descriptor)

        try:
            if progress_callback is None:
                self._run_ffmpeg(source_path, output_path)
            else:
                self._run_ffmpeg_with_progress(
                    source_path, output_path, progress_callback
                )
            result = tempfile.SpooledTemporaryFile(mode="w+b")
            with open(output_path, "rb") as generated_mp3:
                shutil.copyfileobj(generated_mp3, result)
            result.seek(0)
            return result
        finally:
            Path(output_path).unlink(missing_ok=True)
            if source_cleanup is not None:
                Path(source_cleanup).unlink(missing_ok=True)

    def _run_ffmpeg(self, source_path: str, output_path: str) -> None:
        self._command_runner(
            self._ffmpeg_arguments(source_path, output_path),
            check=True,
            capture_output=True,
        )

    def _run_ffmpeg_with_progress(
        self,
        source_path: str,
        output_path: str,
        progress_callback: ProgressCallback,
    ) -> None:
        duration_seconds = self._media_duration(source_path)
        command = self._ffmpeg_arguments(source_path, output_path)
        command[1:1] = ["-progress", "pipe:1", "-nostats"]
        progress_callback(0.0)
        process = self._process_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        highest_progress = 0.0
        assert process.stdout is not None
        for line in process.stdout:
            if not line.startswith("out_time_us="):
                continue
            elapsed_microseconds = int(line.partition("=")[2])
            percentage = min(99.0, elapsed_microseconds / duration_seconds / 10_000)
            if percentage > highest_progress:
                highest_progress = percentage
                progress_callback(percentage)

        return_code = process.wait()
        if return_code != 0:
            stderr = process.stderr.read() if process.stderr is not None else None
            raise subprocess.CalledProcessError(return_code, command, stderr=stderr)
        progress_callback(100.0)

    def _media_duration(self, source_path: str) -> float:
        result = self._command_runner(
            [
                self._ffprobe_command,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(result.stdout.strip())
        if duration <= 0:
            raise ValueError("The MP4 duration must be greater than zero.")
        return duration

    def _ffmpeg_arguments(self, source_path: str, output_path: str) -> list[str]:
        return [
            self._ffmpeg_command,
            "-y",
            "-i",
            source_path,
            "-vn",
            "-codec:a",
            "libmp3lame",
            output_path,
        ]

    @staticmethod
    def _source_path(input_file: BinaryIO) -> tuple[str, str | None]:
        """Get a path ffmpeg can read, materialising unnamed streams if needed."""
        name = getattr(input_file, "name", None)
        if isinstance(name, (str, os.PathLike)) and Path(name).is_file():
            return os.fspath(name), None

        descriptor, temporary_path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(descriptor, "wb") as temporary_input:
            original_position = input_file.tell() if input_file.seekable() else None
            if input_file.seekable():
                input_file.seek(0)
            shutil.copyfileobj(input_file, temporary_input)
            if original_position is not None:
                input_file.seek(original_position)
        return temporary_path, temporary_path
