"""The ``ask_user`` tool — the agent's way to ask the user a question.

The tool declares :attr:`~toddler.tools.policy.Permission.INTERACT`, so
the agent loop always pauses its call and puts the questions on the
approval prompt.  The questions reach the browser as an interactive card
and the terminal as a sequence of prompts; the answers are injected into
:meth:`QuestionTool.execute`, which returns them as the call's tool
result.
"""

from __future__ import annotations

from toddler.tools.base import BaseTool, ToolResult
from toddler.tools.policy import Permission
from toddler.tools.prompts import (
    ChoiceType,
    PromptAnswer,
    PromptChoices,
    Question,
    QuestionChoice,
    QuestionValidationError,
)
from toddler.tools.registry import TOOL_CATALOG

__all__ = [
    "ASK_USER_USAGE",
    "MAX_CHOICES",
    "MAX_QUESTIONS",
    "MIN_CHOICES",
    "QuestionTool",
    "QuestionValidationError",
    "parse_questions",
]

#: Most questions one call may ask.
MAX_QUESTIONS = 5

#: Fewest / most answers one question may offer.
MIN_CHOICES = 2
MAX_CHOICES = 3

#: The model-facing protocol for the tool.  Embedded in the description
#: rather than repeated by each front-end, so the wording lives once.
ASK_USER_USAGE = (
    "Use this when a decision is the user's to make — a missing "
    "requirement, a choice between approaches, or a risky trade-off — "
    "instead of guessing or ending the turn to ask in prose. Each question "
    "has a short title (the tab label), the full question as content, and "
    "two or three choices; each choice has a short title and a sentence "
    "explaining it. Put the recommended choice first. Ask only what you "
    "cannot find out by reading the code, and ask related questions "
    "together in one call."
)


def _text(value: object, where: str) -> str:
    """*value* as non-empty text, or a validation error naming *where*."""
    if not isinstance(value, str):
        raise QuestionValidationError(f"{where} must be a string.")
    text = value.strip()
    if not text:
        raise QuestionValidationError(f"{where} must not be empty.")
    return text


def _title(value: object, where: str) -> str:
    """A title collapsed to one line — the tab and row have no second."""
    return " ".join(_text(value, where).split())


def parse_questions(raw: object) -> tuple[Question, ...]:
    """Validate a model-authored ``questions`` argument.

    Returns the normalized questions, or raises
    :class:`QuestionValidationError` with a message the model can act on.
    The bounds are enforced here, not just in the JSON schema: an
    OpenAI-compatible endpoint may ignore ``minItems`` / ``maxItems`` and
    a malformed call must not reach the question card.
    """
    if not isinstance(raw, (list, tuple)):
        raise QuestionValidationError(
            "questions must be an array of question objects."
        )
    if not 1 <= len(raw) <= MAX_QUESTIONS:
        raise QuestionValidationError(
            f"ask_user needs between 1 and {MAX_QUESTIONS} questions; "
            f"got {len(raw)}."
        )

    questions: list[Question] = []
    for index, item in enumerate(raw, 1):
        where = f"question {index}"
        if not isinstance(item, dict):
            raise QuestionValidationError(
                f"{where} must be an object with a title, content, and "
                "choices."
            )
        title = _title(item.get("title"), f"{where} title")
        content = _text(item.get("content"), f"{where} content")

        choices_raw = item.get("choices")
        if not isinstance(choices_raw, (list, tuple)):
            raise QuestionValidationError(
                f"{where} needs an array of choices."
            )
        if not MIN_CHOICES <= len(choices_raw) <= MAX_CHOICES:
            raise QuestionValidationError(
                f"{where} needs between {MIN_CHOICES} and {MAX_CHOICES} "
                f"choices; got {len(choices_raw)}."
            )

        choices: list[QuestionChoice] = []
        for choice_index, choice in enumerate(choices_raw, 1):
            choice_where = f"{where}, choice {choice_index}"
            if not isinstance(choice, dict):
                raise QuestionValidationError(
                    f"{choice_where} must be an object with a title and "
                    "content."
                )
            choices.append(
                QuestionChoice(
                    title=_title(choice.get("title"), f"{choice_where} title"),
                    content=_text(
                        choice.get("content"), f"{choice_where} content",
                    ),
                )
            )
        questions.append(
            Question(title=title, content=content, choices=tuple(choices))
        )
    return tuple(questions)


@TOOL_CATALOG.register()
class QuestionTool(BaseTool):
    """Ask the user one to five single-choice questions.

    The call pauses at the ordinary approval gate — the ``INTERACT``
    tier makes the loop ask whatever the permission mode — and the
    prompt carries the questions instead of an approve/deny choice.  The
    answers arrive as an injected ``answers`` argument to
    :meth:`execute`, so the tool owns its result just like every other
    tool.  The call is visible: its tool card is the durable Q&A record,
    while the dock card is the interaction.
    """

    name = "ask_user"
    description = (
        "Ask the user one to five single-choice questions, shown together "
        "as tabs in one card, and return their answers as the tool result. "
        + ASK_USER_USAGE
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    "One to five questions asked together; the user answers "
                    "them one at a time and submits once."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "A few words — the tab label for this "
                                "question."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "The full question, as a paragraph.",
                        },
                        "choices": {
                            "type": "array",
                            "minItems": MIN_CHOICES,
                            "maxItems": MAX_CHOICES,
                            "description": (
                                "Two or three single-choice answers; put "
                                "the recommended one first."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": (
                                            "Short label for this answer."
                                        ),
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": (
                                            "One sentence describing what "
                                            "this choice means."
                                        ),
                                    },
                                },
                                "required": ["title", "content"],
                            },
                        },
                    },
                    "required": ["title", "content", "choices"],
                },
            },
        },
        "required": ["questions"],
    }

    #: The questions say what the call does — no call-description param.
    require_description_parameter = False

    #: The tool card is the durable Q&A record (the web has a dedicated
    #: questions layout for it); the dock card is the live interaction.
    visible = True

    @property
    def permission(self) -> Permission:
        """Input, not risk: the gate always pauses for an answer."""
        return Permission.INTERACT

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The pause asks for answers, not an approval."""
        return "Answer these questions"

    def _prompt_description(self, **kwargs) -> str | None:  # noqa: ARG002
        """The question cards say everything the card needs; no summary."""
        return None

    def _prompt_choices(self, **kwargs) -> PromptChoices:
        """The parsed questions, and no approve/deny rows.

        Raises :class:`QuestionValidationError` for model-authored
        arguments that cannot be asked; the loop returns the message as
        the call's failed result instead of pausing.
        """
        return PromptChoices(
            type=ChoiceType.QUESTION,
            data=parse_questions(kwargs.get("questions")),
        )

    def questions_result(
        self,
        questions: tuple[Question, ...],
        answers: list[PromptAnswer] | None,
    ) -> ToolResult:
        """Format the answers as the call's tool result.

        ``answers`` is one :class:`PromptAnswer` per question, in order;
        ``None`` when the user cancelled the pause (Escape, EOF, or a
        dismissed card).  A cancellation ends the turn — the user will
        decide what to do next — so the result must not send the model
        guessing on its own.
        """
        if answers is None:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    "The user cancelled these questions. No answers were "
                    "given; they will decide what to do next."
                ),
                metadata={
                    "questions": [q.to_dict() for q in questions],
                    "declined": True,
                },
            )

        if len(answers) != len(questions):
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"ask_user got {len(answers)} answers for "
                    f"{len(questions)} questions."
                ),
            )
        for answer, question in zip(answers, questions, strict=True):
            if (
                not isinstance(answer, PromptAnswer)
                or not isinstance(answer.choice, int)
                or isinstance(answer.choice, bool)
                or not 0 <= answer.choice < len(question.choices)
            ):
                return ToolResult(
                    tool_id="",
                    tool_name=self.name,
                    success=False,
                    output="",
                    error=(
                        f"ask_user got an invalid answer for question "
                        f"'{question.title}'."
                    ),
                )

        lines = ["The user answered your questions:"]
        for index, (question, answer) in enumerate(
            zip(questions, answers, strict=True), 1,
        ):
            choice = question.choices[answer.choice]
            recommended = (
                " (recommended)"
                if answer.choice == 0 and len(question.choices) > 1
                else ""
            )
            # A row that takes free text (a future "Other" choice) reads
            # as what the user wrote, not as the row's label.
            label = answer.content or choice.title
            lines.append(f"{index}. {question.title}: {label}{recommended}")
            if answer.content is None:
                lines.append(f"   {choice.content}")
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output="\n".join(lines),
            metadata={
                "questions": [q.to_dict() for q in questions],
                "answers": [answer.to_dict() for answer in answers],
            },
        )

    def summarize_call(self, **kwargs) -> str:
        """A log line that counts the questions rather than dumping them."""
        raw = kwargs.get("questions")
        count = len(raw) if isinstance(raw, (list, tuple)) else 0
        return (
            f"ask_user({count} question"
            f"{'' if count == 1 else 's'})"
        )

    async def execute(
        self,
        questions: object,
        answers: list[PromptAnswer] | None = None,
        parsed_questions: tuple[Question, ...] | None = None,
    ) -> ToolResult:
        """Return the user's answers as this call's tool result.

        ``answers`` is injected by the loop after the pause — it is not a
        model argument and never appears in the schema or the transcript.
        ``None`` means the user cancelled the pause (Escape/EOF); the
        loop then ends the turn.

        ``parsed_questions`` is injected alongside them when the call ran
        through a pause: the gate parsed and validated the questions
        already, so the tool does not parse the model's arguments again.
        """
        parsed = parsed_questions or parse_questions(questions)
        return self.questions_result(parsed, answers)
