#!/usr/bin/env python3
"""The seam's promises: an answer that is not an answer permits nothing, and a
configured plugin that will not load stops the tool.

Both are about the same failure — a plugin that cannot answer must never look
like one that answered YES. Item 8 erases the only local copy of footage on the
strength of is_complete(), so "could not ask" arriving as YES or NA is the one
reading that must be unwritable rather than merely unwritten.

RESTATED for the narrowed interface. This file used to pin the fail-closed
rules of Answers and Owed — an unmentioned render reads UNKNOWN, one UNKNOWN
sinks a set, a failed listing owes everything. Those types are gone with the
per-render map they carried: there is one all-or-nothing answer now, so there
is no set to aggregate and no name to leave out. What survives is the rule
underneath them, asked of the one answer that is left, plus the exporter's own
half — it reads anything that is not an Evidence as UNKNOWN rather than
believing it.
"""

import io
import os
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import menu as M                    # noqa: E402
import uploader as U                # noqa: E402
import world as W                   # noqa: E402

EXAMPLE = REPO / "examples" / "local_website.py"
EXAMPLE_SPEC = "%s:LocalWebSiteBuilderPlugin:LocalWebSiteUploader" % EXAMPLE


# ---------------------------------------------------------------------------
# The exporter's half of the fail-closed rule
# ---------------------------------------------------------------------------

class TestAnAnswerThatIsNotAnAnswerPermitsNothing(unittest.TestCase):
    """is_complete() returns one Evidence, and the exporter believes it — but
    only if it IS one.

    A plugin that returns True, "yes" or None is not lying; it is broken. The
    difference matters because the value is compared against Evidence.YES by a
    guard that erases footage, and `True is Evidence.YES` is False while
    `"yes" == ...` is a coin toss for whoever writes the next comparison.
    """

    def setUp(self):
        import pipeline as P

        self.P = P

    def test_anything_that_is_not_an_evidence_reads_unknown(self):
        for answer in (True, "yes", 1, None, object()):
            with self.subTest(answer=answer):
                self.assertIs(self.P._an_evidence(answer), M.Evidence.UNKNOWN)

    def test_a_real_evidence_passes_through_untouched(self):
        for e in M.Evidence:
            with self.subTest(evidence=e.value):
                self.assertIs(self.P._an_evidence(e), e)

    def test_a_plugin_that_raises_reads_unknown_and_says_who(self):
        """An exception is not a thing the plugin said. It has to read as
        unreachable, and the note is what tells the operator afterwards that
        nobody could ask rather than that the answer was no."""
        facts = self.P._answered(_a_ctx(_Raises()), ("trip_A",))
        self.assertIs(facts.complete, M.Evidence.UNKNOWN)
        self.assertIn("Raises", facts.note)
        self.assertIn("the shelf fell over", facts.note)

    def test_a_menu_draw_does_not_ask_and_says_so(self):
        """LOCAL scope reads UNKNOWN, which no guard treats as proven, and the
        note distinguishes it from a destination that was asked and could not
        say."""
        facts = self.P._asked(_a_ctx(_Silent()), M.Scope.LOCAL, ("trip_A",))
        self.assertIs(facts.complete, M.Evidence.UNKNOWN)
        self.assertIn("not asked", facts.note)


class _Silent(U.Uploader):
    def describe(self):
        return "a test's uploader"

    def evaluate(self, workspace):
        return U.go()

    def execute(self, workspace):
        return U.did("nothing")

    def is_complete(self, trip_ids):
        return M.Evidence.YES


class _Raises(_Silent):
    def is_complete(self, trip_ids):
        raise RuntimeError("the shelf fell over")


def _a_ctx(uploader=None):
    import pipeline as P

    ctx = P.Ctx.__new__(P.Ctx)
    ctx.out_dir = Path("/w/out")
    if uploader is not None:
        ctx.plugin = U.Plugin(_A_BUILDER, uploader, "/a/test/plugin.py:B:U")
    return ctx


class _Builder(U.Builder):
    def describe(self):
        return "a test's builder"

    def evaluate(self, workspace):
        return U.go()

    def execute(self, workspace):
        return U.did("nothing")


_A_BUILDER = _Builder()


# ---------------------------------------------------------------------------
# The loader — loud, never a silent fallback
# ---------------------------------------------------------------------------

COMPLETE = '''
from uploader import Builder, Evidence, Uploader, did, go


class Site(Builder):
    def describe(self):
        return "build"

    def evaluate(self, workspace):
        return go()

    def execute(self, workspace):
        return did("built")


class Push(Uploader):
    def describe(self):
        return "push"

    def evaluate(self, workspace):
        return go()

    def execute(self, workspace):
        return did("pushed")

    def is_complete(self, trip_ids):
        return Evidence.YES
'''

HALF_DONE = '''
from uploader import Builder, Uploader, did, go


class Site(Builder):
    def describe(self):
        return "build"

    def evaluate(self, workspace):
        return go()

    def execute(self, workspace):
        return did("built")


class Push(Uploader):
    """Everything but is_complete(). ABC refuses to construct it."""

    def describe(self):
        return "push"

    def evaluate(self, workspace):
        return go()

    def execute(self, workspace):
        return did("pushed")
'''

NOT_A_PLUGIN = "class Site:\n    pass\n\n\nclass Push:\n    pass\n"

EXPLODES = "raise RuntimeError('boom')\n"


class TestTheLoader(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def spec_for(self, source, builder="Site", uploader="Push", name="impl.py"):
        path = self.dir / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return "%s:%s:%s" % (path, builder, uploader)

    def load(self, spec):
        return U.load_plugin(spec, REPO)

    def test_one_plugin_yields_both_acts(self):
        plugin = self.load(self.spec_for(COMPLETE))
        self.assertIsInstance(plugin.builder, U.Builder)
        self.assertIsInstance(plugin.uploader, U.Uploader)

    def test_it_can_import_the_interface_from_a_directory_python_never_heard_of(self):
        """The implementation lives outside this repo and still writes
        `from uploader import ...`. If that stopped working the operator would
        see an import error naming a module he does not own."""
        before = list(sys.path)
        self.load(self.spec_for(COMPLETE))
        self.assertEqual(sys.path, before,
                         "the loader left the exporter shadowing sys.path")

    def test_a_spec_that_names_one_class_is_loud(self):
        """RESTATED: the setting names two classes now, and a spec carried over
        from the one-class shape has to fail at startup rather than half-load.
        """
        with self.assertRaises(U.UploaderNotLoaded) as caught:
            self.load("%s:Push" % (self.dir / "impl.py"))
        self.assertIn("BuilderClass", str(caught.exception))

    def test_a_missing_file_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load("%s:Site:Push" % (self.dir / "nope.py"))

    def test_a_file_without_that_class_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(COMPLETE, uploader="Other"))

    def test_the_two_classes_must_be_the_two_kinds_of_act(self):
        """A builder where an uploader belongs is a plugin whose destination
        nobody can ask about, and it must not load."""
        with self.assertRaises(U.UploaderNotLoaded) as caught:
            self.load(self.spec_for(COMPLETE, uploader="Site"))
        self.assertIn("Uploader", str(caught.exception))

    def test_a_class_that_implements_neither_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(NOT_A_PLUGIN))

    def test_a_module_that_raises_on_import_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(EXPLODES))

    def test_a_half_implemented_uploader_fails_at_startup_not_mid_erase(self):
        """The one that matters: without this, an uploader missing
        is_complete() raises at the moment item 8 asks — after the operator
        typed CLEAN."""
        with self.assertRaises(U.UploaderNotLoaded) as caught:
            self.load(self.spec_for(HALF_DONE))
        self.assertIn("is_complete", str(caught.exception))

    def test_the_name_is_the_uploader_class_so_attribution_always_works(self):
        """Derived, not asked for. A NAME the implementation supplies is a
        second place to say what the class already says."""
        self.assertEqual(self.load(self.spec_for(COMPLETE)).name, "Push")

    def test_the_origin_names_the_implementation_and_where_it_came_from(self):
        spec = self.spec_for(COMPLETE)
        origin = self.load(spec).origin
        self.assertIn("Push", origin)
        self.assertIn(spec, origin)


class TestTheShippedExample(unittest.TestCase):
    """The worked example is driven, not merely shipped.

    An example nobody runs rots into a lie, and this one is the documentation
    for how to implement the interface — the first thing anybody copies.
    tests/test_seam.py runs the real menu items through it; this file calls it
    directly, which is where the already-done path is cheapest to state.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for key, sub in (("DASHCAM_LOCAL_SITE_STAGING", "staging"),
                         ("DASHCAM_LOCAL_SITE_DEST", "dest")):
            os.environ[key] = str(self.dir / sub)
            self.addCleanup(os.environ.pop, key, None)
        self.plugin = U.load_plugin(EXAMPLE_SPEC, REPO)

    def a_workspace(self, trips=("trip_A",)):
        out = self.dir / "out"
        out.mkdir(exist_ok=True)
        renders = tuple(map(self._render, trips))
        return U.Workspace(out_dir=out, renders=renders, trip_ids=tuple(trips),
                           ui=SilentUi())

    def _render(self, trip):
        path = self.dir / "out" / (trip + "_h1080.mp4")
        path.write_bytes(b"x" * 10)
        return W.Render(path.name, 10, path)

    def test_the_builder_copies_and_never_moves_what_it_was_handed(self):
        """The condition of trust, as a property of a run. The exporter's erase
        gates reason about these exact files, so a builder that moved one would
        leave them judging a workspace that shifted underneath."""
        workspace = self.a_workspace()
        before = sorted(p.name for p in workspace.out_dir.rglob("*"))
        self.plugin.builder.execute(workspace)
        self.assertEqual(sorted(p.name for p in workspace.out_dir.rglob("*")),
                         before, "the example modified the exporter's workspace")

    def test_the_second_run_is_satisfied_and_does_nothing(self):
        """The path an implementor gets wrong first. Once the staged site is at
        the destination the act is SATISFIED, which completes the item without
        doing the work again — an exporter that re-ran it would push the whole
        set on every keypress."""
        workspace = self.a_workspace()
        self.plugin.builder.execute(workspace)
        self.assertIs(self.plugin.uploader.evaluate(workspace).ruling, M.Ruling.GO)
        self.plugin.uploader.execute(workspace)
        self.assertIs(self.plugin.uploader.evaluate(workspace).ruling,
                      M.Ruling.SATISFIED)

    def test_is_complete_is_no_until_the_upload_and_yes_after(self):
        workspace = self.a_workspace()
        # An empty destination is a destination that ANSWERED: nothing is
        # there, which is NO. One that cannot be listed at all is the other
        # test below, and it says UNKNOWN.
        Path(os.environ["DASHCAM_LOCAL_SITE_DEST"]).mkdir()
        self.assertIs(self.plugin.uploader.is_complete(("trip_A",)), M.Evidence.NO)
        self.plugin.builder.execute(workspace)
        self.plugin.uploader.execute(workspace)
        self.assertIs(self.plugin.uploader.is_complete(("trip_A",)), M.Evidence.YES)

    def test_a_trip_that_was_never_rendered_makes_it_no(self):
        """All or nothing, and this is what makes that safe. The exporter asks
        about every trip of the import, including one that produced no render;
        the destination does not have it, so the honest answer is NO and its
        footage is not erased."""
        workspace = self.a_workspace()
        self.plugin.builder.execute(workspace)
        self.plugin.uploader.execute(workspace)
        self.assertIs(self.plugin.uploader.is_complete(("trip_A", "trip_never")),
                      M.Evidence.NO)

    def test_an_unreachable_destination_says_unknown_not_no(self):
        """The reading the whole discipline exists for. A destination that
        cannot be listed has not said the trips are absent — and UNKNOWN fails
        closed while NO merely refuses."""
        os.environ["DASHCAM_LOCAL_SITE_DEST"] = str(self.dir / "no" / "such" / "dir")
        self.assertIs(U.load_plugin(EXAMPLE_SPEC, REPO).uploader.is_complete(("trip_A",)),
                      M.Evidence.UNKNOWN)

    def test_a_dropped_trip_is_left_out_of_the_rebuilt_index(self):
        """The fact that used to be a call into the plugin at drop time. As
        state on the workspace it reaches a build that happens next week, and a
        plugin installed after the drop."""
        workspace = self.a_workspace(trips=("trip_A", "trip_B"))
        dropped = U.Workspace(out_dir=workspace.out_dir, renders=workspace.renders,
                              trip_ids=workspace.trip_ids, dropped_ids=("trip_B",),
                              ui=SilentUi())
        self.plugin.builder.execute(dropped)
        index = (Path(os.environ["DASHCAM_LOCAL_SITE_STAGING"]) / "index.html").read_text()
        self.assertIn("trip_A", index)
        self.assertNotIn("trip_B", index)


class SilentUi(U.Ui):
    def say(self, line):
        pass

    def warn(self, line):
        pass

    def run(self, cmd, cwd, label, env=None, parser=None):   # pragma: no cover
        raise AssertionError("the example must not run a subprocess")


class TestTheEraseSaysWhoseAnswerItActedOn(unittest.TestCase):
    """Attribution, which is not a safeguard.

    An implementation is inside the trust boundary and needs no policing; what
    it says is believed. But footage is not recoverable, so when the erase went
    ahead on somebody's answer, WHOSE answer has to be readable off the tool
    rather than reconstructed from memory weeks later. Three places carry it:
    the gate table, the banner above the CLEAN prompt, and the recorded step.
    """

    def setUp(self):
        import pipeline as P

        self.P = P
        self.render = W.Render("trip_A_h1080.mp4", 10)
        self.target = W.TargetFacts(
            configured=True, name="fortknox",
            origin="fortknox (~/dev/mine/up.py:Build:FortKnox)",
            complete=M.Evidence.YES)

    def _world(self, target):
        return W.World(out_dir=Path("/w/out"), renders=(self.render,),
                       renders_here=(self.render,), expected_trips=1,
                       trip_ids=("trip_A",), target=target)

    def _printed(self, world):
        out = io.StringIO()
        with redirect_stdout(out):
            self.P._print_gates(world)
        return out.getvalue()

    def test_the_gate_table_names_the_implementation_being_asked(self):
        self.assertIn("fortknox (~/dev/mine/up.py:Build:FortKnox)",
                      self._printed(self._world(self.target)))

    def test_with_nothing_configured_there_is_nobody_to_name(self):
        """The local edition asks no one, so a "destination:" line would be a
        row about a thing that does not exist."""
        self.assertNotIn("destination:", self._printed(self._world(W.TargetFacts())))

    def test_the_banner_says_whose_answer_the_erase_proceeds_on(self):
        lines = self.P._clean_banner(
            _a_ctx(), self._world(self.target), Path("/w/import"), 1024)
        said = "\n".join(lines)
        self.assertIn("fortknox (~/dev/mine/up.py:Build:FortKnox)", said)
        self.assertIn("Proceeding on", said)

    def test_an_unconfigured_install_gets_the_unverified_banner_instead(self):
        """Nothing was asked, so nothing is attributed — and the sentence that
        replaces it is the stronger one: these renders are the only copy."""
        lines = self.P._clean_banner(
            _a_ctx(), self._world(W.TargetFacts()), Path("/w/import"), 1024)
        said = "\n".join(lines)
        self.assertIn("NOT verified", said)
        self.assertNotIn("Proceeding on", said)

    def test_the_recorded_step_outlives_the_screen(self):
        """A screen is read once, by one person. The question "who said this
        was safe" gets asked after the session is over."""
        self.assertIn("fortknox", self.P._on_whose_word(self.target))
        self.assertEqual(self.P._on_whose_word(W.TargetFacts()), "")


class TestTheToolStopsRatherThanDegrading(unittest.TestCase):
    """A configured plugin that will not load must not become the local
    edition by accident.

    Silently becoming local is how someone's renders quietly stop being
    published: the menu looks normal, item 6 writes a local page, and item 8
    refuses for a reason that reads like a network problem.
    """

    def test_ctx_construction_raises_rather_than_returning_no_plugin(self):
        import pipeline as P

        with self.assertRaises(U.UploaderNotLoaded):
            P._loaded_plugin("/no/such/file.py:Nope:Nope", REPO)

    def test_an_unset_setting_is_the_local_edition_on_purpose(self):
        import pipeline as P

        self.assertIsNone(P._loaded_plugin("", REPO))
        self.assertIsNone(P._loaded_plugin(None, REPO))


if __name__ == "__main__":
    unittest.main()
