"""Every screen the operator reads: the menu grid, the refusals, the help, the
`i` screen and the summary.

Everything here turns items, verdicts and a world into lines of text, and
nothing here acts -- so it can be read and diffed without a card in the
machine, which is what makes "the paint did not change" a checkable claim.

It imports no pipeline, deliberately. The runner needs the painter, so a
painter that needed the runner's module back would be a cycle; what it needs
instead lives in the three modules underneath both (term, edition, results).
"""

from __future__ import annotations

import textwrap

from dashcam_exporter.domain.menu import menu
from dashcam_exporter.application.ports import uploader
from dashcam_exporter.infrastructure.version.edition import COFFEE_URL, EXPORTER_DIR, REPO_URL, SPONSORS_URL, version
from dashcam_exporter.application.workflow.results import ABORTED, FAILED, RAN, SATISFIED
from dashcam_exporter.application.ui.term import C, rule, term_width, tilde

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
    _print_all(_Grid(_in_the_grid(menu_items), verdicts, offered,
                     term_width()).lines())
    # The reasons entries are greyed have moved to `p`. They are the same eight
    # lines under every menu draw, and a block that never changes stops being
    # read -- which is a problem when the one line that DID change is in it.
    # The destructive entries are already red, and every one of them asks for
    # a typed word before it does anything. A line naming them under every
    # draw was a third telling of the same fact.
    print(C.dim("\tp = progress   h = help   i = info   q = quit"))


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


class _Grid:
    """The steps laid out in columns, painted a row at a time.

    A class because no row can be drawn without the whole geometry -- how wide
    a cell is, how many columns fit the terminal, how many rows that makes --
    and handing all of it down to each row put seven arguments on a function
    whose entire job is one line of text. Settled once here, asked rather than
    passed.
    """

    def __init__(self, menu_items, verdicts, offered, width):
        self.ordered = list(map(menu_items.get, sorted(menu_items)))
        self.verdicts = verdicts
        self.offered = offered
        self.cell = _cell_width(menu_items)
        self.cols = _grid_columns(width, self.cell)
        self.rows = (len(self.ordered) + self.cols - 1) // self.cols

    def lines(self):
        return list(map(self._row, range(self.rows)))

    def _row(self, r):
        picks = map(lambda c: r + c * self.rows, range(self.cols))  # down, then across
        here = filter(lambda i: i < len(self.ordered), picks)
        return "  " + "".join(map(self._cell, here))

    def _cell(self, i):
        item = self.ordered[i]
        return _menu_line(item, self.verdicts[item.number],
                          item.number in self.offered, self.cell)


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
    return ("", C.cyan("  Next available steps:")) + tuple(
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
_STATUS_TAGS = {RAN: lambda: C.muted_green("processed"),
                SATISFIED: lambda: C.muted_green("satisfied"),
                ABORTED: lambda: C.yellow("aborted  "),
                FAILED: lambda: C.red("FAILED   ")}


def _status_tag(status):
    return _STATUS_TAGS.get(status, lambda: C.yellow("skipped  "))()
