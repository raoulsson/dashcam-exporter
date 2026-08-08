#!/usr/bin/env bash
# RUN-DASHCAM-EXPORTER-DOS-UI.sh — the same tool, in the DOS-style framed UI.
#
# A fixed title/status band on top, the numbered menu as a bar on the bottom, and
# the work in the middle: a scrolling log with the live progress bar pinned
# beneath it. Exactly the same workflow, config, .env, plugin and keys as
# RUN-DASHCAM-EXPORTER.sh — this only turns the frame on (SET_UI_STYLE=framed)
# and pins it to a FIXED SIZE, then hands over to the normal launcher so the
# interpreter selection lives in one place and cannot drift between the two.
#
# Fixed size: the window is resized to ROWS x COLS (honored by Terminal.app,
# iTerm2 and xterm) and FRAME_ROWS/FRAME_COLS pin the frame to the same geometry,
# so a run looks the same every time regardless of the window it started in.
# Change the two numbers below to taste.
#
# Double-click: rename a copy to end in `.command` (chmod +x) and the Finder
# opens it in a new Terminal window. Or open a fresh window first, then run this.
#
# If a crash ever leaves the terminal wedged, type `reset` and press Enter.

set -euo pipefail

ROWS=60
COLS=140

# Ask the terminal to size itself to the fixed geometry. Only when stdout is a
# real terminal, so a piped run does not get the escape as garbage.
[ -t 1 ] && printf '\033[8;%d;%dt' "$ROWS" "$COLS"

exec env SET_UI_STYLE=framed FRAME_ROWS="$ROWS" FRAME_COLS="$COLS" \
    "$(dirname "$0")/RUN-DASHCAM-EXPORTER.sh" "$@"
