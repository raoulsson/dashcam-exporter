#!/usr/bin/env bash
# RUN-DASHCAM-EXPORTER-DOS-UI.sh — the same tool, in the DOS-style framed UI.
#
# A fixed title/status band on top, the numbered menu as a bar on the bottom, and
# the work in the middle: a scrolling log with the live progress bar pinned
# beneath it. Exactly the same workflow, config, .env, plugin and keys as
# RUN-DASHCAM-EXPORTER.sh — the ONLY difference is that this turns the frame on,
# by setting SET_UI_STYLE=framed before handing over to the normal launcher (so
# the interpreter selection lives in one place and cannot drift between the two).
#
# The frame engages only on a real terminal; piped or redirected, the tool falls
# back to the scrolling UI on its own. Same effect as `ui_style = framed` in
# config.txt, but per-run and leaving the config alone.
#
# If a crash ever leaves the terminal wedged, type `reset` and press Enter.

set -euo pipefail
exec env SET_UI_STYLE=framed "$(dirname "$0")/RUN-DASHCAM-EXPORTER.sh" "$@"
