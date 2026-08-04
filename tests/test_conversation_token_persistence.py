"""Tests for conversation token-count persistence.

Verifies that ``Conversation.total_tokens`` / ``Conversation.model`` replace
the dead ``total_input_tokens`` / ``total_output_tokens`` fields, that the
store round-trips them correctly, the v2→v3 migration works, and that
``ContextManager`` exposes ``count_tokens`` / ``set_token_baseline``.
"""

from __future__ import annotations

import sqlite3

import pytest

from toddler.context.manager import ContextManager
from toddler.context.window import ContextWindowManager
from toddler.llm import ContentBlock, Message, TokenUsage
from toddler.llm.base import BaseLLMProvider
from toddler.session.models import Conversation

# ---------------------------------------------------------------------------
# Helpers (shared with test_token_count_calibration.py)
# ---------------------------------------------------------------------------


def _make_msg(role: str, text: str) -> Message:
    if role == "system":
        return Message.system(text)
    if role == "user":
        return Message.user(text)
    if role == "assistant":
        return Message.assistant([ContentBlock.text_block(text)])
    if role == "tool":
        return Message.tool([ContentBlock.tool_result_block("id1", text)])
    raise ValueError(f"Unknown role: {role}")


def _token_estimate(messages: list[Message]) -> int:
    from toddler.context.token_counter import TokenCounter

    return TokenCounter(model="gpt-4").count_messages(messages)


# ============================================================================
# ContextManager — count_tokens / set_token_baseline
# ============================================================================


class TestContextManagerTokenCount:
    """Unit tests for the new public token-count API on ContextManager."""

    @pytest.fixture
    def ctx(self) -> ContextManager:
        from toddler.config.settings import Settings

        class _MockProvider(BaseLLMProvider):
            @property
            def model(self) -> str:
                return "gpt-4"

            async def generate(self, messages, tools, *, max_tokens=4096,
                               temperature=0.0, stream=True):
                raise NotImplementedError

            async def generate_compact(self, prompt: str) -> str:
                raise NotImplementedError

        return ContextManager(Settings(), _MockProvider())

    def test_count_tokens_matches_full_estimate(self, ctx):
        """count_tokens() returns the window manager's estimate for the buffer."""
        msgs = [
            _make_msg("system", "You are helpful."),
            _make_msg("user", "Hello."),
        ]
        ctx.load(msgs)
        result = ctx.count_tokens()
        expected = _token_estimate(msgs)
        assert result == expected

    def test_count_tokens_respects_baseline(self, ctx):
        """Baseline set via set_token_baseline is used; only delta estimated."""
        msgs = [
            _make_msg("system", "System."),
            _make_msg("user", "Q1."),
        ]
        ctx.load(msgs)
        ctx.set_token_baseline(total_tokens=500, message_count=2)

        # No new messages → exact baseline.
        assert ctx.count_tokens() == 500

        # New message → baseline + delta.
        ctx._messages.append(_make_msg("user", "Q2."))
        delta = _token_estimate([_make_msg("user", "Q2.")])
        assert ctx.count_tokens() == 500 + delta

    def test_set_token_baseline_then_record_usage_overwrites(self, ctx):
        """record_usage() after set_token_baseline replaces the stored baseline."""
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q."),
            _make_msg("assistant", "A."),
        ]
        ctx.load(msgs)

        # Seed from storage.
        ctx.set_token_baseline(total_tokens=300, message_count=3)
        assert ctx.count_tokens() == 300

        # API returns a different count → overwrites.
        ctx.record_usage(TokenUsage(input_tokens=200, output_tokens=50))
        assert ctx.count_tokens() == 250

    def test_load_resets_even_after_set_token_baseline(self, ctx):
        """load() should still reset the baseline regardless of how it was set."""
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q."),
        ]
        ctx.load(msgs)
        ctx.set_token_baseline(total_tokens=500, message_count=2)

        # Load fresh — baseline resets (defensive).
        ctx.load([])
        assert ctx._window_mgr._baseline_total_tokens == 0
        assert ctx.count_tokens() == _token_estimate([])


# ============================================================================
# Store — round-trip new fields
# ============================================================================


class TestConversationStoreRoundTrip:
    """Verify total_tokens / model survive insert → read in SQLite."""

    @pytest.fixture
    def store(self, tmp_path):
        from toddler.session.store import SQLiteStore

        db = SQLiteStore(tmp_path / "test_store.db")
        db.open()
        return db

    @pytest.fixture
    def session_id(self, store) -> str:
        import uuid
        from toddler.session.models import Session

        sid = str(uuid.uuid4())
        store.create_session(Session(id=sid))
        return sid

    def test_create_and_read_back(self, store, session_id):
        """New fields survive create → get_conversation round trip."""
        import uuid

        conv = Conversation(
            id=str(uuid.uuid4()),
            session_id=session_id,
            title="Test Conv",
            sequence_num=1,
            message_count=5,
            total_tokens=1234,
            model="gpt-4",
        )
        store.create_conversation(conv)
        loaded = store.get_conversation(conv.id)
        assert loaded is not None
        assert loaded.total_tokens == 1234
        assert loaded.model == "gpt-4"

    def test_update_and_read_back(self, store, session_id):
        """update_conversation persists changes to total_tokens / model."""
        import uuid

        conv = Conversation(
            id=str(uuid.uuid4()),
            session_id=session_id,
            title="Test",
            sequence_num=2,
        )
        store.create_conversation(conv)

        # Update.
        conv.total_tokens = 9999
        conv.model = "deepseek-v4"
        store.update_conversation(conv)

        loaded = store.get_conversation(conv.id)
        assert loaded is not None
        assert loaded.total_tokens == 9999
        assert loaded.model == "deepseek-v4"

    def test_defaults_on_new_conversation(self, store, session_id):
        """Fields default to 0 / None for a fresh Conversation."""
        import uuid

        conv = Conversation(
            id=str(uuid.uuid4()),
            session_id=session_id,
            title="Defaults",
            sequence_num=3,
        )
        store.create_conversation(conv)
        loaded = store.get_conversation(conv.id)
        assert loaded is not None
        assert loaded.total_tokens == 0
        assert loaded.model is None

    def test_old_fields_not_present(self, store, session_id):
        """total_input_tokens/total_output_tokens should not exist on the model."""
        conv = Conversation(
            id="dummy", session_id=session_id, sequence_num=4,
        )
        assert not hasattr(conv, "total_input_tokens")
        assert not hasattr(conv, "total_output_tokens")


# ============================================================================
# Migration — v2 → v3
# ============================================================================


class TestV2ToV3Migration:
    """Schema v2 databases get total_tokens / model columns on open()."""

    @pytest.fixture
    def v2_db_path(self, tmp_path):
        """Hand-build a v2 database (no v3 columns)."""
        db_path = tmp_path / "v2_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # Schema version table.
        conn.execute(
            "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY)"
        )
        conn.execute("INSERT INTO _schema_version (version) VALUES (2)")

        # v2 conversations table — WITHOUT total_tokens / model.
        conn.execute(
            """CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
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
            )"""
        )

        conn.commit()
        conn.close()
        return db_path

    def test_migration_adds_columns(self, v2_db_path):
        """Opening a v2 DB runs the v3 migration and adds new columns."""
        from toddler.session.store import SQLiteStore

        store = SQLiteStore(v2_db_path)
        store.open()

        conn = sqlite3.connect(str(v2_db_path))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(conversations)")
        }
        conn.close()

        assert "total_tokens" in cols
        assert "model" in cols
        # Old columns stay (not dropped — SQLite table-recreate would be
        # overkill for harmless dead columns).
        assert "total_input_tokens" in cols
        assert "total_output_tokens" in cols

    def test_migration_sets_schema_version_3(self, v2_db_path):
        """After migration, _schema_version is 3."""
        from toddler.session.store import SQLiteStore

        store = SQLiteStore(v2_db_path)
        store.open()

        conn = sqlite3.connect(str(v2_db_path))
        version = conn.execute(
            "SELECT version FROM _schema_version"
        ).fetchone()[0]
        conn.close()
        assert version == 3
