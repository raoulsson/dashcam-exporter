#!/usr/bin/env python3
"""Command-line entry point for extracting MP3 audio from an MP4 file."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..audio.mp4_to_mp3_splicer import Mp4AudioSplicer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source MP4 file")
    parser.add_argument("output", type=Path, help="destination MP3 file")
    arguments = parser.parse_args()

    def show_progress(percentage: float) -> None:
        print(f"\rConverting: {percentage:5.1f}%", end="", flush=True)

    with arguments.input.open("rb") as mp4_file:
        mp3_file = Mp4AudioSplicer().spliceMp3OffMp4(
            mp4_file, progress_callback=show_progress
        )
        with arguments.output.open("wb") as destination:
            destination.write(mp3_file.read())
        mp3_file.close()

    print(f"\nWrote {arguments.output}")


if __name__ == "__main__":
    main()
