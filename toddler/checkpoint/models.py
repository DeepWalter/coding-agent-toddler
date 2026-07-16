"""Checkpoint data models — Checkpoint, AgentStateSnapshot, RollbackResult.

These dataclasses represent the domain objects for the checkpoint subsystem.
The persistence layer lives in :class:`~toddler.session.store.SQLiteStore`;
these models are the in-memory shape used by the manager and snapshotters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# AgentStateSnapshot
# ---------------------------------------------------------------------------


@dataclass
class AgentStateSnapshot:
    """A point-in-time capture of the agent loop's internal state.

    Stored inside each :class:`Checkpoint` so a rollback can restore the
    agent to the exact state it was in before a mutating tool ran.
    """

    mode: str  # "execute", "plan", or "plan_execute"
    iteration: int
    current_plan_json: str | None = None
    pending_tool_calls_json: str | None = None


# ---------------------------------------------------------------------------
# FileManifestEntry
# ---------------------------------------------------------------------------


@dataclass
class FileManifestEntry:
    """A single file recorded in a checkpoint manifest.

    Used by the file-copy snapshot fallback when git is unavailable.
    """

    path: str
    sha256: str
    size: int


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """A pre-mutation snapshot created before a mutating tool executes.

    Captures both the filesystem state (via git or file-copy snapshot) and
    the agent loop state (iteration, mode, message index) so that
    :meth:`CheckpointManager.rollback_to` can restore both.
    """

    id: str  # UUID4
    session_id: str
    sequence_num: int
    created_at: datetime
    description: str  # e.g. "Before EditFile: auth.py line 42"
    tool_name: str
    git_ref: str | None = None  # Git stash commit hash (preferred)
    file_manifest: list[FileManifestEntry] | None = None  # file-copy fallback
    agent_state: AgentStateSnapshot = field(
        default_factory=lambda: AgentStateSnapshot(
            mode="execute", iteration=0,
        )
    )
    message_index: int = 0


# ---------------------------------------------------------------------------
# RollbackResult
# ---------------------------------------------------------------------------


@dataclass
class RollbackResult:
    """The outcome of a checkpoint rollback operation."""

    success: bool
    restored_files: list[str] = field(default_factory=list)
    restored_message_index: int = 0
    warnings: list[str] = field(default_factory=list)
