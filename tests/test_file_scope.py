"""File scope — placing a path, and the tool that reports where it reaches.

:class:`~toddler.tools.base.FileScopePolicy` answers the question a
permission tier cannot: *where* does this call reach.  A file inside the
workspace is the agent's own ground, a file outside it is the user's call,
and a restricted file is nobody's — the policy is what makes ``.env`` a
refusal rather than a confirmation prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toddler.tools import create_default_registry
from toddler.tools.base import BaseTool, FileScope, FileScopePolicy, ToolResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace with a ``.env``, a secrets directory, and a module."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("API_KEY=secret\n")
    (tmp_path / ".env.local").write_text("DEBUG=1\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.pem").write_text("-----BEGIN\n")
    return tmp_path


@pytest.fixture
def policy(workspace: Path) -> FileScopePolicy:
    return FileScopePolicy(workspace, (".env", ".env.local", "secrets"))


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


class TestClassify:
    """The three places a path can be, and the two spellings of each."""

    def test_the_root_itself_is_the_workspace(self, policy, workspace):
        assert policy.classify(workspace) is FileScope.WORKSPACE

    def test_a_file_beneath_the_root_is_the_workspace(self, policy, workspace):
        assert policy.classify(workspace / "pkg" / "mod.py") is FileScope.WORKSPACE

    def test_a_relative_path_lands_where_the_tool_would_read_it(
        self, policy, workspace, monkeypatch
    ):
        """Tools resolve their arguments against the process working
        directory, so the policy has to place them the same way."""
        monkeypatch.chdir(workspace)
        assert policy.classify("pkg/mod.py") is FileScope.WORKSPACE

    def test_an_escape_by_dotdot_leaves_the_workspace(self, policy, workspace):
        assert policy.classify(workspace / ".." / "elsewhere") is FileScope.EXTERNAL

    def test_outside_the_root_is_external(self, policy):
        assert policy.classify("/etc/hosts") is FileScope.EXTERNAL

    def test_a_sibling_of_the_root_is_external(self, policy, workspace):
        assert policy.classify(workspace.parent / "other") is FileScope.EXTERNAL

    def test_a_prefix_that_only_looks_inside_is_external(self, policy, workspace):
        """``/ws-other`` starts with ``/ws`` as a string and is not in it."""
        assert policy.classify(f"{workspace}-other") is FileScope.EXTERNAL


class TestRestricted:
    """Restricted beats both — a file is refused, a directory is refused
    down its whole subtree."""

    def test_a_listed_file_is_restricted(self, policy, workspace):
        assert policy.classify(workspace / ".env") is FileScope.RESTRICTED

    def test_a_beneath_a_listed_directory_is_restricted(self, policy, workspace):
        assert policy.classify(workspace / "secrets" / "key.pem") is FileScope.RESTRICTED

    def test_a_sibling_with_a_shared_prefix_is_not(self, policy, workspace):
        (workspace / "secrets.txt").write_text("notes\n")
        assert policy.classify(workspace / "secrets.txt") is FileScope.WORKSPACE

    def test_a_restricted_path_that_does_not_exist_yet_is_still_restricted(
        self, policy, workspace
    ):
        """``echo x > .env`` writes a file that is not there yet."""
        assert policy.classify(workspace / ".env") is FileScope.RESTRICTED

    def test_an_absolute_entry_is_restricted_from_anywhere(self, workspace):
        outside = workspace.parent / "known-secret"
        outside.write_text("s\n")
        policy = FileScopePolicy(workspace, (str(outside),))
        assert policy.classify(outside) is FileScope.RESTRICTED

    def test_a_home_entry_expands_to_home(self, workspace):
        policy = FileScopePolicy(workspace, ("~/.toddler/.env",))
        target = Path.home() / ".toddler" / ".env"
        assert policy.classify(target) is FileScope.RESTRICTED

    def test_a_restricted_entry_written_through_a_symlink(self, workspace):
        """The entry may itself be a link; the tree it points at is what
        gets opened, so that is what is restricted."""
        target = workspace.parent / "real-secrets"
        target.mkdir()
        (target / "key.pem").write_text("-----BEGIN\n")
        link = workspace / "linked"
        link.symlink_to(target)

        policy = FileScopePolicy(workspace, ("./linked",))
        assert policy.classify(link / "key.pem") is FileScope.RESTRICTED

    def test_a_listed_file_swapped_for_a_link_out_stays_restricted(self, policy, workspace):
        """The resolved reading follows the link out of the workspace and
        would say "external — ask"; the lexical one knows the user listed
        this path, so the answer is still no."""
        secret = workspace.parent / "elsewhere.txt"
        secret.write_text("s\n")
        env = workspace / ".env"
        env.unlink()
        env.symlink_to(secret)

        assert policy.classify(env) is FileScope.RESTRICTED


class TestClassifyMany:
    """One call can name several paths; the strictest of them decides."""

    def test_no_paths_is_the_workspace(self, policy):
        assert policy.classify_many([]) is FileScope.WORKSPACE

    def test_all_inside_is_the_workspace(self, policy, workspace):
        assert (
            policy.classify_many([workspace, workspace / "pkg"])
            is FileScope.WORKSPACE
        )

    def test_one_external_makes_the_call_external(self, policy, workspace):
        assert (
            policy.classify_many([workspace / "pkg", "/etc/hosts"])
            is FileScope.EXTERNAL
        )

    def test_one_restricted_makes_the_call_restricted(self, policy, workspace):
        assert (
            policy.classify_many([workspace / "pkg", "/etc/hosts", workspace / ".env"])
            is FileScope.RESTRICTED
        )


class TestRestrictedMatch:
    """The denial names the path it refused, so the model knows which
    argument was the problem."""

    def test_names_the_matched_path(self, policy, workspace):
        match = policy.restricted_match([workspace / "pkg", workspace / ".env"])
        assert match == workspace / ".env"

    def test_is_none_when_nothing_matches(self, policy, workspace):
        assert policy.restricted_match([workspace / "pkg"]) is None


class TestUnusableInput:
    """A classifier runs on model-authored arguments before the call is
    dispatched — bad input has to answer, not raise."""

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "~nosuchuser/x", "a\x00b"],
    )
    def test_an_unplaceable_path_asks_rather_than_raising(self, policy, value):
        assert policy.classify(value) is FileScope.EXTERNAL

    def test_an_entry_that_cannot_be_resolved_is_dropped(self, workspace):
        policy = FileScopePolicy(workspace, ("~nosuchuser/x", "", "   "))
        assert policy.restricted == ()

    def test_a_path_like_object_is_accepted(self, policy, workspace):
        assert policy.classify(workspace / ".env") is FileScope.RESTRICTED


# ---------------------------------------------------------------------------
# The tool side
# ---------------------------------------------------------------------------


class ProbeTool(BaseTool):
    """A tool that names one file, to exercise the default implementation."""

    name = "probe"
    description = "Touch a file."
    parameters = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
    }
    path_params = ("file_path",)

    async def execute(self, **kwargs) -> ToolResult:  # pragma: no cover
        return ToolResult("", self.name, True, "")


class TestToolScope:
    """A tool answers for its own call, in the base its own arguments use."""

    def test_declared_paths_are_placed(self, policy, workspace):
        tool = ProbeTool(scope_policy=policy)
        assert (
            tool.get_file_scope(file_path=str(workspace / ".env"))
            is FileScope.RESTRICTED
        )
        assert (
            tool.get_file_scope(file_path=str(workspace / "pkg" / "mod.py"))
            is FileScope.WORKSPACE
        )
        assert tool.get_file_scope(file_path="/etc/hosts") is FileScope.EXTERNAL

    def test_a_tool_that_names_no_file_is_the_workspace(self, policy):
        tool = ProbeTool(scope_policy=policy)
        assert tool.get_file_scope() is FileScope.WORKSPACE

    def test_a_tool_built_without_a_policy_falls_back_to_the_working_directory(
        self, workspace, monkeypatch
    ):
        """A bare tool — a test double, the definition-time catalog — still
        answers; the answer is just about wherever the process is."""
        monkeypatch.chdir(workspace)
        tool = ProbeTool()
        assert tool.get_file_scope(file_path=".env") is FileScope.RESTRICTED


class TestRestrictedPathsConfig:
    """Which paths are off-limits is configuration, and its default is the
    reason the feature exists: the secrets a project keeps out of git."""

    def test_the_defaults_list_the_env_files(self):
        from toddler.config import defaults

        assert ".env" in defaults.RESTRICTED_PATHS
        assert ".env.local" in defaults.RESTRICTED_PATHS
        assert "~/.toddler/.env" in defaults.RESTRICTED_PATHS

    def test_an_unset_variable_takes_the_defaults(self, monkeypatch):
        from toddler.config import defaults
        from toddler.config.settings import Settings

        monkeypatch.delenv("TODDLER_RESTRICTED_PATHS", raising=False)
        assert Settings().restricted_paths == defaults.RESTRICTED_PATHS

    def test_the_variable_replaces_the_list(self, monkeypatch):
        import os

        from toddler.config.settings import Settings

        monkeypatch.setenv(
            "TODDLER_RESTRICTED_PATHS",
            os.pathsep.join(["~/keys", " notes.md ", "vault"]),
        )
        assert Settings().restricted_paths == ("~/keys", "notes.md", "vault")

    def test_a_blank_variable_is_unset_not_an_empty_list(self, monkeypatch):
        """A stray ``TODDLER_RESTRICTED_PATHS=`` must not quietly unprotect
        the files the defaults exist to keep the agent out of."""
        from toddler.config import defaults
        from toddler.config.settings import Settings

        monkeypatch.setenv("TODDLER_RESTRICTED_PATHS", "   ")
        assert Settings().restricted_paths == defaults.RESTRICTED_PATHS


class TestPolicyThreading:
    """Every default tool carries the session's policy — the gate reads the
    tool's answer, so a tool that kept a default one would be judged
    against the wrong workspace."""

    def test_every_default_tool_gets_the_policy(self, workspace):
        policy = FileScopePolicy(workspace, (".env",))
        registry = create_default_registry(scope_policy=policy)

        tools = registry.list_all()
        assert tools
        assert all(tool.scope_policy is policy for tool in tools)

    def test_without_a_policy_every_tool_still_answers(self):
        """Bare construction is the catalog's contract; the fallback keeps
        a tool answerable rather than raising when the gate asks."""
        registry = create_default_registry()
        assert all(tool.scope_policy for tool in registry.list_all())
