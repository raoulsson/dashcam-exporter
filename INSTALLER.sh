#!/usr/bin/env bash
# INSTALLER.sh — set this project up on a machine that has never run it.
#
# Creates .venv, installs requirements.txt into it, and checks the two things
# that are NOT pip packages and that everything else depends on: ffmpeg and a
# Python new enough to matter. It changes nothing outside this directory, and it
# is safe to run again — an existing venv is reused, not rebuilt.
#
# Run it, then ./RUN-DASHCAM-EXPORTER.sh.

set -euo pipefail
cd "$(dirname "$0")"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YEL=""; DIM=""; BLD=""; OFF=""; }

ok()   { echo "  ${GRN}ok${OFF}    $*"; }
warn() { echo "  ${YEL}warn${OFF}  $*"; }
bad()  { echo "  ${RED}fail${OFF}  $*"; }

echo
echo "${BLD}dashcam-exporter installer${OFF}"
echo "${DIM}  $(pwd)${OFF}"
echo

# ---------------------------------------------------------------------------
# 1. ffmpeg. Not optional and not installable from here: every render, every
#    still and every duration comes out of it. Checked first because it is the
#    one thing pip cannot fix, and finding out after a venv build is worse.
# ---------------------------------------------------------------------------
echo "${BLD}ffmpeg${OFF}"
if command -v ffmpeg >/dev/null 2>&1; then
    ok "$(command -v ffmpeg)  $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
    bad "not found on PATH"
    echo
    echo "  Install it, then run this again:"
    echo "    macOS          brew install ffmpeg"
    echo "    Debian/Ubuntu  sudo apt install ffmpeg"
    echo "    Fedora         sudo dnf install ffmpeg"
    echo
    exit 1
fi
command -v ffprobe >/dev/null 2>&1 && ok "$(command -v ffprobe)" \
                                   || warn "ffprobe missing — it ships with ffmpeg; durations will fail"
echo

# ---------------------------------------------------------------------------
# 2. A Python to build the venv from. Prefer the newest sensible one rather than
#    whatever `python3` happens to be: Apple's /usr/bin/python3 is old and
#    externally managed, and building the venv from it is how you end up unable
#    to install opencv at all.
# ---------------------------------------------------------------------------
echo "${BLD}python${OFF}"
BASE_PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
    p="$(command -v "$cand" || true)"
    [ -n "$p" ] || continue
    v="$("$p" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    maj="${v%%.*}"; min="${v##*.}"
    if [ "$maj" -eq 3 ] && [ "$min" -ge 9 ]; then BASE_PY="$p"; break; fi
done
if [ -z "$BASE_PY" ]; then
    bad "no python3 >= 3.9 found"
    echo "    macOS          brew install python@3.12"
    echo "    Debian/Ubuntu  sudo apt install python3 python3-venv"
    exit 1
fi
ok "$BASE_PY  ($("$BASE_PY" -c 'import sys;print(sys.version.split()[0])'))"
echo

# ---------------------------------------------------------------------------
# 3. The venv. Reused if it is already there — rebuilding it on every run would
#    throw away a working opencv to solve a problem nobody has.
# ---------------------------------------------------------------------------
echo "${BLD}virtualenv${OFF}"
if [ -x ".venv/bin/python" ]; then
    ok ".venv exists, reusing it  ${DIM}(delete it to start clean)${OFF}"
else
    echo "  creating .venv ..."
    "$BASE_PY" -m venv .venv || {
        bad "could not create .venv"
        echo "    Debian/Ubuntu needs the venv module: sudo apt install python3-venv"
        exit 1
    }
    ok ".venv created"
fi
VPY="./.venv/bin/python"
echo

# ---------------------------------------------------------------------------
# 4. Requirements. opencv is a ~90 MB wheel, so this is the slow part; say so
#    rather than looking hung.
# ---------------------------------------------------------------------------
echo "${BLD}packages${OFF}  ${DIM}(opencv is a large wheel — this can take a few minutes)${OFF}"
"$VPY" -m pip install --quiet --upgrade pip
if "$VPY" -m pip install --quiet -r requirements.txt; then
    ok "requirements.txt installed"
else
    bad "pip install failed — the output above says why"
    exit 1
fi
echo

# ---------------------------------------------------------------------------
# 5. Prove it, by importing exactly what the code imports. A wheel that
#    installed is not the same claim as a module that loads: opencv in
#    particular can install cleanly and then fail on a missing system library.
# ---------------------------------------------------------------------------
echo "${BLD}verify${OFF}"
if "$VPY" - <<'PY'
import sys
mods = ["cv2", "numpy", "staticmap", "PIL"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append("%s (%s)" % (m, e.__class__.__name__))
if bad:
    print("  missing: " + ", ".join(bad)); sys.exit(1)
import cv2, numpy
print("  cv2 %s, numpy %s" % (cv2.__version__, numpy.__version__))
PY
then
    ok "ego-motion trip detection available"
else
    bad "imports failed — the renderer would fall back to GPS-radius grouping,"
    echo "        which finds different trips. Fix this before rendering."
    exit 1
fi
echo

# ---------------------------------------------------------------------------
# 6. Config. Only reported, never written: config.txt is tracked, so guessing
#    someone's paths into it produces a diff they did not ask for. .env is for
#    the private half and is created empty if absent so there is somewhere
#    obvious to put it.
# ---------------------------------------------------------------------------
echo "${BLD}config${OFF}"
if [ -f config.txt ]; then
    ok "config.txt present"
    "$VPY" - <<'PY'
from pathlib import Path
cfg = {}
for raw in Path("config.txt").read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].strip()
    if "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
imp = cfg.get("import_dir") or cfg.get("root") or "~/dashcam-data/import  (default)"
print("        workspace : %s" % imp)
print("        card      : %s" % cfg.get("card", "/Volumes/NO NAME  (default)"))
PY
else
    warn "no config.txt — the defaults will be used"
fi
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        ok ".env created from .env.example  ${DIM}(gitignored; fill it in if you publish)${OFF}"
    fi
else
    ok ".env present"
fi
echo

echo "${GRN}${BLD}Ready.${OFF}  Start it with:"
echo "    ./RUN-DASHCAM-EXPORTER.sh"
echo
