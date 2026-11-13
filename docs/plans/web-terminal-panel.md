# Plan: A toggleable terminal panel in the web editor

## Context

`tod serve` offers a three-pane web UI — explorer/source control, editor, and
agent console — but no shell of its own. Any command run by hand has to go
through the agent's gated shell tool and lands in the transcript. This adds a
real integrated terminal: a topbar toggle opens a PTY-backed shell over the
bottom third of the editor pane, and clicking the toggle again dismisses it
without killing the shell.

Design principles:

- **A real terminal, not a command runner.** A PTY plus xterm.js gives
  interactive programs, job control, colors, and TUI applications.
- **Loopback first.** The terminal is an ungated shell, so it stays off the
  network unless explicitly opted in.
- **Small dependency surface.** xterm.js and its fit addon; nothing else.

## Behavior

- A `>_` icon button joins `.topbar-actions` beside the theme toggle, with
  `aria-pressed` reflecting the panel state.
- First click opens the panel; the next click hides it. Hiding keeps the
  shell alive — the panel is hidden with `v-show`, never unmounted. A page
  reload disconnects and kills the shell.
- The panel overlays the bottom of `.pane-editor` only: absolutely positioned,
  full pane width, default height **33 %** of the pane. A 5 px top edge is a
  drag handle; the height clamps to [15 %, 85 %] and persists in
  `localStorage` as `tod.terminal` (`{ "height": 33 }`). Open/closed is **not**
  persisted: every page load starts with the panel closed.
- A slim header names the panel **Terminal** and carries four icon actions:
  **Add Terminal** (＋), **Delete Terminal** (trash), **Maximize Panel** (a
  chevron, turning into **Restore Panel** while maximized), and **Hide
  Panel** (an ×). Maximize fills the editor pane and Restore returns to the
  last dragged height; Hide is the same close path as the topbar toggle, so
  the shells stay alive. Dragging the top edge all the way up is the same
  gesture — it snaps into maximize — and pulling the edge back down leaves
  the maximized state.
- The panel can hold several terminals. They share one panel; a vertical
  rail on its right edge gives each a numbered tab. Add appends and selects
  a new terminal, Delete closes the active one (the neighbour takes over),
  and clicking a tab switches to that terminal. Closing the last one closes
  the panel.
- Opening focuses the active terminal. The editor underneath keeps its
  tabs, scroll, and cursor untouched.
- Each shell is `$SHELL -l` (fallback `/bin/sh`), started in the served repo
  root with `TERM=xterm-256color` and `COLORTERM=truecolor`. One shell per
  WebSocket connection, i.e. one per terminal tab.
- When a shell exits (`exit`, Ctrl-D, or a killed process), its tab closes;
  when it was the last, the panel closes too. Opening the panel again
  starts one fresh shell.
- The xterm palette follows the app theme (built from the `--editor-bg`,
  `--text`, syntax, cursor, and selection variables) and is rebuilt when the
  theme flips. Font is the editor's `--mono` stack at 13 px.

## Backend: `toddler/web/terminal.py`

A dedicated WebSocket endpoint `/ws/terminal`, separate from the existing
JSON `/ws` protocol: terminal output is raw bytes and must never be parsed as
JSON.

### Protocol

Client → server (JSON text frames):

- `{"type":"input","data":"…"}` — keystrokes and paste; encoded UTF-8 into
  the PTY.
- `{"type":"resize","cols":N,"rows":N}` — clamped to 1..500 × 1..300. Applied
  at spawn (default 80×24 until the first one arrives) and again on every
  change.

Server → client:

- `{"type":"ready","shell":"/bin/zsh","cwd":"/repo"}` — accepted; shell
  spawned.
- **Binary frames** — raw PTY output forwarded verbatim (`send_bytes`). No
  decoding on the server: xterm decodes, and a UTF-8 character split across
  two reads stays intact.
- `{"type":"exit","code":N}` — sent after the last output; the socket then
  closes.
- `{"type":"error","message":"…"}` — unavailable (unsupported platform,
  disabled, spawn failure); the socket closes.

### Lifecycle

- `pty.openpty()`; set the initial window size from the client's first
  resize.
- Spawn with `asyncio.create_subprocess_exec(shell, "-l", stdin=slave,
  stdout=slave, stderr=slave, cwd=repo_root, env=…, preexec_fn=…)`. The
  preexec calls `os.setsid()` then
  `fcntl.ioctl(0, termios.TIOCSCTTY, 0)` so the shell owns the tty — without
  it bash/zsh report "no job control in this shell" and Ctrl-C does not
  reach the foreground job. Close the slave fd in the parent after spawn.
- Reader: `loop.add_reader(master_fd, …)` pushes each
  `os.read(master_fd, 65536)` chunk onto an `asyncio.Queue`; a sender task
  forwards them as binary frames. An empty read or `OSError` is EOF and
  enqueues the exit sentinel — the sender then flushes every queued byte
  before sending `exit`, so output never races the exit frame.
- Receiver: `async for raw in websocket.receive_json()`. `input` writes
  `os.write(master_fd, data.encode())`; `resize` runs `TIOCSWINSZ` (the
  kernel signals SIGWINCH to the tty's foreground process group).
- In `finally` (exit or disconnect):
  - remove the reader and close the master fd (closing it SIGHUPs the
    session);
  - `proc.terminate()`, wait up to ~2 s, then `proc.kill()`;
  - cancel the pump tasks.
  No shell survives its connection.
- POSIX only. On Windows (`os.name != "posix"`) the endpoint accepts, sends
  an `error` frame, and closes — no fake terminal.

### Security

The rest of the UI is file editing plus a gated agent; the terminal is an
ungated shell, so it gets two gates:

- **Loopback client** — `websocket.client.host` must be `127.0.0.1`/`::1`
  (the Vite dev proxy connects from loopback, so it passes). A LAN client
  gets an `error` frame and a close.
- **Origin** — a browser page's `Origin` header must equal the request's
  `Host` origin (in dev, `http://localhost:5173` is also allowed). An absent
  `Origin` is allowed — curl, native clients, and the test client. This
  blocks cross-site WebSocket hijacking from another page in the same
  browser.
- `run_server` computes `terminal_enabled = is_loopback(host)`; binding
  `--host 0.0.0.0` disables the terminal unless the new `--allow-terminal`
  flag opts in. `/api/meta` gains `"terminal": bool` so the frontend knows
  not to offer a dead button.

### Wiring

- `toddler/web/app.py` — include the terminal router next to the others,
  before the static mount; `create_app` takes `terminal_enabled` (default
  false) and `run_server` enables it for loopback binds.
- `toddler/web/state.py` — `WebAppState.terminal_enabled`.
- `toddler/web/server.py`, `toddler/utils/cli.py` — loopback check and the
  `--allow-terminal` flag.
- `toddler/web/api.py` — `meta` returns `terminal`.

## Frontend

### Dependencies

- `@xterm/xterm@^6.0.0`
- `@xterm/addon-fit@^0.11.0`
- Import `@xterm/xterm/css/xterm.css` from the component (Vite bundles it).

xterm 6's default DOM renderer is enough; no WebGL/canvas addon.

### `website/src/components/TerminalPanel.vue`

Props: `visible: boolean`, `theme: 'tokyo-night' | 'github-light'`. It owns
the xterm instance and socket; App owns position and height.

- **Lazy init** — the first `visible === true` creates `new Terminal({…})`,
  loads `FitAddon`, opens it into a host div, connects the WebSocket, and
  attaches `term.onData`. Before that nothing runs and no shell exists.
- **Socket** — URL mirrors `useWebSocket`:
  `${ws|wss}://${location.host}/ws/terminal`, `binaryType = 'arraybuffer'`.
  - `onopen` → fit, send the initial `resize`.
  - `onmessage` → `ArrayBuffer`/`Blob` → `term.write(new Uint8Array(…))`;
    string → `term.write(…)` (the mock sends both shapes).
  - `exit` frame → write `\r\n[process exited with code N]\r\n`, mark the
    session dead, and emit `exit` so App closes the panel.
  - Unexpected close → write `\r\n[terminal disconnected]\r\n`.
  - Toggling hidden → visible after a dead session reconnects (fresh shell);
    a live session is untouched.
- **Input/resize** — `term.onData(data)` → `{type:'input', data}`.
  `term.onResize(({cols, rows}))` → `{type:'resize', cols, rows}`; FitAddon
  fires it on every refit.
- **Refit** — a `ResizeObserver` on the host calls `fit()` on a
  `requestAnimationFrame`, skipped while the host has zero size. A watcher
  on `visible` refits and focuses after `nextTick`.
- **Scrollback** — 5000 lines, and `scrollOnEraseInDisplay: true`: `clear`
  (ED2) scrolls the erased viewport into scrollback instead of wiping it,
  so the output stays reachable with the wheel.
- **Scrollbar** — xterm draws its own, so the global scrollbar rules do not
  reach it; `terminal-panel.css` matches the editor's look: an 8px rounded
  `--editor-border` thumb on a transparent track, brightening on hover/drag.
- **Theme** — build the xterm `theme` from
  `getComputedStyle(document.documentElement)` (`--editor-bg`, `--text`,
  `--cursor`, `--selection`, and the syntax colors for the ANSI slots), and
  rebuild it when the `theme` prop changes.
- **Cleanup** — unmount closes the socket, disposes the terminal, and
  disconnects the observer.

### Editor scroll under the panel

- `FileEditor` adds CodeMirror's `scrollPastEnd()` to non-empty buffers: a
  page of bottom padding lets every line reach the top of the editor, so
  the end of the file can be scrolled clear of the panel.
- The native editor scrollbar is painted with the `.cm-scroller` and thus
  sits under the panel. While the panel is open,
  `website/src/components/EditorScrollbar.vue` replaces it: the native bar
  is hidden (`.editor-cm.terminal-open .cm-scroller`), and an 8px overlay
  bar in the editor body mirrors the scroller's geometry and drives its
  `scrollTop`. The bar's track stops at the panel's top edge (its measured
  height is passed down as `terminalOverlap`), so the chrome sits above the
  terminal instead of over it. The SCM ruler is scaled by the same ratio
  (`transform: scaleY`) into that identical span, sits on the scrollbar's
  8px column in both states (the overlay bar here, the native bar when the
  panel is closed; ticks centred in the column), and its marks are
  normalised over the document height without `scrollPastEnd`'s bottom
  padding — so the last mark meets the ruler's bottom just as the
  scrollbar's range ends there. Closing the panel restores the native bar.

### `App.vue` and layout

- State: `terminalOpen = ref(false)`, `terminalPct =
  ref(readStored('tod.terminal', …, 33))` clamped to [15, 85], plus drag
  state mirroring the horizontal dividers.
- Terminals: `terminals = ref<number[]>([])` and `activeTerminalId`; ids
  are never reused. Add pushes an id and selects it; Delete removes the
  active id and selects the neighbour (or closes the panel when none is
  left); an `exit` frame follows the same remove path. Opening the panel
  with no terminals creates one.
- Drag: pointer capture on the handle; height % is
  `(paneBottom - clientY) / paneHeight * 100`, persisted on pointerup.
  Reaching 100 % sets the maximized flag (restoring from the drag's start
  height), pulling below it clears the flag and follows the pointer; a drag
  that ends maximized stores no height. The handle stays mounted while
  maximized so the pull-back works.
- Markup:
  - Topbar icon button before the theme toggle, sharing the icon-button
    chrome (generalize `.theme-toggle` to a `.topbar-icon` class used by
    both), `:class="{ active: terminalOpen }"`, `aria-pressed`.
  - `.pane-editor` gets `position: relative`. After `FileEditor`, add
    `<div v-show="terminalOpen" class="terminal-panel"
    :style="{ height: terminalPct + '%' }">` containing the 5 px
    `.terminal-resize-handle`, the header, and a `.terminal-body` split
    into the `.terminal-stage` (one absolutely positioned `.terminal-slot`
    per terminal, `v-show`-ed on the active id) and the `.terminal-tabs`
    rail on the right.
  - Each slot owns one `<TerminalPanel :visible="terminalOpen &&
    id === activeTerminalId" @exit="removeTerminal(id)" />`; hidden slots
    stay mounted, so their xterm buffers and sockets survive a tab switch.
  - The overlay sits above the editor (`z-index`); the editor keeps its
    state underneath.
- Availability: fetch `/api/meta` on mount and hide the toggle when
  `terminal` is false (default true until it answers).
- CSS: new `website/src/assets/styles/components/terminal-panel.css`
  imported by `assets/styles/index.css`; panel background `--editor-bg`,
  1 px `--editor-border` top edge, handle `row-resize` with the accent on
  hover.

## Tests and fixtures

### Backend — `tests/test_web_terminal.py`

- Connect; read `ready`; send
  `{"type":"input","data":"echo marker-$((1+1))\n"}`; collect binary frames
  until `marker-2` appears.
- Resize is applied: send `resize` then `stty size` and read `ROWS COLS`.
- `exit\n` → remaining output, then `{"type":"exit","code":0}`.
- Origin `http://evil.example` → handshake refused/closed.
- `terminal_enabled=False` → `error` frame and close.
- Client disconnect cleans up: the child process is reaped and the endpoint
  returns (no hang, no leak).
- Pin `SHELL=/bin/sh` via `monkeypatch` for deterministic output; skip on
  non-POSIX.

### Mock server — `website/scripts/mock-server.mjs`

- A second `WebSocketServer({ server, path: '/ws/terminal' })` — the existing
  server's `path: '/ws'` does not match `/ws/terminal`.
- On connection send `\r\nmock terminal ready\r\n$ ` as a Buffer; echo each
  `input` back, and on a newline send another prompt. Ignore `resize`. No
  real PTY in Node.

### UI — phases 27–28 in `website/scripts/ui-test.mjs`

- Toggle exists; click → `.terminal-panel` visible, one tab, and its height
  is ≈ 33 % of `.pane-editor` (±3 points).
- The header reads "Terminal" with Add/Delete/Maximize/Hide icon actions;
  maximize fills the pane and relabels to "Restore Panel", restore returns
  to the stored height, and Hide closes the panel like the topbar toggle.
- Add opens a second tab with its own socket and screen; switching tabs
  keeps each screen intact; Delete closes the active tab and its
  neighbour takes over.
- Hide preserves every terminal; a reload resets to one fresh tab on open.
- Type `echo one` + Enter; `.xterm-rows` shows the input and the mock's
  echo and prompt.
- Reclick → panel hidden; click again → earlier output is still there (the
  component stayed mounted).
- Drag the handle up/down → height changes and clamps to [15, 100]; dragging
  to the pane's top maximizes, dragging back down restores; reload → the
  panel starts closed, and the dragged height returns when reopened.
- Switching the theme while open repaints the xterm background.
- `clear` empties the viewport but keeps the output in scrollback (a
  wheel-up reaches it).
- `exit` closes only that terminal's tab; the last one closes the panel,
  and reopening starts a shell whose screen has no content from the
  previous one.
- With the panel open, the editor's overlay scrollbar and SCM ruler stop at
  the panel's top edge (neither overlaps the terminal), the ruler occupies
  the bar's 8px column with centered ticks and the same height, and its
  last mark reaches the ruler's bottom. Scrolling to the end puts the last
  line at the top of the editor, clear of the panel; dragging the overlay
  bar scrolls the editor; hiding the panel brings the native bar back with
  the ruler still on its column.

## Verification

```bash
# backend
.venv/bin/python -m pytest tests/test_web_terminal.py tests/test_web_server.py -q
.venv/bin/ruff check toddler/web tests/test_web_terminal.py

# frontend typecheck + production build
cd website && npm install && npm run build

# UI (fresh mock per run)
node website/scripts/mock-server.mjs
cd website && npx vite --config mock-vite.config.ts
BASE_URL=http://localhost:5199/ PW_CHANNEL=chrome \
  node website/scripts/ui-test.mjs
```

Manual: `tod serve --dev` with `npm run dev` in `website/`; toggle the panel;
run `ls`, `vim`/`top`, Ctrl-C, `exit`; drag the handle; flip both themes;
`tod serve --host 0.0.0.0` → the toggle is absent and the endpoint refuses.

## Non-goals / known limitations

- No terminal tabs, splits, or multiple shells per page.
- No scrollback or session persistence across reloads; a reload kills the
  shell.
- A dead shell is replaced by a fresh one on the next open — no resume.
- POSIX only; no ConPTY/Windows support.
- No tmux-style detach; the shell's lifetime is exactly the WebSocket's.
- No auth beyond loopback + Origin checks. `--allow-terminal` on a
  non-loopback bind is only safe on a trusted network or behind a proxy.

## Commits

1. `docs: plan the web terminal panel` — this document.
2. `feat(web): serve a pty over a websocket` — `terminal.py`, state/app/api/
   server/cli wiring, backend tests.
3. `feat(web): add a toggleable terminal panel` — xterm dependencies,
   component, App/topbar/layout CSS, meta gating.
4. `test(web): cover the terminal panel` — mock endpoint and UI phase.

Each commit leaves `pytest`, `npm run build`, and the mock UI run green.
