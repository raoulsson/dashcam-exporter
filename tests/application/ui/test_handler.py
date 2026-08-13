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

    def test_new_live_hands_back_a_stream_live(self):
        from dashcam_exporter.application.ui.progress import Live
        self.assertIsInstance(H.StreamUiHandler().new_live(), Live)

    def test_waiting_hands_back_the_stream_spinner(self):
        from dashcam_exporter.application.ui.progress import Waiting
        self.assertIsInstance(H.StreamUiHandler().waiting("Querying..."), Waiting)

    def test_set_splash_is_declined_so_the_banner_prints_to_the_scroll(self):
        self.assertFalse(H.StreamUiHandler().set_splash(["banner"]))

    def test_log_prints_the_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            H.StreamUiHandler().log("a streamed line")
            H.StreamUiHandler().log()
        self.assertEqual(out.getvalue(), "a streamed line\n\n")

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


class StreamUiHandlerInputDelegatesToPrompt(unittest.TestCase):
    """Input routes through the seam but the raw reads still live in prompt --
    which is the module the tests patch, so routing did not move their point."""

    def test_read_key_ask_confirm_forward_to_prompt(self):
        from unittest import mock
        from dashcam_exporter.application.ui import prompt
        with mock.patch.object(prompt, "read_key", return_value="7") as rk, \
                mock.patch.object(prompt, "ask", return_value="line") as ak, \
                mock.patch.object(prompt, "confirm", return_value=True) as cf:
            h = H.StreamUiHandler()
            self.assertEqual(h.read_key("Select> "), "7")
            self.assertEqual(h.ask("Q?", "def", False), "line")
            self.assertTrue(h.confirm("Y?", True))
        rk.assert_called_once_with("Select> ")
        ak.assert_called_once_with("Q?", "def", False)
        cf.assert_called_once_with("Y?", True)


class OneWaitsForASecondKeySoTenIsReachable(unittest.TestCase):
    """A single-digit menu fires on one keypress, which makes 1 unreachable as
    the start of 10) Delete SIM Data. 1 alone now waits: 0 makes it 10, Enter or
    anything else confirms plain 1. Every other key still fires at once."""

    def _key(self, seq):
        import io, sys
        from unittest import mock
        from dashcam_exporter.application.ui import prompt
        with mock.patch.object(prompt, "_raw_capable", return_value=True), \
                mock.patch.object(prompt, "_one_char", side_effect=list(seq)):
            saved, sys.stdout = sys.stdout, io.StringIO()
            try:
                return prompt.read_key("Select> ")
            finally:
                sys.stdout = saved

    def test_one_then_zero_is_ten(self):
        self.assertEqual(self._key(["1", "0"]), "10")

    def test_one_then_enter_is_one(self):
        self.assertEqual(self._key(["1", "\r"]), "1")

    def test_one_then_another_digit_is_still_one(self):
        self.assertEqual(self._key(["1", "5"]), "1")

    def test_any_other_digit_fires_at_once(self):
        self.assertEqual(self._key(["2"]), "2")

    def test_the_help_key_waits_for_ten_too(self):
        """`h` takes a second key, and it read exactly ONE of them.

        So `h` 1 0 was help about entry 1 plus a LOOSE 0, which the menu took
        as a fresh keypress and answered with Progress -- item 10's help was
        unreachable, and asking for it quietly did something else. It is the
        one entry whose help matters most: the one that erases a card.
        """
        self.assertEqual(self._key(["h", "1", "0"]), "h 10")

    def test_the_help_key_still_takes_a_plain_one(self):
        self.assertEqual(self._key(["h", "1", "\r"]), "h 1")

    def test_the_help_key_is_unchanged_for_every_other_entry(self):
        self.assertEqual(self._key(["h", "5"]), "h 5")
        self.assertEqual(self._key(["h", ""]), "h")


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
