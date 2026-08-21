"""Renderer tests — plan step status display."""

from __future__ import annotations

import io

from rich.console import Console

from toddler.agent.events import PlanStepUpdate, ToolCallStart
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


class TestDynamicPanelHeightBudget:
    """The panel budget is charged only by panels that actually render.

    ``_compute_dynamic_panel_height()`` costs each panel its chrome plus
    rows; a plan must fit at least one row past its chrome before the
    charge applies, so a plan with no rows (or no room for one) never
    drains the pool the tools panel draws from.
    """

    def _renderer(self, height: int) -> StreamingRenderer:
        return StreamingRenderer(
            console=Console(file=io.StringIO(), height=height)
        )

    def test_one_row_plan_fits_at_chrome_plus_one(self):
        # height 14: budget 10, minus the 5-line output minimum leaves 5
        # for extras — exactly the 4 chrome lines + 1 plan row.
        renderer = self._renderer(height=14)
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=[("step-1", "First thing", "pending")])
        )
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_plan_visible == 1
        # The plan panel (chrome + 1 row) is charged, leaving output at
        # its 5-line minimum.
        assert output_height == 5

    def test_chrome_only_budget_never_charges_the_plan_panel(self):
        # height 15: 6 rows for extras.  A plan with no rows must not
        # charge its chrome — otherwise the 6 rows that would show the
        # first tool are drained to 2 and the tools panel is dropped.
        renderer = self._renderer(height=15)
        renderer._plan_steps = []
        renderer.on_tool_call_start(
            ToolCallStart(tool_id="t1", tool_name="read", partial_input={})
        )
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_plan_visible == 0
        assert renderer._max_tools_visible == 1
        assert output_height == 5

    def test_plan_dropped_when_only_chrome_fits_leaves_budget_intact(self):
        # height 13: 4 rows for extras.  A 1-step plan needs 5, so it is
        # dropped — and because nothing is charged, the output panel
        # keeps the whole budget instead of losing the plan's chrome.
        renderer = self._renderer(height=13)
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=[("step-1", "First thing", "pending")])
        )
        output_height = renderer._compute_dynamic_panel_height()
        assert renderer._max_plan_visible == 0
        assert output_height == 9
