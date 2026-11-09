"""Tests for the ask_user tool — schema, validation, and result shaping."""

from __future__ import annotations

import pytest

from toddler.tools.base import (
    Decision,
    FileScope,
    Permission,
    PermissionManager,
    PermissionMode,
)
from toddler.tools.prompts import (
    ChoiceType,
    PromptAnswer,
    QuestionValidationError,
)
from toddler.tools.question import (
    MAX_CHOICES,
    MAX_QUESTIONS,
    MIN_CHOICES,
    QuestionTool,
    parse_questions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _choice(title: str = "SQLite", content: str = "One file.") -> dict:
    return {"title": title, "content": content}


def _question(
    title: str = "Storage",
    content: str = "Where should sessions live?",
    choices: list[dict] | None = None,
) -> dict:
    return {
        "title": title,
        "content": content,
        "choices": choices if choices is not None else [
            _choice(),
            _choice("Redis", "Shared storage across processes."),
        ],
    }


@pytest.fixture
def questions():
    return parse_questions([
        _question(
            choices=[
                _choice("SQLite", "One file, no server to run."),
                _choice("Redis", "Shared storage across processes."),
            ],
        ),
    ])


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


class TestDeclaration:

    def test_it_is_the_ask_user_tool(self):
        tool = QuestionTool()
        assert tool.name == "ask_user"
        assert tool.visible is True
        assert tool.in_default_registry is True
        assert tool.permission is Permission.INTERACT

    def test_schema_carries_the_bounds_and_no_description_param(self):
        schema = QuestionTool().to_api_schema()["function"]
        assert schema["name"] == "ask_user"
        params = schema["parameters"]
        assert "description" not in params["properties"]
        assert params["required"] == ["questions"]

        questions = params["properties"]["questions"]
        assert questions["minItems"] == 1
        assert questions["maxItems"] == MAX_QUESTIONS

        choices = questions["items"]["properties"]["choices"]
        assert choices["minItems"] == MIN_CHOICES
        assert choices["maxItems"] == MAX_CHOICES

    def test_summarize_call_counts_rather_than_dumping_the_payload(self):
        tool = QuestionTool()
        raw = {"questions": [_question(), _question()]}
        assert tool.summarize_call(**raw) == "ask_user(2 questions)"
        assert tool.summarize_call(questions=[_question()]) == (
            "ask_user(1 question)"
        )


# ---------------------------------------------------------------------------
# The interact tier
# ---------------------------------------------------------------------------


class TestInteractTier:
    """``INTERACT`` asks under every mode: the pause is input."""

    @pytest.mark.parametrize("mode", [PermissionMode.MANUAL, PermissionMode.AUTO])
    def test_it_always_asks(self, mode):
        manager = PermissionManager(mode)
        assert manager.decide(
            Permission.INTERACT, FileScope.WORKSPACE,
        ) is Decision.ASK

    def test_it_is_not_mutating(self):
        # No checkpoint precedes a pause that only reads the user.
        assert Permission.INTERACT.is_mutating is False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseQuestions:

    def test_accepts_the_full_range_and_normalizes_titles(self):
        raw = [
            _question(title="  Auth\n  method  "),
            _question(title="F", content="Second?"),
            _question(title="G", content="Third?"),
            _question(title="H", content="Fourth?"),
            _question(title="I", content="Fifth?"),
        ]
        parsed = parse_questions(raw)
        assert len(parsed) == MAX_QUESTIONS
        assert parsed[0].title == "Auth method"
        assert parsed[0].choices[0].title == "SQLite"

    def test_accepts_three_choices(self):
        parsed = parse_questions([_question(choices=[
            _choice("A"), _choice("B"), _choice("C"),
        ])])
        assert len(parsed[0].choices) == 3

    @pytest.mark.parametrize("raw", [None, "questions", {}, 3])
    def test_rejects_a_non_list(self, raw):
        with pytest.raises(QuestionValidationError, match="array"):
            parse_questions(raw)

    def test_rejects_zero_questions(self):
        with pytest.raises(QuestionValidationError, match="between 1 and 5"):
            parse_questions([])

    def test_rejects_more_than_five_questions(self):
        raw = [_question(content=f"Q{i}?") for i in range(6)]
        with pytest.raises(QuestionValidationError, match="between 1 and 5"):
            parse_questions(raw)

    def test_rejects_a_non_object_question(self):
        with pytest.raises(QuestionValidationError, match="question 1"):
            parse_questions(["nope"])

    @pytest.mark.parametrize("title", ["", "   ", None, 4])
    def test_rejects_a_bad_question_title(self, title):
        raw = [_question(title=title)]
        with pytest.raises(QuestionValidationError, match="question 1 title"):
            parse_questions(raw)

    @pytest.mark.parametrize("content", ["", "   ", None, 4])
    def test_rejects_a_bad_question_content(self, content):
        raw = [_question(content=content)]
        with pytest.raises(
            QuestionValidationError, match="question 1 content",
        ):
            parse_questions(raw)

    def test_rejects_no_choices(self):
        with pytest.raises(QuestionValidationError, match="between 2 and 3"):
            parse_questions([_question(choices=[])])

    def test_rejects_one_choice(self):
        with pytest.raises(QuestionValidationError, match="between 2 and 3"):
            parse_questions([_question(choices=[_choice()])])

    def test_rejects_four_choices(self):
        raw = [_question(choices=[_choice(), _choice(), _choice(), _choice()])]
        with pytest.raises(QuestionValidationError, match="between 2 and 3"):
            parse_questions(raw)

    def test_rejects_a_non_object_choice(self):
        with pytest.raises(QuestionValidationError, match="choice 1"):
            parse_questions([_question(choices=["nope", _choice()])])

    @pytest.mark.parametrize("title", ["", " ", None])
    def test_rejects_a_bad_choice_title(self, title):
        raw = [_question(choices=[_choice(title=title), _choice()])]
        with pytest.raises(QuestionValidationError, match="choice 1 title"):
            parse_questions(raw)

    @pytest.mark.parametrize("content", ["", " ", None])
    def test_rejects_a_bad_choice_content(self, content):
        raw = [_question(choices=[_choice(content=content), _choice()])]
        with pytest.raises(QuestionValidationError, match="choice 1 content"):
            parse_questions(raw)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class TestQuestionsResult:

    def test_formats_answers_and_records_metadata(self, questions):
        tool = QuestionTool()
        result = tool.questions_result(
            questions, [PromptAnswer(choice=0)],
        )

        assert result.success
        assert "The user answered your questions:" in result.output
        assert "1. Storage: SQLite (recommended)" in result.output
        assert "One file, no server to run." in result.output
        assert result.metadata == {
            "questions": [question.to_dict() for question in questions],
            "answers": [{"choice": 0, "content": None}],
        }

    def test_a_non_recommended_answer_has_no_marker(self, questions):
        result = QuestionTool().questions_result(
            questions, [PromptAnswer(choice=1)],
        )
        assert result.success
        assert "Redis" in result.output
        assert "(recommended)" not in result.output

    def test_a_free_text_answer_reads_as_the_users_words(self, questions):
        result = QuestionTool().questions_result(
            questions, [PromptAnswer(choice=1, content="DuckDB")],
        )
        assert result.success
        assert "1. Storage: DuckDB" in result.output
        # The row's own explanation is replaced by what the user wrote.
        assert "Shared storage." not in result.output
        assert result.metadata["answers"] == [
            {"choice": 1, "content": "DuckDB"},
        ]

    def test_declined_is_a_failed_result_with_metadata(self, questions):
        result = QuestionTool().questions_result(questions, None)
        assert not result.success
        assert "declined" in result.error.lower()
        assert result.metadata["declined"] is True
        assert "answers" not in result.metadata

    def test_rejects_a_count_mismatch(self, questions):
        result = QuestionTool().questions_result(questions, [])
        assert not result.success
        assert "answers" in result.error

    @pytest.mark.parametrize("answer", [2, -1, True, "0", None])
    def test_rejects_an_out_of_range_or_non_integer_index(
        self, questions, answer,
    ):
        result = QuestionTool().questions_result(
            questions, [PromptAnswer(choice=answer)],
        )
        assert not result.success
        assert "invalid answer" in result.error


# ---------------------------------------------------------------------------
# Call prompt
# ---------------------------------------------------------------------------


class TestCallPrompt:

    def test_it_embeds_the_questions(self):
        prompt = QuestionTool().build_call_prompt(
            questions=[_question()],
        )
        assert prompt.choices.type is ChoiceType.QUESTION
        assert prompt.choices.data[0].title == "Storage"
        assert prompt.choices.data[0].choices[0].title == "SQLite"
        assert prompt.to_dict()["choices"] == {
            "type": "question",
            "data": [_question()],
        }

    def test_it_rejects_malformed_arguments(self):
        with pytest.raises(QuestionValidationError, match="between 1 and 5"):
            QuestionTool().build_call_prompt(questions=[])


# ---------------------------------------------------------------------------
# Direct execution
# ---------------------------------------------------------------------------


class TestExecute:

    async def test_execute_formats_the_answers(self):
        result = await QuestionTool().execute(
            [_question()], [PromptAnswer(choice=0)],
        )
        assert result.success
        assert "SQLite (recommended)" in result.output
        assert result.metadata["answers"] == [
            {"choice": 0, "content": None},
        ]

    async def test_execute_with_no_answers_declines(self):
        result = await QuestionTool().execute([_question()], None)
        assert not result.success
        assert result.metadata["declined"] is True

    async def test_execute_validates_the_questions_again(self):
        with pytest.raises(QuestionValidationError):
            await QuestionTool().execute([], [])

    async def test_a_paused_call_reuses_the_parsed_questions(self):
        """The gate parsed them when it built the prompt; the loop hands
        them back so the tool does not parse the model's arguments twice."""
        parsed = parse_questions([_question()])
        result = await QuestionTool().execute(
            questions=[],  # a payload the parser would reject
            answers=[PromptAnswer(choice=0)],
            parsed_questions=parsed,
        )
        assert result.success
        assert "SQLite (recommended)" in result.output
