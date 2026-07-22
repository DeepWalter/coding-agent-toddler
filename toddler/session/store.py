"""SQLiteStore — low-level database access for sessions, messages, checkpoints,
and conversations.

Manages schema creation, migrations, and raw CRUD operations.  The store is
intentionally "dumb" — it doesn't know about LLM types, content
serialization, or business logic.  That layer lives in
:class:`~toddler.session.manager.StorageManager`.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from toddler.session.models import (
    Conversation,
    ConversationSummary,
    Session,
    SessionSummary,
    StoredMessage,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Current schema version (integer — increments on every schema change)
# ---------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_CREATE_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);
"""

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'execute',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence_num INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    is_compacted BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence_num)
);
"""

_CREATE_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence_num INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    description TEXT NOT NULL,
    tool_name TEXT,
    git_ref TEXT,
    file_manifest_json TEXT,
    agent_state_json TEXT NOT NULL,
    message_index INTEGER NOT NULL
);
"""

_CREATE_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT,
    sequence_num INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    compacted_summary TEXT,
    compacted_at_seq INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_messages_session "
    "ON messages(session_id, sequence_num);",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
    "ON messages(conversation_id, sequence_num);",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_session "
    "ON checkpoints(session_id, sequence_num);",
    "CREATE INDEX IF NOT EXISTS idx_conversations_session "
    "ON conversations(session_id, status);",
]

# ======================================================================
# SQLiteStore
# ======================================================================


class SQLiteStore:
    """Low-level SQLite database for session persistence.

    Opens (or creates) the database at *db_path*, ensures the schema is
    up-to-date, and exposes raw CRUD methods.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.  Parent directories are
        created automatically.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path).expanduser().resolve()

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def open(self) -> None:
        """Open the database and ensure the schema is current.

        Safe to call multiple times — subsequent calls are no-ops if the
        connection is already open and healthy.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._migrate(conn)
        finally:
            conn.close()

    def close(self) -> None:
        """No-op — connections are closed per-operation.

        SQLite connections are created and closed within each public method
        so the store is always safe to use from multiple asyncio tasks.
        """

    # ==================================================================
    # Session CRUD
    # ==================================================================

    def create_session(self, session: Session) -> Session:
        """Insert *session* into the database.

        Returns *session* with its timestamps unchanged (the caller
        populates them).
        """
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sessions
                   (id, title, created_at, updated_at, message_count,
                    total_input_tokens, total_output_tokens, mode,
                    metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.id,
                    session.title,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.message_count,
                    session.total_input_tokens,
                    session.total_output_tokens,
                    session.mode,
                    _json_dumps(session.metadata),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Return the session with *session_id*, or *None*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()

        return self._row_to_session(row) if row else None

    def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]:
        """Return recent sessions ordered by ``updated_at`` descending."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, title, created_at, updated_at, message_count
                   FROM sessions
                   ORDER BY updated_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        finally:
            conn.close()

        return [self._row_to_summary(r) for r in rows]

    def update_session(self, session: Session) -> bool:
        """Persist changes to *session*.  Returns ``True`` if a row was updated."""  # noqa: E501
        conn = self._connect()
        try:
            cur = conn.execute(
                """UPDATE sessions
                   SET title = ?, updated_at = ?, message_count = ?,
                       total_input_tokens = ?, total_output_tokens = ?,
                       mode = ?, metadata_json = ?
                   WHERE id = ?""",
                (
                    session.title,
                    session.updated_at.isoformat(),
                    session.message_count,
                    session.total_input_tokens,
                    session.total_output_tokens,
                    session.mode,
                    _json_dumps(session.metadata),
                    session.id,
                ),
            )
            conn.commit()
            updated = cur.rowcount > 0
        finally:
            conn.close()
        return updated

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages + checkpoints (CASCADE).

        Returns ``True`` if a row was deleted.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
        return deleted

    # ==================================================================
    # Message CRUD
    # ==================================================================

    def append_message(self, msg: StoredMessage) -> StoredMessage:
        """Insert *msg* and return it with ``id`` populated."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO messages
                   (session_id, conversation_id, sequence_num, role,
                    content_json, token_count, is_compacted, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.session_id,
                    msg.conversation_id,
                    msg.sequence_num,
                    msg.role,
                    msg.content_json,
                    msg.token_count,
                    int(msg.is_compacted),
                    msg.created_at.isoformat(),
                ),
            )
            conn.commit()
            msg.id = cur.lastrowid
        finally:
            conn.close()
        return msg

    def get_messages(
        self,
        session_id: str,
        *,
        conversation_id: str | None = None,
        after_sequence: int | None = None,
    ) -> list[StoredMessage]:
        """Return messages for *session_id*, ordered by ``sequence_num``.

        Parameters
        ----------
        conversation_id:
            When set, filter by this conversation.  When *None*, all messages
            for the session are returned (backward-compatible fallback).
        after_sequence:
            When set, only return messages with ``sequence_num > after_sequence``.
        """  # noqa: E501
        conn = self._connect()
        try:
            clauses = ["session_id = ?"]
            params: list[Any] = [session_id]

            if conversation_id:
                clauses.append("conversation_id = ?")
                params.append(conversation_id)
            if after_sequence is not None:
                clauses.append("sequence_num > ?")
                params.append(after_sequence)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY sequence_num",
                params,
            ).fetchall()
        finally:
            conn.close()

        return [self._row_to_message(r) for r in rows]

    def get_message_count(self, session_id: str) -> int:
        """Return the number of messages stored for *session_id*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    def truncate_messages(
        self, session_id: str, *, after_sequence: int,
    ) -> int:
        """Delete all messages for *session_id* with ``sequence_num >
        *after_sequence*``.

        Returns the count of deleted messages.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM messages "
                "WHERE session_id = ? AND sequence_num > ?",
                (session_id, after_sequence),
            )
            deleted = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return deleted

    # ==================================================================
    # Conversation CRUD
    # ==================================================================

    def create_conversation(self, conv: Conversation) -> Conversation:
        """Insert *conv* into the database and return it."""
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO conversations
                   (id, session_id, title, sequence_num, status,
                    compacted_summary, compacted_at_seq, created_at,
                    updated_at, message_count, total_input_tokens,
                    total_output_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conv.id,
                    conv.session_id,
                    conv.title,
                    conv.sequence_num,
                    conv.status,
                    conv.compacted_summary,
                    conv.compacted_at_seq,
                    conv.created_at.isoformat(),
                    conv.updated_at.isoformat(),
                    conv.message_count,
                    conv.total_input_tokens,
                    conv.total_output_tokens,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return conv

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the conversation with *conversation_id*, or *None*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_conversation(row) if row else None

    def get_active_conversation(
        self, session_id: str,
    ) -> Conversation | None:
        """Return the active conversation for *session_id*, or *None*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM conversations "
                "WHERE session_id = ? AND status = 'active' "
                "ORDER BY created_at DESC",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_conversation(row) if row else None

    def list_conversations(
        self,
        session_id: str,
    ) -> list[ConversationSummary]:
        """Return all conversations for *session_id*, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, session_id, title, sequence_num, status,
                          message_count, created_at, updated_at
                   FROM conversations
                   WHERE session_id = ?
                   ORDER BY created_at ASC""",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_conversation_summary(r) for r in rows]

    def update_conversation(self, conv: Conversation) -> bool:
        """Persist changes to *conv*.  Returns ``True`` if a row was updated."""  # noqa: E501
        conn = self._connect()
        try:
            cur = conn.execute(
                """UPDATE conversations
                   SET title = ?, status = ?, compacted_summary = ?,
                       compacted_at_seq = ?, updated_at = ?,
                       message_count = ?, total_input_tokens = ?,
                       total_output_tokens = ?
                   WHERE id = ?""",
                (
                    conv.title,
                    conv.status,
                    conv.compacted_summary,
                    conv.compacted_at_seq,
                    conv.updated_at.isoformat(),
                    conv.message_count,
                    conv.total_input_tokens,
                    conv.total_output_tokens,
                    conv.id,
                ),
            )
            conn.commit()
            updated = cur.rowcount > 0
        finally:
            conn.close()
        return updated

    def archive_conversation(self, conversation_id: str) -> bool:
        """Set the status of *conversation_id* to ``'archived'``.

        Returns ``True`` if a row was updated.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE conversations SET status = 'archived', "
                "updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), conversation_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
        finally:
            conn.close()
        return updated

    def get_conversation_message_count(
        self, conversation_id: str,
    ) -> int:
        """Return the number of messages in *conversation_id*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    # ==================================================================
    # Checkpoint CRUD  (Phase 9 will build on these stubs)
    # ==================================================================

    def create_checkpoint(
        self,
        checkpoint_id: str,
        session_id: str,
        sequence_num: int,
        created_at: datetime,
        description: str,
        tool_name: str | None,
        git_ref: str | None,
        file_manifest_json: str | None,
        agent_state_json: str,
        message_index: int,
    ) -> None:
        """Insert a checkpoint row."""
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO checkpoints
                   (id, session_id, sequence_num, created_at, description,
                    tool_name, git_ref, file_manifest_json, agent_state_json,
                    message_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    session_id,
                    sequence_num,
                    created_at.isoformat(),
                    description,
                    tool_name,
                    git_ref,
                    file_manifest_json,
                    agent_state_json,
                    message_index,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Return a checkpoint row as a dict, or *None*."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return dict(row)

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """Return all checkpoints for *session_id*, newest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM checkpoints
                   WHERE session_id = ?
                   ORDER BY sequence_num DESC""",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete a single checkpoint.  Returns ``True`` if one was deleted."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,)
            )
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
        return deleted

    def prune_checkpoints(
        self, session_id: str, *, keep_latest: int = 50
    ) -> int:
        """Remove old checkpoints, keeping the most recent *keep_latest*.

        Returns the number of rows deleted.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """DELETE FROM checkpoints
                   WHERE session_id = ?
                   AND sequence_num NOT IN (
                       SELECT sequence_num FROM checkpoints
                       WHERE session_id = ?
                       ORDER BY sequence_num DESC
                       LIMIT ?
                   )""",
                (session_id, session_id, keep_latest),
            )
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
        return deleted

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _connect(self) -> sqlite3.Connection:
        """Open (or reuse) a SQLite connection with recommended settings."""
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Ensure the database schema is at ``CURRENT_SCHEMA_VERSION``."""
        conn.execute(_CREATE_VERSION_TABLE)

        row = conn.execute(
            "SELECT version FROM _schema_version"
        ).fetchone()

        current = row["version"] if row else 0

        if current < 1:
            logger.info("Creating initial schema (v1).")
            conn.execute(_CREATE_SESSIONS)
            conn.execute(_CREATE_MESSAGES)
            conn.execute(_CREATE_CHECKPOINTS)
            for idx_sql in _CREATE_INDEXES:
                conn.execute(idx_sql)
            conn.execute(
                "INSERT INTO _schema_version (version) VALUES (1)"
            )
            conn.commit()
            current = 1

        if current < 2:
            logger.info("Migrating to schema v2 — adding conversations.")
            conn.execute(_CREATE_CONVERSATIONS)

            # Add conversation_id column to messages (if not already present).
            _add_column_if_missing(
                conn, "messages", "conversation_id",
                "TEXT NOT NULL DEFAULT ''",
            )

            # Create the new index.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
                "ON messages(conversation_id, sequence_num);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_session "
                "ON conversations(session_id, status);"
            )

            # Backfill: for each session that has messages, create a default
            # "active" conversation and link all existing messages to it.
            _backfill_conversations(conn)

            conn.execute(
                "UPDATE _schema_version SET version = 2"
            )
            conn.commit()
            current = 2

        if current != CURRENT_SCHEMA_VERSION:
            logger.warning(
                f"Database schema is at v{current}, but code expects "
                f"v{CURRENT_SCHEMA_VERSION}.  Migrations may be missing."
            )

    # ==================================================================
    # Row → object conversion
    # ==================================================================

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            message_count=row["message_count"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            mode=row["mode"],
            metadata=_json_loads(row["metadata_json"]),
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> SessionSummary:
        return SessionSummary(
            id=row["id"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            message_count=row["message_count"],
        )

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            sequence_num=row["sequence_num"],
            status=row["status"],
            compacted_summary=row["compacted_summary"],
            compacted_at_seq=row["compacted_at_seq"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            message_count=row["message_count"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
        )

    @staticmethod
    def _row_to_conversation_summary(
        row: sqlite3.Row,
    ) -> ConversationSummary:
        return ConversationSummary(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            sequence_num=row["sequence_num"],
            status=row["status"],
            message_count=row["message_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            id=row["id"],
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            sequence_num=row["sequence_num"],
            role=row["role"],
            content_json=row["content_json"],
            token_count=row["token_count"],
            is_compacted=bool(row["is_compacted"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# ---------------------------------------------------------------------------
# Tiny JSON helpers (no stdlib json import at the top — import locally)
# ---------------------------------------------------------------------------


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_def: str,
) -> None:
    """Add *column* to *table* if it doesn't already exist."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row["name"] for row in info}
    if column not in existing:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
        )


def _backfill_conversations(conn: sqlite3.Connection) -> None:
    """Create a default conversation for each session with messages and
    backfill ``conversation_id`` on existing message rows.

    Sparse sequence_num is assigned per-session: the new conversation
    inherits the session's highest sequence_num.
    """
    now = datetime.now().isoformat()

    # Find sessions that have messages but no conversation.
    rows = conn.execute(
        """SELECT DISTINCT s.id AS sid
           FROM sessions s
           JOIN messages m ON m.session_id = s.id
           WHERE m.conversation_id = ''
           ORDER BY s.id"""
    ).fetchall()

    for row in rows:
        sid = row["sid"]
        conv_id = uuid.uuid4().hex

        # Get the highest sequence_num for this session.
        max_seq = conn.execute(
            "SELECT MAX(sequence_num) FROM messages WHERE session_id = ?",
            (sid,),
        ).fetchone()[0] or 0

        conn.execute(
            """INSERT INTO conversations
               (id, session_id, title, sequence_num, status,
                compacted_summary, compacted_at_seq, created_at,
                updated_at, message_count, total_input_tokens,
                total_output_tokens)
               VALUES (?, ?, ?, ?, 'active', NULL, NULL, ?, ?, 0, 0, 0)""",
            (conv_id, sid, None, max_seq + 1, now, now),
        )

        # Backfill messages with the new conversation_id.
        conn.execute(
            "UPDATE messages SET conversation_id = ? "
            "WHERE session_id = ? AND conversation_id = ''",
            (conv_id, sid),
        )

        # Update message_count on the conversation.
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE conversations SET message_count = ? WHERE id = ?",
            (msg_count, conv_id),
        )

    logger.info(
        f"Backfilled conversations for {len(rows)} session(s)."
    )
