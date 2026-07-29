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
# FRESH OUTPUT for the days THIS run renders: before writing, each destination
# day folder is cleared, so what's left afterwards is exactly this run's output
# with no stragglers from a stopped render. ONLY the day folders this run writes
# to are touched — rendering one import never disturbs another import's day
# folders in the same --out. (This reset lives in make_dashcam_videos.py, which
# knows which days it will write; it's skipped for a --drives subset and for the
# read-only --sidecars-only mode. Pass --no-clean-days to keep existing files.)
# The run log is written to <out>/logs/run-<timestamp>.log, and a copy is
# dropped beside each encoded .mp4 as trip_....log.
#
# Get the indices with `./list-trips-data.sh` first.
#
# Edit the OPTS line below to pre-set any flags you regularly use that you
# DON'T want in config.txt (e.g. a non-default --root for a copied SD card,
# a smaller --output-height for sharing, etc.). Anything you put in
# config.txt is loaded automatically and doesn't need to go here.

set -euo pipefail
cd "$(dirname "$0")"

# Prefer the project venv if present (the map widget needs Pillow/staticmap,
# which Homebrew's externally-managed python3 can't install into directly).
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

OPTS=()
# Examples — uncomment + adapt:
# OPTS+=(--root "$HOME/dashcam-data/import")   # a copied card, not the card itself
# OPTS+=(--out  "$HOME/dashcam-data/output_test")   # render somewhere scratch
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

# Resolve the output dir from the effective args so we can clean it and log
# INTO it. --out wins; fall back to the same env default the import script
# uses. Handles both "--out DIR" and "--out=DIR".
OUT="${DASHCAM_OUT_ROOT:-$HOME/dashcam-data/output}"
_prev=""
for a in ${OPTS[@]+"${OPTS[@]}"} "$@"; do
    case "$a" in
        --out=*)  OUT="${a#--out=}" ;;
        *) [ "$_prev" = "--out" ] && OUT="$a" ;;
    esac
    _prev="$a"
done
OUT="${OUT/#\~/$HOME}"   # expand a leading ~ (bash won't, inside a var)

# NOTE: the fresh-output reset lives in make_dashcam_videos.py, NOT here. It
# clears ONLY the day folders THIS run actually renders (e.g. rendering the
# 2026-07-19 import clears its 2026-07-15..18 days and leaves an unrelated
# 2026-05-11 from another import untouched). Only the renderer knows which days
# it will write; clearing from the wrapper would mean blindly wiping every day
# folder in --out, including another import's output. The wrapper only ensures
# --out exists.
echo ">>> Output dir: $OUT"
mkdir -p "$OUT"

# Logs go in their own folder rather than beside the renders: one run log per
# render adds up fast, and mixed in with the trip folders they bury the output
# you are actually looking for.
LOG_DIR="${LOG_DIR:-$OUT/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"
echo ">>> logging to $LOG_FILE"

# -u forces unbuffered stdout so per-clip progress appears as it happens instead
# of being held in Python's buffer until the run completes.
#
# Not piped through tee. A pipe means stdout is not a terminal, so the
# per-clip scan line cannot redraw itself in place — and emitting the carriage
# return anyway would write it into the log too, collapsing the whole run
# into one unreadable line. --log-file puts the logging inside the renderer,
# which can tell the two apart: carriage returns to the terminal, one line per
# event to the file. stderr is folded into the log as well.
"$PY" -u make_dashcam_videos.py --log-file "$LOG_FILE" ${OPTS[@]+"${OPTS[@]}"} "$@"
RC="$?"

# Parse "  ✓ /full/path/to/foo.mp4" lines from the log and drop a copy of
# the log next to each one as foo.log.
while IFS= read -r mp4; do
    [ -f "$mp4" ] || continue
    log_dest="${mp4%.mp4}.log"
    cp "$LOG_FILE" "$log_dest"
    echo "  saved log copy → $log_dest"
done < <(grep -oE '✓ [^ ]+\.mp4' "$LOG_FILE" | sed 's/^✓ //')

exit "$RC"
