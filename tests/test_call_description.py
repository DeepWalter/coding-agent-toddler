"""Call-description tests — the param tool schemas carry by default.

The merge lives in :meth:`BaseTool.to_api_schema`, so a tool inherits the
param by construction — a tool whose arguments already say what the call
does declares ``require_description_parameter = False`` and is handed
back its own parameters instead.  These tests hold both branches, the
untouched-declaration guarantee they depend on, and the strip that keeps
the param out of tool ``execute`` signatures.
"""

from __future__ import annotations

import json

import pytest

from toddler.tools import create_default_registry
from toddler.tools.base import (
    CALL_DESCRIPTION_PARAM,
    CALL_DESCRIPTION_USAGE,
    BaseTool,
    Permission,
    ToolCall,
    ToolResult,
    execution_params,
)
from toddler.tools.executor import ToolExecutor
from toddler.tools.files import ReadFile
from toddler.tools.plan import PLAN_UPDATE_USAGE, PlanState, PlanUpdateTool
from toddler.tools.registry import ToolRegistry
from toddler.tools.shell import Shell


def _every_tool() -> list[BaseTool]:
    """Every tool the agent can offer.

    The registry factory covers the built-ins; ``plan_update`` is the one
    tool registered outside it, dynamically, while a plan executes.
    """
    return [
        *create_default_registry().list_all(),
        PlanUpdateTool(PlanState()),
    ]


def _injecting_tools() -> list[BaseTool]:
    """The tools whose schemas carry the call-description param."""
    return [t for t in _every_tool() if t.require_description_parameter]


def _opted_out_tools() -> list[BaseTool]:
    """The tools that opt out — their own arguments say what the call
    does."""
    return [t for t in _every_tool() if not t.require_description_parameter]


# ---------------------------------------------------------------------------
# Schema injection
# ---------------------------------------------------------------------------


class TestSchemaInjection:

    def test_both_branches_have_a_tool_to_guard(self):
        """Every loop below passes vacuously on an empty bucket."""
        assert _injecting_tools()
        assert _opted_out_tools()

    def test_every_injecting_tool_requires_a_description(self):
        """Drift guard: adding a tool means inheriting the param, not
        remembering it — the factory and the plan tool are both covered."""
        for tool in _injecting_tools():
            params = tool.to_api_schema()["function"]["parameters"]
            assert params["properties"][CALL_DESCRIPTION_PARAM]["type"] == (
                "string"
            ), tool.name
            assert CALL_DESCRIPTION_PARAM in params["required"], tool.name

    def test_description_leads_properties_and_required(self):
        """First in both: the model emits it first, and the panels render
        arguments in the order they arrived."""
        for tool in _injecting_tools():
            params = tool.to_api_schema()["function"]["parameters"]
            assert next(iter(params["properties"])) == CALL_DESCRIPTION_PARAM
            assert params["required"][0] == CALL_DESCRIPTION_PARAM

    def test_tool_description_carries_the_usage_line(self):
        for tool in _injecting_tools():
            description = tool.to_api_schema()["function"]["description"]
            assert CALL_DESCRIPTION_USAGE in description, tool.name

    def test_plan_update_keeps_its_own_usage_prose(self):
        """The reporting protocol is the plan tool's description to carry —
        no appended line displaces it."""
        schema = PlanUpdateTool(PlanState()).to_api_schema()
        assert PLAN_UPDATE_USAGE in schema["function"]["description"]


# ---------------------------------------------------------------------------
# The tool's own declaration
# ---------------------------------------------------------------------------


class TestDeclarationIsUntouched:

    def test_injection_does_not_mutate_the_declaration(self):
        """``parameters`` is a class attribute shared by every instance —
        the merge has to build fresh dicts, not edit in place."""
        tool = Shell()
        before = json.dumps(tool.parameters, sort_keys=True)

        for _ in range(3):
            tool.to_api_schema()

        assert json.dumps(tool.parameters, sort_keys=True) == before
        assert CALL_DESCRIPTION_PARAM not in tool.parameters["properties"]

    def test_required_does_not_grow_across_calls(self):
        """The in-place append bug leaves one entry per call."""
        required = (
            Shell().to_api_schema()["function"]["parameters"]["required"]
        )
        assert required.count(CALL_DESCRIPTION_PARAM) == 1

    def test_each_call_returns_a_fresh_schema(self):
        """Nothing downstream may hold a dict another tool's schema also
        holds — the provider passes the list to the SDK uncopied."""
        tool = Shell()
        first = tool.to_api_schema()
        second = tool.to_api_schema()

        assert first == second
        assert first is not second
        assert (
            first["function"]["parameters"]["properties"]
            is not second["function"]["parameters"]["properties"]
        )

    def test_a_tool_may_not_shadow_the_param(self):
        """A declaration that collides would silently ship a schema whose
        required property is not the one the base class describes."""

        class Shadowing(BaseTool):
            name = "shadowing"
            description = "Declares its own description property"
            parameters = {
                "type": "object",
                "properties": {CALL_DESCRIPTION_PARAM: {"type": "string"}},
            }

            async def execute(self, **kwargs) -> ToolResult:
                return ToolResult(
                    tool_id="", tool_name=self.name, success=True, output="",
                )

        with pytest.raises(ValueError, match=CALL_DESCRIPTION_PARAM):
            Shadowing().to_api_schema()


# ---------------------------------------------------------------------------
# Opting out of the param
# ---------------------------------------------------------------------------


class TestOptOut:
    """A tool whose own arguments say what the call does declares
    ``require_description_parameter = False`` and ships its declaration
    alone — nothing is merged, and nothing of the injection is owed."""

    def test_the_opted_out_tools_are_the_expected_ones(self):
        """Spelled out, like the registry's built-in list: a tool that
        opts out is a decision, not a default, so one that stops doing so
        has to fail here rather than in the model's schema."""
        assert {t.name for t in _opted_out_tools()} == {
            "read_file", "write_file", "edit_file", "plan_update",
        }

    def test_no_description_property_is_injected(self):
        for tool in _opted_out_tools():
            params = tool.to_api_schema()["function"]["parameters"]
            assert CALL_DESCRIPTION_PARAM not in params["properties"], tool.name
            assert CALL_DESCRIPTION_PARAM not in params["required"], tool.name

    def test_the_description_carries_no_usage_line(self):
        """The line tells the model to fill a param this schema has not
        got."""
        for tool in _opted_out_tools():
            description = tool.to_api_schema()["function"]["description"]
            assert CALL_DESCRIPTION_USAGE not in description, tool.name

    def test_the_declaration_ships_as_written(self):
        """``properties`` and ``required`` come back equal to what the
        class declared — and still fresh dicts, so a schema handed to the
        provider never aliases the class attribute."""
        tool = ReadFile()
        params = tool.to_api_schema()["function"]["parameters"]

        assert params == tool.parameters
        assert params["properties"] is not tool.parameters["properties"]
        assert params["required"] is not tool.parameters["required"]

        # Repeated calls share nothing either.
        again = tool.to_api_schema()["function"]["parameters"]
        assert again["properties"] is not params["properties"]

    def test_an_opted_out_tool_still_may_not_shadow_the_param(self):
        """The executor strips the key before ``execute`` whatever the
        tool declared, so the collision is a bug here too."""

        class Shadowing(BaseTool):
            name = "shadowing"
            description = "Declares its own description property"
            require_description_parameter = False
            parameters = {
                "type": "object",
                "properties": {CALL_DESCRIPTION_PARAM: {"type": "string"}},
            }

            async def execute(self, **kwargs) -> ToolResult:
                return ToolResult(
                    tool_id="", tool_name=self.name, success=True, output="",
                )

        with pytest.raises(ValueError, match=CALL_DESCRIPTION_PARAM):
            Shadowing().to_api_schema()


# ---------------------------------------------------------------------------
# Stripping before execution
# ---------------------------------------------------------------------------


class TestExecutionParams:

    def test_drops_the_description_and_keeps_the_original(self):
        params = {"file_path": "a.py", CALL_DESCRIPTION_PARAM: "read a.py"}
        assert execution_params(params) == {"file_path": "a.py"}
        # The dict is the tool-call record — the events and the transcript
        # read the same object, so the param has to survive in it.
        assert params[CALL_DESCRIPTION_PARAM] == "read a.py"

    def test_everything_else_passes_through(self):
        params = {"a": 1, "b": None}
        assert execution_params(params) == params


class ProbeTool(BaseTool):
    """A tool shaped like the built-ins — arguments spelled out, no
    ``**kwargs`` to swallow a stray one."""

    name = "probe"
    description = "Record the arguments it was executed with"
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }

    def __init__(self) -> None:
        self.seen: dict = {}

    async def execute(self, message: str) -> ToolResult:
        self.seen = {"message": message}
        return ToolResult(
            tool_id="", tool_name=self.name, success=True, output=message,
        )

    @property
    def permission(self) -> Permission:
        return Permission.READ


class TestExecutorStrip:

    def _executor(self) -> tuple[ToolExecutor, ProbeTool]:
        tool = ProbeTool()
        registry = ToolRegistry()
        registry.register(tool)
        return ToolExecutor(registry), tool

    async def test_explicit_signature_tool_still_executes(self):
        """Without the strip the splat raises TypeError, which the
        executor's broad handler would feed back to the model as a tool
        failure rather than a bug."""
        executor, tool = self._executor()
        call = ToolCall(
            tool_id="c1",
            tool_name=tool.name,
            parameters={
                "message": "hi",
                CALL_DESCRIPTION_PARAM: "send a greeting",
            },
        )

        result = await executor.execute(call, mutating=False)

        assert result.success is True
        assert tool.seen == {"message": "hi"}

    async def test_the_call_record_keeps_the_description(self):
        """The strip copies rather than popping: the events, the persisted
        transcript, and ``summarize_call`` all read this dict."""
        executor, tool = self._executor()
        params = {
            "message": "hi",
            CALL_DESCRIPTION_PARAM: "send a greeting",
        }
        call = ToolCall(tool_id="c1", tool_name=tool.name, parameters=params)

        await executor.execute(call, mutating=False)

        assert params[CALL_DESCRIPTION_PARAM] == "send a greeting"
        assert call.parameters is params
