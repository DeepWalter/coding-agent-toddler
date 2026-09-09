"""OpenAI-compatible LLM provider — DeepSeek, OpenAI, local models.

Implements :class:`BaseLLMProvider` via the ``openai`` SDK.  Handles
streaming, tool calling, and converting between Toddler's internal
message format and the OpenAI wire format.

Works with any OpenAI-compatible endpoint:
- DeepSeek (``https://api.deepseek.com``)
- OpenAI (``https://api.openai.com``)
- vLLM / ollama / LiteLLM (``http://localhost:8000/v1``)

Note on ``max_tokens`` and thinking mode — DeepSeek v4 runs in thinking
mode by default; its reasoning tokens (``reasoning_content``) consume the
``max_tokens`` budget.  A thinking-heavy run can exhaust it, ending with
``finish_reason="length"`` and empty ``content`` — raise ``TODDLER_MAX_TOKENS``
(default 8192).  ``TokenUsage.reasoning_tokens`` shows the reasoning/output
split.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from openai import NOT_GIVEN, NotGiven

from toddler.llm._async_openai import AsyncOpenAI
from toddler.llm.base import BaseLLMProvider
from toddler.llm.messages import Message, MessageBlock
from toddler.llm.responses import LLMResponse, StreamEvent, TokenUsage

if TYPE_CHECKING:
    from toddler.config.settings import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Finish-reason mapping: OpenAI → internal
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "stop_sequence",
}


def _map_finish_reason(raw: str | None) -> str:
    """Convert an OpenAI finish reason into our internal label."""
    if raw is None:
        return "end_turn"
    return _FINISH_REASON_MAP.get(raw, "end_turn")


# ---------------------------------------------------------------------------
# OpenAI Compatible Provider
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(BaseLLMProvider):
    """LLM provider for any OpenAI-compatible chat-completion API.

    Parameters
    ----------
    settings:
        Resolved :class:`~toddler.config.settings.Settings` object that
        carries ``api_key``, ``base_url``, and ``model``.
    http_client:
        Optional shared ``httpx.AsyncClient``.  When *None* a default
        client is created internally.
    """

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._model = settings.model
        self._client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            http_client=http_client,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def model(self) -> str:
        """The model name string."""
        return self._model

    # ------------------------------------------------------------------
    # generate — the core API
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent] | LLMResponse:
        openai_messages = self._messages_to_openai(messages)
        openai_tools = self._tools_param(tools)

        if stream:
            return self._generate_streaming(
                openai_messages, openai_tools, max_tokens, temperature
            )
        return await self._generate_non_streaming(
            openai_messages, openai_tools, max_tokens, temperature
        )

    # ------------------------------------------------------------------
    # Compaction helper
    # ------------------------------------------------------------------

    async def generate_compact(self, prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.0,
            stream=False,
        )
        content = response.choices[0].message.content
        return content or ""

    # ==================================================================
    # Streaming path
    # ==================================================================

    async def _generate_streaming(
        self,
        openai_messages: list[dict],
        openai_tools: list[dict] | NotGiven,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[StreamEvent]:
        """Yield :class:`StreamEvent` items from an SSE stream."""
        yield StreamEvent(type="message_start", data={})

        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=openai_messages,
                tools=openai_tools,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as exc:
            logger.exception("Failed to start streaming call")
            yield StreamEvent(
                type="error",
                data={"message": self._format_error(exc)},
            )
            return

        # Track seen tool indices → tool_id so argument deltas can be
        # tagged with the correct tool_id (OpenAI only sends the id in the
        # first chunk for each tool call).
        seen_ids: dict[int, str] = {}

        # Usage arrives on the trailing chunk of the SSE stream.  OpenAI and
        # DeepSeek send it on an empty-``choices`` chunk *after* the finish
        # chunk (that trailer used to be skipped, dropping per-call usage
        # entirely); local servers (vLLM / ollama) attach it to the finish
        # chunk itself.  Track the last non-zero usage seen — it feeds the
        # post-loop fallback below, so the trailer's counts reach the stop
        # event — and emit the stop exactly once.
        last_usage = TokenUsage()
        stop_emitted = False
        stop_reason = "end_turn"

        try:
            async for chunk in stream:
                # Read usage from every chunk — before the empty-``choices``
                # skip below, so the usage-only trailer is never lost.  The
                # local also feeds the finish check below, so each chunk is
                # extracted exactly once.
                usage = self._extract_usage(chunk)
                if usage != TokenUsage():
                    last_usage = usage

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                for evt in self._chunk_events(delta, seen_ids):
                    yield evt

                # -- finish ---------------------------------------------------
                if finish_reason is not None:
                    stop_reason = _map_finish_reason(finish_reason)
                    if usage != TokenUsage():
                        # The finish chunk itself carried the usage (vLLM /
                        # ollama) — the stream is complete; emit now.  Keyed
                        # on this chunk's usage, not the accumulated
                        # last_usage: OpenAI / DeepSeek send usage only on
                        # the post-finish trailer, so deferring here is what
                        # lets the fallback below carry its numbers — and a
                        # gateway that stamps usage mid-stream must not
                        # trigger an early stop.  A finish chunk is
                        # single-shot in the SSE protocol, so no duplicate
                        # guard is needed (the post-loop emission below is
                        # guarded instead).
                        stop_emitted = True
                        yield StreamEvent(
                            type="message_stop",
                            data={
                                "stop_reason": stop_reason,
                                "usage": last_usage,
                            },
                        )

            # Stream exhausted — the exact-once fallback. Fires when the
            # finish chunk carried no usage (OpenAI / DeepSeek send it on
            # the post-finish trailer consumed above) or when the stream
            # ended without a finish chunk at all.
            if not stop_emitted:
                yield StreamEvent(
                    type="message_stop",
                    data={
                        "stop_reason": stop_reason,
                        "usage": last_usage,
                    },
                )
        except Exception as exc:
            logger.exception("Error during streaming")
            yield StreamEvent(
                type="error",
                data={"message": self._format_error(exc)},
            )

    # ==================================================================
    # Non-streaming path
    # ==================================================================

    async def _generate_non_streaming(
        self,
        openai_messages: list[dict],
        openai_tools: list[dict] | NotGiven,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Return a single :class:`LLMResponse` (no streaming)."""

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=openai_messages,
                tools=openai_tools,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
            )
        except Exception:
            logger.exception("Non-streaming call failed")
            raise

        choice = response.choices[0]
        internal_msg = self._openai_message_to_internal(choice.message)
        usage = self._extract_usage(response)

        return LLMResponse(
            messages=[internal_msg] if internal_msg.blocks else [],
            stop_reason=_map_finish_reason(choice.finish_reason),
            usage=usage,
        )

    # ==================================================================
    # Message conversion: Toddler internal → OpenAI wire format
    # ==================================================================

    @staticmethod
    def _messages_to_openai(messages: list[Message]) -> list[dict]:
        """Convert a list of internal :class:`Message` objects to the
        list-of-dicts expected by the OpenAI chat-completion endpoint."""

        openai_msgs: list[dict] = []
        for msg in messages:
            if msg.role == "tool":
                # One internal "tool" message may carry multiple
                # tool_result blocks — OpenAI wants one message per result.
                for block in msg.blocks:
                    if block.type == "tool_result":
                        openai_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.tool_id,
                                "content": block.tool_result_content or "",
                            }
                        )
            else:
                content = msg.content
                tool_calls = []
                for b in msg.blocks:
                    if b.type == "tool_use":
                        tool_calls.append(
                            {
                                "id": b.tool_id,
                                "type": "function",
                                "function": {
                                    "name": b.tool_name,
                                    "arguments": json.dumps(
                                        b.tool_input or {},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        )

                openai_msg: dict = {"role": msg.role}
                if tool_calls:
                    openai_msg["content"] = content or None
                    openai_msg["tool_calls"] = tool_calls
                else:
                    openai_msg["content"] = content or ""

                # DeepSeek echo-back requirement: in thinking mode the
                # tool-round assistant messages' reasoning_content must be
                # passed back on every subsequent request, or the API
                # returns HTTP 400 mid-turn.  Self-scoping — only history
                # produced by a reasoning endpoint carries a reasoning
                # block, so only such traffic ever receives the key; the
                # plain dict rides through the OpenAI SDK unvalidated.
                # Residual risk: a strict third-party validator could
                # reject the unknown key (accepted).
                reasoning = msg.reasoning
                if reasoning:
                    openai_msg["reasoning_content"] = reasoning

                openai_msgs.append(openai_msg)

        return openai_msgs

    # ==================================================================
    # Message conversion: OpenAI wire format → Toddler internal
    # ==================================================================

    @staticmethod
    def _openai_message_to_internal(oa_msg) -> Message:
        """Convert a single OpenAI response message into our internal
        :class:`Message`."""

        blocks: list[MessageBlock] = []

        # DeepSeek thinking mode streams reasoning before the answer —
        # build blocks in that same order.
        reasoning = getattr(oa_msg, "reasoning_content", None)
        if reasoning:
            blocks.append(MessageBlock.reasoning_block(reasoning))

        # Text content
        if oa_msg.content:
            blocks.append(MessageBlock.content_block(oa_msg.content))

        # Tool calls
        if oa_msg.tool_calls:
            for tc in oa_msg.tool_calls:
                tool_input = {}
                if tc.function and tc.function.arguments:
                    tool_input = json.loads(tc.function.arguments)
                blocks.append(
                    MessageBlock.tool_use_block(
                        tool_id=tc.id,
                        tool_name=tc.function.name if tc.function else "",
                        tool_input=tool_input,
                    )
                )

        return Message.assistant(blocks)

    # ==================================================================
    # Streaming chunk helpers
    # ==================================================================

    def _chunk_events(
        self, delta, seen_ids: dict[int, str]
    ) -> list[StreamEvent]:
        """Build the prose and tool-call events carried by one chunk.

        Prose first: reasoning (DeepSeek thinking mode) streams before the
        answer text, and a transition chunk may carry the tail of one and
        the head of the other.  Both kinds feed the shared ``text`` payload
        slot, so both build the slot-derived ``text_delta`` key.  Tool-call
        deltas follow, tagged with the owning ``tool_id`` via *seen_ids*
        (the id only appears in the first chunk of each call, so it is
        memoized by chunk index).
        """
        if delta is None:
            return []
        events: list[StreamEvent] = []

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            events.append(
                StreamEvent(
                    type="reasoning_delta", data={"text_delta": reasoning}
                )
            )
        if delta.content:
            events.append(
                StreamEvent(
                    type="content_delta", data={"text_delta": delta.content}
                )
            )

        for tc in delta.tool_calls or []:
            # Memoize the tool_id from the first chunk where it appears.
            if tc.id:
                seen_ids[tc.index] = tc.id
            tool_id = seen_ids.get(tc.index, "")

            # Events need both a function and an attributable tool_id —
            # anything else would only be dropped by the handler.
            if not tc.function or not tool_id:
                continue

            if tc.function.name:
                events.append(
                    StreamEvent(
                        type="tool_use_start",
                        data={
                            "tool_id": tool_id,
                            "tool_name": tc.function.name,
                        },
                    )
                )
            if tc.function.arguments:
                events.append(
                    StreamEvent(
                        type="tool_use_delta",
                        data={
                            "tool_id": tool_id,
                            "input_delta": {
                                "arguments_fragment": tc.function.arguments
                            },
                        },
                    )
                )

        return events

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _tools_param(tools: list[dict]) -> list[dict] | NotGiven:
        """Return the tools list or ``NOT_GIVEN`` when empty.

        Passing an empty list to some OpenAI-compatible endpoints causes
        a 400 error; omitting the field entirely is safer.
        """
        return tools if tools else NOT_GIVEN

    @staticmethod
    def _extract_usage(chunk_or_response) -> TokenUsage:
        """Pull token usage from an OpenAI chunk or response object."""
        usage = getattr(chunk_or_response, "usage", None)
        if usage is None:
            return TokenUsage()

        # DeepSeek thinking mode itemizes reasoning tokens separately
        # (completion_tokens_details.reasoning_tokens); absent on other
        # providers and the Dummy's chunks — read defensively.
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = 0
        if details is not None:
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0

        return TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            reasoning_tokens=reasoning_tokens,
        )

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """Produce a user-friendly error message from an exception."""
        # httpx / network errors
        if isinstance(exc, httpx.HTTPStatusError):
            return (
                f"API server returned {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        if isinstance(exc, httpx.ConnectError):
            return (
                f"Cannot connect to API endpoint — check your network "
                f"and base_url: {exc}"
            )
        if isinstance(exc, httpx.TimeoutException):
            return f"Request timed out: {exc}"

        # Try to surface OpenAI/DeepSeek error details.
        if hasattr(exc, "status_code"):
            msg = getattr(exc, "message", str(exc))
            return f"[{getattr(exc, 'status_code', '?')}] {msg}"

        return str(exc)
