"""REST API tests — sessions, message replay after a WS turn, the file
tree, and path-safe file read/write.

The repo root is a subdirectory of the tmp session dir so the session
database and logs never pollute the tree under test.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.mocks import make_mock_llm, text_response
from toddler.config.settings import Settings
from toddler.web.app import create_app

# ============================================================================
# Helpers
# ============================================================================


def _repo(tmp_path) -> Path:
    """The repo root used as ``repo_root`` — created on demand."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def _app(tmp_path, llm=None):
    """An app with a tmp session dir and the repo root inside it."""
    settings = Settings(session_dir=tmp_path)
    return create_app(settings, repo_root=_repo(tmp_path), llm=llm)


# ============================================================================
# Sessions
# ============================================================================


class TestSessions:
    def test_list_sessions_returns_summary_rows(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/sessions")
            assert resp.status_code == 200
            sessions = resp.json()["sessions"]
        # Startup resolves a fresh session (CLI parity).
        assert len(sessions) == 1
        assert set(sessions[0]) == {
            "id", "title", "created_at", "updated_at", "message_count",
        }
        assert sessions[0]["message_count"] == 0

    def test_create_session_adds_row(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                "/api/sessions", json={"title": "My session"},
            )
            assert resp.status_code == 201
            row = resp.json()
            assert row["title"] == "My session"
            assert row["message_count"] == 0

            ids = [s["id"] for s in client.get("/api/sessions").json()["sessions"]]
            assert row["id"] in ids

    def test_create_session_without_body_gets_fallback_title(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.post("/api/sessions")
        assert resp.status_code == 201
        # Fallback title comes from the repo root's basename.
        assert resp.json()["title"] == _repo(tmp_path).name


# ============================================================================
# Message replay after a WS-driven turn
# ============================================================================


class TestMessages:
    def test_messages_replay_after_ws_turn(self, tmp_path):
        llm = make_mock_llm(text_response("Hello there."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                session_id = hello["session"]["id"]
                conversation_id = hello["conversation"]["id"]
                ws.send_json({"cmd": "turn", "input": "hi"})
                # Drain until the turn fully completes (persisted by then).
                while True:
                    frame = ws.receive_json()
                    if frame["type"] == "state" and frame["busy"] is False:
                        break

            resp = client.get(
                f"/api/sessions/{session_id}/messages",
                params={"conversation_id": conversation_id},
            )
            assert resp.status_code == 200
            messages = resp.json()["messages"]
            # The persisted system prompt is scaffolding, not transcript —
            # it is filtered out of the replay.
            assert [(m["role"], m["content"]) for m in messages] == [
                ("user", "hi"),
                ("assistant", "Hello there."),
            ]

    def test_messages_unknown_session_returns_404(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/sessions/does-not-exist/messages")
        assert resp.status_code == 404


# ============================================================================
# File read / write
# ============================================================================


class TestFileEndpoints:
    def test_write_then_read_round_trip(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file",
                params={"path": "hello.txt"},
                json={"content": "hi"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "bytes": 2}
            # Written through pathlib directly — visible on disk.
            assert (_repo(tmp_path) / "hello.txt").read_text() == "hi"

            resp = client.get("/api/file", params={"path": "hello.txt"})
            assert resp.status_code == 200
            assert resp.json() == {
                "path": "hello.txt",
                "content": "hi",
                "total_lines": 1,
            }

    def test_write_creates_parent_directories(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file",
                params={"path": "src/new.txt"},
                json={"content": "nested"},
            )
        assert resp.status_code == 200
        assert (_repo(tmp_path) / "src" / "new.txt").read_text() == "nested"

    def test_read_missing_file_returns_404(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "nope.txt"})
        assert resp.status_code == 404

    def test_read_binary_file_returns_400(self, tmp_path):
        (_repo(tmp_path) / "blob.bin").write_bytes(b"\x00\x01\x02")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "blob.bin"})
        assert resp.status_code == 400
        assert "binary" in resp.json()["error"]

    def test_read_non_utf8_file_returns_400(self, tmp_path):
        (_repo(tmp_path) / "latin.txt").write_bytes(b"\xff\xfe\xfa")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "latin.txt"})
        assert resp.status_code == 400

    def test_put_without_content_is_rejected(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file", params={"path": "x.txt"}, json={},
            )
        assert resp.status_code == 400
        assert "content" in resp.json()["error"]

    def test_put_with_form_encoded_body(self, tmp_path):
        # curl -d '{"content":"hi"}' sends the body without a JSON
        # content type — the endpoint must accept it regardless.
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file",
                params={"path": "hello.txt"},
                content='{"content": "hi"}',
            )
        assert resp.status_code == 200
        assert (_repo(tmp_path) / "hello.txt").read_text() == "hi"


# ============================================================================
# Path safety
# ============================================================================


class TestPathEscape:
    def test_read_escape_returns_400(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get(
                "/api/file", params={"path": "../outside.txt"},
            )
        assert resp.status_code == 400
        assert "escapes" in resp.json()["error"]

    def test_write_escape_is_rejected_and_writes_nothing(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file",
                params={"path": "../outside.txt"},
                json={"content": "sneaky"},
            )
        assert resp.status_code == 400
        assert not (tmp_path / "outside.txt").exists()

    def test_absolute_path_rejected(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "/etc/passwd"})
        assert resp.status_code == 400

    def test_symlink_escape_rejected(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (_repo(tmp_path) / "link.txt").symlink_to(outside)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "link.txt"})
        assert resp.status_code == 400


# ============================================================================
# File tree
# ============================================================================


class TestTree:
    def _populated_repo(self, tmp_path) -> Path:
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        (repo / "sub").mkdir()
        (repo / "sub" / "b.py").write_text("b")
        (repo / ".gitignore").write_text("*.tmp\n")
        (repo / "sub" / "junk.tmp").write_text("junk")
        (repo / "node_modules" / "pkg").mkdir(parents=True)
        (repo / "node_modules" / "pkg" / "index.js").write_text("x")
        return repo

    def test_tree_lists_entries_with_types(self, tmp_path):
        repo = self._populated_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["root"] == str(repo.resolve())
        entries = {(e["path"], e["type"]) for e in data["entries"]}
        assert ("a.txt", "file") in entries
        assert ("sub", "dir") in entries
        assert ("sub/b.py", "file") in entries
        # Gitignore matches and IGNORED_TOP names are pruned.
        assert ("node_modules", "dir") not in entries
        assert ("sub/junk.tmp", "file") not in entries

    def test_tree_depth_limits_recursion(self, tmp_path):
        self._populated_repo(tmp_path)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/tree", params={"depth": 1})
        assert resp.status_code == 200
        entries = {(e["path"], e["type"]) for e in resp.json()["entries"]}
        assert ("a.txt", "file") in entries
        assert ("sub", "dir") in entries
        assert ("sub/b.py", "file") not in entries
