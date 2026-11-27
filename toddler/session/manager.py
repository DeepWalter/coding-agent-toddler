"""SessionManager — owns the lifecycle of a session.

Wires together the Agent, Context, Tools, and Storage layers so the CLI
layer only needs to talk to ONE object instead of directly importing from
six packages.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from toddler.agent.events import (
    AgentCancelled,
    AgentEvent,
    AgentFinished,
    AgentPaused,
    ConversationTitle,
    PlanProposed,
    TodoUpdate,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.loop import AgentLoop
from toddler.agent.state_machine import AgentMode, AgentStateMachine
from toddler.checkpoint import create_checkpoint_callback
from toddler.checkpoint.manager import CheckpointManager
from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    RollbackResult,
)
from toddler.config.models import TurnConfig, resolve_slot
from toddler.config.settings import Settings
from toddler.context.manager import (
    CANCELLED_TOOL_RESULT,
    CompactionResult,
    ContextManager,
)
from toddler.llm import BaseLLMProvider, Message, TokenUsage
from toddler.session.lock import SessionLock, SessionLockManager
from toddler.session.models import MAX_TITLE_LENGTH, Conversation, Session
from toddler.session.storage import StorageManager
from toddler.tools import create_default_registry
from toddler.tools.base import ToolResult
from toddler.tools.executor import ToolExecutor
from toddler.tools.plan import (
    PLAN_CHOICE_APPROVE,
    PLAN_CHOICE_APPROVE_AUTO,
    PLAN_CHOICE_FEEDBACK,
    PLAN_CHOICE_REJECT,
    PROPOSE_PLAN_NAME,
    Plan,
    PlanState,
    PlanUpdateTool,
    PlanValidationError,
    ProposePlanTool,
)
from toddler.tools.policy import (
    FileScopePolicy,
    PermissionManager,
    PermissionMode,
)
from toddler.tools.prompts import PromptAnswer
from toddler.tools.todo import (
    TODO_TOOL_NAME,
    TodoItem,
    TodoState,
    TodoWriteTool,
)

logger = logging.getLogger(__name__)

# ======================================================================
# SessionManager
# ======================================================================


class SessionManager:
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
    """

    def __init__(
        self,
        settings: Settings,
        storage_manager: StorageManager,
        llm: BaseLLMProvider,
        *,
        repo_root: Path | None = None,
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

        # State machine — tracks plan workflow state only
        self._sm = AgentStateMachine()

        # Shared permission manager — the loop's gate reads this live
        # reference, so mode changes (via /mode or plan approval) take
        # effect immediately without rebuilding.
        self._perm_mgr = PermissionManager()

        # Shared plan step-status tracker — the plan_update tool mutates
        # it during PLAN_EXECUTING; the manager diffs it after each
        # completed tool call to emit PlanStepUpdate.  Only armed while
        # a plan runs.
        self._plan_state = PlanState()

        # Shared todo list — the todo_write tool mutates it during
        # execution phases; the manager diffs it after each completed
        # tool call to emit TodoUpdate.  Session-scoped and in memory.
        self._todo_state = TodoState()

        # Where the tools' files live — the working directory the registry
        # hands to every tool, so a tool can place the paths a call names
        # rather than judging them from wherever the process happens to be.
        self._scope_policy = FileScopePolicy(
            workspace_root=self._repo_root,
            restricted=settings.restricted_paths,
        )

        # Build tool system
        self._registry = create_default_registry(
            scope_policy=self._scope_policy
        )
        self._executor = ToolExecutor(
            self._registry,
            checkpoint_cb=create_checkpoint_callback(
                ckpt_manager=self._ckpt_mgr,
            ),
        )

        # The plan proposal awaiting the user's decision: the tool call id
        # is the plan's identity, and the parsed plan backs PlanProposed.
        self._plan_tool_id: str | None = None
        self._proposed_plan: Plan | None = None

        # Current session + context (set via resolve())
        self._session: Session | None = None
        self._conv: Conversation | None = None
        self._ctx: ContextManager | None = None
        self._agent_impl: AgentLoop | None = None

        # The model + effort this conversation runs with, resolved from its
        # row — or from the settings' slot when the row names none.  A turn
        # pins a copy of it; the selection itself is what the *next* turn
        # reads, which is why the switch methods replace it rather than
        # mutate it.
        self._selection: TurnConfig | None = None

        # Persistence tracking (manager owns this, not the context).
        self._base_seq: int = 0

        # One live process per session — the claim file under
        # ``<session_dir>/sessions`` is held for as long as this manager
        # owns the session and removed in :meth:`close`.
        self._locks = SessionLockManager(settings.session_dir / "sessions")
        self._lock: SessionLock | None = None

    # ==================================================================
    # Session lifecycle
    # ==================================================================

    async def resolve(
        self,
        session_id: str | None = None,
        *,
        new_session: bool = False,
    ) -> Session:
        """Resolve or create a session and activate its conversation context.

        Must be called once before :meth:`process_turn`.  After this returns,
        :attr:`session` and :attr:`context` are ready for use.

        Parameters
        ----------
        session_id:
            When set, resume the session with this ID.  Raises
            :class:`~toddler.session.lock.SessionInUseError` when another
            process already holds it.
        new_session:
            Start a fresh session even when the project has one to resume.
            Without a *session_id* and without this flag, the most
            recently used session started in the working directory is
            resumed when it is free; a project with no history — or whose
            last session another process holds — gets a new one.
        """
        # Re-resolving starts over; drop any claim from a previous call.
        self._release_lock()
        self._session, self._lock = self._pick_session(
            session_id, new_session=new_session,
        )

        self._ctx = ContextManager(
            self._llm,
            config=self._settings_config(),
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

    def _pick_session(
        self, session_id: str | None, *, new_session: bool,
    ) -> tuple[Session, SessionLock]:
        """Choose the session to run and claim it.

        An explicit ID is claimed as asked — a busy one raises rather than
        quietly opening something else.  Auto-resume is a preference, not
        an instruction: the project's last used session is taken when it
        is free, and a fresh session is opened when another process
        already has it.
        """
        if session_id:
            session = self._storage_mgr.get_or_create(
                session_id, cwd=str(self._repo_root),
            )
            return session, self._locks.acquire(session.id)

        if not new_session:
            last = self._storage_mgr.last_for_project(self._repo_root)
            if last is not None:
                lock = self._locks.try_acquire(last.id)
                if lock is not None:
                    logger.info(
                        f"Resuming session {last.id[:12]} for "
                        f"{self._repo_root}."
                    )
                    return last, lock
                logger.info(
                    f"Session {last.id[:12]} is open in another process — "
                    f"starting a new one."
                )

        session = self._storage_mgr.create(cwd=str(self._repo_root))
        return session, self._locks.acquire(session.id)

    def _release_lock(self) -> None:
        """Drop this manager's claim on its session, if it has one."""
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    async def close(self) -> None:
        """Release the session and prune it if nothing ever happened in it.

        Every exit path calls this — REPL, one-shot, and the web lifespan
        — so the claim file is deleted rather than left for the next
        process's sweep.  Pruning happens before the release, so the
        delete cannot race a process that has just claimed the session.
        """
        try:
            if self._lock is not None:
                await self.prune_if_empty()
        finally:
            self._release_lock()

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
    def permission_mode(self) -> PermissionMode:
        """The current permission gating mode (from the shared manager)."""
        return self._perm_mgr.mode

    @property
    def scope_policy(self) -> FileScopePolicy:
        """Where this session's workspace and restricted paths are."""
        return self._scope_policy

    @property
    def todos(self) -> tuple[TodoItem, ...]:
        """The current todo list — session-scoped and in memory only."""
        return self._todo_state.items

    @property
    def mode_label(self) -> str:
        """User-facing mode label for the REPL header.

        Returns ``"PLAN"`` when in any plan-related mode or when
        ``/plan`` is pending.  Otherwise returns the permission gating
        label — ``"MANUAL"`` or ``"AUTO"``.
        """
        if self._sm.plan_pending:
            return "PLAN"
        if self._sm.current_mode.is_plan_related:
            return "PLAN"
        return self.permission_mode.value.upper()

    @property
    def context_usage_pct(self) -> int | None:
        """Current context usage as a whole-number percentage (0–100+).

        Returns *None* when the context hasn't been initialized yet
        (before :meth:`resolve`).
        """
        if self._ctx is None:
            return None
        return round(self._ctx.usage_ratio * 100)

    # ==================================================================
    # Model selection
    # ==================================================================

    @property
    def selection(self) -> TurnConfig:
        """The model + effort this conversation runs with."""
        return self._current_selection()

    @property
    def model(self) -> str:
        """The model spec this conversation runs with, for display."""
        return self._current_selection().spec

    @property
    def effort(self) -> str | None:
        """The thinking-effort tier this conversation runs with."""
        return self._current_selection().reasoning_effort

    @property
    def model_slot(self) -> str:
        """The slot this conversation's model was selected by, for display.

        The row's own slot wins; a row that names none (fresh, or written
        before the column existed) reports the slot the settings select —
        which is what :meth:`_activate_context` resolves its model from in
        that case, so the answer is the truth rather than a guess.

        Provenance, not identity, and callers must treat it as a claim to
        check rather than a fact: a slot retargeted since the conversation
        was created names a model this conversation does not run, so a
        reader showing "which row is selected" has to confirm the slot
        still resolves to :attr:`model` first.
        """
        if self._conv is not None and self._conv.model_slot:
            return self._conv.model_slot
        return self._settings.model

    @property
    def model_slots(self) -> dict[str, str]:
        """The named model slots, as the settings value them."""
        return self._settings.model_slots

    def set_model(self, slot: str) -> TurnConfig:
        """Select the model slot this conversation runs with.

        Write-through: the row is stamped and the token accounting re-keyed
        at once, so the next turn reads the new model from memory and a
        reload reads it from disk.  The slot *name* is stamped alongside the
        spec it resolves to — provenance for a picker, never identity: two
        slots can name the same model, and which row the user pointed at is
        not recoverable from the spec.

        Raises :class:`ValueError` when *slot* names no configured slot.
        """
        # The stored name is canonical (lowercase), so a surface comparing a
        # picker row against it cannot miss on casing alone.
        name = slot.strip().lower()
        return self._apply_selection(
            replace(
                self._current_selection(),
                spec=resolve_slot(name, self._settings.model_slots),
            ),
            slot=name,
        )

    def set_effort(self, effort: str | None) -> TurnConfig:
        """Select the thinking-effort tier this conversation runs with.

        *None* leaves the endpoint's own default in place.
        """
        return self._apply_selection(
            replace(self._current_selection(), reasoning_effort=effort),
        )

    def _settings_config(self) -> TurnConfig:
        """Build the turn config the settings slots describe.

        What a conversation starts on.  Only the settings know a slot's
        value, so the spec is resolved here rather than stored as one.
        """
        return TurnConfig(
            spec=self._settings.model_spec,
            reasoning_effort=self._settings.reasoning_effort,
        )

    def _current_selection(self) -> TurnConfig:
        """The live selection, falling back to the settings' slot."""
        if self._selection is None:
            self._selection = self._settings_config()
        return self._selection

    def _apply_selection(
        self, config: TurnConfig, slot: str | None = None,
    ) -> TurnConfig:
        """Adopt *config*: re-key the context, stamp the row, persist it.

        The token count is zeroed when the model changes — it was computed
        by the old encoding, and ``model`` is the key it is valid for.

        *slot* names the model slot the selection came from, stamped on the
        conversation as provenance (see
        :meth:`set_model`).  Only a model switch passes one: an effort
        switch leaves the stored slot alone, since it says nothing about
        which model the conversation runs.
        """
        rekeyed = config.spec != self._current_selection().spec
        self._selection = config
        if self._ctx is not None:
            self._ctx.set_config(config)
        if self._conv is not None:
            self._conv.model = config.spec
            self._conv.reasoning_effort = config.reasoning_effort
            if slot is not None:
                self._conv.model_slot = slot
            if rekeyed:
                self._conv.total_tokens = 0
            self._storage_mgr.update_conversation(self._conv)
        return config

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

        Classification happens here so the full state-machine lifecycle is
        visible in one place.  Complex requests run the plan loop inside the
        same agent run: explore, call ``propose_plan``, wait for the user's
        decision, then execute.  Simple requests go straight to execution.

        Yields :class:`AgentEvent` objects for the CLI to render.  The loop
        always ends with a terminal event (:class:`AgentFinished`,
        :class:`AgentCancelled`, or a fatal error) — the CLI stops the
        streaming renderer on those, so ending otherwise would strand the
        terminal in the alternate screen.
        """
        # --- Resolve the turn's model and effort ---
        # Read once, here, and threaded down as a parameter: every request
        # this turn makes carries this same pair, so a selection change
        # mid-turn cannot split one turn across two models.
        config = self._current_selection()

        # --- Conversation-start checkpoint (first turn only) ---
        self._create_conversation_start_checkpoint()

        # --- Auto-title new conversations ---
        # The truncated first input is the visible fallback; the first LLM
        # request of the conversation also asks the endpoint for a proper
        # title (DeepSeek's title feature).  *title_baseline* remembers the
        # seed so a generated title only replaces it while the user has not
        # renamed the conversation.
        request_title = False
        title_baseline: str | None = None
        if self._conv is not None and not self._conv.title:
            self._conv.title = user_input[:MAX_TITLE_LENGTH]
            title_baseline = self._conv.title
            request_title = True

        # --- Classify ---
        self._sm.reset()
        self._sm.classify_and_transition(
            user_input, force_plan=force_plan,
        )

        # --- Collect prior conversation summaries ---
        prior_titles = self._get_prior_titles()
        if self._ctx is not None:
            self._ctx.set_cross_conversation_context(prior_titles)

        # A turn never inherits the previous turn's proposal.
        self._plan_tool_id = None
        self._proposed_plan = None

        # --- Arm the tools for the phase the turn starts in ---
        if self._sm.is_plan_exploring:
            # Every plan cycle starts from the safe baseline — the
            # approval UI then lets the user explicitly choose AUTO.
            # Goes through set_permission_mode so observers (the web
            # UI's session-info broadcaster) see the gating flip.
            self.set_permission_mode(PermissionMode.MANUAL)
            self._activate_plan_exploration()
        elif self._sm.is_executing:
            self._activate_todo_tools()

        try:
            async for event in self._run_phase(
                user_input,
                self._sm.get_mode_hint(),
                config=config,
                request_title=request_title,
            ):
                if isinstance(event, ConversationTitle):
                    self._apply_generated_title(
                        event.title, title_baseline,
                    )
                    continue

                # The plan proposal is a tool call.  Its pause is
                # translated into PlanProposed; the user's decision comes
                # back through approve_plan()/reject_plan(), which answer
                # the same pause.  A malformed plan never produces a pause
                # — the loop returns the failed call to the model.
                if (
                    isinstance(event, ToolCallStart)
                    and event.tool_name == PROPOSE_PLAN_NAME
                ):
                    self._remember_proposal(event)
                elif (
                    isinstance(event, AgentPaused)
                    and event.tool_id
                    and event.tool_id == self._plan_tool_id
                    and self._proposed_plan is not None
                ):
                    yield PlanProposed(plan=self._proposed_plan)
                    continue
                elif (
                    isinstance(event, ToolCallEnd)
                    and event.tool_name == PROPOSE_PLAN_NAME
                ):
                    self._finish_proposal(event)

                if (
                    isinstance(event, AgentFinished)
                    and event.stopped
                    and self._sm.current_mode is AgentMode.PLAN_EXPLORING
                ):
                    # The loop reports the denied tool generically; in a
                    # plan turn that denial *is* the plan decision.
                    event = AgentFinished(
                        reason="Plan rejected by user.",
                        usage=event.usage,
                        stopped=True,
                    )

                yield event
        finally:
            self._deactivate_todo_tools()
            self._deactivate_plan_exploration()

        self._sm.mark_finished()

    async def cancel_turn(self) -> list[ToolCallEnd]:
        """Record a cancelled turn, repair state, and persist it.

        Resets the state machine to IDLE — a cancelled turn leaves it
        mid-plan, which would keep the web UI's mode pill frozen — and
        repairs the in-memory context: the partial messages stay so the
        next turn can continue from them, a dangling assistant
        ``tool_use`` is answered with a cancelled ``tool_result``, and
        a cancellation marker is appended so the model knows the
        previous turn was cut short.  The repair is persisted
        immediately via :meth:`save` — the cancelled turn survives a
        restart instead of waiting for the next turn's ``save()``.
        Observers are notified like a transition so the web UI
        unfreezes.

        Returns one :class:`ToolCallEnd` per dangling call the repair
        answered — the same cancelled result the saved transcript
        replays.  A live front-end broadcasts them so its cards settle
        like a reloaded transcript's instead of showing no output.
        """
        repaired = (
            self._ctx.mark_turn_cancelled()
            if self._ctx is not None
            else []
        )
        await self.save()
        self._plan_tool_id = None
        self._proposed_plan = None
        self._sm.reset()
        self._sm.notify_observers()
        return [
            ToolCallEnd(
                tool_id=block.tool_id or "",
                tool_name=block.tool_name or "",
                input=block.tool_input or {},
                result=ToolResult(
                    tool_id=block.tool_id or "",
                    tool_name=block.tool_name or "",
                    success=False,
                    output="",
                    error=CANCELLED_TOOL_RESULT,
                ),
            )
            for block in repaired
        ]

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Set the permission gating mode — the loop's gate reads the
        shared manager, so the change takes effect automatically.

        State-machine observers are notified too: gating feeds the UI's
        session payload (``permission_mode``) but is not a transition, so
        a gating flip (``/mode auto``, plan approval with auto, plan start
        pinning to manual) would otherwise leave the web UI stale.
        """
        self._perm_mgr.set_mode(mode)
        self._sm.notify_observers()

    def approve_plan(
        self, *, plan_id: str,
        permission_mode: PermissionMode | None = None,
    ) -> bool:
        """Approve the plan with *plan_id* and unblock :meth:`process_turn`.

        Answers the ``propose_plan`` pause the same way any tool pause is
        answered; the loop then runs the approved call.  Returns ``True``
        only when the answer settled the pause.  When *permission_mode* is
        given, gating switches to it for an accepted approval, just before
        execution begins (e.g. "approve with auto accept") — a stale
        approval must not flip gating.
        """
        if self._plan_tool_id is None or plan_id != self._plan_tool_id:
            return False
        choice = (
            PLAN_CHOICE_APPROVE_AUTO
            if permission_mode is PermissionMode.AUTO
            else PLAN_CHOICE_APPROVE
        )
        accepted = self.agent.answer_tool_call(
            plan_id, [PromptAnswer(choice=choice)],
        )
        if not accepted:
            return False
        # Arm execution before the gating flip so observers see the
        # transition first, then the new permission mode.  No await sits
        # between the answer and these calls, so the loop cannot resume
        # in between.
        self._arm_execution()
        if permission_mode is not None:
            self.set_permission_mode(permission_mode)
        return True

    def reject_plan(self, *, plan_id: str, feedback: str = "") -> bool:
        """Reject the plan with *plan_id* and unblock :meth:`process_turn`.

        When *feedback* is provided the tool result carries it and the
        model revises the plan in the same turn.  Otherwise the denied
        call ends the turn.  Returns ``True`` only when the answer settled
        the pause; a stale plan id or a turn past the decision returns
        ``False`` and changes nothing.
        """
        if self._plan_tool_id is None or plan_id != self._plan_tool_id:
            return False
        answer = (
            PromptAnswer(choice=PLAN_CHOICE_FEEDBACK, content=feedback)
            if feedback
            else PromptAnswer(choice=PLAN_CHOICE_REJECT)
        )
        accepted = self.agent.answer_tool_call(plan_id, [answer])
        if accepted:
            self._plan_tool_id = None
            self._proposed_plan = None
        return accepted

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

    def _apply_generated_title(
        self, title: str, baseline: str | None,
    ) -> None:
        """Adopt an endpoint-written conversation title.

        *baseline* is the seed title captured when the first request was
        made.  ``None`` means this turn did not ask for a title, so one
        arriving anyway (a gateway volunteering it) is ignored.  The title
        is adopted only while the conversation still carries the seed — a
        user rename mid-turn wins.

        Persisted immediately, like :meth:`rename_conversation`, and
        observers are notified so the web UI's ``session_info`` frame picks
        it up.  A cosmetic write must not kill the turn, hence the catch.
        """
        if self._conv is None or baseline is None:
            return
        title = title.strip()[:MAX_TITLE_LENGTH]
        if not title or title == baseline or self._conv.title != baseline:
            return
        self._conv.title = title
        try:
            self._storage_mgr.update_conversation(self._conv)
            self._sm.notify_observers()
        except Exception:
            logger.exception(
                "Failed to persist the generated conversation title."
            )

    async def _activate_context(self) -> None:
        """Load messages from storage and populate the context buffer.

        The manager
        reads messages from the DB, builds the initial list (including any
        synthetic compaction summary), and hands it to
        :meth:`ContextManager.load`.
        """
        if self._conv is None or self._ctx is None:
            return

        # The conversation's own selection wins; a row that names none (fresh,
        # or written before the column existed) falls back to the slot the
        # settings select.
        self._selection = (
            TurnConfig(
                spec=self._conv.model,
                reasoning_effort=self._conv.reasoning_effort,
            )
            if self._conv.model
            else self._settings_config()
        )
        self._ctx.set_config(self._selection)

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
        # the model the window is now keyed to — the row can have been
        # written by another process (the REPL and ``tod serve`` share one
        # database), so the check is not tautological.
        if (
            self._conv.total_tokens > 0
            and self._conv.model is not None
            and self._conv.model == self._ctx.model
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
        self, user_input: str, mode_hint: str, *, config: TurnConfig,
        request_title: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent-loop phase and persist afterward.

        Extracts token usage from the terminal event — :class:`AgentFinished`
        or :class:`AgentCancelled` — and accumulates it into the session
        totals.
        """
        stream = self._settings.streaming_enabled
        gen = self.agent.run(
            user_input,
            config=config,
            max_iterations=self._settings.max_iterations,
            stream=stream,
            mode=mode_hint,
            request_title=request_title,
        )

        usage: TokenUsage | None = None

        async for event in gen:
            if isinstance(event, (AgentFinished, AgentCancelled)):
                usage = event.usage
            yield event
            if isinstance(event, ToolCallEnd):
                update = self._todo_state.take_update()
                if update is not None:
                    yield TodoUpdate(items=update)

        # Persist after the phase completes.
        await self._maybe_persist_phase(usage)

    async def _maybe_persist_phase(
        self,
        event_or_usage: AgentFinished | AgentCancelled | TokenUsage | None,
    ) -> None:
        """Persist token usage and context after a phase completes.

        Accepts a terminal event (:class:`AgentFinished` or
        :class:`AgentCancelled`) or a plain :class:`TokenUsage` — the
        path :meth:`_run_phase` uses.
        """
        usage: TokenUsage | None
        if isinstance(event_or_usage, (AgentFinished, AgentCancelled)):
            usage = event_or_usage.usage
        else:
            usage = event_or_usage

        if self._ctx is not None:
            if usage is not None and self._storage_mgr is not None:
                self._storage_mgr.accumulate_tokens(
                    self._session.id, usage,
                )
            await self.save()

    # ------------------------------------------------------------------
    # Plan execution tracking
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Plan proposal
    # ------------------------------------------------------------------

    def _activate_plan_exploration(self) -> None:
        """Register ``propose_plan`` for a plan turn.

        The tool is session-registered rather than a default: only a plan
        turn should see it, and the loop rebuilds its tool list every
        request, so deregistering on approval hides it for execution.
        """
        if PROPOSE_PLAN_NAME not in self._registry:
            self._registry.register(
                ProposePlanTool(scope_policy=self._scope_policy)
            )

    def _deactivate_plan_exploration(self) -> None:
        """Idempotently remove ``propose_plan`` after the turn."""
        self._registry.deregister(PROPOSE_PLAN_NAME)

    def _remember_proposal(self, event: ToolCallStart) -> None:
        """Parse a proposal call's arguments for the PlanProposed event.

        A malformed plan stores ``None``; the loop will fail the call's
        prompt build and return the error to the model, so no pause — and
        no PlanProposed — is ever produced for it.
        """
        self._plan_tool_id = event.tool_id
        try:
            self._proposed_plan = Plan.from_arguments(
                event.input, plan_id=event.tool_id,
            )
        except PlanValidationError:
            self._proposed_plan = None

    def _arm_execution(self) -> None:
        """Switch the turn from planning to execution, once.

        The approve facade calls this before answering the pause, so the
        mode transition lands before the gating flip.  ``ToolCallEnd``
        calls it again for an approval answered directly through the
        agent loop.  Idempotent: a second call finds the turn already
        executing and does nothing.
        """
        if (
            self._proposed_plan is None
            or self._sm.current_mode is not AgentMode.PLAN_EXPLORING
        ):
            return
        if not self._sm.transition(AgentMode.EXECUTING):
            return
        if self._ctx is not None:
            self._ctx.set_mode("execute")
        self._registry.deregister(PROPOSE_PLAN_NAME)
        self._activate_todo_tools()
        self._plan_tool_id = None
        self._proposed_plan = None

    def _finish_proposal(self, event: ToolCallEnd) -> None:
        """Arm execution after an approved proposal's call completes.

        The tool runs only when the pause was granted, so a successful
        result *is* the approval.  The facade normally got here first;
        this covers an approval answered directly through the loop.
        """
        if event.result is None or not event.result.success:
            return
        self._arm_execution()

    def _activate_plan_execution(self, plan: Plan) -> None:
        """Arm plan tracking.

        Captures *plan*'s steps in the shared :class:`PlanState` (arming
        update detection) and registers the tool so the LLM sees it only
        during this phase.
        """
        self._plan_state.activate(plan)
        if "plan_update" not in self._registry:
            self._registry.register(
                PlanUpdateTool(
                    self._plan_state, scope_policy=self._scope_policy
                )
            )

    def _deactivate_plan_execution(self) -> None:
        """Tear down plan tracking after the execution phase.

        Idempotent — safe to call even when no plan was activated this
        turn (deregister silently pops a missing name; deactivate just
        re-clears).  Unconditional so a partially completed activation
        can never leak ``plan_update`` into a later turn.
        """
        self._registry.deregister("plan_update")
        self._plan_state.deactivate()

    # ------------------------------------------------------------------
    # Todo tools
    # ------------------------------------------------------------------

    def _activate_todo_tools(self) -> None:
        """Register ``todo_write`` for an execution phase.

        It is not a default tool — it mutates the session's shared
        ``TodoState`` — and plan exploration must not see it, so the
        execution phase is the only place it is armed.
        """
        if TODO_TOOL_NAME not in self._registry:
            self._registry.register(
                TodoWriteTool(
                    self._todo_state, scope_policy=self._scope_policy,
                )
            )

    def _deactivate_todo_tools(self) -> None:
        """Idempotently remove ``todo_write`` after the phase."""
        self._registry.deregister(TODO_TOOL_NAME)

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

        # The todo list belongs to the conversation; a new one starts
        # without any (it is never persisted).
        self._todo_state.reset()

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
        # The selection rides along: /clear starts a new conversation, not a
        # new model.  So does the slot it was picked by, or /clear would
        # reset the picker to reporting the settings' slot.
        selection = self._current_selection()
        # Read the outgoing conversation's slot before the assignment below
        # replaces it — the property reads the *current* row.
        slot = self.model_slot
        self._conv = self._storage_mgr.create_conversation(
            self._session.id,
            model=selection.spec,
            reasoning_effort=selection.reasoning_effort,
            model_slot=slot,
        )
        await self._activate_context()

    def rename_conversation(self, title: str) -> bool:
        """Retitle the active conversation in place.

        Unlike :meth:`new_conversation` this archives nothing: a title is
        metadata, so the conversation keeps its transcript, its sequence
        number and its place as the active one — the auto-title simply
        never fires again, since it only fills an empty title.

        The title is stripped and clamped to :data:`MAX_TITLE_LENGTH`, the
        same rule the auto-title applies to a first user input.  Returns
        ``False`` when no conversation is active, or when *title* carries
        nothing but whitespace.
        """
        if self._conv is None:
            return False
        title = title.strip()[:MAX_TITLE_LENGTH]
        if not title:
            return False
        self._conv.title = title
        self._storage_mgr.update_conversation(self._conv)
        return True

    async def resume_conversation(self, conversation_id: str) -> None:
        """Switch to an existing (usually archived) conversation.

        Archives the current conversation before switching so only one
        conversation is active at a time.

        Raises :class:`ValueError` if the conversation is not found.
        """
        if self._ctx is None:
            return

        # Archive the current conversation so only one is active at a time.
        prev_conv = self._conv
        if prev_conv is not None:
            self._storage_mgr.archive_conversation(
                prev_conv.id,
            )

        try:
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
        except ValueError:
            # A bad id must be a no-op — undo the archiving so the
            # current conversation stays active and visible.
            if prev_conv is not None:
                prev_conv.status = "active"
                self._storage_mgr.update_conversation(prev_conv)
            raise

        # Reactivate the resumed conversation.
        conv.status = "active"
        self._conv = conv
        self._todo_state.reset()
        await self._activate_context()

    async def switch_session(self, session_id: str) -> None:
        """Switch to a different session by ID.

        Raises :class:`ValueError` if the session is not found, and
        :class:`~toddler.session.lock.SessionInUseError` if another
        process holds it — the manager stays on the session it was on.
        """
        session = self._storage_mgr.get(session_id)
        if session is None:
            raise ValueError(
                f"Session '{session_id[:16]}...' not found."
            )

        if self._session is not None and session.id == self._session.id:
            return  # Already here — and re-claiming our own file would fail.

        # Claim the target before disturbing the current one: a busy
        # session raises here, leaving the manager where it was.
        lock = self._locks.acquire(session.id)

        # Build the new context and swap it in only once it has loaded; a
        # failure leaves — and keeps claiming — the session we came from.
        previous_state = (
            self._session, self._ctx, self._conv,
            self._selection, self._base_seq,
        )
        try:
            self._session = session
            self._ctx = ContextManager(
                self._llm,
                config=self._settings_config(),
                project_root=self._repo_root,
                memory_dir=self._settings.session_dir,
            )
            self._conv = self._storage_mgr.get_or_create_active_conversation(
                session.id,
            )
            await self._activate_context()
        except Exception:
            (
                self._session, self._ctx, self._conv,
                self._selection, self._base_seq,
            ) = previous_state
            # This process is not switching after all — do not keep
            # another process out of the session it does not use.
            lock.release()
            raise

        # Reset the agent so it captures the new context — it holds the
        # one it was built with.
        self._agent_impl = None
        self._todo_state.reset()

        if self._ckpt_mgr is not None:
            self._ckpt_mgr.set_session(self._session.id)

        # The switch succeeded: let go of the session we came from.
        previous, self._lock = self._lock, lock
        if previous is not None:
            previous.release()

    # ==================================================================
    # Persistence
    # ==================================================================

    async def compact_context(self) -> CompactionResult | None:
        """Manually compact the active conversation context and persist it.

        Backs the ``/compact`` slash command.  Runs the compaction
        routine regardless of the auto-compaction token threshold; on
        success the metadata (``compacted_summary`` /
        ``compacted_at_seq``) is persisted immediately via
        :meth:`save`, so a reload shows the summarised transcript
        without waiting for the next turn.  Returns *None* when there
        was nothing to summarise or the summarisation failed — the
        context is then unchanged and nothing is written.
        """
        if self._ctx is None or self._conv is None:
            return None
        logger.warning(
            f"Manual compaction requested — compacting "
            f"{len(self._ctx.messages)} messages..."
        )
        result = await self._ctx.compact()
        if result is not None:
            await self.save()
        return result

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
        # comparable across loads when the model is unchanged, so persist the
        # selection that produced it — the *spec*, not the slot, so that
        # retargeting a slot cannot re-point this row at a model that never
        # ran it.
        self._conv.total_tokens = self._ctx.count_tokens()
        self._conv.model = self._ctx.model
        self._conv.reasoning_effort = self._current_selection().reasoning_effort  # noqa: E501

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
                permission_manager=self._perm_mgr,
            )
        return self._agent_impl

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
