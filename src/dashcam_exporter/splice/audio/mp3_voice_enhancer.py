"""Improve spoken-word MP3 audio with an FFmpeg filter pipeline."""

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


class Mp3VoiceEnhancer:
    """Applies noise reduction, voice enhancement, and loudness normalization."""

    FILTER_CHAIN = (
        "afftdn=nr=15:nf=-30:tn=1,"
        "highpass=f=80,lowpass=f=12000,"
        "equalizer=f=2500:t=q:w=1.2:g=4,"
        "acompressor=threshold=0.09:ratio=3:attack=20:release=250,"
        "loudnorm=I=-16:LRA=11:TP=-1.5"
    )

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

    def enhanceMp3(
        self,
        input: BinaryIO,
        progress_callback: ProgressCallback | None = None,
    ) -> BinaryIO:
        """Return an enhanced MP3 handle without modifying ``input``.

        The optional callback receives progress percentages from 0.0 through
        100.0. The caller owns and must close the returned handle.
        """
        source_path, source_cleanup = self._source_path(input)
        descriptor, output_path = tempfile.mkstemp(suffix=".mp3")
        os.close(descriptor)

        try:
            if progress_callback is None:
                self._command_runner(
                    self._ffmpeg_arguments(source_path, output_path),
                    check=True,
                    capture_output=True,
                )
            else:
                self._run_with_progress(source_path, output_path, progress_callback)
            result = tempfile.SpooledTemporaryFile(mode="w+b")
            with open(output_path, "rb") as enhanced_mp3:
                shutil.copyfileobj(enhanced_mp3, result)
            result.seek(0)
            return result
        finally:
            Path(output_path).unlink(missing_ok=True)
            if source_cleanup is not None:
                Path(source_cleanup).unlink(missing_ok=True)

    def _run_with_progress(
        self,
        source_path: str,
        output_path: str,
        progress_callback: ProgressCallback,
    ) -> None:
        duration = self._duration(source_path)
        command = self._ffmpeg_arguments(source_path, output_path)
        command[1:1] = ["-progress", "pipe:1", "-nostats"]
        progress_callback(0.0)
        process = self._process_factory(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        highest_progress = 0.0
        assert process.stdout is not None
        for line in process.stdout:
            if not line.startswith("out_time_us="):
                continue
            percentage = min(
                99.0, int(line.partition("=")[2]) / duration / 10_000
            )
            if percentage > highest_progress:
                highest_progress = percentage
                progress_callback(percentage)
        return_code = process.wait()
        if return_code != 0:
            stderr = process.stderr.read() if process.stderr is not None else None
            raise subprocess.CalledProcessError(return_code, command, stderr=stderr)
        progress_callback(100.0)

    def _duration(self, source_path: str) -> float:
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
            raise ValueError("The MP3 duration must be greater than zero.")
        return duration

    def _ffmpeg_arguments(self, source_path: str, output_path: str) -> list[str]:
        return [
            self._ffmpeg_command,
            "-y",
            "-i",
            source_path,
            "-af",
            self.FILTER_CHAIN,
            "-codec:a",
            "libmp3lame",
            output_path,
        ]

    @staticmethod
    def _source_path(input_file: BinaryIO) -> tuple[str, str | None]:
        name = getattr(input_file, "name", None)
        if isinstance(name, (str, os.PathLike)) and Path(name).is_file():
            return os.fspath(name), None

        descriptor, temporary_path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(descriptor, "wb") as temporary_input:
            original_position = input_file.tell() if input_file.seekable() else None
            if input_file.seekable():
                input_file.seek(0)
            shutil.copyfileobj(input_file, temporary_input)
            if original_position is not None:
                input_file.seek(original_position)
        return temporary_path, temporary_path
