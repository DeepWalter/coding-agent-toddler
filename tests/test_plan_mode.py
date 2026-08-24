"""Tests for plan mode — state machine, complexity heuristic, plan
serialization, tool gating, and session manager orchestration.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator

import pytest

from toddler.agent.events import (
    AgentError,
    AgentFinished,
    PlanProposed,
    PlanStepUpdate,
    RecoverableAgentError,
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
from toddler.cli.renderer import ConfirmResult
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


class _SilentPlanner:
    """Planner stub whose plan loop ends without a terminal event.

    Models the abnormal ends the session manager defends against (a failed
    approval transition leaving the machine non-executing, or an
    external caller finishing the machine mid-cycle): ``run`` returns,
    but no :class:`AgentFinished` or :class:`FatalAgentError` was
    emitted.  Used to pin the guarantee that a plan turn always ends
    with a terminal event — the CLI stops the streaming renderer only
    on those, and the plan panel re-enters the alternate screen.
    """

    def __init__(
        self, plan: Plan | None, *,
        emit_proposal: bool = False,
        emit_recoverable_error: bool = False,
    ):
        self._plan = plan
        self._emit_proposal = emit_proposal
        self._emit_recoverable_error = emit_recoverable_error

    @property
    def plan(self) -> Plan | None:
        return self._plan

    async def run(self, user_input: str):
        # A recoverable error is NOT a terminal event — the CLI keeps
        # the streaming renderer running on those (it only tears the
        # alt screen down on AgentFinished / FatalAgentError).
        if self._emit_recoverable_error:
            yield RecoverableAgentError(
                message="Simulated LLM hiccup.",
            )
        if self._emit_proposal:
            yield PlanProposed(plan=self._plan)


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
        # Execution instructions lead the prompt.
        assert "plan_update" in output
        assert output.startswith("I have reviewed and approved")


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

    def test_prompt_does_not_ask_for_step_ids(self):
        # Step ids are canonical, position-derived — the LLM must not
        # invent ids that could collide or dangle.
        prompt = plan_proposal_prompt("fix bugs")
        assert '"id"' not in prompt

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


class TestSessionManagerPlanWorkflow:

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

    @pytest.mark.asyncio
    async def test_simple_execution_path(self, session_mgr):
        gen = session_mgr.process_turn("fix a typo")
        events = await self._collect(gen)
        has_plan = any(isinstance(e, PlanProposed) for e in events)
        assert not has_plan, "Simple request should not trigger plan mode"
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_plan_mode_yields_plan_proposed(self, session_mgr):
        """Collect events up to PlanProposed (avoids hanging on approval)."""
        gen = session_mgr.process_turn("refactor the database layer")
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, PlanProposed):
                break
        plan_events = [e for e in events if isinstance(e, PlanProposed)]
        assert len(plan_events) == 1
        assert plan_events[0].plan.title == "Mock Plan"

    @pytest.mark.asyncio
    async def test_plan_generation_failure(self, session_mgr, llm):
        """Zero-step plan is rejected as invalid — yields AgentError."""
        llm._plan_json = {"title": "Bad", "steps": []}
        gen = session_mgr.process_turn("refactor the database layer")
        events = await self._collect(gen)
        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) >= 1

    @pytest.mark.asyncio
    async def test_approve_plan_executes(self, session_mgr):
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_double_approve_executes_once(self, session_mgr, llm):
        """A second approve_plan() is a no-op success — the plan is
        already approved, so the turn still executes exactly once, with
        plan tracking armed."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        # Approving again is idempotent — the machine is already
        # PLAN_EXECUTING, so the call is a no-op that must not fail the
        # turn.
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        # Exactly one execution phase ran: explore + proposal + execute.
        assert llm.call_count == 3
        # Execution used the plan prompt, not the raw user request.
        # (The stored list is mutated by the loop afterwards, so scan
        # every message in the last call rather than just the tail.)
        last_msgs = "\n".join(m.text for m in llm.messages_history[-1])
        assert "I have reviewed and approved" in last_msgs
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_reject_after_approve_is_ignored(self, session_mgr, llm):
        """A rejection arriving after approval is stale input — ignored,
        so the approved plan still executes."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        # The contract is explicit: a rejection arriving while the plan
        # executes is ignored (False) — there is no way to abort an
        # executing plan through the decision API.
        assert session_mgr.reject_plan(
            plan_id=event.plan.id, feedback="too late",
        ) is False
        remaining = await self._collect(gen)
        # The rejection didn't clobber the plan or the state machine.
        assert session_mgr.planner.plan is not None
        assert llm.call_count == 3
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_double_reject_with_feedback_keeps_first_feedback(
        self, session_mgr, llm,
    ):
        """A second reject_plan() while a decision is pending is stale
        input — ignored, so the FIRST feedback survives and drives the
        re-exploration."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        assert session_mgr.reject_plan(
            plan_id=event.plan.id, feedback="avoid sqlite",
        ) is True
        assert session_mgr.reject_plan(
            plan_id=event.plan.id, feedback="stale feedback",
        ) is False
        second_plan = None
        async for event in gen:
            if isinstance(event, PlanProposed):
                second_plan = event
                break
        assert second_plan is not None
        # The feedback loop ran once: explore + propose + re-explore
        # + re-propose.
        assert llm.call_count == 4
        # The re-exploration used the FIRST feedback — the stale second
        # rejection never touched the state.
        texts = "\n".join(m.text for m in session_mgr.context.messages)
        assert "avoid sqlite" in texts
        assert "stale feedback" not in texts

    @pytest.mark.asyncio
    async def test_approve_after_reject_with_feedback_is_ignored(
        self, session_mgr, llm,
    ):
        """An approval arriving after a feedback rejection is stale input —
        ignored, so the machine stays in the plan workflow and re-proposes
        instead of being clobbered into FINISHED.  A stale approval must
        not flip the permission gating either."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                first_plan = event.plan
                break
        assert session_mgr.reject_plan(
            plan_id=first_plan.id, feedback="avoid sqlite",
        ) is True
        # Stale approval — must not mark the machine FINISHED, and must
        # not switch gating to AUTO (which would auto-approve the
        # re-exploration's tool calls).
        assert (
            session_mgr.approve_plan(
                plan_id=first_plan.id,
                permission_mode=PermissionMode.AUTO,
            )
            is False
        )
        assert session_mgr.permission_mode == PermissionMode.MANUAL
        # The feedback loop still runs: explore + propose + re-explore
        # + re-propose.  (Before the guard, the approval clobbered the
        # machine into FINISHED and the generator ended here silently.)
        second_plan = None
        async for event in gen:
            if isinstance(event, PlanProposed):
                second_plan = event
                break
        assert second_plan is not None
        assert second_plan.plan.title == "Mock Plan"
        assert llm.call_count == 4
        # The feedback still drove the re-exploration.
        texts = "\n".join(m.text for m in session_mgr.context.messages)
        assert "avoid sqlite" in texts
        # An accepted approval for the CURRENT plan flips gating as usual.
        assert (
            session_mgr.approve_plan(
                plan_id=second_plan.plan.id,
                permission_mode=PermissionMode.AUTO,
            )
            is True
        )
        assert session_mgr.permission_mode == PermissionMode.AUTO
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_plan_proposed_but_not_executing_still_finishes(
        self, session_mgr,
    ):
        """A plan turn that never executes (failed approval transition
        leaves the machine non-executing) must still end with a terminal
        event.  The plan panel re-enters the streaming renderer's alt
        screen, and only AgentFinished stops it."""
        plan = _plan(
            title="Stale Plan",
            steps=[PlanStep(id="step-1", description="Do it")],
        )
        # The plan loop ends with a PlanProposed but no terminal event,
        # and the machine is left in a non-executing mode — the
        # is_executing gate then skips the execution phase.
        session_mgr._planner = _SilentPlanner(
            plan, emit_proposal=True,
        )

        events = await self._collect(
            session_mgr.process_turn("refactor the database layer"),
        )
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].reason == "Plan did not proceed to execution."

    @pytest.mark.asyncio
    async def test_plan_loop_silent_finish_ends_with_terminal(
        self, session_mgr,
    ):
        """A plan loop that returns without a plan AND without a
        terminal event (e.g. an external caller finished the machine
        mid-cycle) must still yield AgentFinished — the session manager
        guarantees every plan turn ends with a terminal event."""
        session_mgr._planner = _SilentPlanner(None)

        events = await self._collect(
            session_mgr.process_turn("refactor the database layer"),
        )
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].reason == "Plan cycle ended without a decision."

    @pytest.mark.asyncio
    async def test_recoverable_error_before_silent_end_still_finishes(
        self, session_mgr,
    ):
        """A recoverable error during exploration must NOT satisfy the
        terminal-event guarantee — the CLI keeps the streaming renderer
        running on :class:`RecoverableAgentError` (it tears the alt
        screen down only on AgentFinished / FatalAgentError), so the
        session manager's fallback :class:`AgentFinished` must still fire
        when the plan loop ends silently after one."""
        plan = _plan(
            title="Stale Plan",
            steps=[PlanStep(id="step-1", description="Do it")],
        )
        session_mgr._planner = _SilentPlanner(
            plan, emit_proposal=True, emit_recoverable_error=True,
        )

        events = await self._collect(
            session_mgr.process_turn("refactor the database layer"),
        )
        errors = [
            e for e in events if isinstance(e, RecoverableAgentError)
        ]
        assert len(errors) == 1
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].reason == "Plan did not proceed to execution."

    # ------------------------------------------------------------------
    # Plan execution status tracking
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_execution_without_status_updates_emits_nothing(
        self, session_mgr,
    ):
        """A plan run that never calls plan_update emits no step events."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # No initial all-pending emission — it carries no information,
        # and the final emission dedups against the activation baseline.
        assert updates == []
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1

    @pytest.mark.asyncio
    async def test_plan_update_tool_advances_step_statuses(
        self, session_mgr, llm,
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
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        assert updates, "expected PlanStepUpdate events"
        statuses = [
            next(st[2] for st in e.steps if st[0] == "step-1")
            for e in updates
        ]
        assert statuses == ["in_progress", "completed"]
        # Every event carries the complete list — one emission per
        # mutation, no folding or phase-end flush.
        assert [st[0] for st in updates[-1].steps] == ["step-1"]
        assert len(updates) == 2

    @pytest.mark.asyncio
    async def test_completed_then_in_progress_emits_twice(
        self, session_mgr, llm,
    ):
        """A completed and the next step's start emit separate updates."""
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
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # Each mutation emits at its own ToolCallEnd — the streaming
        # renderer's repaint throttle coalesces adjacent frames.
        assert len(updates) == 2
        assert [st[2] for st in updates[0].steps] == ["completed", "pending"]
        assert [st[2] for st in updates[1].steps] == ["completed", "in_progress"]

    @pytest.mark.asyncio
    async def test_all_steps_completed_emits_closing_snapshot(
        self, session_mgr, llm,
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
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        statuses = [[st[2] for st in e.steps] for e in updates]
        assert statuses == [
            ["in_progress", "pending"],
            ["completed", "pending"],
            ["completed", "in_progress"],
            ["completed", "completed"],
        ]
        # One emission per mutation — no folding, no phase-end flush.
        assert len(updates) == 4

    @pytest.mark.asyncio
    async def test_lone_completed_emits_before_agent_finished(
        self, session_mgr, llm,
    ):
        """A completed with no follow-up emits at its own ToolCallEnd."""
        llm._plan_json = _two_step_plan_json()
        llm._sequence = [
            _end_turn_response(),  # explore phase
            _tool_use_response(
                "plan_update", {"step_id": "step-1", "status": "completed"},
                tool_id="t1",
            ),
        ]
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        remaining = await self._collect(gen)
        updates = [e for e in remaining if isinstance(e, PlanStepUpdate)]
        # The lone completed renders on its own — nothing is held back
        # for a phase-end flush.
        assert len(updates) == 1
        assert [st[2] for st in updates[0].steps] == ["completed", "pending"]
        # The emission lands BEFORE AgentFinished so streaming mode can
        # still paint it (its Live display stops on AgentFinished).
        finished_idx = next(
            i for i, e in enumerate(remaining) if isinstance(e, AgentFinished)
        )
        update_idx = next(
            i for i, e in enumerate(remaining) if isinstance(e, PlanStepUpdate)
        )
        assert update_idx < finished_idx

    @pytest.mark.asyncio
    async def test_plan_tracking_torn_down_after_phase(self, session_mgr):
        """The plan_update tool and shared plan are cleared after the turn."""
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        session_mgr.approve_plan(
            plan_id=event.plan.id,
        )
        await self._collect(gen)
        assert session_mgr._registry.get("plan_update") is None
        assert not session_mgr._plan_state.is_active

    @pytest.mark.asyncio
    async def test_simple_execution_has_no_plan_events(self, session_mgr):
        """Non-plan turns emit no PlanStepUpdate and never register the tool."""
        gen = session_mgr.process_turn("fix a typo")
        events = await self._collect(gen)
        assert not any(isinstance(e, PlanStepUpdate) for e in events)
        assert session_mgr._registry.get("plan_update") is None

    @pytest.mark.asyncio
    async def test_reject_plan_outright_finishes(self, session_mgr):
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        assert session_mgr.reject_plan(plan_id=event.plan.id) is True
        remaining = await self._collect(gen)
        finished = [e for e in remaining if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert "rejected" in finished[0].reason.lower()

    @pytest.mark.asyncio
    async def test_reject_with_feedback_loops(self, session_mgr):
        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break
        assert session_mgr.reject_plan(
            plan_id=event.plan.id, feedback="Add more steps",
        ) is True
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
    async def test_plan_entry_resets_permission_to_manual(self, session_mgr):
        """Entering plan mode resets gating to MANUAL."""
        session_mgr.set_permission_mode(PermissionMode.AUTO)
        assert session_mgr.permission_mode == PermissionMode.AUTO

        gen = session_mgr.process_turn("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert session_mgr.permission_mode == PermissionMode.MANUAL

    @pytest.mark.asyncio
    async def test_direct_execute_preserves_permission_auto(self, session_mgr):
        """A simple request does NOT reset an explicitly-set AUTO mode."""
        session_mgr.set_permission_mode(PermissionMode.AUTO)

        gen = session_mgr.process_turn("hello")
        events = []
        async for event in gen:
            events.append(event)

        assert session_mgr.permission_mode == PermissionMode.AUTO
        has_plan = any(isinstance(e, PlanProposed) for e in events)
        assert not has_plan, "Simple request should not trigger plan"

    @pytest.mark.asyncio
    async def test_plan_pending_flag_resets_permission_to_manual(
        self, session_mgr,
    ):
        """When /plan is used, entry into plan mode resets to MANUAL."""
        session_mgr.set_permission_mode(PermissionMode.AUTO)
        session_mgr.state_machine.flag_plan_pending()

        gen = session_mgr.process_turn("do something")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert session_mgr.permission_mode == PermissionMode.MANUAL


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
        assert planner._sm.current_mode == AgentMode.PLAN_WAITING

    @pytest.mark.asyncio
    async def test_approve_plan(self, settings, llm, agent_loop, ctx):
        """Approve transitions to PLAN_EXECUTING and plan is set."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert planner._sm.current_mode == AgentMode.PLAN_WAITING
        planner.approve_plan(plan_id=planner.plan.id)
        assert planner._sm.current_mode == AgentMode.PLAN_EXECUTING
        assert planner.plan is not None
        assert planner.plan.title == "Mock Plan"

    @pytest.mark.asyncio
    async def test_approve_plan_with_numeric_id_is_not_stale(
        self, settings, llm, agent_loop, ctx,
    ):
        """A numeric plan id from the LLM is coerced to str — otherwise
        ``int != str`` would treat every approval as stale input and the
        turn would hang waiting for a decision that never takes effect."""
        llm._plan_json["id"] = 123
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert planner.plan.id == "123"
        assert planner.approve_plan(plan_id="123") is True
        assert planner._sm.current_mode == AgentMode.PLAN_EXECUTING

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

        assert planner.reject_plan(plan_id=planner.plan.id) is True
        assert planner._sm.current_mode == AgentMode.FINISHED
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

        assert planner.reject_plan(
            plan_id=planner.plan.id, feedback="Add more steps",
        ) is True
        assert planner._sm.current_mode == AgentMode.PLAN_EXPLORING
        assert planner.plan is None

        # Advance the generator one step so the feedback injection code
        # runs (the generator was paused at `yield PlanProposed`; after
        # reject_plan() sets the event, we need to resume past the
        # `await event.wait()` to reach the `ctx.append()` call).
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

        # Verify feedback was injected into context.
        feedback_msgs = [
            m for m in ctx._appended
            if "Add more steps" in (m.text or "")
        ]
        assert len(feedback_msgs) == 1

    @pytest.mark.asyncio
    async def test_double_approve_is_idempotent(
        self, settings, llm, agent_loop, ctx,
    ):
        """Approving an already-approved plan is a no-op success — it
        must not fail the transition and kill the turn."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert planner.approve_plan(plan_id=planner.plan.id) is True
        # Second approval is stale input — ignored, not an error.
        assert planner.approve_plan(plan_id=planner.plan.id) is False
        assert planner._sm.current_mode == AgentMode.PLAN_EXECUTING
        assert planner.plan is not None
        assert planner.plan.title == "Mock Plan"

    @pytest.mark.asyncio
    async def test_approve_wrong_plan_is_ignored(
        self, settings, llm, agent_loop, ctx,
    ):
        """An approval for a different plan is stale input — the machine
        keeps waiting on the current plan's own decision."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        current = planner.plan
        assert planner.approve_plan(plan_id="some-other-plan") is False
        assert planner._sm.current_mode == AgentMode.PLAN_WAITING
        assert planner.plan is current
        # The decision for the current plan still goes through.
        assert planner.approve_plan(plan_id=current.id) is True
        assert planner._sm.current_mode == AgentMode.PLAN_EXECUTING

    @pytest.mark.asyncio
    async def test_reject_after_approve_is_ignored(
        self, settings, llm, agent_loop, ctx,
    ):
        """A rejection arriving after approval is stale input — the
        approval stands and the plan is not cleared."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        planner.approve_plan(plan_id=planner.plan.id)
        # The contract is explicit: a rejection outside PLAN_WAITING is
        # ignored and reports False — an executing plan runs to
        # completion.
        assert planner.reject_plan(
            plan_id=planner.plan.id, feedback="too late",
        ) is False
        assert planner._sm.current_mode == AgentMode.PLAN_EXECUTING
        assert planner.plan is not None

    @pytest.mark.asyncio
    async def test_double_reject_keeps_first_feedback(
        self, settings, llm, agent_loop, ctx,
    ):
        """A second reject_plan() while a decision is pending is stale
        input — the first feedback survives and drives the
        re-exploration."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        assert planner.reject_plan(
            plan_id=event.plan.id, feedback="avoid sqlite",
        ) is True
        assert planner.reject_plan(
            plan_id=event.plan.id, feedback="stale feedback",
        ) is False
        assert planner._sm.current_mode == AgentMode.PLAN_EXPLORING
        assert planner.plan is None

        # Resume past the wait so the feedback injection runs.
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

        # Only the FIRST feedback reached the context.
        feedback_msgs = [
            m for m in ctx._appended
            if "avoid sqlite" in (m.text or "")
        ]
        assert len(feedback_msgs) == 1
        stale_msgs = [
            m for m in ctx._appended
            if "stale feedback" in (m.text or "")
        ]
        assert len(stale_msgs) == 0

    @pytest.mark.asyncio
    async def test_reject_wrong_plan_is_ignored(
        self, settings, llm, agent_loop, ctx,
    ):
        """A rejection for a different plan is stale input — the machine
        keeps waiting on the current plan's own decision."""
        planner = self._make_planner(settings, llm, ctx, agent_loop)
        gen = planner.run("refactor the database layer")
        async for event in gen:
            if isinstance(event, PlanProposed):
                break

        current = planner.plan
        assert planner.reject_plan(
            plan_id="some-other-plan", feedback="stale",
        ) is False
        assert planner._sm.current_mode == AgentMode.PLAN_WAITING
        assert planner.plan is current
        # The decision for the current plan still goes through.
        assert planner.reject_plan(
            plan_id=current.id, feedback="real feedback",
        ) is True
        assert planner._sm.current_mode == AgentMode.PLAN_EXPLORING
        assert planner.plan is None

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
        assert planner._sm.current_mode == AgentMode.FINISHED
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
        assert planner._sm.current_mode == AgentMode.PLAN_WAITING


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

    # Tests for plan-entry reset at the SessionManager level are in
    # TestSessionManagerPlanWorkflow below.


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
        self, prompt: str, choices: list[str], *, allow_feedback: bool = False,
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
    def settings(self):
        from toddler.config.settings import Settings
        return Settings(streaming_enabled=False)

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
    async def test_approve_with_manual_executes(self, cli, session_mgr):
        """approve_with_manual approves the plan and runs execution."""
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="approve_with_manual"),
        )

        assert session_mgr.planner.plan is not None
        assert session_mgr.permission_mode == PermissionMode.MANUAL
        assert renderer.calls.count("confirm") == 1
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_approve_with_auto_switches_gating(self, cli, session_mgr):
        """approve_with_auto approves the plan and switches gating to AUTO."""
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="approve_with_auto"),
        )

        assert session_mgr.planner.plan is not None
        assert session_mgr.permission_mode == PermissionMode.AUTO
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_deny_finishes_turn(self, cli, session_mgr):
        """deny rejects the plan outright and ends the turn."""
        renderer = await self._run_plan_turn(
            cli, ConfirmResult(decision="deny"),
        )

        assert session_mgr.planner.plan is None
        assert "on_agent_finished" in renderer.calls

    @pytest.mark.asyncio
    async def test_feedback_reproposes_then_approves(self, cli, session_mgr):
        """feedback re-enters the explore loop and re-proposes a plan,
        which is then approved — confirm() is asked twice."""
        renderer = await self._run_plan_turn(
            cli,
            ConfirmResult(decision="feedback", feedback="avoid sqlite"),
            ConfirmResult(decision="approve_with_manual"),
        )

        assert renderer.calls.count("confirm") == 2
        assert session_mgr.planner.plan is not None
        assert "on_agent_finished" in renderer.calls
