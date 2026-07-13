"""Agent event types — yielded by AgentLoop as it progresses through a turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.llm.types import TokenUsage
    from toddler.tools.base import ToolResult


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

    plan: Plan  # noqa: F821  # forward reference to agent.state_machine.Plan


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
    """A recoverable error occurred during execution."""

    message: str
    recoverable: bool = True
