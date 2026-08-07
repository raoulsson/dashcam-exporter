#!/usr/bin/env python3
"""Print the menu graph FROM the item classes — never from a description.

The table people paste into docs and messages goes stale the moment an edge
moves; this one cannot, because it is the live items answering for themselves.
Per item and per strategy: number, name, start, end, destr, inbound, outbound —
the owner's own columns, in his order, so the output can be laid beside his
table and read line for line.

Underneath, the inbound column he WROTE is diffed against the one derived from
every other item's outbound, and every difference is printed. It is reported,
never reconciled: the derivation is what the tool runs on, and each difference
is a line of his table for him to confirm or fix.

Two items are exempt by definition rather than by a skip list, because their
inbound is a KIND and not a set of numbers:

  * 1) Import SIM declares StartNode. Clean Workspace's outbound is {1}, so it
    does have an arriving edge — and it still declares no inbound, because it
    is where footage comes in and is reachable with nothing done at all. The
    owner: "1 has no inbound cause it's a start node".
  * 0) Progress declares Anywhere. It neighbours everything, and must not force
    the other nine to declare it back.

Run:  python3 tests/print_step_graph.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dashcam_exporter import items, menu as M  # noqa: E402,F401


class NullWork:
    """Enough `work` to CONSTRUCT the ten items, with no pipeline behind them.

    Items 6 and 7 ask for their collaborator in the constructor — that is where
    the strategy branch is settled — so the graph cannot be built without
    something to ask. Nothing here is ever called: this module only reads the
    edges the items declare.
    """

    def builder(self, strategy):
        return NullBuilder()

    def publisher(self, strategy):
        return None


class NullBuilder:
    """Item 5 asks its builder for the menu row's wording in description(),
    which IS read here — this module prints the table."""

    def describe(self):
        return "build"


def nums(neighbours):
    """A neighbour side as the owner writes it: numbers, or the kind's word."""
    edges = neighbours.edges()
    if edges is None:
        return repr(neighbours)
    return _set(edges)


def flag(on):
    return "yes" if on else "-"


def print_table(strategy):
    built = M.build_menu(strategy, NullWork())
    print()
    print("=== %s ===" % strategy.value)
    print("  %-2s  %-16s %-6s %-5s %-6s %-16s %s"
          % ("#", "step", "start", "end", "destr", "inbound", "outbound"))
    for n in sorted(built):
        item = built[n]
        print("  %-2d  %-16s %-6s %-5s %-6s %-16s %s"
              % (n, item.name(), flag(item.start()), flag(item.end()),
                 flag(item.destr()), nums(item.inbound()), nums(item.outbound())))
    print_disagreements(strategy)


def print_disagreements(strategy):
    """The owner's inbound column against the derived one, difference by
    difference. Silence here means his table and the graph agree."""
    found = M.disagreements(M.registry(), strategy)
    if not found:
        print("  inbound as authored vs derived: agrees on every item")
        return
    print("  inbound as authored vs derived: %d items differ" % len(found))
    print("\n".join(map(_difference, found)))


def _difference(d):
    return ("    %d) authored %s | derived %s | derivation adds {%s}, drops {%s}"
            % (d.number, _set(d.authored), _set(d.derived),
               _set(d.extra), _set(d.missing)))


def _set(numbers):
    return ",".join(map(str, sorted(numbers))) or "-"


def main():
    for strategy in M.Strategy:
        print_table(strategy)


if __name__ == "__main__":
    main()
