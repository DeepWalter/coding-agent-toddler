"""Reasoning capture — handler assembly, cancel snapshots, token accounting.

Covers the reasoning half of ``toddler/agent/handler.py`` and the token
accounting around it: the streaming buffers and their reasoning-first
block order, the cancel snapshot (``get_partial_content``) that makes the
next request echo a partial thought back, the non-streaming sibling, and
the context-window charge reasoning carries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from toddler.agent.events import (
    ContentDelta,
    ReasoningDelta,
    ToolCallDelta,
    ToolCallStart,
)
from toddler.agent.handler import NonStreamHandler, StreamHandler
from toddler.context.token_counter import TokenCounter
from toddler.llm import (
    LLMResponse,
    Message,
    MessageBlock,
    StreamEvent,
    TokenUsage,
)

_REASONING = "Check the module docstring first.\n"
_ANSWER = "The answer is 42."


async def _stream(*events: StreamEvent) -> AsyncIterator[StreamEvent]:
    """Yield canned stream events."""
    for event in events:
        yield event


def _reasoning_delta(text: str) -> StreamEvent:
    """A reasoning fragment — the shared ``text_delta`` payload key."""
    return StreamEvent(type="reasoning_delta", data={"text_delta": text})


def _content_delta(text: str) -> StreamEvent:
    return StreamEvent(type="content_delta", data={"text_delta": text})


# ============================================================================
# StreamHandler — accumulation, order, cancel snapshot
# ============================================================================


class TestStreamHandlerReasoning:
    async def test_reasoning_chunks_yield_reasoning_deltas(self):
        handler = StreamHandler()
        events = [e async for e in handler.process(_stream(
            _reasoning_delta("Step 1. "),
            _reasoning_delta("Step 2."),
        ))]
        assert [type(e) for e in events] == [ReasoningDelta, ReasoningDelta]
        assert [e.text_delta for e in events] == ["Step 1. ", "Step 2."]

    async def test_full_stream_assembles_reasoning_first(self):
        """The assembled order is ``[reasoning, content, tool_use]`` —
        verbatim, matching the model's emission order."""
        handler = StreamHandler()
        events = [e async for e in handler.process(_stream(
            _reasoning_delta(_REASONING),
            _reasoning_delta("Look for the docstring."),
            _content_delta(_ANSWER),
            StreamEvent(type="tool_use_start", data={
                "tool_id": "t1", "tool_name": "read_file",
            }),
            StreamEvent(type="tool_use_delta", data={
                "tool_id": "t1",
                "input_delta": {"arguments_fragment": '{"file_path": "a.py"}'},
            }),
            StreamEvent(type="message_stop", data={
                "stop_reason": "tool_use",
                "usage": TokenUsage(
                    input_tokens=12, output_tokens=60, reasoning_tokens=40,
                ),
            }),
        ))]

        assert [type(e) for e in events] == [
            ReasoningDelta,
            ReasoningDelta,
            ContentDelta,
            ToolCallStart,
            ToolCallDelta,
        ]
        result = handler.get_final_result()
        msg = result["assistant_msg"]
        assert [b.type for b in msg.blocks] == [
            "reasoning", "content", "tool_use",
        ]
        assert msg.blocks[0].text == _REASONING + "Look for the docstring."
        assert msg.blocks[1].text == _ANSWER
        assert msg.blocks[2].tool_input == {"file_path": "a.py"}
        assert msg.reasoning == _REASONING + "Look for the docstring."
        assert msg.content == _ANSWER
        assert result["usage"].reasoning_tokens == 40

    async def test_partial_content_carries_partial_reasoning(self):
        """Cancel path: the snapshot keeps the reasoning so far, so the
        cancelled turn persists it and the next request echoes it back."""
        handler = StreamHandler()
        gen = handler.process(_stream(
            _reasoning_delta("Step 1. "),
            _reasoning_delta("Step 2."),
            _content_delta("Half an ans"),
        ))
        assert isinstance(await gen.__anext__(), ReasoningDelta)
        assert isinstance(await gen.__anext__(), ReasoningDelta)
        assert isinstance(await gen.__anext__(), ContentDelta)

        partial = handler.get_partial_content()
        assert [b.type for b in partial] == ["reasoning", "content"]
        assert partial[0].text == "Step 1. Step 2."
        assert partial[1].text == "Half an ans"

    async def test_clear_resets_the_reasoning_buffer(self):
        handler = StreamHandler()
        gen = handler.process(_stream(_reasoning_delta("cot")))
        assert isinstance(await gen.__anext__(), ReasoningDelta)

        handler.clear()
        assert handler.get_partial_content() == []
        assert handler.get_final_result()["assistant_msg"].blocks == []

    async def test_reasoning_only_stream_has_no_content_block(self):
        """A thought-only reply (reasoning then a tool call) must not
        manufacture an empty content block."""
        handler = StreamHandler()
        await handler.process(_stream(
            _reasoning_delta("I need the file first."),
        )).__anext__()

        assert [b.type for b in handler.get_partial_content()] == ["reasoning"]


# ============================================================================
# NonStreamHandler — one joined delta per message, before the answer
# ============================================================================


class TestNonStreamHandlerReasoning:
    async def test_reasoning_yielded_before_content(self):
        resp = LLMResponse(
            messages=[Message.assistant([
                MessageBlock.reasoning_block(_REASONING),
                MessageBlock.content_block(_ANSWER),
            ])],
            stop_reason="end_turn",
            usage=TokenUsage(
                input_tokens=10, output_tokens=60, reasoning_tokens=40,
            ),
        )
        handler = NonStreamHandler()
        events = [e async for e in handler.process(resp)]

        assert [type(e) for e in events] == [ReasoningDelta, ContentDelta]
        assert events[0].text_delta == _REASONING
        assert events[1].text_delta == _ANSWER
        assert [b.type for b in handler.get_partial_content()] == [
            "reasoning", "content",
        ]


# ============================================================================
# Token accounting
# ============================================================================


class TestReasoningTokenAccounting:
    """Reasoning rides along on every request of its round, so it is
    charged to the context window like answer text."""

    def test_reasoning_block_counts_like_content(self):
        counter = TokenCounter(model="deepseek-v4-pro")
        prose = "weigh the options carefully"
        reasoning_only = Message.assistant([
            MessageBlock.reasoning_block(prose),
        ])
        content_only = Message.assistant([
            MessageBlock.content_block(prose),
        ])
        assert counter.count_messages([reasoning_only]) == (
            counter.count_messages([content_only])
        )
        # More than the per-message framing overhead alone.
        assert counter.count_messages([reasoning_only]) > 4

    def test_both_kinds_charged_in_one_message(self):
        counter = TokenCounter(model="deepseek-v4-pro")
        prose = "weigh the options carefully"
        both = Message.assistant([
            MessageBlock.reasoning_block(prose),
            MessageBlock.content_block(prose),
        ])
        content_only = Message.assistant([
            MessageBlock.content_block(prose),
        ])
        delta = (
            counter.count_messages([both])
            - counter.count_messages([content_only])
        )
        assert delta == counter.count_tokens(prose)

    def test_unknown_kind_still_counts_zero(self):
        counter = TokenCounter(model="deepseek-v4-pro")
        assert counter._count_block(MessageBlock(type="tool_use")) == 0


class TestTokenUsageAddition:
    def test_add_preserves_reasoning_tokens(self):
        a = TokenUsage(
            input_tokens=1, output_tokens=2, reasoning_tokens=30,
        )
        b = TokenUsage(
            input_tokens=3, output_tokens=4, reasoning_tokens=12,
        )
        total = a + b
        assert total.input_tokens == 4
        assert total.output_tokens == 6
        assert total.reasoning_tokens == 42
        # Reasoning stays inside output_tokens — never double-counted.
        assert total.total == 10
