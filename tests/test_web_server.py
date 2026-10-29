"""Tests for the web app factory — lifespan wiring, ``/api/meta``, and the
static mount with its dist-missing fallback.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from tests.mocks import MockLLMProvider
from toddler.config.settings import Settings
from toddler.llm import Message
from toddler.session import SessionManager, SQLiteDatabase, StorageManager
from toddler.web.app import create_app


def _seed_session(tmp_path):
    """Write a used session for *tmp_path* straight into the shared DB."""
    db = SQLiteDatabase(tmp_path / "sessions.db")
    db.open()
    storage = StorageManager(db)
    session = storage.create(cwd=str(tmp_path))
    conversation = storage.create_conversation(session.id)
    storage.append_message(
        session.id, Message.user("hi"), conversation_id=conversation.id,
    )
    return session


class TestCreateApp:
    def test_meta_reports_repo_root_model_and_dev(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path, dev=True)
        with TestClient(app) as client:
            resp = client.get("/api/meta")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_root"] == str(tmp_path)
        assert data["model"] == settings.model_spec
        assert data["dev"] is True

    def test_dev_mode_adds_cors_for_vite(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path, dev=True)
        with TestClient(app) as client:
            resp = client.get(
                "/api/meta",
                headers={"Origin": "http://localhost:5173"},
            )
        assert (
            resp.headers["access-control-allow-origin"]
            == "http://localhost:5173"
        )

    def test_startup_creates_fresh_session_row(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            state = client.app.state.web
            assert state.repo_root == tmp_path
            assert len(state.storage_mgr.list_all()) == 1
            # CLI parity: shutdown prunes the fresh empty session.
        db = SQLiteDatabase(tmp_path / "sessions.db")
        db.open()
        assert db.list_sessions() == []

    def test_startup_resumes_the_last_session_for_the_repo(self, tmp_path):
        """``tod serve`` in a project with history reopens that session
        instead of starting a new one every time."""
        prior = _seed_session(tmp_path)
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            state = client.app.state.web
            assert state.session_mgr.session.id == prior.id
        # A resumed session holds messages, so shutdown does not prune it.
        db = SQLiteDatabase(tmp_path / "sessions.db")
        db.open()
        assert db.get_session(prior.id) is not None

    def test_startup_opens_a_new_session_when_the_last_is_held(self, tmp_path):
        """Another live process owns the project's last session — the
        server must not append to the same transcript."""
        prior = _seed_session(tmp_path)
        settings = Settings(session_dir=tmp_path)
        db = SQLiteDatabase(tmp_path / "sessions.db")
        db.open()
        holder = SessionManager(
            settings,
            StorageManager(db),
            MockLLMProvider(),
            repo_root=tmp_path,
        )
        asyncio.run(holder.resolve())
        assert holder.session.id == prior.id

        try:
            app = create_app(settings, repo_root=tmp_path)
            with TestClient(app) as client:
                state = client.app.state.web
                assert state.session_mgr.session.id != prior.id
        finally:
            asyncio.run(holder.close())

    def test_new_session_starts_fresh(self, tmp_path):
        prior = _seed_session(tmp_path)
        settings = Settings(session_dir=tmp_path)
        app = create_app(
            settings, repo_root=tmp_path, new_session=True,
        )
        with TestClient(app) as client:
            state = client.app.state.web
            assert state.session_mgr.session.id != prior.id

    def test_missing_dist_returns_build_hint(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert "npm run build" in resp.json()["error"]

    def test_dist_mount_serves_index_html(self, tmp_path):
        dist = tmp_path / "website" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html><body>hi</body></html>")
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert resp.text == "<html><body>hi</body></html>"
