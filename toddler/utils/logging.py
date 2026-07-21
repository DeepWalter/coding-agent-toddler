"""Logging setup for the Toddler package."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

__all__ = ["setup_logging"]


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
