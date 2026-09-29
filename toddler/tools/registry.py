"""ToolRegistry — register, look up, and list available tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from toddler.tools.base import BaseTool

__all__ = ["TOOL_CATALOG", "ToolRegistry"]

#: A tool class — what the ``@register()`` decorator receives and returns.
_ToolClass = TypeVar("_ToolClass", bound=type[BaseTool])


class ToolRegistry:
    """A named collection of tools that the agent can use.

    The registry is the single source of truth for which tools are available.
    It provides lookup by name and can serialize all registered tools into
    the OpenAI ``tools`` API format.

    Tools are added either as instances or from the class definition itself,
    with :meth:`register` doubling as a decorator::

        registry = ToolRegistry(name="session")
        registry.register(ReadFile())

        @registry.register()
        class Shell(BaseTool):
            ...

        tool = registry.get("read_file")
        schemas = registry.to_api_schemas()
    """

    def __init__(self, name: str = "tools") -> None:
        """Create a registry.

        Parameters
        ----------
        name:
            What this registry is called in its repr and in error messages.
            Worth setting once more than one exists — the definition-time
            catalog, a session's own — so a duplicate-registration error
            says which one already holds the tool.
        """
        self.name = name
        self._tools: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @overload
    def register(self, tool: BaseTool) -> None: ...

    @overload
    def register(self) -> Callable[[_ToolClass], _ToolClass]: ...

    def register(
        self, tool: BaseTool | None = None
    ) -> Callable[[_ToolClass], _ToolClass] | None:
        """Add a tool instance to the registry, or return a class decorator.

        Called with a tool, it registers that instance.  Called with no
        argument, it returns a decorator that instantiates the decorated
        class, registers the instance, and hands the class back unchanged —
        so a tool's definition and its registration are one statement::

            @registry.register()
            class Shell(BaseTool):
                ...

        Raises ``ValueError`` if a tool with the same name is already
        registered, and ``TypeError`` if handed a class rather than an
        instance (``@register()`` is the way to register the class).
        """
        if tool is None:
            def decorator(tool_cls: _ToolClass) -> _ToolClass:
                # No-argument construction is the decorator's contract —
                # a tool that needs state (``PlanUpdateTool`` and its
                # ``PlanState``) is registered explicitly instead.
                self._do_register(tool_cls())
                return tool_cls

            return decorator
        if isinstance(tool, type):
            raise TypeError(
                f"register() takes a tool instance, not the class "
                f"'{tool.__name__}'. Decorate the class with "
                f"'@register()' to register it."
            )
        self._do_register(tool)

    def _do_register(self, tool: BaseTool) -> None:
        """Add a tool instance to the registry.

        Raises ``ValueError`` if a tool with the same name is already
        registered.
        """
        if tool.name in self._tools:
            raise ValueError(
                f"Tool '{tool.name}' is already registered in "
                f"'{self.name}'. Deregister it first, or use a different "
                f"name."
            )
        self._tools[tool.name] = tool

    def deregister(self, name: str) -> BaseTool | None:
        """Remove a tool by name and return it (or ``None`` if not found)."""
        return self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        """Look up a tool by name; returns ``None`` when not found."""
        return self._tools.get(name)

    # ------------------------------------------------------------------
    # Bulk access
    # ------------------------------------------------------------------

    def list_names(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return sorted(self._tools)

    def list_all(self) -> list[BaseTool]:
        """Return every registered tool instance."""
        return list(self._tools.values())

    def to_api_schemas(self) -> list[dict]:
        """Return the OpenAI-compatible tool schema list for every tool.

        This is what gets passed as the ``tools`` parameter to the chat
        completions API.
        """
        return [tool.to_api_schema() for tool in self._tools.values()]

    def __repr__(self) -> str:
        return f"ToolRegistry(name={self.name!r}, tools={len(self._tools)})"

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ---------------------------------------------------------------------------
# Definition-time catalog
# ---------------------------------------------------------------------------

#: The registry the tools decorate into as their modules are imported
#: (``@TOOL_CATALOG.register()``) — every tool in the codebase, whether
#: a session starts with it or not (see
#: :attr:`~toddler.tools.base.BaseTool.in_default_registry`).
#:
#: A catalog of definitions, not a registry to run with: a session gets its
#: own, built from here by :func:`~toddler.tools.create_default_registry`,
#: so the tools that come and go at runtime — ``plan_update`` registered for
#: a plan phase alone, test doubles — never touch the definitions.
TOOL_CATALOG = ToolRegistry(name="catalog")
