"""The framed backend. The layout is pure arithmetic over a terminal size, so
where each band lands is checkable without a real screen; the render is checked
by capturing the ANSI the frame writes and asking which row each thing reached."""

import collections
import contextlib
import io
import os
import re
import signal
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


class TheParagraphPagerTerminates(_NoColour):
    """Item 7 streams every transcript paragraph through `paragraph()`.

    It padded to a page boundary with `while len(self._log) % body != 0`, and
    the log is a BOUNDED deque -- once at maxlen, appending stops changing
    len(), so that condition can never come true. The tool wedged with no
    output and no menu a few hundred paragraphs into a transcription.
    """

    def _handler(self, maxlen):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        h._log = collections.deque(maxlen=maxlen)
        return h

    def test_it_returns_with_the_log_at_its_cap(self):
        # A maxlen deliberately NOT divisible by the page height: that is the
        # case the modulo condition could never satisfy.
        h = self._handler(101)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h.open()
            self.assertNotEqual(101 % h._body_height(), 0, "harness must hit the bad case")
            for i in range(60):
                h.paragraph("paragraph %d, long enough to wrap across a couple "
                            "of rows in an eighty column frame." % i)
        finally:
            h.close()
            sys.stdout = saved
        self.assertEqual(len(h._log), 101)      # still bounded, and it got here

    def test_a_paragraph_lands_on_the_page_it_was_given(self):
        h = self._handler(2000)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h.open()
            h.paragraph("first")
            after_first = len(h._log)
            h.paragraph("second")
        finally:
            h.close()
            sys.stdout = saved
        # Two short paragraphs share a page: the second must not have padded a
        # whole screen of blanks to open one of its own.
        self.assertLess(len(h._log) - after_first, h._body_height())


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
        self.assertTrue(h._held)
        # Output arriving while the operator reads must not snap the view down.
        h.log("a line from somewhere else")
        self.assertEqual(h._log_view, 0)
        # Paging forward moves the view one screenful and KEEPS the hold -- the
        # operator is steering until they arrive back at the tail.
        body = h._body_height()
        h.page("k")
        self.assertEqual(h._log_view, body)        # advanced one page, not to the tail
        self.assertTrue(h._held)
        # Only a new action releases it (or paging back onto the newest page).
        h.clear_log()
        self.assertFalse(h._held)
        list(gen)

    def test_paging_back_after_a_step_survives_the_next_menu_turn(self):
        """BUG: j/k were dead everywhere but the licence.

        The menu loop logged a blank line after every draw, and log() follows to
        the newest page -- so the view the operator had just paged to was snapped
        back on the very next turn and the keys looked like they did nothing. Two
        halves to the fix: paging away from the tail HOLDS the view, and the
        loop's spacer is `menu_spacer`, which the frame answers with nothing.
        """
        gen = self._filled()
        h, cap = next(gen)
        h.page("j")
        paged_to = h._log_view
        self.assertLess(paged_to, h._last_page_start())
        # A menu turn: the spacer must neither move the view nor grow the log.
        history = len(h._log)
        h.menu_spacer()
        self.assertEqual(h._log_view, paged_to)
        self.assertEqual(len(h._log), history)
        # And output arriving anyway (a stray print) leaves the operator's page.
        h.log("something from a background writer")
        self.assertEqual(h._log_view, paged_to)
        list(gen)

    def test_paging_back_onto_the_newest_page_hands_the_view_back(self):
        """The live case: while a step streams, the log must follow its own tail.

        `clear_log` at the start of an action releases the hold, and so does
        walking forward to the last page -- a reader who has reached the bottom
        has said they are done steering, and needing a second key to say it
        again is a mode nobody would find.
        """
        gen = self._filled()
        h, cap = next(gen)
        h.page("j")
        self.assertTrue(h._held)
        h.page("k")                                 # back onto the newest page
        self.assertFalse(h._held)
        h.log("line 100")
        self.assertEqual(h._log_view, h._last_page_start())   # following again
        # And a fresh action always follows, whatever the operator was reading.
        h.page("j")
        h.clear_log()
        for i in range(100):
            h.log("streamed %d" % i)
        self.assertEqual(h._log_view, h._last_page_start())
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


class TheFrameFollowsTheWindow(_NoColour):
    """BUG: nothing handled SIGWINCH, so the layout was only recomputed when
    something incidental (the menu grid growing, the progress box opening)
    happened to rebuild it. A 40-row window shrunk to 24 went on being painted
    at 40 rows -- over the top of the shell."""

    def _open(self, rows=40, cols=100):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (rows, cols)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h.open()
        except Exception:
            sys.stdout = saved
            raise
        return h, cap, saved

    def test_a_resize_relayouts_and_repaints(self):
        h, cap, saved = self._open(40, 100)
        try:
            self.assertEqual(h.layout.rows, 40)
            h._size = lambda: (24, 80)          # the window shrank
            cap.truncate(0)
            cap.seek(0)
            h._on_winch(signal.SIGWINCH, None)
            self.assertEqual((h.layout.rows, h.layout.cols), (24, 80))
            out = cap.getvalue()
        finally:
            h.close()
            sys.stdout = saved
        # The whole frame was repainted at the new size: the closing border is
        # on row 24 now, and nothing was written to a row beyond it.
        self.assertIn("\x1b[24;1H", out)
        beyond = [int(m) for m in re.findall(r"\x1b\[(\d+);1H", out) if int(m) > 24]
        self.assertEqual(beyond, [])
        # Every band, chrome included, is re-cut to the new width -- a stale
        # 100-column title band over a 80-column frame is the same overhang
        # sideways, and only a full repaint (not the log region alone) fixes it.
        for row in (1, 3, 6, 24):
            written = re.search(r"\x1b\[%d;1H([^\x1b]*)" % row, out)
            self.assertIsNotNone(written, "row %d was not repainted" % row)
            self.assertEqual(len(written.group(1)), 80)

    def test_the_handler_is_installed_while_open_and_restored_on_close(self):
        before = signal.getsignal(signal.SIGWINCH)
        h, cap, saved = self._open()
        try:
            self.assertEqual(signal.getsignal(signal.SIGWINCH), h._on_winch)
        finally:
            h.close()
            sys.stdout = saved
        self.assertEqual(signal.getsignal(signal.SIGWINCH), before)

    def test_a_pinned_geometry_does_not_follow_the_window(self):
        # FRAME_ROWS/FRAME_COLS are the launcher saying what size this frame is;
        # a window manager must not overrule that.
        before = signal.getsignal(signal.SIGWINCH)
        with unittest.mock.patch.dict(os.environ,
                                      {"FRAME_ROWS": "30", "FRAME_COLS": "90"}):
            h = framed.FramedUiHandler(splash_seconds=0)     # real _size: pinned
            cap, saved = io.StringIO(), sys.stdout
            sys.stdout = cap
            try:
                h.open()
                self.assertEqual((h.layout.rows, h.layout.cols), (30, 90))
                self.assertEqual(signal.getsignal(signal.SIGWINCH), before)
            finally:
                h.close()
                sys.stdout = saved

    def test_a_resize_after_close_paints_nothing(self):
        # The shell has been handed back; a late SIGWINCH must not draw a frame
        # onto it. And the handler never raises -- it interrupts arbitrary
        # main-thread code, so an exception here reads as a crash somewhere else.
        h, cap, saved = self._open()
        h.close()
        sys.stdout = saved
        cap.truncate(0)
        cap.seek(0)
        h._on_winch(signal.SIGWINCH, None)
        self.assertEqual(cap.getvalue(), "")

    def test_a_resize_inside_a_paint_waits_for_that_paint_to_finish(self):
        # The handler interrupts the main thread between bytecodes, so it can
        # land inside a _write that is part way through a frame. Painting the
        # new frame there would put half the old one after it.
        h, cap, saved = self._open(40, 100)
        try:
            painted = []

            class _Blocking:
                def write(_self, s):
                    painted.append(s)
                    if len(painted) == 1:        # mid-write, as if blocked on the tty
                        h._size = lambda: (24, 80)
                        h._on_winch(signal.SIGWINCH, None)
                        painted.append("<<resize landed here>>")

                def flush(_self):
                    pass

            h._real_stdout = _Blocking()
            h._paint_chrome()                    # one write; the resize lands inside it
            self.assertEqual(h.layout.rows, 24)  # it did happen
            marker = painted.index("<<resize landed here>>")
            # Everything the repaint wrote came AFTER the interrupted write, not
            # through it: the write that was in flight is painted[0].
            self.assertGreater(len(painted), marker + 1)
            self.assertNotIn("\x1b[40;1H", "".join(painted[marker:]))
        finally:
            h._real_stdout = cap
            h.close()
            sys.stdout = saved

    def test_the_write_lock_is_reentrant(self):
        # The handler runs in the MAIN thread between bytecodes, so it can land
        # inside a _write that already holds the lock. A plain Lock deadlocks
        # there; nothing else in the frame's testable surface shows this.
        h = framed.FramedUiHandler(splash_seconds=0)
        self.assertTrue(h._lock.acquire(blocking=False))
        try:
            self.assertTrue(h._lock.acquire(blocking=False))
            h._lock.release()
        finally:
            h._lock.release()


class TheFrameSurvivesAnInterruptedOpen(_NoColour):
    """BUG: `_start` called ctx.ui.open() OUTSIDE the try whose finally closes
    it. open() enters the alternate screen, hides the cursor and turns echo off,
    then holds the splash for two seconds -- ctrl-C in that window left the
    terminal wedged, and the only way out was to blind-type `reset`."""

    def test_start_closes_the_ui_when_the_splash_is_interrupted(self):
        from dashcam_exporter.application.workflow import pipeline as P
        from dashcam_exporter.application.ui import handler as ui_handler
        ctx = unittest.mock.Mock()
        ctx.selected_import = None
        ctx.ui.open.side_effect = KeyboardInterrupt
        saved_active = ui_handler.active()
        try:
            with self.assertRaises(KeyboardInterrupt):
                with contextlib.redirect_stdout(io.StringIO()):
                    P._start(ctx)
        finally:
            ui_handler.set_active(saved_active)
        ctx.ui.close.assert_called_once_with()

    def test_close_after_a_half_finished_open_still_restores_the_screen(self):
        # close() is what the finally now covers, and it has to survive an open()
        # that got part way: the alternate screen must be left and the cursor
        # shown even if the splash never ran.
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h._real_stdout = cap
            h._open = True                # as open() sets it, before the splash
            h.close()
        finally:
            sys.stdout = saved
        out = cap.getvalue()
        self.assertIn(framed.SHOW, out)
        self.assertIn(framed.ALT_OFF, out)


class TheMenuSpacerIsAStreamOnlyBlankLine(_NoColour):
    """The loop's per-turn blank moved from a bare `print()` onto the seam. The
    stream backend must still emit exactly the newline it always did -- that is
    the whole of what keeps its output byte-identical."""

    def test_the_stream_backend_prints_one_newline(self):
        from dashcam_exporter.application.ui.handler import StreamUiHandler
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            StreamUiHandler().menu_spacer()
        self.assertEqual(buf.getvalue(), "\n")

    def test_the_framed_backend_writes_nothing(self):
        h = framed.FramedUiHandler(splash_seconds=0)
        h._size = lambda: (24, 80)
        cap, saved = io.StringIO(), sys.stdout
        sys.stdout = cap
        try:
            h.open()
            cap.truncate(0)
            cap.seek(0)
            h.menu_spacer()
            self.assertEqual(cap.getvalue(), "")
            self.assertEqual(len(h._log), 0)
        finally:
            h.close()
            sys.stdout = saved


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
