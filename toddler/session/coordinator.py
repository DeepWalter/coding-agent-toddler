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
    PlanProposed,
)
from toddler.agent.loop import AgentLoop
from toddler.agent.state_machine import (
    AgentMode,
    AgentStateMachine,
    Plan,
)
from toddler.checkpoint import create_checkpoint_callback
from toddler.checkpoint.manager import CheckpointManager
from toddler.checkpoint.models import (
    AgentStateSnapshot,
    Checkpoint,
    RollbackResult,
)
from toddler.config.settings import Settings
from toddler.context.conversation_context import ConversationContext
from toddler.context.system_prompt import SystemPromptBuilder
from toddler.llm import BaseLLMProvider, Message, TokenUsage
from toddler.llm.responses import LLMResponse
from toddler.session.manager import StorageManager
from toddler.session.models import Session
from toddler.tools import create_default_registry
from toddler.tools.executor import ToolExecutor

if TYPE_CHECKING:
    from toddler.context.compaction import ConversationCompactor
    from toddler.context.memory import PersistentMemory
    from toddler.context.project_map import ProjectMapper
    from toddler.context.window import ContextWindowManager

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

        # Context management components
        self._project_mapper = project_mapper
        self._persistent_memory = persistent_memory
        self._context_window_mgr = context_window_mgr
        self._conversation_compactor = conversation_compactor

        # Pre-build SystemPromptBuilder
        self._prompt_builder = SystemPromptBuilder(
            project_mapper=project_mapper,
            persistent_memory=persistent_memory,
        )

        # State machine
        self._sm = state_machine or AgentStateMachine()

        # Plan approval gating (same asyncio.Event pattern as AgentLoop)
        self._plan_decision_event = asyncio.Event()
        self._plan_feedback: str = ""

        # Current session + context (set via resolve())
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
        self._session = self._storage_mgr.get_or_create(session_id)

        self._ctx = ConversationContext(
            self._storage_mgr,
            self._prompt_builder,
            window_mgr=self._context_window_mgr,
            compactor=self._conversation_compactor,
        )
        conv = self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)

        if self._ckpt_mgr is not None:
            self._ckpt_mgr.set_session(self._session.id)
        logger.info(f"Session resolved: {self._session.id[:12]}...")
        return self._session

    @property
    def session(self) -> Session | None:
        """The current session, or *None* if :meth:`resolve` hasn't been called."""  # noqa: E501
        return self._session

    @property
    def context(self) -> ConversationContext | None:
        """The current conversation context, or *None* before :meth:`resolve`."""  # noqa: E501
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

        For simple requests this is a single pass through the agent loop.
        For complex requests it drives the full plan-mode lifecycle:
        explore → propose → wait (user approval) → execute.

        Yields :class:`AgentEvent` objects for the CLI to render.
        """
        # --- Classify and transition from IDLE ---
        self._sm.reset()
        mode = self._sm.classify_and_transition(
            user_input, force_plan=force_plan,
        )

        # --- Conversation-start checkpoint (first turn only) ---
        self._create_conversation_start_checkpoint()

        # --- Simple execution path (no plan) ---
        if mode == AgentMode.EXECUTING:
            async for event in self._run_phase(user_input, "execute"):
                yield event
            self._sm.mark_finished()
            return

        # --- Plan-mode path: multi-phase orchestration ---
        original_request = user_input
        explore_input = user_input

        while True:
            current_mode = self._sm.current_mode

            if current_mode == AgentMode.PLAN_EXPLORING:
                async for event in self._run_phase(
                    explore_input, "plan_exploring",
                ):
                    yield event
                self._sm.transition(AgentMode.PLAN_PROPOSING)
                continue

            elif current_mode == AgentMode.PLAN_PROPOSING:
                plan = await self._generate_plan(original_request)
                if plan is None:
                    self._sm.mark_finished()
                    yield AgentError(
                        message=(
                            "Failed to generate a valid plan. "
                            "The LLM did not produce parseable JSON. "
                            "Try rephrasing your request."
                        ),
                        recoverable=False,
                    )
                    return
                self._sm.set_plan(plan)
                self._sm.transition(AgentMode.PLAN_WAITING)
                continue

            elif current_mode == AgentMode.PLAN_WAITING:
                # Clear BEFORE yielding so the caller's set() isn't
                # immediately wiped by a trailing clear() on resume.
                self._plan_decision_event.clear()
                yield PlanProposed(plan=self._sm.current_plan)  # type: ignore[arg-type]
                await self._plan_decision_event.wait()

                # Decision was made by approve_plan() / reject_plan()
                if self._sm.current_mode == AgentMode.PLAN_EXECUTING:
                    # Approved — inject plan and execute
                    plan_text = self._sm.current_plan.format_for_prompt()
                    exec_msg = (
                        "I have reviewed and approved the following plan. "
                        "Execute it step by step, reporting progress after "
                        f"each step:\n\n{plan_text}"
                    )
                    if self._ctx is not None:
                        self._ctx.append(Message.user(exec_msg))
                    continue  # next iteration → PLAN_EXECUTING

                elif self._sm.current_mode == AgentMode.PLAN_EXPLORING:
                    # Rejected with feedback — loop back to explore
                    feedback_msg = (
                        "The proposed plan was rejected with this feedback: "
                        f"{self._plan_feedback}\n\n"
                        "Please reconsider the original request and "
                        "re-explore the codebase, addressing the feedback "
                        "above."
                    )
                    if self._ctx is not None:
                        self._ctx.append(Message.user(feedback_msg))
                    explore_input = (
                        f"Revise your research based on this feedback: "
                        f"{self._plan_feedback}"
                    )
                    continue  # next iteration → PLAN_EXPLORING

                else:
                    # Rejected outright → FINISHED
                    yield AgentFinished(
                        reason="Plan rejected by user.",
                        usage=None,
                    )
                    return

            elif current_mode == AgentMode.PLAN_EXECUTING:
                async for event in self._run_phase(
                    "Begin executing the approved plan now.",
                    "plan_executing",
                ):
                    yield event
                self._sm.mark_finished()
                return

            elif current_mode == AgentMode.FINISHED:
                return

            else:
                logger.error(
                    "Unexpected mode in process_turn: %s", current_mode,
                )
                return

    def approve_plan(self) -> bool:
        """Approve the current plan and unblock :meth:`process_turn`.

        Called by the CLI layer after the user confirms approval.
        Returns ``True`` if the plan was successfully approved.
        """
        success = self._sm.approve_plan()
        if not success:
            self._sm.mark_finished()
        self._plan_decision_event.set()
        return success

    def reject_plan(self, *, feedback: str = "") -> None:
        """Reject the current plan and unblock :meth:`process_turn`.

        When *feedback* is provided the agent will re-explore and propose
        a revised plan.  Otherwise the turn finishes.
        """
        self._plan_feedback = feedback
        self._sm.reject_plan(feedback=feedback)
        self._plan_decision_event.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_conversation_start_checkpoint(self) -> None:
        """Create a checkpoint at the start of a new conversation."""
        if (
            self._ckpt_mgr is not None
            and self._ctx is not None
            and self._ctx.conversation is not None
            and self._ctx.conversation.message_count == 0
        ):
            try:
                self._ckpt_mgr.create(
                    description=(
                        "Start of conversation "
                        f"{self._ctx.conversation.sequence_num}"
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
        if self._ctx is not None:
            if usage is not None and self._storage_mgr is not None:
                self._storage_mgr.accumulate_tokens(
                    self._session.id, usage,
                )
            await self._ctx.save()

    async def _generate_plan(self, user_request: str) -> Plan | None:
        """Ask the LLM to produce a structured JSON plan.

        Collects research context from the exploration phase (recent
        assistant messages), sends the plan-proposal prompt to the LLM
        without tools, and parses the JSON response.

        Returns ``None`` when the LLM fails to produce valid JSON.
        """
        # Collect research context from the exploration phase
        research_context = ""
        if self._ctx is not None:
            assistant_texts = []
            for msg in self._ctx.messages:
                if msg.role == "assistant":
                    text = msg.text.strip()
                    if text:
                        assistant_texts.append(text)
            research_context = "\n\n".join(assistant_texts[-5:])
            if len(research_context) > 4000:
                research_context = research_context[:4000] + (
                    "\n... (truncated)"
                )

        prompt = AgentStateMachine.plan_proposal_prompt(
            user_request,
            research_context=research_context,
        )

        try:
            response = await self._llm.generate(
                [Message.user(prompt)],
                tools=[],
                max_tokens=2048,
                temperature=0.0,
                stream=False,
            )
            # Non-streaming response
            if isinstance(response, LLMResponse):
                text = (
                    response.messages[0].text
                    if response.messages else ""
                )
            else:
                logger.error(
                    "Unexpected response type from plan LLM call"
                )
                return None
        except Exception:
            logger.exception("Plan generation LLM call failed")
            return None

        plan = Plan.from_json(text)
        if plan is None:
            logger.warning(
                "Failed to parse plan JSON.  Raw response: %.200s...",
                text,
            )
        elif not plan.steps:
            logger.warning("Plan has no steps — rejecting")
            plan = None

        return plan

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

        if title:
            self._ctx.set_title(title)
        await self._ctx.save()

        conv = self._ctx.conversation
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
        conv = self._storage_mgr.create_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)

    async def resume_conversation(self, conversation_id: str) -> None:
        """Switch to an existing (usually archived) conversation.

        Archives the current conversation before switching so only one
        conversation is active at a time.

        Raises :class:`ValueError` if the conversation is not found.
        """
        if self._ctx is None:
            return

        # Archive the current conversation so only one is active at a time.
        if self._ctx.conversation is not None:
            self._storage_mgr.archive_conversation(
                self._ctx.conversation.id,
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
        await self._ctx.activate(conv)

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
        # Re-resolve: create new context and activate active conversation.
        self._ctx = ConversationContext(
            self._storage_mgr,
            self._prompt_builder,
            window_mgr=self._context_window_mgr,
            compactor=self._conversation_compactor,
        )
        conv = self._storage_mgr.get_or_create_active_conversation(
            self._session.id,
        )
        await self._ctx.activate(conv)
        if self._ckpt_mgr is not None:
            self._ckpt_mgr.set_session(self._session.id)

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

            session = self._storage_mgr.get(session_id)
            if session is None:
                return

            session.title = title if title else None
            session.updated_at = datetime.now(UTC)
            self._storage_mgr.update(session)
            logger.info(f"Auto-titled session {session_id}: {title}")
        except Exception:
            logger.exception(
                f"Auto-title failed for session {session_id} — ignoring."
            )

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
