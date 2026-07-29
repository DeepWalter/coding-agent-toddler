"""Planner — the plan-mode orchestration loop.

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
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    PlanProposed,
)
from toddler.agent.state_machine import AgentMode, AgentStateMachine, Plan
from toddler.llm import Message
from toddler.llm.responses import LLMResponse

if TYPE_CHECKING:
    from toddler.agent.loop import AgentLoop
    from toddler.config.settings import Settings
    from toddler.context.conversation_context import ConversationContext
    from toddler.llm.base import BaseLLMProvider

__all__ = ["Planner"]

logger = logging.getLogger(__name__)


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
        Optional :class:`AgentStateMachine`.  When *None*, a default
        instance is created.
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        context: ConversationContext,
        settings: Settings,
        agent_loop: AgentLoop,
        *,
        state_machine: AgentStateMachine | None = None,
    ) -> None:
        self._llm = llm_provider
        self._ctx = context
        self._settings = settings
        self._agent_loop = agent_loop
        self._sm = state_machine or AgentStateMachine()

        # Gating state — same asyncio.Event pattern as AgentLoop._approval_event.
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
                    explore_input, mode="plan_exploring",
                ):
                    yield event
                self._sm.transition(AgentMode.PLAN_PROPOSING)
                continue

            elif current_mode == AgentMode.PLAN_PROPOSING:
                plan = await self._generate_plan(original_request)
                if plan is None:
                    self._sm.mark_finished()
                    yield AgentError(
                        message=(
                            "Failed to generate a valid plan. "
                            "The LLM did not produce parseable JSON. "
                            "Try rephrasing your request."
                        ),
                        recoverable=False,
                    )
                    return
                self._sm.set_plan(plan)
                self._sm.transition(AgentMode.PLAN_WAITING)
                continue

            elif current_mode == AgentMode.PLAN_WAITING:
                # Clear BEFORE yielding so the caller's set() isn't
                # immediately wiped by a trailing clear() on resume.
                self._plan_decision_event.clear()
                yield PlanProposed(plan=self._sm.current_plan)  # type: ignore[arg-type]
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

    def approve_plan(self) -> bool:
        """Approve the current plan and unblock :meth:`run`.

        Returns ``True`` if the plan was successfully approved.
        """
        success = self._sm.approve_plan()
        if not success:
            self._sm.mark_finished()
        self._plan_decision_event.set()
        return success

    def reject_plan(self, *, feedback: str = "") -> None:
        """Reject the current plan and unblock :meth:`run`.

        When *feedback* is provided the agent will re-explore and propose
        a revised plan.  Otherwise the turn finishes.
        """
        self._plan_feedback = feedback
        self._sm.reject_plan(feedback=feedback)
        self._plan_decision_event.set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def plan(self) -> Plan | None:
        """The current plan, or *None* if no plan has been set."""
        return self._sm.current_plan

    @property
    def current_mode(self) -> AgentMode:
        """The current state-machine mode."""
        return self._sm.current_mode

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

        prompt = AgentStateMachine.plan_proposal_prompt(
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
