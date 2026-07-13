"""Stop-condition checker for the agent loop.

Tracks iteration count and token usage against configured limits so the
agent loop knows when to stop — whether because the LLM finished its turn,
the iteration cap was hit, or a token budget was exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toddler.llm.types import TokenUsage

# ---------------------------------------------------------------------------
# Stop reason
# ---------------------------------------------------------------------------


@dataclass
class StopReason:
    """Why the agent loop should stop (or has stopped)."""

    type: Literal["end_turn", "max_iterations", "token_budget", "error"]
    message: str


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class StopConditionChecker:
    """Tracks loop progress and decides when to stop.

    Parameters
    ----------
    max_iterations:
        Maximum number of LLM round-trips before forcing a stop.
    token_budget:
        Optional hard cap on total tokens consumed.  *None* disables the
        budget check.
    """

    def __init__(
        self,
        max_iterations: int = 50,
        token_budget: int | None = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._token_budget = token_budget
        self.iteration: int = 0
        self._total_tokens: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed so far across all iterations."""
        return self._total_tokens

    @property
    def is_exhausted(self) -> bool:
        """True when either the iteration cap or token budget is hit."""
        return self.iteration >= self._max_iterations or (
            self._token_budget is not None
            and self._total_tokens >= self._token_budget
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def increment(self) -> StopReason | None:
        """Advance the iteration counter.

        Returns a :class:`StopReason` when a hard limit is reached,
        otherwise *None*.
        """
        self.iteration += 1

        if self.iteration > self._max_iterations:
            return StopReason(
                type="max_iterations",
                message=(
                    f"Reached maximum iterations ({self._max_iterations}). "
                    f"The task may be too complex or the agent may be stuck "
                    f"in a loop."
                ),
            )

        if (
            self._token_budget is not None
            and self._total_tokens >= self._token_budget
        ):
            return StopReason(
                type="token_budget",
                message=(
                    f"Exceeded token budget ({self._token_budget:,} tokens). "
                    f"Consider breaking the task into smaller steps."
                ),
            )

        return None

    def add_tokens(self, usage: TokenUsage | int) -> None:
        """Record token consumption from an LLM call.

        Accepts either a :class:`TokenUsage` object (summing input + output)
        or a plain integer.
        """
        if isinstance(usage, TokenUsage):
            self._total_tokens += usage.total
        else:
            self._total_tokens += usage

    # ------------------------------------------------------------------
    # LLM stop-reason evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def from_llm_stop_reason(stop_reason: str) -> StopReason | None:
        """Convert an LLM stop-reason string into a :class:`StopReason`.

        Returns *None* when the LLM wants to continue (i.e. it emitted
        tool calls that need executing).
        """
        if stop_reason == "end_turn":
            return StopReason(
                type="end_turn",
                message="LLM finished its turn.",
            )
        if stop_reason == "tool_use":
            # Not a stop — the loop should execute tools and continue.
            return None
        # max_tokens, stop_sequence, or anything unexpected
        return StopReason(
            type="error",
            message=f"LLM stopped unexpectedly: {stop_reason}",
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset counters for a fresh run."""
        self.iteration = 0
        self._total_tokens = 0
