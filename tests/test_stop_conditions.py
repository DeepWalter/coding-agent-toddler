"""Tests for the stop-condition checker."""

import pytest

from toddler.agent.stop_conditions import StopConditionChecker
from toddler.llm.types import TokenUsage


class TestStopConditionChecker:
    """Unit tests for StopConditionChecker."""

    # ------------------------------------------------------------------
    # Iteration counting
    # ------------------------------------------------------------------

    def test_initial_state(self):
        checker = StopConditionChecker(max_iterations=50)
        assert checker.iteration == 0
        assert checker.total_tokens == 0
        assert not checker.is_exhausted

    def test_increment_advances_iteration(self):
        checker = StopConditionChecker(max_iterations=50)
        result = checker.increment()
        assert result is None
        assert checker.iteration == 1

    def test_increment_returns_none_before_limit(self):
        checker = StopConditionChecker(max_iterations=5)
        for _ in range(5):
            result = checker.increment()
            assert result is None

    def test_increment_stops_at_max_iterations(self):
        checker = StopConditionChecker(max_iterations=3)
        checker.increment()  # 1
        checker.increment()  # 2
        checker.increment()  # 3
        result = checker.increment()  # 4 → exceeds max
        assert result is not None
        assert result.type == "max_iterations"
        assert "3" in result.message

    def test_is_exhausted_after_max_iterations(self):
        checker = StopConditionChecker(max_iterations=2)
        checker.increment()
        checker.increment()
        assert checker.is_exhausted

    # ------------------------------------------------------------------
    # Token budget
    # ------------------------------------------------------------------

    def test_token_budget_not_exhausted_initially(self):
        checker = StopConditionChecker(token_budget=1000)
        assert not checker.is_exhausted

    def test_token_budget_exhausted_when_reached(self):
        checker = StopConditionChecker(token_budget=100)
        checker.add_tokens(100)
        assert checker.is_exhausted

    def test_token_budget_stops_on_increment(self):
        checker = StopConditionChecker(max_iterations=50, token_budget=50)
        checker.add_tokens(60)
        result = checker.increment()
        assert result is not None
        assert result.type == "token_budget"

    def test_add_tokens_with_token_usage_object(self):
        checker = StopConditionChecker()
        usage = TokenUsage(input_tokens=100, output_tokens=50)
        checker.add_tokens(usage)
        assert checker.total_tokens == 150

    def test_add_tokens_with_int(self):
        checker = StopConditionChecker()
        checker.add_tokens(42)
        assert checker.total_tokens == 42

    def test_token_budget_none_is_unlimited(self):
        checker = StopConditionChecker(token_budget=None)
        checker.add_tokens(10_000_000)
        assert not checker.is_exhausted

    # ------------------------------------------------------------------
    # from_llm_stop_reason
    # ------------------------------------------------------------------

    def test_end_turn_returns_stop_reason(self):
        result = StopConditionChecker.from_llm_stop_reason("end_turn")
        assert result is not None
        assert result.type == "end_turn"

    def test_tool_use_returns_none(self):
        result = StopConditionChecker.from_llm_stop_reason("tool_use")
        assert result is None  # loop should continue

    def test_max_tokens_returns_error(self):
        result = StopConditionChecker.from_llm_stop_reason("max_tokens")
        assert result is not None
        assert result.type == "error"

    def test_unknown_stop_reason_returns_error(self):
        result = StopConditionChecker.from_llm_stop_reason("content_filter")
        assert result is not None
        assert result.type == "error"

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def test_reset_clears_counters(self):
        checker = StopConditionChecker(max_iterations=10, token_budget=100)
        checker.increment()
        checker.increment()
        checker.add_tokens(50)

        checker.reset()

        assert checker.iteration == 0
        assert checker.total_tokens == 0
        assert not checker.is_exhausted

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_zero_max_iterations_stops_immediately(self):
        checker = StopConditionChecker(max_iterations=0)
        result = checker.increment()
        assert result is not None
        assert result.type == "max_iterations"

    def test_default_max_iterations(self):
        checker = StopConditionChecker()
        assert not checker.is_exhausted
        # Should have a reasonable default (50 is the module default)
        result = checker.increment()
        assert result is None  # first increment is fine


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
