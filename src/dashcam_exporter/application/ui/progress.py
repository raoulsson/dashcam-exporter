"""The terminal progress family: the live output area and the two bars.

Taken out of pipeline whole. One file rather than three because they are not
independent: Waiting IS a Bar, the wrappers at the bottom exist only to build
one of the two and render it, and Live and Bar implement the same blank-line
protocol on the same `opened` field.
"""

from __future__ import annotations

import sys
import re
import threading
import time
from dataclasses import dataclass

from dashcam_exporter.application.ui.term import C, human_secs, term_width

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clip(text, width):
    """Clip by terminal columns, never by raw string bytes.

    Colour escapes are invisible but used to count toward ``width`` by the
    old slice in ``Live.draw``. That cut the useful filename off the right
    edge even when there was visible room left, and made redraws appear to
    change shape. Keep complete escapes and clip only printable characters.

    A clipped line is closed with a reset. The loop appends the escape it is
    standing on before it notices the budget is spent, so a cut that lands
    inside a coloured span emits that span's OPENING code as the last thing on
    the line and nothing to close it -- the colour then runs on into whatever
    the caller writes next, which in the frame is the box's right border. It
    only showed up once the row's last field stopped being wrapped in colour of
    its own; before that the escape the loop happened to append was usually the
    span's reset, and the bug was invisible rather than absent.
    """
    if width <= 0:
        return ""
    out, used, pos = [], 0, 0
    for match in _ANSI.finditer(text):
        plain = text[pos:match.start()]
        take = min(len(plain), width - used)
        if take:
            out.append(plain[:take])
            used += take
        out.append(match.group(0))
        pos = match.end()
        if used >= width:
            break
    if used < width and pos < len(text):
        out.append(text[pos:pos + width - used])
    clipped = "".join(out)
    if _ANSI.search(clipped) and len(_ANSI.sub("", text)) > width:
        clipped += "\x1b[0m"
    return clipped


# ---------------------------------------------------------------------------
# A progress row as DATA, coloured on the client. The message side (a Readout,
# a waiting bar) fills a ProgressState; render_progress maps the palette and the
# order. The colours no longer live in the string the message builds, so a copy,
# a render and a plugin query are one shape rendered one way.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressState:
    """One progress row, no colour and no layout -- just its fields. Anything
    left None is dropped from the row rather than blanked."""
    action: str                  # "Import", "Render", "Querying the plugin"
    fraction: float = None       # 0..1 fill for the bar; None => not measured
    infinite: bool = False       # indeterminate: a bouncing block, no percent
    percent: int = None          # explicit percent; else omitted
    speed: str = None            # "77.18MB/s"
    time: str = None             # "0:05/--:--" or "0:07"
    size: str = None             # "373.2 MB/32.6 GB"
    subaction: str = None        # the identity: filename / phase / plugin note
    tail: str = None             # the ticking detail beside it (counter, file)
    bounce: int = 0              # frame index, for the infinite block's slot


@dataclass(frozen=True)
class ProgressDetail:
    """What a parser pulls out of a line beyond the fraction -- the fields that
    used to be crammed into one formatted `note`. A parser may still return a
    bare string instead (taken as `subaction`) where it has nothing more.

    `subaction` is the identity (what is being worked on); `tail` is the live
    detail shown dimmer beside it (a clip counter, the child's current file)."""
    subaction: str = ""
    speed: str = ""
    size: str = ""
    tail: str = ""


_FILLED, _EMPTY, _BLOCK = "#", ".", "###"


def _bounce_at(i, width):
    span = max(1, width - len(_BLOCK))
    step = i % (2 * span)
    return step if step < span else 2 * span - step


def _bar_cells(state, width):
    if state.infinite:
        at = _bounce_at(state.bounce, width)
        return _EMPTY * at + _BLOCK + _EMPTY * (width - at - len(_BLOCK))
    frac = min(max(state.fraction or 0.0, 0.0), 1.0)
    filled = int(round(width * frac))
    return _FILLED * filled + _EMPTY * (width - filled)


def render_progress(state, width):
    """Assemble and COLOUR one progress row from its data -- the client side.

    THE one progress renderer. Every bar in the tool -- a child's stream, a
    waiting spinner, an in-process loop -- goes through here, so they all read
    alike. Fixed order and palette:
      action(bold)  bar(violet)  percent(bold)  speed/time/size(dim)  subaction(plain)  tail(dim)

    Every colour on this row used to be borrowed from somewhere it meant
    something else, which is why a running bar read like a wall of alarms:

    - the action and the identity were BRIGHT GREEN, the colour that means a
      completion that just happened. A step at 3% wore the same green as the
      line saying it had finished, so the one colour that should make the eye
      jump was on screen for the whole of every render.
    - the speed, the elapsed time and the size were YELLOW, the colour that
      means a decision is waiting. Nothing is waiting: they are measurements,
      and three of them ticked in the attention colour on every frame.
    - the percentage was CYAN, which belongs to the operator's own input and
      the things he can press. He cannot press a percentage.

    So each field now wears what it is. The bar keeps violet (it is the only
    moving bar), the two things worth reading -- what is running and how far --
    are bold, and the measurements around them recede. The old rule was
    "nothing here is dim", which is what forced every field onto a colour: the
    row had six voices and no background.
    """
    bar_w = max(8, min(24, width - 60))
    parts = [C.bold(state.action),
             C.violet("[%s]" % _bar_cells(state, bar_w))]
    if not state.infinite and state.percent is not None:
        parts.append(C.bold("%3d%%" % state.percent))
    if state.speed:
        parts.append(C.dim(state.speed))
    if state.time:
        parts.append(C.dim(state.time))
    if state.size:
        parts.append(C.dim(state.size))
    if state.subaction:
        parts.append(state.subaction)
    if state.tail:
        parts.append(C.dim(state.tail))
    # Cap (and pad) to exactly one row's width. A long subaction -- a plugin's
    # ssh note, a long filename -- must never overrun the line; and padding to
    # the width clears the tail of the previous, longer redraw so nothing of it
    # is left behind.
    line = _clip("  " + "  ".join(parts), width)
    return line + " " * max(0, width - len(_ANSI.sub("", line)))


# ---------------------------------------------------------------------------
# Live output area: a progress bar (or spinner) plus the raw last line, redrawn
# in place. Nothing the child prints is ever hidden — the last line is always
# on screen, and the full stream is buffered so a failure can dump its tail.
# ---------------------------------------------------------------------------

class Live:
    def __init__(self, enabled):
        self.enabled = enabled
        self.height = 0
        self.opened = False
        self.emitted = False
        if self.enabled:
            sys.stdout.write("\x1b[?25l")   # hide cursor
            sys.stdout.flush()

    def _open(self):
        """A blank line under whatever printed last, once, before the first
        draw. The bar arrived hard against the line above it -- a status row,
        a prompt the operator had just answered -- and read as part of it.

        Taken back by close(), unless something was emitted under it. A step
        that runs two children -- the grouping scan and then the sidecar pass
        -- opened one each and left both behind, so the line that followed sat
        two blank lines below the line that introduced it.
        """
        if not self.opened:
            self.opened = True
            sys.stdout.write("\n")

    def _erase(self):
        if self.height:
            sys.stdout.write("\x1b[%dA" % self.height)
            sys.stdout.write("\x1b[J")
            self.height = 0

    def draw(self, lines):
        if not self.enabled:
            return
        self._open()
        self._erase()
        w = term_width() - 1
        for ln in lines:
            # Truncating can cut a colour sequence's trailing reset off, which
            # would bleed the colour into the rest of the terminal. Re-append it.
            sys.stdout.write(_clip(ln, w) + "\x1b[0m\n")
        self.height = len(lines)
        sys.stdout.flush()

    def emit(self, text):
        """Print a line that stays on screen, above the live area."""
        if not self.enabled:
            print(text)
            return
        self.emitted = True
        self._erase()
        sys.stdout.write(_clip(text, term_width() - 1) + "\n")
        sys.stdout.flush()

    def close(self):
        if self.enabled:
            self._erase()
            if self.opened and not self.emitted:
                # Take the blank line back. It was there to separate a bar
                # that is now gone, and nothing was left under it.
                sys.stdout.write("\x1b[1A\x1b[J")
            sys.stdout.write("\x1b[?25h")   # show cursor
            sys.stdout.flush()
            self.enabled = False


class Bar:
    """The blank-line protocol around an in-place bar -- open a line to draw on,
    take it back when the bar is gone. Nothing about how the bar LOOKS lives here
    any more: render_progress is the one renderer, and Waiting and _still_bar
    draw through it. This carries the label and the open/close bookkeeping they
    share, and Waiting subclasses it for the same open_once/close.
    """

    def __init__(self, label, width=None):
        self.label = label
        self._width = width
        self.opened = False

    def open_once(self):
        """A blank line under whatever printed last, before the first draw.

        Every bar in the tool gets one -- the live area opens with it, the
        indeterminate bar opens with it after its lead-in, and a bar drawn
        directly through _write_line has to ask for it here. Without it the
        bar arrives hard against the line above and reads as part of it.
        """
        if self.opened:
            return
        self.opened = True
        sys.stdout.write("\n")
        sys.stdout.flush()

    def close(self):
        """Erase the bar and take its blank line back with it.

        The same rule the live area follows: the blank was there to separate a
        bar that is now gone. Two bars in one step -- the stills pass and then
        the clip review -- each left one behind, so the sentence after them sat
        two blank lines below the line that introduced them.
        """
        _erase_line()
        if self.opened and C.enabled:
            sys.stdout.write("\x1b[1A\x1b[J")
            sys.stdout.flush()
        self.opened = False


def _eta(fraction, elapsed):
    """None below a fiftieth, where the estimate is noise wearing a number."""
    if fraction <= 0.02:
        return None
    return elapsed * (1 - fraction) / fraction


class Waiting(Bar):
    """A block sliding back and forth, for work whose length nobody can know.

    Used as a context manager, and it runs itself: the work it wraps is a
    blocking call with nothing to report until it returns, so there is no
    caller to redraw the line. Silent when stdout is not a terminal, which is
    every test and every piped run.
    """

    BLOCK, STEP = "###", 0.12
    # Nothing on screen until the work has actually taken this long. Most
    # captures return in a hundredth of a second, and a bar that appears and
    # vanishes on every one of them is a flicker that says "slow" about work
    # that was not -- which is the opposite of what it is for.
    LEAD_IN = 0.4

    def __init__(self, label, width=None):
        super().__init__(label, width)
        self._stop = threading.Event()
        self._thread = None
        self._drawn = False
        self._note = ""

    def update(self, note):
        """Set a live note supplied by a plugin doing the blocking work."""
        self._note = str(note or "")

    def __enter__(self):
        if sys.stdout.isatty():
            self._start()
        return self

    def _start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._drawn and _erase_line()
        return False

    def _run(self):
        started, i = time.time(), 0
        if self._stop.wait(self.LEAD_IN):
            return
        self.open_once()
        while not self._stop.wait(self.STEP):
            self._drawn = True
            _write_line(self.render_at(i, time.time() - started))
            i += 1

    def render_at(self, i, elapsed):
        """`action [.........###....] 0:03  note` -- via the shared renderer, so
        the indeterminate bar wears the same colours as the determinate one."""
        state = ProgressState(action=self.label, infinite=True,
                              time=human_secs(elapsed),
                              subaction=self._note or None, bounce=i)
        return render_progress(state, term_width())


def _write_line(text):
    # Waiting is drawn directly, outside Live.draw. Keep it to one terminal
    # row so _erase_line() can remove the complete bar when the wrapped call
    # returns; a long plugin note otherwise wraps and leaves its first row
    # behind after Website built/lookup completes.
    sys.stdout.write("\r" + _clip(text, term_width() - 1))
    sys.stdout.flush()


def _erase_line():
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.flush()


def waiting(label):
    """The indeterminate bar, as a context manager. Kept as a function because
    that is how every call site reads: `with waiting("..."):`."""
    return Waiting(label)


def show_cursor():
    """Unconditional cursor restore, for the panic paths."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def _still_bar(bar, i, total, name):
    """One line, redrawn, for a countable in-process loop -- a determinate bar
    through the one renderer, so it wears the same colours as a child's stream.
    The filename is the subaction; the count is read from the percentage."""
    if not C.enabled:
        return
    bar.open_once()
    frac = (i / float(total)) if total else 0.0
    state = ProgressState(action=bar.label, fraction=frac,
                          percent=int(frac * 100), subaction=name)
    _write_line(render_progress(state, term_width()))


def _sweep_line(label, i, elapsed):
    """`label [.....###....] 0:05` — the bar for work with no denominator.

    Waiting draws exactly this and is where the shape lives; this is the
    streaming caller's way in. It is deliberately NOT a percentage: run_stream
    never synthesises one from a guess, and a deploy cannot say how many phases
    it has until it has run them. What the operator needs from a step whose
    length nobody knows is that it is alive, and a block that keeps moving says
    that in the same visual language as the bars around it.

    No leading indent and no trailing space: Waiting.render_at owns a whole
    line, this one is a head that the child's latest output is appended to.
    """
    return Waiting(label).render_at(i, elapsed).strip()
