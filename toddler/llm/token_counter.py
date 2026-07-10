"""Token counting via tiktoken with model-aware encoding selection.

OpenAI / DeepSeek models use different tokenizer encodings.  This module
maps model-name prefixes to tiktoken encoding names so callers only need
to pass the model string.
"""

from __future__ import annotations

import json

import tiktoken

from toddler.llm.types import Message

# ---------------------------------------------------------------------------
# Model → tiktoken encoding mapping
# ---------------------------------------------------------------------------
# tiktoken ships three primary encodings:
#   cl100k_base — GPT-4, GPT-4-turbo, GPT-3.5-turbo, text-embedding-ada-002
#   o200k_base  — GPT-4o, GPT-4.1
#   p50k_base   — legacy GPT-3 (davinci, text-davinci-003, …)
#   r50k_base   — legacy GPT-3 (ada, babbage, …)
#
# DeepSeek models are architecturally similar to GPT-4 and use cl100k_base.

_MODEL_ENCODING_MAP: dict[str, str] = {
    # OpenAI — GPT-4 family
    "gpt-4o": "o200k_base",
    "gpt-4.1": "o200k_base",
    "gpt-4": "cl100k_base",
    # OpenAI — GPT-3.5 family
    "gpt-3.5": "cl100k_base",
    # DeepSeek
    "deepseek": "cl100k_base",
}

# Fallback when no model prefix matches.
_DEFAULT_ENCODING = "cl100k_base"

# Per-message framing overhead (OpenAI's formula: 4 tokens / message).
_MESSAGE_OVERHEAD = 4
# Priming token for the final assistant reply.
_REPLY_PRIMING = 3


class TokenCounter:
    """Token counter backed by tiktoken.

    Parameters
    ----------
    model:
        Model name string (e.g. ``"deepseek-v4-pro"``).  Used to select
        the appropriate tiktoken encoding via prefix matching.
    """

    def __init__(self, model: str | None = None) -> None:
        encoding_name = self._resolve_encoding(model)
        self._encoding = tiktoken.get_encoding(encoding_name)
        self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Count tokens in a plain string."""
        return len(self._encoding.encode(text))

    def count_messages(self, messages: list[Message]) -> int:
        """Count total tokens consumed by a list of messages.

        Includes per-message framing overhead (4 tokens/message) and a
        priming token for the assistant reply, matching the formula used
        by OpenAI's dashboard.
        """
        total = 0
        for msg in messages:
            total += _MESSAGE_OVERHEAD
            for block in msg.content:
                total += self._count_block(block)
        total += _REPLY_PRIMING
        return total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_encoding(model: str | None) -> str:
        """Pick a tiktoken encoding name from the model string."""
        if model is None:
            return _DEFAULT_ENCODING
        model_lower = model.lower()
        for prefix, encoding in _MODEL_ENCODING_MAP.items():
            if model_lower.startswith(prefix):
                return encoding
        return _DEFAULT_ENCODING

    def _count_block(self, block) -> int:
        """Count tokens for a single :class:`ContentBlock`."""
        if block.type == "text" and block.text:
            return self.count_tokens(block.text)
        if block.type == "tool_use":
            n = self.count_tokens(block.tool_name or "")
            if block.tool_input:
                n += self.count_tokens(
                    json.dumps(block.tool_input, ensure_ascii=False)
                )
            return n
        if block.type == "tool_result":
            return self.count_tokens(block.tool_result_content or "")
        return 0
