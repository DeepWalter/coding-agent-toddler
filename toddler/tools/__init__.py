"""Tool system — base abstractions, registry, executor, and built-in tools.

Importing the tool modules below is load-bearing: each tool class registers
itself with ``@TOOL_CATALOG.register()`` at definition time, which is
what makes it available to :func:`create_default_registry`.  The plan tool
is imported for the same reason, even though it opts out of the default set.
"""

from toddler.tools.base import BaseTool, Permission, ToolCall, ToolResult
from toddler.tools.executor import (
    CheckpointCallback,
    ConfirmCallback,
    ToolExecutor,
)
from toddler.tools.files import EditFile, ReadFile, WriteFile
from toddler.tools.git import GitBranch, GitCommit, GitDiff, GitLog, GitStatus
from toddler.tools.plan import PlanState, PlanUpdateTool
from toddler.tools.registry import TOOL_CATALOG, ToolRegistry
from toddler.tools.search import Glob, Grep
from toddler.tools.shell import Shell

__all__ = [
    # Base
    "BaseTool",
    "Permission",
    "ToolCall",
    "ToolResult",
    # Registry
    "ToolRegistry",
    "TOOL_CATALOG",
    "HIDDEN_TOOL_NAMES",
    # Executor
    "ToolExecutor",
    "CheckpointCallback",
    "ConfirmCallback",
    # File tools
    "ReadFile",
    "WriteFile",
    "EditFile",
    # Search tools
    "Grep",
    "Glob",
    # Shell
    "Shell",
    # Plan
    "PlanState",
    "PlanUpdateTool",
    # Git tools
    "GitStatus",
    "GitDiff",
    "GitLog",
    "GitCommit",
    "GitBranch",
    # Factory
    "create_default_registry",
]


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

#: Tool names no front-end draws a call row for: what the tool does reaches
#: the user as an event or a print of its own, so a row would say it a
#: second time.
#:
#: Derived from :attr:`~toddler.tools.base.BaseTool.visible` after the
#: imports above have registered every tool — the catalog is empty while
#: ``registry`` is being imported, which is why this constant waits until
#: the whole package is built.
HIDDEN_TOOL_NAMES = frozenset(
    tool.name for tool in TOOL_CATALOG.list_all() if not tool.visible
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_default_registry() -> ToolRegistry:
    """Return a :class:`ToolRegistry` holding every default tool.

    The tool modules above register themselves at import time (each class
    decorates itself with ``@TOOL_CATALOG.register()``), so this factory
    has no list to keep in sync — define a tool, import its module, and it
    is here unless it opts out with
    :attr:`~toddler.tools.base.BaseTool.in_default_registry`.

    Every call builds new instances: the caller owns the registry it gets,
    free to add ``plan_update`` for a plan phase or drop a tool, without
    reaching the definitions or any other session.
    """
    registry = ToolRegistry(name="default")
    for tool in TOOL_CATALOG.list_all():
        if tool.in_default_registry:
            registry.register(type(tool)())
    return registry
