"""Shell command classification — every command in it, or nothing.

:func:`~toddler.tools.shell.classify_command` decides whether a command
is auto-approved or needs confirmation.  The rule under test is that a
safe prefix must not launder what follows it: a command is safe only when
every command it runs is, however they are joined.
"""

from __future__ import annotations

import pytest

from toddler.tools.base import Permission
from toddler.tools.shell import Shell, classify_command

# ---------------------------------------------------------------------------
# Chaining
# ---------------------------------------------------------------------------


class TestSegmentChaining:
    """One unsafe command is enough, at any position and any joiner."""

    @pytest.mark.parametrize(
        "command",
        [
            "ls && python exploit.py",
            "ls | python exploit.py",
            "ls; python exploit.py",
            "ls\npython exploit.py",
            "ls && git commit -m x",
            "git status\ngit commit -m x",
        ],
    )
    def test_a_safe_prefix_does_not_launder_the_rest(self, command):
        assert classify_command(command) is Permission.SHELL_DANGEROUS

    @pytest.mark.parametrize(
        "command",
        [
            "ls && echo hi",
            "ls | wc -l",
            "echo one\necho two",
            "find . -name '*.py' -type f | wc -l",
        ],
    )
    def test_all_segments_safe_is_safe(self, command):
        assert classify_command(command) is Permission.SHELL_SAFE

    def test_a_separator_inside_quotes_is_not_a_boundary(self):
        """Quoting is what keeps this one command."""
        assert classify_command('echo "a; b"') is Permission.SHELL_SAFE


# ---------------------------------------------------------------------------
# Single commands
# ---------------------------------------------------------------------------


class TestKnownShapes:
    """The one-command cases the classifier already knew.

    A segment carries more than the first token now, so these pin the
    verdicts that must not move with it.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat file.txt > out.txt",
            "echo hi 2>&1",
            "git status",
            "git log -n5 --oneline",
            "git branch -a",
            "python -V",
            "node --version",
        ],
    )
    def test_safe(self, command):
        assert classify_command(command) is Permission.SHELL_SAFE

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp/x",
            "sudo ls",
            "git push",
            "git branch -D feature",
            "git checkout -b new",
            "python x.py",
            "python -c 'print(1)'",
            "node script.js",
        ],
    )
    def test_dangerous(self, command):
        assert classify_command(command) is Permission.SHELL_DANGEROUS

    @pytest.mark.parametrize(
        "command",
        ["git -C /tmp push", "git --git-dir=/tmp/r commit -m x"],
    )
    def test_a_flag_does_not_hide_the_subcommand(self, command):
        """Scanning every argument rather than the subcommand slot."""
        assert classify_command(command) is Permission.SHELL_DANGEROUS


# ---------------------------------------------------------------------------
# What the classifier refuses to reason about
# ---------------------------------------------------------------------------


class TestInvisibleNesting:
    """A substitution hides a nested command — confirm instead."""

    @pytest.mark.parametrize(
        "command", ["echo $(ls)", "echo `ls`", 'echo "$(python x)"'],
    )
    def test_dangerous(self, command):
        assert classify_command(command) is Permission.SHELL_DANGEROUS


class TestUnparseable:
    """Uncertain is dangerous, including "this is not even a command"."""

    @pytest.mark.parametrize("command", ["", "   ", 'echo "unbalanced'])
    def test_dangerous(self, command):
        assert classify_command(command) is Permission.SHELL_DANGEROUS


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class TestShellTool:
    """The tool asks the classifier per call, and runs whole scripts."""

    def test_permission_comes_from_the_command(self):
        tool = Shell()
        assert tool.get_permission(command="ls -la") is Permission.SHELL_SAFE
        assert (
            tool.get_permission(command="ls && rm -rf /tmp/x")
            is Permission.SHELL_DANGEROUS
        )

    async def test_a_multi_line_command_runs_every_line(self):
        """Newlines are a script, not a rejection: the whole string goes
        to the shell, which is why classification has to read it all."""
        result = await Shell().execute(command="echo one\necho two")

        assert result.success
        assert result.output == "one\ntwo"
