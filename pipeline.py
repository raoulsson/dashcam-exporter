#!/usr/bin/env python3
"""pipeline.py — the whole dashcam publishing pipeline, in one interactive CLI.

Card -> import -> render -> manifest -> S3 -> website -> (optionally) erase the
import source. Each of those already has a script; the point of this file is
that nobody should have to remember which script, in which repo, with which
flag. Run it, look at the status screen, pick the steps.

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
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
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
               env_extra=None, tail_lines=40):
    """Run a command, stream its output, return (rc, all_lines).

    parser(line) -> (fraction, note) or None. fraction is 0..1 for a real
    progress bar; return None from the parser (or pass none at all) and the
    display falls back to an elapsed-time spinner. We never synthesise a
    percentage from a guess.

    keep(line) -> bool marks lines worth leaving permanently on screen.
    passthrough=True prints everything verbatim and draws no bar — used for
    the trip listing, where the table IS the output.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env,
        # Own process group: Ctrl-C reaches us first and we decide how the child
        # dies, instead of the terminal SIGINT-ing both and racing us to it.
        # Side effect worth knowing: a new session has no controlling terminal,
        # so anything that insists on prompting at /dev/tty (an ssh host-key
        # confirmation on a first-ever deploy, a passphrase-protected key) fails
        # loudly instead of hanging. Loud is the right failure mode here; accept
        # the host key once by hand and the deploy step works from then on.
        start_new_session=True,
    )
    q = queue.Queue()
    t = threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True)
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
        print("  " + C.red("goodnight-drives repo not found — steps 4-6 will not run."))
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
        print(C.yellow("  No DCIM on %s — is the card mounted?" % ctx.card))
        return record(ctx, "Import from SD card", SKIPPED, started, "card not mounted")

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
    print(C.green("  Deleted %s (%s)" % (target, human_bytes(size))))
    return record(ctx, "Delete import source", RAN, started,
                  "%d file(s), %s freed" % (files, human_bytes(size)))


# The pipeline, in order. `in_all` is False for the destructive step: "all" and
# any range skip it, and a selection that names it alongside anything else is
# refused outright — it is only ever reachable as a selection of one.
STEPS = [
    (1, "Import from SD card", step_import, True),
    (2, "List trips (dry-run scan)", step_list, True),
    (3, "Render trips", step_render, True),
    (4, "Build manifest", step_manifest, True),
    (5, "Upload videos to S3", step_upload, True),
    (6, "Deploy site (SIGNED_VIDEOS=1)", step_deploy, True),
    (7, "Delete import source (DESTRUCTIVE)", step_delete_import, False),
]


def print_menu(ctx):
    print()
    print(rule("steps"))
    for num, name, _fn, in_all in STEPS:
        mark = " " if in_all else C.red("!")
        print("  %s %d) %s" % (mark, num, C.bold(name) if in_all else C.red(name)))
    print(rule())
    print(C.dim("  all = 1-6   |   ranges: 3-6   |   list: 1,3,5   |   s = status   |   q = quit"))
    print(C.dim("  7 is excluded from 'all' and ranges, and is refused in any batch —"))
    print(C.dim("  it runs only as a selection of one."))


def parse_selection(s):
    """'all' | '3' | '3-6' | '1,3,5' -> [step numbers]. Returns None if unparseable.

    A step marked in_all=False (the delete) can only ever be run ALONE. Ranges
    skip it, 'all' skips it, and a list that mentions it alongside anything else
    is rejected outright rather than quietly reordered — '7 6' would otherwise
    have erased the footage before the deploy that proves it was published.
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
        print(rule("step %d: %s" % (n, name)))
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
    ap.add_argument("--steps", help="run these steps and exit, e.g. '4-6' or 'all'. "
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
                if sel.lower() in ("s", "status"):
                    print_status(ctx)
                    continue
                picked = parse_selection(sel)
                if not picked:
                    print(C.red("  Did not understand %r." % sel))
                    if any(str(n) in re.split(r"[,\s-]+", sel)
                           for n, _, _, in_all in STEPS if not in_all):
                        print(C.red("  Step 7 destroys footage — it runs alone, never in a batch."))
                    continue
                print("  Will run: %s" % ", ".join(
                    "%d %s" % (n, dict((x[0], x[1]) for x in STEPS)[n]) for n in picked))
                if not confirm("  Go?", True):
                    continue
                run_steps(ctx, picked)
                print_status(ctx)
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
# ---------------------------------------------------------------------------
