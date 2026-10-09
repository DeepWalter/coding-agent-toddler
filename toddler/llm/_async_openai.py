"""AsyncOpenAI import helper — conditionally provides real or dummy client.

When ``TEST=cli``, returns a :class:`DummyAsyncOpenAI` that simulates
``chat.completions.create`` with canned responses — streamed when a stream
is asked for, folded into one complete response otherwise.  Otherwise
returns the real ``openai.AsyncOpenAI``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

__all__ = ["AsyncOpenAI"]

# ---------------------------------------------------------------------------
# Paths the canned responses reach for — this file is used by "text" /
# "write", the repo README by "read", the repo root by "glob" / "grep"
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
_THIS_CONTENT = _THIS_FILE.read_text(encoding="utf-8")
_PROJECT_ROOT = _THIS_FILE.parents[2]
_README_FILE = _PROJECT_ROOT / "README.md"

# ---------------------------------------------------------------------------
# Plan-workflow markers — the canned plan responses key on the prompts the
# plan workflow sends, not on a typed keyword: the proposal arrives as
# ``plan_proposal_prompt()``'s text, execution opens with
# ``Plan.format_for_prompt()``'s text, and progress arrives as the
# ``plan_update`` tool's own output (the ``"Step <id> → <status>."`` line
# ``PlanUpdateTool.execute`` returns).  Each marker names its producer, so
# a prompt rewording points here.
# ---------------------------------------------------------------------------

_PLAN_PROPOSAL_MARKER = "propose a concrete execution plan"
_PLAN_APPROVED_MARKER = "I have reviewed and approved the following plan"

# One step line of the approved-plan text, e.g. ``  step-1: <description>``.
_PLAN_STEP_LINE_RE = re.compile(r"^\s*(step-\d+):", re.MULTILINE)

# A ``plan_update`` tool result, e.g. ``Step step-1 → completed.``
_PLAN_UPDATE_RE = re.compile(
    r"^Step (?P<step_id>\S+) → (?P<status>\w+)\.$"
)

# The canned plan the dummy proposes — two steps, so the execution phase
# shows a step starting, completing and a closing summary.  Step ids are
# position-derived (``step-1``, ``step-2``) by ``Plan.from_json``, but the
# canned execution beats read them back from the approved-plan text rather
# than assume them.
_PLAN_PROPOSAL = {
    "title": "Dummy plan — two canned steps",
    "summary": (
        "A canned two-step plan the dummy proposes so the plan workflow "
        "can be exercised end to end: approval, step updates, summary."
    ),
    "steps": [
        {
            "description": "Run dummy step 1 and report its output",
            "tool_calls_expected": ["plan_update"],
            "files_affected": [],
        },
        {
            "description": "Run dummy step 2 and report its output",
            "tool_calls_expected": ["plan_update"],
            "files_affected": [],
        },
    ],
    "rationale": (
        "Two steps are enough to show a step starting, completing and "
        "the closing summary that follows the last one."
    ),
    "risks": ["None — every call this plan makes is canned."],
    "estimated_files_touched": 0,
}

# ---------------------------------------------------------------------------
# Guard: which AsyncOpenAI to export
# ---------------------------------------------------------------------------

_TEST = os.environ.get("TEST", "")

if _TEST == "cli":

    class AsyncOpenAI:
        """Dummy AsyncOpenAI for CLI testing.

        Simulates ``chat.completions.create`` (``stream=True`` only) with
        canned responses keyed on the last user/tool message.
        """

        def __init__(
            self, base_url=None, api_key=None, http_client=None
        ) -> None:
            self.base_url = base_url
            self.api_key = api_key
            self.chat = self._Chat()

        class _Chat:
            """Fake ``chat`` namespace."""

            @property
            def completions(self):
                return self._Completions()

            class _Completions:
                """Fake ``chat.completions`` namespace."""

                async def create(
                    self,
                    model,
                    messages,
                    tools=None,
                    max_tokens=None,
                    max_completion_tokens=None,
                    temperature=0.0,
                    reasoning_effort=None,
                    response_format=None,
                    extra_body=None,
                    stream=False,
                    stream_options=None,
                ):
                    """Simulate ``chat.completions.create``.

                    The canned response is chosen by inspecting the last
                    message.  ``stream=True`` returns the chunk iterable;
                    ``stream=False`` folds that same chunk list into one
                    complete response — the plan workflow's exploration
                    and proposal calls are non-streaming, so both modes
                    answer from the one dispatch.

                    * ``"plan"`` — stream a text intro and finish,
                      ending the plan workflow's exploration phase so the
                      proposal comes next.
                    * ``"reason"`` — stream reasoning content before the
                      answer text.
                    * ``"text"`` — stream the content of *this* file.
                    * ``"read"`` — emit a ``read_file`` tool call that reads
                      the repo's ``README.md``.
                    * ``"glob"`` — emit a ``glob`` tool call that lists
                      every Python file in the project.
                    * ``"grep"`` — emit a ``grep`` tool call that finds
                      every ``BaseTool`` subclass.
                    * ``"shell"`` — emit a multi-line ``shell`` tool call,
                      every command in it safe.
                    * ``"write"`` — emit a ``write_file`` tool call that
                      writes this file's content to
                      ``~/.toddler/test_write.py``.
                    * the plan proposal prompt — answer with the canned
                      two-step plan JSON.
                    * the approved-plan execution prompt — open the plan
                      by starting its first step.
                    * a ``plan_update`` tool result — answer with the
                      canned plan's next beat: the step's marked text
                      plus its completion call, the next step's start, or
                      the closing summary.
                    * ``role == "tool"`` — stream a short acknowledgment and
                      finish.

                    The request-shaping parameters (token budget, effort,
                    format) are accepted and ignored — the canned responses
                    do not vary with them.
                    """
                    if stream:
                        return _DummyStream(messages)
                    return _DummyStream.response(messages)


    # ======================================================================
    # DummyStream — the async iterable returned by create()
    # ======================================================================

    class _DummyStream:
        """Async iterable that yields simulated OpenAI streaming chunks,
        plus the fold that turns its chunks into a complete response.

        All chunk materialization happens synchronously in ``__aiter__``;
        only the per-chunk delay lives in ``__anext__``.
        """

        def __init__(self, messages: list[dict]) -> None:
            self._chunks: list[SimpleNamespace] = []
            self._idx = 0
            self._messages = messages

        # -- async iterator protocol ---------------------------------------

        def __aiter__(self):
            self._chunks = list(self._build_chunks())
            self._idx = 0
            return self

        async def __anext__(self):
            if self._idx >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._idx]
            self._idx += 1
            await asyncio.sleep(0.1)  # yield to event loop between chunks
            return chunk

        # -- dispatch -------------------------------------------------------

        def _build_chunks(self):
            """Inspect the last message and return the matching chunk list.

            The plan branches come first: their markers are structural
            (the prompts the plan workflow sends), so they hold whatever
            keyword the turn was started with — and unlike a keyword
            match they also fire on the non-streaming calls the workflow
            makes.
            """
            last_msg = self._messages[-1] if self._messages else {}
            role = last_msg.get("role", "")
            content = last_msg.get("content") or ""

            if role == "tool":
                return self._chunks_for_tool_result(last_msg)
            elif _PLAN_PROPOSAL_MARKER in content:
                return self._chunks_for_plan_proposal()
            elif _PLAN_APPROVED_MARKER in content:
                return self._chunks_for_plan_start()
            elif content == "reason":
                return self._chunks_for_reasoning()
            elif content == "plan":
                return self._chunks_for_plan()
            elif content == "text":
                return self._chunks_for_text()
            elif content == "read":
                return self._chunks_for_read()
            elif content == "glob":
                return self._chunks_for_glob()
            elif content == "grep":
                return self._chunks_for_grep()
            elif content == "shell":
                return self._chunks_for_shell()
            elif content == "write":
                return self._chunks_for_write()
            else:
                return self._chunks_for_unknown(content)

        # -- canned responses -----------------------------------------------

        @staticmethod
        def _chunks_for_text():
            """Stream a link to *this* file, then its content as a fenced
            python block."""
            try:
                rel = _THIS_FILE.relative_to(Path.cwd())
            except ValueError:
                # File outside cwd — link to its absolute path instead.
                rel = _THIS_FILE
            link = rel.as_posix()
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    f"Here is the content of [{link}]({link}):\n\n",
                ),
                ("text", "```python\n"),
            ]
            for line in _THIS_CONTENT.splitlines(keepends=True):
                blocks.append(("text", line))
            blocks.append(("text", "```\n"))
            blocks.append(("finish", "stop"))
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_reasoning():
            """Stream a short chain of thought, then the answer text.

            Thinking-mode demo: the reasoning arrives as several
            ``reasoning_content`` fragments before any ``content``, and the
            finish chunk itemizes the reasoning tokens in usage.
            """
            blocks: list[tuple[str, str | None]] = [
                ("reasoning", "The user typed 'reason', which asks for a "),
                ("reasoning", "thinking-mode reply: streams the reasoning "),
                ("reasoning", "first, then the answer.\n"),
                (
                    "text",
                    "That was my reasoning, streamed before this answer.\n",
                ),
                ("finish", "stop"),
            ]
            return _DummyStream._materialize(
                blocks, completion_tokens=260, reasoning_tokens=60,
            )

        @staticmethod
        def _chunks_for_read():
            """Emit a text intro, then a ``read_file`` tool call to read
            the repo's ``README.md``."""
            args = json.dumps(
                {
                    "file_path": str(_README_FILE),
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me read the README.\n\n",
                ),
                ("tool_name", "read_file"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_glob():
            """Emit a text intro, then a ``glob`` tool call listing every
            Python file in the project."""
            args = json.dumps(
                {
                    "description": (
                        "find every Python file in the project"
                    ),
                    "pattern": "**/*.py",
                    "path": str(_PROJECT_ROOT),
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me find every Python file in the project.\n\n",
                ),
                ("tool_name", "glob"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_grep():
            """Emit a text intro, then a ``grep`` tool call finding every
            ``BaseTool`` subclass.

            The pattern brackets every paren and quantifies with ``*``:
            the tool shells out to ``grep`` without ``-E``, where ``\\w+``
            is a word char plus a *literal* ``+`` and ``\\(`` opens a
            group — both silently match nothing.  This spelling is also
            what the pure-Python fallback reads correctly.
            """
            args = json.dumps(
                {
                    "description": (
                        "find every tool class in the project"
                    ),
                    "pattern": "class \\w*[(]BaseTool[)]",
                    "path": str(_PROJECT_ROOT),
                    "include": "*.py",
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me find every tool class.\n\n",
                ),
                ("tool_name", "grep"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_shell():
            """Emit a text intro, then a multi-line ``shell`` tool call.

            Every command in the string is on the safe list, so the call
            still auto-approves: classification reads each segment, and a
            safe prefix cannot launder what follows it.
            """
            args = json.dumps(
                {
                    "description": (
                        "list the directory, the branch state and the "
                        "Python file count"
                    ),
                    "command": (
                        "echo hello world\n"
                        "ls -la\n"
                        "git status --short\n"
                        "find . -name '*.py' -type f | wc -l"
                    ),
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me look around the repo.\n\n",
                ),
                ("tool_name", "shell"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_write():
            """Emit a text intro, then a ``write_file`` tool call to copy
            this file to ``~/.toddler/test_write.py``."""
            args = json.dumps(
                {
                    "description": (
                        "copy this file to ~/.toddler/test_write.py"
                    ),
                    "file_path": "~/.toddler/test_write.py",
                    "content": _THIS_CONTENT,
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "I'll copy this file to "
                    "~/.toddler/test_write.py.\n\n",
                ),
                ("tool_name", "write_file"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        # -- canned plan workflow -------------------------------------------

        @staticmethod
        def _chunks_for_plan():
            """Stream a text intro and finish — the plan workflow's
            exploration phase.

            The stop is what hands control to the proposal: a tool call
            here would be answered by the generic acknowledgment, which
            reads like the turn ending with nothing planned.
            """
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me put together a plan for this request.\n\n",
                ),
                ("finish", "stop"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_plan_proposal():
            """Answer the plan proposal prompt with the canned plan JSON.

            The proposal call is non-streaming, so these blocks normally
            reach the caller through :meth:`response` rather than
            :meth:`__aiter__` — served from the one dispatch either way.
            """
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    json.dumps(_PLAN_PROPOSAL, ensure_ascii=False, indent=2),
                ),
                ("finish", "stop"),
            ]
            return _DummyStream._materialize(blocks)

        def _chunks_for_plan_start(self):
            """Open the approved plan by starting its first step."""
            step_ids = self._plan_step_ids()
            if not step_ids:
                blocks: list[tuple[str, str | None]] = [
                    (
                        "text",
                        "[DummyAsyncOpenAI] The approved-plan prompt "
                        "carried no step lines.\n",
                    ),
                    ("finish", "stop"),
                ]
                return _DummyStream._materialize(blocks)
            return self._chunks_for_plan_call(step_ids[0], "in_progress")

        def _plan_step_ids(self) -> list[str]:
            """Return the approved plan's step ids, in plan order.

            Parsed from the approved-plan message rather than assumed:
            the canned execution beats then follow whatever plan the
            session actually approved.
            """
            for msg in reversed(self._messages):
                content = msg.get("content") or ""
                if (
                    msg.get("role") == "user"
                    and _PLAN_APPROVED_MARKER in content
                ):
                    return _PLAN_STEP_LINE_RE.findall(content)
            return []

        def _chunks_for_plan_update(self, step_id: str, status: str):
            """Return the canned plan's next beat after a ``plan_update``.

            ``in_progress`` answers with the step's marked text plus its
            completion call — the leading ``[step <id>]`` names the step
            the update targets, per the tool's usage note.  A
            ``completed`` answers with the next step's start, or, once
            the last step is done, the closing summary.  ``None`` when
            the line names a step the approved plan does not have, so the
            caller falls back to the generic acknowledgment.
            """
            step_ids = self._plan_step_ids()
            if step_id not in step_ids:
                return None

            if status == "in_progress":
                index = step_ids.index(step_id) + 1
                # The step's text and its completion share one response:
                # text alone would end the execution turn, and the closing
                # summary still follows the completion's tool result, so
                # the last snapshot lands before the summary either way.
                blocks: list[tuple[str, str | None]] = [
                    (
                        "text",
                        f"[{step_id}] Dummy step {index} work output — "
                        "the canned text for this step.\n\n",
                    ),
                    ("tool_name", "plan_update"),
                    (
                        "tool_args",
                        self._plan_call_args(step_id, "completed"),
                    ),
                    ("finish", "tool_calls"),
                ]
                return _DummyStream._materialize(blocks)

            if status == "completed":
                remaining = step_ids[step_ids.index(step_id) + 1:]
                if remaining:
                    return self._chunks_for_plan_call(
                        remaining[0], "in_progress",
                    )
                blocks = [
                    (
                        "text",
                        "Both dummy plan steps completed — the approved "
                        "plan ran end to end. Is there anything else you "
                        "need?\n",
                    ),
                    ("finish", "stop"),
                ]
                return _DummyStream._materialize(blocks)

            return None

        @staticmethod
        def _chunks_for_plan_call(step_id: str, status: str):
            """Emit a bare ``plan_update`` call — no text alongside it."""
            blocks: list[tuple[str, str | None]] = [
                ("tool_name", "plan_update"),
                ("tool_args", _DummyStream._plan_call_args(step_id, status)),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _plan_call_args(step_id: str, status: str) -> str:
            """Serialize ``plan_update`` arguments for *step_id* / *status*.

            The required call-description param states the update itself,
            as the tool's usage note asks.
            """
            return json.dumps(
                {
                    "description": f"mark {step_id} {status}",
                    "step_id": step_id,
                    "status": status,
                },
                ensure_ascii=False,
            )

        # -- tool results and fallback --------------------------------------

        def _chunks_for_tool_result(self, last_msg: dict):
            """Answer a tool result.

            A ``plan_update`` result drives the canned plan's next beat
            (:meth:`_chunks_for_plan_update`); every other result gets a
            short acknowledgment.
            """
            tool_content = last_msg.get("content", "") or ""
            match = _PLAN_UPDATE_RE.match(tool_content)
            if match is not None:
                plan_chunks = self._chunks_for_plan_update(
                    match.group("step_id"), match.group("status"),
                )
                if plan_chunks is not None:
                    return plan_chunks
            preview = (
                tool_content[:80] + "..."
                if len(tool_content) > 80
                else tool_content
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    f"Got tool result: {preview}\n\n"
                    "The operation completed successfully. "
                    "Is there anything else you need?",
                ),
                ("finish", "stop"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_unknown(content: str):
            """Fallback for unrecognized input."""
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    f"[DummyAsyncOpenAI] Unrecognised input: {content!r}",
                ),
                ("finish", "stop"),
            ]
            return _DummyStream._materialize(blocks)

        # -- materializer ---------------------------------------------------

        @staticmethod
        def _materialize(
            blocks: list[tuple[str, str | None]],
            *,
            prompt_tokens: int = 100,
            completion_tokens: int = 200,
            reasoning_tokens: int = 0,
        ) -> list[SimpleNamespace]:
            """Convert *blocks* into a list of chunk ``SimpleNamespace``
            objects.

            Each block is a ``(kind, payload)`` pair:

            ===================  ===========================================
            ``"text"``           text content to stream
            ``"reasoning"``      reasoning content to stream (thinking mode)
            ``"tool_name"``      name of the tool being called
            ``"tool_args"``      JSON fragment of the tool arguments
            ``"finish"``         finish reason string (e.g. ``"stop"``)
            ===================  ===========================================
            """
            chunks: list[SimpleNamespace] = []
            tool_id: str | None = None
            tool_idx = 0

            for kind, payload in blocks:
                if kind == "text":
                    delta = SimpleNamespace(content=payload, tool_calls=None)
                    choice = SimpleNamespace(delta=delta, finish_reason=None)
                    chunks.append(SimpleNamespace(choices=[choice], usage=None))

                elif kind == "reasoning":
                    delta = SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        reasoning_content=payload,
                    )
                    choice = SimpleNamespace(delta=delta, finish_reason=None)
                    chunks.append(SimpleNamespace(choices=[choice], usage=None))

                elif kind == "tool_name":
                    tool_id = f"fake_{uuid.uuid4().hex[:12]}"
                    func = SimpleNamespace(name=payload, arguments=None)
                    tc = SimpleNamespace(index=tool_idx, id=tool_id, function=func)
                    delta = SimpleNamespace(content=None, tool_calls=[tc])
                    choice = SimpleNamespace(delta=delta, finish_reason=None)
                    chunks.append(SimpleNamespace(choices=[choice], usage=None))

                elif kind == "tool_args":
                    func = SimpleNamespace(name=None, arguments=payload)
                    tc = SimpleNamespace(index=tool_idx, id=None, function=func)
                    delta = SimpleNamespace(content=None, tool_calls=[tc])
                    choice = SimpleNamespace(delta=delta, finish_reason=None)
                    chunks.append(SimpleNamespace(choices=[choice], usage=None))

                elif kind == "finish":
                    # The details object always ships, as DeepSeek does —
                    # reasoning tokens are a subset of the completion count.
                    usage = SimpleNamespace(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        completion_tokens_details=SimpleNamespace(
                            reasoning_tokens=reasoning_tokens,
                        ),
                    )
                    delta = SimpleNamespace(content=None, tool_calls=None)
                    choice = SimpleNamespace(delta=delta, finish_reason=payload)
                    chunks.append(SimpleNamespace(choices=[choice], usage=usage))

            return chunks

        @classmethod
        def response(cls, messages: list[dict]) -> SimpleNamespace:
            """Fold the canned chunks for *messages* into one complete
            response object — the shape a non-streaming ``create`` returns.

            Runs the same dispatch the streaming path uses and reverses
            each chunk delta — text concatenates, tool-call fragments
            merge by index, the finish chunk supplies the reason and
            usage — so a canned response never forks per mode.  The
            result mirrors what the OpenAI SDK returns, which is what the
            provider's non-streaming path reads.
            """
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict] = []
            finish_reason = "stop"
            usage = None

            for chunk in cls(messages)._build_chunks():
                choice = chunk.choices[0]
                delta = choice.delta

                content_parts.append(delta.content or "")
                reasoning_parts.append(
                    getattr(delta, "reasoning_content", None) or ""
                )
                for tc in delta.tool_calls or []:
                    cls._merge_tool_delta(tool_calls, tc)

                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason
                    usage = chunk.usage

            message = SimpleNamespace(
                content="".join(content_parts) or None,
                reasoning_content="".join(reasoning_parts) or None,
                tool_calls=[
                    SimpleNamespace(
                        id=tc["id"],
                        type="function",
                        function=SimpleNamespace(
                            name=tc["name"], arguments=tc["arguments"],
                        ),
                    )
                    for tc in tool_calls
                ] or None,
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=message, finish_reason=finish_reason,
                    )
                ],
                usage=usage,
            )

        @staticmethod
        def _merge_tool_delta(slots: list[dict], tc) -> None:
            """Merge one streaming tool-call fragment into *slots*.

            A call's fragments arrive split by index and field — the id
            and the name only in the first fragment, the argument JSON
            split across later ones — so each fragment fills its slot
            and the fold reads complete calls back.
            """
            while len(slots) <= tc.index:
                slots.append({"id": "", "name": None, "arguments": ""})
            slot = slots[tc.index]
            if tc.id:
                slot["id"] = tc.id
            if tc.function is None:
                return
            if tc.function.name:
                slot["name"] = tc.function.name
            if tc.function.arguments:
                slot["arguments"] += tc.function.arguments

else:
    from openai import AsyncOpenAI  # noqa: F401
