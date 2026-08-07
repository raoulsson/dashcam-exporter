"""Reading one key, one line or one yes/no from the operator.

Terminal input and nothing else: it knows about raw mode, about a pipe that
has no terminal to read a single key from, and about q meaning stop. It knows
nothing about what the answer will be used for.

It raises Aborted rather than returning a sentinel, because "the operator said
stop" has to travel up through whatever asked, and every caller that had to
remember to check a return value was a caller that could forget.
"""

from __future__ import annotations

import re
import sys
try:
    import termios
    import tty
except ImportError:      # not a POSIX terminal; typed lines still work
    termios = tty = None

from dashcam_exporter.application.ui.progress import _erase_line
from dashcam_exporter.application.workflow.results import Aborted
from dashcam_exporter.application.ui.screens import _print_all
from dashcam_exporter.application.ui.term import C

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
