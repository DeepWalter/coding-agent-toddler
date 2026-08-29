# Web Frontend: `tod serve`

## Goal

Browser-based GUI with a split-pane layout — file viewer/editor on the left,
agent console with scrollback on the right — coexisting with the CLI via a
`tod serve` command, sharing the same agent backend and SQLite session database
(README roadmap item).

## Decided Stack

- **Backend**: FastAPI + uvicorn (new runtime deps)
- **Frontend**: Vue 3 + Vite build (Composition API, `.vue` SFCs), living in
  `website/` at repo root
- Python ≥3.11, ruff (line 100), pytest `asyncio_mode="auto"`

## Target Architecture

```
tod serve (main.py) ──→ toddler/web/
                           ├─ app.py        create_app(): lifespan wiring, routers, static mount
                           ├─ state.py      WebAppState (settings, db, storage_mgr, llm, session_mgr, runner, repo_root, dev)
                           ├─ events.py     AgentEvent → JSON dict serializers
                           ├─ runners.py    TurnRunner: busy gate, queue fan-out, cancel, paused snapshot
                           ├─ ws.py         /ws WebSocket router + command dispatch
                           ├─ api.py        /api REST router (meta, sessions, messages, tree, file, git)
                           ├─ git.py        git status/diff (porcelain parsing, unified-diff fetch)
                           ├─ diffparse.py  pure unified-diff parser (no git dependency)
                           ├─ files.py      path-safe read/write + gitignore-aware tree walk
                           └─ server.py     run_server() — uvicorn bootstrap
website/  (Vue 3 + Vite)
  ├─ vite.config.ts    @vitejs/plugin-vue; proxy /api + /ws → http://localhost:8000
  └─ src/              App.vue, main.ts, types.ts, api.ts, diff.ts (side-by-side row
                       alignment), composables/ (useWebSocket, useConsole, useGitStatus),
                       components/ (ConsolePane, MessageBubble, ToolCard, PlanCard,
                       PausePrompt, InputBar, FileExplorer, FileEditor, SourceControl,
                       DiffView, SessionList, StatusBar)
```

The agent core is already transport-agnostic: `SessionManager.process_turn()`
yields plain `AgentEvent` dataclasses (toddler/agent/events.py); the Rich
renderer is one swappable consumer. Approval (`approve_plan`/`reject_plan`/
`approve_tool_call`/`deny_tool_call`), permission modes, and cancellation
(`agen.aclose()`) are synchronous set-style calls safe from any asyncio task.
The SQLite `messages` table provides full transcript replay.

## Wiring

`create_app(settings, *, repo_root=Path.cwd(), dev=False)` builds
`SQLiteDatabase(settings.session_dir / "sessions.db")` → `StorageManager` →
`OpenAICompatibleProvider` → `SessionManager` in its lifespan (same wiring as
toddler/main.py), calls `await session_mgr.resolve()` (fresh session — CLI
parity), then:

- mounts `/api` router and `/ws` websocket router
- serves `website/dist` via `StaticFiles(html=True)` as the **last** mount so
  it never shadows `/api`/`/ws`; if `website/dist/index.html` is missing, `/`
  returns a JSON hint ("frontend not built — run `npm run build` in website/")
- adds CORS for `http://localhost:5173` when `dev=True` (belt-and-braces; the
  primary dev path is the Vite proxy)

### `tod serve` CLI

Subcommand (not flag) in `toddler/utils/cli.py`:

```
tod serve [--host 127.0.0.1] [--port 8000] [--no-open] [--dev]
          [--model M] [--base-url URL] [--api-key K] [--no-stream] [--max-iterations N]
```

- `--no-open`: don't auto-open the browser (threading.Timer + webbrowser)
- `--dev`: enable dev CORS
- shared LLM flags so `Settings.from_cli` overlays work (`getattr` fallthrough)

Branch in `toddler/main.py` right after `Settings.from_cli` (before DB/LLM
wiring — the app factory does its own):

```python
if args.command == "serve":
    from toddler.web.server import run_server
    run_server(settings, host=args.host, port=args.port,
               open_browser=not args.no_open, dev=args.dev)
    return
```

`setup_logging` still runs first, so server logs land in `~/.toddler/logs`.

**pyproject.toml**: add `fastapi>=0.115`, `uvicorn>=0.30`; dev extra
`httpx>=0.27` (TestClient). `website/` is not a Python package — no hatchling
changes; `website/dist` won't ride along in an sdist (fine for a checkout-based
personal tool; `[tool.hatch.build.force-include]` only if a pip-installed
distribution matters).

## WebSocket Protocol (`/ws`)

### Client → server (JSON `{"cmd": ...}`)

| cmd | payload | allowed while busy? |
| --- | --- | --- |
| `turn` | `input`, `force_plan` | no — rejected with `{"type":"error","code":"busy"}` |
| `cancel` | — | yes |
| `approve_tool` | `tool_id` | yes |
| `deny_tool` | `tool_id` | yes |
| `approve_plan` | `plan_id`, `mode: "manual"\|"auto"` | yes |
| `reject_plan` | `plan_id`, `feedback` | yes |
| `set_mode` | `mode: "manual"\|"auto"` | yes |
| `new_conversation` | `title` | no |
| `switch_session` | `session_id` | no |
| `ping` | — | yes |

Approval/deny/cancel stay available during a run — parity with the CLI's
separate input task. `new_conversation`/`switch_session` swap state under a
running agent, hence busy rejection.

### Server → client (JSON `{"type": ...}`)

```
hello              {session: {id, title, mode_label, permission_mode, context_usage_pct, model, cwd},
                     conversation: {id, sequence_num, title},
                     busy, paused, plan, messages: [replay from storage_mgr.get_messages()]}
turn_started
state              {busy: true|false}
text_delta         {text}
tool_call_start    {tool_id, tool_name, partial_input}
tool_call_delta    {tool_id, input_delta}
tool_call_end      {tool_id, tool_name, input, result: {success, output, error, checkpoint_id, metadata}}

Note: streaming mode emits `tool_call_start` **twice per call** — once
live from the stream handler and again from the execution phase, same
`tool_id` — followed by a single `tool_call_end`.  Clients must upsert
cards keyed by `tool_id`, not append.

`new_conversation` and `switch_session` ack directly, then **broadcast a
full `hello` frame** (session + conversation + transcript replay) so
every tab re-syncs — the same frame connections get on connect, and the
client applies it with the same "replace everything" reducer path.  An
earlier `conversation_switched {conversation}` frame was dropped from
the protocol: it carried no session info and no replay, so it couldn't
fulfill "session switch replays history" on its own.
plan_proposed      {plan: {id, title, summary, steps, rationale, risks, estimated_files_touched}}
plan_step_update   {steps: [[id, description, status], ...]}   # complete snapshot — replace, don't diff

``hello.plan`` replays a pending proposal as ``{plan, steps}`` (the
``plan_proposed`` payload plus the latest ``plan_step_update`` rows,
``[]`` before execution starts) — the runner snapshots ``PlanProposed``
exactly like ``AgentPaused``, so a reconnecting or new tab re-renders
its PlanCard instead of losing it mid-approval.  The snapshot is
cleared when the turn ends; it dies with the server process, so a
server restart still loses the plan (and the turn) — the stranded-approval
guard treats a plan wait like a tool pause, so a single tab dropping out
mid-approval auto-cancels the turn rather than hanging it.

agent_paused       {prompt, choices}
agent_finished     {reason, usage: {input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}}
recoverable_error  {message}
fatal_error        {message}
ack                {cmd, accepted}
turn_cancelled
pong
error              {code, message}
```

Type strings are the snake_case event class names. `serialize_plan` maps `Plan`
fields explicitly (Plan has no `to_dict`); same for `ToolResult` and
`TokenUsage`.

### Turn lifecycle

`TurnRunner` (one per active session) owns the `process_turn` generator in an
**app-lifetime asyncio.Task** — never owned by a WS connection:

- `start()` takes an `asyncio.Lock`, returns `False` if busy (one turn at a
  time — `SessionManager` holds single-session state)
- events are serialized and broadcast into per-connection `asyncio.Queue`s
  (multi-tab watching works for free)
- `AgentPaused` payload is snapshotted (`_paused_snapshot`), cleared on
  `AgentFinished`/`FatalAgentError`; reconnecters get it in `hello`
- `PlanProposed` is snapshotted the same way (`_plan_snapshot`), with
  `plan_step_update` rows merged in as they stream; `hello.plan` carries
  it so a reconnecting tab keeps its PlanCard
- `cancel()` → `task.cancel()`; the runner broadcasts `turn_cancelled`, closes
  the generator (`await gen.aclose()` — safe no-op on a stopped generator),
  clears the snapshot, broadcasts `state {busy: false}`

WS handler structure: accept → subscribe queue → spawn a separate drain task
for the queue → send `hello` → loop `ws.iter_json()` dispatching commands. On
disconnect: cancel drain, unsubscribe. **Stranded-approval guard**: if
`paused_snapshot` is set and the last subscriber leaves, auto-cancel the turn
(a pending approval nobody can answer would hang forever).

## REST API (`/api`)

```
GET  /api/meta                    -> {repo_root, model, dev}
GET  /api/sessions                -> {sessions: [{id, title, created_at, updated_at, message_count}]}
POST /api/sessions                -> row only (activation is a WS command)
GET  /api/sessions/{sid}/messages?conversation_id=  -> {messages: [{sequence_num, role, content}]}
GET  /api/tree?depth=4            -> {root, entries: [{path, type}]}
GET  /api/file?path=rel           -> {path, content, total_lines}
PUT  /api/file?path=rel           body {"content": "..."} -> {ok, bytes}
GET  /api/git/status              -> {branch, files, dirs, sections}
GET  /api/git/diff?path=rel&staged=0|1  -> {path, staged, binary, truncated, old_path, new_path, hunks}
```

File endpoints use **pathlib directly** — the `ReadFile` tool returns
line-numbered output, which the editor must not parse. Path safety:
`resolve_relative` enforces containment (`path.resolve().is_relative_to(root.resolve())`),
400 on escape, 404 on missing, 400/409 on binary. Tree walk reuses
`GitignoreMatcher` (toddler/context/workspace.py:86) + an `IGNORED_TOP` set
(`.git`, `node_modules`, `.venv`, `dist`, `__pycache__`, ...).

### Git status (`GET /api/git/status`)

Whole-snapshot git state for badges and the source-control panel; clients
fetch it (`cache: no-store`) instead of receiving it over WS.

- `branch`: current branch name, or `null` outside a git repository
- `files` / `dirs`: path → one letter per changed path; `dirs` maps every
  parent directory of a changed file (most severe descendant wins), so
  explorer folders signal changes even below the tree's depth limit
- `sections`: `{staged: {path: letter}, unstaged: {path: letter}}` — the same
  paths split by axis. Staged = index vs HEAD (porcelain X-axis letters,
  `??` excluded), unstaged = worktree vs index (Y-axis; untracked `??` →
  `U`). An `MM` file appears in both, with the same letter in each; unmerged
  pairs (`UU`, `AA`, `DU`, ...) become `C` in both. Letters: `M A D R U T C`
  (copies collapse to `R`, matching the explorer's `files` map).

### Git diff (`GET /api/git/diff`)

Parsed unified diff of one path (`--unified=3`, no color, path-limited with
`--`). `staged=1` diffs index vs HEAD, `staged=0` worktree vs index.
Untracked files (no index entry) diff against `/dev/null` via
`git diff --no-index` — which exits **rc 1 on differences** (the success
case); rc 1 with a non-empty stderr means the file vanished → 404. Errors:
400 escape/directory, 404 missing file, 500 git failure, all as
`{"error": msg}`.

Payload: `hunks` is an array of `{old_start, old_count, new_start,
new_count, lines}`; each line is `{kind: "ctx"|"del"|"add", old_ln,
new_ln, text, no_newline}` (`old_ln`/`new_ln` null on the side the line
does not exist, e.g. all `new_ln: null` for a deleted file). `old_path` /
`new_path` are `null` when that side is `/dev/null` (added/deleted files);
`binary: true` stops parsing (binary files have no hunks); `truncated: true`
when the parser hit its line cap. A clean file returns empty hunks. Header
lines (`diff --git`, `index`, rename/mode metadata) are never parsed for
paths — only `--- /dev/null` / `+++ /dev/null` presence is used; rename
detection is defeated by the `--` pathspec, so a rename renders as a
full-file addition (accepted, noted as future work).

No plan/checkpoint REST endpoints in MVP — plans stream over WS and the
runner snapshots a pending proposal into `hello` (see protocol above), so
plans survive a client reconnect but not a server restart.

## Frontend (Vue 3 + Vite)

- State: Composition API — a `useConsole` composable exposing `ref`/`reactive`
  state for the console (single linear event stream, applied by a plain
  reducer-style function so the transition logic stays pure and testable) + a
  `useWebSocket` composable (auto-reconnect with backoff 1s→10s). No Pinia
  (tree too shallow), no Monaco (textarea editor for MVP).
- On `hello`: replace console state with transcript replay, resume `busy`/`paused`
  (reconnect mid-approval shows the pending prompt again) and re-render a pending
  PlanCard from `hello.plan` — a card whose steps already show progress is treated
  as decided (no re-offered approve/deny buttons).
- Console rendering: `text_delta` appends to the tail assistant block;
  `tool_call_start/end` render collapsible cards (spinner while open, red on
  error); `plan_step_update` replaces step chips; `agent_paused` → approve/deny
  buttons; `plan_proposed` → PlanCard with steps + approve / approve+auto /
  deny + feedback input.
- Input: send disabled while busy, Cancel button instead; Enter=send,
  Shift+Enter=newline.
- Editor: `Ctrl/Cmd+S` → PUT `/api/file`; explorer has a refresh button so
  agent-created files appear.
- Layout: top bar (status dot, mode toggle, session select, new-conversation),
  split-pane with drag divider (clamp 20–80%), footer status bar.
- Dev: `npm run dev` (Vite :5173, proxy to :8000) against a running `tod serve`.
  Prod: `npm run build` once, then `tod serve` alone.

### Source control & diff view

- **Activity bar**: 48px icon rail on the far-left edge of the split pane
  (VSCode-style). Two buttons — Explorer and Source Control — each with an
  active state (inset 2px accent bar); clicking the active icon toggles the
  pane closed. Source Control shows a badge pill with the changed-file count
  (staged + unstaged). The open pane persists in `tod.leftpane`.
- **Source Control panel** (`SourceControl.vue`): header with branch name +
  ↻ refresh; two collapsible sections, **Staged Changes** and **Changes**,
  from `git.sections` with counts, sorted rows (dimmed dir prefix +
  full-weight basename), git letter badges, muted empty states, and a single
  "not a git repository" line when `branch` is null.
- **Diff tabs**: clicking a row opens a read-only diff tab in the editor
  pane — staged rows open the staged diff, unstaged rows the unstaged one;
  an `MM` file has two distinct tabs. Tabs are `TabEntry = file | diff`,
  keyed `f:<path>` / `d:s|u:<path>` so a file and its diffs coexist; only
  file tabs persist to `tod.tabs` (a reload restores files, never diffs).
- **Side-by-side diff** (`DiffView.vue` + `diff.ts`): two columns rendered
  from the same `buildRows()` alignment (positional del/add pairing — no
  `@@` hunk-header lines), line-number gutters, `color-mix` del/add
  backgrounds, sticky column headers, synced vertical scroll, independent
  horizontal scroll (pinned sticky gutters). Untracked → empty old side;
  deleted → empty new side; binary → centered message; truncated output is
  labeled. The CodeMirror host stays mounted while a diff tab is active —
  the `.editor-body` hides with `display: none`, never unmounts.
- **Freshness**: DiffView remounts per activation (keyed by tab key), so a
  re-activated diff refetches after agent writes; a ↻ button refetches in
  place; editor saves and agent file-writes trigger the shared git-status
  refresh (badges + section lists update).

## Development Roadmap

Milestones map onto the Implementation Steps below; each milestone is usable on
its own before the next one starts.

| Milestone | Steps | Exit criteria |
| --- | --- | --- |
| **M1 — Backend foundation** | 1–3 | `tod serve` boots, `/api/meta` + file/session REST work, agent turns stream over WS with approvals, deny, and cancel; backend pytest suite green |
| **M2 — Console MVP** | 4 | Browser connects, replays history, streams a live turn with tool cards, cancels mid-turn, survives server restarts (reconnect + replay) |
| **M3 — Split-pane & interactivity** | 5–6 | File explorer/editor with Ctrl/Cmd+S save; plan cards with approve/reject; permission prompts; session/conversation switching |
| **M4 — Docs & release polish** | 7 | README + docs updated; full `ruff check .` + pytest pass |

MVP = M1 + M2 (usable console with history and live turns). M3 and M4 are the
first working release.

**Post-MVP (tracked here, not in the steps):**

- `GET /api/sessions/{id}/checkpoints` + rollback UI (checkpoints stay CLI-only
  until then)
- Monaco editor swap for the textarea (keep the PUT `/api/file` contract —
  editor internals stay swappable)
- Per-session `RunnerRegistry` for multiple simultaneous sessions (design is
  already in place; MVP runs one active session)
- `tod serve --api-only` (no static mount)
- Frontend tests with vitest once the console UI stabilizes
- `[tool.hatch.build.force-include]` so `website/dist` ships in pip-installed
  distributions
- Streaming-modes UX (risk 5 below: `TODDLER_STREAMING=false` only changes
  perceived latency, never the protocol — no client work required, noted here
  so it isn't rediscovered)

## Implementation Steps

1. ✅ **`build(web): scaffold fastapi server and tod serve`**
   - pyproject deps; `serve` subparser in toddler/utils/cli.py; branch in
     toddler/main.py
   - `toddler/web/{__init__,app,state,server}.py` (lifespan wiring, `/api/meta`,
     static mount with dist-missing fallback)
   - `tests/test_web_server.py` — create_app with `Settings(session_dir=tmp_path)`,
     `/api/meta` assertions, fresh session row after startup
   - Verify: `ruff check .`; `.venv/bin/pytest tests/test_web_server.py`;
     `tod serve --no-open` + `curl localhost:8000/api/meta`

2. ✅ **`feat(web): stream agent turns over websocket`**
   - `toddler/web/events.py` (all serializers), `runners.py` (TurnRunner),
     `ws.py` (turn/cancel/approve_tool/deny_tool/approve_plan/reject_plan/
     set_mode/ping)
   - `tests/mocks.py` — shared `MockLLMProvider` + `make_mock_llm(*responses)`
     and a `pause_on_write` provider (port the pattern from
     tests/test_agent_loop.py); `tests/test_web_events.py` (serializer
     round-trips for every event class); `tests/test_web_ws.py` (TestClient
     `websocket_connect`: full turn stream, busy rejection, approve unblocks a
     paused tool, cancel → `turn_cancelled`, plan approve/reject acks)
   - Verify: pytest above; manual `tod serve` + websocat/scratch page

3. ✅ **`feat(web): add rest endpoints for files and sessions`**
   - `toddler/web/files.py` (resolve_relative, build_file_tree, read/write),
     `api.py` (meta/sessions/messages/tree/file)
   - `tests/test_web_api.py` — sessions list/create, messages replay after a
     WS-driven turn, file read/write round-trip on tmp repo_root, path-escape 400
   - Verify: pytest; `curl localhost:8000/api/tree`;
     `curl -X PUT localhost:8000/api/file?path=hello.txt -d '{"content":"hi"}'`
     then check the file on disk

4. ✅ **`build(web): scaffold vue console frontend`**
   - All of `website/`: package.json, tsconfig.json, vite.config.ts
     (`@vitejs/plugin-vue`), index.html, src/main.ts, src/App.vue (console-only),
     types.ts, api.ts, composables/useWebSocket.ts, composables/useConsole.ts,
     components/{ConsolePane,MessageBubble,ToolCard,PausePrompt,InputBar,
     StatusBar}.vue, styles.css
   - `.gitignore` += `node_modules/`, `web/dist/`
   - Verify: `npm run dev` against `tod serve`; send a turn, watch streaming +
     tool card; Cancel mid-turn; kill server → disconnected status → restart →
     auto-reconnect + replay

5. ✅ **`feat(web): add split-pane file explorer and editor`**
   - `components/{FileExplorer,FileEditor}.vue`; App.vue split layout + drag
     divider (pointer-event handler, clamp 20–80%)
   - Verify: browse repo, open toddler/main.py, edit + Cmd+S persists (check on
     disk); run a turn that writes a file → explorer refresh → open it

6. ✅ **`feat(web): render plans, prompts, and session management`**
   - `components/{PlanCard,SessionList}.vue`; useConsole gains plan state;
     mode toggle (`set_mode`), new-conversation, session switcher; ws.py gains
     `new_conversation`/`switch_session` handlers
   - Verify: plan-mode turn (`{"cmd":"turn","input":"refactor X","force_plan":true}`)
     → PlanCard; approve with auto → steps track in_progress/completed; MANUAL
     write pause → approve/deny prompt; session switch replays history

7. ✅ **`docs(web): document web usage and dev workflow`**
   - README roadmap tick + `tod serve` usage line; final `ruff check .` + full
     pytest pass

8. ✅ **`feat(web): add source control panel and side-by-side git diff`**
   - `toddler/web/diffparse.py` — pure unified-diff parser (`parse_diff`,
     `DiffLine`/`Hunk` dataclasses; no git dependency, testable from canned
     bytes)
   - `toddler/web/git.py` — `parse_status` refactored into `_parse_records`
     (signature pinned by existing tests) with new `build_sections`; `git_status`
     gains `sections: {staged, unstaged}`; new `git_diff` (staged = index vs
     HEAD, unstaged = worktree vs index; untracked via
     `git diff --no-index -- /dev/null <rel>` where rc 1 = differences =
     success); `DiffError` maps to `{"error"}` statuses
   - `toddler/web/api.py` — `GET /api/git/diff?path=&staged=` endpoint
   - `tests/test_web_diff.py` (parser + endpoint suites on real git repos via
     TestClient) + `TestBuildSections` in test_web_git.py
   - Frontend: activity bar + `TabEntry` tab model (file/diff tabs, diff tabs
     ephemeral — only files persist to `tod.tabs`); `SourceControl.vue`;
     `diff.ts` (side-by-side row alignment) + `DiffView.vue` (synced vertical
     scroll, independent horizontal, sticky gutters/headers); FileEditor
     diff-tab branch — CM host hidden with `display: none`, never unmounted
   - Verify: pytest + ruff; `npm run build`; manual pass over the plan's
     verification list (MM files, untracked/deleted/binary, CRLF, both
     themes, scroll sync)

## Testing Strategy

- **Backend fully tested** with existing conventions: class-based groups, plain
  asserts, shared `tests/mocks.py` MockLLMProvider factories, FastAPI TestClient
  against a tmp session dir (WS via `websocket_connect`, safe under pytest-asyncio
  auto mode). Turn flows need no network — mock providers return canned
  `LLMResponse`s; the `pause_on_write` provider drives the `AgentPaused` path
  under MANUAL mode.
- **Frontend: no automated tests in MVP** — single developer, no existing node
  tooling, every feature manually verifiable in minutes; keep the state
  transition logic pure (plain functions over state) so a later vitest port is
  mechanical. Revisit after the UI stabilizes.
- CI discipline unchanged: `ruff check .` + `.venv/bin/pytest` — `website/` is
  invisible to both.

## Risks & Gotchas

1. **One turn at a time** — `SessionManager` holds single-session state; reject
   busy `turn`/`new_conversation`/`switch_session` with a busy error frame,
   never silently drop.
2. **Generator ownership** — `process_turn` consumed only by the app-lifetime
   runner task, never a WS receive loop; disconnects unsubscribe queues, don't
   kill the turn.
3. **Stranded approvals** — auto-cancel when paused and the last subscriber
   leaves.
4. **`resolve(None)` creates a fresh session on every server start** — by design
   (CLI parity); rows are pruned by `prune_if_empty()` on shutdown. Don't "fix"
   by reusing the last session — surprising cross-project state.
5. **Stream vs non-stream** — both modes yield identical event shapes
   (`TextDelta` chunks in both), so the frontend needs no mode awareness.
6. **Path safety** — `resolve_relative` containment check is mandatory; the
   server exposes `repo_root` to any local browser tab.
7. **`Plan`/`ToolResult`/`TokenUsage` have no `to_dict`** — map fields
   explicitly in serializers.
8. **No uvicorn `--reload` by default** (needs watchfiles, complicates
   lifespan); escape hatch: `uvicorn toddler.web.app:create_app --factory --reload`.
9. **`.gitignore`** — must add `node_modules/` and `web/dist/` in step 4
   (workspace.py already excludes them from the project map; git does not).
