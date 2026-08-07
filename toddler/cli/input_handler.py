"""prompt_toolkit input handler — REPL history, multi-line input, autocomplete."""  # noqa: E501

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style

# ---------------------------------------------------------------------------
# Slash commands known to the REPL
# ---------------------------------------------------------------------------

_SLASH_COMMANDS: dict[str, str] = {
    "/plan": "Enter plan mode — agent researches and proposes a plan",
    "/mode": "Show or switch mode — /mode [plan|manual|auto]",
    "/view": "View full output from a turn — /view <turn_number>",
    "/clear": "Archive conversation and start fresh — /clear [title]",
    "/resume": "Resume an archived conversation — /resume <conversation_id>",
    "/conversations": "List conversations in the current session",
    "/rollback": "Rollback to a checkpoint — /rollback <checkpoint_id>",
    "/checkpoints": "List saved checkpoints for the current session",
    "/session": "Session management — /session info|list|switch <id>",
    "/help": "Show available slash commands",
    "/quit": "Exit the REPL",
    "/exit": "Exit the REPL",
    "/q": "Exit the REPL",
}


# ---------------------------------------------------------------------------
# Slash-command autocompleter
# ---------------------------------------------------------------------------


# Sub-option autocomplete entries: command → [(sub_option, description), ...]
_SUB_OPTIONS: dict[str, list[tuple[str, str]]] = {
    "/mode": [
        ("plan", "Research → propose → execute"),
        ("manual", "Confirm WRITE tools (default)"),
        ("auto", "Auto-approve WRITE tools; SHELL_DANGEROUS still confirms"),
    ],
    "/session": [
        ("info", "/session info"),
        ("list", "/session list"),
        ("switch", "/session switch <id>"),
    ],
}


class SlashCommandCompleter(Completer):
    """Autocomplete slash commands and their sub-options."""

    def __init__(self) -> None:
        self._commands = _SLASH_COMMANDS

    def get_completions(self, document: Document, complete_event):  # noqa: ARG002
        text = document.text_before_cursor.lstrip()

        # Only offer slash commands when the line starts with "/"
        if not text.startswith("/"):
            return

        words = text.split()
        if len(words) == 1:
            yield from self._complete_command(words[0])
        elif len(words) == 2:
            yield from self._complete_sub_option(words[0], words[1])

    def _complete_command(self, prefix: str):
        """Yield completions for the command name itself."""
        for cmd, desc in self._commands.items():
            if cmd.startswith(prefix):
                yield Completion(
                    cmd,
                    start_position=-len(prefix),
                    display_meta=desc,
                )

    def _complete_sub_option(self, cmd: str, prefix: str):
        """Yield completions for sub-options of a command."""
        for sub, meta in _SUB_OPTIONS.get(cmd, ()):
            if sub.startswith(prefix):
                yield Completion(
                    sub,
                    start_position=-len(prefix),
                    display_meta=meta,
                )


# ---------------------------------------------------------------------------
# Key bindings
# ---------------------------------------------------------------------------


def _create_key_bindings() -> KeyBindings:
    """Create REPL key bindings.

    - **Alt+Enter** or **Escape Enter**: Insert a newline for multi-line input.
    - **Enter**: Submit the current input.
    """
    kb = KeyBindings()

    @kb.add("escape", "enter", eager=True)
    def _(event):
        """Escape + Enter → insert a newline (multi-line input)."""
        event.current_buffer.insert_text("\n")

    @kb.add("c-d", eager=True)
    def _(event):
        """Ctrl+D on an empty line → exit."""
        if not event.current_buffer.text:
            event.app.exit(result=None)

    return kb


# ---------------------------------------------------------------------------
# Prompt style
# ---------------------------------------------------------------------------


_PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "bold green",
        "continuation": "dim",
        "toolbar": "bg:#333333 #aaaaaa",
        "bottom-toolbar": "bg:#222222 #888888",
    }
)


# ---------------------------------------------------------------------------
# InputHandler
# ---------------------------------------------------------------------------


class InputHandler:
    """Wraps :mod:`prompt_toolkit` for the interactive REPL.

    Provides history persistence, multi-line input, slash-command
    autocompletion, and a status toolbar.

    Parameters
    ----------
    history_file:
        Path to the history file.  Defaults to
        ``~/.toddler/history``.  Created on first use.
    """

    def __init__(self, history_file: Path | None = None) -> None:
        self._history_file = (
            Path(history_file).expanduser()
            if history_file
            else Path.home() / ".toddler" / "history"
        )

    # ------------------------------------------------------------------
    # Input loop
    # ------------------------------------------------------------------

    async def prompt(
        self,
        message: str = "tod> ",
        *,
        bottom_toolbar: str | None = None,
    ) -> str | None:
        """Display a REPL prompt and return the user's input.

        Returns ``None`` when the user exits (Ctrl+D on empty line).
        """
        # Ensure the directory for the history file exists.
        self._history_file.parent.mkdir(parents=True, exist_ok=True)

        session = PromptSession(
            history=FileHistory(str(self._history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=SlashCommandCompleter(),
            key_bindings=_create_key_bindings(),
            style=_PROMPT_STYLE,
            # Enter submits; Alt+Enter / Esc+Enter inserts newline
            multiline=False,
            message=self._format_prompt(message),
            bottom_toolbar=bottom_toolbar or self._default_toolbar(),
        )

        try:
            result = await session.prompt_async()
            return result
        except EOFError:
            return None
        # KeyboardInterrupt propagates to the caller so the REPL can
        # print a message and continue instead of exiting.

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    @staticmethod
    def _default_toolbar() -> str:
        return (
            " Alt+Enter: newline │ Ctrl+D: exit │ "
            "/plan /clear /resume /rollback /session /help /quit "
        )

    @staticmethod
    def _format_prompt(message: str) -> list[tuple[str, str]]:
        """Return prompt_toolkit ``(style, text)`` tuples for the prompt."""
        return [("class:prompt", message)]
