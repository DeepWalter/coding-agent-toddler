"""Tests for plan mode — state machine, complexity heuristic, plan
serialization, tool gating, and coordinator orchestration.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest

from toddler.agent.events import (
    AgentError,
    AgentFinished,
    PlanProposed,
)
from toddler.agent.state_machine import (
    AgentMode,
    AgentStateMachine,
    Plan,
    PlanStep,
    classify_complexity,
)
from toddler.llm import ContentBlock, LLMResponse, Message, TokenUsage
from toddler.llm.base import BaseLLMProvider



# ============================================================================
# Helper — build a Plan with a generated id
# ============================================================================


def _plan(**kwargs) -> Plan:
    """Shorthand for creating a Plan with a random id."""
    defaults = {
        "id": uuid.uuid4().hex,
        "title": "Test Plan",
        "summary": "A test plan.",
        "steps": [PlanStep(id="step-1", description="Do it")],
    }
    defaults.update(kwargs)
    return Plan(**defaults)


# ============================================================================
# classify_complexity() tests
# ============================================================================


class TestClassifyComplexity:

    def test_simple_request_returns_simple(self):
        assert classify_complexity("fix the typo in README") == "simple"
        assert classify_complexity("add a comment to main.py") == "simple"

    def test_keyword_triggers_complex(self):
        for kw in [
            "refactor", "implement", "redesign", "restructure",
            "migrate", "overhaul", "rewrite", "rearchitect",
        ]:
            assert classify_complexity(f"{kw} the auth module") == "complex"

    def test_add_a_feature_triggers_complex(self):
        assert classify_complexity("add a feature for user login") == "complex"

    def test_build_a_triggers_complex(self):
        assert classify_complexity("build a REST API for users") == "complex"

    def test_long_request_triggers_complex(self):
        assert classify_complexity("please " * 201) == "complex"

    def test_199_words_is_simple(self):
        assert classify_complexity("please " * 199) == "simple"

    def test_multi_file_indicator_triggers_complex(self):
        assert classify_complexity("update logging across the codebase") == "complex"
        assert classify_complexity("change error handling in multiple files") == "complex"
        assert classify_complexity("fix imports and also update tests") == "complex"


# ============================================================================
# AgentStateMachine transition tests
# ============================================================================


class TestAgentStateMachineTransitions:

    @pytest.fixture
    def sm(self) -> AgentStateMachine:
        return AgentStateMachine()

    def test_initial_mode_is_idle(self, sm):
        assert sm.current_mode == AgentMode.IDLE

    def test_classify_simple_goes_to_executing(self, sm):
        assert sm.classify_and_transition("fix a typo") == AgentMode.EXECUTING

    def test_classify_complex_goes_to_plan_exploring(self, sm):
        assert (
            sm.classify_and_transition("refactor the CLI app")
            == AgentMode.PLAN_EXPLORING
        )

    def test_force_plan_overrides_simple(self, sm):
        assert (
            sm.classify_and_transition("fix a typo", force_plan=True)
            == AgentMode.PLAN_EXPLORING
        )

    def test_flag_plan_pending_forces_next_turn(self, sm):
        sm.flag_plan_pending()
        assert sm.classify_and_transition("fix a typo") == AgentMode.PLAN_EXPLORING
        # Reset and re-classify — flag is consumed, so normal EXECUTING.
        sm.reset()
        assert sm.classify_and_transition("fix another typo") == AgentMode.EXECUTING

    def test_exploring_to_proposing(self, sm):
        sm.classify_and_transition("refactor auth")
        assert sm.transition(AgentMode.PLAN_PROPOSING) is True

    def test_proposing_to_waiting(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.transition(AgentMode.PLAN_PROPOSING)
        assert sm.transition(AgentMode.PLAN_WAITING) is True

    def test_approve_plan_fails_without_plan(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.transition(AgentMode.PLAN_WAITING)
        assert sm.approve_plan() is False

    def test_approve_plan_succeeds_with_plan(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.set_plan(_plan())
        sm.transition(AgentMode.PLAN_WAITING)
        assert sm.approve_plan() is True
        assert sm.current_mode == AgentMode.PLAN_EXECUTING

    def test_reject_plan_without_feedback(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.transition(AgentMode.PLAN_WAITING)
        assert sm.reject_plan() is True
        assert sm.current_mode == AgentMode.FINISHED

    def test_reject_plan_with_feedback(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.transition(AgentMode.PLAN_WAITING)
        assert sm.reject_plan(feedback="Needs more detail") is True
        assert sm.current_mode == AgentMode.PLAN_EXPLORING

    def test_feedback_loop_complete(self, sm):
        """Full approve → reject-with-feedback → re-approve cycle."""
        sm.classify_and_transition("refactor auth")
        assert sm.current_mode == AgentMode.PLAN_EXPLORING
        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.set_plan(_plan(title="Auth Refactor"))
        sm.transition(AgentMode.PLAN_WAITING)

        sm.reject_plan(feedback="Add more steps")
        assert sm.current_mode == AgentMode.PLAN_EXPLORING

        sm.transition(AgentMode.PLAN_PROPOSING)
        sm.set_plan(_plan(title="Auth Refactor v2"))
        sm.transition(AgentMode.PLAN_WAITING)

        assert sm.approve_plan() is True
        assert sm.current_mode == AgentMode.PLAN_EXECUTING

    def test_mark_finished_from_executing(self, sm):
        sm.classify_and_transition("fix a typo")
        sm.mark_finished()
        assert sm.current_mode == AgentMode.FINISHED

    def test_reset_goes_to_idle(self, sm):
        sm.classify_and_transition("refactor auth")
        sm.reset()
        assert sm.current_mode == AgentMode.IDLE

    def test_invalid_transition_returns_false(self, sm):
        assert sm.transition(AgentMode.PLAN_WAITING) is False
        assert sm.current_mode == AgentMode.IDLE


# ============================================================================
# Plan serialization tests
# ============================================================================


class TestPlanSerialization:

    def test_plan_to_json_and_back(self):
        plan = _plan(
            title="Refactor Auth",
            summary="Extract authentication into its own module.",
            steps=[
                PlanStep(
                    id="step-1", description="Create auth.py module",
                    tool_calls_expected=["write_file"],
                    files_affected=["auth.py"],
                ),
                PlanStep(
                    id="step-2", description="Update imports in main.py",
                    tool_calls_expected=["edit_file", "read_file"],
                    files_affected=["main.py"], depends_on=["step-1"],
                ),
            ],
            rationale="Better separation of concerns.",
            risks=["Breaking import paths", "Session state loss"],
            estimated_files_touched=3,
        )

        json_str = plan.to_json()
        data = json.loads(json_str)
        assert data["title"] == "Refactor Auth"
        assert len(data["steps"]) == 2
        assert data["steps"][1]["depends_on"] == ["step-1"]

        restored = Plan.from_json(json_str)
        assert restored is not None
        assert restored.title == "Refactor Auth"
        assert len(restored.steps) == 2
        assert restored.steps[1].depends_on == ["step-1"]
        assert restored.risks == ["Breaking import paths", "Session state loss"]

    def test_plan_from_json_minimal(self):
        data = {
            "title": "Minimal Plan",
            "summary": "",
            "steps": [{"id": "s1", "description": "Do one thing"}],
        }
        plan = Plan.from_json(json.dumps(data))
        assert plan is not None
        assert plan.title == "Minimal Plan"
        assert len(plan.steps) == 1
        assert plan.rationale == ""
        assert plan.risks == []

    def test_plan_from_json_with_markdown_fences(self):
        data = {
            "title": "Fenced Plan",
            "summary": "Plan inside fences.",
            "steps": [{"id": "s1", "description": "Step"}],
        }
        raw = "```json\n" + json.dumps(data) + "\n```"
        plan = Plan.from_json(raw)
        assert plan is not None
        assert plan.title == "Fenced Plan"

    def test_plan_from_json_just_fences(self):
        data = {
            "title": "Fenced", "summary": "",
            "steps": [{"id": "s1", "description": "S"}],
        }
        raw = "```\n" + json.dumps(data) + "\n```"
        plan = Plan.from_json(raw)
        assert plan is not None
        assert plan.title == "Fenced"

    def test_plan_from_json_invalid_returns_none(self):
        assert Plan.from_json("not json at all") is None
        assert Plan.from_json("") is None
        # Empty dict succeeds with defaults — valid Plan object.
        empty = Plan.from_json("{}")
        assert empty is not None
        assert empty.title == "Untitled Plan"

    def test_plan_format_for_display(self):
        plan = _plan(
            title="Test Plan",
            summary="A plan for testing.",
            steps=[
                PlanStep(id="1", description="First step"),
                PlanStep(id="2", description="Second step"),
            ],
            rationale="Testing is good.",
            risks=["Tests might fail"],
        )
        output = plan.format_for_display()
        assert "Test Plan" in output
        assert "First step" in output
        assert "Testing is good" in output
        assert "Tests might fail" in output

    def test_plan_format_for_prompt(self):
        plan = _plan(
            title="Execute Plan",
            steps=[
                PlanStep(id="1", description="Step one", status="completed"),
                PlanStep(id="2", description="Step two", status="in_progress"),
                PlanStep(id="3", description="Step three", status="pending"),
            ],
        )
        output = plan.format_for_prompt()
        assert "Execute Plan" in output
        assert "Step one" in output


# ============================================================================
# plan_proposal_prompt() tests
# ============================================================================


class TestPlanProposalPrompt:

    def test_prompt_includes_user_request(self):
        prompt = AgentStateMachine.plan_proposal_prompt(
            "refactor the database layer",
        )
        assert "refactor the database layer" in prompt

    def test_prompt_includes_research_context(self):
        prompt = AgentStateMachine.plan_proposal_prompt(
            "refactor the database layer",
            research_context="Found 15 files using the old DB API.",
        )
        assert "Found 15 files using the old DB API." in prompt

    def test_prompt_asks_for_json_format(self):
        prompt = AgentStateMachine.plan_proposal_prompt("fix bugs")
        assert "JSON" in prompt
        assert "title" in prompt
        assert "steps" in prompt

    def test_prompt_without_context_omits_context_block(self):
        prompt = AgentStateMachine.plan_proposal_prompt("do something")
        assert "Context gathered" not in prompt


# ============================================================================
# ============================================================================
# Plan step progress tracking
# ============================================================================


class TestPlanStepTracking:

    def test_all_steps_start_pending(self):
        plan = _plan(steps=[
            PlanStep(id="1", description="A"),
            PlanStep(id="2", description="B"),
        ])
        # current_step returns the first pending step.
        assert plan.current_step is not None
        assert plan.current_step.id == "1"
        assert plan.completed_steps == []
        assert plan.is_complete is False

    def test_mark_step_advances_progress(self):
        plan = _plan(steps=[
            PlanStep(id="1", description="A"),
            PlanStep(id="2", description="B"),
            PlanStep(id="3", description="C"),
        ])
        assert plan.mark_step("1", "completed") is True
        assert plan.steps[0].status == "completed"
        assert len(plan.completed_steps) == 1

        assert plan.mark_step("2", "in_progress") is True
        assert plan.steps[1].status == "in_progress"

    def test_mark_step_unknown_id_returns_false(self):
        plan = _plan(steps=[PlanStep(id="1", description="A")])
        assert plan.mark_step("nonexistent", "completed") is False

    def test_is_complete_when_all_done(self):
        plan = _plan(steps=[PlanStep(id="1", description="A")])
        plan.mark_step("1", "completed")
        assert plan.is_complete is True


# ============================================================================
# SessionCoordinator plan workflow (integration test with mock LLM)
# ============================================================================


class MockPlanLLMProvider(BaseLLMProvider):
    """Mock LLM that returns a valid plan JSON for plan proposal calls,
    and a simple end-turn for agent phases.
    """

    def __init__(self, plan_json: dict | None = None):
        self._plan_json = plan_json or {
            "title": "Mock Plan",
            "summary": "A mocked plan for testing.",
            "steps": [{"id": "step-1", "description": "Do the thing"}],
        }
        self.call_count = 0
        self.messages_history: list[list[Message]] = []

    @property
    def max_context_length(self) -> int:
        return 128_000

    async def generate(
        self, messages, tools, *, max_tokens=4096, temperature=0.0, stream=True,
    ):
        self.messages_history.append(messages)
        self.call_count += 1

        # Empty tools → plan proposal call (non-streaming).
        if not tools:
            return LLMResponse(
                messages=[Message.assistant([
                    ContentBlock.text_block(json.dumps(self._plan_json)),
                ])],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=50, output_tokens=50),
            )

        # Standard agent call → simple end-turn.
        return LLMResponse(
            messages=[Message.assistant([
                ContentBlock.text_block("Research complete."),
            ])],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=20, output_tokens=10),
        )

    def count_tokens(self, messages):
        return 100

    async def generate_compact(self, prompt):
        return "compacted"


class TestSessionCoordinatorPlanWorkflow:

    @pytest.fixture
    def settings(self):
        from toddler.config.settings import Settings
        return Settings(
            streaming_enabled=False,
        )

    @pytest.fixture
    def storage_mgr(self, tmp_path):
        from toddler.session.store import SQLiteStore
        from toddler.session.manager import StorageManager

        db_path = tmp_path / "test_plan.db"
        store = SQLiteStore(db_path)
        store.open()
        return StorageManager(store)

    @pytest.fixture
    def llm(self):
        return MockPlanLLMProvider()

    @pytest.fixture
    async def coordinator(self, settings, storage_mgr, llm):
        from toddler.session.coordinator import SessionCoordinator

        coord = SessionCoordinator(
            settings=settings,
            storage_manager=storage_mgr,
            llm=llm,
        )
        await coord.resolve()
        return coord

    async def _collect(self, gen) -> list:
        events = []
        async for event in gen:
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_simple_execution_path(self, coordinator):
        gen = coordinator.process_turn("fix a typo")
        events = await self._collect(gen)
        has_plan = any(isinstance(e, PlanProposed) for e in events)
        assert not has_plan, "Simple request should not trigger plan mode"
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_plan_mode_yields_plan_proposed(self, coordinator):
        """Collect events up to PlanProposed (avoids hanging on approval)."""
        gen = coordinator.process_turn("refactor the database layer")
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, PlanProposed):
                break
        plan_events = [e for e in events if isinstance(e, PlanProposed)]
        assert len(plan_events) == 1
        assert plan_events[0].plan.title == "Mock Plan"

    @pytest.mark.asyncio
    async def test_plan_generation_failure(self, coordinator, llm):
        """Zero-step plan is rejected as invalid — yields AgentError."""
        llm._plan_json = {"title": "Bad", "steps": []}
        gen = coordinator.process_turn("refactor the database layer")
        events = await self._collect(gen)
        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) >= 1

    @pytest.mark.asyncio
    async def test_approve_plan_executes(self, coordinator):
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_reject_plan_outright_finishes(self, coordinator):
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.reject_plan()
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert "rejected" in finished[0].reason.lower()

    @pytest.mark.asyncio
    async def test_reject_with_feedback_loops(self, coordinator):
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.reject_plan(feedback="Add more steps")
        second_plan = None
        async for event in gen:
            if isinstance(event, PlanProposed):
                second_plan = event
                break
        assert second_plan is not None


# ============================================================================
# Planner unit tests (mock AgentLoop + mock LLM)
# ============================================================================


class MockAgentLoop:
    """Mock AgentLoop that yields a single AgentFinished event."""

    def __init__(self):
        self.run_calls: list[dict] = []

    async def run(self, user_input: str, *, mode: str = "execute",
                  max_iterations: int = 10, stream: bool = False,
                  **kwargs) -> AsyncIterator[AgentFinished]:
        self.run_calls.append({"user_input": user_input, "mode": mode})
        yield AgentFinished(
            reason="Mock phase complete.",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


class MockConversationContext:
    """Minimal mock of ConversationContext for Planner tests."""

    def __init__(self, messages: list[Message] | None = None):
        self._messages: list[Message] = messages or []
        self._appended: list[Message] = []

    @property
    def messages(self) -> list[Message]:
        return self._messages

    def append(self, msg: Message) -> None:
        self._appended.append(msg)
        self._messages.append(msg)


class TestPlanner:
    """Unit tests for Planner — plan loop logic with mocked dependencies."""

    @pytest.fixture
    def llm(self):
        return MockPlanLLMProvider()

    @pytest.fixture
    def agent_loop(self):
        return MockAgentLoop()

    @pytest.fixture
    def ctx(self):
        return MockConversationContext()

    @pytest.fixture
    def settings(self):
        from toddler.config.settings import Settings
        return Settings(streaming_enabled=False)

    @staticmethod
    def _plan_sm() -> AgentStateMachine:
        """Create a state machine pre-transitioned to PLAN_EXPLORING."""
        sm = AgentStateMachine()
        sm.transition(AgentMode.PLAN_EXPLORING)
        return sm

    def _make_planner(self, settings, llm, ctx, agent_loop,
                      state_machine=None):
        if state_machine is None:
            state_machine = self._plan_sm()
        from toddler.agent.planner import Planner
        return Planner(
            llm_provider=llm,
            context=ctx,
            settings=settings,
            agent_loop=agent_loop,
            state_machine=state_machine,
        )

    async def _collect(self, gen) -> list:
        events = []
        async for event in gen:
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_plan_mode_yields_plan_proposed(
        self, settings, llm, agent_loop, ctx,
    ):
        """Complex input → yields PlanProposed with the expected plan."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, PlanProposed):
                break

        plan_events = [e for e in events if isinstance(e, PlanProposed)]
        assert len(plan_events) == 1
        assert plan_events[0].plan.title == "Mock Plan"
        assert planner.current_mode == AgentMode.PLAN_WAITING

    @pytest.mark.asyncio
    async def test_approve_plan(self, settings, llm, agent_loop, ctx):
        """Approve transitions to PLAN_EXECUTING and plan is set."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert planner.current_mode == AgentMode.PLAN_WAITING
        result = planner.approve_plan()
        assert result is True
        assert planner.current_mode == AgentMode.PLAN_EXECUTING
        assert planner.plan is not None
        assert planner.plan.title == "Mock Plan"

    @pytest.mark.asyncio
    async def test_reject_plan_outright(
        self, settings, llm, agent_loop, ctx,
    ):
        """Reject without feedback → FINISHED, plan cleared."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        planner.reject_plan()
        assert planner.current_mode == AgentMode.FINISHED
        assert planner.plan is None

    @pytest.mark.asyncio
    async def test_reject_with_feedback_loops(
        self, settings, llm, agent_loop, ctx,
    ):
        """Reject with feedback → loops back to PLAN_EXPLORING."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        planner.reject_plan(feedback="Add more steps")
        assert planner.current_mode == AgentMode.PLAN_EXPLORING
        assert planner.plan is None

        # Advance the generator one step so the feedback injection code
        # runs (the generator was paused at `yield PlanProposed`; after
        # reject_plan() sets the event, we need to resume past the
        # `await event.wait()` to reach the `ctx.append()` call).
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

        # Verify feedback was injected into context.
        feedback_msgs = [
            m for m in ctx._appended
            if "Add more steps" in (m.text or "")
        ]
        assert len(feedback_msgs) == 1

    @pytest.mark.asyncio
    async def test_plan_generation_failure(
        self, settings, llm, agent_loop, ctx,
    ):
        """Empty steps → AgentError yielded."""
        llm._plan_json = {"title": "Bad", "steps": []}
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        events = await self._collect(gen)

        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) >= 1
        assert planner.current_mode == AgentMode.FINISHED
        assert planner.plan is None

    @pytest.mark.asyncio
    async def test_reuses_state_machine(self, settings, llm, agent_loop, ctx):
        """Planner should accept and use an external state machine."""
        sm = self._plan_sm()
        planner = self._make_planner(
            settings, llm, ctx, agent_loop, state_machine=sm,
        )
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        # State machine should be the same instance and in PLAN_WAITING.
        assert sm.current_mode == AgentMode.PLAN_WAITING
        assert planner.current_mode == AgentMode.PLAN_WAITING
