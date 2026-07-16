"""Session data models — Session, SessionSummary, StoredMessage.

These are the persistence-layer dataclasses for the SQLite-backed session
store.  They are deliberately separate from :mod:`toddler.llm.types` to keep
the wire-format and storage concerns decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """A persistent conversation session.

    Parameters
    ----------
    id:
        UUID4 string generated on creation.
    created_at:
        UTC timestamp of first creation.
    updated_at:
        UTC timestamp of last modification (new message, compaction, etc.).
    title:
        Human-readable title produced by auto-titling.  *None* until the
        background title task completes.
    message_count:
        Total messages stored for this session.
    total_input_tokens:
        Cumulative input tokens across all LLM calls in this session.
    total_output_tokens:
        Cumulative output tokens across all LLM calls in this session.
    mode:
        Current agent mode — ``"execute"``, ``"plan"``, or ``"plan_execute"``.
    metadata:
        Arbitrary key-value pairs (project path, git branch, etc.).
    """

    id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    title: str | None = None
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    mode: str = "execute"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Sum of input + output tokens accumulated so far."""
        return self.total_input_tokens + self.total_output_tokens


# ---------------------------------------------------------------------------
# SessionSummary  (lightweight listing row)
# ---------------------------------------------------------------------------


@dataclass
class SessionSummary:
    """A lightweight row returned by :meth:`~toddler.session.manager.SessionManager.list_all` method for display purposes.

    Only the fields needed to render a session picker / list are included.
    """  # noqa: E501

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int

    @property
    def display_title(self) -> str:
        """Return *title* if set, otherwise a fallback label."""
        return self.title or f"Session {self.id[:8]}"

    @property
    def age(self) -> str:
        """Human-readable age string (e.g. "3 hours ago")."""
        delta = datetime.now(UTC) - self.created_at
        seconds = delta.total_seconds()
        if seconds < 60:
            return "just now"
        minutes = seconds / 60
        if minutes < 60:
            return f"{int(minutes)}m ago"
        hours = minutes / 60
        if hours < 24:
            return f"{int(hours)}h ago"
        days = hours / 24
        if days < 7:
            return f"{int(days)}d ago"
        weeks = days / 7
        return f"{int(weeks)}w ago"


# ---------------------------------------------------------------------------
# StoredMessage
# ---------------------------------------------------------------------------


@dataclass
class StoredMessage:
    """A single message row as stored in the ``messages`` table.

    The ``content_json`` field holds the serialised list[:class:`~toddler.llm.types.ContentBlock`] - use
    :meth:`~toddler.session.manager._serialize_content` / :meth:`~toddler.session.manager._deserialize_content`
    to go between Python objects and the database representation.
    """  # noqa: E501

    id: int | None = None  # auto-increment, *None* until persisted
    session_id: str = ""
    sequence_num: int = 0
    role: str = "user"
    content_json: str = "[]"
    token_count: int = 0
    is_compacted: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
