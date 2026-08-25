"""WebAppState — shared state built once in the app lifespan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from toddler.config.settings import Settings
from toddler.llm import BaseLLMProvider
from toddler.session import SessionManager, SQLiteDatabase, StorageManager
from toddler.web.runners import TurnRunner

if TYPE_CHECKING:
    from toddler.cli.commands import SlashCommandDispatcher

__all__ = ["WebAppState"]


@dataclass
class WebAppState:
    """Components shared by the web app's routers and runners.

    Mirrors the CLI wiring in ``toddler/main.py`` — the server builds its
    own DB / storage / LLM / session manager in the lifespan rather than
    sharing the CLI's, so ``create_app`` is self-contained.  Stored on
    ``app.state.web`` after lifespan startup.
    """

    settings: Settings
    db: SQLiteDatabase
    storage_mgr: StorageManager
    llm: BaseLLMProvider
    session_mgr: SessionManager
    cmd_dispatcher: SlashCommandDispatcher
    runner: TurnRunner
    repo_root: Path
    dev: bool
