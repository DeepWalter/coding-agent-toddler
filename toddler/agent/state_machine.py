"""Agent state machine — mode management, plan tracking, complexity heuristic.

Phase 10: Full state machine covering all operational states and valid
transitions.  Provides the plan-mode workflow (explore → propose → approve →
execute) plus the complexity heuristic that can auto-trigger plan mode for
non-trivial requests.

State diagram (see ``docs/plan.md``):

    IDLE ──► classify ──► EXECUTING ──► FINISHED
               │
               └──► PLAN_EXPLORING ◄──────────────────┐
                       │                              │
                       ▼                              │
                 PLAN_PROPOSING                       │
                       │                              │
                       ▼                              │
                 PLAN_WAITING ─── reject (feedback) ──┘
                  /         ╲
         approve /           ╲ reject (plain)
                /             ╲
               ▼               ▼
      PLAN_EXECUTING        FINISHED
               │
               ▼
           FINISHED
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.tools.base import Permission

logger = logging.getLogger(__name__)


# ============================================================================
# AgentMode — operational modes
# ============================================================================


class AgentMode(Enum):
    """Operating modes for the agent loop.

    There are five active operational states (plus IDLE / FINISHED bookends):

    * **EXECUTING** — normal tool-calling loop; make changes to accomplish
      the user's request.
    * **PLAN_EXPLORING** — research-only; read files, search code, gather
      context.  No mutating tools allowed.
    * **PLAN_PROPOSING** — the agent has finished exploring and is asked to
      produce a structured JSON plan.
    * **PLAN_WAITING** — a plan has been presented to the user; awaiting
      approval, rejection, or feedback.
    * **PLAN_EXECUTING** — executing the approved plan step by step.
    """

    IDLE = "idle"
    EXECUTING = "executing"
    PLAN_EXPLORING = "plan_exploring"
    PLAN_PROPOSING = "plan_proposing"
    PLAN_WAITING = "plan_waiting"
    PLAN_EXECUTING = "plan_executing"
    FINISHED = "finished"

    # ------------------------------------------------------------------
    # Convenience checks
    # ------------------------------------------------------------------

    @property
    def is_plan_related(self) -> bool:
        """Return ``True`` when the mode is part of the plan workflow."""
        return self in (
            AgentMode.PLAN_EXPLORING,
            AgentMode.PLAN_PROPOSING,
            AgentMode.PLAN_WAITING,
            AgentMode.PLAN_EXECUTING,
        )

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` when the agent has stopped (IDLE or FINISHED)."""
        return self in (AgentMode.IDLE, AgentMode.FINISHED)

    @property
    def is_active(self) -> bool:
        """Return ``True`` when the agent is actively processing."""
        return not self.is_terminal


# ============================================================================
# Plan data models
# ============================================================================


@dataclass
class PlanStep:
    """A single step in an execution plan.

    Parameters
    ----------
    id:
        Unique step identifier, e.g. ``"step-1"``.
    description:
        Human-readable description of what this step accomplishes, e.g.
        ``"Read auth.py to understand the current login flow"``.
    tool_calls_expected:
        Tool names that are likely to be called during this step.
    files_affected:
        File paths expected to be read or modified.
    depends_on:
        IDs of steps that must complete before this step can begin.
    status:
        Current execution status — ``"pending"``, ``"in_progress"``, or
        ``"completed"``.
    """

    id: str
    description: str
    tool_calls_expected: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # "pending" | "in_progress" | "completed"

    @classmethod
    def from_dict(cls, d: dict) -> PlanStep:
        """Build a ``PlanStep`` from a JSON-decoded dict."""
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            tool_calls_expected=d.get("tool_calls_expected", []),
            files_affected=d.get("files_affected", []),
            depends_on=d.get("depends_on", []),
            status=d.get("status", "pending"),
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "id": self.id,
            "description": self.description,
            "tool_calls_expected": self.tool_calls_expected,
            "files_affected": self.files_affected,
            "depends_on": self.depends_on,
            "status": self.status,
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
        2–3 sentence overview of what the plan aims to achieve.
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

        Returns ``None`` when parsing fails so callers can feed the error
        back to the LLM rather than crashing.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse plan JSON: {exc}")
            return None

        if not isinstance(data, dict):
            logger.warning("Plan JSON is not a dict.")
            return None

        steps_data = data.get("steps", [])
        steps = [PlanStep.from_dict(s) for s in steps_data]

        return cls(
            id=data.get("id", uuid.uuid4().hex),
            title=data.get("title", "Untitled Plan"),
            summary=data.get("summary", ""),
            steps=steps,
            rationale=data.get("rationale", ""),
            risks=data.get("risks", []),
            estimated_files_touched=data.get(
                "estimated_files_touched", len(steps),
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
            deps = (
                f" (depends on: {', '.join(step.depends_on)})"
                if step.depends_on
                else ""
            )
            files = (
                f" [{', '.join(step.files_affected)}]"
                if step.files_affected
                else ""
            )
            lines.append(f"{i}. **{step.description}**{deps}{files}")

        lines.append("")
        lines.append(
            f"Estimated files touched: {self.estimated_files_touched}"
        )
        return "\n".join(lines)

    def format_for_prompt(self) -> str:
        """Render the plan compactly for inclusion in the system prompt.

        Used during ``PLAN_EXECUTING`` mode so the agent remembers the plan
        without re-reading it from the conversation.
        """
        lines: list[str] = [
            f"## Approved Plan: {self.title}",
            f"Summary: {self.summary}",
            "",
            "Steps:",
        ]
        for step in self.steps:
            status_icon = {
                "pending": "⬜",
                "in_progress": "▶️",
                "completed": "✅",
            }.get(step.status, "⬜")
            lines.append(f"  {status_icon} {step.id}: {step.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Progress tracking
    # ------------------------------------------------------------------

    @property
    def completed_steps(self) -> list[PlanStep]:
        """Return steps that have been completed."""
        return [s for s in self.steps if s.status == "completed"]

    @property
    def current_step(self) -> PlanStep | None:
        """Return the first in-progress step, or the first pending step."""
        for s in self.steps:
            if s.status == "in_progress":
                return s
        for s in self.steps:
            if s.status == "pending":
                return s
        return None

    @property
    def is_complete(self) -> bool:
        """Return ``True`` when all steps are completed."""
        return all(s.status == "completed" for s in self.steps) if self.steps else True

    def mark_step(self, step_id: str, status: str) -> bool:
        """Update a step's status.  Returns ``False`` if the step is unknown."""
        for s in self.steps:
            if s.id == step_id:
                s.status = status
                return True
        return False


# ============================================================================
# Complexity heuristic
# ============================================================================

# Keywords that suggest a complex, multi-step task (trigger plan mode).
_COMPLEXITY_KEYWORDS: list[str] = [
    "refactor",
    "implement",
    "redesign",
    "restructure",
    "migrate",
    "overhaul",
    "rewrite",
    "rearchitect",
    "add a feature",
    "build a",
]

# Phrases that suggest multiple files are involved.
_MULTI_FILE_INDICATORS: list[str] = [
    "across",
    "multiple files",
    "and also",
]

# Word-count threshold above which a request is automatically considered complex.
_COMPLEXITY_MIN_WORDS: int = 200


def classify_complexity(user_input: str) -> str:
    """Classify a user request as ``"simple"`` or ``"complex"``.

    The heuristic checks three signals (any one is sufficient to return
    ``"complex"``):

    1. **Keywords** — the request contains words like ``"refactor"``,
       ``"implement"``, ``"redesign"``, etc.
    2. **Length** — the request exceeds ``_COMPLEXITY_MIN_WORDS`` words.
    3. **Multi-file indicators** — phrases like ``"across"``, ``"multiple
       files"``, ``"and also"`` are present.

    Returns ``"simple"`` when none of the triggers match.
    """
    lowered = user_input.lower()

    # 1. Keyword check
    for kw in _COMPLEXITY_KEYWORDS:
        if kw in lowered:
            logger.debug(
                f"Complexity → complex (keyword: '{kw}')"
            )
            return "complex"

    # 2. Length check
    word_count = len(user_input.split())
    if word_count >= _COMPLEXITY_MIN_WORDS:
        logger.debug(
            f"Complexity → complex (length: {word_count} words)"
        )
        return "complex"

    # 3. Multi-file indicators
    for indicator in _MULTI_FILE_INDICATORS:
        if indicator in lowered:
            logger.debug(
                f"Complexity → complex (multi-file indicator: "
                f"'{indicator}')"
            )
            return "complex"

    logger.debug(f"Complexity → simple ({word_count} words)")
    return "simple"


# ============================================================================
# AgentStateMachine
# ============================================================================


class AgentStateMachine:
    """Manages the agent's operational mode and validates state transitions.

    The state machine drives the plan-mode workflow and provides helpers for
    mode-specific behaviour (system prompt selection, tool auto-approval).

    Parameters
    ----------
    initial_mode:
        The starting mode.  Defaults to ``AgentMode.IDLE``.

    Usage
    -----

    .. code-block:: python

        sm = AgentStateMachine()

        # Classify the user's request:
        if sm.classify_and_transition(user_input, force_plan=False):
            mode = sm.current_mode  # EXECUTING or PLAN_EXPLORING

        # After the agent finishes exploring:
        sm.transition(AgentMode.PLAN_PROPOSING)

        # After the user approves:
        sm.transition(AgentMode.PLAN_EXECUTING)

        # Check tool auto-approval:
        if sm.should_auto_approve_tool("read_file", Permission.READ):
            ...
    """

    # ------------------------------------------------------------------
    # Valid transitions (source → set of valid destinations)
    # ------------------------------------------------------------------

    _VALID_TRANSITIONS: dict[AgentMode, set[AgentMode]] = {
        AgentMode.IDLE: {
            AgentMode.EXECUTING,
            AgentMode.PLAN_EXPLORING,
        },
        AgentMode.EXECUTING: {
            AgentMode.FINISHED,
        },
        AgentMode.PLAN_EXPLORING: {
            AgentMode.PLAN_PROPOSING,
            AgentMode.FINISHED,  # agent gives up / error
        },
        AgentMode.PLAN_PROPOSING: {
            AgentMode.PLAN_WAITING,
            AgentMode.FINISHED,  # failed to produce a valid plan
        },
        AgentMode.PLAN_WAITING: {
            AgentMode.PLAN_EXECUTING,  # approved
            AgentMode.PLAN_EXPLORING,  # rejected with feedback
            AgentMode.FINISHED,        # rejected outright
        },
        AgentMode.PLAN_EXECUTING: {
            AgentMode.FINISHED,
        },
        AgentMode.FINISHED: {
            AgentMode.IDLE,  # reset for next turn
        },
    }

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        initial_mode: AgentMode = AgentMode.IDLE,
    ) -> None:
        self._mode = initial_mode
        self._previous_mode: AgentMode | None = None
        self._current_plan: Plan | None = None
        self._plan_pending: bool = False
        """When ``True``, the next user message should trigger plan mode.

        Set by the ``/plan`` slash command and consumed by
        :meth:`classify_and_transition`.
        """

    # ------------------------------------------------------------------
    # Mode accessors
    # ------------------------------------------------------------------

    @property
    def current_mode(self) -> AgentMode:
        """The current operational mode."""
        return self._mode

    @property
    def previous_mode(self) -> AgentMode | None:
        """The mode before the most recent transition."""
        return self._previous_mode

    @property
    def current_plan(self) -> Plan | None:
        """The currently active plan, if any."""
        return self._current_plan

    @property
    def plan_pending(self) -> bool:
        """Whether ``/plan`` was used and the next message should trigger."""
        return self._plan_pending

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def transition(self, target: AgentMode) -> bool:
        """Attempt to transition to *target*.

        Returns ``True`` on success, ``False`` if the transition is invalid.
        A warning is logged on invalid transitions.
        """
        valid = self._VALID_TRANSITIONS.get(self._mode, set())
        if target not in valid:
            logger.warning(
                f"Invalid state transition: {self._mode.value} → "
                f"{target.value}.  Valid targets: "
                f"{[t.value for t in valid]}."
            )
            return False

        logger.debug(
            f"State transition: {self._mode.value} → {target.value}"
        )
        self._previous_mode = self._mode
        self._mode = target
        return True

    def classify_and_transition(
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> AgentMode:
        """Classify *user_input* and transition from IDLE to the right mode.

        This is the main entry point for the CLI layer.  It runs the
        complexity heuristic (or respects *force_plan* / pending plan flag),
        executes the transition, and returns the resulting mode.

        Parameters
        ----------
        user_input:
            The raw user request.
        force_plan:
            When ``True`` (e.g. ``--plan`` CLI flag), plan mode is forced
            regardless of the heuristic.

        Returns
        -------
        AgentMode
            The new current mode (``EXECUTING`` or ``PLAN_EXPLORING``).
        """
        should_plan = (
            force_plan
            or self._plan_pending
            or classify_complexity(user_input) == "complex"
        )
        self._plan_pending = False  # consume the flag

        target = (
            AgentMode.PLAN_EXPLORING if should_plan else AgentMode.EXECUTING
        )
        self.transition(target)
        return self._mode

    def reset(self) -> None:
        """Reset to IDLE for the next user turn."""
        self._mode = AgentMode.IDLE
        self._previous_mode = None
        # Note: _current_plan persists across turns so PLAN_EXECUTING can
        # reference it.  Clear it explicitly via clear_plan() when done.

    def mark_finished(self) -> None:
        """Transition to FINISHED from any active mode."""
        if self._mode in (AgentMode.IDLE, AgentMode.FINISHED):
            return
        self.transition(AgentMode.FINISHED)

    # ------------------------------------------------------------------
    # Plan lifecycle
    # ------------------------------------------------------------------

    def set_plan(self, plan: Plan) -> None:
        """Store the current plan (e.g. after parsing the LLM's proposal)."""
        self._current_plan = plan
        logger.info(f"Plan set: {plan.title} ({len(plan.steps)} steps)")

    def clear_plan(self) -> None:
        """Discard the current plan."""
        self._current_plan = None

    def flag_plan_pending(self) -> None:
        """Mark that the next user message should trigger plan mode.

        Called by the ``/plan`` slash command.
        """
        self._plan_pending = True

    def approve_plan(self) -> bool:
        """Approve the current plan and transition to PLAN_EXECUTING.

        Returns ``False`` if no plan is set or the transition is invalid.
        """
        if self._current_plan is None:
            logger.warning("Cannot approve: no plan is set.")
            return False
        return self.transition(AgentMode.PLAN_EXECUTING)

    def reject_plan(self, *, feedback: str = "") -> bool:
        """Reject the current plan.

        When *feedback* is provided, transition back to PLAN_EXPLORING so
        the agent can revise.  Otherwise transition to FINISHED.
        """
        self._current_plan = None
        if feedback:
            logger.info(
                "Plan rejected with feedback; returning to exploring."
            )
            return self.transition(AgentMode.PLAN_EXPLORING)
        logger.info("Plan rejected outright; finishing.")
        return self.transition(AgentMode.FINISHED)

    # ------------------------------------------------------------------
    # Mode → system prompt mapping
    # ------------------------------------------------------------------

    def get_mode_hint(self) -> str:
        """Return the mode string expected by
        :class:`~toddler.agent.system_prompt.SystemPromptBuilder`.

        Maps internal :class:`AgentMode` values to the strings that
        :meth:`SystemPromptBuilder.build` understands:
        ``"execute"``, ``"plan_exploring"``, or ``"plan_executing"``.
        """
        _map: dict[AgentMode, str] = {
            AgentMode.EXECUTING: "execute",
            AgentMode.PLAN_EXPLORING: "plan_exploring",
            AgentMode.PLAN_PROPOSING: "plan_exploring",
            AgentMode.PLAN_WAITING: "plan_exploring",
            AgentMode.PLAN_EXECUTING: "plan_executing",
        }
        return _map.get(self._mode, "execute")

    def get_system_prompt_extension(self) -> str:
        """Return mode-specific instructions for the current mode.

        These are appended to the system prompt.  For convenience, this
        delegates to :class:`SystemPromptBuilder` via :meth:`get_mode_hint`
        so callers don't need both objects.
        """
        from toddler.agent.system_prompt import SystemPromptBuilder

        hint = self.get_mode_hint()
        return SystemPromptBuilder.mode_instructions(hint)

    # ------------------------------------------------------------------
    # Tool auto-approval
    # ------------------------------------------------------------------

    @staticmethod
    def should_auto_approve_tool(
        mode: AgentMode,
        tool_name: str,
        permission: Permission,
    ) -> bool | None:
        """Return whether a tool should be auto-approved in *mode*.

        Returns
        -------
        bool | None
            ``True`` — auto-approve without confirmation.
            ``False`` — always require confirmation.
            ``None`` — use the default permission-based logic.
        """
        # In PLAN_EXPLORING mode, never auto-approve mutating tools —
        # the agent is supposed to be READ-ONLY.
        if mode == AgentMode.PLAN_EXPLORING:
            from toddler.tools.base import Permission

            # Only auto-approve READ and SHELL_SAFE.
            return permission not in (
                Permission.WRITE, Permission.SHELL_DANGEROUS,
            )

        # In PLAN_EXECUTING mode, the user already approved the plan.
        # Auto-approve READ / SHELL_SAFE; still confirm dangerous operations.
        if mode == AgentMode.PLAN_EXECUTING:
            from toddler.tools.base import Permission

            # Still confirm dangerous shell commands.
            return permission != Permission.SHELL_DANGEROUS

        # For all other modes, defer to the standard permission logic.
        return None

    # ------------------------------------------------------------------
    # Prompt for plan generation
    # ------------------------------------------------------------------

    @staticmethod
    def plan_proposal_prompt(
        user_request: str,
        *,
        research_context: str = "",
    ) -> str:
        """Build the prompt that asks the LLM to produce a structured plan.

        This is sent as a follow-up user message after the agent finishes
        exploring in ``PLAN_EXPLORING`` mode.

        Parameters
        ----------
        user_request:
            The original user request.
        research_context:
            Any notes or context gathered during the exploration phase.
        """
        context_block = ""
        if research_context:
            context_block = (
                f"\n\nContext gathered during research:\n{research_context}"
            )

        return f"""\
Based on your research, propose a concrete execution plan for the following \
request:

> {user_request}{context_block}

Respond with a JSON plan object in this exact format:

```json
{{
  "title": "Short title for the plan",
  "summary": "2-3 sentence overview of what this plan will accomplish",
  "steps": [
    {{
      "id": "step-1",
      "description": "Detailed description of this step",
      "tool_calls_expected": ["tool_name_1", "tool_name_2"],
      "files_affected": ["path/to/file.py"],
      "depends_on": []
    }}
  ],
  "rationale": "Why this approach was chosen",
  "risks": ["Potential risk 1", "Potential risk 2"],
  "estimated_files_touched": 3
}}
```

Guidelines:
- Steps must be concrete and actionable — each step should be achievable with
  one or two tool calls.
- Order steps so dependencies are satisfied before dependents.
- Include ALL files you expect to read or modify.
- Be realistic about risks — what could go wrong?
- Keep the plan focused: 3–8 steps is ideal.

Return ONLY the JSON object, no other text."""


# ============================================================================
# Re-export for convenience
# ============================================================================

__all__ = [
    "AgentMode",
    "AgentStateMachine",
    "Plan",
    "PlanStep",
    "classify_complexity",
]
