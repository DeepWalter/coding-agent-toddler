# Toddler

A personal Python CLI coding agent — an AI-powered assistant that reads, searches,
edits files, runs shell commands, and interacts with git from your terminal.

## Installation

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Set your API key and optional base URL via environment variables (or a `.env` file):

```bash
export DEEPSEEK_API_KEY="your-key"      # defaults to DeepSeek
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-v4-pro"
```

OpenAI-compatible providers work too — use the `OPENAI_*` env prefix as a fallback.

**Troubleshooting:** DeepSeek thinking mode consumes the `max_tokens` budget
(`TODDLER_MAX_TOKENS`, default 8192).  A thinking-heavy turn can exhaust it,
ending with `finish_reason="length"` and empty output — raise
`TODDLER_MAX_TOKENS`.  `reasoning_tokens` in the recorded usage shows the
reasoning/output split.

## Usage

```bash
tod                     # Interactive REPL with slash commands
tod "refactor auth.py"  # One-shot: run a single task
tod --plan "add tests"  # One-shot in plan mode (research → propose → execute)
tod --session <id>      # Resume a previous session
tod --list-sessions     # List saved sessions
tod serve               # Browser-based web UI (see Web UI below)
```

### CLI Options

| Flag | Purpose |
| --- | --- |
| `--plan` | Force plan mode — agent researches before making changes |
| `--session <id>` | Resume a previous session by ID |
| `--new-session` | Start fresh (don't reuse the last session) |
| `--list-sessions` | List saved sessions and exit |
| `--model <name>` | Override the LLM model |
| `--base-url <url>` | Override the API base URL |
| `--api-key <key>` | Override the API key |
| `--max-iterations <n>` | Cap agent loop iterations |
| `--no-stream` | Disable streaming output |
| `--verbose`, `-v` | Enable debug logging |

### Web UI

`tod serve` runs the browser frontend (FastAPI + Vue) against the same
agent backend and SQLite session database as the CLI:

```bash
tod serve                    # serve at http://127.0.0.1:8000, open browser
tod serve --port 9000        # custom port
tod serve --no-open          # don't auto-open the browser
```

It accepts the shared LLM flags (`--model`, `--base-url`, `--api-key`,
`--no-stream`, `--max-iterations`); `--dev` enables CORS for the Vite dev
server.

Slash commands work in the input bar too (`/help`, `/mode`, `/clear`,
`/session`, …) — they dispatch server-side and never reach the LLM.
`/quit` and `/view` are not available in the browser.

Development workflow:

```bash
cd website && npm run dev    # Vite :5173, proxies /api and /ws to :8000
# keep `tod serve` running in another terminal while editing frontend code

cd website && npm run build  # production bundle into website/dist
tod serve                    # serves the built bundle directly
```

## Architecture

```
toddler/
  main.py             CLI entry point, arg parsing, component wiring
  agent/              Agent loop, state machine, event handling, stop conditions
  cli/                Terminal app, commands, display, input handler, renderer
  config/             Settings (env + CLI overrides), defaults
  context/            Context window, compaction, conversation context,
                      persistent memory, project map, system prompt
  llm/                LLM provider abstraction (OpenAI-compatible), types,
                      token counting
  storage/            SQLite-backed session persistence, store, models
  tools/              Tool registry, base protocol, executor, filesystem, git,
                      search, shell subprocess
  checkpoint/         Snapshot/checkpoint management
```

### Highlights

- **LLM-agnostic** — OpenAI-compatible provider works with DeepSeek, OpenAI, and
  any compatible endpoint.
- **Permission model** — Tools declare a tier (`READ`, `WRITE`, `SHELL_SAFE`,
  `SHELL_DANGEROUS`); the agent pauses for confirmation before destructive ops.
- **Plan mode** — State machine that detects complex tasks, researches first,
  proposes a plan, then executes after user approval.
- **Context window** — Automatic token counting with LLM-powered compaction to
  handle long sessions.
- **Session persistence** — SQLite-backed history at `~/.toddler/sessions.db`
  with multi-conversation support and resumption.
- **Streaming** — Real-time token output via Rich with animated tool-call status.

## Roadmap

- [x] **Web frontend** — browser-based GUI with a split-pane layout: file
  viewer/editor on the left, agent console with scrollback on the right.
  Coexists with the CLI via a `tod serve` command and shares the same
  agent backend and session database.

## Development

Always use the project virtual environment:

```bash
.venv/bin/python
.venv/bin/pip
.venv/bin/pytest
.venv/bin/ruff check .
```
