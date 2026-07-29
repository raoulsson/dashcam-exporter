#!/usr/bin/env python3
"""The collaborators item bodies are handed must fit the call they are handed to.

The other test files ask whether the graph is right (test_step_graph), what the
guards allow (test_guards), what the pipeline does with an item's answer
(test_pipeline, on mocks) and which paths exist (test_paths). None of them can
catch a collaborator that is built with one shape and called with another,
because none of them calls the real body: the mock files deliberately run
nothing real, and the path files stop at the item boundary.

That gap let item 6 ship crashing on every single run under both strategies.
The constructor bound ctx into the gatherer, build_result_page passed ctx again,
and a two-argument function got three. Two hundred and seventy-one tests were
green over it.

So this file asks the narrow question those cannot: for every collaborator the
constructor installs, does its signature accept exactly the arguments its call
site passes? Binding the signature is enough and is side-effect free — the bug
is an arity mismatch, and no footage needs to move to prove it.
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.argv = ["test_wiring"]

import menu as M                                                   # noqa: E402
import pipeline as P                                               # noqa: E402


def a_work():
    """A Work with only what the collaborator factories read: the ctx."""
    work = P.Work.__new__(P.Work)
    work.ctx = object()
    return work


class TestTheGathererFitsItsCallSite(unittest.TestCase):
    """build_result_page calls `gather(ctx, out_dir)` and falls back to
    gather_into_final, so whatever the constructor installs must take the same
    two arguments the default does."""

    def test_the_installed_gatherer_takes_ctx_and_out_dir(self):
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                gather = a_work().gatherer(strategy)
                inspect.signature(gather).bind(object(), Path("/nowhere"))

    def test_it_matches_the_default_it_stands_in_for(self):
        """The fallback is the specification: if the installed gatherer does
        not have the default's shape, the call site cannot treat them alike."""
        want = inspect.signature(P.gather_into_final)
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                got = inspect.signature(a_work().gatherer(strategy))
                self.assertEqual(list(got.parameters), list(want.parameters))

    def test_ctx_is_not_bound_twice(self):
        """The specific mistake, named so it cannot come back quietly: a
        partial over ctx passes the arity check against one argument and fails
        against the two the call site really passes."""
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                gather = a_work().gatherer(strategy)
                self.assertFalse(getattr(gather, "args", ()),
                                 "the gatherer arrives with arguments already bound")


class TestThePublisherFitsItsCallSite(unittest.TestCase):
    """Item 7 asks its publisher two things, and both are asked by name."""

    def test_every_publisher_answers_the_whole_interface(self):
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                publisher = a_work().publisher(strategy)
                for asked in ("why_not", "run"):
                    self.assertTrue(callable(getattr(publisher, asked, None)),
                                    "publisher cannot answer %s()" % asked)

    def test_why_not_takes_the_world(self):
        for strategy in M.Strategy:
            with self.subTest(strategy=strategy.value):
                why_not = a_work().publisher(strategy).why_not
                inspect.signature(why_not).bind(object())


if __name__ == "__main__":
    unittest.main()
