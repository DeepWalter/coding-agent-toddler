"""Path naming — what a tool opens, and what it says it opened.

The two are not always spelled the same.  ``resolve()`` is what the
filesystem needs: it follows links, which on macOS turns ``/tmp`` into
``/private/tmp`` and ``/home`` into ``/System/Volumes/Data/home``.  A
message is for whoever asked, and they named the short spelling.  So a
tool resolves for the disk and names for the reader — and the tests here
pin both halves, including the places where the difference is not
cosmetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toddler.tools.base import display_path
from toddler.tools.files import ReadFile, WriteFile
from toddler.tools.search import Grep

# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestDisplayPath:
    """Absolute, ``~`` expanded, links left in place."""

    @pytest.fixture
    def linked(self, tmp_path: Path) -> tuple[Path, Path]:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "linked"
        link.symlink_to(real)
        return real, link

    def test_a_link_is_left_where_it_is(self, linked):
        real, link = linked
        assert display_path(link / "notes.txt") == link / "notes.txt"

    def test_it_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert display_path("notes.txt") == tmp_path / "notes.txt"
        assert display_path(".") == tmp_path

    def test_home_is_expanded(self):
        assert display_path("~/notes.txt") == Path.home() / "notes.txt"

    def test_dotdot_after_a_link_is_resolved(self, linked, tmp_path):
        """The kernel reads ``link/..`` as the parent of what the link
        points at, so naming the link would name a different file — the
        one place a display path is not the path as written."""
        real, link = linked
        outside = real / "outside.txt"
        outside.write_text("x\n")

        assert display_path(link / ".." / "outside.txt") == (
            tmp_path / "outside.txt"
        )

    def test_an_unexpandable_value_is_handed_back(self):
        assert display_path("~nosuchuser/x") == Path("~nosuchuser/x")


# ---------------------------------------------------------------------------
# What the tools say
# ---------------------------------------------------------------------------


@pytest.fixture
def linked_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A tree and a link to it — the two spellings of one directory."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "mod.py").write_text("x = 1\n")
    link = tmp_path / "linked"
    link.symlink_to(real)
    return real, link


class TestMessagesNameTheWrittenPath:
    def test_a_missing_file_is_named_as_asked(self, linked_workspace):
        real, link = linked_workspace
        result = _run(ReadFile().execute(file_path=str(link / "nope.txt")))

        assert not result.success
        assert str(link / "nope.txt") in result.error
        assert str(real / "nope.txt") not in result.error

    def test_a_write_reports_the_path_as_asked(self, linked_workspace):
        real, link = linked_workspace
        result = _run(
            WriteFile().execute(file_path=str(link / "new.txt"), content="hi")
        )

        assert result.success
        assert str(link / "new.txt") in result.output
        assert (real / "new.txt").read_text() == "hi"   # written where asked

    def test_a_successful_write_still_lands_on_the_file(self, tmp_path):
        target = tmp_path / "out.txt"
        result = _run(WriteFile().execute(file_path=str(target), content="hi"))
        assert result.success
        assert target.read_text() == "hi"


class TestGrepThroughALink:
    """The path handed to ``grep`` and the path printed by it are decided
    separately, because on macOS they have to be: BSD grep will not
    descend into a directory given to it as a link, and silently returns
    nothing rather than an error."""

    def test_the_search_finds_what_is_behind_the_link(self, linked_workspace):
        real, link = linked_workspace
        result = _run(Grep().execute(pattern="x =", path=str(link)))

        assert result.success
        assert "x = 1" in result.output
        assert result.metadata["match_count"] == 1

    def test_the_match_is_named_as_the_tree_was_asked_for(self, linked_workspace):
        real, link = linked_workspace
        result = _run(Grep().execute(pattern="x =", path=str(link)))

        assert str(link / "mod.py") in result.output
        assert str(real / "mod.py") not in result.output

    def test_a_search_under_the_link_is_named_the_same_way(self, linked_workspace):
        real, link = linked_workspace
        result = _run(Grep().execute(pattern="x =", path=str(link / "mod.py")))

        assert str(link / "mod.py") in result.output

    def test_a_search_that_reads_the_tree_directly_is_unchanged(
        self, linked_workspace
    ):
        real, link = linked_workspace
        result = _run(Grep().execute(pattern="x =", path=str(real)))

        assert str(real / "mod.py") in result.output


def _run(coro):
    """Drive one tool call — the tools are async, the assertions are not."""
    import asyncio

    return asyncio.run(coro)
