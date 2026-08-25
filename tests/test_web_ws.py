"""WebSocket protocol tests — hello, turns, approvals, cancel, plans.

Turn flows run against real :class:`SessionManager` components with a
mock LLM provider injected through ``create_app(..., llm=...)`` — no
network.  TestClient runs the app in a portal thread, so the runner task
lives there and the sync ``receive_json`` calls from the test thread are
safe.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.mocks import (
    make_mock_llm,
    pause_on_write,
    plan_proposal_response,
    text_response,
)
from toddler.agent.planner import Plan, PlanStep
from toddler.config.settings import Settings
from toddler.web.app import create_app

# ============================================================================
# Helpers
# ============================================================================


def _app(tmp_path, llm):
    """An app with a tmp session dir, tmp repo root, and the mock LLM."""
    settings = Settings(session_dir=tmp_path)
    return create_app(settings, repo_root=tmp_path, llm=llm)


def _wait_for(ws, expected_type: str, max_frames: int = 50) -> dict:
    """Drain frames until one of *expected_type* arrives.

    Acks and runner-broadcast events travel on different tasks, so their
    arrival order is not deterministic — helpers must skip unrelated
    frames rather than assume a fixed sequence.
    """
    for _ in range(max_frames):
        frame = ws.receive_json()
        if frame["type"] == expected_type:
            return frame
    raise AssertionError(f"no {expected_type!r} frame received")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll *predicate* from the test thread (the app runs on a portal
    thread, so the runner's state flips asynchronously)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def _plan() -> Plan:
    return Plan(
        id="plan-1",
        title="Refactor thing",
        summary="Move the logic.",
        steps=[
            PlanStep(id="step-1", description="Read the code"),
            PlanStep(id="step-2", description="Edit the code"),
        ],
    )


# ============================================================================
# Hello + full turn stream
# ============================================================================


class TestTurnStream:
    def test_hello_then_full_turn(self, tmp_path):
        llm = make_mock_llm(text_response("Hello from the agent."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["busy"] is False
            assert hello["paused"] is None
            assert hello["messages"] == []
            assert hello["session"]["model"] == "test-model"
            assert hello["session"]["permission_mode"] == "manual"
            assert hello["session"]["cwd"] == str(tmp_path)
            assert hello["conversation"]["sequence_num"] == 1

            ws.send_json({"cmd": "turn", "input": "hi"})
            frames = [ws.receive_json() for _ in range(5)]
            assert [f["type"] for f in frames] == [
                "turn_started",
                "state",
                "text_delta",
                "agent_finished",
                "state",
            ]
            assert frames[1] == {"type": "state", "busy": True}
            assert frames[2] == {"type": "text_delta", "text": "Hello from the agent."}
            assert frames[3]["reason"] == "LLM finished its turn."
            assert frames[3]["usage"] == {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
            assert frames[4] == {"type": "state", "busy": False}

    def test_hello_replays_transcript_after_turn(self, tmp_path):
        llm = make_mock_llm(text_response("Hello there."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # hello
                ws.send_json({"cmd": "turn", "input": "hi"})
                # Drain until the turn fully completes.
                while True:
                    frame = ws.receive_json()
                    if frame["type"] == "state" and frame["busy"] is False:
                        break

            # Reconnect — the transcript replays from SQLite.  The
            # context injects a system prompt at turn start, and save()
            # persists it too (CLI parity), so the replay is
            # system + user + assistant.
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                msgs = hello["messages"]
                assert [m["role"] for m in msgs] == [
                    "system", "user", "assistant",
                ]
                assert msgs[1] == {"role": "user", "content": "hi"}
                assert msgs[2]["content"] == "Hello there."


# ============================================================================
# Busy rejection
# ============================================================================


class TestBusyRejection:
    def test_turn_while_busy_rejected(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            paused = _wait_for(ws, "agent_paused")
            assert "write_file" in paused["prompt"]

            ws.send_json({"cmd": "turn", "input": "second turn"})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "busy",
                "message": "A turn is already running.",
            }

            # Clean up so the runner isn't left paused.
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")


# ============================================================================
# Tool approval / denial
# ============================================================================


class TestToolApproval:
    def test_approve_unblocks_paused_tool(self, tmp_path):
        out = tmp_path / "out.txt"
        llm = make_mock_llm(
            pause_on_write(str(out)),
            text_response("Done writing."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write it"})
            paused = _wait_for(ws, "agent_paused")
            assert paused["choices"] == ["approve", "deny"]

            ws.send_json({"cmd": "approve_tool", "tool_id": "call_write"})
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "approve_tool",
                "accepted": True,
            }

            end = _wait_for(ws, "tool_call_end")
            assert end["tool_name"] == "write_file"
            assert end["result"]["success"] is True
            # The tool really executed — the file is on disk.
            assert out.read_text() == "hello"

            finished = _wait_for(ws, "agent_finished")
            assert finished["reason"] == "LLM finished its turn."

    def test_deny_produces_denied_result(self, tmp_path):
        llm = make_mock_llm(
            pause_on_write(str(tmp_path / "out.txt")),
            text_response("Ok, I won't."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "deny_tool", "tool_id": "call_write"})
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "deny_tool",
                "accepted": True,
            }

            end = _wait_for(ws, "tool_call_end")
            assert end["result"]["success"] is False
            assert "denied" in end["result"]["error"].lower()

    def test_approve_when_not_paused_returns_false(self, tmp_path):
        llm = make_mock_llm(text_response("Hi"))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "approve_tool", "tool_id": "x"})
            ack = ws.receive_json()
            assert ack == {
                "type": "ack",
                "cmd": "approve_tool",
                "accepted": False,
            }


# ============================================================================
# Cancel
# ============================================================================


class TestCancel:
    def test_cancel_mid_turn(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "cancel"})
            # Exactly three frames, in any task order: the ack, the
            # turn_cancelled broadcast, and the busy reset.
            frames = [ws.receive_json() for _ in range(3)]
            assert {"type": "ack", "cmd": "cancel", "accepted": True} in frames
            assert "turn_cancelled" in [f["type"] for f in frames]
            states = [f for f in frames if f["type"] == "state"]
            assert any(f["busy"] is False for f in states)

    def test_paused_turn_auto_cancels_when_last_watcher_leaves(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # hello
                ws.send_json({"cmd": "turn", "input": "write"})
                _wait_for(ws, "agent_paused")
                # Exit the context without answering — the stranded
                # approval must not hang forever.
            state = client.app.state.web
            _wait_until(lambda: not state.runner.busy)
            assert state.runner.paused_snapshot is None


# ============================================================================
# Multi-tab: resume a pending approval from a second connection
# ============================================================================


class TestMultiTab:
    def test_reconnect_while_paused_shows_pending_prompt(self, tmp_path):
        llm = make_mock_llm(
            pause_on_write(str(tmp_path / "out.txt")),
            text_response("Done."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as watcher:
            watcher.receive_json()  # hello
            watcher.send_json({"cmd": "turn", "input": "write"})
            _wait_for(watcher, "agent_paused")

            # A second tab connects mid-approval: hello carries the
            # pending prompt (busy + paused snapshot).
            with client.websocket_connect("/ws") as tab2:
                hello2 = tab2.receive_json()
                assert hello2["busy"] is True
                assert hello2["paused"]["type"] == "agent_paused"
                assert "write_file" in hello2["paused"]["prompt"]

                tab2.send_json({
                    "cmd": "approve_tool", "tool_id": "call_write",
                })
                ack = _wait_for(tab2, "ack")
                assert ack["accepted"] is True

            # The original tab still sees the turn complete.
            end = _wait_for(watcher, "tool_call_end")
            assert end["result"]["success"] is True


# ============================================================================
# Plan flow
# ============================================================================


class TestPlanFlow:
    def test_plan_approve_acks_and_executes(self, tmp_path):
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
            text_response("Executing plan."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({
                "cmd": "turn",
                "input": "refactor thing",
                "force_plan": True,
            })
            proposed = _wait_for(ws, "plan_proposed")
            assert proposed["plan"]["id"] == "plan-1"
            assert proposed["plan"]["title"] == "Refactor thing"
            assert [s["id"] for s in proposed["plan"]["steps"]] == [
                "step-1", "step-2",
            ]

            ws.send_json({
                "cmd": "approve_plan",
                "plan_id": "plan-1",
                "mode": "manual",
            })
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "approve_plan",
                "accepted": True,
            }

            # Execution phase runs the approved plan.
            finished = _wait_for(ws, "agent_finished")
            assert finished["reason"] == "LLM finished its turn."

    def test_plan_reject_acks_and_finishes(self, tmp_path):
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({
                "cmd": "turn",
                "input": "refactor thing",
                "force_plan": True,
            })
            _wait_for(ws, "plan_proposed")

            ws.send_json({
                "cmd": "reject_plan",
                "plan_id": "plan-1",
                "feedback": "",
            })
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "reject_plan",
                "accepted": True,
            }

            finished = _wait_for(ws, "agent_finished")
            assert finished["reason"] == "Plan rejected by user."
            # No further LLM calls after the rejection.
            assert llm.call_count == 2

    def test_approve_plan_invalid_mode(self, tmp_path):
        llm = make_mock_llm(text_response("Hi"))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({
                "cmd": "approve_plan",
                "plan_id": "plan-1",
                "mode": "nope",
            })
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_mode"


# ============================================================================
# Session management: new conversation + session switching
# ============================================================================


class TestSessionManagement:
    def test_new_conversation_acks_and_broadcasts_empty_replay(self, tmp_path):
        llm = make_mock_llm(text_response("Hello."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            ws.send_json({"cmd": "new_conversation"})
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "new_conversation",
                "accepted": True,
            }
            # Every tab gets a full hello replay of the new conversation.
            replay = _wait_for(ws, "hello")
            assert replay["conversation"]["sequence_num"] == 2
            assert replay["messages"] == []

    def test_switch_session_between_sessions_replays_history(self, tmp_path):
        llm = make_mock_llm(
            text_response("Hello from session one."),
            text_response("Hello from session two."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            session_a = ws.receive_json()["session"]["id"]

            # A second session exists in the DB (created via REST).
            resp = client.post("/api/sessions", json={"title": "other"})
            assert resp.status_code == 201
            session_b = resp.json()["id"]

            # A turn in session A.
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            # Switch to B — the replay is B's (empty) transcript.
            ws.send_json({"cmd": "switch_session", "session_id": session_b})
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack",
                "cmd": "switch_session",
                "accepted": True,
            }
            replay = _wait_for(ws, "hello")
            assert replay["session"]["id"] == session_b
            assert replay["messages"] == []

            # A turn in B, then switch back — A's history replays.
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")
            ws.send_json({"cmd": "switch_session", "session_id": session_a})
            replay = _wait_for(ws, "hello")
            assert replay["session"]["id"] == session_a
            contents = [m["content"] for m in replay["messages"]]
            assert "Hello from session one." in contents
            assert "Hello from session two." not in contents

    def test_switch_session_unknown_session(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "switch_session", "session_id": "nope"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "not_found"
            assert "nope" in error["message"]

    def test_new_conversation_while_busy_rejected(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "new_conversation"})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "busy",
                "message": "A turn is already running.",
            }
            # Clean up so the runner isn't left paused.
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")

    def test_switch_session_while_busy_rejected(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "switch_session", "session_id": "whatever"})
            error = ws.receive_json()
            assert error["code"] == "busy"
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")


# ============================================================================
# Simple commands
# ============================================================================


class TestSimpleCommands:
    def test_ping_pong(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    def test_set_mode(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": "auto"})
            ack = ws.receive_json()
            assert ack == {"type": "ack", "cmd": "set_mode", "accepted": True}
            assert client.app.state.web.session_mgr.permission_mode.value == "auto"

    def test_set_mode_invalid(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": "sneaky"})
            error = ws.receive_json()
            assert error["code"] == "invalid_mode"

    def test_unknown_command(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "do_the_thing"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "unknown_command"

    def test_turn_without_input(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn"})
            error = ws.receive_json()
            assert error["code"] == "invalid_input"
