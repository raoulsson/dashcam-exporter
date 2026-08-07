#!/usr/bin/env python3
"""Transcribe an MP3 file with faster-whisper."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from faster_whisper_transcriber import FasterWhisperTranscriber
from paragraph_writer import ParagraphWriter
from speaker_diarizer import SpeakerDiarizer, SpeakerLabeler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source MP3 file")
    parser.add_argument("output", type=Path, help="destination text file")
    parser.add_argument(
        "--model", default="small", help="faster-whisper model size (default: small)"
    )
    parser.add_argument(
        "--timeline",
        type=Path,
        help="JSON paragraph-to-media timeline (default: <output>.timeline.json)",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="identify speakers locally with pyannote.audio",
    )
    parser.add_argument(
        "--diarization-model",
        default="pyannote/speaker-diarization-3.1",
        help="pyannote model name or local path",
    )
    parser.add_argument(
        "--hf-token",
        help="Hugging Face token for gated pyannote models (or set HF_TOKEN)",
    )
    arguments = parser.parse_args()

    def show_progress(percentage: float) -> None:
        print(f"\rTranscribing: {percentage:5.1f}%", end="", flush=True)

    timeline_path = arguments.timeline or arguments.output.with_suffix(".timeline.json")
    if arguments.diarize:
        with arguments.input.open("rb") as source:
            transcription = FasterWhisperTranscriber(
                model_name=arguments.model
            ).transcribeMp3(
                source,
                progress_callback=show_progress,
            )

        print("\nIdentifying distinct speakers...", flush=True)
        with arguments.input.open("rb") as diarization_source:
            turns = SpeakerDiarizer(
                model_name=arguments.diarization_model,
                token=arguments.hf_token or os.environ.get("HF_TOKEN"),
            ).diarizeMp3(diarization_source)
        labeler = SpeakerLabeler(turns)

        with arguments.output.open("w", encoding="utf-8") as destination:
            paragraph_writer = ParagraphWriter(destination)
            for segment in transcription.segments:
                paragraph_writer.write_segment(labeler.label(segment))
            paragraph_writer.close()
            paragraph_writer.write_timeline(timeline_path)
    else:
        with arguments.input.open("rb") as source, arguments.output.open(
            "w", encoding="utf-8"
        ) as destination:
            paragraph_writer = ParagraphWriter(destination)
            transcription = FasterWhisperTranscriber(
                model_name=arguments.model
            ).transcribeMp3(
                source,
                progress_callback=show_progress,
                segment_callback=paragraph_writer.write_segment,
                retain_segments=False,
            )
            paragraph_writer.close()
            paragraph_writer.write_timeline(timeline_path)
    print(
        f"\nWrote {arguments.output} ({transcription.language})\n"
        f"Wrote {timeline_path}"
    )


if __name__ == "__main__":
    main()
