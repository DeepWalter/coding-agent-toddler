"""File-system tools — ReadFile, WriteFile, EditFile."""

from __future__ import annotations

from pathlib import Path

from toddler.tools.base import (
    BaseTool,
    Permission,
    ToolResult,
    display_path,
)
from toddler.tools.registry import TOOL_CATALOG

# ---------------------------------------------------------------------------
# ReadFile
# ---------------------------------------------------------------------------


@TOOL_CATALOG.register()
class ReadFile(BaseTool):
    """Read a file from the local filesystem and return its contents.

    Supports optional ``offset`` and ``limit`` for reading long files.
    """

    name = "read_file"
    description = (
        "Read a file from disk. "
        "Use ``offset`` and ``limit`` (especially handy for long files), "
        "but it's recommended to read the whole file by not providing these "
        "parameters."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed).",  # noqa: E501
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
            },
        },
        "required": ["file_path"],
    }

    #: The file it names is what the call does — no call-description param.
    require_description_parameter = False

    #: The file it reads is the whole of its reach — the gate places it.
    path_params = ("file_path",)

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(
        self,
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        path = Path(file_path).expanduser().resolve()
        # What the file is called to whoever asked for it: the resolved
        # path is what gets opened, and on macOS it can be spelled quite
        # differently (``/tmp`` is ``/private/tmp``).
        named = display_path(file_path)
        if not path.exists():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"File not found: {named}",
            )
        if path.is_dir():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Path is a directory, not a file: {named}",
            )

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Cannot read {named} — not a UTF-8 text file.",
            )

        lines = text.splitlines(keepends=True)

        # Apply offset / limit slicing
        start = (offset - 1) if offset is not None else 0
        end = start + limit if limit is not None else len(lines)

        sliced = lines[start:end]

        # Build output with line numbers
        newline = "\n"
        out_lines: list[str] = []
        for i, line in enumerate(sliced, start=start + 1):
            out_lines.append(f"{i}\t{line.rstrip(newline)}")

        output = "\n".join(out_lines)
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=output,
            metadata={"path": str(path), "total_lines": len(lines)},
        )

    def summarize_call(self, **kwargs) -> str:
        path = kwargs.get("file_path", "?")
        return f"read_file({Path(path).name})"

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The call is about a file, so the card asks about that."""
        return "Allow read this file?"


# ---------------------------------------------------------------------------
# WriteFile
# ---------------------------------------------------------------------------


@TOOL_CATALOG.register()
class WriteFile(BaseTool):
    """Write (create or overwrite) a file on disk.

    Creates parent directories if they don't exist.
    """

    name = "write_file"
    description = (
        "Write a file to the local filesystem. "
        "Creates parent directories automatically. "
        "Overwrites the existing file if there is one at the provided path."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    #: The file it names is what the call does — no call-description param.
    require_description_parameter = False

    #: The file it writes is the whole of its reach — the gate places it.
    path_params = ("file_path",)

    @property
    def permission(self) -> Permission:
        return Permission.WRITE

    async def execute(self, file_path: str, content: str) -> ToolResult:
        path = Path(file_path).expanduser().resolve()
        named = display_path(file_path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Failed to write {named}: {exc}",
            )

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Wrote {len(content)} bytes to {named}.",
            metadata={"path": str(path), "bytes": len(content)},
        )

    def summarize_call(self, **kwargs) -> str:
        path = kwargs.get("file_path", "?")
        return f"write_file({Path(path).name})"

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The call is about a file, so the card asks about that."""
        return "Allow write this file?"


# ---------------------------------------------------------------------------
# EditFile
# ---------------------------------------------------------------------------


@TOOL_CATALOG.register()
class EditFile(BaseTool):
    """Perform exact string replacement in an existing file.

    Validates that ``old_string`` appears **exactly once** — the edit is
    rejected with a clear error message otherwise.  This constraint prevents
    ambiguous edits and encourages the LLM to provide enough context for a
    unique match.
    """

    name = "edit_file"
    description = (
        "Performs exact string replacement in a file. "
        "``old_string`` must match the file exactly, including whitespace "
        "and indentation, and must appear exactly once in the file — "
        "the edit fails otherwise. "
        "Set ``replace_all`` to ``true`` to replace every occurrence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to modify.",
            },
            "old_string": {
                "type": "string",
                "description": "The text to replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "Replace all occurrences of old_string (default: false)."
                ),
                "default": False,
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    #: The file it names is what the call does — no call-description param.
    require_description_parameter = False

    #: The file it rewrites is the whole of its reach — the gate places it.
    path_params = ("file_path",)

    @property
    def permission(self) -> Permission:
        return Permission.WRITE

    async def execute(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        path = Path(file_path).expanduser().resolve()
        named = display_path(file_path)

        if not path.exists():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"File not found: {named}",
            )
        if path.is_dir():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Path is a directory, not a file: {named}",
            )

        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Cannot edit {named} — not a UTF-8 text file.",
            )

        count = original.count(old_string)

        if count == 0:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"old_string not found in {named}. "
                    "The text to replace must match exactly, including "
                    "whitespace and indentation."
                ),
            )

        if not replace_all and count > 1:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"old_string appears {count} times in {named}. "
                    "Add more context to make it unique, or set "
                    "``replace_all=true`` to replace every occurrence."
                ),
            )

        modified = original.replace(old_string, new_string)
        # Where the replacement starts, in the file as this call read it.
        # The web card uses it to reveal the exact occurrence after the edit:
        # new_string may already occur elsewhere, and a text search would
        # land on the wrong one.  Line is 1-based, column 0-based.
        offset = original.find(old_string)
        line_start = original.count("\n", 0, offset) + 1
        column_start = offset - (original.rfind("\n", 0, offset) + 1)

        try:
            path.write_text(modified, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Failed to write {named}: {exc}",
            )

        replaced = count if replace_all else 1
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Replaced {replaced} occurrence(s) in {named}.",
            metadata={
                "path": str(path),
                "occurrences_replaced": replaced,
                "replace_all": replace_all,
                "line_start": line_start,
                "column_start": column_start,
            },
        )

    def summarize_call(self, **kwargs) -> str:
        path = kwargs.get("file_path", "?")
        return f"edit_file({Path(path).name})"

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The call is about a file, so the card asks about that."""
        return "Allow edit this file?"
