"""Base tool abstractions — Permission, ToolResult, BaseTool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Permission(Enum):
    """Permission tier for a tool.

    Used by the ``ToolExecutor`` to decide whether auto-approval is allowed
    or user confirmation is needed.
    """

    READ = "read"               # side-effect-free reads (auto-approve)
    WRITE = "write"             # file mutations (confirm by default)
    SHELL_SAFE = "shell_safe"   # safe shell commands — ls, git status, etc.
    SHELL_DANGEROUS = "shell_dangerous"  # rm, sudo, curl, etc. (always confirm)  # noqa: E501


# ---------------------------------------------------------------------------
# Tool call / result
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A parsed tool-call request from the LLM."""

    tool_id: str
    tool_name: str
    parameters: dict


@dataclass
class ToolResult:
    """The outcome of executing a tool."""

    tool_id: str
    tool_name: str
    success: bool
    output: str
    checkpoint_id: str | None = None   # set when a checkpoint was created
    error: str | None = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# BaseTool ABC
# ---------------------------------------------------------------------------


class BaseTool(ABC):
    """Abstract base for every tool in the agent's toolbox.

    Subclasses must provide:
    - ``name`` (str)
    - ``description`` (str)
    - ``parameters`` (JSON Schema dict)
    - ``execute(**kwargs)`` → ToolResult

    They MAY override ``permission`` (default: READ).
    """

    name: str
    description: str
    parameters: dict  # JSON Schema for the tool's input

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with the given keyword arguments.

        ``kwargs`` is the deserialized JSON Schema properties — e.g.
        ``path="/foo/bar.txt"`` for a ReadFile tool.
        """
        ...

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    @property
    def permission(self) -> Permission:
        """Default permission is READ; override for mutating tools."""
        return Permission.READ

    def get_permission(self, **kwargs) -> Permission:  # noqa: ARG002
        """Resolve the effective permission for a specific invocation.

        Most tools return a static ``permission``, but tools like ``Shell``
        that need to inspect the parameters (e.g. the command string) can
        override this method to return a dynamic permission level.

        The executor always calls this method (not the property directly)
        so that dynamic classification works transparently.
        """
        return self.permission

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_api_schema(self) -> dict:
        """Return the OpenAI tool-compatible schema dict.

        Example::

            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from disk",
                    "parameters": {"type": "object", "properties": {...}}
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def summarize_call(self, **kwargs) -> str:
        """One-line summary of the call for display / logging.

        Default: ``"tool_name(key=val, ...)"``.
        """
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{self.name}({args})"
