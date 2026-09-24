"""Search tools — Grep (content search) and Glob (filename search)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from toddler.tools.base import BaseTool, Permission, ToolResult

# Directories never worth searching — build artifacts, caches, and vendored
# dependency trees.  Grep passes each one to ``--exclude-dir``, so grep never
# walks them; the pure-Python paths (Grep's fallback, Glob) filter on the
# parts below the search root instead — see :func:`_is_hidden_or_ignored`.
_IGNORED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".eggs",
})

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
        "Matches are capped at ``max_results`` in total (default: 100) — "
        "not per file. Build, cache, and vendored directories are skipped."
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
                "description": "Maximum number of matches to return in total (default: 100).",  # noqa: E501
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

        return _match_result(
            tool_name=self.name,
            output=output,
            max_results=max_results,
            pattern=pattern,
            path=str(search_path),
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

        # Every match is collected before capping, so the "more matches"
        # count in the output is exact — the subprocess path reads grep's
        # whole output for the same reason, and the cap lives in one place.
        results: list[str] = []
        for file_path in search_path.rglob("*"):
            if not file_path.is_file():
                continue
            if _is_hidden_or_ignored(file_path, search_path):
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

        return _match_result(
            tool_name=self.name,
            output="\n".join(results),
            max_results=max_results,
            pattern=pattern,
            path=str(search_path),
            fallback=True,
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
    """Build the grep command argument list.

    ``--exclude-dir`` keeps grep out of build and vendored trees rather
    than reading their output only to discard it.  ``-m`` stops grep
    reading a pathological file early, but it bounds matches *per file* —
    it is not the result cap, which :func:`_match_result` applies to the
    whole output.  ``-e`` ends the options so a pattern starting with
    ``-`` is read as a pattern.
    """
    cmd: list[str] = ["grep", "-rn", "--color=never"]
    if ignore_case:
        cmd.append("-i")
    if include:
        cmd.extend(["--include", include])
    for ignored in sorted(_IGNORED_DIRS):
        cmd.extend(["--exclude-dir", ignored])
    cmd.extend(["-m", str(max_results)])
    cmd.extend(["-e", pattern, path])
    return cmd


def _match_result(
    *,
    tool_name: str,
    output: str,
    max_results: int,
    pattern: str,
    path: str,
    fallback: bool = False,
) -> ToolResult:
    """Cap *output* to *max_results* lines and build the match result.

    This is the cap that holds over the whole search: ``grep -m`` bounds
    matches per file, so a pattern matching in many files used to return
    ``max_results`` lines for each of them.  ``match_count`` reports what
    was found, not what was kept — Glob reads the same way — and the text
    says how many lines were dropped.  Empty output is not an error: it
    reads as "no matches", which is what the caller wants to tell the
    model.
    """
    lines = output.splitlines()
    truncated = len(lines) > max_results
    text = "\n".join(lines[:max_results])
    if truncated:
        text += f"\n\n... ({len(lines) - max_results} more matches not shown)"

    metadata = {
        "match_count": len(lines),
        "pattern": pattern,
        "path": path,
        "truncated": truncated,
    }
    if fallback:
        metadata["fallback"] = True

    return ToolResult(
        tool_id="",
        tool_name=tool_name,
        success=True,
        output=text or f"No matches found for pattern: {pattern}",
        metadata=metadata,
    )


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
                    "Glob pattern, e.g. ``'**/*.py'`` or ``'src/**/test_*.py'``."  # noqa: E501
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
            p for p in base.glob(pattern) if not _is_hidden_or_ignored(p, base)
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
            output += f"\n\n... ({len(matches) - max_results} more results not shown)"  # noqa: E501

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


def _is_hidden_or_ignored(path: Path, base: Path) -> bool:
    """Skip *path* when it is hidden or inside an ignored directory.

    Only the parts *below* *base* are judged.  The base is a directory the
    caller named, so it is a deliberate choice — judging it too made every
    result vanish when the base itself sat under an ignored name, which a
    repo at ``~/build/proj`` or a worktree under ``.claude/`` would each
    hit.  A path that escapes the base (a ``..`` pattern) is not a result
    of searching under it, and is skipped.
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return True

    return any(
        part.startswith(".")
        or part in _IGNORED_DIRS
        or part.endswith(".egg-info")
        for part in parts
    )
