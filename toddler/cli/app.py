"""CLI application — REPL loop and one-shot mode.

Wires together the agent loop, tool system, renderer, input handler,
and session manager to provide both an interactive REPL and a
single-invocation mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
from toddler.agent.state_machine import (
    AgentMode,
    AgentStateMachine,
)
from toddler.checkpoint import create_checkpoint_callback
from toddler.checkpoint.manager import CheckpointManager
from toddler.cli.commands import (
    HELP_TEXT,
    SlashCommandDispatcher,
)
from toddler.cli.display import StreamDisplay
from toddler.cli.input_handler import InputHandler
from toddler.cli.renderer import Renderer
from toddler.config.settings import Settings
from toddler.context.conversation_context import ConversationContext
from toddler.context.system_prompt import SystemPromptBuilder
from toddler.llm.base import BaseLLMProvider
from toddler.llm.provider import OpenAICompatibleProvider
from toddler.llm.types import Message, TokenUsage
from toddler.storage.manager import StorageManager
from toddler.storage.models import Session
from toddler.tools import create_default_registry
from toddler.tools.executor import ToolExecutor, always_approve

if TYPE_CHECKING:
    from toddler.context.compaction import ConversationCompactor
    from toddler.context.memory import PersistentMemory
    from toddler.context.project_map import ProjectMapper
    from toddler.context.window import ContextWindowManager
    from toddler.storage.store import SQLiteStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auto-titling prompt
# ---------------------------------------------------------------------------

_TITLE_PROMPT = (
    "Generate a short title (3-6 words) for a conversation that starts "
    "with this user message.  Return ONLY the title, no quotes, no "
    "explanation, no punctuation at the end.\n\n"
    "User message: {first_message}\n\n"
    "Title:"
)

# ---------------------------------------------------------------------------
# CLIApp
# ---------------------------------------------------------------------------


class CLIApp:
    """Top-level CLI application.

    Creates and wires all components (tools, executor, provider, agent loop,
    renderer, input handler, session manager), then runs either the
    interactive REPL or a one-shot query.

    Checkpointing is wired **after** session resolution (in :meth:`run_repl`
    and :meth:`run_one_shot`) because a :class:`CheckpointManager` is
    session-scoped — it needs a ``session_id`` that doesn't exist until
    runtime.

    Parameters
    ----------
    settings:
        Resolved settings from env vars + CLI args.
    storage_manager:
        Manager for persistent sessions.  When *None* (e.g. for tests),
        session persistence is disabled.
    llm:
        LLM provider shared with the session manager (for auto-titling) and
        agent loop.  When *None*, a default ``OpenAICompatibleProvider`` is
        created from *settings*.
    store:
        SQLite store (already opened) used to persist checkpoints.  When
        *None* (e.g. for tests), checkpointing is disabled.
    repo_root:
        Absolute path to the working directory (used for git-based
        snapshots).
    project_mapper:
        Optional :class:`ProjectMapper` for structural codebase overview
        in the system prompt (Phase 7).
    persistent_memory:
        Optional :class:`PersistentMemory` for user preferences that
        survive across sessions (Phase 7).
    context_window_mgr:
        Optional :class:`ContextWindowManager` for token tracking and
        compaction/truncation triggers (Phase 7).
    conversation_compactor:
        Optional :class:`ConversationCompactor` for LLM summarisation
        of old conversation turns (Phase 7).
    state_machine:
        Optional :class:`AgentStateMachine` for plan-mode workflow
        (Phase 10).  When *None*, a default instance is created.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        storage_manager: StorageManager | None = None,
        llm: BaseLLMProvider | None = None,
        store: SQLiteStore | None = None,
        repo_root: Path | None = None,
        project_mapper: ProjectMapper | None = None,
        persistent_memory: PersistentMemory | None = None,
        context_window_mgr: ContextWindowManager | None = None,
        conversation_compactor: ConversationCompactor | None = None,
        state_machine: AgentStateMachine | None = None,
    ) -> None:
        self._settings = settings
        self._storage_mgr = storage_manager
        self._store = store
        self._repo_root = repo_root
        self._renderer = Renderer()
        self._input = InputHandler()

        # --- Build tool system ---
        self._registry = create_default_registry()

        # Checkpoint callback is wired later via _wire_checkpointing(),
        # after session resolution — the CheckpointManager needs a
        # session_id.
        self._executor = ToolExecutor(
            self._registry,
            self._settings,
            confirm_cb=always_approve,
        )

        # --- Build LLM provider (or reuse the shared one) ---
        self._llm = llm or OpenAICompatibleProvider(self._settings)

        # --- Context management components (Phase 7.5) ---
        self._project_mapper = project_mapper
        self._persistent_memory = persistent_memory
        self._context_window_mgr = context_window_mgr
        self._conversation_compactor = conversation_compactor

        # --- Pre-build SystemPromptBuilder from context components ---
        self._prompt_builder = SystemPromptBuilder(
            project_mapper=project_mapper,
            persistent_memory=persistent_memory,
        )

        # --- Phase 10: state machine + command dispatcher ---
        self._sm = state_machine or AgentStateMachine()

        # Checkpoint manager provider is wired later via
        # _wire_checkpointing(), after session resolution.
        self._cmd_dispatcher = SlashCommandDispatcher(
            state_machine=self._sm,
            storage_manager=storage_manager,
        )

        # --- Current session + conversation context (set on run) ---
        self._session: Session | None = None
        self._ctx: ConversationContext | None = None
        self._agent_impl: AgentLoop | None = None

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
        if self._storage_mgr is not None:
            self._session = await self._storage_mgr.get_or_create(
                session_id,
            )
            self._renderer.info(
                f"Session: {self._session.id[:12]}..."
            )

            # Create the single ConversationContext for the REPL lifetime.
            self._ctx = ConversationContext(
                self._storage_mgr, self._prompt_builder,
                window_mgr=self._context_window_mgr,
                compactor=self._conversation_compactor,
            )
            conv = await self._storage_mgr.get_or_create_active_conversation(
                self._session.id,
            )
            await self._ctx.activate(conv)
        else:
            self._session = None
            # Create a bare context for tests / no-persistence mode.
            self._ctx = ConversationContext(
                self._storage_mgr,  # type: ignore[arg-type]
                self._prompt_builder,
                window_mgr=self._context_window_mgr,
                compactor=self._conversation_compactor,
            )

        self._wire_checkpointing()

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
        # --- Resolve or create session + conversation context ---
        if self._storage_mgr is not None:
            self._session = await self._storage_mgr.get_or_create(
                session_id,
            )
            self._ctx = ConversationContext(
                self._storage_mgr, self._prompt_builder,
                window_mgr=self._context_window_mgr,
                compactor=self._conversation_compactor,
            )
            conv = await self._storage_mgr.get_or_create_active_conversation(
                self._session.id,
            )
            await self._ctx.activate(conv)
        else:
            self._session = None
            self._ctx = ConversationContext(
                None, self._prompt_builder,
                window_mgr=self._context_window_mgr,
                compactor=self._conversation_compactor,
            )

        self._wire_checkpointing()

        await self._run_agent_turn(query, force_plan=force_plan)

        # Persist after one-shot turn.
        if self._ctx is not None:
            await self._ctx.save()

    # ==================================================================
    # Checkpoint wiring (deferred until session resolution)
    # ==================================================================

    def _wire_checkpointing(self) -> None:
        """Create the session-scoped :class:`CheckpointManager` and wire it
        into the :class:`ToolExecutor` and :class:`SlashCommandDispatcher`.

        Must be called after ``self._session`` has been set.  When there is
        no active session or the store wasn't provided, checkpointing is
        silently disabled.
        """
        if self._session is None or self._store is None:
            return

        ckpt_mgr = CheckpointManager(
            store=self._store,
            session_id=self._session.id,
            repo_root=self._repo_root or Path.cwd(),
            storage_manager=self._storage_mgr,
        )

        def _provider() -> CheckpointManager | None:
            return ckpt_mgr

        self._executor.set_checkpoint_cb(
            create_checkpoint_callback(ckpt_provider=_provider),
        )
        self._cmd_dispatcher.set_checkpoint_manager_provider(_provider)

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

        Uses the :class:`AgentStateMachine` to classify complexity and
        trigger plan mode automatically when appropriate (Phase 10).
        """
        # --- Classify and transition (Phase 10) ---
        self._sm.reset()
        mode = self._sm.classify_and_transition(
            user_input,
            force_plan=force_plan,
        )
        if mode != AgentMode.EXECUTING:
            trigger = (
                "/plan"
                if (force_plan or self._sm.plan_pending)
                else "auto-detected complexity"
            )
            self._renderer.info(f"Plan mode triggered ({trigger}).")

        # Persistence is handled by ConversationContext — no manual
        # append_message calls needed.

        mode_hint = self._sm.get_mode_hint()

        stream = self._settings.streaming_enabled
        if stream:
            await self._run_streaming_turn(
                user_input,
                mode_hint=mode_hint,
            )
        else:
            gen = self._agent.run(
                user_input,
                max_iterations=self._settings.max_iterations,
                stream=False,
                mode=mode_hint,
            )
            await self._process_events(gen)

        # --- Persist deltas after the turn completes ---
        if self._ctx is not None:
            await self._ctx.save()

    # ==================================================================
    # Streaming turn (Phase 6)
    # ==================================================================

    async def _run_streaming_turn(  # noqa: C901
        self,
        user_input: str,
        *,
        mode_hint: str = "execute",
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
            mode=mode_hint,
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

        # --- Persist the assistant response via ConversationContext ---
        if self._ctx is not None and assistant_blocks:
            merged = _merge_text_blocks(assistant_blocks)
            if merged:
                assistant_msg = Message.assistant(merged)
                self._ctx.append(assistant_msg)

            # Accumulate token usage.
            if usage is not None and self._storage_mgr is not None:
                await self._storage_mgr.accumulate_tokens(
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

        # --- Persist the assistant response via ConversationContext ---
        if self._ctx is not None and assistant_blocks:
            merged = _merge_text_blocks(assistant_blocks)
            if merged:
                assistant_msg = Message.assistant(merged)
                self._ctx.append(assistant_msg)

            if usage is not None and self._storage_mgr is not None:
                await self._storage_mgr.accumulate_tokens(
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
            await self._switch_session(target_id)
            return True
        if result.message and result.message.startswith(
            "__NEW_CONVERSATION__"
        ):
            title_part = result.message.split(":", 1)[1] if ":" in result.message else ""  # noqa: E501
            await self._clear_conversation(title_part or None)
            return True
        if result.message and result.message.startswith(
            "__RESUME_CONVERSATION__:"
        ):
            target_id = result.message.split(":", 1)[1]
            await self._resume_conversation(target_id)
            return True

        # --- Display message if not already rendered ---
        if result.message and not result.rendered:
            self._renderer.info(result.message)

        return result.continue_repl

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

    async def _clear_conversation(self, title: str | None = None) -> None:
        """Start a new conversation, archiving the current one.

        If *title* is provided, it overrides the auto-title on the current
        conversation before archiving.
        """
        if self._ctx is None or self._storage_mgr is None:
            self._renderer.info("Session persistence is disabled.")
            return

        # Set user-provided title before archiving.
        if title:
            self._ctx.set_title(title)
        await self._ctx.save()

        # Archive the current conversation.
        if self._ctx.conversation is not None:
            await self._storage_mgr.archive_conversation(
                self._ctx.conversation.id,
            )

        # Create a fresh active conversation.
        conv = await self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)
        self._renderer.info(
            "Started new conversation. "
            "Your previous conversation was archived."
        )

    async def _resume_conversation(self, conversation_id: str) -> None:
        """Switch to an existing (usually archived) conversation."""
        if self._ctx is None or self._storage_mgr is None:
            self._renderer.warning("Session persistence is disabled.")
            return

        old_conv = await self._storage_mgr.get_conversation(conversation_id)
        if old_conv is None:
            self._renderer.error(
                f"Conversation '{conversation_id[:16]}...' not found."
            )
            return

        await self._ctx.activate(old_conv)
        title = old_conv.display_title
        self._renderer.info(
            f"Resumed conversation: {title}"
        )

    async def _show_conversations(self) -> None:
        """List all conversations for the current session."""
        if self._storage_mgr is None or self._session is None:
            self._renderer.warning("Session persistence is disabled.")
            return

        convs = await self._storage_mgr.list_conversations(self._session.id)
        if not convs:
            self._renderer.info("No conversations in this session.")
            return

        active_id = (
            self._ctx.conversation.id
            if self._ctx and self._ctx.conversation
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
        if self._storage_mgr is None:
            self._renderer.warning("Session persistence is disabled.")
            return
        sessions = await self._storage_mgr.list_all()
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
        if self._storage_mgr is None:
            self._renderer.warning("Session persistence is disabled.")
            return

        session = await self._storage_mgr.get(target_id)
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
        if self._session is None or self._storage_mgr is None:
            return

        # Re-fetch to get the authoritative message count.
        session = await self._storage_mgr.get(self._session.id)
        if session is not None and session.message_count == 0:
            await self._storage_mgr.delete(self._session.id)

    # ==================================================================
    # Auto-titling
    # ==================================================================

    def _auto_title_background(
        self,
        session_id: str,
        first_user_message: str,
    ) -> None:
        """Launch a non-blocking background task to generate a session title.

        Call this **after** the first user message has been appended.  The
        title will be updated asynchronously — it's a best-effort convenience
        feature; failures are silently swallowed.
        """
        if self._llm is None:
            return

        asyncio.create_task(
            self._auto_title(session_id, first_user_message)
        )

    async def _auto_title(
        self, session_id: str, first_user_message: str,
    ) -> None:
        """Generate a title by calling the LLM, then persist it."""
        try:
            prompt = _TITLE_PROMPT.format(first_message=first_user_message)
            title = await self._llm.generate_compact(prompt)
            title = title.strip().strip('"').strip("'")
            # Enforce a reasonable max length.
            if len(title) > 100:
                title = title[:97] + "..."

            session = await self._storage_mgr.get(session_id)
            if session is None:
                return

            session.title = title if title else None
            session.updated_at = datetime.now(UTC)
            await self._storage_mgr.update(session)
            logger.info(f"Auto-titled session {session_id}: {title}")
        except Exception:
            logger.exception(
                f"Auto-title failed for session {session_id} — ignoring."
            )

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

    # ==================================================================
    # Property-style access for agent
    # ==================================================================

    @property
    def _agent(self) -> AgentLoop:
        """Lazily build the agent loop.

        ``self._ctx`` is a stable reference — once created it never
        changes (activate() swaps the conversation inside it, not the
        instance itself).  So AgentLoop can safely capture it in
        ``__init__``.
        """
        if self._agent_impl is None:
            self._agent_impl = AgentLoop(
                llm_provider=self._llm,
                tool_registry=self._registry,
                tool_executor=self._executor,
                settings=self._settings,
                context=self._ctx,
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


