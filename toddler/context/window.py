"""Context window manager — token tracking, compaction triggers, truncation.

Monitors the token count of in-flight messages against the model's context
limit and triggers compaction (LLM summarisation of old turns) or emergency
truncation when the conversation grows too large.
"""

from __future__ import annotations

import logging

from toddler.context.token_counter import TokenCounter
from toddler.llm import Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety margins (tokens reserved for the model's response)
# ---------------------------------------------------------------------------
# The model needs headroom to produce a reply.  We reserve a portion of the
# context window for the assistant's output so that compaction/truncation
# kicks in *before* the model runs out of generation budget.
_DEFAULT_OUTPUT_HEADROOM = 4096

# Minimum number of messages to keep after truncation (system + last N).
_MIN_KEEP_MESSAGES = 4


class ContextWindowManager:
    """Tracks token usage and triggers compaction / truncation.

    Parameters
    ----------
    model:
        The model name string (e.g. ``"gpt-4"``) — used to resolve the
        tiktoken encoding.
    max_context_length:
        The model's maximum context window size in tokens.
    compaction_threshold:
        Fraction of the context window (0.0-1.0) at which compaction is
        triggered. The default (0.8) means compaction starts when tokens
        exceed 80 % of the context window.
    output_headroom:
        Tokens reserved for the model's next response.  The effective
        "full" ceiling is ``max_context_length - output_headroom``.
    """

    def __init__(
        self,
        model: str,
        max_context_length: int,
        *,
        compaction_threshold: float = 0.8,
        output_headroom: int = _DEFAULT_OUTPUT_HEADROOM,
    ) -> None:
        self._token_counter = TokenCounter(model=model)
        self._max_context_length = max_context_length
        self._compaction_threshold = compaction_threshold
        self._output_headroom = output_headroom

        # Baseline tracking — set after each API call via set_baseline().
        # When zero, count_tokens() falls back to a full tiktoken estimate.
        self._baseline_total_tokens: int = 0
        self._baseline_message_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def context_limit(self) -> int:
        """The model's advertised context window size (tokens)."""
        return self._max_context_length

    @property
    def effective_limit(self) -> int:
        """Context window minus output headroom — the "full" mark."""
        return max(0, self.context_limit - self._output_headroom)

    @property
    def compaction_threshold(self) -> float:
        return self._compaction_threshold

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def set_baseline(self, *, total_tokens: int, message_count: int) -> None:
        """Set the authoritative baseline after an API call.

        *total_tokens* is ``usage.total`` from the response (input + output).
        *message_count* is ``len(messages)`` AFTER the assistant is appended.
        """
        self._baseline_total_tokens = total_tokens
        self._baseline_message_count = message_count

    def reset_baseline(self) -> None:
        """Invalidate the baseline.

        Called when the message list changes structurally — new conversation
        (``load``), compaction, or truncation.  Subsequent ``count_tokens``
        calls will estimate everything with tiktoken until the next API
        response provides a fresh baseline.
        """
        self._baseline_total_tokens = 0
        self._baseline_message_count = 0

    def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens: actual baseline + estimated delta.

        When a baseline is available (from a prior API response), only the
        new messages since that API call are estimated via tiktoken.  When
        no baseline exists yet, the full list is estimated.
        """
        if self._baseline_total_tokens == 0:
            # No baseline yet — estimate everything
            return self._token_counter.count_messages(messages)

        already_counted = self._baseline_message_count
        if len(messages) < already_counted:
            # Shouldn't happen (reset_baseline is always called on structural
            # changes), but guard defensively.
            return self._token_counter.count_messages(messages)

        if len(messages) == already_counted:
            # No new messages since baseline was set — still exact.
            return self._baseline_total_tokens

        new_msgs = messages[already_counted:]  # only user input + tool results
        delta = self._token_counter.count_messages(new_msgs)
        return self._baseline_total_tokens + delta

    def estimate_remaining(self, messages: list[Message]) -> int:
        """Estimate how many tokens are still available.

        Returns ``effective_limit - current_count`` (clamped to ≥ 0).
        """
        used = self.count_tokens(messages)
        return max(0, self.effective_limit - used)

    def usage_ratio(self, messages: list[Message]) -> float:
        """Return ``used / effective_limit`` (0.0–1.0+)."""
        return self.count_tokens(messages) / max(1, self.effective_limit)

    # ------------------------------------------------------------------
    # Threshold checks
    # ------------------------------------------------------------------

    def should_compact(self, messages: list[Message]) -> bool:
        """Return ``True`` when compaction should be triggered.

        Compaction is triggered when token usage exceeds
        ``compaction_threshold`` of the effective context limit.
        """
        ratio = self.usage_ratio(messages)
        return ratio >= self._compaction_threshold

    def should_truncate(self, messages: list[Message]) -> bool:
        """Return ``True`` when even after compaction tokens still overflow.

        This is the emergency brake — truncation is applied when usage
        exceeds ~95 % of the effective limit.
        """
        return self.usage_ratio(messages) >= 0.95

    def is_over_limit(self, messages: list[Message]) -> bool:
        """Return ``True`` when messages already exceed the effective limit."""
        return self.count_tokens(messages) > self.effective_limit

    # ------------------------------------------------------------------
    # Truncation (last resort)
    # ------------------------------------------------------------------

    def truncate(self, messages: list[Message]) -> list[Message]:
        """Drop the oldest messages until the count fits.

        All leading system messages are always preserved.  After those,
        messages are dropped from the oldest end until the token
        count falls below *effective_limit* or only ``_MIN_KEEP_MESSAGES``
        remain.

        .. warning::

           This is destructive — only call when compaction has already been
           attempted or is not available.
        """
        if not messages:
            return messages

        # Collect all leading system messages (may be more than one).
        system: list[Message] = []
        body_start = 0
        for i, msg in enumerate(messages):
            if msg.role == "system":
                system.append(msg)
                body_start = i + 1
            else:
                break

        # Remaining candidates for truncation.
        body = messages[body_start:]

        # Drop from the oldest end.
        keep_count = len(body)
        while keep_count >= _MIN_KEEP_MESSAGES:
            candidate = system + body[-keep_count:]
            candidate_tokens = self.count_tokens(candidate)
            if candidate_tokens <= self.effective_limit:
                logger.warning(
                    f"Truncation: dropped {len(body) - keep_count} "
                    f"messages ({self.count_tokens(messages)} → "
                    f"{candidate_tokens} tokens)."
                )
                return candidate
            keep_count -= 1

        # Even minimum messages overflow — keep the bare minimum anyway.
        final = system + body[-(_MIN_KEEP_MESSAGES):]
        logger.error(
            f"Truncation could not reduce below effective limit: "
            f"keeping {len(final)} messages anyway "
            f"({self.count_tokens(final)} tokens)."
        )
        return final

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status_line(self, messages: list[Message]) -> str:
        """Return a one-line diagnostic string for logging / display.

        Example: ``"12,340 / 102,400 (12%)"``
        """
        used = self.count_tokens(messages)
        limit = self.effective_limit
        pct = (used / max(1, limit)) * 100
        return f"{used:,} / {limit:,} ({pct:.0f}%)"
