"""Tests for ``/ws/terminal`` — PTY I/O, resize, exit, and the gates.

The endpoint is a real shell on a real PTY, so the tests pin
``SHELL=/bin/sh`` for deterministic output.  They are POSIX-only; the
endpoint refuses non-POSIX platforms by design.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.mocks import MockLLMProvider
from toddler.config.settings import Settings
from toddler.web.app import create_app
from toddler.web.terminal import _client_is_loopback

pytestmark = pytest.mark.skipif(os.name != "posix", reason="pty is POSIX-only")


def _client(tmp_path, *, dev=False, terminal=True) -> TestClient:
    settings = Settings(session_dir=tmp_path)
    app = create_app(
        settings,
        repo_root=tmp_path,
        llm=MockLLMProvider(),
        dev=dev,
        terminal_enabled=terminal,
    )
    return TestClient(app)


def _receive_until(ws, needle: bytes, max_frames: int = 200) -> bytes:
    """Accumulate output frames until *needle* appears (or the socket ends)."""
    seen = bytearray()
    for _ in range(max_frames):
        message = ws.receive()
        if message.get("type") == "websocket.close":
            break
        if message.get("bytes"):
            seen += message["bytes"]
        if message.get("text"):
            seen += message["text"].encode()
        if needle in seen:
            return bytes(seen)
    raise AssertionError(f"no {needle!r} in terminal output: {bytes(seen)!r}")


def _receive_json(ws, kind: str, max_frames: int = 200) -> dict:
    """Read frames until a JSON frame of *kind* arrives, dropping output."""
    for _ in range(max_frames):
        message = ws.receive()
        if message.get("type") == "websocket.close":
            break
        text = message.get("text")
        if not text:
            continue
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            continue
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame received")


def test_shell_echoes_input(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    with _client(tmp_path) as client, client.websocket_connect("/ws/terminal") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["shell"] == "/bin/sh"
        assert ready["cwd"] == str(tmp_path)
        ws.send_json({"type": "input", "data": "echo marker-$((1+1))\n"})
        assert b"marker-2" in _receive_until(ws, b"marker-2")


def test_resize_reaches_the_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    with _client(tmp_path) as client, client.websocket_connect("/ws/terminal") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "resize", "cols": 100, "rows": 40})
        ws.send_json({"type": "input", "data": "stty size\n"})
        assert b"40 100" in _receive_until(ws, b"40 100")


def test_shell_exit_reports_the_code(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    with _client(tmp_path) as client, client.websocket_connect("/ws/terminal") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "input", "data": "exit 3\n"})
        assert _receive_json(ws, "exit")["code"] == 3


def test_cross_origin_is_refused(tmp_path):
    with _client(tmp_path) as client, client.websocket_connect(
        "/ws/terminal", headers={"origin": "http://evil.example"},
    ) as ws:
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert "origin" in frame["message"]


def test_same_origin_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    with _client(tmp_path) as client, client.websocket_connect(
        "/ws/terminal", headers={"origin": "http://testserver"},
    ) as ws:
        assert ws.receive_json()["type"] == "ready"


def test_disabled_terminal_refuses(tmp_path):
    with _client(tmp_path, terminal=False) as client, client.websocket_connect(
        "/ws/terminal",
    ) as ws:
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert "disabled" in frame["message"]


def test_meta_reports_terminal_availability(tmp_path):
    with _client(tmp_path, terminal=True) as client:
        assert client.get("/api/meta").json()["terminal"] is True
    with _client(tmp_path, terminal=False) as client:
        assert client.get("/api/meta").json()["terminal"] is False


class _Peer:
    def __init__(self, host: str | None):
        self.client = None if host is None else SimpleNamespace(host=host)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (None, True),
        ("testclient", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("192.168.1.9", False),
        ("10.0.0.2", False),
    ],
)
def test_client_loopback_check(host, expected):
    assert _client_is_loopback(_Peer(host)) is expected
