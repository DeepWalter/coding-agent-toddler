"""Shared test mocks — MockLLMProvider and LLMResponse factories.

Ports the pattern from ``tests/test_agent_loop.py`` into one place so web
tests can drive real :class:`SessionManager` turns (and the WS protocol)
with no network.  Unlike the agent-loop mock, :meth:`MockLLMProvider.generate`
honours ``stream``: it replays a canned :class:`LLMResponse` as a
:class:`StreamEvent` iterator, so both the streaming agent-loop path and
the non-streaming plan-generation call are exercised.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from toddler.agent.planner import Plan
from toddler.llm import (
    LLMResponse,
    Message,
    MessageBlock,
    StreamEvent,
    TokenUsage,
)
from toddler.llm.base import BaseLLMProvider

__all__ = [
    "MockLLMProvider",
    "SlowStreamLLM",
    "make_mock_llm",
    "pause_on_write",
    "plan_proposal_response",
    "reasoning_response",
    "text_response",
    "tool_use_response",
]

# Reasoning fragments per chunk in MockLLMProvider._stream — small enough
# that a short canned reasoning string still arrives as several deltas.
_REASONING_CHUNK_CHARS = 8


class MockLLMProvider(BaseLLMProvider):
    """A controllable LLM provider for driving full agent turns.

    Pre-load with a list of :class:`LLMResponse` objects — each call to
    :meth:`generate` consumes the next one; when the list is exhausted a
    default ``"Done."`` text response is returned.
    """

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses: list[LLMResponse] = list(responses or [])
        self.call_count: int = 0
        self.messages_history: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent] | LLMResponse:
        self.messages_history.append(messages)
        resp = self._next_response()
        if stream:
            return self._stream(resp)
        return resp

    def _next_response(self) -> LLMResponse:
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        self.call_count += 1
        return text_response("Done.")

    async def _stream(self, resp: LLMResponse) -> AsyncIterator[StreamEvent]:
        """Replay a canned response as a :class:`StreamEvent` stream."""
        blocks = resp.messages[0].blocks if resp.messages else []
        for block in blocks:
            if block.type == "content":
                yield StreamEvent(
                    type="content_delta", data={"text_delta": block.text},
                )
            elif block.type == "reasoning":
                # Split into several fragments, as a real provider streams
                # thinking: the accumulation path (handler buffers →
                # persisted block → replay) is exercised end to end.  Same
                # ``text_delta`` payload key as content — both kinds feed
                # their block's shared ``text`` slot.
                text = block.text or ""
                for i in range(0, len(text), _REASONING_CHUNK_CHARS):
                    yield StreamEvent(
                        type="reasoning_delta",
                        data={"text_delta": text[i:i + _REASONING_CHUNK_CHARS]},
                    )
            elif block.type == "tool_use":
                yield StreamEvent(
                    type="tool_use_start",
                    data={"tool_id": block.tool_id, "tool_name": block.tool_name},
                )
                yield StreamEvent(
                    type="tool_use_delta",
                    data={
                        "tool_id": block.tool_id,
                        "input_delta": {
                            "arguments_fragment": json.dumps(block.tool_input),
                        },
                    },
                )
        yield StreamEvent(
            type="message_stop",
            data={"stop_reason": resp.stop_reason, "usage": resp.usage},
        )

    @property
    def model(self) -> str:
        return "test-model"

    async def generate_compact(self, prompt: str) -> str:
        return "[compacted]"


class SlowStreamLLM(MockLLMProvider):
    """Streams a canned text response with real delays between chunks.

    The stock mock replays a whole response in one event-loop turn, so
    a test can never cancel mid-stream; this one sleeps between chunks
    to give cancellation a deterministic window.
    """

    def __init__(
        self, text: str, *, chunk_size: int = 8, delay: float = 0.02,
    ):
        super().__init__([text_response(text)])
        self._chunk_size = chunk_size
        self._delay = delay

    async def _stream(self, resp: LLMResponse) -> AsyncIterator[StreamEvent]:
        text = resp.messages[0].content
        for i in range(0, len(text), self._chunk_size):
            await asyncio.sleep(self._delay)
            yield StreamEvent(
                type="content_delta",
                data={"text_delta": text[i:i + self._chunk_size]},
            )
        yield StreamEvent(
            type="message_stop",
            data={"stop_reason": resp.stop_reason, "usage": resp.usage},
        )


# ---------------------------------------------------------------------------
# Response factories
# ---------------------------------------------------------------------------


def make_mock_llm(*responses: LLMResponse) -> MockLLMProvider:
    """Build a :class:`MockLLMProvider` pre-loaded with *responses*."""
    return MockLLMProvider(list(responses))


def text_response(
    text: str,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> LLMResponse:
    """A plain-text end-turn response with minimal boilerplate."""
    blocks = [MessageBlock.content_block(text)] if text else []
    return LLMResponse(
        messages=[Message.assistant(blocks)],
        stop_reason=stop_reason,
        usage=TokenUsage(
            input_tokens=input_tokens, output_tokens=output_tokens,
        ),
    )


def reasoning_response(
    reasoning: str,
    content: str,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 60,
    reasoning_tokens: int = 40,
) -> LLMResponse:
    """A thinking-mode response: reasoning first, then the answer.

    Usage itemizes the reasoning tokens the way the API reports them — a
    subset of ``output_tokens``, never an addition to it.
    """
    return LLMResponse(
        messages=[
            Message.assistant([
                MessageBlock.reasoning_block(reasoning),
                MessageBlock.content_block(content),
            ]),
        ],
        stop_reason=stop_reason,
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
    )


def tool_use_response(
    tool_name: str,
    tool_input: dict,
    *,
    tool_id: str = "call_1",
) -> LLMResponse:
    """A single tool-call response."""
    return LLMResponse(
        messages=[
            Message.assistant([
                MessageBlock.tool_use_block(tool_id, tool_name, tool_input),
            ]),
        ],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def pause_on_write(file_path: str, content: str = "hello") -> LLMResponse:
    """A tool call against the real ``write_file`` tool.

    ``write_file`` is WRITE-permissioned, so under MANUAL mode (the
    default) the loop pauses with an :class:`~toddler.agent.events.AgentPaused`
    until the caller approves or denies.
    """
    return tool_use_response(
        "write_file",
        {"file_path": file_path, "content": content},
        tool_id="call_write",
    )


def plan_proposal_response(plan: Plan) -> LLMResponse:
    """The response for the plan-generation call.

    That call is non-streaming and expects raw JSON text in the reply —
    ``Plan.to_json()`` is exactly what the planner parses back.
    """
    return text_response(plan.to_json(), stop_reason="end_turn")
