"""Planner — plan data models and orchestration loop.

The Planner owns the plan lifecycle (explore → propose → wait) and is
symmetric to :class:`AgentLoop`: both are async generators in the agent
package that yield :class:`AgentEvent` subclasses and use the same
``asyncio.Event``-based gating protocol for user decisions.

``AgentLoop`` handles the execution loop (think → act → observe).
``Planner`` handles the plan loop (explore → propose → wait).

``PLAN_EXECUTING`` is *not* part of the plan loop — after the user approves
the plan, the caller runs :class:`AgentLoop` directly with
``mode="plan_executing"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    FatalAgentError,
    PlanProposed,
)
from toddler.agent.state_machine import AgentMode, AgentStateMachine
from toddler.llm import Message
from toddler.llm.responses import LLMResponse

if TYPE_CHECKING:
    from toddler.agent.loop import AgentLoop
    from toddler.config.settings import Settings
    from toddler.context.manager import ContextManager
    from toddler.llm.base import BaseLLMProvider

__all__ = ["Plan", "PlanStep", "Planner", "plan_proposal_prompt"]

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
        report progress via the ``plan_update`` tool, then appends the plan
        body.  The plan body is frozen at approval time; live step statuses
        are tracked separately at runtime and never rendered here.
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
            "Execute it step by step. Use the plan_update tool to "
            "report progress: call plan_update(step_id=..., "
            "status='in_progress') before starting each step, and "
            "plan_update(step_id=..., status='completed') after "
            "finishing it. When marking the final step completed, "
            "call plan_update alone in its own response — do not "
            "write any text alongside it. Once the tool result "
            "returns, end with a brief summary of what was "
            "accomplished, noting any deviations "
            f"from the plan:\n\n{plan_text}"
        )


# ============================================================================
# Plan proposal prompt
# ============================================================================


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
      "description": "Detailed description of this step",
      "tool_calls_expected": ["tool_name_1", "tool_name_2"],
      "files_affected": ["path/to/file.py"]
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
# Planner
# ============================================================================


class Planner:
    """Orchestrate the plan-mode lifecycle: explore → propose → wait.

    Runs as an async generator yielding :class:`AgentEvent` objects — just
    like :class:`AgentLoop`.  During exploration phases, events from the
    underlying :class:`AgentLoop` are passed through transparently.  When the
    plan is ready for user review, a :class:`PlanProposed` event is yielded
    and the generator blocks on user input via :meth:`approve_plan` /
    :meth:`reject_plan`.

    Parameters
    ----------
    llm_provider:
        The LLM backend — used for the plan-generation call (no tools).
    context:
        The conversation context for message injection and research
        collection.
    settings:
        Resolved settings (limits, streaming, etc.).
    agent_loop:
        The :class:`AgentLoop` instance used for exploration phases.
    state_machine:
        The :class:`AgentStateMachine` backing the plan loop.
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        context: ContextManager,
        settings: Settings,
        agent_loop: AgentLoop,
        *,
        state_machine: AgentStateMachine,
    ) -> None:
        self._llm = llm_provider
        self._ctx = context
        self._settings = settings
        self._agent_loop = agent_loop
        self._sm = state_machine

        # Plan state — the Planner owns the Plan object lifecycle.
        self._plan: Plan | None = None

        # Gating state
        self._plan_decision_event = asyncio.Event()
        self._plan_feedback: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(  # noqa: C901
        self,
        user_input: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run the plan loop for a single user request.

        The caller MUST classify the request first and transition the state
        machine to :attr:`AgentMode.PLAN_EXPLORING` before calling this
        method.  The planner handles the full plan lifecycle from there:
        explore → propose → wait (with feedback loop support).

        Returns (generator exhaustion) when a plan is approved, rejected, or
        an error occurs.

        Parameters
        ----------
        user_input:
            The raw user request.
        """
        # --- Plan-mode path: multi-phase orchestration ---
        original_request = user_input
        explore_input = user_input

        while True:
            current_mode = self._sm.current_mode

            if current_mode == AgentMode.PLAN_EXPLORING:
                async for event in self._agent_loop.run(
                    explore_input, mode=self._sm.get_mode_hint(),
                ):
                    yield event
                self._sm.transition(AgentMode.PLAN_PROPOSING)
                continue

            elif current_mode == AgentMode.PLAN_PROPOSING:
                plan = await self._generate_plan(original_request)
                if plan is None:
                    self._sm.mark_finished()
                    yield FatalAgentError(
                        message=(
                            "Failed to generate a valid plan. "
                            "The LLM did not produce parseable JSON. "
                            "Try rephrasing your request."
                        ),
                    )
                    return
                self._set_plan(plan)
                self._sm.transition(AgentMode.PLAN_WAITING)
                continue

            elif current_mode == AgentMode.PLAN_WAITING:
                # Clear BEFORE yielding so the caller's set() isn't
                # immediately wiped by a trailing clear() on resume.
                self._plan_decision_event.clear()
                yield PlanProposed(plan=self._plan)
                await self._plan_decision_event.wait()

                # Decision was made by approve_plan() / reject_plan()
                if self._sm.current_mode == AgentMode.PLAN_EXECUTING:
                    # Approved — caller handles execution.
                    return

                elif self._sm.current_mode == AgentMode.PLAN_EXPLORING:
                    # Rejected with feedback — loop back to explore.
                    feedback_msg = (
                        "The proposed plan was rejected with this feedback: "
                        f"{self._plan_feedback}\n\n"
                        "Please reconsider the original request and "
                        "re-explore the codebase, addressing the feedback "
                        "above."
                    )
                    self._ctx.append(Message.user(feedback_msg))
                    explore_input = (
                        f"Revise your research based on this feedback: "
                        f"{self._plan_feedback}"
                    )
                    continue

                else:
                    # Rejected outright — FINISHED.
                    yield AgentFinished(
                        reason="Plan rejected by user.",
                        usage=None,
                    )
                    return

            elif current_mode == AgentMode.FINISHED:
                return

            else:
                logger.error(
                    "Unexpected mode in Planner.run: %s", current_mode,
                )
                return

    def approve_plan(self, *, plan_id: str) -> bool:
        """Approve the plan with *plan_id* and unblock :meth:`run`.

        Returns ``True`` when the approval took effect (the machine moved
        to ``PLAN_EXECUTING``).  Returns ``False`` when the call was
        ignored as stale input: the machine is not waiting on a decision
        (e.g. a duplicate approval or an approval arriving after a
        rejection), or *plan_id* is not the plan currently awaiting a
        decision.  Stale input must not clobber the state — the machine
        keeps waiting for the current plan's own decision.
        """
        if self._sm.current_mode != AgentMode.PLAN_WAITING:
            logger.warning(
                "Cannot approve: not waiting on a decision "
                "(mode is %s).", self._sm.current_mode.value,
            )
            return False
        if self._plan is not None and self._plan.id != plan_id:
            # Stale — an approval meant for a previous proposal.  The
            # event is deliberately NOT set: the machine keeps waiting
            # for the current plan's own decision.
            logger.warning(
                "Cannot approve: plan %s is not the current plan.",
                plan_id,
            )
            return False
        if self._plan is None:
            # Invariant fallback: PLAN_WAITING is normally entered with
            # a plan set, so this only fires if a reject transition
            # failed earlier (mode stayed PLAN_WAITING with the plan
            # cleared).  No plan exists to approve, so end the turn
            # rather than leave run() blocked forever on the decision
            # event.
            logger.warning("Cannot approve: no plan is set.")
            self._sm.mark_finished()
            self._plan_decision_event.set()
            return False
        success = self._sm.transition(AgentMode.PLAN_EXECUTING)
        if not success:
            self._sm.mark_finished()
        self._plan_decision_event.set()
        return success

    def reject_plan(self, *, plan_id: str, feedback: str = "") -> None:
        """Reject the plan with *plan_id* and unblock :meth:`run`.

        When *feedback* is provided the agent will re-explore and propose
        a revised plan.  Otherwise the turn finishes.

        Ignored (with a warning) when the machine is not waiting on a
        decision, or when *plan_id* is not the plan currently awaiting a
        decision — stale input must not clobber the earlier decision's
        state or kill the pending proposal.
        """
        if self._sm.current_mode != AgentMode.PLAN_WAITING:
            logger.warning(
                "Cannot reject: not waiting on a decision "
                "(mode is %s).", self._sm.current_mode.value,
            )
            return
        if self._plan is not None and self._plan.id != plan_id:
            # Stale — a rejection meant for a previous proposal.  The
            # event is deliberately NOT set: the machine keeps waiting
            # for the current plan's own decision.
            logger.warning(
                "Cannot reject: plan %s is not the current plan.",
                plan_id,
            )
            return
        if self._plan is None:
            # Invariant fallback, mirroring approve_plan(): no plan
            # exists to reject, so end the turn rather than leave run()
            # blocked forever on the decision event.
            logger.warning("Cannot reject: no plan is set.")
            self._sm.mark_finished()
            self._plan_decision_event.set()
            return
        self._plan_feedback = feedback
        self._plan = None
        if feedback:
            logger.info(
                "Plan rejected with feedback; returning to exploring."
            )
            self._sm.transition(AgentMode.PLAN_EXPLORING)
        else:
            logger.info("Plan rejected outright; finishing.")
            self._sm.transition(AgentMode.FINISHED)
        self._plan_decision_event.set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def plan(self) -> Plan | None:
        """The current plan, or *None* if no plan has been set."""
        return self._plan

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_plan(self, plan: Plan) -> None:
        """Store the current plan (e.g. after parsing the LLM's proposal)."""
        self._plan = plan
        logger.info(f"Plan set: {plan.title} ({len(plan.steps)} steps)")

    async def _generate_plan(self, user_request: str) -> Plan | None:
        """Ask the LLM to produce a structured JSON plan.

        Collects research context from the exploration phase (recent
        assistant messages), sends the plan-proposal prompt to the LLM
        without tools, and parses the JSON response.

        Returns ``None`` when the LLM fails to produce valid JSON.
        """
        # Collect research context from the exploration phase.
        research_context = ""
        assistant_texts = []
        for msg in self._ctx.messages:
            if msg.role == "assistant":
                text = msg.text.strip()
                if text:
                    assistant_texts.append(text)
        research_context = "\n\n".join(assistant_texts[-5:])
        if len(research_context) > 4000:
            research_context = research_context[:4000] + (
                "\n... (truncated)"
            )

        prompt = plan_proposal_prompt(
            user_request,
            research_context=research_context,
        )

        try:
            response = await self._llm.generate(
                [Message.user(prompt)],
                tools=[],
                max_tokens=2048,
                temperature=0.0,
                stream=False,
            )
            # Non-streaming response.
            if isinstance(response, LLMResponse):
                text = (
                    response.messages[0].text
                    if response.messages else ""
                )
            else:
                logger.error(
                    "Unexpected response type from plan LLM call"
                )
                return None
        except Exception:
            logger.exception("Plan generation LLM call failed")
            return None

        plan = Plan.from_json(text)
        if plan is None:
            logger.warning(
                "Failed to parse plan JSON.  Raw response: %.200s...",
                text,
            )
        elif not plan.steps:
            logger.warning("Plan has no steps — rejecting")
            plan = None

        return plan
