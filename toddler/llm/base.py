"""Base LLM provider abstraction — provider-agnostic interface.

All LLM backends (OpenAI-compatible, Anthropic, local models) implement this
ABC so the agent loop never couples to a specific API protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.llm.messages import Message
    from toddler.llm.responses import LLMResponse, StreamEvent


class BaseLLMProvider(ABC):
    """Provider-agnostic interface for LLM backends.

    Implementations handle a specific API protocol (OpenAI-compatible,
    Anthropic, etc.) and normalise into Toddler's internal types.
    """

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    @abstractmethod
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
        """Send messages to the LLM and return the response.

        When ``stream=True`` (the default), returns an async iterator of
        :class:`StreamEvent` objects that the agent loop consumes in
        real-time.  When ``stream=False``, returns a complete
        :class:`LLMResponse` — useful for compaction, titling, and other
        quick one-shot calls.

        Parameters
        ----------
        messages:
            The conversation history.  The provider is responsible for
            converting these into the wire format expected by the API
            (e.g. the OpenAI chat-completion structure).
        tools:
            Tool schemas in OpenAI function-calling format — produced by
            :meth:`BaseTool.to_api_schema()
            <toddler.tools.base.BaseTool.to_api_schema>`.  An empty list
            means no tools are available for this call.
        model:
            The model to serve this request.  Required: the provider has no
            identity of its own, so a missing model is a programming error
            rather than a silent fall back to a configured default.  One
            agent turn passes the same model on every call it makes.
        max_completion_tokens:
            Maximum tokens the model is allowed to produce in its response.
            Named for the budget itself, not the wire field — providers pick
            the name their endpoint expects (``max_tokens`` vs
            ``max_completion_tokens``).
        reasoning_effort:
            How much thinking the model may spend before answering, on a
            shared tier scale (``"minimal"`` … ``"max"``); ``"none"``
            disables thinking.  Providers map it onto their own scale, and
            ``None`` leaves the endpoint's default in place.
        response_format:
            Requested output format, e.g. ``{"type": "json_object"}``.
            Providers without a matching mode drop the hint (and warn when
            the type is unrecognised) rather than failing the call.
        stream:
            When ``True`` yields :class:`StreamEvent` items; when
            ``False`` awaits and returns a single :class:`LLMResponse`.
        temperature:
            Sampling temperature (0.0 = deterministic).  Ignored by
            endpoints that fix it in thinking mode.
        """
        ...

    # ------------------------------------------------------------------
    # Compaction helper
    # ------------------------------------------------------------------

    @abstractmethod
    async def generate_compact(self, prompt: str, *, model: str) -> str:
        """Generate a compaction summary — non-streaming, single-turn.

        Parameters
        ----------
        prompt:
            A pre-formatted prompt that asks the model to summarize
            conversation history.  The provider wraps it as a user
            message and returns the model's text response.
        model:
            The model to summarize with — the conversation's own model,
            so the summary is counted by the same encoding that produced
            its messages.

        Returns
        -------
        str
            The summarized text produced by the model.
        """
        ...
