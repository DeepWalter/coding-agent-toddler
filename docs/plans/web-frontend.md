# Web Frontend: `tod serve`

## Goal

Browser-based GUI with a split-pane layout — file viewer/editor on the left,
agent console with scrollback on the right — coexisting with the CLI via a
`tod serve` command, sharing the same agent backend and SQLite session database
(README roadmap item).

## Decided Stack

- **Backend**: FastAPI + uvicorn (new runtime deps)
- **Frontend**: Vue 3 + Vite build (Composition API, `.vue` SFCs), living in
  `web/` at repo root
- Python ≥3.11, ruff (line 100), pytest `asyncio_mode="auto"`

## Target Architecture

```
tod serve (main.py) ──→ toddler/web/
                           ├─ app.py        create_app(): lifespan wiring, routers, static mount
                           ├─ state.py      WebAppState (settings, db, storage_mgr, llm, session_mgr, runner, repo_root, dev)
                           ├─ events.py     AgentEvent → JSON dict serializers
                           ├─ runners.py    TurnRunner: busy gate, queue fan-out, cancel, paused snapshot
                           ├─ ws.py         /ws WebSocket router + command dispatch
                           ├─ api.py        /api REST router (meta, sessions, messages, tree, file)
                           ├─ files.py      path-safe read/write + gitignore-aware tree walk
                           └─ server.py     run_server() — uvicorn bootstrap
web/  (Vue 3 + Vite)
  ├─ vite.config.ts    @vitejs/plugin-vue; proxy /api + /ws → http://localhost:8000
  └─ src/              App.vue, main.ts, types.ts, api.ts, composables/ (useWebSocket,
                       useConsole), components/ (ConsolePane, MessageBubble, ToolCard,
                       PlanCard, PausePrompt, InputBar, FileExplorer, FileEditor,
                       SessionList, StatusBar)
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
- serves `web/dist` via `StaticFiles(html=True)` as the **last** mount so it
  never shadows `/api`/`/ws`; if `web/dist/index.html` is missing, `/` returns
  a JSON hint ("frontend not built — run `npm run build` in web/")
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
`httpx>=0.27` (TestClient). `web/` is not a Python package — no hatchling
changes; `web/dist` won't ride along in an sdist (fine for a checkout-based
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
                     busy, paused, messages: [replay from storage_mgr.get_messages()]}
turn_started
state              {busy: true|false}
text_delta         {text}
tool_call_start    {tool_id, tool_name, partial_input}
tool_call_delta    {tool_id, input_delta}
tool_call_end      {tool_id, tool_name, input, result: {success, output, error, checkpoint_id, metadata}}
plan_proposed      {plan: {id, title, summary, steps, rationale, risks, estimated_files_touched}}
plan_step_update   {steps: [[id, description, status], ...]}   # complete snapshot — replace, don't diff
agent_paused       {prompt, choices}
agent_finished     {reason, usage: {input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}}
recoverable_error  {message}
fatal_error        {message}
ack                {cmd, accepted}
turn_cancelled
conversation_switched  {conversation: {id, sequence_num}}
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
```

File endpoints use **pathlib directly** — the `ReadFile` tool returns
line-numbered output, which the editor must not parse. Path safety:
`resolve_relative` enforces containment (`path.resolve().is_relative_to(root.resolve())`),
400 on escape, 404 on missing, 400/409 on binary. Tree walk reuses
`GitignoreMatcher` (toddler/context/workspace.py:86) + an `IGNORED_TOP` set
(`.git`, `node_modules`, `.venv`, `dist`, `__pycache__`, ...).

No plan/checkpoint REST endpoints in MVP — plans stream over WS and live only
in the live turn.

## Frontend (Vue 3 + Vite)

- State: Composition API — a `useConsole` composable exposing `ref`/`reactive`
  state for the console (single linear event stream, applied by a plain
  reducer-style function so the transition logic stays pure and testable) + a
  `useWebSocket` composable (auto-reconnect with backoff 1s→10s). No Pinia
  (tree too shallow), no Monaco (textarea editor for MVP).
- On `hello`: replace console state with transcript replay, resume `busy`/`paused`
  (reconnect mid-approval shows the pending prompt again).
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
- `[tool.hatch.build.force-include]` so `web/dist` ships in pip-installed
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

3. **`feat(web): add rest endpoints for files and sessions`**
   - `toddler/web/files.py` (resolve_relative, build_file_tree, read/write),
     `api.py` (meta/sessions/messages/tree/file)
   - `tests/test_web_api.py` — sessions list/create, messages replay after a
     WS-driven turn, file read/write round-trip on tmp repo_root, path-escape 400
   - Verify: pytest; `curl localhost:8000/api/tree`;
     `curl -X PUT localhost:8000/api/file?path=hello.txt -d '{"content":"hi"}'`
     then check the file on disk

4. **`build(web): scaffold vue console frontend`**
   - All of `web/`: package.json, tsconfig.json, vite.config.ts
     (`@vitejs/plugin-vue`), index.html, src/main.ts, src/App.vue (console-only),
     types.ts, api.ts, composables/useWebSocket.ts, composables/useConsole.ts,
     components/{ConsolePane,MessageBubble,ToolCard,PausePrompt,InputBar,
     StatusBar}.vue, styles.css
   - `.gitignore` += `node_modules/`, `web/dist/`
   - Verify: `npm run dev` against `tod serve`; send a turn, watch streaming +
     tool card; Cancel mid-turn; kill server → disconnected status → restart →
     auto-reconnect + replay

5. **`feat(web): add split-pane file explorer and editor`**
   - `components/{FileExplorer,FileEditor}.vue`; App.vue split layout + drag
     divider (pointer-event handler, clamp 20–80%)
   - Verify: browse repo, open toddler/main.py, edit + Cmd+S persists (check on
     disk); run a turn that writes a file → explorer refresh → open it

6. **`feat(web): render plans, prompts, and session management`**
   - `components/{PlanCard,SessionList}.vue`; useConsole gains plan state;
     mode toggle (`set_mode`), new-conversation, session switcher; ws.py gains
     `new_conversation`/`switch_session` handlers
   - Verify: plan-mode turn (`{"cmd":"turn","input":"refactor X","force_plan":true}`)
     → PlanCard; approve with auto → steps track in_progress/completed; MANUAL
     write pause → approve/deny prompt; session switch replays history

7. **`docs(web): document web usage and dev workflow`**
   - README roadmap tick + `tod serve` usage line; final `ruff check .` + full
     pytest pass

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
- CI discipline unchanged: `ruff check .` + `.venv/bin/pytest` — `web/` is
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
