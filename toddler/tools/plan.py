"""Plan models and the ``propose_plan`` tool.

The plan is markdown: the tool's arguments carry a short title and the
plan body, and the session presents it for the user's decision.  Progress
during execution is the general todo list, not part of the plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from toddler.tools.base import BaseTool, ToolResult
from toddler.tools.policy import Permission
from toddler.tools.prompts import (
    ChoiceLevel,
    ChoiceType,
    PromptChoice,
    PromptChoices,
)
from toddler.tools.registry import TOOL_CATALOG

__all__ = [
    "PLAN_CHOICE_APPROVE", "PLAN_CHOICE_APPROVE_AUTO",
    "PLAN_CHOICE_FEEDBACK", "PLAN_CHOICE_REJECT",
    "PROPOSE_PLAN_NAME",
    "Plan", "PlanValidationError", "ProposePlanTool",
]

#: Name of the plan-proposal tool.
PROPOSE_PLAN_NAME = "propose_plan"

#: Row indices of the plan approval pause.  The session facade answers
#: with one of these; the tool itself consumes none of them.
PLAN_CHOICE_APPROVE = 0
PLAN_CHOICE_APPROVE_AUTO = 1
PLAN_CHOICE_REJECT = 2
PLAN_CHOICE_FEEDBACK = 3


class PlanValidationError(ValueError):
    """A model-authored plan that cannot be presented or executed.

    Raised while parsing a ``propose_plan`` call; the loop turns the
    message into the call's failed tool result so the model can repair the
    plan instead of the user receiving a malformed card.
    """


def _single_line(text: str) -> str:
    """Collapse whitespace and newlines in *text* to single spaces.

    The title renders as a one-line label in the plan card and the plan
    line, so a multi-line value would break both.
    """
    return " ".join(text.split())


@dataclass
class Plan:
    """A markdown execution plan proposed by the agent.

    Parameters
    ----------
    id:
        Plan identity — the tool call id, assigned when the session parses
        the proposal, so the approval round-trip uses the loop's own
        staleness check.
    title:
        Short title for the card and the tab.
    markdown:
        The plan body, written for the user to read.
    """

    id: str
    title: str
    markdown: str

    @classmethod
    def from_arguments(cls, parameters: dict, *, plan_id: str) -> Plan:
        """Build a plan from a ``propose_plan`` call's arguments.

        Raises :class:`PlanValidationError` for a plan the user cannot
        read.
        """
        raw_title = parameters.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            raise PlanValidationError("title must be a non-empty string.")
        markdown = parameters.get("plan_markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise PlanValidationError(
                "plan_markdown must be a non-empty string."
            )
        return cls(
            id=plan_id,
            title=_single_line(raw_title),
            markdown=markdown.strip(),
        )

    def format_for_display(self) -> str:
        """Return the plan body for display."""
        return self.markdown


@TOOL_CATALOG.register()
class ProposePlanTool(BaseTool):
    """Present a markdown plan for the user to approve.

    ``INTERACT``: the loop pauses on the call and the session translates
    the pause into a ``PlanProposed`` event.  The call runs only when the
    user approves it; its result tells the model to execute the plan and
    keep a todo list.  Hidden from tool cards, and registered for plan
    turns only.
    """

    name = PROPOSE_PLAN_NAME
    description = (
        "Present a complete execution plan when you have finished "
        "researching and the user should decide whether to proceed. "
        "Write the plan for the user to read: context, the approach, "
        "ordered steps, the files involved, risks, and how to verify the "
        "work.  Be concrete and specific to this codebase.  Do not write "
        "the plan as prose in your reply — call this tool instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the plan.",
            },
            "plan_markdown": {
                "type": "string",
                "description": (
                    "The complete plan as markdown. Use headings and an "
                    "ordered list of steps; include files, risks, and "
                    "verification."
                ),
            },
        },
        "required": ["title", "plan_markdown"],
    }

    #: title and plan_markdown are the whole call — no call-description
    #: param.
    require_description_parameter = False

    #: PlanProposed is the display; a card for the call would repeat it.
    visible = False

    #: Registered for plan turns by the session, never handed to every
    #: session.
    in_default_registry = False

    @property
    def permission(self) -> Permission:
        """Input, not risk: the pause is the user's plan decision."""
        return Permission.INTERACT

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        """The pause asks for a decision on the plan, not an approval."""
        return "Approve this plan?"

    def _prompt_content(self, **kwargs) -> str | None:
        """The plan itself — the card's body."""
        return Plan.from_arguments(kwargs, plan_id="").markdown

    def _prompt_description(self, **kwargs) -> str | None:
        """The plan's short title, for the card's one-line summary."""
        return Plan.from_arguments(kwargs, plan_id="").title

    def _prompt_choices(self, **kwargs) -> PromptChoices:
        """Validate the plan before the card is shown.

        A malformed plan must go back to the model as a failed call, not
        reach the user as an unreadable card.
        """
        Plan.from_arguments(kwargs, plan_id="")
        return PromptChoices(
            type=ChoiceType.SINGLE,
            data=(
                PromptChoice("Approve", ChoiceLevel.APPROVE),
                PromptChoice(
                    "Approve and run automatic", ChoiceLevel.APPROVE,
                ),
                PromptChoice("Reject", ChoiceLevel.DENY),
                PromptChoice(
                    "Tell Toddler what to change", ChoiceLevel.FEEDBACK,
                ),
            ),
        )

    async def execute(
        self, title: object, plan_markdown: object,
    ) -> ToolResult:
        """Return the approval ack after the user granted the call.

        The plan body stays in the assistant's own call; the ack carries
        the execution contract and the todo_list instruction.
        """
        try:
            plan = Plan.from_arguments(
                {"title": title, "plan_markdown": plan_markdown},
                plan_id="",
            )
        except PlanValidationError as exc:
            return ToolResult(
                tool_id="",
                tool_name=self.name,
                success=False,
                output="",
                error=(
                    f"Invalid propose_plan call: {exc} "
                    "Send the complete corrected plan."
                ),
            )
        return ToolResult(
            tool_id="",
            tool_name=self.name,
            success=True,
            output=(
                f"The user approved this plan: {plan.title}. Execute it "
                "now, in order. Maintain a todo list with todo_write and "
                "keep it current. If a step cannot be completed as written, "
                "stop and explain rather than deviating."
            ),
        )

    def summarize_call(self, **kwargs) -> str:
        raw = kwargs.get("title")
        title = " ".join(str(raw).split()) if raw else "(untitled plan)"
        return f"propose_plan({title!r})"
