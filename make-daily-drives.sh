#!/usr/bin/env bash
# make-daily-drives.sh
# --------------------
# Render one or more whole days (DAILY mode — every clip on the same
# calendar date concatenated, with auto parking-skip and inter-clip-gap
# Fast-forwarding slides between engine-off intervals).
#
# Pass the 1-based day indices you want encoded as positional arguments,
# e.g.:
#       ./make-daily-drives.sh                 # encode every day on the card
#       ./make-daily-drives.sh 8                # only day 8
#       ./make-daily-drives.sh 6 8              # days 6 and 8
#
# Get the indices with `./list-daily-drives-data.sh` first.
#
# Edit the OPTS line below to pre-set any flags you regularly use that you
# DON'T want in config.txt (e.g. a non-default --root for a copied SD card,
# a smaller --output-height for sharing, etc.). Anything you put in
# config.txt is loaded automatically and doesn't need to go here.

set -euo pipefail
cd "$(dirname "$0")"

OPTS=()
# Examples — uncomment + adapt:
# OPTS+=(--root "$HOME/dashcam_backup/2026-05-11")
# OPTS+=(--out  "$HOME/Movies/Dashcam")
# OPTS+=(--output-height 720)            # smaller / web-sized file
# OPTS+=(--no-audio)                     # privacy: drop passenger conversation

if [ "$#" -gt 0 ]; then
    OPTS+=(--drives "$@")
fi

python3 make_dashcam_videos.py --daily "${OPTS[@]}"
