"""The CLI's ask-user pause — sequential prompts, one answer set.

The terminal has no tabs: an ``AgentPaused`` prompt carrying ``questions``
is asked one at a time through ``Renderer.confirm``, and the collected
answers go back with ``agent.answer_tool_call``.  A cancelled prompt
declines the set through the same call with ``answers=None`` instead of
leaving the loop parked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from toddler.agent.events import AgentFinished, AgentPaused
from toddler.cli.app import CLIApp
from toddler.cli.renderer import ConfirmResult
from toddler.config.settings import Settings
from toddler.tools.prompts import (
    ChoiceLevel,
    ChoiceType,
    PromptAnswer,
    PromptChoice,
    PromptChoices,
    Question,
    QuestionChoice,
    ToolCallPrompt,
)


class _FakeAgent:
    def __init__(self) -> None:
        self.answers: list[tuple[str, list[PromptAnswer] | None]] = []
        self.answer_result = True

    def answer_tool_call(
        self, tool_id: str, answers: list[PromptAnswer] | None,
    ) -> bool:
        self.answers.append((tool_id, answers))
        return self.answer_result


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
        feedback_index: int | None = None,
        detail: str | None = None,
    ) -> ConfirmResult:
        self.confirms.append({
            "prompt": prompt,
            "choices": choices,
            "feedback_index": feedback_index,
            "detail": detail,
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
            choices=PromptChoices(
                type=ChoiceType.QUESTION, data=_questions(),
            ),
        ),
    )


def _gate(
    tool_id: str = "t1",
    rows: tuple[PromptChoice, ...] | None = None,
) -> AgentPaused:
    return AgentPaused(
        tool_id=tool_id,
        prompt=ToolCallPrompt(
            title="Allow this tool call?",
            description="list the files",
            choices=PromptChoices(
                type=ChoiceType.SINGLE,
                data=rows or (
                    PromptChoice("Approve", ChoiceLevel.APPROVE),
                    PromptChoice("Deny", ChoiceLevel.DENY),
                ),
            ),
        ),
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
        pause = _gate()
        finished = AgentFinished(reason="completed")
        app, session = _app(tmp_path, [pause, finished])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="approve", choice_index=0),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("approve")

        assert renderer.confirms[0]["choices"] == ["Approve", "Deny"]
        assert renderer.confirms[0]["feedback_index"] is None
        assert session.agent.answers == [
            ("t1", [PromptAnswer(choice=0)]),
        ]
        assert renderer.finished == [finished]

    async def test_the_rows_level_decides_not_its_title(self, tmp_path):
        """A tool may word its rows for its call; ``Allow once`` still
        approves because of its level, not because of how it reads."""
        pause = _gate(rows=(
            PromptChoice("Allow once", ChoiceLevel.APPROVE),
            PromptChoice("Stop", ChoiceLevel.DENY),
        ))
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", choice_index=0),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("approve")

        assert renderer.confirms[0]["choices"] == ["Allow once", "Stop"]
        assert session.agent.answers == [
            ("t1", [PromptAnswer(choice=0)]),
        ]

    async def test_a_deny_row_denies(self, tmp_path):
        pause = _gate()
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", choice_index=1),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("deny")

        assert session.agent.answers == [
            ("t1", [PromptAnswer(choice=1)]),
        ]

    async def test_a_feedback_row_sends_the_users_reason(self, tmp_path):
        pause = _gate(rows=(
            PromptChoice("Approve", ChoiceLevel.APPROVE),
            PromptChoice("Deny", ChoiceLevel.DENY),
            PromptChoice("Tell me why", ChoiceLevel.FEEDBACK),
        ))
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(
                decision="feedback",
                feedback="use the other file",
                choice_index=2,
            ),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("feedback")

        assert renderer.confirms[0]["feedback_index"] == 2
        assert session.agent.answers == [
            ("t1", [PromptAnswer(
                choice=2, content="use the other file",
            )]),
        ]

    async def test_empty_feedback_sends_the_row_without_text(self, tmp_path):
        """The loop decides a feedback row with no words is a denial; the
        CLI's job is only to report the row the user picked."""
        pause = _gate(rows=(
            PromptChoice("Approve", ChoiceLevel.APPROVE),
            PromptChoice("Tell me why", ChoiceLevel.FEEDBACK),
        ))
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", choice_index=1),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("feedback")

        assert session.agent.answers == [
            ("t1", [PromptAnswer(choice=1)]),
        ]

    async def test_cancelling_a_gate_declines(self, tmp_path):
        pause = _gate()
        app, session = _app(tmp_path, [pause])
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", cancelled=True),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("deny")

        assert session.agent.answers == [("t1", None)]

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
        assert session.agent.answers == [
            ("q1", [PromptAnswer(choice=1), PromptAnswer(choice=0)]),
        ]

    async def test_a_cancelled_prompt_declines_the_set(self, tmp_path):
        events = [_pause()]
        app, session = _app(tmp_path, events)
        renderer = _ScriptedRenderer([
            ConfirmResult(decision="deny", cancelled=True),
        ])
        app._renderer = renderer  # type: ignore[assignment]

        await app._run_agent_turn("ask")

        assert len(renderer.confirms) == 1
        assert session.agent.answers == [("q1", None)]

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

        assert session.agent.answers == [
            ("q1", [PromptAnswer(choice=0), PromptAnswer(choice=0)]),
            ("q1", None),
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
