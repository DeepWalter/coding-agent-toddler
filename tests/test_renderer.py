"""Renderer tests — plan step status display."""

from __future__ import annotations

import io

from rich.console import Console

from toddler.agent.events import PlanStepUpdate
from toddler.agent.planner import Plan, PlanStep
from toddler.cli.renderer import NonStreamingRenderer, StreamingRenderer


def _plan_with_two_steps() -> Plan:
    return Plan(
        id="p1",
        title="Test Plan",
        summary="A test plan.",
        steps=[
            PlanStep(id="step-1", description="First thing"),
            PlanStep(id="step-2", description="Second thing"),
        ],
    )


def _triples(plan: Plan) -> list[tuple[str, str, str]]:
    """Build all-pending step triples from a Plan."""
    return [(s.id, s.description, "pending") for s in plan.steps]


class TestNonStreamingPlanStepUpdates:
    """The one-shot handler prints one line per step in each event."""

    def _renderer(self) -> tuple[NonStreamingRenderer, io.StringIO]:
        buf = io.StringIO()
        return NonStreamingRenderer(console=Console(file=buf)), buf

    def test_full_update_prints_all_steps(self):
        renderer, buf = self._renderer()
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=_triples(_plan_with_two_steps()))
        )
        output = buf.getvalue()
        assert "⬜ step-1: First thing" in output
        assert "⬜ step-2: Second thing" in output

    def test_each_event_reprints_the_complete_list(self):
        renderer, buf = self._renderer()
        plan = _plan_with_two_steps()
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=_triples(plan))
        )
        steps = _triples(plan)
        steps[0] = ("step-1", "First thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        output = buf.getvalue()
        assert "▶️ step-1: First thing" in output
        # The second event also carried step-2 — every event prints the
        # complete list, never just the changed step.
        assert output.count("step-2") == 2

    def test_handler_is_stateless(self):
        # The same event printed twice prints twice — no stored state.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        assert buf.getvalue().count("step-1") == 2


class TestStreamingPlanStepUpdates:
    """The TUI handler replaces its step snapshot from each event; the
    panel is built later.
    """

    def _renderer(self) -> StreamingRenderer:
        return StreamingRenderer(console=Console(file=io.StringIO()))

    def test_full_update_seeds_step_list(self):
        renderer = self._renderer()
        plan = _plan_with_two_steps()
        steps = _triples(plan)
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        assert renderer._plan_steps == [
            ("step-1", "First thing", "completed"),
            ("step-2", "Second thing", "pending"),
        ]

    def test_update_replaces_the_step_list(self):
        renderer = self._renderer()
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=_triples(_plan_with_two_steps()))
        )
        # A later event carries only one step — the panel must replace,
        # not patch, so the missing step disappears.
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=[("step-1", "First thing", "completed")])
        )
        assert renderer._plan_steps == [
            ("step-1", "First thing", "completed"),
        ]

    def test_plan_panel_hidden_without_updates(self):
        renderer = self._renderer()
        assert renderer._build_plan_panel() is None

    def test_plan_panel_rows_show_id_status_and_do_not_wrap(self):
        renderer = self._renderer()
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=[("step-1", "First thing", "in_progress")])
        )
        renderer._max_plan_visible = 1
        panel = renderer._build_plan_panel()
        assert panel is not None
        rows = panel.renderable.renderables
        assert len(rows) == 1
        row = rows[0]
        assert row.plain == "▶️ step-1: First thing"
        # Single-line contract: the height budget counts one line per row.
        assert row.no_wrap is True
        assert row.overflow == "ellipsis"
