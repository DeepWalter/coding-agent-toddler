"""Rich-based renderer for streaming markdown, syntax-highlighted code,
and tool execution status.

Provides an abstract :class:`Renderer` base with two concrete
implementations:

- :class:`StreamingRenderer` — Rich ``Live`` dual-panel display
- :class:`NonStreamingRenderer` — one-shot ``console.print`` output

Use :func:`create_renderer` to instantiate the right subclass for the
current output mode.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment, SegmentLines
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from toddler.agent.events import (
    AgentError,
    AgentFinished,
    AgentPaused,
    PlanProposed,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)

if TYPE_CHECKING:
    from toddler.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Styles / colour palette (shared by base and subclasses)
# ---------------------------------------------------------------------------

_TOOL_INFO = "bold cyan"
_TOOL_SUCCESS = "bold green"
_TOOL_ERROR = "bold red"
_TOOL_RUNNING = "bold yellow"
_HEADER = "bold blue"
_STATUS_MUTED = "dim"
_ACCENT = "bold magenta"


# ---------------------------------------------------------------------------
# Streaming-mode internals
# ---------------------------------------------------------------------------


@dataclass
class _ToolRow:
    """Display state for one tool call in the streaming tools panel."""

    name: str
    status: str   # "running", "success", or "error"
    summary: str  # short description / result preview


_ICON_RUNNING = Text("▶", style="bold yellow")
_ICON_SUCCESS = Text("✓", style="bold green")
_ICON_ERROR = Text("✗", style="bold red")

_STATUS_STYLES = {
    "running": _ICON_RUNNING,
    "success": _ICON_SUCCESS,
    "error": _ICON_ERROR,
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Renderer(ABC):
    """Abstract base for agent output renderers.

    Subclasses implement the four streaming event handlers to provide
    either real-time streaming output or one-shot printing.

    Parameters
    ----------
    console:
        A Rich Console instance.  When *None*, a default ``stderr``
        console is created.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console(stderr=True)

    # ------------------------------------------------------------------
    # Lifecycle (no-ops — override in subclasses that need them)
    # ------------------------------------------------------------------

    def start(
        self, turn_number: int = 0, output_path: Path | None = None  # noqa: ARG002
    ) -> None:
        """Start the renderer. No-op by default."""
        return

    def stop(self) -> None:
        """Stop the renderer. No-op by default."""
        return

    def pause(self) -> None:
        """Temporarily pause for blocking/one-shot content.

        No-op by default.
        """
        return

    def resume(self) -> None:
        """Resume after :meth:`pause`. No-op by default."""
        return

    def flush_to_console(self) -> None:
        """Flush accumulated output to the main console scrollback.

        No-op by default.  :class:`StreamingRenderer` overrides this to
        print the final assembled renderable after the alternate screen
        has been exited.
        """
        return

    def __enter__(self) -> Renderer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def console(self) -> Console:
        """The underlying Rich Console instance."""
        return self._console

    # ------------------------------------------------------------------
    # Top-level rendering helpers
    # ------------------------------------------------------------------

    def print(self, *args, **kwargs) -> None:
        """Print directly to the console without styling or formatting.

        Delegates to :meth:`rich.console.Console.print`.
        """
        self._console.print(*args, **kwargs)

    def banner(self) -> None:
        """Print the Toddler welcome banner."""
        self._console.print()
        self._console.print(
            Text("🐣 Toddler", style="bold yellow"),
            Text(" — coding agent", style="dim"),
        )

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
    # Markdown / code rendering
    # ------------------------------------------------------------------

    def markdown(self, text: str) -> None:
        """Render *text* as GitHub-flavoured markdown."""
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
    # Structured output
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
    # Blocking event handlers (shared by all modes)
    # ------------------------------------------------------------------

    def on_agent_paused(self, event: AgentPaused) -> None:
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

    def on_agent_finished(self, event: AgentFinished) -> None:
        """Render the final completion message."""
        self._console.print()
        if event.usage:
            self.info(
                f"Tokens: {event.usage.input_tokens:,} in / "
                f"{event.usage.output_tokens:,} out"
            )
        self.success(f"Done — {event.reason}")

    def on_agent_error(self, event: AgentError) -> None:
        """Render a recoverable or fatal error."""
        label = "Recoverable" if event.recoverable else "Fatal"
        self._console.print(
            Text(f"⚠ [{label}] {event.message}", style=_TOOL_ERROR)
        )

    def on_plan_proposed(self, event: PlanProposed) -> None:
        """Render a proposed plan."""
        self.info(f"Plan proposed: {event.plan.title}")
        self.markdown(event.plan.summary)

    # ------------------------------------------------------------------
    # Streaming event handlers (abstract)
    # ------------------------------------------------------------------

    @abstractmethod
    def on_text_delta(self, event: TextDelta) -> None:
        """Render a streaming text delta."""

    @abstractmethod
    def on_tool_call_start(self, event: ToolCallStart) -> None:
        """Render a tool call that has started executing."""

    @abstractmethod
    def on_tool_call_delta(self, event: ToolCallDelta) -> None:
        """Render an incremental tool-input fragment."""

    @abstractmethod
    def on_tool_call_end(self, event: ToolCallEnd) -> None:
        """Render a completed tool call."""


# ---------------------------------------------------------------------------
# Streaming implementation
# ---------------------------------------------------------------------------


class StreamingRenderer(Renderer):
    """Real-time streaming renderer using the alternate screen buffer.

    Accumulates text deltas and tool status in memory, refreshing a
    :class:`~rich.live.Live` display at the target frame rate.  Uses
    ``screen=True`` so that intermediate frames never pollute the
    terminal scrollback — only the final output is printed to the main
    screen buffer when the turn completes.

    Parameters
    ----------
    console:
        A Rich Console instance.
    refresh_per_second:
        Max refresh rate for the Live display (default 10).
    max_output_lines:
        Max lines of output before truncating (0 to disable, default 40).
    max_output_panel_height:
        Max height of the output panel in lines.  When > 0 and the
        rendered markdown exceeds this height, the panel clips to show
        only the latest (bottom) content.  Set to 0 to disable.
        During the dismiss prompt, arrow keys scroll the clipped content.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        refresh_per_second: float = 10.0,
        max_output_lines: int = 40,
        max_output_panel_height: int = 0,
    ) -> None:
        super().__init__(console)
        self._text = ""
        self._tools: dict[str, _ToolRow] = {}
        self._tool_order: list[str] = []
        self._min_interval = 1.0 / refresh_per_second
        self._last_update = 0.0
        self._max_lines = max_output_lines
        self._max_panel_height = max_output_panel_height
        self._scroll_offset: int = 0
        self._total_content_height: int = 0
        self._turn_number = 0
        self._output_path: Path | None = None
        self._stopped = False
        self._dismiss_prompt = False
        self._live = Live(
            self._build_renderable(),
            console=self._console,
            refresh_per_second=refresh_per_second,
            screen=True,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        turn_number: int = 0,
        output_path: Path | None = None,
    ) -> None:
        """Start the Live display with a clean slate for a new turn.

        Toggles the alternate screen buffer to force the terminal to
        clear any stale content left over from a previous entry, then
        starts Live which enters it fresh.

        Parameters
        ----------
        turn_number:
            Current turn number for truncation notice and /view hint.
        output_path:
            Path to write full output to when truncation fires.
        """
        self._text = ""
        self._tools.clear()
        self._tool_order.clear()
        self._scroll_offset = 0
        self._total_content_height = 0
        self._turn_number = turn_number
        self._output_path = output_path
        self._stopped = False
        self._dismiss_prompt = False
        self._live.start()
        self._refresh(force=True)

    def stop(self) -> None:
        """Show a dismiss prompt on the alternate screen, wait for Enter,
        then stop the Live display.

        Idempotent — subsequent calls after the first are no-ops.

        When the output panel height limit is active and content exceeds
        it, ``↑`` / ``↓`` arrow keys scroll the output before dismissing.
        """
        if self._stopped:
            return

        self._scroll_offset = 0
        self._dismiss_prompt = True
        self._refresh(force=True)
        self._wait_for_dismiss()
        self._dismiss_prompt = False

        self._stopped = True
        self._live.stop()
        # Disable panel-height clipping so that flush_to_console()
        # renders the full markdown into the terminal scrollback.
        self._max_panel_height = 0

    def _build_truncation_notice(self, filepath: Path) -> Panel:
        """Build a clickable truncation notice with OSC 8 file link."""
        file_uri = f"file://{filepath.resolve()}"
        turn_str = str(self._turn_number)

        text = Text.assemble(
            ("Output truncated to ", ""),
            (str(self._max_lines), "bold yellow"),
            (" lines.  ", ""),
            ("Full output: ", ""),
            (str(filepath), f"link {file_uri}"),
            ("\nView full output by ", ""),
            ("click", f"link {file_uri}"),
            (" or ", ""),
            ("/view ", ""),
            (turn_str, "bold cyan"),
        )

        return Panel(
            text,
            title="Truncated",
            title_align="left",
            border_style="yellow",
        )

    def pause(self) -> None:
        """Temporarily stop Live for blocking content.

        Exits the alternate screen buffer (restoring the original terminal)
        so that blocking content (e.g. confirmation prompt) is displayed
        on the main screen.  Accumulated text and tool state are preserved
        for :meth:`resume`.
        """
        self._live.stop()

    def resume(self) -> None:
        """Resume Live after :meth:`pause` (preserving accumulated state)."""
        self._live.start()

    def flush_to_console(self) -> None:
        """Print the final assembled output to the main console scrollback.

        When the accumulated text exceeds :attr:`_max_lines` and an
        *output_path* was provided, the full text is written to disk,
        only the first N lines are printed to scrollback, and a clickable
        truncation notice is appended.

        Call after :meth:`stop` has exited the alternate screen so the
        output lands in the terminal scrollback rather than on the
        alternate screen.
        """
        lines = self._text.splitlines()
        should_truncate = (
            self._max_lines > 0
            and len(lines) > self._max_lines
            and self._output_path is not None
            and self._turn_number > 0
        )

        if should_truncate:
            full_text = self._text
            assert self._output_path is not None  # narrowed by should_truncate
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_path.write_text(full_text, encoding="utf-8")

            # Swap in truncated text for scrollback display
            self._text = "\n".join(lines[: self._max_lines])
            renderable = self._build_renderable()
            notice = self._build_truncation_notice(self._output_path)
            self._console.print(Group(renderable, notice))

            # Restore full text on the instance
            self._text = full_text
        else:
            self._console.print(self._build_renderable())

    # ------------------------------------------------------------------
    # Streaming event handlers
    # ------------------------------------------------------------------

    def on_text_delta(self, event: TextDelta) -> None:
        """Accumulate text and throttle-refresh the Live display."""
        self._text += event.text
        self._refresh()

    def on_tool_call_start(self, event: ToolCallStart) -> None:
        """Add a running row to the tools panel."""
        summary = _format_tool_table_summary(
            event.tool_name, event.partial_input or {},
        )
        self._tools[event.tool_id] = _ToolRow(
            name=event.tool_name, status="running", summary=summary,
        )
        if event.tool_id not in self._tool_order:
            self._tool_order.append(event.tool_id)
        self._refresh()

    def on_tool_call_delta(self, event: ToolCallDelta) -> None:
        """Update the tool row's input summary."""
        summary = _format_tool_table_summary("", event.input_delta)
        if event.tool_id not in self._tools:
            self._tools[event.tool_id] = _ToolRow(
                name="", status="running", summary=summary,
            )
            if event.tool_id not in self._tool_order:
                self._tool_order.append(event.tool_id)
        else:
            self._tools[event.tool_id].summary = summary
        self._refresh()

    def on_tool_call_end(self, event: ToolCallEnd) -> None:
        """Mark the tool row success or error and refresh."""
        result = event.result
        if event.tool_id in self._tools:
            if result is None:
                self._tools[event.tool_id].status = "error"
            elif result.success:
                self._tools[event.tool_id].status = "success"
            else:
                self._tools[event.tool_id].status = "error"
            self._tools[event.tool_id].summary = (
                _truncate_tool_result(result)
            )
        self._refresh()

    # ------------------------------------------------------------------
    # Streaming internals
    # ------------------------------------------------------------------

    def _refresh(self, force=False) -> None:
        """Rebuild and push the renderable, throttled to target rate."""
        now = time.monotonic()
        if not force and now - self._last_update < self._min_interval:
            return
        self._last_update = now
        self._live.update(self._build_renderable())

    # ------------------------------------------------------------------
    # Output panel height limiting
    # ------------------------------------------------------------------

    def _render_md_to_lines(
        self, md: Markdown
    ) -> list[list[Segment]]:
        """Render *md* and return its styled segment lines.

        Uses ``console.render_lines`` with the current console width so
        that word-wrapping is accounted for in the line count.
        """
        width = self._console.width - 4  # allow for panel border + padding
        options = self._console.options.update_width(max(1, width))
        return list(self._console.render_lines(md, options))

    def _clip_output_panel(self, md: Markdown) -> RenderableType:
        """Return a renderable clipped to ``_max_panel_height`` lines.

        When the markdown height fits within the limit the original
        ``Markdown`` object is returned unchanged.  Otherwise the
        rendered segment lines are sliced to show the visible window
        (respecting ``_scroll_offset``) and wrapped in
        :class:`~rich.segment.SegmentLines`.
        """
        if self._max_panel_height <= 0:
            return md

        all_lines = self._render_md_to_lines(md)
        total = len(all_lines)
        self._total_content_height = total

        if total <= self._max_panel_height:
            return md

        # Reserve room for scroll indicators (up to 2 lines).
        available = self._max_panel_height
        has_above = False
        has_below = False

        # During streaming, always pin to the bottom (ignore offset).
        scroll = self._scroll_offset if self._dismiss_prompt else 0

        # Calculate visible range — we want the *end* of the content
        # when scroll offset is 0 (default / streaming bottom-follow).
        end = total - scroll
        start = max(0, end - available)
        # Adjust end if we started from 0 and there are fewer lines than
        # available in the first scroll position.
        end = min(start + available, total)

        has_above = start > 0
        has_below = end < total

        # If indicators are shown, reduce the content slice accordingly.
        indicator_lines = int(has_above) + int(has_below)
        if indicator_lines and end - start + indicator_lines > available:
            if has_above and has_below:
                start += 1
                end -= 1
            elif has_above:
                start += indicator_lines
            else:
                end -= indicator_lines

        visible = all_lines[start:end]

        # Defensive: if the visible window is empty (shouldn't happen),
        # fall back to showing the raw markdown.
        if not visible:
            return md

        # Build the result with scroll indicators.
        parts: list[RenderableType] = []
        if has_above:
            parts.append(
                Text("↑ … more above", style="dim italic", justify="center")
            )
        parts.append(SegmentLines(visible, new_lines=True))
        if has_below:
            parts.append(
                Text("↓ … more below", style="dim italic", justify="center")
            )

        return Group(*parts)

    # ------------------------------------------------------------------
    # Build renderable
    # ------------------------------------------------------------------

    def _build_renderable(self) -> Group:
        """Build the dual-panel output: Output (markdown) + Tools (table)."""
        md = (
            Markdown(self._text)
            if self._text
            else Markdown("*Waiting for response…*")
        )
        clipped = self._clip_output_panel(md)
        output_panel = Panel(
            clipped,
            title="Output",
            title_align="left",
            border_style="blue",
        )

        elements: list = [output_panel]

        if self._tools:
            table = Table(show_header=True, box=None, padding=(0, 1))
            table.add_column("St", width=2, justify="center")
            table.add_column("Tool", style="bold cyan")
            table.add_column("Summary", style="dim", max_width=60)

            for tool_id in self._tool_order:
                row = self._tools.get(tool_id)
                if row is None:
                    continue
                icon = _STATUS_STYLES[row.status]
                table.add_row(icon, row.name, row.summary)

            tools_panel = Panel(
                table,
                title="Tools",
                title_align="left",
                border_style="dim",
            )
            elements.append(tools_panel)

        if self._dismiss_prompt:
            if self._total_content_height > self._max_panel_height > 0:
                hint = "\n↑↓ to scroll • Enter to continue…"
            else:
                hint = "\nPress Enter to continue…"
            elements.append(Text(hint, style="dim italic"))

        return Group(*elements)

    # ------------------------------------------------------------------
    # Dismiss prompt with scroll support
    # ------------------------------------------------------------------

    def _scroll_up(self, lines: int = 1) -> None:
        """Scroll the output panel view upward (show older content)."""
        max_offset = max(
            0, self._total_content_height - self._max_panel_height
        )
        self._scroll_offset = min(
            self._scroll_offset + lines, max_offset
        )

    def _scroll_down(self, lines: int = 1) -> None:
        """Scroll the output panel view downward (show newer content)."""
        self._scroll_offset = max(self._scroll_offset - lines, 0)

    def _wait_for_dismiss(self) -> None:
        """Block until Enter, allowing ``↑``/``↓`` to scroll the output.

        Disables only canonical mode and echo on stdin so that arrow-key
        sequences can be read byte-by-byte.  Output processing flags
        (notably ``OPOST``) are left untouched so that Rich's
        :class:`~rich.live.Live` display continues to render correctly.

        When the content fits within the panel height limit (or height
        limiting is disabled), falls back to a simple ``sys.stdin.readline``.
        """
        if not (
            self._max_panel_height > 0
            and self._total_content_height > self._max_panel_height
        ):
            sys.stdin.readline()
            return

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            new = termios.tcgetattr(fd)
            # Disable canonical mode (line buffering) and echo only;
            # keep OPOST and all other output flags intact so that
            # Rich's alternate-screen rendering is unaffected.
            new[3] &= ~(termios.ICANON | termios.ECHO)  # lflag
            new[6][termios.VMIN] = 1   # cc: read blocks for at least 1 byte
            new[6][termios.VTIME] = 0  # cc: no inter-byte timeout
            termios.tcsetattr(fd, termios.TCSANOW, new)

            while True:
                self._refresh(force=True)
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not r:
                    continue
                ch = os.read(fd, 1)
                if ch == b"\x1b":
                    # Check for arrow-key / page-up/down sequences.
                    r2, _, _ = select.select([sys.stdin], [], [], 0.01)
                    if r2:
                        seq = os.read(fd, 2)
                        if seq == b"[A":          # ↑
                            self._scroll_up()
                        elif seq == b"[B":        # ↓
                            self._scroll_down()
                        elif seq == b"[5":        # Page Up
                            self._scroll_up(lines=5)
                        elif seq == b"[6":        # Page Down
                            self._scroll_down(lines=5)
                elif ch in (b"\r", b"\n", b"q", b"Q"):
                    break
                elif ch == b"\x03":  # Ctrl+C
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ---------------------------------------------------------------------------
# Non-streaming implementation
# ---------------------------------------------------------------------------


class NonStreamingRenderer(Renderer):
    """One-shot renderer that prints output directly via
    :meth:`~rich.console.Console.print`.
    """

    # ------------------------------------------------------------------
    # Streaming event handlers (one-shot)
    # ------------------------------------------------------------------

    def on_text_delta(self, event: TextDelta) -> None:
        """Print the text delta as markdown immediately."""
        self.markdown(event.text)

    def on_tool_call_start(self, event: ToolCallStart) -> None:
        """Announce the tool call with a one-line print."""
        label = _format_tool_call(
            event.tool_name, event.partial_input or {},
        )
        self._console.print(Text(f"▶ {label}", style=_TOOL_RUNNING))

    def on_tool_call_delta(self, event: ToolCallDelta) -> None:
        """No-op — deltas only arrive during streaming LLM calls."""

    def on_tool_call_end(self, event: ToolCallEnd) -> None:
        """Print the tool result (or error) inline."""
        result = event.result

        if result is None:
            self._console.print(
                Text(
                    f"  ⚠ {event.tool_name} — no result",
                    style=_TOOL_ERROR,
                ),
            )
            return

        if result.success:
            output_preview = _truncate(
                result.output, max_lines=5, max_chars=300,
            )
            self._console.print(Text("  ✓ Done", style=_TOOL_SUCCESS))
            if output_preview.strip():
                self._console.print(
                    Text(output_preview, style=_STATUS_MUTED),
                )
        else:
            err_text = result.error or "Unknown error"
            self._console.print(
                Text(
                    f"  ✗ {_truncate(err_text, max_lines=3, max_chars=200)}",
                    style=_TOOL_ERROR,
                ),
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_renderer(
    *,
    streaming: bool = False,
    console: Console | None = None,
    refresh_per_second: float = 10.0,
    max_output_lines: int = 40,
    max_output_panel_height: int = 0,
) -> Renderer:
    """Create a :class:`Renderer` for the given output mode.

    Parameters
    ----------
    streaming:
        When ``True``, return a :class:`StreamingRenderer` with Rich Live
        dual-panel output.  When ``False`` (default), return a
        :class:`NonStreamingRenderer` with one-shot printing.
    console:
        A Rich Console instance.  When *None*, a default ``stderr``
        console is created.
    refresh_per_second:
        Max refresh rate for the streaming Live display (default 10).
        Only used when *streaming* is ``True``.
    max_output_lines:
        Max lines of output before truncating (0 to disable, default 40).
        Only used when *streaming* is ``True``.
    max_output_panel_height:
        Max height of the output panel in lines.  When > 0 and the
        rendered markdown exceeds this height, the live display clips
        to show only the latest content and arrow-key scrolling is
        enabled during dismiss.  Set to 0 to disable (default 0).
        Only used when *streaming* is ``True``.
    """
    if streaming:
        return StreamingRenderer(
            console=console,
            refresh_per_second=refresh_per_second,
            max_output_lines=max_output_lines,
            max_output_panel_height=max_output_panel_height,
        )
    return NonStreamingRenderer(console=console)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_tool_table_summary(name: str, params: dict) -> str:
    """Format a tool name + key params for the streaming tools table."""
    if not params and not name:
        return "…"

    parts: list[str] = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")

    label = name if name else ""
    args = ", ".join(parts[:2])
    if args:
        return f"{label}({args})" if label else args
    return label


def _truncate_tool_result(result: ToolResult | None) -> str:
    """Return a short preview of a ToolResult for the tools table."""
    if result is None:
        return ""
    text = result.output if result.success else (result.error or "Error")
    text = text.replace("\n", " ").strip()
    if len(text) > 60:
        text = text[:57] + "..."
    return text


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
    short: dict[str, str] = {}
    for k, v in params.items():
        s = str(v)
        if isinstance(v, str) and len(s) > 60:
            s = s[:57] + "..."
        short[k] = s

    args = ", ".join(f"{k}={s!r}" for k, s in short.items())
    return f"{name}({args})"
