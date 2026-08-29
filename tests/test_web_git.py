"""Git-status REST API tests — porcelain v1 -z parsing and the snapshot
endpoint against a real git repository.

The parser unit tests pin the git output format (including the rename
record order, which the docs make easy to get backwards); the endpoint
tests exercise a real ``git init`` repo like the one the server serves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import toddler.web.git as web_git
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


def _porcelain(repo: Path) -> str:
    """Short-status output — the ground truth assertions compare against."""
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    return out


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


# ============================================================================
# POST /api/git/hunk — per-hunk stage / unstage / revert
# ============================================================================


def _diff_payload(client, path: str, staged: bool = False) -> dict:
    resp = client.get("/api/git/diff", params={"path": path, "staged": 1 if staged else 0})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed",
)
class TestHunkApplyEndpoint:
    """Per-hunk actions against a real repo: GET a diff, POST its first
    hunk, refetch and assert the hunk is gone (or moved axis)."""

    def _first_hunk(self, client, path: str, staged: bool = False) -> dict:
        payload = _diff_payload(client, path, staged)
        assert payload["hunks"], f"expected hunks for {path}"
        return payload

    def _body(self, payload: dict, action: str) -> dict:
        """A request body built from a diff payload's first hunk."""
        return {
            "path": payload["path"],
            "staged": payload["staged"],
            "action": action,
            "old_path": payload["old_path"],
            "new_path": payload["new_path"],
            "hunk": payload["hunks"][0],
        }

    def test_stage_moves_unstaged_hunk_to_index(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            assert not diff["staged"]
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True}
            # The hunk left the worktree-vs-index diff…
            assert _diff_payload(client, "a.txt")["hunks"] == []
            # …and landed in the index (staged-only M in porcelain).
            assert _porcelain(repo) == "M  a.txt\n"

    def test_unstage_moves_staged_hunk_back_to_worktree(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "unstage"))
            assert resp.status_code == 200, resp.text
            assert _diff_payload(client, "a.txt", staged=True)["hunks"] == []
            # The change is now unstaged in the worktree.
            assert _diff_payload(client, "a.txt")["hunks"]
            assert _porcelain(repo) == " M a.txt\n"

    def test_revert_unstaged_restores_worktree(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"  # back to the commit
            assert _diff_payload(client, "a.txt")["hunks"] == []
            assert _porcelain(repo) == ""

    def test_revert_staged_restores_index_and_worktree(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            # --index: both the index and the worktree are back at HEAD —
            # a clean tree with no residual unstaged change.
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_stage_works_on_mm_file(self, tmp_path):
        # MM — the path is in both sections, so the unstaged tab must stay
        # stageable (axis check, not a file-level "already staged" check).
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        (repo / "a.txt").write_text("a\nb\nc\nd\n")  # unstaged on top
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
            # The unstaged hunk (adding d) moved to the index; the staged
            # diff now carries the full worktree content (del "a" because
            # the committed "a" had no trailing newline).
            assert _diff_payload(client, "a.txt")["hunks"] == []
            staged = _diff_payload(client, "a.txt", staged=True)
            texts = [line["text"] for line in staged["hunks"][0]["lines"]]
            assert texts == ["a", "a", "b", "c", "d"]

    def test_revert_unstaged_deletion_recreates_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").unlink()
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_unstage_staged_deletion_restores_index(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "unstage"))
            assert resp.status_code == 200, resp.text
            assert _diff_payload(client, "a.txt", staged=True)["hunks"] == []
            assert _porcelain(repo) == " D a.txt\n"

    def test_revert_staged_deletion_restores_everywhere(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_rename_content_hunk_applies(self, tmp_path):
        # A staged rename is served as a pure-add diff: the pathspec keeps
        # the old path out of the comparison, so git emits /dev/null for
        # the old side (old_path None).  Unstaging that hunk drops the
        # index entry — the rename is undone, the worktree file survives
        # as untracked.
        repo = _git_repo(tmp_path)
        _git("mv", "a.txt", "renamed.txt", cwd=repo)
        (repo / "renamed.txt").write_text("a\nX\n")
        _git("add", "renamed.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "renamed.txt", staged=True)
            assert diff["old_path"] is None
            resp = client.post("/api/git/hunk", json=self._body(diff, "unstage"))
            assert resp.status_code == 200, resp.text
            # The index entry is gone; the worktree file is untracked and
            # the old path's deletion is staged.
            assert _porcelain(repo) == "D  a.txt\n?? renamed.txt\n"
            # The untracked file still diffs against /dev/null on either
            # axis — the refetch can never be empty for it.
            assert _diff_payload(client, "renamed.txt", staged=True)["hunks"]
            assert _diff_payload(client, "renamed.txt")["hunks"]

    def test_path_with_spaces_and_unicode(self, tmp_path):
        # Untracked files are unstageable — commit first so the hunk
        # apply must C-quote the paths through the patch header.
        repo = _git_repo(tmp_path)
        path = "dir with space/ümlaut.txt"
        p = repo / path
        p.parent.mkdir()
        p.write_text("one\ntwo\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "space", cwd=repo)
        p.write_text("one\nTWO\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, path)
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
            assert _diff_payload(client, path)["hunks"] == []
            assert "M " in _porcelain(repo)

    def test_crlf_revert_restores_bytes(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "false", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        _git("add", "crlf.txt", cwd=repo)
        _git("commit", "-qm", "crlf", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "crlf.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            assert (repo / "crlf.txt").read_bytes() == b"one\r\ntwo\r\nthree\r\n"

    def test_crlf_stage_updates_index(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "false", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        _git("add", "crlf.txt", cwd=repo)
        _git("commit", "-qm", "crlf", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "crlf.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
            assert _diff_payload(client, "crlf.txt")["hunks"] == []
            assert _diff_payload(client, "crlf.txt", staged=True)["hunks"]

    def test_untracked_hunk_rejected(self, tmp_path):
        repo = _repo(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "u.txt").write_text("u\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "u.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 400
            assert "untracked" in resp.json()["error"]

    def test_unmerged_hunk_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("one\ntwo\nthree\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "base", cwd=repo)
        _git("checkout", "-qb", "side", cwd=repo)
        (repo / "a.txt").write_text("one\nSIDE\nthree\n")
        _git("commit", "-qam", "side", cwd=repo)
        _git("checkout", "-q", "main", cwd=repo)
        (repo / "a.txt").write_text("one\nMAIN\nthree\n")
        _git("commit", "-qam", "main", cwd=repo)
        subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True)
        app = _app(tmp_path)
        with TestClient(app) as client:
            body = {
                "path": "a.txt",
                "staged": False,
                "action": "stage",
                "old_path": "a.txt",
                "new_path": "a.txt",
                "hunk": {
                    "old_start": 1, "old_count": 1,
                    "new_start": 1, "new_count": 1,
                    "lines": [{"kind": "ctx", "text": "one"}],
                },
            }
            resp = client.post("/api/git/hunk", json=body)
            assert resp.status_code == 400
            assert "conflicts" in resp.json()["error"]

    def test_stale_hunk_conflicts(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            # The file changes between the diff and the apply.
            (repo / "a.txt").write_text("a\nb\nX\n")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]

    def test_wrong_axis_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")  # unstaged → no unstage
            resp = client.post("/api/git/hunk", json=self._body(diff, "unstage"))
            assert resp.status_code == 400
            _git("add", "a.txt", cwd=repo)
            diff = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 400

    def test_invalid_action_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            body = self._body(diff, "stage")
            body["action"] = "frobnicate"
            resp = client.post("/api/git/hunk", json=body)
            assert resp.status_code == 422

    def test_malformed_hunk_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            body = self._body(diff, "stage")
            body["hunk"]["lines"] = []  # empty — no change to apply
            resp = client.post("/api/git/hunk", json=body)
            assert resp.status_code == 400
            assert "malformed" in resp.json()["error"]
            # Header counts must match the lines (rejects truncated hunks).
            body = self._body(diff, "stage")
            body["hunk"]["old_count"] += 1
            resp = client.post("/api/git/hunk", json=body)
            assert resp.status_code == 400
            assert "malformed" in resp.json()["error"]

    def test_autocrlf_staged_revert(self, tmp_path):
        # core.autocrlf=true stores LF in the index while the worktree is
        # CRLF — the staged diff's raw bytes are what git apply needs, and
        # --index must leave both sides at the committed content.
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "true", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        _git("add", "crlf.txt", cwd=repo)
        _git("commit", "-qm", "crlf", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\nTWO\r\n")
        _git("add", "crlf.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "crlf.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == ""

    def test_stale_zero_context_hunk_conflicts(self, tmp_path):
        # A full rewrite has no context lines — git apply's fuzzy matching
        # would apply it by position, silently overwriting newer content.
        # The server must reject it because the hunk no longer matches.
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("one\ntwo\nthree\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "base", cwd=repo)
        (repo / "a.txt").write_text("new content\n")  # full rewrite
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            # Zero-context: the rewrite hunk carries no ctx lines.
            kinds = [ln["kind"] for h in diff["hunks"] for ln in h["lines"]]
            assert "ctx" not in kinds
            # The file is rewritten again before the apply — the echoed
            # hunk now matches nothing.
            (repo / "a.txt").write_text("other rewrite\n")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]
            # The rewrite must not have been clobbered.
            assert (repo / "a.txt").read_text() == "other rewrite\n"

    def test_forged_side_names_conflict(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            body = self._body(diff, "revert")
            body["old_path"] = "other.txt"  # forged patch headers
            body["new_path"] = "other.txt"
            resp = client.post("/api/git/hunk", json=body)
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]
            # Nothing was reverted.
            assert (repo / "a.txt").read_text() == "a\nb\nc\n"

    def test_crlf_no_final_newline_roundtrip(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "false", cwd=repo)
        (repo / "a.txt").write_bytes(b"one\r\ntwo\r")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "crlf", cwd=repo)
        (repo / "a.txt").write_bytes(b"one\r\nTWO\r")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            # The final line has no newline — its \r must survive the
            # round-trip too (this is the byte the old rebuild dropped).
            assert diff["hunks"][0]["lines"][-1]["no_newline"] is True
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
            staged = self._first_hunk(client, "a.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(staged, "revert"))
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_bytes() == b"one\r\ntwo\r"

    def test_unstage_crlf_deletion_preserves_index_bytes(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "false", cwd=repo)
        (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        _git("add", "crlf.txt", cwd=repo)
        _git("commit", "-qm", "crlf", cwd=repo)
        _git("rm", "-q", "crlf.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "crlf.txt", staged=True)
            resp = client.post("/api/git/hunk", json=self._body(diff, "unstage"))
            assert resp.status_code == 200, resp.text
        # The deletion is unstaged; the index entry is back — with the
        # exact CRLF bytes, not LF-ified ones.
        out = subprocess.run(
            ["git", "show", ":crlf.txt"], cwd=repo, capture_output=True, check=True,
        ).stdout
        assert out == b"one\r\ntwo\r\n"

    def test_mixed_eol_full_rewrite_stage(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("config", "core.autocrlf", "false", cwd=repo)
        (repo / "a.txt").write_bytes(b"one\r\ntwo\nthree\r\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "mixed", cwd=repo)
        (repo / "a.txt").write_bytes(b"ONE\r\nTWO\nTHREE\r\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "stage"))
            assert resp.status_code == 200, resp.text
        # The index must hold the worktree bytes verbatim.
        out = subprocess.run(
            ["git", "show", ":a.txt"], cwd=repo, capture_output=True, check=True,
        ).stdout
        assert out == b"ONE\r\nTWO\nTHREE\r\n"

    def test_apply_failure_while_still_matching_is_500(self, tmp_path, monkeypatch):
        real_git = web_git._git

        async def failing_git(*args, cwd, stdin=None):
            if args[0] == "apply":
                return b"", b"patch does not apply", 1
            return await real_git(*args, cwd=cwd, stdin=stdin)

        monkeypatch.setattr(web_git, "_git", failing_git)
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            # The hunk still matches the fresh diff — this is a real apply
            # failure, reported as a 500 with git's stderr.
            assert resp.status_code == 500
            assert "patch does not apply" in resp.json()["error"]

    def test_apply_failure_after_stale_change_is_409(self, tmp_path, monkeypatch):
        real_git = web_git._git
        changed = False

        async def failing_git(*args, cwd, stdin=None):
            nonlocal changed
            if args[0] == "apply":
                if not changed:
                    # Mutate the file on the first apply attempt, then
                    # fail — the re-verify must see the new content.
                    (Path(cwd) / "a.txt").write_text("a\nb\nX\n")
                    changed = True
                return b"", b"patch does not apply", 1
            return await real_git(*args, cwd=cwd, stdin=stdin)

        monkeypatch.setattr(web_git, "_git", failing_git)
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            diff = self._first_hunk(client, "a.txt")
            resp = client.post("/api/git/hunk", json=self._body(diff, "revert"))
            # The re-verify found the file changed — staleness, a 409.
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]


# ============================================================================
# Whole-file stage / unstage / discard (source-control row buttons)
# ============================================================================


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed",
)
class TestFileActionEndpoint:
    """Row-level file actions against a real repo: stage, unstage, discard."""

    def _post(self, client, path: str, action: str):
        return client.post("/api/git/file", json={"path": path, "action": action})

    def test_stage_untracked_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "u.txt").write_text("u\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "u.txt", "stage")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True}
            assert _porcelain(repo) == "A  u.txt\n"

    def test_stage_modified_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "stage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == "M  a.txt\n"

    def test_stage_deleted_tracked_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").unlink()
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "stage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == "D  a.txt\n"

    def test_unstage_modified_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "unstage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == " M a.txt\n"

    def test_unstage_staged_add(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "f.txt").write_text("f\n")
        _git("add", "f.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "f.txt", "unstage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == "?? f.txt\n"

    def test_unstage_staged_deletion(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "unstage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == " D a.txt\n"

    def test_unstage_staged_rename(self, tmp_path):
        # Same end state as the hunk-level unstage: the index entry for
        # the new path is dropped — the worktree file is untracked and
        # the old path's staged deletion remains.
        repo = _git_repo(tmp_path)
        _git("mv", "a.txt", "renamed.txt", cwd=repo)
        (repo / "renamed.txt").write_text("a\nX\n")
        _git("add", "renamed.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "renamed.txt", "unstage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == "D  a.txt\n?? renamed.txt\n"

    def test_discard_modified_restores_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_discard_deleted_recreates_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").unlink()
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_discard_untracked_deletes_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "u.txt").write_text("u\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "u.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert not (repo / "u.txt").exists()
            assert _porcelain(repo) == ""

    def test_discard_untracked_in_untracked_dir(self, tmp_path):
        repo = _git_repo(tmp_path)
        d = repo / "newdir"
        d.mkdir()
        (d / "inside.txt").write_text("x\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "newdir/inside.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert not (d / "inside.txt").exists()
            assert _porcelain(repo) == ""

    def test_discard_untracked_directory(self, tmp_path):
        # A raw untracked-dir path (never sent by the UI, which lists
        # files) is removed wholesale.
        repo = _git_repo(tmp_path)
        d = repo / "newdir"
        d.mkdir()
        (d / "inside.txt").write_text("x\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "newdir", "discard")
            assert resp.status_code == 200, resp.text
            assert not d.exists()
            assert _porcelain(repo) == ""

    def test_discard_untracked_symlink_removes_link_not_target(self, tmp_path):
        # The unlink branch must act on the literal path git listed — a
        # symlink's resolved target is a different (possibly tracked) file.
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        os.symlink("a.txt", repo / "link.txt")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "link.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert not (repo / "link.txt").exists()  # the link itself gone
            assert (repo / "a.txt").read_text() == "a\nb\nc\n"  # target intact
            assert _porcelain(repo) == " M a.txt\n"

    def test_discard_untracked_broken_symlink(self, tmp_path):
        # A dangling link fails Path.exists() (a following check) but must
        # still be discardable — the entry itself exists.
        repo = _git_repo(tmp_path)
        os.symlink("missing-target", repo / "broken")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "broken", "discard")
            assert resp.status_code == 200, resp.text
            assert not (repo / "broken").exists()
            assert _porcelain(repo) == ""

    def test_discard_glob_filename_only_touches_that_file(self, tmp_path):
        # Glob metacharacters in a filename must not widen the destructive
        # restore onto sibling files (a[1].txt is a char class to git).
        repo = _git_repo(tmp_path)
        (repo / "a1.txt").write_text("one\n")
        (repo / "a[1].txt").write_text("two\n")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "add both", cwd=repo)
        (repo / "a1.txt").write_text("one modified\n")
        (repo / "a[1].txt").write_text("two modified\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a[1].txt", "discard")
            assert resp.status_code == 200, resp.text
            assert (repo / "a[1].txt").read_text() == "two\n"
            assert (repo / "a1.txt").read_text() == "one modified\n"
            assert _porcelain(repo) == " M a1.txt\n"

    def test_stage_glob_filename_only_touches_that_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a1.txt").write_text("one\n")
        (repo / "a[1].txt").write_text("two\n")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "add both", cwd=repo)
        (repo / "a1.txt").write_text("one modified\n")
        (repo / "a[1].txt").write_text("two modified\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a[1].txt", "stage")
            assert resp.status_code == 200, resp.text
            porcelain = _porcelain(repo)
            assert "M  a[1].txt\n" in porcelain
            assert " M a1.txt\n" in porcelain

    def test_discard_mm_reverts_both_axes(self, tmp_path):
        # A staged-and-modified file must revert index AND worktree —
        # restoring only the worktree would leave the staged half behind.
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        (repo / "a.txt").write_text("a\nX\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_discard_staged_deletion_recreates_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert (repo / "a.txt").read_text() == "a"
            assert _porcelain(repo) == ""

    def test_discard_staged_deletion_recreated_untracked(self, tmp_path):
        # `git rm` + recreate emits both `D  a.txt` and `?? a.txt` — the
        # worktree copy has no index entry, so discard must delete it
        # (previously a permanent 409 the refresh could never fix).
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        (repo / "a.txt").write_text("new content\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert not (repo / "a.txt").exists()
            assert _porcelain(repo) == "D  a.txt\n"

    def test_unstage_unborn_branch(self, tmp_path):
        # No HEAD exists yet — restore --staged dies, plain reset works.
        repo = _repo(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f.txt").write_text("f\n")
        _git("add", "f.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "f.txt", "unstage")
            assert resp.status_code == 200, resp.text
            assert _porcelain(repo) == "?? f.txt\n"

    def test_discard_unborn_branch(self, tmp_path):
        # Discarding a staged file with no HEAD = drop the index entry
        # and delete the worktree copy (rm -f).
        repo = _repo(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f.txt").write_text("f\n")
        _git("add", "f.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "f.txt", "discard")
            assert resp.status_code == 200, resp.text
            assert not (repo / "f.txt").exists()
            assert _porcelain(repo) == ""

    def test_untracked_unstage_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "u.txt").write_text("u\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "u.txt", "unstage")
            assert resp.status_code == 400
            assert "untracked" in resp.json()["error"]

    def test_clean_file_is_stale(self, tmp_path):
        _git_repo(tmp_path)  # the repo _app() serves must be a git repo
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "a.txt", "stage")
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]

    def test_missing_path_is_stale(self, tmp_path):
        _git_repo(tmp_path)  # the repo _app() serves must be a git repo
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "nope.txt", "stage")
            assert resp.status_code == 409
            assert "refresh" in resp.json()["error"]

    def test_path_escape_rejected(self, tmp_path):
        _git_repo(tmp_path)  # the repo _app() serves must be a git repo
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "../outside.txt", "stage")
            assert resp.status_code == 400
            assert "escapes" in resp.json()["error"]

    def test_invalid_action_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                "/api/git/file", json={"path": "a.txt", "action": "frobnicate"},
            )
            assert resp.status_code == 422

    def test_unmerged_all_actions_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("one\ntwo\nthree\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "base", cwd=repo)
        _git("checkout", "-qb", "side", cwd=repo)
        (repo / "a.txt").write_text("one\nSIDE\nthree\n")
        _git("commit", "-qam", "side", cwd=repo)
        _git("checkout", "-q", "main", cwd=repo)
        (repo / "a.txt").write_text("one\nMAIN\nthree\n")
        _git("commit", "-qam", "main", cwd=repo)
        subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True)
        app = _app(tmp_path)
        with TestClient(app) as client:
            for action in ("stage", "unstage", "discard"):
                resp = self._post(client, "a.txt", action)
                assert resp.status_code == 400
                assert "conflicts" in resp.json()["error"]

    def test_submodule_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo",
             f"160000,{head},sub"],
            cwd=repo, check=True,
        )
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "sub", "stage")
            assert resp.status_code == 400
            assert "submodule" in resp.json()["error"]


# ============================================================================
# Commit endpoint
# ============================================================================


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed",
)
class TestCommitEndpoint:
    """POST /api/git/commit — messages via stdin, git as the authority."""

    def _post(self, client, message: str):
        return client.post("/api/git/commit", json={"message": message})

    def _log(self, repo: Path) -> str:
        # %B keeps the message body's trailing newline and git log adds
        # another — strip the outer one for a stable comparison.
        return subprocess.run(
            ["git", "log", "-1", "--format=%B"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.rstrip("\n")

    def test_commit_creates_commit(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "feat: x")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True}
            assert self._log(repo) == "feat: x"
            assert _porcelain(repo) == ""

    def test_commit_multiline_message(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "subject\n\nbody line")
            assert resp.status_code == 200, resp.text
            assert self._log(repo) == "subject\n\nbody line"

    def test_commit_empty_message_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "")
            assert resp.status_code == 400
            assert "empty" in resp.json()["error"]

    def test_commit_whitespace_message_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "   \n  ")
            assert resp.status_code == 400
            assert "empty" in resp.json()["error"]

    def test_commit_nothing_staged(self, tmp_path):
        repo = _git_repo(tmp_path)
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "feat: x")
            assert resp.status_code == 400
            assert "nothing to commit" in resp.json()["error"]
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        assert after == before

    def test_commit_unstaged_changes_only_is_400(self, tmp_path):
        # Clean index, dirty worktree — git says "no changes added to
        # commit", which must surface as the friendly 400, not a 500.
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "feat: x")
            assert resp.status_code == 400
            assert "nothing to commit" in resp.json()["error"]

    def test_commit_message_kept_verbatim(self, tmp_path):
        # --cleanup=verbatim: trailing spaces and runs of blank lines in
        # the typed message must survive, not be stripped/collapsed.
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            message = "subject  \n\nbody\n\n\nend"
            resp = self._post(client, message)
            assert resp.status_code == 200, resp.text
            assert self._log(repo) == message

    def test_commit_unmerged_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("one\ntwo\nthree\n")
        _git("add", "a.txt", cwd=repo)
        _git("commit", "-qm", "base", cwd=repo)
        _git("checkout", "-qb", "side", cwd=repo)
        (repo / "a.txt").write_text("one\nSIDE\nthree\n")
        _git("commit", "-qam", "side", cwd=repo)
        _git("checkout", "-q", "main", cwd=repo)
        (repo / "a.txt").write_text("one\nMAIN\nthree\n")
        _git("commit", "-qam", "main", cwd=repo)
        subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "feat: x")
            assert resp.status_code == 400
            assert "conflicts" in resp.json()["error"]

    def test_commit_hook_failure_is_500(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nb\nc\n")
        _git("add", "a.txt", cwd=repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "feat: x")
            assert resp.status_code == 500
            assert "git commit failed" in resp.json()["error"]

    def test_commit_unborn_branch(self, tmp_path):
        repo = _repo(tmp_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a\n")
        _git("add", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = self._post(client, "root commit")
            assert resp.status_code == 200, resp.text
            assert self._log(repo) == "root commit"

    def test_commit_missing_field_rejected(self, tmp_path):
        _git_repo(tmp_path)  # the repo _app() serves must be a git repo
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.post("/api/git/commit", json={})
            assert resp.status_code == 422
