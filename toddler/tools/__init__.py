"""Tool system — base abstractions, registry, executor, and built-in tools."""

from toddler.tools.base import BaseTool, Permission, ToolCall, ToolResult
from toddler.tools.executor import (
    CheckpointCallback,
    ConfirmCallback,
    ToolExecutor,
)
from toddler.tools.files import EditFile, ReadFile, WriteFile
from toddler.tools.git import GitBranch, GitCommit, GitDiff, GitLog, GitStatus
from toddler.tools.registry import ToolRegistry
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
    # Git tools
    "GitStatus",
    "GitDiff",
    "GitLog",
    "GitCommit",
    "GitBranch",
]
