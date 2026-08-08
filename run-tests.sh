#!/usr/bin/env bash
# Run the test suite. Fixtures only — nothing here reads the SD card, the import
# workspace or the output tree, so it is safe to run at any time, mid-render
# included.
#
# What it covers: the graph the menu is built from, and the predicates the
# destructive items obey. Not the renderer, not the site — the three paths that
# erase things (4) Exclude Trip, 9) Clean Workspace, 10) Delete SIM Data) and the
# guards that decide whether they may. It used to be four: importing swept the
# previous round from inside itself, and that arc is gone.

set -uo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

RC=0
echo
echo "=== guards (python) ==="
# Captured rather than piped straight into tail: -q prints one line per pass
# and a whole traceback per failure, and tail -5 threw the tracebacks away —
# so a red run printed "FAILURES — see above" with nothing above it, and the
# only way to find out what broke was to run unittest again by hand. Quiet
# while green, everything while red.
TESTS="$("$PY" -m unittest discover -s tests -q 2>&1)" || RC=1
if [ "$RC" -eq 0 ]; then
    printf '%s\n' "$TESTS" | tail -5
else
    printf '%s\n' "$TESTS"
fi

echo
echo "=== undefined names and redefinitions (pyflakes) ==="
# Two classes, and only because they cost nothing to catch and everything to
# miss. Each is a crash the suite can be entirely green over.
#
# An undefined name is a crash the moment the line runs. It was -- 408 tests
# passed while main() referred to a variable nobody assigned, because not one
# of them starts the program.
#
# A redefinition is worse, because it does not crash where it is written. The
# second def silently replaces the first and every caller of the first gets the
# new one: _counted(where) counted git commits, a _counted(n) added six hundred
# lines below formatted a clip count, and the version number crashed at launch
# with "%d format: a real number is required, not PosixPath". That was the
# fifth collision in this file -- _offers, _slug, _row and _is_dir came before
# it, and _is_dir silently inverted a symlink check rather than crashing.
#
# The rest of pyflakes is style, and some of it is deliberate (uploader.py
# re-exports on purpose), so it is not a reason to fail a build.
if [ -x ".venv/bin/pyflakes" ]; then
    # src/*.py matched nothing once the code moved into src/dashcam_exporter/**;
    # find recurses the package so the check sees the whole tree again.
    UNDEF="$(.venv/bin/pyflakes $(find src tests -name '*.py') 2>&1 \
             | grep -E "undefined name|redefinition of unused" || true)"
    if [ -n "$UNDEF" ]; then
        echo "$UNDEF"
        RC=1
    else
        echo "  none"
    fi
else
    echo "  pyflakes not installed - skipped"
fi

echo
echo "=== import-sd-card.sh (shell) ==="
bash tests/test_import_sh.sh || RC=1

echo
if [ "$RC" -eq 0 ]; then
    echo "all green"
else
    echo "FAILURES — see above"
fi
exit "$RC"
