"""Plan-progress tracking — status vocabulary, runtime step-status state,
and the LLM-facing ``plan_update`` tool.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from toddler.tools.base import BaseTool, Permission, ToolResult

if TYPE_CHECKING:
    from toddler.agent.planner import Plan

__all__ = [
    "PLAN_STEP_STATUSES", "PLAN_UPDATE_STATUSES", "PLAN_UPDATE_USAGE",
    "PlanStepStatus", "PlanState", "PlanUpdateTool",
]

logger = logging.getLogger(__name__)

class PlanStepStatus(StrEnum):
    """Canonical plan step status values — the single source of truth
    for the status vocabulary, from which the status tuples, prompt
    prose, PlanState comparisons, and renderer display maps are all
    derived.

    Every step starts :attr:`PENDING` (set by ``PlanState.activate``,
    never by the model) and moves toward :attr:`COMPLETED`.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


# All statuses in lifecycle order, as plain strings.  Derived from the
# enum so a vocabulary change propagates.
PLAN_STEP_STATUSES = tuple(s.value for s in PlanStepStatus)

# Statuses the model may write via plan_update.  ``pending`` is the
# initial state and not writable — the render trigger fires exactly on
# these transitions (a step starting, or completion), so every accepted
# mutation is displayable.  Derived by membership so a vocabulary change
# propagates without positional assumptions.
PLAN_UPDATE_STATUSES = tuple(
    s.value for s in PlanStepStatus if s is not PlanStepStatus.PENDING
)

# Canonical plan_update usage instructions — the single source of truth
# for the model's reporting protocol.  Embedded in the tool description,
# which is what the model reads at call time; the PLAN_EXECUTING system
# prompt and the execution user prompt point at the tool description
# instead of repeating the protocol, so a vocabulary or protocol change
# propagates from one place.  Status literals are interpolated from
# PlanStepStatus so they stay in sync.  The final-step carve-out (call
# alone, no text alongside) reconciles the per-step confirmation with
# the renderer needing the closing snapshot before the summary.
PLAN_UPDATE_USAGE = (
    f"Call with status='{PlanStepStatus.IN_PROGRESS.value}' immediately "
    f"before starting a step and status='{PlanStepStatus.COMPLETED.value}' "
    "once it is done, briefly confirming what was done — except when "
    "marking the final step completed: call plan_update alone in its own "
    "response with no text alongside it, and give the closing summary "
    "once its tool result returns. "
    "Statuses: " + ", ".join(PLAN_UPDATE_STATUSES) + "."
)


@dataclass
class PlanState:
    """Runtime step-status tracking for an executing plan.

    Owned by :class:`SessionCoordinator`.  :meth:`activate` captures the
    plan's steps into an ordered status table (plus a frozen description
    table) and baselines the emitted snapshot; :meth:`take_update` then
    returns the complete step list only when something render-worthy
    changed, and :meth:`flush_update` flushes anything held back at
    phase end.  The coordinator wraps the returned triples into
    ``PlanStepUpdate`` events.  Never holds the plan itself.
    """

    _active: bool = field(default=False, repr=False)
    _statuses: OrderedDict[str, PlanStepStatus] = field(
        default_factory=OrderedDict, repr=False,
    )
    _descriptions: dict[str, str] = field(
        default_factory=dict, repr=False,
    )
    _last_emitted: tuple[tuple[str, PlanStepStatus], ...] | None = field(
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
            and all(
                s == PlanStepStatus.COMPLETED
                for s in self._statuses.values()
            )
        )

    @property
    def steps(self) -> list[tuple[str, str, PlanStepStatus]]:
        """Return current ``(id, description, status)`` triples in plan
        order — a pure read, unlike :meth:`take_update`.  Empty when no
        plan is active."""
        if not self._active:
            return []
        return self._step_triples()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def activate(self, plan: Plan) -> None:
        """Arm tracking for *plan*.

        Captures the step ids and descriptions (every status starts
        ``pending``) and baselines the emitted snapshot at the current
        statuses, so the first :meth:`take_update` returns content only
        once something render-worthy moves.
        """
        ids = [s.id for s in plan.steps]
        if len(set(ids)) != len(ids):
            # Invariant guard: :meth:`Plan.from_json` canonicalizes ids
            # to ``step-N``, so duplicates can only come from a plan
            # constructed by hand.  Log loudly instead of silently
            # collapsing rows.
            logger.error(
                "Plan has duplicate step ids (%d unique of %d) — "
                "tracking will collapse rows.",
                len(set(ids)), len(ids),
            )
        self._statuses = OrderedDict(
            (s.id, PlanStepStatus.PENDING) for s in plan.steps
        )
        self._descriptions = {s.id: s.description for s in plan.steps}
        self._last_emitted = self._status_snapshot()
        self._active = True

    def deactivate(self) -> None:
        """Tear down tracking after the execution phase."""
        self._active = False
        self._statuses.clear()
        self._descriptions.clear()
        self._last_emitted = None

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mark_step(self, step_id: str, status: str) -> bool:
        """Update a step's status.

        Returns ``False`` when the step is unknown or no plan is active.
        The status is normalized to a :class:`PlanStepStatus` member so
        tracking stays consistently typed; the ``plan_update`` tool
        validates against ``PLAN_UPDATE_STATUSES`` before calling, so an
        unparseable value signals a caller bug (raises ``ValueError``).
        """
        if not self._active or step_id not in self._statuses:
            return False
        self._statuses[step_id] = PlanStepStatus(status)
        return True

    # ------------------------------------------------------------------
    # Update detection
    # ------------------------------------------------------------------

    def take_update(self) -> list[tuple[str, str, PlanStepStatus]] | None:
        """Return the complete step list when there is something new to show.

        Renders only when a step transitioned to ``in_progress`` (a
        visible step start) or every step is now completed (the closing
        snapshot).  Bare ``completed`` changes are held back — they fold
        into the next render, so the UI never double-renders a
        completed + in_progress pair.  Returns ``None`` when nothing
        needs updating (the UI keeps the former content).  The emitted
        baseline advances on render; :meth:`flush_update` is the
        phase-end counterpart that skips the trigger filter.
        """
        return self._take_update(flush=False)

    def flush_update(self) -> list[tuple[str, str, PlanStepStatus]] | None:
        """Return the complete step list when anything changed since the
        last emission.

        Used at phase end to flush changes that never hit a render
        trigger (e.g. a lone ``completed`` mid-run).  Returns ``None``
        when nothing changed or no plan is active.
        """
        return self._take_update(flush=True)

    def _take_update(
        self, *, flush: bool,
    ) -> list[tuple[str, str, PlanStepStatus]] | None:
        if not self._active or self._last_emitted is None:
            return None
        current = self._status_snapshot()
        if current == self._last_emitted:
            return None
        if not flush and not self._render_due():
            return None
        self._last_emitted = current
        return self._step_triples()

    def _render_due(self) -> bool:
        """Return True when the diff since the last emission is worth a
        render (a step just started, or the plan just completed)."""
        if self.is_complete:
            return True
        last = dict(self._last_emitted or {})
        return any(
            status == PlanStepStatus.IN_PROGRESS and last.get(sid) != status
            for sid, status in self._statuses.items()
        )

    def _step_triples(self) -> list[tuple[str, str, PlanStepStatus]]:
        return [
            (sid, self._descriptions[sid], status)
            for sid, status in self._statuses.items()
        ]

    def _status_snapshot(self) -> tuple[tuple[str, PlanStepStatus], ...]:
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
        + PLAN_UPDATE_USAGE
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
                "enum": list(PLAN_UPDATE_STATUSES),
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
        if status not in PLAN_UPDATE_STATUSES:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Invalid status: {status!r}. "
                    f"Valid statuses: {', '.join(PLAN_UPDATE_STATUSES)}."
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
