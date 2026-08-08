#!/usr/bin/env python3
"""Normalise a local import into the canonical workspace.

Usage:
    PYTHONPATH=src python3 tools/normalize_import.py <import-dir> [--apply]

Without --apply this reports what it WOULD do and writes nothing.

The card is never touched by this. The order is: card in, rsync onto the
disk named by import_dir, card out, then this -- so a camera's filing
system is undone with the footage already safe on a disk, rather than while
it is still the only copy.

Videos are MOVED into clips/ under canonical names, so a 46 GB import is
not duplicated. Images and logs are COPIED into images/ and logs/, small
enough that leaving the originals costs nothing. GPS is TRANSFORMED into
tracks/<stamp>.json, our own format, which is the point of the exercise.
"""

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from dashcam_exporter.application.workflow.normalizer import Normalizer

    args = [a for a in argv[1:] if not a.startswith("--")]
    apply = "--apply" in argv
    if len(args) != 1:
        print(__doc__)
        return 2

    root = Path(args[0]).expanduser()
    if not root.is_dir():
        print("Not a directory: %s" % root)
        return 2

    normalizer = Normalizer(root)
    plan = normalizer.plan()
    print("import:   %s" % root)
    print("plan:     %s" % plan.describe())

    if plan.adapter == "none":
        print("No adapter recognises this tree -- nothing to do.")
        return 1
    if plan.is_noop:
        print("Already normalised. Nothing to do.")
        return 0
    if not apply:
        print()
        print("Dry run. Nothing was written. Re-run with --apply to do it.")
        return 0

    done = normalizer.apply()
    print("applied:  %s" % done.describe())
    print("workspace: %s" % normalizer.workspace.root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
