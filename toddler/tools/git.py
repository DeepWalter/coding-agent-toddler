"""Git tools — status, diff, log, commit, and branch operations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from toddler.tools.base import BaseTool, Permission, ToolResult

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _git(
    *args: str,
    cwd: str | None = None,
    timeout: int = 30,
) -> tuple[str, str, int]:
    """Run a git command and return ``(stdout, stderr, returncode)``."""
    cmd = ["git", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ},
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(
            f"git {' '.join(args)} timed out"
        ) from exc
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, proc.returncode or 0


def _resolve_repo_path(repo_path: str | None) -> str:
    """Resolve the repo path, defaulting to cwd."""
    return str(Path(repo_path).expanduser().resolve()) if repo_path else os.getcwd()


def _git_error(tool_name: str, stderr: str, returncode: int) -> ToolResult:
    """Build a standard error result for git failures."""
    return ToolResult(
        tool_id="",
        tool_name=tool_name,
        success=False,
        output="",
        error=stderr.strip() or f"git exited with code {returncode}",
    )


# ---------------------------------------------------------------------------
# GitStatus
# ---------------------------------------------------------------------------


class GitStatus(BaseTool):
    """Show the working tree status."""

    name = "git_status"
    description = (
        "Show the working tree status — staged, unstaged, and untracked "
        "files.  Use ``repo_path`` to target a specific repo (defaults to "
        "current directory)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Optional path to the git repository.",
            },
        },
    }

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(self, repo_path: str | None = None) -> ToolResult:
        cwd = _resolve_repo_path(repo_path)
        try:
            stdout, stderr, rc = await _git("status", "--short", cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git status timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip() or "Working tree clean.",
            metadata={"repo_path": cwd},
        )

    def summarize_call(self, **kwargs) -> str:
        return "git_status()"


# ---------------------------------------------------------------------------
# GitDiff
# ---------------------------------------------------------------------------


class GitDiff(BaseTool):
    """Show changes between commits, the index, or the working tree."""

    name = "git_diff"
    description = (
        "Show changes in the working tree, index, or between commits. "
        "Use ``staged`` to show staged changes only. "
        "Use ``target`` for a specific file or directory. "
        "Use ``commit`` to diff against a specific ref (e.g. ``HEAD~1``)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Optional path to the git repository.",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged changes only (``--staged``).",
                "default": False,
            },
            "target": {
                "type": "string",
                "description": "Optional file or directory to restrict the diff to.",
            },
            "commit": {
                "type": "string",
                "description": (
                    "Optional commit-ish to diff against "
                    "(e.g. ``HEAD~1``, ``main``)."
                ),
            },
        },
    }

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(
        self,
        repo_path: str | None = None,
        staged: bool = False,
        target: str | None = None,
        commit: str | None = None,
    ) -> ToolResult:
        cwd = _resolve_repo_path(repo_path)
        args: list[str] = ["diff", "--no-color"]
        if staged:
            args.append("--staged")
        if commit:
            args.append(commit)
        if target:
            args.extend(["--", target])

        try:
            stdout, stderr, rc = await _git(*args, cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git diff timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip() or "(no changes)",
            metadata={"repo_path": cwd, "staged": staged},
        )

    def summarize_call(self, **kwargs) -> str:
        staged = " --staged" if kwargs.get("staged") else ""
        return f"git_diff(){staged}"


# ---------------------------------------------------------------------------
# GitLog
# ---------------------------------------------------------------------------


class GitLog(BaseTool):
    """Show the commit history."""

    name = "git_log"
    description = (
        "Show commit history. "
        "Use ``max_count`` to limit the number of entries (default: 20). "
        "Use ``oneline`` for compact output. "
        "Use ``path`` to filter commits touching a specific file/directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Optional path to the git repository.",
            },
            "max_count": {
                "type": "integer",
                "description": "Maximum number of commits to show (default: 20).",
                "default": 20,
            },
            "oneline": {
                "type": "boolean",
                "description": "Use compact one-line-per-commit format.",
                "default": False,
            },
            "path": {
                "type": "string",
                "description": "Optional file/directory to filter commits by.",
            },
        },
    }

    @property
    def permission(self) -> Permission:
        return Permission.READ

    async def execute(
        self,
        repo_path: str | None = None,
        max_count: int = 20,
        oneline: bool = False,
        path: str | None = None,
    ) -> ToolResult:
        cwd = _resolve_repo_path(repo_path)
        args: list[str] = ["log", f"-n{max_count}", "--no-color"]
        if oneline:
            args.append("--oneline")
        else:
            args.append("--format=%h %ad %an: %s")
            args.append("--date=short")
        if path:
            args.extend(["--", path])

        try:
            stdout, stderr, rc = await _git(*args, cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git log timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip() or "(no commits)",
            metadata={"repo_path": cwd, "max_count": max_count},
        )

    def summarize_call(self, **kwargs) -> str:
        n = kwargs.get("max_count", 20)
        return f"git_log(n={n})"


# ---------------------------------------------------------------------------
# GitCommit
# ---------------------------------------------------------------------------


class GitCommit(BaseTool):
    """Create a new commit with staged changes."""

    name = "git_commit"
    description = (
        "Create a new commit with all currently staged changes. "
        "Use ``message`` for the commit message. "
        "If nothing is staged the tool will fail — use ``git add`` via the "
        "shell tool first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Optional path to the git repository.",
            },
            "message": {
                "type": "string",
                "description": "The commit message.",
            },
        },
        "required": ["message"],
    }

    @property
    def permission(self) -> Permission:
        return Permission.WRITE

    async def execute(
        self, message: str, repo_path: str | None = None
    ) -> ToolResult:
        cwd = _resolve_repo_path(repo_path)
        try:
            stdout, stderr, rc = await _git(
                "commit", "-m", message, cwd=cwd, timeout=60
            )
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git commit timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip(),
            metadata={"repo_path": cwd, "message": message},
        )

    def summarize_call(self, **kwargs) -> str:
        msg = kwargs.get("message", "?")
        short = msg[:50] + "..." if len(msg) > 50 else msg
        return f"git_commit({short!r})"


# ---------------------------------------------------------------------------
# GitBranch
# ---------------------------------------------------------------------------


class GitBranch(BaseTool):
    """List, create, or delete branches."""

    name = "git_branch"
    description = (
        "List, create, or delete git branches. "
        "With no arguments, lists local branches. "
        "Use ``name`` to create a new branch. "
        "Use ``delete`` to delete a branch. "
        "Use ``remote`` to show remote-tracking branches."
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Optional path to the git repository.",
            },
            "name": {
                "type": "string",
                "description": "Name of the branch to create.",
            },
            "delete": {
                "type": "string",
                "description": "Name of the branch to delete.",
            },
            "remote": {
                "type": "boolean",
                "description": "List remote-tracking branches (``-r``).",
                "default": False,
            },
        },
    }

    # Dynamic: listing is READ, creating/deleting is WRITE
    @property
    def permission(self) -> Permission:
        return Permission.READ

    def get_permission(self, **kwargs) -> Permission:
        if kwargs.get("name") or kwargs.get("delete"):
            return Permission.WRITE
        return Permission.READ

    async def execute(
        self,
        repo_path: str | None = None,
        name: str | None = None,
        delete: str | None = None,
        remote: bool = False,
    ) -> ToolResult:
        cwd = _resolve_repo_path(repo_path)

        if delete:
            return await self._delete_branch(cwd, delete)
        if name:
            return await self._create_branch(cwd, name)

        # List branches
        args = ["branch", "--no-color"]
        if remote:
            args.append("-r")

        try:
            stdout, stderr, rc = await _git(*args, cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git branch timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)

        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip() or "(no branches)",
            metadata={"repo_path": cwd, "remote": remote},
        )

    async def _create_branch(self, cwd: str, name: str) -> ToolResult:
        try:
            stdout, stderr, rc = await _git("branch", name, cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git branch timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=f"Created branch '{name}'.",
            metadata={"repo_path": cwd, "branch": name},
        )

    async def _delete_branch(self, cwd: str, name: str) -> ToolResult:
        try:
            stdout, stderr, rc = await _git("branch", "-d", name, cwd=cwd)
        except TimeoutError:
            return ToolResult(
                tool_id="", tool_name=self.name, success=False, output="",
                error="git branch timed out.",
            )
        if rc != 0:
            return _git_error(self.name, stderr, rc)
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=stdout.strip() or f"Deleted branch '{name}'.",
            metadata={"repo_path": cwd, "branch": name},
        )

    def summarize_call(self, **kwargs) -> str:
        if kwargs.get("delete"):
            return f"git_branch(delete={kwargs['delete']!r})"
        if kwargs.get("name"):
            return f"git_branch(create={kwargs['name']!r})"
        return "git_branch(list)"
