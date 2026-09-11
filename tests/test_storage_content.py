"""MessageBlock serialization — the session ``content_json`` column.

Every block kind must survive a serialize → deserialize trip unchanged;
the ``content`` and ``reasoning`` kinds share the ``text`` payload key; and
rows written before the kind rename (``"type": "text"``) still load as the
``content`` kind — the permanent read-time alias of
docs/plans/reasoning-content-capture.md (design decision 4).
"""

from __future__ import annotations

import json

import pytest

from toddler.llm import Message, MessageBlock
from toddler.session.models import StoredMessage
from toddler.session.storage import (
    StorageManager,
    _deserialize_content,
    _serialize_content,
)


def _all_kinds() -> list[MessageBlock]:
    """One block per kind, including both error states of ``tool_result``."""
    return [
        MessageBlock.reasoning_block("step 1: think\nstep 2: verify"),
        MessageBlock.content_block("the answer"),
        MessageBlock.tool_use_block(
            "call_1", "read_file", {"file_path": "a.py"},
        ),
        MessageBlock.tool_result_block("call_1", "file body"),
        MessageBlock.tool_result_block("call_2", "boom", is_error=True),
    ]


# ============================================================================
# Pure serializer round-trips
# ============================================================================


class TestBlockRoundTrip:
    def test_every_kind_round_trips_deep_equal(self):
        blocks = _all_kinds()
        assert _deserialize_content(_serialize_content(blocks)) == blocks

    def test_prose_kinds_share_the_text_payload_key(self):
        raw = json.loads(_serialize_content([
            MessageBlock.reasoning_block("cot"),
            MessageBlock.content_block("answer"),
        ]))
        assert raw == [
            {"type": "reasoning", "text": "cot"},
            {"type": "content", "text": "answer"},
        ]

    def test_empty_block_list_round_trips(self):
        assert _deserialize_content(_serialize_content([])) == []


# ============================================================================
# Legacy kind alias — rows written before the rename baseline
# ============================================================================


class TestLegacyKindAlias:
    def test_legacy_text_row_reads_back_as_content(self):
        blocks = _deserialize_content('[{"type": "text", "text": "old row"}]')
        assert blocks == [MessageBlock(type="content", text="old row")]

    def test_alias_is_type_only(self):
        # The payload key never renamed, so the payload needs no alias.
        blocks = _deserialize_content('[{"type": "text", "text": "old row"}]')
        assert blocks[0].text == "old row"

    def test_reasoning_rows_are_not_aliased(self):
        blocks = _deserialize_content('[{"type": "reasoning", "text": "cot"}]')
        assert blocks[0].type == "reasoning"
        assert blocks[0].text == "cot"


# ============================================================================
# Through the database — the same trip via the real column
# ============================================================================


class TestStoredMessageRoundTrip:
    @pytest.fixture
    def storage(self, tmp_path):
        from toddler.session.database import SQLiteDatabase

        db = SQLiteDatabase(tmp_path / "storage.db")
        db.open()
        return StorageManager(db)

    @staticmethod
    def _append(storage: StorageManager, stored: StoredMessage) -> str:
        """Persist *stored* into a fresh session; return the session id."""
        session = storage.create()
        conv = storage.get_or_create_active_conversation(session.id)
        stored.session_id = session.id
        stored.conversation_id = conv.id
        stored.sequence_num = 1
        storage._db.append_message(stored)
        return session.id

    def test_reasoning_survives_the_database(self, storage):
        session = storage.create()
        conv = storage.get_or_create_active_conversation(session.id)
        message = Message.assistant([
            MessageBlock.reasoning_block("cot"),
            MessageBlock.content_block("the answer"),
            MessageBlock.tool_use_block(
                "call_1", "read_file", {"file_path": "a.py"},
            ),
        ])
        storage.append_message(session.id, message, conversation_id=conv.id)

        loaded = storage.get_messages(session.id)[0]
        assert loaded.blocks == message.blocks
        assert loaded.reasoning == "cot"
        assert loaded.content == "the answer"

    def test_legacy_row_still_loads_from_the_database(self, storage):
        """A session written before the rename loads with its prose intact
        — the alias is what makes old sessions replayable."""
        session_id = self._append(storage, StoredMessage(
            role="assistant",
            content_json='[{"type": "text", "text": "legacy answer"}]',
        ))

        loaded = storage.get_messages(session_id)[0]
        assert [b.type for b in loaded.blocks] == ["content"]
        assert loaded.content == "legacy answer"
