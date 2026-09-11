"""Serializer round-trips — every AgentEvent class ↔ its protocol frame."""

from __future__ import annotations

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    AgentPaused,
    ContentDelta,
    FatalAgentError,
    PlanProposed,
    PlanStepUpdate,
    ReasoningDelta,
    RecoverableAgentError,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.planner import Plan, PlanStep
from toddler.llm import TokenUsage
from toddler.llm.messages import Message, MessageBlock
from toddler.tools.base import ToolResult
from toddler.tools.plan import PlanStepStatus
from toddler.web.events import (
    serialize_event,
    serialize_plan,
    serialize_token_usage,
    serialize_tool_result,
    serialize_transcript,
)


def _plan() -> Plan:
    return Plan(
        id="plan-1",
        title="Refactor thing",
        summary="Move the logic.",
        steps=[
            PlanStep(
                id="step-1",
                description="Read the code",
                tool_calls_expected=["read_file"],
                files_affected=["src/a.py"],
            ),
        ],
        rationale="Cleaner.",
        risks=["Risky"],
        estimated_files_touched=2,
    )


class TestSerializeEvent:
    """Frame type + payload for every event class."""

    def test_content_delta(self):
        assert serialize_event(ContentDelta(text_delta="hi")) == {
            "type": "content_delta",
            "text_delta": "hi",
        }

    def test_reasoning_delta(self):
        # Same payload key as content_delta — the frame type carries the
        # kind, the key is named after the shared ``text`` slot it feeds.
        assert serialize_event(ReasoningDelta(text_delta="hmm")) == {
            "type": "reasoning_delta",
            "text_delta": "hmm",
        }

    def test_tool_call_start(self):
        frame = serialize_event(ToolCallStart(
            tool_id="t1", tool_name="read_file",
            partial_input={"path": "a.py"},
        ))
        assert frame == {
            "type": "tool_call_start",
            "tool_id": "t1",
            "tool_name": "read_file",
            "partial_input": {"path": "a.py"},
        }

    def test_tool_call_start_without_partial_input(self):
        frame = serialize_event(ToolCallStart(
            tool_id="t1", tool_name="read_file",
        ))
        assert frame["partial_input"] is None

    def test_tool_call_delta(self):
        assert serialize_event(ToolCallDelta(
            tool_id="t1", input_delta={"path": "a.p"},
        )) == {
            "type": "tool_call_delta",
            "tool_id": "t1",
            "input_delta": {"path": "a.p"},
        }

    def test_tool_call_end(self):
        result = ToolResult(
            tool_id="t1", tool_name="read_file", success=True,
            output="content", checkpoint_id="ck-1",
            metadata={"path": "a.py"},
        )
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file",
            input={"path": "a.py"}, result=result,
        ))
        assert frame == {
            "type": "tool_call_end",
            "tool_id": "t1",
            "tool_name": "read_file",
            "input": {"path": "a.py"},
            "result": {
                "success": True,
                "output": "content",
                "error": None,
                "checkpoint_id": "ck-1",
                "metadata": {"path": "a.py"},
            },
        }

    def test_tool_call_end_without_result(self):
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file", input={},
        ))
        assert frame["result"] is None

    def test_plan_proposed(self):
        plan = _plan()
        frame = serialize_event(PlanProposed(plan=plan))
        assert frame == {
            "type": "plan_proposed",
            "plan": serialize_plan(plan),
        }
        assert frame["plan"]["steps"] == [{
            "id": "step-1",
            "description": "Read the code",
            "tool_calls_expected": ["read_file"],
            "files_affected": ["src/a.py"],
        }]

    def test_plan_step_update(self):
        frame = serialize_event(PlanStepUpdate(steps=[
            ("step-1", "Read the code", PlanStepStatus.IN_PROGRESS),
            ("step-2", "Edit", PlanStepStatus.PENDING),
        ]))
        assert frame == {
            "type": "plan_step_update",
            "steps": [
                ["step-1", "Read the code", "in_progress"],
                ["step-2", "Edit", "pending"],
            ],
        }

    def test_agent_paused(self):
        assert serialize_event(AgentPaused(
            prompt="Allow write_file(x)?", choices=["approve", "deny"],
        )) == {
            "type": "agent_paused",
            "prompt": "Allow write_file(x)?",
            "choices": ["approve", "deny"],
        }

    def test_agent_finished(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=5,
            cache_read_tokens=3, cache_creation_tokens=2,
        )
        frame = serialize_event(AgentFinished(
            reason="LLM finished its turn.", usage=usage,
        ))
        assert frame == {
            "type": "agent_finished",
            "reason": "LLM finished its turn.",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 2,
                "reasoning_tokens": 0,
            },
        }

    def test_agent_finished_without_usage(self):
        frame = serialize_event(AgentFinished(
            reason="Plan rejected by user.",
        ))
        assert frame["usage"] is None

    def test_recoverable_error(self):
        assert serialize_event(RecoverableAgentError(
            message="API hiccup",
        )) == {"type": "recoverable_error", "message": "API hiccup"}

    def test_fatal_error(self):
        assert serialize_event(FatalAgentError(
            message="Unparseable plan",
        )) == {"type": "fatal_error", "message": "Unparseable plan"}

    def test_unknown_event_returns_none(self):
        class UnknownEvent(AgentEvent):
            pass

        assert serialize_event(UnknownEvent()) is None


class TestSerializeTranscript:
    """Stored-message flattening for hello / REST replay."""

    @staticmethod
    def _tool_pair(
        tool_id: str,
        name: str = "read_file",
        tool_input: dict | None = None,
        result: str | None = "content",
        is_error: bool = False,
    ) -> list[Message]:
        """The stored shape of a tool call: an assistant tool_use message
        followed by a tool message with its result."""
        return [
            Message.assistant([
                MessageBlock.tool_use_block(
                    tool_id, name, tool_input or {"path": "a.py"},
                ),
            ]),
            Message.tool([
                MessageBlock.tool_result_block(
                    tool_id, result or "", is_error=is_error,
                ),
            ]),
        ]

    def test_system_message_skipped(self):
        messages = [
            Message.system("You are helpful."),
            Message.user("hi"),
            Message.assistant([MessageBlock.content_block("hello")]),
        ]
        assert serialize_transcript(messages) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_tool_call_paired_into_single_entry(self):
        messages = [
            Message.user("read it"),
            *self._tool_pair("t1"),
            Message.assistant([MessageBlock.content_block("done")]),
        ]
        assert serialize_transcript(messages) == [
            {"role": "user", "content": "read it"},
            {
                "role": "tool",
                "tool_id": "t1",
                "tool_name": "read_file",
                "input": {"path": "a.py"},
                "result": {
                    "success": True,
                    "output": "content",
                    "error": None,
                    "checkpoint_id": None,
                    "metadata": None,
                },
            },
            {"role": "assistant", "content": "done"},
        ]

    def test_failed_tool_result_maps_error(self):
        messages = self._tool_pair(
            "t1", name="write_file",
            tool_input={"path": "b.py"}, result="denied", is_error=True,
        )
        entry = serialize_transcript(messages)[0]
        assert entry["result"] == {
            "success": False,
            "output": None,
            "error": "denied",
            "checkpoint_id": None,
            "metadata": None,
        }

    def test_use_without_result_replays_as_cancelled(self):
        # A turn cancelled mid-execution persists the use but no result.
        messages = [Message.assistant([
            MessageBlock.tool_use_block("t1", "write_file", {}),
        ])]
        entry = serialize_transcript(messages)[0]
        assert entry["tool_id"] == "t1"
        assert entry["result"] is None

    def test_tool_message_not_emitted_on_its_own(self):
        # The tool role carries only results; they attach to the use site.
        messages = [Message.tool([
            MessageBlock.tool_result_block("t1", "content"),
        ])]
        assert serialize_transcript(messages) == []

    def test_assistant_text_and_use_keep_live_order(self):
        messages = [Message.assistant([
            MessageBlock.content_block("checking…"),
            MessageBlock.tool_use_block("t1", "read_file", {"path": "a.py"}),
        ])]
        assert [e["role"] for e in serialize_transcript(messages)] == [
            "assistant", "tool",
        ]

    def test_assistant_reasoning_attached_verbatim(self):
        messages = [Message.assistant([
            MessageBlock.reasoning_block("step 1: "),
            MessageBlock.reasoning_block("step 2"),
            MessageBlock.content_block("the answer"),
        ])]
        assert serialize_transcript(messages) == [{
            "role": "assistant",
            "content": "the answer",
            "reasoning": "step 1: step 2",
        }]

    def test_reasoning_key_absent_without_reasoning(self):
        entry = serialize_transcript([
            Message.assistant([MessageBlock.content_block("plain")]),
        ])[0]
        assert entry == {"role": "assistant", "content": "plain"}
        assert "reasoning" not in entry

    def test_thought_only_entry_replays_with_its_tool(self):
        """A message that reasoned and went straight to a tool call has no
        content — the entry must still replay, or the card is lost."""
        messages = [
            Message.user("read it"),
            Message.assistant([
                MessageBlock.reasoning_block("I need the file first."),
                MessageBlock.tool_use_block(
                    "t1", "read_file", {"path": "a.py"},
                ),
            ]),
            Message.tool([
                MessageBlock.tool_result_block("t1", "content"),
            ]),
        ]
        entries = serialize_transcript(messages)
        assert [e["role"] for e in entries] == ["user", "assistant", "tool"]
        assert entries[1] == {
            "role": "assistant",
            "content": "",
            "reasoning": "I need the file first.",
        }

    def test_reasoning_entry_ordered_before_its_tool_entries(self):
        messages = [Message.assistant([
            MessageBlock.reasoning_block("cot"),
            MessageBlock.content_block("done"),
            MessageBlock.tool_use_block("t1", "read_file", {"path": "a.py"}),
        ])]
        entries = serialize_transcript(messages)
        assert entries[0]["reasoning"] == "cot"
        assert entries[0]["content"] == "done"
        assert entries[1]["role"] == "tool"

    def test_cancel_marker_entry_gets_fold_key(self):
        # The turn-cancelled repair message is model scaffolding — flagged so
        # the frontend renders a fold line, content kept for consumers.
        marker = "[The previous turn was cancelled by the user.]"
        messages = [
            Message.user("hi"),
            Message.user(marker),
        ]
        assert serialize_transcript(messages) == [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": marker, "fold": "cancelled"},
        ]

    def test_compacted_marker_entry_gets_fold_key(self):
        marker = (
            "[Compacted history — summary of the conversation so far]\n\n"
            "user wanted x; agent did y."
        )
        entry = serialize_transcript([Message.user(marker)])[0]
        assert entry["fold"] == "compacted"
        assert entry["content"] == marker

    def test_bracket_text_without_marker_stays_fold_free(self):
        # Only the known repair prefixes fold — ordinary bracketed text the
        # user actually typed must keep replaying as a user entry.
        entry = serialize_transcript([Message.user("[Just a bracket note]")])[0]
        assert entry == {"role": "user", "content": "[Just a bracket note]"}


class TestSerializePayloads:
    """Explicit field maps for Plan, ToolResult, TokenUsage."""

    def test_serialize_plan_maps_every_field(self):
        plan = _plan()
        assert serialize_plan(plan) == {
            "id": "plan-1",
            "title": "Refactor thing",
            "summary": "Move the logic.",
            "steps": [step.to_dict() for step in plan.steps],
            "rationale": "Cleaner.",
            "risks": ["Risky"],
            "estimated_files_touched": 2,
        }

    def test_serialize_tool_result_with_error(self):
        result = ToolResult(
            tool_id="t1", tool_name="write_file", success=False,
            output="", error="User denied permission.",
        )
        assert serialize_tool_result(result) == {
            "success": False,
            "output": "",
            "error": "User denied permission.",
            "checkpoint_id": None,
            "metadata": {},
        }

    def test_serialize_tool_result_none(self):
        assert serialize_tool_result(None) is None

    def test_serialize_token_usage_none(self):
        assert serialize_token_usage(None) is None

    def test_serialize_token_usage_maps_reasoning_tokens(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=60, cache_read_tokens=3,
            cache_creation_tokens=2, reasoning_tokens=40,
        )
        assert serialize_token_usage(usage) == {
            "input_tokens": 10,
            "output_tokens": 60,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 2,
            "reasoning_tokens": 40,
        }
