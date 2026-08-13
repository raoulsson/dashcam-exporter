"""The framed backend. The layout is pure arithmetic over a terminal size, so
where each band lands is checkable without a real screen; the render is checked
by capturing the ANSI the frame writes and asking which row each thing reached."""

import io
import re
import sys
import types
import unittest
import unittest.mock

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
        L = framed.Layout(24, 80)                              # show_progress default
        self.assertEqual((L.title_top_rule, L.title_row, L.title_sep), (1, 3, 5))
        self.assertEqual((L.log_top, L.log_bottom), (6, 14))
        self.assertEqual((L.progress_top_rule, L.bar_row, L.progress_sep), (15, 16, 17))
        self.assertEqual(L.menu_rows, [18, 19, 20])          # 3 grid rows
        self.assertEqual((L.hint_row, L.select_rule, L.select_row, L.bottom_rule),
                         (21, 22, 23, 24))
        self.assertEqual(L.log_height, 9)

    def test_the_progress_box_is_one_bar_row_no_padding(self):
        for rows in (22, 24, 40, 60):
            L = framed.Layout(rows, 80)
            self.assertEqual(L.bar_row, L.progress_top_rule + 1)   # rule, then bar
            self.assertEqual(L.progress_sep, L.bar_row + 1)        # bar, then sep
            self.assertEqual(L.menu_top, L.progress_sep + 1)
            self.assertEqual(L.select_row, L.select_rule + 1)      # rule, then select
            self.assertEqual(L.bottom_rule, L.select_row + 1)      # box closes below it
            self.assertGreaterEqual(L.log_height, 1)

    def test_when_idle_the_progress_box_is_gone_and_the_log_grows(self):
        with_bar = framed.Layout(24, 80, show_progress=True)
        idle = framed.Layout(24, 80, show_progress=False)
        self.assertIsNone(idle.bar_row)
        self.assertIsNone(idle.progress_top_rule)
        self.assertEqual(idle.progress_sep, with_bar.progress_sep)   # menu unmoved
        self.assertEqual(idle.log_bottom, idle.progress_sep - 1)     # log reclaims the box
        self.assertEqual(idle.log_height, with_bar.log_height + 2)   # the rule + bar row

    def test_a_tiny_terminal_is_clamped_not_broken(self):
        L = framed.Layout(4, 10)
        self.assertEqual(L.rows, framed.Layout.MIN_ROWS)
        self.assertEqual(L.cols, framed.Layout.MIN_COLS)
        self.assertGreaterEqual(L.log_height, 1)


class ThePlainHelperStripsColour(unittest.TestCase):
    def test_it_measures_the_visible_width(self):
        self.assertEqual(framed._plain("\x1b[32mgo\x1b[0m"), "go")


class TheDirectionKeysFallBackToText(unittest.TestCase):
    def test_arrows_when_the_terminal_encodes_them(self):
        with unittest.mock.patch.object(framed.sys, "__stdout__",
                                        types.SimpleNamespace(encoding="utf-8")):
            self.assertEqual(framed._dir_keys(), ("↑ j)", "↓ k)"))

    def test_spelled_out_when_it_cannot(self):
        with unittest.mock.patch.object(framed.sys, "__stdout__",
                                        types.SimpleNamespace(encoding="ascii")):
            self.assertEqual(framed._dir_keys(), ("up: j)", "down: k)"))


class TheMenuRegionSizesToItsContent(unittest.TestCase):
    def test_the_layout_reserves_the_grid_rows_it_is_told_to(self):
        L = framed.Layout(40, 140, menu_rows=6)
        self.assertEqual(len(L.menu_rows), 6)
        self.assertEqual(L.menu_bottom, 40 - 4)        # above hint, select-rule, select, bottom
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


class TheLogKeepsHistoryAndPages(_NoColour):
    def _filled(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap = io.StringIO()
        saved = sys.stdout
        sys.stdout = cap
        try:
            h.open()
            for i in range(100):
                h.log("line %d" % i)
            yield h, cap
            h.close()
        finally:
            sys.stdout = saved

    def _visible(self, cap):
        rows = {}
        for m in re.finditer(r"\x1b\[(\d+);1H\s*([^\x1b]*)", cap.getvalue()):
            rows[int(m.group(1))] = m.group(2)
        return rows

    def test_history_is_kept_and_the_view_sits_on_the_newest_page(self):
        gen = self._filled()
        h, cap = next(gen)
        # History retained for paging; the view is on the page with the newest.
        self.assertEqual(len(h._log), 100)
        self.assertEqual(h._log[-1], "line 99")
        self.assertEqual(h._log_view, h._last_page_start())
        list(gen)

    def test_j_pages_back_and_k_pages_forward(self):
        gen = self._filled()
        h, cap = next(gen)
        body, last = h._body_height(), h._last_page_start()
        self.assertEqual(h._log_view, last)
        h.page("j")                       # back a page
        self.assertEqual(h._log_view, max(0, last - body))
        h.page("k")                       # forward, to the newest page
        self.assertEqual(h._log_view, last)
        h.page("k")                       # clamps at the last page
        self.assertEqual(h._log_view, last)
        list(gen)

    def test_scroll_top_anchors_the_view_on_the_first_page(self):
        # A read-me screen (the licence) prints taller than the region; without
        # anchoring, log() follows to the last, near-empty page and the screen
        # reads as blank. scroll_top puts the view back on the heading.
        gen = self._filled()
        h, cap = next(gen)
        self.assertEqual(h._log_view, h._last_page_start())
        h.scroll_top()
        self.assertEqual(h._log_view, 0)
        # The menu loop prints a blank line after every draw; the pin must hold
        # the view at the top through that lone log(), not snap back down, and
        # must not let the blank pile up on the log.
        before = len(h._log)
        h.log("")
        self.assertEqual(h._log_view, 0)
        self.assertEqual(len(h._log), before)      # the blank spacer was dropped
        # Paging forward moves the view one screenful but KEEPS the pin -- the
        # operator is steering, and the next menu blank must not re-follow.
        body = h._body_height()
        h.page("k")
        self.assertEqual(h._log_view, body)        # advanced one page, not to the tail
        self.assertTrue(h._pinned_top)
        h.log("")                                  # the loop's blank, again
        self.assertEqual(h._log_view, body)        # view held where the operator left it
        # Only a new action releases the pin.
        h.clear_log()
        self.assertFalse(h._pinned_top)
        list(gen)

    def test_the_nav_lives_in_the_log_foot_right_aligned_over_a_gutter(self):
        gen = self._filled()
        h, cap = next(gen)
        inner = h.layout.cols - 2
        foot = h._footer_text(inner)
        up_key, down_key = framed._dir_keys()
        self.assertIn(up_key, foot)              # the j key, tagged with its direction
        self.assertIn(down_key, foot)            # the k key
        self.assertEqual(len(foot), inner - 1)   # one col short; the box pads the last
        self.assertTrue(foot.startswith(" "))    # pushed to the right
        list(gen)

    def test_the_foot_is_empty_when_the_log_fits_on_one_page(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h.open()
            h.log("just one line")
            self.assertEqual(h._page_hint(), "")
            self.assertEqual(h._footer_text(h.layout.cols - 2), "")
        finally:
            h.close()
            sys.stdout = saved

    def test_the_nav_keys_are_bold(self):
        gen = self._filled()
        h, cap = next(gen)
        C.enabled = True
        try:
            up_key, down_key = framed._dir_keys()
            hint = h._page_hint()
            self.assertIn(C.bold(up_key), hint)
            self.assertIn(C.bold(down_key), hint)
        finally:
            C.enabled = False
        list(gen)

    def test_the_submenu_keys_are_bold(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        C.enabled = True
        try:
            h.open()
            h._paint_menu()
            bold_keys = [C.bold(k) for k in ("p)", "h)", "i)", "l)", "q)")]
        finally:
            C.enabled = False
            h.close()
            sys.stdout = saved
        out = cap.getvalue()
        for bold_key in bold_keys:
            self.assertIn(bold_key, out)


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
            with h.waiting("Querying the plugin") as w:
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
