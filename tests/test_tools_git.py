"""Git tools — the rules every command in the module runs under.

Two of them are shared rather than per-tool: the process runs under
``LC_ALL=C``, so git answers in one language whoever calls it, and every
stdout is capped, so one call cannot spend the whole context window on a
diff of a large refactor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from toddler.tools.git import _MAX_OUTPUT, GitDiff, _git, _truncate

# ============================================================================
# Helpers
# ============================================================================


def _git_repo(tmp_path) -> Path:
    """A real repo with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


# ============================================================================
# The cap
# ============================================================================


class TestTruncate:
    """Text over the cap is cut, and the cut is visible in the text."""

    def test_text_within_the_cap_is_untouched(self):
        assert _truncate("small") == "small"

    def test_text_over_the_cap_says_how_much_was_cut(self):
        cut = _truncate("x" * (_MAX_OUTPUT + 10))

        assert cut.startswith("x" * 100)
        assert cut.endswith("(truncated 10 chars)")
        assert len(cut) == _MAX_OUTPUT + len("\n\n... (truncated 10 chars)")

    async def test_a_huge_diff_reaches_the_model_capped(self, tmp_path):
        """The cap is wired to the tools, not just available to them."""
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text(
            "\n".join("y" * 40 for _ in range(3000)), encoding="utf-8"
        )

        result = await GitDiff().execute(repo_path=str(repo))

        assert result.success
        assert "(truncated " in result.output
        assert len(result.output) < _MAX_OUTPUT + 100


# ============================================================================
# The locale
# ============================================================================


class TestLocale:
    """Git runs in the C locale, whatever the caller's environment says."""

    async def test_the_callers_locale_does_not_reach_git(self, monkeypatch):
        # An alias body runs in a shell git spawns with its own environ, so
        # this echoes back the locale git itself was given.
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")

        stdout, _stderr, rc = await _git(
            "-c", "alias.leak=!echo LC_ALL=$LC_ALL", "leak"
        )

        assert rc == 0
        assert stdout.strip() == "LC_ALL=C"
