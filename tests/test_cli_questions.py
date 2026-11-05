"""The CLI's ask-user pause — sequential prompts, one answer set.

The terminal has no tabs: an ``AgentPaused`` prompt carrying ``questions``
is asked one at a time through ``Renderer.confirm``, and the collected
choice indexes go back with ``agent.answer_questions``.  A cancelled
prompt declines the set through ``deny_tool_call`` instead of leaving the
loop parked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from toddler.agent.events import AgentFinished, AgentPaused
from toddler.cli.app import CLIApp
from toddler.cli.renderer import ConfirmResult
from toddler.config.settings import Settings
from toddler.tools.base import Question, QuestionChoice, ToolCallPrompt


class _FakeAgent:
    def __init__(self) -> None:
        self.answered: tuple[str, list[int]] | None = None
        self.declined: str | None = None
        self.approved: str | None = None
        self.answer_result = True

    def answer_questions(self, tool_id: str, answers: list[int]) -> bool:
        self.answered = (tool_id, answers)
        return self.answer_result

    def approve_tool_call(self, tool_id: str = "") -> bool:
        self.approved = tool_id
        return True

    def deny_tool_call(self, tool_id: str = "") -> bool:
        self.declined = tool_id
        return True


class _FakeSessionManager:
    def __init__(self, events: list) -> None:
        self.session = None
        self.conversation = None
        self.agent = _FakeAgent()
        self._events = events

    async def process_turn(
        self, user_input: str, *, force_plan: bool = False,  # noqa: ARG002
    ) -> AsyncIterator:
        for event in self._events:
            yield event


class _ScriptedRenderer:
    """Records confirm() calls and returns the scripted results."""

    def __init__(self, results: list[ConfirmResult]) -> None:
        self._results = list(results)
        self.confirms: list[dict] = []
        self.finished: list[AgentFinished] = []

    async def confirm(
        self,
        prompt: str,
        choices: list[str],
        *,
        allow_feedback: bool = False,  # noqa: ARG002
        detail: str | None = None,
    ) -> ConfirmResult:
        self.confirms.append({
            "prompt": prompt, "choices": choices, "detail": detail,
        })
        return self._results.pop(0)

    def on_agent_finished(self, event: AgentFinished) -> None:
        self.finished.append(event)

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


def _questions() -> tuple[Question, Question]:
    return (
        Question(
            title="Storage",
            content="Where should sessions live?",
            choices=(
                QuestionChoice("SQLite", "One file, no server."),
                QuestionChoice("Redis", "Shared storage."),
            ),
        ),
        Question(
            title="Auth",
            content="Which sign-in method?",
            choices=(
                QuestionChoice("OAuth", "Reuse the provider."),
                QuestionChoice("Password", "Add our own check."),
            ),
        ),
    )


def _pause(tool_id: str = "q1") -> AgentPaused:
    return AgentPaused(
        tool_id=tool_id,
        prompt=ToolCallPrompt(
            title="Answer these questions",
            questions=_questions(),
        ),
        choices=None,
    )


def _app(tmp_path, events) -> tuple[CLIApp, _FakeSessionManager]:
    session = _FakeSessionManager(events)
    app = CLIApp(
        Settings(session_dir=tmp_path, streaming_enabled=False),
        session,  # type: ignore[arg-type]
    )
    return app, session


class TestCLIQuestions:

    async def test_a_normal_approval_keeps_the_event_loop_running(
        self, tmp_path,
    ):
        """The AgentPaused branch must not `break` the turn: a normal tool
        approval is one event, and the next event still has to be read."""
        pause = AgentPaused(
            tool_id="t1",
            prompt=ToolCallPrompt(
                title="Allow this tool call?",
                description="list the files",
            ),
            choices=["approve", "deny"],
        )
        finished = AgentFinished(reason="completed")
        app, session = _app(tmp_path, [pause, finished])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="approve"),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("approve")

        assert session.agent.approved == "t1"
        assert renderer.finished == [finished]

    async def test_asks_each_question_and_sends_the_indexes(self, tmp_path):
        pause = _pause()
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", choice_index=1),
            ConfirmResult(decision="deny", choice_index=0),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("ask")

        assert len(renderer.confirms) == 2
        first, second = renderer.confirms
        assert first["prompt"] == "Question 1/2: Storage"
        assert first["detail"] == "Where should sessions live?"
        assert first["choices"] == [
            "1. SQLite — One file, no server.",
            "2. Redis — Shared storage.",
        ]
        assert second["prompt"] == "Question 2/2: Auth"
        assert session.agent.answered == ("q1", [1, 0])
        assert session.agent.declined is None

    async def test_a_cancelled_prompt_declines_the_set(self, tmp_path):
        events = [_pause()]
        app, session = _app(tmp_path, events)
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", cancelled=True),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("ask")

        assert len(renderer.confirms) == 1
        assert session.agent.answered is None
        assert session.agent.declined == "q1"

    async def test_a_stale_answer_declines_instead_of_parking(self, tmp_path):
        events = [_pause()]
        app, session = _app(tmp_path, events)
        session.agent.answer_result = False
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", choice_index=0),
            ConfirmResult(decision="deny", choice_index=0),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("ask")

        assert session.agent.answered == ("q1", [0, 0])
        assert session.agent.declined == "q1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
