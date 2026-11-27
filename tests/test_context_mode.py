"""System-prompt mode handling for the context manager.

The system prompt is built for the mode a turn runs in.  A conversation
that moves between modes — plan approval happens mid-run — must replace
the leading system message instead of keeping the old mode's
instructions.  A reloaded conversation rebuilds on its first turn, so a
stored prompt cannot pin a stale mode.
"""

from __future__ import annotations

import pytest

from tests.mocks import TEST_TURN_CONFIG, MockLLMProvider
from toddler.context.manager import ContextManager
from toddler.llm import Message


def _ctx() -> ContextManager:
    return ContextManager(MockLLMProvider(), config=TEST_TURN_CONFIG)


@pytest.mark.asyncio
async def test_mode_switch_replaces_the_system_prompt():
    ctx = _ctx()
    ctx.load([])
    await ctx.prepare_turn("research this", mode="plan")
    assert ctx.messages[0].role == "system"
    assert "PLAN (Research)" in ctx.messages[0].content

    await ctx.prepare_turn("now do it", mode="execute")
    assert "Current Mode: EXECUTE" in ctx.messages[0].content
    assert sum(1 for m in ctx.messages if m.role == "system") == 1
    assert [m.content for m in ctx.messages[1:]] == [
        "research this", "now do it",
    ]


@pytest.mark.asyncio
async def test_same_mode_does_not_rebuild_the_prompt():
    ctx = _ctx()
    ctx.load([])
    await ctx.prepare_turn("one", mode="execute")
    system = ctx.messages[0]
    await ctx.prepare_turn("two", mode="execute")
    assert ctx.messages[0] is system


@pytest.mark.asyncio
async def test_set_mode_rebuilds_mid_conversation():
    ctx = _ctx()
    ctx.load([])
    await ctx.prepare_turn("go", mode="plan")
    system = ctx.messages[0]
    ctx.set_mode("execute")
    assert ctx.messages[0] is not system
    assert "Current Mode: EXECUTE" in ctx.messages[0].content
    # A mode switch never appends a message of its own.
    assert len(ctx.messages) == 2


@pytest.mark.asyncio
async def test_set_mode_to_the_same_mode_is_a_no_op():
    ctx = _ctx()
    ctx.load([])
    await ctx.prepare_turn("go", mode="execute")
    system = ctx.messages[0]
    ctx.set_mode("execute")
    assert ctx.messages[0] is system


@pytest.mark.asyncio
async def test_reload_forces_a_rebuild_on_the_next_turn():
    ctx = _ctx()
    ctx.load([Message.system("stale prompt"), Message.user("old")])
    await ctx.prepare_turn("new", mode="execute")
    assert ctx.messages[0].role == "system"
    assert ctx.messages[0].content != "stale prompt"
    assert "Current Mode: EXECUTE" in ctx.messages[0].content
    assert [m.content for m in ctx.messages[1:]] == ["old", "new"]
