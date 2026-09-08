"""LLM provider output models — StreamEvent, LLMResponse, TokenUsage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self

if TYPE_CHECKING:
    from toddler.llm.messages import Message

__all__ = ["LLMResponse", "StreamEvent", "TokenUsage"]


# ---------------------------------------------------------------------------
# Streaming events (normalised across providers)
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """One normalised streaming event from the LLM backend.

    Event types and their expected ``data`` keys:

    ====================  =========================================
    ``content_delta``     ``{"text_delta": "..."}``
    ``reasoning_delta``   ``{"text_delta": "..."}``
    ``tool_use_start``    ``{"tool_id": ..., "tool_name": ...}``
    ``tool_use_delta``    ``{"tool_id": ..., "input_delta": {...}}``
    ``message_start``     ``{}``
    ``message_stop``      ``{"stop_reason": ..., "usage": TokenUsage}``
    ``error``             ``{"message": ..., "status_code": ...}``
    ====================  =========================================

    ``content_delta`` and ``reasoning_delta`` both feed the shared
    ``text`` payload slot of their block kind, so their increments share
    the slot-derived key ``text_delta``.
    """

    type: Literal[
        "content_delta",
        "reasoning_delta",
        "tool_use_start",
        "tool_use_delta",
        "message_start",
        "message_stop",
        "error",
    ]
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Token counts for a single API call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    # DeepSeek thinking-mode reasoning tokens — a subset of output_tokens,
    # itemized separately by the API.  `total` deliberately excludes the
    # breakdown (reasoning is already inside output_tokens).
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Self) -> Self:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,  # noqa: E501
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


# ---------------------------------------------------------------------------
# LLM Response (non-streaming)
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """A complete (non-streamed) response from the LLM provider.

    The field ``messages`` is a list because the OpenAI Chat Completions API
    can return multiple choices (via the ``n`` parameter), each carrying
    its own assistant message.  The current integration always reads
    ``choices[0]``, so the list is always 0 or 1 element in practice.
    """

    messages: list[Message]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]
    usage: TokenUsage
