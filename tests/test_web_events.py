"""Serializer round-trips — every AgentEvent class ↔ its protocol frame."""

from __future__ import annotations

from pathlib import Path

from toddler.agent.events import (
    AgentEvent,
    AgentFinished,
    AgentPaused,
    ContentDelta,
    FatalAgentError,
    PlanProposed,
    PlanStepUpdate,
    ReasoningDelta,
    RecoverableAgentError,
    ToolCallEnd,
    ToolCallStart,
    ToolUseStart,
)
from toddler.agent.planner import Plan, PlanStep
from toddler.llm import TokenUsage
from toddler.llm.messages import Message, MessageBlock
from toddler.tools.base import ToolResult
from toddler.tools.plan import PlanStepStatus
from toddler.web.events import (
    serialize_event,
    serialize_plan,
    serialize_token_usage,
    serialize_tool_result,
    serialize_transcript,
)

# The root every frame is serialized against.  Nothing under it exists on
# disk: what the root decides is the shape of a call's resolved ``path``,
# and the same one has to reach a live frame and a replayed entry.
ROOT = Path("/repo")


def _plan() -> Plan:
    return Plan(
        id="plan-1",
        title="Refactor thing",
        summary="Move the logic.",
        steps=[
            PlanStep(
                id="step-1",
                description="Read the code",
                tool_calls_expected=["read_file"],
                files_affected=["src/a.py"],
            ),
        ],
        rationale="Cleaner.",
        risks=["Risky"],
        estimated_files_touched=2,
    )


class TestSerializeEvent:
    """Frame type + payload for every event class."""

    def test_content_delta(self):
        assert serialize_event(ContentDelta(text_delta="hi"), ROOT) == {
            "type": "content_delta",
            "text_delta": "hi",
        }

    def test_reasoning_delta(self):
        # Same payload key as content_delta — the frame type carries the
        # kind, the key is named after the shared ``text`` slot it feeds.
        assert serialize_event(ReasoningDelta(text_delta="hmm"), ROOT) == {
            "type": "reasoning_delta",
            "text_delta": "hmm",
        }

    def test_tool_use_start(self):
        """The provider phase is identity only — no input, no resolution."""
        assert serialize_event(ToolUseStart(
            tool_id="t1", tool_name="read_file",
        ), ROOT) == {
            "type": "tool_use_start",
            "tool_id": "t1",
            "tool_name": "read_file",
        }

    def test_tool_call_start(self):
        frame = serialize_event(ToolCallStart(
            tool_id="t1", tool_name="read_file",
            input={"path": "a.py"},
        ), ROOT)
        assert frame == {
            "type": "tool_call_start",
            "tool_id": "t1",
            "tool_name": "read_file",
            "input": {"path": "a.py"},
            # ``path`` is not the parameter this card reads; the call names
            # no file, and the card says so from the start.
            "path": None,
            "path_abs": None,
            "path_named": None,
            "path_symlink": False,
        }

    def test_tool_call_start_names_the_file_before_the_gate(self):
        """The loop yields the start frame with the input already complete
        and before any permission gate, so the card can name the file — and
        mark it external — while the call is still waiting to run."""
        frame = serialize_event(ToolCallStart(
            tool_id="t1", tool_name="read_file",
            input={"file_path": "/repo/src/a.py"},
        ), ROOT)
        assert frame["path"] == "src/a.py"
        assert frame["path_abs"] == "/repo/src/a.py"
        assert frame["path_named"] == "/repo/src/a.py"
        outside = serialize_event(ToolCallStart(
            tool_id="t2", tool_name="read_file",
            input={"file_path": "/etc/passwd"},
        ), ROOT)
        assert outside["path"] is None
        assert outside["path_abs"] == str(Path("/etc/passwd").resolve())
        # What the card shows is the path the call named — on macOS
        # resolving it would read back as ``/private/etc/passwd``.
        assert outside["path_named"] == "/etc/passwd"

    def test_the_link_marker_rides_where_the_name_reaches_through_one(
        self, tmp_path
    ):
        """``/repo``-style fixtures cannot say this: whether a path goes
        through a link is a fact about the filesystem."""

        def frame(raw: str) -> dict:
            return serialize_event(
                ToolCallStart(
                    tool_id="t1", tool_name="read_file",
                    input={"file_path": raw},
                ),
                tmp_path,
            )

        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "a.py").write_text("x = 1\n")
        link = tmp_path / "linked"
        link.symlink_to(tmp_path / "real")

        through_link = frame(str(link / "a.py"))
        assert through_link["path_symlink"] is True
        assert through_link["path_named"] == str(link / "a.py")
        assert through_link["path_abs"] == str(tmp_path / "real" / "a.py")

        straight = frame(str(tmp_path / "real" / "a.py"))
        assert straight["path_symlink"] is False

    def test_tool_call_end(self):
        result = ToolResult(
            tool_id="t1", tool_name="read_file", success=True,
            output="content", checkpoint_id="ck-1",
            metadata={"path": "a.py"},
        )
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file",
            input={"file_path": "a.py"}, result=result,
        ), ROOT)
        assert frame == {
            "type": "tool_call_end",
            "tool_id": "t1",
            "tool_name": "read_file",
            "input": {"file_path": "a.py"},
            # The call's file in all three spellings: what a tab opens
            # (relative to the served root), where the file is, and the name
            # the card shows it under.
            "path": "a.py",
            "path_abs": "/repo/a.py",
            "path_named": "/repo/a.py",
            "path_symlink": False,
            "result": {
                "success": True,
                "output": "content",
                "error": None,
                "checkpoint_id": "ck-1",
                "metadata": {"path": "a.py"},
            },
        }

    def test_tool_call_end_without_result(self):
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file", input={},
        ), ROOT)
        assert frame["result"] is None

    def test_tool_call_end_names_the_file_it_was_about(self):
        """The card opens the file, so the frame carries where it landed —
        the root-relative spelling a tab holds and the absolute one the
        title offers on hover, whichever way the call spelled it."""
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file",
            input={"file_path": "/repo/src/a.py"},
        ), ROOT)
        assert frame["path"] == "src/a.py"
        assert frame["path_abs"] == "/repo/src/a.py"

    def test_tool_call_end_outside_the_root_names_nothing_to_open(self):
        """A file outside the served root has no tab to open in — but it
        still has a location, and that is what the card shows."""
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="read_file",
            input={"file_path": "/etc/passwd"},
        ), ROOT)
        assert frame["path"] is None
        assert frame["path_abs"] == str(Path("/etc/passwd").resolve())

    def test_tool_call_end_without_a_file_path(self):
        frame = serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="shell", input={"command": "ls"},
        ), ROOT)
        assert frame["path"] is None
        assert frame["path_abs"] is None

    def test_quiet_tool_call_gets_no_frame(self):
        """The plan bookkeeping call is hidden by its own declaration: the
        browser learns what it did from the ``plan_step_update`` frames,
        so a card for the call itself would only repeat them."""
        assert serialize_event(ToolUseStart(
            tool_id="t1", tool_name="plan_update",
        ), ROOT) is None
        assert serialize_event(ToolCallStart(
            tool_id="t1", tool_name="plan_update",
            input={"step_id": "step-1"},
        ), ROOT) is None
        assert serialize_event(ToolCallEnd(
            tool_id="t1", tool_name="plan_update",
            input={"step_id": "step-1"},
            result=ToolResult(
                tool_id="t1", tool_name="plan_update", success=True,
                output="Step step-1 → in_progress.",
            ),
        ), ROOT) is None

    def test_plan_proposed(self):
        plan = _plan()
        frame = serialize_event(PlanProposed(plan=plan), ROOT)
        assert frame == {
            "type": "plan_proposed",
            "plan": serialize_plan(plan),
        }
        assert frame["plan"]["steps"] == [{
            "id": "step-1",
            "description": "Read the code",
            "tool_calls_expected": ["read_file"],
            "files_affected": ["src/a.py"],
        }]
        # The browser's document view reads the display markdown, not the
        # prompt spelling — pin the shape, not just the key's presence.
        assert frame["plan"]["markdown"].startswith("## Plan: Refactor thing")

    def test_plan_step_update(self):
        frame = serialize_event(PlanStepUpdate(steps=[
            ("step-1", "Read the code", PlanStepStatus.IN_PROGRESS),
            ("step-2", "Edit", PlanStepStatus.PENDING),
        ]), ROOT)
        assert frame == {
            "type": "plan_step_update",
            "steps": [
                ["step-1", "Read the code", "in_progress"],
                ["step-2", "Edit", "pending"],
            ],
        }

    def test_agent_paused(self):
        assert serialize_event(AgentPaused(
            prompt="Allow write_file(x)?", choices=["approve", "deny"],
        ), ROOT) == {
            "type": "agent_paused",
            "prompt": "Allow write_file(x)?",
            "choices": ["approve", "deny"],
        }

    def test_agent_finished(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=5,
            cache_read_tokens=3, cache_creation_tokens=2,
        )
        frame = serialize_event(AgentFinished(
            reason="LLM finished its turn.", usage=usage,
        ), ROOT)
        assert frame == {
            "type": "agent_finished",
            "reason": "LLM finished its turn.",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 2,
                "reasoning_tokens": 0,
            },
        }

    def test_agent_finished_without_usage(self):
        frame = serialize_event(AgentFinished(
            reason="Plan rejected by user.",
        ), ROOT)
        assert frame["usage"] is None

    def test_recoverable_error(self):
        assert serialize_event(RecoverableAgentError(
            message="API hiccup",
        ), ROOT) == {"type": "recoverable_error", "message": "API hiccup"}

    def test_fatal_error(self):
        assert serialize_event(FatalAgentError(
            message="Unparseable plan",
        ), ROOT) == {"type": "fatal_error", "message": "Unparseable plan"}

    def test_unknown_event_returns_none(self):
        class UnknownEvent(AgentEvent):
            pass

        assert serialize_event(UnknownEvent(), ROOT) is None


class TestSerializeTranscript:
    """Stored-message flattening for hello / REST replay."""

    @staticmethod
    def _tool_pair(
        tool_id: str,
        name: str = "read_file",
        tool_input: dict | None = None,
        result: str | None = "content",
        is_error: bool = False,
    ) -> list[Message]:
        """The stored shape of a tool call: an assistant tool_use message
        followed by a tool message with its result."""
        return [
            Message.assistant([
                MessageBlock.tool_use_block(
                    tool_id, name, tool_input or {"file_path": "a.py"},
                ),
            ]),
            Message.tool([
                MessageBlock.tool_result_block(
                    tool_id, result or "", is_error=is_error,
                ),
            ]),
        ]

    def test_system_message_skipped(self):
        messages = [
            Message.system("You are helpful."),
            Message.user("hi"),
            Message.assistant([MessageBlock.content_block("hello")]),
        ]
        assert serialize_transcript(messages, ROOT) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_tool_call_paired_into_single_entry(self):
        messages = [
            Message.user("read it"),
            *self._tool_pair("t1"),
            Message.assistant([MessageBlock.content_block("done")]),
        ]
        assert serialize_transcript(messages, ROOT) == [
            {"role": "user", "content": "read it"},
            {
                "role": "tool",
                "tool_id": "t1",
                "tool_name": "read_file",
                "input": {"file_path": "a.py"},
                # Recomputed here rather than replayed: the stored
                # transcript keeps no resolved path.
                "path": "a.py",
                "path_abs": "/repo/a.py",
                "path_named": "/repo/a.py",
                "path_symlink": False,
                "result": {
                    "success": True,
                    "output": "content",
                    "error": None,
                    "checkpoint_id": None,
                    "metadata": None,
                },
            },
            {"role": "assistant", "content": "done"},
        ]

    def test_failed_tool_result_maps_error(self):
        messages = self._tool_pair(
            "t1", name="write_file",
            tool_input={"file_path": "b.py"}, result="denied", is_error=True,
        )
        entry = serialize_transcript(messages, ROOT)[0]
        assert entry["result"] == {
            "success": False,
            "output": None,
            "error": "denied",
            "checkpoint_id": None,
            "metadata": None,
        }

    def test_use_without_result_replays_as_cancelled(self):
        # A turn cancelled mid-execution persists the use but no result.
        messages = [Message.assistant([
            MessageBlock.tool_use_block("t1", "write_file", {}),
        ])]
        entry = serialize_transcript(messages, ROOT)[0]
        assert entry["tool_id"] == "t1"
        assert entry["result"] is None

    def test_tool_message_not_emitted_on_its_own(self):
        # The tool role carries only results; they attach to the use site.
        messages = [Message.tool([
            MessageBlock.tool_result_block("t1", "content"),
        ])]
        assert serialize_transcript(messages, ROOT) == []

    def test_assistant_text_and_use_keep_live_order(self):
        messages = [Message.assistant([
            MessageBlock.content_block("checking…"),
            MessageBlock.tool_use_block("t1", "read_file", {"file_path": "a.py"}),
        ])]
        assert [e["role"] for e in serialize_transcript(messages, ROOT)] == [
            "assistant", "tool",
        ]

    def test_assistant_reasoning_attached_verbatim(self):
        messages = [Message.assistant([
            MessageBlock.reasoning_block("step 1: "),
            MessageBlock.reasoning_block("step 2"),
            MessageBlock.content_block("the answer"),
        ])]
        assert serialize_transcript(messages, ROOT) == [{
            "role": "assistant",
            "content": "the answer",
            "reasoning": "step 1: step 2",
        }]

    def test_reasoning_key_absent_without_reasoning(self):
        entry = serialize_transcript([
            Message.assistant([MessageBlock.content_block("plain")]),
        ], ROOT)[0]
        assert entry == {"role": "assistant", "content": "plain"}
        assert "reasoning" not in entry

    def test_thought_only_entry_replays_with_its_tool(self):
        """A message that reasoned and went straight to a tool call has no
        content — the entry must still replay, or the card is lost."""
        messages = [
            Message.user("read it"),
            Message.assistant([
                MessageBlock.reasoning_block("I need the file first."),
                MessageBlock.tool_use_block(
                    "t1", "read_file", {"file_path": "a.py"},
                ),
            ]),
            Message.tool([
                MessageBlock.tool_result_block("t1", "content"),
            ]),
        ]
        entries = serialize_transcript(messages, ROOT)
        assert [e["role"] for e in entries] == ["user", "assistant", "tool"]
        assert entries[1] == {
            "role": "assistant",
            "content": "",
            "reasoning": "I need the file first.",
        }

    def test_quiet_tool_call_is_not_replayed(self):
        """The plan bookkeeping call stays in the stored transcript — the
        model's context needs it — but no card is rebuilt for it on a
        reload: its statuses arrive as plan step updates instead.  The
        prose the model wrote alongside it is untouched."""
        messages = [
            Message.assistant([
                MessageBlock.content_block("marking step 1 in progress"),
                MessageBlock.tool_use_block(
                    "t1", "plan_update",
                    {"step_id": "step-1", "status": "in_progress"},
                ),
            ]),
            Message.tool([
                MessageBlock.tool_result_block(
                    "t1", "Step step-1 → in_progress.",
                ),
            ]),
        ]
        assert serialize_transcript(messages, ROOT) == [{
            "role": "assistant",
            "content": "marking step 1 in progress",
        }]

    def test_reasoning_entry_ordered_before_its_tool_entries(self):
        messages = [Message.assistant([
            MessageBlock.reasoning_block("cot"),
            MessageBlock.content_block("done"),
            MessageBlock.tool_use_block("t1", "read_file", {"file_path": "a.py"}),
        ])]
        entries = serialize_transcript(messages, ROOT)
        assert entries[0]["reasoning"] == "cot"
        assert entries[0]["content"] == "done"
        assert entries[1]["role"] == "tool"

    def test_cancel_marker_entry_gets_fold_key(self):
        # The turn-cancelled repair message is model scaffolding — flagged so
        # the frontend renders a fold line, content kept for consumers.
        marker = "[The previous turn was cancelled by the user.]"
        messages = [
            Message.user("hi"),
            Message.user(marker),
        ]
        assert serialize_transcript(messages, ROOT) == [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": marker, "fold": "cancelled"},
        ]

    def test_compacted_marker_entry_gets_fold_key(self):
        marker = (
            "[Compacted history — summary of the conversation so far]\n\n"
            "user wanted x; agent did y."
        )
        entry = serialize_transcript([Message.user(marker)], ROOT)[0]
        assert entry["fold"] == "compacted"
        assert entry["content"] == marker

    def test_bracket_text_without_marker_stays_fold_free(self):
        # Only the known repair prefixes fold — ordinary bracketed text the
        # user actually typed must keep replaying as a user entry.
        entry = serialize_transcript([Message.user("[Just a bracket note]")], ROOT)[0]
        assert entry == {"role": "user", "content": "[Just a bracket note]"}


class TestSerializePayloads:
    """Explicit field maps for Plan, ToolResult, TokenUsage."""

    def test_serialize_plan_maps_every_field(self):
        plan = _plan()
        assert serialize_plan(plan) == {
            "id": "plan-1",
            "title": "Refactor thing",
            "summary": "Move the logic.",
            "steps": [step.to_dict() for step in plan.steps],
            "rationale": "Cleaner.",
            "risks": ["Risky"],
            "estimated_files_touched": 2,
            "markdown": plan.format_for_display(),
        }

    def test_serialize_tool_result_with_error(self):
        result = ToolResult(
            tool_id="t1", tool_name="write_file", success=False,
            output="", error="User denied permission.",
        )
        assert serialize_tool_result(result) == {
            "success": False,
            "output": "",
            "error": "User denied permission.",
            "checkpoint_id": None,
            "metadata": {},
        }

    def test_serialize_tool_result_none(self):
        assert serialize_tool_result(None) is None

    def test_serialize_token_usage_none(self):
        assert serialize_token_usage(None) is None

    def test_serialize_token_usage_maps_reasoning_tokens(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=60, cache_read_tokens=3,
            cache_creation_tokens=2, reasoning_tokens=40,
        )
        assert serialize_token_usage(usage) == {
            "input_tokens": 10,
            "output_tokens": 60,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 2,
            "reasoning_tokens": 40,
        }
