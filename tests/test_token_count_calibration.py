"""Tests for token count calibration — API-aware context window.

Verifies that ContextWindowManager uses API-reported baseline + tiktoken
delta estimation instead of estimating the full message list every time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from toddler.context.manager import ContextManager
from toddler.context.window import ContextWindowManager
from toddler.llm.base import BaseLLMProvider
from toddler.llm import ContentBlock, LLMResponse, Message, TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(role: str, text: str) -> Message:
    """Create a simple single-block text message."""
    if role == "system":
        return Message.system(text)
    if role == "user":
        return Message.user(text)
    if role == "assistant":
        return Message.assistant([ContentBlock.text_block(text)])
    if role == "tool":
        return Message.tool([ContentBlock.tool_result_block("id1", text)])
    raise ValueError(f"Unknown role: {role}")


def _token_estimate(messages: list[Message]) -> int:
    """Full tiktoken estimate for *messages* — the ground-truth reference."""
    from toddler.context.token_counter import TokenCounter
    return TokenCounter(model="gpt-4").count_messages(messages)


# ============================================================================
# Unit tests: ContextWindowManager baseline tracking
# ============================================================================


class TestContextWindowManagerBaseline:
    """Tests for set_baseline / reset_baseline / count_tokens with delta."""

    @pytest.fixture
    def wm(self) -> ContextWindowManager:
        return ContextWindowManager(
            "gpt-4",
            max_context_length=128_000,
        )

    def test_count_tokens_no_baseline_falls_back_to_full_estimate(self, wm):
        """Without a baseline, count_tokens estimates the entire list."""
        msgs = [
            _make_msg("system", "You are helpful."),
            _make_msg("user", "Hello."),
        ]
        result = wm.count_tokens(msgs)
        expected = _token_estimate(msgs)
        assert result == expected

    def test_count_tokens_with_baseline_only_estimates_new_messages(self, wm):
        """With a baseline, only the messages after the baseline are estimated."""
        msgs = [
            _make_msg("system", "You are helpful."),
            _make_msg("user", "First question."),
            _make_msg("assistant", "First answer."),
        ]
        # Simulate API reporting these 3 messages as 500 tokens.
        wm.set_baseline(total_tokens=500, message_count=3)

        # Add one more user message.
        msgs.append(_make_msg("user", "Follow-up."))
        result = wm.count_tokens(msgs)

        # Delta should only cover the 4th message.
        delta = _token_estimate([msgs[3]])
        expected = 500 + delta
        assert result == expected

    def test_baseline_clears_on_reset(self, wm):
        """After reset_baseline, count_tokens falls back to full estimate."""
        msgs = [
            _make_msg("system", "System."),
            _make_msg("user", "Hi."),
        ]
        wm.set_baseline(total_tokens=200, message_count=2)
        wm.reset_baseline()

        result = wm.count_tokens(msgs)
        expected = _token_estimate(msgs)
        assert result == expected

    def test_count_tokens_with_tool_results_delta(self, wm):
        """Baseline + tool results are estimated as delta."""
        msgs = [
            _make_msg("system", "You are helpful."),
            _make_msg("user", "Search for X."),
            _make_msg("assistant", "I'll search."),
        ]
        wm.set_baseline(total_tokens=400, message_count=3)

        # Tool result message added.
        msgs.append(_make_msg("tool", "Found: X is 42."))
        result = wm.count_tokens(msgs)

        delta = _token_estimate([msgs[3]])
        expected = 400 + delta
        assert result == expected

    def test_messages_shorter_than_baseline_guards(self, wm):
        """When messages shrink below baseline (shouldn't happen), we fall back."""
        msgs = [
            _make_msg("system", "System."),
            _make_msg("user", "Hi."),
            _make_msg("assistant", "Hey."),
        ]
        # Baseline says 5 messages — but we only have 3.
        wm.set_baseline(total_tokens=500, message_count=5)

        result = wm.count_tokens(msgs)
        # Should fall back to full estimate.
        expected = _token_estimate(msgs)
        assert result == expected

    def test_zero_baseline_is_no_baseline(self, wm):
        """A zero total_tokens baseline is treated as "no baseline"."""
        msgs = [
            _make_msg("system", "System."),
            _make_msg("user", "Hi."),
        ]
        wm.set_baseline(total_tokens=0, message_count=2)

        result = wm.count_tokens(msgs)
        # total_tokens=0 → treated as no baseline → full estimate.
        expected = _token_estimate(msgs)
        assert result == expected


# ============================================================================
# Unit tests: ContextManager.record_usage
# ============================================================================


class TestContextManagerRecordUsage:
    """Tests for record_usage feeding API counts into the window manager."""

    @pytest.fixture
    def ctx(self) -> ContextManager:
        from toddler.config.settings import Settings
        class _CtxMockProvider(BaseLLMProvider):
            @property
            def model(self) -> str:
                return "gpt-4"
            async def generate(self, messages, tools, *, max_tokens=4096,
                               temperature=0.0, stream=True):
                raise NotImplementedError
            async def generate_compact(self, prompt: str) -> str:
                raise NotImplementedError

        return ContextManager(Settings(), _CtxMockProvider())

    def test_record_usage_sets_baseline(self, ctx):
        """After record_usage, the window manager has a baseline."""
        msgs = [
            _make_msg("system", "System."),
            _make_msg("user", "Q1."),
            _make_msg("assistant", "A1."),
            _make_msg("user", "Q2."),
            _make_msg("assistant", "A2."),
        ]
        ctx.load(msgs[:3])  # 3 messages loaded from storage

        # Simulate an API call that returned usage=600 for the full buffer
        # after we appended the assistant response.
        ctx.load([])
        ctx._messages = list(msgs)  # 5 messages in buffer
        ctx.record_usage(TokenUsage(input_tokens=500, output_tokens=100))

        # Now count tokens for the current buffer.
        result = ctx._window_mgr.count_tokens(ctx._messages)
        # Exact baseline (600) + no new messages → 600.
        assert result == 600

    def test_record_usage_skips_zero_usage(self, ctx):
        """Usage with total=0 is ignored (API call failed)."""
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q."),
        ]
        ctx.load(msgs)
        # Record zero usage — should not set baseline.
        ctx.record_usage(TokenUsage(input_tokens=0, output_tokens=0))

        before = ctx._window_mgr.count_tokens(ctx._messages)
        # Should still be working with full estimates (no baseline set).
        assert before == _token_estimate(msgs)

    def test_load_resets_baseline(self, ctx):
        """Loading new messages resets the token baseline."""
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q."),
            _make_msg("assistant", "A."),
        ]
        ctx.load(msgs)
        ctx.record_usage(TokenUsage(input_tokens=200, output_tokens=100))

        # Load fresh — baseline should be reset.
        ctx.load([])
        # With baseline reset, count_tokens() estimates everything.
        # An empty list still has priming tokens (3 by OpenAI's formula).
        result = ctx._window_mgr.count_tokens([])
        assert result == _token_estimate([])
        # Verify baseline is cleared.
        assert ctx._window_mgr._baseline_total_tokens == 0

    def test_record_usage_then_new_message_adds_delta(self, ctx):
        """Baseline set, then new user message → only delta estimated."""
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q1."),
            _make_msg("assistant", "A1."),
        ]
        ctx.load(msgs)
        ctx.record_usage(TokenUsage(input_tokens=200, output_tokens=50))

        # New user message added.
        ctx._messages.append(_make_msg("user", "Q2."))
        result = ctx._window_mgr.count_tokens(ctx._messages)

        # Baseline = 250. Delta = estimate of just the new user message.
        delta = _token_estimate([_make_msg("user", "Q2.")])
        expected = 250 + delta
        assert result == expected


# ============================================================================
# Integration test: AgentLoop passes usage to context
# ============================================================================


class TestAgentLoopRecordUsageIntegration:
    """Verify that AgentLoop.run() calls ctx.record_usage(usage)."""

    async def test_record_usage_called_after_assistant_append(self):
        """After a successful LLM call, the context baseline is updated."""
        from toddler.agent.loop import AgentLoop
        from toddler.config.settings import Settings
        from toddler.tools.executor import ToolExecutor
        from toddler.tools.registry import ToolRegistry

        # Mock LLM that returns a single text response.
        class SingleResponseProvider(BaseLLMProvider):
            @property
            def model(self) -> str:
                return "gpt-4"

            async def generate(self, messages, tools, *, max_tokens=4096,
                               temperature=0.0, stream=True):
                return LLMResponse(
                    messages=[Message.assistant([
                        ContentBlock.text_block("Hello!"),
                    ])],
                    stop_reason="end_turn",
                    usage=TokenUsage(input_tokens=100, output_tokens=10),
                )

            async def generate_compact(self, prompt: str) -> str:
                raise NotImplementedError

        settings = Settings()
        provider = SingleResponseProvider()
        ctx = ContextManager(settings, provider)
        loop = AgentLoop(
            provider, ToolRegistry(), ToolExecutor(ToolRegistry()),
            settings, context=ctx,
        )

        # Drain the run.
        events = []
        async for event in loop.run("Hi!", max_iterations=1):
            events.append(event)

        # The context should have a baseline set.
        result = ctx._window_mgr.count_tokens(ctx._messages)
        # Baseline should be 110 (100 input + 10 output).
        assert result == 110

    async def test_record_usage_tracks_multiple_turns(self):
        """Each LLM response updates the baseline for incremental growth."""
        from toddler.agent.loop import AgentLoop
        from toddler.config.settings import Settings
        from toddler.tools.base import BaseTool, Permission, ToolResult
        from toddler.tools.executor import ToolExecutor
        from toddler.tools.registry import ToolRegistry

        # Simulates: first call tool_use → second call end_turn.
        class MultiTurnProvider(BaseLLMProvider):
            @property
            def model(self) -> str:
                return "gpt-4"

            async def generate_compact(self, prompt: str) -> str:
                raise NotImplementedError

            def __init__(self):
                super().__init__()
                self._call = 0

            async def generate(self, messages, tools, *, max_tokens=4096,
                               temperature=0.0, stream=True):
                self._call += 1
                if self._call == 1:
                    return LLMResponse(
                        messages=[Message.assistant([
                            ContentBlock.tool_use_block(
                                "c1", "echo", {"message": "test"},
                            ),
                        ])],
                        stop_reason="tool_use",
                        usage=TokenUsage(input_tokens=150, output_tokens=20),
                    )
                return LLMResponse(
                    messages=[Message.assistant([
                        ContentBlock.text_block("Done."),
                    ])],
                    stop_reason="end_turn",
                    usage=TokenUsage(input_tokens=300, output_tokens=5),
                )

        class EchoTool(BaseTool):
            name = "echo"
            description = "Echo"
            parameters = {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
            }

            async def execute(self, **kwargs) -> ToolResult:
                return ToolResult(
                    tool_id="", tool_name="echo",
                    success=True, output=f"Echo: {kwargs.get('message', '')}",
                )

            @property
            def permission(self) -> Permission:
                return Permission.READ

        registry = ToolRegistry()
        registry.register(EchoTool())

        settings = Settings()
        provider = MultiTurnProvider()
        ctx = ContextManager(settings, provider)
        loop = AgentLoop(
            provider, registry, ToolExecutor(registry), settings, context=ctx,
        )

        events = []
        async for event in loop.run("Echo test", max_iterations=5):
            events.append(event)

        # After the second LLM call, baseline should be the second API's
        # usage.total.  The second call gets messages:
        # [sys, user, assistant(tool_use), tool_result]
        # usage = TokenUsage(input_tokens=300, output_tokens=5) → total = 305.
        baseline = ctx._window_mgr._baseline_total_tokens
        assert baseline == 305


# ============================================================================
# Regression: count_tokens still works for truncation after baseline set
# ============================================================================


class TestTruncationWithBaseline:
    """Truncation resets baseline so subsequent count_tokens is accurate."""

    def test_truncation_resets_baseline_via_load(self):
        """When messages are structurally changed, baseline resets."""
        wm = ContextWindowManager("gpt-4", max_context_length=1000)
        msgs = [
            _make_msg("system", "Sys."),
            _make_msg("user", "Q."),
            _make_msg("assistant", "A."),
        ]
        wm.set_baseline(total_tokens=500, message_count=3)
        wm.reset_baseline()

        # After reset, count_tokens should estimate from scratch.
        result = wm.count_tokens(msgs)
        assert result == _token_estimate(msgs)
