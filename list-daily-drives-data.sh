#!/usr/bin/env bash
# list-daily-drives-data.sh
# --------------------------
# Dry-run the script in DAILY mode. Every clip from the same calendar date
# is shown as one group with its 1-based index — pass that index to
# `make-daily-drives-rendered.sh` to encode just that day.
#
# Edit the OPTS line below to pre-set any flags you regularly use that you
# DON'T want in config.txt (e.g. a non-default --root for a copied SD card,
# a smaller --output-height, etc.). Anything you put in config.txt is loaded
# automatically and doesn't need to go here.

set -euo pipefail
cd "$(dirname "$0")"

OPTS=()
# Examples — uncomment + adapt:
# OPTS+=(--root "$HOME/dashcam_backup/2026-05-11")
# OPTS+=(--out  "$HOME/Movies/Dashcam")

python3 make_dashcam_videos.py --daily --dry-run ${OPTS[@]+"${OPTS[@]}"} "$@"
