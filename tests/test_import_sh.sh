#!/usr/bin/env bash
# What import-sd-card.sh copies, and when it refuses to erase the card.
#
# The card is the only thing this project deletes that has no second copy, and
# the delete is gated by a verify pass. These exercise that gate with a stubbed
# rsync, so a failure mode can be produced on demand rather than waited for.
#
# Fixtures only: a fake card and a fake destination under a temp dir. Nothing
# here touches /Volumes or ~/dashcam-data.

set -uo pipefail
cd "$(dirname "$0")/.."
SCRIPT="$PWD/import-sd-card.sh"

PASS=0; FAIL=0
ok(){   PASS=$((PASS+1)); printf "  ok    %s\n" "$1"; }
bad(){  FAIL=$((FAIL+1)); printf "  FAIL  %s\n         %s\n" "$1" "${2:-}"; }

setup(){                       # $1 = extra clips on the card
  TMP="$(mktemp -d)"
  mkdir -p "$TMP/card/DCIM/200video/front" "$TMP/card/DCIM/200video/rear" "$TMP/dest" "$TMP/bin"
  for s in 20260725120000 20260725130000; do
    echo clip > "$TMP/card/DCIM/200video/front/${s}_0060.mp4"
    echo clip > "$TMP/card/DCIM/200video/rear/${s}_0060_A.mp4"
  done
  echo notimestamp > "$TMP/card/DCIM/IPSRecord.txt"
  for s in ${1:-}; do echo clip > "$TMP/card/DCIM/200video/front/${s}_0060.mp4"; done
}
teardown(){ rm -rf "$TMP"; }

stub_rsync(){                  # $1 = "fail-verify" | "fail-copy" | "pending"
  cat > "$TMP/bin/rsync" <<EOF
#!/bin/bash
mode="$1"
dry=0; for a in "\$@"; do [ "\$a" = "--dry-run" ] && dry=1; done
if [ "\$mode" = "fail-copy" ] && [ "\$dry" = "0" ]; then
  echo "rsync: simulated copy failure" >&2; exit 23
fi
if [ "\$mode" = "fail-verify" ] && [ "\$dry" = "1" ]; then
  echo "rsync: simulated verify failure" >&2; exit 23
fi
if [ "\$mode" = "pending" ] && [ "\$dry" = "1" ]; then
  echo ">f+++++++++ DCIM/200video/front/missing.mp4"; exit 0
fi
exec /usr/bin/rsync "\$@"
EOF
  chmod +x "$TMP/bin/rsync"
}

card_files(){ find "$TMP/card/DCIM" -type f | wc -l | tr -d ' '; }

# ---------------------------------------------------------------------------
echo "import-sd-card.sh"

# 1. a clean run copies and, without --delete, leaves the card alone
setup
out="$(DASHCAM_IMPORT_ROOT="$TMP/dest" "$SCRIPT" --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "5" ] && echo "$out" | grep -q "Verified:"; then
  ok "clean run: copies, verifies, keeps the card"
else bad "clean run" "$(echo "$out" | tail -2)"; fi
teardown

# 2. --delete erases the card only after the verify passes
setup
out="$(DASHCAM_IMPORT_ROOT="$TMP/dest" "$SCRIPT" --delete --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "0" ] && [ -d "$TMP/card/DCIM/200video/front" ]; then
  ok "--delete: erases the files, keeps the folder tree"
else bad "--delete" "files=$(card_files)"; fi
teardown

# 3. a verify that CANNOT RUN must not erase the card
setup; stub_rsync fail-verify
out="$(PATH="$TMP/bin:$PATH" DASHCAM_IMPORT_ROOT="$TMP/dest" "$SCRIPT" --delete --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "5" ] && echo "$out" | grep -q "verify pass itself failed"; then
  ok "verify fails -> refuses, card intact"
else bad "verify failure must fail closed" "cardfiles=$(card_files)"; fi
teardown

# 4. a verify that reports files still pending must not erase the card
setup; stub_rsync pending
out="$(PATH="$TMP/bin:$PATH" DASHCAM_IMPORT_ROOT="$TMP/dest" "$SCRIPT" --delete --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "5" ] && echo "$out" | grep -q "not yet copied"; then
  ok "pending files -> refuses, card intact"
else bad "pending must fail closed" "cardfiles=$(card_files)"; fi
teardown

# 5. a failed COPY must not reach the delete gate
setup; stub_rsync fail-copy
out="$(PATH="$TMP/bin:$PATH" DASHCAM_IMPORT_ROOT="$TMP/dest" "$SCRIPT" --delete --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "5" ]; then
  ok "copy fails -> card intact"
else bad "copy failure must fail closed" "cardfiles=$(card_files)"; fi
teardown

# 6. a delta copy takes only the new clips, and keeps the untimestamped ones
setup "20260726090000 20260726091000"
out="$(AFTER_STAMP=20260725130000 DASHCAM_IMPORT_ROOT="$TMP/dest" \
       "$SCRIPT" --src "$TMP/card" day 2>&1)"
copied="$(find "$TMP/dest/day/DCIM" -type f 2>/dev/null | wc -l | tr -d ' ')"
if [ "$copied" = "3" ]; then          # 2 new clips + IPSRecord.txt
  ok "delta copy: only clips after the mark, plus untimestamped files"
else bad "delta copy" "copied=$copied, wanted 3"; fi
teardown

# 7. --delete after a DELTA copy is refused: the skipped clips were verified by
#    an earlier run this script cannot see
setup "20260726090000"
out="$(AFTER_STAMP=20260725130000 DASHCAM_IMPORT_ROOT="$TMP/dest" \
       "$SCRIPT" --delete --src "$TMP/card" day 2>&1)"
if [ "$(card_files)" = "6" ] && echo "$out" | grep -q "Refusing --delete"; then
  ok "--delete after a delta -> refused, card intact"
else bad "delta + --delete must refuse" "cardfiles=$(card_files)"; fi
teardown

echo
printf "  %d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
