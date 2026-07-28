# DDPai SD Card — macOS Mount Fix

256 GB SanDisk Extreme Pro, formatted in-camera by a DDPai dashcam.
Windows mounts it fine. macOS says "The disk you inserted was not
readable by this computer."

## Diagnosis

The card is healthy. The filesystem is healthy. One byte is wrong.

The DDPai runs Linux and formats the card with `mkfs.fat` (visible in
the VBR's OEM field). It writes a valid FAT32 volume at LBA 65536, then
stamps the MBR partition type byte as `0x07` (HPFS/NTFS/exFAT) instead
of `0x0C` (FAT32 LBA).

- **Windows** ignores the type byte and probes the volume directly.
  Finds FAT32. Mounts. Never notices anything is wrong.
- **macOS** trusts the type byte. Sees `0x07`, hands the partition to
  the exFAT prober, which finds no exFAT boot region and gives up.
  Result: "unreadable disk". The FAT32 driver is never consulted.

Confirmed by force-mounting past the dispatch logic:

    sudo mount -t msdos /dev/disk10s1 /Volumes/dashcam

...which mounted instantly and showed DCIM. Also `fsck_msdos -n` walks
the whole filesystem clean (800 files, no errors), so nothing else is
wrong with it.

Note: 64 KB clusters (BPB offset 0x0D = 0x80 = 128 sectors/cluster) are
above the 32 KB that FAT32 nominally allows. macOS handles them fine —
this was a red herring during diagnosis.

## The fix

Patch MBR offset `0x1C2` (450 decimal): `07` -> `0c`. Only sector 0 is
rewritten. Filesystem and data are untouched.

## Files

- `mbr-original-ddpai.bin` — MBR as written by the DDPai (type `0x07`)
- `mbr-fat32-patched.bin`  — same MBR, type byte `0x0C`

Both are exactly 512 bytes and differ in exactly one byte.

## Applying the patch

Find the card first. **The identifier changes between insertions** —
never assume `disk10`.

    diskutil list

Look for the 256 GB `FDisk_partition_scheme`. Then, substituting the
real number for N:

    sudo diskutil unmountDisk /dev/diskN
    sudo dd if=~/dev/dashcam-exporter/docs/sd-card-formatting/mbr-fat32-patched.bin of=/dev/rdiskN bs=512 count=1

Expect `1+0 records in / 1+0 records out`. `0+0 records out` means the
write was rejected and nothing changed.

Eject, reinsert. `diskutil list` should show `Windows_FAT_32 NO NAME`
and Finder should mount it.

## Reverting

    sudo diskutil unmountDisk /dev/diskN
    sudo dd if=~/dev/dashcam-exporter/docs/sd-card-formatting/mbr-original-ddpai.bin of=/dev/rdiskN bs=512 count=1

## Caveats

- `rdiskN` (raw) is correct here, not `diskN` — raw writes go in whole
  sectors, which is what we want.
- Writing an MBR to the wrong device will make that device unmountable.
  Check `diskutil list` every single time.
- The DDPai will likely re-stamp `0x07` whenever it formats in-camera.
  Just re-apply.
- This MBR is specific to this card's geometry (partition at LBA 65536,
  499941376 sectors). Do not write it to a different card.

## Verifying a card matches

    sudo dd if=/dev/rdiskN bs=512 count=1 2>/dev/null | xxd | grep '^000001c0'

Should read `1104 07fe ffff 0000 0100 0080 cc1d 0000` before patching,
`1104 0cfe ...` after. If the rest of the line differs, the geometry is
different and these files do not apply.
