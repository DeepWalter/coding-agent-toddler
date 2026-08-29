"""``/api`` REST router — meta, sessions, messages, tree, and file I/O.

Sessions and messages read the same SQLite store as the CLI.  File
endpoints go through :mod:`toddler.web.files`, which enforces path
containment inside the served ``repo_root`` — a local browser tab must
never reach outside it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from toddler.session.models import Session, SessionSummary
from toddler.web import files, git
from toddler.web.events import serialize_transcript
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


def _diff_error(exc: git.DiffError) -> JSONResponse:
    """Serialize a git API error (status code + message) to a response."""
    return JSONResponse({"error": exc.message}, status_code=exc.status_code)


def _summary_payload(session: Session | SessionSummary) -> dict[str, Any]:
    """Serialize a session row for the frontend session picker."""
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "message_count": session.message_count,
    }


class SessionCreate(BaseModel):
    """Body for ``POST /api/sessions`` — a row only; activation is a WS
    command."""

    title: str | None = None


class FileWrite(BaseModel):
    """Body for ``PUT /api/file``."""

    content: str


class HunkLine(BaseModel):
    """One content line of a hunk to apply.  The client sends the
    parsed line it displays; ``old_ln``/``new_ln`` are ignored here."""

    kind: Literal["ctx", "del", "add"]
    text: str
    no_newline: bool = False


class HunkPatch(BaseModel):
    """A parsed hunk — the shape ``GET /api/git/diff`` returns."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine]


class HunkApplyRequest(BaseModel):
    """Body for ``POST /api/git/hunk``.

    The hunk is the one the frontend displays; git apply's context
    matching rejects it if the file changed since the diff was shown.
    """

    path: str
    staged: bool
    action: Literal["stage", "unstage", "revert"]
    old_path: str | None = None
    new_path: str | None = None
    hunk: HunkPatch


class GitFileRequest(BaseModel):
    """Body for ``POST /api/git/file`` — whole-file stage/unstage/discard."""

    path: str
    action: Literal["stage", "unstage", "discard"]


class GitCommitRequest(BaseModel):
    """Body for ``POST /api/git/commit``."""

    message: str


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
    return {
        "messages": serialize_transcript(
            state.storage_mgr.get_messages(
                sid, conversation_id=conversation_id,
            ),
        ),
    }


# ---------------------------------------------------------------------------
# Git status
# ---------------------------------------------------------------------------


@router.get("/git/status")
async def git_status(state: WebAppState = _GetState) -> dict:
    """Git working-tree snapshot — branch, per-path letters, dir badges,
    and staged/unstaged sections."""
    return await git.git_status(state.repo_root)


@router.get("/git/diff", response_model=None)
async def git_diff(
    path: str = Query(...),
    staged: bool = Query(default=False),
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Structured per-file diff — staged (index vs HEAD) or unstaged
    (worktree vs index); untracked paths diff against /dev/null."""
    try:
        return await git.git_diff(state.repo_root, path, staged=staged)
    except git.DiffError as exc:
        return _diff_error(exc)


@router.post("/git/hunk", response_model=None)
async def git_hunk_apply(
    payload: HunkApplyRequest,
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Apply one diff hunk — stage, unstage, or revert it."""
    try:
        return await git.git_apply_hunk(
            state.repo_root,
            payload.path,
            staged=payload.staged,
            action=payload.action,
            old_path=payload.old_path,
            new_path=payload.new_path,
            hunk=payload.hunk.model_dump(),
        )
    except git.DiffError as exc:
        return _diff_error(exc)


@router.post("/git/file", response_model=None)
async def git_file_action(
    payload: GitFileRequest,
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Whole-file stage / unstage / discard from the source-control panel."""
    try:
        return await git.git_file_action(
            state.repo_root, payload.path, action=payload.action,
        )
    except git.DiffError as exc:
        return _diff_error(exc)


@router.post("/git/commit", response_model=None)
async def git_commit(
    payload: GitCommitRequest,
    state: WebAppState = _GetState,
) -> dict | JSONResponse:
    """Create a commit from the staged index (message via stdin)."""
    try:
        return await git.git_commit(state.repo_root, payload.message)
    except git.DiffError as exc:
        return _diff_error(exc)


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
