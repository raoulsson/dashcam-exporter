#!/usr/bin/env bash
# make-trips-rendered.sh
# ----------------------
# Render one or more TRIPS. A trip is the publishing unit: everything from
# leaving an anchor until the car returns to it, or a long engine-off gap /
# the 04:00 day rollover ends it (see group_into_trips in
# make_dashcam_videos.py). Interior parking becomes a 'Fast forwarding…' slide,
# so a "drive out, park, drive back" trip is one continuous .mp4 + .html +
# .gpx + _links.txt + _meta.json sidecars.
#
# Pass the 1-based trip indices you want encoded as positional arguments, e.g.:
#       ./make-trips-rendered.sh                          # encode every trip on the card
#       ./make-trips-rendered.sh 8                         # only trip 8
#       ./make-trips-rendered.sh 6 8                       # trips 6 and 8
#       ./make-trips-rendered.sh --sidecars-only           # refresh sidecars only
#       ./make-trips-rendered.sh 8 --output-height 720     # trip 8, 720p
#
# Integers are forwarded to --drives (aka --trips); everything else (e.g.
# --sidecars-only, --output-height, --trip-return-m …) is passed straight
# through to make_dashcam_videos.py.
#
# Get the indices with `./list-trips-data.sh` first.
#
# Edit the OPTS line below to pre-set any flags you regularly use that you
# DON'T want in config.txt (e.g. a non-default --root for a copied SD card,
# a smaller --output-height for sharing, etc.). Anything you put in
# config.txt is loaded automatically and doesn't need to go here.

set -euo pipefail
cd "$(dirname "$0")"

OPTS=()
# Examples — uncomment + adapt:
# OPTS+=(--root "$HOME/rsc-data/Import_Dashcam/2026-05-11")
# OPTS+=(--out  "$HOME/rsc-data/Dashcam_Videos_working")
# OPTS+=(--output-height 720)            # smaller / web-sized file

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

# Tee stdout+stderr into a timestamped log file so every run leaves a
# paper trail. After the run, copy that log next to each successfully
# encoded .mp4 (as trip_….log alongside trip_….mp4) so the log lives with
# the data it describes.
LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/trips-$(date +%Y%m%d-%H%M%S).log"
echo "logging to $LOG_FILE"

# -u forces unbuffered stdout so per-clip progress shows in the tee output
# instead of being held in Python's buffer until the run completes.
python3 -u make_dashcam_videos.py ${OPTS[@]+"${OPTS[@]}"} "$@" 2>&1 | tee "$LOG_FILE"
RC="${PIPESTATUS[0]}"

# Parse "  ✓ /full/path/to/foo.mp4" lines from the log and drop a copy of
# the log next to each one as foo.log.
while IFS= read -r mp4; do
    [ -f "$mp4" ] || continue
    log_dest="${mp4%.mp4}.log"
    cp "$LOG_FILE" "$log_dest"
    echo "  saved log copy → $log_dest"
done < <(grep -oE '✓ [^ ]+\.mp4' "$LOG_FILE" | sed 's/^✓ //')

exit "$RC"
