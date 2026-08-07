"""Optional local speaker diarization and Whisper-segment alignment."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Protocol

from ..transcription.faster_whisper_transcriber import TranscriptSegment


@dataclass(frozen=True)
class SpeakerTurn:
    start_seconds: float
    end_seconds: float
    speaker: str


class DiarizationPipeline(Protocol):
    def __call__(self, inputs: object) -> object: ...


PipelineFactory = Callable[[str, str | None], DiarizationPipeline]


class SpeakerDiarizer:
    """Run pyannote locally and assign its speakers to transcript segments."""

    def __init__(
        self,
        model_name: str = "pyannote/speaker-diarization-3.1",
        token: str | None = None,
        pipeline_factory: PipelineFactory | None = None,
    ) -> None:
        self._model_name = model_name
        self._token = token
        self._pipeline_factory = pipeline_factory

    def diarizeMp3(self, input_file: BinaryIO) -> tuple[SpeakerTurn, ...]:
        """Return chronological speaker turns without closing the input."""
        source_path, cleanup_path = self._source_path(input_file)
        try:
            pipeline = self._create_pipeline()
            result = pipeline(source_path)
            annotation = getattr(result, "speaker_diarization", result)
            turns = (
                SpeakerTurn(float(turn.start), float(turn.end), str(speaker))
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            )
            return tuple(sorted(turns, key=lambda turn: turn.start_seconds))
        finally:
            if cleanup_path is not None:
                Path(cleanup_path).unlink(missing_ok=True)

    def _create_pipeline(self) -> DiarizationPipeline:
        if self._pipeline_factory is not None:
            return self._pipeline_factory(self._model_name, self._token)
        try:
            from pyannote.audio import Pipeline
        except ImportError as error:
            raise RuntimeError(
                "Speaker diarization requires the optional dependency: "
                "python -m pip install pyannote.audio"
            ) from error
        return Pipeline.from_pretrained(self._model_name, token=self._token)

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


class SpeakerLabeler:
    """Attach the speaker with the greatest overlap to each Whisper segment."""

    def __init__(self, turns: Iterable[SpeakerTurn]) -> None:
        self._turns = tuple(turns)

    def label(self, segment: TranscriptSegment) -> TranscriptSegment:
        overlaps: dict[str, float] = {}
        for turn in self._turns:
            overlap = min(segment.end_seconds, turn.end_seconds) - max(
                segment.start_seconds, turn.start_seconds
            )
            if overlap > 0:
                overlaps[turn.speaker] = overlaps.get(turn.speaker, 0.0) + overlap
        if not overlaps:
            return segment
        speaker = max(overlaps, key=overlaps.get)
        return replace(segment, speaker=speaker)
