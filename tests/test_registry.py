"""ToolRegistry tests — instance registration, the ``@register()``
decorator, and the definition-time catalog the built-ins decorate into."""

from __future__ import annotations

import pytest

from toddler.tools import HIDDEN_TOOL_NAMES, create_default_registry
from toddler.tools.base import BaseTool, ToolResult
from toddler.tools.plan import PlanUpdateTool
from toddler.tools.registry import TOOL_CATALOG, ToolRegistry

#: Every tool :func:`create_default_registry` is expected to hand a session.
#: Spelled out rather than derived from the catalog: adding a tool without
#: registering it (or removing one that is still registered) has to fail
#: here, or the factory's guarantee is only as good as its own source.
BUILTIN_NAMES = {
    "read_file", "write_file", "edit_file",
    "shell",
    "grep", "glob",
    "git_status", "git_diff", "git_log", "git_commit", "git_branch",
}

#: What registers itself at import time — the default tools plus the ones a
#: session arms for a phase of its own.
CATALOG_NAMES = BUILTIN_NAMES | {"plan_update"}


class EchoTool(BaseTool):
    """Minimal concrete tool — parameterless, so it is decorator-ready."""

    name = "echo"
    description = "Echo the given text back."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=str(kwargs.get("text", "")),
        )


# ---------------------------------------------------------------------------
# Instance registration
# ---------------------------------------------------------------------------


class TestInstanceRegistration:

    def test_registering_an_instance_makes_it_lookupable(self):
        registry = ToolRegistry()
        tool = EchoTool()

        registry.register(tool)

        assert registry.get("echo") is tool
        assert "echo" in registry
        assert len(registry) == 1
        assert registry.list_names() == ["echo"]
        assert registry.list_all() == [tool]

    def test_a_duplicate_name_is_rejected(self):
        registry = ToolRegistry()
        registry.register(EchoTool())

        with pytest.raises(ValueError, match="already registered"):
            registry.register(EchoTool())

    def test_a_duplicate_names_the_registry_holding_the_tool(self):
        registry = ToolRegistry(name="catalog")
        registry.register(EchoTool())

        with pytest.raises(ValueError, match="in 'catalog'"):
            registry.register(EchoTool())

    def test_the_name_defaults_to_tools_and_is_settable(self):
        assert ToolRegistry().name == "tools"
        assert ToolRegistry(name="session").name == "session"

    def test_the_repr_carries_the_name_and_the_count(self):
        registry = ToolRegistry(name="session")
        registry.register(EchoTool())

        assert repr(registry) == "ToolRegistry(name='session', tools=1)"

    def test_deregistering_frees_the_name(self):
        registry = ToolRegistry()
        tool = EchoTool()
        registry.register(tool)

        assert registry.deregister("echo") is tool
        assert "echo" not in registry
        assert registry.deregister("echo") is None

    def test_a_class_is_rejected_with_a_pointer_to_the_decorator(self):
        """Passing a class would register something no instance backs."""
        registry = ToolRegistry()

        with pytest.raises(TypeError, match="not the class"):
            registry.register(EchoTool)


# ---------------------------------------------------------------------------
# Decorator registration
# ---------------------------------------------------------------------------


class TestRegisterDecorator:

    def test_the_decorated_class_is_returned_unchanged(self):
        registry = ToolRegistry()

        @registry.register()
        class Local(EchoTool):
            name = "local"

        # Still the class as written — and what got registered is an
        # instance of it.
        assert issubclass(Local, BaseTool)
        assert Local.__module__ == __name__
        assert type(registry.get("local")) is Local

    def test_the_registered_tool_is_an_instance(self):
        registry = ToolRegistry()

        @registry.register()
        class Local(EchoTool):
            name = "local"

        tool = registry.get("local")
        assert isinstance(tool, Local)
        assert tool is not Local

    def test_a_decorated_tool_reaches_the_api_schemas(self):
        registry = ToolRegistry()

        @registry.register()
        class Local(EchoTool):
            name = "local"

        names = [s["function"]["name"] for s in registry.to_api_schemas()]
        assert names == ["local"]

    def test_a_duplicate_name_is_rejected_at_definition_time(self):
        registry = ToolRegistry()
        registry.register(EchoTool())

        with pytest.raises(ValueError, match="already registered"):

            @registry.register()
            class AlsoEcho(EchoTool):
                pass

    def test_a_tool_needing_arguments_cannot_be_decorated(self):
        """The decorator constructs with no arguments by design — stateful
        tools are registered explicitly instead."""
        registry = ToolRegistry()

        with pytest.raises(TypeError):

            @registry.register()
            class Configured(EchoTool):
                name = "configured"

                def __init__(self, required_setting: str) -> None:
                    self._setting = required_setting


# ---------------------------------------------------------------------------
# The built-in catalog
# ---------------------------------------------------------------------------


class TestHiddenToolNames:
    """Which calls the front-ends draw a row for is a tool's own
    declaration, read off the catalog — not a name list either of them
    carries."""

    def test_the_plan_tool_declares_itself_invisible(self):
        """Its statuses arrive as plan step updates, so a row for the call
        itself would repeat them."""
        assert PlanUpdateTool.visible is False
        assert "plan_update" in HIDDEN_TOOL_NAMES

    def test_a_tool_that_declares_nothing_is_drawn(self):
        assert "shell" not in HIDDEN_TOOL_NAMES
        assert "read_file" not in HIDDEN_TOOL_NAMES

    def test_the_set_cannot_drift_from_the_declarations(self):
        """A name listed by hand, or a declaration the set ignores, both
        fail."""
        declared = {
            tool.name for tool in TOOL_CATALOG.list_all() if not tool.visible
        }
        assert declared == HIDDEN_TOOL_NAMES

    def test_hidden_is_not_the_same_question_as_not_default(self):
        """Two declarations, two questions: what a session is handed, and
        what the front-ends draw.  ``plan_update`` answers no to both, but
        nothing ties the two together."""
        assert set(HIDDEN_TOOL_NAMES) == {"plan_update"}
        assert set(create_default_registry().list_names()) == BUILTIN_NAMES


class TestDefaultRegistry:

    def test_the_catalog_names_itself(self):
        assert TOOL_CATALOG.name == "catalog"

    def test_the_factory_names_what_it_builds(self):
        assert create_default_registry().name == "default"

    def test_the_catalog_holds_every_tool_defined(self):
        """Session-only tools included — the catalog is an inventory, not
        the set a session gets."""
        assert set(TOOL_CATALOG.list_names()) == CATALOG_NAMES

    def test_the_factory_hands_out_every_default_tool(self):
        assert set(create_default_registry().list_names()) == BUILTIN_NAMES

    def test_a_session_only_tool_is_filtered_out(self):
        assert PlanUpdateTool.in_default_registry is False
        assert "plan_update" in TOOL_CATALOG
        assert "plan_update" not in create_default_registry()

    def test_the_opt_out_is_what_does_the_filtering(self):
        """The mechanism itself, not just the one tool using it today."""

        @TOOL_CATALOG.register()
        class SessionTool(EchoTool):
            name = "session_tool"
            in_default_registry = False

        try:
            assert "session_tool" in TOOL_CATALOG
            assert "session_tool" not in create_default_registry()
        finally:
            TOOL_CATALOG.deregister("session_tool")

    def test_each_call_builds_its_own_instances(self):
        first = create_default_registry()
        second = create_default_registry()

        assert first is not second
        assert first.get("shell") is not second.get("shell")

    def test_mutating_a_session_registry_leaves_the_catalog_intact(self):
        """``plan_update`` churn and test doubles must not reach the
        definitions the next session is built from."""
        registry = create_default_registry()

        registry.deregister("shell")
        registry.register(EchoTool())

        assert create_default_registry() is not TOOL_CATALOG
        assert set(TOOL_CATALOG.list_names()) == CATALOG_NAMES
