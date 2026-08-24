"""Entry point for the ``tod`` CLI — wired via ``pyproject.toml`` scripts.

Usage::

    tod                      # Enter interactive REPL
    tod "read auth.py"       # One-shot: run a single task
    tod --plan "refactor X"  # One-shot in plan mode
    tod --list-sessions      # List saved sessions
    tod --session <id>       # Resume a previous session
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from toddler.cli.app import CLIApp
from toddler.config.settings import Settings
from toddler.llm import OpenAICompatibleProvider
from toddler.session import (
    SessionManager,
    SQLiteDatabase,
    StorageManager,
    print_sessions,
)
from toddler.utils import (
    build_argparser,
    build_serve_argparser,
    setup_logging,
)


def main() -> None:
    """CLI entry point — parse args, wire components, dispatch mode."""
    argv = sys.argv[1:]
    if argv and argv[0] == "serve":
        parser = build_serve_argparser()
        args = parser.parse_args(argv[1:])
    else:
        parser = build_argparser()
        args = parser.parse_args(argv)

    # --- Build settings from env + CLI args ---
    cli_ns = args
    if cli_ns.no_stream:
        cli_ns.streaming_enabled = False

    settings = Settings.from_cli(cli_ns)

    setup_logging(verbose=args.verbose, log_dir=settings.session_dir)

    # --- Web server (owns its own DB/LLM wiring via the app factory) ---
    if args.command == "serve":
        from toddler.web.server import run_server

        run_server(
            settings,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            dev=args.dev,
        )
        return

    # --- Session persistence (no LLM needed) ---
    db_path = settings.session_dir / "sessions.db"
    db = SQLiteDatabase(db_path)
    db.open()
    storage_mgr = StorageManager(db)

    # --- Session listing (no LLM needed — do it early) ---
    if args.list_sessions:
        print_sessions(storage_mgr)
        return

    # --- Shared LLM provider ---
    llm = OpenAICompatibleProvider(settings)

    # --- Session manager (owns all wiring) ---
    session = SessionManager(
        settings,
        storage_mgr,
        llm,
        repo_root=Path.cwd(),
    )

    # --- CLI (thin display + input layer) ---
    app = CLIApp(settings, session)

    # --- Resolve session for --session flag ---
    session_id = args.session

    # --- Dispatch mode ---
    query = " ".join(args.query).strip() if args.query else ""

    if query:
        # One-shot mode
        asyncio.run(
            app.run_one_shot(
                query,
                force_plan=args.plan,
                session_id=session_id,
            )
        )
    else:
        # REPL mode
        asyncio.run(app.run_repl(session_id=session_id))


if __name__ == "__main__":
    main()
