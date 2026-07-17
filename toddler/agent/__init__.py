"""Agent loop, state machine, stop conditions, and event types."""

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    AgentPaused,
    PlanProposed,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.handler import IncrementalJSONParser, StreamHandler
from toddler.agent.loop import AgentLoop
from toddler.agent.stop_conditions import StopConditionChecker, StopReason

__all__ = [
    "AgentError",
    "AgentEvent",
    "AgentFinished",
    "AgentLoop",
    "AgentPaused",
    "IncrementalJSONParser",
    "PlanProposed",
    "StopConditionChecker",
    "StopReason",
    "StreamHandler",
    "TextDelta",
    "ToolCallDelta",
    "ToolCallEnd",
    "ToolCallStart",
]
