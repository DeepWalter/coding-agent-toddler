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
from toddler.web.files import tool_path, tool_path_abs, tool_path_named

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
    """Reads resolve anywhere — the editor opens external files read-only —
    while writes stay contained inside the root."""

    def test_read_escape_returns_the_outside_file(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get(
                "/api/file", params={"path": "../outside.txt"},
            )
        assert resp.status_code == 200
        assert resp.json()["content"] == "secret"
        # The echo is the canonical name: absolute, outside the root.
        assert resp.json()["path"] == str(outside.resolve())

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

    def test_reads_an_absolute_path(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": str(outside)})
        assert resp.status_code == 200
        assert resp.json()["content"] == "secret"
        assert resp.json()["path"] == str(outside.resolve())

    def test_read_follows_a_symlink_out_of_the_root(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (_repo(tmp_path) / "link.txt").symlink_to(outside)
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/file", params={"path": "link.txt"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "secret"
        assert resp.json()["path"] == str(outside.resolve())

    def test_write_to_an_absolute_path_is_rejected(self, tmp_path):
        """Only the read side was widened — a PUT still cannot leave the
        root, whichever way the path spells it."""
        target = tmp_path / "outside.txt"
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.put(
                "/api/file",
                params={"path": str(target)},
                json={"content": "sneaky"},
            )
        assert resp.status_code == 400
        assert not target.exists()

    def test_blank_and_nul_paths_are_rejected(self, tmp_path):
        app = _app(tmp_path)
        with TestClient(app) as client:
            assert client.get("/api/file", params={"path": ""}).status_code == 400
            assert client.get("/api/file", params={"path": "\x00"}).status_code == 400

    def test_expanded_home_reads(self, tmp_path, monkeypatch):
        """``~`` expands server-side, the one spelling the browser could
        never resolve for itself."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        notes = tmp_path / "home/.toddler/notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("outside the repo\n")
        app = _app(tmp_path)
        with TestClient(app) as client:
            resp = client.get(
                "/api/file", params={"path": "~/.toddler/notes.md"},
            )
        assert resp.status_code == 200
        assert resp.json()["content"] == "outside the repo\n"
        assert resp.json()["path"] == str(notes.resolve())


# ============================================================================
# Tool-call paths
# ============================================================================


class TestToolPath:
    """A path a tool call named, as the card needs it: root-relative, or
    nothing at all.  Unlike a request, it is not an error to be outside —
    there is just no tab here for it to land in."""

    def test_shape_every_tool_call_writes(self, tmp_path):
        """The tools' schema asks for an absolute path and the model
        usually complies; relative ones resolve against the agent's cwd,
        which is the served root."""
        root = _repo(tmp_path)
        absolute = str(root / "src" / "a.py")
        assert tool_path(root, absolute) == "src/a.py"
        assert tool_path(root, "src/a.py") == "src/a.py"
        assert tool_path(root, "./src/../src/a.py") == "src/a.py"
        assert tool_path_abs(root, "src/a.py") == str(root / "src" / "a.py")

    def test_expanded_home_reaches_the_root(self, tmp_path, monkeypatch):
        """``~`` is the shape the browser could never resolve — expanding
        it needs the home directory, which only the server has."""
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _repo(tmp_path)
        assert tool_path(root, "~/repo/src/a.py") == "src/a.py"
        assert tool_path_abs(root, "~/repo/src/a.py") == str(tmp_path / "repo/src/a.py")

    def test_path_outside_the_root_has_a_location_but_no_tab(self, tmp_path):
        """A call that reached outside has nowhere here to open — but it
        still resolved somewhere, and that is what the card hovers.  The
        answer is the resolved one, symlinks and all: the file the call
        touched is the file the tools resolved it to."""
        root = _repo(tmp_path)
        assert tool_path(root, "/etc/passwd") is None
        assert tool_path_abs(root, "/etc/passwd") == str(Path("/etc/passwd").resolve())
        assert tool_path(root, "../outside.txt") is None
        assert tool_path_abs(root, "../outside.txt") == str(tmp_path / "outside.txt")

    def test_a_tilde_path_outside_expands_all_the_same(self, tmp_path, monkeypatch):
        """The whole point of resolving here: the browser is told where a
        file it cannot open actually lives."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        root = _repo(tmp_path)
        assert tool_path(root, "~/.toddler/notes.md") is None
        assert tool_path_abs(root, "~/.toddler/notes.md") == (
            str(tmp_path / "home/.toddler/notes.md")
        )

    def test_nothing_named_is_no_path(self, tmp_path):
        """A call may carry no ``file_path`` at all, or a blank one."""
        root = _repo(tmp_path)
        assert tool_path(root, None) is None
        assert tool_path(root, "") is None
        assert tool_path(root, "   ") is None
        assert tool_path(root, ".") is None
        assert tool_path(root, 7) is None
        assert tool_path_abs(root, None) is None
        assert tool_path_abs(root, "   ") is None

    def test_the_root_itself_is_no_file_but_is_a_location(self, tmp_path):
        root = _repo(tmp_path)
        assert tool_path(root, ".") is None
        assert tool_path_abs(root, ".") == str(root.resolve())

    def test_unknown_user_in_a_tilde_path_is_no_path(self, tmp_path):
        """``~user`` for a user this machine hasn't got raises rather than
        resolving; a model-written path must not take the frame down."""
        root = _repo(tmp_path)
        assert tool_path(root, "~nosuchuser9977/notes.md") is None
        assert tool_path_abs(root, "~nosuchuser9977/notes.md") is None


class TestToolPathNamed:
    """The same file under the name the call used — what the card shows.

    Resolving is for opening; a reader is handed back the path they would
    recognise, which on macOS is the only one of the two that looks like
    anything they typed (``/tmp`` is ``/private/tmp``).
    """

    def test_a_plain_path_is_named_as_it_was_given(self, tmp_path):
        root = _repo(tmp_path)
        assert tool_path_named(root, "/etc/passwd") == "/etc/passwd"
        assert tool_path_named(root, str(root / "src" / "a.py")) == str(
            root / "src" / "a.py"
        )

    def test_a_link_is_left_in_the_name(self, tmp_path):
        root = _repo(tmp_path)
        link = tmp_path / "linked"
        link.symlink_to(root)

        assert tool_path_named(root, str(link / "src" / "a.py")) == str(
            link / "src" / "a.py"
        )
        # And the resolved spelling is still the one that opens.
        assert tool_path(root, str(link / "src" / "a.py")) == "src/a.py"

    def test_a_relative_path_is_named_where_it_starts(self, tmp_path):
        root = _repo(tmp_path)
        assert tool_path_named(root, "src/a.py") == str(root / "src" / "a.py")

    def test_home_is_expanded_but_not_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        root = _repo(tmp_path)
        assert tool_path_named(root, "~/notes.md") == str(
            tmp_path / "home/notes.md"
        )

    def test_dotdot_after_a_link_is_resolved(self, tmp_path):
        """The one case where the named form would be a different file:
        the kernel reads ``link/..`` as the parent of what the link points
        at, not of the link."""
        root = _repo(tmp_path)
        link = tmp_path / "linked"
        link.symlink_to(root)

        assert tool_path_named(root, str(link / ".." / "outside.txt")) == str(
            tmp_path / "outside.txt"
        )

    def test_nothing_named_is_no_path(self, tmp_path):
        root = _repo(tmp_path)
        assert tool_path_named(root, None) is None
        assert tool_path_named(root, "   ") is None
        assert tool_path_named(root, 7) is None
        assert tool_path_named(root, "~nosuchuser9977/notes.md") is None


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
