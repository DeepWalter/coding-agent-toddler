"""Search tools — Grep (content search) and Glob (filename search)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from toddler.tools.base import BaseTool, Permission, ToolResult

# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


class Grep(BaseTool):
    """Search file contents using ``grep -rn`` with configurable options.

    Uses system ``grep`` for speed.  Falls back to a pure-Python scan if
    ``grep`` is not available on the system.
    """

    name = "grep"
    description = (
        "Search for a regex pattern in files under a directory. "
        "Returns matching lines with file path, line number, and content. "
        "Use ``include`` to filter by file extension or glob pattern. "
        "Results are capped at ``max_results`` (default: 100)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory or file to search in. Defaults to the "
                    "current working directory."
                ),
            },
            "include": {
                "type": "string",
                "description": (
                    "Optional file pattern filter passed to grep's "
                    "``--include`` flag, e.g. ``'*.py'`` or ``'*.js'``."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matches to return (default: 100).",
                "default": 100,
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (``-i`` flag).",
                "default": False,
            },
        },
        "required": ["pattern"],
    }

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        include: str | None = None,
        max_results: int = 100,
        ignore_case: bool = False,
    ) -> ToolResult:
        search_path = Path(path).expanduser().resolve()
        if not search_path.exists():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Path not found: {search_path}",
            )

        cmd = _build_grep_cmd(
            pattern=pattern,
            path=str(search_path),
            include=include,
            max_results=max_results,
            ignore_case=ignore_case,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except TimeoutError:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error="Grep timed out after 30s.",
            )
        except FileNotFoundError:
            # grep not available → pure-Python fallback
            return await self._fallback_search(
                pattern, search_path, include, max_results, ignore_case
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        err_output = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode not in (0, 1):
            # returncode 1 = no matches (not an error); >1 = real error
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output=output,
                error=err_output or f"grep exited with code {proc.returncode}",
            )

        if not output:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=True,
                output=f"No matches found for pattern: {pattern}",
                metadata={"match_count": 0, "pattern": pattern},
            )

        # Count matches
        match_count = output.count("\n") + 1 if output else 0
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=output,
            metadata={
                "match_count": match_count,
                "pattern": pattern,
                "path": str(search_path),
            },
        )

    async def _fallback_search(
        self,
        pattern: str,
        search_path: Path,
        include: str | None,
        max_results: int,
        ignore_case: bool,
    ) -> ToolResult:
        """Pure-Python fallback when system grep is unavailable."""
        import fnmatch
        import re

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Invalid regex pattern: {exc}",
            )

        results: list[str] = []
        for file_path in search_path.rglob("*"):
            if not file_path.is_file():
                continue
            if include and not fnmatch.fnmatch(file_path.name, include):
                continue
            # Skip binary-looking files
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{file_path}:{line_no}:{line}")
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break

        if not results:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=True,
                output=f"No matches found for pattern: {pattern}",
                metadata={"match_count": 0, "pattern": pattern},
            )

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output="\n".join(results),
            metadata={
                "match_count": len(results),
                "pattern": pattern,
                "path": str(search_path),
                "fallback": True,
            },
        )

    def summarize_call(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "?")
        return f"grep({pattern!r})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_grep_cmd(
    pattern: str,
    path: str,
    include: str | None,
    max_results: int,
    ignore_case: bool,
) -> list[str]:
    """Build the grep command argument list."""
    cmd: list[str] = ["grep", "-rn", "--color=never"]
    if ignore_case:
        cmd.append("-i")
    if include:
        cmd.extend(["--include", include])
    cmd.extend(["-m", str(max_results)])
    cmd.extend([pattern, path])
    return cmd


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


class Glob(BaseTool):
    """Find files matching a glob pattern using ``pathlib.glob``.

    Supports recursive ``**`` patterns and filtering.
    """

    name = "glob"
    description = (
        "Find files matching a glob pattern. "
        "Uses Python's ``pathlib.glob`` — supports ``**`` for recursive "
        "matching.  Set ``max_results`` to limit output (default: 200)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern, e.g. ``'**/*.py'`` or ``'src/**/test_*.py'``."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Base directory for the search. Defaults to the "
                    "current working directory."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default: 200).",
                "default": 200,
            },
        },
        "required": ["pattern"],
    }

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        max_results: int = 200,
    ) -> ToolResult:
        base = Path(path).expanduser().resolve()
        if not base.is_dir():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Directory not found: {base}",
            )

        matches = sorted(
            p for p in base.glob(pattern) if not _is_hidden_or_ignored(p)
        )

        if not matches:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=True,
                output=f"No files matching '{pattern}' in {base}",
                metadata={"match_count": 0, "pattern": pattern},
            )

        # Convert to relative paths for cleaner output
        result_lines: list[str] = []
        for p in matches[:max_results]:
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p
            suffix = "/" if p.is_dir() else ""
            result_lines.append(f"{rel}{suffix}")

        truncated = len(matches) > max_results
        output = "\n".join(result_lines)
        if truncated:
            output += f"\n\n... ({len(matches) - max_results} more results not shown)"

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=output,
            metadata={
                "match_count": len(matches),
                "pattern": pattern,
                "path": str(base),
                "truncated": truncated,
            },
        )

    def summarize_call(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "?")
        return f"glob({pattern!r})"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _is_hidden_or_ignored(path: Path) -> bool:
    """Skip paths that are hidden or in common ignore directories."""
    # Skip hidden files/dirs (starting with .)
    parts = path.parts
    for part in parts:
        if part.startswith(".") and part not in (".", ".."):
            return True

    # Skip common virtual env / cache dirs
    ignored_dirs = {
        ".venv", "venv", "__pycache__", ".git", "node_modules",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        "dist", "build", ".eggs",
    }
    return any(
        part in ignored_dirs or part.endswith(".egg-info")
        for part in parts
    )
