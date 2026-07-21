"""CLI application — REPL loop and one-shot mode.

A thin display+input layer that delegates all business logic to
:class:`~toddler.session.coordinator.SessionCoordinator`.
"""

from __future__ import annotations

import logging
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
from toddler.cli.commands import (
    HELP_TEXT,
    SlashCommandDispatcher,
)
from toddler.cli.display import StreamDisplay
from toddler.cli.input_handler import InputHandler
from toddler.cli.renderer import Renderer
from toddler.config.settings import Settings
from toddler.session.coordinator import SessionCoordinator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLIApp
# ---------------------------------------------------------------------------


class CLIApp:
    """Thin CLI layer — REPL loop, display, input, slash commands.

    All agent execution, session lifecycle, tool wiring, and context
    management are delegated to :class:`SessionCoordinator`.

    Parameters
    ----------
    settings:
        Resolved settings from env vars + CLI args.
    session:
        The session coordinator that owns all agent/context/tools wiring.
    """

    def __init__(
        self,
        settings: Settings,
        session: SessionCoordinator,
    ) -> None:
        self._settings = settings
        self._coordinator = session
        self._renderer = Renderer()
        self._input = InputHandler()

        # Slash-command dispatcher still uses sentinel strings until
        # Step 5, where it switches to direct SessionCoordinator calls.
        self._cmd_dispatcher = SlashCommandDispatcher(
            state_machine=session.state_machine,
            storage_manager=session.storage_manager,
        )

    # ==================================================================
    # Entry points
    # ==================================================================

    async def run_repl(self, *, session_id: str | None = None) -> None:
        """Start the interactive REPL loop.

        Parameters
        ----------
        session_id:
            When set, resume the session with this ID.  When *None*,
            a fresh session is created.
        """
        await self._coordinator.resolve(session_id)
        self._renderer.info(
            f"Session: {self._coordinator.session.id[:12]}..."
        )

        # Wire checkpoint provider to the slash-command dispatcher.
        self._cmd_dispatcher.set_checkpoint_manager_provider(
            self._coordinator.checkpoint_manager_provider,
        )

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

        # --- Clean up empty session on exit ---
        await self._coordinator.prune_if_empty()

    async def run_one_shot(
        self,
        query: str,
        *,
        force_plan: bool = False,
        session_id: str | None = None,
    ) -> None:
        """Run a single agent invocation and exit.

        When a session manager is available, the turn is persisted so the
        interaction can be resumed later via ``--session``.
        """
        await self._coordinator.resolve(session_id)
        self._cmd_dispatcher.set_checkpoint_manager_provider(
            self._coordinator.checkpoint_manager_provider,
        )

        await self._run_agent_turn(query, force_plan=force_plan)

        # Persist after one-shot turn.
        await self._coordinator.save()

    # ==================================================================
    # Agent turn
    # ==================================================================

    async def _run_agent_turn(
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> None:
        """Run one complete agent turn — user input through to finish.

        Delegates turn execution to SessionCoordinator and handles
        display (streaming or non-streaming).
        """
        stream = self._settings.streaming_enabled
        if stream:
            await self._run_streaming_turn(user_input, force_plan=force_plan)
        else:
            gen = self._coordinator.process_turn(
                user_input, force_plan=force_plan,
            )
            await self._process_events(gen)

    # ==================================================================
    # Streaming turn
    # ==================================================================

    async def _run_streaming_turn(  # noqa: C901
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> None:
        """Run an agent turn with real-time Rich Live display."""
        display = StreamDisplay(
            self._renderer.console,
            refresh_per_second=10,
        )

        gen = self._coordinator.process_turn(
            user_input, force_plan=force_plan,
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
                        display.tool_started(
                            event.tool_id,
                            "",
                            summary=_format_tool_summary(
                                "", event.input_delta,
                            ),
                        )

                    case ToolCallEnd():
                        result_info = event.result
                        success = result_info.success if result_info else False
                        summary = (
                            _truncate_result(result_info)
                            if result_info else ""
                        )
                        display.tool_completed(
                            event.tool_id,
                            success=success,
                            summary=summary,
                        )

                    case AgentPaused():
                        display.stop()
                        self._renderer.agent_paused(event)
                        approved = await self._confirm(event)
                        if approved:
                            await self._coordinator.agent.approve_tool_call()
                        else:
                            await self._coordinator.agent.deny_tool_call()
                        display.start()

                    case AgentFinished():
                        display.stop()
                        self._renderer.agent_finished(event)

                    case AgentError():
                        display.stop()
                        self._renderer.agent_error(event)
                        if not event.recoverable:
                            return
                        display.start()

                    case PlanProposed():
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
    # Event processing (non-streaming path)
    # ==================================================================

    async def _process_events(  # noqa: C901
        self, events: AsyncIterator[AgentEvent],
    ) -> None:
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
                    approved = await self._confirm(event)
                    if approved:
                        await self._coordinator.agent.approve_tool_call()
                    else:
                        await self._coordinator.agent.deny_tool_call()

                case AgentFinished():
                    self._renderer.agent_finished(event)
                    break

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

        Delegates to :class:`SlashCommandDispatcher` for parsing and
        execution.  Returns ``True`` to continue the REPL, ``False`` to exit.
        """
        result = await self._cmd_dispatcher.dispatch(text)

        # --- Handle sentinel messages ---
        if result.message == "__CLEAR__":
            self._renderer.console.clear()
            return True
        if result.message == "__HELP__":
            self._print_help()
            return True
        if result.message == "__SESSION_INFO__":
            await self._show_session_info()
            return True
        if result.message == "__LIST_CONVERSATIONS__":
            await self._show_conversations()
            return True
        if result.message and result.message.startswith(
            "__SESSION_SWITCH__:"
        ):
            target_id = result.message.split(":", 1)[1]
            try:
                await self._coordinator.switch_session(target_id)
                self._renderer.info(
                    f"Switched to session "
                    f"{self._coordinator.session.id[:12]}... "
                    f"({self._coordinator.session.message_count} messages)"
                )
            except ValueError as exc:
                self._renderer.error(str(exc))
            return True
        if result.message and result.message.startswith(
            "__NEW_CONVERSATION__"
        ):
            title_part = result.message.split(":", 1)[1] if ":" in result.message else ""  # noqa: E501
            await self._coordinator.new_conversation(title_part or None)
            self._renderer.info(
                "Started new conversation. "
                "Your previous conversation was archived."
            )
            return True
        if result.message and result.message.startswith(
            "__RESUME_CONVERSATION__:"
        ):
            target_id = result.message.split(":", 1)[1]
            try:
                await self._coordinator.resume_conversation(target_id)
                self._renderer.info(
                    f"Resumed conversation."
                )
            except ValueError as exc:
                self._renderer.error(str(exc))
            return True

        # --- Display message if not already rendered ---
        if result.message and not result.rendered:
            self._renderer.info(result.message)

        return result.continue_repl

    # ==================================================================
    # Session / conversation display helpers
    # ==================================================================

    async def _show_session_info(self) -> None:
        """Display info about the current session."""
        s = self._coordinator.session
        if s is None:
            self._renderer.warning("No active session (persistence disabled).")
            return
        self._renderer.console.print()
        self._renderer.markdown(f"""\
### Session Info

| Field | Value |
|-------|-------|
| **ID** | `{s.id}` |
| **Title** | {s.title or '—'} |
| **Mode** | {s.mode} |
| **Messages** | {s.message_count} |
| **Input tokens** | {s.total_input_tokens} |
| **Output tokens** | {s.total_output_tokens} |
| **Created** | {s.created_at.strftime('%Y-%m-%d %H:%M UTC')} |
""")

    async def _show_conversations(self) -> None:
        """List all conversations for the current session."""
        session = self._coordinator.session
        ctx = self._coordinator.context
        mgr = self._coordinator.storage_manager
        if session is None:
            self._renderer.warning("Session persistence is disabled.")
            return

        convs = await mgr.list_conversations(session.id)
        if not convs:
            self._renderer.info("No conversations in this session.")
            return

        active_id = (
            ctx.conversation.id
            if ctx and ctx.conversation
            else None
        )

        self._renderer.console.print()
        lines = [
            f"| {'':>1} | {'ID':<34} | {'Title':<40} | {'Msgs':>5} | {'Age':<10} |",  # noqa: E501
            f"|{'-'*3}|{'-'*36}|{'-'*42}|{'-'*7}|{'-'*12}|",
        ]
        for c in convs:
            marker = "*" if c.id == active_id else " "
            sid = c.id[:32]
            title = (c.display_title or "—")[:39]
            lines.append(
                f"| {marker:<1} | {sid:<34} | {title:<40} | {c.message_count:>5} | {c.age:<10} |"  # noqa: E501
            )
        self._renderer.markdown("\n".join(lines))
        self._renderer.info("* = active conversation")

    async def _show_session_list(self) -> None:
        """List all saved sessions."""
        mgr = self._coordinator.storage_manager
        sessions = await mgr.list_all()
        if not sessions:
            self._renderer.info("No saved sessions.")
            return

        self._renderer.console.print()
        lines = [
            f"| {'ID':<34} | {'Title':<40} | {'Msgs':>5} | {'Age':<10} |",
            f"|{'-'*36}|{'-'*42}|{'-'*7}|{'-'*12}|",
        ]
        for s in sessions:
            sid = s.id[:32]
            title = (s.display_title or "—")[:39]
            lines.append(
                f"| {sid:<34} | {title:<40} | {s.message_count:>5} | {s.age:<10} |"  # noqa: E501
            )
        self._renderer.markdown("\n".join(lines))

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
        self._renderer.markdown(HELP_TEXT)

    def _repl_toolbar(self) -> str:
        """Build the bottom toolbar string."""
        return (
            " Alt+Enter: newline │ Ctrl+D: exit │ "
            "/plan /clear /resume /conversations /session /help /quit "
        )


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------


def _format_tool_summary(name: str, params: dict) -> str:
    """Format a tool name + key parameters for compact display."""
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


def _truncate_result(result) -> str:
    """Return a short preview of a tool result for the status table."""
    if result is None:
        return ""
    text = result.output if result.success else (result.error or "Error")
    text = text.replace("\n", " ").strip()
    if len(text) > 60:
        text = text[:57] + "..."
    return text
