"""Transcribe MP3 file handles with faster-whisper."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Protocol


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True)
class Transcription:
    language: str
    segments: tuple[TranscriptSegment, ...]

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments).strip()


class WhisperModel(Protocol):
    def transcribe(self, audio: str, **kwargs: object) -> tuple[Iterable[object], object]: ...


ModelFactory = Callable[..., WhisperModel]
ProgressCallback = Callable[[float], None]
SegmentCallback = Callable[[TranscriptSegment], None]


class FasterWhisperTranscriber:
    """Converts MP3 audio to a structured transcript using faster-whisper."""

    def __init__(
        self,
        model_name: str = "small",
        device: str = "auto",
        compute_type: str = "int8",
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._model_factory = model_factory

    def transcribeMp3(
        self,
        input: BinaryIO,
        progress_callback: ProgressCallback | None = None,
        segment_callback: SegmentCallback | None = None,
        retain_segments: bool = True,
    ) -> Transcription:
        """Return a transcription for ``input`` without closing or changing it.

        When given, ``progress_callback`` receives percentages from 0.0 to
        100.0. The initial part represents model loading; thereafter it tracks
        the timestamp of each transcribed segment. ``segment_callback`` is
        called for each segment as soon as it is transcribed. Set
        ``retain_segments`` to ``False`` when streaming to avoid retaining the
        complete transcript in memory.
        """
        source_path, cleanup_path = self._source_path(input)
        try:
            if progress_callback is not None:
                progress_callback(0.0)
            model = self._create_model()
            if progress_callback is not None:
                progress_callback(5.0)
            raw_segments, info = model.transcribe(source_path, beam_size=5)
            if progress_callback is not None:
                progress_callback(10.0)
            segments = []
            highest_progress = 10.0
            for segment in raw_segments:
                transcript_segment = TranscriptSegment(
                    segment.start, segment.end, segment.text
                )
                if segment_callback is not None:
                    segment_callback(transcript_segment)
                if retain_segments:
                    segments.append(transcript_segment)
                if progress_callback is not None and info.duration > 0:
                    percentage = min(99.0, 10.0 + 90.0 * segment.end / info.duration)
                    if percentage > highest_progress:
                        highest_progress = percentage
                        progress_callback(percentage)
            if progress_callback is not None:
                progress_callback(100.0)
            return Transcription(language=info.language, segments=tuple(segments))
        finally:
            if cleanup_path is not None:
                Path(cleanup_path).unlink(missing_ok=True)

    def _create_model(self) -> WhisperModel:
        if self._model_factory is not None:
            return self._model_factory(
                self._model_name, device=self._device, compute_type=self._compute_type
            )
        try:
            from faster_whisper import WhisperModel as FasterWhisperModel
        except ImportError as error:
            raise RuntimeError(
                "Install dependencies with 'python -m pip install -r requirements.txt'."
            ) from error
        return FasterWhisperModel(
            self._model_name, device=self._device, compute_type=self._compute_type
        )

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
