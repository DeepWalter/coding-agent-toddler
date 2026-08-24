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


def _hello_frame(state: WebAppState) -> dict:
    """Build the ``hello`` frame: session info, transcript replay, and
    the live busy/paused state so reconnecting tabs resume mid-approval."""
    mgr = state.session_mgr
    session = mgr.session
    conv = mgr.conversation
    messages: list[dict] = []
    if session is not None and conv is not None:
        for msg in state.storage_mgr.get_messages(
            session.id, conversation_id=conv.id,
        ):
            messages.append({"role": msg.role, "content": msg.text})
    return {
        "type": "hello",
        "session": {
            "id": session.id if session else None,
            "title": session.title if session else None,
            "mode_label": mgr.mode_label,
            "permission_mode": mgr.permission_mode.value,
            "context_usage_pct": mgr.context_usage_pct,
            "model": state.llm.model,
            "cwd": str(state.repo_root),
        },
        "conversation": {
            "id": conv.id if conv else None,
            "sequence_num": conv.sequence_num if conv else None,
            "title": conv.title if conv else None,
        },
        "busy": state.runner.busy,
        # The full agent_paused frame — the frontend re-applies it.
        "paused": state.runner.paused_snapshot,
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
    if state.runner.busy:
        await _send_error(websocket, "busy", "A turn is already running.")
        return
    accepted = await state.runner.start(
        user_input, force_plan=bool(raw.get("force_plan")),
    )
    if not accepted:
        # Lost a race with another connection's start().
        await _send_error(websocket, "busy", "A turn is already running.")


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


_COMMANDS: dict[str, Callable] = {
    "turn": _cmd_turn,
    "cancel": _cmd_cancel,
    "approve_tool": _cmd_approve_tool,
    "deny_tool": _cmd_deny_tool,
    "approve_plan": _cmd_approve_plan,
    "reject_plan": _cmd_reject_plan,
    "set_mode": _cmd_set_mode,
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
