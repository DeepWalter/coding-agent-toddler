"""Call prompt — what a gated call asks the user.

The gate used to hand every front-end one rendered string, which had to
serve both a terminal panel and a browser card and did neither well: the
shell command — the thing the decision is about — arrived truncated
inside a ``repr``, and the model's own line about the call was buried
among the other arguments.  :meth:`BaseTool.build_call_prompt` assembles
the record from three parts instead, each with a helper of its own so a
tool answers only the parts it has something to say about.  The tests
here pin what every tool answers for each of them, and the two renderings
the record gives back.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from toddler.tools.base import (
    ALLOW_CALL_TITLE,
    ONE_LINE_DETAIL_LIMIT,
    BaseTool,
    CallPrompt,
    ToolResult,
)
from toddler.tools.files import EditFile, ReadFile, WriteFile
from toddler.tools.git import GitCommit
from toddler.tools.search import Grep
from toddler.tools.shell import Shell


class _BareTool(BaseTool):
    """A tool whose call declares neither a title, a body, nor a path.

    A test double's shape, and the one the last description tier exists
    for: a card for a call like this has nothing else to say.
    """

    name = "bare"
    description = "Declare nothing"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:  # noqa: ARG002
        return ToolResult(
            tool_id="", tool_name=self.name, success=True, output=""
        )


class _TitledTool(_BareTool):
    """A tool that overrides one part and nothing else — the shape the
    three helpers exist for."""

    name = "titled"

    def _prompt_title(self, **kwargs) -> str:  # noqa: ARG002
        return "Allow this to happen?"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


class TestCallPrompt:
    """The three fields, their wire form, and the one line the terminal
    gets out of them."""

    def test_the_wire_form_keeps_a_missing_part_null(self):
        prompt = CallPrompt(title=ALLOW_CALL_TITLE, content="ls -la")
        assert prompt.to_dict() == {
            "title": ALLOW_CALL_TITLE,
            "content": "ls -la",
            "description": None,
        }

    def test_it_is_frozen(self):
        prompt = CallPrompt(title=ALLOW_CALL_TITLE)
        with pytest.raises(FrozenInstanceError):
            prompt.title = "other"

    def test_a_title_alone_is_the_whole_line(self):
        assert CallPrompt(title=ALLOW_CALL_TITLE).one_line() == (
            ALLOW_CALL_TITLE
        )

    def test_the_content_leads(self):
        prompt = CallPrompt(
            title=ALLOW_CALL_TITLE,
            content="ls -la",
            description="list the files",
        )
        assert prompt.one_line() == f"{ALLOW_CALL_TITLE} — ls -la"

    def test_the_description_answers_where_there_is_no_content(self):
        prompt = CallPrompt(
            title=ALLOW_CALL_TITLE, description="list the files"
        )
        assert prompt.one_line() == f"{ALLOW_CALL_TITLE} — list the files"

    def test_newlines_collapse(self):
        """The line is printed into a Rich panel title — a second line has
        nowhere to go, and markup the console would parse is a worse fate
        than a flattened one."""
        prompt = CallPrompt(
            title=ALLOW_CALL_TITLE, content="set -e\nfor f in *; do\n"
        )
        assert prompt.one_line() == (
            f"{ALLOW_CALL_TITLE} — set -e for f in *; do"
        )

    def test_a_long_detail_is_cut(self):
        prompt = CallPrompt(title=ALLOW_CALL_TITLE, content="x" * 500)
        line = prompt.one_line()
        assert len(line) <= ONE_LINE_DETAIL_LIMIT + len(ALLOW_CALL_TITLE) + 3
        assert line.endswith("…")


# ---------------------------------------------------------------------------
# The three parts
# ---------------------------------------------------------------------------


class TestPromptParts:
    """Each part is asked for on its own, so a tool answers the one it
    knows about."""

    def test_the_base_answers_a_default_title_and_no_content(self):
        assert _BareTool()._prompt_title() == ALLOW_CALL_TITLE
        assert _BareTool()._prompt_content(command="ls -la") is None

    def test_one_override_leaves_the_other_two_answered(self):
        prompt = _TitledTool().build_call_prompt(anything="at all")
        assert prompt.title == "Allow this to happen?"
        assert prompt.content is None
        assert prompt.description == "titled(anything='at all')"


class TestShellPrompt:
    """The one tool whose call has a body: the command itself."""

    def test_the_command_is_the_content_verbatim(self):
        prompt = Shell().build_call_prompt(
            command="ls -la", description="list the files"
        )
        assert prompt.title == "Allow this bash command?"
        assert prompt.content == "ls -la"
        assert prompt.description == "list the files"

    def test_it_is_not_truncated(self):
        """``summarize_call`` cuts at 60 chars, and must keep doing so —
        it is what a refusal and a checkpoint name the call by.  The card
        shows the command whole and scrolls it."""
        command = "python -c " + "'" + "x" * 200 + "'"
        prompt = Shell().build_call_prompt(command=command)
        assert prompt.content == command
        assert Shell().summarize_call(command=command) != command

    def test_a_script_keeps_its_breaks(self):
        command = "set -e\nfor f in *; do\n  echo $f\ndone\n"
        assert Shell().build_call_prompt(command=command).content == command

    def test_without_a_description_the_summary_answers(self):
        prompt = Shell().build_call_prompt(command="ls -la")
        assert prompt.description == "shell('ls -la')"

    def test_an_empty_command_is_no_content(self):
        assert Shell().build_call_prompt(command="   ").content is None
        assert Shell().build_call_prompt().content is None


class TestFilePrompt:
    """A file call is about a file: the card asks about that file, and no
    argument of it is a content block (the card next door opens the file
    itself)."""

    @pytest.mark.parametrize(
        ("tool", "title"),
        [
            (ReadFile(), "Allow read this file?"),
            (WriteFile(), "Allow write this file?"),
            (EditFile(), "Allow edit this file?"),
        ],
    )
    def test_the_file_names_the_call(self, tool, title, tmp_path):
        target = tmp_path / "notes.md"
        prompt = tool.build_call_prompt(file_path=str(target))
        assert prompt.title == title
        assert prompt.content is None
        assert prompt.description == str(target)

    def test_the_path_is_absolute_and_lifted_from_home(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        assert ReadFile().build_call_prompt(
            file_path="notes.md"
        ).description == str(tmp_path / "notes.md")
        assert ReadFile().build_call_prompt(
            file_path="~/notes.md"
        ).description == str(Path.home() / "notes.md")

    def test_a_link_is_left_where_it_is(self, tmp_path):
        """The same rule :func:`display_path` holds for a message: the
        reader is told the path they named, not the one the disk knows."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "linked"
        link.symlink_to(real)

        prompt = WriteFile().build_call_prompt(file_path=str(link / "notes.md"))
        assert prompt.description == str(link / "notes.md")

    def test_a_path_that_cannot_be_spelled_falls_through(self):
        """``read_file`` summarises by ``Path(...).name``, which a number
        is not — so the fallback is the tool's own name.  A classifier
        runs on model-authored arguments, and one bad value must not
        take down the gate that offers to run the call."""
        assert ReadFile().build_call_prompt(file_path=5).description == "read_file"


class TestDescriptionsElsewhere:
    """The tools that carry a description parameter: the model's own line
    when it wrote one, the path or the summary when it did not."""

    def test_grep_prefers_the_description(self):
        prompt = Grep().build_call_prompt(
            pattern="TODO", path="toddler", description="find the TODOs"
        )
        assert prompt.title == ALLOW_CALL_TITLE
        assert prompt.content is None
        assert prompt.description == "find the TODOs"

    def test_grep_falls_back_to_the_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prompt = Grep().build_call_prompt(pattern="TODO", path="toddler")
        assert prompt.description == str(tmp_path / "toddler")

    def test_a_git_call_falls_back_to_its_summary(self):
        prompt = GitCommit().build_call_prompt(message="fix the gate")
        assert prompt.description == "git_commit('fix the gate')"

    def test_a_bare_call_still_names_itself(self):
        """The last tier: a call declaring neither a description nor a
        path draws a card with something in it rather than nothing."""
        prompt = _BareTool().build_call_prompt(anything="at all")
        assert prompt.title == ALLOW_CALL_TITLE
        assert prompt.description == "bare(anything='at all')"
