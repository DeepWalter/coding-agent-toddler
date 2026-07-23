"""Tests for the AgentLoop — core tool-calling orchestration.

Uses a mock LLM provider and a simple echo tool to verify the loop handles
text responses, tool calls, error recovery, permission gating, and stop
conditions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

from toddler.agent.events import (
    AgentError,
    AgentFinished,
    AgentPaused,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.loop import AgentLoop
from toddler.config.settings import Settings
from toddler.context.conversation_context import ConversationContext
from toddler.context.system_prompt import SystemPromptBuilder
from toddler.llm.base import BaseLLMProvider
from toddler.llm.types import ContentBlock, LLMResponse, Message, StreamEvent, TokenUsage
from toddler.tools.base import BaseTool, Permission, ToolResult
from toddler.tools.executor import ToolExecutor
from toddler.tools.registry import ToolRegistry

if TYPE_CHECKING:
    pass

# ============================================================================
# Mock LLM Provider
# ============================================================================


class MockLLMProvider(BaseLLMProvider):
    """A controllable LLM provider for testing the agent loop.

    Pre-load with a list of :class:`LLMResponse` objects — each call to
    :meth:`generate` consumes the next one.  When the list is exhausted
    a default "Done." text response is returned.
    """

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses: list[LLMResponse] = responses or []
        self.call_count: int = 0
        self.messages_history: list[list[Message]] = []

    @property
    def max_context_length(self) -> int:
        return 128_000

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent] | LLMResponse:
        self.messages_history.append(messages)
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        # Default: plain text end-turn.
        self.call_count += 1
        return LLMResponse(
            messages=[Message.assistant([ContentBlock.text_block("Done.")])],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(
            len(getattr(b, "text", "") or "") // 4
            for m in messages
            for b in m.content
        )

    async def generate_compact(self, prompt: str) -> str:
        return "[compacted]"


# ============================================================================
# Helper factories
# ============================================================================


def _make_llm_response(
    text: str = "",
    tool_blocks: list[ContentBlock] | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> LLMResponse:
    """Build an :class:`LLMResponse` with minimal boilerplate."""
    blocks: list[ContentBlock] = []
    if text:
        blocks.append(ContentBlock.text_block(text))
    if tool_blocks:
        blocks.extend(tool_blocks)
    return LLMResponse(
        messages=[Message.assistant(blocks)],
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_tool_use_block(
    tool_id: str, tool_name: str, tool_input: dict | None = None
) -> ContentBlock:
    """Shortcut for creating a tool_use content block."""
    return ContentBlock.tool_use_block(tool_id, tool_name, tool_input or {})


# ============================================================================
# Simple echo tool (READ permission — no confirmation)
# ============================================================================


class EchoTool(BaseTool):
    """A read-only tool that echoes back its input."""

    name = "echo"
    description = "Echo back the message"
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to echo"}
        },
        "required": ["message"],
    }

    async def execute(self, **kwargs) -> ToolResult:
        msg = kwargs.get("message", "")
        return ToolResult(
            tool_id="",
            tool_name="echo",
            success=True,
            output=f"Echo: {msg}",
        )

    @property
    def permission(self) -> Permission:
        return Permission.READ


class FailingTool(BaseTool):
    """A tool that always fails — for testing error recovery."""

    name = "failing"
    description = "Always fails"
    parameters = {
        "type": "object",
        "properties": {},
    }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            tool_id="",
            tool_name="failing",
            success=False,
            output="",
            error="Simulated failure",
        )

    @property
    def permission(self) -> Permission:
        return Permission.READ


class WriteTool(BaseTool):
    """A mock WRITE-permission tool — triggers confirmation gating."""

    name = "write_stuff"
    description = "Write something somewhere"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Content to write"}
        },
        "required": ["content"],
    }

    async def execute(self, **kwargs) -> ToolResult:
        content = kwargs.get("content", "")
        return ToolResult(
            tool_id="",
            tool_name="write_stuff",
            success=True,
            output=f"Wrote: {content}",
        )

    @property
    def permission(self) -> Permission:
        return Permission.WRITE


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def settings() -> Settings:
    """Settings with confirm_write=True (default gating)."""
    return Settings(
        confirm_write=True,
        auto_approve_read=True,
        confirm_shell_dangerous=True,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(FailingTool())
    reg.register(WriteTool())
    return reg


@pytest.fixture
def executor(registry, settings) -> ToolExecutor:
    """Executor that auto-approves — gating is handled by AgentLoop."""
    async def _always_approve(tool, params, perm) -> bool:  # noqa: ARG001
        return True

    return ToolExecutor(
        registry,
        settings,
        confirm_cb=_always_approve,
    )


@pytest.fixture
def conv_ctx() -> ConversationContext:
    """Bare ConversationContext for tests (no backing session)."""
    return ConversationContext(
        session_mgr=None,
        prompt_builder=SystemPromptBuilder(),
    )


@pytest.fixture
def loop(registry, executor, settings, conv_ctx) -> AgentLoop:
    """AgentLoop with mock LLM, echo & failing tools, default settings."""
    return AgentLoop(
        llm_provider=MockLLMProvider(),
        tool_registry=registry,
        tool_executor=executor,
        settings=settings,
        context=conv_ctx,
    )


# ============================================================================
# Helper — collect all events from a run
# ============================================================================


async def _collect_events(gen: AsyncIterator) -> list:
    """Drain an async generator into a list."""
    events = []
    async for event in gen:
        events.append(event)
    return events


# ============================================================================
# Tests: simple text response
# ============================================================================


class TestSimpleTextResponse:
    """Agent loop with a plain-text (end_turn) LLM response."""

    async def test_single_text_response(self, registry, executor, settings, conv_ctx):
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    text="Hello, world!",
                    stop_reason="end_turn",
                ),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Hi!"))

        assert len(events) >= 2
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "Hello, world!"
        assert isinstance(events[-1], AgentFinished)
        assert events[-1].reason == "LLM finished its turn."

    async def test_no_text_response(self, registry, executor, settings, conv_ctx):
        """LLM returns end_turn with no text content."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(text="", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Hi!"))

        # No TextDelta when text is empty.
        assert not any(isinstance(e, TextDelta) for e in events)
        assert isinstance(events[-1], AgentFinished)


# ============================================================================
# Tests: tool calls
# ============================================================================


class TestToolCalls:
    """Agent loop with tool_use LLM responses."""

    async def test_single_tool_call(self, registry, executor, settings, conv_ctx):  # noqa: E501
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "call_1", "echo", {"message": "hello"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                # Second call: LLM sees tool result and finishes.
                _make_llm_response(
                    text="Got your echo!",
                    stop_reason="end_turn",
                ),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Echo please"))

        # Should have: ToolCallStart, ToolCallEnd, TextDelta, AgentFinished
        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        texts = [e for e in events if isinstance(e, TextDelta)]
        finishes = [e for e in events if isinstance(e, AgentFinished)]

        assert len(starts) == 1
        assert starts[0].tool_name == "echo"

        assert len(ends) == 1
        assert ends[0].result is not None
        assert ends[0].result.success
        assert "Echo: hello" in ends[0].result.output

        assert len(texts) == 1

        assert len(finishes) == 1
        assert "LLM finished" in finishes[0].reason

        # LLM should have been called twice.
        assert llm.call_count == 2

    async def test_multiple_tool_calls_in_one_response(self, registry, executor, settings, conv_ctx):
        """LLM requests multiple tools in one response."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "echo", {"message": "first"}
                        ),
                        _make_tool_use_block(
                            "c2", "echo", {"message": "second"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="All done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Double echo"))

        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]

        assert len(starts) == 2
        assert len(ends) == 2


# ============================================================================
# Tests: error recovery
# ============================================================================


class TestErrorRecovery:
    """Tool errors are fed back to the LLM as is_error=True."""

    async def test_failing_tool_error_fed_back(self, registry, executor, settings, conv_ctx):
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block("c1", "failing", {}),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="I'll try something else", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Do something"))

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "Simulated failure" in ends[0].result.error

        # Verify the LLM received the error in its second call.
        assert llm.call_count == 2
        second_call_msgs = llm.messages_history[1]
        tool_msgs = [m for m in second_call_msgs if m.role == "tool"]
        assert len(tool_msgs) == 1
        tool_block = tool_msgs[0].content[0]
        assert tool_block.is_error is True
        assert "Simulated failure" in tool_block.tool_result_content

    async def test_unknown_tool_error(self, registry, executor, settings, conv_ctx):
        """Calling a tool not in the registry should produce an error result."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block("c1", "nonexistent", {}),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Ok, I'll adapt", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Try unknown tool"))

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "Unknown tool" in ends[0].result.error

    async def test_llm_call_error(self, registry, executor, settings, conv_ctx):
        """When the LLM call itself raises, AgentError + AgentFinished are yielded."""

        class FailingLLM(MockLLMProvider):
            async def generate(self, messages, tools, *, max_tokens=4096, temperature=0.0, stream=True):
                raise RuntimeError("API connection lost")

        loop = AgentLoop(FailingLLM(), registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Hi"))

        errors = [e for e in events if isinstance(e, AgentError)]
        finishes = [e for e in events if isinstance(e, AgentFinished)]

        assert len(errors) == 1
        assert "API connection lost" in errors[0].message
        assert errors[0].recoverable is True
        assert len(finishes) == 1


# ============================================================================
# Tests: permission gating
# ============================================================================


class TestPermissionGating:
    """AgentPaused is yielded when a WRITE tool needs confirmation."""

    async def test_write_tool_yields_agent_paused(self, registry, executor, settings, conv_ctx):
        """A WRITE tool with confirm_write=True should pause the loop."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "write_stuff", {"content": "important data"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Written!", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)

        gen = loop.run("Write this down")
        events: list = []

        # Manually step through to handle the async confirmation.
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                # External code approves
                await loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 1
        assert "write_stuff" in paused[0].prompt
        assert paused[0].choices == ["approve", "deny"]

        # Tool should have executed after approval.
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert ends[0].result.success
        assert "Wrote:" in ends[0].result.output

    async def test_deny_write_tool(self, registry, executor, settings, conv_ctx):
        """Denying a WRITE tool produces an error result."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "write_stuff", {"content": "nope"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Ok, I won't write", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)

        gen = loop.run("Write this")
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                await loop.deny_tool_call()

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "denied" in ends[0].result.error.lower()

    async def test_read_tool_auto_approves(self, registry, executor, settings, conv_ctx):
        """READ tools should not trigger AgentPaused."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "echo", {"message": "test"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Echo test"))

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 0  # READ auto-approves


# ============================================================================
# Tests: stop conditions
# ============================================================================


class TestStopConditions:
    """AgentLoop respects max_iterations."""

    async def test_max_iterations_stops_loop(self, registry, executor, settings, conv_ctx):
        """When the LLM keeps requesting tools, max_iterations caps it."""
        # Each response requests a tool, so the loop will keep going.
        tool_response = _make_llm_response(
            tool_blocks=[
                _make_tool_use_block("c1", "echo", {"message": "looping"}),
            ],
            stop_reason="tool_use",
        )

        llm = MockLLMProvider(responses=[tool_response] * 10)
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(
            loop.run("Loop forever", max_iterations=3)
        )

        finishes = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finishes) == 1
        assert "max_iterations" in finishes[0].reason.lower() or \
            "maximum iterations" in finishes[0].reason.lower()

    async def test_tool_use_with_empty_calls_stops(self, registry, executor, settings, conv_ctx):
        """LLM says tool_use but provides no tool blocks — should stop."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[],  # empty
                    stop_reason="tool_use",
                ),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Do something"))

        finishes = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finishes) == 1
        assert "no tool calls" in finishes[0].reason.lower()


# ============================================================================
# Tests: conversation context
# ============================================================================


class TestConversationContext:
    """The LLM receives the full conversation history."""

    async def test_tool_results_appear_in_next_call(self, registry, executor, settings, conv_ctx):
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "echo", {"message": "ctx test"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Complete", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        await _collect_events(loop.run("Test context"))

        # Second call should include: system, user, assistant (tool_use), tool
        second_msgs = llm.messages_history[1]
        roles = [m.role for m in second_msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    async def test_system_prompt_is_built_by_context(self, registry, executor, settings, conv_ctx):  # noqa: E501
        """The system prompt is assembled by ConversationContext.prepare_turn()."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(text="Ok", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        await _collect_events(loop.run("Hi"))

        first_msgs = llm.messages_history[0]
        sys_msg = first_msgs[0]
        assert sys_msg.role == "system"
        # Should contain base persona (not empty).
        assert "Toddler" in sys_msg.text


# ============================================================================
# Tests: multiple iterations
# ============================================================================


class TestMultiIteration:
    """The loop correctly chains multiple LLM → tool → LLM rounds."""

    async def test_three_rounds(self, registry, executor, settings, conv_ctx):
        """Three rounds of tool calls before finishing."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block("r1", "echo", {"message": "a"}),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block("r2", "echo", {"message": "b"}),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Final answer", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx)
        events = await _collect_events(loop.run("Multi-round"))

        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        texts = [e for e in events if isinstance(e, TextDelta)]

        assert len(starts) == 2  # two rounds of tool calls
        assert len(ends) == 2
        assert len(texts) == 1  # final text response
        assert llm.call_count == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
