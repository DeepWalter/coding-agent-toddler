"""ToolExecutor — permission-gated tool execution with checkpoint stubs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from toddler.config.settings import Settings
from toddler.tools.base import BaseTool, Permission, ToolCall, ToolResult
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
    Awaitable[str | None],
]
"""Signature for an async pre-execution checkpoint hook.

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

    Permission logic (driven by ``Settings``)::

        READ            → auto-approve (by default)
        WRITE           → confirm with user (by default)
        SHELL_SAFE      → auto-approve (by default)
        SHELL_DANGEROUS → always confirm

    The executor is deliberately decoupled from the agent loop — it receives
    a ``ToolCall`` and returns a ``ToolResult``, with all side-effect concerns
    (permissions, checkpoints) handled internally.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry to resolve tool names from.
    settings : Settings
        Resolved configuration controlling permission behavior.
    confirm_cb : ConfirmCallback | None
        Async callback invoked when user confirmation is needed.  If
        ``None`` and confirmation would be required, the tool is **denied**.
    checkpoint_cb : CheckpointCallback | None
        Pre-execution hook for creating checkpoints before mutating tools.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        settings: Settings,
        *,
        confirm_cb: ConfirmCallback | None = None,
        checkpoint_cb: CheckpointCallback | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._confirm_cb = confirm_cb
        self._checkpoint_cb = checkpoint_cb

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
            checkpoint_id = await self._checkpoint_cb(tool, params)

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
        """Return ``True`` if execution is allowed for this tool + params."""
        if perm == Permission.READ:
            if self._settings.auto_approve_read:
                return True
            return await self._confirm(tool, params, perm)

        if perm == Permission.SHELL_SAFE:
            # Shell-safe is treated like READ by default
            return True

        if perm == Permission.WRITE:
            if not self._settings.confirm_write:
                return True
            return await self._confirm(tool, params, perm)

        if perm == Permission.SHELL_DANGEROUS:
            return await self._confirm(tool, params, perm)

        # Unknown permission level → deny
        return False

    async def _confirm(
        self, tool: BaseTool, params: dict[str, Any], perm: Permission
    ) -> bool:
        """Ask the user for confirmation via the callback.

        If no callback is configured the tool is denied (safe default).
        """
        if self._confirm_cb is None:
            return False
        return await self._confirm_cb(tool, params, perm)

    @staticmethod
    def _is_mutating(perm: Permission) -> bool:
        """Return ``True`` for permission levels that modify state."""
        return perm in (Permission.WRITE, Permission.SHELL_DANGEROUS)
