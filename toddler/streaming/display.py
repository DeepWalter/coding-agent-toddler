"""StreamDisplay — Rich Live dual-panel display for streaming agent output.

Renders streaming LLM responses and tool execution status in real time
using :mod:`rich`'s ``Live`` + ``Layout`` system.

Layout::

    ┌─ Output ─────────────────────────────┐
    │ Streaming markdown + partial tool    │  ← upper panel (auto-expand)
    │ calls appear here token-by-token …   │
    └──────────────────────────────────────┘
    ┌─ Tools ──────────────────────────────┐
    │ Status  Tool          Summary        │  ← lower panel (fixed height)
    │ [▶]     read_file    auth.py         │
    │ [✓]     grep         Found 3 matches │
    │ [ ]     write_file   Pending …       │
    └──────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Tool-row bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _ToolRow:
    """Display state for one tool call in the lower panel."""

    name: str
    status: str   # "▶" running, "✓" success, "✗" error, " " pending
    summary: str  # short description / result preview


# ---------------------------------------------------------------------------
# Status icons
# ---------------------------------------------------------------------------

_ICON_RUNNING = Text("▶", style="bold yellow")
_ICON_SUCCESS = Text("✓", style="bold green")
_ICON_ERROR = Text("✗", style="bold red")
_ICON_PENDING = Text(" ", style="dim")


_STATUS_STYLES = {
    "running": _ICON_RUNNING,
    "success": _ICON_SUCCESS,
    "error": _ICON_ERROR,
    "pending": _ICON_PENDING,
}


# ---------------------------------------------------------------------------
# StreamDisplay
# ---------------------------------------------------------------------------


class StreamDisplay:
    """Manages a Rich ``Live`` display with dual panels for streaming output.

    The upper panel renders accumulated markdown text from the LLM.  The
    lower panel shows a table of tool calls with their execution status.

    Typical usage::

        display = StreamDisplay(console)
        display.start()
        try:
            display.append_text("Hello")
            display.tool_started("t1", "read_file")
            display.tool_completed("t1", success=True, summary="auth.py")
        finally:
            display.stop()

    Parameters
    ----------
    console:
        The Rich Console to render on.
    refresh_per_second:
        Max refresh rate for the Live display (default 8).
    """

    def __init__(
        self,
        console: Console,
        *,
        refresh_per_second: float = 8.0,
    ) -> None:
        self._console = console
        self._text = ""
        self._tools: dict[str, _ToolRow] = {}
        self._tool_order: list[str] = []
        self._live = Live(
            self._build_layout(),
            console=console,
            refresh_per_second=refresh_per_second,
            vertical_overflow="visible",
        )

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enter the live display context (starts auto-refreshing)."""
        self._live.start()

    def stop(self) -> None:
        """Exit the live display context (stops auto-refreshing).

        After stopping, the final rendered output remains visible on the
        console.
        """
        self._live.stop()

    def __enter__(self) -> StreamDisplay:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Text accumulation
    # ------------------------------------------------------------------

    def append_text(self, text: str) -> None:
        """Append *text* to the streaming output panel."""
        self._text += text
        self._refresh()

    def set_text(self, text: str) -> None:
        """Replace the entire streaming output text."""
        self._text = text
        self._refresh()

    # ------------------------------------------------------------------
    # Tool status
    # ------------------------------------------------------------------

    def tool_started(
        self, tool_id: str, tool_name: str, summary: str = "",
    ) -> None:
        """Register a new tool call that has begun executing.

        Displays with the ▶ (running) icon.
        """
        self._tools[tool_id] = _ToolRow(
            name=tool_name, status="running", summary=summary,
        )
        if tool_id not in self._tool_order:
            self._tool_order.append(tool_id)
        self._refresh()

    def tool_completed(
        self, tool_id: str, *, success: bool, summary: str = "",
    ) -> None:
        """Mark a tool call as completed.

        Parameters
        ----------
        tool_id:
            The tool call ID (must match a previous ``tool_started`` call).
        success:
            ``True`` → ✓ (green), ``False`` → ✗ (red).
        summary:
            Short description or result preview to show in the table.
        """
        if tool_id in self._tools:
            self._tools[tool_id].status = "success" if success else "error"
            if summary:
                self._tools[tool_id].summary = summary
            self._refresh()

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Rebuild the layout and push it to the Live display."""
        self._live.update(self._build_layout())

    def _build_layout(self) -> Layout:
        """Build the dual-panel Rich Layout."""
        layout = Layout()
        layout.split_column(
            Layout(
                self._build_upper_panel(),
                name="upper",
            ),
            Layout(
                self._build_lower_panel(),
                name="lower",
                size=min(len(self._tools) + 3, 12) if self._tools else 3,
            ),
        )
        return layout

    def _build_upper_panel(self) -> Panel:
        """Build the upper panel — streaming markdown text."""
        md = (
            Markdown(self._text)
            if self._text
            else Markdown("*Waiting for response…*")
        )
        return Panel(
            md,
            title="Output",
            title_align="left",
            border_style="blue",
        )

    def _build_lower_panel(self) -> Panel:
        """Build the lower panel — tool execution status table."""
        table = Table(show_header=True, box=None, padding=(0, 1))
        table.add_column("St", width=2, justify="center")
        table.add_column("Tool", style="bold cyan")
        table.add_column("Summary", style="dim", max_width=60)

        if not self._tools:
            table.add_row("", "*No tools executed yet*", "")
        else:
            for tool_id in self._tool_order:
                row = self._tools.get(tool_id)
                if row is None:
                    continue
                icon = _STATUS_STYLES.get(row.status, _ICON_PENDING)
                table.add_row(icon, row.name, row.summary)

        return Panel(
            table,
            title="Tools",
            title_align="left",
            border_style="dim",
        )
