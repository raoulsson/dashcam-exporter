#!/usr/bin/env python3
"""The interface's two promises: an unanswered question reads UNKNOWN, and a
configured implementation that will not load stops the tool.

Both are about the same failure — an implementation that cannot answer must
never look like one that answered yes. Item 8 erases the only local copy of
footage on the strength of these answers, so "could not ask" arriving as YES or
NA is the one reading that must be unwritable rather than merely unwritten.
"""

import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import menu as M                    # noqa: E402
import uploader as U                # noqa: E402
import world as W                   # noqa: E402


A = W.Render("trip_A_h1080.mp4", 500)
B = W.Render("trip_B_h1080.mp4", 700)


# ---------------------------------------------------------------------------
# Answers — the fail-closed rules
# ---------------------------------------------------------------------------

class TestAnUnansweredNameIsUnknown(unittest.TestCase):

    def test_a_target_that_could_not_be_reached_says_unknown_about_everything(self):
        answers = U.Answers.unknown("no route to the host")
        self.assertIs(answers.about(A.name), M.Evidence.UNKNOWN)
        self.assertIs(answers.covers((A, B)), M.Evidence.UNKNOWN)

    def test_a_name_the_target_did_not_mention_is_unknown_not_no(self):
        """The target answered about A and said nothing about B. B is not
        "absent" — nobody asked about it, and the difference is whether the
        next step may erase B's only local copy."""
        answers = U.Answers.of({A.name: M.Evidence.YES})
        self.assertIs(answers.about(B.name), M.Evidence.UNKNOWN)

    def test_one_unknown_sinks_the_whole_set(self):
        answers = U.Answers.of({A.name: M.Evidence.YES})
        self.assertIs(answers.covers((A, B)), M.Evidence.UNKNOWN)

    def test_one_no_sinks_a_set_that_is_otherwise_yes(self):
        answers = U.Answers.of({A.name: M.Evidence.YES, B.name: M.Evidence.NO})
        self.assertIs(answers.covers((A, B)), M.Evidence.NO)

    def test_unknown_is_weaker_than_no(self):
        """Least permissive first: a set holding both reads UNKNOWN, because
        "could not find out" is the answer that must not be argued away."""
        answers = U.Answers.of({A.name: M.Evidence.NO, B.name: M.Evidence.UNKNOWN})
        self.assertIs(answers.covers((A, B)), M.Evidence.UNKNOWN)

    def test_everything_yes_is_yes(self):
        answers = U.Answers.of({A.name: M.Evidence.YES, B.name: M.Evidence.YES})
        self.assertIs(answers.covers((A, B)), M.Evidence.YES)

    def test_not_applicable_is_na_and_only_that(self):
        """NA removes the gate. It is reachable only through the constructor
        that says the question is meaningless here, never through a missing
        answer."""
        answers = U.Answers.not_applicable("this target does not serve anything")
        self.assertIs(answers.about(A.name), M.Evidence.NA)
        self.assertIs(answers.covers((A, B)), M.Evidence.NA)

    def test_an_empty_render_set_is_vacuously_yes(self):
        """Safe here and only here: Clean Workspace refuses outright on zero
        renders_here before any of this is consulted."""
        self.assertIs(U.Answers.of({}).covers(()), M.Evidence.YES)

    def test_there_is_no_accessor_that_takes_a_default(self):
        """`answers.get(name, YES)` is the idiom that turns "could not find
        out" into "everything is fine". It must not be writable at all — which
        is why the storage is reached through about()/covers() and nothing in
        the exporter touches .readings."""
        self.assertFalse(hasattr(U.Answers.of({}), "get"))

    def test_the_exporter_never_reads_the_raw_dict(self):
        offenders = [p.name for p in REPO.glob("*.py")
                     if p.name != "uploader.py" and ".readings" in p.read_text()]
        self.assertEqual(offenders, [],
                         "reach for Answers.about()/covers(), never .readings")


class TestOwed(unittest.TestCase):

    def test_a_target_that_could_not_be_asked_owes_everything(self):
        owed = U.Owed.everything((A, B), "listing failed")
        self.assertEqual(owed.names, frozenset({A.name, B.name}))
        self.assertFalse(owed.certain)
        self.assertTrue(owed.any)

    def test_nothing_owed_is_the_settled_answer(self):
        self.assertFalse(U.Owed.nothing().any)
        self.assertTrue(U.Owed.nothing().certain)

    def test_just_names_what_is_left(self):
        self.assertEqual(U.Owed.just([A.name]).names, frozenset({A.name}))


# ---------------------------------------------------------------------------
# The loader — loud, never a silent fallback
# ---------------------------------------------------------------------------

COMPLETE = '''
from uploader import Answers, Owed, Report, WebsiteUploaderInterface


class Target(WebsiteUploaderInterface):
    def build(self, world, ui):
        return Report(True)

    def upload(self, world, ui):
        return Report(True)

    def why_not_build(self, world):
        return None

    def why_not_upload(self, world):
        return None

    def owes(self, renders):
        return Owed.nothing()

    def holds(self, renders):
        return Answers.of({})

    def published(self, renders):
        return Answers.of({})

    def carries(self, trip_ids):
        return Answers.of({})
'''

HALF_DONE = '''
from uploader import WebsiteUploaderInterface


class Target(WebsiteUploaderInterface):
    """Everything but published(). ABC refuses to construct it."""

    def build(self, world, ui):
        return None

    def upload(self, world, ui):
        return None

    def why_not_build(self, world):
        return None

    def why_not_upload(self, world):
        return None

    def owes(self, renders):
        return None

    def holds(self, renders):
        return None

    def carries(self, trip_ids):
        return None
'''

NOT_AN_UPLOADER = "class Target:\n    pass\n"

EXPLODES = "raise RuntimeError('boom')\n"


class TestTheLoader(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def spec_for(self, source, class_name="Target", name="impl.py"):
        path = self.dir / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return "%s:%s" % (path, class_name)

    def load(self, spec):
        return U.load_uploader(spec, REPO)

    def test_a_complete_implementation_loads_and_is_an_instance(self):
        got = self.load(self.spec_for(COMPLETE))
        self.assertIsInstance(got, U.WebsiteUploaderInterface)

    def test_it_can_import_the_interface_from_a_directory_python_never_heard_of(self):
        """The implementation lives outside this repo and still writes
        `from uploader import ...`. If that stopped working the operator would
        see an import error naming a module he does not own."""
        before = list(sys.path)
        self.load(self.spec_for(COMPLETE))
        self.assertEqual(sys.path, before,
                         "the loader left the exporter shadowing sys.path")

    def test_a_missing_file_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load("%s:Target" % (self.dir / "nope.py"))

    def test_a_file_without_that_class_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(COMPLETE, class_name="Other"))

    def test_a_class_that_does_not_implement_the_interface_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(NOT_AN_UPLOADER))

    def test_a_module_that_raises_on_import_is_loud(self):
        with self.assertRaises(U.UploaderNotLoaded):
            self.load(self.spec_for(EXPLODES))

    def test_a_half_implemented_target_fails_at_startup_not_mid_erase(self):
        """The one that matters: without this, a target missing published()
        raises at the moment item 8 asks — after the operator typed CLEAN."""
        with self.assertRaises(U.UploaderNotLoaded) as caught:
            self.load(self.spec_for(HALF_DONE))
        self.assertIn("published", str(caught.exception))

    def test_the_default_name_is_the_class_so_attribution_always_works(self):
        self.assertEqual(self.load(self.spec_for(COMPLETE)).name(), "Target")

    def test_the_origin_names_the_implementation_and_where_it_came_from(self):
        spec = self.spec_for(COMPLETE)
        origin = U.origin_of(spec, self.load(spec))
        self.assertIn("Target", origin)
        self.assertIn(spec, origin)


class TestTheShippedExample(unittest.TestCase):
    """The worked example is driven, not merely shipped.

    An example nobody runs rots into a lie, and this one is the documentation
    for how to implement the interface — the first thing anybody copies.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "DASHCAM_FOLDER_TARGET", None)
        os.environ["DASHCAM_FOLDER_TARGET"] = str(self.dir / "published")
        self.target = U.load_uploader(
            "%s:FolderTarget" % (REPO / "examples" / "uploader_folder.py"), REPO)

    def a_render(self, name="trip_A_h1080.mp4", size=10):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        return W.Render(name, size, path)

    def test_it_owes_a_render_it_does_not_have_and_stops_owing_it_after_upload(self):
        render = self.a_render()
        self.assertTrue(self.target.owes((render,)).any)
        self.target.upload(W.World(renders=(render,)), SilentUi())
        self.assertFalse(self.target.owes((render,)).any)

    def test_holds_is_yes_only_after_the_upload(self):
        render = self.a_render()
        self.assertIs(self.target.holds((render,)).covers((render,)), M.Evidence.NO)
        self.target.upload(W.World(renders=(render,)), SilentUi())
        self.assertIs(self.target.holds((render,)).covers((render,)), M.Evidence.YES)

    def test_a_copy_at_a_different_size_does_not_vouch_for_the_local_one(self):
        """The failure this rule was written for: a re-render at the same name
        reading as already published while its only other copy was about to be
        erased."""
        render = self.a_render()
        self.target.upload(W.World(renders=(render,)), SilentUi())
        bigger = W.Render(render.name, render.size + 1, render.path)
        self.assertIs(self.target.holds((bigger,)).about(render.name), M.Evidence.NO)

    def test_a_folder_says_the_serving_question_does_not_arise(self):
        """NA, not UNKNOWN: a folder genuinely does not serve anything. The
        difference decides whether Clean Workspace's third gate is dropped or
        failed."""
        self.assertIs(self.target.published(()).covers(()), M.Evidence.NA)


class SilentUi(U.Ui):
    def say(self, line):
        pass

    def warn(self, line):
        pass

    def run(self, cmd, cwd, label, env=None):        # pragma: no cover
        raise AssertionError("the example must not run a subprocess")


class TestTheToolStopsRatherThanDegrading(unittest.TestCase):
    """A configured uploader that will not load must not become the local
    edition by accident.

    Silently becoming local is how someone's renders quietly stop being
    published: the menu looks normal, item 6 writes a local page, and item 8
    refuses for a reason that reads like a network problem.
    """

    def test_ctx_construction_raises_rather_than_returning_no_uploader(self):
        import pipeline as P

        with self.assertRaises(U.UploaderNotLoaded):
            P._loaded_uploader("/no/such/file.py:Nope", REPO)

    def test_an_unset_setting_is_the_local_edition_on_purpose(self):
        import pipeline as P

        self.assertIsNone(P._loaded_uploader("", REPO))
        self.assertIsNone(P._loaded_uploader(None, REPO))


if __name__ == "__main__":
    unittest.main()
