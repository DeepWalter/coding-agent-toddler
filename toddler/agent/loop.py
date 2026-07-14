"""Core agent loop — the tool-calling orchestration engine.

Phase 4 implements the basic non-streaming loop: send messages to the LLM,
parse the response, execute any requested tool calls, feed results back,
and repeat until the LLM signals ``end_turn`` or a stop condition is hit.
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
from toddler.agent.stop_conditions import StopConditionChecker
from toddler.llm.types import ContentBlock, Message
from toddler.tools.base import Permission, ToolCall, ToolResult

if TYPE_CHECKING:
    from toddler.config.settings import Settings
    from toddler.llm.base import BaseLLMProvider
    from toddler.tools.executor import ToolExecutor
    from toddler.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default system prompt
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = """\
You are Toddler, a coding assistant that helps with software engineering tasks.

You have access to tools for reading/writing files, running shell commands,
searching code, and interacting with git. Use them to accomplish the user's
request efficiently.

Guidelines:
- Read files before editing them — never guess their contents.
- Use the most specific tool for the job.
- When editing files, match the surrounding code style exactly.
- Report what you did and why after making changes.
- If a tool returns an error, read the error message and adapt — don't
  retry the same failing call.
- If you're unsure about something, ask before acting."""


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
    """

    def __init__(
        self,
        llm_provider: BaseLLMProvider,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        settings: Settings,
    ) -> None:
        self._llm = llm_provider
        self._registry = tool_registry
        self._executor = tool_executor
        self._settings = settings

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
        system_prompt: str | None = None,
        max_iterations: int | None = None,
        token_budget: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent loop for a single user request.

        Parameters
        ----------
        user_input:
            The user's request (plain text).
        system_prompt:
            Custom system prompt.  *None* uses a sensible coding-assistant
            default.
        max_iterations:
            Override the configured max iterations.
        token_budget:
            Hard cap on total tokens consumed across all LLM calls.
        """
        # --- setup ---
        sys_text = (
            system_prompt
            if system_prompt is not None
            else _DEFAULT_SYSTEM_PROMPT
        )
        messages: list[Message] = [
            Message.system(sys_text),
            Message.user(user_input),
        ]
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

            # -- call LLM (non-streaming) ---
            logger.debug(
                f"Iteration {stop_checker.iteration} — "
                f"calling LLM with {len(messages)} messages"
            )
            try:
                response = await self._llm.generate(
                    messages,
                    tools,
                    max_tokens=self._settings.max_tokens_per_response,
                    temperature=self._settings.temperature,
                    stream=False,
                )
            except Exception as exc:
                logger.exception("LLM call failed")
                yield AgentError(message=str(exc), recoverable=True)
                yield AgentFinished(
                    reason=f"LLM error: {exc}", usage=None
                )
                return

            stop_checker.add_tokens(response.usage)

            # -- extract assistant message ---
            assistant_msg = (
                response.messages[0]
                if response.messages
                else Message.assistant()
            )

            # -- yield any text ---
            text = assistant_msg.text
            if text:
                yield TextDelta(text=text)

            # -- handle stop reason ---
            sr = StopConditionChecker.from_llm_stop_reason(
                response.stop_reason
            )
            if sr is not None:
                yield AgentFinished(
                    reason=sr.message, usage=response.usage
                )
                return

            # -- tool_use: execute and feed back ---
            if response.stop_reason == "tool_use":
                tool_calls = self._extract_tool_calls(assistant_msg)

                if not tool_calls:
                    logger.warning(
                        "LLM returned stop_reason=tool_use but no tool "
                        "calls found — stopping."
                    )
                    yield AgentFinished(
                        reason="LLM indicated tool use but produced no "
                        "tool calls.",
                        usage=response.usage,
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
                reason=f"Unexpected stop reason: {response.stop_reason}",
                usage=response.usage,
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
