# Toddler

A personal Python CLI coding agent. Invoked via `tod` — run interactively in a
REPL or as a one-shot `tod "<task>"`. Uses an OpenAI-compatible LLM provider,
Rich-based terminal UI, and prompt-toolkit for input.

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

## Development Environment

Always use the project virtual environment `.venv` for all Python commands:

```
.venv/bin/python   # Python interpreter
.venv/bin/pip      # Package installer
```

Never use the system Python or any other interpreter.

## Commit Conventions

All commits follow `.github/COMMIT_STYLE_GUIDE.md`.

- Format: `<type>[(scope)][!]: <subject>`
- Subject: ≤60 chars, lowercase, no period, imperative mood ("add", not "added")
- Body: blank line after subject, wrap at 80 chars, explain what/why (not how).
  Use bullet points when listing multiple changes makes the message clearer.
- Footer: `BREAKING CHANGE:` or issue refs (e.g., `Closes #123`)
- Sign-off: `Co-Authored-By: Claude <noreply@anthropic.com>`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `init`

## Code Style

Place `__all__` at the top of each module, immediately after all imports (before
`logger` and other module-level code).
