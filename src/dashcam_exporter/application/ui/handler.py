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
from dashcam_exporter.application.ui.progress import Live


class UiHandler(ABC):
    """The seam. One method per output category; both backends implement all."""

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

    def summary(self, ctx, close=True):
        screens.print_summary(ctx, close)

    def log(self, text=""):
        print(text)

    def new_live(self):
        return Live(enabled=C.enabled)

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
