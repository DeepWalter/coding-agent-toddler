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
from toddler.llm.messages import Message
from toddler.tools.base import ToolResult

__all__ = [
    "serialize_event",
    "serialize_plan",
    "serialize_token_usage",
    "serialize_tool_result",
    "serialize_transcript",
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


def _tool_results(messages: list[Message]) -> dict[str, dict]:
    """Map ``tool_id`` → serialized result from stored ``tool_result``
    blocks."""
    results: dict[str, dict] = {}
    for msg in messages:
        if msg.role != "tool":
            continue
        for block in msg.blocks:
            if block.type != "tool_result" or not block.tool_id:
                continue
            is_error = block.is_error or False
            results[block.tool_id] = {
                "success": not is_error,
                "output": None if is_error else block.tool_result_content,
                "error": block.tool_result_content if is_error else None,
                "checkpoint_id": None,
                "metadata": None,
            }
    return results


def serialize_transcript(messages: list[Message]) -> list[dict]:
    """Flatten stored messages into transcript replay entries.

    Called for ``hello`` (WS reconnect) and ``/api/sessions/.../messages``.
    The persisted system prompt is agent scaffolding, not transcript —
    skipped.  Tool calls pair each stored ``tool_use`` block with its
    ``tool_result`` (matched by ``tool_id``) into a single entry shaped
    like the ``tool_call_end`` frame, so a refresh renders the same
    foldable cards the live stream did.  A use whose result was never
    persisted (cancelled mid-execution) replays with ``result: null`` —
    the frontend renders that as cancelled.
    """
    results = _tool_results(messages)
    entries: list[dict] = []
    for msg in messages:
        if msg.role in ("system", "tool"):
            continue
        if msg.role == "user":
            if msg.content:
                entry: dict = {"role": "user", "content": msg.content}
                # Synthetic repair messages are aimed at the model, not the
                # human — flag them so the frontend renders a fold line
                # instead of a user bubble (content stays for consumers that
                # want the raw text).
                if msg.content.startswith("[The previous turn was cancelled by the user."):
                    entry["fold"] = "cancelled"
                elif msg.content.startswith("[Compacted"):
                    entry["fold"] = "compacted"
                entries.append(entry)
            continue
        # assistant — text first, then its tool uses, matching the live
        # ``content_delta`` → ``tool_call_start`` order.
        if msg.content:
            entries.append({"role": "assistant", "content": msg.content})
        for block in msg.blocks:
            if block.type == "tool_use" and block.tool_id:
                entries.append({
                    "role": "tool",
                    "tool_id": block.tool_id,
                    "tool_name": block.tool_name or "",
                    "input": block.tool_input or {},
                    "result": results.get(block.tool_id),
                })
    return entries


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
