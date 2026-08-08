#!/usr/bin/env python3
"""Run the DDPAI adapter over a real import and report what it found.

Not a test. The suite is fixture-only by design so it stays safe to run
mid-render, and every fixture in it was written by whoever wrote the parser
-- so the fixtures agree with the parser by construction. This reads actual
footage instead, which is the only evidence available that is not
self-confirming.

Usage:
    PYTHONPATH=src python3 tools/calibrate_ddpai.py ~/dashcam-data/import/2026-08-08
"""

import sys
from pathlib import Path

from dashcam_exporter.infrastructure.adapters.ddpai import DdpaiAdapter


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    card = Path(argv[1]).expanduser()

    adapter = DdpaiAdapter()
    print("card:      %s" % card)
    print("detected:  %s" % adapter.detect(card))
    if not adapter.detect(card):
        print("Nothing further to report -- the adapter does not claim this tree.")
        return 1

    layout = adapter.layout_for(card)
    clips = layout.clips()
    paired = [c for c in clips if c.rear is not None]
    print("clips:     %d" % len(clips))
    print("paired:    %d" % len(paired))
    print("unpaired:  %d" % (len(clips) - len(paired)))
    if clips:
        print("first:     %s  %s" % (clips[0].timestamp, clips[0].front.name))
        print("last:      %s  %s" % (clips[-1].timestamp, clips[-1].front.name))

    with_track = 0
    points = 0
    for clip in clips:
        track = layout.track_for(clip)
        if track is not None:
            with_track += 1
            points += len(track.points)
    print("with gps:  %d of %d" % (with_track, len(clips)))
    print("points:    %d" % points)
    if clips and with_track == 0:
        print("WARNING: not one clip resolved a track. Either the archives "
              "were not copied into this import, or archive selection by "
              "time overlap is wrong.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
