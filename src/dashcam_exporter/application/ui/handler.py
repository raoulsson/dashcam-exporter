"""The output seam every component writes through instead of stdout.

The point is one interface, two backends. `StreamUiHandler` reproduces the exact
bytes the tool printed before this seam existed -- it is the default and what
every test exercises, which is what keeps "the output did not change" a checkable
claim while the direct prints are rerouted through here. A framed backend
(added later) implements the same methods and paints regions instead; the
workflow driving them does not know or care which is installed.

The interface grows one method per output category as call sites move onto it.
Today it carries the two sinks that were already centralized in the code -- the
screen painter's `_print_all` and a step's closing `done_line` -- so this first
step changes no output, only who emits it.

The active handler is module-level, matching the pattern the codebase already
uses for cross-cutting output state (the run-log tee, the hint flag): a
component deep in a step body has no `ctx` in hand, and threading one purely to
reach the UI would touch far more than it buys. `Ctx.ui` exposes the same
handler for the call sites that do hold a ctx.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dashcam_exporter.application.ui.term import C
from dashcam_exporter.application.ui import screens
from dashcam_exporter.application.ui import prompt as prompt_mod
from dashcam_exporter.application.ui.progress import Live, waiting as _stream_waiting


class UiHandler(ABC):
    """The seam. One method per output category; both backends implement all."""

    # -- lifecycle and chrome. No-ops for the stream backend (it has no frame to
    #    open or title bar to paint); the framed backend overrides them. Concrete
    #    so the runner can call them without asking which backend is installed. --

    def open(self):
        """Take the screen (framed: enter the alternate screen)."""

    def close(self):
        """Give it back (framed: leave the alternate screen, restore the cursor)."""

    def title(self, app, subtitle=""):
        """The identity line at the top of the frame."""

    def status(self, facts):
        """The status line under the title."""

    def set_splash(self, lines):
        """Offer the launch art (the ASCII banner) as a splash. The framed
        backend shows it centered for a beat and returns True (it consumed it,
        so the caller need not also print it); the stream backend returns False,
        and the caller prints the banner into the scroll as it always has."""
        return False

    def stat(self, name, value):
        """A named running total for the task (Clips reviewed: 45, Stills
        created: 90). The framed backend pins it to the foot of the main window
        and updates it in place, so the mid says what the task is producing
        while the bar says where it is. The stream backend has no fixed region
        to pin it in, so it is a no-op there -- the step's closing line
        summarises."""

    def paragraph(self, text):
        """One finished paragraph of streaming output (a transcript paragraph).
        The framed backend wraps it to the row and starts a FRESH page when the
        current one is full, so paragraphs are read a screenful at a time rather
        than scrolling past. The stream backend just prints it -- the terminal
        scrolls, which is the same reading model there."""
        print(text)

    def page(self, direction):
        """Scroll the mid-screen a page (j/left = older, k/right = newer) when
        the log has more than fits. Returns True if the backend consumed the key.
        The stream backend has no fixed region to page, so it returns False and
        the key falls through to the menu as before."""
        return False

    def scroll_top(self):
        """Anchor the paged mid-screen on its FIRST page, for a screen printed to
        be read from the top (the licence). Without it the log follows each line
        to the newest page, so a block taller than the region lands on its last,
        near-empty page. The stream backend scrolls a real terminal and has no
        region to anchor, so it is a no-op there."""

    def clear_log(self):
        """Start a fresh mid-screen for a new action. The framed backend wipes
        its scrolling region so the step's own header and output begin at the
        top; the stream backend keeps its scrollback (a terminal scrolls, it does
        not have a region to clear), so this is a no-op there."""

    def prime_menu(self, ctx, menu_items, position):
        """Draw the menu straightaway, before the first (possibly slow) world
        capture, so a framed frame is complete from the start; greying arrives
        with the real world on the first loop turn. A no-op for the stream
        backend, which has no standing frame to fill."""

    @abstractmethod
    def block(self, lines):
        """A screen: ready-made lines (help, info, the summary table)."""

    @abstractmethod
    def menu(self, ctx, menu_items, position, world):
        """The menu itself, from its state -- the stream backend paints the grid,
        a framed backend draws a compact bar. Passed the state, not pre-rendered
        lines, so each backend renders it its own way."""

    @abstractmethod
    def summary(self, ctx, close=True):
        """The session log table drawn on the way out (and inside Progress)."""

    @abstractmethod
    def log(self, text=""):
        """One committed line of output -- it stays where it lands."""

    @abstractmethod
    def new_live(self):
        """A live area for a streamed step: an object with draw(lines)/close()
        and a `height`/`enabled`, redrawn in place. The stream backend hands
        back the scrolling Live; a framed backend hands back a drawer that
        paints the pinned progress strip instead."""

    @abstractmethod
    def waiting(self, label):
        """An indeterminate spinner for a blocking call with nothing to report
        until it returns (the plugin query, a delete). A context manager with an
        update(note) the blocking work can call. The stream backend animates a
        line; a framed backend animates the pinned bar."""

    @abstractmethod
    def done(self, what):
        """The one line a step leaves behind when it worked."""

    # -- input. The frame reads keys in-frame; the stream backend keeps the
    #    terminal's raw/cooked reads. Tests patch the prompt module underneath,
    #    so routing through here does not move their seam. --

    @abstractmethod
    def read_key(self, prompt):
        """One keypress at the menu (a digit, p/h/i/q)."""

    @abstractmethod
    def ask(self, prompt, default="", quits=True):
        """A line of input; q/quit raises Aborted when `quits`."""

    @abstractmethod
    def confirm(self, prompt, default=False):
        """A single-key y/n."""


class StreamUiHandler(UiHandler):
    """Writes to stdout exactly as the tool always has, by driving the existing
    painters (`screens`) and colours (`term`). The default backend. The `ui`
    package stays acyclic: this module depends on the renderers, never the
    reverse -- `screens` knows nothing about who is calling it."""

    def block(self, lines):
        screens._print_all(lines)

    def menu(self, ctx, menu_items, position, world):
        screens.print_menu(ctx, menu_items, position, world)

    def prime_menu(self, ctx, menu_items, position):
        # Paint the dimmed menu before the plugin query, so the scrolling UI --
        # like the frame -- shows the menu first and then the "querying" bar,
        # rather than appearing to hang after the status block.
        screens.print_menu(ctx, menu_items, position, None)

    def summary(self, ctx, close=True):
        screens.print_summary(ctx, close)

    def log(self, text=""):
        print(text)

    def new_live(self):
        return Live(enabled=C.enabled)

    def waiting(self, label):
        return _stream_waiting(label)

    def done(self, what):
        print(C.green("  100%% - %s." % what))

    def read_key(self, prompt):
        return prompt_mod.read_key(prompt)

    def ask(self, prompt, default="", quits=True):
        return prompt_mod.ask(prompt, default, quits)

    def confirm(self, prompt, default=False):
        return prompt_mod.confirm(prompt, default)


_active = None


def active():
    """The handler in force. Defaults to the stream backend, so a component that
    asks before anyone has installed one behaves exactly as it did before the
    seam existed."""
    global _active
    if _active is None:
        _active = StreamUiHandler()
    return _active


def set_active(handler):
    """Install a backend (or None to fall back to the default on next `active()`)."""
    global _active
    _active = handler
