"""Tests for the AgentLoop — core tool-calling orchestration.

Uses a mock LLM provider and a simple echo tool to verify the loop handles
text responses, tool calls, error recovery, permission gating, and stop
conditions.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.mocks import TEST_TURN_CONFIG, SlowStreamLLM
from toddler.agent.events import (
    AgentError,
    AgentFinished,
    AgentPaused,
    ContentDelta,
    RecoverableAgentError,
    ToolCallEnd,
    ToolCallStart,
    ToolUseStart,
)
from toddler.agent.handler import NonStreamHandler, StreamHandler
from toddler.agent.loop import AgentLoop, _Verdict
from toddler.config.settings import Settings
from toddler.context.manager import ContextManager
from toddler.llm import (
    LLMResponse,
    Message,
    MessageBlock,
    StreamEvent,
    TokenUsage,
)
from toddler.llm.base import BaseLLMProvider
from toddler.tools.base import (
    BaseTool,
    Decision,
    FileScopePolicy,
    Permission,
    PermissionManager,
    PermissionMode,
    ToolCall,
    ToolResult,
)
from toddler.tools.executor import CheckpointCallback, ToolExecutor
from toddler.tools.registry import ToolRegistry

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

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        model: str,
        reasoning_effort: str | None = None,
        max_completion_tokens: int = 4096,
        response_format: dict | None = None,
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
            messages=[Message.assistant([MessageBlock.content_block("Done.")])],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    async def generate_compact(self, prompt: str, *, model: str) -> str:
        return "[compacted]"


# ============================================================================
# Helper factories
# ============================================================================


def _make_llm_response(
    text: str = "",
    tool_blocks: list[MessageBlock] | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> LLMResponse:
    """Build an :class:`LLMResponse` with minimal boilerplate."""
    blocks: list[MessageBlock] = []
    if text:
        blocks.append(MessageBlock.content_block(text))
    if tool_blocks:
        blocks.extend(tool_blocks)
    return LLMResponse(
        messages=[Message.assistant(blocks)],
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_tool_use_block(
    tool_id: str, tool_name: str, tool_input: dict | None = None
) -> MessageBlock:
    """Shortcut for creating a tool_use content block."""
    return MessageBlock.tool_use_block(tool_id, tool_name, tool_input or {})


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


class DangerousTool(BaseTool):
    """A mock SHELL_DANGEROUS-permission tool — always requires confirmation."""

    name = "dangerous_cmd"
    description = "Run a dangerous shell command"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"}
        },
        "required": ["command"],
    }

    async def execute(self, **kwargs) -> ToolResult:
        cmd = kwargs.get("command", "")
        return ToolResult(
            tool_id="",
            tool_name="dangerous_cmd",
            success=True,
            output=f"Ran: {cmd}",
        )

    @property
    def permission(self) -> Permission:
        return Permission.SHELL_DANGEROUS


class RecordingTool(BaseTool):
    """A mock tool that logs its execution, with a settable tier."""

    name = "recording_tool"
    description = "Records that it ran"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Content"}
        },
    }

    def __init__(self, log: list[str], tier: Permission = Permission.WRITE):
        self._log = log
        self._tier = tier

    async def execute(self, **kwargs) -> ToolResult:
        self._log.append("tool")
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Ran: {kwargs.get('content', '')}",
        )

    @property
    def permission(self) -> Permission:
        return self._tier


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def settings() -> Settings:
    """Default settings for agent loop tests."""
    return Settings()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(FailingTool())
    reg.register(WriteTool())
    reg.register(DangerousTool())
    return reg


@pytest.fixture
def executor(registry) -> ToolExecutor:
    """Executor that does not gate — AgentLoop is the only gate."""
    return ToolExecutor(registry)


@pytest.fixture
def conv_ctx() -> ContextManager:
    """Bare ContextManager for tests (no backing session)."""
    ctx = ContextManager(MockLLMProvider(), config=TEST_TURN_CONFIG)
    ctx.load([])
    return ctx


@pytest.fixture
def loop(registry, executor, settings, conv_ctx) -> AgentLoop:
    """AgentLoop with mock LLM, echo & failing tools, default settings."""
    return AgentLoop(
        llm_provider=MockLLMProvider(),
        tool_registry=registry,
        tool_executor=executor,
        settings=settings,
        context=conv_ctx,
        permission_manager=PermissionManager(),
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Hi!", config=TEST_TURN_CONFIG))

        assert len(events) >= 2
        assert isinstance(events[0], ContentDelta)
        assert events[0].text_delta == "Hello, world!"
        assert isinstance(events[-1], AgentFinished)
        assert events[-1].reason == "LLM finished its turn."

    async def test_no_text_response(self, registry, executor, settings, conv_ctx):
        """LLM returns end_turn with no text content."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(text="", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Hi!", config=TEST_TURN_CONFIG))

        # No ContentDelta when text is empty.
        assert not any(isinstance(e, ContentDelta) for e in events)
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Echo please", config=TEST_TURN_CONFIG))

        # Should have: ToolCallStart, ToolCallEnd, ContentDelta, AgentFinished
        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        texts = [e for e in events if isinstance(e, ContentDelta)]
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Double echo", config=TEST_TURN_CONFIG))

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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Do something", config=TEST_TURN_CONFIG))

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "Simulated failure" in ends[0].result.error

        # Verify the LLM received the error in its second call.
        assert llm.call_count == 2
        second_call_msgs = llm.messages_history[1]
        tool_msgs = [m for m in second_call_msgs if m.role == "tool"]
        assert len(tool_msgs) == 1
        tool_block = tool_msgs[0].blocks[0]
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Try unknown tool", config=TEST_TURN_CONFIG))

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "Unknown tool" in ends[0].result.error

    async def test_llm_call_error(self, registry, executor, settings, conv_ctx):
        """When the LLM call itself raises, RecoverableAgentError + AgentFinished are yielded."""

        class FailingLLM(MockLLMProvider):
            async def generate(
                self, messages, tools, *, model, reasoning_effort=None,
                max_completion_tokens=4096, response_format=None,
                temperature=0.0, stream=True,
            ):
                raise RuntimeError("API connection lost")

        loop = AgentLoop(FailingLLM(), registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Hi", config=TEST_TURN_CONFIG))

        errors = [e for e in events if isinstance(e, AgentError)]
        finishes = [e for e in events if isinstance(e, AgentFinished)]

        assert len(errors) == 1
        assert "API connection lost" in errors[0].message
        assert isinstance(errors[0], RecoverableAgentError)
        assert len(finishes) == 1


# ============================================================================
# Tests: permission gating
# ============================================================================


class TestPermissionGating:
    """AgentPaused is yielded when a WRITE tool needs confirmation."""

    async def test_write_tool_yields_agent_paused(self, registry, executor, settings, conv_ctx):
        """A WRITE tool should always pause the loop for confirmation."""
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())

        gen = loop.run("Write this down", config=TEST_TURN_CONFIG)
        events: list = []

        # Manually step through to handle the async confirmation.
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                # External code approves
                loop.approve_tool_call()

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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())

        gen = loop.run("Write this", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.deny_tool_call()

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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Echo test", config=TEST_TURN_CONFIG))

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 0  # READ auto-approves


class TestPermissionMode:
    """AgentLoop respects the PermissionMode configured on the shared manager."""

    @pytest.fixture
    def perm_mgr(self) -> PermissionManager:
        return PermissionManager()

    async def test_write_tool_auto_approves_in_auto_mode(
        self, registry, executor, settings, conv_ctx, perm_mgr,
    ):
        """In AUTO mode, WRITE tools should NOT pause for confirmation."""
        perm_mgr.set_mode(PermissionMode.AUTO)
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "write_stuff", {"content": "auto-approved"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(
            llm, registry, executor, settings,
            context=conv_ctx, permission_manager=perm_mgr,
        )

        gen = loop.run("Write this", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 0  # WRITE auto-approved in AUTO mode

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert ends[0].result.success

    async def test_dangerous_tool_still_pauses_in_auto_mode(
        self, registry, executor, settings, conv_ctx, perm_mgr,
    ):
        """In AUTO mode, SHELL_DANGEROUS tools still pause."""
        perm_mgr.set_mode(PermissionMode.AUTO)
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "dangerous_cmd", {"command": "rm -rf /"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(
            llm, registry, executor, settings,
            context=conv_ctx, permission_manager=perm_mgr,
        )

        gen = loop.run("Delete everything", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 1
        assert "dangerous_cmd" in paused[0].prompt

    async def test_write_tool_pauses_in_manual_mode(
        self, registry, executor, settings, conv_ctx, perm_mgr,
    ):
        """In MANUAL mode, WRITE tools should pause (current behavior)."""
        perm_mgr.set_mode(PermissionMode.MANUAL)
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "write_stuff", {"content": "needs confirm"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(
            llm, registry, executor, settings,
            context=conv_ctx, permission_manager=perm_mgr,
        )

        gen = loop.run("Write this", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 1  # WRITE still pauses in MANUAL mode

    async def test_explicit_manual_mode_pauses_for_write(
        self, registry, executor, settings, conv_ctx,
    ):
        """When MANUAL mode is explicitly set, WRITE tools pause."""
        perm_mgr = PermissionManager()
        perm_mgr.set_mode(PermissionMode.MANUAL)
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1", "write_stuff", {"content": "default"}
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Done", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(
            llm, registry, executor, settings, context=conv_ctx,
            permission_manager=perm_mgr,
        )

        gen = loop.run("Write this", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 1  # WRITE pauses (MANUAL default)


class PathTool(BaseTool):
    """A mock READ tool whose reach is the file it names."""

    name = "read_path"
    description = "Read the named file"
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File to read"}
        },
        "required": ["file_path"],
    }
    path_params = ("file_path",)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Read: {kwargs.get('file_path')}",
        )

    @property
    def permission(self) -> Permission:
        return Permission.READ


class TestFileScopeGating:
    """Where a call reaches counts as much as what it does.

    A restricted file is refused without a prompt — it is policy, not a
    question — and a file outside the workspace is never taken on the
    agent's own judgement, in either mode.
    """

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        (tmp_path / ".env").write_text("API_KEY=secret\n")
        (tmp_path / "notes.txt").write_text("notes\n")
        return tmp_path

    @pytest.fixture
    def make_loop(self, settings, conv_ctx):
        """Build a loop over a registry whose tool carries *workspace*'s
        policy — the wiring :class:`SessionManager` does."""

        def build(workspace: Path, responses, mode=PermissionMode.MANUAL):
            policy = FileScopePolicy(workspace, (".env",))
            registry = ToolRegistry()
            registry.register(PathTool(scope_policy=policy))
            perm_mgr = PermissionManager(mode)
            executor = ToolExecutor(registry)
            return AgentLoop(
                MockLLMProvider(responses=responses),
                registry,
                executor,
                settings,
                context=conv_ctx,
                permission_manager=perm_mgr,
            )

        return build

    @staticmethod
    def _responses(file_path: str) -> list:
        return [
            _make_llm_response(
                tool_blocks=[
                    _make_tool_use_block("c1", "read_path", {"file_path": file_path})
                ],
                stop_reason="tool_use",
            ),
            _make_llm_response(text="Done", stop_reason="end_turn"),
        ]

    @pytest.mark.parametrize("mode", [PermissionMode.MANUAL, PermissionMode.AUTO])
    async def test_a_restricted_file_is_denied_without_asking(
        self, make_loop, workspace, mode
    ):
        """No pause: there is nothing to ask.  A restricted path is
        refused whatever the mode, so approving is not an option the user
        is offered."""
        loop = make_loop(workspace, self._responses(str(workspace / ".env")), mode)
        events = await _collect_events(loop.run("Read .env", config=TEST_TURN_CONFIG))

        assert not [e for e in events if isinstance(e, AgentPaused)]

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert not ends[0].result.success
        assert "restricted" in ends[0].result.error
        assert ".env" in ends[0].result.error

    @pytest.mark.parametrize("mode", [PermissionMode.MANUAL, PermissionMode.AUTO])
    async def test_an_external_file_pauses(self, make_loop, workspace, mode):
        """Outside the workspace is the user's call — in AUTO too, where
        the agent is otherwise trusted to run on its own."""
        loop = make_loop(workspace, self._responses("/etc/hosts"), mode)

        gen = loop.run("Read /etc/hosts", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert len(paused) == 1
        assert "read_path" in paused[0].prompt

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert ends[0].result.success  # approved, so it ran

    async def test_a_workspace_file_runs_unasked(self, make_loop, workspace):
        """The fast path: nothing changed for a call inside the workspace."""
        loop = make_loop(workspace, self._responses(str(workspace / "notes.txt")))
        events = await _collect_events(loop.run("Read notes", config=TEST_TURN_CONFIG))

        assert not [e for e in events if isinstance(e, AgentPaused)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert ends[0].result.success


class TestExecutorCheckpoint:
    """The executor takes the checkpoint its caller asked for.

    It reads no tier of its own: *mutating* is the caller's answer, and
    that answer alone orders the checkpoint — taken before the call runs,
    so the write it records is the write it can undo.
    """

    @staticmethod
    def _executor(
        log: list[str], cb: CheckpointCallback | None
    ) -> ToolExecutor:
        registry = ToolRegistry()
        registry.register(RecordingTool(log))
        return ToolExecutor(registry, checkpoint_cb=cb)

    @staticmethod
    def _call() -> ToolCall:
        return ToolCall(
            tool_id="c1",
            tool_name="recording_tool",
            parameters={"content": "hi"},
        )

    async def test_a_mutating_call_is_checkpointed_before_it_runs(self):
        log: list[str] = []

        def checkpoint(tool, params):   # noqa: ARG001
            log.append("checkpoint")
            return "ck1"

        executor = self._executor(log, checkpoint)

        result = await executor.execute(self._call(), mutating=True)

        assert log == ["checkpoint", "tool"]
        assert result.success
        assert result.checkpoint_id == "ck1"

    async def test_a_read_only_call_is_not_checkpointed(self):
        log: list[str] = []

        def checkpoint(tool, params):   # noqa: ARG001
            log.append("checkpoint")
            return "ck1"

        executor = self._executor(log, checkpoint)

        result = await executor.execute(self._call(), mutating=False)

        assert log == ["tool"]
        assert result.success
        assert result.checkpoint_id is None

    async def test_without_a_callback_the_call_still_runs(self):
        """A session with no checkpoint manager is not a broken session."""
        log: list[str] = []
        executor = self._executor(log, None)

        result = await executor.execute(self._call(), mutating=True)

        assert log == ["tool"]
        assert result.success
        assert result.checkpoint_id is None


class TestCheckpointOnWrite:
    """What the gate read is what orders the checkpoint.

    The loop classifies the call once and hands the answer down, so an
    approved write is snapshotted before it runs and a read never is —
    the hand-off the executor's own cases cannot see.
    """

    @staticmethod
    def _loop(log, tier, responses, settings, conv_ctx) -> AgentLoop:
        def checkpoint(tool, params):   # noqa: ARG001
            log.append("checkpoint")
            return "ck1"

        registry = ToolRegistry()
        registry.register(RecordingTool(log, tier=tier))
        executor = ToolExecutor(registry, checkpoint_cb=checkpoint)
        return AgentLoop(
            MockLLMProvider(responses=responses),
            registry,
            executor,
            settings,
            context=conv_ctx,
            permission_manager=PermissionManager(),
        )

    @staticmethod
    def _responses() -> list:
        return [
            _make_llm_response(
                tool_blocks=[
                    _make_tool_use_block(
                        "c1", "recording_tool", {"content": "hi"}
                    )
                ],
                stop_reason="tool_use",
            ),
            _make_llm_response(text="Done", stop_reason="end_turn"),
        ]

    async def test_an_approved_write_is_checkpointed(
        self, settings, conv_ctx
    ):
        """MANUAL asks about a write; approving it still snapshots first."""
        log: list[str] = []
        loop = self._loop(
            log, Permission.WRITE, self._responses(), settings, conv_ctx
        )

        gen = loop.run("Write it", config=TEST_TURN_CONFIG)
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert log == ["checkpoint", "tool"]
        assert ends[0].result.checkpoint_id == "ck1"

    async def test_a_read_is_not_checkpointed(self, settings, conv_ctx):
        log: list[str] = []
        loop = self._loop(
            log, Permission.READ, self._responses(), settings, conv_ctx
        )

        events = await _collect_events(
            loop.run("Read it", config=TEST_TURN_CONFIG)
        )

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert log == ["tool"]
        assert ends[0].result.checkpoint_id is None


class TestApprovalChannel:
    """The pause and the answer are one channel, open for exactly as long
    as the call it paused is waiting on it — no longer (a spent answer
    must not become the next call's) and no shorter (a front-end answers
    on a task of its own, after the loop has resumed and is waiting)."""

    def _loop(self, registry, executor, settings, conv_ctx, responses):
        return AgentLoop(
            MockLLMProvider(responses=responses),
            registry,
            executor,
            settings,
            context=conv_ctx,
            permission_manager=PermissionManager(),
        )

    @staticmethod
    def _write_response(content: str, tool_id: str = "c1"):
        return _make_llm_response(
            tool_blocks=[
                _make_tool_use_block(tool_id, "write_stuff", {"content": content})
            ],
            stop_reason="tool_use",
        )

    async def test_an_answer_arriving_after_the_loop_resumed_lands(
        self, registry, executor, settings, conv_ctx
    ):
        """The browser answers from its socket task, so the loop has
        resumed and is waiting on the channel before the click lands —
        the shape the CLI never shows, since it asks the user from inside
        the consumer loop and resumes only afterwards."""
        loop = self._loop(registry, executor, settings, conv_ctx, [
            self._write_response("from the browser"),
            _make_llm_response(text="Done", stop_reason="end_turn"),
        ])
        paused = asyncio.Event()
        events: list = []

        async def drain() -> None:
            async for event in loop.run("Write this", config=TEST_TURN_CONFIG):
                events.append(event)
                if isinstance(event, AgentPaused):
                    paused.set()

        task = asyncio.create_task(drain())
        await asyncio.wait_for(paused.wait(), timeout=5)
        # Let the generator resume and reach its wait, as the runner does
        # the moment it has broadcast the pause.
        await asyncio.sleep(0.05)
        loop.approve_tool_call()
        await asyncio.wait_for(task, timeout=5)

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert len(ends) == 1
        assert ends[0].result.success

    async def test_a_late_answer_does_not_answer_the_next_call(
        self, registry, executor, settings, conv_ctx
    ):
        """Two gated calls in one turn: approving the first must leave the
        second still a question.  A channel that stayed answered — or an
        answer read off the next call's channel — would run it unasked."""
        loop = self._loop(registry, executor, settings, conv_ctx, [
            _make_llm_response(
                tool_blocks=[
                    _make_tool_use_block("c1", "write_stuff", {"content": "one"}),
                    _make_tool_use_block("c2", "write_stuff", {"content": "two"}),
                ],
                stop_reason="tool_use",
            ),
            _make_llm_response(text="Done", stop_reason="end_turn"),
        ])

        pauses = 0
        events: list = []
        async for event in loop.run("Write twice", config=TEST_TURN_CONFIG):
            events.append(event)
            if isinstance(event, AgentPaused):
                pauses += 1
                if pauses == 1:
                    loop.approve_tool_call()
                else:
                    loop.deny_tool_call()

        assert pauses == 2      # the second call was asked about on its own
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert [e.result.success for e in ends] == [True, False]
        assert "denied" in ends[1].result.error.lower()

    async def test_answering_nothing_is_a_no_op(self, loop):
        """Both front-ends answer optimistically, and an answer with no
        pause to land on must land nowhere — not on whatever is paused by
        the time it arrives."""
        loop.approve_tool_call()
        loop.deny_tool_call()
        assert loop._pending is None

    async def test_a_gated_call_with_no_channel_is_not_run(
        self, registry, executor, settings, conv_ctx
    ):
        """A call that reaches the gate with no channel opened for it is a
        bug in the loop, not a question.  It must not run, and must not
        fall back on whatever the previous call left behind."""
        loop = self._loop(registry, executor, settings, conv_ctx, [])
        loop.approve_tool_call()    # an answered call's leftovers, if any

        result = await loop._execute_with_gating(
            ToolCall(
                tool_id="c1",
                tool_name="write_stuff",
                parameters={"content": "unapproved"},
            ),
            _Verdict(Permission.WRITE, Decision.ASK),
        )

        assert not result.success
        assert "Internal error" in result.error


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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(
            loop.run("Loop forever", config=TEST_TURN_CONFIG, max_iterations=3)
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Do something", config=TEST_TURN_CONFIG))

        finishes = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finishes) == 1
        assert "no tool calls" in finishes[0].reason.lower()


# ============================================================================
# Tests: conversation context
# ============================================================================


class TestContextManager:
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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        await _collect_events(loop.run("Test context", config=TEST_TURN_CONFIG))

        # Second call should include: system, user, assistant (tool_use), tool
        second_msgs = llm.messages_history[1]
        roles = [m.role for m in second_msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    async def test_mark_turn_cancelled_answers_dangling_tool_use(self, conv_ctx):
        """A cancel mid-approval leaves orphaned assistant tool_use blocks —
        the repair answers each with a cancelled tool_result (keeping the
        assistant message intact) and appends a marker."""
        await conv_ctx.prepare_turn("refactor thing")
        conv_ctx.append(Message.assistant([
            MessageBlock.content_block("I'll check the files."),
            MessageBlock.tool_use_block("t1", "read_file", {"path": "x.py"}),
            MessageBlock.tool_use_block("t2", "grep", {"pattern": "foo"}),
        ]))

        conv_ctx.mark_turn_cancelled()

        msgs = conv_ctx.messages
        assert [m.role for m in msgs] == [
            "system", "user", "assistant", "tool", "user",
        ]
        # The assistant message is untouched — text and tool_use survive.
        assert [b.type for b in msgs[2].blocks] == [
            "content", "tool_use", "tool_use",
        ]
        # One tool_result per dangling call, paired by tool_id, marked
        # as an error so the model knows the call never ran.
        assert [b.type for b in msgs[3].blocks] == ["tool_result", "tool_result"]
        assert [b.tool_id for b in msgs[3].blocks] == ["t1", "t2"]
        assert all(b.is_error for b in msgs[3].blocks)
        assert all("cancelled" in b.tool_result_content for b in msgs[3].blocks)
        assert msgs[-1].content == "[The previous turn was cancelled by the user.]"

    async def test_mark_turn_cancelled_keeps_complete_messages(self, conv_ctx):
        """A cancel mid-stream keeps the partial history — the next turn
        continues from it; only the marker is added."""
        await conv_ctx.prepare_turn("write a test")
        conv_ctx.append(Message.assistant([
            MessageBlock.content_block("Sure — I'll start by looking at"),
        ]))

        conv_ctx.mark_turn_cancelled()

        msgs = conv_ctx.messages
        assert [m.role for m in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[-1].content == "[The previous turn was cancelled by the user.]"

    async def test_system_prompt_is_built_by_context(self, registry, executor, settings, conv_ctx):  # noqa: E501
        """The system prompt is assembled by ContextManager.prepare_turn()."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(text="Ok", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        await _collect_events(loop.run("Hi", config=TEST_TURN_CONFIG))

        first_msgs = llm.messages_history[0]
        sys_msg = first_msgs[0]
        assert sys_msg.role == "system"
        # Should contain base persona (not empty).
        assert "Toddler" in sys_msg.content


# ============================================================================
# Tests: partial output preserved on cancel
# ============================================================================


class TestHandlerPartialContent:
    """``get_partial_content()`` exposes the model's output so far — the
    loop appends it to the context when a turn is cancelled mid-stream."""

    async def test_stream_handler_partial_text_mid_stream(self):
        """A snapshot taken between chunks keeps the text so far."""

        async def stream() -> AsyncIterator[StreamEvent]:
            yield StreamEvent(type="content_delta", data={"text_delta": "Hello "})
            yield StreamEvent(type="content_delta", data={"text_delta": "world"})

        handler = StreamHandler()
        gen = handler.process(stream())
        assert isinstance(await gen.__anext__(), ContentDelta)

        partial = handler.get_partial_content()
        assert [b.type for b in partial] == ["content"]
        assert partial[0].text == "Hello "

    async def test_stream_handler_partial_includes_started_tool_call(self):
        """A tool call started before the cancel is kept, its accumulated
        arguments parsed once at snapshot time."""

        async def stream() -> AsyncIterator[StreamEvent]:
            yield StreamEvent(type="content_delta", data={"text_delta": "Checking"})
            yield StreamEvent(
                type="tool_use_start",
                data={"tool_id": "t1", "tool_name": "read_file"},
            )
            yield StreamEvent(
                type="tool_use_delta",
                data={
                    "tool_id": "t1",
                    "input_delta": {"arguments_fragment": '{"path": "x.py"}'},
                },
            )

        handler = StreamHandler()
        events = [e async for e in handler.process(stream())]

        # The fragment yields no event of its own: it is accumulated.
        assert [type(e) for e in events] == [ContentDelta, ToolUseStart]

        partial = handler.get_partial_content()
        assert [b.type for b in partial] == ["content", "tool_use"]
        assert partial[1].tool_id == "t1"
        assert partial[1].tool_name == "read_file"
        assert partial[1].tool_input == {"path": "x.py"}

    async def test_a_cancel_snapshot_of_a_truncated_call_parses_to_nothing(self):
        """Arguments cut off mid-JSON must snapshot without raising — the
        repair path keeps the dangling call, with no input to name."""

        async def stream() -> AsyncIterator[StreamEvent]:
            yield StreamEvent(
                type="tool_use_start",
                data={"tool_id": "t1", "tool_name": "read_file"},
            )
            yield StreamEvent(
                type="tool_use_delta",
                data={
                    "tool_id": "t1",
                    "input_delta": {"arguments_fragment": '{"path": "x'},
                },
            )

        handler = StreamHandler()
        _ = [e async for e in handler.process(stream())]

        partial = handler.get_partial_content()
        assert [b.type for b in partial] == ["tool_use"]
        assert partial[0].tool_input == {}

    async def test_a_scalar_argument_snapshots_to_nothing(self):
        """A model that streams a bare JSON scalar must not blow up the
        block assembly: only an object is a tool input."""

        async def stream() -> AsyncIterator[StreamEvent]:
            yield StreamEvent(
                type="tool_use_start",
                data={"tool_id": "t1", "tool_name": "read_file"},
            )
            yield StreamEvent(
                type="tool_use_delta",
                data={
                    "tool_id": "t1",
                    "input_delta": {"arguments_fragment": "42"},
                },
            )

        handler = StreamHandler()
        _ = [e async for e in handler.process(stream())]

        partial = handler.get_partial_content()
        assert partial[0].tool_input == {}

    async def test_non_stream_handler_partial_content(self):
        """The complete response is available as soon as processing
        starts."""
        handler = NonStreamHandler()
        gen = handler.process(_make_llm_response(text="Complete answer"))
        assert isinstance(await gen.__anext__(), ContentDelta)

        partial = handler.get_partial_content()
        assert [b.type for b in partial] == ["content"]
        assert partial[0].text == "Complete answer"

    async def test_partial_content_empty_before_stream(self):
        """Nothing produced yet — no partial content to preserve."""
        assert StreamHandler().get_partial_content() == []
        assert NonStreamHandler().get_partial_content() == []


class TestCancelMidStream:
    """A cancel while the LLM is streaming text preserves the partial
    output in the context — the next turn can continue from it."""

    async def test_partial_text_kept_in_context(
        self, registry, executor, settings, conv_ctx,
    ):
        llm = SlowStreamLLM("Partial response that never finishes")
        loop = AgentLoop(
            llm, registry, executor, settings,
            context=conv_ctx, permission_manager=PermissionManager(),
        )

        events = []

        async def _collect() -> None:
            async for event in loop.run("explain", config=TEST_TURN_CONFIG, stream=True):
                events.append(event)

        task = asyncio.create_task(_collect())
        for _ in range(200):
            if any(isinstance(e, ContentDelta) for e in events):
                break
            await asyncio.sleep(0.01)
        assert any(isinstance(e, ContentDelta) for e in events), (
            "the first text delta never streamed"
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The partial output landed in the context, cut off mid-stream.
        assistant_msgs = [
            m for m in conv_ctx.messages if m.role == "assistant"
        ]
        assert len(assistant_msgs) == 1
        assert "Partial" in assistant_msgs[0].content
        assert "never finishes" not in assistant_msgs[0].content

        # The usual cancel repair follows the partial text with the marker.
        conv_ctx.mark_turn_cancelled()
        msgs = conv_ctx.messages
        assert msgs[-2].role == "assistant"
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "[The previous turn was cancelled by the user.]"


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
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events = await _collect_events(loop.run("Multi-round", config=TEST_TURN_CONFIG))

        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        texts = [e for e in events if isinstance(e, ContentDelta)]

        assert len(starts) == 2  # two rounds of tool calls
        assert len(ends) == 2
        assert len(texts) == 1  # final text response
        assert llm.call_count == 3


# ============================================================================
# Tests: call description
# ============================================================================


class TestCallDescription:
    """The required call-description param rides through the whole loop."""

    async def test_prompt_shows_it_and_the_call_still_runs(
        self, registry, executor, settings, conv_ctx
    ):
        """WriteTool leaves ``summarize_call`` to the base default, which
        renders every kwarg — so the param reaches the approval prompt with
        no display wiring.  It also has to survive into the tool-call event
        the transcript is built from."""
        llm = MockLLMProvider(
            responses=[
                _make_llm_response(
                    tool_blocks=[
                        _make_tool_use_block(
                            "c1",
                            "write_stuff",
                            {
                                "content": "important data",
                                "description": "record the deploy token",
                            },
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                _make_llm_response(text="Written!", stop_reason="end_turn"),
            ]
        )
        loop = AgentLoop(llm, registry, executor, settings, context=conv_ctx, permission_manager=PermissionManager())
        events: list = []

        async for event in loop.run("Write this down", config=TEST_TURN_CONFIG):
            events.append(event)
            if isinstance(event, AgentPaused):
                loop.approve_tool_call()

        paused = [e for e in events if isinstance(e, AgentPaused)]
        assert "record the deploy token" in paused[0].prompt

        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert ends[0].result.success
        assert ends[0].input["description"] == "record the deploy token"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
