#!/usr/bin/env bash
# list-single-drives-data.sh
# ---------------------------
# Dry-run the script in DRIVE mode (gap-based grouping). Each engine-on
# session is shown as a separate group with its 1-based index — pass that
# index to `make-single-drives.sh` to encode just that drive.
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
# OPTS+=(--gap  120)

python3 make_dashcam_videos.py --dry-run "${OPTS[@]}" "$@"
