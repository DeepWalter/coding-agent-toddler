"""CLI application — REPL loop and one-shot mode.

Wires together the agent loop, tool system, renderer, and input handler to
provide both an interactive REPL and a single-invocation mode.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    AgentPaused,
    PlanProposed,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.loop import AgentLoop
from toddler.cli.display import StreamDisplay
from toddler.cli.input_handler import InputHandler
from toddler.cli.renderer import Renderer
from toddler.config.settings import Settings
from toddler.llm.provider import OpenAICompatibleProvider
from toddler.tools import create_default_registry
from toddler.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tod`` CLI."""
    p = argparse.ArgumentParser(
        prog="tod",
        description="Toddler — a personal Python CLI coding agent",
    )
    p.add_argument(
        "query",
        nargs="*",
        help="Task to perform.  Omit to enter the interactive REPL.",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="Force plan mode — agent researches before making changes.",
    )
    p.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="Resume a previous session by its ID.",
    )
    p.add_argument(
        "--new-session",
        action="store_true",
        help="Start a new session (don't reuse the last one).",
    )
    p.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output.",
    )
    p.add_argument(
        "--list-sessions",
        action="store_true",
        help="List saved sessions and exit.",
    )
    p.add_argument(
        "--model",
        metavar="MODEL",
        default=None,
        help="Override the LLM model name.",
    )
    p.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="Override the API base URL.",
    )
    p.add_argument(
        "--api-key",
        metavar="KEY",
        default=None,
        help="Override the API key.",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        metavar="N",
        default=None,
        help="Override the maximum number of agent loop iterations.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return p


# ---------------------------------------------------------------------------
# CLIApp
# ---------------------------------------------------------------------------


class CLIApp:
    """Top-level CLI application.

    Creates and wires all components (tools, executor, provider, agent loop,
    renderer, input handler), then runs either the interactive REPL or a
    one-shot query.

    Parameters
    ----------
    settings:
        Resolved settings from env vars + CLI args.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._renderer = Renderer()
        self._input = InputHandler()

        # --- Build tool system ---
        self._registry = create_default_registry()

        self._executor = ToolExecutor(
            self._registry,
            self._settings,
        )

        # --- Build LLM provider ---
        self._llm = OpenAICompatibleProvider(self._settings)

        # --- Build agent loop ---
        self._agent = AgentLoop(
            llm_provider=self._llm,
            tool_registry=self._registry,
            tool_executor=self._executor,
            settings=self._settings,
        )

    # ==================================================================
    # Entry points
    # ==================================================================

    async def run_repl(self) -> None:
        """Start the interactive REPL loop."""
        self._print_banner()
        self._renderer.info(
            f"Model: {self._settings.model} │ "
            f"Streaming: {'on' if self._settings.streaming_enabled else 'off'}"
        )
        self._renderer.info('Type /help for commands, /quit to exit.')

        while True:
            try:
                user_input = await self._input.prompt(
                    bottom_toolbar=self._repl_toolbar(),
                )
            except KeyboardInterrupt:
                self._renderer.info("Interrupted.  Type /quit to exit.")
                continue

            if user_input is None:
                # Ctrl+D on empty line → exit
                self._renderer.info("Goodbye.")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # --- Slash commands ---
            if user_input.startswith("/"):
                handled = await self._handle_slash_command(user_input)
                if not handled:
                    break  # /quit or /exit
                continue

            # --- Run the agent ---
            await self._run_agent_turn(user_input)

    async def run_one_shot(
        self, query: str, *, force_plan: bool = False
    ) -> None:
        """Run a single agent invocation and exit."""
        await self._run_agent_turn(query, force_plan=force_plan)

    # ==================================================================
    # Agent turn
    # ==================================================================

    async def _run_agent_turn(
        self,
        user_input: str,
        *,
        force_plan: bool = False,  # noqa: ARG002
    ) -> None:
        """Run one complete agent turn — user input through to finish."""
        stream = self._settings.streaming_enabled
        if stream:
            await self._run_streaming_turn(user_input)
        else:
            gen = self._agent.run(
                user_input,
                max_iterations=self._settings.max_iterations,
                stream=False,
            )
            await self._process_events(gen)

    # ==================================================================
    # Streaming turn (Phase 6)
    # ==================================================================

    async def _run_streaming_turn(  # noqa: C901
        self, user_input: str,
    ) -> None:
        """Run an agent turn with real-time Rich Live display."""
        display = StreamDisplay(
            self._renderer.console,
            refresh_per_second=10,
        )

        gen = self._agent.run(
            user_input,
            max_iterations=self._settings.max_iterations,
            stream=True,
        )

        display.start()
        try:
            async for event in gen:
                match event:
                    case TextDelta(text=text):
                        display.append_text(text)

                    case ToolCallStart():
                        display.tool_started(
                            event.tool_id,
                            event.tool_name,
                            summary=_format_tool_summary(
                                event.tool_name,
                                event.partial_input or {},
                            ),
                        )

                    case ToolCallDelta():
                        # Update the tool display with partial input.
                        display.tool_started(
                            event.tool_id,
                            "",  # name already known
                            summary=_format_tool_summary(
                                "", event.input_delta,
                            ),
                        )

                    case ToolCallEnd():
                        result = event.result
                        success = result.success if result else False
                        summary = (
                            _truncate_result(result)
                            if result else ""
                        )
                        display.tool_completed(
                            event.tool_id,
                            success=success,
                            summary=summary,
                        )

                    case AgentPaused():
                        # Stop live display to show confirmation prompt,
                        # then restart after user responds.
                        display.stop()
                        self._renderer.agent_paused(event)
                        approved = await self._confirm(event)
                        if approved:
                            await self._agent.approve_tool_call()
                        else:
                            await self._agent.deny_tool_call()
                        display.start()

                    case AgentFinished():
                        display.stop()
                        self._renderer.agent_finished(event)
                        return

                    case AgentError():
                        display.stop()
                        self._renderer.agent_error(event)
                        if not event.recoverable:
                            return
                        display.start()

                    case PlanProposed():
                        # Briefly stop to show the plan, then resume.
                        display.stop()
                        self._renderer.info(
                            f"Plan proposed: {event.plan.title}"
                        )
                        self._renderer.markdown(event.plan.summary)
                        display.start()

                    case _:
                        pass
        except Exception:
            display.stop()
            raise

    # ==================================================================
    # Event processing
    # ==================================================================

    async def _process_events(self, events: AsyncIterator[AgentEvent]) -> None:  # noqa: C901
        """Iterate through agent events and render / react to each one."""

        async for event in events:
            match event:
                case TextDelta():
                    self._renderer.text_delta(event)

                case ToolCallStart():
                    self._renderer.tool_call_start(event)

                case ToolCallEnd():
                    self._renderer.tool_call_end(event)

                case AgentPaused():
                    self._renderer.agent_paused(event)
                    # Prompt for confirmation inline.
                    approved = await self._confirm(event)
                    if approved:
                        await self._agent.approve_tool_call()
                    else:
                        await self._agent.deny_tool_call()

                case AgentFinished():
                    self._renderer.agent_finished(event)
                    return

                case AgentError():
                    self._renderer.agent_error(event)
                    if not event.recoverable:
                        return

                case PlanProposed():
                    self._renderer.info(
                        f"Plan proposed: {event.plan.title}"
                    )
                    self._renderer.markdown(event.plan.summary)

                case _:
                    logger.debug(f"Unhandled event: {type(event).__name__}")

    # ==================================================================
    # Confirmation prompt
    # ==================================================================

    async def _confirm(self, event: AgentPaused) -> bool:
        """Ask the user to confirm a paused action.

        The default for an empty input is "deny" (safe default).
        """
        choices = event.choices or ["y", "n"]
        # Safe default: prefer "deny" / "n" / "no", otherwise last choice.
        deny_kw = ("deny", "n", "no", "reject")
        default = next(
            (c for c in choices if c.lower() in deny_kw), choices[-1]
        )

        try:
            answer = await self._input.prompt(
                message=f"  [{'/'.join(choices)}] ({default}): ",
                bottom_toolbar=None,
            )
        except (KeyboardInterrupt, EOFError):
            return False

        if answer is None or answer.strip() == "":
            answer = default

        return (answer.strip().lower().startswith("y")
                or answer.strip().lower() in {"approve", "a"})

    # ==================================================================
    # Slash command dispatch
    # ==================================================================

    async def _handle_slash_command(self, text: str) -> bool:
        """Handle a slash command entered at the REPL.

        Returns ``True`` to continue the REPL, ``False`` to exit.
        """
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd in ("/quit", "/exit", "/q"):
            self._renderer.info("Goodbye.")
            return False

        if cmd == "/clear":
            self._renderer.console.clear()
            return True

        if cmd == "/help":
            self._print_help()
            return True

        if cmd == "/plan":
            self._renderer.info(
                "Plan mode will be triggered on your next message "
                "(use --plan for one-shot mode)."
            )
            return True

        if cmd == "/rollback":
            self._renderer.warning(
                "Rollback is not yet implemented (Phase 9 — Checkpoints)."
            )
            return True

        if cmd == "/checkpoints":
            self._renderer.warning(
                "Checkpoints are not yet implemented (Phase 9)."
            )
            return True

        if cmd == "/session":
            self._renderer.warning(
                "Session management is not yet implemented (Phase 8)."
            )
            return True

        self._renderer.error(f"Unknown command: {cmd}.  Type /help for available commands.")  # noqa: E501
        return True

    # ==================================================================
    # Display helpers
    # ==================================================================

    def _print_banner(self) -> None:
        """Print the welcome banner."""
        from rich.text import Text
        self._renderer.console.print()
        self._renderer.console.print(
            Text("🐣 Toddler", style="bold yellow"),
            Text(" — coding agent", style="dim"),
        )

    def _print_help(self) -> None:
        """Print slash-command help."""
        self._renderer.console.print()
        self._renderer.markdown("""\
### Slash Commands

| Command | Description |
|---------|-------------|
| `/plan` | Force plan mode on the next message |
| `/rollback <id>` | Rollback to a checkpoint |
| `/checkpoints` | List checkpoints |
| `/session info` | Show current session info |
| `/help` | Show this help |
| `/clear` | Clear the screen |
| `/quit`, `/exit` | Exit the REPL |
""")

    def _repl_toolbar(self) -> str:
        """Build the bottom toolbar string."""
        return (
            " Alt+Enter: newline │ Ctrl+D: exit │ "
            "/plan /help /session /quit "
        )


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------


def _format_tool_summary(name: str, params: dict) -> str:
    """Format a tool name + key parameters for compact display.

    Used by the streaming display's lower panel to show what each tool
    call is doing.
    """
    if not params and not name:
        return "…"

    parts: list[str] = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")

    label = name if name else ""
    args = ", ".join(parts[:2])  # at most 2 key-value pairs
    if args:
        return f"{label}({args})" if label else args
    return label


def _truncate_result(result) -> str:
    """Return a short preview of a tool result for the status table."""
    if result is None:
        return ""
    text = result.output if result.success else (result.error or "Error")
    text = text.replace("\n", " ").strip()
    if len(text) > 60:
        text = text[:57] + "..."
    return text


def setup_logging(verbose: bool = False) -> None:
    """Configure the logging level for the Toddler package."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    # Suppress noisy openai/httpx logs unless --verbose is set.
    if not verbose:
        for noisy in ("openai", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
