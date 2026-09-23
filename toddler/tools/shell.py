"""Shell tool — run shell commands with timeout, sandboxing, and classification."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
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

# Characters the lexer splits on.  Redirections are in here so that
# ``2>&1`` tokenizes as ``2``, ``>&``, ``1`` instead of splitting at the
# ``&`` — an ``&`` there is redirection, not a background operator.
_OPERATOR_CHARS = ";|&()<>\n"

# The subset that ends one command and starts the next: a run of these is
# a segment boundary, so each side gets classified on its own.  Newlines
# belong here — a multi-line command is a script, not one command.
_SEGMENT_OPERATORS = ";|&()\n"

# Redirection tokens — they belong to a segment, and are never the
# command the segment runs.
_REDIRECTIONS = {"<", ">", ">>", "<<", "<&", ">&", "<<<"}

# Command substitution (``$(...)`` or backticks) hides a nested command
# this classifier cannot see, so its mere presence decides the verdict.
_SUBSTITUTIONS = ("$(", "`")


def classify_command(command: str) -> Permission:
    """Classify a shell command as safe or dangerous.

    The command is split into the commands it actually runs, and **every**
    one of them has to be safe for the whole to be — a safe prefix must
    not launder what follows it, whether that is on the next line or
    after an ``&&``.

    Rules:
    1. Check against dangerous patterns first (regex).
    2. A command substitution hides a nested command from this
       classifier — always dangerous.
    3. Split into segments and require each one to be safe:
       - its command must be in the known-safe set,
       - a ``git`` segment must not mutate,
       - a ``python``/``node`` segment must not execute code.
    4. Default to dangerous if uncertain.
    """
    stripped = command.strip()

    # Check dangerous patterns
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, stripped):
            return Permission.SHELL_DANGEROUS

    if any(marker in stripped for marker in _SUBSTITUTIONS):
        return Permission.SHELL_DANGEROUS

    segments = _split_segments(stripped)
    if not segments:
        return Permission.SHELL_DANGEROUS

    if all(
        _classify_segment(segment) is Permission.SHELL_SAFE
        for segment in segments
    ):
        return Permission.SHELL_SAFE

    # Unknown or unsafe → dangerous
    return Permission.SHELL_DANGEROUS


def _split_segments(command: str) -> list[list[str]] | None:
    """Split *command* into one token list per command it runs.

    Tokenizing rather than splitting on characters keeps quoting intact:
    ``echo "a; b"`` is one segment, not two.  Newlines are punctuation
    here rather than whitespace — otherwise ``ls\\npython x`` would come
    back as a single segment led by ``ls``.  Returns ``None`` when the
    command does not tokenize, e.g. on an unbalanced quote.
    """
    lexer = shlex.shlex(
        command, posix=True, punctuation_chars=_OPERATOR_CHARS,
    )
    lexer.whitespace = " \t\r"
    try:
        tokens = list(lexer)
    except ValueError:
        return None

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= set(_SEGMENT_OPERATORS):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _classify_segment(tokens: list[str]) -> Permission:
    """Classify one command segment, its operators already split off."""
    # The command is the segment's first token that is not a redirection.
    command = next((t for t in tokens if t not in _REDIRECTIONS), "")
    # Strip common path prefixes
    base = command.rsplit("/", 1)[-1]
    args = tokens[tokens.index(command) + 1:] if command in tokens else []

    if base not in _SAFE_COMMANDS:
        # Unknown → dangerous
        return Permission.SHELL_DANGEROUS

    # Special case: git commands that are mutating
    if base == "git" and _is_mutating_git_command(args):
        return Permission.SHELL_DANGEROUS
    # Special case: python/node executing scripts
    if base in ("python", "python3", "node") and _is_executing_script(args):
        return Permission.SHELL_DANGEROUS

    return Permission.SHELL_SAFE


def _is_mutating_git_command(args: list[str]) -> bool:
    """Check if a git segment's arguments modify state (push, commit, …).

    Every argument is scanned rather than just the subcommand position, so
    a flag in front of the subcommand (``git -C /tmp push``) hides
    nothing.  Flags are skipped, but a value of theirs is not — the cost
    of the extra caution is a confirmation for a ref that happens to
    share a subcommand's name.
    """
    mutating = {
        "push", "commit", "merge", "rebase", "reset", "stash",
        "tag", "add", "rm", "mv",
    }
    words = [a for a in args if not a.startswith("-")]
    if any(word in mutating for word in words):
        return True
    # Creating a branch, or deleting one
    if "branch" in words and any(a in ("-d", "-D", "--delete") for a in args):
        return True
    return "checkout" in words and "-b" in args


def _is_executing_script(args: list[str]) -> bool:
    """Check if python/node invocation is executing something.

    Any non-flag argument is treated as code execution — whether it's a
    script file (``script.py``), inline code (``-c "..."``), a module
    (``-m http.server``), or anything else.  The only safe forms are
    version queries (``--version``, ``-V``), help (``--help``, ``-h``),
    or an interactive REPL (no arguments).
    """
    for token in args:
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
