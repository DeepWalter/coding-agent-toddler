"""Core agent loop — the tool-calling orchestration engine.

Phase 4 implemented the basic non-streaming loop.  Phase 6 adds streaming
support: the agent now calls the LLM with ``stream=True`` by default and
uses :class:`~toddler.agent.handler.StreamHandler` to process the
real-time token stream, yielding :class:`AgentEvent` objects as they arrive.

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
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from toddler.agent.handler import StreamHandler
from toddler.agent.stop_conditions import StopConditionChecker
from toddler.llm.types import ContentBlock, Message, TokenUsage
from toddler.tools.base import Permission, ToolCall, ToolResult

if TYPE_CHECKING:
    from toddler.config.settings import Settings
    from toddler.context.conversation_context import ConversationContext
    from toddler.llm.base import BaseLLMProvider
    from toddler.tools.executor import ToolExecutor
    from toddler.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


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
    session_manager:
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

            if stream:
                # ── streaming path ───────────────────────────────────
                async for event in self._call_llm_streaming(
                    messages, tools,
                ):
                    yield event
                assistant_msg = self._stream_handler.assemble_message()
                stop_reason = (
                    self._stream_handler.stop_reason or "end_turn"
                )
                usage = self._stream_handler.usage or TokenUsage()
            else:
                # ── non-streaming path ───────────────────────────────
                try:
                    assistant_msg, stop_reason, usage = (
                        await self._call_llm_non_streaming(
                            messages, tools,
                        )
                    )
                except Exception as exc:
                    logger.exception("LLM call failed")
                    yield AgentError(
                        message=str(exc), recoverable=True,
                    )
                    yield AgentFinished(
                        reason=f"LLM error: {exc}", usage=None,
                    )
                    return

                # Yield text (all at once in non-streaming mode).
                text = assistant_msg.text
                if text:
                    yield TextDelta(text=text)

            # If the LLM call itself failed fatally, assistant_msg will be
            # None and we should stop.
            if assistant_msg is None:
                yield AgentFinished(
                    reason=f"LLM error: {stop_reason or 'unknown'}",
                    usage=usage,
                )
                return

            stop_checker.add_tokens(usage)

            # -- handle stop reason ---
            sr = StopConditionChecker.from_llm_stop_reason(stop_reason)
            if sr is not None:
                yield AgentFinished(reason=sr.message, usage=usage)
                return

            # -- tool_use: execute and feed back ---
            if stop_reason == "tool_use":
                tool_calls = self._extract_tool_calls(assistant_msg)

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

                messages.append(assistant_msg)

                tool_result_blocks: list[ContentBlock] = []

                for call in tool_calls:
                    yield ToolCallStart(
                        tool_id=call.tool_id,
                        tool_name=call.tool_name,
                        partial_input=call.parameters,
                    )

                    # --- permission gating ---
                    # Create the approval event *before* yielding
                    # AgentPaused so that external code can call
                    # approve/deny immediately without a race.
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

                    # Build tool_result block for the LLM — errors are
                    # marked with ``is_error=True`` so the model knows to
                    # adapt rather than retry the same failing call.
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

    async def _call_llm_streaming(
        self, messages: list[Message], tools: list[dict],
    ) -> AsyncIterator[AgentEvent]:
        """Stream LLM response, yielding events in real time.

        Stores the :class:`StreamHandler` as ``self._stream_handler`` so
        the caller can retrieve the assembled message, stop reason, and
        token usage after the iteration completes.
        """
        stream_iter = await self._llm.generate(
            messages,
            tools,
            max_tokens=self._settings.max_tokens_per_response,
            temperature=self._settings.temperature,
            stream=True,
        )

        self._stream_handler = StreamHandler()
        try:
            async for event in self._stream_handler.process(stream_iter):
                yield event
        except Exception as exc:
            logger.exception("Streaming iteration failed")
            yield AgentError(message=str(exc), recoverable=False)

    async def _call_llm_non_streaming(
        self, messages: list[Message], tools: list[dict],
    ) -> tuple[Message, str, TokenUsage]:
        """Call LLM in non-streaming mode and return the assembled state.

        Returns ``(assistant_msg, stop_reason, usage)``.  The caller is
        responsible for yielding :class:`TextDelta` for any text content
        and for handling exceptions.
        """
        response = await self._llm.generate(
            messages,
            tools,
            max_tokens=self._settings.max_tokens_per_response,
            temperature=self._settings.temperature,
            stream=False,
        )

        assistant_msg = (
            response.messages[0]
            if response.messages
            else Message.assistant()
        )

        return assistant_msg, response.stop_reason, response.usage

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

    @staticmethod
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
