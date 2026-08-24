"""Agent state machine — mode management and complexity heuristic.

Phase 10: Full state machine covering all operational states and valid
transitions.  Provides the plan-mode workflow (explore → propose → approve →
execute) plus the complexity heuristic that can auto-trigger plan mode for
non-trivial requests.

The :class:`~toddler.agent.planner.Planner` owns the actual Plan/PlanStep
data models and lifecycle — the state machine only tracks the current
operational mode and validates transitions.

State diagram (see ``docs/plans/plan.md``):

```
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
```
"""

from __future__ import annotations

import logging
from enum import Enum

__all__ = [
    "AgentMode",
    "AgentStateMachine",
    "classify_complexity",
]

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

    @property
    def display_label(self) -> str:
        """Short user-facing mode label for the REPL header."""
        return "PLAN" if self.is_plan_related else "EXECUTE"


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

# Word-count threshold above which a request is automatically considered
# complex.
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
        The starting mode.  Defaults to :attr:`AgentMode.IDLE`.

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
    def plan_pending(self) -> bool:
        """Whether ``/plan`` was used and the next message should trigger."""
        return self._plan_pending

    @property
    def is_plan_exploring(self) -> bool:
        """Whether the machine is in the plan exploration phase."""
        return self._mode == AgentMode.PLAN_EXPLORING

    @property
    def is_plan_executing(self) -> bool:
        """Whether the machine is executing an approved plan."""
        return self._mode == AgentMode.PLAN_EXECUTING

    @property
    def is_executing(self) -> bool:
        """Whether the machine is in an execution phase — plain or plan."""
        return self._mode in (AgentMode.EXECUTING, AgentMode.PLAN_EXECUTING)

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

    def mark_finished(self) -> None:
        """Transition to FINISHED from any active mode."""
        if self._mode in (AgentMode.IDLE, AgentMode.FINISHED):
            return
        self.transition(AgentMode.FINISHED)

    # ------------------------------------------------------------------
    # Plan mode flags
    # ------------------------------------------------------------------

    def flag_plan_pending(self) -> None:
        """Mark that the next user message should trigger plan mode.

        Called by the ``/plan`` slash command.
        """
        self._plan_pending = True

    def clear_plan_pending(self) -> None:
        """Cancel a pending plan flag set by ``/plan``.

        Called by ``/mode manual`` and ``/mode auto`` so the next turn
        starts in execute mode rather than plan mode.  The complexity
        heuristic may still trigger plan mode for complex requests.
        """
        self._plan_pending = False

    # ------------------------------------------------------------------
    # Mode → system prompt mapping
    # ------------------------------------------------------------------

    def get_mode_hint(self) -> str:
        """Return the mode string expected by
        :class:`~toddler.context.builder.SystemPromptBuilder`.

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
        from toddler.context.builder import SystemPromptBuilder

        hint = self.get_mode_hint()
        return SystemPromptBuilder.mode_instructions(hint)
