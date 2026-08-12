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
import textwrap
import threading
import time
try:
    import termios
except ImportError:          # non-unix; the frame is macOS/Linux only anyway
    termios = None

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CSI = re.compile(r"\x1b\[[0-9;?]*([A-Za-z])")


def _plain(s):
    """The text without colour, for measuring how wide a coloured line really is."""
    return _ANSI.sub("", str(s))


def _clean_log(s):
    """A log line reduced to text and colour. Cursor moves and clears (a step's
    in-place \\x1b[K, a stray \\r) would otherwise scribble across the row and eat
    the border; only SGR colour is kept, and a \\r is an in-place update whose
    last segment wins."""
    s = str(s)
    if "\r" in s:
        s = s.rsplit("\r", 1)[-1]
    return _CSI.sub(lambda m: m.group(0) if m.group(1) == "m" else "", s)

from dashcam_exporter.application.ui.term import C, term_width, human_secs
from dashcam_exporter.application.ui import screens
from dashcam_exporter.application.ui import prompt as prompt_mod
from dashcam_exporter.application.ui.handler import UiHandler
from dashcam_exporter.application.ui.progress import ProgressState, render_progress, _clip

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

    A boxed frame with vertical borders down both sides. Top to bottom:

        title box   =====   |pad|   | title      version |   |pad|   =====
        log         | content ... |   (bordered, scrolls)
        progress    -----   |pad|   | bar |   |pad|
        separator   =====
        menu        | grid row |...   | hint |
        menu rule   -----
        select      Select>          (borderless, the very last row)

    Pure: two calls with the same size give the same rows, which is the whole of
    what the frame needs to be tested without a real screen. `menu_rows` is the
    number of grid rows the menu drew (the hint is its own row); the frame passes
    it in and recomputes when the grid's height changes with the width.
    """

    DEFAULT_MENU_ROWS = 3
    MIN_ROWS = 22
    MIN_COLS = 60

    def __init__(self, rows, cols, menu_rows=DEFAULT_MENU_ROWS, show_progress=True):
        self.rows = max(rows, self.MIN_ROWS)
        self.cols = max(cols, self.MIN_COLS)
        self.grid_rows = max(1, menu_rows)
        self.show_progress = show_progress

        # Title box, five rows at the top (top corner, pad, title, pad, sep).
        self.title_top_rule = 1
        self.title_pad_top = 2
        self.title_row = 3
        self.title_pad_bot = 4
        self.title_sep = 5

        # Bottom stack, counted up from the closing border. The box is closed:
        # Select sits inside it, with the bottom corner below.
        self.bottom_rule = self.rows
        self.select_row = self.rows - 1
        self.select_rule = self.rows - 2
        self.hint_row = self.rows - 3
        self.menu_bottom = self.rows - 4
        self.menu_top = self.menu_bottom - self.grid_rows + 1

        # The double rule between the log and the menu is always here. The
        # progress box lives just above it and ONLY when a bar is running: one
        # row for the bar, set off by a single rule, with no blank padding. When
        # idle it is gone entirely and the log reclaims those rows -- an empty
        # box was eating the mid-screen and cutting the log off short.
        self.progress_sep = self.menu_top - 1
        self.log_top = self.title_sep + 1
        if show_progress:
            self.bar_row = self.progress_sep - 1
            self.progress_top_rule = self.bar_row - 1
            self.log_bottom = self.progress_top_rule - 1
        else:
            self.bar_row = None
            self.progress_top_rule = None
            self.log_bottom = self.progress_sep - 1

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


def _pad(text, width):
    """Fit a possibly-coloured line to exactly `width` VISIBLE columns -- the one
    place the box cell fits its content. On overflow, clip by an ANSI-aware pass
    that keeps the colour (a plain slice dropped every escape, which is how the
    bar lost its colour once the box trimmed it to cols-2), and reset at the cut
    so the trimmed colour cannot bleed into the border that follows."""
    n = len(_plain(text))
    if n < width:
        return text + " " * (width - n)
    if n > width:
        return _clip(text, width) + CSI + "0m"
    return text


# Box-drawing, so the frame's lines are continuous. Double for the outer box and
# the major separators, single for the internal rules; junctions where a rule
# meets the double side border.
TL, TR, BL, BR = "╔", "╗", "╚", "╝"
DH, DV = "═", "║"
LTD, RTD = "╠", "╣"     # double rule meeting the side
SH = "─"
LTS, RTS = "╟", "╢"     # single rule meeting the side
UP, DOWN = "↑", "↓"     # page-indicator arrows (lines hidden above / below)


def _top(cols):
    return C.dim(TL + DH * (cols - 2) + TR)


def _bottom(cols):
    return C.dim(BL + DH * (cols - 2) + BR)


def _rule_double(cols):
    return C.dim(LTD + DH * (cols - 2) + RTD)


def _rule_single(cols):
    return C.dim(LTS + SH * (cols - 2) + RTS)


def _box(content, cols):
    """A content row between the frame's double side borders."""
    return C.dim(DV) + _pad(content, cols - 2) + C.dim(DV)


SELECT_LABEL = "  Select> "
SELECT_COL = 2 + len(SELECT_LABEL)      # cursor sits just after the label


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
        self._frame.set_bar("")     # clear the pinned bar when the step ends


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
        started, i = time.time(), 0
        while not self._stop.wait(0.12):
            state = ProgressState(action=self._label, infinite=True,
                                  time=human_secs(time.time() - started),
                                  subaction=self._note or None, bounce=i)
            self._frame.set_bar(render_progress(state, term_width()))
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
    LOG_HISTORY = 2000     # lines kept for paging back with j/k

    def __init__(self, title="dashcam-exporter", subtitle="", splash_seconds=2.0):
        self._title = title
        self._subtitle = subtitle
        self._status = ""
        self._bar = ""
        self._menu_lines = []
        self._menu_rows = Layout.DEFAULT_MENU_ROWS
        self._splash_seconds = splash_seconds
        self._splash_art = ()
        self._lock = threading.Lock()   # the bar spinner draws from a thread
        self._log = collections.deque(maxlen=self.LOG_HISTORY)
        self._log_view = 0             # index of the top visible line (page-anchored)
        self._pinned_top = False       # a read-me screen holds the view at its top
        self._summary = collections.OrderedDict()   # live counters for a task
        self._real_stdout = None
        self._open = False
        self._show_progress = False   # the pbar box appears only while a bar runs
        rows, cols = self._size()
        self.layout = Layout(rows, cols, self._menu_rows, self._show_progress)

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

    # -- terminal mode. The frame owns the tty for its lifetime: echo OFF, so a
    #    key pressed WHILE A STEP RUNS is not cooked-echoed by the terminal into
    #    the middle of the frame (and buffered) -- which is invisible in the log
    #    because it is the terminal's own echo of stdin, not anything we wrote.
    #    Echo is turned back on only for a typed-word prompt, where the operator
    #    must see what they type. --
    def _tty_fd(self):
        if termios is None:
            return None
        try:
            fd = sys.stdin.fileno()
        except (ValueError, OSError, AttributeError):
            return None
        return fd if os.isatty(fd) else None

    def _set_echo(self, on):
        fd = self._tty_fd()
        if fd is None:
            return
        try:
            attrs = termios.tcgetattr(fd)
            if on:
                attrs[3] |= termios.ECHO
            else:
                attrs[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error:
            pass

    def _flush_input(self):
        """Drop keys typed during the step just finished, so impatient presses
        do not fire menu items nobody chose."""
        fd = self._tty_fd()
        if fd is None:
            return
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def open(self):
        rows, cols = self._size()
        self.layout = Layout(rows, cols, self._menu_rows, self._show_progress)
        self._real_stdout = sys.stdout
        sys.stdout = _LogTee(self, self._real_stdout)
        self._open = True
        fd = self._tty_fd()
        self._saved_tty = termios.tcgetattr(fd) if fd is not None else None
        self._set_echo(False)
        self._write(ALT_ON + HIDE + CLEAR)
        self._splash()          # a centered card of the same info, ~1s
        self.repaint()          # then the frame, with that info on top

    def set_splash(self, lines):
        self._splash_art = tuple(lines or ())
        return True

    def prime_menu(self, ctx, menu_items, position):
        self.menu(ctx, menu_items, position, None)   # items now, greying later

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
        # Block-centered: ONE offset from the widest line, applied to every line,
        # so the art keeps its own internal alignment. Centering each line by its
        # own width skews it -- a shorter glyph row slides out of column.
        L = self.layout
        art = list(self._splash_art)
        width = max((len(_plain(ln)) for ln in art), default=1)
        left = max(1, (L.cols - width) // 2 + 1)
        top = max(1, (L.rows - len(art)) // 2)
        buf = ""
        for i, ln in enumerate(art):
            if 1 <= top + i <= L.rows:
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
        fd = self._tty_fd()
        if fd is not None and getattr(self, "_saved_tty", None) is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._saved_tty)
            except termios.error:
                pass
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
        # The grid is drawn inside the borders, so it gets the inner width.
        lines = _grid_lines(menu_items, position, world, self.layout.cols - 2)
        self._menu_lines = lines
        if len(lines) != self._menu_rows:
            # The grid's height changed (a resize); move the region and repaint
            # the whole frame so the bands above it follow.
            self._menu_rows = len(lines)
            rows, cols = self._size()
            self.layout = Layout(rows, cols, self._menu_rows, self._show_progress)
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
            self._log.append(_clean_log(piece))
        self._follow()          # new output shows on the newest page
        self._paint_log()

    def scroll_top(self):
        self._pinned_top = True   # hold here despite the menu loop's trailing blank
        self._log_view = 0
        self._paint_log()
        self._paint_menu()      # refresh the page indicator (now UP 0)

    def clear_log(self):
        self._log.clear()
        self._log_view = 0
        self._pinned_top = False   # a new action follows its own output again
        self._summary.clear()   # each action starts its own tally
        self._paint_log()
        self._paint_menu()      # the j/k page hint clears with the log

    def stat(self, name, value):
        """A named running total shown in the main window and updated in place --
        "Clips reviewed: 45", "Stills created: 90" -- so the mid says what the
        task is producing while the bar says where it is. Pinned to the foot of
        the log so it stays put as any output scrolls above it."""
        self._summary[name] = value
        self._paint_log()

    def _summary_lines(self):
        if not self._summary:
            return []
        w = max(len(k) for k in self._summary)
        return [C.bold("  %-*s %s" % (w + 1, k + ":", v))
                for k, v in self._summary.items()]

    def paragraph(self, text):
        """A finished paragraph, wrapped to the row, PAGED not scrolled: when it
        will not fit in what remains of the page on screen, clear to a fresh one
        and start it at the top. So a transcript is read a screenful at a time."""
        if not self._open:
            print(text)
            return
        width = max(10, self.layout.cols - 4)   # -4: border, a leading and a trailing space
        wrapped = []
        for line in str(text).split("\n"):
            wrapped.extend(textwrap.wrap(line, width) or [""])
        wrapped.append("")                      # a blank row between paragraphs
        body = self._body_height()
        used = len(self._log) - self._last_page_start()   # rows on the current page
        if self._log and used + len(wrapped) > body:
            # A paragraph never straddles a page. Fill the rest of this page so
            # the next starts on a page boundary (keeps j/k pages aligned), then
            # a row of air at its top. Nothing is cleared -- the pages remain in
            # history to walk back through once transcription is done.
            while len(self._log) % body != 0:
                self._log.append("")
            self._log.append("")
        elif not self._log:
            self._log.append("")                # a row of air on the first page
        for ln in wrapped:
            self._log.append(_clean_log(ln))
        self._follow()
        self._paint_log()

    def _body_height(self):
        """Rows the paged log gets -- the summary is pinned below it."""
        return max(1, self.layout.log_height - len(self._summary))

    def _last_page_start(self):
        """Top-line index of the page holding the newest line."""
        body = self._body_height()
        return (max(0, len(self._log) - 1) // body) * body

    def _follow(self):
        """Jump the view to the newest page (while output is being written) --
        unless a read-me screen has pinned the top. The menu loop prints a blank
        line after each draw, and without the pin that lone `log("")` would snap
        a just-shown licence straight back down to its last, empty page."""
        if self._pinned_top:
            return
        self._log_view = self._last_page_start()

    def _clamp_view(self):
        self._log_view = max(0, min(self._log_view, self._last_page_start()))

    def page(self, direction):
        """Turn to the previous/next PAGE of the log -- j back, k forward. Pages
        are kept (nothing is cleared), so this walks the whole history once a
        step is done. Consumes j/k even at the ends so they never fall through
        to the menu as a typo."""
        if not self._open:
            return False
        self._pinned_top = False                  # the operator is steering now
        body = self._body_height()
        if self._last_page_start() > 0:
            if direction in ("j", "left"):        # older, back a page
                self._log_view = max(0, self._log_view - body)
            else:                                 # 'k' / right: newer, forward
                self._log_view = min(self._last_page_start(), self._log_view + body)
            self._paint_log()
            self._paint_menu()                    # refresh the page indicator
        return True

    def new_live(self):
        return FrameLive(self)

    def waiting(self, label):
        return FrameWaiting(self, label)

    def set_bar(self, text):
        # Clip to leave THREE columns of air on BOTH sides, matching the visual
        # left inset (the border, the box's leading space, and render_progress's
        # 2-space action indent read as ~3 spaces before "Deploy"). A long
        # subaction (a deploy's config.js VIDEO_BASE url, a meta pass's
        # "map: ... -> a.gpx") otherwise runs to one space off the right border
        # and reads as if it overflows. The cell is cols-2 wide and _box prepends
        # one leading space, so clipping the bar to cols-6 leaves 3 trailing
        # spaces once _pad fills the cell.
        self._bar = _clip(text, max(1, self.layout.cols - 6)) if text else ""
        want = bool(text)
        if want != self._show_progress:
            # The box opens when a bar arrives and closes when it goes; the log
            # region grows or shrinks with it, so the whole frame is repainted.
            self._show_progress = want
            rows, cols = self._size()
            self.layout = Layout(rows, cols, self._menu_rows, self._show_progress)
            self.repaint()
        else:
            self._paint_bar()

    def done(self, what):
        self.log(C.green("  100%% - %s." % what))

    # -- input: read inside the bordered Select row, on the real screen ---
    def read_key(self, prompt):
        # The frame already shows "Select> " (painted from the start); read the
        # key right after it, with an empty prompt so the label is not redrawn.
        return self._read(lambda: prompt_mod.read_key(""), SELECT_COL, keep_label=True)

    def ask(self, prompt, default="", quits=True):
        return self._read(lambda: prompt_mod.ask(prompt, default, quits), 3,
                          keep_label=False)

    def confirm(self, prompt, default=False):
        return self._read(lambda: prompt_mod.confirm(prompt, default), 3,
                          keep_label=False)

    def _read(self, read, col, keep_label):
        """Read inside the Select row on the real terminal (the tee is bypassed so
        the echo is not swallowed into the log). A menu keypress reads after the
        standing "Select> " label; a prompt (Type DELETE...) clears the row and
        draws itself instead. Afterwards the label is restored, cursor hidden."""
        if not self._open or self._real_stdout is None:
            return read()
        L = self.layout
        # Drop anything typed during the step just finished; it was not a menu
        # choice and must not become one now.
        self._flush_input()
        saved = sys.stdout
        sys.stdout = self._real_stdout
        try:
            if not keep_label:
                # The go-back hint is a separate line + newline; drawn at the
                # cursor it pushes the prompt off the bottom border. Suppress it
                # (the frame keeps the prompt inside its own Select row instead).
                prompt_mod._HINTED[0] = True
                # A typed word must be visible, so echo goes back on just here.
                self._set_echo(True)
                self._write(_at(L.select_row, 1, _box("", L.cols)))
            self._write(_at(L.select_row, col) + SHOW)
            return read()
        finally:
            if not keep_label:
                self._set_echo(False)
            sys.stdout = saved
            self._write(HIDE + _at(L.select_row, 1, _box(SELECT_LABEL, L.cols)))

    # -- painting ---------------------------------------------------------
    def _write(self, s):
        out = self._real_stdout if self._real_stdout is not None else sys.__stdout__
        with self._lock:      # serialise the main thread and the spinner thread
            out.write(s)
            out.flush()

    def repaint(self):
        self._paint_chrome()
        self._paint_log()
        self._paint_progress()
        self._paint_menu()

    def _paint_chrome(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        left = C.bold(self._title) + (C.dim("  " + self._subtitle) if self._subtitle else "")
        right = self._status or ""
        inner = cols - 2
        gap = max(2, inner - len(_plain(left)) - len(_plain(right)) - 2)
        title = " " + left + (" " * gap) + C.dim(right) + " "
        buf = _at(L.title_top_rule, 1, _top(cols))
        buf += _at(L.title_pad_top, 1, _box("", cols))
        buf += _at(L.title_row, 1, _box(title, cols))
        buf += _at(L.title_pad_bot, 1, _box("", cols))
        buf += _at(L.title_sep, 1, _rule_double(cols))
        self._write(buf)

    def _paint_log(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        summary = self._summary_lines()
        h = self._body_height()                  # paged rows above the summary
        self._clamp_view()
        lines = list(self._log)
        window = lines[self._log_view:self._log_view + h]   # the current page
        buf = ""
        for i, row in enumerate(range(L.log_top, L.log_bottom + 1)):
            if i < h:                            # the paged log
                text = (" " + window[i]) if i < len(window) else ""
            else:                                # the pinned summary at the foot
                s = i - h
                text = (" " + summary[s]) if s < len(summary) else ""
            buf += _at(row, 1, _box(text, cols))
        self._write(buf)

    def _paint_progress(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        buf = ""
        if L.show_progress:
            # A single rule, then the one bar row -- no blank padding.
            buf += _at(L.progress_top_rule, 1, _rule_single(cols))
            buf += _at(L.bar_row, 1, _box(" " + self._bar, cols))
        # The log|menu double rule is always drawn (it is the menu's top edge).
        buf += _at(L.progress_sep, 1, _rule_double(cols))
        self._write(buf)

    def _page_hint(self):
        """`j/k) page` with how many lines are hidden above/below, shown only
        when the log has more than one screenful."""
        above = self._log_view
        below = max(0, len(self._log) - self._log_view - self._body_height())
        if not (above or below):
            return ""
        return C.dim("   j/k) page  %s%d %s%d" % (UP, above, DOWN, below))

    def _paint_bar(self):
        # The bar row alone -- redrawn often while a step or the spinner runs.
        if not self._open or not self.layout.show_progress:
            return
        L, cols = self.layout, self.layout.cols
        self._write(_at(L.bar_row, 1, _box((" " + self._bar) if self._bar else "", cols)))

    def _paint_menu(self):
        if not self._open:
            return
        L, cols = self.layout, self.layout.cols
        buf = ""
        for i, row in enumerate(L.menu_rows):
            text = self._menu_lines[i] if i < len(self._menu_lines) else ""
            buf += _at(row, 1, _box(text, cols))
        hint = C.dim("         p) progress   h) help   i) info   l) licence   q) quit")
        hint += self._page_hint()
        buf += _at(L.hint_row, 1, _box(hint, cols))
        buf += _at(L.select_rule, 1, _rule_single(cols))
        buf += _at(L.select_row, 1, _box(SELECT_LABEL, cols))   # visible from the start
        buf += _at(L.bottom_rule, 1, _bottom(cols))
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
        # A newline commits a real log line. It may carry \r redraws inside it
        # (a bar that finished, then printed a newline) -- keep only the segment
        # after the last \r, which is its final content.
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._frame.log(line.rsplit("\r", 1)[-1])
        # A trailing \r with no newline is a live progress redraw: an in-process
        # loop drawing a bar directly to stdout (Bar/_still_bar/_write_line).
        # The log only takes committed lines, so without this those bars vanish
        # in the frame. Pin the redraw to the bar instead; an erase (\r\x1b[2K,
        # nothing visible left) clears it.
        if "\r" in self._buf:
            tail = self._buf.rsplit("\r", 1)[-1]
            shown = _clean_log(tail)
            self._frame.set_bar(shown if _plain(shown).strip() else "")
            # A redraw is a complete line (the loop resends it whole each tick),
            # so the buffer is spent; keeping it would prefix the next log line.
            self._buf = ""
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return self._real.isatty()


def _grid_lines(menu_items, position, world, cols):
    """The exact grid the scrolling UI draws -- screens._Grid, in the same
    columns, coloured and greyed and red the same way. One renderer, so the
    frame's menu and the scroll's cannot drift. The p/h/i/q hint is a separate
    row the frame paints itself.

    world=None is the from-the-start paint before the first capture: every item
    is drawn DIMMED (a loading state -- nothing has been asked yet), and the real
    verdicts light it up on the first loop turn."""
    offered = position.selectable(menu_items)
    verdicts = screens._verdicts(menu_items, world)   # world=None -> all dimmed
    grid = screens._Grid(screens._in_the_grid(menu_items), verdicts, offered, cols)
    return grid.lines()
