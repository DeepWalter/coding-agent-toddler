"""Git diff REST API tests — the unified-diff parser and the endpoint
against a real git repository.

The parser unit tests pin the unified format git actually emits (hunk
headers, /dev/null markers, binary files, no-newline markers, CRLF
lines); the endpoint tests exercise a real ``git init`` repo and cover
staged vs unstaged content, untracked and deleted files, and path
safety.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toddler.config.settings import Settings
from toddler.web.app import create_app
from toddler.web.diffparse import DiffLine, parse_diff

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
    (repo / "a.txt").write_text("a\n")
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
# parse_diff — pins the unified format git emits
# ============================================================================


class TestParseDiff:
    def test_standard_modify_hunk(self):
        out = (
            b"diff --git a/a.txt b/a.txt\n"
            b"index 1b2b3..4c5d6 100644\n"
            b"--- a/a.txt\n"
            b"+++ b/a.txt\n"
            b"@@ -1,4 +1,5 @@\n"
            b" line1\n"
            b"-line2\n"
            b"+line2 changed\n"
            b"+line2b\n"
            b" line3\n"
        )
        parsed = parse_diff(out)
        assert parsed["binary"] is False
        assert parsed["truncated"] is False
        assert parsed["old_dev_null"] is False
        assert parsed["new_dev_null"] is False
        (hunk,) = parsed["hunks"]
        assert hunk.old_start == 1
        assert hunk.old_count == 4
        assert hunk.new_start == 1
        assert hunk.new_count == 5
        assert hunk.lines == [
            DiffLine("ctx", 1, 1, "line1"),
            DiffLine("del", 2, None, "line2"),
            DiffLine("add", None, 2, "line2 changed"),
            DiffLine("add", None, 3, "line2b"),
            DiffLine("ctx", 3, 4, "line3"),
        ]

    def test_new_file(self):
        out = (
            b"diff --git a/new.txt b/new.txt\n"
            b"new file mode 100644\n"
            b"index 0000000..1234567\n"
            b"--- /dev/null\n"
            b"+++ b/new.txt\n"
            b"@@ -0,0 +1,3 @@\n"
            b"+a\n"
            b"+b\n"
            b"+c\n"
        )
        parsed = parse_diff(out)
        assert parsed["old_dev_null"] is True
        assert parsed["new_dev_null"] is False
        (hunk,) = parsed["hunks"]
        assert hunk.old_start == 0
        assert hunk.old_count == 0
        assert hunk.new_start == 1
        assert hunk.new_count == 3
        assert [ln.kind for ln in hunk.lines] == ["add", "add", "add"]
        assert all(ln.old_ln is None for ln in hunk.lines)
        assert [ln.new_ln for ln in hunk.lines] == [1, 2, 3]

    def test_deleted_file(self):
        out = (
            b"diff --git a/gone.txt b/gone.txt\n"
            b"deleted file mode 100644\n"
            b"index 1234567..0000000\n"
            b"--- a/gone.txt\n"
            b"+++ /dev/null\n"
            b"@@ -1,2 +0,0 @@\n"
            b"-x\n"
            b"-y\n"
        )
        parsed = parse_diff(out)
        assert parsed["new_dev_null"] is True
        (hunk,) = parsed["hunks"]
        assert hunk.new_start == 0
        assert hunk.new_count == 0
        assert [ln.kind for ln in hunk.lines] == ["del", "del"]
        assert all(ln.new_ln is None for ln in hunk.lines)
        assert [ln.old_ln for ln in hunk.lines] == [1, 2]

    def test_binary(self):
        out = b"diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n"
        parsed = parse_diff(out)
        assert parsed["binary"] is True
        assert parsed["hunks"] == []

    def test_no_newline_markers(self):
        out = (
            b"@@ -1,2 +1,2 @@\n"
            b"-a\n"
            b"-b\n"
            b"\\ No newline at end of file\n"
            b"+a\n"
            b"+b2\n"
            b"\\ No newline at end of file\n"
        )
        (hunk,) = parse_diff(out)["hunks"]
        assert len(hunk.lines) == 4  # markers are not lines
        assert hunk.lines[1].no_newline is True  # old-side b
        assert hunk.lines[2].no_newline is False
        assert hunk.lines[3].no_newline is True  # new-side b2

    def test_rename_headers_skipped(self):
        out = (
            b"diff --git a/old.txt b/new.txt\n"
            b"similarity index 92%\n"
            b"rename from old.txt\n"
            b"rename to new.txt\n"
            b"index 1234567..7654321 100644\n"
            b"--- a/old.txt\n"
            b"+++ b/new.txt\n"
            b"@@ -1,1 +1,1 @@\n"
            b"-old\n"
            b"+new\n"
        )
        (hunk,) = parse_diff(out)["hunks"]
        assert [ln.kind for ln in hunk.lines] == ["del", "add"]

    def test_two_hunks(self):
        out = (
            b"@@ -1,2 +1,2 @@\n"
            b" a\n"
            b"-b\n"
            b"+b2\n"
            b"@@ -10,2 +10,2 @@\n"
            b" c\n"
            b"-d\n"
            b"+d2\n"
        )
        h1, h2 = parse_diff(out)["hunks"]
        assert h1.old_start == 1 and h1.new_start == 1
        assert h2.old_start == 10 and h2.new_start == 10
        assert [ln.new_ln for ln in h2.lines] == [10, None, 11]

    def test_crlf_lines(self):
        out = b"@@ -1,1 +1,1 @@\n-a\r\n+b\r\n"
        (hunk,) = parse_diff(out)["hunks"]
        assert hunk.lines[0].text == "a"  # trailing \r stripped
        assert hunk.lines[1].text == "b"

    def test_truncated(self):
        out = (
            b"@@ -1,2 +1,2 @@\n"
            b" a\n"
            b"-b\n"
            b"+b2\n"
            b" c\n"
        )
        parsed = parse_diff(out, max_lines=2)
        assert parsed["truncated"] is True
        assert [ln.kind for ln in parsed["hunks"][0].lines] == ["ctx", "del"]

    def test_header_only_no_hunks(self):
        out = b"diff --git a/x.sh b/x.sh\nold mode 100644\nnew mode 100755\n"
        parsed = parse_diff(out)
        assert parsed["hunks"] == []
        assert parsed["binary"] is False

    def test_empty_output(self):
        parsed = parse_diff(b"")
        assert parsed == {
            "hunks": [],
            "binary": False,
            "truncated": False,
            "old_dev_null": False,
            "new_dev_null": False,
        }

    def test_single_line_hunk_without_counts(self):
        out = b"@@ -1 +1 @@\n-a\n+a\n"
        (hunk,) = parse_diff(out)["hunks"]
        assert (hunk.old_start, hunk.old_count) == (1, 1)
        assert (hunk.new_start, hunk.new_count) == (1, 1)


# ============================================================================
# GET /api/git/diff — real repositories
# ============================================================================


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed",
)
class TestDiffEndpoint:
    def test_unstaged_modify(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("a\nline2 changed\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "a.txt"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["path"] == "a.txt"
        assert payload["staged"] is False
        assert payload["binary"] is False
        assert payload["truncated"] is False
        assert payload["old_path"] == "a.txt"
        assert payload["new_path"] == "a.txt"
        # The unchanged "a" line is context; the new line is an addition.
        assert payload["hunks"][0]["lines"][0] == {
            "kind": "ctx",
            "old_ln": 1,
            "new_ln": 1,
            "text": "a",
            "no_newline": False,
        }
        assert payload["hunks"][0]["lines"][1] == {
            "kind": "add",
            "old_ln": None,
            "new_ln": 2,
            "text": "line2 changed",
            "no_newline": False,
        }

    def test_staged_differs_from_unstaged(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "a.txt").write_text("staged change\n")
        _git("add", "a.txt", cwd=repo)
        (repo / "a.txt").write_text("staged change\nunstaged change\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            staged = client.get(
                "/api/git/diff", params={"path": "a.txt", "staged": 1},
            ).json()
            unstaged = client.get(
                "/api/git/diff", params={"path": "a.txt", "staged": 0},
            ).json()
        assert staged["staged"] is True
        assert unstaged["staged"] is False
        staged_adds = [
            ln["text"] for h in staged["hunks"]
            for ln in h["lines"] if ln["kind"] == "add"
        ]
        unstaged_adds = [
            ln["text"] for h in unstaged["hunks"]
            for ln in h["lines"] if ln["kind"] == "add"
        ]
        assert "staged change" in staged_adds
        assert "unstaged change" not in staged_adds
        assert "unstaged change" in unstaged_adds

    def test_untracked_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "u.txt").write_text("hello\nworld\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "u.txt"})
        assert resp.status_code == 200  # git's rc 1 is not an error here
        payload = resp.json()
        assert payload["old_path"] is None
        assert payload["new_path"] == "u.txt"
        lines = [ln for h in payload["hunks"] for ln in h["lines"]]
        assert [ln["kind"] for ln in lines] == ["add", "add"]
        assert all(ln["old_ln"] is None for ln in lines)

    def test_staged_delete(self, tmp_path):
        repo = _git_repo(tmp_path)
        _git("rm", "-q", "a.txt", cwd=repo)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get(
                "/api/git/diff", params={"path": "a.txt", "staged": 1},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["new_path"] is None
        lines = [ln for h in payload["hunks"] for ln in h["lines"]]
        assert [ln["kind"] for ln in lines] == ["del"]
        assert all(ln["new_ln"] is None for ln in lines)

    def test_binary_file(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "b.bin").write_bytes(b"\x00\x01\x02")
        _git("add", "b.bin", cwd=repo)
        _git("commit", "-qm", "bin", cwd=repo)
        (repo / "b.bin").write_bytes(b"\x00\x01\x03")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "b.bin"})
        assert resp.status_code == 200
        assert resp.json()["binary"] is True
        assert resp.json()["hunks"] == []

    def test_no_changes_is_empty(self, tmp_path):
        _git_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "a.txt"})
        assert resp.status_code == 200
        assert resp.json()["hunks"] == []

    def test_path_with_spaces_and_unicode(self, tmp_path):
        repo = _git_repo(tmp_path)
        name = "dir with space/ümlaut.txt"
        (repo / "dir with space").mkdir()
        (repo / name).write_text("before\n")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "u", cwd=repo)
        (repo / name).write_text("after\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": name})
        assert resp.status_code == 200
        texts = [ln["text"] for h in resp.json()["hunks"] for ln in h["lines"]]
        assert texts == ["before", "after"]

    def test_path_escape_rejected(self, tmp_path):
        _git_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "../x"})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_directory_rejected(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "sub").mkdir()
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "sub"})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_missing_untracked_file_404(self, tmp_path):
        _git_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "nope.txt"})
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_non_repo_500(self, tmp_path):
        # _repo(tmp_path) without `git init` — the default repo dir.
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/git/diff", params={"path": "a.txt"})
        assert resp.status_code == 500
        assert "error" in resp.json()
