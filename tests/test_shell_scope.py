"""Shell file scope — where a command's files live.

``shell`` is the one tool whose reach is not in its parameters: the files
it opens are named inside the command string, by an operand, a
redirection, a flag value or a glob.  Reading those is what stops ``cat
.env`` — classified ``SHELL_SAFE`` by command name alone — from being
auto-approved, and what makes ``ls ..`` a decision rather than a habit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toddler.tools.base import FileScope, FileScopePolicy
from toddler.tools.shell import Shell


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A workspace the command runs in — the policy's root *and* the
    process working directory, which is where ``execute`` actually runs."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("notes\n")
    (tmp_path / ".env").write_text("API_KEY=secret\n")
    (tmp_path / ".env.local").write_text("DEBUG=1\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def shell(workspace: Path) -> Shell:
    policy = FileScopePolicy(workspace, (".env", ".env.local"))
    return Shell(scope_policy=policy)


def scope_of(shell: Shell, command: str, **kwargs) -> FileScope:
    return shell.get_file_scope(command=command, **kwargs)


# ---------------------------------------------------------------------------
# What the command names
# ---------------------------------------------------------------------------


class TestOperands:
    """A path argument is a path, whether or not it exists — a redirection
    target is one the command is about to create."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env",
            "cat ./.env",
            "head -n 5 .env",
            "wc -l .env",
            "sed -n 1p .env",
        ],
    )
    def test_a_restricted_operand_is_restricted(self, shell, command):
        assert scope_of(shell, command) is FileScope.RESTRICTED

    def test_a_restricted_operand_spelled_through_a_link_dir(self, shell, workspace):
        (workspace / "sub").mkdir()
        assert scope_of(shell, "cat sub/../.env") is FileScope.RESTRICTED

    def test_a_workspace_file_is_the_workspace(self, shell):
        assert scope_of(shell, "cat notes.txt") is FileScope.WORKSPACE

    def test_a_file_outside_is_external(self, shell):
        assert scope_of(shell, "cat /etc/hosts") is FileScope.EXTERNAL

    def test_a_relative_step_out_is_external(self, shell, workspace):
        (workspace.parent / "outside.txt").write_text("x\n")
        assert scope_of(shell, "cat ../outside.txt") is FileScope.EXTERNAL

    def test_a_name_that_is_not_a_file_is_nothing(self, shell):
        """``echo hi`` opens nothing; a word that matches no file on disk
        is not evidence of reading one."""
        assert scope_of(shell, "echo hi") is FileScope.WORKSPACE

    def test_the_working_directory_is_part_of_the_reach(self, shell, workspace):
        """A command runs *in* its working directory, so a session rooted
        elsewhere asks rather than allowing whatever it finds."""
        assert scope_of(shell, "ls", working_dir="/tmp") is FileScope.EXTERNAL

    def test_an_unresolvable_working_directory_asks(self, shell):
        assert scope_of(shell, "ls", working_dir="~nosuchuser/x") is FileScope.EXTERNAL


class TestRedirections:
    """A redirection names a file the command is about to write, which is
    the one operand that is a path before it exists."""

    def test_a_write_target_that_does_not_exist_yet_is_still_a_path(self, shell):
        assert scope_of(shell, "echo x > .env") is FileScope.RESTRICTED

    def test_an_append_target_is_read_the_same_way(self, shell):
        assert scope_of(shell, "echo x >> .env") is FileScope.RESTRICTED

    def test_a_read_source_is_a_path(self, shell):
        assert scope_of(shell, "cat < .env") is FileScope.RESTRICTED

    def test_an_out_of_workspace_target_is_external(self, shell, tmp_path):
        target = tmp_path.parent / "elsewhere.txt"
        assert scope_of(shell, f"echo x > {target}") is FileScope.EXTERNAL

    def test_a_heredoc_delimiter_is_not_a_path(self, shell):
        assert scope_of(shell, "cat <<EOF") is FileScope.WORKSPACE

    def test_a_descriptor_redirect_is_not_a_path(self, shell):
        assert scope_of(shell, "ls 2>&1") is FileScope.WORKSPACE


class TestFlagValues:
    """A path can hide in a flag value; ``--file=.env`` is the common
    spelling, and it counts whether or not the file exists."""

    def test_a_flag_value_is_a_path(self, shell):
        assert scope_of(shell, "grep --file=.env notes.txt") is FileScope.RESTRICTED

    def test_an_out_of_workspace_flag_value_is_external(self, shell):
        assert (
            scope_of(shell, "git --git-dir=/tmp/repo status") is FileScope.EXTERNAL
        )

    def test_a_bare_flag_is_not_a_path(self, shell):
        assert scope_of(shell, "ls -la") is FileScope.WORKSPACE


class TestPatterns:
    """Some positions hold a pattern, not a file.  Confusing the two would
    refuse a call that opens nothing — and a refusal is not overrulable."""

    def test_a_grep_pattern_is_not_a_file(self, shell):
        """``grep -rn .env .`` searches for the string; it reads ``.``."""
        assert scope_of(shell, "grep -rn .env .") is FileScope.WORKSPACE

    def test_a_grep_pattern_alone_names_nothing(self, shell):
        assert scope_of(shell, "grep -rn .env") is FileScope.WORKSPACE

    def test_a_find_name_directive_is_a_pattern(self, shell):
        assert scope_of(shell, "find . -name .env") is FileScope.WORKSPACE

    def test_the_operands_after_a_pattern_are_still_paths(self, shell):
        assert scope_of(shell, "grep -rn foo .env") is FileScope.RESTRICTED


class TestGlobsAndExpansions:
    """What the scanner cannot see through asks; what it can see through
    is placed by its matches."""

    def test_a_glob_is_placed_by_what_it_matches(self, shell):
        """``cat .env*`` reads ``.env.local``, so the pattern is restricted
        for the same reason the file is."""
        assert scope_of(shell, "cat .env*") is FileScope.RESTRICTED

    def test_a_glob_that_matches_only_workspace_files_is_local(self, shell):
        assert scope_of(shell, "ls pkg/*.py") is FileScope.WORKSPACE

    def test_a_glob_that_matches_nothing_is_nothing(self, shell):
        assert scope_of(shell, "ls *.nope") is FileScope.WORKSPACE

    @pytest.mark.parametrize(
        "command",
        [
            "cat $HOME/.toddler/.env",
            'cat "$HOME"/.env',
            "cat ${HOME}/.env",
        ],
    )
    def test_an_expansion_asks_rather_than_assuming_local(self, shell, command):
        """The shell expands it at run time, so the file behind ``$HOME``
        is not knowable here — asking is the only honest answer."""
        assert scope_of(shell, command) is FileScope.EXTERNAL

    def test_a_restricted_match_outranks_the_expansion(self, shell):
        """A positive match is knowledge; an expansion is the absence of
        it.  The refusal stands."""
        assert (
            scope_of(shell, "cat .env $SECRET") is FileScope.RESTRICTED
        )


class TestSegments:
    """A command is what every segment of it does, and reaches what every
    segment of it names."""

    def test_a_later_segment_is_read(self, shell):
        assert scope_of(shell, "ls && cat .env") is FileScope.RESTRICTED

    def test_a_piped_segment_is_read(self, shell):
        assert scope_of(shell, "cat notes.txt | wc -l") is FileScope.WORKSPACE

    def test_an_unbalanced_quote_scans_nothing(self, shell):
        """It does not tokenize, so there is nothing to read; the command
        is dangerous either way and asks."""
        assert scope_of(shell, "cat 'unclosed") is FileScope.WORKSPACE

    def test_an_empty_command_names_nothing(self, shell):
        assert scope_of(shell, "") is FileScope.WORKSPACE


class TestPermissionIsUnchanged:
    """The scope is a second question, not a replacement for the tier: a
    safe command stays safe, a dangerous one stays dangerous."""

    def test_classification_still_comes_from_the_command(self, shell):
        from toddler.tools.base import Permission

        assert shell.get_permission(command="ls -la") is Permission.SHELL_SAFE
        assert (
            shell.get_permission(command="ls && rm -rf /tmp/x")
            is Permission.SHELL_DANGEROUS
        )
