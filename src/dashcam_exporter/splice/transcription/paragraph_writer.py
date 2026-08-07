"""Streaming paragraph formatting for transcribed audio segments."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from .faster_whisper_transcriber import TranscriptSegment


@dataclass(frozen=True)
class ParagraphTimelineEntry:
    paragraph_index: int
    start_seconds: float
    end_seconds: float
    start_character: int
    end_character: int
    speaker: str | None = None


class ParagraphWriter:
    """Groups transcript segments into readable paragraphs and flushes them.

    Only the paragraph currently being assembled is retained in memory.
    """

    def __init__(
        self,
        destination: TextIO,
        minimum_characters: int = 350,
        maximum_characters: int = 700,
        pause_seconds: float = 1.5,
    ) -> None:
        if minimum_characters > maximum_characters:
            raise ValueError("minimum_characters cannot exceed maximum_characters")
        self._destination = destination
        self._minimum_characters = minimum_characters
        self._maximum_characters = maximum_characters
        self._pause_seconds = pause_seconds
        self._fragments: list[str] = []
        self._character_count = 0
        self._paragraph_start_seconds: float | None = None
        self._previous_end_seconds: float | None = None
        self._timeline: list[ParagraphTimelineEntry] = []
        self._written_character_count = 0
        self._speaker: str | None = None

    def write_segment(self, segment: TranscriptSegment) -> None:
        """Add one segment, flushing a paragraph when a boundary is found."""
        text = self._clean_text(segment.text)
        if not text:
            return

        if self._should_flush_before(segment, text):
            self.flush()
        if self._paragraph_start_seconds is None:
            self._paragraph_start_seconds = segment.start_seconds
            self._speaker = segment.speaker
        self._fragments.append(text)
        self._character_count += len(text) + (1 if len(self._fragments) > 1 else 0)
        self._previous_end_seconds = segment.end_seconds

    def flush(self) -> None:
        """Write the current paragraph and immediately flush the destination."""
        if not self._fragments:
            return
        paragraph = " ".join(self._fragments)
        if self._speaker is not None:
            paragraph = f"{self._speaker}: {paragraph}"
        self._destination.write(paragraph + "\n\n")
        self._destination.flush()
        self._timeline.append(
            ParagraphTimelineEntry(
                paragraph_index=len(self._timeline),
                start_seconds=self._paragraph_start_seconds or 0.0,
                end_seconds=self._previous_end_seconds or 0.0,
                start_character=self._written_character_count,
                end_character=self._written_character_count + len(paragraph),
                speaker=self._speaker,
            )
        )
        self._written_character_count += len(paragraph) + 2
        self._fragments.clear()
        self._character_count = 0
        self._paragraph_start_seconds = None
        self._speaker = None

    def close(self) -> None:
        """Flush the final partial paragraph without closing the destination."""
        self.flush()

    def write_timeline(self, destination: Path) -> None:
        """Write a JSON sidecar mapping transcript paragraphs to media time."""
        destination.write_text(
            json.dumps(
                {
                    "format": "paragraph-timeline/v1",
                    "paragraphs": [
                        {
                            key: value
                            for key, value in asdict(entry).items()
                            if key != "speaker" or value is not None
                        }
                        for entry in self._timeline
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _should_flush_before(self, segment: TranscriptSegment, text: str) -> bool:
        if not self._fragments:
            return False
        if segment.speaker != self._speaker:
            return True
        gap = segment.start_seconds - (self._previous_end_seconds or 0.0)
        if gap >= self._pause_seconds:
            return True
        current_text = self._fragments[-1]
        if self._character_count >= self._minimum_characters and self._ends_sentence(
            current_text
        ):
            return True
        return self._character_count + len(text) + 1 > self._maximum_characters

    @staticmethod
    def _clean_text(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def _ends_sentence(text: str) -> bool:
        return bool(re.search(r"[.!?][\]\"')]*$", text))
