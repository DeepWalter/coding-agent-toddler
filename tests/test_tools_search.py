"""Search tools — how much comes back, and which paths are searched.

Two rules the tools state but a bare ``grep``/``glob`` never did:
``max_results`` bounds a search *in total* rather than per file, and the
ignored-directory filter judges only the parts below the search root — the
root is a directory the caller named, so whatever it is called must not
decide whether anything is found.
"""

from __future__ import annotations

import pytest

from toddler.tools.search import Glob, Grep

# ============================================================================
# Fixtures and helpers
# ============================================================================


@pytest.fixture
def tree(tmp_path):
    """Three matching files, plus three matches inside ``node_modules``."""
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("hit\nhit\nhit\n", encoding="utf-8")
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "d.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")
    return tmp_path


def _match_lines(output: str) -> list[str]:
    """The match lines in *output*, ignoring the truncation notice."""
    return [line for line in output.splitlines() if ":hit" in line]


# ============================================================================
# Grep — the result cap
# ============================================================================


class TestGrepResultCap:
    """``-m`` bounds matches per file; ``max_results`` bounds the search."""

    async def test_the_cap_holds_across_files(self, tree):
        result = await Grep().execute(
            pattern="hit", path=str(tree), max_results=4
        )

        assert result.success
        assert len(_match_lines(result.output)) == 4
        assert "(5 more matches not shown)" in result.output

    async def test_it_still_counts_everything_it_found(self, tree):
        result = await Grep().execute(
            pattern="hit", path=str(tree), max_results=4
        )

        assert result.metadata["match_count"] == 9
        assert result.metadata["truncated"] is True

    async def test_a_search_under_the_cap_is_not_truncated(self, tree):
        result = await Grep().execute(
            pattern="hit", path=str(tree), max_results=100
        )

        assert len(_match_lines(result.output)) == 9
        assert result.metadata["truncated"] is False
        assert "more matches" not in result.output

    async def test_no_matches_is_a_success(self, tree):
        result = await Grep().execute(
            pattern="nothing-matches-this", path=str(tree)
        )

        assert result.success
        assert result.output == (
            "No matches found for pattern: nothing-matches-this"
        )
        assert result.metadata["match_count"] == 0


class TestGrepIgnoredDirs:
    """Build and vendored trees are skipped, so the cap is spent on source."""

    async def test_a_vendored_dir_is_not_searched(self, tree):
        result = await Grep().execute(
            pattern="hit", path=str(tree), max_results=100
        )

        assert "node_modules" not in result.output
        assert result.metadata["match_count"] == 9


class TestGrepPattern:
    """The pattern is a pattern, whatever it starts with."""

    async def test_a_leading_dash_is_not_read_as_a_flag(self, tmp_path):
        (tmp_path / "a.txt").write_text("-rn is literal\n", encoding="utf-8")

        result = await Grep().execute(pattern="-rn", path=str(tmp_path))

        assert result.success
        assert "a.txt" in result.output


class TestGrepFallback:
    """Without grep on PATH the pure-Python scan holds the same rules."""

    async def test_it_ignores_and_caps_like_the_grep_path(self, tree):
        result = await Grep()._fallback_search("hit", tree, None, 4, False)

        assert len(_match_lines(result.output)) == 4
        assert "node_modules" not in result.output
        assert result.metadata["match_count"] == 9
        assert result.metadata["fallback"] is True


# ============================================================================
# Glob — what counts as an ignored path
# ============================================================================


class TestGlobBaseFiltering:
    """Only the parts below the base are judged."""

    async def test_a_base_under_an_ignored_name_still_finds_files(
        self, tmp_path
    ):
        """``~/build/proj`` is a base the caller chose, not a build tree."""
        src = tmp_path / "build" / "src"
        src.mkdir(parents=True)
        (src / "a.py").write_text("x", encoding="utf-8")

        result = await Glob().execute(
            pattern="**/*.py", path=str(tmp_path / "build")
        )

        assert result.output == "src/a.py"

    async def test_an_ignored_dir_below_the_base_is_skipped(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("x", encoding="utf-8")
        vendored = tmp_path / "node_modules" / "pkg"
        vendored.mkdir(parents=True)
        (vendored / "b.py").write_text("x", encoding="utf-8")

        result = await Glob().execute(pattern="**/*.py", path=str(tmp_path))

        assert result.output == "src/a.py"

    async def test_a_hidden_file_below_the_base_is_skipped(self, tmp_path):
        (tmp_path / ".hidden.py").write_text("x", encoding="utf-8")
        (tmp_path / "visible.py").write_text("x", encoding="utf-8")

        result = await Glob().execute(pattern="*.py", path=str(tmp_path))

        assert result.output == "visible.py"
