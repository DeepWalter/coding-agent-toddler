"""Renderer tests — plan step status display."""

from __future__ import annotations

import io

from rich.console import Console

from toddler.agent.events import (
    AgentFinished,
    FatalAgentError,
    PlanProposed,
    PlanStepUpdate,
    ToolCallStart,
)
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
    """The one-shot handler prints one line per step only when the
    snapshot is render-worthy: a step start, or the closing snapshot.
    A bare completed mid-plan folds into the next step's start, and a
    snapshot still folded at phase end is flushed by
    on_agent_finished.
    """

    def _renderer(self) -> tuple[NonStreamingRenderer, io.StringIO]:
        buf = io.StringIO()
        return NonStreamingRenderer(console=Console(file=buf)), buf

    def test_in_progress_update_prints_all_steps(self):
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        output = buf.getvalue()
        assert "▶️ step-1: First thing" in output
        assert "⬜ step-2: Second thing" in output

    def test_all_pending_prints_nothing(self):
        renderer, buf = self._renderer()
        renderer.on_plan_step_update(
            PlanStepUpdate(steps=_triples(_plan_with_two_steps()))
        )
        assert buf.getvalue() == ""

    def test_each_event_reprints_the_complete_list(self):
        renderer, buf = self._renderer()
        plan = _plan_with_two_steps()
        steps = _triples(plan)
        steps[0] = ("step-1", "First thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        steps[0] = ("step-1", "First thing", "completed")
        steps[1] = ("step-2", "Second thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        output = buf.getvalue()
        assert "✅ step-1: First thing" in output
        # The second event also carried step-2 — every event prints the
        # complete list, never just the changed step.
        assert output.count("step-2") == 2

    def test_render_worthy_events_are_not_deduplicated(self):
        # The same render-worthy event printed twice prints twice — the
        # fold memory never suppresses an actually-shown snapshot.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        assert buf.getvalue().count("step-1") == 2

    def test_bare_completed_mid_plan_is_skipped(self):
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        assert buf.getvalue() == ""

    def test_completed_folds_into_next_steps_start(self):
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        steps[1] = ("step-2", "Second thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        output = buf.getvalue()
        # One combined print — the completed and the next step's start.
        assert output.count("step-1") == 1
        assert "✅ step-1: First thing" in output
        assert "▶️ step-2: Second thing" in output

    def test_closing_snapshot_prints(self):
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        steps[1] = ("step-2", "Second thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        output = buf.getvalue()
        assert output.count("step-1") == 1
        assert "✅ step-2: Second thing" in output

    def test_folded_snapshot_flushed_at_phase_end(self):
        # A lone completed never renders mid-run — the turn ends with it
        # folded, so on_agent_finished prints the terminal picture.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        assert buf.getvalue() == ""
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        output = buf.getvalue()
        assert "✅ step-1: First thing" in output
        assert "⬜ step-2: Second thing" in output
        # The plan flush lands before the completion summary.
        assert output.index("✅ step-1") < output.index("Done")

    def test_no_duplicate_flush_when_snapshot_was_printed(self):
        # A normal chained run already printed the terminal picture —
        # the phase-end flush must not reprint it.
        renderer, buf = self._renderer()
        plan = _plan_with_two_steps()
        steps = _triples(plan)
        steps[0] = ("step-1", "First thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        steps[0] = ("step-1", "First thing", "completed")
        steps[1] = ("step-2", "Second thing", "in_progress")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        assert buf.getvalue().count("step-1") == 2

    def test_finish_without_plan_events_prints_no_status(self):
        # No plan_update ever arrived — nothing folded, nothing to flush.
        renderer, buf = self._renderer()
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        assert "step-" not in buf.getvalue()

    def test_new_plan_resets_display_state(self):
        # The renderer outlives turns — an earlier turn's folded
        # snapshot must not leak into a later turn's phase end.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        assert "✅ step-1" in buf.getvalue()
        renderer.on_plan_proposed(PlanProposed(plan=_plan_with_two_steps()))
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        # Only the first turn's flush printed — the second turn had no
        # plan events, so nothing stale reappears.
        assert buf.getvalue().count("✅ step-1") == 1

    def test_fatal_error_flushes_folded_snapshot(self):
        # A fatal error ends the turn without on_agent_finished — the
        # terminal plan picture must print with the error, never in a
        # later turn's summary.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.on_agent_error(FatalAgentError(message="boom"))
        output = buf.getvalue()
        # Flushed before the error message...
        assert output.index("✅ step-1: First thing") < output.index("Fatal")
        # ...and not again in a later turn's completion summary.
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        assert buf.getvalue().count("✅ step-1") == 1

    def test_start_drops_unflushed_snapshot_from_prior_turn(self):
        # An exit path that never flushes (e.g. an interrupted turn)
        # leaves a folded snapshot behind — the next turn's start drops
        # it so it cannot surface in that turn's summary.
        renderer, buf = self._renderer()
        steps = _triples(_plan_with_two_steps())
        steps[0] = ("step-1", "First thing", "completed")
        renderer.on_plan_step_update(PlanStepUpdate(steps=steps))
        renderer.start()
        renderer.on_agent_finished(AgentFinished(reason="completed"))
        assert "step-" not in buf.getvalue()


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
