#!/usr/bin/env python3
"""Print the step graph FROM the step objects — never from a description.

The table people paste into docs and messages goes stale the moment an edge
moves; this one cannot, because it is the live ALL_STEPS answering for itself.
Per step and per strategy: number, label, is_start_node, is_destructive,
reachable_from, reaches_to — and inconsistent_edges() at the bottom, so an
edge declared from one side only shows up in the output rather than having to
be spotted by eye. "Code describing itself is never wrong."

Run:  python3 tests/print_step_graph.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import steps as S    # noqa: E402  (after sys.path)


def fake_ctx(strategy):
    """Just enough ctx for is_start_node: configuration, no filesystem."""
    publishing = strategy is S.Strategy.WEBSITE_REPO
    return SimpleNamespace(site=Path("/nonexistent") if publishing else None,
                           s3_bucket="bucket" if publishing else None,
                           card=Path("/nonexistent"), out_dir=Path("/nonexistent"),
                           render_root=Path("/nonexistent"))


def nums(values):
    return ",".join(str(n) for n in sorted(values)) or "-"


def main():
    for strategy in S.Strategy:
        ctx = fake_ctx(strategy)
        print()
        print("strategy: %s" % strategy.value)
        print("  %2s  %-16s %-6s %-12s %-16s %s"
              % ("#", "step", "start", "destructive", "reachable_from", "reaches_to"))
        for n in sorted(S.ALL_STEPS):
            step = S.ALL_STEPS[n]
            note = " (view)" if step.is_view() else ""
            print("  %2d  %-16s %-6s %-12s %-16s %s"
                  % (n, step.title + note,
                     "yes" if step.is_start_node(ctx) else "-",
                     "yes" if step.is_destructive() else "-",
                     nums(step.reachable_from(strategy)),
                     nums(step.reaches_to(strategy))))
        problems = S.inconsistent_edges(strategy)
        print("  inconsistent edges: %s" % (problems if problems else "none"))


if __name__ == "__main__":
    main()
