"""What one step's outcome is called, and the record kept of it.

The runner produces an outcome and the summary paints one, so this vocabulary
belongs to neither of them. While it lived in pipeline the painter had to
import the pipeline back just to know that a failure is spelled "failed".
"""

from __future__ import annotations

import time
import traceback

import menu
from term import C, tilde

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


# ---------------------------------------------------------------------------
# What is said and recorded about one outcome. It is the runner's vocabulary,
# not the pipeline's: these turn an item's Outcome into the line the operator
# reads, the seconds the summary bills and the crash file a bug report quotes.
# ---------------------------------------------------------------------------

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
