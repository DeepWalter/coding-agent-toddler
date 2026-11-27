"""Plan models and progress tracking — the plan data model, status
vocabulary, runtime step-status state, and the LLM-facing ``plan_update``
tool.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum

from toddler.tools.base import BaseTool, ToolResult
from toddler.tools.policy import FileScopePolicy, Permission
from toddler.tools.registry import TOOL_CATALOG

__all__ = [
    "PLAN_STEP_STATUSES", "PLAN_UPDATE_STATUSES", "PLAN_UPDATE_USAGE",
    "Plan", "PlanState", "PlanStep", "PlanStepStatus", "PlanUpdateTool",
]

logger = logging.getLogger(__name__)


# ============================================================================
# Plan data models
# ============================================================================


def _single_line(text: str) -> str:
    """Collapse whitespace and newlines in *text* to single spaces.

    Plan title/summary/description render as ``Prefix: {value}`` one-liners
    in the prompt and as single rows in the renderer Plan panel, so
    multi-line values from LLM-generated JSON would break both.
    """
    return " ".join(str(text).split())


@dataclass
class PlanStep:
    """A single step in an execution plan.

    Parameters
    ----------
    id:
        Canonical step identifier, e.g. ``"step-1"``.  Assigned from
        position at parse time (see :meth:`Plan.from_json`) — ids
        proposed by the LLM are ignored, so duplicates and missing ids
        cannot reach tracking.
    description:
        Human-readable description of what this step accomplishes, e.g.
        ``"Read auth.py to understand the current login flow"``.
    tool_calls_expected:
        Tool names that are likely to be called during this step.
    files_affected:
        File paths expected to be read or modified.
    """

    id: str
    description: str
    tool_calls_expected: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict, *, step_id: str) -> PlanStep:
        """Build a ``PlanStep`` from a JSON-decoded dict.

        *step_id* is the canonical, position-derived id — stray keys in
        *d* (``"id"``, legacy ``"depends_on"``) are deliberately ignored.
        """
        return cls(
            id=step_id,
            description=_single_line(d.get("description", "")),
            tool_calls_expected=d.get("tool_calls_expected", []),
            files_affected=d.get("files_affected", []),
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "id": self.id,
            "description": self.description,
            "tool_calls_expected": self.tool_calls_expected,
            "files_affected": self.files_affected,
        }


@dataclass
class Plan:
    """A structured execution plan proposed by the agent.

    Parameters
    ----------
    id:
        Unique plan identifier (UUID4).
    title:
        Short title, e.g. ``"Fix authentication bug in auth.py"``.
    summary:
        2-3 sentence overview of what the plan aims to achieve.
    steps:
        Ordered list of :class:`PlanStep` objects.
    rationale:
        Why this approach was chosen over alternatives.
    risks:
        Known risks or things that could go wrong.
    estimated_files_touched:
        Rough count of files that will be modified.
    """

    id: str
    title: str
    summary: str
    steps: list[PlanStep] = field(default_factory=list)
    rationale: str = ""
    risks: list[str] = field(default_factory=list)
    estimated_files_touched: int = 0

    # ------------------------------------------------------------------
    # Factory / serialization
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, title: str, summary: str) -> Plan:
        """Create a new plan with a fresh UUID."""
        return cls(
            id=uuid.uuid4().hex,
            title=title,
            summary=summary,
        )

    @classmethod
    def from_json(cls, raw: str) -> Plan | None:
        """Parse a JSON string into a :class:`Plan`.

        Strips markdown code fences (`` ```json `` / `` ``` ``) if present,
        then parses the inner JSON.

        Returns ``None`` when parsing fails so callers can feed the error
        back to the LLM rather than crashing.
        """
        # Strip markdown code fences if present.
        json_str = raw.strip()
        if json_str.startswith("```"):
            first_nl = json_str.find("\n")
            if first_nl != -1:
                json_str = json_str[first_nl + 1:]
            if json_str.endswith("```"):
                json_str = json_str[:-3]
        json_str = json_str.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse plan JSON: {exc}")
            return None

        if not isinstance(data, dict):
            logger.warning("Plan JSON is not a dict.")
            return None

        steps_data = data.get("steps", [])
        # Step ids are canonical, position-derived: LLM-proposed ids are
        # ignored, so duplicates and missing ids cannot collapse rows in
        # PlanState tracking or mismatch the execution prompt.
        steps = [
            PlanStep.from_dict(s, step_id=f"step-{i}")
            for i, s in enumerate(steps_data, 1)
        ]

        return cls(
            # Coerce to str — a numeric id from the LLM would otherwise
            # never match the string plan_id passed to approve/reject
            # (int != str), making every decision stale input.
            id=str(data.get("id") or uuid.uuid4().hex),
            title=_single_line(data.get("title", "Untitled Plan")),
            summary=_single_line(data.get("summary", "")),
            steps=steps,
            rationale=_single_line(data.get("rationale", "")),
            risks=[_single_line(r) for r in data.get("risks", [])],
            # Default to 0 (matching the dataclass default) when the LLM
            # omits the field — falling back to the step count would
            # misreport an 8-step, 2-file refactor as 8 files touched.
            estimated_files_touched=data.get(
                "estimated_files_touched", 0,
            ),
        )

    def to_json(self) -> str:
        """Serialize the plan to a JSON string for storage."""
        return json.dumps(
            {
                "id": self.id,
                "title": self.title,
                "summary": self.summary,
                "steps": [s.to_dict() for s in self.steps],
                "rationale": self.rationale,
                "risks": self.risks,
                "estimated_files_touched": self.estimated_files_touched,
            },
            ensure_ascii=False,
            indent=2,
        )

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def format_for_display(self) -> str:
        """Render the plan as a markdown string for user display."""
        lines: list[str] = [
            f"## Plan: {self.title}",
            "",
            self.summary,
            "",
        ]

        if self.rationale:
            lines.append(f"**Rationale**: {self.rationale}")
            lines.append("")

        if self.risks:
            lines.append("**Risks**:")
            for r in self.risks:
                lines.append(f"- {r}")
            lines.append("")

        lines.append(f"**Steps** ({len(self.steps)}):")
        for i, step in enumerate(self.steps, 1):
            files = (
                f" [{', '.join(step.files_affected)}]"
                if step.files_affected
                else ""
            )
            lines.append(f"{i}. **{step.description}**{files}")

        lines.append("")
        lines.append(
            f"Estimated files touched: {self.estimated_files_touched}"
        )
        return "\n".join(lines)

    def format_for_prompt(self) -> str:
        """Render the full execution prompt: instructions plus the plan.

        Used during ``PLAN_EXECUTING`` mode as the user message that kicks
        off execution — it tells the agent to run the steps in order and
        report progress per the ``plan_update`` tool description (the
        single source for the reporting protocol), then appends the plan
        body.  The plan body is frozen at approval time; live step
        statuses are tracked separately at runtime and never rendered
        here.
        """
        plan_lines: list[str] = [
            f"## Approved Plan: {self.title}",
            f"Summary: {self.summary}",
            "",
            "Steps:",
        ]
        for step in self.steps:
            plan_lines.append(f"  {step.id}: {step.description}")
        plan_text = "\n".join(plan_lines)

        return (
            "I have reviewed and approved the following plan. "
            "Execute it step by step, reporting progress per the "
            "plan_update tool "
            f"description:\n\n{plan_text}"
        )


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
# the renderer needing the closing snapshot before the summary.  The
# call carries no call-description param (see PlanUpdateTool below), so
# the confirmation prose alongside it is what states the update.
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

    Owned by :class:`SessionManager`.  :meth:`activate` captures the
    plan's steps into an ordered status table (plus a frozen description
    table) and baselines the emitted snapshot; :meth:`take_update` then
    returns the complete step list whenever any status changed — every
    mutation emits, with no trigger filter — and the session manager wraps
    the returned triples into ``PlanStepUpdate`` events.  Never holds
    the plan itself.
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
        once a status actually moves.
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
        """Return the complete step list when any status changed.

        Emits on every mutation — no trigger filter — so a bare
        ``completed`` gets its own emission and the session manager needs no
        phase-end flush: every mutation happens during a tool call and is
        picked up at the following ``ToolCallEnd``.  Back-to-back
        mutations (a completed followed by the next step's start) would
        otherwise paint two frames close together; the streaming
        renderer's repaint throttle coalesces adjacent frames instead.
        Returns ``None`` when nothing changed (the UI keeps the former
        content).
        """
        if not self._active or self._last_emitted is None:
            return None
        current = self._status_snapshot()
        if current == self._last_emitted:
            return None
        self._last_emitted = current
        return self._step_triples()

    def _step_triples(self) -> list[tuple[str, str, PlanStepStatus]]:
        return [
            (sid, self._descriptions[sid], status)
            for sid, status in self._statuses.items()
        ]

    def _status_snapshot(self) -> tuple[tuple[str, PlanStepStatus], ...]:
        return tuple(self._statuses.items())


@TOOL_CATALOG.register()
class PlanUpdateTool(BaseTool):
    """Update the status of a step in the approved execution plan.

    Mutates the shared :class:`PlanState` it was given.  The session
    manager arms that state only while PLAN_EXECUTING is active, and
    registers this tool dynamically for that phase alone, so the LLM sees
    the tool schema only when a plan is being executed — which is why the
    definition-time registration above opts this tool out of the default
    registry (see :attr:`BaseTool.in_default_registry`).
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

    #: ``step_id`` and ``status`` are the whole of what the call records —
    #: no call-description param.
    require_description_parameter = False

    #: Registered per plan phase by the session manager, never handed to
    #: every session like a default tool.
    in_default_registry = False

    #: No card in the console: the statuses this call records arrive as
    #: plan step updates, so a card would only say it a second time.
    visible = False

    def __init__(
        self,
        plan_state: PlanState | None = None,
        *,
        scope_policy: FileScopePolicy | None = None,
    ) -> None:
        # The default covers definition-time registration, which constructs
        # with no arguments; a session always passes the state it shares
        # with the agent loop.
        super().__init__(scope_policy=scope_policy)
        self._plan_state = plan_state if plan_state is not None else PlanState()

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
