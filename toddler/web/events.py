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


def serialize_event(event: AgentEvent) -> dict | None:  # noqa: C901
    """Serialize one :class:`AgentEvent` to its protocol frame.

    Returns ``None`` for event classes this server doesn't know about
    (forward compatibility) so the runner can skip them instead of
    dropping the whole turn.
    """
    match event:
        case TextDelta(text=text):
            return {"type": "text_delta", "text": text}

        case ToolCallStart(
            tool_id=tool_id,
            tool_name=tool_name,
            partial_input=partial_input,
        ):
            return {
                "type": "tool_call_start",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "partial_input": partial_input,
            }

        case ToolCallDelta(tool_id=tool_id, input_delta=input_delta):
            return {
                "type": "tool_call_delta",
                "tool_id": tool_id,
                "input_delta": input_delta,
            }

        case ToolCallEnd(
            tool_id=tool_id,
            tool_name=tool_name,
            input=input,
            result=result,
        ):
            return {
                "type": "tool_call_end",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "input": input,
                "result": serialize_tool_result(result),
            }

        case PlanProposed(plan=plan):
            return {"type": "plan_proposed", "plan": serialize_plan(plan)}

        case PlanStepUpdate(steps=steps):
            # Complete snapshot — the frontend replaces, not diffs.
            return {
                "type": "plan_step_update",
                "steps": [
                    [step_id, description, status.value]
                    for step_id, description, status in steps
                ],
            }

        case AgentPaused(prompt=prompt, choices=choices):
            return {
                "type": "agent_paused",
                "prompt": prompt,
                "choices": choices,
            }

        case AgentFinished(reason=reason, usage=usage):
            return {
                "type": "agent_finished",
                "reason": reason,
                "usage": serialize_token_usage(usage),
            }

        case RecoverableAgentError(message=message):
            return {"type": "recoverable_error", "message": message}

        case FatalAgentError(message=message):
            return {"type": "fatal_error", "message": message}

        # Any other AgentEvent subclass (forward compatibility).
        case AgentEvent():
            return None
