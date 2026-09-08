"""Tests for manual context compaction — the ``/compact`` slash command.

Covers the shared routine ``ContextManager.compact`` (called ungated by
``SessionManager.compact_context`` and, behind the usage threshold, by
``_auto_compact``), the persistence wrapper, and the ``/compact`` command
handler wiring.
"""

from __future__ import annotations

import pytest

from toddler.cli.commands import SlashCommandDispatcher
from toddler.context.manager import ContextManager
from toddler.llm import Message, MessageBlock
from toddler.llm.base import BaseLLMProvider
from toddler.session.manager import SessionManager

# Compactor keeps this many most-recent body messages intact.
_KEEP_RECENT = 12

_SUMMARY = "Test summary of the earlier work."


class _StubLLM(BaseLLMProvider):
    """Minimal provider: compaction summarisation only (no generation)."""

    def __init__(self, *, summary: str = _SUMMARY) -> None:
        self._summary = summary
        self.fail_compact = False

    @property
    def model(self) -> str:
        return "test-model"

    async def generate(
        self, messages, tools, *, max_tokens=4096, temperature=0.0, stream=True,
    ):
        raise NotImplementedError

    async def generate_compact(self, prompt: str) -> str:
        if self.fail_compact:
            raise RuntimeError("summarisation failed")
        return self._summary


def _conversation(body_pairs: int) -> list[Message]:
    """Build a system message plus *body_pairs* user/assistant turns."""
    msgs = [Message.system("You are a helpful assistant.")]
    for i in range(body_pairs):
        msgs.append(Message.user(f"Question {i}"))
        msgs.append(Message.assistant([MessageBlock.content_block(f"Reply {i}")]))
    return msgs


def _has_summary_marker(messages: list[Message]) -> bool:
    return any(
        (m.content or "").startswith("[Compacted history") for m in messages
    )


# ============================================================================
# ContextManager.compact
# ============================================================================


class TestCompact:
    """Unit tests for the shared compaction routine."""

    @pytest.fixture
    def llm(self) -> _StubLLM:
        return _StubLLM()

    @pytest.fixture
    def ctx(self, llm: _StubLLM) -> ContextManager:
        from toddler.config.settings import Settings

        return ContextManager(Settings(), llm)

    @pytest.mark.asyncio
    async def test_compacts_long_conversation(self, ctx, llm):
        """A conversation longer than keep_recent is summarised in place."""
        original = _conversation(16)  # 1 system + 32 body messages
        ctx.load(original)

        result = await ctx.compact()

        assert result is not None
        assert result.messages_before == len(original)
        assert result.messages_after == 1 + 1 + _KEEP_RECENT
        assert result.token_count_after < result.token_count_before
        assert _SUMMARY in result.summary

        # Buffer: rebuilt compact system + summary marker + last 12 intact.
        assert len(ctx.messages) == 1 + 1 + _KEEP_RECENT
        assert ctx.messages[0].role == "system"
        assert ctx.messages[1].role == "user"
        assert ctx.messages[1].content.startswith("[Compacted history")
        tail = [m.content for m in ctx.messages[2:]]
        assert tail == [m.content for m in original[-_KEEP_RECENT:]]

        # Compaction metadata recorded; baseline reset (nothing new to save).
        assert ctx.last_compaction is result
        assert ctx.has_compacted is True
        assert ctx.new_message_count == 0

    @pytest.mark.asyncio
    async def test_auto_path_gated_but_compact_is_ungated(self, ctx):
        """Below the token threshold auto-compaction is a no-op while
        a direct ``compact`` still summarises (the point of ``/compact``)."""
        ctx.load(_conversation(16))  # plenty of messages, few tokens

        # Auto path (as called before each LLM request): below threshold.
        messages = await ctx.get_messages()
        assert not _has_summary_marker(messages)
        assert ctx.last_compaction is None
        assert ctx.has_compacted is False

        # Manual path: same buffer compacts regardless of usage ratio.
        result = await ctx.compact()
        assert result is not None
        assert _has_summary_marker(ctx.messages)

    @pytest.mark.asyncio
    async def test_short_conversation_is_a_no_op(self, ctx):
        """A conversation short enough to fit keep_recent is left alone."""
        original = _conversation(6)  # 1 system + 12 body → nothing to fold
        ctx.load(original)

        result = await ctx.compact()

        assert result is None
        assert ctx.messages == original
        assert not _has_summary_marker(ctx.messages)
        assert ctx.last_compaction is None
        assert ctx.has_compacted is False

    @pytest.mark.asyncio
    async def test_llm_failure_is_a_no_op(self, ctx, llm):
        """A failed summarisation call leaves the buffer untouched."""
        original = _conversation(16)
        ctx.load(original)
        llm.fail_compact = True

        result = await ctx.compact()

        assert result is None
        assert ctx.messages == original
        assert not _has_summary_marker(ctx.messages)
        assert ctx.last_compaction is None


# ============================================================================
# SessionManager.compact_context + /compact dispatch
# ============================================================================


class TestCompactContextPersistence:
    """End-to-end: manual compaction persists and survives a reload."""

    @pytest.fixture
    def settings(self):
        from toddler.config.settings import Settings

        return Settings(streaming_enabled=False)

    @pytest.fixture
    def storage_mgr(self, tmp_path):
        from toddler.session.database import SQLiteDatabase
        from toddler.session.storage import StorageManager

        db = SQLiteDatabase(tmp_path / "compact.db")
        db.open()
        return StorageManager(db)

    @pytest.fixture
    def llm(self) -> _StubLLM:
        return _StubLLM()

    @pytest.fixture
    async def mgr(self, settings, storage_mgr, llm) -> SessionManager:
        mgr = SessionManager(
            settings=settings,
            storage_manager=storage_mgr,
            llm=llm,
        )
        await mgr.resolve()
        return mgr

    @staticmethod
    async def _run_turns(mgr: SessionManager, turns: int) -> None:
        """Simulate complete turns: user prompt, assistant reply, save."""
        ctx = mgr.context
        for t in range(turns):
            await ctx.prepare_turn(f"User question {t}")
            ctx.append(Message.assistant(
                [MessageBlock.content_block(f"Assistant reply {t}")]
            ))
            await mgr.save()

    @pytest.mark.asyncio
    async def test_compact_context_persists_and_reloads(self, mgr):
        await self._run_turns(mgr, 7)  # 1 system + 14 body messages
        assert len(mgr.context.messages) == 15
        assert mgr.conversation.message_count == 15

        result = await mgr.compact_context()

        assert result is not None
        assert result.messages_before == 15
        assert result.messages_after == 14
        assert len(mgr.context.messages) == 14

        # Persisted immediately on the conversation row; result consumed.
        conv = mgr.conversation
        assert conv.compacted_summary.startswith("[Compacted history")
        assert _SUMMARY in conv.compacted_summary
        assert conv.compacted_at_seq is not None
        assert mgr.context.last_compaction is None

        # A fresh manager on the same session replays the summarised
        # transcript — the synthetic summary leads the reloaded context.
        from toddler.config.settings import Settings
        from toddler.session.manager import SessionManager

        mgr2 = SessionManager(
            settings=Settings(streaming_enabled=False),
            storage_manager=mgr.storage_manager,
            llm=_StubLLM(),
        )
        await mgr2.resolve(session_id=mgr.session.id)
        reloaded = mgr2.context.messages
        assert reloaded and reloaded[0].role == "user"
        assert reloaded[0].content.startswith("[Compacted history")
        assert mgr2.conversation.compacted_summary == conv.compacted_summary

    @pytest.mark.asyncio
    async def test_short_conversation_returns_none(self, mgr):
        await self._run_turns(mgr, 2)  # 1 system + 4 body messages

        result = await mgr.compact_context()

        assert result is None
        assert mgr.conversation.compacted_summary is None
        assert mgr.context.last_compaction is None

    @pytest.mark.asyncio
    async def test_dispatch_compact_success(self, mgr):
        await self._run_turns(mgr, 7)
        dispatcher = SlashCommandDispatcher(session_mgr=mgr)

        result = await dispatcher.dispatch("/compact")

        assert result.continue_repl is True
        assert result.changed is True
        assert "Compacted context: 15 → 14 messages" in result.message

    @pytest.mark.asyncio
    async def test_dispatch_compact_noop(self, mgr):
        await self._run_turns(mgr, 2)
        dispatcher = SlashCommandDispatcher(session_mgr=mgr)

        result = await dispatcher.dispatch("/compact")

        assert result.continue_repl is True
        assert result.changed is False
        assert "Nothing to compact" in result.message
