"""AgentEvent → JSON frame serializers for the WebSocket protocol.

Each serializer maps one :class:`~toddler.agent.events.AgentEvent`
subclass (or payload type) to its protocol frame.  Frame ``type`` strings
are the snake_case event class names — that is the wire contract the
frontend switches on.

:class:`~toddler.agent.planner.Plan`, :class:`~toddler.tools.base.ToolResult`
and :class:`~toddler.llm.TokenUsage` have no ``to_dict``, so their fields
are mapped explicitly here.
"""

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
from toddler.agent.planner import Plan
from toddler.llm import TokenUsage
from toddler.tools.base import ToolResult

__all__ = [
    "serialize_event",
    "serialize_plan",
    "serialize_token_usage",
    "serialize_tool_result",
]


def serialize_plan(plan: Plan) -> dict:
    """Serialize a :class:`Plan` to its JSON frame.

    ``Plan`` has no ``to_dict`` (only ``to_json``), so fields are mapped
    explicitly.  ``PlanStep`` has ``to_dict`` and is reused as-is.
    """
    return {
        "id": plan.id,
        "title": plan.title,
        "summary": plan.summary,
        "steps": [step.to_dict() for step in plan.steps],
        "rationale": plan.rationale,
        "risks": plan.risks,
        "estimated_files_touched": plan.estimated_files_touched,
    }


def serialize_tool_result(result: ToolResult | None) -> dict | None:
    """Serialize a :class:`ToolResult` to its JSON frame.

    Returns ``None`` when the result is missing (a tool call that never
    executed, e.g. after cancellation).
    """
    if result is None:
        return None
    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "checkpoint_id": result.checkpoint_id,
        "metadata": result.metadata,
    }


def serialize_token_usage(usage: TokenUsage | None) -> dict | None:
    """Serialize a :class:`TokenUsage` to its JSON frame.

    Returns ``None`` when the turn carried no usage information.
    """
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


def serialize_event(event: AgentEvent) -> dict | None:
    """Serialize one :class:`AgentEvent` to its protocol frame.

    Returns ``None`` for event classes this server doesn't know about
    (forward compatibility) so the runner can skip them instead of
    dropping the whole turn.
    """
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}

    if isinstance(event, ToolCallStart):
        return {
            "type": "tool_call_start",
            "tool_id": event.tool_id,
            "tool_name": event.tool_name,
            "partial_input": event.partial_input,
        }

    if isinstance(event, ToolCallDelta):
        return {
            "type": "tool_call_delta",
            "tool_id": event.tool_id,
            "input_delta": event.input_delta,
        }

    if isinstance(event, ToolCallEnd):
        return {
            "type": "tool_call_end",
            "tool_id": event.tool_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "result": serialize_tool_result(event.result),
        }

    if isinstance(event, PlanProposed):
        return {"type": "plan_proposed", "plan": serialize_plan(event.plan)}

    if isinstance(event, PlanStepUpdate):
        # Complete snapshot — the frontend replaces, not diffs.
        return {
            "type": "plan_step_update",
            "steps": [
                [step_id, description, status.value]
                for step_id, description, status in event.steps
            ],
        }

    if isinstance(event, AgentPaused):
        return {
            "type": "agent_paused",
            "prompt": event.prompt,
            "choices": event.choices,
        }

    if isinstance(event, AgentFinished):
        return {
            "type": "agent_finished",
            "reason": event.reason,
            "usage": serialize_token_usage(event.usage),
        }

    if isinstance(event, RecoverableAgentError):
        return {"type": "recoverable_error", "message": event.message}

    if isinstance(event, FatalAgentError):
        return {"type": "fatal_error", "message": event.message}

    return None
