"""StorageManager — high-level storage lifecycle and message persistence.

Sits between the CLI / agent loop and :class:`~toddler.storage.store.SQLiteStore`.
Handles ContentBlock serialization, token accumulation, and all
business logic that shouldn't live in the raw data layer.
"""  # noqa: E501

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from toddler.llm.types import ContentBlock, Message, TokenUsage
from toddler.storage.models import (
    Conversation,
    ConversationSummary,
    Session,
    SessionSummary,
    StoredMessage,
)
from toddler.storage.store import SQLiteStore

logger = logging.getLogger(__name__)


# ======================================================================
# StorageManager
# ======================================================================


class StorageManager:
    """High-level manager for session persistence.

    Wraps :class:`SQLiteStore` with ContentBlock serialization
    and token-usage bookkeeping.

    Parameters
    ----------
    store:
        The underlying SQLite store (already opened).
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    # ==================================================================
    # Session lifecycle
    # ==================================================================

    async def create(
        self,
        *,
        title: str | None = None,
        mode: str = "execute",
        metadata: dict[str, Any] | None = None,
        cwd: str | None = None,
    ) -> Session:
        """Create a new session and persist it.

        All parameters are optional — sensible defaults are provided.

        Parameters
        ----------
        cwd:
            Working directory for this session.  Stored in
            ``metadata["cwd"]`` and used as a fallback title.
            Defaults to :func:`os.getcwd`.
        """
        cwd = cwd or os.getcwd()
        merged_metadata = {"cwd": cwd, **(metadata or {})}
        session = Session(
            id=uuid.uuid4().hex,
            title=title or os.path.basename(cwd),
            mode=mode,
            metadata=merged_metadata,
        )
        self._store.create_session(session)
        logger.info(f"Created session {session.id} (mode={mode}).")
        return session

    async def get(self, session_id: str) -> Session | None:
        """Return the session with *session_id*, or *None*."""
        return self._store.get_session(session_id)

    async def get_or_create(
        self,
        session_id: str | None = None,
        *,
        mode: str = "execute",
        cwd: str | None = None,
    ) -> Session:
        """Return the session identified by *session_id*, or create a new one.

        When *session_id* is *None* a fresh session is always created.

        Parameters
        ----------
        cwd:
            Working directory (forwarded to :meth:`create` when a new
            session is needed — ignored when resuming).
        """
        if session_id:
            session = self._store.get_session(session_id)
            if session:
                self._warn_cwd_mismatch(session, cwd)
                return session
            logger.warning(
                f"Session {session_id} not found — creating new session."
            )
        return await self.create(mode=mode, cwd=cwd)

    @staticmethod
    def _warn_cwd_mismatch(session: Session, cwd: str | None) -> None:
        """Log a warning if *session* was created in a different directory."""
        stored_cwd = session.metadata.get("cwd") if session.metadata else None
        current_cwd = cwd or os.getcwd()
        if stored_cwd and stored_cwd != current_cwd:
            logger.warning(
                f"Session {session.id[:12]} was created in "
                f"{stored_cwd}, but current directory is "
                f"{current_cwd}. Tool calls may fail if paths differ."
            )

    async def list_all(self) -> list[SessionSummary]:
        """Return all sessions, most-recently-updated first."""
        return self._store.list_sessions()

    async def delete(self, session_id: str) -> bool:
        """Delete *session_id* and all its messages / checkpoints.

        Returns ``True`` if the session existed.
        """
        return self._store.delete_session(session_id)

    async def update(self, session: Session) -> None:
        """Persist changes to *session* (title, mode, metadata, etc.)."""
        session.updated_at = datetime.now(UTC)
        self._store.update_session(session)

    # ==================================================================
    # Token tracking
    # ==================================================================

    async def accumulate_tokens(
        self, session_id: str, usage: TokenUsage,
    ) -> None:
        """Add *usage* counts to the session's running totals."""
        session = self._store.get_session(session_id)
        if session is None:
            logger.warning(
                f"Cannot accumulate tokens — session {session_id} not found."
            )
            return

        session.total_input_tokens += usage.input_tokens
        session.total_output_tokens += usage.output_tokens
        session.updated_at = datetime.now(UTC)
        self._store.update_session(session)

    # ==================================================================
    # Message persistence
    # ==================================================================

    async def append_message(
        self,
        session_id: str,
        message: Message,
        *,
        conversation_id: str,
        token_count: int = 0,
    ) -> int:
        """Serialise *message* and persist it as the next message.

        Parameters
        ----------
        session_id:
            The parent session ID.
        message:
            The Message to persist.
        conversation_id:
            The conversation this message belongs to (required).
        token_count:
            Optional token count for the message.

        Returns the new ``sequence_num``.
        """  # noqa: E501
        # Use global sequence within the session.
        current_count = self._store.get_message_count(session_id)
        stored = StoredMessage(
            session_id=session_id,
            conversation_id=conversation_id,
            sequence_num=current_count,
            role=message.role,
            content_json=_serialize_content(message.content),
            token_count=token_count,
            created_at=message.timestamp,
        )
        self._store.append_message(stored)

        # Update session counters.
        session = self._store.get_session(session_id)
        if session:
            session.message_count = current_count + 1
            session.updated_at = datetime.now(UTC)
            self._store.update_session(session)

        # Update conversation counters.
        conv = self._store.get_conversation(conversation_id)
        if conv:
            conv.message_count = (
                self._store.get_conversation_message_count(conversation_id)
            )
            conv.updated_at = datetime.now(UTC)
            self._store.update_conversation(conv)

        return stored.sequence_num

    async def get_messages(
        self,
        session_id: str,
        *,
        conversation_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[Message]:
        """Retrieve and deserialise messages for *session_id*.

        When *conversation_id* is provided, only messages from that
        conversation are returned.  When *after_sequence* is set, only
        messages with ``sequence_num > after_sequence`` are returned.
        """
        stored_list = self._store.get_messages(
            session_id,
            conversation_id=conversation_id,
            after_sequence=after_sequence,
        )
        return [_stored_to_message(s) for s in stored_list]

    # ==================================================================
    # Conversation lifecycle
    # ==================================================================

    async def create_conversation(
        self,
        session_id: str,
        *,
        title: str | None = None,
        status: str = "active",
    ) -> Conversation:
        """Create a new conversation within *session_id*.

        Returns the new :class:`Conversation`.
        """
        # Get the next global sequence_num from session messages.
        current_count = self._store.get_message_count(session_id)
        conv = Conversation(
            id=uuid.uuid4().hex,
            session_id=session_id,
            title=title,
            sequence_num=current_count,
            status=status,
        )
        self._store.create_conversation(conv)
        logger.info(
            f"Created conversation {conv.id} in session {session_id}."
        )
        return conv

    async def get_or_create_active_conversation(
        self, session_id: str,
    ) -> Conversation:
        """Return the active conversation for *session_id*.

        If no active conversation exists (e.g. migrated session), create one.
        """
        conv = self._store.get_active_conversation(session_id)
        if conv is not None:
            return conv
        logger.info(
            f"No active conversation for session {session_id} — creating."
        )
        return await self.create_conversation(session_id)

    async def list_conversations(
        self, session_id: str,
    ) -> list[ConversationSummary]:
        """Return all conversations for *session_id*, newest first."""
        return self._store.list_conversations(session_id)

    async def get_conversation(
        self, conversation_id: str,
    ) -> Conversation | None:
        """Return the conversation with *conversation_id*, or *None*."""
        return self._store.get_conversation(conversation_id)

    async def update_conversation(self, conv: Conversation) -> None:
        """Persist changes to *conv* (title, compaction pointers, etc.)."""
        conv.updated_at = datetime.now(UTC)
        self._store.update_conversation(conv)

    async def archive_conversation(
        self, conversation_id: str,
    ) -> None:
        """Archive *conversation_id* so it's no longer active."""
        self._store.archive_conversation(conversation_id)
        logger.info(f"Archived conversation {conversation_id}.")

    async def get_conversation_summaries(
        self, session_id: str, *, exclude_id: str | None = None,
    ) -> list[tuple[int, str]]:
        """Return ``(sequence_num, title)`` tuples for all non-empty
        conversations in *session_id*, excluding *exclude_id* if given.

        Used for cross-conversation context injection into the system prompt.
        """
        convs = await self.list_conversations(session_id)
        result: list[tuple[int, str]] = []
        for c in convs:
            if exclude_id is not None and c.id == exclude_id:
                continue
            if c.message_count > 0:
                result.append(
                    (c.sequence_num, c.display_title)
                )
        return result

    # ==================================================================
    # Checkpoint delegates  (Phase 9 will build on these)
    # ==================================================================

    async def list_checkpoints(
        self, session_id: str,
    ) -> list[dict[str, Any]]:
        """Return all checkpoints for *session_id*, newest first."""
        return self._store.list_checkpoints(session_id)


# ---------------------------------------------------------------------------
# CLI display helper
# ---------------------------------------------------------------------------


async def print_sessions(mgr: StorageManager) -> None:
    """Print a formatted table of all saved sessions to stdout."""
    sessions = await mgr.list_all()
    if not sessions:
        print("No saved sessions.")
        return

    print(f"{'ID':<34} {'Title':<40} {'Msgs':>5}  {'Age'}")
    print("-" * 100)
    for s in sessions:
        sid = s.id[:32]
        title = (s.display_title or "—")[:39]
        print(f"{sid:<34} {title:<40} {s.message_count:>5}  {s.age}")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_content(blocks: list[ContentBlock]) -> str:
    """Convert a list of ContentBlock objects to a JSON string."""

    def _block_to_dict(b: ContentBlock) -> dict[str, Any]:
        d: dict[str, Any] = {"type": b.type}
        if b.text is not None:
            d["text"] = b.text
        if b.tool_id is not None:
            d["tool_id"] = b.tool_id
        if b.tool_name is not None:
            d["tool_name"] = b.tool_name
        if b.tool_input is not None:
            d["tool_input"] = b.tool_input
        if b.tool_result_content is not None:
            d["tool_result_content"] = b.tool_result_content
        if b.is_error is not None:
            d["is_error"] = b.is_error
        return d

    return json.dumps(
        [_block_to_dict(b) for b in blocks], ensure_ascii=False, default=str,
    )


def _deserialize_content(json_str: str) -> list[ContentBlock]:
    """Parse a JSON string back into ContentBlock objects."""

    def _dict_to_block(d: dict[str, Any]) -> ContentBlock:
        return ContentBlock(
            type=d["type"],
            text=d.get("text"),
            tool_id=d.get("tool_id"),
            tool_name=d.get("tool_name"),
            tool_input=d.get("tool_input"),
            tool_result_content=d.get("tool_result_content"),
            is_error=d.get("is_error"),
        )

    raw = json.loads(json_str)
    if not isinstance(raw, list):
        return []
    return [_dict_to_block(item) for item in raw]


def _message_to_stored(
    session_id: str, seq: int, msg: Message,
) -> StoredMessage:
    """Convert a :class:`Message` to a :class:`StoredMessage`."""
    return StoredMessage(
        session_id=session_id,
        sequence_num=seq,
        role=msg.role,
        content_json=_serialize_content(msg.content),
        token_count=0,
        created_at=msg.timestamp,
    )


def _stored_to_message(stored: StoredMessage) -> Message:
    """Convert a :class:`StoredMessage` back to a :class:`Message`."""
    return Message(
        role=stored.role,  # type: ignore[arg-type]
        content=_deserialize_content(stored.content_json),
        timestamp=stored.created_at,
    )
