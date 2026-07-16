"""SessionManager — high-level session lifecycle and message persistence.

Sits between the CLI / agent loop and :class:`~toddler.session.store.SQLiteStore`.
Handles ContentBlock serialization, auto-titling, token accumulation, and all
business logic that shouldn't live in the raw data layer.
"""  # noqa: E501

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from toddler.llm.types import ContentBlock, Message, TokenUsage
from toddler.session.models import Session, SessionSummary, StoredMessage
from toddler.session.store import SQLiteStore

if TYPE_CHECKING:
    from toddler.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_TITLE_PROMPT = (
    "Generate a short title (3-6 words) for a conversation that starts "
    "with this user message.  Return ONLY the title, no quotes, no "
    "explanation, no punctuation at the end.\n\n"
    "User message: {first_message}\n\n"
    "Title:"
)


# ======================================================================
# SessionManager
# ======================================================================


class SessionManager:
    """High-level manager for session persistence.

    Wraps :class:`SQLiteStore` with ContentBlock serialization, background
    auto-titling, and token-usage bookkeeping.

    Parameters
    ----------
    store:
        The underlying SQLite store (already opened).
    llm_provider:
        Optional LLM provider used for auto-titling.  When *None*,
        auto-titling is skipped.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        llm_provider: BaseLLMProvider | None = None,
    ) -> None:
        self._store = store
        self._llm = llm_provider

    # ==================================================================
    # Session lifecycle
    # ==================================================================

    async def create(
        self,
        *,
        title: str | None = None,
        mode: str = "execute",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session and persist it.

        All parameters are optional — sensible defaults are provided.
        """
        session = Session(
            id=uuid.uuid4().hex,
            title=title,
            mode=mode,
            metadata=metadata or {},
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
    ) -> Session:
        """Return the session identified by *session_id*, or create a new one.

        When *session_id* is *None* a fresh session is always created.
        """
        if session_id:
            session = self._store.get_session(session_id)
            if session:
                return session
            logger.warning(
                f"Session {session_id} not found — creating new session."
            )
        return await self.create(mode=mode)

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
        token_count: int = 0,
    ) -> int:
        """Serialise *message* and persist it as the next message in *session_id*.

        Returns the new ``sequence_num``.
        """  # noqa: E501
        current_count = self._store.get_message_count(session_id)
        stored = StoredMessage(
            session_id=session_id,
            sequence_num=current_count,
            role=message.role,
            content_json=_serialize_content(message.content),
            token_count=token_count,
            created_at=message.timestamp,
        )
        self._store.append_message(stored)

        # Update the session's message_count and timestamp.
        session = self._store.get_session(session_id)
        if session:
            session.message_count = current_count + 1
            session.updated_at = datetime.now(UTC)
            self._store.update_session(session)

        return stored.sequence_num

    async def get_messages(
        self,
        session_id: str,
        *,
        exclude_compacted: bool = True,
        after_sequence: int | None = None,
    ) -> list[Message]:
        """Retrieve and deserialise all messages for *session_id*."""
        stored_list = self._store.get_messages(
            session_id,
            exclude_compacted=exclude_compacted,
            after_sequence=after_sequence,
        )
        return [_stored_to_message(s) for s in stored_list]

    async def replace_messages(
        self,
        session_id: str,
        messages: list[Message],
        *,
        mark_compacted: bool = False,
    ) -> None:
        """Atomically replace all messages for *session_id*.

        Used by the compactor (Phase 7) to swap original messages with
        summary versions.
        """
        to_persist = [
            _message_to_stored(session_id, i, m)
            for i, m in enumerate(messages)
        ]
        self._store.replace_messages(
            session_id, to_persist, mark_compacted=mark_compacted,
        )

        # Update message_count to match.
        session = self._store.get_session(session_id)
        if session:
            session.message_count = len(to_persist)
            session.updated_at = datetime.now(UTC)
            self._store.update_session(session)

    async def compact_messages(
        self,
        session_id: str,
        compacted_messages: list[Message],
    ) -> None:
        """Replace session messages with compacted versions.

        Old messages are marked ``is_compacted=True`` rather than deleted
        so they can be inspected for debugging.
        """
        await self.replace_messages(
            session_id, compacted_messages, mark_compacted=True,
        )

    # ==================================================================
    # Auto-titling
    # ==================================================================

    def auto_title_background(
        self,
        session_id: str,
        first_user_message: str,
    ) -> None:
        """Launch a non-blocking background task to generate a session title.

        Call this **after** the first user message has been appended.  The
        title will be updated asynchronously — it's a best-effort convenience
        feature; failures are silently swallowed.

        Parameters
        ----------
        session_id:
            The session to title.
        first_user_message:
            The text of the first user message, used as input to the LLM.
        """
        if self._llm is None:
            logger.debug("No LLM provider available — skipping auto-title.")
            return

        asyncio.create_task(
            self._auto_title(session_id, first_user_message)
        )

    async def _auto_title(
        self, session_id: str, first_user_message: str,
    ) -> None:
        """Generate a title by calling the LLM, then persist it."""
        try:
            prompt = _TITLE_PROMPT.format(first_message=first_user_message)
            title = await self._llm.generate_compact(prompt)
            title = title.strip().strip('"').strip("'")
            # Enforce a reasonable max length.
            if len(title) > 100:
                title = title[:97] + "..."

            session = self._store.get_session(session_id)
            if session is None:
                return

            session.title = title if title else None
            session.updated_at = datetime.now(UTC)
            self._store.update_session(session)
            logger.warning(f"Auto-titled session {session_id}: {title}")
        except Exception:
            logger.exception(
                f"Auto-title failed for session {session_id} — ignoring."
            )

    # ==================================================================
    # Checkpoint delegates  (Phase 9 will build on these)
    # ==================================================================

    async def list_checkpoints(
        self, session_id: str,
    ) -> list[dict[str, Any]]:
        """Return all checkpoints for *session_id*, newest first."""
        return self._store.list_checkpoints(session_id)


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
