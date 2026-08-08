"""The DOS-style framed backend: a fixed title/status band on top, the numbered
menu as a bar on the bottom, and the work in the middle -- a scrolling log with
the live progress bar pinned beneath it.

The workflow does not know it is here. It calls the same UiHandler methods the
stream backend answers; this one paints them into regions of a full-screen frame
instead of letting them scroll. Three things make that safe:

- The layout is a PURE function of the terminal size (`Layout`), so where every
  band lands is decided by arithmetic that can be tested without a terminal.
- The frame OWNS stdout while it is open: `open()` swaps in a tee that funnels
  any stray `print` -- a step body not yet on the seam, a traceback -- into the
  log region, so the frame can never be corrupted by output that went around it.
- `close()` always leaves the alternate screen and restores the cursor, wired to
  a finally in the runner, so a crash never leaves a wedged terminal.

Manual ANSI, no curses: the tool already speaks raw ANSI, and keeping both
backends "format and write" is what lets the stream backend stay byte-identical.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import sys
import threading
import time

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s):
    """The text without colour, for measuring how wide a coloured line really is."""
    return _ANSI.sub("", str(s))

from dashcam_exporter.application.ui.term import C, term_width
from dashcam_exporter.application.ui import screens
from dashcam_exporter.application.ui import prompt as prompt_mod
from dashcam_exporter.application.ui.handler import UiHandler

CSI = "\x1b["
ALT_ON = CSI + "?1049h"
ALT_OFF = CSI + "?1049l"
HIDE = CSI + "?25l"
SHOW = CSI + "?25h"
CLEAR = CSI + "2J"


def _at(row, col, text=""):
    return "%s%d;%dH%s" % (CSI, row, col, text)


def _clear_row(row, cols):
    return _at(row, 1) + (" " * cols)


class Layout:
    """Where each band sits, in 1-based ANSI rows, for a given terminal size.

    Bands, top to bottom: title, status, a rule, the log, a dotted divider, the
    pinned bar, a rule, the menu (fixed rows), the select line. Pure: two calls
    with the same size give the same rows, which is the whole of what the frame
    needs to be tested without a real screen.
    """

    # The menu region holds the original grid (screens._Grid) plus a hint line.
    # Its height is not fixed -- the grid's row count depends on the width -- so
    # the frame passes it in and recomputes when it changes. DEFAULT_MENU_ROWS is
    # the initial guess before the first real menu is drawn.
    DEFAULT_MENU_ROWS = 4
    MIN_ROWS = 14
    MIN_COLS = 48

    def __init__(self, rows, cols, menu_rows=DEFAULT_MENU_ROWS):
        self.rows = max(rows, self.MIN_ROWS)
        self.cols = max(cols, self.MIN_COLS)
        self.menu_rows_count = max(2, menu_rows)
        self.title_row = 1
        self.status_row = 2
        self.top_rule_row = 3
        self.select_row = self.rows
        self.menu_bottom = self.rows - 1
        self.menu_top = self.rows - self.menu_rows_count
        self.bottom_rule_row = self.menu_top - 1
        self.bar_row = self.bottom_rule_row - 1
        self.divider_row = self.bar_row - 1
        self.log_top = self.top_rule_row + 1
        self.log_bottom = self.divider_row - 1

    @property
    def log_height(self):
        return max(1, self.log_bottom - self.log_top + 1)

    @property
    def menu_rows(self):
        return list(range(self.menu_top, self.menu_bottom + 1))


def _fit(text, cols):
    """Keep a line's colour when it fits; when it does not, clip on the plain
    text (colour is dropped on the overflow case, which is rare and harmless)."""
    if len(_plain(text)) <= cols:
        return text
    return _plain(text)[:cols - 1] + "…"


class FrameLive:
    """The `new_live()` object run_stream draws into. Same surface the stream
    Live exposes -- draw(lines)/close()/height/enabled -- but a draw paints the
    pinned bar instead of scrolling, so the live signal never floods the log."""

    def __init__(self, frame):
        self._frame = frame
        self.enabled = True
        self.height = 1

    def draw(self, lines):
        self._frame.set_bar(lines[-1] if lines else "")

    def emit(self, line):
        self._frame.log(line)

    def close(self):
        pass


class FrameWaiting:
    """The framed answer to `waiting(...)`: an indeterminate spinner painted into
    the pinned bar. The stream version writes carriage-return redraws to stdout,
    which the frame's tee cannot render (it flushes on newline) -- so a blocking
    call like the plugin query looked frozen. Here a daemon thread animates the
    bar, and a note from the plugin (`update`) rides along beside the label."""

    def __init__(self, frame, label):
        self._frame = frame
        self._label = label
        self._note = ""
        self._stop = threading.Event()
        self._thread = None

    def update(self, note):
        self._note = str(note or "")

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._frame.set_bar("")
        return False

    def _run(self):
        width, i = 14, 0
        while not self._stop.wait(0.12):
            pos = i % (2 * (width - 3))
            pos = pos if pos < width - 2 else 2 * (width - 3) - pos
            track = "." * pos + "###" + "." * (width - 3 - pos)
            note = ("  " + self._note) if self._note else ""
            self._frame.set_bar("%s [%s]%s" % (self._label, track, note))
            i += 1


def _fixed_size():
    """(rows, cols) from FRAME_ROWS/FRAME_COLS, or None to follow the terminal."""
    try:
        rows = int(os.environ.get("FRAME_ROWS", ""))
        cols = int(os.environ.get("FRAME_COLS", ""))
    except ValueError:
        return None
    if rows > 0 and cols > 0:
        return rows, cols
    return None


class FramedUiHandler(UiHandler):
    def __init__(self, title="dashcam-exporter", subtitle="", splash_seconds=1.0):
        self._title = title
        self._subtitle = subtitle
        self._status = ""
        self._bar = ""
        self._menu_lines = []
        self._menu_rows = Layout.DEFAULT_MENU_ROWS
        self._splash_seconds = splash_seconds
        self._splash_art = ()
        self._log = collections.deque()
        self._real_stdout = None
        self._open = False
        rows, cols = self._size()
        self.layout = Layout(rows, cols, self._menu_rows)

    # -- lifecycle --------------------------------------------------------
    def _size(self):
        """The frame size. FRAME_ROWS/FRAME_COLS pin it to a fixed geometry (the
        DOS-UI launcher sets them and resizes the window to match), so a
        double-clicked run looks the same every time; otherwise it follows the
        terminal."""
        fixed = _fixed_size()
        if fixed:
            return fixed
        sz = shutil.get_terminal_size(fallback=(term_width(), 24))
        return sz.lines, sz.columns

    def open(self):
        rows, cols = self._size()
        self.layout = Layout(rows, cols, self._menu_rows)
        self._real_stdout = sys.stdout
        sys.stdout = _LogTee(self, self._real_stdout)
        self._open = True
        self._write(ALT_ON + HIDE + CLEAR)
        self._splash()          # a centered card of the same info, ~1s
        self.repaint()          # then the frame, with that info on top

    def set_splash(self, lines):
        self._splash_art = tuple(lines or ())
        return True

    def _splash(self):
        """Held for a beat when a window is launched. Shows the launch art (the
        ASCII banner) centered when it has been given some; otherwise a small
        card of the identity."""
        if not self._splash_seconds:
            return
        if self._splash_art:
            self._paint_splash_art()
        else:
            self._paint_splash_card()
        if self._real_stdout is not None and self._real_stdout.isatty():
            time.sleep(self._splash_seconds)
        self._write(CLEAR)

    def _paint_splash_art(self):
        L = self.layout
        art = [ln for ln in self._splash_art if _plain(ln).strip()]
        top = max(1, (L.rows - len(art)) // 2)
        buf = ""
        for i, ln in enumerate(art):
            left = max(1, (L.cols - len(_plain(ln))) // 2 + 1)
            buf += _at(top + i, left, ln)
        self._write(buf)

    def _paint_splash_card(self):
        L = self.layout
        lines = [C.bold(self._title)]
        if self._subtitle:
            lines.append(self._subtitle)
        if self._status:
            lines.append(C.dim(self._status))
        inner = min(max(len(_plain(x)) for x in lines) + 8, L.cols - 4)
        top = max(1, (L.rows - (len(lines) + 2)) // 2)
        left = max(1, (L.cols - inner - 2) // 2)
        buf = _at(top, left, "+" + "-" * inner + "+")
        for i, ln in enumerate(lines):
            pad = inner - len(_plain(ln))
            lp = pad // 2
            buf += _at(top + 1 + i, left,
                       "|" + (" " * lp) + ln + (" " * (pad - lp)) + "|")
        buf += _at(top + 1 + len(lines), left, "+" + "-" * inner + "+")
        self._write(buf)

    def close(self):
        if not self._open:
            return
        self._open = False
        if self._real_stdout is not None:
            sys.stdout = self._real_stdout
        self._write(SHOW + ALT_OFF)
        self._real_stdout = None

    # -- the seam ---------------------------------------------------------
    def title(self, app, subtitle=""):
        self._title, self._subtitle = app, subtitle
        self._paint_chrome()

    def status(self, facts):
        self._status = facts or ""
        self._paint_chrome()

    def menu(self, ctx, menu_items, position, world):
        lines = _menu_lines_original(menu_items, position, world, self.layout.cols)
        self._menu_lines = lines
        if len(lines) != self._menu_rows:
            # The grid's height changed (a resize); move the region and repaint
            # the whole frame so the bands above it follow.
            self._menu_rows = len(lines)
            rows, cols = self._size()
            self.layout = Layout(rows, cols, self._menu_rows)
            self.repaint()
        else:
            self._paint_menu()

    def summary(self, ctx, close=True):
        # Its prints land in the log through the tee, so the exit summary reads
        # in the same region everything else did.
        screens.print_summary(ctx, close)

    def block(self, lines):
        for line in lines:
            self.log(line.rstrip())

    def log(self, text=""):
        for piece in str(text).split("\n"):
            self._log.append(piece)
        keep = self.layout.log_height
        while len(self._log) > keep:
            self._log.popleft()
        self._paint_log()

    def new_live(self):
        return FrameLive(self)

    def waiting(self, label):
        return FrameWaiting(self, label)

    def set_bar(self, text):
        self._bar = text
        self._paint_bar()

    def done(self, what):
        self.log(C.green("  100%% - %s." % what))

    # -- input: hand the select line to prompt, on the real screen --------
    def read_key(self, prompt):
        return self._on_select(lambda: prompt_mod.read_key(prompt))

    def ask(self, prompt, default="", quits=True):
        return self._on_select(lambda: prompt_mod.ask(prompt, default, quits))

    def confirm(self, prompt, default=False):
        return self._on_select(lambda: prompt_mod.confirm(prompt, default))

    def _on_select(self, read):
        """Run a prompt at the select row on the real terminal. prompt writes and
        reads there directly; the tee is bypassed so its echo is not swallowed
        into the log. Afterwards the select row is wiped and the cursor hidden."""
        if not self._open or self._real_stdout is None:
            return read()
        saved = sys.stdout
        sys.stdout = self._real_stdout
        try:
            self._write(_at(self.layout.select_row, 1)
                        + (" " * self.layout.cols) + _at(self.layout.select_row, 1) + SHOW)
            return read()
        finally:
            sys.stdout = saved
            self._write(HIDE + _clear_row(self.layout.select_row, self.layout.cols))

    # -- painting ---------------------------------------------------------
    def _write(self, s):
        out = self._real_stdout if self._real_stdout is not None else sys.__stdout__
        out.write(s)
        out.flush()

    def repaint(self):
        self._paint_chrome()
        self._paint_log()
        self._paint_divider()
        self._paint_bar()
        self._paint_menu()

    def _paint_chrome(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        head = " %s " % self._title
        if self._subtitle:
            tail = "%s " % self._subtitle
            head = head + "-" * max(1, cols - len(head) - len(tail)) + tail
        buf = _clear_row(L.title_row, cols) + _at(L.title_row, 1, C.bold(_fit(head, cols)))
        buf += _clear_row(L.status_row, cols) + _at(L.status_row, 1, C.dim(_fit(" " + self._status, cols)))
        buf += _at(L.top_rule_row, 1, C.dim("-" * cols))
        self._write(buf)

    def _paint_log(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        lines = list(self._log)[-L.log_height:]
        buf = ""
        for i, row in enumerate(range(L.log_top, L.log_bottom + 1)):
            text = lines[i] if i < len(lines) else ""
            buf += _clear_row(row, cols) + _at(row, 1, _fit(text, cols))
        self._write(buf)

    def _paint_divider(self):
        if not self._open:
            return
        L = self.layout
        self._write(_at(L.divider_row, 1, C.dim("." * L.cols)))

    def _paint_bar(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        self._write(_clear_row(L.bar_row, cols) + _at(L.bar_row, 1, _fit(self._bar, cols)))

    def _paint_menu(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        buf = _at(L.bottom_rule_row, 1, C.dim("-" * cols))
        for i, row in enumerate(L.menu_rows):
            text = self._menu_lines[i] if i < len(self._menu_lines) else ""
            buf += _clear_row(row, cols) + _at(row, 1, _fit(text, cols))
        buf += _clear_row(L.select_row, cols)
        self._write(buf)


class _LogTee:
    """Stdout while the frame is open: whole lines go to the log region, so a
    stray print cannot scribble over the frame."""

    def __init__(self, frame, real):
        self._frame = frame
        self._real = real
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._frame.log(line)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return self._real.isatty()


def _menu_lines_original(menu_items, position, world, cols):
    """The exact grid the scrolling UI draws -- screens._Grid, in the same
    columns, coloured and greyed and red the same way -- followed by the p/h/i/q
    hint line. This is what "same as the original menu" means: one renderer, so
    the frame and the scroll cannot drift."""
    verdicts = screens._verdicts(menu_items, world)
    offered = position.selectable(menu_items)
    grid = screens._Grid(screens._in_the_grid(menu_items), verdicts, offered, cols)
    hint = C.dim("  p) progress   h) help   i) info   q) quit")
    return grid.lines() + [hint]
