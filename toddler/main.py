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
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

from toddler.cli.app import CLIApp
from toddler.config.models import wire_model
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

logger = logging.getLogger(__name__)

# The model families this build can serve.  The provider has a branch for
# OpenAI-compatible endpoints — it picks a different token-budget key — but
# the reasoning echo-back is DeepSeek's requirement and knows no other
# dialect: a foreign model runs happily until the first turn whose history
# carries reasoning, then dies mid-turn, with the conversation already
# written to disk.  ``_parse_params`` and the echo branch apply the same
# family test inline; the three move together.
_SUPPORTED_MODEL_PREFIXES = ("deepseek-",)


def is_supported_model(spec: str) -> bool:
    """Whether *spec* names a model this build can serve.

    The ``[1m]`` notation is stripped first: ``"deepseek-v4-pro[1m]"`` is a
    DeepSeek model carrying a local window suffix, not another family.
    """
    return wire_model(spec).lower().startswith(_SUPPORTED_MODEL_PREFIXES)


def require_supported_models(slots: Mapping[str, str]) -> None:
    """Fail when a model slot names something this build cannot serve.

    Every slot is checked, not just the selected one: ``/model`` can switch
    to any of them, and the failure a foreign model produces is not at
    startup but mid-turn — the reasoning echo-back knows no dialect but
    DeepSeek's, so a tool round dies once the history carries reasoning,
    after the conversation has been written to disk.

    Raises
    ------
    ValueError
        Naming each offending slot and the model it holds.
    """
    unsupported = {
        name: spec for name, spec in slots.items()
        if not is_supported_model(spec)
    }
    if not unsupported:
        return

    listed = ", ".join(
        f"{name}={spec}" for name, spec in sorted(unsupported.items())
    )
    raise ValueError(
        f"unsupported model slot(s): {listed}.  This build serves only "
        f"DeepSeek-family models — the reasoning echo-back has no dialect "
        f"for anything else, so a tool round would fail once the "
        f"conversation carried reasoning."
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

    # --- Refuse a model this build cannot serve, before anything starts ---
    # Ahead of the serve branch, so both entry points fail identically, and
    # ahead of any DB or LLM wiring, so nothing is half-built when it does.
    try:
        require_supported_models(settings.model_slots)
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

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
    # The raw flag values go in as well: the settings carry a model and an
    # effort whether or not the user asked for one, so "explicit" is only
    # knowable here.
    app = CLIApp(
        settings,
        session,
        model_slot=args.model,
        effort=args.reasoning_effort,
    )

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
