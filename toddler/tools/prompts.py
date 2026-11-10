"""Pause payloads — what a call asks the user while it waits.

The vocabulary shared by the agent loop, the gate, and both front-ends:
:class:`ToolCallPrompt` is what a card renders, :class:`PromptChoices`
is what the pause offers — approval rows or questions — and
:class:`PromptAnswer` is what comes back.  Nothing here touches a tool
or the filesystem; a pause is described by these objects and shown by
whichever surface owns it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ALLOW_CALL_TITLE",
    "ONE_LINE_DETAIL_LIMIT",
    "AnswerVerdict",
    "ChoiceLevel",
    "ChoiceType",
    "DEFAULT_SINGLE_CHOICES",
    "PromptAnswer",
    "PromptChoice",
    "PromptChoices",
    "Question",
    "QuestionChoice",
    "QuestionValidationError",
    "ToolCallPrompt",
]


# ---------------------------------------------------------------------------
# Call prompt — what a call asks the user while it waits to be approved
# ---------------------------------------------------------------------------

#: Title the gate card leads with for every tool whose call is not one of
#: the few with a title of its own — a tool varies it by overriding
#: :meth:`~toddler.tools.base.BaseTool._prompt_title`.
ALLOW_CALL_TITLE = "Allow this tool call?"

#: Cap on the detail half of :meth:`ToolCallPrompt.one_line`.  The terminal
#: gets one line and no more; the browser is handed the same text whole
#: and scrolls it, so this cut is the terminal's alone.
ONE_LINE_DETAIL_LIMIT = 200


class ChoiceLevel(Enum):
    """What picking a prompt choice means.

    The level, not the title, decides the answer: a tool is free to word
    its rows for its own call (``"Allow once"``, ``"Always allow"``) and
    still get the behaviour it named.  ``APPROVE`` runs the call,
    ``DENY`` refuses it, and ``FEEDBACK`` refuses it with the user's
    reason attached for the model to act on.
    """

    APPROVE = "approve"
    DENY = "deny"
    FEEDBACK = "feedback"


class ChoiceType(Enum):
    """The shape of a pause's choice payload.

    ``SINGLE`` is the ordinary gate: one row is selected and its
    :class:`ChoiceLevel` decides what happens.  ``QUESTION`` is an
    ask-user pause: the data is the questions, answered one at a time.
    """

    SINGLE = "single"
    QUESTION = "question"


@dataclass(frozen=True)
class PromptChoice:
    """One row of a single-choice pause — a title and what it does."""

    title: str
    level: ChoiceLevel

    def to_dict(self) -> dict:
        """The wire form — the title the row shows, the meaning it carries."""
        return {"title": self.title, "level": self.level.value}


@dataclass(frozen=True)
class PromptAnswer:
    """One answer to a pause's choice.

    ``choice`` indexes the pause's data — the row selected in a single
    gate, or the chosen answer for one question.  ``content`` is the
    user's own words where the chosen row takes them: a feedback reason,
    or the text of a future "Other" question choice.
    """

    choice: int
    content: str | None = None

    def to_dict(self) -> dict:
        """The wire form — the index and any free text."""
        return {"choice": self.choice, "content": self.content}


class AnswerVerdict(Enum):
    """What a pause's answers mean for the call they gate.

    ``INVALID`` is not an answer to this pause at all (wrong count, an
    index out of range): the pause stays open and the caller may retry.
    ``DENIED`` is a well-formed answer that does not let the call run.
    ``FEEDBACK`` also refuses the call, but the user's words ride along
    as the correction the model should act on.  ``GRANTED`` runs it — a
    question pause is input, not a permission decision, so any valid
    answer set grants.
    """

    INVALID = "invalid"
    DENIED = "denied"
    FEEDBACK = "feedback"
    GRANTED = "granted"


@dataclass(frozen=True)
class PromptChoices:
    """What a pause asks the user to choose between.

    One object for both pause shapes, told apart by :attr:`type`, so a
    front-end branches once and then reads ``data`` in the shape that
    type promises: :class:`PromptChoice` rows for ``SINGLE``,
    :class:`Question` objects for ``QUESTION``.
    """

    type: ChoiceType
    data: tuple[PromptChoice, ...] | tuple[Question, ...]

    def to_dict(self) -> dict:
        """The wire form — the discriminant and its payload."""
        return {
            "type": self.type.value,
            "data": [item.to_dict() for item in self.data],
        }

    def verdict(self, answers: Sequence[PromptAnswer]) -> AnswerVerdict:
        """What *answers* mean for the call this pause gates.

        A single gate takes exactly one answer; the selected row's level
        decides it — the row's title never does.  ``APPROVE`` grants,
        ``FEEDBACK`` refuses with the user's words attached, and
        ``DENY`` refuses.  A question pause takes one answer per
        question, and any valid set grants: that call is held for input,
        not for a permission decision.
        """
        if self.type is ChoiceType.SINGLE:
            return self._single_verdict(answers)
        questions = self.data
        if len(answers) != len(questions):
            return AnswerVerdict.INVALID
        for answer, question in zip(answers, questions, strict=True):
            if not isinstance(question, Question):
                return AnswerVerdict.INVALID
            if (
                not isinstance(answer, PromptAnswer)
                or not isinstance(answer.choice, int)
                or isinstance(answer.choice, bool)
                or not 0 <= answer.choice < len(question.choices)
            ):
                return AnswerVerdict.INVALID
        return AnswerVerdict.GRANTED

    def _single_verdict(
        self, answers: Sequence[PromptAnswer],
    ) -> AnswerVerdict:
        """What one answer to a single gate means, by its row's level."""
        if len(answers) != 1 or not isinstance(
            answers[0], PromptAnswer,
        ):
            return AnswerVerdict.INVALID
        answer = answers[0]
        if (
            not isinstance(answer.choice, int)
            or isinstance(answer.choice, bool)
            or not 0 <= answer.choice < len(self.data)
        ):
            return AnswerVerdict.INVALID
        row = self.data[answer.choice]
        if not isinstance(row, PromptChoice):
            return AnswerVerdict.INVALID
        if row.level is ChoiceLevel.APPROVE:
            return AnswerVerdict.GRANTED
        if row.level is ChoiceLevel.FEEDBACK:
            return AnswerVerdict.FEEDBACK
        return AnswerVerdict.DENIED


#: The ordinary gate's rows, and the default every prompt carries.  A tool
#: that wants different wording or a feedback row overrides
#: :meth:`~toddler.tools.base.BaseTool._prompt_choices`.
DEFAULT_SINGLE_CHOICES = PromptChoices(
    type=ChoiceType.SINGLE,
    data=(
        PromptChoice("Approve", ChoiceLevel.APPROVE),
        PromptChoice("Deny", ChoiceLevel.DENY),
    ),
)


@dataclass(frozen=True)
class ToolCallPrompt:
    """What one call asks the user while it waits to be approved or answered.

    Built by :meth:`~toddler.tools.base.BaseTool.build_call_prompt` — one
    part from each of that tool's ``_prompt_*`` methods — and carried on
    :class:`~toddler.agent.events.AgentPaused`.  The parts stay apart
    rather than being rendered into one string, because each front-end
    lays them out for its own surface: the browser boxes and scrolls the
    content block, the terminal flattens everything to a line.

    ``content`` is the call's body, for a tool whose arguments have one —
    today only ``Shell``, whose command *is* the decision.  ``description``
    is the call's own line about what it does: the ``description``
    parameter, or the file the call names.  ``None`` is "nothing to show"
    rather than an empty string, and the wire form keeps the difference.

    ``choices`` is what the pause asks for.  An ordinary call carries the
    approve/deny set (:data:`DEFAULT_SINGLE_CHOICES`); a tool may offer
    rows of its own, and an ask-user tool carries questions instead.
    ``choices.type`` is the pause's discriminant: ``SINGLE`` is answered
    with one :class:`PromptAnswer` whose row's level decides the outcome,
    ``QUESTION`` with one :class:`PromptAnswer` per question, in order.
    """

    title: str
    content: str | None = None
    description: str | None = None
    choices: PromptChoices = DEFAULT_SINGLE_CHOICES

    def to_dict(self) -> dict:
        """The wire form — all keys present."""
        return {
            "title": self.title,
            "content": self.content,
            "description": self.description,
            "choices": self.choices.to_dict(),
        }

    def one_line(self) -> str:
        """The terminal's rendering: one line, never two.

        The content leads where there is one — a command is what is being
        approved — and the description answers where there is not.  A
        question prompt has neither, so it names its count instead.  The
        terminal front-end asks its questions one at a time; this is only
        the summary a log or a card title would show.
        """
        if self.choices.type is ChoiceType.QUESTION:
            count = len(self.choices.data)
            detail = f"{count} question{'' if count == 1 else 's'}"
        else:
            detail = self.content or self.description
        if not detail:
            return self.title
        collapsed = " ".join(detail.split())
        if len(collapsed) > ONE_LINE_DETAIL_LIMIT:
            collapsed = collapsed[:ONE_LINE_DETAIL_LIMIT - 1].rstrip() + "…"
        return f"{self.title} — {collapsed}"


# ---------------------------------------------------------------------------
# Question payload — what an ask-user tool pauses the turn for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionChoice:
    """One answer to a question the agent asks the user.

    ``title`` is the short label a choice row leads with; ``content`` is
    the sentence explaining what choosing it means.  The first choice of
    a question is the recommended one.
    """

    title: str
    content: str

    def to_dict(self) -> dict:
        """The wire form — what the web card and replay entries carry."""
        return {"title": self.title, "content": self.content}


@dataclass(frozen=True)
class Question:
    """One single-choice question an ask-user tool asks the user.

    ``title`` is the short tab label, ``content`` the full question, and
    ``choices`` the two or three answers, best first.
    """

    title: str
    content: str
    choices: tuple[QuestionChoice, ...]

    def to_dict(self) -> dict:
        """The wire form — questions nested as the card renders them."""
        return {
            "title": self.title,
            "content": self.content,
            "choices": [choice.to_dict() for choice in self.choices],
        }


class QuestionValidationError(ValueError):
    """A model-authored question payload that cannot be asked.

    Raised while a tool builds its pause prompt; the loop turns the
    message into the call's failed tool result so the model can retry with
    corrected arguments instead of taking the turn down.
    """
