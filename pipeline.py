#!/usr/bin/env python3
"""pipeline.py — the whole dashcam publishing pipeline, in one interactive CLI.

Card -> import -> preview -> render -> manifest -> S3 -> website -> (optionally)
erase the import source. Each of those already has a script; the point of this
file is that nobody should have to remember which script, in which repo, with
which flag. Run it, look at the status screen, pick the steps.

The preview and drop steps in the middle are the cheap decision point: sidecars
and one still per trip cost minutes, while encoding costs hours and uploading
costs days on a 250 KB/s line. Deciding what to keep afterwards means paying for
footage that was never wanted.

    python3 pipeline.py

Standard library only, Python 3.9+ (the system /usr/bin/python3 on this Mac).
It never re-implements the underlying tools — it shells out to exactly the same
entry points the READMEs document, streams their output, and turns what it can
parse into a progress bar. Where real progress cannot be derived it shows an
elapsed-time spinner rather than inventing a percentage.

Two repos are involved and this file lives in the first:
    dashcam-exporter/    import + render          (this repo)
    goodnight-drives/    manifest + S3 + deploy   (sibling; --site-repo to override)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults — kept identical to the scripts we drive, so this CLI can never
# disagree with what those scripts would do on their own.
# ---------------------------------------------------------------------------

DEFAULT_CARD = "/Volumes/NO NAME"                 # make_dashcam_videos.DEFAULT_ROOT
DEFAULT_OUT = "~/dashcam-data/output"             # make_dashcam_videos.DEFAULT_OUT
DEFAULT_IMPORT_ROOT = "~/dashcam-data/import_sink"  # import-sd-card.sh DEST_ROOT
LIVE_TRIPS_URL = "https://example.com/your-site/trips.json"

EXPORTER_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def human_bytes(n):
    if n is None:
        return "?"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= step
    return "?"


def human_secs(s):
    if s is None:
        return "--:--"
    s = int(s)
    if s >= 3600:
        return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)
    return "%d:%02d" % (s // 60, s % 60)


def human_age(seconds):
    """Coarse age for the status screen. human_secs would render a week-old
    manifest as '168:00:00', which reads as a duration, not an age."""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    return "%d day(s)" % (seconds // 86400)


def term_width(default=100):
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


class C:
    """ANSI escapes. Disabled wholesale with --no-color or a non-tty stdout."""
    enabled = sys.stdout.isatty()

    @classmethod
    def _w(cls, code, s):
        return s if not cls.enabled else "\x1b[%sm%s\x1b[0m" % (code, s)

    @classmethod
    def bold(cls, s):
        return cls._w("1", s)

    @classmethod
    def dim(cls, s):
        return cls._w("2", s)

    @classmethod
    def red(cls, s):
        return cls._w("31", s)

    @classmethod
    def green(cls, s):
        return cls._w("32", s)

    @classmethod
    def yellow(cls, s):
        return cls._w("33", s)

    @classmethod
    def cyan(cls, s):
        return cls._w("36", s)


def rule(title=""):
    w = term_width()
    if not title:
        return "-" * w
    head = "-- %s " % title
    return C.bold(head) + "-" * max(0, w - len(head))


# ---------------------------------------------------------------------------
# Config — same parser semantics as make_dashcam_videos.load_config_file, so a
# setting means here exactly what it means there.
# ---------------------------------------------------------------------------

def load_config(path):
    """key = value, '#' starts a comment to end of line, blank lines ignored."""
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class Ctx:
    """Everything the steps need: resolved paths, config, and session state."""

    def __init__(self, args):
        self.exporter = EXPORTER_DIR
        self.config_path = Path(args.config).expanduser() if args.config else self.exporter / "config.txt"
        self.cfg = load_config(self.config_path)

        # The site repo is the exporter's sibling by convention; an env var or a
        # flag wins, because "sibling" is a convention and not a guarantee.
        site = args.site_repo or os.environ.get("GOODNIGHT_DRIVES_DIR")
        self.site = Path(site).expanduser().resolve() if site else (self.exporter.parent / "goodnight-drives")

        # `root` in config.txt is what make_dashcam_videos reads from. It points
        # at the import sink now, not the card, so renders survive an ejected card.
        self.render_root = Path(self.cfg.get("root", DEFAULT_CARD)).expanduser()
        self.out_dir = Path(self.cfg.get("out", DEFAULT_OUT)).expanduser()
        self.import_root = Path(os.environ.get("DASHCAM_IMPORT_ROOT", DEFAULT_IMPORT_ROOT)).expanduser()
        self.card = Path(args.card or DEFAULT_CARD)

        try:
            self.output_height = int(self.cfg.get("output_height", "1080"))
        except ValueError:
            self.output_height = 1080

        self.offline = args.offline
        # A non-default --config must reach the renderer too, or this CLI would
        # compute its paths from one config while the wrappers read another.
        self.config_args = ["--config", str(self.config_path)] if args.config else []

        # Session state carried between steps.
        self.selected_import = None     # the folder passed as --root to the renderer
        self.last_scan = None           # ScanResult from the most recent list-trips
        # (root, payload) from the most recent --print-groups. The scan behind it
        # is expensive, and both the preview sheet and the drop step need the same
        # answer, so it is cached per import folder — and invalidated the moment
        # anything changes what is on disk.
        self.last_groups = None
        self.results = []               # StepResult log for the final summary


# ---------------------------------------------------------------------------
# Live output area: a progress bar (or spinner) plus the raw last line, redrawn
# in place. Nothing the child prints is ever hidden — the last line is always
# on screen, and the full stream is buffered so a failure can dump its tail.
# ---------------------------------------------------------------------------

SPIN = "|/-\\"


class Live:
    def __init__(self, enabled):
        self.enabled = enabled
        self.height = 0
        if self.enabled:
            sys.stdout.write("\x1b[?25l")   # hide cursor
            sys.stdout.flush()

    def _erase(self):
        if self.height:
            sys.stdout.write("\x1b[%dA" % self.height)
            sys.stdout.write("\x1b[J")
            self.height = 0

    def draw(self, lines):
        if not self.enabled:
            return
        self._erase()
        w = term_width() - 1
        for ln in lines:
            # Truncating can cut a colour sequence's trailing reset off, which
            # would bleed the colour into the rest of the terminal. Re-append it.
            sys.stdout.write(ln[:w] + "\x1b[0m\n")
        self.height = len(lines)
        sys.stdout.flush()

    def emit(self, text):
        """Print a line that stays on screen, above the live area."""
        if not self.enabled:
            print(text)
            return
        self._erase()
        sys.stdout.write(text[:term_width() - 1] + "\n")
        sys.stdout.flush()

    def close(self):
        if self.enabled:
            self._erase()
            sys.stdout.write("\x1b[?25h")   # show cursor
            sys.stdout.flush()
            self.enabled = False


def show_cursor():
    """Unconditional cursor restore, for the panic paths."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subprocess streaming
# ---------------------------------------------------------------------------

class Aborted(Exception):
    """Ctrl-C during a child process. Carries no message; the runner prints."""


def _reader(stream, q):
    """Split the child's output on BOTH \\n and \\r.

    rsync --info=progress2 and aws s3 sync draw their progress by rewriting one
    line with a carriage return; reading by '\\n' alone would show nothing until
    the transfer finished. Reading raw bytes and splitting on either terminator
    gives us every intermediate update.
    """
    buf = b""
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(b"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                q.put(p.decode("utf-8", "replace"))
    except Exception:
        pass
    finally:
        if buf:
            q.put(buf.decode("utf-8", "replace"))
        q.put(None)


def run_stream(cmd, cwd, label, parser=None, keep=None, passthrough=False,
               env_extra=None, tail_lines=40, stdout_file=None):
    """Run a command, stream its output, return (rc, all_lines).

    parser(line) -> (fraction, note) or None. fraction is 0..1 for a real
    progress bar; return None from the parser (or pass none at all) and the
    display falls back to an elapsed-time spinner. We never synthesise a
    percentage from a guess.

    keep(line) -> bool marks lines worth leaving permanently on screen.
    passthrough=True prints everything verbatim and draws no bar — used for
    the trip listing, where the table IS the output.

    stdout_file redirects the child's STDOUT to that path and streams its
    STDERR instead. That is for --print-groups, whose stdout is a JSON document
    the caller parses: merging it into the progress stream (what every other
    step wants) would corrupt the very thing we ran the command for.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)

    out_fh = open(stdout_file, "wb") if stdout_file else None
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=(out_fh if out_fh else subprocess.PIPE),
            stderr=(subprocess.PIPE if out_fh else subprocess.STDOUT),
            env=env,
            # Own process group: Ctrl-C reaches us first and we decide how the
            # child dies, instead of the terminal SIGINT-ing both and racing us
            # to it. Side effect worth knowing: a new session has no controlling
            # terminal, so anything that insists on prompting at /dev/tty (an ssh
            # host-key confirmation on a first-ever deploy, a passphrase-protected
            # key) fails loudly instead of hanging. Loud is the right failure mode
            # here; accept the host key once by hand and the deploy step works
            # from then on.
            start_new_session=True,
        )
    except BaseException:
        # A command that cannot even start (a missing interpreter, say) must not
        # leave the redirect file handle open behind it.
        if out_fh:
            out_fh.close()
        raise
    q = queue.Queue()
    t = threading.Thread(target=_reader,
                         args=(proc.stderr if out_fh else proc.stdout, q), daemon=True)
    t.start()

    live = Live(enabled=C.enabled and not passthrough)
    started = time.time()
    lines = []
    last_raw = ""
    frac = None
    note = ""
    spin = 0
    done = False

    def render():
        """One line, redrawn in place: progress, the n/m counters, then as much
        of the child's current output as still fits. Two lines meant the counter
        and the log it belongs to were never quite in the same glance; this way
        the whole state of the step is a single row that just keeps moving.
        The full stream is still buffered, so a failure dumps its real tail.
        """
        elapsed = time.time() - started
        if frac is not None:
            # narrower bar than before — the space now goes to the log tail,
            # which is the part that tells you it is still alive
            width = max(8, min(24, term_width() - 60))
            filled = int(round(width * min(frac, 1.0)))
            bar = "#" * filled + "." * (width - filled)
            eta = (elapsed * (1 - frac) / frac) if frac > 0.02 else None
            head = "%s [%s] %3d%% %s/%s" % (
                label, bar, int(frac * 100), human_secs(elapsed), human_secs(eta))
        else:
            head = "%s %s %s" % (label, SPIN[spin % len(SPIN)], human_secs(elapsed))
        if note:
            head += "  " + note

        # give whatever is left of the terminal to the child's latest line
        room = term_width() - len(head) - 4
        tail = ""
        if last_raw and room > 12:
            t = last_raw.strip()
            if len(t) > room:
                # Keep the START. Both encoders and aws put what identifies the
                # line at the front ("[ 4/6] 2026… encoding", "Completed 3.0
                # MiB/20.0 MiB"); the ends are filenames and units that repeat.
                t = t[:room - 1] + "…"
            tail = "  " + C.dim(t)
        live.draw([C.cyan(head) + tail])

    try:
        while not done:
            try:
                item = q.get(timeout=0.12)
            except queue.Empty:
                spin += 1
                render()
                continue
            if item is None:
                done = True
                break
            lines.append(item)
            if passthrough or not live.enabled:
                # No live area (piped output, --no-color, or a step whose full
                # output is the point): print everything plainly. Suppressing it
                # here would leave a long render looking like a hung terminal.
                print(item)
                continue
            stripped = item.rstrip()
            if stripped:
                last_raw = stripped
            if keep and keep(stripped):
                live.emit("  " + stripped)
            if parser:
                got = parser(stripped)
                if got is not None:
                    frac, note = got
            spin += 1
            render()
        rc = proc.wait()
    except KeyboardInterrupt:
        # Kill the whole child group, not just the wrapper shell — otherwise the
        # ffmpeg or rsync it spawned keeps running after we return to the menu.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        live.close()
        raise Aborted()
    finally:
        live.close()
        if out_fh:
            out_fh.close()

    if rc != 0:
        print(C.red("  FAILED: %s (exit %d)" % (" ".join(cmd), rc)))
        tail = [l for l in lines if l.strip()][-tail_lines:]
        if tail:
            print(C.dim("  --- last %d line(s) of output ---" % len(tail)))
            for l in tail:
                print(C.dim("  " + l))
    return rc, lines


# ---------------------------------------------------------------------------
# Parsers — one per tool, each derived from that tool's real output format.
# ---------------------------------------------------------------------------

# rsync --info=progress2:  "  1,234,567,890  47%  120.55MB/s    0:01:23 (xfr#…)"
RE_RSYNC_P2 = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d{2}:\d{2})")
# aws s3 sync byte form:   "Completed 3.5 MiB/~120.0 MiB (2.0 MiB/s) with ~5 file(s) remaining"
RE_AWS_BYTES = re.compile(r"Completed\s+([\d.]+)\s+(\w+)/~?([\d.]+)\s+(\w+)")
# aws s3 sync file form:   "Completed 3 file(s) with ~5 file(s) remaining"
RE_AWS_FILES = re.compile(r"Completed\s+(\d+)\s+file\(s\)\s+with\s+~?(\d+)\s+file\(s\) remaining")
RE_AWS_UPLOAD = re.compile(r"^upload:\s+(.+?)\s+to\s+s3://")
# make_dashcam_videos:     "[Trip 2/5] 2026-07-19 12:46 -> 13:20  (87 clips, ~14:02)"
RE_TRIP = re.compile(r"^\[Trip\s+(\d+)/(\d+)\]")
#                          "  [ 12/ 87] 2026-07-19 12:46:03  encoding ..."
RE_CLIP = re.compile(r"^\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]")

_AWS_UNIT = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
             "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


def rsync_parser(line):
    m = RE_RSYNC_P2.match(line)
    if not m:
        return None
    pct = int(m.group(2)) / 100.0
    # rsync computes its own ETA from its own byte totals; prefer it over ours.
    return pct, "rate %s  rsync eta %s" % (m.group(3), m.group(4))


def make_render_parser():
    """Trip progress from the '[Trip a/b]' headers, clip progress within a trip.

    'a' in that header is the per-DAY publish number and repeats across days, so
    it cannot be used as a counter — we count how many headers we have seen and
    take only the denominator from it.
    """
    state = {"trips_seen": 0, "trips_total": None, "clip": 0, "clips": 0}

    def parse(line):
        m = RE_TRIP.match(line)
        if m:
            state["trips_seen"] += 1
            state["trips_total"] = int(m.group(2))
            state["clip"], state["clips"] = 0, 0
        else:
            m = RE_CLIP.match(line)
            if m:
                state["clip"], state["clips"] = int(m.group(1)), int(m.group(2))
            else:
                return None
        total = state["trips_total"]
        if not total:
            return None
        within = (state["clip"] / state["clips"]) if state["clips"] else 0.0
        frac = (max(state["trips_seen"] - 1, 0) + within) / total
        # Show the clip counter too. A trip takes minutes and its number barely
        # moves; the clip counter ticks every few seconds, and it is the thing
        # that tells you the encode is alive rather than wedged.
        note = "trip %d/%d" % (state["trips_seen"], total)
        if state["clips"]:
            note += " clip %d/%d" % (state["clip"], state["clips"])
        return min(frac, 1.0), note

    return parse


def make_upload_parser():
    state = {"files": 0}

    def parse(line):
        m = RE_AWS_UPLOAD.match(line)
        if m:
            state["files"] += 1
            return None
        m = RE_AWS_BYTES.search(line)
        if m:
            try:
                done = float(m.group(1)) * _AWS_UNIT.get(m.group(2), 1)
                total = float(m.group(3)) * _AWS_UNIT.get(m.group(4), 1)
            except ValueError:
                return None
            if total <= 0:
                return None
            return min(done / total, 1.0), "%s / %s, %d file(s) done" % (
                human_bytes(done), human_bytes(total), state["files"])
        m = RE_AWS_FILES.search(line)
        if m:
            done, remaining = int(m.group(1)), int(m.group(2))
            if done + remaining <= 0:
                return None
            return done / float(done + remaining), "%d file(s) done" % done
        return None

    return parse


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def count_files(path):
    n = 0
    for _root, _dirs, files in os.walk(path):
        n += len(files)
    return n


def tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def clip_count(dcim_parent):
    """Clip pairs in an import folder = files in DCIM/200video/front."""
    front = dcim_parent / "DCIM" / "200video" / "front"
    if not front.is_dir():
        return None
    return sum(1 for p in front.iterdir() if p.is_file())


def import_candidates(ctx):
    """Folders that could be passed as --root: anything holding a DCIM tree.

    The sink itself is a candidate (config.txt's `root` points straight at it),
    and so is every dated subfolder, because import-sd-card.sh writes to
    <sink>/<day>/DCIM. Both layouts exist in the wild here — see the note at the
    bottom of this file.
    """
    found = []
    root = ctx.import_root
    if (root / "DCIM").is_dir():
        found.append(root)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "DCIM").is_dir():
                found.append(child)
    # config.txt's root may live somewhere else entirely; include it if so.
    if (ctx.render_root / "DCIM").is_dir() and ctx.render_root not in found:
        found.insert(0, ctx.render_root)
    return found


def rendered_mp4s(out_dir):
    """Finished renders only.

    Skips dot-directories: .intermediates/ holds the per-clip scratch encodes,
    which exist mid-render and after --keep-intermediates. Counting them would
    inflate the status screen and, worse, let the delete guard's "enough mp4s
    exist" check be satisfied by scratch files.
    """
    if not out_dir.is_dir():
        return []
    return sorted(p for p in out_dir.rglob("*.mp4")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(out_dir).parts))


def live_trip_count(ctx):
    if ctx.offline:
        return None
    try:
        with urllib.request.urlopen(LIVE_TRIPS_URL, timeout=6) as r:
            data = json.load(r)
        # Count the days array, not the top-level trip_count. That field is
        # denormalised and can go stale if anything edits trips.json without
        # recomputing it — which has already happened once, and made this
        # status screen confidently report a trip that was not there.
        # The array IS the manifest; the count is a summary of it.
        n = sum(len(d.get("trips", [])) for d in data.get("days", []))
        declared = data.get("trip_count")
        if declared is not None and declared != n:
            n = "%d (manifest says %d — stale trip_count)" % (n, declared)
        return n
    except Exception:
        return None


def print_status(ctx):
    print()
    print(rule("status"))

    # SD card
    card_dcim = ctx.card / "DCIM"
    if card_dcim.is_dir():
        n = clip_count(ctx.card)
        print("  SD card      %s  %s" % (
            C.green("mounted"),
            C.dim("%s  (%s clips)" % (ctx.card, n if n is not None else "?"))))
    else:
        print("  SD card      %s  %s" % (C.dim("not mounted"), C.dim(str(ctx.card))))

    # Import sink
    cands = import_candidates(ctx)
    if cands:
        for p in cands:
            n = clip_count(p)
            print("  Import       %s  %s" % (
                C.bold(str(p)),
                C.dim("%s clips, %s" % (n if n is not None else "?", human_bytes(tree_size(p))))))
    else:
        print("  Import       %s  %s" % (C.dim("empty"), C.dim(str(ctx.import_root))))

    # Renders
    mp4s = rendered_mp4s(ctx.out_dir)
    size = sum(p.stat().st_size for p in mp4s) if mp4s else 0
    print("  Rendered     %s  %s" % (
        C.bold("%d mp4" % len(mp4s)) if mp4s else C.yellow("none"),
        C.dim("%s in %s" % (human_bytes(size), ctx.out_dir))))

    # Manifest / live site
    manifest = ctx.site / "public_html" / "trips.json"
    if manifest.is_file():
        try:
            local_trips = json.loads(manifest.read_text()).get("trip_count", "?")
        except Exception:
            local_trips = "?"
        age = human_age(time.time() - manifest.stat().st_mtime)
        print("  Manifest     %s  %s" % (C.bold("%s trips" % local_trips),
                                         # "just now" already reads as a time;
                                         # appending "ago" gives "just now ago"
                                         C.dim("trips.json, %s" % age if age == "just now"
                                               else "trips.json, %s ago" % age)))
    else:
        print("  Manifest     %s  %s" % (C.yellow("not built"), C.dim(str(manifest))))

    live = live_trip_count(ctx)
    if live is None:
        print("  Live site    %s" % C.dim("unknown (offline or unreachable)"))
    else:
        print("  Live site    %s  %s" % (C.bold("%d trips" % live), C.dim(LIVE_TRIPS_URL)))

    # Disk
    for label, path in (("output", ctx.out_dir), ("import", ctx.import_root)):
        try:
            u = shutil.disk_usage(str(path if path.exists() else path.parent))
            print("  Disk (%s) %s free of %s" % (
                label.ljust(6), C.bold(human_bytes(u.free)), human_bytes(u.total)))
        except OSError:
            pass

    print("  Repos        %s" % C.dim("%s  |  %s" % (ctx.exporter, ctx.site)))
    if not ctx.site.is_dir():
        print("  " + C.red("goodnight-drives repo not found — steps 6-8 will not run."))
    print(rule())


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def ask(prompt, default=""):
    # Ctrl-C at a prompt has to mean the same thing as Ctrl-C during a child
    # process: abort the step and let it be recorded, not slip out at exit 0.
    try:
        s = input(C.bold(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Aborted()
    return s or default


def confirm(prompt, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    s = ask(prompt + suffix).lower()
    if not s:
        return default
    return s in ("y", "yes")


def pick_import(ctx, purpose):
    """Choose which import folder a step should work on."""
    cands = import_candidates(ctx)
    if not cands:
        print(C.red("  No import folder with a DCIM tree under %s" % ctx.import_root))
        return None
    if len(cands) == 1:
        ctx.selected_import = cands[0]
        return cands[0]
    if ctx.selected_import in cands:
        keep = confirm("  Use %s for %s?" % (ctx.selected_import, purpose), True)
        if keep:
            return ctx.selected_import
    print("  Import folders:")
    for i, p in enumerate(cands, 1):
        n = clip_count(p)
        print("    %d) %s  %s" % (i, p, C.dim("%s clips" % (n if n is not None else "?"))))
    s = ask("  Which one? [1] ", "1")
    try:
        ctx.selected_import = cands[int(s) - 1]
    except (ValueError, IndexError):
        print(C.red("  Not a listed number."))
        return None
    return ctx.selected_import


# ---------------------------------------------------------------------------
# Step results / summary
# ---------------------------------------------------------------------------

RAN, SKIPPED, FAILED = "ran", "skipped", "failed"


class StepResult:
    def __init__(self, name, status, seconds, detail=""):
        self.name, self.status, self.seconds, self.detail = name, status, seconds, detail


def record(ctx, name, status, started, detail=""):
    ctx.results.append(StepResult(name, status, time.time() - started, detail))
    return status != FAILED


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_import(ctx):
    """Copy the card's DCIM tree into a dated import folder (import-sd-card.sh)."""
    started = time.time()
    if not (ctx.card / "DCIM").is_dir():
        # No card is not automatically a problem. The configured root may
        # already hold a copied card, in which case importing is simply not the
        # step he wants — saying "is the card mounted?" there sends him looking
        # for a fault that does not exist. Check the second location and answer
        # the question he actually has: is there footage here to work on?
        if (ctx.import_root / "DCIM").is_dir():
            n = clip_count(ctx.import_root)
            sz = human_bytes(tree_size(ctx.import_root / "DCIM"))
            print(C.green("  Nothing to import — %s already holds %s clips (%s)."
                          % (ctx.import_root, n, sz)))
            print(C.dim("  That is the configured root from config.txt. Go to Preview (3) "
                        "or Render (5)."))
            return record(ctx, "Import from SD card", SKIPPED, started,
                          "import already present, %s clips" % n)
        print(C.yellow("  No card at %s and no DCIM at %s — is the card mounted?"
                       % (ctx.card, ctx.import_root)))
        return record(ctx, "Import from SD card", SKIPPED, started, "no card, no import")

    clips = clip_count(ctx.card)
    size = tree_size(ctx.card / "DCIM")
    print("  Source: %s  (%s clips, %s)" % (ctx.card, clips, human_bytes(size)))
    day = ask("  Day folder name [%s]: " % time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d"))
    print(C.dim("  The card is NOT erased by default; import-sd-card.sh only deletes"))
    print(C.dim("  the card's files after the copy verifies file-for-file."))
    erase = confirm("  Erase the card's files after a verified copy?", False)

    cmd = ["./import-sd-card.sh"]
    if erase:
        cmd.append("--delete")
    cmd.append(day)
    if str(ctx.card) != DEFAULT_CARD:
        cmd[1:1] = ["--src", str(ctx.card)]

    if not confirm("  Run: %s ?" % " ".join(cmd), True):
        return record(ctx, "Import from SD card", SKIPPED, started, "declined")

    rc, lines = run_stream(cmd, ctx.exporter, "Import", parser=rsync_parser,
                           keep=lambda l: l.startswith(("Verified:", "Card cleaned", "Done.")))
    if rc != 0:
        return record(ctx, "Import from SD card", FAILED, started, "exit %d" % rc)

    dest = ctx.import_root / day
    ctx.selected_import = dest if (dest / "DCIM").is_dir() else ctx.selected_import
    # An import MERGES into an existing day folder (rsync), so any scan taken
    # before it is now stale — it does not know about the clips that just landed.
    # The delete guard leans on that scan, so leaving it in place would let it
    # approve erasing footage nothing has ever looked at.
    ctx.last_scan = None
    ctx.last_groups = None
    return record(ctx, "Import from SD card", RAN, started,
                  "%s clips, %s -> %s" % (clips, human_bytes(size), dest))


class ScanResult:
    def __init__(self, root, total, skipped, lines):
        self.root = root
        self.total = total            # trips found
        self.skipped = skipped        # indices auto-skipped (fragments / stationary)
        self.lines = lines

    @property
    def renderable(self):
        return self.total - len(self.skipped)


def parse_scan(root, lines):
    total = 0
    skipped = set()
    for l in lines:
        m = re.search(r"grouped into (\d+) trips", l)
        if m:
            total = int(m.group(1))
        if "Auto-skipping" in l:
            # The indices follow on the same line as "#3 (2 clips), #7" etc.
            for num in re.findall(r"#(\d+)", l):
                skipped.add(int(num))
    return ScanResult(root, total, skipped, lines)


def step_list(ctx):
    """Dry-run scan: the trip table, with indices to feed the render step."""
    started = time.time()
    root = pick_import(ctx, "the trip scan")
    if root is None:
        return record(ctx, "List trips", SKIPPED, started, "no import folder")

    print(C.dim("  Scanning %s (read-only, no encoding). This walks the video for" % root))
    print(C.dim("  drive-away/park detection, so it takes a while on a full card."))
    # passthrough: the trip table IS the result, so print it verbatim and keep it.
    rc, lines = run_stream(["./list-trips-data.sh", "--root", str(root)] + ctx.config_args,
                           ctx.exporter, "Scan", passthrough=True)
    if rc != 0:
        return record(ctx, "List trips", FAILED, started, "exit %d" % rc)
    ctx.last_scan = parse_scan(root, lines)
    return record(ctx, "List trips", RAN, started,
                  "%d trips found, %d renderable" % (ctx.last_scan.total, ctx.last_scan.renderable))


# ---------------------------------------------------------------------------
# The trip -> source clip mapping.
#
# Everything below that names a file of original footage — the preview contact
# sheet, and the drop step that DELETES — reads this one mapping, straight from
# the scanner's own grouping (make_dashcam_videos --print-groups, which
# serialises what group_into_trips returned). Nothing here reconstructs a trip
# boundary from filename timestamps: the boundaries come from video ego-motion
# and GPS, a filename cannot express them, and being one clip wrong at a
# boundary means deleting footage that belonged to a trip he wanted.
# ---------------------------------------------------------------------------

def renderer_python(ctx):
    """The interpreter the wrapper scripts use for the renderer.

    list-trips-data.sh and make-trips-rendered.sh both prefer .venv/bin/python
    when it exists, and that is not cosmetic: the venv has numpy + opencv, so
    trip boundaries are found by video ego-motion there and degrade to the
    GPS-radius fallback under a bare python3. The two can group the same card
    differently. Since the drop step deletes by this mapping, it has to be the
    mapping the renders were made from.
    """
    venv = ctx.exporter / ".venv" / "bin" / "python"
    return str(venv) if os.access(str(venv), os.X_OK) else "python3"


def require_ego_motion(ctx):
    """Refuse to start without numpy + opencv.

    Without them the renderer does not fail — it quietly groups trips by GPS
    radius instead of video ego-motion, and the two disagree. On this card the
    fallback found 9 trips over 15h12m where ego-motion finds 6 over 10h48m,
    inventing a 3-second trip and folding 4.5 hours of parked recording into a
    'drive'. Everything downstream inherits that: the previews you judge from,
    the render, and the mapping the drop step deletes by.

    A silently worse answer is the failure mode worth refusing outright.
    """
    py = renderer_python(ctx)
    r = subprocess.run([py, "-c", "import cv2, numpy"],
                       capture_output=True, cwd=str(ctx.exporter))
    if r.returncode == 0:
        return True
    print()
    print(C.red("  Ego-motion detection is not available — refusing to start."))
    print()
    print("  Trip boundaries would fall back to a GPS radius, which groups this")
    print("  card differently: it merges parked hours into drives and invents")
    print("  trips that are not there. Previews, renders and the drop step would")
    print("  all be built on the wrong grouping.")
    print()
    print("  Interpreter checked: %s" % py)
    print(C.bold("  Install with:"))
    print("    cd %s" % ctx.exporter)
    print("    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    print()
    return False


def load_groups(ctx, root, refresh=False):
    """Run --print-groups against `root` and return its parsed JSON, or None.

    Cached per session per import folder: the scan decodes video to find the
    pull-away and park moments, so it costs the same minutes as step 2.
    """
    if not refresh and ctx.last_groups and ctx.last_groups[0] == root:
        print(C.dim("  Using the trip grouping already scanned in this session."))
        return ctx.last_groups[1]

    print(C.dim("  Scanning %s for the authoritative trip grouping." % root))
    print(C.dim("  This is the same work as step 2 (it walks the video), so it takes"))
    print(C.dim("  a while; the result is reused for the rest of this session."))
    fd, tmp = tempfile.mkstemp(prefix="dashcam-groups-", suffix=".json")
    os.close(fd)
    try:
        rc, _lines = run_stream(
            [renderer_python(ctx), "-u", "make_dashcam_videos.py", "--print-groups",
             "--root", str(root)] + ctx.config_args,
            ctx.exporter, "Grouping", stdout_file=tmp)
        if rc != 0:
            return None
        try:
            with open(tmp, "r") as fh:
                payload = json.load(fh)
        except ValueError as e:
            print(C.red("  --print-groups did not return usable JSON: %s" % e))
            return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    ctx.last_groups = (root, payload)
    return payload


def trip_files(trip):
    """Every source file the scanner put in this trip: front clips, then rear.

    A clip can have no rear file, so the rear list is often shorter than the
    front one — they are two lists, not two columns.
    """
    return ([Path(p) for p in trip.get("front", [])] +
            [Path(p) for p in trip.get("rear", [])])


def trip_bytes(trip):
    total = 0
    for p in trip_files(trip):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def trip_meta(trip):
    """The trip's _meta.json (written by a render OR by --sidecars-only), or None."""
    base = trip.get("out_base")
    if not base:
        return None
    p = Path(base + "_meta.json")
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _overlaps(a_start, a_end, b_start, b_end):
    """Half-open interval overlap on '%Y-%m-%d %H:%M:%S' strings.

    That format is fixed-width and zero-padded, so lexicographic order IS
    chronological order and no date parsing is needed.
    """
    return a_start < b_end and b_start < a_end


def trip_renders(ctx, payload, trip):
    """Rendered mp4s whose footage is this trip's, as (same_import, other_import).

    Two questions with two different answers. 'Is a render of THIS import built
    on these clips?' decides whether dropping them is allowed at all — that is
    the delete-import operation, which has its own guards. 'Does a render of
    this footage exist ANYWHERE?' decides whether these clips are the last copy.

    Both are answered from recorded metadata: each rendered trip's _meta.json
    carries source_import plus its start/end, and the mp4 must actually be on
    disk (a --sidecars-only preview writes the meta without any video, and that
    is precisely the state this whole flow is designed to review). A mp4 with
    no meta beside it is matched by name against the trip's own out_base, which
    is where this trip's render would land.
    """
    same, other = [], []
    if not ctx.out_dir.is_dir():
        return same, other
    # Compare resolved paths: the same folder reached through a symlink or a
    # relative --root spells differently in source_import, and a mismatch here
    # would quietly downgrade a refusal into a permission.
    root = Path(payload.get("root", "")).expanduser().resolve()
    for meta_path in ctx.out_dir.rglob("trip_*_meta.json"):
        try:
            m = json.loads(meta_path.read_text())
        except Exception:
            continue
        mp4 = meta_path.parent / m.get("video", "")
        if not mp4.is_file():
            continue
        if not _overlaps(trip["start"], trip["end"],
                         m.get("start", ""), m.get("end", "")):
            continue
        src = m.get("source_import")
        if not src:
            # An overlapping render whose origin was never recorded (an older
            # meta). Count it as this import's: refusing on the unknown is the
            # cheap mistake, permitting on it is the expensive one.
            same.append(mp4)
        elif Path(src).expanduser().resolve() == root:
            same.append(mp4)
        else:
            other.append(mp4)
    base = trip.get("out_base")
    if base:
        day_dir = Path(base).parent
        if day_dir.is_dir():
            for mp4 in sorted(day_dir.glob(Path(base).name + "*.mp4")):
                if mp4 not in same:
                    same.append(mp4)
    return same, other


# ---------------------------------------------------------------------------
# Preview pass: sidecars + one still per trip + a local contact sheet
# ---------------------------------------------------------------------------

PREVIEW_DIRNAME = "previews"
PREVIEW_STILL_W = 1600      # same intent as build_manifest.make_poster's POSTER_W
PREVIEW_STILL_T = 1.0       # seconds into the clip; see extract_still


def extract_still(src, dst, seconds=PREVIEW_STILL_T, width=PREVIEW_STILL_W):
    """One frame from a source clip, written as a jpg. True on success.

    Same recipe as build_manifest.make_poster, for the same reasons: a beat in,
    so a fade-from-black or still-auto-exposing first frame is not what he
    judges the trip by, and scale='min(W,iw)' so a clip narrower than W is
    never upscaled into invented detail. `-ss` before `-i` plus -frames:v 1
    means ffmpeg seeks and decodes one frame — it does not read the clip.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    for t in (seconds, 0):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(src),
               "-frames:v", "1", "-vf", "scale='min(%d\\,iw)':-2" % width,
               "-q:v", "5", str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            break
        if r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return True
        # A clip shorter than the seek point yields no frame; retry at 0 before
        # giving up, rather than reporting a trip as unpreviewable.
    # Do not leave a truncated or empty jpg behind: the contact sheet would show
    # a broken image instead of saying plainly that there is no still.
    try:
        if dst.is_file() and dst.stat().st_size == 0:
            dst.unlink()
    except OSError:
        pass
    return False


PREVIEW_CSS = """
:root{--bg0:#060C16;--bg1:#0a1422;--card:#0b1524;--line:#1b2a3e;
      --ink:#e8eef6;--dim:#9fb2c9;--faint:#6b7f99;--orange:#E08A3C;
      --red:#e5564a;--cyan:#35C3D6;
      --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg0);color:var(--ink);
     font-family:var(--font);font-size:15px;line-height:1.5}
header{max-width:1180px;margin:0 auto 26px}
h1{margin:0 0 6px;font-size:22px;letter-spacing:.04em}
h1 span{color:var(--orange);text-transform:uppercase;letter-spacing:.16em;font-size:15px}
header p{margin:6px 0;color:var(--dim);font-size:14px;max-width:78ch}
header code{color:var(--ink);background:#0e1a2b;padding:1px 5px;border-radius:4px;
            font-size:13px}
main{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      overflow:hidden;display:grid;grid-template-columns:minmax(0,420px) minmax(0,1fr)}
@media (max-width:860px){.card{grid-template-columns:1fr}}
.shot{background:#000;display:block}
.shot img{display:block;width:100%;height:auto}
.shot .none{padding:48px 16px;text-align:center;color:var(--faint);font-size:13px}
.body{padding:16px 18px}
.title{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.title b{font-size:17px}
.title .day{color:var(--dim);font-size:14px}
.flags{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.flag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
      color:var(--dim)}
.flag.warn{color:var(--orange);border-color:#4a3520}
.flag.bad{color:var(--red);border-color:#4a2320}
.flag.ok{color:var(--cyan);border-color:#1d3a44}
dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));
   gap:10px 16px;margin:0 0 14px}
dt{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
dd{margin:2px 0 0;font-size:15px}
dd small{color:var(--faint);font-size:12px;display:block;line-height:1.35}
.links a{color:var(--cyan);text-decoration:none;font-size:13px;margin-right:14px}
.links a:hover{text-decoration:underline}
.links .off{color:var(--faint);font-size:13px;margin-right:14px}
details{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;color:var(--dim);font-size:13px}
details ul{list-style:none;margin:10px 0 0;padding:0;
           display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:2px 14px}
details li{font-size:12px;color:var(--dim);font-family:ui-monospace,Menlo,monospace}
details li span{color:var(--faint)}
details pre{margin:10px 0 0;padding:10px;background:#08111d;border-radius:6px;
            overflow-x:auto;font-size:11.5px;color:var(--faint);
            font-family:ui-monospace,Menlo,monospace}
footer{max-width:1180px;margin:34px auto 0;color:var(--faint);font-size:12.5px}
"""


def _clip_list_html(label, paths, previews_dir):
    if not paths:
        return ""
    total = 0
    items = []
    for p in paths:
        try:
            n = p.stat().st_size
        except OSError:
            n = 0
        total += n
        items.append("<li title=\"%s\">%s <span>%s</span></li>" % (
            html.escape(str(p)), html.escape(p.name), human_bytes(n)))
    return (
        "<details><summary>%s source clips: %d file(s), %s</summary>"
        "<ul>%s</ul>"
        "<details><summary>full paths (copy/paste)</summary><pre>%s</pre></details>"
        "</details>" % (
            html.escape(label), len(paths), human_bytes(total), "".join(items),
            html.escape("\n".join(str(p) for p in paths))))


def write_contact_sheet(ctx, root, payload, previews_dir, stills):
    """One self-contained page, openable from file://, one card per trip.

    No external CSS, fonts, scripts or images and only relative hrefs, because
    the entire point of this pass is reviewing BEFORE anything is published.
    """
    trips = payload.get("trips", [])
    cards = []
    for t in trips:
        idx = t["index"]
        meta = trip_meta(t)
        still = stills.get(idx)
        if still is not None:
            rel = html.escape(os.path.relpath(str(still), str(previews_dir)))
            shot = ('<a class="shot" href="%s"><img src="%s" alt="Trip %d first frame"></a>'
                    % (rel, rel, idx))
        else:
            shot = '<div class="shot"><div class="none">no still<br>(ffmpeg could not read the first clip)</div></div>'

        span_secs = t.get("duration_secs") or 0
        moving = meta.get("moving_min") if meta else None
        gps_points = meta.get("gps_points") if meta else None

        flags = []
        if not t.get("renderable"):
            flags.append('<span class="flag bad">auto-skipped: %s</span>'
                         % html.escape(t.get("reason") or "not renderable"))
        if meta is None:
            # An auto-skipped trip is never rendered, so --sidecars-only writes
            # nothing for it: no map, no stats. Say that, rather than leaving him
            # wondering why a card is empty.
            flags.append('<span class="flag warn">%s</span>' % (
                "no map or stats — auto-skipped trips get no sidecars"
                if not t.get("renderable") else "no sidecar metadata yet"))
        elif not gps_points:
            flags.append('<span class="flag warn">no GPS — renders without a map, '
                         'stats are all zero</span>')
        if t.get("renderable") and meta is not None and gps_points:
            flags.append('<span class="flag ok">ready to render</span>')

        def num(v, unit, digits=1):
            if v is None:
                return "&mdash;"
            return "%.*f %s" % (digits, v, html.escape(unit))

        rows = [
            ("span", "%s &rarr; %s" % (html.escape(t["start"][11:16]),
                                       html.escape(t["end"][11:16])),
             "%s wall clock, parking included" % human_secs(span_secs)),
            ("moving", (human_secs(moving * 60) if moving else "&mdash;"),
             "what actually gets rendered"),
            ("distance", num(meta.get("distance_km") if meta else None, "km", 1), ""),
            ("speed", ("max %s / avg %s" % (num(meta.get("max_kmh"), "", 0),
                                            num(meta.get("avg_kmh"), "", 0))).strip()
             if meta else "&mdash;", "km/h" if meta else ""),
            ("clips", "%d" % t.get("clips", 0), "%s of source" % human_bytes(trip_bytes(t))),
            ("gps points", ("%d" % gps_points) if gps_points
             else ("0" if meta else "&mdash;"), ""),
        ]
        dl = "".join("<dt>%s</dt><dd>%s%s</dd>" % (
            html.escape(k), v, ("<small>%s</small>" % s) if s else "")
            for k, v, s in rows)

        links = []
        base = t.get("out_base")
        for label, suffix in (("map (.html)", ".html"), ("track (.gpx)", ".gpx")):
            p = Path(base + suffix) if base else None
            if p is not None and p.is_file():
                links.append('<a href="%s">%s</a>' % (
                    html.escape(os.path.relpath(str(p), str(previews_dir))),
                    html.escape(label)))
            else:
                links.append('<span class="off">%s: none</span>' % html.escape(label))

        cards.append(
            '<section class="card">%s<div class="body">'
            '<div class="title"><b>Trip %d</b><span class="day">%s &middot; %s</span></div>'
            '<div class="flags">%s</div><dl>%s</dl><div class="links">%s</div>%s%s'
            '</div></section>' % (
                shot, idx, html.escape(t["day"]), html.escape(t["start"][11:16]),
                "".join(flags) or '<span class="flag">&nbsp;</span>', dl,
                "".join(links),
                _clip_list_html("front", [Path(p) for p in t.get("front", [])], previews_dir),
                _clip_list_html("rear", [Path(p) for p in t.get("rear", [])], previews_dir)))

    total_bytes = sum(trip_bytes(t) for t in trips)
    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Trip previews — %s</title><style>%s</style></head><body>"
        "<header><h1><span>preview</span> %d trip(s) in %s</h1>"
        "<p>Stills are a single frame from each trip's first front clip — no video "
        "has been encoded and nothing has been published. The maps and numbers come "
        "from the sidecars written by <code>--sidecars-only</code>.</p>"
        "<p><b>Span</b> is wall clock from the first clip to the last and includes "
        "parking; <b>moving</b> is what the render actually keeps, and it is the one "
        "that predicts encode time and upload size. All times are the camera's local "
        "clock, as on the site.</p>"
        "<p>%s of source footage in total. Drop the trips you do not want from the "
        "import with the pipeline's drop step — the clips listed on a card are exactly "
        "the files that step deletes.</p></header>"
        "<main>%s</main>"
        "<footer>Generated by pipeline.py from make_dashcam_videos.py "
        "--print-groups. Self-contained: no network, no scripts.</footer>"
        "</body></html>" % (
            html.escape(root.name), PREVIEW_CSS, len(trips), html.escape(str(root)),
            human_bytes(total_bytes), "".join(cards)))

    index = previews_dir / "index.html"
    index.write_text(doc, encoding="utf-8")
    return index


def step_preview(ctx):
    """Sidecars + one still per trip + a local contact sheet. No encoding.

    The cheap pass that makes pruning possible: encoding is hours and uploading
    is days, so the decision about which trips to keep has to be makeable before
    either. Nothing here writes video and nothing here publishes.
    """
    started = time.time()
    root = pick_import(ctx, "the preview pass")
    if root is None:
        return record(ctx, "Preview all trips", SKIPPED, started, "no import folder")

    previews_dir = ctx.out_dir / PREVIEW_DIRNAME
    print(C.dim("  Three cheap things, no encoding:"))
    print(C.dim("    1. --sidecars-only: each trip's .html map, .gpx and _meta.json"))
    print(C.dim("    2. one still per trip, a frame from its first front clip"))
    print(C.dim("    3. %s/index.html — a contact sheet to open locally" % previews_dir))
    print(C.dim("  Reviewing is entirely offline; deploying stays a separate choice."))

    if not confirm("  Run the preview pass on %s?" % root, True):
        return record(ctx, "Preview all trips", SKIPPED, started, "declined")

    # 1. Sidecars. The renderer prints its usual "[Trip a/b]" headers here, so
    #    the real trip counter drives the bar; there are no per-clip lines in
    #    this mode, and the parser simply shows no clip counter.
    cmd = ["./make-trips-rendered.sh", "--sidecars-only", "--root", str(root)] + ctx.config_args
    rc, _lines = run_stream(cmd, ctx.exporter, "Sidecars", parser=make_render_parser(),
                            keep=lambda l: l.startswith("[Trip "))
    if rc != 0:
        return record(ctx, "Preview all trips", FAILED, started, "sidecars exit %d" % rc)

    # 2. The grouping — which is also the trip -> source clip mapping the stills
    #    and the contact sheet's clip lists are built from.
    payload = load_groups(ctx, root)
    if payload is None:
        return record(ctx, "Preview all trips", FAILED, started, "--print-groups failed")
    trips = payload.get("trips", [])
    if not trips:
        print(C.yellow("  The scan found no trips in %s." % root))
        return record(ctx, "Preview all trips", SKIPPED, started, "no trips")

    # 3. Stills. Every trip gets one, including the auto-skipped fragments — he
    #    is deciding what to keep, and a trip he cannot see is one he cannot judge.
    previews_dir.mkdir(parents=True, exist_ok=True)
    stills, failed = {}, []
    for i, t in enumerate(trips, 1):
        front = t.get("front") or []
        name = "trip_%02d_%s_%s.jpg" % (t["index"], t["day"], t["start"][11:16].replace(":", "-"))
        dst = previews_dir / name
        print("  still %d/%d  %s" % (i, len(trips), name))
        if not front:
            failed.append(t["index"])
            continue
        if extract_still(Path(front[0]), dst):
            stills[t["index"]] = dst
        else:
            failed.append(t["index"])
    if failed:
        print(C.yellow("  No still for trip(s) %s — ffmpeg could not read the first clip."
                       % ", ".join(str(i) for i in failed)))

    index = write_contact_sheet(ctx, root, payload, previews_dir, stills)

    # 4. Keep trips.json current. The site is not deployed here — this only means
    #    a later deploy is not carrying a stale manifest.
    if ctx.site.is_dir():
        rc, _lines = run_stream(["python3", "build_manifest.py"], ctx.site, "Manifest",
                                keep=lambda l: l.startswith("wrote trips.json"))
        if rc != 0:
            return record(ctx, "Preview all trips", FAILED, started, "build_manifest exit %d" % rc)
    else:
        print(C.yellow("  goodnight-drives not at %s — skipped the manifest rebuild." % ctx.site))

    print()
    print(C.green("  previews are in %s" % previews_dir))
    print("  %d trip(s), %d still(s). Open the contact sheet with:" % (len(trips), len(stills)))
    print("    open %s" % index)
    print(C.dim("  Nothing was encoded and nothing was published. On the website these"))
    print(C.dim("  trips would say the video is not available — that is expected: the"))
    print(C.dim("  sidecars carry the map, the stats and the places, but no video exists"))
    print(C.dim("  yet. Render (and only then upload) the ones you decide to keep."))
    return record(ctx, "Preview all trips", RAN, started,
                  "%d trip(s), %d still(s) in %s" % (len(trips), len(stills), previews_dir))


# ---------------------------------------------------------------------------
# Drop a trip from the import — DESTRUCTIVE
# ---------------------------------------------------------------------------

def step_drop_trip(ctx):
    """Delete one trip's original clips from the import. Unrecoverable.

    The counterpart to the preview pass: he looks at the contact sheet, decides
    a trip is not worth hours of encoding and days of uploading, and removes its
    source clips so nothing downstream ever touches them.

    This is deliberately NOT the delete-import step. That one erases an import
    whose every trip is already rendered, on S3 and live — footage that exists
    elsewhere. This one erases footage that in the normal case exists NOWHERE
    else, so its job is to make that unmistakable and then get out of the way.
    """
    started = time.time()
    root = pick_import(ctx, "dropping a trip")
    if root is None:
        return record(ctx, "Drop trip from import", SKIPPED, started, "no import folder")

    payload = load_groups(ctx, root)
    if payload is None:
        return record(ctx, "Drop trip from import", FAILED, started, "--print-groups failed")
    trips = payload.get("trips", [])
    if not trips:
        print(C.yellow("  No trips in %s — nothing to drop." % root))
        return record(ctx, "Drop trip from import", SKIPPED, started, "no trips")

    print()
    print(rule("trips in %s" % root.name))
    by_index = {}
    for t in trips:
        by_index[t["index"]] = t
        note = "" if t.get("renderable") else C.yellow("  [%s]" % (t.get("reason") or "skipped"))
        print("  %2d) %s  %s -> %s  %3d clips  %8s  %9s%s" % (
            t["index"], t["day"], t["start"][11:16], t["end"][11:16], t.get("clips", 0),
            human_secs(t.get("duration_secs")), human_bytes(trip_bytes(t)), note))
    print(rule())
    print(C.dim("  The stills and maps for these are in %s" % (ctx.out_dir / PREVIEW_DIRNAME)))

    sel = ask("  Trip indices to DROP (space separated, blank = cancel): ")
    if not sel.strip():
        return record(ctx, "Drop trip from import", SKIPPED, started, "cancelled")
    picked = []
    for part in re.split(r"[,\s]+", sel.strip()):
        if not part.isdigit() or int(part) not in by_index:
            print(C.red("  %r is not one of the listed trip indices." % part))
            return record(ctx, "Drop trip from import", SKIPPED, started, "bad selection")
        if int(part) not in picked:
            picked.append(int(part))

    # --- guard: never drop the source of something already rendered from THIS
    # import. That is the delete-import operation, which has its own three
    # guards (rendered / on S3 / live on the site) precisely because it is a
    # different and more dangerous thing than discarding footage nobody kept.
    blocked = []
    for i in picked:
        same, _other = trip_renders(ctx, payload, by_index[i])
        if same:
            blocked.append((i, same))
    if blocked:
        print()
        for i, mp4s in blocked:
            print(C.red("  Trip %d is already rendered from this import:" % i))
            for p in mp4s[:5]:
                print(C.red("      %s" % p))
        print(C.red("  Refusing. Dropping the source of an existing render is the"))
        print(C.red("  delete-import operation — use that step, which first proves the"))
        print(C.red("  renders are on S3 and live on the site."))
        return record(ctx, "Drop trip from import", SKIPPED, started,
                      "refused: trip(s) %s already rendered" % ", ".join(str(i) for i, _ in blocked))

    # --- what will actually be deleted, file by file.
    files, total = [], 0
    for i in picked:
        files.extend(trip_files(by_index[i]))
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass

    print()
    print(rule("drop from import"))
    for i in picked:
        t = by_index[i]
        print("  Trip %d  %s  %s -> %s  %d clips  %s" % (
            i, t["day"], t["start"][11:16], t["end"][11:16], t.get("clips", 0),
            human_bytes(trip_bytes(t))))
    print()
    print("  %d file(s) will be deleted:" % len(files))
    for p in files:
        print(C.dim("    %s" % p))
    print("  Total: %s" % C.bold(human_bytes(total)))
    print(C.dim("  (The .gpx files in DCIM/203gps are left alone — they are tiny and"))
    print(C.dim("   harmless without their clips.)"))

    # --- the "only copy" warning. A trip with no render anywhere and nothing on
    # S3 exists solely as these files, and this deletes them.
    objs = None
    consulted_s3 = False        # NOT the same as objs is None: a failed listing
                                # also returns None, and an empty bucket returns
                                # {}. Saying "not consulted" when the listing
                                # actually failed is a lie in a delete prompt.
    only_copy, elsewhere = [], []
    for i in picked:
        t = by_index[i]
        _same, other = trip_renders(ctx, payload, t)
        on_s3 = False
        base = t.get("out_base")
        if base and not other:
            if not consulted_s3:
                objs = s3_objects()
                consulted_s3 = True
            if objs:
                key_part = Path(base).name
                on_s3 = any(key_part in k for k in objs)
        if other or on_s3:
            elsewhere.append(i)
        else:
            only_copy.append(i)
    if elsewhere:
        print(C.dim("  Trip(s) %s also exist as a render elsewhere or on S3." %
                    ", ".join(str(i) for i in elsewhere)))
    if only_copy:
        print()
        print(C.red("  " + "!" * (term_width() - 4)))
        print(C.red("  Trip(s) %s are NOT rendered anywhere and NOT on S3." %
                    ", ".join(str(i) for i in only_copy)))
        print(C.red("  These files are the ONLY copy of that footage. Deleting them"))
        print(C.red("  ends it — there is nothing to restore from, here or online."))
        if not consulted_s3:
            print(C.red("  (The S3 bucket was not consulted: those trips have no render"))
            print(C.red("   name to look for, so no object could exist for them.)"))
        elif objs is None:
            print(C.red("  (The bucket listing FAILED, so 'not on S3' is unverified —"))
            print(C.red("   an unknown is not evidence of a copy.)"))
        print(C.red("  " + "!" * (term_width() - 4)))

    answer = ask("  Type DROP to delete these %d file(s), anything else to cancel: " % len(files))
    if answer != "DROP":
        print("  Cancelled.")
        return record(ctx, "Drop trip from import", SKIPPED, started, "cancelled at the prompt")

    deleted, freed, errors = 0, 0, []
    for p in files:
        try:
            n = p.stat().st_size
        except OSError:
            n = 0
        try:
            p.unlink()
            deleted += 1
            freed += n
        except OSError as e:
            errors.append("%s: %s" % (p, e))
    for e in errors[:10]:
        print(C.red("  could not delete %s" % e))

    # Any cached view of this import is now wrong: the grouping is computed from
    # the clips that just stopped existing, and the delete guard leans on the
    # scan. Both have to be re-taken before anything trusts them again.
    ctx.last_groups = None
    ctx.last_scan = None

    # Offer to clear the preview sidecars of the trips that are now gone. They
    # are not source footage — they are a preview of something that no longer
    # exists, and left in place build_manifest keeps publishing a trip whose
    # video can never be rendered. Only ever offered for a trip with NO mp4,
    # which the guard above has already established.
    orphans = []
    for i in picked:
        base = by_index[i].get("out_base")
        if not base:
            continue
        for suffix in (".html", ".gpx", "_links.txt", "_meta.json"):
            p = Path(base + suffix)
            if p.is_file():
                orphans.append(p)
    if orphans:
        print()
        print("  %d preview sidecar(s) now describe a trip that no longer exists:" % len(orphans))
        for p in orphans:
            print(C.dim("    %s" % p))
        if confirm("  Remove them too (they are derived data, not footage)?", False):
            for p in orphans:
                try:
                    p.unlink()
                except OSError as e:
                    print(C.red("  could not delete %s: %s" % (p, e)))
            print(C.dim("  Removed. Re-run the manifest step so trips.json drops them."))

    if errors:
        return record(ctx, "Drop trip from import", FAILED, started,
                      "%d of %d file(s) deleted, %d error(s)" % (deleted, len(files), len(errors)))
    print(C.green("  Dropped trip(s) %s: %d file(s), %s freed." % (
        ", ".join(str(i) for i in picked), deleted, human_bytes(freed))))
    return record(ctx, "Drop trip from import", RAN, started,
                  "trip(s) %s, %d file(s), %s freed" % (
                      ", ".join(str(i) for i in picked), deleted, human_bytes(freed)))


def step_render(ctx):
    """Encode trips to mp4 + sidecars (make-trips-rendered.sh)."""
    started = time.time()
    root = pick_import(ctx, "rendering")
    if root is None:
        return record(ctx, "Render trips", SKIPPED, started, "no import folder")

    if ctx.last_scan and ctx.last_scan.root == root:
        print("  Last scan: %d trips, %d renderable%s" % (
            ctx.last_scan.total, ctx.last_scan.renderable,
            (", auto-skipped %s" % sorted(ctx.last_scan.skipped)) if ctx.last_scan.skipped else ""))
    else:
        print(C.dim("  No scan of this folder in this session — run step 2 for the indices."))

    idx = ask("  Trip indices to render (space separated, blank = all renderable): ")
    height = ask("  Output height [%d]: " % ctx.output_height, str(ctx.output_height))
    try:
        height = int(height)
    except ValueError:
        print(C.red("  Not a number."))
        return record(ctx, "Render trips", SKIPPED, started, "bad height")

    before = set(rendered_mp4s(ctx.out_dir))

    cmd = ["./make-trips-rendered.sh"]
    cmd += idx.split()                       # bare integers become --drives
    cmd += ["--root", str(root), "--output-height", str(height)] + ctx.config_args
    if not confirm("  Run: %s ?" % " ".join(cmd), True):
        return record(ctx, "Render trips", SKIPPED, started, "declined")

    rc, _lines = run_stream(cmd, ctx.exporter, "Render", parser=make_render_parser(),
                            keep=lambda l: l.startswith("[Trip ") or l.strip().startswith("✓ "))
    after = set(rendered_mp4s(ctx.out_dir))
    new = after - before
    if rc != 0:
        return record(ctx, "Render trips", FAILED, started,
                      "exit %d (%d new mp4 before the failure)" % (rc, len(new)))
    return record(ctx, "Render trips", RAN, started,
                  "%d new mp4, %s" % (len(new), human_bytes(sum(p.stat().st_size for p in new))))


def step_manifest(ctx):
    """Regenerate public_html/trips.json + thumbs from the render output."""
    started = time.time()
    if not ctx.site.is_dir():
        print(C.red("  goodnight-drives not found at %s" % ctx.site))
        return record(ctx, "Build manifest", FAILED, started, "site repo missing")

    n_meta = len(list(ctx.out_dir.rglob("trip_*_meta.json"))) if ctx.out_dir.is_dir() else 0
    print(C.dim("  %d rendered trip(s) to scan. Thumbnails are extracted with ffmpeg" % n_meta))
    print(C.dim("  and place names are reverse-geocoded (rate-limited to 1/s), so this"))
    print(C.dim("  is slow on a first build and near-instant on a rebuild."))
    # build_manifest.py uses relative paths (public_html/, admin.json,
    # .geocode_cache.json), so it MUST run with the site repo as cwd.
    # No parseable per-trip progress: spinner, not a fake percentage.
    rc, lines = run_stream(["python3", "build_manifest.py"], ctx.site, "Manifest",
                           keep=lambda l: l.startswith("wrote trips.json"))
    if rc != 0:
        return record(ctx, "Build manifest", FAILED, started, "exit %d" % rc)
    detail = next((l for l in lines if l.startswith("wrote trips.json")), "")
    return record(ctx, "Build manifest", RAN, started, detail or "trips.json written")


def s3_objects(bucket="your-media-bucket"):
    """{key: size} for every .mp4 in the bucket, or None if the listing failed."""
    try:
        p = subprocess.run(["aws", "s3", "ls", "s3://%s/" % bucket, "--recursive"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[3].endswith(".mp4"):
            try:
                out[parts[3]] = int(parts[2])
            except ValueError:
                pass
    return out


def deleted_ids(ctx):
    """Trip ids the admin flagged mode=delete — upload-videos-s3.sh skips these,
    so they must not count as 'missing from S3' when we verify the sync."""
    p = ctx.site / "admin.json"
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return set()
    return {e["id"] for e in data.get("excluded", []) if e.get("mode") == "delete" and e.get("id")}


def verify_s3(ctx, quiet=False):
    """Compare local mp4s against the bucket. Returns (ok, missing, mismatched).

    This exists because `aws s3 sync` exits 0 even when individual objects fail
    to upload — the exit code alone is not evidence that anything landed. The
    only trustworthy check is comparing keys and sizes afterwards.
    """
    local = rendered_mp4s(ctx.out_dir)
    skip = deleted_ids(ctx)
    objs = s3_objects()
    if objs is None:
        if not quiet:
            print(C.red("  Could not list the bucket (aws missing, no credentials, or network)."))
        return None, [], []
    missing, mismatched = [], []
    for p in local:
        key = p.relative_to(ctx.out_dir).as_posix()
        if any(i in key for i in skip):
            continue
        if key not in objs:
            missing.append(key)
        elif objs[key] != p.stat().st_size:
            mismatched.append("%s (local %s, S3 %s)" % (
                key, human_bytes(p.stat().st_size), human_bytes(objs[key])))
    return (not missing and not mismatched), missing, mismatched


def step_upload(ctx):
    """Sync the rendered mp4s to the private S3 bucket, then verify."""
    started = time.time()
    if not ctx.site.is_dir():
        return record(ctx, "Upload videos to S3", FAILED, started, "site repo missing")
    if not shutil.which("aws"):
        print(C.red("  awscli not found. brew install awscli && aws configure"))
        return record(ctx, "Upload videos to S3", FAILED, started, "awscli missing")

    # upload-videos-s3.sh syncs whatever public_html/videos resolves to, and the
    # object keys are paths relative to THAT. If it points somewhere other than
    # our out_dir, the keys we verify against would not be the keys it wrote.
    link = ctx.site / "public_html" / "videos"
    target = link.resolve() if link.exists() else None
    if target != ctx.out_dir.resolve():
        print(C.red("  public_html/videos -> %s but config out is %s" % (target, ctx.out_dir)))
        print(C.red("  Point the symlink at the render output before uploading."))
        return record(ctx, "Upload videos to S3", FAILED, started, "videos symlink mismatch")

    local = rendered_mp4s(ctx.out_dir)
    if not local:
        print(C.yellow("  No rendered mp4s under %s — nothing to upload." % ctx.out_dir))
        return record(ctx, "Upload videos to S3", SKIPPED, started, "no renders")
    total = sum(p.stat().st_size for p in local)
    print("  %d local mp4 (%s) -> s3://your-media-bucket" % (len(local), human_bytes(total)))
    if not confirm("  Upload to S3 (writes outside this machine)?", False):
        return record(ctx, "Upload videos to S3", SKIPPED, started, "declined")

    # Pass the source explicitly. Left to itself the script syncs whatever
    # public_html/videos resolves to, which is not necessarily the tree we just
    # counted and are about to verify — and verifying a different tree than the
    # one uploaded is worse than not verifying at all.
    rc, _lines = run_stream(["./deploy/upload-videos-s3.sh", str(ctx.out_dir)], ctx.site, "Upload",
                            parser=make_upload_parser(),
                            keep=lambda l: l.startswith(("Skipping ", "Creating bucket")))
    if rc != 0:
        return record(ctx, "Upload videos to S3", FAILED, started, "exit %d" % rc)

    # aws s3 sync returns 0 even when some objects failed. Verify by comparison.
    print(C.dim("  Verifying against the bucket listing (sync's exit code is not proof)..."))
    ok, missing, mismatched = verify_s3(ctx)
    if ok is None:
        return record(ctx, "Upload videos to S3", FAILED, started, "could not verify (no bucket listing)")
    if not ok:
        for k in missing[:10]:
            print(C.red("    missing on S3: %s" % k))
        for k in mismatched[:10]:
            print(C.red("    size mismatch: %s" % k))
        return record(ctx, "Upload videos to S3", FAILED, started,
                      "%d missing, %d size mismatch" % (len(missing), len(mismatched)))
    return record(ctx, "Upload videos to S3", RAN, started,
                  "%d mp4 verified on S3, %s" % (len(local), human_bytes(total)))


def step_deploy(ctx):
    """Push the site to EC2. SIGNED_VIDEOS=1 is not optional — see below."""
    started = time.time()
    if not ctx.site.is_dir():
        return record(ctx, "Deploy site", FAILED, started, "site repo missing")

    print(C.dim("  deploy-site.sh pulls the live curation + trips.json first (the live"))
    print(C.dim("  site is the merge base), rebuilds the manifest, then rsyncs public_html/."))
    print(C.yellow("  SIGNED_VIDEOS=1 is set for this run."))
    print(C.dim("  Since 2026-07-26 the bucket is PRIVATE. Deploying without SIGNED_VIDEOS=1"))
    print(C.dim("  writes a config.js pointing the page at raw S3 URLs, which now 403 —"))
    print(C.dim("  the site comes up and no video plays. There is no reason to deploy"))
    print(C.dim("  without it while the bucket is private, so this CLI always sets it."))
    if not confirm("  Deploy to raoulsson.com (writes outside this machine)?", False):
        return record(ctx, "Deploy site", SKIPPED, started, "declined")

    rc, _lines = run_stream(["./deploy/deploy-site.sh"], ctx.site, "Deploy",
                            env_extra={"SIGNED_VIDEOS": "1"},
                            keep=lambda l: l.startswith("=== ") or l.startswith("Done."))
    if rc != 0:
        return record(ctx, "Deploy site", FAILED, started, "exit %d" % rc)

    live = live_trip_count(ctx)
    return record(ctx, "Deploy site", RAN, started,
                  "live site reports %s trips" % (live if live is not None else "?"))


def is_complete_summary(ctx):
    """Run deploy/is-complete.py and return (safe, total, output_lines).

    We shell out rather than re-implement it: that script is the definition of
    'fully published' (on S3 with a matching size, in the live trips.json, and
    its map sidecar reachable on the site), and having two copies of that rule
    is how they drift apart.

    Its own N/M summary line is NOT usable directly, though: it walks every
    local trip, including ones the admin flagged mode=delete. Those are off S3
    and off the site deliberately, so they can never be 'complete' and would
    pin the guard shut forever after the first deletion. We re-count from its
    table instead, skipping the flagged trips.
    """
    try:
        p = subprocess.run(["python3", "deploy/is-complete.py", str(ctx.out_dir)],
                           cwd=str(ctx.site), capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, None, ["is-complete.py could not run: %s" % e]
    lines = p.stdout.splitlines()
    if p.returncode != 0:
        return None, None, lines + p.stderr.splitlines()

    # Table rows are "<trip base>   local  S3  json  site  YES|no".
    skip = deleted_ids(ctx)
    safe = total = 0
    seen_row = False
    for l in lines:
        m = re.match(r"^(trip_\S+)\s+.*\s(YES|no)\s*$", l)
        if not m:
            continue
        seen_row = True
        base = m.group(1)
        if any(i in base for i in skip):
            continue
        total += 1
        if m.group(2) == "YES":
            safe += 1
    if seen_row:
        return safe, total, lines
    # No rows parsed — fall back to the script's own summary rather than guess.
    for l in lines:
        m = re.search(r"(\d+)/(\d+) trip\(s\) fully published", l)
        if m:
            return int(m.group(1)), int(m.group(2)), lines
    return None, None, lines


def step_delete_import(ctx):
    """Erase the original footage of one import. Unrecoverable — heavily gated.

    Three independent things must hold before the prompt even appears:
      1. every renderable trip in that import has a rendered mp4 locally,
      2. every rendered mp4 is on S3 with a matching size,
      3. is-complete.py agrees every trip is fully published (S3 + live site).
    Then the word DELETE has to be typed. A y/n here is too easy to fat-finger
    for an action that destroys tens of GB of irreplaceable source footage.
    """
    started = time.time()
    root = pick_import(ctx, "deletion")
    if root is None:
        return record(ctx, "Delete import source", SKIPPED, started, "no import folder")

    # What actually gets erased. If `root` is the sink itself it may also contain
    # OTHER imports as dated subfolders (<sink>/<day>/DCIM), and those have not
    # been scanned, rendered or verified by anything below — rmtree of the sink
    # would take them with it. In that case narrow the target to this import's
    # own DCIM tree and leave the siblings alone.
    siblings = [c for c in sorted(root.iterdir())
                if c.is_dir() and (c / "DCIM").is_dir()] if root.is_dir() else []
    if siblings:
        target = root / "DCIM"
        print()
        print(C.yellow("  %s also holds %d other import(s): %s" % (
            root, len(siblings), ", ".join(c.name for c in siblings))))
        print(C.yellow("  Narrowing the delete to %s; the others are untouched." % target))
    else:
        target = root
    if not target.is_dir():
        print(C.red("  Nothing to delete at %s" % target))
        return record(ctx, "Delete import source", SKIPPED, started, "nothing at the target")

    size = tree_size(target)
    files = count_files(target)
    print()
    print(rule("delete import source"))
    print("  Target: %s" % C.bold(str(target)))
    print("  %d file(s), %s — this is the ORIGINAL footage and it is not recoverable." % (
        files, C.bold(human_bytes(size))))
    print()

    # --- guard 1: everything renderable in this import has actually been rendered.
    # Renders are namespaced by import folder name (out_dir/<import name>/<day>/),
    # so this compares like with like and ignores other imports' output.
    ns = ctx.out_dir / root.name
    ns_mp4s = rendered_mp4s(ns)
    print("  [1/3] rendered locally ... ", end="")
    sys.stdout.flush()
    if not ns_mp4s:
        print(C.red("no"))
        print(C.red("        No mp4 under %s — nothing from this import was rendered." % ns))
        return record(ctx, "Delete import source", SKIPPED, started, "refused: nothing rendered")
    scan = ctx.last_scan if (ctx.last_scan and ctx.last_scan.root == root) else None
    if scan is None:
        print(C.yellow("%d mp4, but no scan of this import in this session" % len(ns_mp4s)))
        print(C.yellow("        Cannot prove every trip was rendered. Run step 2 first."))
        return record(ctx, "Delete import source", SKIPPED, started, "refused: no scan to compare against")
    if len(ns_mp4s) < scan.renderable:
        print(C.red("no"))
        print(C.red("        Scan found %d renderable trip(s); only %d mp4 exist." % (
            scan.renderable, len(ns_mp4s))))
        return record(ctx, "Delete import source", SKIPPED, started,
                      "refused: %d/%d trips rendered" % (len(ns_mp4s), scan.renderable))
    print(C.green("yes (%d mp4 for %d renderable trip(s))" % (len(ns_mp4s), scan.renderable)))

    # --- guard 2: those mp4s are on S3, byte-size matched.
    print("  [2/3] present on S3 ..... ", end="")
    sys.stdout.flush()
    ok, missing, mismatched = verify_s3(ctx, quiet=True)
    if ok is None:
        print(C.red("unknown"))
        print(C.red("        Could not list the bucket. Refusing to delete on an unknown."))
        return record(ctx, "Delete import source", SKIPPED, started, "refused: S3 unverifiable")
    if not ok:
        print(C.red("no"))
        for k in (missing + mismatched)[:10]:
            print(C.red("        %s" % k))
        return record(ctx, "Delete import source", SKIPPED, started,
                      "refused: %d missing / %d mismatched on S3" % (len(missing), len(mismatched)))
    print(C.green("yes"))

    # --- guard 3: the site actually serves them.
    print("  [3/3] published on the site (is-complete.py) ... ", end="")
    sys.stdout.flush()
    safe, total, out_lines = is_complete_summary(ctx)
    if safe is None:
        print(C.red("unknown"))
        for l in out_lines[-15:]:
            print(C.dim("        " + l))
        return record(ctx, "Delete import source", SKIPPED, started, "refused: is-complete.py inconclusive")
    if total == 0 or safe < total:
        print(C.red("no (%s/%s)" % (safe, total)))
        for l in out_lines:
            print(C.dim("        " + l))
        return record(ctx, "Delete import source", SKIPPED, started,
                      "refused: %s/%s trips fully published" % (safe, total))
    print(C.green("yes (%d/%d)" % (safe, total)))

    print()
    print(C.red("  Deleting %s removes %s of original footage permanently." % (target, human_bytes(size))))
    print(C.dim("  The renders and their S3 copies stay; the raw clips do not come back."))
    answer = ask("  Type DELETE to erase it, anything else to cancel: ")
    if answer != "DELETE":
        print("  Cancelled.")
        return record(ctx, "Delete import source", SKIPPED, started, "cancelled at the prompt")

    try:
        shutil.rmtree(str(target))
    except OSError as e:
        print(C.red("  Delete failed: %s" % e))
        return record(ctx, "Delete import source", FAILED, started, str(e))
    if ctx.selected_import == root:
        ctx.selected_import = None
    ctx.last_scan = None
    ctx.last_groups = None
    print(C.green("  Deleted %s (%s)" % (target, human_bytes(size))))
    return record(ctx, "Delete import source", RAN, started,
                  "%d file(s), %s freed" % (files, human_bytes(size)))


# The pipeline, in order. `in_all` is False for the destructive steps: "all" and
# any range skip them, and a selection that names one alongside anything else is
# refused outright — they are only ever reachable as a selection of one.
#
# 2-4 are the deciding phase: list what is on the card, look at it, throw away
# what is not worth keeping. All of it happens before step 5, because encoding is
# hours and uploading is days — pruning after either is paying for footage twice.
STEPS = [
    (1, "Import from SD card", step_import, True),
    (2, "List trips (dry-run scan)", step_list, True),
    (3, "Preview all trips (sidecars + stills, no encoding)", step_preview, True),
    (4, "Drop trip from import (DESTRUCTIVE)", step_drop_trip, False),
    (5, "Render trips", step_render, True),
    (6, "Build manifest", step_manifest, True),
    (7, "Upload videos to S3", step_upload, True),
    (8, "Deploy site (SIGNED_VIDEOS=1)", step_deploy, True),
    (9, "Delete import source (DESTRUCTIVE)", step_delete_import, False),
]


def _compact_ranges(nums):
    """[1,2,3,5,6,7,8] -> '1-3,5-8'. The menu's 'all' hint has to be derived
    from STEPS, not written out by hand: the two drifted apart the moment a step
    was inserted, and a wrong hint here teaches the wrong selection."""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if j == i else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ",".join(out)


def solo_steps():
    """Step numbers that may only ever run alone (the destructive ones)."""
    return [n for n, _, _, in_all in STEPS if not in_all]


# Short label for the grid; the full sentence is printed when the step is picked,
# where there is room for it and where it is actually wanted. Keeping the long
# names in the menu cost eleven lines every time round the loop.
SHORT = {
    1: "Import", 2: "List trips", 3: "Preview", 4: "Drop trip", 5: "Render",
    6: "Manifest", 7: "Upload", 8: "Deploy", 9: "Del source",
}
# Steps that write nothing, send nothing AND finish in seconds run straight from
# the menu with no "Go?" — a confirmation that guards nothing is noise, and worse
# it is practice at saying yes without reading.
#
# Deliberately empty. The obvious candidate was 2 (List trips): it changes
# nothing, so it looks free. But the scan runs under .venv, which has opencv, so
# boundaries come from video ego-motion over every clip — minutes on a full card,
# not the two seconds the same command takes under a python without opencv. Both
# halves of the test have to hold, and this one fails the second.
READ_ONLY = set()
DESC = {
    1: "Copy the card's DCIM tree into the import sink, verify, then optionally erase the card.",
    2: "Scan the import and print the trip table. Reads nothing else, changes nothing.",
    3: "Sidecars, a still per trip and a local contact sheet. No encoding, no deploy.",
    4: "Delete a trip's source clips from the import so it is never rendered or uploaded.",
    5: "Encode the chosen trips. The slow step: hours for a full card.",
    6: "Rebuild trips.json from the renders, carrying forward everything already published.",
    7: "Sync the mp4s to the Zurich bucket. Slow on a home uplink; resumes if interrupted.",
    8: "Push the site to EC2 with SIGNED_VIDEOS=1, so clips load as signed CloudFront URLs.",
    9: "Erase the whole import source. Only after everything is rendered and published.",
}


def print_menu(ctx):
    """Compact grid. Columns are chosen to fit the terminal, so a narrow window
    gets fewer columns rather than a wrapped mess."""
    print()
    w = term_width()
    cell = max(len(s) for s in SHORT.values()) + 6      # "! 9) Del source" + gap
    cols = max(1, min(4, w // cell))
    rows = (len(STEPS) + cols - 1) // cols
    ordered = sorted(STEPS, key=lambda s: s[0])
    for r in range(rows):
        line = ""
        for c in range(cols):
            i = r + c * rows                            # fill down, then across
            if i >= len(ordered):
                continue
            num, _name, _fn, in_all = ordered[i]
            mark = " " if in_all else C.red("!")
            label = SHORT[num]
            txt = "%s %d) %s" % (mark, num, C.bold(label) if in_all else C.red(label))
            pad = cell - (len(label) + 5)
            line += txt + " " * max(1, pad)
        print("  " + line.rstrip())
    solo = ",".join(str(n) for n in solo_steps())
    rng = _compact_ranges([n for n, _, _, a in STEPS if a])
    # Two short lines beat one that wraps: a wrapped hint reads as broken output.
    if w < 78:
        print(C.dim("   0 status   all=%s   q quit" % rng))
        print(C.dim("   %s destructive, alone only" % solo))
    else:
        print(C.dim("   0) status    all = %s    q = quit    (%s destructive, alone only)"
                    % (rng, solo)))


def parse_selection(s):
    """'all' | '3' | '3-6' | '1,3,5' -> [step numbers]. Returns None if unparseable.

    A step marked in_all=False (the drop, the delete) can only ever be run
    ALONE. Ranges skip it, 'all' skips it, and a list that mentions it alongside
    anything else is rejected outright rather than quietly reordered — '9 8'
    would otherwise have erased the footage before the deploy that proves it was
    published, and '4 5' would have dropped a trip and then rendered from a
    grouping that no longer exists.
    """
    s = s.strip().lower()
    if s in ("a", "all"):
        return [n for n, _, _, in_all in STEPS if in_all]
    picked = []
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                return None
            for n, _, _, in_all in STEPS:
                if lo <= n <= hi and in_all and n not in picked:
                    picked.append(n)
            continue
        if part.isdigit():
            n = int(part)
            if not any(n == x[0] for x in STEPS):
                return None
            if n not in picked:
                picked.append(n)
            continue
        return None
    if not picked:
        return None
    solo = set(n for n, _, _, in_all in STEPS if not in_all)
    if solo & set(picked) and len(picked) > 1:
        return None
    return picked


def print_summary(ctx):
    if not ctx.results:
        return
    print()
    print(rule("summary"))
    for r in ctx.results:
        if r.status == RAN:
            tag = C.green("ran    ")
        elif r.status == FAILED:
            tag = C.red("FAILED ")
        else:
            tag = C.yellow("skipped")
        print("  %s  %-34s %8s   %s" % (tag, r.name, human_secs(r.seconds), C.dim(r.detail)))
    print(rule())


def run_steps(ctx, numbers):
    by_num = dict((n, (name, fn)) for n, name, fn, _ in STEPS)
    for i, n in enumerate(numbers):
        name, fn = by_num[n]
        print()
        # Short banner, not a full-width dash fill. On a narrow terminal the rule
        # was ~100 characters of noise announcing a step whose actual outcome
        # then arrived as a quiet line underneath. The outcome is the message.
        print()
        print(C.bold("== %d) %s" % (n, SHORT.get(n, name))))
        try:
            ok = fn(ctx)
        except Aborted:
            print()
            print(C.yellow("  Interrupted — step '%s' stopped." % name))
            ctx.results.append(StepResult(name, FAILED, 0, "interrupted"))
            return False
        if not ok:
            remaining = numbers[i + 1:]
            if remaining:
                # Every later step consumes an earlier one's output, so continuing
                # after a failure would deploy or upload a half-finished state.
                print(C.red("  Stopping: step %d failed and steps %s depend on it." % (
                    n, ", ".join(str(x) for x in remaining))))
                for m in remaining:
                    ctx.results.append(StepResult(by_num[m][0], SKIPPED, 0, "not reached"))
            return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Interactive driver for the dashcam publishing pipeline.",
        epilog="Runs the same scripts the READMEs document; it adds status, "
               "step selection, progress and a summary.")
    ap.add_argument("--site-repo", help="path to the goodnight-drives repo "
                                        "(default: sibling of this repo, or $GOODNIGHT_DRIVES_DIR)")
    ap.add_argument("--config", help="path to config.txt (default: this repo's)")
    ap.add_argument("--card", help="SD card mount point (default: %s)" % DEFAULT_CARD)
    ap.add_argument("--steps", help="run these steps and exit, e.g. '5-8' or 'all'. "
                                    "Still prompts for confirmations.")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live-site lookups in the status screen")
    ap.add_argument("--no-color", action="store_true", help="plain output, no ANSI")
    args = ap.parse_args(argv)

    if args.no_color:
        C.enabled = False

    ctx = Ctx(args)

    print()
    print(C.bold("  dashcam pipeline") + C.dim("   card -> render -> S3 -> site"))
    # Checked before the status screen: there is nothing useful to show if the
    # numbers behind it would come from the wrong grouping.
    if not require_ego_motion(ctx):
        return 3
    print_status(ctx)

    exit_code = 0
    try:
        if args.steps:
            picked = parse_selection(args.steps)
            if not picked:
                print(C.red("Could not parse --steps %r" % args.steps))
                return 2
            run_steps(ctx, picked)
        else:
            while True:
                print_menu(ctx)
                sel = ask("  Steps to run: ")
                if sel.lower() in ("q", "quit", "exit"):
                    break
                if sel.lower() in ("s", "status", "0"):
                    print_status(ctx)
                    continue
                picked = parse_selection(sel)
                if not picked:
                    print(C.red("  Did not understand %r." % sel))
                    named = [n for n in solo_steps() if str(n) in re.split(r"[,\s-]+", sel)]
                    if named:
                        print(C.red("  Step%s %s destroy%s footage — each runs alone, never "
                                    "in a batch." % ("" if len(named) == 1 else "s",
                                                     ", ".join(str(n) for n in named),
                                                     "s" if len(named) == 1 else "")))
                    continue
                # The description now lives here rather than in the grid — this
                # is the moment it is wanted, and there is room for a sentence.
                for n in picked:
                    print("  %s %s" % (C.bold("%d)" % n), SHORT[n]))
                    print(C.dim("     " + DESC[n]))
                # Only ask before something that writes, sends or takes a while.
                # Confirming a read-only scan just trains you to hit enter, which
                # is exactly the habit you do not want by the time step 9 asks.
                if not all(n in READ_ONLY for n in picked):
                    if not confirm("  Go?", True):
                        continue
                run_steps(ctx, picked)
    except (KeyboardInterrupt, Aborted):
        print()
        print(C.yellow("  Interrupted."))
    finally:
        show_cursor()
        print_summary(ctx)

    if any(r.status == FAILED for r in ctx.results):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        show_cursor()


# ---------------------------------------------------------------------------
# Notes on things that surprised me while wiring this up — kept here rather than
# in a doc, because they are the reasons the code above is shaped the way it is.
#
# * Two import layouts coexist. import-sd-card.sh writes to
#   <sink>/<YYYY-MM-DD>/DCIM, but config.txt's `root` currently points at the
#   sink itself, which today holds a DCIM tree directly. Neither is wrong; they
#   are just different vintages. Rather than pick one, import_candidates()
#   accepts both and every render/scan/delete passes an explicit --root, so the
#   CLI never depends on which layout is in place.
#
# * `aws s3 sync` exits 0 even when individual objects fail to upload. The
#   upload step therefore ignores the exit code as evidence and re-lists the
#   bucket, comparing keys and sizes against the local mp4s. Trips the admin
#   flagged mode=delete are excluded from that comparison, because
#   upload-videos-s3.sh deliberately does not upload them.
#
# * Progress can only be derived where the tool emits it: rsync's --info=progress2
#   percentage (import), the renderer's [Trip a/b] + [clip/N] lines (render), and
#   aws's "Completed X/Y" lines (upload). build_manifest.py and deploy-site.sh
#   print nothing countable, so those show a spinner. rsync and aws draw with
#   carriage returns, which is why the reader splits on \r as well as \n.
#
# * The renderer's "[Trip a/b]" a is the per-DAY publish number, so it repeats
#   across days within one run. Only b is usable; the counter is our own.
#
# * is-complete.py's own "N/M fully published" line cannot gate the delete. It
#   walks every local trip including ones the admin flagged mode=delete, which
#   are off S3 and off the site on purpose and so can never be complete — the
#   guard would jam shut permanently after the first admin deletion. We re-count
#   from its table with those rows skipped.
#
# * The sink can hold several imports side by side (<sink>/<day>/DCIM). Deleting
#   the sink itself would take the ones nothing has verified, so when siblings
#   exist the delete narrows to this import's own DCIM tree.
#
# * Which source clips belong to a trip is NOT knowable from filenames. The
#   boundaries come from video ego-motion (the real pull-away and park) with GPS
#   only gating which clips get checked, so two adjacent clips a minute apart can
#   sit either side of a boundary for reasons no timestamp shows. That is why the
#   drop step reads make_dashcam_videos --print-groups instead of inferring: the
#   scanner serialises the very grouping it would render, and one clip of error
#   at a boundary is destroyed original footage.
#
# * --print-groups writes JSON on stdout and everything human-readable on stderr,
#   so run_stream grew a stdout_file argument: the child's stdout goes to a file
#   and its stderr is what gets streamed and turned into progress. Merging the
#   two (what every other step wants) would corrupt the JSON.
#
# * The grouping scan is expensive, so it is cached per import folder in
#   ctx.last_groups — and cleared by anything that changes the clips on disk (an
#   import merges new files in, a drop removes some, a delete removes all).
#
# * The preview contact sheet lives in <out>/previews/ and deploy-site.sh excludes
#   that directory. It sits inside the tree the `videos` symlink points at (it has
#   to, for its relative links to the .html/.gpx sidecars to resolve from file://),
#   and without the exclude a deploy would publish stills of footage he may be
#   about to drop.
# ---------------------------------------------------------------------------
