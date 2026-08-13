"""Terminal primitives: how a number, a path or a duration is spelled on
screen, how wide the screen is, and the colours.

Split out of pipeline so that the progress bars can use them without the two
modules importing each other. Nothing here knows what the tool does.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def human_bytes(n):
    """Decimal GB, the way df -H and the label on the disk report it.

    Powers of ten, not two. The operator compares these figures against what
    the Finder and df tell him about the same card, and 1024-based units are
    seven per cent smaller than both at gigabyte scale -- so a 136.6 GB card
    read as "127.2 GB" and the difference looked like footage the tool could
    not see.
    """
    if n is None:
        return "?"
    step = 1000.0
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
    return "%d days" % (seconds // 86400)


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
    def bright_green(cls, s):
        return cls._w("92", s)

    @classmethod
    def muted_green(cls, s):
        """A positive state that is informational, not a fresh completion."""
        return cls._w("2;32", s)

    @classmethod
    def yellow(cls, s):
        """Attention: a decision to make, or a warning that is not an error."""
        return cls._w("33", s)

    @classmethod
    def cyan(cls, s):
        return cls._w("36", s)

    @classmethod
    def bright_cyan(cls, s):
        return cls._w("96", s)

    @classmethod
    def violet(cls, s):
        """The bars, and nothing else. 256-colour, because magenta (35) is the
        same weight as the red that means "this destroys something"."""
        return cls._w("38;5;177", s)

    # There were two more names here, `gold` for yellow and `magenta` for
    # violet, and both taught the reader the wrong thing. `gold` was a second
    # name for one colour, so five call sites looked like a different decision
    # from the forty-nine that typed `yellow` for the same ink; and `magenta`
    # named the exact colour (35) this class deliberately refuses, so a call
    # site reading `C.magenta` claimed the shade violet exists to avoid. A
    # semantic alias that only ever forwards is not an abstraction, it is a
    # synonym -- and a palette with synonyms cannot be audited by grep.


def rule(title="", ch="-"):
    # Exactly the reported width. A character wider reaches the right edge and
    # then wraps a blank line onto the next one, which reads as a gap between
    # sections rather than as a rule.
    #
    # The dashes are dim, the title is not. A hundred columns of "=" at full
    # weight was the brightest thing on a screen whose content is a menu of dim
    # and bold names -- chrome outshouting what it frames. The framed backend
    # has always drawn its borders dim (`framed._rule_double`); this is the same
    # separator in the scrolling backend, so it had to be the same weight or the
    # two backends teach two different ideas of what a rule is.
    w = term_width()
    if not title:
        return C.dim(ch * w)
    head = "%s %s " % (ch * 2, title)
    return C.bold(head) + C.dim(ch * max(0, w - len(head)))
