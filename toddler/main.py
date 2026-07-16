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

from toddler.cli.app import CLIApp, build_argparser, setup_logging
from toddler.config.settings import Settings
from toddler.llm.provider import OpenAICompatibleProvider
from toddler.session.manager import SessionManager
from toddler.session.store import SQLiteStore


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

    # --- Shared LLM provider ---
    llm = OpenAICompatibleProvider(settings)

    # --- Session persistence ---
    db_path = settings.session_dir / "sessions.db"
    store = SQLiteStore(db_path)
    store.open()
    session_mgr = SessionManager(store, llm_provider=llm)

    # --- Session listing ---
    if args.list_sessions:
        asyncio.run(_list_sessions(session_mgr))
        return

    # --- Build app ---
    app = CLIApp(settings, session_manager=session_mgr, llm=llm)

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


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------


async def _list_sessions(mgr: SessionManager) -> None:
    """Print a formatted list of all saved sessions."""
    sessions = await mgr.list_all()
    if not sessions:
        print("No saved sessions.")
        return

    print(f"{'ID':<34} {'Title':<40} {'Msgs':>5}  {'Age'}")
    print("-" * 100)
    for s in sessions:
        sid = s.id[:32]
        title = (s.display_title or "—")[:39]
        print(f"{sid:<34} {title:<40} {s.message_count:>5}  {s.age}")


if __name__ == "__main__":
    main()
