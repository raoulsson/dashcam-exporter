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
# The window runs the tool under Terminal's login shell (NOT exec'd -- exec'ing
# it away breaks Terminal's `busy` tracking, and the watcher below then reads the
# tab as idle while the tool is only waiting for a key, and closes it). A
# DETACHED watcher polls `busy` and closes the window once the tab goes idle --
# i.e. the tool has quit (q, or ctrl-c, which the frame's raw mode reads as a
# byte). Only the idle login shell is left then, which does not trip Terminal's
# "processes are running" confirmation, so it closes cleanly.
if [ -z "${DOS_UI_INNER:-}" ] && [ "$(uname)" = "Darwin" ] \
        && command -v osascript >/dev/null 2>&1; then
    inner="cd '$HERE' && DOS_UI_INNER=1 '$HERE/RUN-DASHCAM-EXPORTER-DOS-UI.sh'"
    # Open the window EMPTY, size it, THEN run the tool in it. Two reasons this
    # order matters: number of rows/columns are WINDOW properties (setting them
    # on the tab that `do script` returns errors, and the try swallows it), and
    # the frame reads the terminal size once at startup with no SIGWINCH catch --
    # so it has to be the right size before the tool starts, not resized under
    # it afterwards. The request is clamped to what the screen/font can show
    # (140 cols may land at ~131), and the frame follows that actual size rather
    # than painting a fixed 140 that overflows the window.
    winid="$(osascript 2>/dev/null <<OSA
tell application "Terminal"
    activate
    set theTab to do script ""
    set theWin to window 1
    delay 0.3
    try
        set number of columns of theWin to $COLS
        set number of rows of theWin to $ROWS
    end try
    delay 0.2
    do script "$inner" in theTab
    return id of theWin
end tell
OSA
)"
    if [ -n "$winid" ]; then
        (
            # Wait for the tab to start, then for it to go idle, then close it.
            sleep 1
            while :; do
                b="$(osascript -e "tell application \"Terminal\"
                    try
                        return (busy of tab 1 of (first window whose id is $winid)) as string
                    on error
                        return \"gone\"
                    end try
                end tell" 2>/dev/null)"
                [ "$b" = "true" ] || break
                sleep 0.4
            done
            osascript -e "tell application \"Terminal\"
                try
                    close (first window whose id is $winid) saving no
                end try
            end tell" 2>/dev/null
        ) >/dev/null 2>&1 &
        disown 2>/dev/null || true
        echo "Opened the DOS UI in a new Terminal window (${ROWS}x${COLS}); it closes on quit."
        exit 0
    fi
    echo "Could not open a new window (automation permission?); running here."
fi

# --- Inner run (the new window), or non-macOS/fallback: size and run --------
# The window is already sized (macOS osascript above, or this printf on a
# terminal that honours it). Do NOT pin FRAME_ROWS/FRAME_COLS: let the frame
# read the ACTUAL terminal size, so a request clamped by the screen (140 -> ~131)
# gives a frame that fits the window instead of one that overflows it.
[ -t 1 ] && printf '\033[8;%d;%dt' "$ROWS" "$COLS"
exec env SET_UI_STYLE=framed "$HERE/RUN-DASHCAM-EXPORTER.sh" "$@"
