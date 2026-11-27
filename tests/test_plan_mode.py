"""Tests for plan mode — state machine, complexity heuristic, plan
serialization, tool gating, and session manager orchestration.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.mocks import (
    text_response,
    tool_use_response,
)
from toddler.agent.events import (
    AgentCancelled,
    AgentFinished,
    PlanProposed,
    TodoUpdate,
    ToolCallEnd,
)
from toddler.agent.state_machine import (
    AgentMode,
    AgentStateMachine,
    classify_complexity,
)
from toddler.cli.renderer import ConfirmResult
from toddler.llm import LLMResponse, Message, MessageBlock, TokenUsage
from toddler.llm.base import BaseLLMProvider
from toddler.tools.plan import (
    Plan,
    PlanState,
    PlanStep,
    PlanUpdateTool,
)
from toddler.tools.policy import Decision, FileScope, Permission, PermissionMode

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

    def test_observer_notified_after_each_transition(self, sm):
        seen = []
        sm.add_observer(lambda m: seen.append(m.current_mode))
        sm.classify_and_transition("refactor auth")
        assert seen == [AgentMode.PLAN_EXPLORING]
        sm.transition(AgentMode.PLAN_PROPOSING)
        assert seen == [
            AgentMode.PLAN_EXPLORING,
            AgentMode.PLAN_PROPOSING,
        ]

    def test_observer_not_notified_on_invalid_transition(self, sm):
        seen = []
        sm.add_observer(lambda m: seen.append(m.current_mode))
        assert sm.transition(AgentMode.PLAN_WAITING) is False
        assert seen == []

    def test_notify_observers_manual_call(self, sm):
        """Observers see a change even when state changed without a
        transition (e.g. permission-gating flips)."""
        seen = []
        sm.add_observer(lambda m: seen.append(m.current_mode))
        sm.notify_observers()
        assert seen == [AgentMode.IDLE]


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
                    files_affected=["main.py"],
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
        assert "depends_on" not in data["steps"][1]

        restored = Plan.from_json(json_str)
        assert restored is not None
        assert restored.title == "Refactor Auth"
        assert len(restored.steps) == 2
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
        assert plan.steps[0].id == "step-1"  # canonicalized, not "s1"
        assert plan.rationale == ""
        assert plan.risks == []
        # Omitted field must not default to the step count.
        assert plan.estimated_files_touched == 0

    def test_plan_from_json_canonicalizes_step_ids(self):
        # LLM-proposed ids are ignored — ids are assigned from position,
        # so duplicate, missing, or arbitrary ids cannot collapse
        # tracking rows in PlanState.
        data = {
            "title": "Canonical Ids",
            "summary": "",
            "steps": [
                {"id": "step-1", "description": "First"},
                {"id": "step-1", "description": "Second"},   # duplicate
                {"description": "Third"},                    # missing
                {"id": "weird-id", "description": "Fourth"},  # arbitrary
            ],
        }
        plan = Plan.from_json(json.dumps(data))
        assert plan is not None
        assert [s.id for s in plan.steps] == [
            "step-1", "step-2", "step-3", "step-4",
        ]

    def test_plan_from_json_ignores_stray_depends_on(self):
        # depends_on was removed — steps execute top-to-bottom.  The LLM
        # may still emit the key (it appeared in older prompt schemas),
        # and must not crash on a list or null.
        data = {
            "title": "Deps",
            "summary": "",
            "steps": [
                {"id": "step-1", "description": "First",
                 "depends_on": []},
                {"id": "step-2", "description": "Second",
                 "depends_on": ["step-1", "nope"]},
                {"id": "step-3", "description": "Third",
                 "depends_on": None},
            ],
        }
        plan = Plan.from_json(json.dumps(data))
        assert plan is not None
        assert [s.id for s in plan.steps] == [
            "step-1", "step-2", "step-3",
        ]

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

    @pytest.mark.asyncio
    async def test_pending_is_not_writable(self):
        """pending is the initial state — the model may not write it back.

        Accepting it would silently mutate a status the render trigger
        never shows (only in_progress transitions or completion render),
        leaving the panel stale until the phase-end flush.
        """
        plan = _plan()
        tool, state = _plan_tool(plan)
        result = await tool.execute(step_id="step-1", status="pending")
        assert result.success is False
        assert "pending" in (result.error or "")
        assert "in_progress" in (result.error or "")
        assert state.steps == [("step-1", "Do it", "pending")]

    def test_permission_is_read(self):
        tool, _ = _plan_tool()
        assert tool.permission is Permission.READ

    def test_schema_enum_lists_valid_statuses(self):
        tool, _ = _plan_tool()
        enum = tool.parameters["properties"]["status"]["enum"]
        assert enum == ["in_progress", "completed"]


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

    def test_bare_completed_emits_immediately(self):
        state = PlanState()
        state.activate(_plan(steps=[
            PlanStep(id="step-1", description="One"),
            PlanStep(id="step-2", description="Two"),
        ]))
        state.mark_step("step-1", "completed")
        # Every mutation emits — no hold-back filter folding a lone
        # completed into the next render.
        assert state.take_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "pending"),
        ]
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
        assert state.take_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "pending"),
        ]
        state.mark_step("step-2", "completed")
        # All steps done — the closing snapshot renders.
        assert state.take_update() == [
            ("step-1", "One", "completed"),
            ("step-2", "Two", "completed"),
        ]

    def test_take_update_returns_none_when_inactive(self):
        assert PlanState().take_update() is None

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
        # Re-activating with a fresh plan re-arms detection cleanly.
        state.activate(_plan())
        assert state.take_update() is None


# ============================================================================
# SessionManager plan workflow (integration test with mock LLM)
# ============================================================================


class MockPlanLLMProvider(BaseLLMProvider):
    """Scripted responses for the tool-driven plan flow.

    The sequence is consumed one response per LLM call; when it is
    exhausted a plain end-turn follows.  Every call records the tool list
    it was offered, so the phase swaps can be asserted.
    """

    def __init__(self, sequence: list[LLMResponse] | None = None):
        self._sequence = sequence or []
        self._seq_index = 0
        self.call_count = 0
        self.messages_history: list[list[Message]] = []
        self.call_configs: list[tuple[str, str | None]] = []
        self.title_requests: list[bool] = []
        self.tools_history: list[list[dict]] = []

    async def generate(
        self, messages, tools, *, model, reasoning_effort=None,
        max_completion_tokens=4096, response_format=None, temperature=0.0,
        stream=True, request_title=False,
    ):
        self.messages_history.append(messages)
        self.call_configs.append((model, reasoning_effort))
        self.title_requests.append(request_title)
        self.tools_history.append(tools)
        self.call_count += 1
        if self._seq_index < len(self._sequence):
            response = self._sequence[self._seq_index]
            self._seq_index += 1
            return response
        return LLMResponse(
            messages=[Message.assistant([
                MessageBlock.content_block("Research complete."),
            ])],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=20, output_tokens=10),
        )

    async def generate_compact(self, prompt, *, model):
        return "compacted"


def _mock_plan(
    *, plan_id: str = "plan-1", title: str = "Mock Plan",
) -> Plan:
    return Plan(
        id=plan_id,
        title=title,
        summary="A mocked plan.",
        markdown="## Mock Plan\n\n1. Do the thing.",
    )


def _proposal(plan: Plan | None = None) -> LLMResponse:
    plan = plan or _mock_plan()
    return tool_use_response(
        "propose_plan",
        {
            "title": plan.title,
            "plan_markdown": plan.markdown or plan.format_for_display(),
        },
        tool_id=plan.id,
    )


class TestSessionManagerPlanWorkflow:
    """The plan loop inside one AgentLoop run."""

    @pytest.fixture
    def settings(self, tmp_path):
        from toddler.config.settings import Settings
        return Settings(session_dir=tmp_path, streaming_enabled=False)

    @pytest.fixture
    def storage_mgr(self, tmp_path):
        from toddler.session.database import SQLiteDatabase
        from toddler.session.storage import StorageManager

        db = SQLiteDatabase(tmp_path / "test_plan.db")
        db.open()
        return StorageManager(db)

    @pytest.fixture
    def llm(self):
        return MockPlanLLMProvider()

    @pytest.fixture
    async def session_mgr(self, settings, storage_mgr, llm):
        from toddler.session.manager import SessionManager

        mgr = SessionManager(
            settings=settings,
            storage_manager=storage_mgr,
            llm=llm,
        )
        await mgr.resolve()
        return mgr

    async def _collect(self, gen) -> list:
        events = []
        async for event in gen:
            events.append(event)
        return events

    async def _until_proposed(self, gen):
        """Collect events through the first PlanProposed."""
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, PlanProposed):
                return events, event
        return events, None

    @pytest.mark.asyncio
    async def test_simple_execution_path(self, session_mgr):
        events = await self._collect(session_mgr.process_turn("fix a typo"))
        assert not any(isinstance(e, PlanProposed) for e in events)
        assert len([e for e in events if isinstance(e, AgentFinished)]) == 1

    @pytest.mark.asyncio
    async def test_plan_mode_yields_plan_proposed(self, session_mgr, llm):
        llm._sequence = [_proposal()]
        events, proposed = await self._until_proposed(
            session_mgr.process_turn("refactor the database layer"),
        )
        assert proposed is not None
        assert proposed.plan.id == "plan-1"
        assert proposed.plan.title == "Mock Plan"
        assert "Do the thing" in proposed.plan.markdown
        # The proposal is a tool call in the same run — no tool-less
        # proposal request follows it, and todo_write is not offered yet.
        first_tools = {t["function"]["name"] for t in llm.tools_history[0]}
        assert "propose_plan" in first_tools
        assert "todo_write" not in first_tools

    @pytest.mark.asyncio
    async def test_approve_plan_executes_in_the_same_run(
        self, session_mgr, llm,
    ):
        llm._sequence = [_proposal(), text_response("Executing plan.")]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert proposed is not None
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is True

        remaining = await self._collect(gen)
        assert llm.call_count == 2
        assert len([e for e in remaining if isinstance(e, AgentFinished)]) == 1

        second_tools = {t["function"]["name"] for t in llm.tools_history[1]}
        assert "todo_write" in second_tools
        assert "propose_plan" not in second_tools
        assert "Current Mode: EXECUTE" in session_mgr.context.messages[0].content

    @pytest.mark.asyncio
    async def test_the_title_rides_only_the_first_request(
        self, session_mgr, llm,
    ):
        first = _proposal()
        first.title = "Plan the refactor"
        llm._sequence = [first, text_response("Executing plan.")]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is True
        await self._collect(gen)
        assert llm.title_requests == [True, False]

    @pytest.mark.asyncio
    async def test_double_approve_is_stale(self, session_mgr, llm):
        llm._sequence = [_proposal(), text_response("Executing plan.")]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is True
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is False
        await self._collect(gen)
        assert llm.call_count == 2

    @pytest.mark.asyncio
    async def test_reject_after_approve_is_ignored(self, session_mgr, llm):
        llm._sequence = [_proposal(), text_response("Executing plan.")]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is True
        assert session_mgr.reject_plan(plan_id=proposed.plan.id) is False
        remaining = await self._collect(gen)
        assert len([e for e in remaining if isinstance(e, AgentFinished)]) == 1

    @pytest.mark.asyncio
    async def test_reject_outright_ends_the_turn(self, session_mgr, llm):
        llm._sequence = [_proposal()]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.reject_plan(plan_id=proposed.plan.id) is True
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].stopped is True

    @pytest.mark.asyncio
    async def test_reject_with_feedback_reproposes(self, session_mgr, llm):
        llm._sequence = [
            _proposal(_mock_plan(plan_id="plan-1")),
            _proposal(_mock_plan(plan_id="plan-2", title="Revised Plan")),
            text_response("Done."),
        ]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.reject_plan(
            plan_id=proposed.plan.id, feedback="avoid sqlite",
        ) is True

        revised = None
        async for event in gen:
            if isinstance(event, PlanProposed):
                revised = event
                break
        assert revised is not None
        assert revised.plan.id == "plan-2"
        assert revised.plan.title == "Revised Plan"
        assert llm.call_count == 2
        tool_results = [
            block.tool_result_content or ""
            for msg in session_mgr.context.messages
            if msg.role == "tool"
            for block in msg.blocks
            if block.type == "tool_result"
        ]
        assert any("avoid sqlite" in text for text in tool_results)

        assert session_mgr.approve_plan(plan_id=revised.plan.id) is True
        remaining = await self._collect(gen)
        assert len([e for e in remaining if isinstance(e, AgentFinished)]) == 1

    @pytest.mark.asyncio
    async def test_approve_after_feedback_is_stale(self, session_mgr, llm):
        llm._sequence = [
            _proposal(_mock_plan(plan_id="plan-1")),
            _proposal(_mock_plan(plan_id="plan-2")),
        ]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.reject_plan(
            plan_id=proposed.plan.id, feedback="try again",
        ) is True
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is False

        second = None
        async for event in gen:
            if isinstance(event, PlanProposed):
                second = event
                break
        assert second is not None
        assert second.plan.id == "plan-2"

    @pytest.mark.asyncio
    async def test_invalid_plan_returns_a_tool_error(self, session_mgr, llm):
        llm._sequence = [
            tool_use_response(
                "propose_plan",
                {"title": "", "plan_markdown": ""},
                tool_id="bad-plan",
            ),
            text_response("Done."),
        ]
        events = await self._collect(
            session_mgr.process_turn("refactor the database layer"),
        )
        assert not any(isinstance(e, PlanProposed) for e in events)
        ends = [
            e for e in events
            if isinstance(e, ToolCallEnd) and e.tool_name == "propose_plan"
        ]
        assert len(ends) == 1
        assert ends[0].result is not None
        assert not ends[0].result.success
        assert "Invalid propose_plan call" in (ends[0].result.error or "")

    @pytest.mark.asyncio
    async def test_cancel_at_the_plan_pause(self, session_mgr, llm):
        llm._sequence = [_proposal()]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert proposed is not None
        assert session_mgr.agent.answer_tool_call(
            proposed.plan.id, None,
        ) is True
        remaining = await self._collect(gen)
        assert any(isinstance(e, AgentCancelled) for e in remaining)

    @pytest.mark.asyncio
    async def test_todo_updates_during_execution(self, session_mgr, llm):
        llm._sequence = [
            _proposal(),
            tool_use_response(
                "todo_write",
                {"todos": [
                    {"content": "Do the thing", "status": "in_progress"},
                ]},
                tool_id="todo-1",
            ),
            text_response("Done."),
        ]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.approve_plan(plan_id=proposed.plan.id) is True

        events = await self._collect(gen)
        updates = [e for e in events if isinstance(e, TodoUpdate)]
        assert len(updates) == 1
        assert updates[0].items[0].content == "Do the thing"

    @pytest.mark.asyncio
    async def test_plan_entry_pins_manual_gating(self, session_mgr, llm):
        llm._sequence = [_proposal()]
        gen = session_mgr.process_turn("refactor the database layer")
        await self._until_proposed(gen)
        assert session_mgr.permission_mode is PermissionMode.MANUAL

    @pytest.mark.asyncio
    async def test_approve_with_auto_flips_gating(self, session_mgr, llm):
        llm._sequence = [_proposal(), text_response("Done.")]
        gen = session_mgr.process_turn("refactor the database layer")
        _, proposed = await self._until_proposed(gen)
        assert session_mgr.approve_plan(
            plan_id=proposed.plan.id,
            permission_mode=PermissionMode.AUTO,
        ) is True
        await self._collect(gen)
        assert session_mgr.permission_mode is PermissionMode.AUTO

    @pytest.mark.asyncio
    async def test_simple_execution_keeps_auto(self, session_mgr):
        session_mgr.set_permission_mode(PermissionMode.AUTO)
        events = await self._collect(session_mgr.process_turn("fix a typo"))
        assert session_mgr.permission_mode is PermissionMode.AUTO
        assert not any(isinstance(e, PlanProposed) for e in events)

    @pytest.mark.asyncio
    async def test_plan_pending_flag_resets_permission_to_manual(
        self, session_mgr, llm,
    ):
        llm._sequence = [_proposal()]
        session_mgr.set_permission_mode(PermissionMode.AUTO)
        session_mgr.state_machine.flag_plan_pending()
        gen = session_mgr.process_turn("make this better")
        await self._until_proposed(gen)
        assert session_mgr.permission_mode is PermissionMode.MANUAL


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
        from toddler.tools.policy import PermissionManager
        mgr = PermissionManager()
        assert mgr.mode == PermissionMode.MANUAL

    def test_set_mode_persists(self):
        from toddler.tools.policy import PermissionManager
        mgr = PermissionManager()
        mgr.set_mode(PermissionMode.AUTO)
        assert mgr.mode == PermissionMode.AUTO

    @pytest.mark.parametrize(
        ("mode", "perm", "scope", "expected"),
        [
            # Workspace files: the tiers as they have always been.
            (PermissionMode.MANUAL, Permission.READ, FileScope.WORKSPACE, Decision.ALLOW),
            (PermissionMode.MANUAL, Permission.SHELL_SAFE, FileScope.WORKSPACE, Decision.ALLOW),
            (PermissionMode.MANUAL, Permission.WRITE, FileScope.WORKSPACE, Decision.ASK),
            (
                PermissionMode.MANUAL,
                Permission.SHELL_DANGEROUS,
                FileScope.WORKSPACE,
                Decision.ASK,
            ),
            (PermissionMode.AUTO, Permission.READ, FileScope.WORKSPACE, Decision.ALLOW),
            (PermissionMode.AUTO, Permission.WRITE, FileScope.WORKSPACE, Decision.ALLOW),
            (
                PermissionMode.AUTO,
                Permission.SHELL_DANGEROUS,
                FileScope.WORKSPACE,
                Decision.ASK,
            ),
            # Outside the workspace nothing is automatic, AUTO included.
            (PermissionMode.MANUAL, Permission.READ, FileScope.EXTERNAL, Decision.ASK),
            (PermissionMode.AUTO, Permission.READ, FileScope.EXTERNAL, Decision.ASK),
            (PermissionMode.AUTO, Permission.WRITE, FileScope.EXTERNAL, Decision.ASK),
            (
                PermissionMode.AUTO,
                Permission.SHELL_SAFE,
                FileScope.EXTERNAL,
                Decision.ASK,
            ),
            # A restricted path is refused at every tier, in every mode —
            # no approval lifts it.
            (PermissionMode.MANUAL, Permission.READ, FileScope.RESTRICTED, Decision.DENY),
            (PermissionMode.AUTO, Permission.READ, FileScope.RESTRICTED, Decision.DENY),
            (PermissionMode.AUTO, Permission.WRITE, FileScope.RESTRICTED, Decision.DENY),
            (
                PermissionMode.AUTO,
                Permission.SHELL_SAFE,
                FileScope.RESTRICTED,
                Decision.DENY,
            ),
            (
                PermissionMode.AUTO,
                Permission.SHELL_DANGEROUS,
                FileScope.RESTRICTED,
                Decision.DENY,
            ),
        ],
    )
    def test_decide(self, mode, perm, scope, expected):
        from toddler.tools.policy import PermissionManager
        mgr = PermissionManager(mode)
        assert mgr.decide(perm, scope) is expected

    @pytest.mark.parametrize(
        ("perm", "expected"),
        [
            (Permission.READ, False),
            (Permission.SHELL_SAFE, False),
            (Permission.WRITE, True),
            (Permission.SHELL_DANGEROUS, True),
        ],
    )
    def test_is_mutating(self, perm, expected):
        """The tiers the executor checkpoints a call of — the loop reads
        this answer off the tier it classified the call with."""
        assert perm.is_mutating is expected

    # Tests for plan-entry reset at the SessionManager level are in
    # TestSessionManagerPlanWorkflow above.


# ============================================================================
# CLIApp plan-approval handler
# ============================================================================


class _ScriptedRenderer:
    """No-op renderer whose confirm() returns scripted decisions.

    Only confirm() is awaited by the CLI handler; every other renderer
    call is recorded as a no-op so tests can assert dispatch order.
    """

    def __init__(self, decisions: list[ConfirmResult]) -> None:
        self._decisions = list(decisions)
        self.calls: list[str] = []

    async def confirm(
        self, prompt: str, choices: list[str], *,
        feedback_index: int | None = None,
        detail: str | None = None,  # noqa: ARG002
        input_label: str = "Feedback",  # noqa: ARG002
    ) -> ConfirmResult:
        self.calls.append("confirm")
        if self._decisions:
            return self._decisions.pop(0)
        return ConfirmResult(decision="deny")

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)

        return _record


class TestCLIAppPlanApproval:
    """Drive the CLI's PlanProposed handler end-to-end with a real
    session manager.

    Regression coverage for the sync approve_plan() / reject_plan()
    session manager calls: awaiting them raises TypeError (``bool``/``None``
    are not awaitable) and crashes the turn, so each decision branch
    must complete without error.
    """

    @pytest.fixture
    def settings(self, tmp_path):
        from toddler.config.settings import Settings
        return Settings(session_dir=tmp_path, streaming_enabled=False)

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
    async def session_mgr(self, settings, storage_mgr, llm):
        from toddler.session.manager import SessionManager

        mgr = SessionManager(
            settings=settings,
            storage_manager=storage_mgr,
            llm=llm,
        )
        await mgr.resolve()
        return mgr

    @pytest.fixture
    def cli(self, settings, session_mgr):
        """CLIApp wired to the real session manager (renderer swapped later)."""
        from toddler.cli.app import CLIApp

        return CLIApp(settings=settings, session=session_mgr)

    async def _run_plan_turn(self, cli, *decisions) -> _ScriptedRenderer:
        """Run a plan-mode turn, answering the approval prompt with
        *decisions* in order."""
        renderer = _ScriptedRenderer(list(decisions))
        cli._renderer = renderer
        await cli._run_agent_turn("refactor the database layer")
        return renderer

    @pytest.mark.asyncio
    async def test_approve_with_manual_executes(self, cli, session_mgr, llm):
        """approve_with_manual approves the plan and runs execution."""
        llm._sequence = [_proposal(), text_response("Executing plan.")]
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="approve_with_manual"),
        )

        assert session_mgr.permission_mode == PermissionMode.MANUAL
        assert llm.call_count == 2
        assert renderer.calls.count("confirm") == 1
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_approve_with_auto_switches_gating(
        self, cli, session_mgr, llm,
    ):
        """approve_with_auto approves the plan and switches gating to AUTO."""
        llm._sequence = [_proposal(), text_response("Executing plan.")]
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="approve_with_auto"),
        )

        assert session_mgr.permission_mode == PermissionMode.AUTO
        assert llm.call_count == 2
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_deny_finishes_turn(self, cli, session_mgr, llm):
        """deny rejects the plan outright and ends the turn."""
        llm._sequence = [_proposal()]
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="deny"),
        )

        assert llm.call_count == 1
        assert renderer.calls.count("confirm") == 1
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_feedback_reproposes_then_approves(
        self, cli, session_mgr, llm,
    ):
        """feedback re-proposes a plan, which is then approved — confirm()
        is asked twice."""
        llm._sequence = [
            _proposal(_mock_plan(plan_id="plan-1")),
            _proposal(_mock_plan(plan_id="plan-2", title="Revised")),
            text_response("Executing plan."),
        ]
        renderer = await self._run_plan_turn(
            cli,
            ConfirmResult(decision="feedback", feedback="avoid sqlite"),
            ConfirmResult(decision="approve_with_manual"),
        )

        assert renderer.calls.count("confirm") == 2
        assert llm.call_count == 3
        assert "on_agent_finished" in renderer.calls
