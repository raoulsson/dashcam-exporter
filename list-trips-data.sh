#!/usr/bin/env bash
# list-trips-data.sh
# ------------------
# Dry-run the script: list the TRIPS found on the card without encoding
# anything. Each trip shows its 1-based index, the 04:00-rollover day it
# belongs to, its start -> end, clip count and duration. Pass an index to
# `make-trips-rendered.sh` to encode just that trip.
#
# Tune trip detection here or in config.txt if the grouping isn't what you
# want, then re-run:
#   --trip-return-m M         back within M metres of the anchor ends a trip
#   --trip-day-rollover H     day/label boundary hour (default 4 = 04:00)
#
# Edit the OPTS line below to pre-set any flags you regularly use that you
# DON'T want in config.txt (e.g. a non-default --root for a copied SD card).
# Anything you put in config.txt is loaded automatically and doesn't need to
# go here.

set -euo pipefail
cd "$(dirname "$0")"

# Prefer the project venv if present (keeps parity with make-trips-rendered.sh).
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

OPTS=()
# Examples — uncomment + adapt:
# OPTS+=(--root "$HOME/dashcam-data/import")   # a copied card, not the card itself
# OPTS+=(--out  "$HOME/dashcam-data/output_test")   # render somewhere scratch
# OPTS+=(--trip-return-m 120)

PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m dashcam_exporter.renderer --dry-run ${OPTS[@]+"${OPTS[@]}"} "$@"
