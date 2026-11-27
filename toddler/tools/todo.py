"""The general todo list — state, validation, and the ``todo_write`` tool.

The list is the agent's live progress record for any execution turn,
whether or not an approved plan is running.  The model sends the whole
list on every call; the session emits a
:class:`~toddler.agent.events.TodoUpdate` whenever it changes, so the
frontends render a checklist without a per-item protocol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from toddler.tools.base import BaseTool, ToolResult
from toddler.tools.policy import FileScopePolicy, Permission
from toddler.tools.registry import TOOL_CATALOG

__all__ = [
    "MAX_TODOS",
    "TODO_STATUSES",
    "TODO_TOOL_NAME",
    "TODO_WRITE_USAGE",
    "TodoItem",
    "TodoState",
    "TodoStatus",
    "TodoValidationError",
    "TodoWriteTool",
    "parse_todos",
]

logger = logging.getLogger(__name__)

#: The tool's name — the session registers it by this key during
#: execution phases and removes it when they end.
TODO_TOOL_NAME = "todo_write"

#: Most items one list may hold.  A longer list is a plan, not a status
#: record, and stops fitting the progress panel.
MAX_TODOS = 20


class TodoStatus(StrEnum):
    """Canonical todo status values — the single source of truth for the
    schema, validation, and the renderers."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


# All statuses in lifecycle order, as plain strings.  Derived from the
# enum so a vocabulary change propagates.
TODO_STATUSES = tuple(status.value for status in TodoStatus)

#: The model-facing protocol.  Embedded in the tool description, which is
#: what the model reads at call time.
TODO_WRITE_USAGE = (
    "Use this to keep the user informed while you work. Send the complete "
    "list on every call — it replaces the previous one. Keep exactly one "
    "item in_progress while you work on it, mark it completed as soon as "
    "it is done, and leave later work pending. Clear the list with an empty "
    "array when the task is finished. Keep items short and concrete."
)


class TodoValidationError(ValueError):
    """A model-authored todo list that cannot be stored.

    Raised while parsing; :class:`TodoWriteTool` turns it into a failed
    tool result the model can correct.
    """


@dataclass(frozen=True)
class TodoItem:
    """One row of the todo list: what to do, and where it stands."""

    content: str
    status: TodoStatus

    def to_dict(self) -> dict:
        """The wire form — the content the row shows, the status it marks."""
        return {"content": self.content, "status": self.status.value}


def _text(value: object, where: str) -> str:
    """*value* as non-empty single-line text, or a validation error."""
    if not isinstance(value, str):
        raise TodoValidationError(f"{where} must be a string.")
    text = " ".join(value.split())
    if not text:
        raise TodoValidationError(f"{where} must not be empty.")
    return text


def parse_todos(raw: object) -> tuple[TodoItem, ...]:
    """Validate a model-authored ``todos`` argument.

    Returns the normalized items, or raises :class:`TodoValidationError`
    with a message the model can act on.  An empty list is valid: it
    clears the list.  The bounds and the one-in-progress rule are enforced
    here, not just in the JSON schema — an OpenAI-compatible endpoint may
    ignore them.
    """
    if not isinstance(raw, (list, tuple)):
        raise TodoValidationError("todos must be an array of todo objects.")
    if len(raw) > MAX_TODOS:
        raise TodoValidationError(
            f"todo_write takes at most {MAX_TODOS} items; got {len(raw)}."
        )

    items: list[TodoItem] = []
    for index, item in enumerate(raw, 1):
        where = f"todo {index}"
        if not isinstance(item, dict):
            raise TodoValidationError(
                f"{where} must be an object with content and status."
            )
        content = _text(item.get("content"), f"{where} content")
        status_raw = item.get("status")
        try:
            status = TodoStatus(status_raw)
        except ValueError:
            raise TodoValidationError(
                f"{where} status must be one of "
                f"{', '.join(TODO_STATUSES)}; got {status_raw!r}."
            ) from None
        items.append(TodoItem(content=content, status=status))

    in_progress = sum(
        1 for item in items if item.status is TodoStatus.IN_PROGRESS
    )
    if in_progress > 1:
        raise TodoValidationError(
            "At most one todo may be in_progress; mark the others pending "
            "or completed."
        )
    return tuple(items)


@dataclass
class TodoState:
    """The session's live todo list.

    The tool replaces the whole list; :meth:`take_update` returns it only
    when the content changed, so the session emits one frame per change and
    never repeats one.  An empty list is a real state — it clears the
    checklist — not "no update".
    """

    _items: tuple[TodoItem, ...] = ()
    _last_emitted: tuple[TodoItem, ...] = ()

    @property
    def items(self) -> tuple[TodoItem, ...]:
        """The current list, in the model's order."""
        return self._items

    def replace(self, items: tuple[TodoItem, ...]) -> None:
        """Replace the list with *items* (which may be empty)."""
        self._items = tuple(items)

    def take_update(self) -> tuple[TodoItem, ...] | None:
        """Return the complete list when it changed, else ``None``.

        The returned value is a snapshot: the session wraps it into a
        :class:`~toddler.agent.events.TodoUpdate`, and a renderer replaces
        its display from it without diffing.
        """
        if self._last_emitted == self._items:
            return None
        self._last_emitted = self._items
        return self._items

    def reset(self) -> None:
        """Clear the list and its emitted snapshot."""
        self._items = ()
        self._last_emitted = ()


@TOOL_CATALOG.register()
class TodoWriteTool(BaseTool):
    """Replace the todo list for the current task.

    Bookkeeping only — ``READ`` tier, so it never pauses.  The session
    registers it during execution phases and emits a ``TodoUpdate``
    whenever the list changes.  It opts out of the default registry because
    it mutates the session's shared :class:`TodoState`.
    """

    name = TODO_TOOL_NAME
    description = (
        "Replace the todo list for the current task. " + TODO_WRITE_USAGE
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "maxItems": MAX_TODOS,
                "description": (
                    "The complete list, in order. An empty array clears it."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Short, concrete task text.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(TODO_STATUSES),
                            "description": "Where this item stands.",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    #: ``todos`` is the whole of what the call records — no call-description
    #: param.
    require_description_parameter = False

    #: The checklist is the display; a card for the call would repeat it.
    visible = False

    #: Registered per execution phase by the session, never handed to
    #: every session like a default tool.
    in_default_registry = False

    def __init__(
        self,
        todo_state: TodoState | None = None,
        *,
        scope_policy: FileScopePolicy | None = None,
    ) -> None:
        # The default covers definition-time registration, which constructs
        # with no arguments; a session always passes the state it shares
        # with the manager.
        super().__init__(scope_policy=scope_policy)
        self._todo_state = (
            todo_state if todo_state is not None else TodoState()
        )

    @property
    def permission(self) -> Permission:
        """Bookkeeping only — never gated."""
        return Permission.READ

    async def execute(self, todos: object) -> ToolResult:
        """Validate and store the complete list, returning a short ack."""
        try:
            items = parse_todos(todos)
        except TodoValidationError as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Invalid todo_write call: {exc} "
                    "Send the complete corrected list."
                ),
            )
        self._todo_state.replace(items)
        output = (
            "Todo list cleared."
            if not items
            else f"Todo list updated ({len(items)} items)."
        )
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=output,
        )

    def summarize_call(self, **kwargs) -> str:
        """A log line that counts the items rather than dumping them."""
        raw = kwargs.get("todos")
        count = len(raw) if isinstance(raw, (list, tuple)) else 0
        return f"todo_write({count} item{'' if count == 1 else 's'})"
