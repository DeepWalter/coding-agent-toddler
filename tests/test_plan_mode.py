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
    PlanStepUpdate,
)
from toddler.agent.planner import (
    Plan,
    PlanStep,
    plan_proposal_prompt,
)
from toddler.agent.state_machine import (
    AgentMode,
    AgentStateMachine,
    classify_complexity,
)
from toddler.llm import ContentBlock, LLMResponse, Message, TokenUsage
from toddler.llm.base import BaseLLMProvider
from toddler.tools.base import Permission, PermissionMode
from toddler.tools.plan import PlanState, PlanUpdateTool

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


def _two_step_plan_json() -> dict:
    """Proposal JSON for a two-step plan (for MockPlanLLMProvider)."""
    return {
        "title": "Two-Step Plan",
        "summary": "A two-step mocked plan.",
        "steps": [
            {"id": "step-1", "description": "First thing"},
            {"id": "step-2", "description": "Second thing"},
        ],
    }


def _end_turn_response(text: str = "Research complete.") -> LLMResponse:
    """Build a plain end-turn LLMResponse."""
    return LLMResponse(
        messages=[Message.assistant([ContentBlock.text_block(text)])],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _tool_use_response(
    tool_name: str, tool_input: dict, *, tool_id: str = "t1",
) -> LLMResponse:
    """Build a single tool_use LLMResponse."""
    return LLMResponse(
        messages=[Message.assistant([
            ContentBlock.tool_use_block(tool_id, tool_name, tool_input),
        ])],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _plan_tool(
    plan: Plan | None = None,
) -> tuple[PlanUpdateTool, PlanState]:
    """Build a plan_update tool wired to a PlanState tracking *plan*."""
    state = PlanState()
    if plan is not None:
        state.activate(plan)
    return PlanUpdateTool(state), state


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

    def test_plan_from_json_collapses_multiline_fields(self):
        # LLM-generated JSON escapes newlines inside string values;
        # from_json turns them back into real newlines.  Fields rendered
        # as one-liners (prompt rows, renderer panel rows) must collapse.
        data = {
            "title": "Multi\nline title",
            "summary": "First sentence.\nSecond sentence.",
            "steps": [
                {"id": "s1", "description": "Do this.\n  Then do that."},
            ],
        }
        plan = Plan.from_json(json.dumps(data))
        assert plan is not None
        assert plan.title == "Multi line title"
        assert plan.summary == "First sentence. Second sentence."
        assert plan.steps[0].description == "Do this. Then do that."

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
                PlanStep(id="1", description="Step one"),
                PlanStep(id="2", description="Step two"),
                PlanStep(id="3", description="Step three"),
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
        prompt = plan_proposal_prompt(
            "refactor the database layer",
        )
        assert "refactor the database layer" in prompt

    def test_prompt_includes_research_context(self):
        prompt = plan_proposal_prompt(
            "refactor the database layer",
            research_context="Found 15 files using the old DB API.",
        )
        assert "Found 15 files using the old DB API." in prompt

    def test_prompt_asks_for_json_format(self):
        prompt = plan_proposal_prompt("fix bugs")
        assert "JSON" in prompt
        assert "title" in prompt
        assert "steps" in prompt

    def test_prompt_without_context_omits_context_block(self):
        prompt = plan_proposal_prompt("do something")
        assert "Context gathered" not in prompt


# ============================================================================
# ============================================================================
# Plan step progress tracking
# ============================================================================


class TestPlanStepTracking:

    def test_all_steps_start_pending(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="1", description="A"),
            PlanStep(id="2", description="B"),
        ]))
        assert state.steps == [
            ("1", "A", "pending"),
            ("2", "B", "pending"),
        ]
        assert state.is_complete is False

    def test_mark_step_advances_progress(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="1", description="A"),
            PlanStep(id="2", description="B"),
            PlanStep(id="3", description="C"),
        ]))
        assert state.mark_step("1", "completed") is True
        assert state.mark_step("2", "in_progress") is True
        assert state.steps == [
            ("1", "A", "completed"),
            ("2", "B", "in_progress"),
            ("3", "C", "pending"),
        ]

    def test_mark_step_unknown_id_returns_false(self):
        state = PlanState()
        state.activate(_plan(steps=[PlanStep(id="1", description="A")]))
        assert state.mark_step("nonexistent", "completed") is False

    def test_mark_step_inactive_returns_false(self):
        assert PlanState().mark_step("1", "completed") is False

    def test_is_complete_when_all_done(self):
        state = PlanState()
        state.activate(_plan(steps=[PlanStep(id="1", description="A")]))
        state.mark_step("1", "completed")
        assert state.is_complete is True


# ============================================================================
# PlanUpdateTool (LLM-driven step status tracking)
# ============================================================================


class TestPlanUpdateTool:

    @pytest.mark.asyncio
    async def test_successful_update_mutates_step_status(self):
        plan = _plan()
        tool, state = _plan_tool(plan)
        result = await tool.execute(step_id="step-1", status="completed")
        assert result.success is True
        assert state.steps == [("step-1", "Do it", "completed")]

    @pytest.mark.asyncio
    async def test_unknown_step_returns_error(self):
        tool, _ = _plan_tool(_plan())
        result = await tool.execute(step_id="nope", status="completed")
        assert result.success is False
        assert result.error is not None
        assert "nope" in result.error

    @pytest.mark.asyncio
    async def test_no_active_plan_returns_error(self):
        tool, _ = _plan_tool()
        result = await tool.execute(step_id="step-1", status="completed")
        assert result.success is False
        assert result.error is not None
        assert "No approved plan" in result.error

    @pytest.mark.asyncio
    async def test_invalid_status_returns_error(self):
        tool, _ = _plan_tool(_plan())
        result = await tool.execute(step_id="step-1", status="bogus")
        assert result.success is False
        assert result.error is not None
        assert "bogus" in result.error

    def test_permission_is_read(self):
        tool, _ = _plan_tool()
        assert tool.permission is Permission.READ

    def test_schema_enum_lists_valid_statuses(self):
        tool, _ = _plan_tool()
        enum = tool.parameters["properties"]["status"]["enum"]
        assert enum == ["pending", "in_progress", "completed"]


class TestPlanState:

    def test_take_update_returns_none_until_something_changes(self):
        state = PlanState()
        state.activate(_plan())
        # Emitted baseline matches activation — nothing to update.
        assert state.take_update() is None

    def test_take_update_returns_full_list_on_step_start(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="step-1", description="One"),
            PlanStep(id="step-2", description="Two"),
        ]))
        state.mark_step("step-1", "in_progress")
        assert state.take_update() == [
            ("step-1", "One", "in_progress"),
            ("step-2", "Two", "pending"),
        ]
        # Emitted baseline advanced — no update without a new change.
        assert state.take_update() is None

    def test_bare_completed_is_held_back_and_folds_into_next_render(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="step-1", description="One"),
            PlanStep(id="step-2", description="Two"),
        ]))
        state.mark_step("step-1", "completed")
        # A completed alone renders nothing — it folds into the next
        # step's start instead of double-rendering with it.
        assert state.take_update() is None
        state.mark_step("step-2", "in_progress")
        assert state.take_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "in_progress"),
        ]

    def test_completing_the_final_step_renders(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="step-1", description="One"),
            PlanStep(id="step-2", description="Two"),
        ]))
        state.mark_step("step-1", "in_progress")
        assert state.take_update() is not None
        state.mark_step("step-1", "completed")
        assert state.take_update() is None
        state.mark_step("step-2", "completed")
        # All steps done — the closing snapshot renders.
        assert state.take_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "completed"),
        ]

    def test_take_update_returns_none_when_inactive(self):
        assert PlanState().take_update() is None

    def test_flush_update_renders_held_back_changes(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="step-1", description="One"),
            PlanStep(id="step-2", description="Two"),
        ]))
        state.mark_step("step-1", "completed")
        # Held back by the trigger filter mid-run...
        assert state.take_update() is None
        # ...but the phase-end flush emits any change at all.
        assert state.flush_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "pending"),
        ]
        # Emitted baseline advanced — nothing new without a change.
        assert state.flush_update() is None

    def test_flush_update_returns_none_when_inactive(self):
        assert PlanState().flush_update() is None

    def test_steps_empty_when_inactive(self):
        assert PlanState().steps == []

    def test_returned_triples_are_a_fresh_list(self):
        state = PlanState()
        state.activate(_plan())
        state.mark_step("step-1", "in_progress")
        update = state.take_update()
        assert update is not None
        update.clear()
        # Mutating the returned list must not change the tracked state.
        assert state.steps == [("step-1", "Do it", "in_progress")]

    def test_steps_read_carries_all_steps(self):
        state = PlanState()
        state.activate(_plan())
        state.mark_step("step-1", "completed")
        assert state.steps == [("step-1", "Do it", "completed")]

    def test_activate_baselines_and_deactivate_clears(self):
        state = PlanState()
        state.activate(_plan())
        state.mark_step("step-1", "in_progress")
        assert state.take_update() is not None
        state.deactivate()
        assert state.is_active is False
        assert state.take_update() is None
        assert state.flush_update() is None
        # Re-activating with a fresh plan re-arms detection cleanly.
        state.activate(_plan())
        assert state.take_update() is None
        assert state.flush_update() is None


# ============================================================================
# SessionCoordinator plan workflow (integration test with mock LLM)
# ============================================================================


class MockPlanLLMProvider(BaseLLMProvider):
    """Mock LLM that returns a valid plan JSON for plan proposal calls,
    scripted responses for tool-carrying agent calls, and a simple
    end-turn once the script is exhausted.
    """

    def __init__(
        self,
        plan_json: dict | None = None,
        sequence: list[LLMResponse] | None = None,
    ):
        self._plan_json = plan_json or {
            "title": "Mock Plan",
            "summary": "A mocked plan for testing.",
            "steps": [{"id": "step-1", "description": "Do the thing"}],
        }
        # Responses consumed by agent calls that carry tools (explore and
        # execute phases, in order).  Plan-proposal calls (no tools)
        # bypass the sequence.
        self._sequence = sequence or []
        self._seq_index = 0
        self.call_count = 0
        self.messages_history: list[list[Message]] = []

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

        # Scripted agent-call response.
        if self._seq_index < len(self._sequence):
            response = self._sequence[self._seq_index]
            self._seq_index += 1
            return response

        # Standard agent call → simple end-turn.
        return LLMResponse(
            messages=[Message.assistant([
                ContentBlock.text_block("Research complete."),
            ])],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=20, output_tokens=10),
        )

    @property
    def model(self) -> str:
        return "test-model"

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
        from toddler.session.database import SQLiteDatabase
        from toddler.session.storage import StorageManager

        db_path = tmp_path / "test_plan.db"
        db = SQLiteDatabase(db_path)
        db.open()
        return StorageManager(db)

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

    # ------------------------------------------------------------------
    # Plan execution status tracking
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_execution_without_status_updates_emits_nothing(
        self, coordinator,
    ):
        """A plan run that never calls plan_update emits no step events."""
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # No initial all-pending emission — it carries no information,
        # and the final emission dedups against the activation baseline.
        assert updates == []
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_plan_update_tool_advances_step_statuses(
        self, coordinator, llm,
    ):
        """plan_update tool calls surface as PlanStepUpdate events."""
        llm._sequence = [
            _end_turn_response(),  # explore phase
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "in_progress"},
                tool_id="t1",
            ),
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "completed"},
                tool_id="t2",
            ),
        ]
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        assert updates, "expected PlanStepUpdate events"
        statuses = [
            next(st[2] for st in e.steps if st[0] == "step-1")
            for e in updates
        ]
        assert statuses == ["in_progress", "completed"]
        # Every event carries the complete list; completing the only step
        # is the closing snapshot, so the final emission dedups to zero.
        assert [st[0] for st in updates[-1].steps] == ["step-1"]
        assert len(updates) == 2

    @pytest.mark.asyncio
    async def test_completed_then_in_progress_renders_once(
        self, coordinator, llm,
    ):
        """A bare completed folds into the next step's start — one render."""
        llm._plan_json = _two_step_plan_json()
        llm._sequence = [
            _end_turn_response(),  # explore phase
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "completed"},
                tool_id="t1",
            ),
            _tool_use_response(
                "plan_update", {"step_id": "step-2", "status": "in_progress"},
                tool_id="t2",
            ),
        ]
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # One emission at the step-2 start — the lone completed never
        # rendered on its own, it folds into this snapshot.
        assert len(updates) == 1
        assert [st[2] for st in updates[0].steps] == ["completed", "in_progress"]

    @pytest.mark.asyncio
    async def test_all_steps_completed_emits_closing_snapshot(
        self, coordinator, llm,
    ):
        """Completing the final step emits the closing snapshot once."""
        llm._plan_json = _two_step_plan_json()
        llm._sequence = [
            _end_turn_response(),  # explore phase
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "in_progress"},
                tool_id="t1",
            ),
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "completed"},
                tool_id="t2",
            ),
            _tool_use_response(
                "plan_update", {"step_id": "step-2", "status": "in_progress"},
                tool_id="t3",
            ),
            _tool_use_response(
                "plan_update", {"step_id": "step-2", "status": "completed"},
                tool_id="t4",
            ),
        ]
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        statuses = [[st[2] for st in e.steps] for e in updates]
        assert statuses == [
            ["in_progress", "pending"],
            ["completed", "in_progress"],
            ["completed", "completed"],
        ]
        # The closing snapshot was already emitted — the final emission
        # dedups to zero.
        assert len(updates) == 3

    @pytest.mark.asyncio
    async def test_lone_completed_flushed_at_phase_end(
        self, coordinator, llm,
    ):
        """A completed with no follow-up trigger is flushed at phase end."""
        llm._plan_json = _two_step_plan_json()
        llm._sequence = [
            _end_turn_response(),  # explore phase
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "completed"},
                tool_id="t1",
            ),
        ]
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # Nothing rendered mid-run; the final emission flushes the
        # un-emitted completed status.
        assert len(updates) == 1
        assert [st[2] for st in updates[0].steps] == ["completed", "pending"]

    @pytest.mark.asyncio
    async def test_plan_tracking_torn_down_after_phase(self, coordinator):
        """The plan_update tool and shared plan are cleared after the turn."""
        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        coordinator.approve_plan()
        await self._collect(gen)
        assert coordinator._registry.get("plan_update") is None
        assert not coordinator._plan_state.is_active

    @pytest.mark.asyncio
    async def test_simple_execution_has_no_plan_events(self, coordinator):
        """Non-plan turns emit no PlanStepUpdate and never register the tool."""
        gen = coordinator.process_turn("fix a typo")
        events = await self._collect(gen)
        assert not any(isinstance(e, PlanStepUpdate) for e in events)
        assert coordinator._registry.get("plan_update") is None

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

    # ------------------------------------------------------------------
    # Permission mode integration with plan workflow
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_entry_resets_permission_to_manual(self, coordinator):
        """Entering plan mode resets gating to MANUAL."""
        coordinator.set_permission_mode(PermissionMode.AUTO)
        assert coordinator.permission_mode == PermissionMode.AUTO

        gen = coordinator.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert coordinator.permission_mode == PermissionMode.MANUAL

    @pytest.mark.asyncio
    async def test_direct_execute_preserves_permission_auto(self, coordinator):
        """A simple request does NOT reset an explicitly-set AUTO mode."""
        coordinator.set_permission_mode(PermissionMode.AUTO)

        gen = coordinator.process_turn("hello")
        events = []
        async for event in gen:
            events.append(event)

        assert coordinator.permission_mode == PermissionMode.AUTO
        has_plan = any(isinstance(e, PlanProposed) for e in events)
        assert not has_plan, "Simple request should not trigger plan"

    @pytest.mark.asyncio
    async def test_plan_pending_flag_resets_permission_to_manual(
        self, coordinator,
    ):
        """When /plan is used, entry into plan mode resets to MANUAL."""
        coordinator.set_permission_mode(PermissionMode.AUTO)
        coordinator.state_machine.flag_plan_pending()

        gen = coordinator.process_turn("do something")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert coordinator.permission_mode == PermissionMode.MANUAL


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


class MockContextManager:
    """Minimal mock of ContextManager for Planner tests."""

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
        return MockContextManager()

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


class TestModeDisplayLabel:
    """Unit tests for AgentMode.display_label."""

    def test_execute_label(self):
        """IDLE, EXECUTING, and FINISHED all return EXECUTE."""
        assert AgentMode.IDLE.display_label == "EXECUTE"
        assert AgentMode.EXECUTING.display_label == "EXECUTE"
        assert AgentMode.FINISHED.display_label == "EXECUTE"

    def test_plan_label(self):
        """All PLAN_* modes return PLAN."""
        for mode in (
            AgentMode.PLAN_EXPLORING,
            AgentMode.PLAN_PROPOSING,
            AgentMode.PLAN_WAITING,
            AgentMode.PLAN_EXECUTING,
        ):
            assert mode.display_label == "PLAN"

    def test_all_modes_have_label(self):
        """Every AgentMode has a non-empty display label."""
        for mode in AgentMode:
            assert mode.display_label in ("EXECUTE", "PLAN")


class TestPermissionMode:
    """Permission gating via the shared :class:`PermissionManager`."""

    # ------------------------------------------------------------------
    # Unit tests — PermissionManager
    # ------------------------------------------------------------------

    def test_default_is_manual(self):
        from toddler.tools.base import PermissionManager
        mgr = PermissionManager()
        assert mgr.mode == PermissionMode.MANUAL

    def test_set_mode_persists(self):
        from toddler.tools.base import PermissionManager
        mgr = PermissionManager()
        mgr.set_mode(PermissionMode.AUTO)
        assert mgr.mode == PermissionMode.AUTO

    def test_needs_confirmation_delegates(self):
        from toddler.tools.base import Permission, PermissionManager
        mgr = PermissionManager()
        # READ never needs confirmation
        assert not mgr.needs_confirmation(Permission.READ)
        # WRITE needs confirmation in MANUAL
        assert mgr.needs_confirmation(Permission.WRITE)
        # WRITE does NOT need confirmation in AUTO
        mgr.set_mode(PermissionMode.AUTO)
        assert not mgr.needs_confirmation(Permission.WRITE)
        # SHELL_DANGEROUS always needs confirmation
        assert mgr.needs_confirmation(Permission.SHELL_DANGEROUS)

    # Coordinator-level tests for plan-entry reset are in
    # TestSessionCoordinatorPlanWorkflow below.
