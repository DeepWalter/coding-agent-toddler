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

from pathlib import Path

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
    ToolCallEnd,
    ToolCallStart,
    ToolUseStart,
)
from toddler.agent.planner import Plan
from toddler.llm import TokenUsage
from toddler.llm.messages import Message
from toddler.tools import HIDDEN_TOOL_NAMES, ToolResult
from toddler.web.files import (
    tool_path,
    tool_path_abs,
    tool_path_is_file,
    tool_path_named,
)

__all__ = [
    "serialize_event",
    "serialize_plan",
    "serialize_token_usage",
    "serialize_tool_result",
    "serialize_transcript",
    "tool_card_paths",
]


def serialize_plan(plan: Plan) -> dict:
    """Serialize a :class:`Plan` to its JSON frame.

    ``Plan`` has no ``to_dict`` (only ``to_json``), so fields are mapped
    explicitly.  ``PlanStep`` has ``to_dict`` and is reused as-is.

    ``markdown`` is the plan pre-rendered by
    :meth:`Plan.format_for_display` for the browser's document view — the
    same text the CLI prints, so the client never re-renders the format.
    It is computed once, here, at proposal time: ``plan_step_update``
    frames carry the live statuses, and the document itself stays frozen.
    """
    return {
        "id": plan.id,
        "title": plan.title,
        "summary": plan.summary,
        "steps": [step.to_dict() for step in plan.steps],
        "rationale": plan.rationale,
        "risks": plan.risks,
        "estimated_files_touched": plan.estimated_files_touched,
        "markdown": plan.format_for_display(),
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


def tool_card_paths(root: Path, input: dict) -> dict:
    """The file a tool call names, resolved for the card that draws it —
    all three spellings of it.  Any tool whose parameters carry a
    ``file_path`` — the file tools, and nothing else — gets them; a call
    that names no file gets ``None``s, and the browser draws its title
    from whatever else the call said.

    ``path`` is where a tab opens it, relative to the served root, and is
    ``None`` for a file the root cannot hold.  ``path_abs`` is where the
    file actually is: a file with no relative name opens under it
    instead — read-only, since it lies outside the root.  ``path_named``
    is the same file under the name the call used, which is what the card
    shows: resolving is the server's job, but reading
    ``/System/Volumes/Data/home/...`` back at whoever asked for
    ``/home/...`` is nobody's idea of an answer.

    ``path_symlink`` says those last two are one file reached two ways —
    the name goes through a link.  Worth marking: without it a card that
    names one path and opens another looks like a mistake, and on macOS
    it is not even unusual (``/tmp`` and ``/home`` are links).

    ``path_is_file`` is the one thing here that is not a spelling: whether
    there is a file to open at all, which is what decides whether the
    card's title is a control or its plain-text twin.  It is a fact about
    the disk as the frame is built, and every frame that carries the
    spellings carries it with them — so the browser never has to ask, and
    a write that creates a file is clickable from the frame its result
    lands in, not before.

    The ``tool_call_start`` and ``tool_call_end`` frames carry this (see
    :func:`serialize_event`).  The start frame is the one that matters for
    how early the card can name the file: the call's input is complete by
    then, and the frame arrives before any permission gate.  The
    identity-only ``tool_use_start`` carries none of them.
    """
    raw = input.get("file_path")
    named = tool_path_named(root, raw)
    resolved = tool_path_abs(root, raw)
    return {
        "path": tool_path(root, raw),
        "path_abs": resolved,
        "path_named": named,
        "path_symlink": named is not None and named != resolved,
        "path_is_file": tool_path_is_file(root, raw),
    }


def serialize_transcript(messages: list[Message], root: Path) -> list[dict]:  # noqa: C901
    """Flatten stored messages into transcript replay entries.

    Called for ``hello`` (WS reconnect) and ``/api/sessions/.../messages``.
    The persisted system prompt is agent scaffolding, not transcript —
    skipped.  Tool calls pair each stored ``tool_use`` block with its
    ``tool_result`` (matched by ``tool_id``) into a single entry shaped
    like the ``tool_call_end`` frame, so a refresh renders the same
    foldable cards the live stream did.  A use whose result was never
    persisted (cancelled mid-execution) replays with ``result: null`` —
    the frontend renders that as cancelled.

    *root* resolves each call's ``file_path`` for the card (see
    :func:`tool_card_paths`); the stored transcript no longer carries that
    answer, so it is recomputed on both paths rather than replayed.
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
                if msg.content.startswith("[The previous turn was cancelled by the user."):  # noqa: E501
                    entry["fold"] = "cancelled"
                elif msg.content.startswith("[Compacted"):
                    entry["fold"] = "compacted"
                entries.append(entry)
            continue
        # assistant — thinking first, then text, then its tool uses,
        # matching the live ``reasoning_delta`` → ``content_delta`` →
        # ``tool_call_start`` order.  A thought-only message (reasoning
        # then straight to a tool call) still emits its entry, so the
        # thinking card replays; ``reasoning`` stays absent when there is
        # none, keeping plain entries byte-identical to before.
        if msg.content or msg.reasoning:
            entry = {"role": "assistant", "content": msg.content}
            if msg.reasoning:
                entry["reasoning"] = msg.reasoning
            entries.append(entry)
        for block in msg.blocks:
            # No card is rebuilt for a hidden tool, matching the live
            # stream — its effect arrives as a frame of its own instead.
            # The call itself stays in the stored transcript; the model's
            # context needs it.
            if (
                block.type == "tool_use"
                and block.tool_id
                and block.tool_name not in HIDDEN_TOOL_NAMES
            ):
                tool_input = block.tool_input or {}
                entries.append({
                    "role": "tool",
                    "tool_id": block.tool_id,
                    "tool_name": block.tool_name or "",
                    "input": tool_input,
                    **tool_card_paths(root, tool_input),
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
        "reasoning_tokens": usage.reasoning_tokens,
    }


def serialize_event(event: AgentEvent, root: Path) -> dict | None:  # noqa: C901
    """Serialize one :class:`AgentEvent` to its protocol frame.

    Returns ``None`` for event classes this server doesn't know about
    (forward compatibility) so the runner can skip them instead of
    dropping the whole turn — and for calls to hidden tools, which the
    browser draws no card for.

    *root* is the served repo root, which resolves the ``file_path`` a
    tool call completed with: the browser cannot resolve one itself, and
    a replayed call has to land on the same answer as a live one.
    """
    # A hidden tool says nothing the browser needs: what it does reaches
    # the UI as a frame of its own, so a card for the call itself would
    # only say the same thing a second time.
    if (
        isinstance(event, (ToolUseStart, ToolCallStart, ToolCallEnd))
        and event.tool_name in HIDDEN_TOOL_NAMES
    ):
        return None

    match event:
        case ContentDelta(text_delta=text_delta):
            return {"type": "content_delta", "text_delta": text_delta}

        case ReasoningDelta(text_delta=text_delta):
            # Same payload key as content_delta — both prose kinds feed
            # their block's shared ``text`` slot; the frame type carries
            # the kind.
            return {"type": "reasoning_delta", "text_delta": text_delta}

        case ToolUseStart(tool_id=tool_id, tool_name=tool_name):
            # The provider phase, identity only: the card it opens is a
            # skeleton, because a JSON object has no fragment that parses.
            return {
                "type": "tool_use_start",
                "tool_id": tool_id,
                "tool_name": tool_name,
            }

        case ToolCallStart(
            tool_id=tool_id,
            tool_name=tool_name,
            input=input,
        ):
            return {
                "type": "tool_call_start",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "input": input,
                # The loop hands the call over with its input already
                # complete, and this frame is yielded before any permission
                # gate — so the card names the file (and marks it external)
                # while the call is still waiting to run.
                **tool_card_paths(root, input),
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
                **tool_card_paths(root, input),
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
