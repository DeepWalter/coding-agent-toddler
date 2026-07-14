"""StreamHandler — consumes StreamEvent items, yields AgentEvent objects.

Wraps the raw :class:`~toddler.llm.types.StreamEvent` iterator from the
LLM provider and aggregates text + tool-call deltas into higher-level
:class:`~toddler.agent.events.AgentEvent` objects for the CLI layer.

Uses :class:`IncrementalJSONParser` to parse streaming tool-call
arguments so the display can show partial JSON as it builds up.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallStart,
)
from toddler.llm.types import ContentBlock, Message, StreamEvent, TokenUsage

logger = logging.getLogger(__name__)


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
        with contextlib.suppress(json.JSONDecodeError):
            self._parsed = json.loads(self._buffer)
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


class _PartialTool:
    """Bookkeeping for one streaming tool call."""

    __slots__ = ("tool_id", "tool_name", "parser")

    def __init__(
        self,
        tool_id: str,
        tool_name: str,
        parser: IncrementalJSONParser,
    ) -> None:
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.parser = parser


# =============================================================================
# StreamHandler
# =============================================================================


class StreamHandler:
    """Consumes :class:`StreamEvent` items from the LLM provider and yields
    :class:`~toddler.agent.events.AgentEvent` objects for the agent loop.

    Maintains internal accumulators for text and tool calls, using
    :class:`IncrementalJSONParser` for streaming tool-call arguments.
    After the stream completes, the assembled state is available via
    :attr:`stop_reason`, :attr:`usage`, and :meth:`assemble_message`.

    Usage::

        handler = StreamHandler()
        async for agent_event in handler.process(stream):
            yield agent_event

        # After the stream ends:
        msg = handler.assemble_message()
        reason = handler.stop_reason
        tokens = handler.usage
    """

    def __init__(self) -> None:
        self._text_buf = ""
        self._tools: dict[str, _PartialTool] = {}  # tool_id → state
        self._tool_order: list[str] = []            # insertion order of tool_ids

        # Set by message_stop / error events during processing.
        self.stop_reason: str | None = None
        self.usage: TokenUsage | None = None
        self._error_message: str | None = None

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def accumulated_text(self) -> str:
        """All text accumulated from the streaming response so far."""
        return self._text_buf

    @property
    def has_error(self) -> bool:
        """True if the stream encountered an error."""
        return self._error_message is not None

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def process(  # noqa: C901
        self, stream: AsyncIterator[StreamEvent],
    ) -> AsyncIterator[AgentEvent]:
        """Consume *stream* and yield :class:`AgentEvent` objects.

        The caller should iterate until exhaustion; afterwards
        :attr:`stop_reason`, :attr:`usage`, and :meth:`assemble_message`
        are available.
        """
        async for event in stream:
            match event.type:
                case "text_delta":
                    text = event.data.get("text", "")
                    self._text_buf += text
                    yield TextDelta(text=text)

                case "tool_use_start":
                    for evt in self._on_tool_start(event.data):
                        yield evt

                case "tool_use_delta":
                    evt = self._on_tool_delta(event.data)
                    if evt is not None:
                        yield evt

                case "tool_use_done":
                    # The provider sends tool_use_done when a tool call's
                    # input JSON is complete.  Finalise the incremental
                    # parser and update our state — but we do NOT yield
                    # ToolCallEnd here.  Execution happens in the agent
                    # loop, which is responsible for yielding ToolCallEnd
                    # with the result.
                    self._on_tool_done(event.data)

                case "message_stop":
                    self.stop_reason = event.data.get("stop_reason")
                    self.usage = event.data.get("usage")

                case "error":
                    self._error_message = event.data.get(
                        "message", "Unknown streaming error"
                    )
                    yield AgentError(
                        message=self._error_message, recoverable=True,
                    )

                case "message_start":
                    pass  # No-op — stream lifecycle tracking if needed later.

                case "text_done":
                    pass  # Internal — already have all text in _text_buf.

                case _:
                    logger.debug(
                        "Unhandled StreamEvent type: %s", event.type
                    )

    # ------------------------------------------------------------------
    # Assembled output
    # ------------------------------------------------------------------

    def assemble_message(self) -> Message:
        """Build the completed assistant :class:`Message` from accumulated data.

        Returns a message with text content (if any) and tool-use blocks
        (if any), suitable for appending to the conversation history.
        """
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

    # ------------------------------------------------------------------
    # Tool-call tracking helpers
    # ------------------------------------------------------------------

    def _on_tool_start(self, data: dict) -> list[AgentEvent]:
        """Handle a ``tool_use_start`` StreamEvent.

        Creates a new :class:`_PartialTool` entry and yields a
        :class:`ToolCallStart` event for the display.
        """
        tool_id = data.get("tool_id", "")
        tool_name = data.get("tool_name", "")

        if not tool_id:
            # Edge case: provider hasn't yet received the tool ID.
            # We'll create a placeholder — the ID should arrive in the
            # next tool_use_delta chunk.
            tool_id = f"_pending_{len(self._tool_order)}"

        # Create parser and start with whatever partial input we have.
        parser = IncrementalJSONParser()
        partial_input = data.get("input") or data.get("partial_input") or {}
        if partial_input:
            # We have some pre-parsed input — seed the parser.
            raw = json.dumps(partial_input, ensure_ascii=False)
            parser.feed(raw)

        self._tools[tool_id] = _PartialTool(
            tool_id=tool_id, tool_name=tool_name, parser=parser,
        )
        self._tool_order.append(tool_id)

        return [ToolCallStart(
            tool_id=tool_id,
            tool_name=tool_name,
            partial_input=partial_input or None,
        )]

    def _on_tool_delta(self, data: dict) -> AgentEvent | None:
        """Handle a ``tool_use_delta`` StreamEvent.

        Feeds the arguments fragment into the incremental parser and
        yields a :class:`ToolCallDelta` with the current best-effort parse.
        """
        tool_id = data.get("tool_id", "")
        input_delta = data.get("input_delta", {})

        # Resolve tool — may be under a temporary _pending_ key.
        pt = self._resolve_tool(tool_id)
        if pt is None:
            return None

        fragment = input_delta.get("arguments_fragment", "")
        if not fragment:
            return None

        partial = pt.parser.feed(fragment)
        return ToolCallDelta(tool_id=tool_id, input_delta=partial)

    def _on_tool_done(self, data: dict) -> None:
        """Handle a ``tool_use_done`` StreamEvent.

        Finalises the tool's parser and updates the tool ID mapping in
        case a temporary key was used.
        """
        final_tool_id = data.get("tool_id", "")
        final_input = data.get("input", {})

        if not final_tool_id:
            return

        # If the tool was tracked under a different (temporary) key,
        # migrate it.
        pt = self._resolve_tool(final_tool_id)
        if pt is None:
            # Tool was tracked under a temp key — find and rename.
            for old_id, old_pt in list(self._tools.items()):
                if old_id.startswith("_pending_") and old_pt.tool_name == data.get("tool_name", ""):  # noqa: E501
                    del self._tools[old_id]
                    self._tools[final_tool_id] = old_pt
                    old_pt.tool_id = final_tool_id
                    # Fix up the order list too.
                    if old_id in self._tool_order:
                        idx = self._tool_order.index(old_id)
                        self._tool_order[idx] = final_tool_id
                    pt = old_pt
                    break

        if pt is not None and final_input:
            # Seed the parser with the final parsed input so
            # assemble_message() gets the complete data.
            raw = json.dumps(final_input, ensure_ascii=False)
            pt.parser._buffer = raw  # noqa: SLF001
            with contextlib.suppress(Exception):
                pt.parser._parsed = final_input  # noqa: SLF001

    # ------------------------------------------------------------------
    # Tool resolution
    # ------------------------------------------------------------------

    def _resolve_tool(self, tool_id: str) -> _PartialTool | None:
        """Find a :class:`_PartialTool` by its ``tool_id``.

        Falls back to matching a ``_pending_*`` key when the definitive
        ``tool_id`` hasn't been associated yet.
        """
        if tool_id in self._tools:
            return self._tools[tool_id]

        # Search pending entries — the tool_id may not have been set
        # in the start event yet.
        for pt in self._tools.values():
            if pt.tool_id == tool_id:
                return pt

        return None
