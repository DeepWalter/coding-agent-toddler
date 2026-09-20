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
from typing import TYPE_CHECKING, Any

import httpx
from openai import NOT_GIVEN, NotGiven

from toddler.llm._async_openai import AsyncOpenAI
from toddler.llm.base import BaseLLMProvider
from toddler.llm.messages import Message, MessageBlock
from toddler.llm.responses import LLMResponse, StreamEvent, TokenUsage

if TYPE_CHECKING:
    from toddler.config.settings import Settings

logger = logging.getLogger(__name__)


# Thinking-effort tiers → DeepSeek's coarser scale.  DeepSeek accepts only
# ``low`` / ``high`` / ``max`` (its own default is ``high``); callers speak a
# wider scale, so the finer tiers collapse onto the nearest DeepSeek one.
_DEEPSEEK_REASON_EFFORT_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}

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
        carries the endpoint's ``api_key`` and ``base_url``.  The model is
        not provider state — every call names its own.
    http_client:
        Optional shared ``httpx.AsyncClient``.  When *None* a default
        client is created internally.
    """

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            http_client=http_client,
        )

    # ------------------------------------------------------------------
    # generate — the core API
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        model: str,
        max_completion_tokens: int = 4096,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        stream: bool = True,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamEvent] | LLMResponse:
        """Send one chat-completion request — streaming by default.

        *model* and *reasoning_effort* are the caller's, not the
        provider's: one agent turn names the same model on every call it
        makes.  A ``None`` *reasoning_effort* omits the field, leaving the
        endpoint's own default in place.  The model family decides the
        final wire shape — see :meth:`_parse_params` — including whether
        prior reasoning is echoed back, see :meth:`_messages_to_openai`.
        """
        openai_messages = self._messages_to_openai(messages, model=model)
        openai_tools = self._tools_param(tools)

        kwargs = self._parse_params(
            model,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            temperature=temperature,
        )

        if stream:
            kwargs.update({
                "stream": True,
                "stream_options": {"include_usage": True},
            })
            return self._generate_streaming(
                openai_messages, openai_tools, model=model, **kwargs
            )

        kwargs["stream"] = False
        return await self._generate_non_streaming(
            openai_messages, openai_tools, model=model, **kwargs
        )

    # ------------------------------------------------------------------
    # Compaction helper
    # ------------------------------------------------------------------

    async def generate_compact(self, prompt: str, *, model: str) -> str:
        # Deliberately bare: no thinking-effort directive (compaction wants
        # the shortest path to an answer) and no response format.  It still
        # routes through _parse_params so the token-budget key matches the
        # model family — a non-DeepSeek reasoning model rejects max_tokens.
        kwargs = self._parse_params(
            model,
            max_completion_tokens=1024,
            reasoning_effort=None,
            response_format=None,
            temperature=0.0,
        )
        response = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            **kwargs,
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
        *,
        model: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Yield :class:`StreamEvent` items from an SSE stream."""
        yield StreamEvent(type="message_start", data={})

        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=openai_messages,
                tools=openai_tools,
                **kwargs
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
        *,
        model: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """Return a single :class:`LLMResponse` (no streaming)."""

        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=openai_messages,
                tools=openai_tools,
                **kwargs
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
    def _messages_to_openai(
        messages: list[Message],
        *,
        model: str,
    ) -> list[dict]:
        """Convert a list of internal :class:`Message` objects to the
        list-of-dicts expected by the OpenAI chat-completion endpoint.

        *model* identifies the request's dialect — whether the endpoint
        wants prior reasoning echoed back, see the note on the assistant
        branch.
        """

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

                # DeepSeek echo-back requirement: the reasoning_content of
                # *all previous turns* must be passed back — "even for
                # turns where the model did not perform a tool call" — or
                # the API returns HTTP 400.  It attaches to the request
                # carrying ``tools``, not to the thinking toggle of the
                # current request, so this consults neither: the agent loop
                # always sends tools, and the calls that do not (the plan
                # proposal, compaction) have the key ignored, so one rule
                # covers both regimes.
                #
                # A documented 400 that could not be reproduced against
                # either available model is recorded in
                # ``docs/plans/model-selection.md``; the echo stays because
                # a redundant key is inert while an omitted one is a
                # documented failure.
                #
                # The dialect test is the same ``deepseek-`` family test
                # :meth:`_parse_params` applies to the token-budget key.
                # Another endpoint with its own dialect would extend it
                # here.  Self-scoping in the ordinary case: only a dialect
                # that emits reasoning_content ever produces a block to
                # replay, and the plain dict rides through the OpenAI SDK
                # unvalidated.
                reasoning = msg.reasoning
                if reasoning:
                    if model.lower().startswith("deepseek-"):
                        openai_msg["reasoning_content"] = reasoning
                    else:
                        # Reasoning from a dialect we cannot echo to.  Only
                        # a model change can produce this: the in-flight
                        # round is always the serving endpoint's own, so
                        # the blocks here are another model's.  Dropping
                        # them silently would hide a real mismatch — the
                        # endpoint either rejects the key or absorbs
                        # reasoning it never wrote — so fail loudly and
                        # name the model, and log it too in case the caller
                        # swallows the exception into a recoverable error.
                        logger.error(
                            "No reasoning echo dialect for model %r — the "
                            "request carries reasoning_content written by "
                            "another model.",
                            model,
                        )
                        raise NotImplementedError(
                            f"reasoning echo is not implemented for model "
                            f"{model!r}; it carries another model's "
                            f"reasoning, which this endpoint cannot "
                            f"receive."
                        )

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
    def _parse_params(
        model: str,
        *,
        max_completion_tokens: int,
        reasoning_effort: str | None,
        response_format: dict | None,
        temperature: float,
    ) -> dict:
        """Translate our parameter names into *model*'s wire format.

        The two API families disagree on both the token-budget key and the
        thinking knob, so the shape is resolved here instead of at the call
        sites:

        * ``deepseek-*`` — the older ``max_tokens``, and
          ``reasoning_effort`` collapsed onto DeepSeek's ``low``/``high``/
          ``max`` tiers (``"none"`` disables thinking outright, which is a
          separate ``thinking`` field rather than an effort tier).
        * everything else — ``max_completion_tokens``, which the OpenAI
          reasoning models require; ``max_tokens`` is rejected there.
          ``reasoning_effort`` passes through untranslated.

        A ``None`` ``reasoning_effort`` or ``response_format`` omits the
        field, leaving the endpoint's own default in place.
        """
        kwargs: dict = {"temperature": temperature}
        if model.lower().startswith("deepseek-"):
            kwargs["max_tokens"] = max_completion_tokens
            if reasoning_effort is not None:
                if reasoning_effort == "none":
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                else:
                    tier = reasoning_effort.lower()
                    kwargs["reasoning_effort"] = (
                        _DEEPSEEK_REASON_EFFORT_MAP.get(tier, "max")
                    )
            if response_format is not None:
                rf_type = response_format.get("type", "")
                if rf_type == "json_schema":
                    # No guided-decoding mode — ask for any JSON object and
                    # let the caller validate it against the schema.
                    kwargs["response_format"] = {"type": "json_object"}
                elif rf_type in ("text", "json_object"):
                    kwargs["response_format"] = {"type": rf_type}
                else:
                    logger.warning(
                        "Ignoring unsupported response_format type %r for "
                        "%s — sending the request without a format hint",
                        rf_type, model,
                    )
        else:
            kwargs["max_completion_tokens"] = max_completion_tokens
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
            if response_format is not None:
                kwargs["response_format"] = response_format
        return kwargs

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
