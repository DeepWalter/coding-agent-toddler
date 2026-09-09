"""Provider reasoning-content tests — DeepSeek ``reasoning_content``.

Covers phase 2 of the reasoning-capture plan in
:class:`OpenAICompatibleProvider`: the ``reasoning_content`` echo-back on
the request path (``_messages_to_openai``), reasoning-block capture on the
response path (``_openai_message_to_internal`` / non-streaming), and the
usage-trailer fix in the streaming loop (``_extract_usage`` every chunk,
``message_stop`` exactly once, carrying the trailer's usage).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from toddler.config.settings import Settings
from toddler.llm import Message, MessageBlock, StreamEvent, TokenUsage
from toddler.llm.provider import OpenAICompatibleProvider

_REASONING = "Check the module docstring first.\n"
_ANSWER = "The answer is 42."
_TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path": "a.py"}',
        },
    },
]


# ============================================================================
# Stub client — canned streaming chunks / non-streaming response
# ============================================================================


class _ChunkStream:
    """Async iterable over canned OpenAI chunk objects."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _install_stub(monkeypatch, *, chunks=None, response=None):
    """Replace ``toddler.llm.provider.AsyncOpenAI`` with a stub client.

    ``chat.completions.create`` returns an async iterable of *chunks* when
    called with ``stream=True``, else *response*.
    """
    class _StubCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("stream"):
                return _ChunkStream(chunks or [])
            return response

    completions = _StubCompletions()

    class _StubClient:
        def __init__(
            self, base_url=None, api_key=None, http_client=None,
        ) -> None:
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("toddler.llm.provider.AsyncOpenAI", _StubClient)
    return completions


def _make_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
        )
    )


def _chunk(*, delta=None, finish_reason=None, usage=None,
           empty_choices=False) -> SimpleNamespace:
    """A single OpenAI SSE chunk as a ``SimpleNamespace``.

    ``empty_choices=True`` builds the usage-only trailer chunk (an empty
    ``choices`` array) that OpenAI / DeepSeek send after the finish chunk.
    """
    if empty_choices:
        return SimpleNamespace(choices=[], usage=usage)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _delta(*, content=None, reasoning=None,
           tool_calls=None) -> SimpleNamespace:
    """A chunk delta; ``reasoning_content`` is absent unless set (some
    endpoints omit it — the provider reads it defensively via ``getattr``).
    """
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    return delta


def _usage(*, prompt: int = 10, completion: int = 5,
           reasoning: int | None = None) -> SimpleNamespace:
    """A chunk/response usage object; ``completion_tokens_details`` present
    only on DeepSeek-style responses."""
    if reasoning is None:
        return SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
        )
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        completion_tokens_details=SimpleNamespace(
            reasoning_tokens=reasoning,
        ),
    )


# ============================================================================
# _messages_to_openai — the DeepSeek echo-back on the request path
# ============================================================================


class TestMessagesToOpenaiEcho:
    """Assistant messages carrying reasoning echo it back on the wire."""

    def test_reasoning_and_content_echoed_verbatim(self):
        msg = Message.assistant([
            MessageBlock.reasoning_block(_REASONING),
            MessageBlock.content_block(_ANSWER),
        ])
        out = OpenAICompatibleProvider._messages_to_openai([msg])[0]
        assert out == {
            "role": "assistant",
            "content": _ANSWER,
            "reasoning_content": _REASONING,
        }

    def test_reasoning_with_tool_use_echoed(self):
        # The tool-round assistant message shape DeepSeek requires on echo.
        msg = Message.assistant([
            MessageBlock.reasoning_block("I need the file first."),
            MessageBlock.tool_use_block(
                "call_1", "read_file", {"path": "a.py"},
            ),
        ])
        out = OpenAICompatibleProvider._messages_to_openai([msg])[0]
        assert out["reasoning_content"] == "I need the file first."
        assert out["content"] is None
        assert out["tool_calls"] == _TOOL_CALLS

    def test_omitted_without_reasoning_blocks(self):
        plain = Message.assistant([MessageBlock.content_block("hi")])
        tool_only = Message.assistant([
            MessageBlock.tool_use_block("call_1", "shell", {}),
        ])
        user = Message.user("hi")
        out = OpenAICompatibleProvider._messages_to_openai(
            [plain, tool_only, user]
        )
        assert out[0] == {"role": "assistant", "content": "hi"}
        assert "reasoning_content" not in out[0]
        assert out[1]["content"] is None
        assert "reasoning_content" not in out[1]
        assert out[2] == {"role": "user", "content": "hi"}

    def test_tool_fanout_untouched(self):
        # Tool messages fan out one dict per result; no reasoning key.
        msg = Message.tool([
            MessageBlock.tool_result_block("call_1", "ok"),
            MessageBlock.tool_result_block("call_2", "boom", is_error=True),
        ])
        out = OpenAICompatibleProvider._messages_to_openai([msg])
        assert out == [
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "tool", "tool_call_id": "call_2", "content": "boom"},
        ]


# ============================================================================
# _openai_message_to_internal — reasoning lands first on the response path
# ============================================================================


class TestOpenaiMessageToInternal:
    """Reasoning content becomes a leading ``reasoning`` block."""

    def test_reasoning_before_content(self):
        oa = SimpleNamespace(
            content=_ANSWER,
            tool_calls=None,
            reasoning_content=_REASONING,
        )
        msg = OpenAICompatibleProvider._openai_message_to_internal(oa)
        assert [b.type for b in msg.blocks] == ["reasoning", "content"]
        assert msg.blocks[0].text == _REASONING
        assert msg.blocks[1].text == _ANSWER
        assert msg.reasoning == _REASONING
        assert msg.content == _ANSWER

    def test_reasoning_content_then_tool_use(self):
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="read_file", arguments='{"path": "a.py"}',
            ),
        )
        oa = SimpleNamespace(
            content="Let me look.",
            tool_calls=[tc],
            reasoning_content="Find the file.",
        )
        msg = OpenAICompatibleProvider._openai_message_to_internal(oa)
        assert [b.type for b in msg.blocks] == [
            "reasoning", "content", "tool_use",
        ]
        assert msg.blocks[2].tool_id == "call_1"
        assert msg.blocks[2].tool_input == {"path": "a.py"}

    def test_no_reasoning_attribute(self):
        # Non-DeepSeek endpoints never send the field at all.
        oa = SimpleNamespace(content="plain", tool_calls=None)
        msg = OpenAICompatibleProvider._openai_message_to_internal(oa)
        assert [b.type for b in msg.blocks] == ["content"]
        assert msg.blocks[0].text == "plain"


# ============================================================================
# Streaming — reasoning deltas and the usage-trailer fix
# ============================================================================


class TestStreamingUsageTrailer:
    """The empty-``choices`` usage trailer must reach ``message_stop``."""

    @pytest.mark.asyncio
    async def test_trailer_usage_reaches_message_stop(self, monkeypatch):
        """Regression: the final usage-only chunk used to be skipped before
        its usage was read, dropping per-call usage (and, with thinking
        mode, ``reasoning_tokens``) from the streaming path."""
        chunks = [
            _chunk(delta=_delta(reasoning="Let me think.\n")),
            # A transition chunk can carry the tail of the reasoning and
            # the head of the answer together.
            _chunk(delta=_delta(reasoning=" more", content="Answer: 42")),
            _chunk(delta=_delta(), finish_reason="stop"),
            _chunk(empty_choices=True, usage=_usage(
                prompt=12, completion=60, reasoning=40,
            )),
        ]
        _install_stub(monkeypatch, chunks=chunks)
        provider = _make_provider()

        stream = await provider.generate(
            [Message.user("hi")], [], stream=True,
        )
        events = [evt async for evt in stream]

        assert events == [
            StreamEvent(type="message_start", data={}),
            StreamEvent(
                type="reasoning_delta", data={"text_delta": "Let me think.\n"},
            ),
            StreamEvent(
                type="reasoning_delta", data={"text_delta": " more"},
            ),
            StreamEvent(
                type="content_delta", data={"text_delta": "Answer: 42"},
            ),
            StreamEvent(
                type="message_stop",
                data={
                    "stop_reason": "end_turn",
                    "usage": TokenUsage(
                        input_tokens=12, output_tokens=60,
                        reasoning_tokens=40,
                    ),
                },
            ),
        ]

    @pytest.mark.asyncio
    async def test_finish_chunk_usage_emits_stop_once(self, monkeypatch):
        """vLLM / ollama attach usage to the finish chunk itself — the stop
        goes out there, exactly once, and the post-loop fallback is
        suppressed."""
        chunks = [
            _chunk(delta=_delta(content="Hi")),
            _chunk(
                delta=_delta(),
                finish_reason="stop",
                usage=_usage(prompt=5, completion=9, reasoning=3),
            ),
        ]
        _install_stub(monkeypatch, chunks=chunks)
        provider = _make_provider()

        stream = await provider.generate(
            [Message.user("hi")], [], stream=True,
        )
        events = [evt async for evt in stream]

        stops = [e for e in events if e.type == "message_stop"]
        assert len(stops) == 1
        assert stops[0].data["stop_reason"] == "end_turn"
        assert stops[0].data["usage"] == TokenUsage(
            input_tokens=5, output_tokens=9, reasoning_tokens=3,
        )
        # Stop is the final event; the fallback did not double-fire.
        assert [e.type for e in events] == [
            "message_start", "content_delta", "message_stop",
        ]

    @pytest.mark.asyncio
    async def test_mid_stream_usage_defers_to_trailer(self, monkeypatch):
        """A gateway stamping usage onto content chunks must not trigger
        the stop early: the finish chunk carries none here, so the stop
        waits for the trailer's authoritative usage."""
        chunks = [
            # Mid-stream chunk with gateway-stamped usage — present, but
            # not final.
            _chunk(delta=_delta(content="Hi"), usage=_usage(
                prompt=5, completion=9, reasoning=3,
            )),
            _chunk(delta=_delta(), finish_reason="stop"),
            _chunk(empty_choices=True, usage=_usage(
                prompt=5, completion=20, reasoning=10,
            )),
        ]
        _install_stub(monkeypatch, chunks=chunks)
        provider = _make_provider()

        stream = await provider.generate(
            [Message.user("hi")], [], stream=True,
        )
        events = [evt async for evt in stream]

        stops = [e for e in events if e.type == "message_stop"]
        assert len(stops) == 1
        # The trailer's authoritative counts win over the mid-stream stamp.
        assert stops[0].data["usage"] == TokenUsage(
            input_tokens=5, output_tokens=20, reasoning_tokens=10,
        )
        assert [e.type for e in events] == [
            "message_start", "content_delta", "message_stop",
        ]

    @pytest.mark.asyncio
    async def test_non_streaming_assembles_reasoning_first(self, monkeypatch):
        """The non-streaming response path stores reasoning ahead of the
        answer, and usage carries ``reasoning_tokens``."""
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="Answer: 42",
                    tool_calls=None,
                    reasoning_content="Compute it.",
                ),
                finish_reason="stop",
            )],
            usage=_usage(prompt=10, completion=20, reasoning=5),
        )
        _install_stub(monkeypatch, response=response)
        provider = _make_provider()

        result = await provider.generate(
            [Message.user("hi")], [], stream=False,
        )

        assert result.stop_reason == "end_turn"
        msg = result.messages[0]
        assert [b.type for b in msg.blocks] == ["reasoning", "content"]
        assert msg.blocks[0].text == "Compute it."
        assert msg.blocks[1].text == "Answer: 42"
        assert result.usage.reasoning_tokens == 5
