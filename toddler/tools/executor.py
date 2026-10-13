"""ToolExecutor — permission-gated tool execution with checkpoint stubs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from toddler.tools.base import (
    BaseTool,
    Decision,
    Permission,
    PermissionManager,
    PermissionMode,
    ToolCall,
    ToolResult,
    blocked_message,
    execution_params,
)
from toddler.tools.registry import ToolRegistry

__all__ = [
    "CheckpointCallback",
    "ConfirmCallback",
    "ToolExecutor",
    "always_approve",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases for callbacks
# ---------------------------------------------------------------------------

ConfirmCallback = Callable[
    [BaseTool, dict[str, Any], Permission],
    Awaitable[bool],
]
"""Signature for an async user-confirmation callback.

Receives the tool instance, the resolved kwargs, and the tool's permission
level.  Must return ``True`` to allow execution, ``False`` to deny.
"""

CheckpointCallback = Callable[
    [BaseTool, dict[str, Any]],
    str | None,
]
"""Signature for a pre-execution checkpoint hook.

Receives the tool and kwargs.  Returns a checkpoint id string, or ``None``
if checkpointing was skipped / unavailable.
"""

# ---------------------------------------------------------------------------
# Trivial callbacks
# ---------------------------------------------------------------------------


async def always_approve(
    tool: BaseTool, params: dict[str, Any], perm: Permission,  # noqa: ARG001
) -> bool:
    """Auto-approve every tool call unconditionally.

    Suitable as :class:`ToolExecutor`\\'s *confirm_cb* when permission
    gating is handled upstream (e.g. by :class:`~toddler.agent.loop.AgentLoop`
    yielding :class:`~toddler.agent.events.AgentPaused` events before the
    executor ever sees the call).
    """
    return True


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Executes tool calls with permission gating and checkpoint integration.

    The verdict comes from :meth:`~PermissionManager.decide`, shared with
    the agent loop, over a call's tier and the scope of its files::

        restricted (any tier, any mode): deny
        outside the workspace:           ask, whatever the mode
        workspace, MANUAL:               READ/SHELL_SAFE allow;
                                         WRITE/SHELL_DANGEROUS ask
        workspace, AUTO:                 as MANUAL, plus WRITE allows

    Only ``ask`` reaches *confirm_cb*: a denial is policy rather than a
    question, so it holds even when no callback is configured — gating is
    then assumed to be handled upstream by the agent loop.

    The executor is deliberately decoupled from the agent loop — it receives
    a ``ToolCall`` and returns a ``ToolResult``, with all side-effect concerns
    (permissions, checkpoints) handled internally.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry to resolve tool names from.
    confirm_cb : ConfirmCallback | None
        Async callback invoked when user confirmation is needed.  When
        ``None`` (the default), every confirmation is granted — the loop
        pre-gates before calling the executor — but a denial still is not.
    checkpoint_cb : CheckpointCallback | None
        Pre-execution hook for creating checkpoints before mutating tools.
    permission_manager : PermissionManager | None
        Shared permission manager for live gating-mode reads.
        When ``None``, a default (MANUAL) manager is created internally.
    """  # noqa: E501

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        confirm_cb: ConfirmCallback | None = None,
        checkpoint_cb: CheckpointCallback | None = None,
        permission_manager: PermissionManager | None = None,
    ) -> None:
        self._registry = registry
        self._confirm_cb = confirm_cb
        self._checkpoint_cb = checkpoint_cb
        self._perm_mgr = permission_manager or PermissionManager()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Update the permission gating mode via the shared manager.

        Kept for backward compatibility — the manager reference already
        tracks the live mode, so this is only needed when the executor
        owns its own private manager (i.e. no shared manager was passed).
        """
        self._perm_mgr.set_mode(mode)

    async def execute(self, call: ToolCall) -> ToolResult:
        """Resolve, gate, and run a single tool call.

        Returns a ``ToolResult`` — even on permission-denied or tool-not-found
        errors (the result will have ``success=False`` so the agent loop can
        feed it back to the LLM).
        """
        tool = self._registry.get(call.tool_name)
        if tool is None:
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=f"Unknown tool: '{call.tool_name}'",
            )

        params = call.parameters

        # --- permission gate ---
        perm = tool.get_permission(**params)
        decision = self._perm_mgr.decide(perm, tool.get_file_scope(**params))
        if decision is Decision.DENY:
            # Refused before the confirmation callback is consulted: a
            # restricted path is not a question, so no callback — least of
            # all the auto-approving default — may answer it.
            logger.warning("Blocked restricted call: %s", call.tool_name)
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=blocked_message(tool, params),
            )
        if decision is Decision.ASK and not await self._confirm(tool, params, perm):
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error="User denied permission to execute this tool.",
            )

        # --- pre-execution checkpoint (stub) ---
        checkpoint_id: str | None = None
        if self._is_mutating(perm) and self._checkpoint_cb is not None:
            checkpoint_id = self._checkpoint_cb(tool, params)

        # --- execute ---
        # The call description is a record, not an argument: a tool's
        # ``execute`` takes its own parameters only, and ``params`` itself
        # has to keep the key (the events and the transcript read it).
        try:
            result = await tool.execute(**execution_params(params))
            result.checkpoint_id = result.checkpoint_id or checkpoint_id
            return result
        except Exception as exc:
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=f"{type(exc).__name__}: {exc}",
                checkpoint_id=checkpoint_id,
            )

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    async def _confirm(
        self, tool: BaseTool, params: dict[str, Any], perm: Permission
    ) -> bool:
        """Ask the user for confirmation via the callback.

        If no callback is configured the tool is denied (safe default).
        """
        if self._confirm_cb is None:
            return True   # no callback → auto-approve (gating is upstream)
        return await self._confirm_cb(tool, params, perm)

    @staticmethod
    def _is_mutating(perm: Permission) -> bool:
        """Return ``True`` for permission levels that modify state."""
        return perm in (Permission.WRITE, Permission.SHELL_DANGEROUS)
