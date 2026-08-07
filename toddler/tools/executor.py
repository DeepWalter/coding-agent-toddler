"""ToolExecutor — permission-gated tool execution with checkpoint stubs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from toddler.tools.base import (
    BaseTool,
    Permission,
    PermissionManager,
    PermissionMode,
    ToolCall,
    ToolResult,
)
from toddler.tools.registry import ToolRegistry

__all__ = [
    "CheckpointCallback",
    "ConfirmCallback",
    "ToolExecutor",
    "always_approve",
]

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

    Permission logic delegates to
    :meth:`~PermissionManager.needs_confirmation` via a shared
    :class:`PermissionManager`::

        MANUAL (default): READ/SHELL_SAFE auto; WRITE/SHELL_DANGEROUS confirm
        AUTO:             READ/SHELL_SAFE/WRITE auto; SHELL_DANGEROUS confirm

    The executor is deliberately decoupled from the agent loop — it receives
    a ``ToolCall`` and returns a ``ToolResult``, with all side-effect concerns
    (permissions, checkpoints) handled internally.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry to resolve tool names from.
    confirm_cb : ConfirmCallback | None
        Async callback invoked when user confirmation is needed.
        When ``None`` (the default), all tools are auto-approved —
        gating is assumed to be handled upstream by the agent loop.
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
        if not await self._check_permission(tool, params, perm):
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
        try:
            result = await tool.execute(**params)
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

    async def _check_permission(
        self, tool: BaseTool, params: dict[str, Any], perm: Permission
    ) -> bool:
        """Return ``True`` if execution is allowed for this tool + params.

        Delegates the policy decision to the shared
        :class:`PermissionManager` and only invokes
        the user-confirmation callback when the policy says so.
        """
        if not self._perm_mgr.needs_confirmation(perm):
            return True
        return await self._confirm(tool, params, perm)

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
