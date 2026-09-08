"""LLM provider input models — MessageBlock and Message."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Self

__all__ = ["Message", "MessageBlock"]


# ---------------------------------------------------------------------------
# MessageBlock — the building block of messages
# ---------------------------------------------------------------------------


@dataclass
class MessageBlock:
    """A single block within a Message.

    Exactly one of the type-specific payload fields should be set,
    determined by ``type``:

    - ``content`` → ``text``
    - ``reasoning`` → ``text`` — the ``content`` and ``reasoning`` kinds
      share the ``text`` payload slot
    - ``tool_use`` → ``tool_id``, ``tool_name``, ``tool_input``
    - ``tool_result`` → ``tool_id``, ``tool_result_content``, ``is_error``
    """

    type: Literal["content", "reasoning", "tool_use", "tool_result"]

    # prose payload — shared by the content and reasoning kinds
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
    def content_block(cls, text: str) -> Self:
        return cls(type="content", text=text)

    @classmethod
    def reasoning_block(cls, text: str) -> Self:
        return cls(type="reasoning", text=text)

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

    Each message pairs a ``role`` with a list of :class:`MessageBlock`
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
    blocks: list[MessageBlock]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def system(cls, text: str) -> Self:
        return cls(role="system", blocks=[MessageBlock.content_block(text)])

    @classmethod
    def user(cls, text: str) -> Self:
        return cls(role="user", blocks=[MessageBlock.content_block(text)])

    @classmethod
    def assistant(cls, blocks: list[MessageBlock] | None = None) -> Self:
        return cls(role="assistant", blocks=blocks or [])

    @classmethod
    def tool(cls, blocks: list[MessageBlock]) -> Self:
        return cls(role="tool", blocks=blocks)

    @property
    def content(self) -> str:
        """Concatenated answer text from the ``content`` blocks."""
        return "".join(
            b.text for b in self.blocks if b.type == "content" and b.text
        )

    @property
    def reasoning(self) -> str:
        """Concatenated reasoning text from the ``reasoning`` blocks."""
        return "".join(
            b.text
            for b in self.blocks
            if b.type == "reasoning" and b.text
        )
