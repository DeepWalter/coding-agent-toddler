"""Call-description tests — the required param every tool schema carries.

The merge lives in :meth:`BaseTool.to_api_schema`, so a tool inherits the
param by construction.  These tests hold that invariant, the
untouched-declaration guarantee it depends on, and the strip that keeps
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


def _every_tool() -> list[BaseTool]:
    """Every tool the agent can offer.

    The registry factory covers the built-ins; ``plan_update`` is the one
    tool registered outside it, dynamically, while a plan executes.
    """
    return [
        *create_default_registry().list_all(),
        PlanUpdateTool(PlanState()),
    ]


# ---------------------------------------------------------------------------
# Schema injection
# ---------------------------------------------------------------------------


class TestSchemaInjection:

    def test_every_tool_requires_a_description(self):
        """Drift guard: adding a tool means inheriting the param, not
        remembering it — the factory and the plan tool are both covered."""
        for tool in _every_tool():
            params = tool.to_api_schema()["function"]["parameters"]
            assert params["properties"][CALL_DESCRIPTION_PARAM]["type"] == (
                "string"
            ), tool.name
            assert CALL_DESCRIPTION_PARAM in params["required"], tool.name

    def test_description_leads_properties_and_required(self):
        """First in both: the model emits it first, and the panels render
        arguments in the order they arrived."""
        for tool in _every_tool():
            params = tool.to_api_schema()["function"]["parameters"]
            assert next(iter(params["properties"])) == CALL_DESCRIPTION_PARAM
            assert params["required"][0] == CALL_DESCRIPTION_PARAM

    def test_tool_description_carries_the_usage_line(self):
        for tool in _every_tool():
            description = tool.to_api_schema()["function"]["description"]
            assert CALL_DESCRIPTION_USAGE in description, tool.name

    def test_plan_update_keeps_its_own_usage_prose(self):
        """The reporting protocol is the plan tool's description to carry —
        the appended line must not displace it."""
        schema = PlanUpdateTool(PlanState()).to_api_schema()
        assert PLAN_UPDATE_USAGE in schema["function"]["description"]


# ---------------------------------------------------------------------------
# The tool's own declaration
# ---------------------------------------------------------------------------


class TestDeclarationIsUntouched:

    def test_injection_does_not_mutate_the_declaration(self):
        """``parameters`` is a class attribute shared by every instance —
        the merge has to build fresh dicts, not edit in place."""
        tool = ReadFile()
        before = json.dumps(tool.parameters, sort_keys=True)

        for _ in range(3):
            tool.to_api_schema()

        assert json.dumps(tool.parameters, sort_keys=True) == before
        assert CALL_DESCRIPTION_PARAM not in tool.parameters["properties"]

    def test_required_does_not_grow_across_calls(self):
        """The in-place append bug leaves one entry per call."""
        required = (
            ReadFile().to_api_schema()["function"]["parameters"]["required"]
        )
        assert required.count(CALL_DESCRIPTION_PARAM) == 1

    def test_each_call_returns_a_fresh_schema(self):
        """Nothing downstream may hold a dict another tool's schema also
        holds — the provider passes the list to the SDK uncopied."""
        tool = ReadFile()
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

        result = await executor.execute(call)

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

        await executor.execute(call)

        assert params[CALL_DESCRIPTION_PARAM] == "send a greeting"
        assert call.parameters is params
