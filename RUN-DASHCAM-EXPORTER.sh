#!/usr/bin/env bash
# RUN-DASHCAM-EXPORTER.sh — start the interactive pipeline.
#
# Double-click it, or run ./RUN-DASHCAM-EXPORTER.sh. There is nothing to
# configure here and no arguments to pass: pipeline.py has no command line at
# all, and everything it needs is in config.txt (and .env for the private half).
#
# All this does is find a Python that has the dependencies and hand over to it.
# That sounds trivial until it is not: the project venv, a pyenv shim, Homebrew
# python3 and Apple's /usr/bin/python3 can all be on PATH at once, and only one
# of them has opencv. Picking the wrong one does not fail loudly — the renderer
# silently falls back to GPS-radius trip detection and groups the drives
# differently, which you discover hours later in a finished render. So this
# checks before it starts, and refuses rather than guesses.

set -euo pipefail
cd "$(dirname "$0")"

RED=$'\033[31m'; GRN=$'\033[32m'; DIM=$'\033[2m'; BLD=$'\033[1m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; DIM=""; BLD=""; OFF=""; }

have_deps() {   # $1 = interpreter
    "$1" -c "import cv2, numpy, staticmap, PIL" >/dev/null 2>&1
}

# In order of preference. The project venv first: it is the one INSTALLER.sh
# builds and the one make-trips-rendered.sh uses, so agreeing with it keeps the
# CLI and the renderer on the same interpreter.
CANDIDATES=(
    "./.venv/bin/python"
    "${VIRTUAL_ENV:-/nonexistent}/bin/python"
    "$(command -v python3 || true)"
    "$(command -v python || true)"
)

PY=""
for c in "${CANDIDATES[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if have_deps "$c"; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
    echo
    echo "${RED}Cannot start: no Python here has the dependencies.${OFF}"
    echo
    echo "Checked, in order:"
    for c in "${CANDIDATES[@]}"; do
        [ -n "$c" ] || continue
        if [ -x "$c" ]; then
            printf '  %-34s %s\n' "$c" "${DIM}present, missing packages${OFF}"
        else
            printf '  %-34s %s\n' "$c" "${DIM}not found${OFF}"
        fi
    done
    echo
    echo "The renderer needs opencv and numpy to find trip boundaries from the"
    echo "video. Without them it falls back to a GPS radius, which groups the"
    echo "same card into different trips — so this refuses to start rather than"
    echo "quietly produce a different answer."
    echo
    echo "${BLD}Fix it with:${OFF}  ./INSTALLER.sh"
    echo
    exit 1
fi

echo "${DIM}python: $PY${OFF}"
exec "$PY" pipeline.py "$@"
