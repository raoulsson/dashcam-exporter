#!/usr/bin/env python3
"""The collaborators item bodies are handed must fit the call they are handed to.

The other test files ask whether the graph is right (test_step_graph), what the
guards allow (test_guards), what the pipeline does with an item's answer
(test_pipeline, on mocks) and which paths exist (test_paths). None of them can
catch a collaborator that is built with one shape and called with another,
because none of them calls the real body: the mock files deliberately run
nothing real, and the path files stop at the item boundary.

That gap let item 5 ship crashing on every single run under both strategies.
The constructor bound ctx into the gatherer, build_result_page passed ctx again,
and a two-argument function got three. Two hundred and seventy-one tests were
green over it.

The gap got WIDER when the publishing half became an interface. There is a
surface an outsider implements and this repo calls, and the call sites are
spread across capture_world, item 5 and item 7. So the question is asked here
for every method on the interface, and the table of call sites is checked
against the abstract methods of Builder and Uploader — a method added later
fails this file until someone writes down where it is called from. Binding the
signature is enough and is side-effect free: the bug class is an arity
mismatch, and no footage needs to move to prove it.
"""

import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.argv = ["test_wiring"]

from dashcam_exporter import menu as M                              # noqa: E402
from dashcam_exporter import pipeline as P                          # noqa: E402
from dashcam_exporter import uploader as U, world as W               # noqa: E402

UPLOADER, LOCAL = M.Strategy.UPLOADER, M.Strategy.LOCAL_PAGE


class NothingBuilt(U.Builder):
    """A builder whose bodies are as empty as the ABC permits.

    Only its SHAPE is under test here. What the answers mean is
    test_uploader.py's subject and what the guards do with them is
    test_guards.py's.
    """

    def describe(self):
        return "a wiring test's builder"

    def evaluate(self, workspace):
        return U.go()

    def execute(self, workspace):
        return U.did("nothing")


class NothingSent(U.Uploader):
    def describe(self):
        return "a wiring test's uploader"

    def evaluate(self, workspace):
        return U.go()

    def execute(self, workspace):
        return U.did("nothing")

    def is_complete(self, trip_ids):
        return M.Evidence.UNKNOWN     # a wiring test does not go and look


def a_plugin():
    return U.Plugin(NothingBuilt(), NothingSent(), "/a/wiring/test.py:B:U")


def a_ctx(plugin=None):
    """Enough of a Ctx for the collaborator factories: the plugin, and
    nothing that would make anything happen."""
    ctx = P.Ctx.__new__(P.Ctx)
    ctx.plugin = plugin
    return ctx


def a_work(plugin=None):
    work = P.Work.__new__(P.Work)
    work.ctx = a_ctx(plugin)
    return work


# ---------------------------------------------------------------------------
# The interface against its real call sites
# ---------------------------------------------------------------------------

A_WORLD = W.World()
TRIP_IDS = ("trip_2026-07-28_08-57_01",)
A_UI = P.Console(a_ctx())
A_WORKSPACE = U.Workspace(trip_ids=TRIP_IDS, ui=A_UI)

# What each method is really called with, written where it can be checked.
# Read it as documentation of the boundary: the left column is everything the
# exporter asks an outsider, the right column is all it ever hands over.
CALL_SITES = {
    # pipeline.TargetBuild/TargetPublish.describe, for the menu row, and
    # pipeline._target_status once at launch
    "describe": (),
    # pipeline.TargetBuild/TargetPublish.evaluate, from item 5's and item 7's
    # evaluate, on every menu draw
    "evaluate": (A_WORKSPACE,),
    # pipeline.TargetBuild/TargetPublish.execute, from the items' _perform
    "execute": (A_WORKSPACE,),
}

# The uploader's one extra question, asked by pipeline._answered inside
# capture_world at FULL scope.
UPLOADER_CALL_SITES = dict(CALL_SITES, is_complete=(TRIP_IDS,))


class TestEveryInterfaceMethodHasAKnownCallSite(unittest.TestCase):
    """The tables above are the boundary, stated once.

    Their value is that they go stale loudly: add a method to an act and this
    fails until the table says where the exporter calls it from, which is the
    moment to notice that nothing calls it at all.
    """

    def _no_orphans(self, cls, table):
        self.assertEqual(cls.__abstractmethods__ - set(table), set(),
                         "%s grew a method with no call site written down" % cls.__name__)

    def test_the_builder_declares_nothing_the_exporter_does_not_call(self):
        self._no_orphans(U.Builder, CALL_SITES)

    def test_the_uploader_declares_nothing_the_exporter_does_not_call(self):
        self._no_orphans(U.Uploader, UPLOADER_CALL_SITES)

    def test_no_entry_in_the_table_is_absent_from_the_interface(self):
        for name in UPLOADER_CALL_SITES:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(U.Uploader, name, None)),
                                "%s is called but the interface does not declare it" % name)


class TestTheInterfaceAcceptsWhatItsCallSitesPass(unittest.TestCase):
    """Signature binding, method by method, against the real arguments.

    Against the ABCs AND against the shipped example, because a subclass is
    free to narrow a signature and only the subclass is what actually runs.
    """

    def _bind_all(self, act, table):
        for name, args in table.items():
            with self.subTest(method=name):
                inspect.signature(getattr(act, name)).bind(*args)

    def test_the_declared_interface_binds(self):
        self._bind_all(NothingBuilt(), CALL_SITES)
        self._bind_all(NothingSent(), UPLOADER_CALL_SITES)

    def test_the_shipped_example_binds(self):
        spec = ("%s:LocalWebSiteBuilderPlugin:LocalWebSiteUploader"
                % (REPO / "examples" / "local_website.py"))
        plugin = U.load_plugin(spec, REPO)
        self._bind_all(plugin.builder, CALL_SITES)
        self._bind_all(plugin.uploader, UPLOADER_CALL_SITES)


# ---------------------------------------------------------------------------
# The collaborators the item constructors install
# ---------------------------------------------------------------------------

class TestTheBuilderFitsItsCallSite(unittest.TestCase):
    """Item 5 asks its builder three things, all by name.

    There is no gatherer any more: only the MOVER used to be the strategy
    branch, so the page writer ran under both editions and a publishing install
    got a local page announcing that nothing had left the machine. The whole
    body is the branch now, which is why what is checked here is a builder.
    """

    def _builders(self):
        return ((UPLOADER, a_work(a_plugin()).builder(UPLOADER)),
                (LOCAL, a_work().builder(LOCAL)))

    def test_every_builder_answers_the_whole_call_site(self):
        for strategy, builder in self._builders():
            with self.subTest(strategy=strategy.value):
                inspect.signature(builder.describe).bind()
                inspect.signature(builder.evaluate).bind(A_WORLD)
                inspect.signature(builder.execute).bind(A_WORLD)

    def test_the_edition_decides_which_builder_is_installed(self):
        """The defect being fixed, stated as a property of the wiring: under a
        configured plugin the local page's writer is not installed at all,
        so it cannot run and cannot claim nothing left this machine."""
        self.assertIsInstance(a_work(a_plugin()).builder(UPLOADER), P.TargetBuild)
        self.assertIsInstance(a_work().builder(LOCAL), P.LocalPage)

    def test_nothing_arrives_with_arguments_already_bound(self):
        """The specific mistake, named so it cannot come back quietly: a
        partial over ctx passes the arity check against one argument and fails
        against the two the call site really passes."""
        for strategy, builder in self._builders():
            with self.subTest(strategy=strategy.value):
                self.assertFalse(getattr(builder.execute, "args", ()))


class TestThePublisherFitsItsCallSite(unittest.TestCase):
    """Item 7 asks its publisher the same three things item 5 asks its
    builder, which is what "one act, twice" means at the wiring."""

    def _publishers(self):
        return ((UPLOADER, a_work(a_plugin()).publisher(UPLOADER)),
                (LOCAL, a_work().publisher(LOCAL)))

    def test_every_publisher_answers_the_whole_call_site(self):
        for strategy, publisher in self._publishers():
            with self.subTest(strategy=strategy.value):
                inspect.signature(publisher.describe).bind()
                inspect.signature(publisher.evaluate).bind(A_WORLD)
                inspect.signature(publisher.execute).bind(A_WORLD)

    def test_the_local_edition_installs_one_that_refuses_by_configuration(self):
        self.assertIsInstance(a_work().publisher(LOCAL), P.NoPublisher)
        self.assertTrue(a_work().publisher(LOCAL).evaluate(A_WORLD).blocked)


# What items 5 and 7 really ask their collaborator, written where it can be
# checked, exactly as CALL_SITES above does for the plugin-facing interface.
# The left column is everything the exporter asks its own publishing halves;
# the right column is all it ever hands over.
COLLABORATOR_CALL_SITES = {
    # items.BuildWebsite.description and items.UploadWebsite.description, for
    # the menu row, on every draw
    "describe": (),
    # items.BuildWebsite.evaluate and items.UploadWebsite._still_owed
    "evaluate": (A_WORLD,),
    # both items' _perform
    "execute": (A_WORLD,),
    # handover.Handover._long_description, reached from both items' about(),
    # for h 5 and h 7
    "get_website_upload_description": (),
    # handover.Handover._plugin_name, to attribute the paragraph above
    "plugin_name": (),
}


def every_collaborator():
    """All four of them, built the way the item constructors build them."""
    return ((UPLOADER, "builder", a_work(a_plugin()).builder(UPLOADER)),
            (LOCAL, "builder", a_work().builder(LOCAL)),
            (UPLOADER, "publisher", a_work(a_plugin()).publisher(UPLOADER)),
            (LOCAL, "publisher", a_work().publisher(LOCAL)))


class TestEveryCollaboratorIsDeclaredAndNotMerelyShaped(unittest.TestCase):
    """The seam items 5 and 7 install, declared instead of hoped for.

    This is the file the last one of these got past. A long description was
    added to what the items ask their collaborator, none of the four real
    collaborators had it, and both help screens raised AttributeError on every
    installation -- while a test stub that DID have it kept the suite green.
    Subclassing is what turns that into a TypeError at construction, and these
    are the assertions that keep the subclassing there.
    """

    def test_every_collaborator_is_an_instance_of_the_seam(self):
        for strategy, half, act in every_collaborator():
            with self.subTest(strategy=strategy.value, half=half):
                self.assertIsInstance(act, P.PublishingCollaborator)

    def test_the_seam_declares_nothing_the_items_do_not_call(self):
        """The table above goes stale loudly. Add a method to the seam and this
        fails until someone writes down where items.py calls it from, which is
        the moment to notice whether anything calls it at all."""
        self.assertEqual(
            P.PublishingCollaborator.__abstractmethods__
            - set(COLLABORATOR_CALL_SITES), set(),
            "the seam grew a method with no call site written down")

    def test_no_entry_in_the_table_is_absent_from_the_seam(self):
        for name in COLLABORATOR_CALL_SITES:
            with self.subTest(method=name):
                self.assertIn(name, P.PublishingCollaborator.__abstractmethods__,
                              "%s is called but the seam does not declare it" % name)

    def test_every_collaborator_binds_every_call_site(self):
        for strategy, half, act in every_collaborator():
            for name, args in COLLABORATOR_CALL_SITES.items():
                with self.subTest(strategy=strategy.value, half=half, method=name):
                    inspect.signature(getattr(act, name)).bind(*args)

    def test_a_half_that_misses_a_method_cannot_be_constructed(self):
        """The guarantee itself, stated once. A future method added to the seam
        stops the tool where the operator can see it -- at construction, before
        a menu is drawn -- rather than under the key he pressed."""
        class Forgetful(P.PublishingCollaborator):
            def describe(self):
                return "a half that forgot the rest"

        with self.assertRaises(TypeError) as caught:
            Forgetful()
        self.assertIn("evaluate", str(caught.exception))

    def test_a_collaborator_is_not_offered_as_a_plugin_act(self):
        """The two seams are separate on purpose. An Act is handed a Workspace
        and a collaborator is handed a World, so a collaborator that also
        claimed to be a Builder would sail through load_plugin's shape check
        and then be handed the one argument it cannot read."""
        for strategy, half, act in every_collaborator():
            with self.subTest(strategy=strategy.value, half=half):
                self.assertNotIsInstance(act, U.Act)


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
