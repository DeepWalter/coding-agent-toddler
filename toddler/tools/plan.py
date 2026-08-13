"""Plan-progress tool — LLM-driven plan step status tracking."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from toddler.tools.base import BaseTool, Permission, ToolResult

if TYPE_CHECKING:
    from toddler.agent.planner import Plan

# Canonical PlanStep.status values — the single source of truth for the
# model's status vocabulary (see PlanStep.status in toddler.agent.planner).
PLAN_STEP_STATUSES = ("pending", "in_progress", "completed")


class PlanUpdateTool(BaseTool):
    """Update the status of a step in the approved execution plan.

    Stateless itself — mutates the active plan through the injected
    getter.  The session coordinator exposes its plan only while
    PLAN_EXECUTING is active, and registers this tool dynamically for
    that phase alone, so the LLM sees the tool schema only when a plan
    is being executed.
    """

    name = "plan_update"
    description = (
        "Update the status of a step in the approved execution plan. "
        "Call with status='in_progress' immediately before starting a "
        "step and status='completed' once it is done. "
        "Statuses: pending, in_progress, completed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "step_id": {
                "type": "string",
                "description": "Plan step id, e.g. 'step-1'.",
            },
            "status": {
                "type": "string",
                "enum": list(PLAN_STEP_STATUSES),
                "description": "New status for the step.",
            },
        },
        "required": ["step_id", "status"],
    }

    def __init__(self, get_plan: Callable[[], Plan | None]) -> None:
        self._get_plan = get_plan

    @property
    def permission(self) -> Permission:
        return Permission.READ  # bookkeeping only — never gated

    async def execute(self, **kwargs) -> ToolResult:
        step_id = str(kwargs.get("step_id", ""))
        status = str(kwargs.get("status", ""))
        plan = self._get_plan()
        if plan is None:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error="No approved plan is active.",
            )
        if status not in PLAN_STEP_STATUSES:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Invalid status: {status!r}. "
                    f"Valid statuses: {', '.join(PLAN_STEP_STATUSES)}."
                ),
            )
        if not plan.mark_step(step_id, status):
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Unknown step: {step_id!r}. "
                    f"Known steps: {', '.join(s.id for s in plan.steps)}."
                ),
            )
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Step {step_id} → {status}.",
        )

    def summarize_call(self, **kwargs) -> str:
        step_id = kwargs.get("step_id", "?")
        status = kwargs.get("status", "?")
        return f"plan_update({step_id!r} → {status!r})"
