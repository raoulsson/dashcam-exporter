"""The UI seam. StreamUiHandler must reproduce the exact bytes the tool printed
before the seam existed -- that is what lets the reroute proceed without the
existing suite (which asserts on captured stdout) noticing."""

import io
import unittest
from contextlib import redirect_stdout

from dashcam_exporter.application.ui import handler as H
from dashcam_exporter.application.ui.term import C


class _NoColour(unittest.TestCase):
    """The colour gate is a real-tty global; force it off so an assertion about
    exact bytes does not depend on whether the test runs under a terminal."""

    def setUp(self):
        self._enabled = C.enabled
        C.enabled = False

    def tearDown(self):
        C.enabled = self._enabled


class StreamUiHandlerReproducesTheOldOutput(_NoColour):
    def test_block_prints_each_line_right_stripped(self):
        out = io.StringIO()
        with redirect_stdout(out):
            H.StreamUiHandler().block(["a  ", "b\t", "c"])
        self.assertEqual(out.getvalue(), "a\nb\nc\n")

    def test_block_consumes_an_iterator_not_only_a_list(self):
        # print_summary hands _print_all a map(); block must not require a list.
        out = io.StringIO()
        with redirect_stdout(out):
            H.StreamUiHandler().block(iter(("x", "y")))
        self.assertEqual(out.getvalue(), "x\ny\n")

    def test_done_is_the_closing_hundred_percent_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            H.StreamUiHandler().done("rendered 3 trips")
        self.assertEqual(out.getvalue(), "  100% - rendered 3 trips.\n")


class StreamUiHandlerDrivesTheExistingPainters(unittest.TestCase):
    """The stream backend renders by calling screens, not by re-implementing it,
    so the grid the operator sees is the one screens.print_menu has always drawn.
    Asserting delegation (not bytes) keeps this test from re-pinning the grid
    format, which screens' own tests already own."""

    def test_menu_delegates_to_screens_print_menu(self):
        from unittest import mock
        from dashcam_exporter.application.ui import screens
        seen = {}
        with mock.patch.object(screens, "print_menu",
                               side_effect=lambda *a: seen.setdefault("args", a)):
            H.StreamUiHandler().menu("CTX", "ITEMS", "POS", "WORLD")
        self.assertEqual(seen["args"], ("CTX", "ITEMS", "POS", "WORLD"))

    def test_summary_delegates_to_screens_print_summary(self):
        from unittest import mock
        from dashcam_exporter.application.ui import screens
        seen = {}
        with mock.patch.object(screens, "print_summary",
                               side_effect=lambda *a: seen.setdefault("args", a)):
            H.StreamUiHandler().summary("CTX", close=False)
        self.assertEqual(seen["args"], ("CTX", False))

    def test_block_delegates_to_the_screens_sink(self):
        out = io.StringIO()
        with redirect_stdout(out):
            H.StreamUiHandler().block(["one ", "two"])
        self.assertEqual(out.getvalue(), "one\ntwo\n")


class TheActiveHandler(unittest.TestCase):
    def tearDown(self):
        H.set_active(None)   # never leak a backend into another test

    def test_defaults_to_the_stream_backend(self):
        H.set_active(None)
        self.assertIsInstance(H.active(), H.StreamUiHandler)

    def test_the_default_is_a_single_shared_instance(self):
        H.set_active(None)
        self.assertIs(H.active(), H.active())

    def test_set_active_installs_a_backend(self):
        sentinel = H.StreamUiHandler()
        H.set_active(sentinel)
        self.assertIs(H.active(), sentinel)

    def test_set_active_none_falls_back_to_the_default(self):
        H.set_active(H.StreamUiHandler())
        H.set_active(None)
        self.assertIsInstance(H.active(), H.StreamUiHandler)


if __name__ == "__main__":
    unittest.main()
