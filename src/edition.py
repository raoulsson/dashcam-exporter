"""Which edition of the tool this is: where it is installed, and what it calls
itself.

Split out of pipeline because the `i` screen has to state the version and the
repository, and the screens must not import the pipeline back to learn them --
two of our own modules importing each other is a missed seam rather than a
design. This is the third thing they both depend on.

The checkout object is constructed here rather than a second time next to the
screens: a second RealCheckout would be a second answer to the question
checkout.py exists to make sure is only answered once.
"""

from __future__ import annotations

import subprocess

from checkout import RealCheckout

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
