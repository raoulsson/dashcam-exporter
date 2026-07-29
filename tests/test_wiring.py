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

The gap got WIDER when the publishing half became an interface. There is now a
whole surface an outsider implements and this repo calls, and the call sites are
spread across capture_world, item 6, item 7 and Exclude Trip. So the question is
asked here for every method on the interface, and the table of call sites is
checked against WebsiteUploaderInterface.__abstractmethods__ — a method added to
the interface later fails this file until someone writes down where it is called
from. Binding the signature is enough and is side-effect free: the bug class is
an arity mismatch, and no footage needs to move to prove it.
"""

import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.argv = ["test_wiring"]

import menu as M                                                   # noqa: E402
import pipeline as P                                               # noqa: E402
import uploader as U                                               # noqa: E402
import world as W                                                  # noqa: E402

UPLOADER, LOCAL = M.Strategy.UPLOADER, M.Strategy.LOCAL_PAGE


class Nothing(U.WebsiteUploaderInterface):
    """A target that answers everything with the null answer.

    Only its SHAPE is under test here, so the bodies are as empty as the ABC
    permits. What the answers mean is test_uploader.py's subject and what the
    guards do with them is test_guards.py's.
    """

    def build(self, world, ui):
        return U.Report(True)

    def upload(self, world, ui):
        return U.Report(True)

    def why_not_build(self, world):
        return None

    def why_not_upload(self, world):
        return None

    def owes(self, renders):
        return U.Owed.nothing()

    def holds(self, renders):
        return U.Answers.unknown("a wiring test does not go and look")

    def published(self, renders):
        return U.Answers.unknown("a wiring test does not go and look")

    def carries(self, trip_ids):
        return U.Answers.unknown("a wiring test does not go and look")


def a_ctx(uploader=None):
    """Enough of a Ctx for the collaborator factories: the uploader, and
    nothing that would make anything happen."""
    ctx = P.Ctx.__new__(P.Ctx)
    ctx.uploader = uploader
    ctx.uploader_origin = "Nothing (a wiring test)"
    return ctx


def a_work(uploader=None):
    work = P.Work.__new__(P.Work)
    work.ctx = a_ctx(uploader)
    return work


# ---------------------------------------------------------------------------
# The interface against its real call sites
# ---------------------------------------------------------------------------

A_WORLD = W.World()
RENDERS = (W.Render("trip_2026-07-28_08-57_01_h1080.mp4", 64),)
TRIP_IDS = ("trip_2026-07-28_08-57_01",)
A_UI = P.Console(a_ctx())

# What each method is really called with, written where it can be checked.
# Read it as documentation of the boundary: the left column is everything the
# exporter asks an outsider, the right column is all it ever hands over.
CALL_SITES = {
    # pipeline.TargetBuild.build, from item 6's _perform
    "build": (A_WORLD, A_UI),
    # pipeline.TargetPublish.run, from item 7's _perform
    "upload": (A_WORLD, A_UI),
    # pipeline.TargetBuild.why_not, from item 6's evaluate, every menu draw
    "why_not_build": (A_WORLD,),
    # pipeline.TargetPublish.why_not, from item 7's evaluate, every menu draw
    "why_not_upload": (A_WORLD,),
    # pipeline._answered, inside capture_world at FULL scope
    "owes": (RENDERS,),
    "holds": (RENDERS,),
    "published": (RENDERS,),
    "carries": (TRIP_IDS,),
    # pipeline._record_curation, when Exclude Trip has actually dropped one
    "dropped": (TRIP_IDS, A_UI),
    # pipeline._target_status, once at launch
    "status_lines": (),
    # read for the menu row, the gate table and the erase banner
    "name": (),
    "describe": (),
}


class TestEveryInterfaceMethodHasAKnownCallSite(unittest.TestCase):
    """The table above is the boundary, stated once.

    Its value is that it goes stale loudly: add a method to the interface and
    this fails until the table says where the exporter calls it from, which is
    the moment to notice that nothing calls it at all.
    """

    def test_no_abstract_method_is_missing_from_the_table(self):
        missing = U.WebsiteUploaderInterface.__abstractmethods__ - set(CALL_SITES)
        self.assertEqual(missing, set(),
                         "the interface grew a method with no call site written down")

    def test_no_entry_in_the_table_is_absent_from_the_interface(self):
        for name in CALL_SITES:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(U.WebsiteUploaderInterface, name, None)),
                                "%s is called but the interface does not declare it" % name)


class TestTheInterfaceAcceptsWhatItsCallSitesPass(unittest.TestCase):
    """Signature binding, method by method, against the real arguments.

    Against the ABC AND against the shipped example, because a subclass is
    free to narrow a signature and only the subclass is what actually runs.
    """

    def _bind_all(self, target):
        for name, args in CALL_SITES.items():
            with self.subTest(method=name):
                inspect.signature(getattr(target, name)).bind(*args)

    def test_the_declared_interface_binds(self):
        self._bind_all(Nothing())

    def test_the_shipped_example_binds(self):
        spec = "%s:FolderTarget" % (REPO / "examples" / "uploader_folder.py")
        self._bind_all(U.load_uploader(spec, REPO))


# ---------------------------------------------------------------------------
# The collaborators the item constructors install
# ---------------------------------------------------------------------------

class TestTheBuilderFitsItsCallSite(unittest.TestCase):
    """Item 6 asks its builder three things, all by name.

    There is no gatherer any more: only the MOVER used to be the strategy
    branch, so the page writer ran under both editions and a publishing install
    got a local page announcing that nothing had left the machine. The whole
    body is the branch now, which is why what is checked here is a builder.
    """

    def _builders(self):
        return ((UPLOADER, a_work(Nothing()).builder(UPLOADER)),
                (LOCAL, a_work().builder(LOCAL)))

    def test_every_builder_answers_the_whole_call_site(self):
        for strategy, builder in self._builders():
            with self.subTest(strategy=strategy.value):
                inspect.signature(builder.describe).bind()
                inspect.signature(builder.why_not).bind(A_WORLD)
                inspect.signature(builder.build).bind(A_WORLD)

    def test_the_edition_decides_which_builder_is_installed(self):
        """The defect being fixed, stated as a property of the wiring: under a
        configured uploader the local page's writer is not installed at all,
        so it cannot run and cannot claim nothing left this machine."""
        self.assertIsInstance(a_work(Nothing()).builder(UPLOADER), P.TargetBuild)
        self.assertIsInstance(a_work().builder(LOCAL), P.LocalPage)

    def test_nothing_arrives_with_arguments_already_bound(self):
        """The specific mistake, named so it cannot come back quietly: a
        partial over ctx passes the arity check against one argument and fails
        against the two the call site really passes."""
        for strategy, builder in self._builders():
            with self.subTest(strategy=strategy.value):
                self.assertFalse(getattr(builder.build, "args", ()))


class TestThePublisherFitsItsCallSite(unittest.TestCase):
    """Item 7 asks its publisher two things, and both are asked by name."""

    def _publishers(self):
        return ((UPLOADER, a_work(Nothing()).publisher(UPLOADER)),
                (LOCAL, a_work().publisher(LOCAL)))

    def test_every_publisher_answers_the_whole_call_site(self):
        for strategy, publisher in self._publishers():
            with self.subTest(strategy=strategy.value):
                inspect.signature(publisher.why_not).bind(A_WORLD)
                inspect.signature(publisher.run).bind(A_WORLD)

    def test_the_local_edition_installs_one_that_refuses_by_configuration(self):
        self.assertIsInstance(a_work().publisher(LOCAL), P.NoPublisher)
        self.assertTrue(a_work().publisher(LOCAL).why_not(A_WORLD))


class TestTheUiHandedOverIsTheRealOne(unittest.TestCase):
    """An implementation is handed pipeline.Console and told it is a Ui. If the
    two ever drift, every target that used the nice output breaks at once."""

    def test_console_is_a_ui(self):
        self.assertIsInstance(A_UI, U.Ui)

    def test_it_answers_every_method_the_interface_promises(self):
        for name in ("say", "warn", "run"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(A_UI, name, None)))

    def test_run_accepts_what_the_interface_documents(self):
        inspect.signature(A_UI.run).bind(["true"], Path("/tmp"), "a label")

    def test_the_progress_hook_the_console_offers_is_the_one_declared(self):
        """Console has always taken a `parser`; the ABC did not declare it, so
        an implementation reading the contract would never know it could turn
        its spinner into a bar. Two shapes for one call is how undocumented
        behaviour becomes load-bearing."""
        self.assertEqual(list(inspect.signature(A_UI.run).parameters),
                         list(inspect.signature(U.Ui.run).parameters)[1:])


if __name__ == "__main__":
    unittest.main()
