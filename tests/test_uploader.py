#!/usr/bin/env python3
"""The seam's promises: an answer that is not an answer permits nothing, and a
configured plugin that will not load stops the tool.

Both are about the same failure — a plugin that cannot answer must never look
like one that answered YES. Item 9 erases the only local copy of footage on the
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
sys.path.insert(0, str(REPO / "src"))

from dashcam_exporter import menu as M, uploader as U, world as W  # noqa: E402

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
        from dashcam_exporter import pipeline as P

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
        facts = self.P._answered(_a_ctx(_Raises()), "import", ("trip_A",))
        self.assertIs(facts.complete, M.Evidence.UNKNOWN)
        self.assertIn("Raises", facts.note)
        self.assertIn("the shelf fell over", facts.note)

    def test_a_menu_draw_asks_like_any_other_capture(self):
        """RESTATED. This asserted that LOCAL scope skips the question and
        answers "not asked".

        Scope no longer gates it. The exporter does not get to decide, on a
        guess about somebody else's code, when going to look is too expensive
        — it cannot see what is behind the interface, and deciding not to ask
        made the menu offer items that refused when picked. Caching belongs to
        the implementation, which knows what its destination costs and can
        invalidate on its own upload.

        So at either scope the answer is the PLUGIN'S. _Silent was only ever
        silent because nothing asked it.
        """
        for scope in (M.Scope.LOCAL, M.Scope.FULL):
            with self.subTest(scope=scope.value):
                facts = self.P._asked(_a_ctx(_Silent()), scope, "import",
                                      ("trip_A",))
                self.assertIs(facts.complete, M.Evidence.YES)
                self.assertNotIn("not asked", facts.note)

    def test_the_answer_carries_the_import_it_is_about(self):
        """An answer is about ONE import's trips, and several imports can sit
        under one <out>. Read without its scope a yes about this round clears
        last round's renders, which nobody was asked about — so the scope
        travels with the answer rather than being inferred by each reader.

        The empty case is the one that bites: with no import settled on, the
        trip list handed over is empty and an implementation that folds an
        empty list to YES is answering about nothing at all.
        """
        ctx = _a_ctx(_Silent())
        self.assertEqual(
            self.P._target_facts(ctx, M.Scope.FULL, Path("/w/2026-07-28"),
                                 ("trip_A",)).namespace, "2026-07-28")
        self.assertEqual(
            self.P._target_facts(ctx, M.Scope.FULL, None, ()).namespace, "")

    def test_offline_does_not_stop_a_configured_plugin_being_asked(self):
        """`offline` is a hint FOR the plugin, not an answer about it.

        It used to short-circuit to NA, and NA is not a weaker yes -- it is the
        answer meaning there is no destination at all, and the workspace rule
        drops its gate entirely on it. So a setting config.txt documented as
        inert ("nothing in this repo reads it") turned a fail-closed delete
        gate into one that was never asked. Whether the destination is
        reachable is the plugin's question; it is handed `offline` on its
        Workspace and can answer UNKNOWN itself, which blocks.
        """
        ctx = _a_ctx(_Silent())
        ctx.offline = True
        facts = self.P._target_facts(ctx, M.Scope.FULL, Path("/w/2026-07-28"),
                                     ("trip_A",))
        self.assertIsNot(facts.complete, M.Evidence.NA,
                         "offline dropped the destination gate instead of asking")
        self.assertEqual(facts.complete,
                         self.P._target_facts(_a_ctx(_Silent()), M.Scope.FULL,
                                              Path("/w/2026-07-28"),
                                              ("trip_A",)).complete)


class _Silent(U.Uploader):
    def describe(self):
        return "a test's uploader"

    def evaluate(self, workspace):
        return U.go()

    def execute(self, workspace, includeVideos=False):
        return U.did("nothing")

    def is_complete(self, trip_ids):
        return M.Evidence.YES


class _Raises(_Silent):
    def is_complete(self, trip_ids):
        raise RuntimeError("the shelf fell over")


def _a_ctx(uploader=None):
    from dashcam_exporter import pipeline as P

    ctx = P.Ctx.__new__(P.Ctx)
    ctx.out_dir = Path("/w/out")
    ctx.offline = False
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
from dashcam_exporter.application.ports.uploader import Builder, Evidence, Uploader, did, go


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

    def execute(self, workspace, includeVideos=False):
        return did("pushed")

    def is_complete(self, trip_ids):
        return Evidence.YES
'''

HALF_DONE = '''
from dashcam_exporter.application.ports.uploader import Builder, Uploader, did, go


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

    def execute(self, workspace, includeVideos=False):
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
        is_complete() raises at the moment item 9 asks — after the operator
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
    the gate table, the banner above the prompt, and the recorded step.
    """

    def setUp(self):
        from dashcam_exporter import pipeline as P

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
            self.P._print_gates(self.P.guards.Gates(world))
        return out.getvalue()

    def test_the_gate_table_names_no_implementation_either(self):
        """RESTATED: it opened with "destination: fortknox (<loader spec>)".

        Which gates can be asked still depends on what is configured, and the
        table still numbers itself against that — [1/2] rather than [1/3] when
        only two can run. What went is the name over the top of it: the gates
        say what was answered, and who answered is this tool's own business.
        """
        said = self._printed(self._world(self.target))
        self.assertNotIn("fortknox", said)
        self.assertNotIn("destination:", said)
        self.assertIn("complete at the destination", said)

    def test_with_nothing_configured_there_is_nobody_to_name(self):
        """Unchanged in effect, and now true of both editions alike."""
        self.assertNotIn("destination:", self._printed(self._world(W.TargetFacts())))

    def test_the_banner_names_no_plugin_at_all(self):
        """RESTATED: it used to name it, and the loader spec with it.

        The screen above an irreversible delete carried the plugin's name
        three times — over the gate table, in "the copies <name> holds stay",
        and in "proceeding on <name> (<spec>)'s answer". Repeating a third
        party's name over footage about to go reads as the tool laying the
        decision off on it, and the decision is the operator's.

        Attribution is not lost: _swept_on() puts it in the ledger's note for
        that sweep, which is a file and outlives the session. A screen is read
        once and scrolls.
        """
        lines = self.P._clean_banner(
            _a_ctx(), self.P.guards.Gates(self._world(self.target)),
            Path("/w/import"), 1024)
        said = "\n".join(lines)
        self.assertNotIn("fortknox", said)
        self.assertNotIn("up.py:Build:FortKnox", said)
        self.assertIn("the raw clips do not come back", said)

    def test_an_unconfigured_install_gets_the_unverified_banner_instead(self):
        """Nothing was asked, so nothing is attributed — and the sentence that
        replaces it is the stronger one: these renders are the only copy."""
        lines = self.P._clean_banner(
            _a_ctx(), self.P.guards.Gates(self._world(W.TargetFacts())),
            Path("/w/import"), 1024)
        said = "\n".join(lines)
        self.assertIn("NOT verified", said)
        self.assertNotIn("Proceeding on", said)

    def test_the_summary_line_does_not_attribute_the_erase(self):
        """REMOVED: the exit summary used to carry "(on <name> (<spec>)\'s
        answer)".

        Two reasons it went. It named the plugin on a DISCARD, where the
        destination was not asked and had no part in the decision -- an
        attribution that is simply false. And on a sweep it printed the whole
        loader spec, path and both class names, which is most of a line to say
        one thing. The banner still names the origin on screen at the moment
        the word is typed, which is where the decision is actually made.
        """
        self.assertFalse(hasattr(self.P, "_on_whose_word"))


class TestTheToolStopsRatherThanDegrading(unittest.TestCase):
    """A configured plugin that will not load must not become the local
    edition by accident.

    Silently becoming local is how someone's renders quietly stop being
    published: the menu looks normal, item 5 writes a local page, and item 8
    refuses for a reason that reads like a network problem.
    """

    def test_ctx_construction_raises_rather_than_returning_no_plugin(self):
        from dashcam_exporter import pipeline as P

        with self.assertRaises(U.UploaderNotLoaded):
            P._loaded_plugin("/no/such/file.py:Nope:Nope", REPO)

    def test_an_unset_setting_is_the_local_edition_on_purpose(self):
        from dashcam_exporter import pipeline as P

        self.assertIsNone(P._loaded_plugin("", REPO))
        self.assertIsNone(P._loaded_plugin(None, REPO))


class TestTheExportRecordIsWrittenDownAndStaysWrittenDown(unittest.TestCase):
    """Which trips the built-in export has copied out, ever.

    It exists because the answer cannot be recovered by looking. The built-in
    destination is a directory the operator named -- ordinarily a stick or an
    external disk -- and the ordinary state of one of those is unplugged and in
    a drawer, so a gate that went back to look would read the empty slot, or a
    DIFFERENT disk mounted at the same path, as though the copy never took
    place. That is the same reason the import manifest exists, and it lives in
    the same ledger, which survives Clean Workspace: a record erased by the
    step it authorises would be no record at all.
    """

    def setUp(self):
        from dashcam_exporter import pipeline as P

        self.P = P
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = P.Ctx.__new__(P.Ctx)
        self.ctx.state_dir = Path(self.tmp.name) / "state"
        self.record = P.ExportRecord(self.ctx)

    def test_a_fresh_ledger_has_exported_nothing(self):
        """Empty rather than absent, and never an error. No record means no
        trip has been copied out, which refuses an erase -- so this is the
        reading everything else rests on."""
        self.assertEqual(self.record.exported(), set())

    def test_marking_unions_rather_than_replacing(self):
        """Two exports of two different days is the ordinary case: the operator
        copies out one import, renders another, copies that. A mark that
        replaced would drop the first import's trips out of the record, and
        Clean Workspace would then refuse on footage already on the disk."""
        self.record.mark(("trip_A",))
        self.record.mark(("trip_B",))
        self.assertEqual(self.record.exported(), {"trip_A", "trip_B"})

    def test_marking_nothing_leaves_the_record_alone(self):
        """A pages-only run reaches mark() with an empty set, and must not
        rewrite the ledger to say so."""
        self.record.mark(("trip_A",))
        self.record.mark(())
        self.assertEqual(self.record.exported(), {"trip_A"})

    def test_it_survives_a_re_read_by_a_record_built_later(self):
        """The point of it being durable. The session that copied the files out
        is long gone by the time item 9 asks, so the answer has to come off the
        disk rather than out of an object somebody kept."""
        self.record.mark(("trip_A",))
        self.assertEqual(self.P.ExportRecord(self.ctx).exported(), {"trip_A"})

    def test_it_shares_the_ledger_without_disturbing_the_import_manifest(self):
        """One file, two records. The import manifest is what says a card's
        clips are safely on this machine, and losing it to an export mark would
        make an imported card unerasable -- so the merge is per key, not a
        rewrite of the file."""
        self.P.record_imported_stamps(self.ctx, ("20260728085700",))
        self.P.write_ledger(self.ctx, "20260728085700", note="a test")
        self.record.mark(("trip_A",))
        self.assertEqual(self.P.imported_stamps(self.ctx), {"20260728085700"})
        self.assertEqual(self.P.read_ledger(self.ctx).get("through"),
                         "20260728085700")
        self.assertEqual(self.record.exported(), {"trip_A"})


class TestWhichPublisherAnInstallEndsUpWith(unittest.TestCase):
    """One destination, decided out loud.

    Two publishers disagreeing about where the footage went is how the only
    copy gets erased, so `upload_plugin` WINS whenever both are configured and
    the built-in is not installed at all -- not installed and ignored, not
    installed as a second opinion. The regression that matters more is the
    other end: an install that configures NEITHER must be the local edition it
    has always been, with item 8 switched off.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.dir / "src").mkdir()
        # The example plugin reads these, and a real environment must not reach
        # into the test either way: website_export_dir is a PRIVATE_KEY, so a
        # SET_WEBSITE_EXPORT_DIR in the operator's shell would otherwise
        # configure an edition the test never asked for.
        for key, sub in (("DASHCAM_LOCAL_SITE_STAGING", "staging"),
                         ("DASHCAM_LOCAL_SITE_DEST", "dest")):
            os.environ[key] = str(self.dir / sub)
            self.addCleanup(os.environ.pop, key, None)
        for key in ("SET_WEBSITE_EXPORT_DIR", "SET_UPLOAD_PLUGIN"):
            if key in os.environ:
                self.addCleanup(os.environ.__setitem__, key, os.environ[key])
                del os.environ[key]

    def ctx(self, **settings):
        from dashcam_exporter.application.ports.checkout import FakeCheckout
        from dashcam_exporter import pipeline as P

        lines = ["workspace=%s/ws" % self.dir]
        lines += ["%s=%s" % (k, v) for k, v in settings.items()]
        (self.dir / "config.txt").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")
        return P.Ctx(FakeCheckout(self.dir))

    def test_a_destination_alone_installs_the_built_in_export(self):
        from dashcam_exporter.infrastructure.plugin import ExportPlugin

        ctx = self.ctx(website_export_dir=str(self.dir / "stick"))
        self.assertIsInstance(ctx.plugin, ExportPlugin)
        self.assertEqual(ctx.plugin.uploader.destination, self.dir / "stick")
        self.assertFalse(ctx.export_dir_ignored)
        self.assertIs(M.Strategy.of(ctx.plugin), M.Strategy.UPLOADER)

    def test_the_old_name_for_the_setting_still_installs_it(self):
        """`default_website_export_dir` is the name the setting shipped under.
        A config written before it was renamed must not stop working."""
        from dashcam_exporter.infrastructure.plugin import ExportPlugin

        ctx = self.ctx(default_website_export_dir=str(self.dir / "stick"))
        self.assertIsInstance(ctx.plugin, ExportPlugin)
        self.assertEqual(ctx.plugin.uploader.destination, self.dir / "stick")

    def test_a_configured_plugin_wins_and_the_built_in_is_not_installed(self):
        """Both set. The plugin owns the destination and can be asked about it;
        the built-in would answer from a record about a different place
        entirely, and two answers about where the footage went is exactly what
        must not exist."""
        from dashcam_exporter.infrastructure.plugin import ExportPlugin

        ctx = self.ctx(upload_plugin=EXAMPLE_SPEC,
                       website_export_dir=str(self.dir / "stick"))
        self.assertNotIsInstance(ctx.plugin, ExportPlugin)
        self.assertEqual(ctx.plugin.name, "LocalWebSiteUploader")
        # Read back, not thrown away: the tool says at startup that it is
        # ignoring the setting rather than resolving it quietly.
        self.assertTrue(ctx.export_dir_ignored)

    def test_the_render_output_tree_is_a_different_setting_entirely(self):
        """`export_dir` has meant "where the renders go" since long before any
        of this, and the two names are one character apart in conversation. It
        must not install a publisher, or every existing install would silently
        acquire an item 8 that copies footage into its own output tree."""
        ctx = self.ctx(export_dir=str(self.dir / "renders"))
        self.assertIsNone(ctx.plugin)
        self.assertEqual(ctx.out_dir, self.dir / "renders")
        self.assertIsNone(ctx.website_export_dir)

    def test_with_neither_configured_the_edition_is_unchanged(self):
        """The regression that matters most. A fresh clone has nowhere to put
        anything, which is why item 8 is switched off rather than refusing when
        picked -- and adding the built-in export must leave that exactly as it
        was."""
        from dashcam_exporter import pipeline as P

        ctx = self.ctx()
        self.assertIsNone(ctx.plugin)
        self.assertIsNone(ctx.website_export_dir)
        self.assertIs(M.Strategy.of(ctx.plugin), M.Strategy.LOCAL_PAGE)
        built = M.build_menu(M.Strategy.LOCAL_PAGE, P.Work(ctx))
        upload = built[M.UPLOAD]
        self.assertEqual(upload.inbound().edges(), frozenset(),
                         "publishing has no place in the local edition")
        self.assertEqual(upload.outbound().offers(frozenset(built)), frozenset())


if __name__ == "__main__":
    unittest.main()
