#!/usr/bin/env python3
"""Write a simulated dashcam card.

Usage:
    PYTHONPATH=src python3 tools/simulate_card.py <camera> <destination> [clips]

Cameras: ddpai, blackvue, viofo

The DDPAI card is calibrated against a real one. The other two are built
from manuals and open-source parsers, and the adapters that read them come
from the same documents -- so they demonstrate the contract, not the camera.
"""

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    from simulator.blackvue_simulator import BlackvueSimulator
    from simulator.ddpai_simulator import DdpaiSimulator
    from simulator.viofo_simulator import ViofoSimulator

    simulators = {simulator.name: simulator
                  for simulator in (DdpaiSimulator(), BlackvueSimulator(),
                                    ViofoSimulator())}
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    camera = argv[1]
    if camera not in simulators:
        print("Unknown camera %r. Known: %s"
              % (camera, ", ".join(sorted(simulators))))
        return 2
    destination = Path(argv[2]).expanduser()
    clips = int(argv[3]) if len(argv) == 4 else 6
    destination.mkdir(parents=True, exist_ok=True)
    simulators[camera].write(destination, clips)
    print("wrote a %s card of %d clips to %s" % (camera, clips, destination))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv))
