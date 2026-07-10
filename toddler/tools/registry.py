"""ToolRegistry — register, look up, and list available tools."""

from __future__ import annotations

from toddler.tools.base import BaseTool


class ToolRegistry:
    """A named collection of tools that the agent can use.

    The registry is the single source of truth for which tools are available.
    It provides lookup by name and can serialize all registered tools into
    the OpenAI ``tools`` API format.

    Usage::

        registry = ToolRegistry()
        registry.register(ReadFile())
        tool = registry.get("read_file")
        schemas = registry.to_api_schemas()
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, tool: BaseTool) -> None:
        """Add a tool instance to the registry.

        Raises ``ValueError`` if a tool with the same name is already
        registered.
        """
        if tool.name in self._tools:
            raise ValueError(
                f"Tool '{tool.name}' is already registered. "
                f"Deregister it first, or use a different name."
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

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
