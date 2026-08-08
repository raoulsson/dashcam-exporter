#!/usr/bin/env python3
"""Generate a card per camera, then read each one back through the registry.

Usage:
    PYTHONPATH=src python3 tools/calibrate_adapters.py [clips]

Not a test: it shells out to ffmpeg and writes hundreds of files.

What it shows: that one contract carries three unrelated filing systems.
What it does not show: that the BlackVue and VIOFO adapters read real
cards. Those cards were written by this project from the same documents the
adapters were written from, so agreement between them is self-consistency
and nothing more. Only the DDPAI adapter has been run against real footage.
"""

import sys
import tempfile
from pathlib import Path


def report(simulator, card: Path, clips: int) -> bool:
    from dashcam_exporter.infrastructure.adapters import default_registry

    simulator.write(card, clips)
    adapter = default_registry().detect(card)
    layout = adapter.layout_for(card)
    found = layout.clips()
    channels = sorted({channel.value for clip in found
                       for channel in clip.videos})
    modes = sorted({clip.mode.value for clip in found})
    tracked = [layout.track_for(clip) for clip in found]
    points = sum(len(track.points) for track in tracked if track is not None)
    with_track = sum(1 for track in tracked if track is not None)

    print("%-9s detected %-9s clips %-3d channels %-13s modes %-22s "
          "gps %d/%d, %d points"
          % (simulator.name, adapter.name, len(found), ",".join(channels),
             ",".join(modes), with_track, len(found), points))

    ok = adapter.name == simulator.name and len(found) == clips and points > 0
    if not ok:
        print("          MISMATCH -- expected %d clips detected as %s with "
              "telemetry" % (clips, simulator.name))
    return ok


def main(argv: list[str]) -> int:
    from simulator.blackvue_simulator import BlackvueSimulator
    from simulator.ddpai_simulator import DdpaiSimulator
    from simulator.viofo_simulator import ViofoSimulator

    clips = int(argv[1]) if len(argv) > 1 else 6
    every = True
    with tempfile.TemporaryDirectory() as temporary:
        for simulator in (DdpaiSimulator(), BlackvueSimulator(),
                          ViofoSimulator()):
            card = Path(temporary) / simulator.name
            card.mkdir(parents=True)
            every = report(simulator, card, clips) and every
    print()
    print("Only the DDPAI result is calibrated against a real card. The other "
          "two read back what this project wrote.")
    return 0 if every else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv))
