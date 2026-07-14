"""Entry point for the ``tod`` CLI — wired via ``pyproject.toml`` scripts.

Usage::

    tod                      # Enter interactive REPL
    tod "read auth.py"       # One-shot: run a single task
    tod --plan "refactor X"  # One-shot in plan mode
    tod --list-sessions      # List saved sessions (Phase 8)
    tod --session <id>       # Resume a previous session (Phase 8)
"""

from __future__ import annotations

import asyncio

from toddler.cli.app import CLIApp, build_argparser, setup_logging
from toddler.config.settings import Settings


def main() -> None:
    """CLI entry point — parse args, wire components, dispatch mode."""
    parser = build_argparser()
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    # --- Build settings from env + CLI args ---
    # The --no-stream flag negates streaming_enabled.
    cli_ns = args
    if cli_ns.no_stream:
        cli_ns.streaming_enabled = False

    settings = Settings.from_cli(cli_ns)

    # --- Session listing (Phase 8 stub) ---
    if args.list_sessions:
        _list_sessions_stub(settings)

    # --- Build app ---
    app = CLIApp(settings)

    # --- Dispatch mode ---
    query = " ".join(args.query).strip() if args.query else ""

    if query:
        # One-shot mode
        asyncio.run(app.run_one_shot(query, force_plan=args.plan))
    else:
        # REPL mode
        asyncio.run(app.run_repl())


# ---------------------------------------------------------------------------
# Session listing stub (Phase 8 replaces this)
# ---------------------------------------------------------------------------


def _list_sessions_stub(settings: Settings) -> None:
    """Print a placeholder message for --list-sessions."""
    db_path = settings.session_dir / "sessions.db"
    if db_path.exists():
        print(f"Session database exists at: {db_path}")
        print("Session listing will be available in Phase 8.")
    else:
        print(f"No session database found at: {db_path}")
        print("Sessions are not yet implemented (Phase 8).")


if __name__ == "__main__":
    main()
