"""Conversation-title plumbing — provider output to the session layer.

The title is response metadata: a provider surfaces it on
``LLMResponse.title`` or as a ``conversation_title`` stream event, a
handler normalises both into :class:`ConversationTitle`, and
:class:`SessionManager` decides whether to keep it.
"""

from __future__ import annotations

import pytest

from tests.mocks import MockLLMProvider, text_response, tool_use_response
from toddler.agent.events import ContentDelta, ConversationTitle
from toddler.agent.handler import NonStreamHandler, StreamHandler
from toddler.config.settings import Settings
from toddler.llm import LLMResponse, Message, StreamEvent, TokenUsage
from toddler.session.database import SQLiteDatabase
from toddler.session.manager import SessionManager
from toddler.session.models import MAX_TITLE_LENGTH
from toddler.session.storage import StorageManager


async def _aiter(events):
    for event in events:
        yield event


async def _events(handler, response):
    return [event async for event in handler.process(response)]


def _stop() -> StreamEvent:
    return StreamEvent(
        type="message_stop",
        data={"stop_reason": "end_turn", "usage": TokenUsage()},
    )


def _response(*, title: str | None = None) -> LLMResponse:
    return LLMResponse(
        messages=[Message.assistant()],
        stop_reason="end_turn",
        usage=TokenUsage(),
        title=title,
    )


# ============================================================================
# Handlers — both wire shapes normalise to one agent event
# ============================================================================


class TestHandlers:

    async def test_stream_title_becomes_an_agent_event(self):
        handler = StreamHandler()
        stream = _aiter([
            StreamEvent(type="content_delta", data={"text_delta": "Hi."}),
            StreamEvent(
                type="conversation_title",
                data={"title": "A generated title"},
            ),
            _stop(),
        ])

        events = await _events(handler, stream)

        assert [
            event for event in events
            if isinstance(event, ConversationTitle)
        ] == [ConversationTitle(title="A generated title")]

    async def test_empty_stream_title_is_not_surfaced(self):
        handler = StreamHandler()
        stream = _aiter([
            StreamEvent(type="conversation_title", data={"title": ""}),
            _stop(),
        ])

        events = await _events(handler, stream)

        assert not [
            event for event in events
            if isinstance(event, ConversationTitle)
        ]

    async def test_non_stream_title_becomes_an_agent_event(self):
        handler = NonStreamHandler()

        events = await _events(
            handler, _response(title="A generated title"),
        )

        assert events[-1] == ConversationTitle(title="A generated title")

    async def test_non_stream_without_a_title_surfaces_nothing(self):
        handler = NonStreamHandler()

        events = await _events(handler, _response())

        assert not [
            event for event in events
            if isinstance(event, ConversationTitle)
        ]


# ============================================================================
# SessionManager — the seed is the fallback, the generated title wins
# ============================================================================


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(session_dir=tmp_path, streaming_enabled=False)


@pytest.fixture
def storage_mgr(tmp_path) -> StorageManager:
    db = SQLiteDatabase(tmp_path / "titles.db")
    db.open()
    return StorageManager(db)


async def _make_mgr(settings, storage_mgr, llm, repo_root) -> SessionManager:
    mgr = SessionManager(settings, storage_mgr, llm, repo_root=repo_root)
    await mgr.resolve()
    return mgr


async def _turn(mgr: SessionManager, text: str) -> list:
    return [event async for event in mgr.process_turn(text)]


class TestManager:

    async def test_first_turn_adopts_and_persists_the_generated_title(
        self, settings, storage_mgr, tmp_path,
    ):
        llm = MockLLMProvider([
            text_response("Hello.", title="A proper title"),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "explain the session manager")

        assert llm.title_requests == [True]
        assert mgr.conversation.title == "A proper title"
        stored = storage_mgr.get_conversation(mgr.conversation.id)
        assert stored.title == "A proper title"

    async def test_a_missing_title_keeps_the_seed(
        self, settings, storage_mgr, tmp_path,
    ):
        llm = MockLLMProvider([text_response("Hello.")])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "explain the session manager")

        assert mgr.conversation.title == "explain the session manager"

    async def test_an_empty_title_keeps_the_seed(
        self, settings, storage_mgr, tmp_path,
    ):
        llm = MockLLMProvider([
            text_response("Hello.", title="   "),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "explain the session manager")

        assert mgr.conversation.title == "explain the session manager"

    async def test_the_generated_title_is_clamped(
        self, settings, storage_mgr, tmp_path,
    ):
        llm = MockLLMProvider([
            text_response("Hello.", title="t" * (MAX_TITLE_LENGTH * 2)),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "explain the session manager")

        assert mgr.conversation.title == "t" * MAX_TITLE_LENGTH

    async def test_only_the_first_turn_asks_for_a_title(
        self, settings, storage_mgr, tmp_path,
    ):
        llm = MockLLMProvider([
            text_response("one", title="First title"),
            text_response("two", title="Second title"),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "first request")
        await _turn(mgr, "second request")

        assert llm.title_requests == [True, False]
        assert mgr.conversation.title == "First title"

    async def test_a_tool_use_turn_asks_once(
        self, settings, storage_mgr, tmp_path,
    ):
        """Only the turn's first request carries the title — the tool
        round-trip that follows must not ask again."""
        llm = MockLLMProvider([
            tool_use_response("no_such_tool", {}, tool_id="call_x"),
            text_response("done", title="Tools and all"),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "look something up")

        assert llm.title_requests == [True, False]
        assert mgr.conversation.title == "Tools and all"

    async def test_a_rename_mid_turn_wins(
        self, settings, storage_mgr, tmp_path,
    ):
        """A user rename lands before the title does — keep the user's."""
        settings.streaming_enabled = True
        llm = MockLLMProvider([
            text_response("Hello.", title="Generated title"),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        async for event in mgr.process_turn("hi"):
            if isinstance(event, ContentDelta):
                mgr.rename_conversation("Mid-turn name")

        assert mgr.conversation.title == "Mid-turn name"

    async def test_a_volunteered_title_is_ignored_after_the_first_turn(
        self, settings, storage_mgr, tmp_path,
    ):
        """The manager only adopts a title it asked for.  A later response
        carrying one anyway (a gateway quirk) must not clobber the title."""
        llm = MockLLMProvider([
            text_response("one"),
            text_response("two", title="Volunteered"),
        ])
        mgr = await _make_mgr(settings, storage_mgr, llm, tmp_path)

        await _turn(mgr, "first request")
        await _turn(mgr, "second request")

        assert llm.title_requests == [True, False]
        assert mgr.conversation.title == "first request"
