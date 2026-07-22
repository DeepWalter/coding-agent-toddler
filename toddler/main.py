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
from pathlib import Path

from toddler.cli.app import CLIApp
from toddler.config.settings import Settings
from toddler.llm.provider import OpenAICompatibleProvider
from toddler.session import SessionCoordinator, print_sessions
from toddler.session.manager import StorageManager
from toddler.session.store import SQLiteStore
from toddler.utils import build_argparser, setup_logging


def main() -> None:
    """CLI entry point — parse args, wire components, dispatch mode."""
    parser = build_argparser()
    args = parser.parse_args()

    # --- Build settings from env + CLI args ---
    cli_ns = args
    if cli_ns.no_stream:
        cli_ns.streaming_enabled = False

    settings = Settings.from_cli(cli_ns)

    setup_logging(verbose=args.verbose, log_dir=settings.session_dir)

    # --- Session persistence (no LLM needed) ---
    db_path = settings.session_dir / "sessions.db"
    store = SQLiteStore(db_path)
    store.open()
    storage_mgr = StorageManager(store)

    # --- Session listing (no LLM needed — do it early) ---
    if args.list_sessions:
        print_sessions(storage_mgr)
        return

    # --- Shared LLM provider ---
    llm = OpenAICompatibleProvider(settings)

    # --- Session coordinator (owns all wiring) ---
    session = SessionCoordinator(
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
