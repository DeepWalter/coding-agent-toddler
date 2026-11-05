"""Core agent loop — the tool-calling orchestration engine.

Supports both streaming and non-streaming modes via
:class:`~toddler.agent.handler.BaseHandler` implementations — use
:func:`~toddler.agent.handler.create_handler` to get the right handler
for the current mode.  The loop yields :class:`AgentEvent` objects as
they arrive (real-time in streaming mode, or batched from the full
response in non-streaming mode).

The loop receives a single :class:`~toddler.context.manager.ContextManager`
instance that handles system prompt assembly, context window tracking,
and compaction — keeping the orchestration layer clean.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    AgentPaused,
    RecoverableAgentError,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.handler import create_handler
from toddler.agent.stop_conditions import StopConditionChecker
from toddler.config.models import TurnConfig
from toddler.llm import Message, MessageBlock, TokenUsage
from toddler.tools.base import (
    ALLOW_CALL_TITLE,
    Decision,
    Permission,
    PermissionManager,
    Question,
    ToolCall,
    ToolCallPrompt,
    ToolResult,
    blocked_message,
)

if TYPE_CHECKING:
    from toddler.agent.handler import BaseHandler
    from toddler.config.settings import Settings
    from toddler.context.manager import ContextManager
    from toddler.llm.base import BaseLLMProvider
    from toddler.tools.executor import ToolExecutor
    from toddler.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_tool_calls(msg: Message) -> list[ToolCall]:
    """Pull every ``tool_use`` block out of *msg* as :class:`ToolCall`."""
    calls: list[ToolCall] = []
    for block in msg.blocks:
        if block.type == "tool_use":
            calls.append(
                ToolCall(
                    tool_id=block.tool_id or "",
                    tool_name=block.tool_name or "",
                    parameters=block.tool_input or {},
                )
            )
    return calls


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class _PendingApproval:
    """The answer channel for one paused call.

    Created when the pause is yielded and consumed by the call it paused,
    so an answer cannot outlive the question: a call that finds no
    channel here was never paused for, and says so rather than waiting on
    a channel that is not its own — or, worse, reading one left answered
    by the call before it.

    A normal call waits for approve/deny; an ask-user call carries its
    ``questions`` and waits for an answer array.  The questions ride along
    so :meth:`AgentLoop.answer_questions` can validate the count and every
    choice index without trusting the caller.  One-shot: once settled, a
    later answer lands nowhere.
    """

    def __init__(
        self,
        tool_id: str = "",
        questions: tuple[Question, ...] | None = None,
    ) -> None:
        self.tool_id = tool_id
        self.questions = questions
        self.event = asyncio.Event()
        self.granted = False
        self.answers: list[int] | None = None
        self.declined = False

    @property
    def settled(self) -> bool:
        """Whether an answer, a decline, or an approval is recorded."""
        return self.granted or self.declined or self.answers is not None

    def answer(self, granted: bool) -> None:
        """Record an approve/deny and release the waiting call."""
        self.granted = granted
        self.event.set()

    def answer_questions(self, answers: list[int]) -> None:
        """Record a complete answer set and release the waiting call."""
        self.answers = list(answers)
        self.event.set()

    def decline(self) -> None:
        """Decline the call — the terminal's Escape/EOF path."""
        self.declined = True
        self.event.set()


@dataclass(frozen=True)
class _Verdict:
    """The gate's answer for one call: what it does, and what was decided.

    The tier rides with the decision because the executor orders its
    checkpoint by it — one classification per call, and the pause and the
    execution cannot disagree about what was asked.
    """

    permission: Permission | None   # None: the classification raised
    decision: Decision

    @property
    def mutating(self) -> bool:
        """Whether the executor should checkpoint before running.

        A tier that could not be read counts as mutating: the call is
        about to run on an approval, and the safe direction is a
        checkpoint it may not have needed.
        """
        return self.permission is None or self.permission.is_mutating


class AgentLoop:
    """Orchestrates the core tool-calling loop.

    Runs as an async generator yielding :class:`AgentEvent` objects.  The
    CLI layer (Phase 5) iterates over these to drive the display.

    Permission gating is done *inline* in the loop body so that
    :class:`AgentPaused` events can be yielded naturally — this gate is
    the only one a call passes.  :class:`ToolExecutor` runs what the gate
    clears, and is told whether the call mutates so it can take the
    checkpoint first.

    Parameters
    ----------
    llm_provider:
        The LLM backend.
    tool_registry:
        Registry of available tools.
    tool_executor:
        Executor that runs tool calls — handed the gate's classification,
        so it can checkpoint a mutating call before running it.
    settings:
        Resolved settings (limits, permissions, etc.).
    context:
        :class:`ContextManager` that owns the message buffer, system prompt
        building, token tracking, compaction, and persistence coordination.
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        settings: Settings,
        *,
        context: ContextManager,
        permission_manager: PermissionManager,
    ) -> None:
        self._llm = llm_provider
        self._registry = tool_registry
        self._executor = tool_executor
        self._settings = settings

        # Single context object — handles prompt building, window tracking,
        # compaction, and persistence.
        self._ctx = context

        # Shared permission manager — live reference so mode changes
        # (e.g. /mode auto) take effect immediately without rebuilding
        # the agent loop.
        self._perm_mgr = permission_manager

        # Confirmation/answer gate — the live pause, if any.  See
        # _execute_with_gating for the protocol.  One call at a time; the
        # loop executes tool calls sequentially.
        self._pending: _PendingApproval | None = None

    # ==================================================================
    # Public API
    # ==================================================================

    async def run(  # noqa: C901
        self,
        user_input: str,
        *,
        config: TurnConfig,
        max_iterations: int | None = None,
        token_budget: int | None = None,
        stream: bool = False,
        mode: str = "execute",
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent loop for a single user request.

        Parameters
        ----------
        user_input:
            The user's request (plain text).
        config:
            The model and effort every request in this run carries.  A
            parameter rather than provider state, so one run cannot drift
            onto a second model when the selection changes underneath it.
        max_iterations:
            Override the configured max iterations.
        token_budget:
            Hard cap on total tokens consumed across all LLM calls.
        stream:
            When ``True``, uses streaming LLM calls with real-time
            token-by-token output (default ``False`` for backward
            compatibility with tests and non-streaming flows).
        mode:
            Agent mode hint — ``"execute"``, ``"plan_exploring"``, or
            ``"plan_executing"``.  Used by the prompt builder to select
            mode-specific instructions.
        """
        # --- build/append to the message list via the context ---
        await self._ctx.prepare_turn(user_input, mode)
        tools = self._registry.to_api_schemas()

        max_iter = (
            max_iterations
            if max_iterations is not None
            else self._settings.max_iterations
        )
        stop_checker = StopConditionChecker(
            max_iterations=max_iter,
            token_budget=token_budget,
        )
        handler = create_handler(stream=stream)

        # --- main loop ---
        while True:
            # -- check iteration / token limits ---
            stop_reason = stop_checker.increment()
            if stop_reason is not None:
                yield AgentFinished(reason=stop_reason.message, usage=None)
                return

            # -- call LLM (context auto-compacts before returning messages) ---
            messages = await self._ctx.get_messages()
            logger.debug(
                f"Iteration {stop_checker.iteration} — "
                f"calling LLM with {len(messages)} messages "
                f"(stream={stream})"
            )

            llm_result: dict[str, Message | None | str | TokenUsage] = {}
            try:
                async for event in self._call_llm(
                    messages, tools, config=config, stream=stream,
                    handler=handler, llm_result=llm_result,
                ):
                    yield event
            except asyncio.CancelledError:
                # A cancel mid-stream leaves the model's partial output
                # only in the handler's accumulators.  Append it (text
                # plus any tool calls already started) so the next turn
                # can continue from where the model left off — the
                # session layer's cancel repair then answers any
                # dangling tool calls and appends the marker.
                partial = handler.get_partial_content()
                if partial:
                    self._ctx.append(Message.assistant(partial))
                raise

            assistant_msg = llm_result["assistant_msg"]
            stop_reason = llm_result["stop_reason"]
            usage = llm_result["usage"]

            # If the LLM call itself failed fatally, assistant_msg will be
            # None and we should stop.
            if assistant_msg is None:
                yield AgentFinished(
                    reason=f"LLM error: {stop_reason or 'unknown'}",
                    usage=usage,
                )
                return

            stop_checker.add_tokens(usage)

            # Append every non-empty assistant response to the conversation
            # history so that tool-call/tool-result pairs are always persisted
            # (the session manager no longer reconstructs this from events).
            # Skip empty messages — they can occur on streaming errors where
            # the handler assembled no text and no tool calls.
            if assistant_msg.blocks:
                self._ctx.append(assistant_msg)

            # Feed API-reported token counts back into the context so
            # subsequent count_tokens() calls only estimate the delta.
            self._ctx.record_usage(usage)

            # -- handle stop reason ---
            sr = StopConditionChecker.from_llm_stop_reason(stop_reason)
            if sr is not None:
                yield AgentFinished(reason=sr.message, usage=usage)
                return

            # -- tool_use: execute and feed back ---
            if stop_reason == "tool_use":
                tool_calls = _extract_tool_calls(assistant_msg)

                if not tool_calls:
                    logger.warning(
                        "LLM returned stop_reason=tool_use but no tool "
                        "calls found — stopping."
                    )
                    yield AgentFinished(
                        reason="LLM indicated tool use but produced no "
                        "tool calls.",
                        usage=usage,
                    )
                    return

                tool_result_blocks: list[MessageBlock] = []
                # aclosing so a turn cancelled while a question waits
                # closes the inner generator now, running its finally and
                # releasing the answer channel — rather than whenever the
                # async-generator finalizer gets around to it.
                async with aclosing(self._execute_tool_calls(
                    tool_calls, tool_result_blocks,
                )) as executions:
                    async for event in executions:
                        yield event

                self._ctx.append(Message.tool(tool_result_blocks))

                if stop_checker.is_exhausted:
                    extra = stop_checker.increment()
                    yield AgentFinished(
                        reason=(
                            extra.message
                            if extra
                            else "Stop condition reached."
                        ),
                        usage=None,
                    )
                    return

                continue

            # -- unexpected stop reason ---
            yield AgentFinished(
                reason=f"Unexpected stop reason: {stop_reason}",
                usage=usage,
            )
            return

    # ==================================================================
    # Confirmation API  (called by external code, e.g. the CLI layer)
    # ==================================================================

    def approve_tool_call(self, tool_id: str = "") -> bool:
        """Approve the pending tool confirmation and unblock the loop.

        Returns ``False`` when nothing is paused, when *tool_id* names a
        different call (a stale or multi-tab answer), when the call has
        already been settled, or when the pause is an ask-user prompt —
        that one is answered with :meth:`answer_questions`, never
        approved.
        """
        pending = self._pending
        if pending is None:
            return False
        if pending.questions is not None:
            return False
        if tool_id and pending.tool_id and pending.tool_id != tool_id:
            return False
        if pending.settled:
            return False
        pending.answer(True)
        return True

    def deny_tool_call(self, tool_id: str = "") -> bool:
        """Deny or decline the pending pause and unblock the loop.

        For an ordinary call this is the tool gate's deny.  For an
        ask-user call it declines the questions: the model gets a failed
        result saying the user declined, and the turn continues.
        Returns ``False`` for a stale or already-settled pause.
        """
        pending = self._pending
        if pending is None:
            return False
        if tool_id and pending.tool_id and pending.tool_id != tool_id:
            return False
        if pending.settled:
            return False
        pending.decline()
        return True

    def answer_questions(self, tool_id: str, answers: list[int]) -> bool:
        """Answer the pending ask-user call and unblock the loop.

        Returns ``False`` when nothing is waiting, when the pause is an
        ordinary approval rather than a question, when *tool_id* names a
        different call (a stale or multi-tab answer), when the call has
        already been settled, or when *answers* does not match the
        questions — in which case the caller may try again with a valid
        payload.  ``bool`` values are rejected even though they subclass
        ``int``.
        """
        pending = self._pending
        if pending is None or pending.questions is None:
            return False
        if tool_id and pending.tool_id and pending.tool_id != tool_id:
            return False
        if pending.settled:
            return False
        if not isinstance(answers, (list, tuple)):
            return False
        if len(answers) != len(pending.questions):
            return False
        for answer, question in zip(answers, pending.questions, strict=True):
            if not isinstance(answer, int) or isinstance(answer, bool):
                return False
            if not 0 <= answer < len(question.choices):
                return False
        pending.answer_questions(answers)
        return True

    # ==================================================================
    # LLM calling helpers
    # ==================================================================

    async def _call_llm(
        self, messages: list[Message], tools: list[dict], *,
        config: TurnConfig, stream: bool, handler: BaseHandler,
        llm_result: dict,
    ) -> AsyncIterator[AgentEvent]:
        """Call the LLM and yield :class:`AgentEvent` items in real time.

        Delegates response processing to *handler*.  On success,
        *llm_result* is populated via :meth:`handler.get_final_result`;
        on failure the dict is set with ``assistant_msg=None`` and an
        error stop reason.
        """
        handler.clear()
        try:
            response = await self._llm.generate(
                messages,
                tools,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                max_completion_tokens=config.max_completion_tokens,
                temperature=self._settings.temperature,
                stream=stream,
            )
            async for event in handler.process(response):
                yield event
            llm_result.update(handler.get_final_result())
        except Exception as exc:
            logger.exception("LLM call failed")
            yield RecoverableAgentError(message=str(exc))
            llm_result["assistant_msg"] = None
            llm_result["stop_reason"] = f"LLM error: {exc}"
            llm_result["usage"] = TokenUsage()

    # ==================================================================
    # Tool execution
    # ==================================================================

    async def _execute_tool_calls(
        self, tool_calls: list[ToolCall],
        tool_result_blocks: list[MessageBlock],
    ) -> AsyncIterator[AgentEvent]:
        """Execute *tool_calls* with permission gating, yielding events.

        Yields :class:`ToolCallStart` (input already complete — the call
        left the provider phase whole), :class:`AgentPaused` (an
        approve/deny card or an ask-user prompt), and :class:`ToolCallEnd`
        for each call.  Appends a :class:`MessageBlock` to
        *tool_result_blocks* for each completed call — the caller then
        feeds them back to the LLM.
        """
        tool_result_blocks.clear()

        for call in tool_calls:
            yield ToolCallStart(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                input=call.parameters,
            )

            # --- permission gating ---
            # The verdict is reached once here and handed to the executor
            # below, so the pause and the execution can never disagree
            # about what was asked — and the tier rides with it, so the
            # executor never classifies the call a second time.
            verdict = self._decision_for(call)
            tool = self._registry.get(call.tool_name)

            # --- pause / execute ---
            # Every call reaches the same gate.  A normal ASK yields an
            # approve/deny card; an ask-user call carries its questions on
            # the same prompt and waits for an answer array.  The channel
            # opens before the event is yielded so an answer that arrives
            # immediately has somewhere to land.  A denial never pauses:
            # there is nothing to ask.
            result: ToolResult
            if verdict.decision is Decision.ASK:
                try:
                    # The tool builds the card the user answers from.  The
                    # fallback is for a tool the registry has not got: the
                    # gate above allows such a call through, so reaching
                    # this pause without one takes a tool that raised while
                    # being classified, and naming it beats rendering
                    # nothing.
                    prompt = (
                        tool.build_call_prompt(**call.parameters)
                        if tool
                        else ToolCallPrompt(
                            title=ALLOW_CALL_TITLE,
                            description=call.tool_name,
                        )
                    )
                except Exception as exc:
                    # Arguments the tool cannot present (an ask_user call
                    # with the wrong shape) never reach the user: the
                    # model gets the error and can retry.
                    result = ToolResult(
                        tool_id=call.tool_id,
                        tool_name=call.tool_name,
                        success=False,
                        output="",
                        error=(
                            f"Invalid {call.tool_name} call: {exc} "
                            "Fix the arguments and call it again."
                        ),
                    )
                else:
                    pending = _PendingApproval(
                        call.tool_id, prompt.questions,
                    )
                    self._pending = pending
                    try:
                        yield AgentPaused(
                            prompt=prompt,
                            choices=(
                                None
                                if prompt.questions is not None
                                else ["approve", "deny"]
                            ),
                            tool_id=call.tool_id,
                        )
                        result = await self._execute_with_gating(
                            call, verdict,
                        )
                    finally:
                        # A generator closed at the yield never reaches
                        # _execute_with_gating; release the channel here.
                        if self._pending is pending:
                            self._pending = None
            else:
                result = await self._execute_with_gating(call, verdict)

            yield ToolCallEnd(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                input=call.parameters,
                result=result,
            )

            # Build tool_result block for the LLM — errors are marked with
            # ``is_error=True`` so the model knows to adapt rather than
            # retry the same failing call.
            output_text = (
                result.output
                if result.success
                else result.error or "Unknown error"
            )
            tool_result_blocks.append(
                MessageBlock.tool_result_block(
                    call.tool_id,
                    output_text,
                    is_error=not result.success,
                    metadata=result.metadata,
                )
            )

    # ==================================================================
    # Internal helpers
    # ==================================================================

    async def _execute_with_gating(
        self, call: ToolCall, verdict: _Verdict
    ) -> ToolResult:
        """Execute *call* under *verdict*.

        For :attr:`Decision.ASK` the caller (the ``run()`` loop body) has
        already opened a :class:`_PendingApproval` and yielded
        :class:`AgentPaused`, so this method blocks on that channel until
        the user answers it.  An ordinary call is answered with
        approve/deny; an ask-user call with an answer array.  An answered
        ask is then run through the executor exactly like an approved
        call, with the answers injected as ``extra_params``; a declined
        one returns the tool's declined result without executing.
        :attr:`Decision.ALLOW` passes straight through, and
        :attr:`Decision.DENY` never runs at all — it is the policy
        answering, not a question left open.

        A call that does reach the executor takes the verdict's own answer
        about whether it mutates: that is what orders the checkpoint.
        """
        decision = verdict.decision

        # Unknown tool — produce error result directly.
        tool = self._registry.get(call.tool_name)
        if tool is None:
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=f"Unknown tool: '{call.tool_name}'",
            )

        if decision is Decision.DENY:
            logger.warning(
                "Blocked restricted call: %s", call.tool_name
            )
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=blocked_message(tool, call.parameters),
            )

        # Nothing to ask — go straight to execution.
        if decision is Decision.ALLOW:
            return await self._executor.execute(
                call, mutating=verdict.mutating
            )

        # The channel the pause opened.  It stays on the loop until the
        # answer has arrived: a front-end answers on a task of its own,
        # and the loop has usually resumed and be waiting here by then —
        # so taking it off the loop first would drop exactly the answers
        # that come from anywhere but the caller's own thread of control.
        pending = self._pending
        if pending is None:
            # Only reachable if a pause was never yielded for this call:
            # a bug in this file, and not one to run a call over.
            logger.error(
                "Paused call without an approval channel: %s", call.tool_name
            )
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error=(
                    "Internal error: this call was gated but never asked "
                    "about. It was not executed — nothing was approved."
                ),
            )

        try:
            await pending.event.wait()
        finally:
            # Spent even when the generator is closed mid-wait (a
            # cancelled turn): a late or duplicate answer must land
            # nowhere instead of on the next pause.
            if self._pending is pending:
                self._pending = None

        if pending.answers is not None:
            return await self._executor.execute(
                call,
                mutating=verdict.mutating,
                extra_params={"answers": pending.answers},
            )

        if pending.questions is not None:
            # The ask was declined (the terminal's Escape/EOF path).  The
            # tool owns the declined wording; a formatting bug must not
            # take the turn down.
            try:
                result = tool.questions_result(pending.questions, None)
            except Exception as exc:
                result = ToolResult(
                    tool_id=call.tool_id,
                    tool_name=call.tool_name,
                    success=False,
                    output="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            result.tool_id = call.tool_id
            result.tool_name = call.tool_name
            return result

        if not pending.granted:
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error="User denied permission to execute this tool.",
            )

        return await self._executor.execute(
            call, mutating=verdict.mutating
        )

    def _decision_for(self, call: ToolCall) -> _Verdict:
        """The gate's verdict on *call*, with the tier it was read from.

        Both halves are the tool's own answer — its tier, and where its
        files live — so a tool that classifies dynamically (``shell``,
        ``git_branch``) is judged on this call rather than on its class.
        An unclassifiable call asks: a policy that cannot place a call
        has not cleared it.
        """
        tool = self._registry.get(call.tool_name)
        if tool is None:
            return _Verdict(None, Decision.ALLOW)   # the executor errors
        # An ask-user call always pauses, whatever the permission mode and
        # file scope: the pause is input, not a risk decision.  It is
        # non-mutating by declaration, so no checkpoint is taken.
        if tool.asks_user:
            return _Verdict(Permission.READ, Decision.ASK)
        try:
            perm = tool.get_permission(**call.parameters)
            scope = tool.get_file_scope(**call.parameters)
        except Exception:
            logger.exception(
                "Could not classify %s — asking", call.tool_name
            )
            return _Verdict(None, Decision.ASK)
        return _Verdict(perm, self._perm_mgr.decide(perm, scope))
