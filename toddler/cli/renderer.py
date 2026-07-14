"""Rich-based renderer for streaming markdown, syntax-highlighted code,
and tool execution status."""

from __future__ import annotations

import re

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from toddler.agent.events import (
    AgentError,
    AgentFinished,
    AgentPaused,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)

# ---------------------------------------------------------------------------
# Styles / colour palette
# ---------------------------------------------------------------------------

_TOOL_INFO = "bold cyan"
_TOOL_SUCCESS = "bold green"
_TOOL_ERROR = "bold red"
_TOOL_RUNNING = "bold yellow"
_HEADER = "bold blue"
_STATUS_MUTED = "dim"
_ACCENT = "bold magenta"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """Handles console output for the agent loop.

    Wraps a :class:`rich.console.Console` and provides methods to render
    streaming text, tool calls, errors, confirmation prompts, and final
    summaries in a consistent visual style.

    Parameters
    ----------
    console:
        A Rich Console instance.  When *None*, a default ``stderr`` console
        is created (``stderr`` avoids interfering with stdout-based piping
        in one-shot mode).
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console(stderr=True)
        self._tool_panel_lines: list[str] = []

    # ------------------------------------------------------------------
    # Top-level rendering helpers
    # ------------------------------------------------------------------

    def header(self, text: str) -> None:
        """Print a prominent header."""
        self._console.print()
        self._console.print(Text(text, style=_HEADER))

    def info(self, text: str) -> None:
        """Print a muted informational line."""
        self._console.print(Text(text, style=_STATUS_MUTED))

    def success(self, text: str) -> None:
        """Print a success message."""
        self._console.print(Text(f"✓ {text}", style=_TOOL_SUCCESS))

    def error(self, text: str) -> None:
        """Print an error message."""
        self._console.print(Text(f"✗ {text}", style=_TOOL_ERROR))

    def warning(self, text: str) -> None:
        """Print a warning message."""
        self._console.print(Text(f"⚠ {text}", style=_TOOL_RUNNING))

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def markdown(self, text: str) -> None:
        """Render *text* as GitHub-flavoured markdown."""
        # Rich's Markdown class handles code fences, lists, headings, etc.
        self._console.print(Markdown(text))

    def code(self, code: str, language: str = "") -> None:
        """Render a code block with syntax highlighting."""
        if not code.strip():
            return
        lang = language or _guess_language(code)
        self._console.print(
            Syntax(code, lang, theme="monokai", line_numbers=False)
        )

    # ------------------------------------------------------------------
    # Agent event renderers
    # ------------------------------------------------------------------

    def text_delta(self, event: TextDelta) -> None:
        """Render a text delta from the agent (non-streaming: print all at once)."""  # noqa: E501
        self.markdown(event.text)

    def tool_call_start(self, event: ToolCallStart) -> None:
        """Announce that a tool is being invoked."""
        label = _format_tool_call(event.tool_name, event.partial_input or {})
        self._console.print(Text(f"▶ {label}", style=_TOOL_RUNNING))

    def tool_call_end(self, event: ToolCallEnd) -> None:
        """Render a completed tool call."""
        result = event.result
        if result is None:
            self._console.print(
                Text(f"  ⚠ {event.tool_name} — no result", style=_TOOL_ERROR)
            )
            return

        if result.success:
            # Truncate large outputs for display.
            output_preview = _truncate(
                result.output, max_lines=5, max_chars=300
            )
            self._console.print(Text("  ✓ Done", style=_TOOL_SUCCESS))
            if output_preview.strip():
                self._console.print(
                    Text(output_preview, style=_STATUS_MUTED),
                )
        else:
            err_text = result.error or "Unknown error"
            self._console.print(
                Text(f"  ✗ {_truncate(err_text, max_lines=3, max_chars=200)}",
                     style=_TOOL_ERROR),
            )

    def agent_paused(self, event: AgentPaused) -> None:
        """Render a confirmation / pause prompt."""
        choices_str = "/".join(event.choices) if event.choices else "y/n"
        self._console.print()
        self._console.print(
            Panel(
                Text(event.prompt, style="bold white"),
                title=f"[{choices_str}]",
                title_align="left",
                border_style=_TOOL_RUNNING,
            )
        )

    def agent_finished(self, event: AgentFinished) -> None:
        """Render the final completion message."""
        self._console.print()
        if event.usage:
            self.info(
                f"Tokens: {event.usage.input_tokens:,} in / "
                f"{event.usage.output_tokens:,} out"
            )
        self.success(f"Done — {event.reason}")

    def agent_error(self, event: AgentError) -> None:
        """Render a recoverable error."""
        label = "Recoverable" if event.recoverable else "Fatal"
        self._console.print(
            Text(f"⚠ [{label}] {event.message}", style=_TOOL_ERROR)
        )

    # ------------------------------------------------------------------
    # Structured output helpers
    # ------------------------------------------------------------------

    def table(
        self,
        title: str,
        columns: list[str],
        rows: list[list[str]],
    ) -> None:
        """Render a Rich table."""
        tbl = Table(title=title, title_style=_HEADER)
        for col in columns:
            tbl.add_column(col)
        for row in rows:
            tbl.add_row(*row)
        self._console.print(tbl)

    def tree(self, root_label: str, children: list[str]) -> None:
        """Render a simple tree."""
        tr = Tree(root_label, style=_HEADER)
        for child in children:
            tr.add(child)
        self._console.print(tr)

    # ------------------------------------------------------------------
    # Live display (streaming mode, Phase 6 — stubbed)
    # ------------------------------------------------------------------

    @property
    def console(self) -> Console:
        """The underlying Rich Console instance."""
        return self._console

    def live_context(self) -> Live | None:
        """Return a Live context manager for streaming updates.

        Returns *None* in Phase 5 (no streaming display); Phase 6 replaces
        this with a Rich ``Live`` + ``Layout`` dual-panel setup.
        """
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^```(\w*)")


def _guess_language(code: str) -> str:
    """Guess a language for syntax highlighting from shebang / keywords."""
    first = code.strip().split("\n", 1)[0] if code.strip() else ""
    if first.startswith("#!") and "python" in first:
        return "python"
    if "def " in code or "import " in code or "class " in code:
        return "python"
    if "function " in code or "const " in code or "let " in code:
        return "javascript"
    if "SELECT " in code.upper() or "CREATE TABLE" in code.upper():
        return "sql"
    return "text"


def _truncate(text: str, max_lines: int = 5, max_chars: int = 300) -> str:
    """Truncate *text* for compact display."""
    lines = text.splitlines()
    truncated = False

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars]
        truncated = True

    if truncated:
        result += "\n… (truncated)"
    return result


def _format_tool_call(name: str, params: dict) -> str:
    """Format a tool name + key parameters for display."""
    # Shorten common long params.
    short: dict[str, str] = {}
    for k, v in params.items():
        s = str(v)
        if isinstance(v, str) and len(s) > 60:
            s = s[:57] + "..."
        short[k] = s

    args = ", ".join(f"{k}={s!r}" for k, s in short.items())
    return f"{name}({args})"
