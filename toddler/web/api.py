"""``/api`` REST router — meta, sessions, messages, tree, and file I/O.

Sessions and messages read the same SQLite store as the CLI.  File
endpoints go through :mod:`toddler.web.files`, which enforces path
containment inside the served ``repo_root`` — a local browser tab must
never reach outside it.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from toddler.session.models import Session, SessionSummary
from toddler.web import files
from toddler.web.files import FileApiError
from toddler.web.state import WebAppState

__all__ = ["router"]

router = APIRouter(prefix="/api")


def _get_state(request: Request) -> WebAppState:
    """Dependency — the app-lifetime state built in the lifespan."""
    return request.app.state.web


# Evaluated once at import time — using the object as a default keeps
# Depends() out of argument defaults (ruff B008).
_GetState = Depends(_get_state)


def _summary_payload(session: Session | SessionSummary) -> dict[str, Any]:
    """Serialize a session row for the frontend session picker."""
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "message_count": session.message_count,
    }


def _content_text(content_json: str) -> str:
    """Plain text of a stored message (same shape as ``Message.text``).

    The content JSON format is owned by ``toddler/session/storage.py``
    (``_serialize_content``) — only ``type == "text"`` blocks carry text.
    """
    try:
        blocks = json.loads(content_json)
    except ValueError:
        return ""
    if not isinstance(blocks, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text")
    )


class SessionCreate(BaseModel):
    """Body for ``POST /api/sessions`` — a row only; activation is a WS
    command."""

    title: str | None = None


class FileWrite(BaseModel):
    """Body for ``PUT /api/file``."""

    content: str


# ---------------------------------------------------------------------------
# Meta + sessions
# ---------------------------------------------------------------------------


@router.get("/meta")
async def meta(state: WebAppState = _GetState) -> dict:
    """Server identity — repo root, model, and dev mode."""
    return {
        "repo_root": str(state.repo_root),
        "model": state.settings.model,
        "dev": state.dev,
    }


@router.get("/sessions")
async def list_sessions(state: WebAppState = _GetState) -> dict:
    """All sessions, most-recently-updated first."""
    return {
        "sessions": [
            _summary_payload(s) for s in state.storage_mgr.list_all()
        ],
    }


@router.post("/sessions", status_code=201)
async def create_session(
    payload: SessionCreate | None = None,
    state: WebAppState = _GetState,
) -> dict:
    """Create a session row tied to the served repo root."""
    session = state.storage_mgr.create(
        title=payload.title if payload else None,
        cwd=str(state.repo_root),
    )
    return _summary_payload(session)


@router.get("/sessions/{sid}/messages", response_model=None)
async def get_messages(
    sid: str,
    conversation_id: str | None = Query(default=None),
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Transcript replay for a session (optionally one conversation)."""
    if state.db.get_session(sid) is None:
        return JSONResponse(
            {"error": f"session not found: {sid}"}, status_code=404,
        )
    stored = state.db.get_messages(sid, conversation_id=conversation_id)
    return {
        "messages": [
            {
                "sequence_num": row.sequence_num,
                "role": row.role,
                "content": _content_text(row.content_json),
            }
            # The persisted system prompt is agent scaffolding, not
            # transcript — never replay it to the UI.
            for row in stored
            if row.role != "system"
        ],
    }


# ---------------------------------------------------------------------------
# File tree + file I/O
# ---------------------------------------------------------------------------


@router.get("/tree")
async def tree(
    depth: int = Query(default=4, ge=1, le=20),
    state: WebAppState = _GetState,
) -> dict:
    """Gitignore-aware file listing down to *depth* levels."""
    return files.build_file_tree(state.repo_root, depth=depth)


@router.get("/file", response_model=None)
async def read_file(
    path: str = Query(...),
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Read a text file from the repo (path containment enforced)."""
    try:
        return files.read_file(state.repo_root, path)
    except FileApiError as exc:
        return JSONResponse(
            {"error": exc.message}, status_code=exc.status_code,
        )


@router.put("/file", response_model=None)
async def write_file(
    request: Request,
    path: str = Query(...),
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Write a text file into the repo (parents created on demand).

    The body is parsed as raw JSON so ``curl -d '{"content": "..."}'``
    works without an explicit ``Content-Type`` header.
    """
    try:
        payload = FileWrite.model_validate(await request.json())
    except (ValueError, ValidationError):
        return JSONResponse(
            {"error": "body must be JSON with a `content` string field."},
            status_code=400,
        )
    try:
        return files.write_file(state.repo_root, path, payload.content)
    except FileApiError as exc:
        return JSONResponse(
            {"error": exc.message}, status_code=exc.status_code,
        )
