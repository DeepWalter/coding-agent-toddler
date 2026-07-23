"""OpenAI-compatible LLM provider — DeepSeek, OpenAI, local models.

Implements :class:`BaseLLMProvider` using the ``openai`` SDK.  Supports
streaming, tool calling, and message conversion between Toddler's internal
format and the OpenAI chat-completion wire format.

Works with any OpenAI-compatible endpoint:
- DeepSeek (``https://api.deepseek.com``)
- OpenAI (``https://api.openai.com``)
- vLLM / ollama / LiteLLM (``http://localhost:8000/v1``)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from openai import NOT_GIVEN, AsyncOpenAI, NotGiven

from toddler.llm.base import BaseLLMProvider
from toddler.llm.token_counter import TokenCounter
from toddler.llm.types import (
    ContentBlock,
    LLMResponse,
    Message,
    StreamEvent,
    TokenUsage,
)

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
        carries ``api_key``, ``base_url``, ``model``, and
        ``context_window``.
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
        self._token_counter = TokenCounter(model=settings.model)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def context_window(self) -> int:
        return self._settings.context_window

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
        if stream:
            return self._generate_streaming(
                messages, tools, max_tokens, temperature
            )
        return await self._generate_non_streaming(
            messages, tools, max_tokens, temperature
        )

    # ------------------------------------------------------------------
    # Token counting (delegates to TokenCounter)
    # ------------------------------------------------------------------

    def count_tokens(self, messages: list[Message]) -> int:
        return self._token_counter.count_messages(messages)

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
        messages: list[Message],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[StreamEvent]:
        """Yield :class:`StreamEvent` items from an SSE stream."""

        openai_messages = self._messages_to_openai(messages)
        openai_tools = self._tools_param(tools)

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

        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                # -- text deltas ----------------------------------------------
                if delta is not None and delta.content:
                    yield StreamEvent(
                        type="text_delta", data={"text": delta.content}
                    )

                # -- tool-call deltas -----------------------------------------
                if delta is not None and delta.tool_calls:
                    for evt in self._process_tool_call_deltas(
                        delta.tool_calls, seen_ids
                    ):
                        yield evt

                # -- finish ---------------------------------------------------
                if finish_reason is not None:
                    usage = self._extract_usage(chunk)
                    yield StreamEvent(
                        type="message_stop",
                        data={
                            "stop_reason": _map_finish_reason(finish_reason),
                            "usage": usage,
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
        messages: list[Message],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Return a single :class:`LLMResponse` (no streaming)."""

        openai_messages = self._messages_to_openai(messages)
        openai_tools = self._tools_param(tools)

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
            messages=[internal_msg] if internal_msg.content else [],
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
                for block in msg.content:
                    if block.type == "tool_result":
                        openai_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.tool_id,
                                "content": block.tool_result_content or "",
                            }
                        )
            else:
                text = "".join(
                    b.text
                    for b in msg.content
                    if b.type == "text" and b.text
                )
                tool_calls = []
                for b in msg.content:
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
                    openai_msg["content"] = text or None
                    openai_msg["tool_calls"] = tool_calls
                else:
                    openai_msg["content"] = text or ""

                openai_msgs.append(openai_msg)

        return openai_msgs

    # ==================================================================
    # Message conversion: OpenAI wire format → Toddler internal
    # ==================================================================

    @staticmethod
    def _openai_message_to_internal(oa_msg) -> Message:
        """Convert a single OpenAI response message into our internal
        :class:`Message`."""

        blocks: list[ContentBlock] = []

        # Text content
        if oa_msg.content:
            blocks.append(ContentBlock.text_block(oa_msg.content))

        # Tool calls
        if oa_msg.tool_calls:
            for tc in oa_msg.tool_calls:
                tool_input = {}
                if tc.function and tc.function.arguments:
                    tool_input = json.loads(tc.function.arguments)
                blocks.append(
                    ContentBlock.tool_use_block(
                        tool_id=tc.id,
                        tool_name=tc.function.name if tc.function else "",
                        tool_input=tool_input,
                    )
                )

        return Message.assistant(blocks)

    # ==================================================================
    # Tool-call delta processing (streaming)
    # ==================================================================

    @staticmethod
    def _process_tool_call_deltas(
        tc_deltas: list, seen_ids: dict[int, str]
    ) -> list[StreamEvent]:
        """Forward tool-call deltas from one streaming chunk as events.

        Updates *seen_ids* in place (mapping OpenAI's chunk index → tool_id)
        so argument deltas can be tagged with the correct tool_id even when
        the id is only present in the first chunk.
        """
        events: list[StreamEvent] = []

        for tc in tc_deltas:
            idx: int = tc.index

            # Capture tool_id from the first chunk where it appears.
            if tc.id:
                seen_ids[idx] = tc.id
            tool_id = seen_ids.get(idx, "")

            if tc.function:
                if tc.function.name and tool_id:
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
        return TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
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
