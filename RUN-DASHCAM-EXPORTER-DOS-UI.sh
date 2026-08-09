#!/usr/bin/env bash
# RUN-DASHCAM-EXPORTER-DOS-UI.sh — the same tool, in the DOS-style framed UI.
#
# A fixed title/status band on top, the numbered menu as a bar on the bottom, and
# the work in the middle: a scrolling log with the live progress bar pinned
# beneath it. Same workflow, config, .env, plugin and keys as
# RUN-DASHCAM-EXPORTER.sh — this only turns the frame on (SET_UI_STYLE=framed).
#
# The frame wants a FIXED size, and a terminal resize hits the whole window (so
# every tab in it). To avoid resizing tabs you are already using, this opens a
# FRESH Terminal window at ROWS x COLS and runs there. The inner run (marked by
# DOS_UI_INNER) skips the spawn and does the actual work.
#
# macOS only for the new-window part; elsewhere (or with DOS_UI_INNER set) it
# resizes the current terminal in place, as before. Uses Terminal.app — if you
# live in iTerm2, say so and I'll add an iTerm path.
#
# If a crash ever leaves a terminal wedged, type `reset` and press Enter.

set -euo pipefail

ROWS=40
COLS=140
HERE="$(cd "$(dirname "$0")" && pwd)"

# --- Spawn a fresh, correctly-sized window and run there --------------------
if [ -z "${DOS_UI_INNER:-}" ] && [ "$(uname)" = "Darwin" ] \
        && command -v osascript >/dev/null 2>&1; then
    inner="cd '$HERE' && DOS_UI_INNER=1 '$HERE/RUN-DASHCAM-EXPORTER-DOS-UI.sh'"
    # A first run may prompt for permission to control Terminal; if it is
    # declined (or anything else fails) fall through to sizing this window.
    if osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
    activate
    set theTab to do script "$inner"
    delay 0.3
    try
        set number of columns of theTab to $COLS
        set number of rows of theTab to $ROWS
    end try
end tell
OSA
    then
        echo "Opened the DOS UI in a new Terminal window (${ROWS}x${COLS})."
        exit 0
    fi
    echo "Could not open a new window (automation permission?); running here."
fi

# --- Inner run (the new window), or non-macOS: size in place and run --------
[ -t 1 ] && printf '\033[8;%d;%dt' "$ROWS" "$COLS"
exec env SET_UI_STYLE=framed FRAME_ROWS="$ROWS" FRAME_COLS="$COLS" \
    "$HERE/RUN-DASHCAM-EXPORTER.sh" "$@"
