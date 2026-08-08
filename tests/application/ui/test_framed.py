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
        self.assertEqual((L.title_row, L.status_row, L.top_rule_row), (1, 2, 3))
        self.assertEqual((L.log_top, L.log_bottom), (4, 18))
        self.assertEqual((L.divider_row, L.bar_row, L.bottom_rule_row), (19, 20, 21))
        self.assertEqual(L.menu_rows, [22, 23])
        self.assertEqual(L.select_row, 24)
        self.assertEqual(L.log_height, 15)

    def test_the_bar_sits_just_above_the_menu_at_any_height(self):
        for rows in (18, 24, 40, 60):
            L = framed.Layout(rows, 80)
            self.assertEqual(L.bar_row, L.divider_row + 1)
            self.assertEqual(L.bottom_rule_row, L.bar_row + 1)
            self.assertEqual(L.menu_top, L.bottom_rule_row + 1)
            self.assertGreaterEqual(L.log_height, 1)

    def test_a_tiny_terminal_is_clamped_not_broken(self):
        L = framed.Layout(4, 10)
        self.assertEqual(L.rows, framed.Layout.MIN_ROWS)
        self.assertEqual(L.cols, framed.Layout.MIN_COLS)
        self.assertGreaterEqual(L.log_height, 1)


class ThePlainHelperStripsColour(unittest.TestCase):
    def test_it_measures_the_visible_width(self):
        self.assertEqual(framed._plain("\x1b[32mgo\x1b[0m"), "go")


class TheMenuBarWrapsToItsRows(unittest.TestCase):
    def test_wrap_never_exceeds_the_row_budget(self):
        cells = ["%d)Item" % n for n in range(1, 11)]
        lines = framed._wrap_cells(cells, 40, framed.Layout.MENU_ROWS)
        self.assertLessEqual(len(lines), framed.Layout.MENU_ROWS)
        for line in lines:
            self.assertLessEqual(len(framed._plain(line)), 40)


class TheFrameRendersIntoRegions(_NoColour):
    """open() grabs the current stdout as the real terminal; the frame paints
    absolute-positioned rows onto it. We capture those and read the row numbers
    back out of the escape sequences."""

    def _render(self):
        h = framed.FramedUiHandler(title="dashcam-exporter", subtitle="import 2026-07-19")
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
        self.assertEqual(self._row_of(out, "dashcam-exporter"), 1)
        self.assertEqual(self._row_of(out, "3 trips"), 2)
        self.assertEqual(self._row_of(out, "Render ####"), 20)   # the pinned bar
        # the six log lines fill the top of the log region (rows 4..)
        self.assertEqual(self._row_of(out, "clip 1/20"), 4)
        self.assertEqual(self._row_of(out, "clip 6/20"), 9)


class TheLogIsARingOfTheVisibleHeight(_NoColour):
    def test_it_keeps_only_the_last_screenful(self):
        h = framed.FramedUiHandler()
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


class TheStdoutTeeCatchesStrayPrints(_NoColour):
    def test_a_bare_print_lands_in_the_log_while_open(self):
        h = framed.FramedUiHandler()
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
