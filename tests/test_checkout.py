#!/usr/bin/env python3
"""Where the tool thinks its own files are, and that a test can say otherwise.

Three things are worth pinning here, and only three.

The first is the layout itself: config.txt, .env and VERSION at the checkout
root, sources under src/. Getting src/ wrong stops the tool at launch, because
that directory is what a plugin's `from uploader import ...` resolves against;
getting the root wrong moves the tool's memory out of ~/.dashcam-exporter and
every card previously imported reads as never imported.

The second is that the derived paths are NOT overridable. A second Checkout
that disagreed about where config.txt lives would be a second answer to a
question that must have one, so a subclass supplying nothing but root() has to
get the whole layout for free.

The third is the payoff, and the reason the class exists at all: a Ctx handed
a Checkout reads that tree's config.txt and never looks at the operator's. If
that test cannot be written, the injection is decoration.
"""

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dashcam_exporter.checkout import Checkout, FakeCheckout, RealCheckout  # noqa: E402


def load_pipeline():
    sys.argv = ["pipeline.py"]
    from dashcam_exporter import pipeline
    return pipeline


P = load_pipeline()


class OnlyRoot(Checkout):
    """The whole point, stated as a class: nothing but root()."""

    def root(self):
        return Path("/somewhere")


class TestDerivedPaths(unittest.TestCase):

    def test_fake_puts_everything_under_the_root_it_was_given(self):
        c = FakeCheckout("/nowhere")
        self.assertEqual(c.root(), Path("/nowhere"))
        self.assertEqual(c.src(), Path("/nowhere/src"))
        self.assertEqual(c.config_file(), Path("/nowhere/config.txt"))
        self.assertEqual(c.env_file(), Path("/nowhere/.env"))

    def test_only_root_is_abstract(self):
        # If a derived path were abstract too, a second implementation could
        # answer it differently and the layout would have two versions.
        self.assertEqual(Checkout.__abstractmethods__, frozenset({"root"}))

    def test_a_subclass_supplying_only_root_gets_the_whole_layout(self):
        c = OnlyRoot()
        self.assertEqual(c.src(), Path("/somewhere/src"))
        self.assertEqual(c.config_file(), Path("/somewhere/config.txt"))
        self.assertEqual(c.env_file(), Path("/somewhere/.env"))

    def test_neither_shipped_implementation_overrides_a_derived_path(self):
        for impl in (RealCheckout, FakeCheckout):
            for name in ("src", "config_file", "env_file"):
                self.assertIs(getattr(impl, name), getattr(Checkout, name),
                              "%s overrides %s" % (impl.__name__, name))


class TestRealCheckout(unittest.TestCase):

    def test_a_module_under_src_names_the_checkout_above_it(self):
        c = RealCheckout(REPO / "src" / "dashcam_exporter" / "pipeline.py")
        self.assertEqual(c.root(), REPO)
        self.assertEqual(c.src(), REPO / "src")

    def test_the_files_it_names_are_really_there(self):
        c = RealCheckout(REPO / "src" / "dashcam_exporter" / "pipeline.py")
        self.assertTrue(c.config_file().is_file())
        self.assertTrue(c.src().is_dir())
        self.assertTrue((c.src() / "dashcam_exporter" / "uploader.py").is_file())


class TestWhatThePipelineDerives(unittest.TestCase):

    def test_src_dir_is_the_source_directory(self):
        # What goes on sys.path for a plugin. Hand it the checkout and the
        # plugin fails to import, which stops the tool.
        self.assertEqual(P.SRC_DIR.name, "src")
        self.assertEqual(P.SRC_DIR, P.EXPORTER_DIR / "src")

    def test_the_memory_is_named_after_the_checkout(self):
        # ~/.dashcam-exporter, never ~/.src. Named after the source directory
        # instead, every previously imported card reads as never imported.
        self.assertEqual(P.HOME_DIR.name, ".dashcam-exporter")
        self.assertEqual(P.home_dir_for(P.CHECKOUT.root()).name,
                         ".dashcam-exporter")


class TestCtxTakesACheckout(unittest.TestCase):
    """The acceptance test: a Ctx reads the tree it was handed, and no other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "config.txt").write_text(
            "workspace=%s/ws\nstill_width=321\n" % self.root, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_it_reads_the_fake_tree(self):
        ctx = P.Ctx(FakeCheckout(self.root))
        self.assertEqual(ctx.exporter, self.root)
        self.assertEqual(ctx.config_path, self.root / "config.txt")
        self.assertEqual(ctx.still_width, 321)
        self.assertEqual(ctx.workspace, self.root / "ws")

    def test_it_never_opens_the_real_config(self):
        seen = []
        real = P.load_config
        P.load_config = lambda path: (seen.append(Path(path)), real(path))[1]
        try:
            P.Ctx(FakeCheckout(self.root))
        finally:
            P.load_config = real
        self.assertEqual(seen, [self.root / "config.txt"])
        self.assertNotIn(P.EXPORTER_DIR / "config.txt", seen)

    def test_no_argument_still_means_the_real_checkout(self):
        # Ctx is built with no arguments all over the tool; the default has to
        # keep meaning the installation the code was loaded from.
        ctx = P.Ctx()
        self.assertEqual(ctx.exporter, P.EXPORTER_DIR)
        self.assertEqual(ctx.config_path, P.EXPORTER_DIR / "config.txt")


class TestTheRendererIsWhereThePipelineSaysItIs(unittest.TestCase):
    """A child process launched by a name that does not resolve.

    The package move must keep `--print-groups` executable from the checkout
    root. The renderer is launched as a module with src/ on PYTHONPATH.

    Asserting the file exists is a poor substitute for running it, and it is
    the part that actually went wrong: not the arguments, the path.
    """

    def test_the_renderer_module_the_pipeline_launches_is_importable(self):
        text = (REPO / "src" / "dashcam_exporter" / "pipeline.py").read_text()
        launched = re.findall(r'"-m", "([a-z_\.]+)"', text)
        self.assertIn("dashcam_exporter.renderer", launched)
        module = REPO / "src" / "dashcam_exporter" / "renderer.py"
        self.assertTrue(module.is_file(), "the packaged renderer is missing")

    def test_no_child_is_launched_by_a_bare_module_name(self):
        """The shape of the bug, not just this instance of it."""
        text = (REPO / "src" / "dashcam_exporter" / "pipeline.py").read_text()
        self.assertNotIn('"-u", "make_dashcam_videos.py"', text)


if __name__ == "__main__":
    unittest.main()
