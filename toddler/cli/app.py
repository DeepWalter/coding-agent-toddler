"""CLI application — REPL loop and one-shot mode.

A thin display+input layer that delegates all business logic to
:class:`~toddler.session.coordinator.SessionCoordinator`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.markdown import Markdown

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
from toddler.cli.commands import (
    HELP_TEXT,
    SlashCommandDispatcher,
)
from toddler.cli.input_handler import InputHandler
from toddler.cli.renderer import create_renderer
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
        self._renderer = create_renderer(
            streaming=self._settings.streaming_enabled,
            max_output_lines=self._settings.max_output_lines,
            max_output_panel_height=self._settings.max_output_panel_height,
        )
        self._input = InputHandler()
        self._turn_counter = 0
        self._output_base = (
            settings.session_dir / "outputs"
        )

        # Slash-command dispatcher makes direct calls on SessionCoordinator.
        self._cmd_dispatcher = SlashCommandDispatcher(
            state_machine=session.state_machine,
            session_coordinator=session,
            output_base=self._output_base,
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

        self._renderer.banner()
        self._renderer.info(
            f"Model: {self._settings.model} │ "
            f"Streaming: {'on' if self._settings.streaming_enabled else 'off'}"
        )
        self._renderer.info('Type /help for commands, /quit to exit.')

        while True:
            try:
                user_input = await self._input.prompt()
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

        await self._run_agent_turn(query, force_plan=force_plan)

        # Persist after one-shot turn.
        await self._coordinator.save()

    # ==================================================================
    # Agent turn
    # ==================================================================

    async def _run_agent_turn(  # noqa: C901
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> None:
        """Run one complete agent turn — user input through to finish.

        Delegates turn execution to SessionCoordinator and routes every
        agent event to :class:`Renderer`, which handles streaming vs.
        non-streaming output internally.
        """
        self._turn_counter += 1
        turn_number = self._turn_counter

        # Compute output path scoped by session + conversation
        output_path: Path | None = None
        session = self._coordinator.session
        conv = self._coordinator.conversation
        if session is not None and conv is not None:
            output_path = (
                self._output_base
                / session.id[:12]
                / conv.id[:12]
                / f"turn-{turn_number:04d}.md"
            )

        self._renderer.start(
            turn_number=turn_number, output_path=output_path,
        )

        gen = self._coordinator.process_turn(
            user_input, force_plan=force_plan,
        )

        async for event in gen:
            match event:
                case TextDelta():
                    self._renderer.on_text_delta(event)

                case ToolCallStart():
                    self._renderer.on_tool_call_start(event)

                case ToolCallDelta():
                    self._renderer.on_tool_call_delta(event)

                case ToolCallEnd():
                    self._renderer.on_tool_call_end(event)

                case AgentPaused():
                    result = await self._renderer.confirm(
                        prompt=event.prompt,
                        choices=event.choices or ["approve", "deny"],
                    )
                    if result.decision == "approve":
                        await self._coordinator.agent.approve_tool_call()
                    else:
                        await self._coordinator.agent.deny_tool_call()

                case AgentFinished():
                    self._renderer.stop()
                    self._renderer.flush_to_console()
                    self._renderer.on_agent_finished(event)

                case AgentError():
                    if event.recoverable:
                        # Accumulate error for inline display during
                        # the dismiss prompt (StreamingRenderer), or
                        # print directly (NonStreamingRenderer).
                        self._renderer.on_agent_error(event)
                    else:
                        # Non-recoverable — exit alternate screen and
                        # print to the main console before returning.
                        self._renderer.pause()
                        self._renderer.on_agent_error(event)
                        return

                case PlanProposed():
                    # Explore phase already stopped the renderer.
                    # on_plan_proposed re-enters the alt screen for
                    # streaming mode and displays the plan there;
                    # for non-streaming it prints to the main console.
                    self._renderer.on_plan_proposed(event)

                    result = await self._renderer.confirm(
                        prompt="Approve this plan?",
                        choices=["approve", "deny", "feedback"],
                        allow_feedback=True,
                    )

                    match result.decision:
                        case "approve":
                            await self._coordinator.approve_plan()
                            # Reset content for execution phase.
                            self._renderer.start(
                                turn_number=turn_number,
                                output_path=output_path,
                            )
                        case "feedback":
                            await self._coordinator.reject_plan(
                                feedback=result.feedback or "",
                            )
                            if result.feedback:
                                # Feedback loop: reset content for
                                # new explore phase.
                                self._renderer.start(
                                    turn_number=turn_number,
                                    output_path=output_path,
                                )
                        case "deny":
                            await self._coordinator.reject_plan()
                            # Renderer stays stopped (or will be
                            # stopped by AgentFinished handler).

                case _:
                    pass

    # ==================================================================
    # Slash command dispatch
    # ==================================================================

    async def _handle_slash_command(self, text: str) -> bool:
        """Handle a slash command entered at the REPL.

        Delegates to :class:`SlashCommandDispatcher` for parsing and
        execution.  Returns ``True`` to continue the REPL, ``False`` to exit.
        """
        result = await self._cmd_dispatcher.dispatch(text)

        # --- Pager display (e.g. /view) ---
        if result.pager_path:
            filepath = Path(result.pager_path)
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8")
                with self._renderer.console.pager(styles=True):
                    self._renderer.console.print(Markdown(content))
            else:
                self._renderer.warning(
                    f"Output file not found: {filepath}"
                )
            return True

        # --- Display-only sentinels (commands that need CLI rendering) ---
        if result.message == "__HELP__":
            self._renderer.print()
            self._renderer.markdown(HELP_TEXT)
            return True

        # --- Display message if not already rendered ---
        if result.message and not result.rendered:
            self._renderer.info(result.message)

        return result.continue_repl
