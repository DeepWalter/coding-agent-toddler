"""Core agent loop — the tool-calling orchestration engine.

Supports both streaming and non-streaming modes via
:class:`~toddler.agent.handler.BaseHandler` implementations — use
:func:`~toddler.agent.handler.create_handler` to get the right handler
for the current mode.  The loop yields :class:`AgentEvent` objects as
they arrive (real-time in streaming mode, or batched from the full
response in non-streaming mode).

The loop receives a single :class:`~toddler.context.ConversationContext`
instance that handles system prompt assembly, context window tracking,
compaction, and persistence — keeping the orchestration layer clean.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from toddler.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    AgentPaused,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.handler import create_handler
from toddler.agent.stop_conditions import StopConditionChecker
from toddler.llm import ContentBlock, Message, TokenUsage
from toddler.tools.base import Permission, ToolCall, ToolResult

if TYPE_CHECKING:
    from toddler.agent.handler import BaseHandler
    from toddler.config.settings import Settings
    from toddler.context.conversation_context import ConversationContext
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
    for block in msg.content:
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


class AgentLoop:
    """Orchestrates the core tool-calling loop.

    Runs as an async generator yielding :class:`AgentEvent` objects.  The
    CLI layer (Phase 5) iterates over these to drive the display.

    Permission gating is done *inline* in the loop body so that
    :class:`AgentPaused` events can be yielded naturally.  The
    :class:`ToolExecutor` should be configured to auto-approve everything
    (the loop pre-gates before calling it).

    Parameters
    ----------
    llm_provider:
        The LLM backend.
    tool_registry:
        Registry of available tools.
    tool_executor:
        Executor that runs tool calls (with checkpoint hooks if configured).
    settings:
        Resolved settings (limits, permissions, etc.).
    system_prompt_builder:
        Optional :class:`SystemPromptBuilder` for layered system prompts.
        When *None*, a default builder with no project map or memory is used.
    context_window_mgr:
        Optional :class:`ContextWindowManager` for token tracking and
        compaction/truncation triggers.  When *None*, context management
        is skipped.
    conversation_compactor:
        Optional :class:`ConversationCompactor` for LLM-powered conversation
        summarisation.  Required when *context_window_mgr* is provided and
        you want automatic compaction.
    storage_manager:
        Optional :class:`StorageManager` for persisting compaction results.
        When provided, compacted messages are written back to the session
        store so the compaction survives restarts.
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        settings: Settings,
        *,
        context: ConversationContext,
    ) -> None:
        self._llm = llm_provider
        self._registry = tool_registry
        self._executor = tool_executor
        self._settings = settings

        # Single context object — handles prompt building, window tracking,
        # compaction, and persistence.
        self._ctx = context

        # Confirmation gate — see _execute_with_gating for the protocol.
        self._approval_event: asyncio.Event | None = None
        self._approval_granted: bool = False

    # ==================================================================
    # Public API
    # ==================================================================

    async def run(  # noqa: C901
        self,
        user_input: str,
        *,
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
        messages = await self._ctx.prepare_turn(user_input, mode)
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

            # -- context window management ---
            await self._ctx.check_and_compact()

            # -- call LLM ---
            logger.debug(
                f"Iteration {stop_checker.iteration} — "
                f"calling LLM with {len(messages)} messages "
                f"(stream={stream})"
            )

            llm_result: dict[str, Message | None | str | TokenUsage] = {}
            async for event in self._call_llm(
                messages, tools, stream=stream,
                handler=handler, llm_result=llm_result,
            ):
                yield event

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
            # (the coordinator no longer reconstructs this from events).
            # Skip empty messages — they can occur on streaming errors where
            # the handler assembled no text and no tool calls.
            if assistant_msg.content:
                messages.append(assistant_msg)

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

                tool_result_blocks: list[ContentBlock] = []
                async for event in self._execute_tool_calls(
                    tool_calls, tool_result_blocks,
                ):
                    yield event

                messages.append(Message.tool(tool_result_blocks))

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

    async def approve_tool_call(self, tool_id: str = "") -> None:  # noqa: ARG002
        """Approve the pending tool confirmation and unblock the loop."""
        self._approval_granted = True
        self._signal_approval()

    async def deny_tool_call(self, tool_id: str = "") -> None:  # noqa: ARG002
        """Deny the pending tool confirmation and unblock the loop."""
        self._approval_granted = False
        self._signal_approval()

    def _signal_approval(self) -> None:
        if self._approval_event is not None:
            self._approval_event.set()

    # ==================================================================
    # LLM calling helpers
    # ==================================================================

    async def _call_llm(
        self, messages: list[Message], tools: list[dict], *,
        stream: bool, handler: BaseHandler, llm_result: dict,
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
                max_tokens=self._settings.max_tokens_per_response,
                temperature=self._settings.temperature,
                stream=stream,
            )
            async for event in handler.process(response):
                yield event
            llm_result.update(handler.get_final_result())
        except Exception as exc:
            logger.exception("LLM call failed")
            yield AgentError(message=str(exc), recoverable=True)
            llm_result["assistant_msg"] = None
            llm_result["stop_reason"] = f"LLM error: {exc}"
            llm_result["usage"] = TokenUsage()

    # ==================================================================
    # Tool execution
    # ==================================================================

    async def _execute_tool_calls(
        self, tool_calls: list[ToolCall],
        tool_result_blocks: list[ContentBlock],
    ) -> AsyncIterator[AgentEvent]:
        """Execute *tool_calls* with permission gating, yielding events.

        Yields :class:`ToolCallStart`, :class:`AgentPaused` (when
        confirmation is needed), and :class:`ToolCallEnd` for each call.
        Appends a :class:`ContentBlock` to *tool_result_blocks* for each
        completed call — the caller then feeds them back to the LLM.
        """
        tool_result_blocks.clear()

        for call in tool_calls:
            yield ToolCallStart(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                partial_input=call.parameters,
            )

            # --- permission gating ---
            # Create the approval event *before* yielding AgentPaused so
            # that external code can call approve/deny immediately without
            # a race.
            if self._needs_confirmation_for(call):
                self._approval_event = asyncio.Event()
                self._approval_granted = False

                tool = self._registry.get(call.tool_name)
                summary = (
                    tool.summarize_call(**call.parameters)
                    if tool
                    else f"{call.tool_name}(...)"
                )
                yield AgentPaused(
                    prompt=f"Allow {summary}?",
                    choices=["approve", "deny"],
                )

            result = await self._execute_with_gating(call)

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
                ContentBlock.tool_result_block(
                    call.tool_id,
                    output_text,
                    is_error=not result.success,
                )
            )

    # ==================================================================
    # Internal helpers
    # ==================================================================

    async def _execute_with_gating(self, call: ToolCall) -> ToolResult:
        """Execute *call*, respecting the confirmation gate.

        When the caller (the ``run()`` loop body) has determined that
        confirmation is needed, it creates ``_approval_event`` and yields
        :class:`AgentPaused` **before** calling this method.  This method
        then blocks on the event until :meth:`approve_tool_call` or
        :meth:`deny_tool_call` is called externally.

        When no confirmation is needed the call is passed straight through
        to the executor.
        """
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

        perm = tool.get_permission(**call.parameters)

        # If confirmation is not needed, go straight to execution.
        if not self._needs_confirmation(perm):
            return await self._executor.execute(call)

        # Confirmation is needed — the event was already created by run().
        # Wait for external approval.
        if self._approval_event is not None:
            await self._approval_event.wait()

        if not self._approval_granted:
            return ToolResult(
                tool_id=call.tool_id,
                tool_name=call.tool_name,
                success=False,
                output="",
                error="User denied permission to execute this tool.",
            )

        return await self._executor.execute(call)

    def _needs_confirmation(self, perm: Permission) -> bool:
        """Return ``True`` when *perm* requires user confirmation.

        Mirrors :meth:`ToolExecutor._check_permission`.
        """
        if perm == Permission.READ:
            return not self._settings.auto_approve_read
        if perm == Permission.SHELL_SAFE:
            return False
        if perm == Permission.WRITE:
            return self._settings.confirm_write
        if perm == Permission.SHELL_DANGEROUS:
            return self._settings.confirm_shell_dangerous
        return True  # unknown — be safe

    def _needs_confirmation_for(self, call: ToolCall) -> bool:
        """Shorthand: does *call* need user confirmation?"""
        tool = self._registry.get(call.tool_name)
        if tool is None:
            return False  # executor will produce the error
        perm = tool.get_permission(**call.parameters)
        return self._needs_confirmation(perm)
