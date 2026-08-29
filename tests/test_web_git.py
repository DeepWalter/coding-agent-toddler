"""Git-status REST API tests — porcelain v1 -z parsing and the snapshot
endpoint against a real git repository.

The parser unit tests pin the git output format (including the rename
record order, which the docs make easy to get backwards); the endpoint
tests exercise a real ``git init`` repo like the one the server serves.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toddler.config.settings import Settings
from toddler.web.app import create_app
from toddler.web.git import (
    _dir_badges,
    _parse_records,
    build_sections,
    parse_status,
)

# ============================================================================
# Helpers
# ============================================================================


def _repo(tmp_path) -> Path:
    """The repo root used as ``repo_root`` — created on demand."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def _git_repo(tmp_path) -> Path:
    """A real git repo with one committed file."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def _app(tmp_path):
    """An app with a tmp session dir and the repo root inside it."""
    settings = Settings(session_dir=tmp_path)
    return create_app(settings, repo_root=_repo(tmp_path))


# ============================================================================
# parse_status — pins the porcelain v1 -z format
# ============================================================================


class TestParseStatus:
    def test_clean_branch(self):
        assert parse_status(b"## main\x00") == ("main", {})

    def test_upstream_and_ahead_behind_suffixes(self):
        header = b"## feat/x...origin/feat/x [ahead 1, behind 2]\x00"
        assert parse_status(header) == ("feat/x", {})

    def test_detached_head(self):
        assert parse_status(b"## HEAD (no branch)\x00?? x\x00") == (
            "HEAD", {"x": "U"},
        )

    def test_unborn_branch(self):
        assert parse_status(b"## No commits yet on main\x00?? x.txt\x00") == (
            "main", {"x.txt": "U"},
        )

    def test_rename_badges_the_new_path(self):
        # Porcelain v1 -z emits a rename as `R  <new>\0<old>\0` — the
        # status-bearing record carries the NEW path; the bare record is
        # the old path, which is gone from disk and gets no badge.
        assert parse_status(
            b"## main\x00R  new.txt\x00old.txt\x00"
        ) == ("main", {"new.txt": "R"})

    def test_unmerged_collapses_to_c(self):
        for code in ("UU", "AA", "DU"):
            assert parse_status(b"## main\x00" + f"{code} f\x00".encode()) == (
                "main", {"f": "C"},
            )

    def test_mm_is_modified_not_conflict(self):
        assert parse_status(b"## main\x00MM f.txt\x00") == (
            "main", {"f.txt": "M"},
        )

    def test_staged_takes_precedence(self):
        assert parse_status(b"## main\x00AM f.txt\x00") == (
            "main", {"f.txt": "A"},
        )

    def test_untracked_dir_slash_stripped(self):
        assert parse_status(b"## main\x00?? dir/\x00") == (
            "main", {"dir": "U"},
        )

    def test_paths_with_spaces_and_unicode(self):
        record = "M  dir with space/ümlaut.txt".encode()
        assert parse_status(b"## main\x00" + record + b"\x00") == (
            "main", {"dir with space/ümlaut.txt": "M"},
        )

    def test_empty_output(self):
        assert parse_status(b"") == (None, {})

    def test_no_branch_header(self):
        assert parse_status(b"?? x.txt\x00") == (None, {"x.txt": "U"})


# ============================================================================
# build_sections — staged/unstaged split for the source-control panel
# ============================================================================


class TestBuildSections:
    def test_clean(self):
        assert build_sections([]) == ({}, {})

    def test_modified_in_both_axes(self):
        # MM — staged + unstaged modification, in both sections.
        assert build_sections([("f.txt", "M", "M")]) == (
            {"f.txt": "M"}, {"f.txt": "M"},
        )

    def test_untracked_is_unstaged_only(self):
        assert build_sections([("f.txt", "?", "?")]) == (
            {}, {"f.txt": "U"},
        )

    def test_staged_add_plus_unstaged_modify(self):
        assert build_sections([("f.txt", "A", "M")]) == (
            {"f.txt": "A"}, {"f.txt": "M"},
        )

    def test_rename_badges_only_the_new_path(self):
        # The bare old-path record is dropped by _parse_records, so
        # build_sections only ever sees the status-bearing record.
        assert _parse_records(b"## main\x00R  new.txt\x00old.txt\x00") == (
            "main", [("new.txt", "R", " ")],
        )
        assert build_sections([("new.txt", "R", " ")]) == (
            {"new.txt": "R"}, {},
        )

    def test_unmerged_conflicts_both_sections(self):
        for code in ("UU", "AA", "DU"):
            assert build_sections([("f", code[0], code[1])]) == (
                {"f": "C"}, {"f": "C"},
            )

    def test_copy_collapses_to_rename(self):
        assert build_sections([("new.txt", "C", " ")]) == (
            {"new.txt": "R"}, {},
        )

    def test_staged_only_change(self):
        assert build_sections([("f.txt", "D", " ")]) == (
            {"f.txt": "D"}, {},
        )

    def test_unstaged_only_change(self):
        assert build_sections([("f.txt", " ", "T")]) == (
            {}, {"f.txt": "T"},
        )


# ============================================================================
# _dir_badges — directory aggregation for the explorer
# ============================================================================


class TestDirBadges:
    def test_nested_path_badges_each_parent(self):
        assert _dir_badges({"a/b/c.txt": "M"}) == {"a": "M", "a/b": "M"}

    def test_most_severe_letter_wins(self):
        # M outranks U in the C > D > M > A > R > T > U ordering.
        assert _dir_badges({"a/one.txt": "U", "a/two.txt": "M"}) == {"a": "M"}

    def test_conflict_outranks_everything(self):
        assert _dir_badges({"a/one.txt": "U", "a/two.txt": "C"}) == {"a": "C"}

    def test_deleted_outranks_modified(self):
        assert _dir_badges({"a/x.txt": "M", "a/y.txt": "D"}) == {"a": "D"}

    def test_untracked_is_least_severe(self):
        assert _dir_badges({"a/x.txt": "U", "a/y.txt": "R"}) == {"a": "R"}

    def test_root_file_badges_no_dir(self):
        # a.txt must not badge a directory "a" — split on "/" yields no
        # parent prefixes.
        assert _dir_badges({"a.txt": "M"}) == {}

    def test_sibling_dirs_are_distinct(self):
        assert _dir_badges({"a/x.txt": "D", "b/y.txt": "R"}) == {
            "a": "D",
            "b": "R",
        }


# ============================================================================
# GET /api/git/status — real repositories
# ============================================================================


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed",
)
class TestStatusEndpoint:
    def test_status_clean_repo(self, tmp_path):
        _git_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/status")
        assert resp.status_code == 200
        assert resp.json() == {
            "branch": "main",
            "files": {},
            "dirs": {},
            "sections": {"staged": {}, "unstaged": {}},
        }

    def test_status_reports_changes(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "b.txt").write_text("b")
        _git("add", "b.txt", cwd=repo)
        _git("commit", "-qm", "more", cwd=repo)
        # One change per letter: modified, deleted, renamed, untracked
        # (bare + inside an untracked dir), and staged-added.
        (repo / "a.txt").write_text("a2")
        _git("rm", "-q", "b.txt", cwd=repo)
        _git("mv", "a.txt", "renamed.txt", cwd=repo)
        (repo / "untracked.txt").write_text("u")
        (repo / "newdir").mkdir()
        (repo / "newdir" / "inside.txt").write_text("i")
        (repo / "staged.txt").write_text("s")
        _git("add", "staged.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/status")
        assert resp.status_code == 200
        assert resp.json() == {
            "branch": "main",
            "files": {
                "renamed.txt": "R",
                "b.txt": "D",
                "untracked.txt": "U",
                "newdir/inside.txt": "U",  # -uall: files, not the dir
                "staged.txt": "A",
            },
            # newdir holds the untracked file — the dir gets a badge too.
            "dirs": {"newdir": "U"},
            "sections": {
                "staged": {
                    "renamed.txt": "R",  # the mv was staged; the edit rides along
                    "b.txt": "D",  # git rm stages the deletion
                    "staged.txt": "A",
                },
                "unstaged": {
                    "renamed.txt": "M",
                    "untracked.txt": "U",
                    "newdir/inside.txt": "U",
                },
            },
        }

    def test_status_untracked_only_repo(self, tmp_path):
        repo = _repo(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "x.txt").write_text("x")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/status")
        assert resp.status_code == 200
        assert resp.json() == {
            "branch": "main",
            "files": {"x.txt": "U"},
            "dirs": {},
            "sections": {"staged": {}, "unstaged": {"x.txt": "U"}},
        }

    def test_status_non_repo(self, tmp_path):
        # _repo(tmp_path) without `git init` — the default repo dir.
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/status")
        assert resp.status_code == 200
        assert resp.json() == {
            "branch": None,
            "files": {},
            "dirs": {},
            "sections": {"staged": {}, "unstaged": {}},
        }

    def test_status_payload_keys(self, tmp_path):
        _git_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/status")
        assert set(resp.json()) == {"branch", "files", "dirs", "sections"}
