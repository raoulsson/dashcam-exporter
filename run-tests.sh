#!/usr/bin/env bash
# Run the test suite. Fixtures only — nothing here reads the SD card, the import
# workspace or the output tree, so it is safe to run at any time, mid-render
# included.
#
# What it covers: the predicates the destructive steps obey. Not the renderer,
# not the site — the four paths that erase things (steps 4, 9, 10 and the
# import-time sweep) and the guards that decide whether they may.

set -uo pipefail
cd "$(dirname "$0")"

PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

RC=0
echo
echo "=== guards (python) ==="
"$PY" -m unittest discover -s tests -q 2>&1 | tail -5 || RC=1

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
