#!/usr/bin/env bash
# make-daily-drives-rendered.sh
# -----------------------------
# Render one or more whole days (DAILY mode — every clip on the same
# calendar date concatenated, with auto parking-skip and inter-clip-gap
# Fast-forwarding slides between engine-off intervals).
#
# Pass the 1-based day indices you want encoded as positional arguments,
# and / or extra flags. Anything that's a plain integer is forwarded to
# --drives; everything else is passed through to make_dashcam_videos.py.
# e.g.:
#       ./make-daily-drives-rendered.sh                          # encode every day on the card
#       ./make-daily-drives-rendered.sh 8                         # only day 8
#       ./make-daily-drives-rendered.sh 6 8                       # days 6 and 8
#       ./make-daily-drives-rendered.sh --sidecars-only           # refresh .html/.gpx only
#       ./make-daily-drives-rendered.sh 8 --output-height 720     # day 8, 720p
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

# Leading integers feed --drives; everything from the first non-integer
# onward is forwarded to the python script as-is. Stops slurping at the
# first non-integer so e.g. "8 --output-height 720" doesn't mis-treat 720.
INDICES=()
while [ "$#" -gt 0 ] && [[ $1 =~ ^[0-9]+$ ]]; do
    INDICES+=("$1")
    shift
done
if [ "${#INDICES[@]}" -gt 0 ]; then
    OPTS+=(--drives "${INDICES[@]}")
fi

python3 make_dashcam_videos.py --daily ${OPTS[@]+"${OPTS[@]}"} "$@"
