# Toddler

Custom Python CLI coding agent. See `docs/plan.md` for architecture and roadmap.

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
- Body: blank line after subject, wrap at 80 chars, explain what/why (not how)
- Footer: `BREAKING CHANGE:` or issue refs (e.g., `Closes #123`)
- Sign-off: `Co-Authored-By: Claude <noreply@anthropic.com>`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `init`

## Code Style

Place `__all__` at the top of each module, immediately after all imports (before
`logger` and other module-level code).
