"""SessionCoordinator — owns the lifecycle of a session.

Wires together the Agent, Context, Tools, and Storage layers so the CLI
layer only needs to talk to ONE object instead of directly importing from
six packages.
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
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.loop import AgentLoop
from toddler.agent.state_machine import AgentMode, AgentStateMachine
from toddler.checkpoint import create_checkpoint_callback
from toddler.checkpoint.manager import CheckpointManager
from toddler.config.settings import Settings
from toddler.context.conversation_context import ConversationContext
from toddler.context.system_prompt import SystemPromptBuilder
from toddler.llm.base import BaseLLMProvider
from toddler.llm.types import ContentBlock, Message, TokenUsage
from toddler.session.manager import StorageManager
from toddler.session.models import Conversation, Session
from toddler.tools import create_default_registry
from toddler.tools.executor import ToolExecutor, always_approve

if TYPE_CHECKING:
    from toddler.context.compaction import ConversationCompactor
    from toddler.context.memory import PersistentMemory
    from toddler.context.project_map import ProjectMapper
    from toddler.context.window import ContextWindowManager
    from toddler.session.store import SQLiteStore

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


# ======================================================================
# SessionCoordinator
# ======================================================================


class SessionCoordinator:
    """Owns the lifecycle of a session — wires Agent, Context, and Storage.

    The CLI talks ONLY to this object.  It creates and manages:

    - ToolRegistry + ToolExecutor
    - ConversationContext + SystemPromptBuilder
    - AgentLoop (lazily)
    - CheckpointManager (deferred until session resolution)

    Parameters
    ----------
    settings:
        Resolved settings from env vars + CLI args.
    storage_manager:
        Manager for persistent sessions.
    llm:
        LLM provider shared with the agent loop and auto-titling.
    store:
        SQLite store (already opened) used to persist checkpoints.
    repo_root:
        Absolute path to the working directory.
    project_mapper:
        Optional :class:`ProjectMapper` for structural codebase overview
        in the system prompt.
    persistent_memory:
        Optional :class:`PersistentMemory` for user preferences that
        survive across sessions.
    context_window_mgr:
        Optional :class:`ContextWindowManager` for token tracking and
        compaction/truncation triggers.
    conversation_compactor:
        Optional :class:`ConversationCompactor` for LLM summarisation
        of old conversation turns.
    state_machine:
        Optional :class:`AgentStateMachine` for plan-mode workflow.
        When *None*, a default instance is created.
    """

    def __init__(
        self,
        settings: Settings,
        storage_manager: StorageManager,
        llm: BaseLLMProvider,
        *,
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
        self._llm = llm
        self._store = store
        self._repo_root = repo_root or Path.cwd()

        # --- Build tool system ---
        self._registry = create_default_registry()
        self._executor = ToolExecutor(
            self._registry,
            self._settings,
            confirm_cb=always_approve,
        )

        # --- Context management components ---
        self._project_mapper = project_mapper
        self._persistent_memory = persistent_memory
        self._context_window_mgr = context_window_mgr
        self._conversation_compactor = conversation_compactor

        # --- Pre-build SystemPromptBuilder ---
        self._prompt_builder = SystemPromptBuilder(
            project_mapper=project_mapper,
            persistent_memory=persistent_memory,
        )

        # --- State machine ---
        self._sm = state_machine or AgentStateMachine()

        # --- Current session + context (set via resolve()) ---
        self._session: Session | None = None
        self._ctx: ConversationContext | None = None
        self._agent_impl: AgentLoop | None = None

    # ==================================================================
    # Session lifecycle
    # ==================================================================

    async def resolve(self, session_id: str | None = None) -> Session:
        """Resolve or create a session and activate its conversation context.

        Must be called once before :meth:`process_turn`.  After this returns,
        :attr:`session` and :attr:`context` are ready for use.

        Parameters
        ----------
        session_id:
            When set, resume the session with this ID.  When *None*,
            a fresh session is created.
        """
        self._session = await self._storage_mgr.get_or_create(session_id)

        self._ctx = ConversationContext(
            self._storage_mgr,
            self._prompt_builder,
            window_mgr=self._context_window_mgr,
            compactor=self._conversation_compactor,
        )
        conv = await self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)

        self._wire_checkpointing()
        logger.info(f"Session resolved: {self._session.id[:12]}...")
        return self._session

    @property
    def session(self) -> Session | None:
        """The current session, or *None* if :meth:`resolve` hasn't been called."""
        return self._session

    @property
    def context(self) -> ConversationContext | None:
        """The current conversation context, or *None* before :meth:`resolve`."""
        return self._ctx

    @property
    def state_machine(self) -> AgentStateMachine:
        """The agent state machine (exposed for slash-command dispatch)."""
        return self._sm

    # ==================================================================
    # Turn execution
    # ==================================================================

    async def process_turn(  # noqa: C901
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Run one complete agent turn — user input through to finish.

        Yields :class:`AgentEvent` objects for the CLI to render.  Handles
        state-machine classification, agent execution, and post-turn
        persistence internally.

        Parameters
        ----------
        user_input:
            The user's message text.
        force_plan:
            Force plan mode regardless of state-machine classification.
        """
        # --- Classify and transition ---
        self._sm.reset()
        mode = self._sm.classify_and_transition(
            user_input,
            force_plan=force_plan,
        )

        mode_hint = self._sm.get_mode_hint()

        stream = self._settings.streaming_enabled
        gen = self._agent.run(
            user_input,
            max_iterations=self._settings.max_iterations,
            stream=stream,
            mode=mode_hint,
        )

        # Collect assistant response for persistence.
        assistant_blocks: list[ContentBlock] = []
        usage: TokenUsage | None = None

        async for event in gen:
            match event:
                case TextDelta(text=text) if text:
                    assistant_blocks.append(ContentBlock.text_block(text))

                case ToolCallEnd():
                    assistant_blocks.append(
                        ContentBlock.tool_use_block(
                            event.tool_id,
                            event.tool_name,
                            event.input,
                        )
                    )

                case AgentFinished():
                    usage = event.usage

                case _:
                    pass

            yield event

        # --- Persist the assistant response ---
        if self._ctx is not None and assistant_blocks:
            merged = _merge_text_blocks(assistant_blocks)
            if merged:
                assistant_msg = Message.assistant(merged)
                self._ctx.append(assistant_msg)

            if usage is not None and self._storage_mgr is not None:
                await self._storage_mgr.accumulate_tokens(
                    self._session.id, usage,
                )

        # --- Persist deltas after the turn completes ---
        if self._ctx is not None:
            await self._ctx.save()

    # ==================================================================
    # Conversation management
    # ==================================================================

    async def new_conversation(self, title: str | None = None) -> None:
        """Start a new conversation, archiving the current one.

        If *title* is provided, it is set on the current conversation
        before archiving.
        """
        if self._ctx is None:
            return

        if title:
            self._ctx.set_title(title)
        await self._ctx.save()

        if self._ctx.conversation is not None:
            await self._storage_mgr.archive_conversation(
                self._ctx.conversation.id,
            )

        conv = await self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)

    async def resume_conversation(self, conversation_id: str) -> None:
        """Switch to an existing (usually archived) conversation.

        Raises :class:`ValueError` if the conversation is not found.
        """
        if self._ctx is None:
            return

        conv = await self._storage_mgr.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(
                f"Conversation '{conversation_id[:16]}...' not found."
            )
        await self._ctx.activate(conv)

    async def switch_session(self, session_id: str) -> None:
        """Switch to a different session by ID.

        Raises :class:`ValueError` if the session is not found.
        """
        session = await self._storage_mgr.get(session_id)
        if session is None:
            raise ValueError(
                f"Session '{session_id[:16]}...' not found."
            )

        self._session = session
        # Re-resolve: create new context and activate active conversation.
        self._ctx = ConversationContext(
            self._storage_mgr,
            self._prompt_builder,
            window_mgr=self._context_window_mgr,
            compactor=self._conversation_compactor,
        )
        conv = await self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)
        self._wire_checkpointing()

    # ==================================================================
    # Persistence
    # ==================================================================

    async def save(self) -> None:
        """Persist the current conversation context to the database."""
        if self._ctx is not None:
            await self._ctx.save()

    async def prune_if_empty(self) -> None:
        """Delete the current session if no messages were added.

        Called on REPL exit to avoid leaving ghost sessions.
        """
        if self._session is None:
            return

        session = await self._storage_mgr.get(self._session.id)
        if session is not None and session.message_count == 0:
            await self._storage_mgr.delete(self._session.id)

    # ==================================================================
    # Accessors
    # ==================================================================

    @property
    def agent(self) -> AgentLoop:
        """Lazily build and return the agent loop.

        ``self._ctx`` is a stable reference — once created it never changes
        (activate() swaps the conversation inside it, not the instance
        itself).  So AgentLoop can safely capture it in ``__init__``.
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

    @property
    def storage_manager(self) -> StorageManager:
        """The storage manager (for listing sessions, etc.)."""
        return self._storage_mgr

    # ==================================================================
    # Auto-titling
    # ==================================================================

    def auto_title_background(
        self,
        session_id: str,
        first_user_message: str,
    ) -> None:
        """Launch a non-blocking background task to generate a session title.

        Call this **after** the first user message has been appended.
        """
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
    # Checkpoint wiring (deferred until session resolution)
    # ==================================================================

    def _wire_checkpointing(self) -> None:
        """Create the session-scoped :class:`CheckpointManager` and wire it
        into the :class:`ToolExecutor`.

        Must be called after ``self._session`` has been set.
        """
        if self._session is None or self._store is None:
            return

        ckpt_mgr = CheckpointManager(
            store=self._store,
            session_id=self._session.id,
            repo_root=self._repo_root,
            storage_manager=self._storage_mgr,
        )

        def _provider() -> CheckpointManager | None:
            return ckpt_mgr

        self._executor.set_checkpoint_cb(
            create_checkpoint_callback(ckpt_provider=_provider),
        )
        # Stash the provider for CLI's SlashCommandDispatcher.
        self._ckpt_provider = _provider

    @property
    def checkpoint_manager_provider(self):
        """Return the checkpoint manager provider, or *None* if not wired."""
        return getattr(self, "_ckpt_provider", None)


# ---------------------------------------------------------------------------
# Text block merging
# ---------------------------------------------------------------------------


def _merge_text_blocks(
    blocks: list[ContentBlock],
) -> list[ContentBlock]:
    """Merge consecutive text blocks into one to keep stored forms clean.

    Tool use blocks are left in place — only adjacent text blocks are
    coalesced.
    """
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
