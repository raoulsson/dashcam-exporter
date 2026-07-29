#!/usr/bin/env python3
"""pipeline.py — the whole dashcam publishing pipeline, in one interactive CLI.

Card -> import -> sidecars -> preview -> render -> local page -> bucket and
site -> clear the workspace, and separately free the card. Each of those
already has a script; the point of this file is that nobody should have to
remember which script, in which repo, with which flag. Run it, look at the
status screen, pick an item.

Build Preview and Exclude Trip in the middle are the cheap decision point:
sidecars and one still per trip cost minutes, while encoding costs hours and
uploading costs days on a 250 KB/s line. Deciding what to keep afterwards means
paying for footage that was never wanted.

The menu itself is a state machine and lives elsewhere: menu.py holds the
interface and the graph, items.py the ten items, guards.py every predicate that
stands between this tool and lost footage, world.py the snapshot they judge.
What is left here is the machinery — the functions that DO the work, the one
place that goes and looks at the disk (capture_world), and the painter, which
derives everything it draws from the items and the position.

    python3 pipeline.py

Standard library only, Python 3.9+ (the system /usr/bin/python3 on this Mac).
It never re-implements the underlying tools — it shells out to exactly the same
entry points the READMEs document, streams their output, and turns what it can
parse into a progress bar. Where real progress cannot be derived it shows an
elapsed-time spinner rather than inventing a percentage.

This repo does import, render and a local site on its own. Publishing — a bucket
and a deployed website — lives in a second repo and is entirely optional: set
`site_repo`, `s3_bucket` and `live_trips_url` in config.txt and Upload Website
lights up; leave them unset (what a fresh clone gets) and it stays greyed out
with the key that would enable it printed underneath. Nothing here contacts a
network host that has not been named in the config.
"""
from __future__ import annotations

import base64
import functools
import html
import itertools
import json
import math
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
import urllib.parse
import urllib.request
from pathlib import Path

# The state machine. Ordering is the graph's job, evidence is the guards',
# and the world is the one snapshot both judge. This module keeps the
# machinery — the functions that DO the work — and asks the items everything
# else.
import guards
import items
import menu
import world as W
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)

# The item titles, read from the one place they are declared. A sentence with
# a literal title in it is a second place, and second places go stale silently.
NAME = items.NAMES

# ---------------------------------------------------------------------------
# Defaults — kept identical to the scripts we drive, so this CLI can never
# disagree with what those scripts would do on their own.
# ---------------------------------------------------------------------------

# Last-resort fallbacks, used only when config.txt says nothing. They are not
# settings — every one of these is overridable in config.txt, and the code reads
# it from there. A compiled-in path that a second checkout inherits silently is
# how ~/dashcam-data/output ended up being swept by a clone that thought it was
# working on its own data, so anything naming a location belongs in the file the
# person edits, not in the file they clone.
FALLBACK_CARD = "/Volumes/NO NAME"                # make_dashcam_videos.DEFAULT_ROOT
FALLBACK_OUT = "~/dashcam-data/output"            # make_dashcam_videos.FALLBACK_OUT
FALLBACK_IMPORT_ROOT = "~/dashcam-data/import"    # import-sd-card.sh DEST_ROOT
# There is deliberately no default for the site repo, the bucket or the live
# manifest URL. A default would mean a clone reaching for someone else's
# checkout on disk and someone else's host on the network, on every launch.

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


def tilde(p):
    """~/dashcam-data/... instead of /Users/<you>/dashcam-data/... — on a narrow
    terminal the home prefix is a third of the line and says nothing."""
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


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


# The publishing settings point at a specific person's bucket, website and
# checkout. config.txt is tracked, so putting real values there commits them —
# which is exactly what happened, and why they now resolve from the gitignored
# .env first. Same rule the home coordinates already followed: config.txt may
# carry a commented EXAMPLE, the real value lives in .env or not at all.
PRIVATE_KEYS = ("site_repo", "s3_bucket", "s3_region", "live_trips_url",
                "home_lat", "home_lon")


def as_bool(v, default=False):
    """A config value read as a flag. Absent or empty means the default."""
    s = (v or "").strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def load_env(path):
    """KEY=value from a .env file. Same forgiving parse as load_config."""
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Ctx:
    """Everything the steps need: resolved paths, config, and session state."""

    def __init__(self):
        self.exporter = EXPORTER_DIR
        self.config_path = self.exporter / "config.txt"
        self.cfg = load_config(self.config_path)
        # .env overlays config.txt for the private keys, and a real environment
        # variable beats both — so a one-off run can point somewhere else without
        # editing a file.
        env = load_env(self.exporter / ".env")
        for key in PRIVATE_KEYS:
            name = "SET_" + key.upper()
            val = os.environ.get(name) or env.get(name)
            if val:
                self.cfg[key] = val

        # --- the optional, personal half. All three are unset by default.
        #
        # The site repo holds the publishing scripts (build_manifest.py,
        # deploy/upload-videos-s3.sh, deploy/deploy-site.sh). Unset means this
        # CLI is import -> render -> local site and nothing else; the steps that
        # need it are greyed out rather than removed, so the numbering is the
        # same for everyone and the greyed line says which key turns them on.
        # A flag or an env var wins over the file: both exist to point one run
        # somewhere other than the configured place.
        site = (os.environ.get("GOODNIGHT_DRIVES_DIR")
                or self.cfg_opt("site_repo"))
        self.site = Path(site).expanduser().resolve() if site else None

        # The bucket the Upload step verifies against once the sync has run.
        # aws s3 sync exits 0 even when objects fail, so this name is what makes
        # "uploaded" provable; without it Upload cannot be trusted and stays off.
        # The region is only needed when the credentials' default is a different
        # one — an eu-central-1 bucket listed with a us-east-1 default fails.
        self.s3_bucket = self.cfg_opt("s3_bucket")
        self.s3_region = self.cfg_opt("s3_region")

        # The deployed site's manifest, read for one line of the status screen.
        # Unset means no request is made at all — not a request that fails. A
        # clone must not phone a host its owner has never heard of.
        self.live_trips_url = self.cfg_opt("live_trips_url")

        # The workspace holding the footage to work on. `root` is the old name
        # and still read, because configs carrying it exist; import_dir wins.
        # Its fallback is the workspace, NOT the card — the card is `card`, and
        # a root that defaulted to a mount point is what made "where does this
        # render from" have two plausible answers.
        self.render_root = Path(self.cfg.get("import_dir")
                                or self.cfg.get("root")
                                or FALLBACK_IMPORT_ROOT).expanduser()
        # `out` defaults NEXT TO the workspace, not to a global constant.
        # A second checkout that sets import_dir and leaves `out` alone otherwise
        # inherits ~/dashcam-data/output — the first checkout's live working area
        # — and Clean Workspace erases that. A clone set up to be
        # independent has to actually be independent, and the setting the person
        # did supply is the best evidence of where their data lives.
        if self.cfg.get("out"):
            self.out_dir = Path(self.cfg["out"]).expanduser()
        else:
            self.out_dir = self.render_root.parent / "output"
        # Where import-sd-card.sh drops the card. It follows config's `root`,
        # because that is what every render, scan and delete is pointed at — when
        # the two diverged (renaming `root` while the script kept its own
        # default) the copy landed in a folder no later step ever looked in, and
        # nothing said so. DASHCAM_IMPORT_ROOT still wins for a one-off.
        self.import_root = Path(os.environ.get("DASHCAM_IMPORT_ROOT")
                                or self.render_root).expanduser()
        self.card = Path(self.cfg.get("card", FALLBACK_CARD)).expanduser()

        try:
            self.output_height = int(self.cfg.get("output_height", "1080"))
        except ValueError:
            self.output_height = 1080

        self.offline = as_bool(self.cfg.get("offline"), False)
        # A non-default --config must reach the renderer too, or this CLI would
        # compute its paths from one config while the wrappers read another.
        self.config_args = []
        # Trip boundaries are the slowest part of a scan (about two and a half
        # minutes on a full card) and identical for the same inputs, so the cache
        # persists across runs. It is safe to do that because the key covers
        # everything the grouping is derived from — every clip, every .gpx by
        # size and mtime, every grouping option — so it cannot outlive its data:
        # change any of them and it misses and recomputes. Living beside the
        # renders rather than in /tmp keeps it with the card it describes.
        self.scan_cache = self.out_dir / ".scan_cache.json"
        self.scan_args = ["--scan-cache", str(self.scan_cache)]

        # Session state carried between steps.
        self.selected_import = None     # the folder passed as --root to the renderer
        self.last_scan = None           # ScanResult from the most recent scan
        # (root, payload) from the most recent --print-groups. The scan behind it
        # is expensive, and both the preview sheet and Exclude Trip need the same
        # answer, so it is cached per import folder — and invalidated the moment
        # anything changes what is on disk.
        self.last_groups = None
        self.results = []               # StepResult log for the final summary
        # Colouring the route by speed is on by default because it says something
        # the shape alone does not — where you were held up and where you were
        # moving. Someone who wants the shape plain can say so.
        # Where the finished folders land. Default beside the renders; set it to
        # an external disk or a Dropbox folder and the result arrives there
        # directly instead of being moved by hand afterwards.
        fr = self.cfg_opt("final_dir")
        # Beside the output dir, not inside it: the output dir is the working
        # area that gets emptied, and a finished folder is the one thing that
        # must survive that.
        self.final_root = Path(fr).expanduser() if fr else self.out_dir.parent
        self.speed_colour = as_bool(self.cfg.get("speed_colour"), True)
        # Still-frame knobs. Compiled-in numbers are the fallback, config wins.
        self.still_width = self.cfg_int("still_width", PREVIEW_STILL_W)
        self.still_seconds = self.cfg_float("still_seconds", PREVIEW_STILL_T)
        self.site_still_seconds = self.cfg_float("site_still_seconds", 2.0)

    def cfg_int(self, key, default):
        """An integer setting, falling back rather than crashing on nonsense."""
        try:
            return int(self.cfg[key])
        except (KeyError, ValueError, TypeError):
            return default

    def cfg_float(self, key, default):
        try:
            return float(self.cfg[key])
        except (KeyError, ValueError, TypeError):
            return default

    def cfg_opt(self, key):
        """A configured value, or None when the setting is absent.

        Empty counts as absent: `s3_bucket =` with nothing after it is someone
        clearing the setting, and the alternative — an empty string that still
        gets used — produces `s3://` and a listing of the whole account.
        """
        v = (self.cfg.get(key) or "").strip()
        return v or None

    @property
    def site_ready(self):
        """Configured AND actually on disk. Two different failures, and the menu
        distinguishes them: 'needs site_repo in config.txt' is a setup step,
        'site_repo not found' is a wrong path."""
        return self.site is not None and self.site.is_dir()

    def site_script(self, *parts):
        """Path to a script inside the site repo, or None if it is not there.

        The site repo is whatever the user pointed at, so its scripts are a
        claim rather than a guarantee — checking is what lets the menu say which
        one is missing instead of failing halfway through a step.
        """
        if not self.site_ready:
            return None
        p = self.site.joinpath(*parts)
        return p if p.is_file() else None


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
            # here; accept the host key once by hand and the deploy works
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
    counts = ""
    spin = 0
    done = False
    rc = None          # the finally block reads this; an abort can reach it
                       # before proc.wait() ever assigns it

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
            # The note already carries the counter, so a tail that starts with
            # "[scan  17/ 239]" spends its width repeating it. Strip the bracket
            # and show what it identifies — the file being worked on.
            if note:
                # Drop whatever the note already says. Two shapes do this:
                # "[scan  17/ 239] NAME" and aws's "Completed 6.0 MiB/13.0 GiB
                # (457.4 KiB/s) with 6 file(s) remaining" — in both, the head of
                # the line is the counter we have already extracted, and the
                # useful remainder (the filename, or the rate and files left)
                # was being pushed off the right edge by it.
                t = re.sub(r"^\[[^\]]*\]\s*", "", t)
                t = re.sub(r"^Completed\s+[\d.]+\s*\w+\s*/\s*~?[\d.]+\s*\w+\s*", "", t)
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
                    # A parser can ask for the tail to be dropped by ending its
                    # note with \0. The child prints nothing during a silent
                    # phase, so the last line it DID print would otherwise sit
                    # there looking like the file being worked on right now.
                    if note.endswith("\0"):
                        note = note[:-1]
                        last_raw = ""
                    # Keep the last real counter seen. The note at the END of a
                    # run is often a phase description ("finding drive
                    # boundaries"), which is the wrong thing to close on — the
                    # count is what says how much was done.
                    mc = re.search(r"\d+\s*/\s*\d+", note or "")
                    if mc:
                        counts = mc.group(0).replace(" ", "")
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
        # A finished step should leave a line behind saying so. live.close()
        # erases the live area, so without this the progress simply vanishes and
        # the screen gives no evidence the work happened or how much of it.
        if rc == 0 and live.enabled:
            el = human_secs(time.time() - started)
            bar = "#" * 24
            tail_bits = " ".join(x for x in (counts,) if x)
            live.draw([C.green("%s [%s] 100%% %s  %s  completed"
                               % (label, bar, el, tail_bits)).rstrip()])
            print()          # commit that line; the next erase starts below it
            live.height = 0
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
# The scanner announces each clip as it reads it: "[scan   17/ 239] NAME.mp4".
# That loop is the long silent stretch of a scan, so it is the only thing that
# can honestly drive a bar there.
RE_SCAN = re.compile(r"^\[scan\s+(\d+)/(\d+)\]")
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


def make_scan_parser():
    """Two phases, reported honestly rather than as one bar.

    Reading the clips is countable ("[scan i/n]") and takes most of the wall
    clock on a big card. The per-trip work that follows is countable too
    ("[Trip a/b]"), but it is a different unit — so once trips start arriving we
    switch to counting those instead of leaving the bar pinned at 100% while the
    slowest part of the run is still going.
    """
    state = {"i": 0, "n": 0, "trips": 0, "trips_total": 0}

    def parse(line):
        m = RE_SCAN.match(line)
        if m and not state["trips"]:
            state["i"], state["n"] = int(m.group(1)), int(m.group(2))
            if state["n"] and state["i"] >= state["n"]:
                # Reading is done, but the run is not: what follows is the
                # ego-motion pass that finds each drive-away and park, and it
                # prints nothing until the table appears. Leaving the bar at
                # 100% there says "finished" during the slowest part of the
                # step. Hand back to the elapsed spinner instead — no number is
                # better than a wrong one.
                # Keep the n/n in the note: it is what the completion line
                # closes on, and dropping it here leaves that line reporting
                # the second-to-last clip.
                return (None,
                        # trailing \0: clear the tail. Nothing prints during the
                        # ego-motion pass, so the last clip read would freeze on
                        # screen for minutes as if it were being processed.
                        "%d/%d read, finding drive boundaries\0" % (state["n"], state["n"]))
            return ((state["i"] / state["n"]) if state["n"] else None,
                    "reading %d/%d" % (state["i"], state["n"]))
        m = RE_TRIP.match(line)
        if m:
            state["trips"] += 1
            state["trips_total"] = int(m.group(2))
            return ((state["trips"] - 1) / state["trips_total"] if state["trips_total"] else None,
                    "trip %d/%d" % (state["trips"], state["trips_total"]))
        return None

    return parse


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
    # It matches the renderer's trip_ naming rather than every .mp4 for the same
    # reason: anything else in the working area is somebody's file, not this
    # tool's output, and must not move a published/not-published decision in
    # either direction. Four source clips parked in there read as "4 renders not
    # on S3"; six named right would read as a finished round. The sweep still
    # takes everything — this is only about what counts as EVIDENCE.
    return sorted(p for p in out_dir.rglob("trip_*.mp4")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(out_dir).parts))


def live_trip_count(ctx):
    """Trips the deployed site is currently serving, or None.

    Returns None WITHOUT touching the network when live_trips_url is unset —
    that is the whole point of the setting. This runs on every launch, so a
    hardcoded URL here would mean every clone of this repo pinging one person's
    host every time anyone opened the menu.
    """
    if ctx.offline or not ctx.live_trips_url:
        return None
    try:
        with urllib.request.urlopen(ctx.live_trips_url, timeout=6) as r:
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

    # The footage source: the mounted card, or any folder holding a DCIM tree.
    card_dcim = ctx.card / "DCIM"
    if card_dcim.is_dir():
        n = clip_count(ctx.card)
        print("  Source       %s  %s" % (
            C.green("present"),
            C.dim("%s  (%s clips)" % (tilde(ctx.card), n if n is not None else "?"))))
    else:
        print("  Source       %s  %s" % (C.dim("not found"), C.dim(tilde(ctx.card))))

    # Import sink
    cands = import_candidates(ctx)
    if cands:
        for p in cands:
            n = clip_count(p)
            print("  Import       %s  %s" % (
                C.bold(tilde(p)),
                C.dim("%s clips, %s" % (n if n is not None else "?", human_bytes(tree_size(p))))))
    else:
        # Name the folder the config actually points at — import_root is only a
        # fallback, and showing it sends you to create the wrong directory.
        print("  Import       %s  %s" % (C.dim("empty"),
                                         C.dim(tilde(ctx.render_root if ctx.render_root
                                                     else ctx.import_root))))

    # Renders
    mp4s = rendered_mp4s(ctx.out_dir)
    size = sum(p.stat().st_size for p in mp4s) if mp4s else 0
    print("  Rendered     %s  %s" % (
        C.bold("%d mp4" % len(mp4s)) if mp4s else C.yellow("none"),
        C.dim("%s in %s" % (human_bytes(size), tilde(ctx.out_dir)))))

    # Local site — always meaningful, because Site needs nothing but this machine.
    # The page lives in the newest final_* folder once one exists.
    froot = getattr(ctx, "final_root", ctx.out_dir)
    finals = sorted(froot.glob(FINAL_PREFIX + "*")) if froot.is_dir() else []
    site_index = (finals[-1] / RESULT_FILE) if finals else (ctx.out_dir / RESULT_FILE)
    if site_index.is_file():
        age = human_age(time.time() - site_index.stat().st_mtime)
        print("  Local site   %s  %s" % (
            C.bold(tilde(site_index)),
            C.dim("built %s" % age if age == "just now" else "built %s ago" % age)))
    else:
        print("  Local site   %s  %s" % (C.yellow("not built"), C.dim(tilde(site_index))))

    # Everything below is the publishing half, and each row appears only when
    # the thing behind it is configured. A "Live site: unknown" row on a machine
    # that has no live site is not status, it is a permanent question mark — and
    # a row naming a repo the reader has never heard of is worse.
    if ctx.site is not None:
        manifest = ctx.site / "public_html" / "trips.json"
        if manifest.is_file():
            try:
                local_trips = json.loads(manifest.read_text()).get("trip_count", "?")
            except Exception:
                local_trips = "?"
            age = human_age(time.time() - manifest.stat().st_mtime)
            print("  Prepared     %s  %s" % (C.bold("%s trips" % local_trips),
                                             # "just now" already reads as a time;
                                             # appending "ago" gives "just now ago"
                                             C.dim("updated %s" % age if age == "just now"
                                                   else "updated %s ago" % age)))
        else:
            print("  Prepared     %s  %s" % (C.yellow("none yet"), C.dim(tilde(manifest))))

    if ctx.live_trips_url:
        live = live_trip_count(ctx)
        if live is None:
            print("  Live site    %s" % C.dim("unknown (offline or unreachable)"))
        else:
            print("  Live site    %s" % C.bold("%s trips" % live))

    if ctx.site is not None:
        print("  Repos        %s" % C.dim("%s | %s" % (tilde(ctx.exporter), tilde(ctx.site))))
    else:
        print("  Repo         %s" % C.dim(tilde(ctx.exporter)))
    print(rule())

    # Disk goes below the rule, as a footnote rather than a status row.
    # Printed once per directory it would read as two disks with suspiciously
    # identical numbers — import and output are normally the same volume.
    # Collapse when they are, and only say anything loud when the free space is
    # actually worth worrying about next to what is waiting to render.
    try:
        seen = {}
        for path in (ctx.out_dir, ctx.import_root):
            p = path if path.exists() else path.parent
            u = shutil.disk_usage(str(p))
            seen.setdefault((u.total, u.free), []).append(p)
        parts = []
        for (total, free), paths in seen.items():
            where = "" if len(seen) == 1 else " (%s)" % paths[0]
            parts.append("%s free of %s%s" % (human_bytes(free), human_bytes(total), where))
        line = "  disk: " + "   ".join(parts)
        # 15 GB is roughly a full card's renders; below that, say so plainly.
        low = any(free < 15 * 1024 ** 3 for (total, free) in seen)
        print(C.red(line + "  — low") if low else C.dim(line))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _readline_safe(s):
    """Mark ANSI sequences as zero-width for readline.

    input() goes through readline, which counts the prompt to know where the
    typed text starts. Raw escape codes are counted as printable, so a bolded
    prompt makes it believe the cursor is further right than it is — the typing
    lands in the wrong column and editing the line smears it. \\001 .. \\002
    brackets tell readline "this part occupies no space".
    """
    return re.sub(r"(\x1b\[[0-9;]*m)", "\001\\1\002", s)


# Printed once per step, above its first prompt. Ctrl-C is the way out of a
# prompt sequence — Render alone asks four questions — and it is only obvious if
# you already know it. Once per step, not once per prompt: repeating it four
# times is the kind of noise that stops being read.
_HINTED = [True]     # True at the menu, so the hint never appears there


def hint_reset():
    _HINTED[0] = False


def ask(prompt, default="", quits=True):
    """quits: a bare q answers "take me back to the menu".

    Ctrl-C already did this, but only if you knew — and inside a step every
    prompt looks like it wants a value, so q was being read as one (as an index,
    as a height). It raises the same Aborted that Ctrl-C does, which the runner
    catches per item, so the item stops and the menu comes back. Off at the menu
    itself, where q is handled as a real answer.
    """
    if not _HINTED[0]:
        _HINTED[0] = True
        print(C.dim("  (q or ctrl-c to go back)"))
    # Ctrl-C at a prompt has to mean the same thing as Ctrl-C during a child
    # process: abort the step and let it be recorded, not slip out at exit 0.
    try:
        s = input(_readline_safe(C.bold(prompt))).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Aborted()
    if quits and s.lower() in ("q", "quit"):
        raise Aborted()
    return s or default


def confirm(prompt, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    s = ask(prompt + suffix).lower()      # q here aborts the step, as elsewhere
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

# Four outcomes, not three. SATISFIED is the one the item interface needs and
# the old convention could not express: the postcondition already holds, so
# nothing was done AND nothing is owed. Re-running the sidecar pass on complete
# sidecars is that, and it is not the same answer as "you cancelled" — one
# advances the pipeline and the other leaves it where it was.
RAN, SATISFIED, SKIPPED, FAILED = "ran", "satisfied", "skipped", "failed"

# What counts as having done the step, for MenuItem.completed().
COMPLETING = (RAN, SATISFIED)


class StepResult:
    def __init__(self, name, status, seconds, detail=""):
        self.name, self.status, self.seconds, self.detail = name, status, seconds, detail


def record(ctx, name, status, started, detail=""):
    """Log one outcome and return it.

    It used to return `status != FAILED`, which made "the user typed anything
    but DROP" indistinguishable from "the trip was removed" — both True. The
    three-valued answer was computed and thrown away at every return statement
    in the file; now it is the return value, and the Work facade turns it into
    the item's Outcome.
    """
    result = StepResult(name, status, time.time() - started, detail)
    ctx.results.append(result)
    return result


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

LEDGER_FILE = ".imported.json"
DEPLOYED_FILE = ".deployed.json"


def read_ledger(ctx):
    try:
        return json.loads((ctx.out_dir / LEDGER_FILE).read_text())
    except Exception:
        return {}


def write_ledger(ctx, stamp, note=""):
    """The one thing that must outlive a cleanup.

    Everything else in the output tree is either published elsewhere or
    regenerable, but "what have I already imported" cannot be recovered from
    anything once the renders and their _meta.json are gone. Two lines of JSON at
    the root, deliberately outside the folders the cleanup empties.
    """
    if not stamp:
        return
    d = read_ledger(ctx)
    if stamp <= (d.get("through") or ""):
        return
    d["through"] = stamp
    d.setdefault("history", []).append(
        {"through": stamp, "at": time.strftime("%Y-%m-%d %H:%M"), "note": note})
    d["history"] = d["history"][-20:]
    try:
        (ctx.out_dir / LEDGER_FILE).write_text(json.dumps(d, indent=1))
    except OSError:
        pass


STAMP_RE = re.compile(r"(\d{14})")


def last_imported_stamp(ctx):
    """The newest source clip this machine has already taken in, or None.

    Read from what survives deleting the import: every rendered trip's _meta.json
    records the wall clock it ended, and the boundary cache records the source
    filenames it grouped. Both outlive the footage, which is the point — the
    question "have I already copied this card" has to be answerable after the
    card's local copy is long gone.

    Returns the DDPAI stamp form (YYYYMMDDHHMMSS) because that is what the clip
    filenames carry, so the comparison is a string compare on the name itself.
    """
    best = read_ledger(ctx).get("through") or ""
    cache = ctx.out_dir / ".scan_cache.json"
    if cache.is_file():
        try:
            d = json.loads(cache.read_text())
            for g in d.get("groups", []):
                for f in g:
                    m = STAMP_RE.search(Path(f).name)
                    if m and m.group(1) > best:
                        best = m.group(1)
        except Exception:
            pass
    if ctx.out_dir.is_dir():
        for meta in ctx.out_dir.rglob("*_meta.json"):
            try:
                end = str(json.loads(meta.read_text()).get("end") or "")
            except Exception:
                continue
            digits = re.sub(r"\D", "", end)[:14]
            if len(digits) == 14 and digits > best:
                best = digits
    return best or None


EXCLUDED_FILE = ".excluded.json"

# In-memory view of the workspace's excluded stamps, for the callers that have
# no ctx (card_split's signature is (card, after)). It is a CACHE of the file,
# not the record: excluded_stamps(ctx) refreshes it from disk, and every path
# that acts on card_split's answer refreshes first. One process, one workspace
# — the file is what survives a restart.
_EXCLUDED = set()


def excluded_stamps(ctx):
    """The stamps of clips deliberately excluded, refreshed from disk.

    An excluded clip is treated as if imported ever after: the delta import
    does not re-copy it, and the clean-up counts it as accounted for. The warning
    happened once, at exclude time — that was the decision, and re-warning at
    every later step would be asking the same question again.
    """
    global _EXCLUDED
    try:
        d = json.loads((ctx.out_dir / EXCLUDED_FILE).read_text())
        _EXCLUDED = {str(s) for s in d.get("stamps", [])}
    except Exception:
        _EXCLUDED = set()
    return set(_EXCLUDED)


def record_excluded_stamps(ctx, stamps):
    """Persist clip stamps whose footage was deliberately dropped."""
    global _EXCLUDED
    merged = excluded_stamps(ctx) | {str(s) for s in stamps}
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / EXCLUDED_FILE).write_text(
            json.dumps({"stamps": sorted(merged)}, indent=1))
    except OSError:
        pass
    _EXCLUDED = merged
    return merged


def card_split(card, after):
    """(new, already) counts of front clips on the card against a stamp.

    An excluded clip counts as already-imported regardless of the mark: its
    footage was dropped on purpose, and 'new' here means 'would be copied by
    the next import', which an excluded clip must never be.
    """
    front = card / "DCIM" / "200video" / "front"
    if not front.is_dir():
        return 0, 0
    new = old = 0
    for f in front.glob("*.mp4"):
        m = STAMP_RE.search(f.name)
        if m and ((after and m.group(1) <= after) or m.group(1) in _EXCLUDED):
            old += 1
        else:
            new += 1
    return new, old


def listed_trips(ctx):
    """The trips as a listing should show them, read from the sidecar metas.

    A trip whose span covers an excluded clip is ABSENT, not flagged: exclusion
    is 'this never happened as far as the pipeline is concerned', and a row
    saying 'excluded' would keep asking the reader to re-decide something that
    was decided when the footage was dropped.
    """
    ex = excluded_stamps(ctx)
    out = []
    if not ctx.out_dir.is_dir():
        return out
    for meta in sorted(ctx.out_dir.rglob("trip_*_meta.json")):
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        start = re.sub(r"\D", "", str(m.get("start") or ""))[:14]
        end = re.sub(r"\D", "", str(m.get("end") or ""))[:14]
        if start and end and any(start <= s <= end for s in ex):
            continue
        out.append({"id": meta.name[:-len("_meta.json")],
                    "day": m.get("day"), "start": m.get("start"),
                    "end": m.get("end"), "meta": meta})
    return out


def import_is_expendable(ctx, root):
    """(ok, reason) — is everything from `root` rendered, and published if this
    install publishes? The delete step's proof, factored out so clearing the
    working dir before a copy cannot become a softer version of the same act."""
    ns = ctx.out_dir / root.name
    mp4s = rendered_mp4s(ns) if ns.is_dir() else []
    if not mp4s:
        return False, "nothing from it was rendered"
    # Only ask the scanner how many trips there SHOULD be when the source is
    # still there to scan. Once the import is deleted the question is
    # unanswerable and asking it makes the renderer error out on a missing DCIM
    # folder — which is not the same as "these renders are incomplete". The S3
    # check below still has to pass either way.
    if (root / "DCIM").is_dir():
        payload = load_groups(ctx, root)
        gs = (payload or {}).get("trips") or []
        want = sum(1 for g in gs if g.get("renderable", True)) if gs else None
        if want is not None and len(mp4s) < want:
            return False, "%d of %d trip(s) rendered" % (len(mp4s), want)
    if ctx.cfg_opt("s3_bucket"):
        remote = s3_objects(ctx)
        if remote is None:
            return False, "could not list the bucket to confirm the uploads"
        missing = [p.name for p in mp4s
                   # "/" boundary — a bare endswith let sometrip_X.mp4 in the
                   # bucket vouch for trip_X.mp4 on disk.
                   if not any((k == p.name or k.endswith("/" + p.name))
                              and v == p.stat().st_size
                              for k, v in remote.items())]
        if missing:
            return False, "%d render(s) not on S3" % len(missing)
    return True, ""


OWNER_FILE = ".owned-by"
LOCK_FILE = ".pipeline.lock"


def _pid_alive(pid):
    """Is a process with this pid running? Signal 0 probes without touching it.

    PermissionError means the pid exists but belongs to someone else — alive.
    Any other failure to find out counts as alive: the expensive mistake is
    reclaiming a lock whose owner is still working.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def acquire_single_instance_lock(ctx):
    """Take the workspace lock, or return False because someone holds it.

    Two instances against one working area is two menus both believing their
    cached scans and both willing to sweep — so only one runs. The lock is a
    pid file rather than a flag file, because a flag left by a crash would
    lock the owner out of his own tool forever: a lock whose recorded pid is
    no longer running is stale by definition and is reclaimed silently. Not
    being able to write the lock at all is not a reason to refuse either —
    the lock is extra safety, not a gate the tool can die behind.
    """
    lock = ctx.out_dir / LOCK_FILE
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        if lock.is_file():
            try:
                pid = int(lock.read_text().split()[0])
            except (ValueError, IndexError, OSError):
                pid = None          # unreadable/garbage: nobody provably owns it
            if pid is not None and _pid_alive(pid):
                return False
        lock.write_text("%d\n" % os.getpid())
        return True
    except OSError:
        return True


def release_single_instance_lock(ctx):
    """Remove the lock, but only if it is ours — never someone else's."""
    lock = ctx.out_dir / LOCK_FILE
    try:
        if lock.is_file() and int(lock.read_text().split()[0]) == os.getpid():
            lock.unlink()
    except (ValueError, IndexError, OSError):
        pass


def claim_out_dir(ctx):
    """Record which checkout this working area belongs to, and return the other
    one if it is already claimed by somebody else.

    Deriving `out` from `root` fixes the accidental case; this catches the
    deliberate one, where two checkouts are pointed at the same directory on
    purpose or by a copied config. The sweep is silent and total, so "whose
    files are these" has to be answerable before it runs, not after.
    """
    marker = ctx.out_dir / OWNER_FILE
    mine = str(ctx.exporter)
    try:
        if marker.is_file():
            owner = marker.read_text(encoding="utf-8").strip()
            return None if owner == mine else owner
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(mine + "\n", encoding="utf-8")
    except OSError:
        pass          # unwritable is not a reason to refuse; it is a reason to
    return None       # carry on without the extra proof


def working_area_is_expendable(ctx):
    """(ok, why, stragglers) — is everything in the working area a second copy?

    The sweeps act on the premise that the working area's contents belong to a
    round that ended by being uploaded or gathered into final_. This function
    exists because that premise has to be TRUE before it is acted on, not
    assumed: a render whose upload failed or was skipped is the only copy, and
    unlinking it costs the hours it took to encode.

    A render is expendable when it is EITHER
      - in the bucket at a matching size (only checkable with s3_bucket set) —
        AND, when a site repo is also configured, recorded by a site deploy
        that ran after it existed (see DEPLOYED_FILE), OR
      - inside a final_<date> folder, which is where Build Website moves the deliverable
        on an install that does not publish.
    Everything else in the working area — previews/, the caches, the stills — is
    derived from renders and costs seconds to rebuild, so it never blocks.

    The deploy half is the owner's rule — no deleting the workspace without a
    verified S3 upload AND the site deploy — and it binds only the configured
    edition: a local-only install has no bucket and no site, and gathering
    into final_ is what makes its workspace expendable. The evidence is the
    record Upload Website writes on a successful deploy, because one that never ran
    leaves exactly nothing to find.

    No renders at all is expendable: there is nothing to lose.
    """
    out = ctx.out_dir
    if not out.is_dir():
        return True, "nothing there", []

    # Renders outside a final_ folder are the ones that still need proving.
    loose = [f for f in rendered_mp4s(out)
             if not any(part.startswith(FINAL_PREFIX) for part in f.relative_to(out).parts)]
    if not loose:
        return True, "no unfinished renders in the working area", []

    # Gathered: Build Website MOVES a render into final_<date>. Matched on name AND
    # SIZE, because trip filenames repeat exactly across re-renders at the same
    # height — on name alone, a re-rendered trip collides with the stale copy
    # in final_ and is declared expendable, deleting the very file
    # gather_into_final refuses to overwrite so it can be looked at.
    gathered = set()
    froot = getattr(ctx, "final_root", out)
    for base in (froot, out):
        if base.is_dir():
            for d in base.glob(FINAL_PREFIX + "*"):
                for f in d.rglob("*.mp4"):
                    try:
                        gathered.add((f.name, f.stat().st_size))
                    except OSError:
                        pass

    remote = {}
    if ctx.cfg_opt("s3_bucket"):
        remote = s3_objects(ctx) or {}

    # When BOTH halves of publishing are configured, "uploaded" alone does not
    # settle it: the render must also be covered by a site deploy that
    # actually happened. Upload Website records the render names it deployed
    # with; absence of a name there means the site has never served it.
    require_deploy = bool(ctx.cfg_opt("s3_bucket")) and getattr(ctx, "site", None) is not None
    deployed = set()
    if require_deploy:
        try:
            deployed = set(json.loads((out / DEPLOYED_FILE).read_text()).get("videos", []))
        except Exception:
            deployed = set()

    stragglers = []
    for f in loose:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if (f.name, size) in gathered:
            continue
        # endswith on a "/" boundary: a bare endswith let sometrip_X.mp4 in the
        # bucket satisfy trip_X.mp4 on disk.
        if any((k == f.name or k.endswith("/" + f.name)) and v == size
               for k, v in remote.items()) \
                and (not require_deploy or f.name in deployed):
            continue
        stragglers.append(f)

    if stragglers:
        what = ("not both uploaded and site-deployed" if require_deploy
                else "neither uploaded nor gathered")
        return False, "%d render(s) %s" % (len(stragglers), what), stragglers
    return True, "%d render(s), all uploaded or gathered" % len(loose), []


def purge_published_renders(ctx, root):
    """Empty the working area. Everything goes except a short keep-list.

    Once the trips are on S3 and on the site, every file here is a third copy or
    a cache of something that no longer exists: the renders, their sidecars,
    previews/ from the review pass, the extracted GPX cache, the boundary cache
    that names clips already deleted. Keeping any of it leaves exactly the files
    that are impossible to make a decision about later.

    Kept: logs/ (the history of what was done), the import directory itself so
    the next copy has somewhere to land, the ledger, and every *_meta.json. Any
    final_* folder is unaffected because it lives beside this tree, not in it.

    The metadata stays because it IS the state, and it is nothing: ~1.4 KB per
    trip against gigabytes released. It is what last_imported_stamp reads to
    answer "have I already imported this card" once the footage is gone, what
    build_manifest carries forward for a trip whose render was deleted after
    publishing, and what Delete SIM Data's evidence check reads to decide whether a
    card's clips are inside a rendered trip. Earlier this swept them away while
    two printed messages claimed they survived — the messages were right about
    the intent and the code was wrong.

    The ledger is written BEFORE anything is removed. It is the only fact here
    that cannot be recovered from somewhere else — how far the imports have
    reached — and a crash midway must not lose it.
    """
    write_ledger(ctx, last_imported_stamp(ctx), "cleanup after publish")

    out = ctx.out_dir
    if not out.is_dir():
        return 0, 0
    # EXCLUDED_FILE survives for the same reason the ledger does: it is state
    # ("these clips were dropped on purpose"), unrecoverable once gone, and
    # the delta import and the clean-up both read it after the footage is deleted.
    keep_names = {"logs", LEDGER_FILE, OWNER_FILE, EXCLUDED_FILE, DEPLOYED_FILE, root.name}
    freed = n = 0
    for child in sorted(out.iterdir()):
        if child.name in keep_names or child.name.startswith(FINAL_PREFIX):
            # The import dir stays, but empties. The SAME _meta.json exemption
            # as the general branch below, because this branch does not only see
            # footage: renders are namespaced out_dir/<import name>/, so the
            # render namespace of the very import being cleaned up carries
            # root's name and lands HERE, not below. Without the exemption the
            # sweep destroyed exactly the metas the docstring promises to keep,
            # for exactly the trips whose footage was just deleted. A real
            # footage dir holds no _meta.json, so sparing them costs nothing.
            if child.name == root.name and child.is_dir():
                for f in sorted(child.rglob("*")):
                    if f.is_file() and not f.name.endswith("_meta.json"):
                        try:
                            freed += f.stat().st_size
                            f.unlink()
                            n += 1
                        except OSError:
                            pass
                for d in sorted(child.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
            continue
        try:
            if child.is_dir():
                # Delete file by file so the metadata can be spared, then drop
                # the directories that end up empty. rmtree would take the
                # _meta.json with everything else.
                for f in sorted(child.rglob("*")):
                    if f.is_file() and not f.name.endswith("_meta.json"):
                        freed += f.stat().st_size
                        f.unlink()
                        n += 1
                for d in sorted(child.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass          # still holds metadata — that is the point
                try:
                    child.rmdir()
                except OSError:
                    pass
            elif not child.name.endswith("_meta.json"):
                freed += child.stat().st_size
                child.unlink()
                n += 1
        except OSError:
            pass
    return n, freed

def prepare_for_import(ctx):
    """Drop the caches a new import would silently poison.

    <out>/.gpx_cache holds tracks harvested from the PREVIOUS card's tar
    archives, keyed by nothing but their filenames — so a new card's clip that
    shares a name resolves to the old card's track, and nothing says so. The
    boundary cache protects itself (its key covers every clip and gpx); this
    one cannot, so a new import starts it empty. Everything in it is derived
    (re-harvested from the import's own archives on the next scan), which is
    what makes deleting it safe. Says what it removed.
    """
    removed = 0
    cache = ctx.out_dir / ".gpx_cache"
    if cache.is_dir():
        for f in sorted(cache.iterdir()):
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        print(C.dim("  Cleared %d cached gpx file(s) from %s — a new card must not"
                    " inherit the old card's tracks." % (removed, tilde(cache))))
    return removed


def record_import(ctx, card):
    """Advance the ledger to the newest clip on the card just imported.

    Called only after import-sd-card.sh exits 0, which it does only after
    verifying the copy file-for-file — so 'a verified copy of everything up to
    this stamp exists on this disk' is a fact, and that fact is what the
    ledger holds. Taken from the CARD because the card is what the next delta
    compares against. write_ledger refuses to move backwards.
    """
    newest = ""
    front = card / "DCIM" / "200video" / "front"
    if front.is_dir():
        for f in front.glob("*.mp4"):
            m = STAMP_RE.search(f.name)
            if m and m.group(1) > newest:
                newest = m.group(1)
    if newest:
        write_ledger(ctx, newest, "imported and verified")
    return newest or None


def step_import(ctx):
    """Copy the source's DCIM tree into a dated import folder (import-sd-card.sh).

    The source (`card` in config.txt) is any directory holding a DCIM tree —
    the mounted SD card is the common case, but a card copied onto an external
    disk or a folder someone handed over works the same.
    """
    started = time.time()
    if not (ctx.card / "DCIM").is_dir():
        # No source is not automatically a problem. The configured root may
        # already hold a copied card, in which case importing is simply not the
        # step he wants — saying "is the card mounted?" there sends him looking
        # for a fault that does not exist. Check the second location and answer
        # the question he actually has: is there footage here to work on?
        cands = import_candidates(ctx)
        if cands:
            src = cands[0]
            n = clip_count(src)
            sz = human_bytes(tree_size(src / "DCIM"))
            print(C.green("  Nothing to import — %s already holds %s clips (%s)."
                          % (tilde(src), n, sz)))
            print(C.dim("  That is the configured root from config.txt. Go to %d) %s"
                        " or %d) %s." % (META, NAME[META], RENDER, NAME[RENDER])))
            # SATISFIED, not skipped: this item's postcondition is that footage
            # is in the workspace, and it is. Nothing was done and nothing is
            # owed, so the pipeline may move on.
            return record(ctx, NAME[IMPORT], SATISFIED, started,
                          "import already present, %s clips" % n)
        print(C.yellow("  No footage at %s and none under %s." % (
            tilde(ctx.card), tilde(ctx.render_root))))
        print(C.dim("  Mount the card, or point `card` in config.txt at any folder"))
        print(C.dim("  holding a DCIM tree (a copied card works the same)."))
        return record(ctx, NAME[IMPORT], SKIPPED, started, "no source, no import")

    clips = clip_count(ctx.card)
    size = tree_size(ctx.card / "DCIM")
    print("  Source: %s  (%s clips, %s)" % (tilde(ctx.card), clips, human_bytes(size)))

    other = claim_out_dir(ctx)
    if other:
        # Never work on somebody else's behalf. Two checkouts sharing a working
        # area is not automatically wrong, but "whose files are these" has to be
        # answerable before a copy lands in it.
        print()
        print(C.red("  %s is claimed by another checkout:" % tilde(ctx.out_dir)))
        print(C.red("    %s" % tilde(Path(other))))
        print(C.dim("  Not touching it. Set `out` in config.txt to a directory of"))
        print(C.dim("  your own, or delete %s if that claim is stale."
                    % tilde(ctx.out_dir / OWNER_FILE)))
        return record(ctx, NAME[IMPORT], SKIPPED, started,
                      "output dir owned by %s" % other)

    # Footage from a previous round still in the sink. This used to offer to
    # CLEAR it, and in one branch swept the working area with no prompt at all
    # — item 8's job done from inside item 1, twice. Under this graph item 1
    # offers item 8 directly, so the offer has nowhere to earn its place. What
    # stays is the warning and the gate on this item's own job: importing on
    # top mixes two cards into one grouping and nothing afterwards records
    # which clip came from which.
    leftovers = import_candidates(ctx)
    if leftovers:
        print()
        print(C.yellow("  The import area is not empty:"))
        for src in leftovers:
            print(C.yellow("    %s  %s clips, %s"
                           % (tilde(src), clip_count(src), human_bytes(tree_size(src / "DCIM")))))
        print(C.dim("  Importing now adds this card alongside that footage. Trips are"))
        print(C.dim("  grouped across everything found, so the two cards would be mixed"))
        print(C.dim("  and there is no record afterwards of which clip came from which."))
        print(C.dim("  Clear it with %d) %s first, or finish the round it belongs to."
                    % (CLEAN_WS, NAME[CLEAN_WS])))
        if not confirm("  Import anyway, on top of what is there?", False):
            return record(ctx, NAME[IMPORT], SKIPPED, started,
                          "declined: import area not empty")
        print()

    # Delta copy is the default. A card left in the car accumulates: this one
    # holds 1039 front clips of which 427 were already taken in last time, and
    # copying those again costs tens of GB and the minutes you want back to put
    # the card away. The high-water mark survives deleting the local import,
    # because it is read from the renders and the boundary cache, not the
    # footage.
    after = last_imported_stamp(ctx)
    excluded_stamps(ctx)                 # refresh the cache card_split reads
    if after:
        n_new, n_old = card_split(ctx.card, after)
        print()
        print("  Already imported through %s" % C.bold(after))
        print("  At the source: %s new, %s already here" % (
            C.bold("%d clip(s)" % n_new), C.dim("%d" % n_old)))
        if not n_new:
            print(C.green("  Nothing new at the source — it is already all imported."))
            return record(ctx, NAME[IMPORT], SATISFIED, started, "no new clips")
        delta = confirm("  Copy only the %d new clip(s)?" % n_new, True)
    else:
        delta = False
        print(C.dim("  Nothing imported before, so this copies the whole card."))

    # No prompt for this. It names the folder the copy lands in, which is an
    # implementation detail of where the tool puts things — asking put a question
    # on screen that the person cannot answer better than the tool can, and whose
    # only sane answer is the default. Today's date, which is what it defaulted to.
    day = time.strftime("%Y-%m-%d")
    # Not offered after a delta copy. Only the new clips come over, so erasing
    # the card would take the earlier ones — the ones already imported, whose
    # only record is a ledger the shell script cannot read. The script refuses
    # the combination outright; asking here would just be a prompt whose yes
    # ends in a failed run.
    if delta and after:
        # The reason is not that the skipped clips are precious — they are
        # already imported, and once rendered and uploaded the card is the copy
        # that matters least. It is that --delete only ever fires after a verify,
        # and this run verifies the files it copied. The other 427 were verified
        # by a run that finished days ago, a fact recorded in a ledger the shell
        # script cannot read. Erasing them here would be a delete on somebody
        # else's evidence.
        print(C.dim("  Source kept. Erasing it would also remove the %d clip(s) this run" % n_old))
        print(C.dim("  skipped, and this run checked nothing about those — they were"))
        print(C.dim("  verified by the earlier import, not by this one. Erase the card"))
        print(C.dim("  yourself once these have rendered and uploaded."))
        erase = False
    else:
        print(C.dim("  The source is NOT erased by default; import-sd-card.sh only deletes"))
        print(C.dim("  its files after the copy verifies file-for-file."))
        erase = confirm("  Erase the source's files after a verified copy?", False)

    env = {"DASHCAM_IMPORT_ROOT": str(ctx.import_root)}
    if after and delta:
        env["AFTER_STAMP"] = after
    cmd = ["./import-sd-card.sh"]
    if erase:
        cmd.append("--delete")
    cmd.append(day)
    if str(ctx.card) != FALLBACK_CARD:
        cmd[1:1] = ["--src", str(ctx.card)]

    # No "Run: ... ?" either. Copying only the new clips was already answered,
    # and so was erasing the card; the command line adds nothing you can act on.
    # It is echoed so it is on screen and in the log.
    prepare_for_import(ctx)
    print(C.dim("  %s" % " ".join(cmd)))

    rc, lines = run_stream(cmd, ctx.exporter, "Import", parser=rsync_parser,
                           env_extra=env,
                           keep=lambda l: l.startswith(("Verified:", "Card cleaned", "Done.",
                                                        ">>> only clips newer", ">>> ")))
    if rc != 0:
        return record(ctx, NAME[IMPORT], FAILED, started, "exit %d" % rc)

    dest = ctx.import_root / day
    ctx.selected_import = dest if (dest / "DCIM").is_dir() else ctx.selected_import
    # An import MERGES into an existing day folder (rsync), so any scan taken
    # before it is now stale — it does not know about the clips that just landed.
    # The delete guard leans on that scan, so leaving it in place would let it
    # approve erasing footage nothing has ever looked at.
    ctx.last_scan = None
    ctx.last_groups = None

    # Record the high-water mark HERE, not only at clean-up time. The next
    # delta import reads it to know what is already in, and item 9 compares the
    # card against it — recorded only after publishing, an interrupted cycle
    # would leave every clip counting as "never imported".
    record_import(ctx, ctx.card)

    return record(ctx, NAME[IMPORT], RAN, started,
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


def step_progress(ctx):
    """Progress: the files on disk and what has been done to them. Read-only.

    An observation of state, not a transition in the flow: which trips exist,
    which are excluded, which are rendered, uploaded and live. It generates
    nothing and writes nothing — every fact here is read from the sidecars,
    the renders, the bucket listing and the deploy record, so an empty
    workspace is a legitimate answer, not an error.
    """
    started = time.time()
    trips = listed_trips(ctx)
    excluded = excluded_stamps(ctx)
    if not trips:
        cands = import_candidates(ctx)
        if cands:
            n = clip_count(cands[0])
            print("  %s clips imported in %s — no sidecars yet." % (n, tilde(cands[0])))
            print(C.dim("  Run %d) %s to write them." % (META, NAME[META])))
        else:
            print("  Nothing imported yet.")
            print(C.dim("  Run %d) %s to bring footage in." % (IMPORT, NAME[IMPORT])))
        return record(ctx, NAME[PROGRESS], RAN, started, "no trips yet")

    renders = {}
    for p in rendered_mp4s(ctx.out_dir):
        try:
            renders[p.name] = p.stat().st_size
        except OSError:
            continue
    # The bucket listing is one network call and only when it can mean
    # something; offline or unconfigured, the column honestly says unknown.
    remote = None
    if ctx.s3_bucket and not ctx.offline:
        remote = s3_objects(ctx)
    deployed = set()
    try:
        deployed = set(json.loads((ctx.out_dir / DEPLOYED_FILE).read_text()).get("videos", []))
    except Exception:
        pass

    n_rendered = n_uploaded = n_live = 0
    print("  %-38s %-9s %-9s %-9s %s" % ("trip", "sidecars", "rendered", "uploaded", "live"))
    for t in trips:
        mp4 = next((n for n in sorted(renders) if n.startswith(t["id"] + "_h")), None)
        up = live = False
        if mp4:
            n_rendered += 1
            size = renders[mp4]
            if remote is not None:
                up = any((k == mp4 or k.endswith("/" + mp4)) and v == size
                         for k, v in remote.items())
                n_uploaded += 1 if up else 0
            live = mp4 in deployed
            n_live += 1 if live else 0
        yn = lambda b: "yes" if b else "-"
        upcol = "?" if (mp4 and remote is None) else yn(up)
        print("  %-38s %-9s %-9s %-9s %s"
              % (t["id"], "yes", yn(bool(mp4)), upcol, yn(live)))
    print()
    line = "  %d trip(s): %d rendered, %d live" % (len(trips), n_rendered, n_live)
    if remote is not None:
        line = "  %d trip(s): %d rendered, %d uploaded, %d live" % (
            len(trips), n_rendered, n_uploaded, n_live)
    print(line)
    if excluded:
        print(C.dim("  %d clip stamp(s) excluded on purpose." % len(excluded)))
    return record(ctx, NAME[PROGRESS], RAN, started,
                  "%d trip(s), %d rendered, %d live" % (len(trips), n_rendered, n_live))


def step_generate_meta(ctx):
    """Write the sidecars: each trip's _meta.json, .gpx and .html map.

    The metadata is what every later item reads — the render's trip list, the
    site's manifest, the guards' evidence — so making it exist is its own item,
    sitting right after the import. No stills, no encoding, no table: looking
    at the result is Build Preview's job.

    No availability check here any more. MenuItem.execute consults evaluate()
    before it calls this, against a world captured a moment ago; a body that
    re-asks is a second copy of the same rule.
    """
    started = time.time()
    root = pick_import(ctx, "the sidecar pass")
    if root is None:
        return record(ctx, NAME[META], SKIPPED, started, "no import folder")

    # Only wake the renderer when a trip is missing its set. "Missing" is per
    # trip, and a trip counts as done when all three files exist. A partial set
    # means an interrupted pass, so that trip gets redone — and since
    # --sidecars-only has no per-trip selection, one missing trip means the
    # whole pass runs again. Correct and occasionally slow beats fast and
    # subtly incomplete.
    have = load_groups(ctx, root)
    need = True
    if have:
        gs = [g for g in have.get("trips", []) if g.get("renderable", True)]
        if gs:
            done = 0
            for g in gs:
                base = g.get("out_base")
                if base and all(Path(base + s).is_file()
                                for s in (".gpx", ".html", "_meta.json")):
                    done += 1
            need = done < len(gs)
            if not need:
                print(C.dim("  Sidecars already written for all %d trip(s) — nothing to"
                            " generate." % len(gs)))
                # The postcondition holds: every trip has its set. Nothing was
                # done and nothing is owed.
                return record(ctx, NAME[META], SATISFIED, started,
                              "sidecars already complete for %d trip(s)" % len(gs))
            if done:
                print(C.dim("  %d of %d trip(s) have sidecars; rewriting all (the"
                            " renderer has no per-trip mode)." % (done, len(gs))))
    # The renderer prints its usual "[Trip a/b]" headers here, so the real trip
    # counter drives the bar; there are no per-clip lines in this mode.
    cmd = (["./make-trips-rendered.sh", "--sidecars-only", "--root", str(root), "--out", str(ctx.out_dir)]
           + ctx.config_args + ctx.scan_args)
    rc, _lines = run_stream(cmd, ctx.exporter, "Sidecars", parser=make_scan_parser(),
                            keep=lambda l: l.startswith("[Trip "))
    if rc != 0:
        return record(ctx, NAME[META], FAILED, started, "sidecars exit %d" % rc)
    metas = len(list(ctx.out_dir.rglob("trip_*_meta.json")))
    print(C.green("  Sidecars in place — %d trip meta file(s) under %s."
                  % (metas, tilde(ctx.out_dir))))
    return record(ctx, NAME[META], RAN, started, "%d trip meta file(s)" % metas)


# ---------------------------------------------------------------------------
# The trip -> source clip mapping.
#
# Everything below that names a file of original footage — the preview contact
# sheet, and Exclude Trip which DELETES — reads this one mapping, straight from
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
    differently. Since Exclude Trip deletes by this mapping, it has to be the
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
    the render, and the mapping Exclude Trip deletes by.

    A silently worse answer is the failure mode worth refusing outright.
    """
    py = renderer_python(ctx)
    # Say what is happening first. Importing cv2 for the first time reads ~100 MB
    # of shared libraries, and on a machine already busy encoding that can take
    # the better part of a minute — during which a silent check is
    # indistinguishable from a hang, and the natural response is ctrl-C. The
    # message is erased again on success so the normal startup stays clean.
    msg = "  Checking for ego-motion support (first run can take a moment)..."
    live = sys.stdout.isatty()
    if live:
        sys.stdout.write(C.dim(msg))
        sys.stdout.flush()
    try:
        # The timeout is generous rather than snappy: it exists to end a genuine
        # hang, not to judge a slow import, and failing a working install because
        # ffmpeg had the CPU would be the worse error by far.
        r = subprocess.run([py, "-c", "import cv2, numpy"],
                           capture_output=True, cwd=str(ctx.exporter), timeout=180)
        rc, why = r.returncode, ""
    except subprocess.TimeoutExpired:
        rc, why = 1, "timeout"
    except KeyboardInterrupt:
        if live:
            sys.stdout.write("\r" + " " * len(msg) + "\r")
        print(C.dim("  Cancelled."))
        return False
    if live:
        sys.stdout.write("\r" + " " * len(msg) + "\r")
        sys.stdout.flush()
    if rc == 0:
        return True
    print()
    # A timeout is not the same finding as a failed import, and giving it the
    # install advice sends someone to reinstall a working venv. It says nothing
    # was learned, because that is what happened.
    if why == "timeout":
        print()
        print(C.yellow("  Could not verify ego-motion support — the check timed out."))
        print()
        print("  Importing opencv reads about 120 MB from disk, and on a machine")
        print("  already busy encoding that can exceed the 3 minutes allowed. This")
        print("  says nothing about the install: it neither succeeded nor failed.")
        print()
        print("  Try again once the render finishes. If it times out on an idle")
        print("  machine, then something really is wrong with:")
        print("    %s" % py)
        print()
        return False
    print(C.red("  Ego-motion detection is not available — refusing to start."))
    print()
    print("  Trip boundaries would fall back to a GPS radius, which groups this")
    print("  card differently: it merges parked hours into drives and invents")
    print("  trips that are not there. Previews, renders and Exclude Trip would")
    print("  all be built on the wrong grouping.")
    print()
    print("  Interpreter checked: %s" % py)
    # Print the interpreter he demonstrably has — the one running this — rather
    # than a bare "python3". On a machine where python3 is not on PATH, or is a
    # different build from the one that works, a hardcoded name sends him to fix
    # the wrong thing. sys.executable is by definition present and runnable.
    ver = "%d.%d.%d" % sys.version_info[:3]
    print(C.bold("  Install with:"))
    print("    cd %s" % ctx.exporter)
    print("    %s -m venv .venv && .venv/bin/pip install -r requirements.txt"
          % sys.executable)
    print(C.dim("    (that is the python %s running this script — swap it if you "
                "prefer another)" % ver))
    print()
    return False


def load_groups(ctx, root, refresh=False):
    """Run --print-groups against `root` and return its parsed JSON, or None.

    Cached per session per import folder: the scan decodes video to find the
    pull-away and park moments, so it costs the same minutes as the sidecar
    pass's own boundary scan.
    """
    if not refresh and ctx.last_groups and ctx.last_groups[0] == root:
        print(C.dim("  Using the trip grouping already scanned in this session."))
        return ctx.last_groups[1]

    print(C.dim("  Scanning %s for the authoritative trip grouping." % root))
    print(C.dim("  This is the same boundary scan %d) %s runs (it walks the video), so it takes"
                % (META, NAME[META])))
    print(C.dim("  a while; the result is reused for the rest of this session."))
    fd, tmp = tempfile.mkstemp(prefix="dashcam-groups-", suffix=".json")
    os.close(fd)
    try:
        rc, _lines = run_stream(
            [renderer_python(ctx), "-u", "make_dashcam_videos.py", "--print-groups",
             "--root", str(root), "--out", str(ctx.out_dir)] + ctx.config_args + ctx.scan_args,
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


def sidecar_set(mp4):
    """A rendered trip's mp4 and everything written beside it.

    The renderer writes trip_<day>_<time>_<nn>_h<height>.mp4 plus .gpx, .html,
    _links.txt, _meta.json and a .log under the same stem minus the _h<height>
    suffix. Deleting the mp4 alone leaves a map and a metadata file that the
    site build and the manifest still read, so the trip comes back as an entry
    with no video — which looks like a bug rather than a decision.
    """
    out = [mp4]
    stem = re.sub(r"_h\d+$", "", mp4.stem)
    for f in sorted(mp4.parent.glob(stem + "*")):
        if f != mp4 and f.is_file():
            out.append(f)
    return out


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
# Defaults for the still-frame knobs; config.txt's still_width / still_seconds
# override both the preview sheet and the site page, which is the only sane
# arrangement — a still that is right for one is right for the other.
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
/* The route, drawn from the trip's GPX. Sits between the numbers and the
   links because it reads as one of the stats, not as an illustration. */
.route{margin:10px 0 2px;background:#081221;border:1px solid var(--line);
       border-radius:8px;padding:6px;overflow:hidden}
.route svg{display:block;width:100%;height:auto}
.route.none{color:var(--faint);font-size:13px;padding:14px;text-align:center}

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
    # Chronological, earliest first. --print-groups returns them in discovery
    # order, which is index order and only accidentally the order they were
    # driven — a card review reads as a day, so the page should too. The trip
    # INDEX on each card stays what it was, because that is what Exclude Trip
    # takes and renumbering it here would be a trap.
    trips = sorted(payload.get("trips", []), key=lambda x: (x.get("start") or "", x["index"]))
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

        # The route, drawn inline, not the .html map sidecar. The sidecar pulls
        # leaflet from unpkg and tiles from OSM, so on the machine doing the
        # reviewing (often offline, always before publishing) it opens as an
        # empty grey box. The same GPX rendered as an SVG needs nothing, and
        # this page's whole premise is that it works from file:// with no
        # network. The sidecar link stays, for when you do want the pannable
        # version.
        gpx = Path(t["out_base"] + ".gpx") if t.get("out_base") else None
        pts = gpx_track(gpx) if (gpx and gpx.is_file()) else []
        if pts:
            route = '<div class="route">%s</div>' % route_glyph(
                pts, w=560, h=200, speed_colour=ctx.speed_colour)
        elif meta is None:
            route = ''      # already explained by the flags above
        else:
            route = '<div class="route none">no GPS track for this trip</div>'

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
            '<div class="flags">%s</div><dl>%s</dl>%s<div class="links">%s</div>%s%s'
            '</div></section>' % (
                shot, idx, html.escape(t["day"]), html.escape(t["start"][11:16]),
                "".join(flags) or '<span class="flag">&nbsp;</span>', dl, route,
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

    # Named for what it is and which footage it describes, not "index.html".
    # It gets opened from a file manager, mailed, kept next to a final_<date>
    # folder — and in every one of those places "index.html" is the name of a
    # hundred other files. The date is the newest day in the batch, the same
    # tag final_dir_for uses, so the pair reads as one thing.
    days = sorted({t.get("day") for t in trips if t.get("day")})
    tag = days[-1] if days else time.strftime("%Y-%m-%d")
    index = previews_dir / ("preview_%s.html" % tag)
    index.write_text(doc, encoding="utf-8")
    # A stale page from an earlier run of the same batch would sit beside it
    # looking equally current.
    for old_page in previews_dir.glob("preview_*.html"):
        if old_page != index:
            old_page.unlink()
    stale = previews_dir / "index.html"
    if stale.is_file():
        stale.unlink()
    return index


def build_sidecars(ctx):
    """Write the sidecars that are missing; touch nothing that exists.

    The re-runnable form of the sidecar pass: safe to call again after an
    interruption, and a no-op when there is nothing to do. 'Nothing to do' is
    judged per import day — a day whose clips already have at least one
    complete sidecar set (meta + .gpx + .html sharing a stem) in its output
    folder counts as done. That is deliberately a cheaper test than
    step_generate_meta's per-trip check via the grouping scan: this gate decides
    whether to WAKE the renderer, and waking it to discover 'nothing missing'
    costs the minutes the check exists to save. When any day is missing, the
    whole pass runs (--sidecars-only has no per-trip selection) and rewrites
    the same bytes from the same clips — idempotent by construction.
    """
    ran = []
    for cand in import_candidates(ctx):
        front = cand / "DCIM" / "200video" / "front"
        days = set()
        if front.is_dir():
            for f in front.glob("*.mp4"):
                m = STAMP_RE.search(f.name)
                if m:
                    s = m.group(1)
                    days.add("%s-%s-%s" % (s[0:4], s[4:6], s[6:8]))
        ns = ctx.out_dir / cand.name
        missing = []
        for day in sorted(days):
            d = ns / day
            complete = False
            if d.is_dir():
                for meta in d.glob("trip_*_meta.json"):
                    stem = meta.name[:-len("_meta.json")]
                    if (d / (stem + ".gpx")).is_file() and (d / (stem + ".html")).is_file():
                        complete = True
                        break
            if not complete:
                missing.append(day)
        if not missing:
            continue
        cmd = (["./make-trips-rendered.sh", "--sidecars-only",
                "--root", str(cand), "--out", str(ctx.out_dir)]
               + ctx.config_args + ctx.scan_args)
        rc, _lines = run_stream(cmd, ctx.exporter, "Sidecars", parser=make_scan_parser(),
                                keep=lambda l: l.startswith("[Trip "))
        if rc == 0:
            ran.append(cand)
    return ran


def step_preview(ctx):
    """One still per trip + a local contact sheet, from the sidecars. No encoding.

    The LOOKING half: the sidecars were written by Generate Meta, and this
    builds what a human judges from — a frame per trip and a page to see them
    on. The cheap pass that makes pruning possible: encoding is hours and
    uploading is days, so the decision about which trips to keep has to be
    makeable before either. If the sidecars are missing this step is blocked
    and says which step writes them — it does not generate them on the side.
    """
    started = time.time()
    root = pick_import(ctx, "the preview pass")
    if root is None:
        return record(ctx, NAME[PREVIEW], SKIPPED, started, "no import folder")

    previews_dir = ctx.out_dir / PREVIEW_DIRNAME
    print(C.dim("  Two cheap things, no encoding:"))
    print(C.dim("    1. one still per trip, a frame from its first front clip"))
    print(C.dim("    2. %s/preview_<day>.html — a contact sheet to open locally"
                % previews_dir))
    print(C.dim("  Reviewing is entirely offline; deploying stays a separate choice."))

    # No second confirmation here: the menu already asked "Go?", and the source
    # directory is resolved and printed just above. Asking again puts a question
    # on screen whose answer is already on screen — two prompts for one decision,
    # which is how you teach someone to stop reading them.

    # The grouping — which is also the trip -> source clip mapping the stills
    # and the contact sheet's clip lists are built from.
    payload = load_groups(ctx, root)
    if payload is None:
        return record(ctx, NAME[PREVIEW], FAILED, started, "--print-groups failed")
    trips = payload.get("trips", [])
    if not trips:
        print(C.yellow("  The scan found no trips in %s." % root))
        return record(ctx, NAME[PREVIEW], SKIPPED, started, "no trips")

    # Stills. Every trip gets one, including the auto-skipped fragments — he
    # is deciding what to keep, and a trip he cannot see is one he cannot judge.
    previews_dir.mkdir(parents=True, exist_ok=True)
    stills, failed = {}, []
    for i, t in enumerate(trips, 1):
        front = t.get("front") or []
        name = "trip_%02d_%s_%s.jpg" % (t["index"], t["day"], t["start"][11:16].replace(":", "-"))
        dst = previews_dir / name
        if not front:
            print("  still %d/%d  %s" % (i, len(trips), name))
            failed.append(t["index"])
            continue
        # Keep a still that is already there and not older than its clip. It is
        # one ffmpeg seek per trip, which is seconds rather than minutes, but it
        # is seconds spent producing a file that already exists — and on a
        # second look at forty trips that is the difference between a glance and
        # a wait.
        src = Path(front[0])
        if dst.is_file() and src.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            print("  still %d/%d  %s %s" % (i, len(trips), name, C.dim("(have it)")))
            stills[t["index"]] = dst
            continue
        print("  still %d/%d  %s" % (i, len(trips), name))
        if extract_still(src, dst,
                         seconds=ctx.still_seconds, width=ctx.still_width):
            stills[t["index"]] = dst
        else:
            failed.append(t["index"])
    if failed:
        print(C.yellow("  No still for trip(s) %s — ffmpeg could not read the first clip."
                       % ", ".join(str(i) for i in failed)))

    index = write_contact_sheet(ctx, root, payload, previews_dir, stills)

    # No trips.json refresh here any more. Preview used to re-index the site
    # manifest "while we're here", which made a looking-step write into the
    # site repo; deploy-site.sh re-indexes as its own first act, so the deploy
    # never carries a stale manifest anyway. One step, one job.

    print()
    print(C.green("  previews are in %s" % previews_dir))
    print("  %d trip(s), %d still(s). Open the contact sheet with:" % (len(trips), len(stills)))
    print("    open %s" % index)
    print(C.dim("  Nothing was encoded and nothing was published. On the website these"))
    print(C.dim("  trips would say the video is not available — that is expected: the"))
    print(C.dim("  sidecars carry the map, the stats and the places, but no video exists"))
    print(C.dim("  yet. Render (and only then upload) the ones you decide to keep."))
    return record(ctx, NAME[PREVIEW], RAN, started,
                  "%d trip(s), %d still(s) in %s" % (len(trips), len(stills), previews_dir))


# ---------------------------------------------------------------------------
# Drop a trip from the import — DESTRUCTIVE
# ---------------------------------------------------------------------------

def _print_trip_table(ctx, root, trips):
    """List the trips, and return them by index."""
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
    return by_index


def _ask_trip_indices(by_index):
    """The indices to drop, or None when the answer was not one."""
    sel = ask("  Trip indices to DROP (space separated, blank = cancel): ")
    if not sel.strip():
        return None
    return _parse_indices(sel, by_index)


def _parse_indices(sel, by_index):
    picked = []
    for part in re.split(r"[,\s]+", sel.strip()):
        if not part.isdigit() or int(part) not in by_index:
            print(C.red("  %r is not one of the listed trip indices." % part))
            return None
        if int(part) not in picked:
            picked.append(int(part))
    return picked


def _renders_of(ctx, payload, by_index, picked):
    """Every file that belongs to a picked trip's existing render.

    An already-rendered trip is not refused. Refusing would be the wrong answer
    to "this trip is bad, remove it": the render is the thing you most want
    gone, and you only find out it is bad by watching it, which happens after
    rendering. So the render comes too — the mp4 and every sidecar beside it.
    """
    out = []
    for i in picked:
        same, _other = trip_renders(ctx, payload, by_index[i])
        for mp4 in same:
            out.extend(sidecar_set(mp4))
    return out


def _note_renders_on_s3(world, render_files):
    """Local deletion is not unpublishing. Say so rather than letting a clean
    local result imply the trip is gone from the world."""
    names = world.bucket.names()
    up = [f.name for f in render_files if f.suffix == ".mp4"
          and any(f.name in k for k in names)]
    if not up:
        return
    print(C.red("  NOTE: %d of these are already on S3 and stay there." % len(up)))
    print(C.dim("  Deleting locally does not remove them from the bucket or"))
    print(C.dim("  from the site. Rebuild and redeploy (%d, %d) after this, and"
                % (BUILD, UPLOAD)))
    print(C.dim("  remove the object from the bucket if you want it truly gone."))


def _only_copy_lines(ctx, world, payload, by_index, picked):
    """The last-copy warning, and an honest account of what was checked.

    Three states, three sentences: the bucket was never asked (those trips have
    no render name to look for), the bucket could not be read, or there is no
    bucket. Saying "not consulted" when the listing actually failed is a lie in
    a delete prompt.
    """
    only_copy, elsewhere, consulted = [], [], [False]
    for i in picked:
        (elsewhere if _exists_elsewhere(ctx, world, payload, by_index[i], consulted)
         else only_copy).append(i)
    lines = []
    if elsewhere:
        lines.append(C.dim("  Trip(s) %s also exist as a render elsewhere or on S3."
                           % ", ".join(str(i) for i in elsewhere)))
    if only_copy:
        lines.extend(_last_copy_banner(world, only_copy, consulted[0]))
    return tuple(lines)


def _exists_elsewhere(ctx, world, payload, trip, consulted):
    _same, other = trip_renders(ctx, payload, trip)
    if other:
        return True
    return _on_the_bucket(world, trip, consulted)


def _on_the_bucket(world, trip, consulted):
    base = trip.get("out_base")
    if not base:
        return False            # no render name to look for; nothing to ask
    consulted[0] = True
    return any(Path(base).name in k for k in world.bucket.names())


def _last_copy_banner(world, only_copy, consulted):
    bar = C.red("  " + "!" * (term_width() - 4))
    lines = [bar,
             C.red("  Trip(s) %s are NOT rendered anywhere and NOT on S3."
                   % ", ".join(str(i) for i in only_copy)),
             C.red("  These files are the ONLY copy of that footage. Deleting them"),
             C.red("  ends it — there is nothing to restore from, here or online.")]
    lines.extend(_why_unchecked(world, consulted))
    lines.append(bar)
    return lines


def _why_unchecked(world, consulted):
    if not consulted:
        return [C.red("  (The S3 bucket was not consulted: those trips have no render"),
                C.red("   name to look for, so no object could exist for them.)")]
    return _bucket_caveat(world)


def _bucket_caveat(world):
    if isinstance(world.bucket, W.Unlistable):
        return [C.red("  (The bucket listing FAILED, so 'not on S3' is unverified —"),
                C.red("   an unknown is not evidence of a copy.)")]
    if isinstance(world.bucket, W.NoBucket):
        return [C.red("  (No s3_bucket is configured, so nothing of this is off"),
                C.red("   this machine.)")]
    return []


def drop_plan(ctx, world):
    """Item 4's plan: choose the trips, show exactly what goes, hand back the act.

    Everything irreversible is inside `act`, which only ever receives the world
    captured after the word is typed. This function may print and ask; it may
    not delete.
    """
    started = time.time()
    root = pick_import(ctx, "dropping a trip")
    if root is None:
        return menu.Plan.nothing_to_do("no import folder")
    payload = load_groups(ctx, root)
    if payload is None:
        return menu.Plan.nothing_to_do("--print-groups failed")
    trips = payload.get("trips", [])
    if not trips:
        print(C.yellow("  No trips in %s — nothing to drop." % root))
        return menu.Plan.nothing_to_do("no trips")
    by_index = _print_trip_table(ctx, root, trips)
    picked = _ask_trip_indices(by_index)
    if not picked:
        return menu.Plan.nothing_to_do("cancelled")
    return _drop_plan_for(ctx, world, payload, by_index, picked, started)


def _drop_plan_for(ctx, world, payload, by_index, picked, started):
    render_files = _renders_of(ctx, payload, by_index, picked)
    if render_files:
        print()
        print(C.yellow("  Already rendered. The render goes too, %d file(s):"
                       % len(render_files)))
        _note_renders_on_s3(world, render_files)
        for f in render_files[:8]:
            print(C.dim("      %s" % tilde(f)))
        if len(render_files) > 8:
            print(C.dim("      ... and %d more" % (len(render_files) - 8)))

    files = [p for i in picked for p in trip_files(by_index[i])] + render_files
    total = sum(_size_of(p) for p in files)
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

    banner = _only_copy_lines(ctx, world, payload, by_index, picked)
    return menu.Plan(menu.nothing_to_recheck,
                     lambda fresh: _drop_commit(ctx, picked, by_index, files,
                                                render_files, started),
                     banner=banner)


def _size_of(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _drop_commit(ctx, picked, by_index, files, render_files, started):
    """The irreversible half. Everything above this line only printed."""
    deleted, freed, errors = _unlink_all(files)
    for e in errors[:10]:
        print(C.red("  could not delete %s" % e))

    # Record the dropped clips' stamps as excluded. From here on they are
    # treated as if imported: the next delta import does not re-copy them off
    # the card, and item 9 counts them as accounted for — the warning above was
    # the decision, made once, at the only moment it can matter.
    dropped_stamps = {m.group(1) for p in files
                      for m in [STAMP_RE.search(p.name)] if m}
    if dropped_stamps:
        record_excluded_stamps(ctx, dropped_stamps)
        print(C.dim("  Recorded %d excluded clip stamp(s); the delta import will not"
                    " re-copy them." % len(dropped_stamps)))

    # Any cached view of this import is now wrong: the grouping is computed from
    # the clips that just stopped existing.
    ctx.last_groups = None
    ctx.last_scan = None

    _drop_orphan_sidecars(by_index, picked)
    _record_curation(ctx, by_index, picked, render_files)
    _rebuild_manifest(ctx, render_files)

    if errors:
        return _outcome(record(ctx, NAME[EXCLUDE], FAILED, started,
                               "%d of %d file(s) deleted, %d error(s)"
                               % (deleted, len(files), len(errors))))
    print(C.green("  Dropped trip(s) %s: %d file(s), %s freed." % (
        ", ".join(str(i) for i in picked), deleted, human_bytes(freed))))
    return _outcome(record(ctx, NAME[EXCLUDE], RAN, started,
                           "trip(s) %s, %d file(s), %s freed" % (
                               ", ".join(str(i) for i in picked), deleted,
                               human_bytes(freed))))


def _unlink_all(files):
    deleted, freed, errors = 0, 0, []
    for p in files:
        n = _size_of(p)
        try:
            p.unlink()
            deleted += 1
            freed += n
        except OSError as e:
            errors.append("%s: %s" % (p, e))
    return deleted, freed, errors


def _drop_orphan_sidecars(by_index, picked):
    """Sidecars of trips that are now gone.

    They are not source footage — they describe something that no longer
    exists, and left in place build_manifest keeps publishing a trip whose
    video can never be rendered. For a trip that WAS rendered these went with
    the render; what this catches is the preview-only case.
    """
    orphans = []
    for i in picked:
        base = by_index[i].get("out_base")
        if base:
            orphans.extend(_existing_sidecars(base))
    if not orphans:
        return
    print()
    print("  %d preview sidecar(s) now describe a trip that no longer exists:"
          % len(orphans))
    for p in orphans:
        print(C.dim("    %s" % p))
    if confirm("  Remove them too (they are derived data, not footage)?", False):
        _unlink_all(orphans)
        print(C.dim("  Removed. The next %s or %s drops them from the site index."
                    % (NAME[META], NAME[RENDER])))


def _existing_sidecars(base):
    paths = (Path(base + suffix)
             for suffix in (".html", ".gpx", "_links.txt", "_meta.json"))
    return [p for p in paths if p.is_file()]


def _record_curation(ctx, by_index, picked, render_files):
    """Exclude the trip from the site's curation.

    Deleting the files is NOT enough: build_manifest deliberately carries a
    previously-published trip forward when its local output is gone, because
    that is what makes "delete local after publish" safe. A dropped trip and a
    cleaned-up published trip look identical to it — uid in the previous
    manifest, nothing on disk — so no rebuild can tell them apart. The excluded
    list is the one place that says which of the two this is.

    Written to the LOCAL admin.json. It is not the last word: deploy-site.sh
    pulls the live server's state over this file first, so the live site keeps
    showing the trip until the same exclusion is made there.
    """
    if not (render_files and ctx.site):
        return
    admin = ctx.site / "admin.json"
    # The BASE name, not the uid path: build_manifest matches the excluded list
    # against the trip's id in two places, and the path-style uid matches
    # neither, so an exclusion written that way is silently ignored.
    uids = [Path(by_index[i]["out_base"]).name for i in picked
            if by_index[i].get("out_base")]
    added = _add_exclusions(admin, uids)
    if not added:
        return
    print()
    print(C.dim("  Excluded %d trip(s) in %s, so the next build omits them."
                % (added, tilde(admin))))
    print(C.yellow("  The LIVE site still shows them: deploy pulls the server's"))
    print(C.yellow("  curation over this file. Exclude them in the site's admin"))
    print(C.yellow("  view too, or this is undone on the next deploy."))
    for uid in uids:
        print(C.dim("    %s" % uid))


def _add_exclusions(admin, uids):
    try:
        state = json.loads(admin.read_text()) if admin.is_file() else {}
    except Exception:
        state = {}
    ex = state.setdefault("excluded", [])
    added = 0
    for uid in uids:
        if not any(x.get("id") == uid for x in ex):
            ex.append({"id": uid, "title": "", "day": "",
                       "mode": "delete", "at": int(time.time())})
            added += 1
    if added:
        admin.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return added


def _rebuild_manifest(ctx, render_files):
    """Rebuild trips.json so the drop reaches the thing that publishes.

    This is the one place outside Upload Website that touches the site repo,
    and it stays here on purpose: build_manifest cannot otherwise distinguish
    "dropped" from "cleaned up after publishing", so recording the drop where
    the manifest can see it is part of dropping the trip, not a second job.
    """
    if not render_files:
        return
    if not ctx.site_script("build_manifest.py"):
        _warn_no_manifest_builder(ctx)
        return
    print()
    rc, _lines = run_stream(["python3", "build_manifest.py"], ctx.site, "Indexing",
                            keep=lambda l: l.startswith("wrote trips.json"))
    if rc != 0:
        print(C.red("  build_manifest.py exited %d — trips.json still lists the"
                    " dropped trip." % rc))
        return
    print(C.dim("  trips.json rebuilt without it. Run %d) %s to put that live —"
                % (UPLOAD, NAME[UPLOAD])))
    print(C.dim("  until then the site still serves the old index."))


def _warn_no_manifest_builder(ctx):
    if ctx.site is None:
        return
    print(C.yellow("  No build_manifest.py under %s — trips.json was NOT updated."
                   % tilde(ctx.site)))


def _clear_intermediates(ctx):
    """Empty <out>/.intermediates — per-clip scratch encodes, nothing else.

    Only files inside that one dot-directory are touched: everything in it is
    scratch a render writes and a finished render has consumed. The directory
    itself stays, so the next render does not have to recreate it.
    """
    inter = ctx.out_dir / ".intermediates"
    removed = 0
    if inter.is_dir():
        for f in sorted(inter.rglob("*")):
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        for d in sorted(inter.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    return removed


def recover_aborted_render(ctx):
    """Detect an aborted render and clean up what it left behind.

    Two kinds of debris, both provably not deliverables: *.mp4.part files
    (an encode that never finished — the finished file has no .part suffix)
    and the scratch frames in .intermediates/. Left in place, a .part sits in
    the render tree looking like a video to anything globbing loosely, and
    the scratch inflates the working area Clean Workspace has to reason about.
    Says what it removed, like every other path here that deletes.
    """
    removed = []
    if ctx.out_dir.is_dir():
        for p in sorted(ctx.out_dir.rglob("*.part")):
            if p.is_file():
                try:
                    p.unlink()
                    removed.append(p)
                except OSError:
                    pass
    scratch = _clear_intermediates(ctx)
    if removed or scratch:
        print(C.dim("  Aborted render cleaned up: %d partial file(s), %d scratch file(s)."
                    % (len(removed), scratch)))
        for p in removed[:6]:
            print(C.dim("    removed %s" % tilde(p)))
    return len(removed) + scratch


def after_render(ctx):
    """A finished render leaves renders and sidecars, not intermediates."""
    n = _clear_intermediates(ctx)
    if n:
        print(C.dim("  Cleared %d scratch file(s) from .intermediates." % n))
    return n


def step_render(ctx):
    """Encode trips to mp4 + sidecars (make-trips-rendered.sh)."""
    started = time.time()
    root = pick_import(ctx, "rendering")
    if root is None:
        return record(ctx, NAME[RENDER], SKIPPED, started, "no import folder")
    # A previous render that died mid-encode leaves .part files and scratch
    # frames; start clean so nothing half-written is mistaken for output.
    recover_aborted_render(ctx)

    # Show the trips here rather than making him remember them from the listing or go
    # back for them. The grouping comes from --print-groups (cached, so this is
    # instant once the boundaries are known) — the same source Exclude Trip
    # deletes by, so what is listed is exactly what would be rendered.
    payload = load_groups(ctx, root)
    groups = (payload or {}).get("trips") or []
    tot_span = tot_move = 0.0     # also read by the height estimates below
    if groups:
        print()
        have_move = False
        for g in groups:
            mark = "  " if g.get("renderable", True) else C.dim(" -")
            secs = g.get("duration_secs") or 0
            span = human_secs(secs)
            # The encoded length is not the span: parking is cut out. The real
            # figure lives in the sidecar _meta.json, so it is only known once
            # Generate Meta has run — show it when it is there and say nothing
            # when it is not, rather than estimating.
            meta = trip_meta(g) or {}
            move_min = meta.get("moving_min")
            if g.get("renderable", True):
                tot_span += secs
                if move_min is not None:
                    tot_move += float(move_min) * 60.0
                    have_move = True
            movecol = ("  -> %s video" % human_secs(float(move_min) * 60.0)
                       if move_min is not None else "")
            line = "%s%2d) %s  %s -> %s  %3d clips  %s%s" % (
                mark, g["index"], g.get("day", ""),
                str(g.get("start", ""))[11:16], str(g.get("end", ""))[11:16],
                g.get("clips", 0), span, C.bold(movecol))
            if not g.get("renderable", True):
                line += C.dim("  auto-skipped: %s" % (g.get("reason") or "fragment"))
            print(line)
        if have_move:
            print("      total %s span  ->  %s of video to encode"
                  % (human_secs(tot_span), C.bold(human_secs(tot_move))))
            print(C.dim("      parking inside a trip is cut"))
        else:
            print(C.dim("  span is start->end. The encode is shorter — parking is cut — but by"))
            print(C.dim("  how much is only known after %d) %s writes the sidecars."
                        % (META, NAME[META])))
        print()
    elif ctx.last_scan and ctx.last_scan.root == root:
        print("  Last scan: %d trips, %d renderable%s" % (
            ctx.last_scan.total, ctx.last_scan.renderable,
            (", auto-skipped %s" % sorted(ctx.last_scan.skipped)) if ctx.last_scan.skipped else ""))

    # Which trips already have a video. Blank means "the ones with no video" —
    # NOT "all renderable", which on an import that is already rendered would
    # mean: clear the day folders (a full render does that, by design, so a
    # re-group cannot leave stale trip_* behind), delete every finished mp4, and
    # encode them all again. Hours, to arrive back where you started.
    # Re-running after a partial render is what blank is for, and passing
    # explicit indices also makes the run resume-like so the day clean is
    # skipped.
    done_idx, todo_idx = [], []
    for g in groups:
        if not g.get("renderable", True):
            continue
        base = g.get("out_base")
        have = False
        if base:
            b = Path(base)
            have = any(b.parent.glob(b.name + "_h*.mp4"))
        (done_idx if have else todo_idx).append(g["index"])
    if done_idx:
        print()
        print("  Already rendered: %s" % C.green(", ".join(str(i) for i in done_idx)))
        if todo_idx:
            print("  Not yet rendered: %s" % C.bold(", ".join(str(i) for i in todo_idx)))
            print(C.dim("  Blank renders only those. Naming a rendered trip re-encodes it."))
        else:
            print(C.dim("  Every renderable trip has a video. Blank does nothing; name"))
            print(C.dim("  trips explicitly to re-encode them."))

    idx = ask("  Trip indices to render (space separated, blank = %s): "
              % ("the %d not yet rendered" % len(todo_idx) if done_idx and todo_idx
                 else "nothing to do" if done_idx else "all renderable"))
    if not idx.strip() and done_idx:
        if not todo_idx:
            print(C.dim("  Nothing to render."))
            # Every renderable trip has its mp4: the postcondition holds.
            return record(ctx, NAME[RENDER], SATISFIED, started, "all trips already rendered")
        idx = " ".join(str(i) for i in todo_idx)
        print(C.dim("  Rendering %s." % idx))
    # Offer the heights that are actually worth choosing, with what each costs.
    # Bytes scale roughly with pixel count, so halving the height quarters the
    # area — but real footage does not compress linearly, so treat these as the
    # order of magnitude they are. The rate below is measured: 1h47m of this card
    # came out at 13.0 GB at crf 23, and crf 26 (the default now) is about half
    # that, giving ~3.6 GB per hour of video at 1080.
    # Standard rungs. 540 and 360 are exact halves and thirds of 1080, which
    # keeps the scaling clean; an odd height like 380 buys nothing over 360 and
    # is not a size anything else uses. Any number is still accepted — the list
    # is a shortcut, not a restriction.
    OPTS = [(1080, "native 2402x1080, full detail"),
            (720,  "still reads plates and signs"),
            (540,  "half size, fine on a laptop"),
            (360,  "phone only, plates unreadable")]
    vid_secs = tot_move          # 0 until Generate Meta has written the sidecars
    print()
    print("  Output height:")
    for h, why in OPTS:
        est = ""
        if vid_secs:
            gb = 3.6 * (vid_secs / 3600.0) * (h / 1080.0) ** 2
            est = "  ~%.1f GB" % gb
        mark = C.bold(" <- default") if h == ctx.output_height else ""
        print("    %4d  %-30s%s%s" % (h, why, C.bold(est), mark))
    if vid_secs:
        print(C.dim("        estimates for %s of video, at the current crf" % human_secs(vid_secs)))
    print(C.dim("        or type any height"))
    height = ask("  Height [%d]: " % ctx.output_height, str(ctx.output_height))
    try:
        height = int(height)
    except ValueError:
        print(C.red("  Not a number."))
        return record(ctx, NAME[RENDER], SKIPPED, started, "bad height")

    before = set(rendered_mp4s(ctx.out_dir))

    # Idempotence: a render replaces its output rather than adding to whatever
    # survived. Partial state is the failure mode worth designing out — a run
    # interrupted halfway, or files deleted by hand, leaves a namespace that
    # LOOKS rendered while missing pieces, and nothing downstream can tell the
    # difference: the manifest indexes what it finds, the upload syncs what
    # exists, and the site shows the result.
    #
    # Rendering everything clears the whole import namespace. Rendering a subset
    # clears only those trips' files, because wiping the namespace would delete
    # renders the run is not going to recreate.
    # ONLY the mp4s. A render replaces the video it produces — it does not own
    # the .html map, the .gpx, the _meta.json or the _links.txt sitting beside
    # them. Those come from the sidecar pass, they are what the contact sheet and
    # the site read, and deleting them to re-make a video throws away work the
    # render was not asked to touch. (An interrupted render would have taken them
    # with it and left nothing.)
    ns = ctx.out_dir / root.name
    def _videos(paths):
        return [f for f in paths if f.is_file() and f.suffix == ".mp4"]
    if idx.strip():
        picked = {int(n) for n in idx.split() if n.isdigit()}
        bases = [g.get("out_base") for g in groups if g.get("index") in picked]
        doomed = _videos([f for b in bases if b
                          for f in Path(b).parent.glob(Path(b).name + "*")])
        what = "the mp4(s) of %d trip(s)" % len(bases)
    else:
        doomed = _videos(ns.rglob("*")) if ns.is_dir() else []
        what = "every mp4 under %s" % tilde(ns)
    if doomed:
        size = sum(f.stat().st_size for f in doomed if f.exists())
        print()
        print(C.yellow("  Replacing existing output: %s" % what))
        print(C.yellow("  %d file(s), %s — deleted first so the result is exactly this run"
                       % (len(doomed), human_bytes(size))))
        print(C.dim("  Maps, GPX and metadata beside them are left alone; only video goes."))
        if not confirm("  Delete and re-render?", True):
            return record(ctx, NAME[RENDER], SKIPPED, started, "declined the clean")
        for f in doomed:
            try:
                f.unlink()
            except OSError:
                pass
        # drop the day folders left empty, so the tree matches what exists
        if ns.is_dir():
            for d in sorted(ns.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        before = set(rendered_mp4s(ctx.out_dir))

    cmd = ["./make-trips-rendered.sh"]
    cmd += idx.split()                       # bare integers become --drives
    cmd += ["--root", str(root), "--out", str(ctx.out_dir), "--output-height", str(height)] + ctx.config_args
    # No confirmation here. Choosing the trips, the height and (when there is
    # output to replace) the clean are three deliberate answers already; asking a
    # fourth time with the command spelled out is the same decision again. The
    # renderer records its own argv at the top of the run log, so what ran is
    # still written down — just not asked about.
    print(C.dim("  %s" % " ".join(cmd)))

    rc, _lines = run_stream(cmd, ctx.exporter, "Render", parser=make_render_parser(),
                            keep=lambda l: l.startswith("[Trip ") or l.strip().startswith("✓ "))
    after = set(rendered_mp4s(ctx.out_dir))
    new = after - before
    if rc != 0:
        return record(ctx, NAME[RENDER], FAILED, started,
                      "exit %d (%d new mp4 before the failure)" % (rc, len(new)))
    detail = "%d new mp4, %s" % (len(new), human_bytes(sum(p.stat().st_size for p in new)))
    # A finished render leaves renders and sidecars, not scratch.
    after_render(ctx)

    # No manifest rebuild here any more. Rebuilding trips.json is the site
    # repo's index, which is Upload Website's business, and deploy-site.sh
    # re-indexes as its own first act — so the publish path never carried a
    # stale manifest because of this. One item, one job. (Exclude Trip still
    # rebuilds, for a reason that is not convenience: it is the only place
    # that can tell build_manifest a trip was DROPPED rather than cleaned up.)
    return record(ctx, NAME[RENDER], _render_status(new), started, detail)


def _render_status(new):
    """Producing nothing is not the same as rendering something.

    The renderer can exit 0 having written no new mp4 — a run that was asked
    for trips it had already done, or that skipped everything. That used to be
    reported as a completed render, which would advance the pipeline on a
    no-op.
    """
    if new:
        return RAN
    return SATISFIED


# ---------------------------------------------------------------------------
# Site: a browsable static site built from what the render already produced
# ---------------------------------------------------------------------------
#
# Nothing here computes anything new. Every number, map and track already exists
# on disk as a sidecar next to the mp4; this pass only arranges them into pages.
# That is deliberate: the site has to be buildable by someone who has no S3
# account, no second repo and no manifest — so it reads the output tree and
# nothing else. It never calls build_manifest, never lists a bucket, never opens
# admin.json. If those things are absent the site is still complete.
#
# Videos are referenced in place, not copied: a full card is tens of gigabytes
# and duplicating it to make site/ self-contained would cost more disk than the
# footage is worth. The consequence is that site/ is portable only together with
# the render tree above it — copy <out> wholesale and the relative paths hold.


RE_TRIP_VIDEO = re.compile(r"^(?P<label>.+)_h\d+\.mp4$")
RE_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _trip_part(name):
    """A rendered file name -> (trip label, which kind of file), or None.

    The renderer names every artefact of a trip trip_<label>.<something>, so the
    label is the join key and this is the only place that knows how to recover
    it. The video is matched by pattern rather than by a literal _h1080 because
    output_height is a config setting: a tree rendered at 720 must not silently
    become a site with no videos in it.
    """
    if not name.startswith("trip_"):
        return None
    stem = name[len("trip_"):]
    for suffix, kind in (("_meta.json", "meta"), ("_links.txt", "links"),
                         (".gpx", "gpx"), (".html", "map")):
        if stem.endswith(suffix):
            return stem[:-len(suffix)], kind
    m = RE_TRIP_VIDEO.match(stem)
    if m:
        return m.group("label"), "video"
    return None


def _slug(label):
    """Label -> a file name safe on any host. Labels are already tame, but they
    come from the day and clock of a recording and this is what the URLs are
    made of, so it is not a place to trust an assumption."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", label)


def _rel_url(target, base_dir):
    """Relative URL from a generated page to a file on disk.

    Quoted, because these end up in href/src: a space or a '#' in a directory
    name would otherwise truncate the link, and the failure looks like a missing
    file rather than a bad URL. Forward slashes always — a URL is not a path.
    """
    rel = os.path.relpath(str(target), str(base_dir))
    return urllib.parse.quote(Path(rel).as_posix())


def collect_site_trips(out_dir, site_dir):
    """Every trip in the render tree, newest day first, newest trip first.

    Grouped by directory as well as by label so that two imports that happen to
    contain a same-named trip cannot merge into one. A trip is anything with at
    least one artefact — a trip whose mp4 was never rendered still has a map and
    stats worth showing, and a trip whose meta.json is missing still has a video.
    """
    found = {}
    site_dir = site_dir.resolve() if site_dir.exists() else site_dir
    for dirpath, dirnames, filenames in os.walk(str(out_dir)):
        d = Path(dirpath)
        # Dot directories hold the renderer's per-clip scratch encodes; the site
        # directory holds our own output. Neither is input.
        dirnames[:] = sorted(x for x in dirnames if not x.startswith("."))
        if d == site_dir or site_dir in d.parents:
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            part = _trip_part(name)
            if part is None:
                continue
            label, kind = part
            t = found.setdefault((str(d), label), {
                "label": label, "dir": d, "meta": {},
                "video": None, "map": None, "gpx": None, "links": None,
                "still": None,
            })
            t[kind if kind != "meta" else "meta_path"] = d / name

    trips = []
    for t in found.values():
        mp = t.pop("meta_path", None)
        if mp is not None:
            try:
                t["meta"] = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A half-written meta.json must not take the whole site down; the
                # trip still has a video and a map worth linking.
                t["meta"] = {}
        meta = t["meta"]
        m = RE_DAY.search(t["label"])
        t["day"] = meta.get("day") or (m.group(1) if m else t["dir"].name)
        t["start"] = meta.get("start") or ""
        t["import"] = t["dir"].parent.name
        trips.append(t)

    # Newest DAY first — the day you just drove belongs at the top — but within a
    # day, earliest first. The drives are numbered in the order they happened, so
    # listing them newest-first put Drive 2 above Drive 1 and made the numbering
    # read as a mistake. A day is a sequence; the list of days is not.
    trips.sort(key=lambda t: (t["start"], t["label"]))
    trips.sort(key=lambda t: t["day"], reverse=True)

    # Page names come from the label; only if two directories produced the same
    # label does the import folder have to appear, and then only for those two.
    counts = {}
    for t in trips:
        counts[_slug(t["label"])] = counts.get(_slug(t["label"]), 0) + 1
    for t in trips:
        s = _slug(t["label"])
        t["slug"] = s if counts[s] == 1 else "%s-%s" % (_slug(t["import"]), s)
        t["page"] = "trip-%s.html" % t["slug"]
    return trips


def _jpeg_size(path):
    """(width, height) of a jpeg, or None. Twenty lines of stdlib instead of a
    dependency: the pages only need it so the browser can reserve the right box
    for an image it has not fetched yet, and being wrong is a layout jump, not a
    wrong number. Walks the segment headers to the frame header (SOF) and reads
    the two 16-bit fields out of it."""
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return None
            while True:
                b = fh.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                marker = fh.read(1)
                while marker == b"\xff":        # fill bytes are legal padding
                    marker = fh.read(1)
                if not marker:
                    return None
                m = marker[0]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    continue                    # no payload on these
                head = fh.read(2)
                if len(head) < 2:
                    return None
                length = (head[0] << 8) + head[1]
                # Every SOF marker but DHT/DAC/DNL carries size at the same offset.
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    data = fh.read(7)
                    if len(data) < 5:
                        return None
                    return ((data[3] << 8) + data[4], (data[1] << 8) + data[2])
                fh.seek(length - 2, os.SEEK_CUR)
    except OSError:
        return None


def parse_links_file(path):
    """trip_*_links.txt -> [(label, url)].

    The file is written for a human to read, so the label of a link is the line
    above it. Anything that does not parse is dropped rather than guessed at —
    the map and the .gpx are linked separately and do not depend on this.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, label, seen = [], "", set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("http"):
            if line not in seen:
                seen.add(line)
                out.append((label or "map link", line))
        elif line.endswith(":"):
            label = line[:-1].strip()
    return out


# ---------------------------------------------------------------------------
# The result page: one self-contained file in the output dir.
#
# A drive's most characteristic artifact is not a video frame — every dashcam
# frame looks like every other dashcam frame — it is the track. The shape of
# where you went is unique to that drive, so it is what identifies a trip here,
# drawn from the .gpx and coloured by the speed the renderer already records per
# point. The colours are its own legend (<20 / 20-40 / 40-60 / 60-80 / >80 km/h),
# not an invented palette, so the glyph reads the same way as the map burned into
# the video.
#
# Everything is inline — stills as data URIs, no fonts, no scripts, no network —
# because the file's whole job is to be openable and sendable on its own.
# ---------------------------------------------------------------------------

SPEED_BANDS = [(20, "#4FC3F7"), (40, "#7DD3A0"), (60, "#E8C547"), (80, "#E8874A"), (10**9, "#E05252")]


def _band(kmh):
    for ceiling, colour in SPEED_BANDS:
        if kmh < ceiling:
            return colour
    return SPEED_BANDS[-1][1]


def gpx_track(path):
    """[(lat, lon, km/h)] from a .gpx the renderer wrote. Speed is per point in
    m/s under <extensions>; absent points fall back to 0 rather than guessing."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    pts = []
    for m in re.finditer(r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)"(.*?)</trkpt>', raw, re.S):
        lat, lon, rest = float(m.group(1)), float(m.group(2)), m.group(3)
        sp = re.search(r"<speed>([-\d.]+)</speed>", rest)
        pts.append((lat, lon, (float(sp.group(1)) * 3.6) if sp else 0.0))
    return pts


def route_glyph(pts, w=560, h=280, pad=14, speed_colour=True):
    """The track as an SVG, speed-coloured, aspect-correct.

    Longitude degrees shrink with latitude, so scaling lat and lon by the same
    factor would stretch every route east-west. cos(lat) corrects it; without
    that a city loop comes out looking like a motorway sprint.
    """
    if len(pts) < 2:
        return ""
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    la0, la1, lo0, lo1 = min(lats), max(lats), min(lons), max(lons)
    kx = math.cos(math.radians((la0 + la1) / 2.0)) or 1e-6
    spanx = max((lo1 - lo0) * kx, 1e-9)
    spany = max(la1 - la0, 1e-9)
    s = min((w - 2 * pad) / spanx, (h - 2 * pad) / spany)
    ox = (w - spanx * s) / 2.0
    oy = (h - spany * s) / 2.0

    def xy(p):
        return (ox + (p[1] - lo0) * kx * s, h - (oy + (p[0] - la0) * s))

    # One colour draws the route as a shape; speed colouring makes it a reading of
    # the drive. Off is a legitimate preference, so it is a setting.
    plain = "#7DD3A0"
    segs = []
    run, colour = [xy(pts[0])], (_band(pts[0][2]) if speed_colour else plain)
    for p in pts[1:]:
        c = _band(p[2]) if speed_colour else plain
        run.append(xy(p))
        if c != colour:
            segs.append((colour, run))
            run, colour = [run[-1]], c
    segs.append((colour, run))

    out = ['<svg class="glyph" viewBox="0 0 %d %d" role="img" aria-label="route">' % (w, h)]
    for colour, run in segs:
        if len(run) < 2:
            continue
        d = " ".join("%s%.1f %.1f" % ("M" if i == 0 else "L", x, y) for i, (x, y) in enumerate(run))
        out.append('<path d="%s" fill="none" stroke="%s" stroke-width="2.4" '
                   'stroke-linecap="round" stroke-linejoin="round"/>' % (d, colour))
    sx, sy = xy(pts[0]); ex, ey = xy(pts[-1])
    out.append('<circle cx="%.1f" cy="%.1f" r="4.5" class="ptA"/>' % (sx, sy))
    out.append('<circle cx="%.1f" cy="%.1f" r="4.5" class="ptB"/>' % (ex, ey))
    out.append("</svg>")
    return "".join(out)


def still_data_uri(mp4, seconds=2.0, width=760):
    """A frame as a data: URI. Deliberately smaller than the poster stills — this
    one is inlined into a file meant to be sent around, and full-width frames
    would make it tens of MB."""
    if not mp4 or not Path(mp4).is_file():
        return ""
    tmp = Path(tempfile.gettempdir()) / ("dcsite-%d.jpg" % os.getpid())
    for ss in (seconds, 0):
        r = subprocess.run(["ffmpeg", "-y", "-ss", str(ss), "-i", str(mp4), "-frames:v", "1",
                            "-vf", "scale='min(%d\\,iw)':-2" % width, "-q:v", "6",
                            str(tmp), "-loglevel", "error"], capture_output=True)
        if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size:
            b = base64.b64encode(tmp.read_bytes()).decode("ascii")
            try:
                tmp.unlink()
            except OSError:
                pass
            return "data:image/jpeg;base64," + b
    return ""

RESULT_CSS = """
:root{
  --ink:#0F131A; --panel:#161B24; --edge:rgba(255,255,255,.09);
  --fg:#E7EBF1; --dim:#8B96A6; --faint:#5C6675;
  --s1:#4FC3F7; --s2:#7DD3A0; --s3:#E8C547; --s4:#E8874A; --s5:#E05252;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--fg);font-family:var(--sans);
  -webkit-font-smoothing:antialiased;line-height:1.5}
.wrap{max-width:1040px;margin:0 auto;padding:44px 20px 80px}

/* masthead: the aggregate is telemetry, so it is set as telemetry */
h1{font-size:26px;font-weight:650;letter-spacing:-.02em;margin:0}
.sub{color:var(--dim);font-size:14px;margin:6px 0 0}
.totals{display:flex;flex-wrap:wrap;gap:26px;margin:26px 0 8px;
  padding:18px 0;border-top:1px solid var(--edge);border-bottom:1px solid var(--edge)}
.tot .n{font-family:var(--mono);font-size:22px;letter-spacing:-.02em}
.tot .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);margin-top:3px}

/* a trip: the frame on the left, its shape and its numbers on the right */
.trip{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:22px;
  padding:26px 0;border-bottom:1px solid var(--edge);align-items:start}
.shot{width:100%;display:block;border-radius:3px;background:#000}
.noshot{aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;
  border:1px dashed var(--edge);border-radius:3px;color:var(--faint);
  font-family:var(--mono);font-size:12px}
.when{font-family:var(--mono);font-size:12.5px;color:var(--dim);letter-spacing:.02em}
.title{font-size:17px;font-weight:600;margin:2px 0 12px;letter-spacing:-.01em}
.rt{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--s2);border:1px solid var(--s2);border-radius:2px;padding:1px 6px;margin-left:8px;
  vertical-align:2px}
.glyph{width:100%;height:auto;display:block;background:rgba(255,255,255,.02);
  border:1px solid var(--edge);border-radius:3px}
.ptA{fill:var(--s2)} .ptB{fill:var(--s5)}
.nogps{font-family:var(--mono);font-size:12px;color:var(--faint);
  border:1px dashed var(--edge);border-radius:3px;padding:22px;text-align:center}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 10px;margin-top:14px}
.cell .n{font-family:var(--mono);font-size:15px}
.cell .k{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);margin-top:2px}
.links{margin-top:16px;display:flex;flex-wrap:wrap;gap:8px}
.links a{font-family:var(--mono);font-size:11.5px;letter-spacing:.06em;color:var(--fg);
  text-decoration:none;border:1px solid var(--edge);border-radius:2px;padding:5px 10px}
.links a:hover{border-color:var(--dim)}
.links a:focus-visible{outline:2px solid var(--s1);outline-offset:2px}
.key{display:flex;gap:14px;flex-wrap:wrap;margin:30px 0 0;color:var(--faint);
  font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase}
.key i{display:inline-block;width:16px;height:2px;vertical-align:3px;margin-right:5px}
foot,.foot{display:block;margin-top:34px;color:var(--faint);font-size:12.5px}
@media (max-width:720px){.trip{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,1fr)}}
@media (prefers-reduced-motion:no-preference){.trip{animation:in .5s both}
  @keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}}
"""

RESULT_FILE = "dashcam_import_data_site.html"
FINAL_PREFIX = "final_"


def current_import_id(ctx):
    """What identifies the import currently in the workspace, or None.

    The ledger's high-water mark: it advances exactly once per import (when
    the verified copy lands), survives every sweep, and is identical across
    re-runs within one round — which is precisely the lifetime a final_
    folder's identity needs. No new file to keep alive.
    """
    return read_ledger(ctx).get("through") or None


def final_dir_for(root, days, import_id=None):
    """<root>/final_<newest day>_<import id> — one folder PER IMPORT.

    Keyed on the IMPORT, not only the newest trip day. Two rules meet here:
    rebuilding the page tomorrow must land in the same folder as today (so
    the name cannot come from the render date), and a SECOND import must get
    a folder of its own even when it covers the same day (so the day alone
    cannot be the whole name). The import id — the ledger mark, constant
    across re-runs of one round and different for the next import — satisfies
    both. The day stays in the name because it is what a person recognises;
    the id's time-of-day suffix is what keeps two same-day imports apart.

    With no id to key on (a workspace whose footage arrived without the
    import step), a fresh folder is allocated rather than merging into some
    other import's: final_<day>, then final_<day>_2, and so on.
    """
    tag = max(days) if days else time.strftime("%Y-%m-%d")
    if import_id:
        sid = str(import_id)
        # A 14-digit ledger stamp reads better as its time-of-day; the date
        # half is already in the tag (or close enough to be noise).
        if re.fullmatch(r"\d{14}", sid):
            sid = sid[8:]
        return root / (FINAL_PREFIX + "%s_%s" % (tag, sid))
    cand = root / (FINAL_PREFIX + tag)
    n = 1
    while cand.exists():
        n += 1
        cand = root / (FINAL_PREFIX + "%s_%d" % (tag, n))
    return cand


def _final_dir_now(ctx, out_dir):
    """Where this round's deliverable belongs, from the days in the tree."""
    days = set()
    for child in sorted(out_dir.iterdir()):
        if child.is_dir() and not child.name.startswith(".") \
                and not child.name.startswith(FINAL_PREFIX) \
                and child.name not in ("logs", "previews"):
            days.update(d.name for d in child.iterdir()
                        if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name))
    return final_dir_for(ctx.final_root, days, current_import_id(ctx))


def no_gather(ctx, out_dir):
    """The publishing edition's gatherer: it does not gather.

    trips.json records each trip by a uid containing its import folder name, so
    moving the tree renames every published trip out from under the manifest
    and orphans them. This used to be an `if ctx.site_ready` in the middle of
    the mover; it is now which collaborator item 6's constructor installs, so
    the branch is settled once when the menu is built.
    """
    final = _final_dir_now(ctx, out_dir)
    if final.is_dir():
        return final
    return None


def gather_into_final(ctx, out_dir):
    """Move the rendered trips into final_<day>_<import> so the result is one
    folder.

    The point is a directory the user can drag anywhere: the page, the videos,
    the maps and the tracks together, with every link inside it still
    resolving. It is also what makes the local edition's workspace expendable —
    working_area_is_expendable counts a render as safe when it is in there.
    """
    final = _final_dir_now(ctx, out_dir)
    moved = 0
    kept = []
    existed = final.is_dir()
    final.mkdir(parents=True, exist_ok=True)
    if existed:
        print(C.dim("  %s already exists; merging into it." % tilde(final)))
    for child in sorted(out_dir.iterdir()):
        if child == final or child.name.startswith(".") or child.is_file():
            continue
        if child.name in ("logs", "previews") or child.name.startswith(FINAL_PREFIX):
            continue
        for day in sorted(child.iterdir()):
            if not day.is_dir():
                continue
            dest = final / day.name
            if dest.exists():
                # Already there from an earlier run: merge, never replace. A file
                # that is already in place was produced by this render or a
                # previous one, and overwriting it is the only way this step could
                # destroy anything. Anything left behind in the source is a
                # collision, and it stays put so it can be looked at.
                for f in sorted(day.iterdir()):
                    target = dest / f.name
                    if not target.exists():
                        shutil.move(str(f), str(target))
                        moved += 1
                    else:
                        kept.append(target)
            else:
                shutil.move(str(day), str(dest))
                moved += 1
        try:
            child.rmdir()          # only succeeds once it is genuinely empty
        except OSError:
            pass
    if kept:
        print(C.yellow("  %d file(s) already present were left as they were:" % len(kept)))
        for f in kept[:5]:
            print(C.yellow("    %s" % tilde(f)))
    return final if moved or any(final.iterdir()) else None


def _f(v):
    """A number from meta, or 0.0 — meta may be missing or half-written."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _trip_title(meta, t):
    """A name a person would use, never the filename.

    route_label is the good case ("Muntinlupa to Tagaytay") — the renderer sets
    it when the track passes somewhere it can name. Failing that, the trip's
    number on its day, which is how you would refer to it out loud. The internal
    label (2026-07-24_16-16_02) identifies a file, not a drive, and putting it in
    the title would tell the reader about our storage rather than their afternoon.
    """
    lbl = (meta.get("route_label") or "").strip()
    if lbl:
        return lbl
    # Drive N, per day. Several days each having a "Drive 1" is fine — the date
    # is on the line above, and this is a placeholder for a name a person gives
    # it later, not an attempt to invent one.
    n = meta.get("trip_index")
    return "Drive %d" % n if n else "Drive"


def _cell(n, k):
    return '<div class="cell"><div class="n">%s</div><div class="k">%s</div></div>' % (n, k)


def build_result_page(ctx, out_dir=None, gather=None):
    """Write RESULT_FILE into the output dir. Returns a summary dict.

    One file, no folder: it exists to be opened, and to be sent to someone who
    will open it once. A folder of assets is the wrong shape for that.

    `gather` is supplied by item 6's constructor and decides whether the
    deliverable MOVES. Defaulting to the mover keeps the local behaviour for
    anything calling this directly.
    """
    out_dir = Path(out_dir or ctx.out_dir)
    gather = gather or gather_into_final
    # Gather first, so the trips are found where the page will link to them.
    final = gather(ctx, out_dir)
    base = final if final else out_dir
    # The second argument is the directory to EXCLUDE from the walk — it exists
    # so a folder-shaped site does not index itself. There is no such folder
    # any more, and passing base here would exclude the entire tree.
    trips = collect_site_trips(base, base / "__no_such_dir__")
    page = base / RESULT_FILE
    made = {"trips": len(trips), "path": page, "no_video": 0, "no_gps": 0}
    if not trips:
        return made

    n_dist = n_move = n_secs = 0.0
    top = 0.0
    rows = []
    for t in trips:
        meta = t.get("meta") or {}
        mp4 = t.get("video")
        gpx = t.get("gpx")
        pts = gpx_track(gpx) if gpx else []
        if not mp4:
            made["no_video"] += 1
        if not pts:
            made["no_gps"] += 1

        dist = _f(meta.get("distance_km"))
        move = _f(meta.get("moving_min"))
        mx = _f(meta.get("max_kmh"))
        av = _f(meta.get("avg_kmh"))
        n_dist += dist or 0
        n_move += move or 0
        top = max(top, mx or 0)
        n_secs += _f(meta.get("duration_secs")) or 0

        shot = still_data_uri(mp4, seconds=ctx.site_still_seconds) if mp4 else ""
        # A player, not a link. "play video" navigated away from the page to a
        # bare mp4 — which is not playing it, it is leaving. The embedded still
        # becomes the poster, so the card looks identical until you press play.
        # preload="none" because six 1-2 GB videos would otherwise all start
        # fetching the moment the page opens.
        if mp4:
            left = ('<video class="shot" controls preload="none"%s src="%s"></video>'
                    % ((' poster="%s"' % shot) if shot else "", _rel_url(mp4, base)))
        else:
            left = '<div class="noshot">no video for this trip</div>'
        art = route_glyph(pts, speed_colour=ctx.speed_colour) if pts else '<div class="nogps">no GPS recorded for this trip</div>'

        rt = '<span class="rt">round trip</span>' if meta.get("round_trip") else ""
        start = str(meta.get("start") or "")
        day = (meta.get("day") or start[:10] or "")
        clock = start[11:16] if len(start) >= 16 else ""

        links = []
        if t.get("map"):
            links.append('<a href="%s">map</a>' % _rel_url(t["map"], base))
        if gpx:
            links.append('<a href="%s">gpx</a>' % _rel_url(gpx, base))

        rows.append(
            '<section class="trip">'
            '<div>%s</div>'
            '<div>'
            '<div class="when">%s%s</div>'
            '<div class="title">%s%s</div>'
            '%s'
            '<div class="grid">%s%s%s%s</div>'
            '<div class="links">%s</div>'
            '</div></section>' % (
                left,
                html.escape(day), (" &middot; " + html.escape(clock)) if clock else "",
                html.escape(_trip_title(meta, t)), rt,
                art,
                _cell("%.1f km" % dist if dist else "&mdash;", "distance"),
                _cell(human_secs(move * 60) if move else "&mdash;", "moving"),
                _cell("%.0f km/h" % mx if mx else "&mdash;", "max"),
                _cell("%.0f km/h" % av if av else "&mdash;", "avg"),
                "".join(links) or '<span class="k">nothing to link yet</span>',
            ))

    head = (
        '<div class="wrap">'
        '<h1>%d drive%s</h1>'
        '<p class="sub">Rendered on this machine. Nothing was uploaded anywhere; '
        'the videos sit beside this file.</p>'
        '<div class="totals">%s%s%s%s</div>' % (
            len(trips), "" if len(trips) == 1 else "s",
            '<div class="tot">%s</div>' % _cell("%.0f km" % n_dist, "distance"),
            '<div class="tot">%s</div>' % _cell(human_secs(n_move * 60), "moving"),
            '<div class="tot">%s</div>' % _cell(human_secs(n_secs), "span"),
            '<div class="tot">%s</div>' % _cell("%.0f km/h" % top, "top speed"),
        ))

    key = ('<div class="key">'
           '<span><i style="background:var(--s1)"></i>under 20</span>'
           '<span><i style="background:var(--s2)"></i>20&ndash;40</span>'
           '<span><i style="background:var(--s3)"></i>40&ndash;60</span>'
           '<span><i style="background:var(--s4)"></i>60&ndash;80</span>'
           '<span><i style="background:var(--s5)"></i>over 80 km/h</span>'
           '</div>')

    foot = ('<p class="foot">Each shape is that drive\'s GPS track, coloured by speed. '
            'Made with dashcam-exporter.</p></div>')

    doc = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Drives</title><style>%s</style></head><body>%s%s%s%s</body></html>"
            % (RESULT_CSS, head, "".join(rows), key, foot))
    page.write_text(doc, encoding="utf-8")
    made["bytes"] = page.stat().st_size
    return made


def step_site(ctx, gather=None):
    """Write the one-file result page into the output dir.

    Under the local product `gather` also MOVES the render tree into
    final_<day>_<import>: there is no separate gather item, and gathering is
    what makes the workspace expendable, so it lives here or nowhere.
    """
    started = time.time()
    print(C.dim("  Writes %s into %s." % (RESULT_FILE, tilde(ctx.out_dir))))
    print(C.dim("  One self-contained file: every still is embedded and every route is"))
    print(C.dim("  drawn from its .gpx, so it opens with no network and can be sent as"))
    print(C.dim("  it is. The videos are linked where they already sit, not copied."))

    if not ctx.out_dir.is_dir():
        print(C.yellow("  Nothing rendered yet: %s does not exist." % tilde(ctx.out_dir)))
        return record(ctx, NAME[BUILD], SKIPPED, started, "no output tree")

    info = build_result_page(ctx, ctx.out_dir, gather)
    if not info["trips"]:
        print(C.yellow("  No trips found under %s — render some first." % tilde(ctx.out_dir)))
        return record(ctx, NAME[BUILD], SKIPPED, started, "no trips")

    if info["no_video"]:
        print(C.dim("  %d trip(s) have no video yet; the page says so." % info["no_video"]))
    if info["no_gps"]:
        print(C.dim("  %d trip(s) have no GPS, so they show no route." % info["no_gps"]))

    print()
    print(C.green("  %s" % info["path"]))
    print("  %d drive(s), %s. Open it with:" % (info["trips"], human_bytes(info.get("bytes", 0))))
    print("    open %s" % info["path"])
    return record(ctx, NAME[BUILD], RAN, started,
                  "%d trip(s), %s" % (info["trips"], human_bytes(info.get("bytes", 0))))

def s3_objects(ctx):
    """{key: size} for every .mp4 in the configured bucket, or None.

    None means "could not find out" — no bucket configured, aws missing, no
    credentials, network down. Every caller treats that as unproven rather than
    as an empty bucket, which is why there is no default bucket name here: a
    wrong-but-plausible one would answer the question with someone else's data.
    """
    if not ctx.s3_bucket:
        return None
    cmd = ["aws", "s3", "ls", "s3://%s/" % ctx.s3_bucket, "--recursive"]
    if ctx.s3_region:
        cmd += ["--region", ctx.s3_region]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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
    so they must not count as 'missing from S3' when we verify the sync.

    No site repo means no curation file and so no exclusions: everything local
    is expected on S3.
    """
    if ctx.site is None:
        return set()
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
    objs = s3_objects(ctx)
    if objs is None:
        if not quiet:
            print(C.red("  Could not list %s (no s3_bucket configured, aws missing, "
                        "no credentials, or network)."
                        % ("s3://%s" % ctx.s3_bucket if ctx.s3_bucket else "the bucket")))
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


def uploads_outstanding(ctx):
    """Local renders not yet in the bucket at a matching size.

    This is what makes an interrupted upload resumable: the sync is judged by
    what actually landed (key present, size equal — the same evidence
    verify_s3 uses), so a re-run owes only what is missing. A listing that
    fails, and trips flagged mode=delete, follow the established rules: a
    failed listing proves nothing (everything stays outstanding), flagged
    trips are deliberately absent and owe nothing.
    """
    skip = deleted_ids(ctx)
    remote = s3_objects(ctx) or {}
    todo = []
    for p in rendered_mp4s(ctx.out_dir):
        if any(i in p.name for i in skip):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        # "/" boundary as everywhere: sometrip_X.mp4 must not vouch for trip_X.mp4.
        if any((k == p.name or k.endswith("/" + p.name)) and v == size
               for k, v in remote.items()):
            continue
        todo.append(p)
    return todo


def _upload_assets(ctx):
    """Transport one of two: sync the rendered mp4s to the bucket, then verify."""
    started = time.time()
    if not shutil.which("aws"):
        print(C.red("  awscli not found. brew install awscli && aws configure"))
        return record(ctx, NAME[UPLOAD], FAILED, started, "awscli missing")

    # upload-videos-s3.sh syncs whatever public_html/videos resolves to, and the
    # object keys are paths relative to THAT. If it points somewhere other than
    # our out_dir, the keys we verify against would not be the keys it wrote.
    link = ctx.site / "public_html" / "videos"
    target = link.resolve() if link.exists() else None
    if target != ctx.out_dir.resolve():
        print(C.red("  public_html/videos -> %s but config out is %s" % (target, ctx.out_dir)))
        print(C.red("  Point the symlink at the render output before uploading."))
        return record(ctx, NAME[UPLOAD], FAILED, started, "videos symlink mismatch")

    local = rendered_mp4s(ctx.out_dir)
    if not local:
        print(C.yellow("  No rendered mp4s under %s — nothing to upload." % ctx.out_dir))
        return record(ctx, NAME[UPLOAD], SKIPPED, started, "no renders")
    total = sum(p.stat().st_size for p in local)
    # An interrupted or partial earlier upload resumes: say up front how much
    # is genuinely outstanding, so a re-run after a dropped connection reads
    # as the resume it is, not as re-sending everything.
    todo = uploads_outstanding(ctx)
    if len(todo) < len(local):
        print(C.dim("  %d of %d already verified in the bucket — the sync sends only the"
                    " %d outstanding." % (len(local) - len(todo), len(local), len(todo))))
    print("  %d local mp4 (%s) -> s3://%s%s" % (
        len(local), human_bytes(total), ctx.s3_bucket,
        " (%s)" % ctx.s3_region if ctx.s3_region else ""))
    print(C.dim("  The script decides the destination; s3_bucket is what this CLI"))
    print(C.dim("  verifies against afterwards. If they name different buckets the"))
    print(C.dim("  verification fails, which is the intended way to find that out."))
    # No second confirmation: the menu already asked, and the line above states
    # the size and the destination. Two prompts for one decision is how you
    # teach someone to stop reading them.

    # Pass the source explicitly. Left to itself the script syncs whatever
    # public_html/videos resolves to, which is not necessarily the tree we just
    # counted and are about to verify — and verifying a different tree than the
    # one uploaded is worse than not verifying at all.
    rc, _lines = run_stream(["./deploy/upload-videos-s3.sh", str(ctx.out_dir)], ctx.site, "Upload",
                            parser=make_upload_parser(),
                            keep=lambda l: l.startswith(("Skipping ", "Creating bucket")))
    if rc != 0:
        return record(ctx, NAME[UPLOAD], FAILED, started, "exit %d" % rc)

    # aws s3 sync returns 0 even when some objects failed. Verify by comparison.
    print(C.dim("  Verifying against the bucket listing (sync's exit code is not proof)..."))
    ok, missing, mismatched = verify_s3(ctx)
    if ok is None:
        return record(ctx, NAME[UPLOAD], FAILED, started, "could not verify (no bucket listing)")
    if not ok:
        for k in missing[:10]:
            print(C.red("    missing on S3: %s" % k))
        for k in mismatched[:10]:
            print(C.red("    size mismatch: %s" % k))
        return record(ctx, NAME[UPLOAD], FAILED, started,
                      "%d missing, %d size mismatch" % (len(missing), len(mismatched)))
    return record(ctx, NAME[UPLOAD], RAN, started,
                  "%d mp4 verified on S3, %s" % (len(local), human_bytes(total)))


def record_deploy(ctx):
    """Note which renders a successful site deploy covered.

    The delete guard's other half: 'uploaded' is provable by listing the
    bucket, but 'the site deploy happened' leaves no remote artefact this CLI
    can interrogate per-render — so the fact is recorded at the only moment
    it is known to be true, right after deploy-site.sh exits 0. Merged, not
    replaced: a render deployed last round stays covered even if the deploy
    re-runs after the working area changed.

    Without this file item 8 is permanently unreachable on a publishing
    install: working_area_is_expendable requires a render to be in the bucket
    AND named here before it will call it a second copy.
    """
    names = {p.name for p in rendered_mp4s(ctx.out_dir)}
    path = ctx.out_dir / DEPLOYED_FILE
    try:
        prev = set(json.loads(path.read_text()).get("videos", []))
    except Exception:
        prev = set()
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"videos": sorted(prev | names),
             "at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    except OSError:
        pass
    return names


def _deploy_pages(ctx):
    """Transport two of two: run the site repo's deploy script."""
    started = time.time()
    print(C.dim("  deploy-site.sh pulls the live curation + trips.json first (the live"))
    print(C.dim("  site is the merge base), re-indexes, then rsyncs public_html/."))
    print(C.yellow("  SIGNED_VIDEOS=1 is set for this run."))
    print(C.dim("  It is set unconditionally because the alternative is a silent failure:"))
    print(C.dim("  against a private bucket, deploying without it writes a config.js"))
    print(C.dim("  pointing the page at raw S3 URLs, which 403 — the site comes up and no"))
    print(C.dim("  video plays. A deploy script that ignores the variable is unaffected."))

    rc, _lines = run_stream(["./deploy/deploy-site.sh"], ctx.site, "Deploy",
                            env_extra={"SIGNED_VIDEOS": "1"},
                            keep=lambda l: l.startswith("=== ") or l.startswith("Done."))
    if rc != 0:
        return record(ctx, NAME[UPLOAD], FAILED, started, "deploy exit %d" % rc)

    record_deploy(ctx)
    live = live_trip_count(ctx)
    return record(ctx, NAME[UPLOAD], RAN, started,
                  "live site reports %s trips" % (live if live is not None else "?"))


def publish_website(ctx, world):
    """Item 7: one job, two transports, in this order.

    The assets have to land before the deploy record is written, because
    working_area_is_expendable wants both facts about the same file — the
    object in the bucket at a matching size AND its name in .deployed.json —
    before it will let item 8 erase anything. Uploading without deploying
    publishes nothing visible; deploying without uploading publishes a page
    whose videos 403.
    """
    assets = _upload_assets(ctx)
    if assets.status not in COMPLETING:
        return _outcome(assets)
    print()
    print(rule("deploy the pages"))
    return _outcome(_deploy_pages(ctx))


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
    if not ctx.site_script("deploy", "is-complete.py"):
        return None, None, ["no deploy/is-complete.py to run"]
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


# ---------------------------------------------------------------------------
# Item 8 — Clean Workspace. Erase the imported footage and the renders it
# produced. DESTRUCTIVE, and the half that used to be folded together with the
# card wipe below.
# ---------------------------------------------------------------------------

def _nothing(ctx, number, started, reason):
    """Record a plan that found nothing to do, and say so to the item."""
    record(ctx, NAME[number], SKIPPED, started, reason)
    return menu.Plan.nothing_to_do(reason)


def _clean_target(ctx, root):
    """What actually gets erased, narrowed when siblings share the sink.

    If `root` is the sink itself it may also hold OTHER imports as dated
    subfolders, and those have not been scanned, rendered or verified by
    anything here — rmtree of the sink would take them with it.
    """
    siblings = [c for c in sorted(root.iterdir())
                if c.is_dir() and (c / "DCIM").is_dir()] if root.is_dir() else []
    if not siblings:
        return root
    target = root / "DCIM"
    print()
    print(C.yellow("  %s also holds %d other import(s): %s" % (
        root, len(siblings), ", ".join(c.name for c in siblings))))
    print(C.yellow("  Narrowing the delete to %s; the others are untouched." % target))
    return target


def _print_gates(world):
    """The three gates and what each one answered.

    Which proofs are even possible depends on what is configured, so the count
    comes first and the gates number themselves against it. Writing "[1/3]"
    when only one check can run would claim two that never happened.
    """
    readings = [(label, e) for label, e in guards.gate_readings(world) if e.applicable]
    for i, (label, e) in enumerate(readings, 1):
        print("  [%d/%d] %s %s" % (i, len(readings), (label + " ").ljust(40, "."),
                                   _evidence_colour(e)))


def _evidence_colour(e):
    if e is menu.Evidence.YES:
        return C.green(e.value)
    return C.red(e.value)


def _clean_banner(ctx, world, target, size):
    """The last thing on screen before the word is asked for."""
    lines = [C.red("  Deleting %s removes %s of original footage permanently."
                   % (target, human_bytes(size)))]
    unproven = guards.unproven_lines(world)
    if not unproven:
        lines.append(C.dim("  The renders and their S3 copies stay; the raw clips do not"
                           " come back."))
        return tuple(lines)
    # Not a warning about missing setup — a statement of what survives this,
    # which is strictly less than it would be with a bucket and a site behind
    # it. The check that could not run is named, so it is obvious this passed
    # unexamined rather than passed.
    lines.append(C.red("  Publication was NOT verified — it could not be:"))
    lines.extend(C.red("    " + line) for line in unproven)
    lines.append(C.red("  The renders under %s are therefore the only" % tilde(ctx.out_dir)))
    lines.append(C.red("  copy of this footage that exists. Lose that disk and the drive"
                       " is gone."))
    lines.append(C.dim("  Back the renders up elsewhere first, or leave the import where"
                       " it is — keeping it costs disk, not data."))
    return tuple(lines)


def clean_workspace_plan(ctx, world):
    """Item 8's plan. Prints, asks nothing irreversible, refuses early.

    The heavy guard runs TWICE on purpose: here, so a refusal never reaches
    the CLEAN prompt, and again inside the commit against a world captured
    after the word was typed. Same callable both times, so the two cannot
    drift the way two hand-copied chains did.
    """
    started = time.time()
    root = pick_import(ctx, "clearing the workspace")
    if root is None:
        return _nothing(ctx, CLEAN_WS, started, "no import folder")
    target = _clean_target(ctx, root)
    if not target.is_dir():
        print(C.red("  Nothing to delete at %s" % target))
        return _nothing(ctx, CLEAN_WS, started, "nothing at the target")

    size, files = tree_size(target), count_files(target)
    print()
    print(rule("erase the imported footage"))
    print("  Target: %s" % C.bold(str(target)))
    print("  %d file(s), %s — this is the ORIGINAL footage and it is not recoverable."
          % (files, C.bold(human_bytes(size))))
    print()
    _print_gates(world)
    verdict = guards.workspace_is_expendable(world)
    if verdict.blocked:
        print(C.red("  Refusing: %s." % verdict.reason))
        _print_gate_detail(world)
        return _nothing(ctx, CLEAN_WS, started, "refused: %s" % verdict.reason)

    return menu.Plan(guards.workspace_is_expendable,
                     lambda fresh: _clean_workspace_commit(ctx, root, target,
                                                           size, files, started),
                     banner=_clean_banner(ctx, world, target, size))


def _print_gate_detail(world):
    """What the deciding gate actually saw, under the refusal."""
    for line in world.site.published_lines[-15:]:
        print(C.dim("        " + line))


def _clean_workspace_commit(ctx, root, target, size, files, started):
    """The irreversible half of item 8."""
    try:
        shutil.rmtree(str(target))
    except OSError as e:
        print(C.red("  Delete failed: %s" % e))
        return _outcome(record(ctx, NAME[CLEAN_WS], FAILED, started, str(e)))
    if ctx.selected_import == root:
        ctx.selected_import = None
    ctx.last_scan = None
    ctx.last_groups = None
    print(C.green("  Deleted %s (%s)" % (target, human_bytes(size))))

    # The renders go too — but only when that is separately proven. The gates
    # above approved deleting the FOOTAGE; with neither bucket nor site the
    # banner has just finished saying these renders are the only copy of it,
    # and deleting them anyway would contradict the sentence above it. What
    # survives either way: the ledger's high-water mark, written before any of
    # this runs, and every _meta.json.
    ok, why, stragglers = working_area_is_expendable(ctx)
    n = freed = 0
    if ok:
        n, freed = purge_published_renders(ctx, root)
    else:
        _keeping_the_renders(why, stragglers)
    return _outcome(record(ctx, NAME[CLEAN_WS], RAN, started,
                           "%d file(s), %s freed" % (files + n, human_bytes(size + freed))))


def _keeping_the_renders(why, stragglers):
    print()
    print(C.yellow("  Keeping the renders: %s." % why))
    for f in stragglers[:6]:
        print(C.dim("    %s" % tilde(f)))
    print(C.dim("  The original footage is gone; these are now the only copy"))
    print(C.dim("  of those trips. Publish them (%d) or gather them (%d), then"
                % (UPLOAD, BUILD)))
    print(C.dim("  clean up again."))


# ---------------------------------------------------------------------------
# Item 9 — Delete SIM Data. The card itself: DESTRUCTIVE, and the only target
# whose contents have no second copy unless this machine holds one.
#
# Unfolded back out of the clean-up, which is not tidying. Folded, the card's
# evidence was gathered ("the clips are in the workspace"), the workspace was
# then erased, and the card half re-checked against a workspace that no longer
# held them — refusing after the irreversible half had run, having already
# printed that the card was verified. Item 8's outbound is {1}, so it can
# never precede this; the window is closed by the graph, not by discipline.
# ---------------------------------------------------------------------------

def _card_advisory(ctx):
    """Whether the workspace copy is published yet — a note, not a gate.

    Erasing the card is allowed on the strength of a copy existing here. If
    that copy is not published, it becomes the ONLY one the moment the card
    goes, which is worth saying out loud even though it does not refuse.
    """
    lines = []
    for cand in import_candidates(ctx):
        ok, why = import_is_expendable(ctx, cand)
        if not ok:
            lines.append(C.yellow("  Not yet published (%s: %s) — after this the copy"
                                  % (tilde(cand), why)))
            lines.append(C.yellow("  on this machine is the only one, so do not lose it."))
    return lines


def erase_card_plan(ctx, world):
    """Item 9's plan."""
    started = time.time()
    lines = [C.red("  The card's %d clip(s) go; its folders stay so the camera can"
                   " record." % len(world.card.stamps)),
             C.green("  Every clip is accounted for: %s." % world.card.note)]
    lines.extend(_card_advisory(ctx))
    return menu.Plan(guards.card_is_expendable,
                     lambda fresh: _erase_card_commit(ctx, started),
                     banner=tuple(lines))


def _erase_card_commit(ctx, started):
    gone, freed, reason = wipe_card(ctx)
    if reason:
        print(C.red("  Card NOT cleaned: %s." % reason))
        return _outcome(record(ctx, NAME[ERASE_CARD], SKIPPED, started,
                               "refused: %s" % reason))
    return _outcome(record(ctx, NAME[ERASE_CARD], RAN, started,
                           "%d file(s), %s freed" % (gone, human_bytes(freed))))


def card_stamps(ctx):
    """The clip stamps on the card in the slot.

    Harvested first and from the CARD, so every question below is about THIS
    card. Without it the evidence checks would be permanently true the moment
    any final_ folder existed — and final_ folders survive every sweep by
    design, so a card whose own import was lost would be erased on the
    strength of last month's renders.
    """
    stamps = set()
    front = ctx.card / "DCIM" / "200video" / "front"
    if front.is_dir():
        for f in front.glob("*.mp4"):
            m = STAMP_RE.search(f.name)
            if m:
                stamps.add(m.group(1))
    return stamps


def covered_stamps(ctx, stamps):
    """Which of these clips sit inside a rendered trip's span.

    Read from each trip's _meta.json rather than from filenames: a card clip's
    name IS its wall clock, so containment is a real test. Matching filename
    days instead would accept any render that happens to share a date.
    """
    metas = []
    froot = getattr(ctx, "final_root", ctx.out_dir)
    for base in (froot, ctx.out_dir):
        if base.is_dir():
            for d in base.glob(FINAL_PREFIX + "*"):
                metas += list(d.rglob("trip_*_meta.json"))
    metas += list(ctx.out_dir.rglob("trip_*_meta.json"))
    spans = _spans_of(metas)
    return {st for st in stamps if any(a <= st <= b for a, b in spans)}


def _spans_of(metas):
    spans = []
    for mp in metas:
        try:
            md = json.loads(mp.read_text())
        except Exception:
            continue
        s, e = md.get("start"), md.get("end")
        if s and e:
            spans.append((_digits14(s), _digits14(e)))
    return spans


def _digits14(v):
    """A timestamp in the 14-digit form a clip filename carries. None is ""."""
    return str(v or "").replace("-", "").replace(":", "").replace(" ", "")[:14]


def workspace_stamps(ctx, stamps):
    """Which of these clips are still sitting in the workspace.

    Filenames are the stamp, so this is an identity check against THIS card
    rather than a headcount of whatever footage is around.
    """
    found = set()
    for cand in import_candidates(ctx):
        cfront = cand / "DCIM" / "200video" / "front"
        if cfront.is_dir():
            for f in cfront.glob("*.mp4"):
                m = STAMP_RE.search(f.name)
                if m and m.group(1) in stamps:
                    found.add(m.group(1))
    return found


def card_accounting(ctx):
    """(owed, note) — which of this card's clips are accounted for by nothing.

    The per-clip guard on erasing the card. Every clip must be accounted for,
    by whichever mix of evidence: excluded on purpose, inside a rendered
    trip's span, or still in the workspace. Approving on any single accounted
    clip is what let one rendered trip vouch for a whole card, and the wipe
    then erased clips whose only copy was the card itself. The kinds of
    evidence may mix; the accounting may not have gaps.
    """
    stamps = card_stamps(ctx)
    # Excluded clips are accounted for BY the exclusion: their footage was
    # dropped on purpose, so no copy is supposed to exist and none is owed.
    dropped = stamps & excluded_stamps(ctx)
    stamps = stamps - dropped
    covered = covered_stamps(ctx, stamps)
    in_workspace = workspace_stamps(ctx, stamps)
    return stamps - covered - in_workspace, _accounting_note(dropped, covered,
                                                             in_workspace - covered)


def _accounting_note(dropped, covered, only_in_workspace):
    bits = []
    if dropped:
        bits.append("%d excluded on purpose" % len(dropped))
    if covered:
        bits.append("%d inside rendered trips" % len(covered))
    if only_in_workspace:
        bits.append("%d in the workspace" % len(only_in_workspace))
    return ", ".join(bits) or "nothing on the card"


def copy_still_exists(ctx):
    """(ok, what) — is the footage the ledger claims actually still here?

    The ledger records that a verified copy WAS made. It cannot notice that the
    copy was later deleted, moved to a disk that is not plugged in, or lost to
    a sweep — "imported through X" reads identically in all those cases. On its
    own it is a claim, and acting on a claim means erasing the last copy of a
    drive because a 154-byte JSON file said not to worry.

    So the ledger decides WHETHER the card's clips were ever copied, and this
    decides whether that copy is still somewhere. Deliberately about THIS card,
    never a shortcut through import_is_expendable on the workspace: that proves
    the CURRENT import is rendered, which says nothing about the card in the
    slot.
    """
    if not card_stamps(ctx):
        return False, ""
    owed, note = card_accounting(ctx)
    if owed:
        return False, ""
    return True, note


def wipe_card(ctx):
    """Erase the card's files, keeping its folder tree. Returns (gone, freed, reason).

    The guarded core of item 9 without the conversation. The guard is the same
    pure predicate the item's evaluate and its post-word re-check use — one
    implementation, three call sites, so they cannot drift — asked against a
    world derived right now. Deletes FILES only: the camera writes into
    DCIM/200video/{front,rear} and expects those folders to exist, so erasing
    the tree makes the next recording fail in the car. reason is "" on success.
    """
    dcim = ctx.card / "DCIM"
    if not dcim.is_dir():
        return 0, 0, "no card at %s" % tilde(ctx.card)
    verdict = guards.card_is_expendable(capture_world(ctx, menu.Scope.LOCAL))
    if verdict.blocked:
        return 0, 0, verdict.reason
    return _unlink_card_files(dcim)


def _unlink_card_files(dcim):
    gone = freed = 0
    for f in [f for f in dcim.rglob("*") if f.is_file()]:
        try:
            freed += f.stat().st_size
            f.unlink()
            gone += 1
        except OSError as e:
            print(C.red("  %s: %s" % (f.name, e)))
    print(C.green("  Erased %d file(s), %s freed. Folders kept so the camera can record."
                  % (gone, human_bytes(freed))))
    return gone, freed, ""


# ---------------------------------------------------------------------------
# The world — the ONE place that goes and looks at the disk.
#
# Every guard used to walk the filesystem itself, at the moment it was asked.
# That made them untestable without a fixture tree, and it made "when was this
# true" a question about call order rather than about a value. Now the disk is
# read here, once, into a frozen snapshot, and everything downstream is a pure
# function of it.
#
# Two scopes. LOCAL is the filesystem alone and is what the menu draws on every
# loop; FULL also lists the bucket and shells out to is-complete.py, which is a
# network call and a subprocess. Painting the menu with FULL would put both on
# every keystroke, and a menu that is not instant stops being recomputed and
# starts being remembered — which is the one thing a greying rule must never be.
# ---------------------------------------------------------------------------

def _render_of(path):
    try:
        return W.Render(path.name, path.stat().st_size, path)
    except OSError:
        return None


def _renders_of_tree(out_dir):
    """Every rendered mp4 with its size. rendered_mp4s is what defines
    "a render": trip_*.mp4, never a scratch encode in a dot-directory."""
    return tuple(filter(None, map(_render_of, rendered_mp4s(out_dir))))


def _renders_here(ctx, root):
    """This import's own renders, namespaced the way the guard counts them."""
    if root is None:
        return ()
    return _renders_of_tree(ctx.out_dir / root.name)


def _meta_of(path):
    try:
        md = json.loads(path.read_text())
    except Exception:
        return None
    return _trip_meta(path, md)


def _trip_meta(path, md):
    return W.TripMeta(path.stem, _digits14(md.get("start")),
                      _digits14(md.get("end")), path)


def _metas_of(ctx):
    found = map(_meta_of, _safe_rglob(ctx.out_dir, "trip_*_meta.json"))
    return tuple(filter(None, found))


def _safe_rglob(base, pattern):
    try:
        return sorted(base.rglob(pattern))
    except OSError:
        return []


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _newest_mtime(paths):
    return max(filter(None, map(_mtime, paths)), default=0.0)


def _is_dir(path):
    return path.is_dir()


def _final_glob(base):
    try:
        return sorted(base.glob(FINAL_PREFIX + "*"))
    except OSError:
        return []


def _final_folders(ctx):
    roots = (getattr(ctx, "final_root", ctx.out_dir), ctx.out_dir)
    found = itertools.chain.from_iterable(map(_final_glob, roots))
    return tuple(filter(_is_dir, found))


def _is_track_file(f):
    """The track lives in DCIM/<NNN>gps/ as .gpx, or as a '*.git' tar archive
    the renderer harvests .gpx members from. Either counts."""
    return f.suffix.lower() in (".gpx", ".git")


def _is_gps_dir(path):
    return path.is_dir() and "gps" in path.name.lower()


def _gps_dirs(cands):
    subs = itertools.chain.from_iterable(map(_dcim_subdirs, cands))
    return filter(_is_gps_dir, subs)


def _dcim_subdirs(cand):
    return _subdirs(cand / "DCIM")


def _subdirs(dcim):
    if not dcim.is_dir():
        return []
    return list(dcim.iterdir())


def _has_track(cands):
    return any(map(lambda d: any(map(_is_track_file, d.iterdir())), _gps_dirs(cands)))


def _expected_trips(ctx, root):
    """How many trips this import SHOULD produce, or None when not known.

    Read from THIS SESSION'S cached grouping and never by starting a scan.
    The grouping comes from make_dashcam_videos --print-groups, which decodes
    video to find the real pull-away and park moments and costs minutes; the
    world is captured on every menu draw, so a capture that could start one
    would make the menu unusable.

    None is therefore an ordinary state, not a failure — and it is not zero
    and not "fine". It is what the workspace guard reads as UNKNOWN: the local
    render count cannot be compared against anything, so that gate abstains
    and, with no site to ask instead, the erase is refused.
    """
    return _renderable_count(_cached_grouping(ctx, root))


def _cached_grouping(ctx, root):
    """This session's grouping for `root`, or None. Never starts a scan.

    ctx.last_groups is a single (root, payload) pair, so as a one-entry
    mapping the lookup IS the match test — and a root of None misses it the
    same way any other absent key would.
    """
    return dict(filter(None, (ctx.last_groups,))).get(root)


def _renderable_count(payload):
    if payload is None:
        return None
    return len(list(filter(_is_renderable, payload.get("trips", []))))


def _is_renderable(trip):
    return trip.get("renderable", True)


def _stills_current(ctx):
    """A contact sheet exists and every still beside it is a real file."""
    sheet = ctx.out_dir / PREVIEW_DIRNAME / "index.html"
    return sheet.is_file()


def _already_imported(stamp, mark, excluded):
    return stamp in excluded or _at_or_before(stamp, mark)


def _at_or_before(stamp, mark):
    return bool(mark) and stamp <= mark


def _never_imported_stamps(stamps, mark, excluded):
    """The clips a delta import would still copy.

    card_split's rule, as a SET rather than a count: an excluded clip counts
    as already imported however old the mark is, because its footage was
    dropped on purpose and "new" here means "would be copied next time".
    """
    return frozenset(itertools.filterfalse(
        lambda s: _already_imported(s, mark, excluded), stamps))


def _card_facts(ctx):
    """The card, and the per-clip accounting for it, derived once.

    new_stamps and owed_stamps are FIELDS rather than calls because their old
    form read a module global that four call sites remembered to refresh
    first. A global four places remember is a global the fifth forgets.
    """
    dcim = ctx.card / "DCIM"
    if not dcim.is_dir():
        return W.Card(path=ctx.card)
    excluded = frozenset(excluded_stamps(ctx))   # the file is the source; refreshed here
    stamps = frozenset(card_stamps(ctx))
    owed, note = card_accounting(ctx)
    return W.Card(path=ctx.card, dcim=True, present=_holds_files(dcim), stamps=stamps,
                  new_stamps=_never_imported_stamps(stamps, last_imported_stamp(ctx),
                                                    excluded),
                  owed_stamps=frozenset(owed), note=note)


def _holds_files(dcim):
    return next(filter(_is_file, _safe_rglob(dcim, "*")), None) is not None


def _is_file(path):
    return path.is_file()


def _bucket_listing(ctx, scope):
    """Listed, Unlistable or NoBucket — three answers, never collapsed.

    "Could not read the bucket" is not "not in the bucket". The type is what
    makes `s3_objects(ctx) or {}` — the idiom that used to turn one into the
    other — unwritable downstream.
    """
    if not ctx.cfg_opt("s3_bucket"):
        return W.NoBucket()
    return _listing_or_unknown(_listed_at(ctx, scope))


def _listed_at(ctx, scope):
    """Only FULL scope pays for the listing; it is a subprocess and a network
    round trip, and the menu is drawn on every keystroke."""
    if scope is not menu.Scope.FULL:
        return None
    return s3_objects(ctx)


def _listing_or_unknown(objs):
    if objs is None:
        return W.Unlistable()
    return W.Listed(dict(objs))


def _deployed_names(ctx):
    try:
        return frozenset(json.loads((ctx.out_dir / DEPLOYED_FILE).read_text())
                         .get("videos", []))
    except Exception:
        return frozenset()


_DEPLOY_SCRIPTS = ("deploy-site.sh", "upload-videos-s3.sh", "is-complete.py")


def _site_scripts(ctx):
    return frozenset(filter(lambda n: ctx.site_script("deploy", n), _DEPLOY_SCRIPTS))


def _resolved(path):
    try:
        return path.resolve()
    except OSError:
        return path


def _videos_link(ctx):
    if ctx.site is None:
        return None
    return _existing(ctx.site / "public_html" / "videos")


def _existing(link):
    if link.exists():
        return _resolved(link)
    return None


def _can_ask_the_site(ctx, scope):
    return scope is menu.Scope.FULL and bool(ctx.site_script("deploy", "is-complete.py"))


def _published(ctx, scope):
    """(evidence, counts, lines) from is-complete.py, the deciding gate.

    NA when it cannot be asked at all, which is what makes the workspace rule
    fall back to unanimity among whatever else can answer.
    """
    if not _can_ask_the_site(ctx, scope):
        return menu.Evidence.NA, (None, None), ()
    safe, total, lines = is_complete_summary(ctx)
    return _published_evidence(safe, total), (safe, total), tuple(lines)


def _published_evidence(safe, total):
    """UNKNOWN when the script could not answer, which is not NO and not YES."""
    if None in (safe, total):
        return menu.Evidence.UNKNOWN
    return _all_published(safe, total)


def _all_published(safe, total):
    # total == 0 is NO, not YES: is-complete.py finding nothing to report is
    # not the same as it reporting that everything is live.
    if not total:
        return menu.Evidence.NO
    return _covers_all(safe, total)


def _covers_all(safe, total):
    if safe >= total:
        return menu.Evidence.YES
    return menu.Evidence.NO


def _site_facts(ctx, scope):
    if ctx.site is None:
        return W.SiteFacts(page=_page_exists(ctx))
    published, counts, lines = _published(ctx, scope)
    return W.SiteFacts(
        configured=True, on_disk=ctx.site.is_dir(), scripts=_site_scripts(ctx),
        has_manifest_builder=bool(ctx.site_script("build_manifest.py")),
        videos_link=_videos_link(ctx),
        curation_deleted=frozenset(deleted_ids(ctx)),
        deployed=_deployed_names(ctx), published=published,
        published_counts=counts, published_lines=lines, page=_page_exists(ctx))


def _has_result_file(base):
    return (base / RESULT_FILE).is_file()


def _page_exists(ctx):
    if _has_result_file(ctx.out_dir):
        return True
    return any(map(_has_result_file, _final_folders(ctx)))


def _excluded_at(ctx):
    try:
        return (ctx.out_dir / EXCLUDED_FILE).stat().st_mtime
    except OSError:
        return 0.0


def _chosen_import(ctx, imports):
    """Which import the world is about: the one already picked, or the only
    one there. Several unpicked imports is genuinely "not decided yet"."""
    if ctx.selected_import in imports:
        return ctx.selected_import
    return _the_only_one(imports)


def _the_only_one(imports):
    if len(imports) == 1:
        return imports[0]
    return None


def capture_world(ctx, scope=menu.Scope.LOCAL):
    """Read the disk once and freeze what it said.

    Called on every menu draw, again at dispatch, and a third time inside a
    destructive item after the word is typed and before anything irreversible
    runs. Re-derived rather than updated: an update is a second way to be
    wrong, and the world moves under this tool — an operator swaps the card or
    deletes a sidecar in Finder while the prompt is on screen.
    """
    imports = tuple(import_candidates(ctx))
    root = _chosen_import(ctx, imports)
    metas = _metas_of(ctx)
    return _world_of(ctx, scope, imports, root, metas,
                     working_area_is_expendable(ctx))


def _meta_paths(metas):
    return list(filter(None, map(_path_of, metas)))


def _path_of(meta):
    return meta.path


def _world_of(ctx, scope, imports, root, metas, expendable):
    settled, why, stragglers = expendable
    return W.World(
        at=time.time(), scope=scope, strategy=menu.Strategy.of(ctx),
        # RESOLVED, because the only thing that compares it is the videos
        # symlink check and a symlink resolves to the real path. Comparing
        # /var/... against /private/var/... would report a mismatch on every
        # macOS install and grey publishing out permanently.
        out_dir=_resolved(ctx.out_dir), out_dir_owner=claim_out_dir(ctx),
        imports=imports, selected_import=root, metas=metas,
        renders=_renders_of_tree(ctx.out_dir), renders_here=_renders_here(ctx, root),
        final_folders=_final_folders(ctx), expected_trips=_expected_trips(ctx, root),
        has_track=_has_track(imports), stills_current=_stills_current(ctx),
        ledger_mark=last_imported_stamp(ctx),
        excluded=frozenset(excluded_stamps(ctx)), excluded_at=_excluded_at(ctx),
        newest_meta_at=_newest_mtime(_meta_paths(metas)),
        workspace_settled=settled, workspace_note=why,
        stragglers=tuple(stragglers), card=_card_facts(ctx),
        bucket=_bucket_listing(ctx, scope), site=_site_facts(ctx, scope),
        awscli=bool(shutil.which("aws")))


# ---------------------------------------------------------------------------
# The Work facade — the only thing the items know about this module.
#
# items.py imports guards and menu and nothing else, so an item can be driven
# by a mock that answers these method names. What the items call "work" is one
# object per run holding the live ctx; the strategy branch is asked for ONCE
# here, at construction, and never inside a body.
# ---------------------------------------------------------------------------

def _outcome(result):
    """A StepResult becomes the item's Outcome.

    `completed` is the owner's signal and it is exactly RAN-or-SATISFIED: the
    postcondition holds and the operator did not abort. The old convention
    returned `status != FAILED`, which made "you typed anything but DROP"
    indistinguishable from "the trip was removed".
    """
    return menu.Outcome(result.status in COMPLETING, result.detail)


class Gatherer:
    """Which mover item 6 installs. The strategy decides, once."""

    @staticmethod
    def for_strategy(strategy):
        if strategy is menu.Strategy.WEBSITE_REPO:
            return no_gather
        return gather_into_final


class Publisher:
    """Item 7 under the publishing product: two transports, one job."""

    def __init__(self, ctx):
        self._ctx = ctx

    def why_not(self, world):
        """The configuration questions, named by the key that fixes them.

        This is where someone who cloned the repo finds out publishing exists
        at all, so the reason names the config key rather than saying "not
        configured", which tells them nothing they can act on.
        """
        return _first_reason(_publish_reasons(self._ctx, world))

    def run(self, world):
        return publish_website(self._ctx, world)


class NoPublisher:
    """Item 7 under the local product: no edges, and nothing to run.

    Constructed rather than omitted so that every number means the same thing
    on every installation — a menu that renumbers itself makes every sentence
    anyone writes about "item 5" true only locally.
    """

    def why_not(self, world):
        return "needs site_repo and s3_bucket in config.txt"

    def run(self, world):        # pragma: no cover - unreachable by two rules
        return menu.stopped("publishing is not configured")


def _first_reason(reasons):
    return next(filter(None, reasons), None)


def _publish_reasons(ctx, world):
    return (_no_bucket_reason(world), _no_site_reason(ctx, world),
            _no_script_reason(world), _no_awscli_reason(world),
            guards.videos_link_wrong(world))


def _no_bucket_reason(world):
    if not isinstance(world.bucket, W.NoBucket):
        return None
    return "needs s3_bucket in config.txt"


def _no_site_reason(ctx, world):
    if world.site.on_disk:
        return None
    return _site_key_or_path(ctx, world)


def _site_key_or_path(ctx, world):
    if world.site.configured:
        return "site_repo not found: %s" % tilde(ctx.site)
    return "needs site_repo in config.txt"


_PUBLISH_SCRIPTS = ("upload-videos-s3.sh", "deploy-site.sh")


def _no_script_reason(world):
    missing = next(itertools.filterfalse(world.site.has, _PUBLISH_SCRIPTS), None)
    if not missing:
        return None
    return "no deploy/%s in the site repo" % missing


def _no_awscli_reason(world):
    if world.awscli:
        return None
    return "awscli not found — brew install awscli && aws configure"


class Work:
    """One per run. Holds the ctx; hands the items their bodies."""

    def __init__(self, ctx):
        self.ctx = ctx

    # -- the bodies --------------------------------------------------------
    def progress(self, world):
        return _outcome(step_progress(self.ctx))

    def import_footage(self, world):
        return _outcome(step_import(self.ctx))

    def generate_meta(self, world):
        return _outcome(step_generate_meta(self.ctx))

    def build_preview(self, world):
        return _outcome(step_preview(self.ctx))

    def render(self, world):
        return _outcome(step_render(self.ctx))

    def build_website(self, world, gather):
        return _outcome(step_site(self.ctx, gather))

    # -- the collaborators the constructor installs ------------------------
    def gatherer(self, strategy):
        return functools.partial(Gatherer.for_strategy(strategy), self.ctx)

    def publisher(self, strategy):
        if strategy is menu.Strategy.WEBSITE_REPO:
            return Publisher(self.ctx)
        return NoPublisher()

    # -- the destructive plans ---------------------------------------------
    def exclude_plan(self, world):
        return drop_plan(self.ctx, world)

    def clean_workspace_plan(self, world):
        return clean_workspace_plan(self.ctx, world)

    def erase_card_plan(self, world):
        return erase_card_plan(self.ctx, world)

    # -- what Destructive needs between the plan and the act ---------------
    def show(self, banner):
        print()
        for line in banner:
            print(line)

    def ask_word(self, word):
        print()
        return ask("  Type %s to confirm, anything else to cancel: " % word)

    def recapture(self, scope):
        """The refresh point. Called after the word and before the act."""
        return capture_world(self.ctx, scope)

    def refuse(self, reason):
        print(C.red("  Refusing after the re-check: %s." % reason))
        print(C.dim("  Something changed while the prompt was on screen. Nothing"
                    " was touched."))
        return menu.stopped("refused after re-check: %s" % reason)


# ---------------------------------------------------------------------------
# The painter. Everything it draws is derived from the position, the world and
# the items' own methods — there is no second list of steps here to drift from
# ALL_ITEMS, no hardcoded number and no hardcoded label. If it needs a fact it
# asks the item or the world for it.
# ---------------------------------------------------------------------------

def _paint_body(item, verdict, offered):
    """Grey means unselectable, red means it destroys, bold means go.

    Each of the three comes from the item or its verdict, never from a table
    of numbers: destructive is item.destr(), unselectable is the position not
    offering it or its own guard blocking it.
    """
    if _why_not(item, verdict, offered):
        return C.dim(item.name())
    return _paint_live(item)


def _paint_live(item):
    if item.destr():
        return C.red(item.name())
    return C.bold(item.name())


def _cell_width(menu_items):
    return max(len(i.name()) for i in menu_items.values()) + 9


def _grid_columns(width, cell):
    return max(1, min(4, width // cell))


def _menu_line(item, verdict, offered, cell):
    body = _paint_body(item, verdict, offered)
    pad = cell - (len(item.name()) + len(str(item.number)) + 2)
    return "%d) %s%s" % (item.number, body, " " * max(1, pad))


def _why_lines(menu_items, verdicts, offered):
    """Why the greyed entries are greyed, one gate at a time.

    Two gates, and they are different problems, so they get different
    sentences. The GUARD's refusal is about this world and is actionable —
    "no GPS track in the import", "no card at <path>" — so it gets a line
    each. The GRAPH's is about where the pipeline is, is the same sentence
    for every entry it applies to, and gets one line naming them together;
    printing it eight times taught the eye to skip the block that also holds
    the actionable ones.
    """
    return _blocked_lines(menu_items, verdicts, offered) + _not_here_line(
        menu_items, offered)


def _blocked_lines(menu_items, verdicts, offered):
    said = map(lambda n: _blocked_line(n, verdicts[n]), sorted(offered))
    return list(filter(None, said))


def _blocked_line(number, verdict):
    if not verdict.blocked:
        return ""
    return C.dim("   %d) %s" % (number, verdict.reason))


def _not_here_line(menu_items, offered):
    elsewhere = sorted(set(menu_items) - set(offered))
    if not elsewhere:
        return []
    return [C.dim("   %s) not from here — the graph does not offer them yet"
                  % ",".join(map(str, elsewhere)))]


def _why_not(item, verdict, offered):
    """Is this entry unpickable, and by which gate. "" means it is pickable."""
    if not offered:
        return "does not follow where we are"
    return _guard_reason(verdict)


def _guard_reason(verdict):
    if verdict.blocked:
        return verdict.reason
    return ""


def print_menu(ctx, menu_items, position, world):
    """The grid, painted from the state machine and nothing else."""
    print(rule())
    verdicts = _verdicts(menu_items, world)
    offered = position.selectable(menu_items)
    cell = _cell_width(menu_items)
    _print_all(_grid(menu_items, verdicts, offered, cell,
                     _grid_columns(term_width(), cell)))
    _print_all(_why_lines(menu_items, verdicts, offered))
    print(C.dim("   %s   s = status   q = quit    (%s destroy footage)"
                % (_where_line(menu_items, position), _destructive_list(menu_items))))


def _verdicts(menu_items, world):
    return {n: _safe_verdict(item, world) for n, item in menu_items.items()}


def _print_all(lines):
    for line in lines:
        print(line.rstrip())


def _safe_verdict(item, world):
    """A guard that raises must not take the menu down with it."""
    try:
        return item.evaluate(world)
    except Exception as e:                        # pragma: no cover - defensive
        return menu.blocked("guard error: %s" % e)


def _grid(menu_items, verdicts, offered, cell, cols):
    ordered = list(map(menu_items.get, sorted(menu_items)))
    rows = (len(ordered) + cols - 1) // cols
    return list(map(lambda r: _row(ordered, verdicts, offered, cell, cols, rows, r),
                    range(rows)))


def _row(ordered, verdicts, offered, cell, cols, rows, r):
    picks = map(lambda c: r + c * rows, range(cols))      # fill down, then across
    here = filter(lambda i: i < len(ordered), picks)
    return "  " + "".join(map(
        lambda i: _menu_line(ordered[i], verdicts[ordered[i].number],
                             ordered[i].number in offered, cell), here))


def _where_line(menu_items, position):
    """Where the pipeline is, in the item's own words."""
    if position.current == menu.NOWHERE:
        return "at: the start"
    return "at: %d) %s" % (position.current, menu_items[position.current].name())


def _destructive_list(menu_items):
    hits = filter(lambda n: menu_items[n].destr(), sorted(menu_items))
    return ",".join(map(str, hits))


# ---------------------------------------------------------------------------
# The runner: one selection, one item, one world per dispatch.
# ---------------------------------------------------------------------------

def print_summary(ctx):
    if not ctx.results:
        return
    print()
    print(rule("summary"))
    _print_all(map(_summary_line, ctx.results))
    print(rule())


def _summary_line(r):
    return "  %s  %-34s %8s   %s" % (_status_tag(r.status), r.name,
                                     human_secs(r.seconds), C.dim(r.detail))


_STATUS_TAGS = {RAN: lambda: C.green("ran      "),
                SATISFIED: lambda: C.green("satisfied"),
                FAILED: lambda: C.red("FAILED   ")}


def _status_tag(status):
    return _STATUS_TAGS.get(status, lambda: C.yellow("skipped  "))()


class Runner:
    """Holds the position, draws the menu, dispatches one item at a time.

    Batch selection is gone with the numbers it was keyed on. It is incoherent
    with a position — the second number's legality depends on the first one's
    outcome — and the rule it needed (a destructive step may only run alone,
    so '10 9' cannot erase the footage before the deploy that proves it was
    published) is subsumed by the graph: Clean Workspace's outbound is {1}.
    """

    def __init__(self, ctx, menu_items, position):
        self.ctx = ctx
        self.menu = menu_items
        self.position = position

    def loop(self):
        while self._turn():
            pass

    def _turn(self):
        world = capture_world(self.ctx, menu.Scope.LOCAL)
        print_menu(self.ctx, self.menu, self.position, world)
        print()
        _HINTED[0] = True                      # no hint on the menu itself
        return self._dispatch(ask("Select> ", quits=False).strip().lower())

    def _dispatch(self, sel):
        if sel in ("q", "quit", "exit"):
            return False
        return self._not_quit(sel)

    def _not_quit(self, sel):
        if sel in ("s", "status"):
            print_status(self.ctx)
            return True
        return self._select(sel)

    def _select(self, sel):
        """One number. Batch selection went with the numbers it was keyed on:
        the second item's legality depends on the first one's outcome."""
        if not self._is_item(sel):
            print(C.red("  Pick one item, or s for status, or q to quit."))
            return True
        return self._offered(int(sel))

    def _is_item(self, sel):
        return sel.isdigit() and int(sel) in self.menu

    def _offered(self, number):
        if number not in self.position.selectable(self.menu):
            self._not_from_here(number)
            return True
        self.run_one(number)
        return True

    def _not_from_here(self, number):
        print(C.yellow("  %d) %s does not follow %s."
                       % (number, self.menu[number].name(),
                          _where_line(self.menu, self.position)[4:])))

    def run_one(self, number):
        """One item, against a world captured for ITS scope, right now.

        Not the world the menu was drawn with: that one is a prompt old, and
        the card can be swapped while the prompt is on screen.
        """
        item = self.menu[number]
        print()
        print(C.bold("== %d) %s" % (number, item.name())))
        print(C.dim("     " + item.description()))
        hint_reset()
        outcome = self._execute(item)
        self.position.advance(item)
        return outcome

    def _execute(self, item):
        try:
            return item.execute(capture_world(self.ctx, item.SCOPE))
        except Aborted:
            print()
            print(C.yellow("  Interrupted — %s stopped." % item.name()))
            ctx_record(self.ctx, item, "interrupted")
            return menu.stopped("interrupted")


def ctx_record(ctx, item, detail):
    """An abort is recorded as one, and does NOT complete the item.

    execute() never returned, so the item's own outcome is unset; the position
    stays where it was, which is what "steps back by one" means for a move
    that did not take effect.
    """
    ctx.results.append(StepResult(item.name(), FAILED, 0, detail))
    item._outcome = menu.stopped(detail)


def build_runner(ctx, classes=None):
    """Wire the state machine for this ctx. Injectable for a test.

    The strategy is resolved once, here, and the ten items are constructed
    for it. `classes` lets a test drive the whole loop with mocks instead of
    the real ten.
    """
    strategy = menu.Strategy.of(ctx)
    menu_items = menu.build_menu(strategy, Work(ctx), classes)
    position = menu.position_for(menu_items)
    position.orient(capture_world(ctx, menu.Scope.LOCAL), items.COLD_START_RULES)
    return Runner(ctx, menu_items, position)


def _no_colour():
    """NO_COLOR is honoured because that is the environment's convention and
    not this tool's setting."""
    if os.environ.get("NO_COLOR"):
        C.enabled = False


def _lock_taken(ctx):
    print()
    print(C.red("  Another instance is already running against %s." % tilde(ctx.out_dir)))
    print(C.dim("  Quit it first. (Lock: %s — a crashed instance's lock clears"
                % tilde(ctx.out_dir / LOCK_FILE)))
    print(C.dim("  itself; this one's owner is still running.)"))
    return 2


def _chain(ctx):
    """What THIS installation actually does, which is not the same on every
    machine: with nothing configured the chain really does stop at a local
    page, and saying "-> S3 -> site" there would be a promise the greyed-out
    menu below then breaks."""
    if menu.Strategy.of(ctx) is menu.Strategy.WEBSITE_REPO:
        return "card -> render -> S3 -> site"
    return _local_chain(ctx)


def _local_chain(ctx):
    if ctx.site:
        return "card -> render -> site"
    return "card -> render -> local site"


def _exit_code(ctx):
    if any(map(_failed, ctx.results)):
        return 1
    return 0


def _failed(result):
    return result.status == FAILED


def _run_menu(ctx):
    """The menu loop IS the state machine: it draws from the position and the
    world, and dispatches one item at a time.

    There is no "Go?" any more. Every item that destroys asks for its own word
    — DROP, CLEAN, ERASE — after showing exactly what goes, and a blind
    confirmation in front of that is practice at pressing enter, which is the
    habit the typed word exists to defeat.
    """
    try:
        build_runner(ctx).loop()
    except (KeyboardInterrupt, Aborted):
        print()
        print(C.yellow("  Interrupted."))
    finally:
        show_cursor()
        release_single_instance_lock(ctx)
        print_summary(ctx)


def main(argv=None):
    """No command line. Everything this needs is in config.txt.

    A flag with a config equivalent means the same question has two answers
    that can disagree — and a default compiled in here gets inherited by a
    fresh checkout that never set it, with the erase following the compiled-in
    path into somebody else's data. One source, and it is the file the person
    edits.
    """
    _no_colour()
    ctx = Ctx()
    # One instance per working area. A second menu against the same tree
    # trusts scans the first one may be invalidating right now, and the erases
    # trust the scans. The lock self-clears when its pid is gone, so a crash
    # cannot strand it.
    if not acquire_single_instance_lock(ctx):
        return _lock_taken(ctx)
    return _start(ctx)


def _start(ctx):
    print()
    print(C.bold("  dashcam pipeline") + C.dim("   " + _chain(ctx)))
    # Checked before the status screen: there is nothing useful to show if the
    # numbers behind it would come from the wrong grouping.
    if not require_ego_motion(ctx):
        return 3
    print_status(ctx)
    _run_menu(ctx)
    return _exit_code(ctx)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # main() catches this around the menu loop, but not around startup — so
        # a ctrl-C while the ego-motion check or the config load was still going
        # printed a traceback, which reads like a crash caused by pressing it.
        print()
        print("  Cancelled.")
        sys.exit(130)
    finally:
        show_cursor()


# ---------------------------------------------------------------------------
# Non-obvious facts the code above is shaped around — kept here rather than in
# a doc, because they are the reasons for that shape.
#
# * Two import LAYOUTS coexist — <root>/<YYYY-MM-DD>/DCIM from the import
#   script, and a DCIM tree sitting directly in <root> from older imports — and
#   import_candidates() accepts both, with every render/scan/delete passing an
#   explicit --root. There is only ONE import ROOT, though: the CLI passes
#   DASHCAM_IMPORT_ROOT to the import script, so the script cannot default to a
#   sink of its own while config's `root` says somewhere else — that split
#   would send the copy to a folder nothing downstream reads. One answer to
#   "where did it go".
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
