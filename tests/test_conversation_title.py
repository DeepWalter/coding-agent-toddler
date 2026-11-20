"""Conversation-title plumbing — provider output to the session layer.

The title is response metadata: a provider surfaces it on
``LLMResponse.title`` or as a ``conversation_title`` stream event, a
handler normalises both into :class:`ConversationTitle`, and
:class:`SessionManager` decides whether to keep it.
"""

from __future__ import annotations

from toddler.agent.events import ConversationTitle
from toddler.agent.handler import NonStreamHandler, StreamHandler
from toddler.llm import LLMResponse, Message, StreamEvent, TokenUsage


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
