"""Tests for the general todo list — validation, state, and the tool."""

from __future__ import annotations

import pytest

from tests.mocks import (
    MockLLMProvider,
    text_response,
    tool_use_response,
)
from toddler.agent.events import TodoUpdate
from toddler.config.settings import Settings
from toddler.session.database import SQLiteDatabase
from toddler.session.manager import SessionManager
from toddler.session.storage import StorageManager
from toddler.tools import HIDDEN_TOOL_NAMES
from toddler.tools.policy import Permission
from toddler.tools.todo import (
    MAX_TODOS,
    TODO_STATUSES,
    TodoItem,
    TodoState,
    TodoStatus,
    TodoValidationError,
    TodoWriteTool,
    parse_todos,
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_parse_todos_normalizes_content():
    items = parse_todos([
        {"content": "  first   item ", "status": "in_progress"},
        {"content": "second item", "status": "pending"},
    ])
    assert items == (
        TodoItem("first item", TodoStatus.IN_PROGRESS),
        TodoItem("second item", TodoStatus.PENDING),
    )


def test_parse_todos_empty_list_is_valid():
    assert parse_todos([]) == ()


def test_parse_todos_rejects_more_than_the_cap():
    raw = [
        {"content": f"item {i}", "status": "pending"}
        for i in range(MAX_TODOS + 1)
    ]
    with pytest.raises(TodoValidationError, match="at most"):
        parse_todos(raw)


def test_parse_todos_rejects_two_in_progress():
    with pytest.raises(TodoValidationError, match="At most one"):
        parse_todos([
            {"content": "one", "status": "in_progress"},
            {"content": "two", "status": "in_progress"},
        ])


def test_parse_todos_rejects_unknown_status():
    with pytest.raises(TodoValidationError, match="status must be one of"):
        parse_todos([{"content": "one", "status": "doing"}])


def test_parse_todos_rejects_empty_content():
    with pytest.raises(TodoValidationError, match="must not be empty"):
        parse_todos([{"content": "   ", "status": "pending"}])


def test_parse_todos_rejects_non_list():
    with pytest.raises(TodoValidationError, match="must be an array"):
        parse_todos("nope")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_todo_state_emits_only_when_the_list_changes():
    state = TodoState()
    assert state.take_update() is None

    items = (TodoItem("one", TodoStatus.PENDING),)
    state.replace(items)
    assert state.take_update() == items
    assert state.take_update() is None

    state.replace(items)
    assert state.take_update() is None

    state.replace(())
    assert state.take_update() == ()


def test_todo_state_reset_clears_the_snapshot():
    state = TodoState()
    state.replace((TodoItem("one", TodoStatus.PENDING),))
    state.take_update()
    state.reset()
    assert state.items == ()
    assert state.take_update() is None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def test_tool_is_bookkeeping_only():
    tool = TodoWriteTool()
    assert tool.permission is Permission.READ
    assert tool.visible is False
    assert tool.in_default_registry is False
    assert "todo_write" in HIDDEN_TOOL_NAMES


def test_tool_schema_uses_the_status_vocabulary():
    tool = TodoWriteTool()
    props = tool.parameters["properties"]["todos"]["items"]["properties"]
    assert props["status"]["enum"] == list(TODO_STATUSES)
    # The call carries no call-description parameter.
    assert "description" not in tool.to_api_schema()["function"]["parameters"][
        "properties"
    ]


@pytest.mark.asyncio
async def test_tool_execute_replaces_the_shared_state():
    state = TodoState()
    tool = TodoWriteTool(state)

    result = await tool.execute(todos=[
        {"content": "one", "status": "in_progress"},
        {"content": "two", "status": "pending"},
    ])

    assert result.success
    assert state.items == (
        TodoItem("one", TodoStatus.IN_PROGRESS),
        TodoItem("two", TodoStatus.PENDING),
    )
    assert "2 items" in result.output


@pytest.mark.asyncio
async def test_tool_execute_clears_with_an_empty_list():
    state = TodoState()
    state.replace((TodoItem("one", TodoStatus.PENDING),))
    tool = TodoWriteTool(state)

    result = await tool.execute(todos=[])

    assert result.success
    assert state.items == ()
    assert "cleared" in result.output


@pytest.mark.asyncio
async def test_tool_execute_returns_a_correctable_error():
    state = TodoState()
    tool = TodoWriteTool(state)

    result = await tool.execute(todos=[
        {"content": "one", "status": "in_progress"},
        {"content": "two", "status": "in_progress"},
    ])

    assert not result.success
    assert "At most one" in result.error
    assert state.items == ()


# ---------------------------------------------------------------------------
# Session integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_emits_todo_update_after_the_call(tmp_path):
    """A todo_write call lands as one TodoUpdate, and the list survives."""
    db = SQLiteDatabase(tmp_path / "todo.db")
    db.open()
    settings = Settings(session_dir=tmp_path, streaming_enabled=False)
    llm = MockLLMProvider([
        tool_use_response(
            "todo_write",
            {"todos": [
                {"content": "first", "status": "in_progress"},
                {"content": "second", "status": "pending"},
            ]},
            tool_id="todo-1",
        ),
        text_response("Done."),
    ])
    mgr = SessionManager(
        settings=settings,
        storage_manager=StorageManager(db),
        llm=llm,
    )
    await mgr.resolve()

    events = []
    async for event in mgr.process_turn("do two things"):
        events.append(event)

    updates = [e for e in events if isinstance(e, TodoUpdate)]
    assert len(updates) == 1
    assert updates[0].items == (
        TodoItem("first", TodoStatus.IN_PROGRESS),
        TodoItem("second", TodoStatus.PENDING),
    )
    assert mgr.todos == updates[0].items
