"""LLM response handlers — convert provider output into AgentEvent streams.

Provides a :class:`BaseHandler` ABC with two implementations:
:class:`StreamHandler` for real-time token streams and
:class:`NonStreamHandler` for complete responses.  The
:func:`create_handler` factory returns the right one for the current mode.

Streaming tool-call arguments are accumulated verbatim and parsed once,
lazily, when a message or a cancel snapshot is assembled — no fragment of
a JSON object parses on its own, so there is nothing to show mid-stream.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from toddler.agent.events import (
    AgentEvent,
    ContentDelta,
    ReasoningDelta,
    RecoverableAgentError,
    ToolUseStart,
)
from toddler.llm import Message, MessageBlock, TokenUsage

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
    def get_partial_content(self) -> list[MessageBlock]:
        """Return the content the model produced so far, mid-processing.

        The agent loop appends this to the conversation when a turn is
        cancelled mid-stream, so the next turn continues from the
        model's partial output instead of losing it.  Returns ``[]``
        when nothing has been produced yet.
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Reset all internal state so the handler can be reused."""
        ...


# =============================================================================
# Internal — per-tool-call accumulator
# =============================================================================


@dataclass(slots=True)
class _PartialTool:
    """Bookkeeping for one streaming tool call.

    ``arguments`` is the raw fragment text, concatenated in arrival order
    and parsed once — by :func:`_parse_arguments` — when a message or a
    cancel snapshot is assembled.
    """

    tool_id: str
    tool_name: str
    arguments: str = ""


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Parse an accumulated tool-argument string, exactly once.

    Returns ``{}`` for empty input, for JSON that never became valid (the
    model produced malformed arguments — logged the same way the old
    parser logged it), and for non-object JSON: a scalar argument must
    never blow up the block assembly.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug(
            "Tool-call arguments never became valid JSON: %s", raw[:200],
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


# =============================================================================
# StreamHandler
# =============================================================================


class StreamHandler(BaseHandler):
    """Consumes :class:`StreamEvent` items from the LLM provider and yields
    :class:`~toddler.agent.events.AgentEvent` objects for the agent loop.

    Maintains internal accumulators for answer text, reasoning text, and
    tool calls — the arguments of a streaming call are buffered verbatim
    and parsed once, when the assembled message is built.

    Usage::

        handler = StreamHandler()
        async for agent_event in handler.process(stream):
            yield agent_event

        # After the stream ends:
        assistant_msg, stop_reason, usage = handler.get_final_result()
    """

    def __init__(self) -> None:
        self._content_buf = ""
        self._reasoning_buf = ""
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
        self._content_buf = ""
        self._reasoning_buf = ""
        self._tools.clear()
        self._tool_order.clear()
        self.stop_reason = None
        self.usage = None

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def process(  # noqa: C901 — one arm per StreamEvent type
        self, stream: AsyncIterator[StreamEvent],
    ) -> AsyncIterator[AgentEvent]:
        """Consume *stream* and yield :class:`AgentEvent` objects.

        The caller should iterate until exhaustion; afterwards call
        :meth:`get_final_result` to get the assembled state.
        """
        async for event in stream:
            match event.type:
                case "content_delta":
                    text_delta = event.data.get("text_delta", "")
                    self._content_buf += text_delta
                    yield ContentDelta(text_delta=text_delta)

                case "reasoning_delta":
                    text_delta = event.data.get("text_delta", "")
                    self._reasoning_buf += text_delta
                    yield ReasoningDelta(text_delta=text_delta)

                case "tool_use_start":
                    evt = self._on_tool_start(event.data)
                    if evt is not None:
                        yield evt

                case "tool_use_delta":
                    # Nothing to yield: the fragment is accumulated, and a
                    # JSON object has no prefix that parses.
                    self._on_tool_delta(event.data)

                case "message_stop":
                    self.stop_reason = event.data.get("stop_reason")
                    self.usage = event.data.get("usage")

                case "error":
                    error_msg = event.data.get(
                        "message", "Unknown streaming error"
                    )
                    yield RecoverableAgentError(message=error_msg)

                case "message_start":
                    pass  # No-op — stream lifecycle tracking if needed later.

                case _:
                    logger.debug(f"Unhandled StreamEvent type: {event.type}")

    # ------------------------------------------------------------------
    # Assembled output
    # ------------------------------------------------------------------

    def _content_blocks(self) -> list[MessageBlock]:
        """Build content blocks from the accumulated text and tool calls.

        Shared by the completed-message assembly and the partial-content
        extraction on cancel — both expose the same content, just at
        different points in the stream.  Tool arguments accumulated so far
        are parsed here, once: a cancelled call may carry a truncated
        buffer, which parses to nothing rather than raising.

        Reasoning comes first, matching both the model's emission order
        and the ``Message.blocks`` invariant.  Because this path serves
        the cancel handler too, a cancelled turn persists its partial
        reasoning and the next request echoes it back — which DeepSeek
        requires for tool-round messages.
        """
        blocks: list[MessageBlock] = []

        if self._reasoning_buf:
            blocks.append(MessageBlock.reasoning_block(self._reasoning_buf))

        if self._content_buf:
            blocks.append(MessageBlock.content_block(self._content_buf))

        for tool_id in self._tool_order:
            pt = self._tools[tool_id]
            parsed = _parse_arguments(pt.arguments)
            blocks.append(
                MessageBlock.tool_use_block(
                    tool_id=pt.tool_id,
                    tool_name=pt.tool_name,
                    tool_input=parsed,
                )
            )

        return blocks

    def _assemble_message(self) -> Message:
        """Build the completed assistant :class:`Message` from accumulated data.

        Returns a message with reasoning (if any), text content (if any),
        and tool-use blocks (if any), suitable for appending to the
        conversation history.
        """  # noqa: E501
        return Message.assistant(self._content_blocks())

    def get_partial_content(self) -> list[MessageBlock]:
        """Return the content the model produced so far, mid-stream.

        Reasoning and text accumulated so far plus any tool calls
        started (with best-effort parsed arguments).  Safe to append to
        the conversation on cancellation — the turn's partial output is
        preserved so the next turn can continue from it.  Returns
        ``[]`` when nothing has been produced yet.
        """
        return self._content_blocks()

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

    def _on_tool_start(self, data: dict) -> ToolUseStart | None:
        """Handle a ``tool_use_start`` StreamEvent.

        Registers a :class:`_PartialTool` accumulator and returns a
        :class:`ToolUseStart` for the display — the provider's start
        carries identity only; the arguments follow as fragments.
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

        if tool_id not in self._tools:
            self._tool_order.append(tool_id)
        # A repeated start replaces the accumulator: last start wins, and
        # the order list above never records the id twice.
        self._tools[tool_id] = _PartialTool(
            tool_id=tool_id, tool_name=tool_name,
        )

        return ToolUseStart(tool_id=tool_id, tool_name=tool_name)

    def _on_tool_delta(self, data: dict) -> None:
        """Handle a ``tool_use_delta`` StreamEvent — accumulate only.

        The fragment is raw argument text; parsing happens once, in
        :func:`_parse_arguments`, when a message or cancel snapshot is
        assembled.  A fragment for a call this handler never saw start is
        dropped: there is no accumulator to append it to.
        """
        tool_id = data.get("tool_id", "")
        input_delta = data.get("input_delta", {})

        pt = self._tools.get(tool_id)
        if pt is None:
            return

        fragment = input_delta.get("arguments_fragment", "")
        if not fragment:
            return

        pt.arguments += fragment


# =============================================================================
# Non-streaming handler
# =============================================================================


class NonStreamHandler(BaseHandler):
    """Wraps a complete :class:`LLMResponse` in the handler interface.

    Yields a single :class:`ReasoningDelta` (if the message carries
    reasoning) then a single :class:`ContentDelta` (if it has text), then
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

        Yields the reasoning (if any) then the text (if any), mirroring
        the streaming emission order.
        """
        self._assistant_msg = (
            response.messages[0]
            if response.messages
            else Message.assistant()
        )
        self._stop_reason = response.stop_reason
        self._usage = response.usage
        reasoning = self._assistant_msg.reasoning
        if reasoning:
            yield ReasoningDelta(text_delta=reasoning)
        content = self._assistant_msg.content
        if content:
            yield ContentDelta(text_delta=content)

    def get_partial_content(self) -> list[MessageBlock]:
        """Return the response content.

        The complete response is available as soon as :meth:`process`
        starts, so a cancel at any point still preserves it.
        """
        if self._assistant_msg is None:
            return []
        return list(self._assistant_msg.blocks)

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
