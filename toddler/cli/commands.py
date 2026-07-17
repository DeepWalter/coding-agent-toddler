"""Slash command parser and dispatcher for the REPL.

Phase 10: Structured slash-command handling extracted from the inline
``_handle_slash_command`` method in :class:`~toddler.cli.app.CLIApp`.

Commands:
    ``/plan``           — Flag the next message for plan mode.
    ``/rollback <id>``  — Rollback files + conversation to a checkpoint.
    ``/checkpoints``    — List checkpoints for the current session.
    ``/session info|list|switch <id>`` — Session management.
    ``/help``           — Show available commands.
    ``/clear``          — Clear the screen.
    ``/quit``, ``/exit``— Exit the REPL.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from toddler.agent.state_machine import AgentStateMachine
    from toddler.checkpoint.manager import CheckpointManager
    from toddler.session.manager import SessionManager
    from toddler.session.models import Session

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
# Callback protocols — allow the dispatcher to remain decoupled from the
# concrete CLIApp while still triggering actions that require its internals.
# ============================================================================


class SessionInfoProvider(Protocol):
    """Protocol for objects that can provide current-session context."""

    @property
    def current_session(self) -> Session | None: ...


# ============================================================================
# SlashCommandDispatcher
# ============================================================================


class SlashCommandDispatcher:
    """Parse and dispatch slash commands entered at the REPL.

    Holds references to the subsystems each command needs and provides a
    single :meth:`dispatch` entry point.  Return values are
    :class:`CommandResult` objects — the caller decides how to render
    messages and whether to exit the REPL.

    Parameters
    ----------
    state_machine:
        Used by ``/plan`` to flag plan-pending.
    session_manager:
        Used by ``/session`` subcommands.  When *None*, session commands
        show a "persistence disabled" message.
    checkpoint_manager_provider:
        An async callable that returns a :class:`CheckpointManager` for the
        current session.  Used by ``/rollback`` and ``/checkpoints``.  We
        use a factory rather than a direct reference because the checkpoint
        manager is session-scoped (needs a ``session_id``).  When *None*,
        checkpoint commands show a "not implemented" message.
    """

    def __init__(
        self,
        *,
        state_machine: AgentStateMachine | None = None,
        session_manager: SessionManager | None = None,
        checkpoint_manager_provider: (
            CheckpointManagerProvider | None
        ) = None,
    ) -> None:
        self._sm = state_machine
        self._session_mgr = session_manager
        self._ckpt_provider = checkpoint_manager_provider

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

    async def _cmd_clear(self, _args: str) -> CommandResult:
        """``/clear`` — clear the screen."""
        # The caller (CLIApp) handles the actual clearing because it owns
        # the Rich Console reference.  We return a sentinel that the caller
        # can check, but for simplicity we just return success with a
        # "clear" flag via convention.
        return CommandResult(
            continue_repl=True,
            message="__CLEAR__",
            rendered=True,
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
        if self._ckpt_provider is None:
            return CommandResult(
                continue_repl=True,
                message="Checkpoints are not available (no checkpoint manager configured).",
            )

        checkpoint_id = args.strip()
        if not checkpoint_id:
            return CommandResult(
                continue_repl=True,
                message="Usage: /rollback <checkpoint_id>",
            )

        try:
            ckpt_mgr = await self._ckpt_provider()
        except Exception as exc:
            return CommandResult(
                continue_repl=True,
                message=f"Failed to create checkpoint manager: {exc}",
            )

        if ckpt_mgr is None:
            return CommandResult(
                continue_repl=True,
                message="No active session — cannot rollback.",
            )

        try:
            result = await ckpt_mgr.rollback_to(checkpoint_id)
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
                message=f"✅ Rolled back to checkpoint `{checkpoint_id[:12]}...`.\n{files}{warnings}",
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
        if self._ckpt_provider is None:
            return CommandResult(
                continue_repl=True,
                message="Checkpoints are not available (no checkpoint manager configured).",
            )

        try:
            ckpt_mgr = await self._ckpt_provider()
        except Exception as exc:
            return CommandResult(
                continue_repl=True,
                message=f"Failed to create checkpoint manager: {exc}",
            )

        if ckpt_mgr is None:
            return CommandResult(
                continue_repl=True,
                message="No active session — no checkpoints to list.",
            )

        try:
            checkpoints = await ckpt_mgr.list_for_session()
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
            f"{'#':>4}  {'ID':<14}  {'Created':<20}  {'Tool':<20}  {'Description'}",
            f"{'─'*4}  {'─'*14}  {'─'*20}  {'─'*20}  {'─'*40}",
        ]
        for ck in checkpoints:
            cid = ck.id[:12]
            ts = ck.created_at.strftime("%Y-%m-%d %H:%M")
            tool = (ck.tool_name or "")[:19]
            desc = (ck.description or "")[:40]
            lines.append(
                f"{ck.sequence_num:>4}  {cid:<14}  {ts:<20}  {tool:<20}  {desc}"
            )

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
        # The caller (CLIApp) has the actual session object.  We return a
        # sentinel that says "render session info" and let CLIApp handle it.
        return CommandResult(
            continue_repl=True,
            message="__SESSION_INFO__",
            rendered=True,
        )

    async def _session_list(self) -> CommandResult:
        """Return session list as a pre-formatted message."""
        if self._session_mgr is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        try:
            sessions = await self._session_mgr.list_all()
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
        if self._session_mgr is None:
            return CommandResult(
                continue_repl=True,
                message="Session persistence is disabled.",
            )
        try:
            session = await self._session_mgr.get(target_id)
        except Exception as exc:
            logger.exception("Failed to switch session.")
            return CommandResult(
                continue_repl=True,
                message=f"Failed to switch session: {exc}",
            )

        if session is None:
            return CommandResult(
                continue_repl=True,
                message=f"Session '{target_id[:16]}...' not found.",
            )

        # We can't actually switch the session from here — that requires
        # updating CLIApp._session.  Return a sentinel with the session ID.
        return CommandResult(
            continue_repl=True,
            message=f"__SESSION_SWITCH__:{session.id}",
            rendered=True,
        )


# ============================================================================
# CheckpointManager provider type
# ============================================================================

CheckpointManagerProvider = Callable[
    [],
    Awaitable["CheckpointManager | None"],
]
"""Async factory that returns a :class:`CheckpointManager` for the current
session, or ``None`` when there is no active session.

Used by :class:`SlashCommandDispatcher` to lazily create a checkpoint manager
on demand (since the session ID is only known at runtime).
"""


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
| `/rollback <id>` | Rollback to a checkpoint (restores files + conversation) |
| `/checkpoints` | List all checkpoints for the current session |
| `/session info` | Show current session details |
| `/session list` | List all saved sessions |
| `/session switch <id>` | Switch to another session |
| `/help` | Show this help text |
| `/clear` | Clear the screen |
| `/quit`, `/exit` | Exit the REPL |
"""


# ============================================================================
# Re-export
# ============================================================================

__all__ = [
    "CommandResult",
    "HELP_TEXT",
    "SlashCommandDispatcher",
]
