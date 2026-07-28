#!/usr/bin/env bash
# import-sd-card.sh
# -----------------
# Copy the dashcam SD card's whole DCIM tree into a dated import folder, VERIFY
# the copy, then delete the source FILES from the card — keeping the DCIM folder
# structure so the dashcam can keep recording into it. Nothing on the card is
# deleted until the copy has been verified byte-count/– file-for-file, and the
#   The verify ALWAYS runs and always gates the delete; --keep is a
#   back-compat no-op now that keeping the card is the default.
#
# The card is NOT erased by default. Import first, render, check the result —
# then come back with --delete once you're happy. Deleting is still gated on the
# copy verifying, so --delete can never wipe an incomplete import.
#
# Usage:
#   ./import-sd-card.sh                          # /Volumes/NO NAME  ->  ~/dashcam-data/import/<today>
#   ./import-sd-card.sh 2026-07-20               # name the day folder explicitly
#   ./import-sd-card.sh --delete                 # copy + verify, THEN erase the card's files
#   ./import-sd-card.sh --checksum               # rigorous byte-for-byte verify (slow)
#   ./import-sd-card.sh --src "/Volumes/OTHER" 2026-07-20
#
# Notes:
#   - Only the FILES on the card are removed; the DCIM/... directories stay.
#   - DCIM/IPSRecord.txt (the camera's drive/park event log) is copied too.
#   - Re-importing the same day merges into the existing folder (rsync).

set -euo pipefail

SRC="/Volumes/NO NAME"
DEST_ROOT="${DASHCAM_IMPORT_ROOT:-$HOME/dashcam-data/import}"
DAY="$(date +%Y-%m-%d)"
DELETE_SOURCE=0          # default: keep the card; erase only with --delete
CHECKSUM=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --delete)   DELETE_SOURCE=1; shift ;;
        --keep)     DELETE_SOURCE=0; shift ;;   # kept for back-compat (now default)
        --checksum) CHECKSUM=1; shift ;;
        --src)      SRC="$2"; shift 2 ;;
        --help|-h)  sed -n '2,25p' "$0"; exit 0 ;;
        -*)         echo "unknown option: $1" >&2; exit 2 ;;
        *)          DAY="$1"; shift ;;   # first bare arg = day-folder name
    esac
done

DEST="$DEST_ROOT/$DAY"

[ -d "$SRC/DCIM" ] || { echo "ERROR: no DCIM folder at '$SRC'. Is the card mounted?" >&2; exit 1; }

src_files=$(find "$SRC/DCIM" -type f | wc -l | tr -d ' ')
src_size=$(du -sh "$SRC/DCIM" 2>/dev/null | cut -f1)
echo "Source: $SRC/DCIM  ($src_files files, ${src_size:-?})"
echo "Dest:   $DEST/DCIM"
[ "$src_files" -gt 0 ] || { echo "Source DCIM has no files — nothing to import."; exit 0; }
[ -d "$DEST/DCIM" ] && echo "NOTE: $DEST/DCIM already exists — merging into it."

mkdir -p "$DEST"
echo "Copying..."
# Optional high-water mark: skip clips at or before a timestamp already
# imported. DDPAI names every file <YYYYMMDDHHMMSS>_*, so the name alone orders
# them and an --after of 20260724185333 means "everything since that clip".
# Without it the card's whole DCIM is copied, which is what a first import wants.
FILTER=()
if [ -n "${AFTER_STAMP:-}" ]; then
    echo ">>> only clips newer than $AFTER_STAMP"
    tmp_list="$(mktemp)"
    # Matched with bash's own regex, not `grep | head`. Under `set -euo pipefail`
    # a filename with no 14-digit stamp — IPSRecord.txt, the gps tars — makes
    # grep exit 1, pipefail propagates it, and set -e kills the script before the
    # "keep anything without a timestamp" branch below can ever run. The import
    # died at exit 1 with no message for exactly that reason. This also drops two
    # subprocesses per file, which over 2700 files is most of the loop's runtime.
    while IFS= read -r rel; do
        base="${rel##*/}"
        if [[ $base =~ ([0-9]{14}) ]]; then
            stamp="${BASH_REMATCH[1]}"
        else
            stamp=""      # no timestamp: always keep it
        fi
        if [ -z "$stamp" ] || [ "$stamp" \> "$AFTER_STAMP" ]; then
            printf '%s\n' "$rel" >> "$tmp_list"
        fi
    done < <( cd "$SRC" && find DCIM -type f )
    echo ">>> $(wc -l < "$tmp_list" | tr -d ' ') of $( ( cd "$SRC" && find DCIM -type f ) | wc -l | tr -d ' ') file(s) selected"
    # --files-from paths are relative to the SOURCE root, so the source has to
    # be $SRC (the list already says DCIM/...); pairing it with $SRC/DCIM would
    # copy into DEST/DCIM/DCIM.
    FILTER=(--files-from="$tmp_list")
    SRC_ARG="$SRC"
    # What THIS run is responsible for. The completeness check below compares
    # against it, not against the card: a delta import is expected to leave most
    # of the card behind, so measuring the copy against the whole DCIM tree
    # declares every successful delta a failure.
    expect_files=$(wc -l < "$tmp_list" | tr -d ' ')
else
    SRC_ARG="$SRC/DCIM"
    expect_files="$src_files"
fi

# macOS ships openrsync ("rsync 2.6.9 compatible"), which does NOT accept
# --info=progress2 — it prints its usage and exits non-zero, with or without
# --files-from. Homebrew's rsync 3.x does. Probe rather than assume, because the
# failure mode is a wall of usage text and an exit code, which reads like a
# broken argument list rather than a missing feature.
PROGRESS=(--progress)
if rsync --help 2>&1 | grep -q -- '--info='; then
    PROGRESS=(--info=progress2)
fi
rsync -a "${PROGRESS[@]}" ${FILTER[@]+"${FILTER[@]}"} "$SRC_ARG" "$DEST/"

# --- verify before we delete anything ---------------------------------------
# ${CHECKSUM:+...} expands whenever CHECKSUM is set to ANYTHING, and it is
# always set — to 0 or 1. So this printed "(checksum)" on every run while the
# flag was only added below when it is 1: a safety message overstating the
# rigour of the check that gates erasing the card.
if [ "$CHECKSUM" -eq 1 ]; then echo "Verifying (checksum)..."; else echo "Verifying..."; fi
verify_opts=(-a --dry-run --itemize-changes)
[ "$CHECKSUM" -eq 1 ] && verify_opts+=(--checksum)

# Run the verify into a file, and check rsync's OWN exit status, separately from
# grep's. `rsync ... | grep -c '^>f' || true` swallowed both: the || true is
# there because grep exits 1 when it matches nothing, but it equally hides an
# rsync that failed outright — which yields pending=0, "nothing left to copy",
# and a green light to erase the card on a verify that never ran.
verify_out="$(mktemp)"
trap 'rm -f "$verify_out"' EXIT
if ! rsync "${verify_opts[@]}" ${FILTER[@]+"${FILTER[@]}"} "$SRC_ARG" "$DEST/" > "$verify_out" 2>&1; then
    echo "ERROR: the verify pass itself failed — NOT deleting source." >&2
    sed 's/^/    /' "$verify_out" | tail -20 >&2
    exit 1
fi
pending=$(grep -c '^>f' "$verify_out" || true)
dest_files=$(find "$DEST/DCIM" -type f | wc -l | tr -d ' ')
if [ "$pending" -ne 0 ]; then
    echo "ERROR: verify found $pending file(s) not yet copied. NOT deleting source." >&2
    exit 1
fi
if [ "$dest_files" -lt "$expect_files" ]; then
    echo "ERROR: dest has $dest_files files but this run should have copied $expect_files. NOT deleting source." >&2
    exit 1
fi
echo "Verified: $dest_files file(s) in dest ($expect_files expected from this run)."

# --- clean the card (files only, keep the folder tree) ----------------------
if [ "$DELETE_SOURCE" -eq 1 ] && [ -n "${AFTER_STAMP:-}" ]; then
    echo "Refusing --delete after a filtered copy: this run only brought over the" >&2
    echo "clips newer than $AFTER_STAMP, so erasing the card would take the earlier" >&2
    echo "ones with it, and their only proof of having been imported is a ledger" >&2
    echo "this script cannot read. Erase it with a full import, or by hand." >&2
    exit 1
fi
if [ "$DELETE_SOURCE" -eq 1 ]; then
    echo "Deleting source FILES (keeping DCIM folder structure)..."
    find "$SRC/DCIM" -type f -delete
    left=$(find "$SRC/DCIM" -type f | wc -l | tr -d ' ')
    echo "Card cleaned — $left files remain, folders kept."
else
    echo "Card left untouched (default). Once you've rendered and checked the"
    echo "result, erase it with:  $0 --delete ${DAY}"
fi

echo "Done. Imported $dest_files file(s) to $DEST/DCIM"
echo "Render with: ./make-trips-rendered.sh --root \"$DEST\" --out \"${DASHCAM_OUT_ROOT:-$HOME/dashcam-data/output}\""
