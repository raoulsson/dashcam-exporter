# DOS-style framed UI via a UiHandler seam — design

Date: 2026-07-26 · Branch: `dos-ui`

## Goal

Give the operator tool an optional **DOS-style framed UI** — a fixed title/status
bar at the top, the numbered menu as a bar at the bottom, and the work (progress +
output) in the middle — while keeping the **exact same workflow** (the same eleven
menu items, the same typed-word delete gates, the same steps). The enabling change
is architectural: components stop writing to stdout directly and instead call a
single **`UiHandler`** seam, so the *same* workflow can drive either today's
scrolling output or the new frame.

Hard constraint: **do not destroy the currently working tool.** The current
scrolling behaviour stays the default; the frame is opt-in. All of it lands on the
`dos-ui` branch.

## Current state (verified, 2026-07-26)

- Output has **no chokepoint** today: ~320 I/O sites, ~178 in `pipeline.py`, 25 in
  `progress.py`, 7 in `screens.py`, ~75 in the child `renderer.py` (separate process).
- **Input is already centralized** in `application/ui/prompt.py` — `read_key`, `ask`
  (one builtin `input()`), `confirm` (one raw `termios` read). Tests patch this seam.
- `screens.py` already funnels its screens through **one sink**, `_print_all`, and its
  painters (`print_menu`, `_help_lines`, `_info_lines`, `_next_steps`, `print_summary`,
  `_about`) mostly **return strings**.
- `progress.py` (`Live`, `Bar`, `Waiting`, `_write_line`) writes ANSI **directly** to
  stdout — cursor up `\x1b[NA`, clear-below `\x1b[J`, clear-line `\x1b[2K`, hide/show
  cursor `\x1b[?25l/h`, carriage-return redraw.
- `term.py` returns strings (`rule`, `human_*`, colour class `C`) and detects width
  (`term_width`, floor 40). No direct writes except `C.enabled = isatty()`.
- The **child `renderer.py` is captured, not inherited**: `Child.start` pipes its
  stdout/stderr into the parent (`subprocess.PIPE`, `stderr=STDOUT`); `run_stream`
  reads it on a daemon thread, splitting on both `\r` and `\n`, and feeds lines to
  `Readout` → `Live.draw`. So every child line already passes through the parent.
- **No alt-screen, no full-screen clear** anywhere — the tool scrolls; it never takes
  over the screen.
- Two existing interposition precedents: `_RunLogTee` (swaps stdout, strips ANSI, tees
  to a per-run log) and `publishing.Console` (a plugin-facing `uploader.Ui` sink).

## Design

### The seam

One ABC, `UiHandler`, with a **semantic** vocabulary (not a string sink — a sink
cannot place content in regions). Methods, grouped:

```
chrome:    title(app, import_label)   status(facts)   menu(entries, selected)
work pane: log(line)   block(lines)   banner(lines)
           progress(label, fraction, detail)   progress_text(text)   done(label, summary)
input:     read_key(prompt)   ask(prompt, default)   confirm(prompt, default)
lifecycle: open()   close()
```

The active handler lives on `ctx.ui`. Components call `ctx.ui.log(...)` etc. instead
of `print`. The vocabulary is derived from the real output categories the map found:
menu paint, streamed log lines, live progress bar, marquee/segment text, the closing
`done_line`, destructive banners, help/info/summary blocks, prompts, title/status.

### Two backends, one vocabulary

- **`StreamUiHandler`** — every method calls **today's exact print/ANSI code**, so its
  output is byte-for-byte what the tool prints now. This is the **default** and what
  every test exercises.
- **`FramedUiHandler`** — routes each call to a region: `title/status` → top bar,
  `menu` → bottom bar, `log/block` → the scrolling middle, `progress/progress_text` →
  a **pinned status strip** at the bottom of the middle (the live bar never scrolls
  away), prompts → inline at the bottom of the middle.

### Layout (chosen: split — log + pinned bar)

```
+- dashcam-exporter ------ import 2026-07-19 -+   title + identity (fixed)
| 3 trips  2 rendered  workspace 22.4 GB     |   status facts (fixed)
+--------------------------------------------+
| clip 10/20  ok                             |   scrolling log / history
| clip 11/20  ok                             |   (help, info, summary,
| clip 12/20  ok                             |    streamed lines scroll here)
|............................................|
| Render ##########------ 62% 12/20  ETA 1:40|   PINNED live bar / marquee
+--------------------------------------------+
| 1)Import ... 10)Delete  p h i q   Select> _ |   menu bar (fixed, bottom)
+--------------------------------------------+
```

### Reroute scope

Only the **parent** process is rerouted:

- `screens.py`: painters keep returning strings; the Runner routes them to
  `ui.menu(...)` / `ui.block(...)` instead of `_print_all`.
- `progress.py`: `Live`/`Bar` stop writing stdout; the live signal becomes
  `ui.progress(...)` / `ui.progress_text(...)`, fed from `run_stream`.
- `pipeline.py`: the ~178 direct prints become the matching `ui.*` call, in batches by
  category (menu, progress, banners, help/info, errors, `done_line`).
- `prompt.py`: becomes a **thin façade** over `ctx.ui.{read_key, ask, confirm}` so the
  framed backend can render prompts in-frame; the stream backend keeps today's raw
  `termios`/`input()` behaviour. The patch points the tests use stay put.

The **child `renderer.py` is not changed** — it prints to its pipe; the parent
captures those lines through `run_stream` and calls `ui.log/ui.progress`. This halves
the surface and keeps the renderer a standalone CLI.

### Framed tech: manual ANSI (not curses)

The frame is built from primitives already present plus three additions: alt-screen
(`\x1b[?1049h` / `\x1b[?1049l`), absolute cursor positioning (`\x1b[row;colH`), and a
`SIGWINCH` handler that recomputes regions and repaints. Manual ANSI keeps both
backends symmetric ("format + write"), avoids curses' non-tty/test friction, and fits
a codebase that already does raw ANSI and avoids dependencies. (Curses is the
fallback if manual proves fiddly, but is not the plan.)

### Selection & safety

- Framed mode activates only when `stdout.isatty()` **and** explicitly enabled:
  `ui_style = framed` in `config.txt` (default `stream`), env override `SET_UI_STYLE`.
- Non-tty, piped output, and **every existing test** resolve to `StreamUiHandler`,
  unchanged. The 677 tests keep patching `prompt` + `redirect_stdout` and stay green.
- `FramedUiHandler.close()` (and a crash/`finally` path) always leaves the alt-screen
  and restores the cursor — a framed crash must never leave a wedged terminal.

## Phasing (branch `dos-ui`)

- **P0 — seam, no behaviour change.** `UiHandler` ABC + `StreamUiHandler` + `ctx.ui`
  wiring; route the three already-centralized sinks (`screens._print_all`,
  `progress.Live/Bar`, `done_line`). Acceptance: tests green **and** a captured-output
  diff of a dry run is identical to `master`.
- **P1 — finish the parent reroute.** The ~178 `pipeline.py` prints move to `ui.*` in
  category batches; `prompt` becomes the façade. Acceptance: tests green after each
  batch; output still diff-identical under `StreamUiHandler`.
- **P2 — `FramedUiHandler`.** Chrome, region math, scrolling log, pinned bar, inline
  prompts, `SIGWINCH`, alt-screen enter/leave, crash-safe restore.
- **P3 — selection + tests + polish.** `ui_style` wiring; a framed-specific test that
  drives region math / wrapping / resize against a fake terminal size; manual look pass.

## Testing strategy

- The existing suite is the regression net for the reroute: it runs under
  `StreamUiHandler` and must stay green and output-identical through P0–P1.
- A P0 "golden output" check: capture a scripted dry-run's stdout on `master` and on
  the branch under `StreamUiHandler`; assert identical. This is what proves "exact same
  look and feel" for the default path.
- `FramedUiHandler` gets unit tests for pure region math (given a terminal size, where
  do the title/log/bar/menu rows land; how does a long line wrap/clip; what does a
  resize recompute) — no real terminal needed, since layout is pure given a size.
- The delete-gate tests are untouched: they drive `prompt` + the menu machine, both of
  which keep their seams.

## Risks & mitigations

- **Rerouting a delete-gate-bearing file (`pipeline.py`).** Mitigation: the reroute is
  mechanical (print → `ui.*`), never touches guard logic; category batches with tests
  green each; golden-output diff catches any drift.
- **A framed crash wedging the terminal.** Mitigation: alt-screen leave + cursor
  restore in `close()` and a top-level `finally`, mirroring the existing
  `show_cursor()` panic path.
- **Interface churn.** Mitigation: `StreamUiHandler` is written first against the real
  output, so the vocabulary is validated against current behaviour before the frame
  consumes it.

## Out of scope (YAGNI)

Mouse input; colour themes/skins beyond what exists; resizable/movable panes; a config
UI inside the frame; touching the child renderer's own printing; Windows console
support (macOS/Linux ANSI only, matching the current tool).
