"""``/ws`` WebSocket router — client command dispatch for the web frontend.

Connection lifecycle: accept → subscribe a queue → spawn a drain task
that forwards broadcast frames → send ``hello`` (session info + transcript
replay + busy/paused state) → loop ``iter_json()`` dispatching commands.

Agent events flow through the runner's per-connection queues (so multiple
tabs watch the same turn); acks/errors/pong are sent directly from the
command handler.  On disconnect the drain task is cancelled and the queue
unsubscribed — the turn itself is owned by the runner, not this connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket

from toddler.cli.commands import HELP_TEXT
from toddler.tools.base import PermissionMode

if TYPE_CHECKING:
    from toddler.web.state import WebAppState

__all__ = ["router"]

router = APIRouter()

# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def _error_frame(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def _ack_frame(cmd: str, accepted: bool) -> dict:
    return {"type": "ack", "cmd": cmd, "accepted": accepted}


def _notice_frame(message: str) -> dict:
    return {"type": "notice", "message": message}


def _session_payload(state: WebAppState) -> dict:
    mgr = state.session_mgr
    session = mgr.session
    return {
        "id": session.id if session else None,
        "title": session.title if session else None,
        "mode_label": mgr.mode_label,
        "permission_mode": mgr.permission_mode.value,
        "context_usage_pct": mgr.context_usage_pct,
        "model": state.llm.model,
        "cwd": str(state.repo_root),
    }


def _conversation_payload(state: WebAppState) -> dict:
    mgr = state.session_mgr
    conv = mgr.conversation
    return {
        "id": conv.id if conv else None,
        "sequence_num": conv.sequence_num if conv else None,
        "title": conv.title if conv else None,
    }


def _session_info_frame(state: WebAppState) -> dict:
    """Session/conversation metadata update without a transcript replay.

    Broadcast after slash commands that mutate session state but keep
    the transcript (``/mode``, ``/plan``) — the frontend updates its
    header labels and the console scroll-back survives.
    """
    return {
        "type": "session_info",
        "session": _session_payload(state),
        "conversation": _conversation_payload(state),
    }


def _hello_frame(state: WebAppState) -> dict:
    """Build the ``hello`` frame: session info, transcript replay, and
    the live busy/paused/plan state so reconnecting tabs resume
    mid-approval."""
    mgr = state.session_mgr
    session = mgr.session
    conv = mgr.conversation
    messages: list[dict] = []
    if session is not None and conv is not None:
        for msg in state.storage_mgr.get_messages(
            session.id, conversation_id=conv.id,
        ):
            # The persisted system prompt is agent scaffolding, not
            # transcript — replaying it would render it as an assistant
            # bubble at the top of the console after a refresh.
            if msg.role == "system":
                continue
            messages.append({"role": msg.role, "content": msg.text})
    return {
        "type": "hello",
        "session": _session_payload(state),
        "conversation": _conversation_payload(state),
        "busy": state.runner.busy,
        # The full agent_paused frame — the frontend re-applies it.
        "paused": state.runner.paused_snapshot,
        # The pending plan proposal (plan + latest step statuses) — the
        # frontend re-renders its PlanCard from this instead of losing
        # the card on reconnect.
        "plan": state.runner.plan_snapshot,
        "messages": messages,
    }


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json(_error_frame(code, message))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _cmd_turn(websocket: WebSocket, state: WebAppState, raw: dict) -> None:
    user_input = raw.get("input")
    if not isinstance(user_input, str) or not user_input.strip():
        await _send_error(
            websocket, "invalid_input", "`input` is required.",
        )
        return
    stripped = user_input.strip()
    if stripped.startswith("/"):
        # Slash command (e.g. /help) — dispatch locally, never to the LLM.
        await _dispatch_slash_command(websocket, state, stripped)
        return
    if state.runner.busy:
        await _send_error(websocket, "busy", "A turn is already running.")
        return
    accepted = await state.runner.start(
        user_input, force_plan=bool(raw.get("force_plan")),
    )
    if not accepted:
        # Lost a race with another connection's start().
        await _send_error(websocket, "busy", "A turn is already running.")


# Slash commands that change which transcript is displayed — new
# conversation, resume, session switch, rollback.  They are rejected
# while a turn runs (same gate as new_conversation / switch_session)
# and broadcast a fresh hello afterwards so every tab re-renders the
# (new) transcript.  The console wipe is intended here — the messages
# in storage genuinely changed.
_CONVERSATION_CHANGING_SLASH = {"/clear", "/resume", "/rollback", "/session"}

# Slash commands that mutate session metadata but keep the transcript —
# workflow / gating changes.  Also busy-gated, but they broadcast a
# lightweight session_info frame (header labels only) instead of a
# hello replay so the console scroll-back survives.
_SESSION_MUTATING_SLASH = {"/mode", "/plan"}

# Slash commands that make no sense in the browser — they would quit the
# server or open a pager.  Answered with a notice instead.
_UNAVAILABLE_SLASH = {"/quit", "/exit", "/q", "/view"}


def _slash_kind(cmd: str, sub: str) -> str | None:
    """Classify a slash command line: ``"conversation"`` (transcript
    swap), ``"session"`` (metadata mutation), ``"unavailable"`` (quit /
    view), or *None* for informational commands.

    The command token is matched exactly — an unknown command such as
    ``/moded`` or ``/clearly`` is *not* a mutation, so it is never
    busy-rejected and never replays the console.  ``/session`` only
    mutates for the ``switch`` subcommand; ``/mode`` mutates for any
    argument (the CLI validates the subcommand itself).
    """
    if cmd in _UNAVAILABLE_SLASH:
        return "unavailable"
    if cmd in _SESSION_MUTATING_SLASH:
        return "session"
    if cmd == "/session":
        return "conversation" if sub.startswith("switch") else None
    if cmd in _CONVERSATION_CHANGING_SLASH:
        return "conversation"
    return None


async def _dispatch_slash_command(
    websocket: WebSocket, state: WebAppState, text: str,
) -> None:
    """Handle a slash command entered in the web input bar.

    Mirrors the CLI's ``/`` dispatch (``toddler/cli/app.py``) but renders
    the :class:`CommandResult` as broadcast frames instead of terminal
    output.  Informational commands (``/help``, ``/conversations``,
    ``/checkpoints``, ``/session info|list``, …) are answered with a
    notice and never touch the console.  Mutating commands are
    busy-gated; when dispatch reports a change (``CommandResult.changed``
    — a failed ``/resume`` or unknown id changes nothing), those that
    swapped the transcript replay ``hello`` so every tab re-renders,
    while mode/gating changes broadcast ``session_info`` so the
    scroll-back survives.  The result notice follows either way.
    """
    parts = text.strip().lower().split(maxsplit=1)
    cmd = parts[0]
    sub = parts[1] if len(parts) > 1 else ""
    kind = _slash_kind(cmd, sub)

    if kind == "unavailable":
        state.runner.broadcast(_notice_frame(
            f"`{cmd}` is not available in the web UI.",
        ))
        return

    if kind is None:
        # Informational — answered directly, never busy-gated.
        result = await state.cmd_dispatcher.dispatch(text)
    else:
        # Mutating commands: the busy check and the mutation run under
        # the runner's busy lock, so a turn starting on another tab
        # cannot slip in between them (the check-then-mutate TOCTOU
        # window).  The state broadcast happens after the lock is
        # released, so ``hello`` reports the true busy state.
        async with state.runner.mutation_guard() as acquired:
            if not acquired:
                await _send_error(
                    websocket, "busy", "A turn is already running.",
                )
                return
            result = await state.cmd_dispatcher.dispatch(text)

    if result.changed:
        if kind == "conversation":
            # The transcript changed — full replay so every tab re-renders.
            state.runner.broadcast(_hello_frame(state))
        elif kind == "session":
            state.runner.broadcast(_session_info_frame(state))
    if result.message:
        state.runner.broadcast(_notice_frame(
            HELP_TEXT if result.message == "__HELP__" else result.message,
        ))


async def _cmd_cancel(websocket: WebSocket, state: WebAppState, raw: dict) -> None:
    await websocket.send_json(
        _ack_frame("cancel", state.runner.cancel()),
    )


def _tool_decision(
    state: WebAppState, cmd: str, tool_id: str,
) -> bool:
    """Approve or deny the pending tool confirmation.

    The calls are no-ops when the agent isn't paused, so the snapshot
    check doubles as the ``accepted`` verdict.
    """
    if state.runner.paused_snapshot is None:
        return False
    if cmd == "approve_tool":
        state.session_mgr.agent.approve_tool_call(tool_id)
    else:
        state.session_mgr.agent.deny_tool_call(tool_id)
    return True


async def _cmd_approve_tool(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    tool_id = str(raw.get("tool_id", ""))
    await websocket.send_json(
        _ack_frame("approve_tool", _tool_decision(state, "approve_tool", tool_id)),
    )


async def _cmd_deny_tool(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    tool_id = str(raw.get("tool_id", ""))
    await websocket.send_json(
        _ack_frame("deny_tool", _tool_decision(state, "deny_tool", tool_id)),
    )


async def _cmd_approve_plan(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    mode = raw.get("mode", "manual")
    if mode not in ("manual", "auto"):
        await _send_error(
            websocket, "invalid_mode",
            "mode must be 'manual' or 'auto'.",
        )
        return
    permission_mode = PermissionMode.AUTO if mode == "auto" else PermissionMode.MANUAL
    accepted = state.session_mgr.approve_plan(
        plan_id=str(raw.get("plan_id", "")),
        permission_mode=permission_mode,
    )
    await websocket.send_json(_ack_frame("approve_plan", accepted))


async def _cmd_reject_plan(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    accepted = state.session_mgr.reject_plan(
        plan_id=str(raw.get("plan_id", "")),
        feedback=str(raw.get("feedback", "")),
    )
    await websocket.send_json(_ack_frame("reject_plan", accepted))


async def _cmd_set_mode(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    mode = raw.get("mode")
    if mode not in ("manual", "auto"):
        await _send_error(
            websocket, "invalid_mode",
            "mode must be 'manual' or 'auto'.",
        )
        return
    state.session_mgr.set_permission_mode(PermissionMode(mode))
    await websocket.send_json(_ack_frame("set_mode", True))


async def _cmd_ping(websocket: WebSocket, state: WebAppState, raw: dict) -> None:
    await websocket.send_json({"type": "pong"})


async def _cmd_new_conversation(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    """Start a fresh conversation in the current session.

    The manager archives the current conversation (or renames it in
    place when empty) and activates a new one; a full ``hello`` replay
    is broadcast so every tab resets to the empty transcript.

    The busy check and the mutation run under the runner's busy lock —
    a turn arriving from another tab cannot slip in between them (the
    check-then-mutate TOCTOU window), and the replay is broadcast after
    the lock is released so ``hello`` reports the true busy state.
    """
    async with state.runner.mutation_guard() as acquired:
        if not acquired:
            await _send_error(websocket, "busy", "A turn is already running.")
            return
        title = raw.get("title")
        await state.session_mgr.new_conversation(
            title=title if isinstance(title, str) and title.strip() else None,
        )
        await websocket.send_json(_ack_frame("new_conversation", True))
    state.runner.broadcast(_hello_frame(state))


async def _cmd_switch_session(
    websocket: WebSocket, state: WebAppState, raw: dict,
) -> None:
    """Switch the active session; the new session's transcript replays
    through the same ``hello`` frame every tab applies on connect.

    The busy check and the mutation run under the runner's busy lock —
    a turn arriving from another tab cannot slip in between them (the
    check-then-mutate TOCTOU window), and the replay is broadcast after
    the lock is released so ``hello`` reports the true busy state.
    """
    async with state.runner.mutation_guard() as acquired:
        if not acquired:
            await _send_error(websocket, "busy", "A turn is already running.")
            return
        session_id = str(raw.get("session_id", ""))
        try:
            await state.session_mgr.switch_session(session_id)
        except ValueError as exc:
            await _send_error(websocket, "not_found", str(exc))
            return
        await websocket.send_json(_ack_frame("switch_session", True))
    state.runner.broadcast(_hello_frame(state))


_COMMANDS: dict[str, Callable] = {
    "turn": _cmd_turn,
    "cancel": _cmd_cancel,
    "approve_tool": _cmd_approve_tool,
    "deny_tool": _cmd_deny_tool,
    "approve_plan": _cmd_approve_plan,
    "reject_plan": _cmd_reject_plan,
    "set_mode": _cmd_set_mode,
    "new_conversation": _cmd_new_conversation,
    "switch_session": _cmd_switch_session,
    "ping": _cmd_ping,
}


async def _dispatch(
    websocket: WebSocket, state: WebAppState, raw: object,
) -> None:
    if not isinstance(raw, dict):
        await _send_error(websocket, "invalid_message", "expected a JSON object.")
        return
    cmd = raw.get("cmd")
    handler = _COMMANDS.get(cmd)
    if handler is None:
        await _send_error(
            websocket, "unknown_command", f"unknown command: {cmd!r}",
        )
        return
    await handler(websocket, state, raw)


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def _drain(websocket: WebSocket, queue: asyncio.Queue[dict]) -> None:
    """Forward runner-broadcast frames from *queue* to the websocket."""
    while True:
        frame = await queue.get()
        await websocket.send_json(frame)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Accept a connection, subscribe to the runner, and dispatch commands."""
    state: WebAppState = websocket.app.state.web
    await websocket.accept()
    queue_id, queue = state.runner.subscribe()
    drain = asyncio.create_task(_drain(websocket, queue))
    try:
        await websocket.send_json(_hello_frame(state))
        async for raw in websocket.iter_json():
            await _dispatch(websocket, state, raw)
    finally:
        # Cancel the drain before unsubscribing so nothing is sent on a
        # closing socket; the turn itself keeps running for other tabs.
        # Disconnect cancels the endpoint coroutine, and a cancellation
        # landing on the gather below must not skip the unsubscribe —
        # that would strand the paused-approval guard.  Suppress it
        # here; the connection is going away regardless.
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(drain, return_exceptions=True)
        state.runner.unsubscribe(queue_id)
