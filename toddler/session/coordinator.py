"""SessionCoordinator — owns the lifecycle of a session.

Wires together the Agent, Context, Tools, and Storage layers so the CLI
layer only needs to talk to ONE object instead of directly importing from
six packages.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from toddler.agent.events import AgentEvent, AgentFinished
from toddler.agent.loop import AgentLoop
from toddler.agent.planner import Planner
from toddler.agent.state_machine import AgentMode, AgentStateMachine
from toddler.checkpoint import create_checkpoint_callback
from toddler.checkpoint.manager import CheckpointManager
from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    RollbackResult,
)
from toddler.config.settings import Settings
from toddler.context.manager import ContextManager
from toddler.llm import BaseLLMProvider, Message, TokenUsage
from toddler.session.manager import StorageManager
from toddler.session.models import Conversation, Session
from toddler.tools import create_default_registry
from toddler.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)

# ======================================================================
# SessionCoordinator
# ======================================================================


class SessionCoordinator:
    """Owns the lifecycle of a session — wires Agent, Context, and Storage.

    The CLI talks ONLY to this object.  It creates and manages:

    - ToolRegistry + ToolExecutor
    - ContextManager (+ its internal sub-components)
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
    repo_root:
        Absolute path to the working directory.
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
        repo_root: Path | None = None,
        state_machine: AgentStateMachine | None = None,
    ) -> None:
        self._settings = settings
        self._storage_mgr = storage_manager
        self._llm = llm
        self._repo_root = repo_root or Path.cwd()

        # Build checkpointing (session-scoped, set_session called later)
        self._ckpt_mgr = CheckpointManager(
            storage_mgr=storage_manager,
            repo_root=self._repo_root,
        )

        # Build tool system
        self._registry = create_default_registry()
        self._executor = ToolExecutor(
            self._registry,
            checkpoint_cb=create_checkpoint_callback(
                ckpt_manager=self._ckpt_mgr,
            ),
        )

        # State machine
        self._sm = state_machine or AgentStateMachine()

        # Planner instance for the current turn (created in process_turn).
        self._planner: Planner | None = None

        # Current session + context (set via resolve())
        self._session: Session | None = None
        self._conv: Conversation | None = None
        self._ctx: ContextManager | None = None
        self._agent_impl: AgentLoop | None = None

        # Persistence tracking (coordinator owns this, not the context).
        self._base_seq: int = 0

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
        self._session = self._storage_mgr.get_or_create(session_id)

        self._ctx = ContextManager(
            self._settings,
            self._llm,
            project_root=self._repo_root,
            memory_dir=self._settings.session_dir,
        )
        self._conv = self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._activate_context()

        if self._ckpt_mgr is not None:
            self._ckpt_mgr.set_session(self._session.id)
        logger.info(f"Session resolved: {self._session.id[:12]}...")
        return self._session

    @property
    def session(self) -> Session | None:
        """The current session, or *None* if :meth:`resolve` hasn't been called."""  # noqa: E501
        return self._session

    @property
    def context(self) -> ContextManager | None:
        """The current conversation context, or *None* before :meth:`resolve`."""  # noqa: E501
        return self._ctx

    @property
    def conversation(self) -> Conversation | None:
        """The current conversation model, or *None* before :meth:`resolve`."""
        return self._conv

    @property
    def state_machine(self) -> AgentStateMachine:
        """The agent state machine (exposed for slash-command dispatch)."""
        return self._sm

    @property
    def mode_label(self) -> str:
        """User-facing mode label for the REPL header.

        Returns ``"PLAN"`` when in any plan-related mode or when
        ``/plan`` has been entered but not yet consumed by a turn.
        Otherwise returns ``"EXECUTE"``.
        """
        if self._sm.plan_pending:
            return "PLAN"
        return self._sm.current_mode.display_label

    @property
    def context_usage_pct(self) -> int | None:
        """Current context usage as a whole-number percentage (0–100+).

        Returns *None* when the context hasn't been initialised yet
        (before :meth:`resolve`).
        """
        if self._ctx is None:
            return None
        return round(self._ctx.usage_ratio * 100)

    # ==================================================================
    # Turn execution
    # ==================================================================

    async def process_turn(
        self,
        user_input: str,
        *,
        force_plan: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Run one complete agent turn — user input through to finish.

        Classification happens here so the full state-machine lifecycle is
        visible in one place.  Simple requests go straight to execution;
        complex ones are delegated to :class:`~toddler.agent.planner.Planner`
        for the plan loop (explore → propose → wait), then executed.

        Yields :class:`AgentEvent` objects for the CLI to render.
        """
        # --- Conversation-start checkpoint (first turn only) ---
        self._create_conversation_start_checkpoint()

        # --- Auto-title new conversations ---
        if self._conv is not None and not self._conv.title:
            self._conv.title = user_input[:80]

        # --- Classify ---
        self._sm.reset()
        mode = self._sm.classify_and_transition(
            user_input, force_plan=force_plan,
        )

        # --- Collect prior conversation summaries ---
        prior_titles = self._get_prior_titles()
        if self._ctx is not None:
            self._ctx.set_cross_conversation_context(prior_titles)

        # --- Plan path ---
        if mode == AgentMode.PLAN_EXPLORING:
            async for event in self.planner.run(user_input):
                if isinstance(event, AgentFinished):
                    await self._maybe_persist_phase(event)
                yield event

            if self.planner.plan is None:
                self._sm.mark_finished()
                return

            plan_text = self.planner.plan.format_for_prompt()
            user_input = (
                "I have reviewed and approved the following plan. "
                "Execute it step by step, reporting progress after "
                f"each step:\n\n{plan_text}"
            )
            mode_hint = "plan_executing"
            self._sm.transition(AgentMode.PLAN_EXECUTING)
        else:
            mode_hint = "execute"

        # --- Execute ---
        async for event in self._run_phase(user_input, mode_hint):
            yield event
        self._sm.mark_finished()

    def approve_plan(self) -> bool:
        """Approve the current plan and unblock :meth:`process_turn`.

        Called by the CLI layer after the user confirms approval.
        Returns ``True`` if the plan was successfully approved.
        """
        if self._planner is not None:
            return self._planner.approve_plan()
        return False

    def reject_plan(self, *, feedback: str = "") -> None:
        """Reject the current plan and unblock :meth:`process_turn`.

        When *feedback* is provided the agent will re-explore and propose
        a revised plan.  Otherwise the turn finishes.
        """
        if self._planner is not None:
            self._planner.reject_plan(feedback=feedback)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_conversation_start_checkpoint(self) -> None:
        """Create a checkpoint at the start of a new conversation."""
        if (
            self._ckpt_mgr is not None
            and self._conv is not None
            and self._conv.message_count == 0
        ):
            try:
                self._ckpt_mgr.create(
                    description=(
                        "Start of conversation "
                        f"{self._conv.sequence_num}"
                    ),
                    tool_name="conversation_start",
                    agent_state=AgentStateSnapshot(
                        mode="execute", iteration=0,
                    ),
                    message_index=-1,
                )
            except Exception:
                logger.exception(
                    "Failed to create conversation-start checkpoint."
                )

    async def _activate_context(self) -> None:
        """Load messages from storage and populate the context buffer.

        The coordinator
        reads messages from the DB, builds the initial list (including any
        synthetic compaction summary), and hands it to
        :meth:`ContextManager.load`.
        """
        if self._conv is None or self._ctx is None:
            return

        after_seq = self._conv.compacted_at_seq or -1
        recent = self._storage_mgr.get_messages(
            session_id=self._conv.session_id,
            conversation_id=self._conv.id,
            after_sequence=after_seq,
        )

        self._base_seq = after_seq + 1
        initial: list[Message] = []

        if self._conv.compacted_summary:
            initial.append(
                Message.user(
                    "[Compacted history — summary of the conversation"
                    " so far]\n\n"
                    + self._conv.compacted_summary
                )
            )

        initial.extend(recent)
        self._ctx.load(initial)

        # Seed the token-count baseline from the persisted conversation row so
        # the first count_tokens() call skips a full tiktoken re-estimate.
        # Only valid when the stored count is nonzero AND was computed with
        # the current model.
        if (
            self._conv.total_tokens > 0
            and self._conv.model is not None
            and self._conv.model == self._llm.model
        ):
            self._ctx.set_token_baseline(
                total_tokens=self._conv.total_tokens,
                message_count=len(initial),
            )

    def _get_prior_titles(self) -> list[str] | None:
        """Collect titles of prior conversations in the same session."""
        if self._storage_mgr is None or self._conv is None:
            return None
        summaries = self._storage_mgr.get_conversation_summaries(
            self._conv.session_id,
            exclude_id=self._conv.id,
        )
        if not summaries:
            return None
        return [title for _, title in summaries]

    async def _run_phase(
        self, user_input: str, mode_hint: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent-loop phase and persist afterward.

        Extracts token usage from :class:`AgentFinished` events and
        accumulates it into the session totals.
        """
        stream = self._settings.streaming_enabled
        gen = self.agent.run(
            user_input,
            max_iterations=self._settings.max_iterations,
            stream=stream,
            mode=mode_hint,
        )

        usage: TokenUsage | None = None

        async for event in gen:
            if isinstance(event, AgentFinished):
                usage = event.usage
            yield event

        # Persist after the phase completes.
        await self._maybe_persist_phase(usage)

    async def _maybe_persist_phase(
        self, event_or_usage: AgentFinished | TokenUsage | None,
    ) -> None:
        """Persist token usage and context after a phase completes.

        Accepts an :class:`AgentFinished` event (used when iterating over
        Planner events) or a plain :class:`TokenUsage` (used by
        :meth:`_run_phase`).
        """
        usage: TokenUsage | None
        if isinstance(event_or_usage, AgentFinished):
            usage = event_or_usage.usage
        else:
            usage = event_or_usage

        if self._ctx is not None:
            if usage is not None and self._storage_mgr is not None:
                self._storage_mgr.accumulate_tokens(
                    self._session.id, usage,
                )
            await self.save()

    # ==================================================================
    # Conversation management
    # ==================================================================

    async def new_conversation(self, title: str | None = None) -> None:
        """Start a new conversation, archiving the current one.

        If *title* is provided and the current conversation is non-empty,
        it is set on the current conversation before archiving.

        When the current conversation has no messages it is simply reused
        (title updated in-place) rather than archived — archiving an empty
        conversation would only create junk.
        """
        if self._ctx is None:
            return

        if title and self._conv:
            self._conv.title = title.strip() or None
        await self.save()

        conv = self._conv
        if conv is not None and conv.message_count == 0:
            # Current conversation is empty — just rename it in-place
            # instead of archiving and creating a new one.
            if title:
                self._storage_mgr.update_conversation(conv)
            return

        if conv is not None:
            self._storage_mgr.archive_conversation(conv.id)

        # Always create a fresh conversation — never reuse a stale "active"
        # conversation that may have been left behind by a bug or crash.
        self._conv = self._storage_mgr.create_conversation(
            self._session.id,
        )
        await self._activate_context()

    async def resume_conversation(self, conversation_id: str) -> None:
        """Switch to an existing (usually archived) conversation.

        Archives the current conversation before switching so only one
        conversation is active at a time.

        Raises :class:`ValueError` if the conversation is not found.
        """
        if self._ctx is None:
            return

        # Archive the current conversation so only one is active at a time.
        if self._conv is not None:
            self._storage_mgr.archive_conversation(
                self._conv.id,
            )

        # Resolve by sequence number (#N) or UUID.
        if conversation_id.isdigit():
            conv = self._storage_mgr.get_conversation_by_sequence(
                self._session.id, int(conversation_id),
            )
            if conv is None:
                raise ValueError(
                    f"Conversation #{conversation_id} not found."
                )
        else:
            conv = self._storage_mgr.get_conversation(conversation_id)
            if conv is None:
                raise ValueError(
                    f"Conversation '{conversation_id[:16]}...' not found."
                )

        # Reactivate the resumed conversation.
        conv.status = "active"
        self._conv = conv
        await self._activate_context()

    async def switch_session(self, session_id: str) -> None:
        """Switch to a different session by ID.

        Raises :class:`ValueError` if the session is not found.
        """
        session = self._storage_mgr.get(session_id)
        if session is None:
            raise ValueError(
                f"Session '{session_id[:16]}...' not found."
            )

        self._session = session
        # Create new context and load active conversation.
        self._ctx = ContextManager(
            self._settings,
            self._llm,
            project_root=self._repo_root,
            memory_dir=self._settings.session_dir,
        )
        self._conv = self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._activate_context()

        # Reset the agent so it captures the new context.
        self._agent_impl = None

        if self._ckpt_mgr is not None:
            self._ckpt_mgr.set_session(self._session.id)

    # ==================================================================
    # Persistence
    # ==================================================================

    async def save(self) -> None:
        """Persist new messages and conversation metadata to the database.

        Reads new messages from the context (those added since the last
        :meth:`ContextManager.acknowledge`), persists them to the messages
        table, and updates the conversation row.  Also persists compaction
        metadata if a compaction occurred during the last turn.
        """
        if self._ctx is None or self._conv is None:
            return

        # Persist new messages.
        new_msgs = self._ctx.new_messages
        for msg in new_msgs:
            self._storage_mgr.append_message(
                self._conv.session_id,
                msg,
                conversation_id=self._conv.id,
            )
        self._ctx.acknowledge()
        self._conv.message_count += len(new_msgs)

        # Persist compaction metadata if a compaction occurred.
        cmeta = self._ctx.last_compaction
        if cmeta is not None:
            self._conv.compacted_summary = cmeta.summary
            # Compute compacted_at_seq.
            body_before = cmeta.messages_before - 1  # minus system msg
            body_after = cmeta.messages_after - 1     # minus system msg
            summarized = max(0, body_before - body_after)
            if summarized > 0:
                prev = self._conv.compacted_at_seq or self._base_seq - 1
                self._conv.compacted_at_seq = prev + summarized
            self._ctx.clear_compaction_result()

        # Snapshot the current context size so a future reload can seed the
        # baseline and skip a full tiktoken re-estimate.  The count is only
        # comparable across loads when the model is unchanged, so persist it
        # too.
        self._conv.total_tokens = self._ctx.count_tokens()
        self._conv.model = self._llm.model

        # Persist conversation metadata.
        self._storage_mgr.update_conversation(self._conv)

    async def prune_if_empty(self) -> None:
        """Delete the current session if no messages were added.

        Called on REPL exit to avoid leaving ghost sessions.
        """
        if self._session is None:
            return

        session = self._storage_mgr.get(self._session.id)
        if session is not None and session.message_count == 0:
            self._storage_mgr.delete(self._session.id)

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
    def planner(self) -> Planner:
        """Lazily build and return the plan-mode orchestrator.

        Same lazy-init pattern as :attr:`agent` — dependencies are stable
        references so the Planner is created once and reused across turns.
        """
        if self._planner is None:
            self._planner = Planner(
                llm_provider=self._llm,
                context=self._ctx,
                settings=self._settings,
                agent_loop=self.agent,
                state_machine=self._sm,
            )
        return self._planner

    @property
    def storage_manager(self) -> StorageManager:
        """The storage manager (for listing sessions, etc.)."""
        return self._storage_mgr

    # ==================================================================
    # Checkpoint delegation
    # ==================================================================

    @property
    def checkpoint_manager(self) -> CheckpointManager | None:
        """The checkpoint manager, or *None* if checkpoints are disabled."""
        return self._ckpt_mgr

    def rollback_to(self, checkpoint_id: str) -> RollbackResult:
        """Rollback to a checkpoint — delegates to :class:`CheckpointManager`.

        Raises :class:`ValueError` if checkpoints are disabled.
        """
        if self._ckpt_mgr is None:
            raise ValueError("Checkpoints are not available.")
        return self._ckpt_mgr.rollback_to(checkpoint_id)

    def list_checkpoints(self) -> list[Checkpoint]:
        """List checkpoints for the current session.

        Raises :class:`ValueError` if checkpoints are disabled.
        """
        if self._ckpt_mgr is None:
            raise ValueError("Checkpoints are not available.")
        return self._ckpt_mgr.list_for_session()
