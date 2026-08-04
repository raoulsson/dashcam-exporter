#!/usr/bin/env python3
"""pipeline.py — the whole dashcam publishing pipeline, in one interactive CLI.

Card -> import -> sidecars -> preview -> render -> build -> publish -> clear
the workspace, and separately free the card. Each of those already has a
script; the point of this file is that nobody should have to remember which
script, with which flag. Run it, look at the status screen, pick an item.

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

This repo does import, render and a local page on its own. Publishing is
supplied from outside: `upload_plugin` names a file and the two classes in
it — a uploader.Builder for item 5 and a uploader.Uploader for item 7 — and
where they send things is their business, not this module's. Set it and items 5
and 7 do what they do; leave it unset (what a fresh clone gets) and item 7 stays
greyed out with the reason printed underneath. Nothing in this repo contacts a
network host at any point — not as a setting, but because there is no networked
code left here.
"""
from __future__ import annotations

import collections
import contextlib
import base64
import html
import itertools
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
try:
    import termios
    import tty
except ImportError:      # not a POSIX terminal; typed lines still work
    termios = tty = None
import time
import traceback
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

# The state machine. Ordering is the graph's job, evidence is the guards',
# and the world is the one snapshot both judge. This module keeps the
# machinery — the functions that DO the work — and asks the items everything
# else.
import guards
import items
import menu
import uploader
import world as W
from checkout import RealCheckout
from menu import (PROGRESS, IMPORT, META, PREVIEW, EXCLUDE, RENDER, BUILD,
                  UPLOAD, CLEAN_WS, ERASE_CARD)

# The terminal itself: how things are spelled, how wide it is, the colours,
# and the progress bars. Moved out whole; imported back under the same names
# so every call site below still reads the way it always did.
from term import C, human_age, human_bytes, human_secs, rule, term_width, tilde
from progress import (Bar, Live, Waiting, _bar_line, _erase_line,  # noqa: F401
                      _eta, _still_bar, _sweep_line, _write_line, show_cursor,
                      waiting)

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
FALLBACK_WORKSPACE = "~/dashcam-data"             # the declared root, if unset
FALLBACK_IMPORT_ROOT = "~/dashcam-data/import"    # import-sd-card.sh DEST_ROOT
# There is deliberately no default for `upload_plugin`. A default would
# mean a clone loading and running someone else's code on every launch.

# Two facts, not one. They were the same directory until the sources moved
# under src/, and every use had to pick which it meant: the CHECKOUT is where
# config.txt, .env, .venv, VERSION and the git history live, and is the cwd a
# child process should run in; SRC_DIR is the one directory a plugin needs on
# sys.path so that `from uploader import ...` resolves. Naming the checkout
# after the source directory would have put the tool's memory in ~/.src.
#
# Both come off one Checkout now, so the two can no longer drift apart, and a
# Ctx can be pointed at a different tree without any of this being reassigned.
CHECKOUT = RealCheckout(__file__)
SRC_DIR = CHECKOUT.src()
EXPORTER_DIR = CHECKOUT.root()


# ---------------------------------------------------------------------------
# Config — same parser semantics as make_dashcam_videos.load_config_file, so a
# setting means here exactly what it means there.
# ---------------------------------------------------------------------------

def card_root(configured):
    """The directory that actually holds DCIM, at or under what was configured.

    A card copied off with Finder usually arrives wrapped: SimCard31/this/DCIM
    rather than SimCard31/DCIM, and pointing the setting one level too high is
    then indistinguishable from an empty card. Everything downstream says
    `card / "DCIM"`, so resolving it once here keeps every one of those right,
    including the erase.

    Breadth-first and shallow: the first DCIM found wins, and the walk gives up
    after a few levels rather than searching a disk somebody pointed this at by
    accident. Nothing found means the configured path, unchanged -- which reads
    as "not mounted", which is what an empty folder is.
    """
    found = _find_dcim([configured], depth=4)
    return found or configured


def _find_dcim(level, depth):
    if not level:
        return None
    return _found_or_deeper(level, depth)


def _found_or_deeper(level, depth):
    found = _holding_dcim(level)
    if found:
        return found
    return _find_dcim(_next_level(level, depth), depth - 1)


def _next_level(level, depth):
    if depth <= 1:
        return []
    return _subdirs_of(level)


def _holding_dcim(level):
    return next(filter(lambda d: (d / "DCIM").is_dir(), level), None)


def _subdirs_of(level):
    out = []
    for d in level:
        out += sorted(filter(_real_dir, _safe_iterdir(d)))
    return out


def _real_file(p):
    """A real file. A symlink is unlinked as the name it is, never followed:
    its size is not ours to count and its target is not ours to delete."""
    if p.is_symlink():
        return False
    return p.is_file()


def _real_dir(p):
    """A real directory. Symlinks are not followed: the walk would leave the
    place it was pointed at, and a link back up its own tree would not end."""
    if p.is_symlink():
        return False
    return p.is_dir()


def _safe_iterdir(d):
    try:
        return list(d.iterdir())
    except OSError:
        return []


def load_config(path):
    """key = value, '#' starts a comment to end of line, blank lines ignored.

    A value may name an earlier setting as ${that}:

        workspace  = ~/dashcam-data
        import_dir = ${workspace}/import

    Only settings already read are substituted, so a reference reads top to
    bottom like the file does and cannot chase its own tail. An unknown name is
    left exactly as written -- a path with ${typo} still in it fails where you
    can see it, where an empty one would quietly point at the filesystem root.

    Same rules as make_dashcam_videos.load_config_file, deliberately: two
    parsers that disagree about one file is two answers to every question.
    """
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _expand_refs(v.strip(), out)
    return out


def _expand_refs(value, so_far):
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda m: so_far.get(m.group(1), m.group(0)), value)


# Settings that name a place belonging to one person. config.txt is tracked,
# so putting real values there commits them — which is exactly what happened,
# and why they resolve from the gitignored .env first. Same rule the home
# coordinates already followed: config.txt may carry a commented EXAMPLE, the
# real value lives in .env or not at all.
PRIVATE_KEYS = ("upload_plugin", "home_lat", "home_lon", "card")


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


def _loaded_plugin(spec, exporter_dir):
    """The configured plugin — both its classes — or None for the local edition.

    UploaderNotLoaded is deliberately not caught here. It reaches main(), which
    prints the reason and exits without drawing a menu.
    """
    if not spec:
        return None
    return uploader.load_plugin(spec, exporter_dir)


class Ctx:
    """Everything the steps need: resolved paths, config, and session state."""

    def __init__(self, checkout=None):
        # Defaulted, because a Ctx is built with no arguments in a dozen places
        # and none of them has an opinion about where the checkout is. Passing
        # one is how a test gets a Ctx that reads a config.txt it wrote itself
        # rather than the operator's.
        self.checkout = checkout or CHECKOUT
        self.exporter = self.checkout.root()
        self.config_path = self.checkout.config_file()
        self.cfg = load_config(self.config_path)
        # .env overlays config.txt for the private keys, and a real environment
        # variable beats both — so a one-off run can point somewhere else without
        # editing a file.
        env = load_env(self.checkout.env_file())
        for key in PRIVATE_KEYS:
            name = "SET_" + key.upper()
            val = os.environ.get(name) or env.get(name)
            if val:
                self.cfg[key] = val

        # Who publishes, if anybody. "<path to a .py>:<Builder>:<Uploader>",
        # one plugin supplying the two acts. Absent means the local edition,
        # exactly as an unconfigured install has always behaved; present and
        # broken stops the tool rather than quietly becoming the local edition,
        # because a menu that silently stops publishing looks exactly like a
        # menu that is publishing fine.
        # The source directory, not the checkout: this is what goes on
        # sys.path so a plugin's `from uploader import ...` resolves.
        self.plugin = _loaded_plugin(self.cfg_opt("upload_plugin"),
                                     self.checkout.src())

        # The workspace holding the footage to work on. `root` is the old name
        # and still read, because configs carrying it exist; import_dir wins.
        # Its fallback is the workspace, NOT the card — the card is `card`, and
        # a root that defaulted to a mount point is what made "where does this
        # render from" have two plausible answers.
        # The workspace is DECLARED, never inferred. Everything that belongs
        # to a session rather than to the footage -- the lock and the run logs
        # -- lives here, and working it out from import_dir's parent broke the
        # moment someone pointed import somewhere real: import_dir=/usr/bin
        # made /usr the workspace. Declared, `workspace=/tmp` with import and
        # output on two other disks is a sentence that still means something.
        self.workspace = Path(self.cfg.get("workspace")
                              or FALLBACK_WORKSPACE).expanduser()
        self.render_root = Path(self.cfg.get("import_dir")
                                or (self.workspace / "import")).expanduser()
        # export_dir, because that is what the tool does and what the project
        # is called. `output_dir` and `out` are the names it had before and are
        # still read: a config written last month should not stop working
        # because a key got a better name.
        self.out_dir = Path(self.cfg.get("export_dir") or self.cfg.get("output_dir")
                            or self.cfg.get("out")
                            or (self.workspace / "export")).expanduser()
        # Where import-sd-card.sh drops the card. It follows config's `root`,
        # because that is what every render, scan and delete is pointed at — when
        # the two diverged (renaming `root` while the script kept its own
        # default) the copy landed in a folder no later step ever looked in, and
        # nothing said so. DASHCAM_IMPORT_ROOT still wins for a one-off.
        self.import_root = Path(os.environ.get("DASHCAM_IMPORT_ROOT")
                                or self.render_root).expanduser()
        self.card = card_root(Path(self.cfg.get("card", FALLBACK_CARD)).expanduser())
        # Where the LOCAL edition puts the finished site. Item 7 under that
        # edition is a copy into here rather than a network transport; with a
        # plugin configured nothing reads it. Defaults beside the export tree
        # so an unconfigured install still has an answer to print.
        self.website_export_dir = Path(
            self.cfg.get("default_website_export_dir")
            or (self.workspace / "website")).expanduser()

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
        # Outside every working area, because it outlives all of them. Not
        # configurable: it is this machine's record of what it has finished,
        # and a second copy under a second setting would be a second answer.
        self.archive_dir = ARCHIVE_DIR
        # Beside the trees, not inside either. Import holds footage and output
        # holds renders; a record of what happened is neither, and both get
        # wiped wholesale to know the workspace is clean. Nothing but footage
        # lives under import now.
        self.log_dir = self.workspace / "logs"
        self.state_dir = state_dir_for()
        self.lock_file = lock_path_for(self.workspace)
        _migrate_state(self.out_dir, self.state_dir)
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

        Empty counts as absent: `upload_plugin =` with nothing after it is
        someone clearing the setting, and the alternative — an empty string
        that still gets used — is a spec that cannot be parsed and a tool that
        refuses to start over a line the operator thought he had removed.
        """
        v = (self.cfg.get(key) or "").strip()
        return v or None


# ---------------------------------------------------------------------------
# Subprocess streaming
# ---------------------------------------------------------------------------

class Aborted(Exception):
    """The operator stopping the step, from a prompt or during the work.

    `mid_run` is the difference between the two, and it is the only thing a
    reader of the log a week later needs: nothing was touched before the work
    started, and something was part way through once it had. A prompt raises
    it plain; the child-process streamer raises it mid_run.
    """

    def __init__(self, mid_run=False):
        super().__init__()
        self.mid_run = mid_run


def _reader(stream, q):
    """Split the child's output on BOTH \\n and \\r.

    rsync --info=progress2 and many upload tools draw their progress by rewriting one
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


def _renderer_env(ctx):
    """What every run of the renderer needs told, in one place.

    LOG_DIR above all: make-trips-rendered.sh defaults it to <out>/logs, so a
    call that did not pass it wrote its run log into the EXPORT tree — beside
    the sidecars, inside the directory item 8 sweeps, and nowhere near the
    logs/ the workspace keeps at its root. Only the render step was passing
    it, so every sidecar pass left a log in the wrong place.
    """
    return {"LOG_DIR": str(ctx.log_dir)}


def run_stream(cmd, cwd, label, parser=None, keep=None, passthrough=False,
               env_extra=None, tail_lines=40, stdout_file=None, note_first=True,
               quiet_finish=False):
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
            # The bar is deliberately narrow: the room goes to the log tail
            # below, which is the part that says it is still alive.
            head = _bar_line(label, frac, elapsed, note, note_first)
            # note_first has already put it in the line; appending it again
            # would print it twice. NOT by rebinding `note` -- this is a
            # closure over it, and an assignment here makes every read in this
            # function local, including the one on the line above.
            used = note_first and bool(note)
        else:
            # The indeterminate bar, not a spinner. Both say "still working",
            # but only one of them looks like the rest of the tool: a step with
            # a countable unit draws [####......] and a step without one drew a
            # bare |/-\, so the deploy read as a different program from the
            # render three lines above it. Waiting already knew how to draw
            # this for blocking calls; it just had no way in from here.
            head = _sweep_line(label, spin, elapsed)
            used = False
        if note and not used:
            head += "  " + C.yellow(note)

        # give whatever is left of the terminal to the child's latest line.
        # _visible_len, not len: the bar inside `head` carries colour now, and
        # counting its escapes as width would shrink the tail by a dozen
        # characters that are not on the screen.
        room = term_width() - _visible_len(head) - 4
        tail = ""
        if last_raw and room > 12:
            t = last_raw.strip()
            # The note already carries the counter, so a tail that starts with
            # "[scan  17/ 239]" spends its width repeating it. Strip the bracket
            # and show what it identifies — the file being worked on.
            if note:
                # Drop whatever the note already says. Two shapes do this:
                # "[scan  17/ 239] NAME" and aws's "Completed 6.0 MiB/13.0 GiB
                # (457.4 KiB/s) with 6 files remaining" — in both, the head of
                # the line is the counter we have already extracted, and the
                # useful remainder (the filename, or the rate and files left)
                # was being pushed off the right edge by it.
                t = re.sub(r"^\[[^\]]*\]\s*", "", t)
                t = re.sub(r"^Completed\s+[\d.]+\s*\w+\s*/\s*~?[\d.]+\s*\w+\s*", "", t)
            t = _fit(t, room)
            tail = "  " + C.dim(t)
        # No wrapper round the whole line: every piece carries its own colour
        # now, and a colour that spans a nested one ends at the nested one's
        # reset -- which is how the closing bracket and everything after it
        # lost the amber the opening bracket had.
        live.draw(["  " + head + tail])

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
        raise Aborted(mid_run=True)
    finally:
        # A finished step should leave a line behind saying so. live.close()
        # erases the live area, so without this the progress simply vanishes and
        # the screen gives no evidence the work happened or how much of it.
        if rc == 0 and live.enabled and not quiet_finish:
            el = human_secs(time.time() - started)
            bar = "#" * 24
            tail_bits = " ".join(x for x in (counts,) if x)
            live.draw([C.green("%s [%s] 100%% %s  %s  completed"
                               % (label, bar, el, tail_bits)).rstrip()])
            print()          # commit that line; the next erase starts below it
        elif rc == 0 and live.enabled:
            live.close()     # the caller has its own sentence for this
            live.height = 0
        live.close()
        if out_fh:
            out_fh.close()

    if rc != 0:
        # The command line is this module's business: which flags it composed
        # and where the script lives. What the operator can act on is the tail
        # below, which is what the child said before it gave up.
        print(C.red("  %s failed (exit %d). Last lines:" % (label, rc)))
        tail = [l for l in lines if l.strip()][-tail_lines:]
        if tail:
            print(C.dim("  --- last %d lines of output ---" % len(tail)))
            for l in tail:
                print(C.dim("  " + l))
    return rc, lines


# ---------------------------------------------------------------------------
# Parsers — one per tool, each derived from that tool's real output format.
# ---------------------------------------------------------------------------

# rsync --info=progress2:  "  1,234,567,890  47%  120.55MB/s    0:01:23 (xfr#…)"
RE_RSYNC_P2 = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d{2}:\d{2})")
# make_dashcam_videos:     "[Trip 2/5] 2026-07-19 12:46 -> 13:20  (87 clips, ~14:02)"
# The scanner announces each clip as it reads it: "[scan   17/ 239] NAME.mp4".
# That loop is the long silent stretch of a scan, so it is the only thing that
# can honestly drive a bar there.
RE_SCAN = re.compile(r"^\[scan\s+(\d+)/(\d+)\]")
RE_TRIP = re.compile(r"^\[Trip\s+(\d+)/(\d+)\]\s*(\S+\s+\S+)?")
#                          "  [ 12/ 87] 2026-07-19 12:46:03  encoding ..."
RE_CLIP = re.compile(r"^\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]")

def make_import_parser(total):
    """Progress over the whole copy, which rsync will not tell us.

    macOS ships openrsync, whose --progress reports ONE FILE at a time: each
    clip runs 0 to 100 and the percentage says nothing about the job. So the
    bytes are accumulated here — every file that reaches 100% adds its size to
    the total moved — and measured against what the import screen already
    worked out it would copy.

    The line rsync prints before each file is its name, so the last one that
    was not a progress line IS what is being copied right now.
    """
    state = {"name": "", "done": 0, "files": 0}

    def parse(line):
        m = RE_RSYNC_P2.match(line)
        if not m:
            _remember_name(state, line)
            return None
        moved = state["done"] + int(m.group(1).replace(",", ""))
        if int(m.group(2)) >= 100:
            state["done"], state["files"] = moved, state["files"] + 1
        return _import_progress(state, moved, total, m.group(3))

    # What it counted, for the one line printed when the copy finishes. The
    # shell script says "Done. Imported N files" about the whole destination;
    # this is what THIS run moved.
    parse.state = state
    return parse


def _remember_name(state, line):
    """rsync names a file on its own line, then reports on it. Anything with
    no digits-and-percent shape is that name."""
    name = line.strip()
    if name and not name.startswith(">>>"):
        state["name"] = name.rsplit("/", 1)[-1]


# Fixed columns, so nothing on the line moves except what it is measuring.
# A clip is "20260730141804_0060.mp4" -- 23 characters, the same every time --
# and the rate and the two sizes are right-aligned against their widest form
# ("271.36MB/s", "142.3 MB"). Left to size themselves, every field jumped
# sideways whenever a number gained a digit, which is the whole line wiggling
# to report that one clip is faster than the last.
# 9 on the sizes, not 8: human_bytes stays in MB up to 1024, so it emits
# "1023.0 MB" -- nine characters -- for the twenty-four megabytes between 1000
# and a gigabyte. A field one short there shifts everything to its right by a
# character, twice per gigabyte, which is the left half of the line wiggling.
NAME_W, RATE_W, SIZE_W = uploader.NAME_W, uploader.RATE_W, uploader.SIZE_W


def _import_progress(state, moved, total, rate):
    if not total:
        return None
    note = uploader.progress_note(state["name"], rate, moved, total)
    # \0: no log tail after this. The tail shows the child's last raw line,
    # and for rsync that line IS where these numbers came from -- it printed
    # the rate and a percentage again, truncated, after the ones on the left.
    return min(moved / float(total), 1.0), note + "\0"


def _fitted(text, width):
    if len(text) > width:
        return text[:width - 1] + "…"
    return "%-*s" % (width, text)


def _right(text, width):
    return "%*s" % (width, text)


def done_line(what):
    """The one line a step leaves behind when it worked.

    Every step used to end on run_stream's own "Label [####] 100% 0:34
    completed" -- a bar redrawn full to announce that the finished thing had
    finished, in the streamer's words rather than the step's. This is the
    step's own sentence, and there is exactly one of it.
    """
    print(C.green("  100%% - %s." % what))


def _fit(text, room):
    """Trim a child's line to the space left, keeping BOTH ends.

    It used to keep only the start, on the reasoning that an encoder puts what
    identifies a line at the front. That is true of "[ 4/6] 2026-07-28
    encoding" and false of "concatenating 99 clips -> trip_2026-07-28_14-14_01
    _h1080.mp4", where the front is the same words every time and the name at
    the end is the only part that says which trip is being written. Cut from
    the front, that line reads "concatenating 99 clips -> trip_2026-".

    So the middle goes. Whatever the line's shape, the two things worth reading
    -- what is happening, and what it is happening to -- are at the ends.
    """
    if len(text) <= room:
        return text
    if room < 12:
        return text[:room]
    # Clamped so head stays at least 1. Unclamped it went negative in a narrow
    # terminal, and text[:-1] slices from the END -- returning nearly the whole
    # line into a space that could not hold it.
    tail = min(room - 2, max(12, room // 2))
    head = room - tail - 1
    return text[:head] + "…" + text[-tail:]


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
                        "%d/%d read, finding trip boundaries\0" % (state["n"], state["n"]))
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
    state = {"trips_seen": 0, "trips_total": None, "clip": 0, "clips": 0,
             "name": ""}

    def parse(line):
        m = RE_TRIP.match(line)
        if m:
            state["trips_seen"] += 1
            state["trips_total"] = int(m.group(2))
            state["clip"], state["clips"] = 0, 0
            # WHICH trip, from the header the renderer already prints. Every
            # other bar names what it is working on in the left-hand column;
            # this one had only a counter, and on a card of one long trip a
            # counter reading 1/1 says nothing at all.
            state["name"] = (m.group(3) or "").strip()
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
        doing = "trip %d/%d" % (state["trips_seen"], total)
        if state["clips"]:
            doing += "  clip %d/%d" % (state["clip"], state["clips"])
        return min(frac, 1.0), uploader.progress_note(state["name"], tail=doing)

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
    # published"; six named right would read as a finished round. The sweep still
    # takes everything — this is only about what counts as EVIDENCE.
    return sorted(p for p in out_dir.rglob("trip_*.mp4")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(out_dir).parts))


def _edition_rows(ctx):
    """Which edition this is, and under the uploader one, what is registered.

    The two editions have different deliverables, so they report different
    rows. A "Local site: not built" line on an install that publishes is not
    status — item 5 does not write that page here and never will, so the row
    is a permanent complaint about a file nobody wants.

    What matters instead is the thing that WOULD be hard to find out: that a
    plugin is registered at all, which classes, and from which file. Reading it
    off the spec rather than off a name the implementation supplies means it
    cannot drift, and it answers "where is this coming from" without going and
    grepping the config.
    """
    if ctx.plugin is None:
        return _local_site_rows(ctx)
    return _plugin_rows(ctx.plugin)


def _plugin_rows(plugin):
    return (_setting("Export-Mode", "uploader: User plugin handles build and "
                                "upload of website"),
            _setting("Plugin", "%s, %s" % (type(plugin.builder).__name__,
                                       type(plugin.uploader).__name__)),
            _setting("Location", tilde(Path(plugin.spec.split(":")[0])), indent=4),
            _setting("Description", plugin.uploader.describe(), indent=4))


def _local_site_rows(ctx):
    """The local edition's deliverable, which is a file on this machine."""
    return (_setting("Export-Mode", "local page: one self-contained .html, "
                                "no upload_plugin configured"),
            _setting("Local site", _built_or_not(_result_page(ctx))))


def _result_page(ctx):
    """Where the page is, which is inside the newest final_ folder once one
    exists and beside the renders until then."""
    gathered = _gathered_page(getattr(ctx, "final_root", ctx.out_dir))
    return gathered or (ctx.out_dir / RESULT_FILE)


def _gathered_page(froot):
    finals = _final_dirs(froot)
    if not finals:
        return None
    return finals[-1] / RESULT_FILE


def _final_dirs(froot):
    if not froot.is_dir():
        return []
    return sorted(froot.glob(FINAL_PREFIX + "*"))


def _built_or_not(page):
    if not page.is_file():
        return "%s  %s" % (C.dim("not built"), C.dim(tilde(page)))
    return "%s  %s" % (C.bold(tilde(page)), C.dim(_age_phrase(page)))


def _age_phrase(page):
    age = human_age(time.time() - page.stat().st_mtime)
    if age == "just now":
        return "built just now"
    return "built %s ago" % age


def _state(label, state, where):
    """One status row: what it is, what state it is in, and where that is.

    The path in brackets at the end, in its own column, because it is the same
    path every launch and the state is the part that changed. Left where the
    state was, the eye has to find the one word that moved among three
    different-length paths.

    The state column is wide enough for the longest state any row can hold --
    "mounted  239 clips (48.7 GB)" -- because a column that only usually lines
    up is one the eye stops trusting, and then the path it was drawn for is no
    easier to find than it was before.
    """
    return "    %s%s(%s)" % (_padded(label, 13), _padded(state, 30), C.dim(where))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text):
    """How wide it prints. %-18s pads to the length of the STRING, and a
    coloured one carries a dozen invisible bytes of escape -- so every column
    holding colour came out short by however much colour was in it."""
    return len(_ANSI.sub("", text))


def _padded(text, width):
    return text + " " * max(1, width - _visible_len(text))


def _card_rows(ctx):
    """Mounted or not, and where. One row, one question answered."""
    if not (ctx.card / "DCIM").is_dir():
        return (_state("SIM Card", C.dim("not mounted"), tilde(ctx.card)),)
    return (_state("SIM Card", C.green("mounted") + "  " + _card_note(ctx),
                   tilde(ctx.card)),)


def _render_state(mp4s, size):
    """Dim, not amber. Nothing rendered yet is the ordinary state at the start
    of a cycle, exactly like an empty import -- and a colour that says "look at
    this" about the normal case has spent itself by the time something is
    actually wrong.

    Nor bold when there is something: no value in this block is. Bold is the
    screen saying "this one" and every row saying it is the same as none of
    them saying it. What survives is colour that MEANS something -- green for
    a card in the slot, dim for a state that is simply absent, red for a disk
    that cannot take what is queued."""
    if not mp4s:
        return C.dim("none")
    return "%d videos, %s" % (len(mp4s), human_bytes(size))


def _volume_rows(ctx):
    """One row per volume in play, not one per directory and not one "disk".

    Import and export normally live on the same disk, so a row per directory
    reads as two disks with suspiciously identical numbers. But they need not:
    the export can sit on an external drive while the workspace stays in home,
    and then a single nameless footnote answers the wrong question. Dedup by
    mount point and let the mount point name the row -- one line if all three
    directories are on the same volume, three if they are on three.
    """
    queued = _queued_bytes(ctx)
    return tuple(filter(None, (_disk_row(m, queued.get(m, 0))
                               for m in _mounts_in_play(ctx))))


def _queued_bytes(ctx):
    """What each volume is about to be asked for, in bytes.

    A fixed floor cannot answer this. 15 GB free passes a "is there room" check
    and the run still blows up when the pending import is 15 GB and its renders
    are another 8 -- and by default import and export are the same volume, so
    both demands land on the same disk. So add up, per mount point, what is
    actually waiting: the clips the next import would copy off the card, and
    the footage already imported that still has to be encoded.
    """
    need = {}
    _add_need(need, _mount_of(ctx.import_root), _pending_import_bytes(ctx))
    _add_need(need, _mount_of(ctx.out_dir), _pending_render_bytes(ctx))
    return need


def _add_need(need, mount, size):
    need[mount] = need.get(mount, 0) + size


def _pending_import_bytes(ctx):
    """Bytes the next import would copy: the clips on the card newer than the
    high-water mark, both cameras. Zero once the card has been taken in, which
    is the point -- a demand that never clears is a warning nobody reads."""
    after = last_imported_stamp(ctx)
    return sum(_size_of(p) for p in _card_clips(ctx.card) if _is_new_clip(p, after))


def _card_clips(card):
    return (card / "DCIM").rglob("*.mp4") if (card / "DCIM").is_dir() else ()


def _is_new_clip(p, after):
    m = STAMP_RE.search(p.name)
    return not (m and after and m.group(1) <= after)


def _pending_render_bytes(ctx):
    """Room the encode still needs on the export volume.

    Bounded by the source it reads, which is inferred rather than measured: a
    trip encoded at output_height is smaller than the clips it came from in
    every render this tool has done, and the intermediates are swept after. So
    this over-states, deliberately -- erring high on a "will it fit" question
    costs a warning, erring low costs a dead run at 90%.

    Counted only while nothing is rendered yet. Once renders exist the encode
    is done or half done, and there is a Rendered row above saying so.
    """
    if rendered_mp4s(ctx.out_dir):
        return 0
    return sum(tree_size(p / "DCIM") for p in import_candidates(ctx))


def _mounts_in_play(ctx):
    """Distinct mount points, in the order the configuration names them."""
    seen = []
    for path in (ctx.workspace, ctx.import_root, ctx.out_dir):
        mount = _mount_of(path)
        if mount not in seen:
            seen.append(mount)
    return seen


def _mount_of(path):
    """The volume `path` sits on: the first ancestor that is a mount point.

    Ancestors that do not exist are simply walked past -- a directory the first
    run has not created yet is still on a disk, and disk_usage on a missing
    path raises rather than answering for the volume it would live on."""
    for p in (path,) + tuple(path.parents):
        if _is_mount(p):
            return p
    return Path("/")


def _is_mount(p):
    return p.exists() and os.path.ismount(str(p))


def _disk_row(mount, needed):
    try:
        usage = shutil.disk_usage(str(mount))
    except OSError:
        return None
    return _state("Disk", _free_state(usage, needed), _volume_path(mount))


def _free_state(usage, needed):
    # Plain, not bold: bold is for the one thing on the screen that wants the
    # eye, and a figure that reads the same every launch is not it. Red is
    # earned by a number that says the next step cannot finish.
    free = "%s free of %s" % (human_bytes(usage.free), human_bytes(usage.total))
    # The queued figure only earns its place when it does not fit. Printed on
    # every launch beside a disk with 140 GB free it was a second number to
    # read past to get to the one that answers the question, and it moved --
    # so it looked like news every time it changed, about nothing.
    if needed and usage.free < needed:
        return C.red("%s — will not fit the %s queued" % (free, human_bytes(needed)))
    return free


def _volume_path(mount):
    """The name the volume goes by. macOS mounts the boot disk at "/" and puts
    a named symlink to it in /Volumes -- that name is what Finder shows and
    what is written on the drive, so it beats a bare slash. Anything else is
    already mounted under its own name."""
    for entry in _safe_iterdir(Path("/Volumes")):
        if os.path.realpath(str(entry)) == os.path.realpath(str(mount)):
            return str(entry)
    return str(mount)


def _card_note(ctx):
    """What this card is WORTH, not what it holds.

    The clips an import would fetch and their weight, because that is the
    figure the operator is deciding on. A DDPAI card hoards: it keeps every
    old day until the space is needed, so the total is mostly rounds this
    machine finished with weeks ago and says nothing about the work in front
    of him.
    """
    try:
        _here, todo, size, _files, _done, _done_size = _delta_counts(
            ctx, last_imported_stamp(ctx))
    except Exception:
        n = clip_count(ctx.card)
        return C.dim("%s clips" % (n if n is not None else "?"))
    return C.dim("%d clips (%s)" % (todo, human_bytes(size)))


def print_configuration(ctx):
    """What this install IS, printed once at launch.

    The settings, not the state: which card, which three directories, which
    interpreter, which edition, which plugin. It answers "is this thing pointed
    where I think it is", which is asked once on starting up and never again --
    so it is printed once and the menu never repeats it.
    """
    print()
    print(rule("Configuration (read from %s)" % tilde(ctx.config_path)))
    _print_all(_config_rows(ctx))


def _config_rows(ctx):
    return (_setting("SIM Card", tilde(ctx.card)),
            _setting("Workspace Directory", tilde(ctx.workspace)),
            _setting("Import Directory", tilde(ctx.import_root)),
            _setting("Export Directory", tilde(ctx.out_dir)),
            _setting("Running in", tilde(ctx.exporter)),
            ) + _edition_rows(ctx) + (
            # Last, and after the edition, because it is not a setting: nobody
            # configures it here, it is whatever interpreter the launcher found.
            # It is printed at all so a "works on mine" report says which one.
            _setting("Python", "%s (%s)" % (platform.python_version(),
                                            tilde(Path(sys.executable)))),
            )


def _setting(label, value, indent=2):
    return "%s%-*s %s" % (" " * indent, 22 - indent, label, C.dim(value))


def print_status(ctx):
    print()
    print(rule("Status"))

    _print_all(_card_rows(ctx))

    # Import sink
    cands = import_candidates(ctx)
    if cands:
        for p in cands:
            n = clip_count(p)
            print(_state("Imported", "%s clips, %s"
                         % (n if n is not None else "?",
                            human_bytes(tree_size(p))), tilde(p)))
    else:
        # Name the folder the config actually points at — import_root is only a
        # fallback, and showing it sends you to create the wrong directory.
        print(_state("Imported", C.dim("empty"),
                     tilde(ctx.render_root if ctx.render_root else ctx.import_root)))

    # Renders
    mp4s = rendered_mp4s(ctx.out_dir)
    size = sum(p.stat().st_size for p in mp4s) if mp4s else 0
    print(_state("Rendered", _render_state(mp4s, size), tilde(ctx.out_dir)))

    # Where all of that has to fit. A status row like the others, because
    # running out of disk stops the render exactly the way a missing card does.
    _print_all(_volume_rows(ctx))


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


def read_key(prompt):
    """One keypress at the menu, no Enter. Falls back to a typed line.

    The menu is single characters -- a digit, p, h, i, q -- so making the
    operator press return after each one is a keystroke that carries no
    information. h is the exception: it takes a second key, and reads it the
    same way, so `h` then `4` is help about entry 4 and `h` then anything else
    is the general help.

    Falls back to input() when stdin is not a terminal, which is every test and
    any piped run, so nothing here changes what those see.
    """
    ch = _one_char_at(prompt)
    if ch is None:
        # Not a terminal: a typed line through the same prompt everything else
        # uses, so a piped run and every test are unchanged.
        return ask(prompt, quits=False).strip().lower()
    return _echoed(ch)


def _raw_capable():
    return sys.stdin.isatty() and termios is not None


def _one_char_at(prompt):
    if not _raw_capable():
        return None
    sys.stdout.write(C.bold(prompt))
    sys.stdout.flush()
    return _one_char()


def _one_char():
    """A single character in raw mode, or None when that is not possible."""
    if not _raw_capable():
        return None
    return _raw_read()


def _raw_read():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _echoed(ch):
    """Show what was pressed, since raw mode does not, and read h's second key."""
    if ch in ("\x03", "\x04"):
        print()
        raise Aborted()
    return _key_or_help(ch)


def _key_or_help(ch):
    if ch.lower() == "h":
        return _help_key()
    print(_printable(ch))
    return ch.strip().lower()


def _printable(ch):
    if ch.isprintable():
        return ch
    return ""


def _help_key():
    sys.stdout.write("h")
    sys.stdout.flush()
    second = _one_char() or ""
    print(_printable(second))
    return _help_command(second)


def _help_command(second):
    if second.strip():
        return "h " + second.strip()
    return "h"


def _hint_lines():
    """Said once per step, by whichever prompt comes first."""
    if _HINTED[0]:
        return ()
    _HINTED[0] = True
    return (C.dim("  (q or ctrl-c to go back)"),)


def ask(prompt, default="", quits=True):
    """quits: a bare q answers "take me back to the menu".

    Ctrl-C already did this, but only if you knew — and inside a step every
    prompt looks like it wants a value, so q was being read as one (as an index,
    as a height). It raises the same Aborted that Ctrl-C does, which the runner
    catches per item, so the item stops and the menu comes back. Off at the menu
    itself, where q is handled as a real answer.
    """
    _print_all(_hint_lines())
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
    """y, n, or Enter for the default -- one keypress, no return.

    A yes/no question carries one bit and the answer to it is one character,
    so asking for a character and then for return as well is a keystroke that
    says nothing. The menu has read this way for a while; the prompts inside
    the steps had not, which made them feel like a different tool.

    Anything that is not y, n or Enter is asked again rather than taken as a
    no. These sit in front of copies and erases, and a fat-fingered r reading
    as "no" is a silent wrong answer to a question the operator thought he had
    answered. q and ctrl-c still abort the step, as everywhere else.
    """
    suffix = " [Y/n] " if default else " [y/N] "
    return _yes_or_no(prompt + suffix, default)


def _yes_or_no(prompt, default):
    while True:
        answer = _read_answer(prompt, default)
        if answer is not None:
            return answer


def _read_answer(prompt, default):
    """None means "that was not an answer, ask again"."""
    if not _raw_capable():
        return _typed_answer(prompt, default)
    _print_all(_hint_lines())
    ch = _one_char_at(prompt)
    return _from_key(ch, default)


def _from_key(ch, default):
    """A key that is not an answer costs no line.

    Every re-ask used to print the prompt again underneath, so leaning on the
    keyboard left twenty identical lines above the one being answered. The
    question has not changed, so neither should the screen: erase and ask
    again in place.
    """
    if ch in ("\x03", "\x04", "q", "Q"):
        print()
        raise Aborted()
    answer = _meaning(ch.strip().lower(), default)
    if answer is None:
        _erase_line()
        return None
    print(_printable(ch))
    return answer


def _meaning(key, default):
    if not key:
        return default                       # Enter
    return {"y": True, "n": False}.get(key)  # None -> ask again


def _typed_answer(prompt, default):
    """Not a terminal: a typed line, which is every test and every piped run.

    It answers on the first line rather than looping, because a pipe that is
    out of input would spin here forever.
    """
    s = ask(prompt).lower()
    if not s:
        return default
    return s in ("y", "yes")


def pick_import(ctx, purpose):
    """Choose which import folder a step should work on."""
    cands = import_candidates(ctx)
    if not cands:
        print(C.dim("  No import folder with a DCIM tree under %s."
                    % tilde(ctx.import_root)))
        return None
    if len(cands) == 1:
        ctx.selected_import = cands[0]
        return cands[0]
    if ctx.selected_import in cands:
        keep = confirm("  Use %s for %s?" % (tilde(ctx.selected_import), purpose), True)
        if keep:
            return ctx.selected_import
    print("  Import folders:")
    for i, p in enumerate(cands, 1):
        n = clip_count(p)
        print("    %d) %-40s %s" % (i, tilde(p),
                                   C.dim("%s clips" % (n if n is not None else "?"))))
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
# Ctrl-C is the operator deciding, which is not the tool failing. The summary
# said FAILED in red beside a step he had stopped on purpose, and the process
# exited 1 for it. Its own word, and it does not colour the exit code.
ABORTED = "aborted"

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

LEDGER_FILE = "imported.json"


def read_ledger(ctx):
    try:
        return json.loads((ctx.state_dir / LEDGER_FILE).read_text())
    except Exception:
        return {}


def remember_step(ctx, number):
    """The last STEP the operator took, beside the ledger so it outlives a clean.

    Where the cycle has got to is the last thing he did, and nothing on disk
    says that as well as he does. Deriving it was always an inference, and the
    inference went wrong in both directions in one evening: a swept workspace
    read as "7) Upload Website" because the destination still says the trips
    are published, and then as "2) Generate Meta" because six receipts had not
    been archived yet.

    Not the views and not the keys. Progress, help, info and status answer a
    question without changing anything, so they are not where you are.

    A remembered position cannot lie its way past anything: it decides what is
    OFFERED, and every item still asks its own guard before it runs.
    """
    d = read_ledger(ctx)
    d["at"] = number
    _write_ledger(ctx, d)


def remembered_step(ctx):
    """The step this workspace was left at, or None.

    NOWHERE is not a step. It is the position saying "I could not tell", and
    written to the ledger it stops being an admission and starts being an
    answer: _resume trusts it, orient() never runs again, and the menu keeps
    offering only the start entries however much has since appeared on disk.
    That is how an interrupted first import became a dead end -- two clips in
    the workspace, item 1 refusing to import on top of them and pointing at
    item 8, and item 8 not on offer because the position was still nowhere.
    """
    at = read_ledger(ctx).get("at")
    if at == menu.NOWHERE:
        return None
    return at if isinstance(at, int) else None


def _write_ledger(ctx, d):
    try:
        state_path(ctx, LEDGER_FILE).write_text(json.dumps(d, indent=1))
    except OSError:
        pass


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
        state_path(ctx, LEDGER_FILE).write_text(json.dumps(d, indent=1))
    except OSError:
        pass


STAMP_RE = re.compile(r"(\d{14})")

# The camera's own drive/park event log, at the root of DCIM. Named here
# because it is the one file on the card that GROWS between an import and a
# look back at it: everything else is written once and rotated away whole.
CAMERA_LOG = "IPSRecord.txt"


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


EXCLUDED_FILE = "excluded.json"

# The receipts of finished cycles, outside the working area entirely.
#
# A trip's _meta.json used to survive the clean-up in place, which left the
# output tree holding six files that looked like six trips waiting to be
# rendered. They are not work; they are the record that the work was done, and
# the only thing still read from them is whether a card's clips are already
# inside a trip that was published -- the question 9) Delete SIM Data asks
# before erasing footage that has no second copy.
#
# So they move out on the way past, keeping their shape under the import they
# belonged to, and the working area is left genuinely empty. Nothing writes
# here except the clean-up and nothing reads it except that guard and the count
# on the status screen.
def home_dir_for(exporter):
    """The hidden directory this checkout remembers things in.

    Named after the checkout, so a second clone is a second edition with its
    own memory. `git clone ... dashcam-exporter-jondoe` gets
    ~/.dashcam-exporter-jondoe and shares nothing with the first -- its own
    high-water mark, its own receipts -- which is what lets someone run two
    without either one thinking the other's card was already imported.

    A name that already says what this is keeps it; anything else gets the
    prefix, so a directory called `myfork` does not put a bare ~/.myfork in
    somebody's home and leave them guessing what left it there.
    """
    return Path.home() / ("." + _home_name(Path(exporter).name))


def _home_name(dirname):
    name = re.sub(r"\s+", "-", dirname.strip())
    if name.startswith("dashcam-exporter"):
        return name
    return "dashcam-exporter-" + name


HOME_DIR = home_dir_for(EXPORTER_DIR)
ARCHIVE_DIR = HOME_DIR / "processed"


def _migrate_state(out_dir, state_dir):
    """Carry the four files out of an older workspace, once.

    Best effort and fail-safe: a file that does not arrive reads as "never
    imported", which refuses a card wipe rather than permitting one.
    """
    try:
        _move_state(out_dir, state_dir)
    except OSError:
        pass


def _move_state(out_dir, state_dir):
    stale = list(filter(lambda p: _worth_moving(out_dir, state_dir, p),
                        WAS_CALLED.items()))
    if stale:
        _move_all(out_dir, state_dir, stale)


def _move_all(out_dir, state_dir, stale):
    state_dir.mkdir(parents=True, exist_ok=True)
    for now, before in stale:
        shutil.move(str(out_dir / before), str(state_dir / now))


def _worth_moving(out_dir, state_dir, pair):
    now, before = pair
    if (state_dir / now).exists():
        return False
    return (out_dir / before).is_file()


def state_path(ctx, name):
    """A state file's path, with its directory made. Writers only.

    Readers take the plain path and treat a missing file as "nothing recorded
    yet", which is the honest reading and the safe one: no ledger means no
    record that this footage exists anywhere else, and that refuses an erase.
    """
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    return ctx.state_dir / name


def state_dir_for(_import_root=None):
    """Where the bookkeeping lives: one place, for this machine.

    The files are ABOUT a working area rather than part of one, and leaving
    them in it meant the tree Clean Workspace empties could never actually be
    empty -- each needed an exemption from the sweep, which is a rule that has
    to stay right in the one place where being wrong deletes footage.

    NOT keyed by any path. The main thing recorded here is how far the imports
    have reached, and that is a fact about the CARD: point the import dir
    somewhere new -- which is the ordinary way to start clean -- and it is
    still the same card with the same clips already taken off it. Keyed by a
    path, that answer would be forgotten exactly when it matters, and the card
    would read as never imported.

    The pid lock is the exception and is keyed, because two trees genuinely can
    run at once and must not share one.
    """
    return HOME_DIR / "state"


def lock_path_for(workspace):
    """In the tree, and visible.

    Everything else the tool remembers is in ~/.dashcam-exporter, because it
    outlives any working area. The lock is the opposite: it says THIS tree is
    busy right now, it is meaningless once the process is gone, and it is the
    one file an operator has a reason to look for -- so it sits where he is
    already looking, under its own name rather than behind a dot.

    In the declared workspace, because that is what a session belongs to. You
    wipe import and output in Finder to know you are clean, and a lock is not
    something to have to notice while doing that. One per workspace, which is
    what lets two run at once, and it falls out of living there rather than out
    of a key.
    """
    return workspace / LOCK_FILE

BANNER = r"""
  ____            _                                   _____                       _            
 |  _ \  __ _ ___| |__   ___ __ _ _ __ ___           | ____|_  ___ __   ___  _ __| |_ ___ _ __ 
 | | | |/ _` / __| '_ \ / __/ _` | '_ ` _ \   _____  |  _| \ \/ / '_ \ / _ \| '__| __/ _ \ '__|
 | |_| | (_| \__ \ | | | (_| (_| | | | | | | |_____| | |___ >  <| |_) | (_) | |  | ||  __/ |   
 |____/ \__,_|___/_| |_|\___\__,_|_| |_| |_|         |_____/_/\_\ .__/ \___/|_|   \__\___|_|   
                                                                |_|
"""

BANNER_WIDTH = max(len(line) for line in BANNER.splitlines())


VERSION_FALLBACK = "0.0.0"
VERSION_FILE = "VERSION"
# Set by hand, and the only part of the version that is a decision. Bump it
# when the tool changes in a way that makes a habit wrong -- a menu that means
# something else, an output layout that moves. The other two numbers cannot
# carry that, because they only know how many commits there have been.
VERSION_MAJOR = 3


def version(exporter=None):
    """major.minor.patch: the major by hand, the rest off the commit count.

    418 commits is 3.4.18 -- hundreds are the minor, the remainder is the
    patch. It costs a subprocess at launch and it cannot disagree with what is
    checked out, which is more than a hand-kept constant manages: two thirds of
    the number are the history rather than a note about it.

    The digits used to be sliced apart instead (249 -> 2.4.9), which spent the
    major on nothing but the passage of a hundred commits and could say
    neither 99 nor 1000. Arithmetic on the count leaves the major free to mean
    the one thing a major number is for, and counts as high as you like.

    A copy without git -- an archive, an install -- has no history to count and
    says so rather than inventing one.
    """
    where = exporter or EXPORTER_DIR
    return _counted_or_recalled(where, _version_of(_commit_count(where)))


def _counted_or_recalled(where, counted):
    if counted == VERSION_FALLBACK:
        return _recalled(where)
    return _remembered(where, counted)


def _remembered(where, counted):
    """Leave it on disk, so a copy of this folder knows what it is.

    A deployed app is decoupled from the repository it was built from -- there
    is no history to count in a zip, an rsync or a Docker layer -- and a tool
    that cannot say which build it is is a tool nobody can report a bug
    against. Written whenever git CAN answer, so the file is never staler than
    the last run in a checkout, and carried along by whatever copies the
    folder.
    """
    _write_version(where / VERSION_FILE, counted)
    return counted


def _write_version(path, counted):
    if _already_says(path, counted):
        return
    _try_write(path, counted)


def _try_write(path, counted):
    try:
        path.write_text(counted + "\n")
    except OSError:
        pass                                # read-only install: it still runs


def _already_says(path, counted):
    try:
        return path.read_text().strip() == counted
    except OSError:
        return False


def _recalled(where):
    """What the last checkout that ran here wrote down, or nothing."""
    return _read_version(where / VERSION_FILE) or VERSION_FALLBACK


def _read_version(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _commit_count(where):
    try:
        return _counted(where)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _counted(where):
    p = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(where),
                       capture_output=True, text=True, timeout=5)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _version_of(count):
    if not _countable(count):
        return VERSION_FALLBACK
    n = int(count)
    return "%d.%d.%d" % (VERSION_MAJOR, n // 100, n % 100)


def _countable(count):
    """A commit count: a plain non-negative integer, of any width.

    It had to be exactly three digits while the version was those digits
    sliced apart. The arithmetic above has no such edge: 7 commits is 3.0.7
    and 1042 is 3.10.42, both perfectly sayable.
    """
    if not count:
        return False
    return count.isdigit()


REPO_URL = "https://github.com/raoulsson/dashcam-exporter"
SPONSORS_URL = "https://github.com/sponsors/raoulsson"
COFFEE_URL = "https://www.buymeacoffee.com/raoulsson"


def archive_dir(ctx):
    """Where THIS ctx keeps its receipts. No default, on purpose.

    A module constant read directly is a path into the real home directory,
    and a test that exercises the clean-up then MOVES ITS FIXTURES THERE. That
    happened: three fixture receipts landed in ~/.dashcam-exporter and the next
    test read them as evidence about a real card. A missing attribute raising
    here is a fixture that has not said where its archive is, which is loud and
    fixable; falling back to the home directory is silent and writes to it.
    """
    return ctx.archive_dir

# In-memory view of the workspace's excluded stamps, for the callers that have
# no ctx (card_split's signature is (card, after)). It is a CACHE of the file,
# not the record: excluded_stamps(ctx) refreshes it from disk, and every path
# that acts on card_split's answer refreshes first. One process, one workspace
# — the file is what survives a restart.
_EXCLUDED = set()


def _excluded_record(ctx):
    """The whole file: {"stamps": [...], "ids": [...], "trips": [...]}.

    Three facts about one act, so one file. They are read by different readers —
    the delta import and the card accounting want the stamps, a publisher wants
    the ids, the progress block wants the trips — and writing any one of them on
    its own is how the others get dropped by a rewrite.

    "ids" and "trips" are not the same list and neither is redundant. An id is
    an out_base name, the thing a DESTINATION is keyed on, and a trip too short
    to render never had one — so the id list is silently short by exactly the
    fragments, and counting it answered "3 trips excluded" with 1. A trip key is
    this workspace's own handle on a trip, its first clip's stamp: every trip
    has one, it is already the vocabulary of the stamps list beside it, and
    unlike the index in the table it does not move when the grouping renumbers.
    """
    try:
        return json.loads((ctx.state_dir / EXCLUDED_FILE).read_text())
    except Exception:
        return {}


def _write_excluded(ctx, record):
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        state_path(ctx, EXCLUDED_FILE).write_text(json.dumps(record, indent=1))
    except OSError:
        pass


def excluded_stamps(ctx):
    """The stamps of clips deliberately excluded, refreshed from disk.

    An excluded clip is treated as if imported ever after: the delta import
    does not re-copy it, and the clean-up counts it as accounted for. The warning
    happened once, at exclude time — that was the decision, and re-warning at
    every later step would be asking the same question again.
    """
    global _EXCLUDED
    _EXCLUDED = {str(s) for s in _excluded_record(ctx).get("stamps", [])}
    return set(_EXCLUDED)


def dropped_trip_ids(ctx):
    """The trips deleted on purpose, ever, in this workspace.

    Handed to a builder as Workspace.dropped_ids. It outlives the sweep for the
    same reason the ledger does: a publisher rebuilding an index from the
    previous one cannot otherwise tell a dropped trip from one that was
    published and then cleaned up.
    """
    return tuple(sorted(str(i) for i in _excluded_record(ctx).get("ids", [])))


def dropped_trip_keys(ctx, namespace):
    """The trips deleted on purpose FROM THIS IMPORT, keyed by its folder name.

    Per import rather than for all time, because the one thing that reads this
    is the progress block, and the progress block describes a cycle. Clean
    Workspace ends a cycle; a count that survives it reports last week's
    decisions on a screen whose row above says the workspace is empty, and the
    operator is left wondering which trips he is supposed to still care about.

    Only this list is scoped. dropped_ids stays permanent and unscoped: a
    builder rebuilding an index needs every trip ever dropped here, because a
    dropped trip and a published-then-cleaned-up one are indistinguishable to
    it forever after.

    A record written before this was keyed reads as a plain list, and every
    namespace finds nothing in it. That is the right answer rather than a
    migration: those drops belong to cycles that are over.
    """
    trips = _excluded_record(ctx).get("trips", {})
    if not isinstance(trips, dict):
        return ()
    return tuple(sorted(str(k) for k in trips.get(namespace, [])))


def _strings(values):
    return set(map(str, values))


def record_excluded_stamps(ctx, stamps):
    """Persist clip stamps whose footage was deliberately dropped."""
    global _EXCLUDED
    record = _excluded_record(ctx)
    merged = _strings(record.get("stamps", ())) | _strings(stamps)
    record["stamps"] = sorted(merged)
    _write_excluded(ctx, record)
    _EXCLUDED = merged
    return merged


def record_dropped_trips(ctx, trip_ids, keys=(), namespace=""):
    """Persist what a drop removed: the trip ids, and the trips themselves.

    Both in one write. They are two keys of one record, and a second write for
    the second key is the rewrite the record's own docstring warns about.

    The ids go in flat and forever; the trips go in under the import they came
    from, because the progress block counts a cycle and not a workspace's whole
    history.
    """
    record = _excluded_record(ctx)
    merged = _strings(record.get("ids", ())) | _strings(trip_ids)
    record["ids"] = sorted(merged)
    trips = record.get("trips")
    if not isinstance(trips, dict):
        trips = {}
    trips[namespace] = sorted(_strings(trips.get(namespace, ())) | _strings(keys))
    record["trips"] = trips
    _write_excluded(ctx, record)
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


def import_is_expendable(ctx, root, target):
    """(ok, reason) — is everything from `root` rendered, and at the destination
    if this install publishes? The delete step's proof, factored out so
    clearing the working dir before a copy cannot become a softer version of
    the same act.

    `target` is the frozen TargetFacts from the world being judged, not a live
    handle: this is asked while a destructive prompt is on screen, and a second
    question to the network here would answer about a different instant than
    the gates the operator just read.
    """
    ns = ctx.out_dir / root.name
    mp4s = rendered_mp4s(ns) if ns.is_dir() else []
    if not mp4s:
        return False, "nothing from it was rendered"
    # Only ask the scanner how many trips there SHOULD be when the source is
    # still there to scan. Once the import is deleted the question is
    # unanswerable and asking it makes the renderer error out on a missing DCIM
    # folder — which is not the same as "these renders are incomplete". The
    # destination check below still has to pass either way.
    if (root / "DCIM").is_dir():
        payload = load_groups(ctx, root)
        gs = (payload or {}).get("trips") or []
        want = sum(1 for g in gs if g.get("renderable", True)) if gs else None
        if want is not None and len(mp4s) < want:
            return False, "%d of %d trips rendered" % (len(mp4s), want)
    # One answer about the whole import rather than one per render. It is the
    # only shape there is now, and it is the right one for an advisory: the
    # sentence it feeds says "the copy on this machine is the only one", which
    # is true as soon as ANY of it is unconfirmed.
    if _not_at_the_destination(root, target):
        return False, "not confirmed at %s" % target.name
    return True, ""


def _not_at_the_destination(root, target) -> bool:
    """NA counts as settled: the local edition has no destination to confirm
    anything, and this is an advisory rather than a gate."""
    if target.complete is menu.Evidence.NA:
        return False
    return not _confirmed_for(root, target)


def _confirmed_for(root, target) -> bool:
    """YES, and about THIS import.

    Item 9's advisory walks every import in the workspace; the plugin was asked
    about one of them. An answer about a different import settles nothing here,
    so it has to read the same as no confirmation — otherwise the one import
    that IS published silences the warning about the one that is not.
    """
    return target.namespace == root.name and target.complete is menu.Evidence.YES


OWNER_FILE = "owned-by"
LOCK_FILE = "pid.lock"

# The four names above are plain now: they sit in a directory of their own, and
# a leading dot inside a hidden directory hides a file from the one person who
# went looking for it. What they were called in the working area is mapped
# below, once, so an existing workspace does not lose its high-water mark.
WAS_CALLED = {"imported.json": ".imported.json",
              "excluded.json": ".excluded.json",
              "owned-by": ".owned-by"}


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
    lock = ctx.lock_file
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
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
    lock = ctx.lock_file
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
    marker = ctx.state_dir / OWNER_FILE
    mine = str(ctx.exporter)
    try:
        if marker.is_file():
            owner = marker.read_text(encoding="utf-8").strip()
            return None if owner == mine else owner
        state_path(ctx, OWNER_FILE).write_text(mine + "\n", encoding="utf-8")
    except OSError:
        pass          # unwritable is not a reason to refuse; it is a reason to
    return None       # carry on without the extra proof


def working_area_is_expendable(ctx, target):
    """(ok, why, stragglers) — is everything in the working area a second copy?

    The sweeps act on the premise that the working area's contents belong to a
    round that ended by being published or gathered into final_. This function
    exists because that premise has to be TRUE before it is acted on, not
    assumed: a render whose upload failed or was skipped is the only copy, and
    unlinking it costs the hours it took to encode.

    A render is expendable when it is EITHER
      - covered by the plugin saying every trip of this import is complete at
        the destination, which is ITS answer and its definition of "there" —
        one arrangement meant an object in a bucket at a matching size plus a
        deploy that covered it, and that rule now lives with the arrangement
        instead of here, OR
      - inside a final_<date> folder, which is where Build Website moves the
        deliverable on an install that does not publish.
    Everything else in the working area — previews/, the caches, the stills —
    is derived from renders and costs seconds to rebuild, so it never blocks.

    Only YES clears a render. UNKNOWN does not, so a target that could not be
    reached keeps the renders rather than having them swept on the strength of
    a question nobody could answer.

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

    # All or nothing, and in the safe direction: unless the plugin vouched for
    # the whole import, only a gathered render clears. A render it did not
    # speak for is kept, which costs disk and never footage.
    covered = _vouched_for(ctx, target, loose)
    stragglers = []
    for f in loose:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if (f.name, size) in gathered or f in covered:
            continue
        stragglers.append(f)

    if stragglers:
        what = ("not confirmed at %s and not gathered" % target.name if target.configured
                else "neither published nor gathered")
        return False, "%d renders %s" % (len(stragglers), what), stragglers
    return True, "%d renders, all published or gathered" % len(loose), []


def _vouched_for(ctx, target, loose):
    """The loose renders the destination's answer actually clears.

    All or nothing WITHIN ONE IMPORT, which is the scope of the question that
    was asked: every trip of THAT import is at the destination. The working
    area is not one import — <out> holds a namespace per import, and the sweep
    below walks all of them — so an answer read across the whole tree lets a
    yes about this round's trips authorise deleting last round's renders, which
    were never in the question and may exist nowhere else. Those are kept, and
    the clean-up prints them as stragglers.
    """
    if target.complete is not menu.Evidence.YES:
        return set()
    return set(filter(lambda f: _answer_covers(target, ctx.out_dir, f), loose))


def _answer_covers(target, out_dir, path) -> bool:
    """Is this file inside the import the destination was asked about?

    An empty namespace covers nothing on purpose: it means no import was
    settled on, so the trip list handed over was empty and a YES to it says
    only that nothing was missing from nothing.

    RESOLVED on both sides, the same rule trip_renders states: one of these
    paths comes out of a scan and the other off the world, and /var against
    /private/var is a mismatch on every macOS install. Here the mismatch would
    read as "nobody was asked about this", which is the safe direction but
    turns the last-copy panel into something that fires over everything —
    a warning that is always on is one nobody reads.
    """
    if not target.namespace:
        return False
    return _is_under(_resolved(path), _resolved(out_dir / target.namespace))


def archive_sidecars(ctx):
    """Move every trip's receipt out of the working area, keeping its shape.

    Before the sweep, so a crash between the two leaves the receipts safe
    rather than deleted: they are the only record that survives a cycle, and
    the guard that refuses to erase a card reads them.
    """
    return tuple(filter(None, map(lambda m: _archived(ctx, m),
                                  _safe_rglob(ctx.out_dir, "trip_*_meta.json"))))


def _archived(ctx, meta):
    try:
        return _move_under_archive(ctx, meta)
    except OSError:
        return None                 # a receipt left behind is not worth failing over


def _move_under_archive(ctx, meta):
    target = archive_dir(ctx) / meta.relative_to(ctx.out_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(meta), str(target))
    return target


def archived_trips(ctx):
    """How many trips have been through the whole cycle on this machine."""
    return len(_safe_rglob(archive_dir(ctx), "trip_*_meta.json"))


def _swept_on(ctx):
    """Who vouched for the renders this sweep is about to remove."""
    plugin = getattr(ctx, "plugin", None)
    if plugin is None:
        return "cleanup after a local build"
    return "cleanup on %s's answer (%s)" % (plugin.name, plugin.origin)


def purge_published_renders(ctx, root, finished=True):
    """Empty the working area. Everything goes except a short keep-list.

    Once the trips are at the destination, every file here is a third copy or
    a cache of something that no longer exists: the renders, their sidecars,
    previews/ from the review pass, the extracted GPX cache, the boundary cache
    that names clips already deleted. Keeping any of it leaves exactly the files
    that are impossible to make a decision about later.

    Kept: any final_* folder, which is the gathered result of a finished cycle
    and the whole point of having gathered it. Nothing else -- the run logs
    went to the import root and the four state files to ~/.dashcam-exporter,
    so there is no longer a keep-list to get right in the one place where being
    wrong deletes footage.

    The trip receipts are not kept HERE any more — archive_sidecars moves them
    to ARCHIVE_DIR first, and this then takes everything. Sparing them in place
    left the output tree holding six files that read as six trips waiting to be
    rendered, when what they record is that the work is finished. They are
    still the state and are still read: what Delete SIM Data consults to decide
    whether a card's clips already sit inside a published trip, which is the
    question standing between a card and an erase that has no second copy.

    The ledger is written BEFORE anything is removed. It is the only fact here
    that cannot be recovered from somewhere else — how far the imports have
    reached — and a crash midway must not lose it. Its note carries WHOSE
    answer the sweep acted on, which used to be tacked onto the exit summary:
    a screen is read once and then scrolls, and "who said the footage was
    safe" is a question asked weeks later, so it belongs in the file that
    outlives the session rather than on the line that does not.

    `finished` is what separates the two acts that reach here. A sweep ends a
    cycle: the trips are published, their receipts are the record of that, and
    the mark may advance. A DISCARD ends nothing — it throws away a workspace
    whose only remaining copy is the card — so it must write neither. Archiving
    a receipt for a trip that was never published tells covered_stamps that
    its clips sit inside a rendered trip, which then hides them from the next
    import and clears the card to be erased. Ten clips read as safe when they
    existed in one place.
    """
    if finished:
        write_ledger(ctx, last_imported_stamp(ctx), _swept_on(ctx))
        archive_sidecars(ctx)

    out = ctx.out_dir
    if not out.is_dir():
        return 0, 0
    keep_names = {root.name}
    freed = n = 0
    for child in sorted(out.iterdir()):
        if child.name in keep_names or child.name.startswith(FINAL_PREFIX):
            # This is out_dir, so a child carrying root's name is the RENDER
            # NAMESPACE of the import being cleaned up -- out_dir/<import
            # name>/ -- and not the import itself, which lives in another tree
            # and has already been rmtree'd by the commit above. So it empties
            # AND goes: an import that no longer exists has no namespace, and
            # the one left behind was an empty dated folder the operator had
            # to look inside to find out it held nothing.
            #
            # A final_* child is a different thing entirely and is only ever
            # skipped: it is the gathered result of a finished cycle, which is
            # what the sweep exists to preserve.
            if child.name == root.name and _real_dir(child):
                for f in sorted(child.rglob("*")):
                    if _real_file(f):
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
                # rmdir, never rmtree: it refuses on a non-empty directory, so
                # anything the walk above deliberately spared keeps the folder
                # standing instead of being taken out by the tidy-up.
                try:
                    child.rmdir()
                except OSError:
                    pass
            continue
        try:
            if _real_dir(child):
                # Delete file by file, then drop the directories that end up
                # empty. A SYMLINK is not this branch: it is unlinked below
                # like any other name, because walking one would delete through
                # it into a tree the tool was never pointed at.
                for f in sorted(child.rglob("*")):
                    if _real_file(f):
                        freed += f.stat().st_size
                        f.unlink()
                        n += 1
                for d in sorted(child.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                try:
                    child.rmdir()
                except OSError:
                    pass
            else:
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
        print(C.dim("  Cleared %d cached gpx files from %s — a new card must not"
                    " inherit the old card's tracks." % (removed, tilde(cache))))
    return removed


def record_import(ctx, card):
    """Advance the ledger to the newest clip on the card just imported.

    Called only after import-sd-card.sh exits 0, which it does only after
    verifying the copy file-for-file. Taken from the CARD because the card is
    what the next delta compares against, and write_ledger refuses to move
    backwards.

    What this stamp does NOT establish, though it used to say it did: that a
    verified copy of everything up to it exists on this disk. It is one
    number, and it is lifted by things that are not a copy — a cleanup writes
    it from last_imported_stamp(), which reads the END of every rendered
    trip's meta. Render a trip that ends on the 24th and the mark asserts the
    2nd of May was imported too.

    That is not hypothetical: the first mark this machine ever wrote came from
    a cleanup, at 20260724185433, and thirteen clips older than it had never
    been copied. Every delta after that skipped them as already imported,
    while item 9 refused to erase the card because nothing accounted for them.

    The mark is therefore an optimisation, not the authority. What a delta
    actually offers is to_import(): above the mark OR owed, and owed is the
    per-clip accounting that cannot be lifted by an unrelated render.
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


def _same_card(ctx, leftovers):
    """Is everything already in the import area off the card in the slot.

    The mixing warning is about TWO CARDS, and that is not what an interrupted
    copy leaves behind: the leftovers are a prefix of this very card, and
    importing again finishes the job. Told it would mix two cards, the
    operator is being warned off the one action that resolves the state.
    """
    return all(map(lambda root: not _unsourced_files(root, ctx.card,
                                                     _import_files(root)),
                   leftovers))


def _delta_counts(ctx, after):
    """(already here, to fetch, bytes, files, already done, their bytes).

    One computation, and the list it produces is what the script is given.
    The screen used to count clips here while import-sd-card.sh worked out
    "new" for itself from a high-water mark, and the two disagreed the moment
    an owed clip was older than the mark: fourteen offered, one copied.

    Not card_split's (new, below the mark) either. What is below the mark is a
    fact about rounds long since rendered and swept; what is being decided is
    how much of THIS card is not on this machine yet.
    """
    stamps = frozenset(card_stamps(ctx))
    owed, _note = card_accounting(ctx)
    new = to_import(ctx, stamps, after, frozenset(excluded_stamps(ctx)), owed)
    here = workspace_stamps(ctx, new)
    files = _not_here_yet(ctx, _files_for(ctx.card, new - here))
    # What the card holds that this import does NOT offer: clips a previous
    # round took and this machine has finished with. Named because the two
    # figures are read side by side -- 416 on the status row, 239 on this one
    # -- and an unexplained gap reads as a tool that cannot see the rest.
    done = stamps - new
    return (len(here), len(new - here), _weigh(ctx.card, files), files,
            len(done), _weigh(ctx.card, _files_for(ctx.card, done)))


def _not_here_yet(ctx, relative):
    """Drop what the workspace already holds, by path and size.

    Without this the list carried every GPS archive on the card on every
    import, because those come along whatever their stamp -- so the screen
    offered a gigabyte that rsync would then skip, and "what is offered is
    what is fetched" stopped being true in the other direction.
    """
    folders = import_candidates(ctx)
    return [r for r in relative
            if not any(_same_file(ctx.card / r, f / r) for f in folders)]


def _same_file(src, dst):
    return dst.is_file() and _size_of(dst) == _size_of(src)


# What the renderer opens: the clips, and the GPS beside them. Everything
# else the camera writes -- 201photo, 202thumb, 207log, 750 MB of it -- is
# never read by anything here, so it is not copied into a workspace whose
# whole purpose is to be rendered from and then thrown away.
VIDEO_DIR, GPS_DIR = "200video", "203gps"


def _files_for(card, stamps):
    """Every file those clips need, as paths relative to the card.

    The clips are matched by stamp -- both cameras, since front and rear share
    one. EVERYTHING ELSE comes along whatever its name, and that is the part
    worth stating: a GPS archive is stamped with ITS OWN start and a span,
    "20260712191931_0120_T.git", so it almost never carries a clip's stamp.
    Matching it by stamp took two files off a card that held thirty for that
    day, and the trip came out with gps_points 0 -- a drive with no route,
    from footage whose track was sitting right there.

    203gps is 251 MB against 25 GB of video, and rsync skips what the
    destination already has, so taking all of it costs a stat per file on
    every import after the first. Erring toward copying is the safe direction
    here: the cost of being wrong is bytes, and the cost the other way is a
    trip that can never be described.
    """
    out = []
    for p in _safe_rglob(card / "DCIM", "*"):
        if p.is_file() and _wanted_file(p, stamps):
            out.append(str(p.relative_to(card)))
    return sorted(out)


def _wanted_file(path, stamps):
    """A clip we asked for, the GPS beside it, or anything unstamped."""
    if GPS_DIR in path.parts:
        return True
    if VIDEO_DIR in path.parts:
        return _stamp_of_name(path.name) in stamps
    return _stamp_of_name(path.name) is None


def _weigh(card, relative):
    return sum(_size_of(card / r) for r in relative)


def _write_import_list(relative):
    """Hand the script the exact files, one relative path per line.

    It used to be given AFTER_STAMP and left to work out "new" for itself.
    Two places computing that is two answers, and when they parted the screen
    promised fourteen clips and the run copied one untimestamped file. Then
    the other way: dropping the stamp so the script would stop skipping them
    made it copy the whole card, because without a filter rsync fetches
    everything the empty workspace lacks -- 26 GB against an offer of 2.5.

    A list cannot disagree with itself.
    """
    if not relative:
        return ""
    fd, path = tempfile.mkstemp(prefix="dashcam-import-", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(relative) + "\n")
    return path


def _bytes_of(card, stamps):
    """What those clips weigh on the card, both cameras."""
    return sum(_size_of(p) for p in _card_clips(card)
               if _stamp_of_name(p.name) in stamps)


def _stamp_of_name(name):
    m = STAMP_RE.search(name)
    return m.group(1) if m else None


def _delta_lines(here, todo, size, files, done=0, done_size=0):
    """What the y is answering: what comes over, then what already has.

    Clips and gigabytes, not the file count. The file count was on this line
    because a clip is written twice with its GPS beside it, so 239 clips and
    601 files are both true -- but the second number answers a question about
    the camera's layout, and the one being asked is how much is about to be
    copied. The size says that.

    The already-done line is dim and second because it is reassurance rather
    than the decision: 239 offered out of 416 on the card reads as a tool that
    cannot see half of them until something says where the other 177 went.

    Amber on the figures: they are the only part that changes between runs.
    """
    tail = "%s files, %s" % (C.yellow("%d" % files), size)
    if here:
        return ("  %s clips already imported, %s to go (%s)"
                % (C.yellow("%d" % here), C.yellow("%d" % todo), tail), "")
    if not todo:
        # No clips, but something to fetch: the GPS for clips that came over
        # before the tracks were being collected at all.
        return ("  no new clips, %s to fetch" % tail, "")
    lines = ["  %s clips to import (%s)" % (C.yellow("%d" % todo), size)]
    if done:
        lines.append(C.dim("  %d clips already processed (%s)"
                           % (done, human_bytes(done_size))))
    return tuple(lines) + ("",)


def _leftover_lines():
    """Only the mixing case reaches this now: footage that is NOT off the card
    in the slot, so a second card's clips would be grouped into one set of
    trips with no record afterwards of which came from which."""
    return (C.dim("  Importing now adds this card alongside that footage. Trips are"),
            C.dim("  grouped across everything found, so the two cards would be mixed"),
            C.dim("  and there is no record afterwards of which clip came from which."),
            C.dim("  Clear it with %d) %s first, or finish the round it belongs to."
                  % (CLEAN_WS, NAME[CLEAN_WS])))


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

    # The path only. It used to carry the card's whole contents -- "131 clips,
    # 26.2 GB" -- which is a number nothing here acts on: 13 of those belong to
    # a round already rendered, published and swept, and quoting them next to
    # the figures that decide this run invites adding the two together.
    print("  Source: %s" % tilde(ctx.card))

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
                    % tilde(ctx.state_dir / OWNER_FILE)))
        return record(ctx, NAME[IMPORT], SKIPPED, started,
                      "output dir owned by %s" % other)

    # Footage from a previous round still in the sink. This used to offer to
    # CLEAR it, and in one branch swept the working area with no prompt at all
    # — item 8's job done from inside item 1, twice. Under this graph item 1
    # offers item 8 directly, so the offer has nowhere to earn its place. What
    # stays is the warning and the gate on this item's own job: importing on
    # top mixes two cards into one grouping and nothing afterwards records
    # which clip came from which.
    # Footage in the way, and only when it is somebody else's. Leftovers that
    # are all off the card in the slot are an interrupted copy of THIS card,
    # and importing again finishes it -- rsync brings over what is missing and
    # nothing else. Asking "import on top of what is there?" about that is a
    # question with one answer, in front of the delta prompt that asks the
    # same thing usefully.
    leftovers = import_candidates(ctx)
    if leftovers and not _same_card(ctx, leftovers):
        print()
        print(C.yellow("  The import area is not empty:"))
        for src in leftovers:
            print(C.yellow("    %s  %s clips, %s"
                           % (tilde(src), clip_count(src), human_bytes(tree_size(src / "DCIM")))))
        _print_all(_leftover_lines())
        if not confirm("  Import anyway, on top of what is there?", False):
            return record(ctx, NAME[IMPORT], ABORTED, started,
                          "Aborted by user pre-run.")

    # Delta, always. A card left in the car accumulates: this one holds 1039
    # front clips of which 427 were already taken in last time, and copying
    # those again costs tens of GB and the minutes you want back to put the
    # card away. The high-water mark survives deleting the local import,
    # because it is read from the renders and the boundary cache, not the
    # footage.
    #
    # It used to print how far the mark reached and how many clips sat below
    # it, then ask whether to take the delta or the lot. Three lines and a
    # decision, on every import, for a question with one answer -- and the
    # count below the mark is a fact about rounds long since rendered and
    # swept, not about anything on this machine. What is left is the one
    # number that matters and one y/n. Winding the mark back is what makes a
    # clip new again, and item 8 does exactly that when it discards an import.
    after = last_imported_stamp(ctx)
    excluded_stamps(ctx)                 # refresh the cache the split reads
    here, todo, size, wanted, done, done_size = _delta_counts(ctx, after)
    print()
    if not wanted:
        print(C.green("  Nothing new at the source — it is already all imported."))
        return record(ctx, NAME[IMPORT], SATISFIED, started, "no new clips")
    _print_all(_delta_lines(here, todo, human_bytes(size), len(wanted),
                            done, done_size))
    if not confirm("  Run delta import", True):
        return record(ctx, NAME[IMPORT], ABORTED, started,
                      "Aborted by user pre-run.")

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
    if after:                       # a mark exists, so this is not a first copy'
        # The reason is not that the skipped clips are precious — they are
        # already imported, and once rendered and uploaded the card is the copy
        # that matters least. It is that --delete only ever fires after a verify,
        # and this run verifies the files it copied. The other 427 were verified
        # by a run that finished days ago, a fact recorded in a ledger the shell
        # script cannot read. Erasing them here would be a delete on somebody
        # else's evidence.
        # Said in the comment above rather than on screen: it explains a
        # question that is no longer asked, and a paragraph of reasoning about
        # an option nobody was offered is four lines between the operator and
        # the copy he started.
        erase = False
    else:
        print(C.dim("  The source is NOT erased by default; import-sd-card.sh only deletes"))
        print(C.dim("  its files after the copy verifies file-for-file."))
        erase = confirm("  Erase the source's files after a verified copy?", False)

    env = {"DASHCAM_IMPORT_ROOT": str(ctx.import_root)}
    listing = _write_import_list(wanted)
    if listing:
        env["IMPORT_LIST"] = listing
    cmd = ["./import-sd-card.sh"]
    if erase:
        cmd.append("--delete")
    cmd.append(day)
    if str(ctx.card) != FALLBACK_CARD:
        cmd[1:1] = ["--src", str(ctx.card)]

    # Not echoed. The command line is this module's business -- which flags it
    # composed, where the script lives -- and the operator answered the only
    # question in it two lines ago. What is worth keeping from the run is what
    # it CONCLUDED, which is the keep list below; the script's own preamble
    # ("only clips newer than ...", "306 of 888 files selected") restates the
    # delta decision he just made, in the script's numbers rather than his.
    prepare_for_import(ctx)
    watch = make_import_parser(size)
    # No keep list. "Verified: 306 files in dest (306 expected from this run)"
    # and "Done. Imported 306 files to <path>" are the script reporting to
    # whoever ran it by hand; here they say twice, in the script's words, what
    # the line below says once in the tool's.
    rc, lines = run_stream(cmd, ctx.exporter, "Import", parser=watch,
                           quiet_finish=True, env_extra=env)
    if rc != 0:
        return record(ctx, NAME[IMPORT], FAILED, started, "exit %d" % rc)

    moved, count = watch.state["done"], watch.state["files"]
    done_line("imported %s clips (%s files, %s) from SIM"
              % (C.yellow("%d" % todo), C.yellow("%d" % count), human_bytes(moved)))

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
                  "%d clips, %d files, %s -> %s"
                  % (todo, count, human_bytes(moved), tilde(dest)))


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


def step_progress(ctx, world):
    """Progress: one line per stage, saying what exists. Read-only.

    An observation of state, not a transition: it generates nothing and writes
    nothing. Seven lines in the order the pipeline runs, each answering the
    same question about its own stage -- is there anything, and how much.

    It used to be a table of trips with a sidecars/rendered column, plus the
    destination's answer, plus the count of everything ever processed, plus
    the session summary. Every one of those was true and none of them was the
    question being asked: the operator presses p to see where the workspace
    got to, and a table that grows with the card buried that in rows. The
    trip ids are still in the sidecars for anyone who wants them.
    """
    started = time.time()
    _print_all(_progress_lines(ctx, world))
    return record(ctx, NAME[PROGRESS], RAN, started,
                  "%d trips, %d rendered" % (len(world.metas), len(world.renders)))


def _progress_lines(ctx, world):
    return tuple(filter(None, (
        _imported_line(world), _excluded_line(world), _meta_line(world),
        _rendered_line(world), _preview_line(world), _built_line(world),
        _uploaded_line(world))))


def _state_line(text, there):
    """Dim when the answer is "nothing yet", exactly as the status block does
    it: absent is the ordinary state at the start of a cycle, and a colour
    that shouts about the normal case has nothing left for the odd one."""
    if there:
        return "  " + text
    return C.dim("  " + text)


def _clip_total(n):
    """A folder with no DCIM/200video/front answers None, not zero -- it is a
    directory nobody has looked in, which is a different thing from empty."""
    if n is None:
        return "?"
    return "%d" % n


def _fact(value, label, there):
    """One progress line: what there is, then what it is.

    Two columns, because the seven lines answer the same question seven times
    and the answer is the part that moves. Down one column the eye reads
    118 / 2 / 6 / 6 / [x] without picking it out of seven sentences of
    different lengths. Dim when there is nothing yet, exactly as the status
    block marks an absent state.

    The box sits in the value column with the counts, because it is the same
    kind of thing: a count where counting means something, a box where the
    answer is only yes or no.
    """
    # Five wide, which is one more than a full card ever needs. 1039 clips is
    # a long session and 9999 is not reachable, so the column never shifts the
    # labels sideways to fit a number -- and a column that moves is a column
    # the eye has to re-find every launch.
    return _state_line("%5s   %s" % (value, label), there)


def _box(done):
    return "[%s]" % ("x" if done else " ")


def _imported_line(world):
    # "in workspace", not "imported": this counts the clips that are THERE, and
    # excluding a trip deletes clips, so the figure goes down. Beside a "Trips
    # excluded" row that only ever grows, the past tense read as a running
    # total of everything ever fetched and the two rows could not be reconciled
    # -- six imported and three excluded looks like a card of thirteen clips.
    if not world.imports:
        return _fact("0", "Clips in workspace", False)
    return _fact(_clip_total(clip_count(world.imports[0])),
                 "Clips in workspace", True)


def _excluded_line(world):
    # Dim at zero, like every other row. The reasoning for keeping it bright
    # was that dim says "work outstanding" and nobody owes an exclusion — but
    # the row counts this cycle now, and at the start of one there is nothing
    # to say. A lit zero among four dim ones draws the eye to the only number
    # on the block that means nothing yet.
    n = len(world.dropped_trips)
    return _fact("%d" % n, "Trips excluded", bool(n))


def _meta_line(world):
    # "Described" was a word only this line used. Sidecars is what the rest of
    # the tool calls them -- every guard that refuses says "no sidecars on
    # disk for the import" -- so the row and the refusal now name one thing.
    return _fact("%d" % len(world.metas), "Trips with sidecars",
                 bool(world.metas))


def _rendered_line(world):
    # No size. It is on the Rendered row of the status block, where the
    # question is whether the disk can take the next round; here the question
    # is how far the cycle has got, and gigabytes do not answer it.
    return _fact("%d" % len(world.renders), "Trips rendered",
                 bool(world.renders))


def _preview_line(world):
    return _fact(_box(world.stills_current), "Preview built",
                 world.stills_current)


def _built_line(world):
    """The local page or a gathered folder -- whichever this edition makes.

    Only the local edition makes either. Under an uploader, item 5 hands the
    build to the plugin, which stages wherever it likes and is not asked
    afterwards whether it did -- so the box could never tick, and a workspace
    with everything published read "[x] Website uploaded" one line under
    "[ ] Website built". A line the tool cannot answer is not status.
    """
    if world.strategy is not menu.Strategy.LOCAL_PAGE:
        return None
    built = world.local_page or bool(world.final_folders)
    return _fact(_box(built), "Website built", built)


def _uploaded_line(world):
    """The destination's own answer, and only its own. UNKNOWN and NA are both
    "not said to be up there", which is what the negative line says.

    With no trips there is no answer worth printing either. The question put
    to the plugin is "are all of THESE trips complete", and an empty list
    makes a yes vacuous -- which is how an untouched workspace, two stray
    clips and no sidecar anywhere read as "website uploaded".
    """
    up = bool(world.trip_ids) and world.target.complete is menu.Evidence.YES
    return _fact(_box(up), "Website uploaded", up)


def _processed_line(ctx):
    """Everything finished on this machine, as one number.

    The receipts of past cycles used to sit in the output tree, where Progress
    listed them as trips with no render — six rows that read as work waiting,
    when what they record is work completed. They live outside the working area
    now, so all that is left to say about them is how many there have been.
    """
    n = archived_trips(ctx)
    if not n:
        return ()
    return (C.dim("  Processed trips since installation: %d" % n),)


def _destination_line(target):
    """What the destination said about this import, in one line.

    Nothing at all when there is no destination: a row reading "not
    applicable" is a question mark where the local edition has no question.
    """
    if not target.configured:
        return ()
    return ("  %s: these trips complete at the destination — %s"
            % (target.name, target.complete.value),)


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

    have = load_groups(ctx, root)
    if have is None:
        # The scan failed and said so in red. Carrying on runs the whole pass
        # again on a grouping nobody could read -- minutes of work behind a
        # failure the operator has already been shown.
        return record(ctx, NAME[META], FAILED, started, "the trip scan failed")
    # The sidecars go first, then they are written again. This used to answer
    # SATISFIED when every trip already had its set, which is the wrong answer
    # to the reason anyone presses 2 a second time: the inputs changed. A GPS
    # track arrived that the first pass did not have, or the grouping moved, or
    # the tool learned something about the data it did not know before -- and a
    # sidecar written under the old understanding sits there looking current,
    # because nothing in a _meta.json says which run wrote it.
    #
    # ONLY the sidecars. A render is hours and is not derived from anything
    # this step knows; the four files beside it are seconds and are derived
    # from everything it knows.
    _wipe_sidecars(have)
    # The renderer prints its usual "[Trip a/b]" headers here, so the real trip
    # counter drives the bar. They are not KEPT: the bar's note already reads
    # "trip 2/6", and a permanent line per trip in the renderer's words is the
    # same count a second time, spelled differently.
    cmd = (["./make-trips-rendered.sh", "--sidecars-only", "--root", str(root),
            "--out", str(ctx.out_dir)] + ctx.config_args + ctx.scan_args)
    rc, _lines = run_stream(cmd, ctx.exporter, "Sidecars",
                            parser=make_scan_parser(), quiet_finish=True,
                            env_extra=_renderer_env(ctx))
    if rc != 0:
        return record(ctx, NAME[META], FAILED, started, "sidecars exit %d" % rc)
    # Counted where they were written, not across the whole export tree: an
    # rglob there counts every earlier import's trips too, so six scanned trips
    # reported eighteen.
    # The trips themselves, here and nowhere else. This is the step that works
    # out what they ARE, and until now it said only how many -- the list lived
    # behind item 4, which is the entry for throwing one away. Reading what the
    # card turned out to hold should not require opening the screen that
    # deletes it.
    #
    # The same table item 4 prints, from the same function: two spellings of
    # one list would disagree the first time either changed.
    _print_trip_table(ctx, root, (have or {}).get("trips", []))
    n = _sidecars_for(ctx, root)
    done_line("described %s trips%s, sidecars under %s"
              % (C.yellow("%d" % n), _skipped_note(have),
                 tilde(ctx.out_dir / root.name)))
    return record(ctx, NAME[META], RAN, started, "%d trips described" % n)


def _wipe_sidecars(payload):
    """Remove this import's .gpx, .html, _links.txt and _meta.json.

    Named suffixes rather than "everything that is not an mp4", because the
    export tree also holds the contact sheet, the clip stills and the caches,
    and a sweep that took those would have item 2 quietly undo item 3.
    """
    doomed = [f for trip in payload.get("trips", [])
              if trip.get("out_base")
              for f in _existing_sidecars(trip["out_base"])]
    deleted, _freed, _errors = _unlink_all(doomed)
    return deleted


def _skipped_note(payload):
    """Why the count here is smaller than the number of trips on the card.

    A trip below --min-clips-per-group gets no sidecar, so "described 2
    trips" sat next to Build Preview's "5 stills for 5 trips" with nothing
    saying they had counted different things. The renderer says it -- "Auto-
    skipping 3 fragment trips: #1 (2 clips)..." -- and this step stopped
    keeping the renderer's lines, which took the explanation with them.
    """
    skipped = [t for t in (payload or {}).get("trips", [])
               if not t.get("renderable", True)]
    if not skipped:
        return ""
    return " of %d (%d too short to render)" % (
        len((payload or {}).get("trips", [])), len(skipped))


def _day_of(meta):
    try:
        return re.sub(r"\D", "", str(json.loads(meta.read_text()).get("day") or ""))
    except (OSError, ValueError):
        return ""


def _has_fix(meta):
    try:
        return bool(json.loads(meta.read_text()).get("gps_points"))
    except (OSError, ValueError):
        return False


def _sidecars_for(ctx, root):
    """How many trips of THIS import have their meta on disk."""
    return len(list((ctx.out_dir / root.name).rglob("trip_*_meta.json")))


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
            sys.stdout.write("\r\x1b[2K")
        print(C.dim("  Cancelled."))
        return False
    if live:
        # Erase the line rather than paint spaces over it: spaces are still a
        # line once the next print moves past them, and this one sits between
        # the banner and the status block where it reads as a stray blank.
        sys.stdout.write("\r\x1b[2K")
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
    print("  card differently: it merges parked hours into trips and invents")
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
        return ctx.last_groups[1]

    # No line above the bar. It was there because an uncached scan takes
    # minutes and something has to say so -- but the bar says it, by moving,
    # and its tail already names the very path this sentence was naming. On a
    # cached scan the whole thing came and went in under a second, leaving one
    # more sentence between a confirmation prompt and the figure it is about.
    fd, tmp = tempfile.mkstemp(prefix="dashcam-groups-", suffix=".json")
    os.close(fd)
    try:
        rc, _lines = run_stream(
            # The full path under src/, not a bare name against the checkout
            # cwd: the sources moved and this call did not, so every grouping
            # died with "can't open file". It also decides the child's
            # sys.path[0] -- src/, which is what lets that module import its
            # siblings the way make-trips-rendered.sh already runs it.
            [renderer_python(ctx), "-u",
             str(SRC_DIR / "make_dashcam_videos.py"), "--print-groups",
             "--root", str(root), "--out", str(ctx.out_dir)] + ctx.config_args + ctx.scan_args,
            ctx.exporter, "Grouping", stdout_file=tmp, quiet_finish=True,
            env_extra=_renderer_env(ctx))
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
CLIP_REVIEW_DIRNAME = "clip_review"
# Defaults for the still-frame knobs; config.txt's still_width / still_seconds
# override both the preview sheet and the site page, which is the only sane
# arrangement — a still that is right for one is right for the other.
PREVIEW_STILL_W = 1600      # wide enough to be a poster frame, not just a thumb
PREVIEW_STILL_T = 1.0       # seconds into the clip; see extract_still


def write_clip_review(ctx, trips):
    """A still from every CLIP, in a folder per trip.

    Returns (expected, root, newly made, orphans dropped).

    The trip still answers "what is this drive"; this answers "where does the
    grouping think each clip sits", which is the question when the boundaries
    themselves are suspect. A three-hour drive that came back as five
    fragments is not judged from one frame per fragment -- it is judged by
    walking the clip starts and seeing where the run really breaks.

    Trips the scanner will not render go under not_renderable/, together
    rather than mixed in: they are the ones under suspicion, and the point is
    to be able to look at exactly those.
    """
    root = ctx.out_dir / CLIP_REVIEW_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    seen, made = set(), 0
    bar, n_done, total = Bar("Clips"), 0, sum(len(t.get("front") or []) for t in trips)
    for trip in trips:
        folder = _review_folder(root, trip)
        for n, clip in enumerate(trip.get("front") or [], 1):
            n_done += 1
            _still_bar(bar, n_done, total, Path(clip).name)
            dst, was_made = _one_clip_still(Path(clip), folder, n)
            seen.add(dst)
            made += was_made
    bar.close()
    dropped = _drop_orphans(root, seen)
    return n_done, root, made, dropped


def _drop_orphans(folder, keep):
    """Remove what no longer corresponds to anything, and the folders left empty.

    The other half of building by delta. A still is named for the trip or the
    clip it shows, so one whose name nothing asks for any more is a picture of
    something that is gone -- an excluded trip, or a boundary that moved and
    renumbered the trips after it. Left there it is indistinguishable from a
    current one, which is the whole reason this folder gets swept at all.
    """
    gone = 0
    for f in sorted(folder.rglob("*")):
        if _real_file(f) and f not in keep:
            try:
                f.unlink()
                gone += 1
            except OSError:
                pass
    for d in sorted(folder.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    return gone


def _review_folder(root, trip):
    """<clip_review>/[not_renderable/]trip_NN_<day>_<hh-mm>."""
    name = "trip_%02d_%s_%s" % (trip["index"], trip.get("day", ""),
                                str(trip.get("start", ""))[11:16].replace(":", "-"))
    if trip.get("renderable", True):
        return root / name
    return root / "not_renderable" / name


def _one_clip_still(src, folder, n):
    """(path, was_made). Skipped when it is already there.

    A clip still is named for the clip it came from, so a file that exists is
    a frame of that same clip -- there is nothing about it a second seek would
    change. Only clips with no still yet cost anything.
    """
    dst = folder / ("%02d_%s.jpg" % (n, src.stem))
    if dst.is_file():
        return dst, False
    folder.mkdir(parents=True, exist_ok=True)
    extract_still(src, dst, seconds=CLIP_REVIEW_T, width=PREVIEW_STILL_W)
    return dst, True


# A beat in, not frame zero: a dashcam's first frame is often still
# auto-exposing, and a black square says nothing about where the clip starts.
CLIP_REVIEW_T = 1.0


def extract_still(src, dst, seconds=PREVIEW_STILL_T, width=PREVIEW_STILL_W):
    """One frame from a source clip, written as a jpg. True on success.

    A beat in, so a fade-from-black or still-auto-exposing first frame is not
    what he judges the trip by, and scale='min(W,iw)' so a clip narrower than W is
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
        "<details><summary>%s source clips: %d files, %s</summary>"
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
        "<header><h1><span>preview</span> %d trips in %s</h1>"
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
        rc, _lines = run_stream(cmd, ctx.exporter, "Sidecars",
                                parser=make_scan_parser(), quiet_finish=True,
                                env_extra=_renderer_env(ctx))
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
        print(C.dim("  No trips in %s." % tilde(root)))
        return record(ctx, NAME[PREVIEW], SKIPPED, started, "no trips")

    # Stills. Every trip gets one, including the auto-skipped fragments — he
    # is deciding what to keep, and a trip he cannot see is one he cannot judge.
    #
    # BY DELTA, not by rebuilding. A still is named for the trip or the clip it
    # shows, so one that is already there is a frame of that same thing and a
    # second seek would produce the same picture. Only what is missing costs
    # anything, which on a thousand-clip card is the difference between
    # minutes and nothing at all.
    #
    # What makes that safe is the sweep at the end rather than a wipe at the
    # start: a still whose name nothing asks for any more is a picture of
    # something gone — an excluded trip, or a boundary that moved and
    # renumbered the trips after it — and it goes then.
    previews_dir.mkdir(parents=True, exist_ok=True)
    stills, failed, made = {}, [], 0
    # A bar rather than a line per trip. Forty trips was forty lines of scroll
    # for a countable loop, and the two shapes it printed -- one for a still
    # that was made and one for a trip with no front clip at all -- were the
    # same line, so a failure looked exactly like a success.
    bar = Bar("Stills")
    for i, t in enumerate(trips, 1):
        front = t.get("front") or []
        name = "trip_%02d_%s_%s.jpg" % (t["index"], t["day"], t["start"][11:16].replace(":", "-"))
        dst = previews_dir / name
        _still_bar(bar, i, len(trips), name)
        if not front:
            failed.append(t["index"])
            continue
        if dst.is_file():
            stills[t["index"]] = dst
            continue
        src = Path(front[0])
        if extract_still(src, dst,
                         seconds=ctx.still_seconds, width=ctx.still_width):
            stills[t["index"]] = dst
            made += 1
        else:
            failed.append(t["index"])
    bar.close()
    if failed:
        # Not "ffmpeg could not read it": one of these two reasons is that the
        # trip has no front clip at all, and ffmpeg was never asked.
        print(C.yellow("  No still for %d trips: %s."
                       % (len(failed), ", ".join(str(i) for i in failed))))

    index = write_contact_sheet(ctx, root, payload, previews_dir, stills)
    # The contact sheet is rewritten every run and belongs to this folder, so
    # it is kept alongside the stills it links to.
    dropped = _drop_orphans(previews_dir, set(stills.values()) | {index})
    shots, review, clips_made, clips_dropped = write_clip_review(ctx, trips)
    dropped += clips_dropped

    # No trips.json refresh here any more. Preview used to re-index the site
    # manifest "while we're here", which made a looking-step write into the
    # target; whatever publishes re-indexes as its own first act, so publishing
    # never carries a stale manifest anyway. One step, one job.

    done_line("%s stills for %s trips, contact sheet at %s"
              % (C.yellow("%d" % len(stills)), C.yellow("%d" % len(trips)),
                 tilde(index)))
    print(C.green("  100%% - %s clip stills to walk the boundaries by, under %s."
                  % (C.yellow("%d" % shots), tilde(review))))
    # ALWAYS, including when it is "0 new". The two lines above are the same on
    # every run, so a pass that rebuilt nothing looks exactly like one that
    # rebuilt everything -- and a step that finishes in half a second reads as
    # a step that did not run. The reassurance is wanted most precisely when
    # there is nothing to report.
    print(C.dim("  %d new, %d already there, %d no longer wanted"
                % (made + clips_made,
                   len(stills) + shots - made - clips_made, dropped)))
    return record(ctx, NAME[PREVIEW], RAN, started,
                  "%d trips, %d stills in %s" % (len(trips), len(stills), previews_dir))


# ---------------------------------------------------------------------------
# Drop a trip from the import — DESTRUCTIVE
# ---------------------------------------------------------------------------

def _print_trip_table(ctx, root, trips):
    """List the trips, and return them by index."""
    # No rule of its own. The runner already opened "== Exclude Trip ==" and
    # a step that draws two more inside it reads as three sections where there
    # is one screen.
    print()
    by_index = {}
    for t in trips:
        by_index[t.get("index")] = t
        note = "" if t.get("renderable") else C.dim("  [%s]" % (t.get("reason") or "skipped"))
        # Every field read with a default. A grouping that is short of one is a
        # reason to print a thinner row, not to take down the step drawing it.
        print("  %2s) %s  %s -> %s  %3d clips  %8s  %9s%s" % (
            t.get("index", "?"), t.get("day", "?"),
            t.get("start", "")[11:16] or "--:--", t.get("end", "")[11:16] or "--:--",
            t.get("clips", 0), human_secs(t.get("duration_secs")),
            human_bytes(trip_bytes(t)), note))
    print()
    print(C.dim("  Stills and maps for these are in %s"
                % tilde(ctx.out_dir / PREVIEW_DIRNAME)))
    return by_index


def _ask_trip_indices(by_index):
    """The indices to drop, or None when the answer was not one."""
    _print_all(_never_renders(by_index))
    sel = ask("  Enter Trip indices to exclude: ")
    if not sel.strip():
        return None
    return _parse_indices(sel, by_index)


def _never_renders(by_index):
    """Name the trips the scanner will not encode, without picking them.

    Auto-skipping is the scanner's opinion -- too short to be worth encoding
    -- and excluding is the operator's decision that the footage never
    happened. They are not the same act, and the tool must not make the second
    on the strength of the first: a sixteen-second fragment is exactly the
    shape of the clip worth keeping, and auto-excluding one would quietly make
    the card erasable while dropping it.

    But they ARE the obvious candidates, and the alternative is that they sit
    in the workspace forever blocking item 9 -- accounted for by nothing,
    which is the state that gave "13 clips exist nowhere but this card".

    Blank stays cancel. This is a delete, and an empty answer must never be
    one, however good the suggestion.
    """
    never = [(i, t) for i, t in sorted(by_index.items())
             if not t.get("renderable", True)]
    if not never:
        return ()
    return (C.dim("  Trips %s %s. Include them to forget them."
                  % (", ".join("%d" % i for i, _t in never),
                     _too_short(t for _i, t in never))),)


def _too_short(trips):
    """Why the scanner will not render them, in its own number.

    Taken from what it SAID rather than from the setting read again here: two
    readings of one number drift, and this one is only ever printed beside the
    trips that number excluded.
    """
    for t in trips:
        m = re.search(r"--min-clips-per-group (\d+)", t.get("reason") or "")
        if m:
            return "contain less than %s clips as configured" % m.group(1)
    return "are too short to render"


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


def _picked_ids(by_index, picked):
    """The trip ids behind the chosen rows, e.g. trip_2026-07-28_08-57_01.

    The name everything off this machine is keyed on: out_base is a path under
    <out>, and only its last component means anything to a target.
    """
    return list(filter(None, map(lambda i: _out_base_name(by_index[i]), picked)))


def _out_base_name(trip):
    base = trip.get("out_base")
    return Path(base).name if base else None


def _picked_keys(by_index, picked):
    """This workspace's own handle on each chosen trip: its first clip's stamp.

    Unlike _picked_ids this never comes back short. A trip with no out_base is
    a trip nothing off this machine has ever heard of, which is precisely why
    it has no id — but it is still a trip the operator excluded, and the row
    that counts them has to say so.
    """
    return list(filter(None, map(lambda i: _trip_key(by_index[i]), picked)))


def _trip_key(trip):
    front = trip.get("front", [])
    return _stamp_of_name(Path(front[0]).name) if front else None


def _note_trips_published(world, ids):
    """Local deletion is not unpublishing. Say so rather than letting a clean
    local result imply the trip is gone from the world.

    Fired off the one all-or-nothing answer: the destination has every trip of
    this import, so it has these. It cannot fire on a partial state any more —
    with one answer there is no "this trip yes, that one no" to read — and that
    is the conservative direction: an unsaid note costs the operator a second
    look at the site, while a wrong one has him believe a copy is safe.
    """
    if _all_at_the_destination(world, ids):
        _say_they_stay(world.target.name, len(ids))


def _all_at_the_destination(world, ids) -> bool:
    if not ids:
        return False
    return _the_answer_names(world, ids)


def _the_answer_names(world, ids) -> bool:
    """YES, and given about these very trips.

    world.trip_ids IS the list handed to the plugin, so containment in it is
    the exact test for "this answer speaks about that trip". With several
    imports in the workspace the trips picked here can come from another one,
    and telling the operator a copy stays online when nobody was asked about it
    is the wrong half of this note to get wrong.
    """
    if world.target.complete is not menu.Evidence.YES:
        return False
    return set(ids) <= set(world.trip_ids)


def _say_they_stay(where, count):
    print(C.red("  NOTE: %d of these trips are already at %s and stay there."
                % (count, where)))
    print(C.dim("  Deleting locally does not remove them from the destination."))
    print(C.dim("  Rebuild and republish (%d, %d) after this, and remove them"
                % (BUILD, UPLOAD)))
    print(C.dim("  there by hand if you want them truly gone."))


class Consulted:
    """Whether the destination was asked about these trips, and what came back.

    A list-of-one bool used to carry the first half of this. The second half —
    "asked, and it could not say" — has to be recorded as the answer is read,
    because it cannot be recovered afterwards: "not published" and "nobody
    knows" are different sentences to put in a delete prompt, and a trip with
    no render name to look for was never asked about at all.
    """

    def __init__(self):
        self.asked = False
        self.unknown = False

    def saw(self, evidence):
        self.asked = True
        self.unknown = self.unknown or evidence is menu.Evidence.UNKNOWN
        return evidence is menu.Evidence.YES


def _only_copy_lines(ctx, world, payload, by_index, picked):
    """The last-copy warning, and an honest account of what was checked.

    Three states, three sentences: the target was never asked (those trips have
    no render name to look for), the target could not answer, or there is no
    target at all. Saying "not consulted" when the question actually failed is
    a lie in a delete prompt.
    """
    if _all_still_on_the_card(ctx, files_picked(by_index, picked)):
        return _safe_to_drop_lines()
    only_copy, elsewhere, consulted = [], [], Consulted()
    for i in picked:
        (elsewhere if _exists_elsewhere(ctx, world, payload, by_index[i], consulted)
         else only_copy).append(i)
    lines = []
    if elsewhere:
        lines.append(C.dim("  Trips %s also exist as a render elsewhere or at %s."
                           % (", ".join(str(i) for i in elsewhere),
                              world.target.name or "the destination")))
    if only_copy:
        lines.extend(_last_copy_banner(world, only_copy, consulted))
    return tuple(lines)


def files_picked(by_index, picked):
    return [p for i in picked for p in trip_files(by_index[i])]


def _all_still_on_the_card(ctx, files):
    """Is every file about to go still sitting on the card, by path and size.

    The strongest form of "there is another copy", and the cheapest to check:
    it does not depend on a render, a destination, or anybody's answer about
    either. When it holds, the warning below would be false.
    """
    root = ctx.selected_import
    if not (files and root):
        return False
    try:
        rel = [f.relative_to(root) for f in files]
    except ValueError:
        return False
    return all(_also_on_the_card(root / r, ctx.card / r) for r in rel)


def _safe_to_drop_lines():
    """What excluding means, when the footage is demonstrably still on the card.

    Not the last-copy banner: that one is true when it is true, and printing
    it over a delete that takes a second copy is how a warning stops being
    read where it counts.
    """
    return (C.dim("  They will be ignored in future attempts to read the same"
                  " data from the SIM card."),
            C.dim("  They can still be copied off the SIM card before the card"
                  " is erased."))


def _exists_elsewhere(ctx, world, payload, trip, consulted):
    _same, other = trip_renders(ctx, payload, trip)
    if other:
        return True
    return _at_the_destination(world, trip, consulted)


def _at_the_destination(world, trip, consulted):
    """Is this trip at the destination — the loose question, not the strict one.

    Answered by the import-wide "are these trips complete there", which is
    keyed on TRIP IDS and so still covers the case this was written for: a trip
    whose local render is long gone because it was published and cleaned up.
    Asked about a render name it would read UNKNOWN and the full "ONLY copy of
    that footage" panel would fire over a trip that is safely published, which
    is how an operator learns to stop reading warnings.
    """
    base = trip.get("out_base")
    if not base:
        return False            # no render name to look for; nothing to ask
    return consulted.saw(_answer_about(world, Path(base)))


def _answer_about(world, base) -> menu.Evidence:
    """The destination's answer where it applies, UNKNOWN where it does not.

    The world was captured about ONE import; the trips on screen come from
    whichever import the operator picked a moment later, and with several in
    the workspace those differ. Read across that boundary a yes about this
    round's trips suppresses the last-copy panel over another round's footage —
    the one sentence that has to be right in front of an irreversible delete.
    UNKNOWN is what "nobody was asked about this" has to read as here, and it
    keeps the panel.
    """
    if _answer_covers(world.target, world.out_dir, base):
        return world.target.complete
    return menu.Evidence.UNKNOWN


def _last_copy_banner(world, only_copy, consulted):
    bar = C.red("  " + "!" * (term_width() - 4))
    lines = [bar,
             C.red("  Trips %s are NOT rendered anywhere and NOT published."
                   % ", ".join(str(i) for i in only_copy)),
             C.red("  These files are the ONLY copy of that footage. Excluding"),
             C.red("  them ends it — there is nothing to restore from, here or"),
             C.red("  online.")]
    lines.extend(_why_unchecked(world, consulted))
    lines.append(bar)
    return lines


def _why_unchecked(world, consulted):
    if not consulted.asked:
        return [C.red("  (The destination was not consulted: those trips have no"),
                C.red("   render name to look for, so nothing could exist for them.)")]
    return _target_caveat(world, consulted)


def _target_caveat(world, consulted):
    """Which of the three "nothing came back" states this was.

    The distinction survives the move onto the interface: no target at all is
    NA, and a target that was asked and could not say is UNKNOWN. Collapsing
    them would print "nothing of this is off this machine" over a destination
    that simply timed out.
    """
    if not world.target.configured:
        return [C.red("  (No upload_plugin is configured, so nothing of this is"),
                C.red("   off this machine.)")]
    return _unreachable_caveat(world, consulted)


def _unreachable_caveat(world, consulted):
    """"No answer covering these trips", which is two states with one meaning.

    The plugin could not be reached, or it was asked about a different import
    than the one these trips came from. Both leave the same hole and the same
    sentence is true of both; naming a reason would mean guessing which,
    in the panel where a guess is worth least.
    """
    if not consulted.unknown:
        return []
    return [C.red("  (%s gave no answer covering these trips, so 'not published'"
                  % world.target.name),
            C.red("   is unverified — an unknown is not evidence of a copy.)")]


def drop_plan(ctx, world):
    """Item 4's plan: choose the trips, show exactly what goes, hand back the act.

    Everything irreversible is inside `act`, which only ever receives the world
    captured after the word is typed. This function may print and ask; it may
    not delete.
    """
    started = time.time()
    # Through _nothing(), which RECORDS. These four exits returned a plan and
    # logged nothing, so "no import folder", "the scan failed", "no trips" and
    # "cancelled" left no row in the session summary at all -- the one step
    # whose refusals were invisible afterwards.
    root = pick_import(ctx, "dropping a trip")
    if root is None:
        return _nothing(ctx, EXCLUDE, started, "no import folder")
    payload = load_groups(ctx, root)
    if payload is None:
        return _nothing(ctx, EXCLUDE, started, "the trip scan failed")
    trips = payload.get("trips", [])
    if not trips:
        print(C.dim("  No trips in %s." % tilde(root)))
        return _nothing(ctx, EXCLUDE, started, "no trips")
    by_index = _print_trip_table(ctx, root, trips)
    picked = _ask_trip_indices(by_index)
    if not picked:
        return _nothing(ctx, EXCLUDE, started, "cancelled")
    return _drop_plan_for(ctx, world, payload, by_index, picked, started)


def _drop_plan_for(ctx, world, payload, by_index, picked, started):
    render_files = _renders_of(ctx, payload, by_index, picked)
    if render_files:
        print()
        print(C.yellow("  Already rendered. The render goes too, %d files:"
                       % len(render_files)))
        _note_trips_published(world, _picked_ids(by_index, picked))
        for f in render_files[:8]:
            print(C.dim("      %s" % tilde(f)))
        if len(render_files) > 8:
            print(C.dim("      ... and %d more" % (len(render_files) - 8)))

    files = [p for i in picked for p in trip_files(by_index[i])] + render_files
    total = sum(_size_of(p) for p in files)
    # The table above already listed every picked trip by index, day, span and
    # clip count. Repeating it under a rule of its own, then naming all 212
    # files one per line in full, is the same screen twice and then some -- and
    # the figure the typed word answers was the one thing not standing out.
    print()
    print("  Excluding trips %s: %s files (%s) from workspace."
          % (", ".join(str(i) for i in picked), C.yellow("%d" % len(files)),
             C.yellow(human_bytes(total))))

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
        print(C.red("  Could not delete %s" % e))

    # Record the dropped clips' stamps as excluded. From here on they are
    # treated as if imported: the next delta import does not re-copy them off
    # the card, and item 9 counts them as accounted for — the warning above was
    # the decision, made once, at the only moment it can matter.
    dropped_stamps = {m.group(1) for p in files
                      for m in [STAMP_RE.search(p.name)] if m}
    if dropped_stamps:
        record_excluded_stamps(ctx, dropped_stamps)

    # Any cached view of this import is now wrong: the grouping is computed from
    # the clips that just stopped existing.
    ctx.last_groups = None
    ctx.last_scan = None

    _drop_orphan_sidecars(by_index, picked)
    _record_the_drop(ctx, by_index, picked)

    if errors:
        print(C.red("  Dropped %s of %s files (%s could not be deleted)."
                    % (deleted, len(files), len(errors))))
        return _outcome(record(ctx, NAME[EXCLUDE], FAILED, started,
                               "%d of %d files deleted, %d errors"
                               % (deleted, len(files), len(errors))))
    done_line("excluded trips %s: %s files, %s freed"
              % (", ".join(str(i) for i in picked), C.yellow("%d" % deleted),
                 human_bytes(freed)))
    return _outcome(record(ctx, NAME[EXCLUDE], RAN, started,
                           "trips %s, %d files, %s freed" % (
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
    """Sidecars of trips that are now gone. Removed, not asked about.

    They are not source footage — they describe something that no longer
    exists, and left in place an index rebuild keeps publishing a trip whose
    video can never be rendered. For a trip that WAS rendered these went with
    the render; what this catches is the preview-only case.

    It used to list them and ask "remove them too (they are derived data, not
    footage)?" after the word had already been typed. That is a second
    question about the same decision, defaulting to the answer that leaves the
    workspace describing a trip the operator has just deleted — and a prompt
    whose only sane answer is yes teaches him to stop reading prompts. They go
    with the footage, silently, because they are a consequence of it and not a
    choice of their own.
    """
    orphans = []
    for i in picked:
        base = by_index[i].get("out_base")
        if base:
            orphans.extend(_existing_sidecars(base))
    if orphans:
        _unlink_all(orphans)


def _existing_sidecars(base):
    paths = (Path(base + suffix)
             for suffix in (".html", ".gpx", "_links.txt", "_meta.json"))
    return [p for p in paths if p.is_file()]


def _namespace_now(ctx):
    """The import a drop belongs to, by folder name."""
    return ctx.selected_import.name if ctx.selected_import else ""


def _record_the_drop(ctx, by_index, picked):
    """Write down that these trips went ON PURPOSE.

    Deleting the files is NOT enough, and the reason is a fact about every
    index-rebuilding publisher rather than about one of them: a rebuild
    deliberately carries a previously-published trip forward when its local
    output is gone, because that is what makes "delete local after publish"
    safe. A dropped trip and a cleaned-up published trip look identical to it —
    id in the previous index, nothing on disk — so nothing downstream can tell
    them apart. Only the moment of dropping knows which this is.

    Recorded rather than announced. It used to be a call into the plugin, which
    made the exporter run a stranger's code immediately after an irreversible
    delete, and told only the plugin that happened to be configured at that
    moment. As a fact in the workspace it survives a restart, reaches a plugin
    installed next week, and arrives where a builder can act on it: item 5
    hands it over as Workspace.dropped_ids.
    """
    ids = _picked_ids(by_index, picked)
    keys = _picked_keys(by_index, picked)
    if not ids and not keys:
        return
    # It used to return early on an empty id list, which is the ONLY case a
    # fragment ever reaches: too short to render, so no out_base, so no id, so
    # nothing about it was written down at all and the progress row counted it
    # as never excluded.
    #
    # Recorded, not announced. Both ledgers are bookkeeping the operator
    # cannot act on, and each was a dim line above the one green sentence that
    # says what actually happened.
    record_dropped_trips(ctx, ids, keys, _namespace_now(ctx))


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
        print(C.dim("  Aborted render cleaned up: %d partial files, %d scratch files."
                    % (len(removed), scratch)))
        for p in removed[:6]:
            print(C.dim("    removed %s" % tilde(p)))
    return len(removed) + scratch


def after_render(ctx):
    """A finished render leaves renders and sidecars, not intermediates."""
    n = _clear_intermediates(ctx)
    if n:
        print(C.dim("  Cleared %d scratch files from .intermediates." % n))
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
            # %8s on the span and %9s on the encode length, the same widths
            # item 4's table uses for the same two figures. Unpadded, every
            # row's last column started somewhere else -- 48:12 is five
            # characters and 6:29:06 is seven.
            movecol = (human_secs(float(move_min) * 60.0)
                       if move_min is not None else "")
            line = "%s%2d) %s  %s -> %s  %3d clips  %8s  %9s" % (
                mark, g["index"], g.get("day", ""),
                str(g.get("start", ""))[11:16], str(g.get("end", ""))[11:16],
                g.get("clips", 0), span, movecol)
            if not g.get("renderable", True):
                line += C.dim("  auto-skipped: %s" % (g.get("reason") or "fragment"))
            print(line)
        if have_move:
            print("      %8s span, %9s to encode (parking is cut)"
                  % (human_secs(tot_span), C.yellow(human_secs(tot_move))))
        print()
    elif ctx.last_scan and ctx.last_scan.root == root:
        print("  Last scan: %d trips, %d renderable%s" % (
            ctx.last_scan.total, ctx.last_scan.renderable,
            (", auto-skipped %s" % ", ".join(map(str, sorted(ctx.last_scan.skipped))))
            if ctx.last_scan.skipped else ""))

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
        print(C.dim("  Already rendered: %s" % ", ".join(str(i) for i in done_idx)))
        if todo_idx:
            # Amber on the list the blank answer acts on; dim on the one it
            # does not. Green was saying "good" about a fact, and bold was
            # spent on the same list the prompt below repeats.
            print("  Not yet rendered: %s"
                  % C.yellow(", ".join(str(i) for i in todo_idx)))

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
        what = "the video of 1 trip" if len(bases) == 1 else \
               "the videos of %d trips" % len(bases)
    else:
        doomed = _videos(ns.rglob("*")) if ns.is_dir() else []
        what = "every video under %s" % tilde(ns)
    if doomed:
        size = sum(f.stat().st_size for f in doomed if f.exists())
        print()
        print("  Replacing %s: %s files (%s). Only video goes."
              % (what, C.yellow("%d" % len(doomed)), C.yellow(human_bytes(size))))
        if not confirm("  Delete and re-render?", True):
            return record(ctx, NAME[RENDER], ABORTED, started,
                          "Aborted by user pre-run.")
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
    # Not echoed, and not kept. The command line is this module's business,
    # and the renderer already records its own argv at the top of the run log.
    # The "[Trip 3/6] ..." headers it prints are what the bar's own note
    # already says, and its "✓ <absolute path>" lines are one per video in the
    # renderer's words -- the sentence below counts them once, in the tool's.
    rc, _lines = run_stream(cmd, ctx.exporter, "Render", parser=make_render_parser(),
                            env_extra=_renderer_env(ctx), quiet_finish=True)
    after = set(rendered_mp4s(ctx.out_dir))
    new = after - before
    if rc != 0:
        return record(ctx, NAME[RENDER], FAILED, started,
                      "exit %d (%d new videos before the failure)" % (rc, len(new)))
    grown = human_bytes(sum(p.stat().st_size for p in new))
    detail = "%d new videos, %s" % (len(new), grown)
    done_line("encoded %s videos (%s) into %s"
              % (C.yellow("%d" % len(new)), grown, tilde(ctx.out_dir)))
    # A finished render leaves renders and sidecars, not scratch.
    after_render(ctx)

    # Nothing is told to the publishing target here. Whatever index it keeps
    # is Upload Website's business, and a target rebuilds as its own first act
    # — so the publish path never carried a stale index because of this. One
    # item, one job. (Exclude Trip still calls dropped(), for a reason that is
    # not convenience: it is the only place that can say a trip was DROPPED
    # rather than cleaned up after publishing, and the two are indistinguishable
    # from the outside afterwards.)
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
# That is deliberate: the page has to be buildable by someone who has no
# account, no second repo and no manifest — so it reads the output tree and
# nothing else. It never asks the configured target anything and never reaches
# a network host. If nothing is configured the page is still complete.
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

RESULT_FILE = "dashcam_export_data_site.html"
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
        print(C.yellow("  %d files already present were left as they were:" % len(kept)))
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


def build_result_page(ctx, out_dir, gather):
    """Write RESULT_FILE into the output dir. Returns a summary dict.

    One file, no folder: it exists to be opened, and to be sent to someone who
    will open it once. A folder of assets is the wrong shape for that.

    `gather` has no default. It used to fall back to the mover for "anything
    calling this directly", and there is nothing calling it directly any more:
    this is the local edition's deliverable and LocalPage is its one caller.
    A default here would be a second place the gathering decision is made.
    """
    out_dir = Path(out_dir)
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

    foot = ('<p class="foot">Each shape is that trip\'s GPS track, coloured by speed. '
            'Made with dashcam-exporter.</p></div>')

    doc = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Trips</title><style>%s</style></head><body>%s%s%s%s</body></html>"
            % (RESULT_CSS, head, "".join(rows), key, foot))
    page.write_text(doc, encoding="utf-8")
    made["bytes"] = page.stat().st_size
    return made


def step_site(ctx, gather):
    """Write the one-file result page into the output dir. THE LOCAL EDITION.

    `gather` also MOVES the render tree into final_<day>_<import>: there is no
    separate gather item, and gathering is what makes the workspace
    expendable, so it lives here or nowhere.

    Nothing calls this when an uploader is configured. The page and the
    sentence below it — nothing leaves this machine — are the local edition's
    deliverable and its promise, and printing either on an install that is
    about to publish everything was the bug that started this.
    """
    started = time.time()

    # An absent state is dim, not amber. Nothing is wrong with a workspace that
    # has not been rendered yet -- it is where every cycle starts.
    if not ctx.out_dir.is_dir():
        print(C.dim("  Nothing rendered yet: %s does not exist." % tilde(ctx.out_dir)))
        return record(ctx, NAME[BUILD], SKIPPED, started, "no output tree")

    info = build_result_page(ctx, ctx.out_dir, gather)
    if not info["trips"]:
        print(C.dim("  No trips under %s yet." % tilde(ctx.out_dir)))
        return record(ctx, NAME[BUILD], SKIPPED, started, "no trips")

    _print_all(_page_caveats(info))
    size = human_bytes(info.get("bytes", 0))
    done_line("built %s trips into %s (%s)"
              % (C.yellow("%d" % info["trips"]), tilde(Path(info["path"])), size))
    return record(ctx, NAME[BUILD], RAN, started, "%d trips, %s" % (info["trips"], size))


def _page_caveats(info):
    """What the page cannot show, and only when there is something it cannot.

    Dim: these are absences, and both are ordinary -- a trip described but not
    yet encoded, a trip through a tunnel with no fix.
    """
    lines = []
    if info["no_video"]:
        lines.append(C.dim("  %d trips have no video yet; the page says so."
                           % info["no_video"]))
    if info["no_gps"]:
        lines.append(C.dim("  %d trips have no GPS, so they show no route."
                           % info["no_gps"]))
    return lines

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
    siblings = _sibling_imports(root)
    if not siblings:
        return root
    target = root / "DCIM"
    print()
    print(C.yellow("  %s also holds %d other imports: %s" % (
        root, len(siblings), ", ".join(c.name for c in siblings))))
    print(C.yellow("  Narrowing the delete to %s; the others are untouched." % target))
    return target


def _sibling_imports(root):
    if not root.is_dir():
        return []
    return [c for c in sorted(root.iterdir())
            if c.is_dir() and (c / "DCIM").is_dir()]


def _target_still(root, planned):
    """Is the folder about to be rmtree'd still the one that was checked.

    The narrowing above is decided when the plan is drawn, and the prompt can
    then sit on screen for as long as it takes to answer it. A second terminal
    running an import in that window creates a dated folder under the sink,
    which turns "delete the sink" into "delete the sink AND the import that
    just landed in it" — footage nothing here ever looked at, and which the
    fresh world cannot object to because the guard is handed the world and not
    the target.
    """
    return _clean_target_quietly(root) == planned


def _clean_target_quietly(root):
    if _sibling_imports(root):
        return root / "DCIM"
    return root


def _print_gates(gates):
    """Who is being asked, then the gates and what each one answered.

    Which proofs are even possible depends on what is configured, so the count
    comes first and the gates number themselves against it. Writing "[1/3]"
    when only one check can run would claim two that never happened.
    """
    _print_who_answers(gates.world.target)
    _print_readings(_applicable_readings(gates))


def _print_who_answers(target):
    """Nothing. The gates below say what was answered; who answered is the
    tool's own business.

    It printed the plugin's name and its whole loader spec over a screen about
    erasing footage, and then again in the banner, and a third time in the
    sentence the erase proceeded on. Naming a third party three times above a
    delete reads as laying off the decision on it -- the decision is the
    operator's, taken on evidence this tool gathered and asked for.
    """
    return


def _applicable_readings(gates):
    return list(filter(_can_answer, gates.gate_readings()))


def _can_answer(reading):
    return reading[1].applicable


def _print_readings(readings):
    for i, (label, e) in enumerate(readings, 1):
        print("  [%d/%d] %s %s" % (i, len(readings), (label + " ").ljust(40, "."),
                                   _evidence_colour(e)))


def _evidence_colour(e):
    if e is menu.Evidence.YES:
        return C.green(e.value)
    return C.red(e.value)


def _clean_banner(ctx, gates, target, size):
    """The last thing on screen before the word is asked for.

    Nothing, for a discard. The target and the file count are three lines up
    and have not moved, and repeating them louder before the prompt is the
    screen insisting about a delete that takes a second copy. A sweep keeps
    its banner: that one destroys the only copy there is.
    """
    if gates.import_is_disposable():
        return ()
    return (C.red("  Deleting %s removes %s of original footage permanently."
                  % (tilde(target), human_bytes(size))),) + _what_survives(ctx, gates)


def _what_survives(ctx, gates):
    unproven = gates.unproven_lines()
    if unproven:
        return _nothing_off_this_machine_was_checked(ctx, unproven)
    return _on_the_targets_word(gates.world.target, gates.destination_proof())


def _on_the_targets_word(target, proof):
    """What survives, without naming who vouched for it.

    It named the implementation twice -- once as "the copies <name> holds"
    and once as "proceeding on <name> (<full loader spec>)'s answer" -- on the
    argument that the decision should stay attributable. It still is: the
    ledger's note for the sweep records whose answer it rested on, in a file
    that outlives the session. On screen, above a delete, the same fact three
    times reads as the tool laying the decision off on somebody else. The
    decision is the operator's, on evidence this tool gathered and printed
    two lines above.
    """
    return (C.dim("  The renders stay, and so do the copies at the destination;"
                  " the raw clips do not come back."),)


def _nothing_off_this_machine_was_checked(ctx, unproven):
    """Not a warning about missing setup — a statement of what survives this,
    which is strictly less than it would be with something to publish to. The
    check that could not run is named, so it is obvious this passed unexamined
    rather than passed."""
    return ((C.red("  Publication was NOT verified — it could not be:"),)
            + tuple(C.red("    " + line) for line in unproven)
            + (C.red("  The renders under %s are therefore the only" % tilde(ctx.out_dir)),
               C.red("  copy of this footage that exists. Lose that disk and the trip"
                     " is gone."),
               C.dim("  Back the renders up elsewhere first, or leave the import where"
                     " it is — keeping it costs disk, not data.")))


def clean_workspace_plan(ctx, world):
    """Item 8's plan. Prints, asks nothing irreversible, refuses early.

    The heavy guard runs TWICE on purpose: here, so a refusal never reaches
    the prompt, and again inside the commit against a world captured
    after the word was typed. Same method both times, so the two cannot
    drift the way two hand-copied chains did — what differs is the world it
    is bound to, which is the entire point of asking again.

    One Gates for this whole screen. Every question below — may it go, what
    goes, what survives, which gate answered what — is asked of the same
    frozen world, and it is the object rather than a convention that says so.
    """
    started = time.time()
    root = pick_import(ctx, "clearing the workspace")
    if root is None:
        return _nothing(ctx, CLEAN_WS, started, "no import folder")
    target = _clean_target(ctx, root)
    if not target.is_dir():
        print(C.dim("  Nothing to delete at %s." % tilde(target)))
        return _nothing(ctx, CLEAN_WS, started, "nothing at the target")

    size, files = tree_size(target), count_files(target)
    rows, _loose = _trip_rows(ctx, root, world)
    print("  Target: %s" % tilde(target))
    print()
    _print_all(_cleaning_block(rows, files, size))

    gates = guards.Gates(world)
    verdict = gates.clean_is_allowed()
    if verdict.blocked:
        _print_all(_refusal_block(rows, verdict, world))
        if not world.orphan_clips:
            # Only when they bear on the refusal. Over an orphan refusal the
            # gates all read yes -- that is exactly why this floor exists --
            # and "601 of the 601 files are not on the card" is the DISCARD
            # path explaining itself about a path nobody took.
            _print_all(guards.unsourced_lines(world))
            _print_gate_detail(world)
        return _nothing(ctx, CLEAN_WS, started, _refusal_note(rows, verdict))

    _print_all(_what_goes_lines(gates))
    _print_all(_why_it_may_go(gates))
    return menu.Plan(_clean_gate,
                     lambda fresh: _clean_workspace_commit(ctx, fresh, root, target,
                                                           size, files, started),
                     banner=_clean_banner(ctx, gates, target, size))


def _clean_gate(world):
    """Item 8's heavy guard in the shape menu.Plan asks for: world -> Verdict.

    The gates the screen above was printed from are bound to the world it was
    drawn with, and the re-check is only worth running against a world captured
    after the word was typed. So the fresh world gets its own Gates here, and
    the method asked of it is the same one.
    """
    return guards.Gates(world).clean_is_allowed()


# One row per trip in the import: what it is, and what accounts for it.
# `state` is the whole question this screen exists to answer, so it is a word
# rather than a flag -- "not excluded" is what the operator has to act on, and
# it has to read as such next to the two that need nothing.
TripRow = collections.namedtuple("TripRow", "index day clips bytes state")


def _trip_rows(ctx, root, world):
    """Every trip in this import, and whether anything accounts for it.

    Rendered means it became a video, which is what the destination is then
    asked about. Excluded means he decided it never happened. A trip that is
    neither is footage about to be deleted on no one's decision, and that is
    what the refusal is about.

    Returns (rows, clips the scan grouped into no trip at all).
    """
    payload = _cached_grouping(ctx, root) or load_groups(ctx, root) or {}
    excluded = excluded_stamps(ctx)
    rendered = {r.name for r in world.renders_here}
    rows, grouped = [], set()
    for trip in payload.get("trips", []):
        stamps = {m.group(1) for c in (trip.get("front") or [])
                  for m in [STAMP_RE.search(Path(c).name)] if m}
        base = _out_base_name(trip) or ""
        has_render = bool(base) and any(n.startswith(base) for n in rendered)
        if stamps and stamps <= excluded:
            state = "excluded"
        elif has_render and stamps <= covered_stamps(ctx, stamps):
            state = "rendered"
        elif has_render:
            # A render exists but does not reach every clip of the trip. The
            # grouping moved after it was encoded, so the trip has clips the
            # video does not contain -- and those clips are covered by nothing.
            state = "render is stale"
        else:
            state = "not excluded"
        rows.append(TripRow(trip.get("index"), trip.get("day", "?"),
                            len(trip.get("front") or []), trip_bytes(trip), state))
        grouped |= stamps
    return rows, len(_import_clip_stamps(root) - grouped)


def _cleaning_block(rows, files, size):
    """What is about to go, before anything is decided about it.

    Trips only. The parking-mode snippets that belong to no drive are already
    inside the total, and naming them here answers a question nobody asked on
    the way past -- this screen is about to delete everything either way. They
    still get their line in the REFUSAL, where they are the reason.
    """
    lines = [C.green("  Cleaning:"),
             C.green("    Total:   %s files (%s)" % (files, human_bytes(size))),
             C.green("    %d trip%s:" % (len(rows), "" if len(rows) == 1 else "s"))]
    lines.extend(_trip_line(r) for r in rows)
    return tuple(lines) + ("",)


def _trip_line(row):
    text = _trip_line_plain(row)
    # Dim for the states that need nothing, so the eye lands on the ones that
    # do -- a trip to exclude, or one whose video no longer reaches it.
    needs_him = row.state in ("not excluded", "render is stale")
    return C.green(text) if needs_him else C.dim(text)


def _refusal_note(rows, verdict):
    """The one line this refusal leaves in the session summary.

    In trips, like the screen it came from. The guard counts clips because
    clips are what it can see in a World, but a summary read hours later has
    to say what the operator has to DO, and what he does is exclude a trip.
    """
    owed = [r for r in rows if r.state in ("not excluded", "render is stale")]
    # Only for the refusal this is about. The orphan floor is the one that
    # names what it refused over, so its verdict is the one carrying evidence;
    # the floors above it are different facts -- "nothing from this import was
    # rendered" is not answered by excluding a trip, and a note saying so
    # would send the operator to the wrong entry hours later.
    if not (owed and verdict.evidence):
        return "refused: %s" % verdict.reason
    return ("%d trip%s not excluded, refused to clean workspace"
            % (len(owed), "" if len(owed) == 1 else "s"))


def _refusal_block(rows, verdict, world):
    """The one red line, then exactly what to do about it.

    Numbered, because it is two steps in a fixed order and the second is easy
    to forget: excluding does not clean, it only removes the reason this
    refused.
    """
    stale = [r for r in rows if r.state == "render is stale"]
    owed = [r for r in rows if r.state == "not excluded"]
    if not (stale or owed):
        return (C.red("  Refusing to clean workspace: %s." % verdict.reason),
                ) + _orphan_detail_lines(world)
    lines = [C.red("  Refusing to clean workspace: %s." % _what_is_owed(stale, owed))]
    lines.extend(C.green(_trip_line_plain(r)) for r in stale + owed)
    lines.append("")
    lines.append(C.green("  To proceed:"))
    step = 1
    if stale:
        # Not excludable, and it must not be: the trip is wanted, its video is
        # simply short of it. Re-encoding is the answer, and item 2 first
        # because the sidecar was written from the old grouping too.
        lines.append(C.green("    %d. Run \"%d) %s\" then \"%d) %s\" — the grouping"
                             " moved, so the video"
                             % (step, META, NAME[META], RENDER, NAME[RENDER])))
        lines.append(C.green("       no longer reaches every clip of the trip."))
        step += 1
    if owed:
        lines.append(C.green("    %d. Select \"%d) %s\" from the menu and exclude"
                             " the trips in this list." % (step, EXCLUDE, NAME[EXCLUDE])))
        step += 1
    lines.append(C.green("    %d. Run \"%d) %s\" again." % (step, CLEAN_WS, NAME[CLEAN_WS])))
    if owed:
        lines.extend(["",
                      C.dim("  Excluding them there records the decision against"
                            " the trip. When the same"),
                      C.dim("  footage turns up on a later card, we know it is"
                            " not wanted.")])
    lines.append("")
    return tuple(lines)


def _what_is_owed(stale, owed):
    parts = []
    if stale:
        parts.append("%d trip%s whose render no longer covers it"
                     % (len(stale), "" if len(stale) == 1 else "s"))
    if owed:
        parts.append("%d trip%s not rendered and not excluded"
                     % (len(owed), "" if len(owed) == 1 else "s"))
    return " and ".join(parts)


def _trip_line_plain(row):
    return ("      Trip %2s   - %5d clip%s (%8s)   - %s"
            % (row.index, row.clips, " " if row.clips == 1 else "s",
               human_bytes(row.bytes), row.state))


def _orphan_detail_lines(world):
    """The clips no trip holds, when they are what is left refusing."""
    n = len(world.orphan_clips)
    if not n:
        return ()
    return (C.dim("      %d clip%s in no trip at all — parking-mode snippets"
                  % (n, "" if n == 1 else "s")),)


def _orphan_detail(ctx, root, world):
    """WHICH trips go, not which clip stamps.

    A stamp is a filename; a trip is the thing he decided to keep or drop, and
    it is what item 4 asks him to name. Clips the scanner grouped into no trip
    at all are counted rather than listed -- a card's parking-mode event
    snippets are dozens of one-clip nothings, and naming each teaches nothing
    the count does not.
    """
    if not world.orphan_clips:
        return ()
    # The cache only. A refusal must not start a minutes-long boundary scan to
    # phrase itself, and without a grouping the counted line below still says
    # the whole truth -- just without naming the trips.
    payload = _cached_grouping(ctx, root) or load_groups(ctx, root) or {}
    by_stamp = {}
    for trip in payload.get("trips", []):
        for clip in trip.get("front") or []:
            m = STAMP_RE.search(Path(clip).name)
            if m:
                by_stamp[m.group(1)] = trip
    named, loose = {}, 0
    for stamp in world.orphan_clips:
        trip = by_stamp.get(stamp)
        if trip is None:
            loose += 1
        else:
            named.setdefault(trip["index"], trip)
    lines = []
    for index in sorted(named):
        trip = named[index]
        n = len(trip.get("front") or [])
        lines.append(C.red("        trip %d, %s, %d clip%s — this footage goes"
                           % (index, trip.get("day", "?"), n,
                              "" if n == 1 else "s")))
    if loose:
        # Said plainly, because the way out differs. Item 4 drops TRIPS, and a
        # clip the scanner joined to no drive cannot be selected there at all
        # -- told to go and exclude it, he finds nothing to pick and comes
        # back to the same refusal.
        lines.append(C.dim("        %d clip%s in no trip at all — parking-mode"
                           " snippets, not selectable in %d) %s"
                           % (loose, "" if loose == 1 else "s",
                              EXCLUDE, NAME[EXCLUDE])))
    return tuple(lines)


def _what_goes_lines(gates):
    """A discard names what else goes; a sweep says the one thing that matters.

    They are not the same act. A discard deletes a second copy of clips the
    card still holds, checked file by file -- but it takes the sidecars and
    the renders with it, and a render is hours even though the footage it
    came from is safe. Worth one line, not a warning.

    A sweep destroys the only copy of the raw footage there will ever be, and
    that sentence stays however tidy the screen gets.
    """
    if gates.import_holds_no_footage():
        # Not red, and not that sentence: every clip is already gone, dropped
        # by item 4. Saying "the ORIGINAL footage" over a workspace holding no
        # footage is a warning about nothing, and a warning that is sometimes
        # about nothing is one the operator learns to click past.
        return (C.dim("  No footage here: every trip was excluded. What goes "
                      "is the sidecars and the GPS logs."),)
    if not gates.import_is_disposable():
        return (C.red("  This is the ORIGINAL footage and it is not recoverable."),)
    trips = max(len(gates.world.metas), len(gates.world.renders))
    if not trips:
        return ()
    return (C.dim("  Cleaning the workspace removes %d trips and the metadata."
                  % trips),
            C.dim("  All sources are still on the SIM card and the process can be"),
            C.dim("  restarted without any loss."))


def _why_it_may_go(gates):
    """The publish gates, and only when there is publishing to gate.

    A discard rests on the card holding every file, which the guard has just
    checked one by one; narrating that made four lines of reassurance in
    front of a delete that takes a second copy. The gates stay on the sweep,
    where the operator is being asked to act on somebody else's answer.
    """
    if gates.import_is_disposable():
        return ()
    print()
    _print_gates(gates)
    return ()


def _the_way_out(world):
    """Name the step that resolves this, when there is one.

    A refusal that only says no leaves the operator to work out which of nine
    entries un-sticks it, and the answer here is not obvious: the clips belong
    to a trip too short to render, which is why nothing accounts for them, and
    the entry that helps is the one for deleting trips on purpose.

    Only for the orphan floor. The other two refusals are answered by doing the
    work -- render the trips, publish them -- and a step that says "go and
    render" is telling him what he already came here from.
    """
    if not world.orphan_clips:
        return ()
    back = (C.yellow("  Put the card back in and they are accounted for again."),) \
        if not world.card.dcim else ()
    return back + (
        C.yellow("  Or drop them here with the word below — that records the"
                 " decision instead of"),
        C.yellow("  losing it, and the import stops offering them back every"
                 " cycle."))


def _print_gate_detail(world):
    """How the destination's answer was arrived at, under the refusal.

    The exporter's own words, not the plugin's: whether it was asked at all,
    and what it raised if it fell over. An implementation that wants to explain
    a NO says so on its own output while it is being asked; what must be here
    is the difference between "the destination said no" and "nobody could ask
    it", because those two look identical in a one-word answer.
    """
    if world.target.note:
        print(C.dim("        " + world.target.note))


def _clean_workspace_commit(ctx, fresh, root, target, size, files, started):
    """The irreversible half of item 8.

    `fresh` is the world captured AFTER the word was typed — the same one the
    guard just approved. The render sweep below re-asks a second question of
    it, and asking that of the world the menu was drawn with would judge the
    renders by an answer from before the prompt.
    """
    if not _target_still(root, target):
        print(C.red("  Refusing: %s is not the folder that was checked any more."
                    % target))
        print(C.dim("  Something landed under it while the prompt was on screen."
                    " Nothing was touched."))
        return _outcome(record(ctx, NAME[CLEAN_WS], SKIPPED, started,
                               "refused: the delete target moved"))
    discarding = guards.Gates(fresh).import_is_disposable()
    try:
        shutil.rmtree(str(target))
    except OSError as e:
        print(C.red("  Delete failed: %s" % e))
        return _outcome(record(ctx, NAME[CLEAN_WS], FAILED, started, str(e)))
    if ctx.selected_import == root:
        ctx.selected_import = None
    ctx.last_scan = None
    ctx.last_groups = None
    done_line("deleted %s files (%s) from %s"
              % (C.yellow("%d" % files), human_bytes(size), tilde(target)))
    if discarding:
        _unclaim_the_discarded(ctx, fresh)

    # The renders go too — when that is separately proven, OR when the card
    # still holds every clip they were made from. The second case is the one
    # the operator means by "wipe it, I want to start over": the footage is on
    # the card, so what an encode costs to redo is time, and leaving the
    # renders behind leaves a workspace he asked to be empty half full.
    #
    # What survives either way: the ledger's high-water mark and the archived
    # receipts of finished cycles.
    ok, why, stragglers = working_area_is_expendable(ctx, fresh.target)
    if discarding:
        ok, why, stragglers = True, "", []
    n = freed = 0
    if ok:
        n, freed = purge_published_renders(ctx, root, finished=not discarding)
    else:
        _keeping_the_renders(why, stragglers)
    return _outcome(record(ctx, NAME[CLEAN_WS], RAN, started,
                           "%d files, %s freed"
                           % (files + n, human_bytes(size + freed))))


def _unclaim_the_discarded(ctx, world):
    """Wind the high-water mark back past the footage just thrown away.

    The mark says "this machine has already taken these in", and after a
    discard that is no longer true of them. Left standing it is the trap the
    banner would have walked into: item 1 answers "nothing new at the source"
    and returns satisfied without offering the copy, so the clips are on the
    card, gone from here, and the only remaining offer is item 9.

    Only the discarded span is unclaimed. Anything older keeps its mark, which
    is what stops this from turning into a full re-copy of a card whose earlier
    rounds were published and swept.
    """
    lowest = min(_stamps_in(world.import_files), default="")
    if not lowest:
        return
    kept = max((s for s in world.card.stamps if s < lowest), default="")
    _lower_the_mark(ctx, lowest, kept)


def _lower_the_mark(ctx, lowest, kept):
    d = read_ledger(ctx)
    if (d.get("through") or "") < lowest:
        return                                  # never claimed them anyway
    d["through"] = kept
    _write_ledger(ctx, d)
    print(C.dim("  Import mark wound back to %s — those clips count as new again."
                % (kept or "nothing")))


def _stamps_in(files):
    return set(filter(None, map(_stamp_in_name, files)))


def _stamp_in_name(name):
    m = STAMP_RE.search(name)
    return m.group(1) if m else None


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

def _card_advisory(ctx, world):
    """Whether the workspace copy is published yet — a note, not a gate.

    Erasing the card is allowed on the strength of a copy existing here. If
    that copy is not published, it becomes the ONLY one the moment the card
    goes, which is worth saying out loud even though it does not refuse.
    """
    # One line for the lot. It said the same two-line sentence once per
    # unpublished import, hard-wrapped at a column the terminal knows nothing
    # about -- three imports meant six amber lines saying one thing.
    unpublished = [cand for cand in import_candidates(ctx)
                   if not import_is_expendable(ctx, cand, world.target)[0]]
    if not unpublished:
        return []
    return [C.yellow("  %s imports here are not published yet: after this the copy"
                     " on this machine is the only one."
                     % C.yellow("%d" % len(unpublished)))]


def _dropped_on_purpose(ctx, number, stamps, guard, scope, started):
    """Record these clips as dropped, then re-ask the guard. (fresh, refusal).

    Both ways past a refusal do this, and it is the whole of what makes them
    safe: neither skips its gate. Excluding is what this tool already calls
    "this footage never happened, deliberately" -- it is permanent, it survives
    every clean-up, the delta import stops offering the clips back, and every
    guard honours it. So the refusal becomes FALSE and the act then passes the
    same gates it always did.

    `fresh` is the world captured after the recording, for a caller that goes
    on to act on it. `refusal` is non-None when something ELSE is still in the
    way, which is a stop rather than a failure: the ledger changed, nothing
    was erased, and the reason is on screen.
    """
    stamps = frozenset(stamps)
    if not stamps:
        return None, None
    record_excluded_stamps(ctx, stamps)
    print(C.dim("  %d clips recorded as dropped on purpose." % len(stamps)))
    fresh = looked_at(ctx, scope)
    verdict = guard(fresh)
    if verdict.blocked:
        print(C.red("  Still refusing: %s." % verdict.reason))
        return None, _outcome(record(ctx, NAME[number], SKIPPED, started,
                                     "refused: %s" % verdict.reason))
    return fresh, None


def erase_card_plan(ctx, world):
    """Item 9's plan."""
    started = time.time()
    # One sentence, one colour. The erase takes more than the clips -- the rear
    # camera, the GPS archives, the photos, the thumbnails, the event log --
    # and "including related data" is what says so without putting a second
    # figure on a screen where every number should reconcile.
    #
    # The accounting breakdown that used to sit under it is GONE FROM THE
    # SCREEN ONLY. card_is_expendable still decides, per clip, and still
    # refuses when anything is accounted for by nothing -- both before the
    # word and again after it. What was removed is a reassurance printed at
    # the moment of maximum attention, directly under the sentence that says
    # footage is about to go; the refusal, when there is one, still names what
    # is owed and why.
    lines = [C.red("  %d clips will be deleted from the SIM card"
                   " (including related data)." % len(world.card.stamps))]
    lines.extend(_card_advisory(ctx, world))
    return menu.Plan(guards.card_is_expendable,
                     lambda fresh: _erase_card_commit(ctx, started),
                     banner=tuple(lines))


def drop_unaccounted_then_erase(ctx, world):
    """Record the strays as dropped on purpose, then erase the card.

    The recording is the point. It is the same act item 4 performs per trip,
    it is permanent, it survives every clean-up, and every guard already
    honours it -- so afterwards the card is expendable for a reason that is
    written down rather than waived. The delta will not offer those clips
    again either, which is the other half of the decision.

    Then the ordinary path: capture again, ask card_is_expendable, and erase
    only if it now says yes. If some OTHER refusal is standing -- nothing ever
    imported, a clip that is owed for a different reason -- this stops, having
    changed only the ledger.
    """
    started = time.time()
    fresh, refusal = _dropped_on_purpose(ctx, ERASE_CARD, world.card.new_stamps,
                                         guards.card_is_expendable,
                                         menu.Scope.LOCAL, started)
    if refusal is not None:
        return refusal
    return _erase_card_commit(ctx, started) if fresh else None


def _erase_card_commit(ctx, started):
    gone, freed, reason = wipe_card(ctx)
    if reason:
        print(C.red("  Card NOT cleaned: %s." % reason))
        return _outcome(record(ctx, NAME[ERASE_CARD], SKIPPED, started,
                               "refused: %s" % reason))
    return _outcome(record(ctx, NAME[ERASE_CARD], RAN, started,
                           "%d files, %s freed" % (gone, human_bytes(freed))))


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
    # And the receipts of cycles already finished. Without them a card whose
    # trips were published and cleaned up last week reads as footage that
    # exists nowhere, and the erase is refused forever.
    metas += _safe_rglob(archive_dir(ctx), "trip_*_meta.json")
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


def orphan_clips(ctx, root):
    """Clips OF A TRIP whose only copy is this import.

    Not on the card, not inside a trip that was rendered, not excluded on
    purpose. Every other clip in the workspace has something else vouching for
    it; these have nothing, and Clean Workspace is about to delete them.

    They exist because a trip too short to render gets no sidecar, so it is in
    no trip_ids and counts toward no expected_trips — which makes it invisible
    to both of item 8's other gates. The destination answers YES honestly about
    the trips it was asked about, the local floor is satisfied by the renders
    that do exist, and the fragment goes with the sweep.

    OF A TRIP, and that is the whole of the restriction. A camera parked for a
    week writes event snippets that join no drive, and the scanner groups them
    into nothing — so no render can cover them and item 4, which drops TRIPS,
    cannot select them. Counted here they would refuse the clean-up forever
    with no way to answer it. They are named on the screen as what they are and
    they go with the sweep.

    The grouping comes from this session's cache. Without one nothing here can
    tell a trip's clip from a snippet, and it says nothing rather than guessing
    -- the plan reads the grouping before it prints, and the re-check after the
    typed word runs with it cached.
    """
    if root is None:
        return ()
    here = _import_clip_stamps(root)
    if not here:
        return ()
    accounted = (set(card_stamps(ctx)) | set(excluded_stamps(ctx))
                 | set(covered_stamps(ctx, here)))
    return tuple(sorted((here - accounted) & _clips_in_a_trip(ctx, root)))


def _clips_in_a_trip(ctx, root):
    """Every stamp the cached grouping put in some trip."""
    payload = _cached_grouping(ctx, root)
    if not payload:
        return set()
    return {m.group(1) for trip in payload.get("trips", [])
            for clip in (trip.get("front") or [])
            for m in [STAMP_RE.search(Path(clip).name)] if m}


def _import_clip_stamps(root):
    front = root / "DCIM" / VIDEO_DIR / "front"
    if not front.is_dir():
        return set()
    return {m.group(1) for f in front.glob("*.mp4")
            for m in [STAMP_RE.search(f.name)] if m}


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
        bits.append("%d excluded" % len(dropped))
    if covered:
        bits.append("%d rendered" % len(covered))
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
    """Erase the card: DCIM and everything under it. Returns (gone, freed, reason).

    The guarded core of item 9 without the conversation. The guard is the same
    pure predicate the item's evaluate and its post-word re-check use — one
    implementation, three call sites, so they cannot drift — asked against a
    world derived right now.

    DCIM is emptied and kept. Everything under it goes, folders included — the
    camera makes the ones it needs again on the next recording, so leaving
    200video/front and its siblings behind was keeping a shape nothing
    depended on. DCIM itself stays because it is how this tool knows a card is
    in the slot at all, and nothing outside it is touched.

    reason is "" on success.
    """
    dcim = ctx.card / "DCIM"
    if not dcim.is_dir():
        return 0, 0, "no card at %s" % tilde(ctx.card)
    verdict = guards.card_is_expendable(capture_world(ctx, menu.Scope.LOCAL))
    if verdict.blocked:
        return 0, 0, verdict.reason
    return _unlink_card_files(ctx, dcim)


def _unlink_card_files(ctx, dcim):
    """Every FILE under DCIM. Every FOLDER stays, at every depth.

    The camera does not rebuild its own tree. Handed a card with DCIM there but
    200video/front missing, it sits on "loading card" and records nothing —
    which is a failure discovered in the car, hours after the erase, with
    nothing recorded in between.

    So the whole structure is left standing and only its contents go. That
    costs a stat and an unlink per file where removing five trees would have
    been five calls, and the trade is not close: the fast version cannot be
    trusted to leave a card the camera will accept.
    """
    # The indeterminate bar, because neither half of this reports progress and
    # both take real time on a full card: the walk stats every file on a slow
    # bus, and 888 unlinks over a card reader is not instant. Without it the
    # screen sat on the typed word for a minute or more with nothing between
    # the word and the closing line.
    failed = ""
    with waiting("Erasing"):
        gone = freed = 0
        for f in sorted(dcim.rglob("*")):
            if not _real_file(f):
                continue
            try:
                size = f.stat().st_size
                f.unlink()
                gone += 1
                freed += size
            except OSError as e:
                failed = str(e)
    if failed:
        print(C.red("  Could not erase everything under %s: %s"
                    % (tilde(dcim), failed)))
        return gone, freed, failed
    _unlink_quietly(ctx.workspace / ORPHAN_LIST)
    done_line("erased %s files from the card, %s freed"
              % (C.yellow("%d" % gone), human_bytes(freed)))
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
# loop; FULL also asks the configured target what it holds and serves, which may
# go to the network or shell out. Painting the menu with FULL would put that on
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


def _expected_trips(ctx, root, metas):
    """How many trips this import has. We know this; nothing needs asking.

    It used to come only from this session's cached grouping, so a fresh launch
    had no number, the local render gate answered UNKNOWN, and Clean Workspace
    refused until a scan had been run again. That was the exporter failing to
    know something entirely its own: WE do the rendering, and a trip we know
    about is a trip we wrote a sidecar for.

    So the sidecars answer it. Generate Meta writes one per trip, they sit in
    the output tree, and counting them costs a listing that has already
    happened. The grouping still wins when this session has one — it is the
    authority the sidecars were written from — but its absence is no longer a
    reason to refuse.
    """
    return _first_count(_renderable_count(_cached_grouping(ctx, root)), metas)


def _first_count(from_grouping, metas):
    if from_grouping is not None:
        return from_grouping
    return len(metas)


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
    """Is there a contact sheet.

    It looked for previews/index.html, which is the name the sheet had BEFORE
    it was dated -- write_contact_sheet now writes preview_<day>.html and
    unlinks any index.html it finds, so the one file this asked about was the
    one file guaranteed not to be there. "[ ] Preview built" was permanent,
    and item 3's own cold-start rule read the same false answer.
    """
    return any(_safe_glob(ctx.out_dir / PREVIEW_DIRNAME, "preview_*.html"))


def _safe_glob(d, pattern):
    if not d.is_dir():
        return []
    return d.glob(pattern)


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


def to_import(ctx, stamps, mark, excluded, owed):
    """The clips a delta import would copy: the ones accounted for by nothing.

    Which is `owed`, and only owed. It took three tries to see that.

    First the rule was the high-water mark alone, and a card carrying footage
    older than the mark was a dead end: the delta skipped those clips as
    already imported, item 9 refused to erase the card because they existed
    nowhere, and each was right on its own terms.

    So owed was added to the mark, as a union. That fixed the dead end and
    broke the other direction the first time the mark went backwards -- a
    discard winds it back, and with it at zero every clip on the card counted
    as never imported, including a hundred and seventeen already published
    and swept, whose renders are online and whose receipts are in the archive.
    25 GB offered where 2.5 GB was wanted.

    Owed alone is both. A clip is owed when nothing accounts for it: not
    excluded on purpose, not inside a rendered trip's span, not sitting in the
    workspace. Anything above the mark that has not been taken in yet is owed
    by that definition; anything the mark would have hidden is owed too. The
    mark stays for the questions it can answer -- what item 1 tells the
    script, what card_split counts -- and stops being the authority on what
    exists.
    """
    return frozenset(owed)


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
    new = to_import(ctx, stamps, last_imported_stamp(ctx), excluded, owed)
    here = workspace_stamps(ctx, new)
    fetch = _not_here_yet(ctx, _files_for(ctx.card, new - here))
    return W.Card(path=ctx.card, dcim=True, present=_holds_files(dcim), stamps=stamps,
                  new_stamps=new, new_files=_clips_named(ctx.card, new),
                  to_fetch=len(fetch), owed_stamps=frozenset(owed), note=note)


def _clips_named(card, stamps):
    """One path per clip, front camera, in time order.

    Front only, because the count beside it is front only: the card accounting
    counts a clip once and the camera writes it twice, so naming both made
    "13 new clips" print thirty lines and then say "and 17 more" about the
    same thirteen clips.
    """
    front = card / "DCIM" / "200video" / "front"
    return tuple(sorted(str(p) for p in _safe_glob(front, "*.mp4")
                        if _stamp_of_name(p.name) in stamps))


def _holds_files(dcim):
    return next(filter(_is_file, _safe_rglob(dcim, "*")), None) is not None


def _is_file(path):
    return path.is_file()


def _resolved(path):
    try:
        return path.resolve()
    except OSError:
        return path


# ---------------------------------------------------------------------------
# What the configured target says. Asked HERE, at capture, and frozen into the
# world — never from inside a guard.
#
# Two things follow from that and both matter. A guard stays a pure function
# over data a test can write down, and the destructive re-check gets a fresh
# set of answers for nothing: Destructive._commit calls recapture(), which
# calls capture_world(), which asks the target again. There is no second
# refresh mechanism to keep in step with the first.
# ---------------------------------------------------------------------------

def _target_facts(ctx, scope, root, trip_ids):
    """What the plugin says about this import's trips, or NA for the local
    edition.

    `root` travels with the answer as its namespace: the reply is about the
    trips of THAT import and nothing else, and the readers of it work over a
    whole <out> that can hold several.
    """
    if ctx.plugin is None:
        return W.TargetFacts()
    return _asked(ctx, scope, _namespace_of(root), trip_ids)


def _namespace_of(root):
    return root.name if root is not None else ""


def _asked(ctx, scope, namespace, trip_ids):
    """Ask, every time the world is captured. Scope does not gate this.

    It used to: LOCAL skipped the question and reported UNKNOWN, so a menu draw
    never went to the destination. That was the exporter deciding, on a guess
    about somebody else's code, that asking is expensive — and it has no idea
    what is behind the interface. An ssh session, a dict, a mock in a test, a
    binary somebody dropped in. Budgeting around a guess is the one thing this
    seam exists to prevent, and it cost real accuracy: the menu showed items as
    available that would refuse the moment they were picked.

    So the exporter asks whenever it needs to know, and WHETHER TO CACHE IS THE
    IMPLEMENTATION'S DECISION. It knows what its destination costs; this module
    does not. A plugin whose answer is slow memoises behind its own front door,
    where it can also invalidate on its own upload — which nothing out here
    could do correctly anyway.
    """
    return _answered(ctx, namespace, trip_ids)


def _facts(ctx, evidence, namespace, note=""):
    return W.TargetFacts(configured=True, name=ctx.plugin.name,
                         origin=ctx.plugin.origin, complete=evidence,
                         namespace=namespace, note=note)


def _answered(ctx, namespace, trip_ids):
    """Ask, and let a raising implementation read as unreachable.

    An implementation is trusted about what it SAYS; an exception is not a
    thing it said. Fail closed: UNKNOWN, which is exactly the reading a
    destination that could not be reached produces, and it permits nothing.
    """
    try:
        return _facts(ctx, _an_evidence(ctx.plugin.uploader.is_complete(trip_ids)),
                      namespace)
    except Exception as e:
        return _facts(ctx, menu.Evidence.UNKNOWN, namespace,
                      "%s raised while being asked: %s" % (ctx.plugin.name, e))


def _an_evidence(answer):
    """Anything that is not an Evidence is not an answer.

    Cheap, and it guards the one value in this tool that can permit an erase: a
    plugin returning True, "yes" or None would otherwise flow into a guard that
    compares it against Evidence.YES and gets an arbitrary result.
    """
    if isinstance(answer, menu.Evidence):
        return answer
    return menu.Evidence.UNKNOWN


def _trip_ids_here(metas, root, out_dir):
    """The trips THIS IMPORT contains, by id, read off the sidecars.

    Off the sidecars rather than the renders, because a trip that was never
    encoded must be in the list: it is what makes an all-or-nothing answer safe
    to act on — the destination does not have that trip, says NO, and its
    footage is not erased. Read off the renders it would be invisible.

    Namespaced to the import under judgement. Sidecars outlive every sweep, so
    the whole tree would carry months of trips a destination may legitimately
    no longer serve, and one of those would hold the erase gate shut forever.
    """
    if root is None:
        return ()
    return tuple(sorted(set(map(_trip_id_of, _metas_under(metas, out_dir / root.name)))))


def _metas_under(metas, base):
    return filter(lambda m: _is_under(m.path, base), metas)


def _is_under(path, base):
    return path is not None and base in path.parents


def _trip_id_of(meta):
    return meta.id[:-len("_meta")] if meta.id.endswith("_meta") else meta.id


def _has_result_file(base):
    return (base / RESULT_FILE).is_file()


def _page_exists(ctx):
    if _has_result_file(ctx.out_dir):
        return True
    return any(map(_has_result_file, _final_folders(ctx)))


def _excluded_at(ctx):
    try:
        return (ctx.state_dir / EXCLUDED_FILE).stat().st_mtime
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
    renders = _renders_of_tree(ctx.out_dir)
    trip_ids = _trip_ids_here(metas, root, ctx.out_dir)
    # The plugin is asked BEFORE the expendability check, because that check is
    # now half local (a render in a final_ folder) and half its answer.
    target = _target_facts(ctx, scope, root, trip_ids)
    return _world_of(ctx, scope, imports, root, metas, renders, trip_ids, target,
                     working_area_is_expendable(ctx, target))


def looked_at(ctx, scope):
    """Every capture the operator sits through, under one bar.

    A capture reads the workspace and asks the plugin, and what the second
    half costs is not knowable from here -- a dict, an ssh session, a bucket
    listing, a mock. Most of the time the plugin has an answer in hand and
    this is instant; after a step that told it to forget, it is not. One
    helper so there is one answer to "what does the operator see while it
    happens", rather than four call sites of which three showed nothing.
    """
    with waiting("Reading the workspace and querying the plugin..."):
        return capture_world(ctx, scope)


def _meta_paths(metas):
    return list(filter(None, map(_path_of, metas)))


def _path_of(meta):
    return meta.path


def _world_of(ctx, scope, imports, root, metas, renders, trip_ids, target,
              expendable):
    settled, why, stragglers = expendable
    card = _card_facts(ctx)
    mine = _import_files(root)
    return W.World(
        at=time.time(), scope=scope, strategy=menu.Strategy.of(ctx.plugin),
        offline=ctx.offline,
        # RESOLVED: an implementation may compare this against a symlink of its
        # own, and a symlink resolves to the real path. Comparing /var/...
        # against /private/var/... reports a mismatch on every macOS install.
        out_dir=_resolved(ctx.out_dir), out_dir_owner=claim_out_dir(ctx),
        imports=imports, selected_import=root, metas=metas,
        renders=renders, renders_here=_renders_here(ctx, root),
        trip_ids=trip_ids, dropped_ids=dropped_trip_ids(ctx),
        dropped_trips=dropped_trip_keys(ctx, root.name if root else ""),
        import_files=mine, unsourced_files=_unsourced_files(root, ctx.card, mine),
        card_shares_the_import=_card_shares(ctx.card, root),
        orphan_clips=orphan_clips(ctx, root),
        final_folders=_final_folders(ctx), expected_trips=_expected_trips(ctx, root, metas),
        has_track=_has_track(imports), stills_current=_stills_current(ctx),
        local_page=_page_exists(ctx), ledger_mark=last_imported_stamp(ctx),
        excluded=frozenset(excluded_stamps(ctx)), excluded_at=_excluded_at(ctx),
        newest_meta_at=_newest_mtime(_meta_paths(metas)),
        workspace_settled=settled, workspace_note=why,
        stragglers=tuple(stragglers), card=card, target=target)


def _import_files(root):
    """Every file in ONE import, by path relative to it.

    Everything, not the front clips: the delete is an rmtree of the folder, so
    the question is whether the CARD has what that folder holds -- the rear
    camera, the GPS tars, the event log, and anything else in there. A check
    that only knew about front clips approved deleting the rest unexamined.
    """
    if root is None:
        return frozenset()
    return frozenset(str(p.relative_to(root)) for p in _safe_rglob(root, "*")
                     if p.is_file())


def _unsourced_files(root, card_path, files):
    """The ones the card does not have. Empty means the import is a copy."""
    if root is None:
        return frozenset()
    return frozenset(f for f in files if not _also_on_the_card(root / f,
                                                               card_path / f))


def _also_on_the_card(here, there):
    """Same path, same size.

    Not the same bytes: hashing a full card is minutes, and the file this
    protects came off that card through a verified rsync. Same size is what
    catches the case that matters -- a name still on the card whose clip was
    rotated away and replaced, or truncated by a bad eject.

    The event log is the one exception, and it is a real one rather than a
    convenience: the camera APPENDS to it, so the card's copy is a superset of
    whatever was imported and can never match on size again.
    """
    if not there.is_file():
        return False
    if here.name == CAMERA_LOG:
        return _size_of(there) >= _size_of(here)
    return _size_of(there) == _size_of(here)


def _card_shares(card_path, root):
    """Is the configured card the import, or something holding it.

    card_root() searches down for a DCIM tree, and an import folder holds one.
    Point the card at the workspace -- or at a symlink into it -- and the card
    resolves to the very folder item 8 is about to delete, at which point
    "every file is on the card" compares a directory against itself and comes
    out true. The one state where the check is meaningless is the one where
    acting on it erases the only copy, so it is asked separately.
    """
    if root is None:
        return False
    return _overlapping(_resolved(card_path), _resolved(root))


def _overlapping(a, b):
    return a == b or _inside(a, b) or _inside(b, a)


def _inside(a, b):
    try:
        return a.is_relative_to(b)
    except (AttributeError, ValueError):    # pragma: no cover - old pythons
        return False


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


class Console(uploader.Ui):
    """The exporter's output, lent to an implementation while it works.

    So a target's build and upload look like the rest of the tool — same
    progress bar, same colours — without it importing pipeline internals that
    will move. Nothing here is a restriction: an implementation is free to
    print() and to run its own subprocesses, and several do. This exists to
    make the nice thing the easy thing.
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def say(self, line):
        print(C.dim(line))

    def warn(self, line):
        print(C.yellow(line))

    def run(self, cmd, cwd, label, env=None, parser=None):
        # quiet_finish: the plugin says what its act achieved through the
        # Outcome it returns. Without it every plugin child left the
        # streamer's own "Deploy [####] 100% 0:34  completed" behind, which is
        # this module announcing somebody else's work in its own words.
        rc, _lines = run_stream(cmd, cwd, label, parser=parser, env_extra=env,
                                quiet_finish=True)
        return rc


def _handed_over(ctx, world):
    """The World, reduced to what an act needs to find the material.

    Not the World itself: that carries card facts, ledger marks and the
    destination's own answers, which an implementation has no business reading
    and which would couple it to internals that move.
    """
    return uploader.Workspace(
        out_dir=world.out_dir, import_dir=world.selected_import,
        renders=world.renders, metas=world.metas, trip_ids=world.trip_ids,
        dropped_ids=world.dropped_ids, offline=world.offline, ui=Console(ctx))


class PublishingCollaborator(ABC):
    """What items 5 and 7 install as their body, declared rather than assumed.

    NOT uploader.Act, and the difference is the whole reason this exists. An
    Act is handed a Workspace — the reduced, plugin-facing view — and knows
    nothing about this repo. A collaborator is handed the exporter's own
    World, because two of the four are the exporter doing the job itself and
    the other two have to translate before an Act ever sees anything. Naming
    them Acts would let a LocalPage pass load_plugin's shape check while being
    unable to accept the one argument a plugin is ever given.

    So this is the exporter's INTERNAL seam, and every method on it is
    abstract. That is safe here in a way it is not in uploader.py: the only
    implementations are the four below, all in this file, and a method added
    to the seam should stop the tool at construction rather than at the moment
    an operator presses a key. It already did the other thing once — a long
    description was added to the interface, all four collaborators silently
    lacked it, and `h 5` and `h 7` raised AttributeError on every install.
    """

    @abstractmethod
    def describe(self):
        """One line for the menu row: what THIS installation's step does.

        Read on every draw, so it must be cheap and must not go looking at
        anything. It is the entry answering for its own job rather than the
        session restating how the tool was installed, which is why it differs
        between the two editions instead of being a constant in items.py.
        """

    @abstractmethod
    def evaluate(self, world):
        """May the step run against this World, and would it do anything.

        The exporter's own guards have already had their say by the time this
        is asked; what comes back is about the collaborator's half of the job.
        GO, SATISFIED and BLOCKED all mean here what they mean everywhere else
        in the menu, and the item passes the answer through verbatim.

        Asked on every menu draw, so no network and no subprocess. Anything
        that has to leave the machine belongs behind capture_world's FULL
        scope, which asks once per dispatch and freezes what came back.
        """

    @abstractmethod
    def execute(self, world):
        """Do the step, once, and say what happened as an Outcome.

        `completed` is all the runner reads: it decides whether the position
        advances or the pipeline stays where it was. Whatever this prints is
        the operator's whole picture of the step, since the menu adds nothing
        beyond the outcome's note.
        """

    @abstractmethod
    def get_website_upload_description(self):
        """What this step amounts to on this installation, for `h 5` and `h 7`.

        Printed under the exporter's own two sentences and attributed, so a
        reader can tell which prose this repo is answerable for. Return "" when
        there is nothing to add — the local edition's collaborators do, because
        the exporter has already said everything true about them — and the help
        screen then simply ends where it ended before.

        It must not raise. This is the one screen in the tool that exists to
        tell an operator who to complain to.
        """

    @abstractmethod
    def plugin_name(self):
        """Whose words those were, or None when they are the exporter's own.

        The help screen attributes a quoted paragraph to the thing that said
        it. None means there is nothing to attribute — no plugin is configured
        — and the help screen prints no attribution line rather than crediting
        an empty quotation to "the plugin".
        """


class LocalPage(PublishingCollaborator):
    """Item 5 under the local edition: write the page, and gather.

    Gathering is what makes the local edition's workspace expendable, so the
    two belong to one job. Under a plugin neither happens here.
    """

    def __init__(self, ctx):
        self._ctx = ctx

    def describe(self):
        return ("Build the local result page from the renders. Nothing leaves "
                "this machine.")

    def get_website_upload_description(self):
        return ""      # nothing is handed over under this edition

    def plugin_name(self):
        return None    # there is no plugin to name

    def evaluate(self, world):
        """Its own renders floor, because THIS build also gathers.

        The shared guard stopped demanding renders so a described trip can be
        published before it is encoded — right for a page, wrong here. execute
        moves whole day directories into final_<day>_<import>, sidecars and
        all, and a gather with nothing rendered would carry the sidecars out
        of the import namespace where _is_described looks for them, leaving
        item 2 with a trip it no longer believes it has described.
        """
        if not guards.renders_exist(world):
            return menu.blocked("no renders on disk to gather")
        return menu.go()

    def execute(self, world):
        return _outcome(step_site(self._ctx, gather_into_final))


class TargetBuild(PublishingCollaborator):
    """Item 5 with a plugin configured: whatever its builder builds.

    The local page is not written and gather_into_final does not run. Moving
    the render tree would rename every published trip out from under whatever
    index the plugin keeps, and the page is the other product's deliverable.
    """

    def __init__(self, ctx, act):
        self._ctx = ctx
        self._act = act

    def describe(self):
        return self._act.describe()

    def get_website_upload_description(self):
        return _long_description(self._act)

    def plugin_name(self):
        return type(self._act).__name__

    def evaluate(self, world):
        return self._act.evaluate(_handed_over(self._ctx, world))

    def execute(self, world):
        # Counted on both sides of the act, because the interesting number is
        # the difference. The act's own note says what it did; this says what
        # changed, which is the thing a second press cannot fake.
        before = _holds(self._act)
        outcome = _logged(self._ctx, BUILD,
                          lambda: self._act.execute(_handed_over(self._ctx, world)))
        return _with_the_count(outcome, before, _holds(self._act))


class TargetPublish(TargetBuild):
    """Item 7 with a plugin configured: one job, however many transports.

    The same three calls as item 5's collaborator against a different act,
    which is the whole point of the acts having one shape — the only thing that
    differs is which step the outcome is logged against.

    Subclassing TargetBuild rather than the seam directly, and it stays sound
    now that TargetBuild has a base: everything inherited — describe,
    evaluate, the long description, the plugin's name — is a bare delegation to
    self._act, and self._act here is the uploader. There is nothing about item
    5 left in any of them to be wrong about item 7.

    It is not an uploader.Uploader either, and deliberately: is_complete() is
    the destination's answer and the exporter asks the ACT for it, in
    capture_world at FULL scope. Routing it through here would put a wrapper
    on the one question the erase gates rest on, for no caller.
    """

    def execute(self, world):
        return _logged(self._ctx, UPLOAD,
                       lambda: self._act.execute(_handed_over(self._ctx, world)))


def _holds(act):
    """What the builder says its result contains, or None if it will not say."""
    try:
        return act.holds()
    except Exception:
        return None      # a builder's bookkeeping is not this module's problem


def _long_description(act):
    """An act's own long-form help, or "" if it has none.

    Same shape as _holds above and for the same reason: this is a published
    interface and an act written before the question existed has to keep
    working. getattr rather than hasattr-then-call, because a plugin is free to
    answer it dynamically, and a raise here would take down a HELP SCREEN --
    the one place in the tool nothing should ever fail.
    """
    try:
        said = getattr(act, "get_website_upload_description", lambda: "")()
    except Exception:
        return ""
    return said if isinstance(said, str) else ""


def _with_the_count(outcome, before, after):
    """The act's own words, plus what the build changed.

    Only when the answer is a real one and the act succeeded. A builder that
    cannot count, or a build that failed, says exactly what it said before.
    """
    if after is None or not outcome.completed:
        return outcome
    added = after - before if before is not None else None
    return menu.did("%s, %s" % (outcome.note, _delta_words(added, after)))


def _delta_words(added, total):
    if added is None:
        return "%d trip%s total" % (total, "" if total == 1 else "s")
    if added <= 0:
        return "no new trips, %d total" % total
    return "%d new trip%s added, %d total" % (added, "" if added == 1 else "s", total)


class NoPublisher(PublishingCollaborator):
    """Item 7 under the local edition: no edges, and nothing to run.

    Constructed rather than omitted so that every number means the same thing
    on every installation — a menu that renumbers itself makes every sentence
    anyone writes about "item 6" true only locally.

    A collaborator and NOT an uploader.Uploader, which would demand
    is_complete(). There is no destination here to be complete at, and the two
    answers such a stub could give are both wrong: YES and NA each REMOVE the
    erase gate, and UNKNOWN would put a permanent "could not reach the
    destination" in front of an operator who never configured one. Nothing
    asks — the question goes to ctx.plugin.uploader, which under this edition
    is None, and the local edition's Clean Workspace rests on what is on this
    machine instead.
    """

    def describe(self):
        return "Put what was built online. Not part of this edition."

    def get_website_upload_description(self):
        return ""      # nothing is handed over under this edition

    def plugin_name(self):
        return None    # there is no plugin to name

    def evaluate(self, world):
        """What is missing from THIS installation, never how to change it.

        Which product this is and how to turn the other one on is said once at
        startup and belongs in the README; an item that answered it would be
        describing the machine it runs in rather than the job it does.
        """
        return menu.blocked("not part of this edition")

    def execute(self, world):    # pragma: no cover - unreachable by two rules
        return menu.stopped("publishing is not configured")


def _logged(ctx, number, run):
    """Run an act, and write what it amounted to into this session's log.

    It takes the act rather than its result BECAUSE OF THE CLOCK. Handed the
    outcome, the only start time available is the moment the work already
    finished, so every act logged 0:00 — a fifty-seven second deploy included,
    which is exactly the line an operator would read to find out where a
    session went.

    The log is the exporter's, not the plugin's: a StepResult carries a
    duration and a status the summary and the crash log read, and an
    implementation must not have to construct one. The Outcome itself goes
    back to the item untouched — it is the act's answer, not this module's.
    """
    started = time.time()
    outcome = run()
    record(ctx, NAME[number], _status_of(outcome), started, outcome.note)
    _closed_on(outcome)
    return outcome


def _closed_on(outcome):
    """The one green line every step ends on, for the two that are a plugin's.

    Every other step closes on `100% - <what it did>`, and these two closed on
    nothing at all: the heading, the description, whatever the child streamed,
    and then the menu again. The operator was left inferring success from the
    absence of red.

    The act's own words, not this module's. It is the only thing that knows
    what it achieved -- how many trips it indexed, how many videos went up --
    and quiet_finish exists precisely so the streamer's "completed" does not
    speak over it. Nothing is printed for an outcome that did not complete: the
    failure has already said more than a summary line could.
    """
    if outcome.completed and outcome.performed and outcome.note:
        done_line(outcome.note)


def _status_of(outcome):
    """Not completing is not failing.

    An act that stops because the operator did not type the word, or answered
    n, has done exactly what he asked. FAILED is for something going wrong,
    and a red line beside a decision he made teaches him to read past red.
    """
    if not outcome.completed:
        return ABORTED
    return _did_or_settled(outcome)


def _did_or_settled(outcome):
    if outcome.performed:
        return RAN
    return SATISFIED


class Work:
    """One per run. Holds the ctx; hands the items their bodies."""

    def __init__(self, ctx):
        self.ctx = ctx

    def yellow(self, text):
        """Paint, lent to the items. They own the words; colours live here."""
        return C.yellow(text)

    def website_export_dir(self):
        """Where the local edition puts the finished site, for help to name."""
        out = getattr(self.ctx, "website_export_dir", None)
        return tilde(out) if out else None

    def site_dir(self):
        """Where the local edition writes its page, for item 5's help to name.

        A real path beats "<dir>" by enough to be worth asking for. Returns
        None when there is nothing sensible to print, and the help falls back
        to saying it in words.
        """
        out = getattr(self.ctx, "out_dir", None)
        return tilde(out) if out else None

    # -- the bodies --------------------------------------------------------
    def progress(self, world):
        return _outcome(step_progress(self.ctx, world))

    def import_footage(self, world):
        return _outcome(step_import(self.ctx))

    def generate_meta(self, world):
        return _outcome(step_generate_meta(self.ctx))

    def build_preview(self, world):
        return _outcome(step_preview(self.ctx))

    def render(self, world):
        return _outcome(step_render(self.ctx))

    # -- the collaborators the constructor installs ------------------------
    def builder(self, strategy):
        """Item 5's whole body, not merely its mover.

        Only the MOVER used to be the branch, so the page writer ran under
        both editions and a publishing install got a local page announcing
        that nothing had left the machine. Making the whole body the branch is
        what fixes that, and it is why there is no gatherer() any more.
        """
        if strategy is menu.Strategy.UPLOADER:
            return TargetBuild(self.ctx, self.ctx.plugin.builder)
        return LocalPage(self.ctx)

    def publisher(self, strategy):
        if strategy is menu.Strategy.UPLOADER:
            return TargetPublish(self.ctx, self.ctx.plugin.uploader)
        return NoPublisher()

    # -- the destructive plans ---------------------------------------------
    def exclude_plan(self, world):
        return drop_plan(self.ctx, world)

    def clean_workspace_plan(self, world):
        return clean_workspace_plan(self.ctx, world)

    def erase_card_plan(self, world):
        return erase_card_plan(self.ctx, world)

    def drop_unaccounted_then_erase(self, world):
        return drop_unaccounted_then_erase(self.ctx, world)

    # -- what Destructive needs between the plan and the act ---------------
    def show(self, banner):
        """Nothing at all when there is nothing to show.

        The blank line belongs to the banner, not to the act of showing one.
        A discard has no banner, and printing the separator anyway put two
        blank lines between the file count and the prompt.
        """
        if not banner:
            return
        print()
        for line in banner:
            print(line)

    def ask_word(self, word):
        print()
        return ask("  Type %s to confirm: " % word)

    def recapture(self, scope):
        """The refresh point. Called after the word and before the act.

        It asks again rather than reusing the world the banner was drawn from,
        because the LOCAL half can move under the prompt — an operator deleting
        a render in Finder while the confirmation sits on screen.

        It does not tell the plugin to forget first. An act answers for the
        state it is in, so the same state has to give the same answer however
        many times it is asked; a plugin that caches and is wrong about its own
        destination is wrong, and reaching in to defeat its cache would be this
        module compensating for a contract it should be relying on.
        """
        return looked_at(self.ctx, scope)

    def refuse(self, name, reason):
        """The word was typed and the world had moved. Said once, and RECORDED.

        This path returned an Outcome and logged nothing, so the one outcome
        where an operator typed DELETE and the tool said no afterwards left no
        row in the summary at all. It takes the item's name for that: the
        refusal belongs to a step, and Work does not know which one is asking.
        """
        print(C.red("  Refused after the re-check: %s." % reason))
        print(C.dim("  Something changed while the prompt was on screen."
                    " Nothing was touched."))
        note = "Refused after the re-check: %s." % reason
        self.ctx.results.append(StepResult(name, SKIPPED, 0, note))
        return menu.stopped(note)


# ---------------------------------------------------------------------------
# The painter. Everything it draws is derived from the position, the world and
# the items' own methods — there is no second list of steps here to drift from
# ALL_ITEMS, no hardcoded number and no hardcoded label. If it needs a fact it
# asks the item or the world for it.
# ---------------------------------------------------------------------------

def _paint_body(item, verdict, offered):
    """Grey means unselectable, bold means go. Two states, and that is all.

    Destructive entries used to be red here. Nothing was gained by it: Exclude
    Trip, Clean Workspace and Delete SIM Data say what they do in their own
    names, and every one of them asks for a typed word before it erases
    anything. What red bought was three alarming entries on screen at all
    times, in a menu where the ordinary end of a cycle is to clean up — so the
    colour that should mean "stop and read" was the colour of the resting
    state, and stopped meaning anything.

    It still means that where it is earned: the only-copy banner in front of a
    drop, and a step that failed. Neither of those is on screen unless
    something is genuinely wrong.

    Unselectable comes from the item or its verdict, never from a table of
    numbers: the position not offering it, or its own guard blocking it.
    """
    if _why_not(item, verdict, offered):
        return C.dim(item.name())
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
    """Two reasons an entry is not on the table, and they are not the same.

    "Not from here" is where the pipeline stands and changes as it moves. An
    item this product does not have never becomes selectable however far you
    walk, so saying "not yet" of it is a lie that hides the one thing worth
    knowing — which product you are running.
    """
    elsewhere = sorted(set(menu_items) - set(offered))
    off = list(filter(lambda n: menu.switched_off(menu_items[n]), elsewhere))
    return _off_line(menu_items, off) + _later_line(
        list(filter(lambda n: n not in off, elsewhere)))


def _off_line(menu_items, numbers):
    if not numbers:
        return []
    return [C.dim("   %s) not available for %s"
                  % (",".join(map(str, numbers)),
                     menu_items[numbers[0]].strategy().value))]


def _later_line(numbers):
    if not numbers:
        return []
    return [C.dim("   %s) not available from here"
                  % ",".join(map(str, numbers)))]


def _why_not(item, verdict, offered):
    """Is this entry unpickable, and by which gate. "" means it is pickable."""
    if not offered:
        return _not_offered_reason(item)
    return _guard_reason(verdict)


def _not_offered_reason(item):
    if menu.switched_off(item):
        return "not available for %s" % item.strategy().value
    return "does not follow where we are"


def _guard_reason(verdict):
    """Blocked or SATISFIED both grey the entry, for different reasons.

    Blocked means it cannot run. Satisfied means it would run and find its
    work already done -- "Nothing to do: everything on the card is already
    here", which is the whole output. A name in the same colour as the steps
    that WILL do something invites pressing it to find that out, so it is
    greyed and says why when asked.

    It stays selectable: pressing it is harmless, completes, and moves the
    position on, which is what a satisfied postcondition means to the graph.
    """
    if verdict.blocked:
        return verdict.reason
    if verdict.ruling is menu.Ruling.SATISFIED:
        return verdict.reason or "nothing to do"
    return ""


def print_menu(ctx, menu_items, position, world):
    """The grid, painted from the state machine and nothing else.

    Opened with a heavy rule, because everything above it is whatever the last
    step printed -- a render's log, a deploy's output, an erase's banner -- and
    a line of the same dashes those use does not separate them from it.
    """
    print(rule(ch="="))
    verdicts = _verdicts(menu_items, world)
    offered = position.selectable(menu_items)
    shown = _in_the_grid(menu_items)
    cell = _cell_width(shown)
    _print_all(_grid(shown, verdicts, offered, cell,
                     _grid_columns(term_width(), cell)))
    # The reasons entries are greyed have moved to `p`. They are the same eight
    # lines under every menu draw, and a block that never changes stops being
    # read -- which is a problem when the one line that DID change is in it.
    # The destructive entries are already red, and every one of them asks for
    # a typed word before it does anything. A line naming them under every
    # draw was a third telling of the same fact.
    print(C.dim("  p = progress   h = help   i = info   q = quit"))


def _verdicts(menu_items, world):
    return {n: _safe_verdict(item, world) for n, item in menu_items.items()}


def _print_all(lines):
    for line in lines:
        print(line.rstrip())


ORPHAN_LIST = "orphaned_clips_on_sim_card.txt"
SHOWN = 5


def _evidence_lines(verdict):
    """What the refusal is about: enough to recognise, and a file for the rest.

    Five is enough to see WHAT they are -- the dates and the clip lengths --
    which is the decision the refusal is asking for. The whole list goes to a
    file in the workspace, because a card that has never been imported puts
    every clip in here and a thousand paths on the screen answer nothing.
    """
    files = getattr(verdict, "evidence", ()) or ()
    lines = tuple(C.dim("        %s" % tilde(f)) for f in files[:SHOWN])
    if len(files) > SHOWN:
        lines += (C.dim("        ... and %d more" % (len(files) - SHOWN)),)
    return lines


def _orphan_file(ctx, files):
    """Keep <workspace>/orphaned_clips_on_sim_card.txt in step with the truth.

    Written whole on every refusal that has clips to name, and DELETED when
    there are none -- a stale list of clips that have since been imported is
    worse than no list, because it reads as current.
    """
    path = ctx.workspace / ORPHAN_LIST
    if not files:
        _unlink_quietly(path)
        return ()
    try:
        path.write_text("\n".join(files) + "\n")
    except OSError:
        return ()
    return (C.bold("        Full list: %s" % tilde(path)),)


def _unlink_quietly(path):
    try:
        path.unlink()
    except OSError:
        pass


def _colon(reason):
    if not reason:
        return ""
    return ": " + reason


def _safe_verdict(item, world):
    """A guard that raises must not take the menu down with it."""
    try:
        return item.evaluate(world)
    except Exception as e:                        # pragma: no cover - defensive
        return menu.blocked("guard error: %s" % e)


def _in_the_grid(menu_items):
    """The entries the grid draws: the steps.

    Progress is not one. It changes nothing, it is reached with `p` like the
    other things that only show you something, and a number beside it invited
    the reading that looking at the workspace is a step in working through it.
    """
    return {n: item for n, item in menu_items.items() if not menu.is_view(item)}


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


def _info_lines(plugin=None):
    """Who made it, what it is, and where the money goes if anyone wants to.

    Its own screen rather than a banner, because it is the same every launch
    and nobody needs it twice.

    The version sits in the table rather than beside the title. It is a fact
    about this checkout, like the licence and the date, and reading it off a
    heading meant the one line nobody scans held the one number a bug report
    needs. A configured plugin gets a section of its own, in its own words, so
    a report says which of the two is at fault.
    """
    return (("",
             C.bold("  dashcam-exporter"),
             _info_setting("Designed by", "Raoul Marc Schmidiger"),
             _info_setting("Implemented by", "Claude"),
             _info_setting("Repository", REPO_URL),
             _info_setting("Licence", "MIT"),
             _info_setting("Version", version()))
            + _dated("Last Update", uploader.last_change(EXPORTER_DIR))
            + _plugin_info_lines(plugin)
            + ("",
               C.bold("  Funding"),
               _info_setting("Sponsor", SPONSORS_URL),
               _info_setting("Buy a coffee", COFFEE_URL),
               ""))


def _plugin_info_lines(plugin):
    """The plugin's own section, printed only when there is one to print.

    An empty heading over nothing says a plugin is configured and has nothing
    to say, which is a different sentence from the true one.
    """
    rows = plugin.info if plugin is not None else ()
    if not rows:
        return ()
    return ("", C.bold("  plugin")) + tuple(
        _info_setting(str(label), str(value)) for label, value in rows)


def _dated(label, when):
    """The row, or no row. A date nothing could answer is left out rather than
    printed as "unknown", which is a value nobody can act on."""
    return (_info_setting(label, when),) if when else ()


def _info_setting(label, value):
    # Not dimmed. Dim is for what the eye may skip, and this screen is nothing
    # but the facts a bug report has to quote -- the version, the licence, who
    # wrote which half. There is no filler here to push into the background.
    return "    %-16s %s" % (label, value)


def _help_lines(menu_items, position, args):
    """`h` for the keys, `h` then a digit for one entry, in its own words."""
    if args:
        return _entry_help(menu_items, position, args[0])
    return _general_help(menu_items)


def _general_help(menu_items):
    return (rule("Help", ch="="),
            C.bold("  keys"),
            "    <n>          run entry n",
            "    h<n>         what entry n is, and why it is or is not offered",
            "    p            progress, and why each greyed entry is greyed",
            "    i            version, licence, repository, funding",
            "    q            quit",
            "",
            C.dim("  Entries erasing footage ask for a typed word first: %s."
                  % _destructive_list(menu_items)),
            "")


def _entry_help(menu_items, position, arg):
    if not arg.isdigit() or int(arg) not in menu_items:
        return ("", C.yellow("  No entry %s. Press h then a number from the menu." % arg), "")
    return _about(menu_items, position, int(arg))


def _about(menu_items, position, number):
    """The one-liner, then what it leaves out, then the graph.

    The graph rows answer a question about the SHAPE of the tool, which is the
    thing you want once you already know what the step does. Read first they
    are three lines of notation in front of the sentence you came for, so they
    sit at the end and in dim: still there for whoever wants them, no longer
    the first thing the eye lands on.
    """
    item = menu_items[number]
    erases = "yes, and asks for a typed word" if item.destr() else "no"
    return ((rule("Help: %s" % item.name(), ch="="),
             "    " + item.description())
            + _about_paragraphs(item.about())
            + ("",)
            + _graph_row("leads to", _named_list(menu_items, item.outbound()))
            + _graph_row("reached from", _named_list(menu_items, item.inbound()))
            + _graph_row("erases", [erases])
            + ("",))


def _about_paragraphs(text):
    """Wrap ABOUT to the terminal, blank line between paragraphs.

    Clamped rather than wrapped to whatever the window happens to be: a full-
    width paragraph on a wide terminal is a line the eye loses its place in on
    the way back, and the rest of this screen is indented four spaces anyway.

    A single newline inside a paragraph is kept as a line break, and a line
    that begins with whitespace is emitted exactly as written. That is what
    lets an entry put a path on its own line: reflowed into the prose it would
    break across two lines and stop being a thing you can select and paste.
    """
    if not text:
        return ()
    room = max(40, min(92, term_width() - 8))
    out = []
    for para in text.split("\n\n"):
        out.append("")
        # A paragraph opening with a tab is a nested block -- the plugin's own
        # words, set in from the exporter's. One tab is stripped and paid back
        # as indentation, which leaves a SECOND tab inside it doing what a
        # first one does at the outer level: keeping a path off the wrap.
        nested = para.startswith("\t")
        pad, width = ("        ", room - 4) if nested else ("    ", room)
        for line in para.split("\n"):
            if nested and line.startswith("\t"):
                line = line[1:]
            if line[:1] in (" ", "\t"):
                out.append(pad + line.replace("\t", "    ").rstrip())
            else:
                out.extend(pad + w for w in textwrap.wrap(line, width))
    return tuple(out)


# Three shapes answer edges() with None, and they do not mean the same thing.
# Reading all three as the first one told the operator that Delete SIM Data,
# which erases a card, "is a view".
_NO_EDGES = {
    menu.Anywhere:  "anywhere (it is a view)",
    menu.StartNode: "nothing — this is where a cycle starts",
    menu.StepBack:  "back to whoever offered it",
}


# Eight neighbours on one line is a paragraph pretending to be a table: the eye
# has nothing to count by, and it forces the widest line on the screen into the
# part nobody came to read. Three is enough to still scan as a list.
_PER_ROW = 3
_LABEL_W = 13


def _named_list(menu_items, where):
    """The neighbours in printable chunks, or one phrase saying there are none."""
    numbers = where.edges()
    if numbers is None:
        return [_NO_EDGES.get(type(where), "nothing")]
    if not numbers:
        return ["nothing"]
    names = ["%d) %s" % (n, menu_items[n].name()) for n in sorted(numbers)]
    return [", ".join(names[i:i + _PER_ROW])
            + ("," if i + _PER_ROW < len(names) else "")
            for i in range(0, len(names), _PER_ROW)]


def _graph_row(label, chunks):
    """One labelled row, wrapped under its own label rather than under the
    left margin, so the continuation reads as more of the same answer."""
    lead = "    %-*s" % (_LABEL_W, label)
    return tuple(C.dim(lead + chunk if i == 0 else " " * len(lead) + chunk)
                 for i, chunk in enumerate(chunks))


def _next_steps(menu_items, verdicts, offered):
    """What can be done from here, each in one sentence.

    The menu is nine names and a number; it says what the steps ARE, not what
    they do. Asked for status, the useful answer is the shortlist of what is
    possible right now with a line of explanation each -- which is the thing a
    grid of names cannot carry and a wall of every step's description would
    bury.
    """
    # Nor a step whose work is already done: "next available" is what there is
    # to DO, and an entry that would report "nothing to do" is not on that list.
    ready = sorted(n for n in offered
                   if not _why_not(menu_items[n], verdicts[n], True)
                   and not menu.is_view(menu_items[n]))
    if not ready:
        return ()
    return ("", C.bold("  Next available steps:")) + tuple(
        "     %d - %s" % (n, menu_items[n].description()) for n in ready) + ("",)


def _where_lines(menu_items, position):
    """Where the pipeline is. On the status screen, not under every menu.

    Under the grid it was one more thing that looks the same every draw. Asked
    for, it is the answer to a question.
    """
    return ("  Last: %s" % _last_name(menu_items, position),)


def _last_name(menu_items, position):
    """The step just taken, by name and nothing else.

    It was "Position: 8) Clean Workspace", bold. The number is already beside
    that entry two lines down in the grid, bold is for something that wants
    the eye, and "position" described the machine rather than answering what
    the reader wants to know -- which is what he last did.
    """
    if position.current not in menu_items:
        return "nothing yet"
    return menu_items[position.current].name()


def _destructive_list(menu_items):
    hits = filter(lambda n: menu_items[n].destr(), sorted(menu_items))
    return ",".join(map(str, hits))


# ---------------------------------------------------------------------------
# The runner: one selection, one item, one world per dispatch.
# ---------------------------------------------------------------------------

def print_summary(ctx, close=True):
    """The session log. `close` draws the rule under it.

    Inside Progress it does not, because the menu draws its own rule
    immediately afterwards and two in a row read as an empty section between
    them. On the way out there is nothing after it, so it closes itself.
    """
    if not ctx.results:
        return
    print()
    print(rule("Summary", ch="="))
    print(C.dim("  %-9s  %-37s %18s   %s"
                % ("State", "Task", "CPU / Network Time", "Description")))
    _print_all(map(_summary_line, ctx.results))
    print(C.bold(_total_line(ctx.results)))
    _print_all((rule(ch="="),) if close else ())


# Where the time column ends: two spaces, the nine-wide status tag, two more,
# the thirty-seven-wide name, a space, and eighteen right-aligned digits.
TIME_COL = 2 + 9 + 2 + 37 + 1 + 18


def _total_line(results):
    """What the session cost, in the column it is the total of.

    Right-aligned as one phrase so the figure lands on the same edge as every
    row above it -- a total that does not line up with its column is a number
    the eye has to hunt for to add anything up.
    """
    return "%*s" % (TIME_COL, "Total Runtime  %s"
                    % _hms(sum(r.seconds or 0 for r in results)))


def _summary_line(r):
    return "  %s  %-37s %18s   %s" % (_status_tag(r.status), r.name,
                                      _hms(r.seconds), C.dim(r.detail))


def _hms(seconds):
    """hh:mm:ss, always, and only here.

    human_secs drops the hour when there is not one, which reads well in prose
    -- a trip is "9:00 long" -- and badly in a column, where the same width
    every row is what lets a long step be spotted by shape instead of read.
    """
    s = int(seconds or 0)
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


# "ran" is what the machine did; "processed" is what happened to the footage,
# which is the sentence the operator is reading the summary for.
_STATUS_TAGS = {RAN: lambda: C.green("processed"),
                SATISFIED: lambda: C.green("satisfied"),
                ABORTED: lambda: C.yellow("aborted  "),
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
        world = self._look()
        print_menu(self.ctx, self.menu, self.position, world)
        print()
        _HINTED[0] = True                      # no hint on the menu itself
        return self._dispatch(read_key("Select> "))

    def _look(self):
        """The capture behind every menu draw, and it is not always cheap.

        Every capture asks the plugin -- deliberately, because the exporter
        has no idea what is behind that interface and budgeting on a guess
        about someone else's code is what this seam exists to prevent. The
        plugin caches or does not, as it sees fit. But a step that performed
        work has just told it to forget, so the draw right after an import,
        an exclude or a clean-up is the one that pays full price for the
        answer -- and it did that in silence. Aborting an import and watching
        nothing happen for a minute reads as a hang, and the fix for that is
        to say what is going on, not to ask less often.
        """
        return looked_at(self.ctx, menu.Scope.LOCAL)

    def _dispatch(self, sel):
        """Empty is Enter on its own, which is not a choice and not an error.

        It used to reach `sel.split()[0]` and take the whole session down with
        an IndexError -- for pressing return at a prompt, which is the most
        ordinary thing anyone does to a menu.
        """
        if not sel:
            return True
        return self._chosen(sel)

    def _chosen(self, sel):
        if sel in ("q", "quit", "exit"):
            return False
        return self._not_quit(sel)

    def _not_quit(self, sel):
        if sel in ("p", "progress"):
            return self._progress()
        if sel.split()[0] in ("h", "help", "?"):
            return self._help(sel)
        if sel in ("i", "info"):
            return self._info()
        return self._select(sel)

    def _progress(self):
        """`p` is Progress, plus where we are and why each entry is greyed.

        The two answered halves of one question -- what is here, and what can
        be done about it -- and asking them separately meant reading two
        screens to get one answer. Progress is no longer a numbered entry
        because it is not a step: it changes nothing, and a key that shows you
        something is not the same kind of thing as a key that does something.
        """
        self.run_one(PROGRESS)
        world = looked_at(self.ctx, menu.Scope.LOCAL)
        verdicts = _verdicts(self.menu, world)
        offered = self.position.selectable(self.menu)
        _print_all(_next_steps(self.menu, verdicts, offered))
        _print_all(_blocked_lines(self.menu, verdicts, offered))
        _print_all(_where_lines(self.menu, self.position))
        return True

    def _help(self, sel):
        _print_all(_help_lines(self.menu, self.position, sel.split()[1:]))
        return True

    def _info(self):
        _print_all(_info_lines(self.ctx.plugin))
        return True

    def _select(self, sel):
        """One number. Batch selection went with the numbers it was keyed on:
        the second item's legality depends on the first one's outcome."""
        if not self._is_item(sel):
            print(C.red('  Unknown option "%s"' % sel))
            return True
        return self._offered(int(sel))

    def _is_item(self, sel):
        return sel.isdigit() and int(sel) in self.menu

    def _offered(self, number):
        if number not in self.position.selectable(self.menu):
            self._not_available(number)
            return True
        self.run_one(number)
        return True

    def _not_available(self, number, verdict=None):
        """Plainly, and in terms of what to do rather than of the machine.

        "does not follow 7) Upload Website" described the graph to someone who
        wanted to know whether they could press the key. The answer is that
        they cannot, and one line says it.

        It used to add "5) Build Website comes first" when exactly one entry
        led here, and stay silent when several did. So the same refusal came
        with an explanation or without one depending on the shape of the graph
        at that point, which reads as the tool being arbitrary. `p` lists what
        IS available, which is the same question answered once, in one place,
        the same way every time.

        The same sentence carries a guard's refusal, with its reason on the
        same line. "8) Clean Workspace is not available: nothing imported"
        answers the keypress; opening the screen, printing the heading and
        the description and then reporting that it did not complete is three
        acts of theatre around a no.
        """
        print(C.yellow("  %d) %s is not available%s."
                       % (number, self.menu[number].name(),
                          _colon(getattr(verdict, "reason", "")))))
        _print_all(_evidence_lines(verdict))
        _print_all(_orphan_file(self.ctx, getattr(verdict, "evidence", ()) or ()))
        self._offer_way_past(number, verdict)

    def _offer_way_past(self, number, verdict):
        """A refusal an item declares a way past, asked for by its word.

        Only under a refusal that NAMED what it is about: the operator has the
        paths on screen and the full list in a file, which is the whole basis
        on which he is allowed to answer this.
        """
        item = self.menu[number]
        if not (item.OVERRIDE_WORD and getattr(verdict, "evidence", ())):
            return
        print()
        if ask("  Type %s to drop anyway: " % item.OVERRIDE_WORD) \
                != item.OVERRIDE_WORD:
            print(C.dim("  Aborted by user pre-run."))
            return
        outcome = item.override(looked_at(self.ctx, item.SCOPE))
        _print_all(_stayed_lines(item, outcome) if outcome else ())

    def run_one(self, number):
        """One item, against a world captured for ITS scope, right now.

        Not the world the menu was drawn with: that one is a prompt old, and
        the card can be swapped while the prompt is on screen. The guard is
        asked against THAT world before the screen opens, so a refusal is a
        line rather than a step that starts and stops.
        """
        item = self.menu[number]
        world = looked_at(self.ctx, item.SCOPE)
        verdict = _safe_verdict(item, world)
        if verdict.blocked:
            self._not_available(number, verdict)
            return None
        print()
        # The heading, and then whatever the step itself says. The description
        # used to sit between them, restating in a sentence what the rule above
        # already names and what the menu showed a keypress ago — and at item 7
        # it was the same two facts the step's own first two lines give. It
        # earns its place where a step is CHOSEN, not after it has been.
        print(rule(item.name(), ch="="))
        hint_reset()
        started, already = time.time(), len(self.ctx.results)
        outcome = self._execute(item, world)
        _stamp_elapsed(self.ctx.results[already:], time.time() - started)
        _tell_the_plugin(self.ctx, item, outcome)
        _print_all(_nothing_to_do_lines(outcome))
        self.position.advance(item)
        _remember_position(self.ctx, item, self.position)
        _print_all(_stayed_lines(item, outcome))
        return outcome

    def _execute(self, item, world):
        try:
            return item.execute(world)
        except KeyboardInterrupt:
            # Ctrl-C inside a step's own loop -- the stills pass, a long walk,
            # anything not wrapped in run_stream. KeyboardInterrupt is a
            # BaseException, so `except Exception` never saw it: it went past
            # the runner entirely and ended the SESSION, when what the operator
            # stopped was one step.
            return self._interrupted(item, Aborted(mid_run=True))
        except Exception as exc:
            return self._after_exception(item, exc)

    def _after_exception(self, item, exc):
        """Which kind of not-completing this was. Both leave the position where
        it is; only the wording and the log differ."""
        if isinstance(exc, Aborted):
            return self._interrupted(item, exc)
        return self._crashed(item, exc)

    def _crashed(self, item, exc):
        """A runtime failure is one item's failure, not the session's.

        Anything can raise mid-run: a disk fills, the source is unplugged while
        a copy is in flight, a call is wired wrong. Letting it reach the top
        killed the menu and took the position with it, which is the worst
        moment to lose both -- the operator is left with no tool and no record
        of where the cycle had got to. So it lands here instead: the item did
        not complete, the position therefore does not move, and the next menu
        comes back offering the same choices.

        Not BaseException. Ctrl-C and a quit are the operator deciding, and
        they must still leave.
        """
        print()
        print(C.red("  %s failed: %s: %s"
                    % (item.name(), type(exc).__name__, exc)))
        _print_all(_crash_log_line(_log_crash(self.ctx, item)))
        self.ctx.results.append(StepResult(
            item.name(), FAILED, 0, "%s: %s" % (type(exc).__name__, exc)))
        # performed: a crash lands anywhere, including after the act.
        return item.aborted("failed: %s" % exc, performed=True)

    def _interrupted(self, item, exc):
        """An abort does NOT complete the item, so the position stays put —
        which is what "steps back by one" means for a move that never took
        effect.

        Pre-run or mid-run comes off the exception rather than being assumed.
        Typing q at the DELETE prompt reported "mid-run" about a step that had
        not started, which is the one thing the two words exist to tell apart.
        """
        # The bar was mid-draw when the key landed; without this its first
        # characters survive the shorter line printed over them, and the
        # summary reads "20  Aborted by user mid-run."
        _erase_line()
        note = "Aborted by user %s-run." % ("mid" if exc.mid_run else "pre")
        self.ctx.results.append(StepResult(item.name(), ABORTED, 0, note))
        # The same words to the item, because the outcome's note is what the
        # line under this one prints. Two spellings of one event gave the
        # summary "Aborted by user mid-run." and the screen "interrupted".
        return item.aborted(note, performed=exc.mid_run)


def _nothing_to_do_lines(outcome):
    """An item whose postcondition already held says so.

    It completes and the position moves on, which is right -- nothing is owed.
    But it prints nothing on the way, so the screen showed the heading, the
    description and then the menu again, and "already done" was indis-
    tinguishable from "did nothing and would not say why".
    """
    if outcome.performed or not outcome.completed:
        return []
    return [C.green("  Nothing to do: %s." % (outcome.note or "already done"))]


def _tell_the_plugin(ctx, item, outcome):
    """A step changed the plugin's INPUT, so what it was holding is stale.

    It cannot see an import land, a trip get dropped, or the working area get
    erased — none of that goes anywhere near it — and every one of them changes
    the workspace it is handed and therefore what its destination should be
    asked about.

    A step that performed work, is not a VIEW, and moves what the destination
    is asked about. The first two are derived; the third the item declares,
    because nothing here can see whether making a still or encoding an mp4
    changed a published trip -- and the answer is no, while writing a sidecar
    or dropping a trip changes the very list is_complete() is asked about.

    It defaults to true, so forgetting to think about it costs a refresh
    rather than correctness. Nine seconds of ssh is the price of the safe
    mistake; a stale YES is the price of the other one, and it is paid in
    footage.
    """
    plugin = getattr(ctx, "plugin", None)
    if _changed_the_input(plugin, item, outcome):
        _reset_quietly(plugin)


def _changed_the_input(plugin, item, outcome):
    if plugin is None:
        return False
    return _did_real_work(item, outcome)


def _did_real_work(item, outcome):
    if menu.is_view(item):
        return False
    if not getattr(item, "CHANGES_THE_QUESTION", True):
        return False
    return outcome.performed


def _reset_quietly(plugin):
    try:
        plugin.reset()
    except Exception as e:
        # Its own cache is its own problem. A step that just finished must not
        # be reported as failed because a notification about it went wrong.
        print(C.dim("  (%s.reset() raised: %s)" % (plugin.name, e)))


def _remember_position(ctx, item, position):
    """Written where it survives a clean-up, and only for real steps.

    Nor for a position that is not one: an item that did not complete leaves
    the position where it was, and where it was on a cold start with an empty
    workspace is NOWHERE. Writing that down turns "I could not tell" into a
    remembered fact -- see remembered_step.
    """
    if menu.is_view(item) or position.current == menu.NOWHERE:
        return
    remember_step(ctx, position.current)


def _stamp_elapsed(results, seconds):
    """How long the operator waited, from the menu's side of the call.

    Each step body used to time itself from its own first line, which left out
    everything the operator sat through but the body did not do -- above all
    the world capture, which at FULL scope now shells out over ssh and lists a
    bucket. The menu knows when it dispatched and when it got control back,
    and that is the number he is actually asking about.

    One dispatch is normally one result; a body that logs more than one gets
    the same elapsed on each, because they are one wait.
    """
    for result in results:
        result.seconds = seconds


def _stayed_lines(item, outcome):
    """One line when a run did not complete.

    It used to add "Still at Clean Workspace." -- the machine's half, that the
    position had not moved. The menu redraws underneath it a line later and
    `p` says where we are on request, so the clause was a third telling of
    something the screen shows anyway, on the line whose job is to say what
    happened.
    """
    if item.completed():
        return []
    return [C.dim("  %s" % _because(outcome))]


def _because(outcome):
    """The note, when it is already a sentence; a sentence about it when not.

    "Aborted by user pre-run." says it. Wrapping that in "Did not complete
    (...)" was the machine narrating a fact the sentence already carried, and
    with the note's own brackets it closed two parens in a row. A note that is
    not a sentence -- a crash carries "TypeError: ..." -- still gets one built
    around it, because that one is a fragment.
    """
    note = getattr(outcome, "note", "")
    if not note:
        return "Did not complete."
    if note.endswith("."):
        return note
    return "Did not complete: %s." % note


def _crash_log_line(path):
    if not path:
        return []
    return [C.dim("  Traceback written to %s" % tilde(path))]


def _log_crash(ctx, item):
    try:
        return _write_crash(ctx, item)
    except Exception:
        # Best effort and deliberately silent. The operator is already looking
        # at one error; a second one about the log file helps nobody.
        return None


def _write_crash(ctx, item):
    path = ctx.log_dir / "crashes.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write("%s  %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                    item.name(), traceback.format_exc()))
    return path


def build_runner(ctx, classes=None):
    """Wire the state machine for this ctx. Injectable for a test.

    The strategy is resolved once, here, and the ten items are constructed
    for it. `classes` lets a test drive the whole loop with mocks instead of
    the real ten.
    """
    strategy = menu.Strategy.of(ctx.plugin)
    menu_items = menu.build_menu(strategy, Work(ctx), classes)
    position = menu.position_for(menu_items)
    # FULL, so the plugin IS asked before the first menu is drawn. Where the
    # cycle has got to is not knowable from this machine alone once publishing
    # is somebody else's code: the local artefacts that used to answer it are
    # the local edition's, and a configured install never makes them. Without
    # asking, every restart landed back at the renders however much had been
    # published.
    #
    # And the exporter does not get to decide that asking is expensive. It has
    # no idea what is behind the interface -- an ssh session, a dict, a mock in
    # a test -- so budgeting on a guess about someone else's implementation is
    # the one thing this seam exists to stop. Startup is a defined moment an
    # implementor can plan for; what it costs there is theirs to manage.
    world = looked_at(ctx, menu.Scope.FULL)
    _resume(ctx, position, world)
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
                % tilde(ctx.lock_file)))
    print(C.dim("  itself; this one's owner is still running.)"))
    return 2


def _banner_lines(ctx):
    """The name, big, or the one-line version when the terminal is narrow.

    Which edition this is and what the chain does both live on the status
    screen a few lines below, so the header does not have to carry them too --
    it says what the program is called and gets out of the way.
    """
    if term_width() < BANNER_WIDTH + 2:
        return (C.bold("  Dashcam-Exporter") + C.dim("   " + _chain(ctx)),)
    return _big_banner()


def _big_banner():
    """The art, with the version sitting at the right end of it.

    On the last line of the glyphs rather than a line of its own: the art is
    already the header, and a version under it would be a second one.

    No blank above it. The launcher's own line sits there and is the break;
    one of ours as well made two. The blank below comes from the status block,
    which has always printed its own.
    """
    return tuple(map(C.bold, _with_version(BANNER.strip("\n").splitlines())))


DESIGNED_BY = "--- designed by Raoul Marc Schmidiger"
IMPLEMENTED_BY = "--- implemented by Claude"


def _credited(tail):
    """The two names, either side of the descender the art ends on.

    The last line of the letterforms is one glyph and a lot of empty space,
    which is the only room a header has for anything else -- and this is the
    one thing worth putting in it.
    """
    at = tail.index("|_|")
    return "%s|_|   %s" % (("      " + DESIGNED_BY).ljust(at), IMPLEMENTED_BY)


def _with_version(lines):
    at = len(lines) - 2                     # the last line of the letterforms
    tagged = list(lines)
    tagged[at] = "%s v%s" % (tagged[at].rstrip(), version())
    tagged[-1] = _credited(tagged[-1])
    return tagged


def _edition_line(ctx):
    """Which of the two editions this install is, named once, at the top.

    The menu says "not available for <edition>" about the entries the other one
    owns, and this is where that word is explained. It lives here rather than
    in the item that is switched off because how an installation is configured
    is not something a menu entry knows about — an entry answers for its own
    job, and the shape of the machine around it is the session's to state.
    """
    return C.dim("  %s edition — README.md has the step graph and what each"
                 " edition does." % menu.Strategy.of(ctx.plugin).value)


def _chain(ctx):
    """What THIS installation actually does, which is not the same on every
    machine: with nothing configured the chain really does stop at a local
    page, and naming a destination there would be a promise the greyed-out
    menu below then breaks.

    The configured half is named by whoever publishes, because the exporter no
    longer knows where anything goes."""
    if ctx.plugin is None:
        return "card -> render -> local page"
    return "card -> render -> %s" % ctx.plugin.name


def _exit_code(ctx):
    if any(map(_failed, ctx.results)):
        return 1
    return 0


def _failed(result):
    return result.status == FAILED


def _resume(ctx, position, world):
    """Pick up where the operator left off, and only guess when he never was.

    Or when what he left is no longer true. A remembered position is a fact
    about what completed, and the world can move past it without anything
    completing at all.
    """
    at = remembered_step(ctx)
    if at is None or _undone_since(at, world):
        position.orient(world, items.COLD_START_RULES)
        return
    position.current = at


def _undone_since(at, world):
    """Has the world undone the step the position remembers.

    One case is provable and this is it. 8 completing means the working area
    was emptied, so footage sitting in it now arrived AFTER — an import that
    was interrupted or declined does not complete, so the position stays on 8
    while the disk fills up behind it. From 8 the menu offers 1 and 8, and the
    operator with 118 clips imported cannot reach 2) Generate Meta.

    Orientation is right for exactly this. It reads the disk, and the disk
    cannot be older than the position; what made it wrong as a general rule
    was overriding a position the operator had actually reached, which is not
    what a contradicted one is.
    """
    return at == CLEAN_WS and bool(world.imports)


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
        print("Bye!")


def main(argv=None):
    """No command line. Everything this needs is in config.txt.

    A flag with a config equivalent means the same question has two answers
    that can disagree — and a default compiled in here gets inherited by a
    fresh checkout that never set it, with the erase following the compiled-in
    path into somebody else's data. One source, and it is the file the person
    edits.
    """
    _no_colour()
    try:
        ctx = Ctx()
    except uploader.UploaderNotLoaded as e:
        return _uploader_broken(e)
    # One instance per working area. A second menu against the same tree
    # trusts scans the first one may be invalidating right now, and the erases
    # trust the scans. The lock self-clears when its pid is gone, so a crash
    # cannot strand it.
    if not acquire_single_instance_lock(ctx):
        return _lock_taken(ctx)
    return _start(ctx)


def _uploader_broken(error):
    """A configured uploader that will not load stops the tool before the menu.

    Not a fallback to the local edition. That fallback is silent by nature: the
    menu would look normal, item 5 would write a local page, and item 8 would
    go on refusing for a reason that reads like a network problem — while
    nothing was being published at all.
    """
    print()
    print(C.red("  upload_plugin is configured and will not load:"))
    print(C.red("    %s" % error))
    print(C.dim("  Fix it, or remove upload_plugin to run the local edition"
                " on purpose."))
    return 4


def _start(ctx):
    _print_all(_banner_lines(ctx))
    # Checked before the status screen: there is nothing useful to show if the
    # numbers behind it would come from the wrong grouping.
    if not require_ego_motion(ctx):
        return 3
    print_configuration(ctx)
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
# * Progress can only be derived where the tool emits it: rsync's --info=progress2
#   percentage (import) and the renderer's [Trip a/b] + [clip/N] lines (render).
#   A tool that prints nothing countable shows a spinner instead. Many draw with
#   carriage returns, which is why the reader splits on \r as well as \n — and
#   why Ui.run takes a parser: an uploader's output format is its own to parse,
#   not a table this repo would have to maintain.
#
# * The renderer's "[Trip a/b]" a is the per-DAY publish number, so it repeats
#   across days within one run. Only b is usable; the counter is our own.
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
# ---------------------------------------------------------------------------
