"""CLI utility functions — argument parsing."""

from __future__ import annotations

import argparse

__all__ = ["build_argparser", "build_serve_argparser"]


def build_argparser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tod`` CLI."""
    p = argparse.ArgumentParser(
        prog="tod",
        description="Toddler — a personal Python CLI coding agent",
    )
    p.set_defaults(command=None)
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
        "--max-output-lines",
        type=int,
        metavar="N",
        default=None,
        help="Max lines of streaming output before truncating (0 to disable).",
    )
    p.add_argument(
        "--max-output-panel-height",
        type=int,
        metavar="N",
        default=None,
        help="Fallback panel height when terminal size is unknown (0 = no clipping).",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return p


def build_serve_argparser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tod serve`` subcommand.

    A separate parser (not an argparse subparser) because a subparser
    conflicts with the main parser's ``query`` positional — the query
    would greedily swallow ``serve`` (or leave a stray token that fails
    the subparser choice).  ``toddler.main`` dispatches on ``argv[0]``
    instead, git-style.
    """
    p = argparse.ArgumentParser(
        prog="tod serve",
        description="Serve the web frontend (FastAPI + uvicorn).",
    )
    p.set_defaults(command="serve")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: %(default)s).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: %(default)s).",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open the browser after startup.",
    )
    p.add_argument(
        "--dev",
        action="store_true",
        help="Enable dev-mode CORS for the Vite dev server (:5173).",
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
        "--no-stream",
        action="store_true",
        help="Disable streaming output.",
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
