"""CheckpointManager — create snapshots, rollback files + conversation.

Coordinates between :class:`~toddler.checkpoint.snapshot.GitSnapshotter`
(preferred) and :class:`~toddler.checkpoint.snapshot.FileSnapshotter`
(fallback) for filesystem snapshots, and the
:class:`~toddler.session.store.SQLiteStore` for persistence.

Rollback restores **both** the filesystem **and** the conversation — messages
after the checkpoint are truncated.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    FileManifestEntry,
    RollbackResult,
)
from toddler.checkpoint.snapshot import FileSnapshotter, GitSnapshotter
from toddler.config.defaults import CHECKPOINT_KEEP_LATEST

if TYPE_CHECKING:
    from toddler.session.manager import SessionManager
    from toddler.session.store import SQLiteStore
    from toddler.tools.executor import CheckpointCallback

logger = logging.getLogger(__name__)


# ======================================================================
# CheckpointManager
# ======================================================================


class CheckpointManager:
    """Create and manage pre-mutation checkpoints for a single session.

    Parameters
    ----------
    store:
        The SQLite store (already opened) used to persist checkpoints.
    session_id:
        The session that all checkpoint operations are scoped to.
    repo_root:
        Absolute path to the working directory (used for git operations).
    session_manager:
        Optional session manager reference — needed for
        :meth:`rollback_to` to truncate conversation messages.  When
        *None*, rollback will only restore files.
    """

    def __init__(
        self,
        store: SQLiteStore,
        session_id: str,
        repo_root: str | Path,
        *,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._repo_root = Path(repo_root).resolve()

        self._git = GitSnapshotter(self._repo_root)
        self._file = FileSnapshotter()
        self._session_mgr = session_manager

    # ==================================================================
    # Public API
    # ==================================================================

    async def create(
        self,
        *,
        description: str,
        tool_name: str,
        agent_state: AgentStateSnapshot,
        message_index: int,
    ) -> Checkpoint:
        """Snapshot filesystem + agent state before a mutating tool executes.

        The filesystem snapshot uses git when available (via
        ``git commit-tree`` — dangling commits that do **not** touch the
        stash stack).  Falls back to a file-copy manifest when git is
        unavailable.

        Parameters
        ----------
        description:
            Human-readable label, e.g. ``"Before EditFile: auth.py line 42"``.
        tool_name:
            Name of the tool that is about to run.
        agent_state:
            Snapshot of the agent loop's current mode, iteration, and plan.
        message_index:
            Index of the last persisted message before this tool executes.
            A rollback will truncate the conversation to this index.
        """
        checkpoint_id = uuid.uuid4().hex
        seq_num = self._next_sequence_num()

        # --- filesystem snapshot ---
        git_ref: str | None = None
        manifest: list[FileManifestEntry] | None = None

        if self._git.available:
            git_ref = await self._git.create()
            if git_ref is None:
                logger.warning(
                    "Git snapshot create returned empty — "
                    "no changes captured."
                )
        else:
            # File-copy fallback: snapshot all tracked files.
            files = list(self._repo_root.rglob("*"))
            files = [f for f in files if f.is_file() and not _is_ignored(f)]
            if files:
                manifest = await self._file.create(
                    self._session_id, checkpoint_id, files,
                )

        # --- persist checkpoint ---
        created_at = datetime.now(UTC)
        self._store.create_checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=self._session_id,
            sequence_num=seq_num,
            created_at=created_at,
            description=description,
            tool_name=tool_name,
            git_ref=git_ref,
            file_manifest_json=_serialize_manifest(manifest) if manifest else None,
            agent_state_json=_serialize_agent_state(agent_state),
            message_index=message_index,
        )

        checkpoint = Checkpoint(
            id=checkpoint_id,
            session_id=self._session_id,
            sequence_num=seq_num,
            created_at=created_at,
            description=description,
            tool_name=tool_name,
            git_ref=git_ref,
            file_manifest=manifest,
            agent_state=agent_state,
            message_index=message_index,
        )

        logger.info(
            f"Checkpoint {checkpoint_id[:12]} created "
            f"(#{seq_num}, {tool_name}, "
            f"git={'yes' if git_ref else 'no'})."
        )
        return checkpoint

    def executor_callback(
        self,
        agent_state: AgentStateSnapshot,
        message_index: int,
    ) -> CheckpointCallback:
        """Return a callback suitable for :class:`ToolExecutor`'s
        ``checkpoint_cb`` parameter.

        The returned callable captures *agent_state* and *message_index*
        and creates a checkpoint via :meth:`create` before every mutating
        tool invocation.

        Parameters
        ----------
        agent_state:
            Snapshot of the current agent loop state (mode, iteration).
        message_index:
            The index of the last persisted message before tool execution
            begins.  On rollback the conversation is truncated here.

        Returns
        -------
        CheckpointCallback
            An async callable matching the
            ``Callable[[BaseTool, dict], Awaitable[str | None]]`` protocol.
        """  # noqa: E501
        from toddler.tools.base import BaseTool

        async def _cb(
            tool: BaseTool, params: dict,
        ) -> str | None:
            try:
                checkpoint = await self.create(
                    description=(
                        f"Before {tool.name}: "
                        f"{tool.summarize_call(**params)}"
                    ),
                    tool_name=tool.name,
                    agent_state=agent_state,
                    message_index=message_index,
                )
                return checkpoint.id
            except Exception:
                logger.exception(
                    f"Checkpoint creation failed for {tool.name} — "
                    f"tool will execute without a safety net."
                )
                return None

        return _cb

    async def rollback_to(self, checkpoint_id: str) -> RollbackResult:
        """Restore filesystem state and truncate conversation to *checkpoint_id*.

        Returns a :class:`RollbackResult` with details of what was restored.
        The caller should also restore agent loop state (mode, iteration,
        plan) from the checkpoint's :attr:`Checkpoint.agent_state`.

        Raises
        ------
        ValueError
            If *checkpoint_id* is not found.
        """
        row = self._store.get_checkpoint(checkpoint_id)
        if row is None:
            raise ValueError(
                f"Checkpoint '{checkpoint_id}' not found."
            )

        checkpoint = _row_to_checkpoint(row)
        warnings: list[str] = []
        restored_files: list[str] = []

        # --- restore filesystem ---
        if checkpoint.git_ref:
            try:
                restored_files = await self._git.restore(checkpoint.git_ref)
            except Exception as exc:
                warnings.append(
                    f"Git restore failed ({exc}) — trying file-copy fallback."
                )
                logger.exception("Git restore failed.")
                # Fall through to file-copy restore if manifest exists.
                if checkpoint.file_manifest:
                    restored_files = await self._file.restore(
                        checkpoint.session_id,
                        checkpoint.id,
                        checkpoint.file_manifest,
                    )
                else:
                    return RollbackResult(
                        success=False,
                        warnings=warnings,
                    )
        elif checkpoint.file_manifest:
            restored_files = await self._file.restore(
                checkpoint.session_id,
                checkpoint.id,
                checkpoint.file_manifest,
            )
        else:
            warnings.append(
                "Checkpoint has neither git_ref nor file_manifest — "
                "nothing to restore."
            )

        # --- truncate conversation ---
        restored_msg_index = checkpoint.message_index
        if self._session_mgr is not None:
            try:
                messages = await self._session_mgr.get_messages(
                    self._session_id,
                )
                # Keep only messages up to and including message_index.
                truncated = messages[: checkpoint.message_index + 1]

                # Insert a rollback marker so the user/LLM can see what
                # happened.
                from toddler.llm.types import ContentBlock, Message

                marker = Message.user(
                    [
                        ContentBlock(
                            type="text",
                            text=(
                                f"⚡ **Rolled back to checkpoint "
                                f"`{checkpoint_id[:12]}`**\n\n"
                                f"Description: {checkpoint.description}\n"
                                f"Restored {len(restored_files)} file(s)."
                            ),
                        )
                    ]
                )
                truncated.append(marker)

                await self._session_mgr.replace_messages(
                    self._session_id, truncated,
                )
                restored_msg_index = len(truncated) - 1
            except Exception as exc:
                warnings.append(
                    f"Failed to truncate conversation messages: {exc}"
                )
                logger.exception("Message truncation during rollback failed.")

        success = len(restored_files) > 0 or restored_msg_index >= 0

        return RollbackResult(
            success=success,
            restored_files=restored_files,
            restored_message_index=restored_msg_index,
            warnings=warnings,
        )

    async def list_for_session(self) -> list[Checkpoint]:
        """Return all checkpoints for the current session, newest first."""
        rows = self._store.list_checkpoints(self._session_id)
        return [_row_to_checkpoint(r) for r in rows]

    async def get(self, checkpoint_id: str) -> Checkpoint | None:
        """Return a single checkpoint by id, or *None*."""
        row = self._store.get_checkpoint(checkpoint_id)
        if row is None:
            return None
        return _row_to_checkpoint(row)

    async def prune(
        self, *, keep_latest: int = CHECKPOINT_KEEP_LATEST,
    ) -> int:
        """Delete old checkpoints, keeping the most recent *keep_latest*.

        Also cleans up file-snapshot directories for deleted checkpoints.

        Returns the count of deleted checkpoints.
        """
        # Collect which checkpoints will be deleted (for cleanup).
        all_rows = self._store.list_checkpoints(self._session_id)
        to_delete = all_rows[keep_latest:]

        deleted = self._store.prune_checkpoints(
            self._session_id, keep_latest=keep_latest,
        )

        # Clean up file-snapshot directories.
        for row in to_delete:
            ck = _row_to_checkpoint(row)
            if ck.file_manifest and not ck.git_ref:
                await self._file.cleanup(
                    ck.session_id, ck.id,
                )

        if deleted:
            logger.info(
                f"Pruned {deleted} checkpoint(s) from session "
                f"{self._session_id} (keeping latest {keep_latest})."
            )
        return deleted

    async def delete(self, checkpoint_id: str) -> bool:
        """Delete a single checkpoint and its file-snapshot directory."""
        row = self._store.get_checkpoint(checkpoint_id)
        if row is not None:
            ck = _row_to_checkpoint(row)
            if ck.file_manifest:
                await self._file.cleanup(ck.session_id, ck.id)

        return self._store.delete_checkpoint(checkpoint_id)

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _next_sequence_num(self) -> int:
        """Return the next sequence number for this session."""
        existing = self._store.list_checkpoints(self._session_id)
        if not existing:
            return 1
        return max(r.get("sequence_num", 0) for r in existing) + 1


# ======================================================================
# Serialization helpers
# ======================================================================


def _serialize_agent_state(state: AgentStateSnapshot) -> str:
    return json.dumps(
        {
            "mode": state.mode,
            "iteration": state.iteration,
            "current_plan_json": state.current_plan_json,
            "pending_tool_calls_json": state.pending_tool_calls_json,
        },
        ensure_ascii=False,
    )


def _deserialize_agent_state(raw: str) -> AgentStateSnapshot:
    d = json.loads(raw)
    return AgentStateSnapshot(
        mode=d.get("mode", "execute"),
        iteration=d.get("iteration", 0),
        current_plan_json=d.get("current_plan_json"),
        pending_tool_calls_json=d.get("pending_tool_calls_json"),
    )


def _serialize_manifest(manifest: list[FileManifestEntry]) -> str:
    return json.dumps(
        [
            {"path": e.path, "sha256": e.sha256, "size": e.size}
            for e in manifest
        ],
        ensure_ascii=False,
    )


def _deserialize_manifest(raw: str) -> list[FileManifestEntry]:
    items = json.loads(raw)
    if not isinstance(items, list):
        return []
    return [
        FileManifestEntry(
            path=item["path"],
            sha256=item["sha256"],
            size=item["size"],
        )
        for item in items
    ]


def _row_to_checkpoint(row: dict) -> Checkpoint:
    """Convert a raw database row (dict) into a :class:`Checkpoint`."""
    return Checkpoint(
        id=row["id"],
        session_id=row["session_id"],
        sequence_num=row["sequence_num"],
        created_at=_parse_dt(row["created_at"]),
        description=row.get("description", ""),
        tool_name=row.get("tool_name", ""),
        git_ref=row.get("git_ref"),
        file_manifest=(
            _deserialize_manifest(row["file_manifest_json"])
            if row.get("file_manifest_json")
            else None
        ),
        agent_state=(
            _deserialize_agent_state(row["agent_state_json"])
            if row.get("agent_state_json")
            else AgentStateSnapshot(mode="execute", iteration=0)
        ),
        message_index=row.get("message_index", 0),
    )


def _parse_dt(val: str | datetime) -> datetime:
    """Parse an ISO datetime string, passing through datetime objects."""
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(val)


def _is_ignored(path: Path) -> bool:
    """Return ``True`` if *path* should be excluded from file snapshots."""
    parts = path.parts
    for skip in (".git", "__pycache__", ".venv", "venv", ".toddler",
                 "node_modules", ".tox", ".eggs", "*.egg-info"):
        if skip in parts:
            return True
    return False
