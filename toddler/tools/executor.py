"""ToolExecutor — runs a tool call, checkpointing a mutating one first."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from toddler.tools.base import (
    BaseTool,
    ToolCall,
    ToolResult,
    execution_params,
)
from toddler.tools.registry import ToolRegistry

__all__ = [
    "CheckpointCallback",
    "ToolExecutor",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases for callbacks
# ---------------------------------------------------------------------------


CheckpointCallback = Callable[
    [BaseTool, dict[str, Any]],
    str | None,
]
"""Signature for a pre-execution checkpoint hook.

Receives the tool and kwargs.  Returns a checkpoint id string, or ``None``
if checkpointing was skipped / unavailable.
"""

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Executes a tool call, with a checkpoint taken before a mutating one.

    It does not gate.  The agent loop decides whether a call may run — over
    the shared :class:`~toddler.tools.base.PermissionManager` and its pause
    channel — and hands the answer down; the executor runs what the gate
    cleared.  A direct caller therefore gets exactly what it passes: this
    is fail-open for permissions, and gating is the caller's job.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry to resolve tool names from.
    checkpoint_cb : CheckpointCallback | None
        Pre-execution hook for creating checkpoints before mutating tools.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        checkpoint_cb: CheckpointCallback | None = None,
    ) -> None:
        self._registry = registry
        self._checkpoint_cb = checkpoint_cb

    async def execute(self, call: ToolCall, *, mutating: bool) -> ToolResult:
        """Resolve, checkpoint, and run a single tool call.

        *mutating* is the caller's classification of the call — what it read
        from the tool's tier, via
        :attr:`~toddler.tools.base.Permission.is_mutating` — and it orders
        the checkpoint only: a call that changes state gets its snapshot
        taken before it runs.

        Returns a ``ToolResult`` — even for a tool the registry does not
        know, or one that raises (the result will have ``success=False`` so
        the agent loop can feed it back to the LLM).
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

        # --- pre-execution checkpoint (stub) ---
        checkpoint_id: str | None = None
        if mutating and self._checkpoint_cb is not None:
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
