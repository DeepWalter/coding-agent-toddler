"""Serializer round-trips — every AgentEvent class ↔ its protocol frame."""

from __future__ import annotations

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    AgentPaused,
    FatalAgentError,
    PlanProposed,
    PlanStepUpdate,
    RecoverableAgentError,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.planner import Plan, PlanStep
from toddler.llm import TokenUsage
from toddler.tools.base import ToolResult
from toddler.tools.plan import PlanStepStatus
from toddler.web.events import (
    serialize_event,
    serialize_plan,
    serialize_token_usage,
    serialize_tool_result,
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

    def test_text_delta(self):
        assert serialize_event(TextDelta(text="hi")) == {
            "type": "text_delta",
            "text": "hi",
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
