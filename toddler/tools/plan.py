"""Plan-progress tracking — status vocabulary, runtime step-status state,
and the LLM-facing ``plan_update`` tool.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from toddler.tools.base import BaseTool, Permission, ToolResult

if TYPE_CHECKING:
    from toddler.agent.planner import Plan

__all__ = ["PLAN_STEP_STATUSES", "PlanState", "PlanUpdateTool"]

# Canonical plan step status values — the single source of truth for the
# model's status vocabulary, the tool schema enum, and PlanState.
PLAN_STEP_STATUSES = ("pending", "in_progress", "completed")


@dataclass
class PlanState:
    """Runtime step-status tracking for an executing plan.

    Owned by :class:`SessionCoordinator`.  :meth:`activate` captures the
    plan's steps into an ordered status table (plus a frozen description
    table) and baselines the change-detection snapshot; the coordinator
    calls :meth:`take_update` after each agent event and wraps the
    resulting triples into ``PlanStepUpdate`` events.  Never holds the
    plan itself.
    """

    _active: bool = field(default=False, repr=False)
    _statuses: OrderedDict[str, str] = field(
        default_factory=OrderedDict, repr=False,
    )
    _descriptions: dict[str, str] = field(
        default_factory=dict, repr=False,
    )
    _last_snap: tuple[tuple[str, str], ...] | None = field(
        default=None, repr=False,
    )

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return ``True`` while a plan is being tracked."""
        return self._active

    @property
    def step_ids(self) -> list[str]:
        """Return the tracked step ids in plan order."""
        return list(self._statuses)

    @property
    def is_complete(self) -> bool:
        """Return ``True`` when every tracked step is completed."""
        return (
            self._active
            and bool(self._statuses)
            and all(s == "completed" for s in self._statuses.values())
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def activate(self, plan: Plan) -> None:
        """Arm tracking for *plan*.

        Captures the step ids and descriptions (every status starts
        ``pending``) and baselines the snapshot at the current statuses,
        so the first :meth:`take_update` returns changes only once
        something actually moves.
        """
        self._statuses = OrderedDict(
            (s.id, "pending") for s in plan.steps
        )
        self._descriptions = {s.id: s.description for s in plan.steps}
        self._last_snap = self._status_snapshot()
        self._active = True

    def deactivate(self) -> None:
        """Tear down tracking after the execution phase."""
        self._active = False
        self._statuses.clear()
        self._descriptions.clear()
        self._last_snap = None

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mark_step(self, step_id: str, status: str) -> bool:
        """Update a step's status.

        Returns ``False`` when the step is unknown or no plan is active.
        Status validity is not checked here — the ``plan_update`` tool
        validates against ``PLAN_STEP_STATUSES`` before calling.
        """
        if not self._active or step_id not in self._statuses:
            return False
        self._statuses[step_id] = status
        return True

    # ------------------------------------------------------------------
    # Update detection
    # ------------------------------------------------------------------

    def full_update(self) -> list[tuple[str, str, str]] | None:
        """Return all steps as ``(id, description, status)`` triples.

        Used for the initial seed right after :meth:`activate` (so the UI
        shows the step list before any tool call) and for the final
        emission at phase end (so renderers can show the complete
        last-known statuses).  Returns ``None`` when no plan is active,
        mirroring :meth:`take_update`.
        """
        if not self._active:
            return None
        return self._step_triples()

    def take_update(self) -> list[tuple[str, str, str]] | None:
        """Return ``(id, description, status)`` triples when statuses changed.

        Carries only the changed steps, computed by diffing against the
        last emitted snapshot (which is then advanced).  Returns ``None``
        when no plan is active or nothing changed.
        """
        if not self._active or self._last_snap is None:
            return None
        last = dict(self._last_snap)
        changed = [
            (sid, self._descriptions[sid], status)
            for sid, status in self._statuses.items()
            if last.get(sid) != status
        ]
        if not changed:
            return None
        self._last_snap = self._status_snapshot()
        return changed

    def _step_triples(self) -> list[tuple[str, str, str]]:
        return [
            (sid, self._descriptions[sid], status)
            for sid, status in self._statuses.items()
        ]

    def _status_snapshot(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._statuses.items())


class PlanUpdateTool(BaseTool):
    """Update the status of a step in the approved execution plan.

    Mutates the shared :class:`PlanState` it was given.  The session
    coordinator arms that state only while PLAN_EXECUTING is active, and
    registers this tool dynamically for that phase alone, so the LLM sees
    the tool schema only when a plan is being executed.
    """

    name = "plan_update"
    description = (
        "Update the status of a step in the approved execution plan. "
        "Call with status='in_progress' immediately before starting a "
        "step and status='completed' once it is done. "
        "Statuses: " + ", ".join(PLAN_STEP_STATUSES) + "."
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

    def __init__(self, plan_state: PlanState) -> None:
        self._plan_state = plan_state

    @property
    def permission(self) -> Permission:
        return Permission.READ  # bookkeeping only — never gated

    async def execute(self, **kwargs) -> ToolResult:
        step_id = str(kwargs.get("step_id", ""))
        status = str(kwargs.get("status", ""))
        if not self._plan_state.is_active:
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
        if not self._plan_state.mark_step(step_id, status):
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Unknown step: {step_id!r}. "
                    f"Known steps: {', '.join(self._plan_state.step_ids)}."
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
