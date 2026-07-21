"""Slash command parser and dispatcher for the REPL.

Phase 10: Structured slash-command handling extracted from the inline
``_handle_slash_command`` method in :class:`~toddler.cli.app.CLIApp`.

Commands:
    ``/plan``           — Flag the next message for plan mode.
    ``/rollback <id>``  — Rollback files + conversation to a checkpoint.
    ``/checkpoints``    — List checkpoints for the current session.
    ``/session info|list|switch <id>`` — Session management.
    ``/help``           — Show available commands.
    ``/clear``          — Archive conversation and start fresh.
    ``/quit``, ``/exit``— Exit the REPL.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toddler.agent.state_machine import AgentStateMachine
    from toddler.session.coordinator import SessionCoordinator

__all__ = [
    "CommandResult",
    "HELP_TEXT",
    "SlashCommandDispatcher",
]

logger = logging.getLogger(__name__)


# ============================================================================
# Command result types
# ============================================================================


@dataclass
class CommandResult:
    """The outcome of dispatching a slash command.

    Parameters
    ----------
    continue_repl:
        ``True`` to keep the REPL running, ``False`` to exit.
    message:
        Optional status / error message for the user.
    rendered:
        ``True`` when the command handler has already rendered its output
        (so the caller should not add extra formatting).
    """

    continue_repl: bool = True
    message: str = ""
    rendered: bool = False


# ============================================================================
# SlashCommandDispatcher
# ============================================================================


class SlashCommandDispatcher:
    """Parse and dispatch slash commands entered at the REPL.

    Holds references to the subsystems each command needs and provides a
    single :meth:`dispatch` entry point.  Return values are
    :class:`CommandResult` objects — the caller decides how to render
    messages and whether to exit the REPL.

    Session and conversation commands make direct calls on the
    :class:`~toddler.session.coordinator.SessionCoordinator` instead of
    returning sentinel strings.

    Parameters
    ----------
    state_machine:
        Used by ``/plan`` to flag plan-pending.
    session_coordinator:
        Used by ``/session``, ``/clear``, ``/resume``,
        ``/conversations``, ``/rollback``, and ``/checkpoints`` commands.
        When *None*, those commands show a "persistence disabled" message.
    """

    def __init__(
        self,
        *,
        state_machine: AgentStateMachine | None = None,
        session_coordinator: SessionCoordinator | None = None,
    ) -> None:
        self._sm = state_machine
        self._coordinator = session_coordinator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def dispatch(self, text: str) -> CommandResult:
        """Parse and execute a single slash-command string.

        Parameters
        ----------
        text:
            The full command line, e.g. ``"/session switch abc123"``.

        Returns
        -------
        CommandResult
            Describes whether to continue the REPL, any message to display,
            and whether the handler already rendered output.
        """
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = _COMMAND_TABLE.get(cmd)
        if handler is None:
            return CommandResult(
                continue_repl=True,
                message=(
                    f"Unknown command: {cmd}.  "
                    f"Type /help for available commands."
                ),
            )

        try:
            return await handler(self, args)
        except Exception:
            logger.exception(f"Command '{cmd}' failed.")
            return CommandResult(
                continue_repl=True,
                message=f"Command '{cmd}' failed — check logs for details.",
            )

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_quit(self, _args: str) -> CommandResult:
        """``/quit``, ``/exit``, ``/q`` — exit the REPL."""
        return CommandResult(continue_repl=False, message="Goodbye.")

    async def _cmd_clear(self, args: str) -> CommandResult:
        """``/clear [title]`` — archive current conversation and start fresh.

        If an optional title is provided, it is set on the current
        conversation before archiving.
        """
        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        title = args.strip() or None
        await self._coordinator.new_conversation(title)
        return CommandResult(
            continue_repl=True,
            message=(
                "Started new conversation. "
                "Your previous conversation was archived."
            ),
        )

    async def _cmd_help(self, _args: str) -> CommandResult:
        """``/help`` — show available commands."""
        return CommandResult(
            continue_repl=True,
            message="__HELP__",
            rendered=True,
        )

    async def _cmd_plan(self, _args: str) -> CommandResult:
        """``/plan`` — flag the next message for plan mode."""
        if self._sm is not None:
            self._sm.flag_plan_pending()
            return CommandResult(
                continue_repl=True,
                message=(
                    "Plan mode enabled — your next message will trigger "
                    "research and plan proposal."
                ),
            )
        return CommandResult(
            continue_repl=True,
            message="Plan mode will be triggered on your next message.",
        )

    async def _cmd_rollback(self, args: str) -> CommandResult:
        """``/rollback <checkpoint_id>`` — rollback to a checkpoint."""
        checkpoint_id = args.strip()
        if not checkpoint_id:
            return CommandResult(
                continue_repl=True,
                message="Usage: /rollback <checkpoint_id>",
            )

        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Checkpoints are not available (no session manager configured).",  # noqa: E501
            )

        try:
            result = await self._coordinator.rollback_to(checkpoint_id)
        except ValueError as exc:
            return CommandResult(
                continue_repl=True,
                message=f"Rollback failed: {exc}",
            )
        except Exception as exc:
            logger.exception("Rollback failed.")
            return CommandResult(
                continue_repl=True,
                message=f"Rollback failed: {exc}",
            )

        if result.success:
            files = (
                f"Restored {len(result.restored_files)} file(s)."
                if result.restored_files
                else "No files to restore."
            )
            warnings = (
                f"\nWarnings: {', '.join(result.warnings)}"
                if result.warnings
                else ""
            )
            return CommandResult(
                continue_repl=True,
                message=f"✅ Rolled back to checkpoint `{checkpoint_id[:12]}...`.\n{files}{warnings}",  # noqa: E501
            )
        return CommandResult(
            continue_repl=True,
            message=(
                f"Rollback partially failed.\n"
                f"Warnings: {', '.join(result.warnings)}"
                if result.warnings
                else "Rollback failed with no details."
            ),
        )

    async def _cmd_checkpoints(self, _args: str) -> CommandResult:
        """``/checkpoints`` — list checkpoints for the current session."""
        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Checkpoints are not available (no session manager configured).",  # noqa: E501
            )

        try:
            checkpoints = await self._coordinator.list_checkpoints()
        except ValueError as exc:
            return CommandResult(
                continue_repl=True,
                message=str(exc),
            )
        except Exception as exc:
            logger.exception("Failed to list checkpoints.")
            return CommandResult(
                continue_repl=True,
                message=f"Failed to list checkpoints: {exc}",
            )

        if not checkpoints:
            return CommandResult(
                continue_repl=True,
                message="No checkpoints for the current session.",
            )

        lines: list[str] = [
            f"{'#':>4}  {'ID':<14}  {'Created':<20}  {'Tool':<20}  {'Description'}",  # noqa: E501
            f"{'─'*4}  {'─'*14}  {'─'*20}  {'─'*20}  {'─'*40}",
        ]
        for ck in checkpoints:
            cid = ck.id[:12]
            ts = ck.created_at.strftime("%Y-%m-%d %H:%M")
            tool = (ck.tool_name or "")[:19]
            desc = (ck.description or "")[:40]
            lines.append(
                f"{ck.sequence_num:>4}  {cid:<14}  {ts:<20}  {tool:<20}  {desc}"  # noqa: E501
            )

        return CommandResult(
            continue_repl=True,
            message="\n".join(lines),
        )

    async def _cmd_resume(self, args: str) -> CommandResult:
        """``/resume <conversation_id>`` — resume an archived conversation."""
        conv_id = args.strip()
        if not conv_id:
            return CommandResult(
                continue_repl=True,
                message="Usage: /resume <conversation_id>",
            )
        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        try:
            await self._coordinator.resume_conversation(conv_id)
            return CommandResult(
                continue_repl=True,
                message="Resumed conversation.",
            )
        except ValueError as exc:
            return CommandResult(
                continue_repl=True,
                message=str(exc),
            )

    async def _cmd_conversations(self, _args: str) -> CommandResult:
        """``/conversations`` — list conversations in the current session."""
        if self._coordinator is None or self._coordinator.session is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )

        mgr = self._coordinator.storage_manager
        convs = await mgr.list_conversations(self._coordinator.session.id)
        if not convs:
            return CommandResult(
                continue_repl=True,
                message="No conversations in this session.",
            )

        active_id = (
            self._coordinator.context.conversation.id
            if self._coordinator.context
            and self._coordinator.context.conversation
            else None
        )

        lines: list[str] = [
            f"{'':>1}  {'ID':<34}  {'Title':<40}  {'Msgs':>5}  {'Age':<10}",
            f"{'─'*1}  {'─'*34}  {'─'*40}  {'─'*5}  {'─'*10}",
        ]
        for c in convs:
            marker = "*" if c.id == active_id else " "
            sid = c.id[:32]
            title = (c.display_title or "—")[:39]
            lines.append(
                f"{marker:<1}  {sid:<34}  {title:<40}  {c.message_count:>5}  {c.age:<10}"  # noqa: E501
            )
        lines.append("* = active conversation")

        return CommandResult(
            continue_repl=True,
            message="\n".join(lines),
        )

    async def _cmd_session(self, args: str) -> CommandResult:
        """``/session <subcommand>`` — session management."""
        sub = args.strip().lower()

        if sub == "info" or not sub:
            return await self._session_info()
        if sub == "list":
            return await self._session_list()
        if sub.startswith("switch "):
            target = sub[7:].strip()
            return await self._session_switch(target)

        return CommandResult(
            continue_repl=True,
            message=(
                f"Unknown /session subcommand: '{sub}'. "
                f"Available: info, list, switch <id>."
            ),
        )

    # ------------------------------------------------------------------
    # Session subcommand helpers
    # ------------------------------------------------------------------

    async def _session_info(self) -> CommandResult:
        """Return session info as a pre-formatted message."""
        s = self._coordinator.session if self._coordinator else None
        if s is None:
            return CommandResult(
                continue_repl=True,
                message="No active session (persistence disabled).",
            )

        lines = [
            f"  {'ID':<16}  {s.id}",
            f"  {'Title':<16}  {s.title or '—'}",
            f"  {'Mode':<16}  {s.mode}",
            f"  {'Messages':<16}  {s.message_count}",
            f"  {'Input tokens':<16}  {s.total_input_tokens}",
            f"  {'Output tokens':<16}  {s.total_output_tokens}",
            f"  {'Created':<16}  {s.created_at.strftime('%Y-%m-%d %H:%M UTC')}",  # noqa: E501
        ]
        return CommandResult(
            continue_repl=True,
            message="\n".join(lines),
        )

    async def _session_list(self) -> CommandResult:
        """Return session list as a pre-formatted message."""
        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        try:
            sessions = await self._coordinator.storage_manager.list_all()
        except Exception as exc:
            logger.exception("Failed to list sessions.")
            return CommandResult(
                continue_repl=True,
                message=f"Failed to list sessions: {exc}",
            )

        if not sessions:
            return CommandResult(
                continue_repl=True,
                message="No saved sessions.",
            )

        lines: list[str] = [
            f"{'ID':<34}  {'Title':<40}  {'Msgs':>5}  {'Age':<10}",
            f"{'─'*34}  {'─'*40}  {'─'*5}  {'─'*10}",
        ]
        for s in sessions:
            sid = s.id[:32]
            title = (s.display_title or "—")[:39]
            lines.append(
                f"{sid:<34}  {title:<40}  {s.message_count:>5}  {s.age:<10}"
            )

        return CommandResult(
            continue_repl=True,
            message="\n".join(lines),
        )

    async def _session_switch(self, target_id: str) -> CommandResult:
        """Switch to a different session."""
        if self._coordinator is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        try:
            await self._coordinator.switch_session(target_id)
            s = self._coordinator.session
            return CommandResult(
                continue_repl=True,
                message=(
                    f"Switched to session {s.id[:12]}... "
                    f"({s.message_count} messages)"
                ),
            )
        except ValueError as exc:
            return CommandResult(
                continue_repl=True,
                message=str(exc),
            )
        except Exception as exc:
            logger.exception("Failed to switch session.")
            return CommandResult(
                continue_repl=True,
                message=f"Failed to switch session: {exc}",
            )


# ============================================================================
# Command handler table
# ============================================================================

_Handler = Callable[
    [SlashCommandDispatcher, str],
    Awaitable[CommandResult],
]

_COMMAND_TABLE: dict[str, _Handler] = {
    "/quit": SlashCommandDispatcher._cmd_quit,
    "/exit": SlashCommandDispatcher._cmd_quit,
    "/q": SlashCommandDispatcher._cmd_quit,
    "/clear": SlashCommandDispatcher._cmd_clear,
    "/help": SlashCommandDispatcher._cmd_help,
    "/plan": SlashCommandDispatcher._cmd_plan,
    "/resume": SlashCommandDispatcher._cmd_resume,
    "/conversations": SlashCommandDispatcher._cmd_conversations,
    "/rollback": SlashCommandDispatcher._cmd_rollback,
    "/checkpoints": SlashCommandDispatcher._cmd_checkpoints,
    "/session": SlashCommandDispatcher._cmd_session,
}


# ============================================================================
# Help text
# ============================================================================

HELP_TEXT = """\
### Slash Commands

| Command | Description |
|---------|-------------|
| `/plan` | Flag the next message for plan mode (research → propose → execute) |
| `/clear [title]` | Archive current conversation and start a fresh one |
| `/resume <id>` | Resume a previously archived conversation |
| `/conversations` | List conversations in the current session |
| `/rollback <id>` | Rollback to a checkpoint (restores files + conversation) |
| `/checkpoints` | List all checkpoints for the current session |
| `/session info` | Show current session details |
| `/session list` | List all saved sessions |
| `/session switch <id>` | Switch to another session |
| `/help` | Show this help text |
| `/quit`, `/exit` | Exit the REPL |
"""  # noqa: E501
