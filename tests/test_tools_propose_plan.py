"""Tests for the markdown ``propose_plan`` tool."""

from __future__ import annotations

import pytest

from toddler.tools import HIDDEN_TOOL_NAMES
from toddler.tools.plan import (
    PROPOSE_PLAN_NAME,
    Plan,
    PlanValidationError,
    ProposePlanTool,
)
from toddler.tools.policy import Permission
from toddler.tools.prompts import ChoiceLevel, ChoiceType


def _tool() -> ProposePlanTool:
    return ProposePlanTool()


# ---------------------------------------------------------------------------
# Plan.from_arguments
# ---------------------------------------------------------------------------


def test_from_arguments_builds_a_markdown_plan():
    plan = Plan.from_arguments(
        {
            "title": "  Fix   auth ",
            "plan_markdown": "\n## Plan\n\nDo the thing.\n",
        },
        plan_id="call-1",
    )
    assert plan.id == "call-1"
    assert plan.title == "Fix auth"
    assert plan.markdown == "## Plan\n\nDo the thing."
    assert plan.format_for_display() == plan.markdown
    assert plan.steps == []


def test_from_arguments_rejects_an_empty_title():
    with pytest.raises(PlanValidationError, match="title"):
        Plan.from_arguments(
            {"title": "  ", "plan_markdown": "# Body"}, plan_id="c",
        )


def test_from_arguments_rejects_an_empty_plan():
    with pytest.raises(PlanValidationError, match="plan_markdown"):
        Plan.from_arguments(
            {"title": "T", "plan_markdown": "   "}, plan_id="c",
        )


def test_from_arguments_rejects_non_string_values():
    with pytest.raises(PlanValidationError, match="title"):
        Plan.from_arguments(
            {"title": 3, "plan_markdown": "# Body"}, plan_id="c",
        )
    with pytest.raises(PlanValidationError, match="plan_markdown"):
        Plan.from_arguments(
            {"title": "T", "plan_markdown": None}, plan_id="c",
        )


# ---------------------------------------------------------------------------
# Tool schema and prompt
# ---------------------------------------------------------------------------


def test_the_call_carries_title_and_markdown_only():
    schema = _tool().to_api_schema()["function"]["parameters"]
    assert schema["required"] == ["title", "plan_markdown"]
    assert set(schema["properties"]) == {"title", "plan_markdown"}


def test_the_prompt_carries_the_plan_and_four_choices():
    prompt = _tool().build_call_prompt(
        title="Fix auth", plan_markdown="# Plan\n\nDo it.",
    )
    assert prompt.title == "Approve this plan?"
    assert prompt.content == "# Plan\n\nDo it."
    assert prompt.description == "Fix auth"
    assert prompt.choices.type is ChoiceType.SINGLE
    rows = prompt.choices.data
    assert [row.level for row in rows] == [
        ChoiceLevel.APPROVE,
        ChoiceLevel.APPROVE,
        ChoiceLevel.DENY,
        ChoiceLevel.FEEDBACK,
    ]


def test_an_invalid_plan_never_builds_a_card():
    tool = _tool()
    with pytest.raises(PlanValidationError):
        tool.build_call_prompt(title="", plan_markdown="# Body")
    with pytest.raises(PlanValidationError):
        tool.build_call_prompt(title="T", plan_markdown="  ")


# ---------------------------------------------------------------------------
# Tool behaviour
# ---------------------------------------------------------------------------


def test_the_tool_is_interactive_and_hidden():
    tool = _tool()
    assert tool.permission is Permission.INTERACT
    assert tool.visible is False
    assert tool.in_default_registry is False
    assert PROPOSE_PLAN_NAME in HIDDEN_TOOL_NAMES


@pytest.mark.asyncio
async def test_execute_returns_the_execution_contract():
    result = await _tool().execute(
        title="Fix auth", plan_markdown="# Plan\n\nDo it.",
    )
    assert result.success
    assert "approved" in result.output
    assert "todo_write" in result.output
    assert "in order" in result.output


@pytest.mark.asyncio
async def test_execute_reports_a_correctable_error():
    result = await _tool().execute(title="T", plan_markdown="")
    assert not result.success
    assert "plan_markdown" in result.error
