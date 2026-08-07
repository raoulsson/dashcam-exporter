#!/usr/bin/env python3
"""Improve spoken-word MP3 audio from the command line."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..audio.mp3_voice_enhancer import Mp3VoiceEnhancer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source MP3 file")
    parser.add_argument("output", type=Path, help="enhanced MP3 file")
    arguments = parser.parse_args()

    def show_progress(percentage: float) -> None:
        print(f"\rEnhancing: {percentage:5.1f}%", end="", flush=True)

    with arguments.input.open("rb") as source:
        enhanced_mp3 = Mp3VoiceEnhancer().enhanceMp3(source, show_progress)
        with arguments.output.open("wb") as destination:
            destination.write(enhanced_mp3.read())
        enhanced_mp3.close()
    print(f"\nWrote {arguments.output}")


if __name__ == "__main__":
    main()
