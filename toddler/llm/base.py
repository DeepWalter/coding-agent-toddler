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
    # Properties
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def max_context_length(self) -> int:
        """Maximum context length in tokens for the active model."""
        ...

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = True,
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
        max_tokens:
            Maximum tokens the model is allowed to produce in its response.
        temperature:
            Sampling temperature (0.0 = deterministic).
        stream:
            When ``True`` yields :class:`StreamEvent` items; when
            ``False`` awaits and returns a single :class:`LLMResponse`.
        """
        ...

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    @abstractmethod
    def count_tokens(self, messages: list[Message]) -> int:
        """Count the total tokens consumed by *messages*.

        Used by the context window manager to decide when compaction or
        truncation is needed.
        """
        ...

    # ------------------------------------------------------------------
    # Compaction helper
    # ------------------------------------------------------------------

    @abstractmethod
    async def generate_compact(self, prompt: str) -> str:
        """Generate a compaction summary — non-streaming, single-turn.

        Parameters
        ----------
        prompt:
            A pre-formatted prompt that asks the model to summarize
            conversation history.  The provider wraps it as a user
            message and returns the model's text response.

        Returns
        -------
        str
            The summarized text produced by the model.
        """
        ...
