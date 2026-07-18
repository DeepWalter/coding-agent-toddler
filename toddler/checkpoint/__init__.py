"""Checkpoint subsystem — pre-mutation snapshots with rollback capability.

Public API
----------
- :class:`Checkpoint` — pre-mutation snapshot record
- :class:`AgentStateSnapshot` — agent loop state captured at checkpoint time
- :class:`RollbackResult` — outcome of a rollback operation
- :class:`FileManifestEntry` — individual file in a file-copy snapshot
- :class:`CheckpointManager` — create, rollback, list, prune
- :class:`GitSnapshotter` — git-based snapshot strategy (preferred)
- :class:`FileSnapshotter` — file-copy snapshot fallback
- :class:`CheckpointManagerProvider` — async factory type for lazy
  session-scoped :class:`CheckpointManager` creation
- :func:`create_checkpoint_callback` — factory that builds a
  :class:`~toddler.tools.executor.CheckpointCallback` for
  :class:`~toddler.tools.executor.ToolExecutor`
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from toddler.checkpoint.manager import CheckpointManager
from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    FileManifestEntry,
    RollbackResult,
)
from toddler.checkpoint.snapshot import FileSnapshotter, GitSnapshotter

if TYPE_CHECKING:
    from toddler.tools.executor import CheckpointCallback

__all__ = [
    "AgentStateSnapshot",
    "Checkpoint",
    "CheckpointManager",
    "CheckpointManagerProvider",
    "FileManifestEntry",
    "FileSnapshotter",
    "GitSnapshotter",
    "RollbackResult",
    "create_checkpoint_callback",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CheckpointManager provider type
# ---------------------------------------------------------------------------

CheckpointManagerProvider = Callable[
    [],
    Awaitable[CheckpointManager | None],
]
"""Async factory that returns a :class:`CheckpointManager` for the current
session, or ``None`` when there is no active session.

Used by :class:`~toddler.cli.commands.SlashCommandDispatcher` to lazily create
a checkpoint manager on demand (since the session ID is only known at runtime).
"""


# ---------------------------------------------------------------------------
# Checkpoint callback factory
# ---------------------------------------------------------------------------

def create_checkpoint_callback(
    ckpt_provider: CheckpointManagerProvider | None = None,
) -> CheckpointCallback:
    """Build a callback for
    :class:`~toddler.tools.executor.ToolExecutor`\\'s *checkpoint_cb*
    parameter.

    The returned callback creates a pre-mutation checkpoint via the
    session-scoped :class:`CheckpointManager` obtained from *ckpt_provider*
    before every mutating tool invocation.

    Parameters
    ----------
    ckpt_provider:
        Optional async factory that returns a session-scoped
        :class:`CheckpointManager`.  When *None* or when the factory
        returns *None*, the callback is a no-op (returns *None*).

    Returns
    -------
    CheckpointCallback
        An async callable matching
        ``Callable[[BaseTool, dict], Awaitable[str | None]]``.
    """
    async def _cb(tool: object, params: dict) -> str | None:  # noqa: C901
        if ckpt_provider is None:
            return None
        try:
            ckpt_mgr = await ckpt_provider()
        except Exception:
            logger.debug("Checkpoint provider failed — skipping checkpoint.")
            return None
        if ckpt_mgr is None:
            return None  # no active session

        try:
            tool_name = getattr(tool, "name", "unknown")
            summarize = getattr(tool, "summarize_call", None)
            summary = (
                summarize(**params) if callable(summarize)
                else str(params)[:80]
            )
            checkpoint = await ckpt_mgr.create(
                description=f"Before {tool_name}: {summary}",
                tool_name=tool_name,
                agent_state=AgentStateSnapshot(
                    mode="execute", iteration=0,
                ),
                message_index=0,
            )
            return checkpoint.id
        except Exception:
            logger.exception(
                f"Checkpoint creation failed for {tool_name} — "
                f"tool will execute without a safety net."
            )
            return None

    return _cb
