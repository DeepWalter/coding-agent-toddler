"""Entry point for the ``tod`` CLI — wired via ``pyproject.toml`` scripts.

Usage::

    tod                      # Enter interactive REPL
    tod "read auth.py"       # One-shot: run a single task
    tod --plan "refactor X"  # One-shot in plan mode
    tod --list-sessions      # List saved sessions
    tod --session <id>       # Resume a previous session
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from toddler.cli.app import CLIApp
from toddler.config.settings import Settings
from toddler.llm.provider import OpenAICompatibleProvider
from toddler.session import SessionCoordinator, print_sessions
from toddler.session.manager import StorageManager
from toddler.session.store import SQLiteStore


def build_argparser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tod`` CLI."""
    p = argparse.ArgumentParser(
        prog="tod",
        description="Toddler — a personal Python CLI coding agent",
    )
    p.add_argument(
        "query",
        nargs="*",
        help="Task to perform.  Omit to enter the interactive REPL.",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="Force plan mode — agent researches before making changes.",
    )
    p.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="Resume a previous session by its ID.",
    )
    p.add_argument(
        "--new-session",
        action="store_true",
        help="Start a new session (don't reuse the last one).",
    )
    p.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output.",
    )
    p.add_argument(
        "--list-sessions",
        action="store_true",
        help="List saved sessions and exit.",
    )
    p.add_argument(
        "--model",
        metavar="MODEL",
        default=None,
        help="Override the LLM model name.",
    )
    p.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="Override the API base URL.",
    )
    p.add_argument(
        "--api-key",
        metavar="KEY",
        default=None,
        help="Override the API key.",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        metavar="N",
        default=None,
        help="Override the maximum number of agent loop iterations.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return p


def setup_logging(
    verbose: bool = False,
    *,
    log_dir: str | Path | None = None,
) -> None:
    """Configure logging for the Toddler package.

    Logs go to stderr at DEBUG (``--verbose``) or WARNING (default) level.
    When *log_dir* is provided, INFO-and-above messages are also written to
    ``<log_dir>/toddler.log`` so session lifecycle and errors are always
    captured on disk.
    """
    level = logging.DEBUG if verbose else logging.WARNING

    # Root logger captures everything; handlers control routing.
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # --- stderr handler (level varies with --verbose) ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(
        logging.Formatter("%(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(stderr_handler)

    # --- file handler (always INFO, so key events are persisted) ---
    if log_dir is not None:
        log_path = Path(log_dir).expanduser()
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            str(log_path / "toddler.log"), encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(file_handler)

    # Suppress noisy openai/httpx logs unless --verbose is set.
    if not verbose:
        for noisy in ("openai", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


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
        asyncio.run(print_sessions(storage_mgr))
        return

    # --- Shared LLM provider ---
    llm = OpenAICompatibleProvider(settings)

    # --- Session coordinator (owns all wiring) ---
    session = SessionCoordinator(
        settings,
        storage_mgr,
        llm,
        store=store,
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
