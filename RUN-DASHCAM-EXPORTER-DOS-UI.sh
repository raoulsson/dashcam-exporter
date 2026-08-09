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

# --- On restart, stop any instance already running --------------------------
# Testing the frame means launching it over and over; a previous run still
# holding the single-instance lock would refuse the new one. Kill it here, in
# the OUTER launch only -- the inner run is the new instance and must not kill
# itself. SIGTERM, so it can tidy up; the stale lock clears on the dead pid.
if [ -z "${DOS_UI_INNER:-}" ]; then
    pkill -f 'dashcam_exporter.application.workflow.pipeline' 2>/dev/null || true
fi

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

# --- Inner run (the new window), or non-macOS/fallback: size and run --------
[ -t 1 ] && printf '\033[8;%d;%dt' "$ROWS" "$COLS"

# In our own spawned window: run the tool, then close the window when it exits
# (q, or ctrl-c -- which the frame's raw mode reads as a byte, so the tool exits
# cleanly and we still get here). exec the close so bash is gone and Terminal
# has nothing but osascript left to terminate, closing without a prompt.
if [ -n "${DOS_UI_INNER:-}" ] && [ "$(uname)" = "Darwin" ] \
        && command -v osascript >/dev/null 2>&1; then
    WINID="$(osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null || true)"
    env SET_UI_STYLE=framed FRAME_ROWS="$ROWS" FRAME_COLS="$COLS" \
        "$HERE/RUN-DASHCAM-EXPORTER.sh" "$@"
    [ -n "$WINID" ] && exec osascript -e \
        "tell application \"Terminal\" to close (every window whose id is $WINID) saving no"
    exit 0
fi

exec env SET_UI_STYLE=framed FRAME_ROWS="$ROWS" FRAME_COLS="$COLS" \
    "$HERE/RUN-DASHCAM-EXPORTER.sh" "$@"
