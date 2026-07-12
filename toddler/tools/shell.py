"""Shell tool — run shell commands with timeout, sandboxing, and classification."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from toddler.tools.base import BaseTool, Permission, ToolResult

# ---------------------------------------------------------------------------
# Command classification — patterns for safe vs dangerous commands
# ---------------------------------------------------------------------------

# Commands or patterns that are always considered dangerous.
_DANGEROUS_PATTERNS: list[str] = [
    # Destructive filesystem
    r"\brm\b", r"\brmdir\b", r"\bdd\b",
    # Privilege escalation
    r"\bsudo\b", r"\bsu\b",
    # Network installers / fetchers (could download untrusted code)
    r"\bcurl\b", r"\bwget\b",
    r"\bpip\s+install\b", r"\bpip3\s+install\b",
    r"\bnpm\s+install\b", r"\bnpx\b",
    r"\byarn\s+add\b",
    r"\bcargo\s+install\b",
    r"\bgem\s+install\b",
    # Permission changes
    r"\bchmod\b", r"\bchown\b", r"\bchgrp\b",
    # System control
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\bsystemctl\b", r"\bservice\b",
    r"\bkill\b", r"\bkillall\b", r"\bpkill\b",
    # Fork bombs / resource exhaustion
    r":\(\)\s*\{", r"fork\s+bomb",
    # Disk / mount
    r"\bmount\b", r"\bumount\b", r"\bmkfs\b",
]

# Commands that are always considered safe (read-only or introspection).
_SAFE_COMMANDS: set[str] = {
    # File reading
    "cat", "head", "tail", "less", "more",
    # Directory listing
    "ls", "dir", "tree",
    # Search
    "grep", "egrep", "fgrep", "find", "locate", "which", "whereis",
    # File info
    "file", "stat", "wc", "du", "df", "md5sum", "sha1sum", "sha256sum",
    # Process info
    "ps", "top", "htop", "pgrep", "pidof",
    # Network info
    "ifconfig", "ip", "netstat", "ss", "hostname", "ping",
    # Environment
    "env", "printenv", "pwd", "whoami", "id", "groups", "uname",
    "echo", "printf", "date", "uptime",
    # Git read-only
    "git",
    # Python/node read-only (running scripts is dangerous, but these are
    # commonly used to check versions / paths)
    "python", "python3", "node", "rustc", "go", "java",
    # Dev tool version checks
    "cargo", "make", "cmake", "gcc", "g++", "clang",
    # Text processing
    "awk", "sed", "cut", "sort", "uniq", "tr", "tee",
    "diff", "cmp", "comm",
    # Archival
    "tar", "gzip", "gunzip", "zip", "unzip",
}


def classify_command(command: str) -> Permission:
    """Classify a shell command as safe or dangerous.

    Rules:
    1. Check against dangerous patterns first (regex).
    2. Extract the base command (first word) and check against known-safe set.
    3. Default to dangerous if uncertain.
    """
    stripped = command.strip()

    # Check dangerous patterns
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, stripped):
            return Permission.SHELL_DANGEROUS

    # Extract base command (first word, stripping path prefixes and sudo)
    first_word = stripped.split()[0] if stripped.split() else ""
    # Strip common path prefixes
    base = first_word.rsplit("/", 1)[-1] if "/" in first_word else first_word

    if base in _SAFE_COMMANDS:
        # Special case: git commands that are mutating
        if base == "git" and _is_mutating_git_command(stripped):
            return Permission.SHELL_DANGEROUS
        # Special case: python/node executing scripts
        if (
            base in ("python", "python3", "node") and
            _is_executing_script(stripped)
            ):
            return Permission.SHELL_DANGEROUS
        return Permission.SHELL_SAFE

    # Unknown → dangerous
    return Permission.SHELL_DANGEROUS


def _is_mutating_git_command(cmd: str) -> bool:
    """Check if a git command modifies state (push, commit, etc.)."""
    mutating = {
        "push", "commit", "merge", "rebase", "reset", "stash",
        "branch -D", "branch -d", "tag", "checkout -b",
        "add", "rm", "mv",
    }
    tokens = cmd.split()
    if len(tokens) < 2:
        return False
    sub = tokens[1]
    # Check two-word subcommands like "branch -D"
    if len(tokens) >= 3:
        sub2 = f"{tokens[1]} {tokens[2]}"
        if sub2 in mutating:
            return True
    return sub in mutating


def _is_executing_script(cmd: str) -> bool:
    """Check if python/node invocation is executing something.

    Any non-flag token after the interpreter name is treated as code
    execution — whether it's a script file (``script.py``), inline code
    (``-c "..."``), a module (``-m http.server``), or anything else.
    The only safe forms are version queries (``--version``, ``-V``),
    help (``--help``, ``-h``), or an interactive REPL (no arguments).
    """
    for token in cmd.split()[1:]:  # skip the interpreter
        if token.startswith("-") and token not in ("-c", "-m"):
            continue  # purely informational flag, e.g. --version, -V, -h
        # Any other token means code is being executed
        return True
    return False


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


class Shell(BaseTool):
    """Execute a shell command with timeout and working-directory support.

    Commands are **classified** before execution:
    - ``SHELL_SAFE`` — read-only / introspection commands (auto-approved)
    - ``SHELL_DANGEROUS`` — everything else (requires confirmation)

    Output is truncated to a configurable maximum length to prevent
    flooding the context window.
    """

    name = "shell"
    description = (
        "Execute a shell command. "
        "Use ``command`` for the shell command to run. "
        "Set ``working_dir`` to change the working directory. "
        "Set ``timeout`` to override the default timeout in seconds. "
        "Output is truncated to avoid flooding the context window."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "working_dir": {
                "type": "string",
                "description": (
                    "Optional working directory. Defaults to the current "
                    "working directory."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Optional timeout in seconds. Defaults to 60."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(
        self, default_timeout: int = 60, max_output: int = 50_000
    ) -> None:
        self._default_timeout = default_timeout
        self._max_output = max_output

    # ------------------------------------------------------------------
    # Permission (dynamic — based on the command string)
    # ------------------------------------------------------------------

    @property
    def permission(self) -> Permission:
        """Static fallback: SHELL_SAFE (classification is done per-call)."""
        return Permission.SHELL_SAFE

    def get_permission(self, **kwargs) -> Permission:
        command = str(kwargs.get("command", ""))
        if not command:
            return Permission.SHELL_DANGEROUS
        return classify_command(command)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
    ) -> ToolResult:
        cwd = (
            str(Path(working_dir).expanduser().resolve())
            if working_dir
            else os.getcwd()
        )
        if working_dir and not Path(cwd).is_dir():
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Working directory does not exist: {cwd}",
            )

        effective_timeout = (
            timeout if timeout is not None else self._default_timeout
        )

        try:
            stdout, stderr, returncode = await _run_command(
                command,
                cwd=cwd,
                timeout=effective_timeout,
            )
        except TimeoutError:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Command timed out after {effective_timeout}s: "
                      f"{command[:200]}",
            )
        except Exception as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=f"Failed to execute command: {exc}",
            )

        # Build output
        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        output = "\n".join(parts).strip()

        # Truncate
        if len(output) > self._max_output:
            output = (
                output[: self._max_output]
                + f"\n\n... (truncated {len(output) - self._max_output} chars)"
            )

        success = returncode == 0
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=success,
            output=output or "(no output)",
            metadata={
                "command": command,
                "returncode": returncode,
                "cwd": cwd,
                "timeout": effective_timeout,
            },
        )

    def summarize_call(self, **kwargs) -> str:
        cmd = kwargs.get("command", "?")
        short = cmd[:60] + "..." if len(cmd) > 60 else cmd
        return f"shell({short!r})"


# ---------------------------------------------------------------------------
# Internal — asyncio subprocess runner
# ---------------------------------------------------------------------------


async def _run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
) -> tuple[str, str, int]:
    """Run a shell command and return ``(stdout, stderr, returncode)``.

    Uses ``asyncio.create_subprocess_shell`` for non-blocking I/O.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        # Inherit a clean environment from the current process
        env={**os.environ},
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, proc.returncode or 0
