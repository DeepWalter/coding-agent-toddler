"""Agent event types — yielded by AgentLoop as it progresses through a turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.agent.planner import Plan
    from toddler.llm import TokenUsage
    from toddler.tools.base import ToolResult
    from toddler.tools.plan import PlanStepStatus


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """Base class for all agent lifecycle events."""


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


@dataclass
class TextDelta(AgentEvent):
    """A single token (or small chunk) of streaming text."""

    text: str


@dataclass
class ToolCallStart(AgentEvent):
    """The LLM has started emitting a tool call."""

    tool_id: str
    tool_name: str
    partial_input: dict | None = None


@dataclass
class ToolCallDelta(AgentEvent):
    """An incremental fragment of a tool call's input arrived."""

    tool_id: str
    input_delta: dict


@dataclass
class ToolCallEnd(AgentEvent):
    """A tool call has been fully received and (optionally) executed."""

    tool_id: str
    tool_name: str
    input: dict
    result: ToolResult | None = None


# ---------------------------------------------------------------------------
# Plan mode events
# ---------------------------------------------------------------------------


@dataclass
class PlanProposed(AgentEvent):
    """The agent is presenting a plan for user approval."""

    plan: Plan  # forward reference to agent.planner.Plan


@dataclass
class PlanStepUpdate(AgentEvent):
    """Plan step statuses should be (re)displayed.

    Always carries the complete ``(id, description, status)`` triple
    list, so renderers replace their snapshot without diffing.  The
    coordinator emits it at meaningful moments — a step starting, all
    steps completed, and phase end (flushing any status changes that
    never hit a trigger).
    """

    steps: list[tuple[str, str, PlanStepStatus]]  # (id, description, status)


# ---------------------------------------------------------------------------
# Interaction events
# ---------------------------------------------------------------------------


@dataclass
class AgentPaused(AgentEvent):
    """The agent is waiting for user input (approval, confirmation, etc.)."""

    prompt: str
    choices: list[str] | None = None


# ---------------------------------------------------------------------------
# Terminal events
# ---------------------------------------------------------------------------


@dataclass
class AgentFinished(AgentEvent):
    """The agent has completed the task."""

    reason: str
    usage: TokenUsage | None = None


@dataclass
class AgentError(AgentEvent):
    """Base for errors yielded during a turn — do not instantiate directly.

    The recoverable/fatal distinction is a type-level contract, not a
    property: consumers stop the streaming renderer (exiting the
    alternate screen) only on :class:`FatalAgentError`, while a
    :class:`RecoverableAgentError` keeps the turn running.  Use the
    concrete subclasses.
    """

    message: str

    def __init__(self, message: str) -> None:
        raise NotImplementedError(
            "AgentError is abstract; use RecoverableAgentError or "
            "FatalAgentError",
        )


@dataclass
class RecoverableAgentError(AgentError):
    """An error the turn can continue past (e.g. a failed LLM call)."""
    pass

@dataclass
class FatalAgentError(AgentError):
    """An error that ends the turn (e.g. unparseable plan output)."""
    pass
