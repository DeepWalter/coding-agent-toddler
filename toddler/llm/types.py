"""Core LLM data models — Message, ContentBlock, StreamEvent, TokenUsage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Self

# ---------------------------------------------------------------------------
# Content blocks — the building blocks of messages
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    """A single block within a Message.

    Exactly one of the type-specific payload fields should be set,
    determined by ``type``:

    - ``text`` → ``text``
    - ``tool_use`` → ``tool_id``, ``tool_name``, ``tool_input``
    - ``tool_result`` → ``tool_id``, ``tool_result_content``, ``is_error``
    """

    type: Literal["text", "tool_use", "tool_result"]

    # text payload
    text: str | None = None

    # tool_use payload
    tool_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None

    # tool_result payload
    tool_result_content: str | None = None
    is_error: bool | None = None

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def text_block(cls, text: str) -> Self:
        return cls(type="text", text=text)

    @classmethod
    def tool_use_block(
        cls, tool_id: str, tool_name: str, tool_input: dict
    ) -> Self:
        return cls(
            type="tool_use",
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    @classmethod
    def tool_result_block(
        cls, tool_id: str, content: str, *, is_error: bool = False
    ) -> Self:
        return cls(
            type="tool_result",
            tool_id=tool_id,
            tool_result_content=content,
            is_error=is_error,
        )


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A conversation turn — one role + a list of ContentBlocks."""

    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentBlock]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def system(cls, text: str) -> Self:
        return cls(role="system", content=[ContentBlock.text_block(text)])

    @classmethod
    def user(cls, text: str) -> Self:
        return cls(role="user", content=[ContentBlock.text_block(text)])

    @classmethod
    def assistant(cls, blocks: list[ContentBlock] | None = None) -> Self:
        return cls(role="assistant", content=blocks or [])

    @classmethod
    def tool(cls, blocks: list[ContentBlock]) -> Self:
        return cls(role="tool", content=blocks)

    @property
    def text(self) -> str:
        """Concatenated text from all text blocks (convenience)."""
        return "".join(
            b.text for b in self.content if b.type == "text" and b.text
        )


# ---------------------------------------------------------------------------
# Streaming events (normalised across providers)
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """One normalised streaming event from the LLM backend.

    Event types and their expected ``data`` keys:

    ===================  =========================================
    ``text_delta``       ``{"text": "..."}``
    ``text_done``        ``{"text": "full accumulated text"}``
    ``tool_use_start``   ``{"tool_id": ..., "tool_name": ...}``
    ``tool_use_delta``   ``{"tool_id": ..., "input_delta": {...}}``
    ``tool_use_done``    ``{"tool_id": ..., "tool_name": ..., "input": {...}}``
    ``message_start``    ``{}``
    ``message_stop``     ``{"stop_reason": ..., "usage": TokenUsage}``
    ``error``            ``{"message": ..., "status_code": ...}``
    ===================  =========================================
    """

    type: Literal[
        "text_delta",
        "text_done",
        "tool_use_start",
        "tool_use_delta",
        "tool_use_done",
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

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Self) -> Self:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,  # noqa: E501
        )


# ---------------------------------------------------------------------------
# LLM Response (non-streaming)
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """A complete (non-streamed) response from the LLM."""

    messages: list[Message]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]
    usage: TokenUsage
