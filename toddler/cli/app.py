"""CLI application — REPL loop and one-shot mode.

Wires together the agent loop, tool system, renderer, input handler,
and session manager to provide both an interactive REPL and a
single-invocation mode.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

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
from toddler.llm.types import Message, TokenUsage
from toddler.session.manager import SessionManager
from toddler.session.models import Session
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
    renderer, input handler, session manager), then runs either the
    interactive REPL or a one-shot query.

    Parameters
    ----------
    settings:
        Resolved settings from env vars + CLI args.
    session_manager:
        Manager for persistent sessions.  When *None* (e.g. for tests), session
        persistence is disabled.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._settings = settings
        self._session_mgr = session_manager
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

        # --- Current session (set on run) ---
        self._session: Session | None = None
        self._session_titles_scheduled: set[str] = set()

    # ==================================================================
    # Entry points
    # ==================================================================

    async def run_repl(self, *, session_id: str | None = None) -> None:
        """Start the interactive REPL loop.

        Parameters
        ----------
        session_id:
            When set, resume the session with this ID.  When *None*
            (and ``--new-session`` was not passed), a fresh session is created.
        """
        # --- Resolve or create session ---
        if self._session_mgr is not None:
            self._session = await self._session_mgr.get_or_create(
                session_id,
            )
            self._renderer.info(
                f"Session: {self._session.id[:12]}..."
            )
        else:
            self._session = None

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
        await self._prune_empty_session()

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
        # --- Resolve or create session ---
        if self._session_mgr is not None:
            self._session = await self._session_mgr.get_or_create(
                session_id,
            )
        else:
            self._session = None

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
        # --- Persist the user message ---
        user_msg = Message.user(user_input)
        if self._session is not None and self._session_mgr is not None:
            await self._session_mgr.append_message(
                self._session.id, user_msg,
            )
            # Auto-title after first user message (non-blocking).
            if self._session.id not in self._session_titles_scheduled:
                self._session_titles_scheduled.add(self._session.id)
                self._session_mgr.auto_title_background(
                    self._session.id, user_input,
                )

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

        # Collect assistant response for session persistence.
        assistant_blocks: list = []
        usage: TokenUsage | None = None

        display.start()
        try:
            async for event in gen:
                match event:
                    case TextDelta(text=text):
                        display.append_text(text)
                        # Track text for persistence.
                        from toddler.llm.types import ContentBlock

                        assistant_blocks.append(
                            ContentBlock.text_block(text)
                        )

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
                        # Persist tool call + result.
                        from toddler.llm.types import ContentBlock

                        assistant_blocks.append(
                            ContentBlock.tool_use_block(
                                event.tool_id,
                                event.tool_name,
                                event.input,
                            )
                        )
                        # Tool results are persisted separately as a tool
                        # message (see below).

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
                        usage = event.usage
                        break  # exit the loop to persist

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

        # --- Persist the assistant response ---
        if self._session is not None and self._session_mgr is not None:
            # Build the assistant message from collected blocks.
            # Merge consecutive text blocks to keep the stored form clean.
            merged = _merge_text_blocks(assistant_blocks)
            if merged:
                assistant_msg = Message.assistant(merged)
                await self._session_mgr.append_message(
                    self._session.id, assistant_msg,
                )

            # Accumulate token usage.
            if usage is not None:
                await self._session_mgr.accumulate_tokens(
                    self._session.id, usage,
                )

    # ==================================================================
    # Event processing  (non-streaming path)
    # ==================================================================

    async def _process_events(self, events: AsyncIterator[AgentEvent]) -> None:  # noqa: C901
        """Iterate through agent events and render / react to each one."""

        assistant_blocks: list = []
        usage: TokenUsage | None = None

        async for event in events:
            match event:
                case TextDelta():
                    self._renderer.text_delta(event)
                    from toddler.llm.types import ContentBlock

                    if event.text:
                        assistant_blocks.append(
                            ContentBlock.text_block(event.text)
                        )

                case ToolCallStart():
                    self._renderer.tool_call_start(event)

                case ToolCallEnd():
                    self._renderer.tool_call_end(event)
                    from toddler.llm.types import ContentBlock

                    assistant_blocks.append(
                        ContentBlock.tool_use_block(
                            event.tool_id,
                            event.tool_name,
                            event.input,
                        )
                    )

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
                    usage = event.usage
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

        # --- Persist the assistant response ---
        if self._session is not None and self._session_mgr is not None:
            merged = _merge_text_blocks(assistant_blocks)
            if merged:
                assistant_msg = Message.assistant(merged)
                await self._session_mgr.append_message(
                    self._session.id, assistant_msg,
                )

            if usage is not None:
                await self._session_mgr.accumulate_tokens(
                    self._session.id, usage,
                )

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
        args = parts[1] if len(parts) > 1 else ""

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
            await self._handle_session_command(args)
            return True

        self._renderer.error(
            f"Unknown command: {cmd}.  Type /help for available commands."
        )
        return True

    async def _handle_session_command(self, args: str) -> None:
        """Handle ``/session <subcommand>``."""
        sub = args.strip().lower()

        if sub == "info" or not sub:
            await self._show_session_info()
        elif sub == "list":
            await self._show_session_list()
        elif sub.startswith("switch "):
            target = args.strip()[7:].strip()
            await self._switch_session(target)
        else:
            self._renderer.error(
                f"Unknown /session subcommand: '{sub}'. "
                f"Available: info, list, switch <id>."
            )

    async def _show_session_info(self) -> None:
        """Display info about the current session."""
        if self._session is None:
            self._renderer.warning("No active session (persistence disabled).")
            return
        s = self._session
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

    async def _show_session_list(self) -> None:
        """List all saved sessions."""
        if self._session_mgr is None:
            self._renderer.warning("Session persistence is disabled.")
            return
        sessions = await self._session_mgr.list_all()
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

    async def _switch_session(self, target_id: str) -> None:
        """Switch to a different session by ID."""
        if self._session_mgr is None:
            self._renderer.warning("Session persistence is disabled.")
            return

        session = await self._session_mgr.get(target_id)
        if session is None:
            self._renderer.error(f"Session '{target_id[:16]}...' not found.")
            return

        self._session = session
        self._renderer.info(
            f"Switched to session {session.id[:12]}... "
            f"({session.message_count} messages)"
        )

    async def _prune_empty_session(self) -> None:
        """Delete the current session if no messages were added.

        Called on REPL exit to avoid leaving ghost sessions when the user
        enters and quits without sending any real messages.
        """
        if self._session is None or self._session_mgr is None:
            return

        # Re-fetch to get the authoritative message count.
        session = await self._session_mgr.get(self._session.id)
        if session is not None and session.message_count == 0:
            await self._session_mgr.delete(self._session.id)

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
| `/rollback <id>` | Rollback to a checkpoint (Phase 9) |
| `/checkpoints` | List checkpoints (Phase 9) |
| `/session info` | Show current session info |
| `/session list` | List all saved sessions |
| `/session switch <id>` | Switch to another session |
| `/help` | Show this help |
| `/clear` | Clear the screen |
| `/quit`, `/exit` | Exit the REPL |
""")

    def _repl_toolbar(self) -> str:
        """Build the bottom toolbar string."""
        return (
            " Alt+Enter: newline │ Ctrl+D: exit │ "
            "/plan /session /help /quit "
        )

    # ==================================================================
    # Property-style access for agent
    # ==================================================================

    @property
    def _agent(self) -> AgentLoop:
        """Lazily build the agent loop (needs the LLM provider)."""
        if not hasattr(self, '_agent_impl'):
            self._agent_impl = AgentLoop(
                llm_provider=self._llm,
                tool_registry=self._registry,
                tool_executor=self._executor,
                settings=self._settings,
            )
        return self._agent_impl


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


def _merge_text_blocks(
    blocks: list,
) -> list:
    """Merge consecutive text blocks into one to keep stored forms clean.

    Tool use blocks are left in place — only adjacent text blocks are
    coalesced.
    """
    from toddler.llm.types import ContentBlock

    if not blocks:
        return []

    merged: list[ContentBlock] = []
    buf: list[str] = []

    def _flush_buf() -> None:
        if buf:
            merged.append(ContentBlock.text_block("".join(buf)))
            buf.clear()

    for b in blocks:
        if b.type == "text" and b.text:
            buf.append(b.text)
        else:
            _flush_buf()
            merged.append(b)

    _flush_buf()
    return merged


def setup_logging(
    verbose: bool = False,
    *,
    log_dir: str | Path | None = None,
) -> None:
    """Configure logging for the Toddler package.

    Logs go to stderr at DEBUG (``--verbose``) or WARNING (default) level.
    When *log_dir* is provided, INFO-and-above messages are also written to
    ``<log_dir>/toddler.log`` so session lifecycle and errors are always
    captured on disk.
    """
    from pathlib import Path

    level = logging.DEBUG if verbose else logging.WARNING

    # Root logger captures everything; handlers control routing.
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # --- stderr handler (level varies with --verbose) ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(
        logging.Formatter("%(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(stderr_handler)

    # --- file handler (always INFO, so key events are persisted) ---
    if log_dir is not None:
        log_path = Path(log_dir).expanduser()
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            str(log_path / "toddler.log"), encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(file_handler)

    # Suppress noisy openai/httpx logs unless --verbose is set.
    if not verbose:
        for noisy in ("openai", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
