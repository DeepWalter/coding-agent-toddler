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
    SlowStreamLLM,
    make_mock_llm,
    pause_on_write,
    plan_proposal_response,
    text_response,
    tool_use_response,
)
from toddler.agent.planner import Plan, PlanStep
from toddler.agent.state_machine import AgentMode
from toddler.cli.commands import CommandResult
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
            assert hello["session"]["gating_editable"] is True
            assert hello["session"]["cwd"] == str(tmp_path)
            assert hello["conversation"]["sequence_num"] == 1

            ws.send_json({"cmd": "turn", "input": "hi"})
            frames = [ws.receive_json() for _ in range(7)]
            # The classification and finish transitions each broadcast a
            # session_info (mode_label, gating, permission) — a no-op for
            # a simple turn, but the same push the plan workflow needs.
            assert [f["type"] for f in frames] == [
                "turn_started",
                "state",
                "session_info",
                "content_delta",
                "agent_finished",
                "session_info",
                "state",
            ]
            assert frames[1] == {"type": "state", "busy": True}
            assert frames[3] == {
                "type": "content_delta",
                "text_delta": "Hello from the agent.",
            }
            assert frames[4]["reason"] == "LLM finished its turn."
            assert frames[4]["usage"] == {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
            }
            assert frames[6] == {"type": "state", "busy": False}

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
            # context injects a system prompt at turn start and save()
            # persists it too (CLI parity), but it is scaffolding, not
            # transcript — the replay skips it, so the console shows
            # user + assistant only.
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                msgs = hello["messages"]
                assert [m["role"] for m in msgs] == [
                    "user", "assistant",
                ]
                assert msgs[0] == {"role": "user", "content": "hi"}
                assert msgs[1]["content"] == "Hello there."

    def test_hello_replays_tool_card_after_turn(self, tmp_path):
        out = tmp_path / "out.txt"
        llm = make_mock_llm(
            pause_on_write(str(out)),
            text_response("Done."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # hello
                ws.send_json({"cmd": "turn", "input": "write it"})
                _wait_for(ws, "agent_paused")
                ws.send_json({
                    "cmd": "approve_tool", "tool_id": "call_write",
                })
                _wait_for(ws, "tool_call_end")
                _wait_for(ws, "agent_finished")

            # Reconnect — the foldable tool card replays from storage as
            # a closed block shaped like tool_call_end.
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                msgs = hello["messages"]
                assert [m["role"] for m in msgs] == [
                    "user", "tool", "assistant",
                ]
                tool = msgs[1]
                assert tool["tool_id"] == "call_write"
                assert tool["tool_name"] == "write_file"
                assert tool["input"] == {
                    "file_path": str(out), "content": "hello",
                }
                assert tool["result"]["success"] is True
                assert tool["result"]["output"] is not None
                assert tool["result"]["error"] is None


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
            # Exactly four frames, in any task order: the ack, the
            # turn_cancelled broadcast, the reset's session_info (the
            # machine is back to IDLE — pill unfrozen), and the busy
            # reset.
            frames = [ws.receive_json() for _ in range(4)]
            assert {"type": "ack", "cmd": "cancel", "accepted": True} in frames
            assert "turn_cancelled" in [f["type"] for f in frames]
            states = [f for f in frames if f["type"] == "state"]
            assert any(f["busy"] is False for f in states)
            infos = [f for f in frames if f["type"] == "session_info"]
            assert any(
                f["session"]["mode_label"] == "MANUAL"
                and f["session"]["gating_editable"] is True
                for f in infos
            )

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

    def test_cancel_persists_messages_without_next_turn(self, tmp_path):
        """A cancelled turn is persisted immediately — the repair
        (partial output + cancellation marker) survives a reconnect
        before any next turn runs."""
        llm = SlowStreamLLM("Partial response that never finishes")
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # hello
                ws.send_json({"cmd": "turn", "input": "explain"})
                _wait_for(ws, "content_delta")
                ws.send_json({"cmd": "cancel"})
                _wait_for(ws, "ack")
                _wait_for(ws, "turn_cancelled")
                # busy:false is broadcast from the runner's finally,
                # after cancel_turn() has persisted the repair.
                assert _wait_for(ws, "state")["busy"] is False

            # Reconnect — the transcript replays from SQLite.  The
            # cancelled turn is there (user input, partial assistant
            # text, cancellation marker) even though no next turn ran.
            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                msgs = hello["messages"]
                assert [m["role"] for m in msgs] == [
                    "user", "assistant", "user",
                ]
                assert msgs[0]["content"] == "explain"
                assert "Partial" in msgs[1]["content"]
                assert "never finishes" not in msgs[1]["content"]
                assert msgs[2]["content"] == (
                    "[The previous turn was cancelled by the user.]"
                )
                # The marker is tagged at replay so the frontend renders a
                # fold line instead of a user bubble; real entries stay bare.
                assert msgs[2]["fold"] == "cancelled"
                assert "fold" not in msgs[0]


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

    def test_complex_turn_broadcasts_plan_mode_state(self, tmp_path):
        """A complexity-classified plan turn pushes session_info so the
        frontend's badge/pill follow the state machine mid-turn."""
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
            text_response("Executing plan."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["session"]["mode_label"] == "MANUAL"

            # "refactor" is a complexity keyword — no force_plan needed.
            ws.send_json({"cmd": "turn", "input": "refactor thing"})

            # Classification into plan mode broadcasts the new label.
            info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "PLAN"
            assert info["session"]["gating_editable"] is False

            _wait_for(ws, "plan_proposed")
            ws.send_json({
                "cmd": "approve_plan",
                "plan_id": "plan-1",
                "mode": "manual",
            })
            _wait_for(ws, "ack")
            _wait_for(ws, "agent_finished")

            # The finish transition returns the pill/badge to idle state.
            info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "MANUAL"
            assert info["session"]["gating_editable"] is True

    def test_approve_with_auto_broadcasts_gating_flip(self, tmp_path):
        """Approve-with-auto flips the pill to auto — gating changes are
        not state-machine transitions, so the runner pushes them too."""
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
            _wait_for(ws, "plan_proposed")
            ws.send_json({
                "cmd": "approve_plan",
                "plan_id": "plan-1",
                "mode": "auto",
            })
            _wait_for(ws, "ack")
            # Approval broadcasts twice (transition, then the gating
            # flip) — drain until the pill reads auto.
            info = _wait_for(ws, "session_info")
            while info["session"]["permission_mode"] != "auto":
                info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "PLAN"
            assert info["session"]["gating_editable"] is True
            _wait_for(ws, "agent_finished")

    def test_cancel_plan_turn_unfreezes_pill(self, tmp_path):
        """Cancelling a plan turn resets the machine to IDLE, so the
        frozen pill unfreezes — mode_label drops back to the gate."""
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
            # Frozen while the plan awaits approval.
            info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "PLAN"
            assert info["session"]["gating_editable"] is False
            _wait_for(ws, "plan_proposed")

            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "ack")
            _wait_for(ws, "turn_cancelled")
            # The cancel resets the machine — the pill unfreezes.
            info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "MANUAL"
            assert info["session"]["gating_editable"] is True
            assert _wait_for(ws, "state")["busy"] is False

    def test_cancel_appends_marker_to_next_turn_context(self, tmp_path):
        """A cancelled turn leaves a marker in the context — the next
        turn's LLM call knows the previous turn was cut short."""
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
            text_response("Continuing."),
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

            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "ack")
            _wait_for(ws, "turn_cancelled")

            # The next turn's first LLM call sees the marker.
            ws.send_json({"cmd": "turn", "input": "continue anyway"})
            _wait_for(ws, "content_delta")

            call = llm.messages_history[-1]
            assert any(
                m.content == "[The previous turn was cancelled by the user.]"
                for m in call
            )
            # The cancelled turn's own messages are still there — the
            # model can continue from them.
            assert [m.role for m in call].count("user") >= 2

    def test_cancel_answers_pending_tool_call_in_next_turn_context(self, tmp_path):
        """A cancel while paused on a tool approval leaves a dangling
        assistant tool_use — the repair answers it with a cancelled
        tool_result, so the next turn's LLM call sees a valid pair."""
        llm = make_mock_llm(
            pause_on_write(str(tmp_path / "out.txt")),
            text_response("Continuing."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "ack")
            _wait_for(ws, "turn_cancelled")

            ws.send_json({"cmd": "turn", "input": "continue anyway"})
            _wait_for(ws, "content_delta")

            call = llm.messages_history[-1]
            tool_msgs = [m for m in call if m.role == "tool"]
            assert len(tool_msgs) == 1
            assert all(
                b.type == "tool_result"
                and b.is_error is True
                and "cancelled" in b.tool_result_content
                for b in tool_msgs[0].blocks
            )
            # The assistant message with the tool_use survives intact,
            # immediately before the answering tool message.
            idx = call.index(tool_msgs[0])
            assert call[idx - 1].role == "assistant"
            assert any(b.type == "tool_use" for b in call[idx - 1].blocks)
            # The marker is still there so the model knows the turn was
            # cut short, not finished.
            assert any(
                m.content == "[The previous turn was cancelled by the user.]"
                for m in call
            )

    def test_cancel_mid_stream_keeps_partial_text_in_next_turn(self, tmp_path):
        """A cancel while the LLM is streaming text keeps the partial
        output in the context — the next turn's LLM call sees it."""
        llm = SlowStreamLLM("Partial response that never finishes")
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "explain"})
            _wait_for(ws, "content_delta")

            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "ack")
            _wait_for(ws, "turn_cancelled")

            ws.send_json({"cmd": "turn", "input": "continue"})
            _wait_for(ws, "content_delta")

            call = llm.messages_history[-1]
            assistant_msgs = [m for m in call if m.role == "assistant"]
            # The partial output survived the cancel…
            assert any("Partial" in m.content for m in assistant_msgs)
            # …but the stream was cut off before the end.
            assert all("never finishes" not in m.content for m in assistant_msgs)
            # And the marker is there so the model knows the turn was
            # cut short, not finished.
            assert any(
                m.content == "[The previous turn was cancelled by the user.]"
                for m in call
            )

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
# Plan reconnect: a pending plan survives in hello
# ============================================================================


class TestPlanReconnect:
    def test_second_tab_sees_pending_plan(self, tmp_path):
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as watcher:
            watcher.receive_json()  # hello
            watcher.send_json({
                "cmd": "turn",
                "input": "refactor thing",
                "force_plan": True,
            })
            _wait_for(watcher, "plan_proposed")

            # A second tab connects while the plan waits for approval:
            # hello carries the proposal so the PlanCard isn't lost.
            with client.websocket_connect("/ws") as tab2:
                hello2 = tab2.receive_json()
                assert hello2["busy"] is True
                assert hello2["paused"] is None
                assert hello2["plan"]["plan"]["id"] == "plan-1"
                assert hello2["plan"]["steps"] == []
                # The pill is frozen while the plan awaits approval.
                assert hello2["session"]["gating_editable"] is False

                tab2.send_json({
                    "cmd": "approve_plan",
                    "plan_id": "plan-1",
                    "mode": "manual",
                })
                ack = _wait_for(tab2, "ack")
                assert ack == {
                    "type": "ack",
                    "cmd": "approve_plan",
                    "accepted": True,
                }

            # The original tab still sees the turn complete.
            finished = _wait_for(watcher, "agent_finished")
            assert finished["reason"] == "LLM finished its turn."

    def test_second_tab_joins_mid_execution_with_step_statuses(self, tmp_path):
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
            # Step statuses move only via the plan_update tool — call it
            # to stream a plan_step_update during execution.
            tool_use_response(
                "plan_update",
                {"step_id": "step-1", "status": "in_progress"},
                tool_id="call_update",
            ),
            pause_on_write(str(tmp_path / "out.txt")),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as watcher:
            watcher.receive_json()  # hello
            watcher.send_json({
                "cmd": "turn",
                "input": "refactor thing",
                "force_plan": True,
            })
            _wait_for(watcher, "plan_proposed")
            watcher.send_json({
                "cmd": "approve_plan",
                "plan_id": "plan-1",
                "mode": "manual",
            })
            # Execution: plan_update (READ — no pause under MANUAL) flips
            # step-1 to in_progress and streams the status update; the
            # write step then parks the turn on approval.
            _wait_for(watcher, "plan_step_update")
            _wait_for(watcher, "agent_paused")

            with client.websocket_connect("/ws") as tab2:
                hello2 = tab2.receive_json()
                assert hello2["busy"] is True
                assert hello2["plan"]["plan"]["id"] == "plan-1"
                assert any(
                    status != "pending"
                    for _, _, status in hello2["plan"]["steps"]
                )
                # The card is decided (steps running) — and the paused
                # write tool is still answerable from the new tab.
                assert hello2["paused"]["type"] == "agent_paused"

                tab2.send_json({
                    "cmd": "approve_tool", "tool_id": "call_write",
                })
                _wait_for(tab2, "ack")

            _wait_for(watcher, "agent_finished")

    def test_plan_wait_auto_cancels_when_last_watcher_leaves(self, tmp_path):
        llm = make_mock_llm(
            text_response("Exploring..."),
            plan_proposal_response(_plan()),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # hello
                ws.send_json({
                    "cmd": "turn",
                    "input": "refactor thing",
                    "force_plan": True,
                })
                _wait_for(ws, "plan_proposed")
                # Exit the context without answering — the stranded plan
                # approval must not hang forever.
            state = client.app.state.web
            _wait_until(lambda: not state.runner.busy)
            assert state.runner.plan_snapshot is None


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

    def test_new_conversation_holds_lock_against_starting_turn(self, tmp_path):
        """TOCTOU regression: new_conversation's busy check and the
        mutation are atomic — a turn arriving mid-mutation (another tab)
        is rejected, never started behind a hello replay wipe."""
        llm = make_mock_llm(text_response("Hello."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            state = client.app.state.web
            probe: dict = {}
            original_new_conversation = state.session_mgr.new_conversation

            async def new_conversation_with_sneak_attempt(title=None):
                probe["lock_held"] = state.runner.busy
                probe["sneak_accepted"] = await state.runner.start("sneak")
                return await original_new_conversation(title=title)

            state.session_mgr.new_conversation = new_conversation_with_sneak_attempt
            ws.send_json({"cmd": "new_conversation"})
            ack = _wait_for(ws, "ack")
            assert ack["accepted"] is True
            replay = _wait_for(ws, "hello")
            assert replay["conversation"]["sequence_num"] == 2

            assert probe["lock_held"] is True
            assert probe["sneak_accepted"] is False
            assert llm.call_count == 1  # only the real "hi" turn
            assert state.runner.busy is False


# ============================================================================
# Slash commands (dispatch locally, never to the LLM)
# ============================================================================


class TestSlashCommands:
    def test_help_broadcasts_help_text(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/help"})
            notice = _wait_for(ws, "notice")
            assert "Slash Commands" in notice["message"]
            assert "/mode" in notice["message"]
            assert llm.call_count == 0

    def test_mode_shows_status_notice(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/mode"})
            notice = _wait_for(ws, "notice")
            assert "Mode:" in notice["message"]
            assert llm.call_count == 0

    def test_mode_auto_broadcasts_session_info_and_notice(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/mode auto"})
            # A session_info frame (header update), not a full hello
            # replay — the console scroll-back must survive.
            frame = ws.receive_json()
            assert frame["type"] == "session_info"
            assert frame["session"]["permission_mode"] == "auto"
            notice = _wait_for(ws, "notice")
            assert "**Gating:** AUTO" in notice["message"]
            assert client.app.state.web.session_mgr.permission_mode.value == "auto"

    def test_mode_does_not_clear_previous_notices(self, tmp_path):
        """Regression: /help then /mode must not wipe the console.

        The mutating broadcast used to be a full hello replay, which
        resets all blocks — the /help output vanished with no
        scroll-back.  Bare /mode changes nothing, so the notice is the
        only frame: no hello, no session_info.
        """
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/help"})
            _wait_for(ws, "notice")

            ws.send_json({"cmd": "turn", "input": "/mode"})
            frame = ws.receive_json()
            assert frame["type"] == "notice"  # straight to the notice
            assert "Mode:" in frame["message"]
            assert llm.call_count == 0

    def test_clear_archives_and_broadcasts_hello(self, tmp_path):
        llm = make_mock_llm(text_response("Hello."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            ws.send_json({"cmd": "turn", "input": "/clear"})
            replay = _wait_for(ws, "hello")
            assert replay["conversation"]["sequence_num"] == 2
            assert replay["messages"] == []
            notice = _wait_for(ws, "notice")
            assert "Started new conversation" in notice["message"]
            assert llm.call_count == 1  # only the real turn hit the LLM

    def test_quit_and_view_unavailable(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/quit"})
            notice = _wait_for(ws, "notice")
            assert "not available in the web UI" in notice["message"]

            ws.send_json({"cmd": "turn", "input": "/view 1"})
            notice = _wait_for(ws, "notice")
            assert "not available in the web UI" in notice["message"]

            # The connection is still alive after both.
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            assert llm.call_count == 0

    def test_unknown_slash_command_notice(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/nope"})
            notice = _wait_for(ws, "notice")
            assert "Unknown command: /nope" in notice["message"]
            assert llm.call_count == 0

    def test_mutating_slash_while_busy_rejected(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "turn", "input": "/clear"})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "busy",
                "message": "A turn is already running.",
            }
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")

    def test_help_allowed_while_busy(self, tmp_path):
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "turn", "input": "/help"})
            notice = _wait_for(ws, "notice")
            assert "Slash Commands" in notice["message"]
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")

    def test_failed_resume_does_not_replay_hello(self, tmp_path):
        """Regression: a failed /resume (bad id) changes nothing — no
        hello replay, so the console scroll-back (/help output) survives,
        and the current conversation stays active.

        The replay decision used to be made on the command string, so a
        failed resume broadcast a full hello that reset all blocks.
        """
        llm = make_mock_llm(text_response("Hello."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            ws.send_json({"cmd": "turn", "input": "/help"})
            _wait_for(ws, "notice")

            conv_id = client.app.state.web.session_mgr.conversation.id
            ws.send_json({"cmd": "turn", "input": "/resume no-such-id"})
            frame = ws.receive_json()
            assert frame["type"] == "notice"  # no hello replay first
            assert "not found" in frame["message"]

            # The failed resume is a no-op: same conversation, still active.
            mgr = client.app.state.web.session_mgr
            assert mgr.conversation.id == conv_id
            assert mgr.conversation.status == "active"
            assert llm.call_count == 1

    def test_unknown_prefix_is_not_a_mode_change(self, tmp_path):
        """Regression: /moded (a prefix of /mode) is an unknown command,
        not a mutation — it must not broadcast session_info or wipe the
        console.
        """
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "/moded"})
            frame = ws.receive_json()
            assert frame["type"] == "notice"  # straight to the notice
            assert "Unknown command: /moded" in frame["message"]
            assert llm.call_count == 0

    def test_unknown_prefix_not_busy_gated(self, tmp_path):
        """Regression: the busy gate matches exact commands — /moded while
        a turn runs is an informational notice, not a busy rejection."""
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "turn", "input": "/moded"})
            notice = _wait_for(ws, "notice")
            assert "Unknown command: /moded" in notice["message"]
            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")

    def test_mutation_holds_lock_against_starting_turn(self, tmp_path):
        """TOCTOU regression: the busy check and the mutation are atomic.

        A turn arriving while /clear is mid-dispatch (another tab) used
        to slip in between the ``busy`` check and the mutation — the
        sneaked turn started, and the hello replay then wiped its
        transcript mid-run.  The mutation must hold the busy lock, so
        the sneaked start is rejected and the LLM is never hit twice.
        """
        llm = make_mock_llm(text_response("Hello."))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "hi"})
            _wait_for(ws, "agent_finished")

            state = client.app.state.web
            probe: dict = {}
            original_dispatch = state.cmd_dispatcher.dispatch

            async def dispatch_with_sneak_attempt(text: str) -> CommandResult:
                # The mutation is under way — the lock is already held,
                # so the simulated second tab's turn must be rejected.
                probe["lock_held"] = state.runner.busy
                probe["sneak_accepted"] = await state.runner.start("sneak")
                return await original_dispatch(text)

            state.cmd_dispatcher.dispatch = dispatch_with_sneak_attempt
            ws.send_json({"cmd": "turn", "input": "/clear"})
            replay = _wait_for(ws, "hello")
            assert replay["conversation"]["sequence_num"] == 2

            assert probe["lock_held"] is True
            assert probe["sneak_accepted"] is False
            assert llm.call_count == 1  # only the real "hi" turn
            assert state.runner.busy is False


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

    def test_set_mode_plan(self, tmp_path):
        """The pill's plan state flags the next turn for plan mode (same
        semantics as ``/mode plan``) and broadcasts session_info so every
        tab's pill updates."""
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": "plan"})
            ack = ws.receive_json()
            assert ack == {"type": "ack", "cmd": "set_mode", "accepted": True}
            mgr = client.app.state.web.session_mgr
            assert mgr.permission_mode.value == "manual"
            assert mgr.state_machine.plan_pending is True
            info = ws.receive_json()
            assert info["type"] == "session_info"
            assert info["session"]["mode_label"] == "PLAN"

    def test_set_mode_manual_clears_plan(self, tmp_path):
        """Switching back to manual/auto cancels a pending plan flag,
        mirroring the CLI's ``/mode manual``."""
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": "plan"})
            ws.receive_json()  # ack
            ws.receive_json()  # session_info
            ws.send_json({"cmd": "set_mode", "mode": "manual"})
            # The gating flip is pushed by the observer before the ack —
            # drain either way.
            ack = _wait_for(ws, "ack")
            assert ack == {
                "type": "ack", "cmd": "set_mode", "accepted": True,
            }
            info = _wait_for(ws, "session_info")
            assert info["session"]["mode_label"] == "MANUAL"
            mgr = client.app.state.web.session_mgr
            assert mgr.permission_mode.value == "manual"
            assert mgr.state_machine.plan_pending is False

    def test_set_mode_invalid(self, tmp_path):
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": "sneaky"})
            error = ws.receive_json()
            assert error["code"] == "invalid_mode"

    def test_set_mode_non_string_rejected(self, tmp_path):
        """A non-string mode (JSON array/object) must not raise a
        TypeError that kills the connection — same invalid_mode error."""
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "set_mode", "mode": ["auto"]})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "invalid_mode",
                "message": "mode must be 'manual', 'auto' or 'plan'.",
            }
            # The connection survives.
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    def test_set_mode_plan_while_busy_rejected(self, tmp_path):
        """Plan flags the next turn, so it can only be set while idle —
        a mid-turn click is rejected with a busy error and leaves the
        pending flag untouched."""
        llm = make_mock_llm(pause_on_write(str(tmp_path / "out.txt")))
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "set_mode", "mode": "plan"})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "busy",
                "message": "Plan mode can only be set while the agent is idle.",
            }
            mgr = client.app.state.web.session_mgr
            assert mgr.state_machine.plan_pending is False
            assert mgr.permission_mode.value == "manual"

            ws.send_json({"cmd": "cancel"})
            _wait_for(ws, "turn_cancelled")

    def test_set_mode_gating_frozen_until_plan_approved(self, tmp_path):
        """Gating is pinned to manual while a plan awaits approval — a
        flip is rejected with a frozen error, whichever tab clicks."""
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

            ws.send_json({"cmd": "set_mode", "mode": "auto"})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "frozen",
                "message": "Gating is fixed to manual until the plan is approved.",
            }
            mgr = client.app.state.web.session_mgr
            assert mgr.permission_mode.value == "manual"

            ws.send_json({
                "cmd": "reject_plan", "plan_id": "plan-1", "feedback": "",
            })
            _wait_for(ws, "agent_finished")

    def test_set_mode_gating_allowed_during_executing(self, tmp_path):
        """A live gating flip while a plain turn runs (EXECUTING) is
        allowed and takes effect immediately."""
        llm = make_mock_llm(
            pause_on_write(str(tmp_path / "out.txt")),
            text_response("Done."),
        )
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_json({"cmd": "turn", "input": "write"})
            _wait_for(ws, "agent_paused")

            ws.send_json({"cmd": "set_mode", "mode": "auto"})
            ack = ws.receive_json()
            assert ack == {"type": "ack", "cmd": "set_mode", "accepted": True}
            mgr = client.app.state.web.session_mgr
            assert mgr.permission_mode.value == "auto"
            info = ws.receive_json()
            assert info["type"] == "session_info"
            assert info["session"]["gating_editable"] is True
            assert info["session"]["permission_mode"] == "auto"

            # The parked approval is still answerable.
            ws.send_json({"cmd": "approve_tool", "tool_id": "call_write"})
            _wait_for(ws, "ack")
            _wait_for(ws, "agent_finished")

    def test_set_mode_gating_allowed_during_plan_executing(self, tmp_path):
        """Once the plan is approved (PLAN_EXECUTING), gating flips are
        live again — e.g. downgrading back to manual mid-execution."""
        llm = make_mock_llm()
        app = _app(tmp_path, llm)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            sm = client.app.state.web.session_mgr.state_machine
            # Each transition broadcasts a session_info now — drain them
            # before asserting the ack's frame order.
            assert sm.transition(AgentMode.PLAN_EXPLORING)
            assert sm.transition(AgentMode.PLAN_PROPOSING)
            assert sm.transition(AgentMode.PLAN_WAITING)
            assert sm.transition(AgentMode.PLAN_EXECUTING)
            for _ in range(4):
                assert ws.receive_json()["type"] == "session_info"

            ws.send_json({"cmd": "set_mode", "mode": "auto"})
            ack = _wait_for(ws, "ack")
            assert ack == {"type": "ack", "cmd": "set_mode", "accepted": True}
            assert client.app.state.web.session_mgr.permission_mode.value == "auto"
            info = _wait_for(ws, "session_info")
            assert info["session"]["gating_editable"] is True
            assert info["session"]["permission_mode"] == "auto"
            # The plan turn is still reported as PLAN.
            assert info["session"]["mode_label"] == "PLAN"

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
