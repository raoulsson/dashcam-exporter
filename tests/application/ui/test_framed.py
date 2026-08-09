"""The framed backend. The layout is pure arithmetic over a terminal size, so
where each band lands is checkable without a real screen; the render is checked
by capturing the ANSI the frame writes and asking which row each thing reached."""

import io
import re
import sys
import unittest

from dashcam_exporter.application.ui.term import C
from dashcam_exporter.application.ui import framed


class _NoColour(unittest.TestCase):
    def setUp(self):
        self._enabled = C.enabled
        C.enabled = False

    def tearDown(self):
        C.enabled = self._enabled


class TheLayoutIsPureArithmetic(unittest.TestCase):
    def test_the_bands_stack_in_order_on_an_80x24(self):
        L = framed.Layout(24, 80)
        self.assertEqual((L.title_top_rule, L.title_row, L.title_bot_rule), (1, 3, 5))
        self.assertEqual((L.log_top, L.log_bottom), (6, 13))
        self.assertEqual((L.progress_top_rule, L.bar_row, L.progress_sep), (14, 16, 18))
        self.assertEqual(L.menu_rows, [19, 20, 21])          # 3 grid rows
        self.assertEqual((L.hint_row, L.menu_bot_rule, L.select_row), (22, 23, 24))
        self.assertEqual(L.log_height, 8)

    def test_the_boxes_stack_in_order_at_any_height(self):
        for rows in (20, 24, 40, 60):
            L = framed.Layout(rows, 80)
            self.assertEqual(L.bar_row, L.progress_top_rule + 2)   # rule, pad, bar
            self.assertEqual(L.progress_sep, L.bar_row + 2)        # bar, pad, sep
            self.assertEqual(L.menu_top, L.progress_sep + 1)
            self.assertEqual(L.select_row, L.menu_bot_rule + 1)
            self.assertGreaterEqual(L.log_height, 1)

    def test_a_tiny_terminal_is_clamped_not_broken(self):
        L = framed.Layout(4, 10)
        self.assertEqual(L.rows, framed.Layout.MIN_ROWS)
        self.assertEqual(L.cols, framed.Layout.MIN_COLS)
        self.assertGreaterEqual(L.log_height, 1)


class ThePlainHelperStripsColour(unittest.TestCase):
    def test_it_measures_the_visible_width(self):
        self.assertEqual(framed._plain("\x1b[32mgo\x1b[0m"), "go")


class TheMenuRegionSizesToItsContent(unittest.TestCase):
    def test_the_layout_reserves_the_grid_rows_it_is_told_to(self):
        L = framed.Layout(40, 140, menu_rows=6)
        self.assertEqual(len(L.menu_rows), 6)
        self.assertEqual(L.menu_bottom, 40 - 3)        # above hint + rule + select
        self.assertEqual(L.menu_top, L.menu_bottom - 5)
        self.assertGreaterEqual(L.log_height, 1)


class TheFrameRendersIntoRegions(_NoColour):
    """open() grabs the current stdout as the real terminal; the frame paints
    absolute-positioned rows onto it. We capture those and read the row numbers
    back out of the escape sequences."""

    def _render(self):
        h = framed.FramedUiHandler(title="dashcam-exporter", subtitle="import 2026-07-19", splash_seconds=0)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            h.status("3 trips  2 rendered")
            for i in range(6):
                h.log("clip %d/20  ok" % (i + 1))
            h.set_bar("Render ####------ 62%")
            h.close()
        finally:
            sys.stdout = saved
        return cap.getvalue()

    @staticmethod
    def _row_of(out, needle):
        for m in re.finditer(r"\x1b\[(\d+);1H([^\x1b]*)", out):
            if needle in m.group(2):
                return int(m.group(1))
        return None

    def test_it_enters_and_leaves_the_alternate_screen(self):
        out = self._render()
        self.assertIn(framed.ALT_ON, out)
        self.assertIn(framed.ALT_OFF, out)
        self.assertIn(framed.SHOW, out)          # cursor restored on close

    def test_the_bands_land_on_their_rows(self):
        out = self._render()
        self.assertEqual(self._row_of(out, "dashcam-exporter"), 3)   # in the title box
        self.assertEqual(self._row_of(out, "3 trips"), 3)            # status shares that row
        self.assertEqual(self._row_of(out, "Render ####"), 16)       # the pinned bar
        # the six log lines fill the top of the log region (rows 6..)
        self.assertEqual(self._row_of(out, "clip 1/20"), 6)
        self.assertEqual(self._row_of(out, "clip 6/20"), 11)


class TheSplashShowsTheInfoCentered(_NoColour):
    def test_open_paints_a_bordered_card_with_the_same_info(self):
        h = framed.FramedUiHandler(title="dashcam-exporter",
                                   subtitle="import 2026-07-19", splash_seconds=0.01)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.status("workspace ~/dashcam-data")
            h.open()          # StringIO is not a tty, so it paints without the pause
            h.close()
        finally:
            sys.stdout = saved
        out = cap.getvalue()
        self.assertIn("+-", out)                      # a bordered card
        self.assertIn("dashcam-exporter", out)
        self.assertIn("import 2026-07-19", out)
        self.assertIn("workspace ~/dashcam-data", out)


class TheSplashCanCarryLaunchArt(_NoColour):
    def test_set_splash_makes_the_frame_paint_the_art_centered(self):
        h = framed.FramedUiHandler(splash_seconds=0.01)
        h._size = lambda: (24, 80)
        consumed = h.set_splash(["  __ ART __  ", " the banner "])
        self.assertTrue(consumed)          # framed consumes the banner
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            h.close()
        finally:
            sys.stdout = saved
        out = cap.getvalue()
        self.assertIn("__ ART __", out)
        self.assertIn("the banner", out)
        self.assertNotIn("+-", out)        # art, not the fallback card


class TheLogIsARingOfTheVisibleHeight(_NoColour):
    def test_it_keeps_only_the_last_screenful(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            for i in range(100):
                h.log("line %d" % i)
            h.close()
        finally:
            sys.stdout = saved
        self.assertEqual(len(h._log), h.layout.log_height)
        self.assertEqual(h._log[-1], "line 99")


class TheWaitingSpinnerUsesThePinnedBar(_NoColour):
    """The stream spinner writes carriage-return redraws the frame's tee cannot
    show; the framed one paints the pinned bar, with the plugin's note beside it,
    and clears it on exit. This is the 'blocked at querying plugin' fix."""

    def test_it_paints_the_label_and_note_then_clears(self):
        import time
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            with h.waiting("Querying the plugin...") as w:
                w.update("asking the plugin")
                time.sleep(0.3)          # let the animator draw at least once
            cleared = h._bar
            h.close()
        finally:
            sys.stdout = saved
        self.assertIn("Querying the plugin", cap.getvalue())
        self.assertIn("asking the plugin", cap.getvalue())
        self.assertEqual(cleared, "")    # bar emptied when the wait ends


class TheStdoutTeeCatchesStrayPrints(_NoColour):
    def test_a_bare_print_lands_in_the_log_while_open(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            print("a step that never learned the seam")   # goes through the tee
            captured = list(h._log)
            h.close()
        finally:
            sys.stdout = saved
        self.assertIn("a step that never learned the seam", captured)


if __name__ == "__main__":
    unittest.main()
