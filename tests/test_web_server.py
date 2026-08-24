"""Tests for the web app factory — lifespan wiring, ``/api/meta``, and the
static mount with its dist-missing fallback.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from toddler.config.settings import Settings
from toddler.session import SQLiteDatabase
from toddler.web.app import create_app


class TestCreateApp:
    def test_meta_reports_repo_root_model_and_dev(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path, dev=True)
        with TestClient(app) as client:
            resp = client.get("/api/meta")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_root"] == str(tmp_path)
        assert data["model"] == settings.model
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

    def test_missing_dist_returns_build_hint(self, tmp_path):
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert "npm run build" in resp.json()["error"]

    def test_dist_mount_serves_index_html(self, tmp_path):
        dist = tmp_path / "web" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html><body>hi</body></html>")
        settings = Settings(session_dir=tmp_path)
        app = create_app(settings, repo_root=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert resp.text == "<html><body>hi</body></html>"
