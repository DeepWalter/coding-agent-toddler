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

from toddler.agent.state_machine import AgentStateMachine
from toddler.checkpoint.manager import CheckpointManager
from toddler.cli.app import CLIApp, build_argparser, setup_logging
from toddler.config.settings import Settings
from toddler.context.compaction import ConversationCompactor
from toddler.context.memory import PersistentMemory
from toddler.context.project_map import ProjectMapper
from toddler.context.window import ContextWindowManager
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

    # --- Session persistence (no LLM needed) ---
    db_path = settings.session_dir / "sessions.db"
    store = SQLiteStore(db_path)
    store.open()
    session_mgr = SessionManager(store)

    # --- Session listing (no LLM needed — do it early) ---
    if args.list_sessions:
        asyncio.run(_list_sessions(session_mgr))
        return

    # --- Shared LLM provider ---
    llm = OpenAICompatibleProvider(settings)

    # --- Context management (Phase 7.5) ---
    project_mapper = ProjectMapper()
    persistent_memory = PersistentMemory(settings.session_dir)
    context_window_mgr = ContextWindowManager(llm)
    conversation_compactor = ConversationCompactor(llm)

    # --- State machine (Phase 10) ---
    state_machine = AgentStateMachine()

    # --- Checkpoint manager factory (Phase 9 + 10) ---
    # The checkpoint manager is session-scoped, so we use a factory that
    # captures the store, repo root, and session manager, and resolves
    # the current session at call time.  A list is used as a mutable
    # container so the closure can access ``app`` after it is created.
    repo_root = Path.cwd()
    _app_ref: list[CLIApp] = []

    async def _ckpt_factory() -> CheckpointManager | None:
        """Create a CheckpointManager for the current session."""
        if not _app_ref:
            return None
        app_ref = _app_ref[0]
        if app_ref._session is None:  # noqa: SLF001
            return None
        return CheckpointManager(
            store=store,
            session_id=app_ref._session.id,  # noqa: SLF001
            repo_root=repo_root,
            session_manager=session_mgr,
        )

    # --- Build app ---
    app = CLIApp(
        settings,
        session_manager=session_mgr,
        llm=llm,
        project_mapper=project_mapper,
        persistent_memory=persistent_memory,
        context_window_mgr=context_window_mgr,
        conversation_compactor=conversation_compactor,
        state_machine=state_machine,
        checkpoint_manager_factory=_ckpt_factory,
    )
    _app_ref.append(app)

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
