"""LLM provider input models — ContentBlock and Message."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Self

__all__ = ["ContentBlock", "Message"]


# ---------------------------------------------------------------------------
# ContentBlock — the building block of messages
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

    # shared tool payload
    tool_id: str | None = None

    # tool_use payload
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
    """An entry in the LLM conversation history.

    Each message pairs a ``role`` with a list of :class:`ContentBlock`
    items and a :class:`~datetime.datetime` timestamp.

    Roles follow the OpenAI Chat Completions convention:

    - ``system``  — high-level instructions injected at the start of the
      conversation to steer the model's behaviour.
    - ``user`` — input from the human (or a proxy acting on their behalf).
    - ``assistant`` — LLM-generated replies, including text and tool-use
      requests.
    - ``tool`` — results returned by tools after an assistant's tool-use
      request.  Must carry the same ``tool_id`` that the assistant used.
    """

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
