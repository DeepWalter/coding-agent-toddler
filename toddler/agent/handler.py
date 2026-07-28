"""LLM response handlers — convert provider output into AgentEvent streams.

Provides a :class:`BaseHandler` ABC with two implementations:
:class:`StreamHandler` for real-time token streams and
:class:`NonStreamHandler` for complete responses.  The
:func:`create_handler` factory returns the right one for the current mode.

Uses :class:`IncrementalJSONParser` to parse streaming tool-call
arguments so the display can show partial JSON as it builds up.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallStart,
)
from toddler.llm import ContentBlock, Message, TokenUsage

if TYPE_CHECKING:
    from toddler.llm.responses import LLMResponse, StreamEvent

logger = logging.getLogger(__name__)


# =============================================================================
# Base handler
# =============================================================================


class BaseHandler(ABC):
    """Abstract base for handlers that convert LLM output into AgentEvents.

    Each concrete handler understands one kind of LLM response — streaming
    (:class:`~toddler.llm.StreamEvent` iterator) or non-streaming
    (:class:`~toddler.llm.LLMResponse`) — and provides a uniform interface
    so the agent loop never needs to branch on ``stream``.
    """

    @abstractmethod
    async def process(
        self,
        response: AsyncIterator[StreamEvent] | LLMResponse,
    ) -> AsyncIterator[AgentEvent]:
        """Convert *response* into :class:`AgentEvent` items in real time.

        Parameters
        ----------
        response:
            Either an async iterator of :class:`StreamEvent` (streaming) or a
            complete :class:`LLMResponse` (non-streaming).
        """
        ...

    @abstractmethod
    def get_final_result(self) -> dict[str, Message | None | str | TokenUsage]:
        """Return the assembled result dict.

        Keys are ``assistant_msg``, ``stop_reason``, ``usage``.
        Only valid after :meth:`process` has been fully consumed.
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Reset all internal state so the handler can be reused."""
        ...


# =============================================================================
# Incremental JSON parser
# =============================================================================


class IncrementalJSONParser:
    """Accumulates JSON string fragments and attempts to parse after each feed.

    When parsing fails, the previously-successful parse is retained so
    callers always see the best-effort partial result.  This gives the
    display a progressively-completing dict as the model streams arguments.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._parsed: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> dict[str, Any]:
        """Feed a new JSON fragment; return the best-effort parsed dict.

        Parameters
        ----------
        chunk:
            A fragment of JSON text (e.g. ``"path": "/foo``).

        Returns
        -------
        dict
            The currently-parseable dict — a snapshot (shallow copy) so
            callers can safely mutate it.
        """
        self._buffer += chunk
        # Plain try/except rather than contextlib.suppress — this is the
        # hot path (called once per streaming chunk) and try/except avoids
        # the context-manager __enter__/__exit__ overhead on every call.
        try:  # noqa: SIM105
            self._parsed = json.loads(self._buffer)
        except json.JSONDecodeError:
            pass  # Keep previous successfully-parsed state
        return dict(self._parsed)

    def finalize(self) -> dict[str, Any]:
        """Return the best-effort complete parse after the stream ends.

        If the accumulated buffer never formed valid JSON (e.g. the model
        produced malformed arguments), returns the last good partial parse.
        """
        try:
            self._parsed = json.loads(self._buffer)
        except json.JSONDecodeError:
            logger.debug(
                "Tool-call arguments never became valid JSON: %s",
                self._buffer[:200],
            )
        return dict(self._parsed)

    def reset(self) -> None:
        """Clear the buffer and parsed state for reuse."""
        self._buffer = ""
        self._parsed = {}


# =============================================================================
# Internal — per-tool-call accumulator
# =============================================================================


@dataclass(slots=True)
class _PartialTool:
    """Bookkeeping for one streaming tool call."""

    tool_id: str
    tool_name: str
    parser: IncrementalJSONParser


# =============================================================================
# StreamHandler
# =============================================================================


class StreamHandler(BaseHandler):
    """Consumes :class:`StreamEvent` items from the LLM provider and yields
    :class:`~toddler.agent.events.AgentEvent` objects for the agent loop.

    Maintains internal accumulators for text and tool calls, using
    :class:`IncrementalJSONParser` for streaming tool-call arguments.

    Usage::

        handler = StreamHandler()
        async for agent_event in handler.process(stream):
            yield agent_event

        # After the stream ends:
        assistant_msg, stop_reason, usage = handler.get_final_result()
    """

    def __init__(self) -> None:
        self._text_buf = ""
        self._tools: dict[str, _PartialTool] = {}  # tool_id → state
        self._tool_order: list[str] = []  # insertion order of tool_ids

        # Set by message_stop events during processing.
        self.stop_reason: str | None = None
        self.usage: TokenUsage | None = None

    # ------------------------------------------------------------------
    # Reuse
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Reset all internal state so the handler can be reused."""
        self._text_buf = ""
        self._tools.clear()
        self._tool_order.clear()
        self.stop_reason = None
        self.usage = None

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def process(
        self, stream: AsyncIterator[StreamEvent],
    ) -> AsyncIterator[AgentEvent]:
        """Consume *stream* and yield :class:`AgentEvent` objects.

        The caller should iterate until exhaustion; afterwards call
        :meth:`get_final_result` to get the assembled state.
        """
        async for event in stream:
            match event.type:
                case "text_delta":
                    text = event.data.get("text", "")
                    self._text_buf += text
                    yield TextDelta(text=text)

                case "tool_use_start":
                    evt = self._on_tool_start(event.data)
                    if evt is not None:
                        yield evt

                case "tool_use_delta":
                    evt = self._on_tool_delta(event.data)
                    if evt is not None:
                        yield evt

                case "message_stop":
                    self.stop_reason = event.data.get("stop_reason")
                    self.usage = event.data.get("usage")

                case "error":
                    error_msg = event.data.get(
                        "message", "Unknown streaming error"
                    )
                    yield AgentError(
                        message=error_msg, recoverable=True,
                    )

                case "message_start":
                    pass  # No-op — stream lifecycle tracking if needed later.

                case _:
                    logger.debug(f"Unhandled StreamEvent type: {event.type}")

    # ------------------------------------------------------------------
    # Assembled output
    # ------------------------------------------------------------------

    def _assemble_message(self) -> Message:
        """Build the completed assistant :class:`Message` from accumulated data.

        Returns a message with text content (if any) and tool-use blocks
        (if any), suitable for appending to the conversation history.
        """  # noqa: E501
        blocks: list[ContentBlock] = []

        if self._text_buf:
            blocks.append(ContentBlock.text_block(self._text_buf))

        for tool_id in self._tool_order:
            pt = self._tools[tool_id]
            parsed = pt.parser.finalize()
            blocks.append(
                ContentBlock.tool_use_block(
                    tool_id=pt.tool_id,
                    tool_name=pt.tool_name,
                    tool_input=parsed,
                )
            )

        return Message.assistant(blocks)

    def get_final_result(self) -> dict[str, Message | None | str | TokenUsage]:
        """Return the assembled result dict."""
        return {
            "assistant_msg": self._assemble_message(),
            "stop_reason": (
                self.stop_reason
                if self.stop_reason is not None
                else "end_turn"
            ),
            "usage": self.usage or TokenUsage(),
        }

    # ------------------------------------------------------------------
    # Tool-call tracking helpers
    # ------------------------------------------------------------------

    def _on_tool_start(self, data: dict) -> ToolCallStart | None:
        """Handle a ``tool_use_start`` StreamEvent.

        Creates a new :class:`_PartialTool` entry and returns a
        :class:`ToolCallStart` event for the display.
        """
        tool_id = data.get("tool_id", "")
        tool_name = data.get("tool_name", "")

        if not tool_id:
            # The OpenAI streaming protocol always sends tc.id in the
            # first chunk for a tool call — if it's missing the stream
            # is malformed; skip to avoid state corruption.
            logger.warning(
                "tool_use_start without tool_id for %s — skipping",
                tool_name or "<unknown>",
            )
            return None

        # The provider's tool_use_start only carries tool_id + tool_name;
        # actual arguments arrive in subsequent tool_use_delta chunks.
        parser = IncrementalJSONParser()

        self._tools[tool_id] = _PartialTool(
            tool_id=tool_id, tool_name=tool_name, parser=parser,
        )
        self._tool_order.append(tool_id)

        return ToolCallStart(
            tool_id=tool_id,
            tool_name=tool_name,
            partial_input=None,
        )

    def _on_tool_delta(self, data: dict) -> AgentEvent | None:
        """Handle a ``tool_use_delta`` StreamEvent.

        Feeds the arguments fragment into the incremental parser and
        returns a :class:`ToolCallDelta` with the current best-effort parse.
        """
        tool_id = data.get("tool_id", "")
        input_delta = data.get("input_delta", {})

        pt = self._tools.get(tool_id)
        if pt is None:
            return None

        fragment = input_delta.get("arguments_fragment", "")
        if not fragment:
            return None

        partial = pt.parser.feed(fragment)
        return ToolCallDelta(tool_id=tool_id, input_delta=partial)


# =============================================================================
# Non-streaming handler
# =============================================================================


class NonStreamHandler(BaseHandler):
    """Wraps a complete :class:`LLMResponse` in the handler interface.

    Yields a single :class:`TextDelta` (if the response has text), then
    exposes the assembled result via :meth:`get_final_result`.

    Usage::

        handler = NonStreamHandler()
        async for agent_event in handler.process(response):
            yield agent_event

        # After processing:
        assistant_msg, stop_reason, usage = handler.get_final_result()
    """

    def __init__(self) -> None:
        self._assistant_msg: Message | None = None
        self._stop_reason: str = "end_turn"
        self._usage: TokenUsage = TokenUsage()

    # ------------------------------------------------------------------
    # BaseHandler implementation
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Reset internal state for reuse."""
        self._assistant_msg = None
        self._stop_reason = "end_turn"
        self._usage = TokenUsage()

    async def process(
        self, response: LLMResponse,
    ) -> AsyncIterator[AgentEvent]:
        """Convert *response* into agent events.

        Yields a single :class:`TextDelta` if the response contained text.
        """
        self._assistant_msg = (
            response.messages[0]
            if response.messages
            else Message.assistant()
        )
        self._stop_reason = response.stop_reason
        self._usage = response.usage
        text = self._assistant_msg.text
        if text:
            yield TextDelta(text=text)

    def get_final_result(self) -> dict[str, Message | None | str | TokenUsage]:
        """Return the assembled result dict."""
        return {
            "assistant_msg": self._assistant_msg,
            "stop_reason": self._stop_reason,
            "usage": self._usage,
        }


# =============================================================================
# Factory
# =============================================================================


def create_handler(*, stream: bool) -> BaseHandler:
    """Create the right handler for the given streaming mode.

    Returns a :class:`StreamHandler` when *stream* is ``True``, otherwise a
    :class:`NonStreamHandler`.
    """
    return StreamHandler() if stream else NonStreamHandler()
