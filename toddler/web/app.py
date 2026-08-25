"""FastAPI app factory for the web frontend — ``tod serve``.

Wires the same backend components as the CLI (DB → storage → LLM →
session manager) inside the app lifespan, so the server is a drop-in
companion to the REPL sharing the SQLite session database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from toddler.cli.commands import SlashCommandDispatcher
from toddler.config.settings import Settings
from toddler.llm import BaseLLMProvider, OpenAICompatibleProvider
from toddler.session import SessionManager, SQLiteDatabase, StorageManager
from toddler.web.api import router as api_router
from toddler.web.runners import TurnRunner
from toddler.web.state import WebAppState
from toddler.web.ws import router as ws_router

__all__ = ["create_app"]

# Evaluated once at import — same value as a ``Path.cwd()`` argument
# default, but without the B008 function-call-in-default lint.
_DEFAULT_REPO_ROOT = Path.cwd()


def create_app(
    settings: Settings,
    *,
    repo_root: Path = _DEFAULT_REPO_ROOT,
    dev: bool = False,
    llm: BaseLLMProvider | None = None,
) -> FastAPI:
    """Build the FastAPI app with lifespan wiring and the static mount.

    Parameters
    ----------
    settings:
        Resolved settings (the CLI applies the same ``Settings.from_cli``
        overlay before dispatching to ``tod serve``).
    repo_root:
        Working directory the agent operates on — also where ``website/dist``
        is expected to live.  Defaults to the current directory.
    dev:
        Enable dev-mode CORS for the Vite dev server at ``:5173``.
    llm:
        Optional provider override — tests inject a mock provider so
        turn flows need no network.  Defaults to the real
        :class:`OpenAICompatibleProvider` built from *settings*.
    """
    root = repo_root

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Same wiring as toddler/main.py — the server owns its own DB,
        # storage, LLM provider, and session manager.
        db = SQLiteDatabase(settings.session_dir / "sessions.db")
        db.open()
        storage_mgr = StorageManager(db)
        provider = llm or OpenAICompatibleProvider(settings)
        session_mgr = SessionManager(
            settings, storage_mgr, provider, repo_root=root,
        )
        await session_mgr.resolve()
        app.state.web = WebAppState(
            settings=settings,
            db=db,
            storage_mgr=storage_mgr,
            llm=provider,
            session_mgr=session_mgr,
            # Slash commands entered in the web input bar dispatch
            # through the same dispatcher the CLI REPL uses.
            cmd_dispatcher=SlashCommandDispatcher(session_mgr=session_mgr),
            runner=TurnRunner(session_mgr),
            repo_root=root,
            dev=dev,
        )
        try:
            yield
        finally:
            # CLI parity: prune the fresh empty session created on startup.
            await session_mgr.prune_if_empty()
            db.close()

    app = FastAPI(title="Toddler", lifespan=lifespan)

    if dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Routers before the static mount so /api and /ws are never shadowed.
    app.include_router(api_router)
    app.include_router(ws_router)

    # Static mount LAST so /api (and later /ws) are never shadowed.  When
    # the frontend isn't built, / returns a JSON hint instead.
    dist = root / "website" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="static")
    else:

        @app.get("/")
        async def dist_hint() -> JSONResponse:
            return JSONResponse(
                {"error": "frontend not built — run `npm run build` in website/"}
            )

    return app
