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
# Plan-workflow markers — the canned plan responses key on the tools the
# plan workflow calls, not on a typed keyword: the proposal is a
# ``propose_plan`` call, approval is the tool result
# ``ProposePlanTool.execute`` returns, and progress is the ``todo_write``
# results the session echoes back.  Each marker names its producer, so a
# message rewording points here.
# ---------------------------------------------------------------------------

# The approval ack emitted by ProposePlanTool.execute.
_PLAN_APPROVED_MARKER = "The user approved this plan"

# A todo_write result, e.g. "Todo list updated (2 items)."
_TODO_RESULT_MARKER = "Todo list updated"

# The canned plan the dummy proposes — two steps, so the execution phase
# shows a todo item starting, completing and a closing summary.
_PLAN_PROPOSAL = {
    "title": "Dummy plan — two canned steps",
    "plan_markdown": (
        "## Dummy plan — two canned steps\n\n"
        "A canned plan the dummy proposes so the plan workflow can be "
        "exercised end to end: approval, todo progress, summary.\n\n"
        "1. Run dummy step 1 and report its output.\n"
        "2. Run dummy step 2 and report its output.\n\n"
        "**Risks**: none — every call this plan makes is canned.\n"
    ),
}

# The canned questions the dummy asks on "ask" / "question": two
# three-choice questions, recommendation first, so the tabbed card and
# the next-unanswered advance both have somewhere to go.
_ASK_USER_QUESTIONS = [
    {
        "title": "Storage",
        "content": "Where should session data live?",
        "choices": [
            {
                "title": "SQLite",
                "content": "One local file; no server to run.",
            },
            {
                "title": "Postgres",
                "content": "A shared server the team already operates.",
            },
            {
                "title": "In-memory",
                "content": "Fastest, but nothing survives a restart.",
            },
        ],
    },
    {
        "title": "Interface",
        "content": "How should the new command be exposed?",
        "choices": [
            {
                "title": "CLI flag",
                "content": "A flag on the existing command.",
            },
            {
                "title": "Slash command",
                "content": "A command typed in the REPL.",
            },
            {
                "title": "Environment variable",
                "content": "Configure it once, outside the command line.",
            },
        ],
    },
]

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
                    * ``"edit"`` — emit an ``edit_file`` tool call that
                      rewrites the canned plan's title in
                      ``~/.toddler/test_write.py``.
                    * ``"ask"`` / ``"question"`` — emit an ``ask_user``
                      tool call with two three-choice questions, so the
                      tabbed question card can be exercised.
                    * ``"plan"`` — emit a ``propose_plan`` call carrying
                      the canned markdown plan.
                    * the propose_plan approval result — start the canned
                      execution with a ``todo_write`` call.
                    * a ``todo_write`` result — advance the canned todo
                      list and finish with the closing summary.
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

            Tool results come first: the plan workflow's approval and todo
            results drive its canned beats, whatever keyword the turn was
            started with.
            """
            last_msg = self._messages[-1] if self._messages else {}
            role = last_msg.get("role", "")
            content = last_msg.get("content") or ""

            if role == "tool":
                return self._chunks_for_tool_result(last_msg)
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
            elif content == "edit":
                return self._chunks_for_edit()
            elif content in ("ask", "question"):
                return self._chunks_for_ask()
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
                ("reasoning", " # Level One title\n\n"),
                ("reasoning", " ## Level Two title\n\n"),
                ("reasoning", " ### Level Three title\n\n"),
                ("reasoning", " content with *emphasis* and **strong** and `code` and a [link](CLAUDE.md)\n"),
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
                        "rm nonexistent_file || true\n"
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

        @staticmethod
        def _chunks_for_edit():
            """Emit a text intro, then an ``edit_file`` tool call that
            rewrites the canned plan's title in ``~/.toddler/test_write.py``.

            The target only exists after the ``"write"`` response has run,
            so that turn is this one's setup.
            """
            # Spelled in two pieces on purpose: the "write" response
            # copies this file, so a verbatim literal here would make the
            # old string appear twice in the copy and ``edit_file`` would
            # reject the call as ambiguous.
            old_string = '"title": "' + 'Dummy plan — two canned steps",'
            new_string = '"title": "' + 'Example plan — two canned steps",'
            args = json.dumps(
                {
                    "file_path": "~/.toddler/test_write.py",
                    "old_string": old_string,
                    "new_string": new_string,
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "I'll replace dummy with example in _PLAN_PROPOSAL in "
                    "~/.toddler/test_write.py.\n\n",
                ),
                ("tool_name", "edit_file"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_ask():
            """Emit an ``ask_user`` call with two three-choice questions.

            The first choice of each question is the recommendation.  The
            loop pauses on the call; the answer comes back as its tool
            result, which the generic tool-result branch acknowledges.
            """
            args = json.dumps(
                {"questions": _ASK_USER_QUESTIONS},
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "I need a couple of decisions before I continue.\n\n",
                ),
                ("tool_name", "ask_user"),
                ("tool_args", args),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        # -- canned plan workflow -------------------------------------------

        @staticmethod
        def _chunks_for_plan():
            """Emit a ``propose_plan`` call — the plan turn's endpoint.

            The plan body is markdown in the call's arguments; the loop
            pauses on the call for the user's decision.  There is no
            separate proposal request to answer.
            """
            blocks: list[tuple[str, str | None]] = [
                (
                    "text",
                    "Let me put together a plan for this request.\n\n",
                ),
                ("tool_name", "propose_plan"),
                (
                    "tool_args",
                    json.dumps(_PLAN_PROPOSAL, ensure_ascii=False),
                ),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _todo_args(todos: list[tuple[str, str]]) -> str:
            """Serialize a whole ``todo_write`` list for (content, status)."""
            return json.dumps(
                {
                    "todos": [
                        {"content": content, "status": status}
                        for content, status in todos
                    ],
                },
                ensure_ascii=False,
            )

        @classmethod
        def _chunks_for_todo(
            cls, todos: list[tuple[str, str]], *, text: str = "",
        ):
            """Emit a bare ``todo_write`` call, with optional prose."""
            blocks: list[tuple[str, str | None]] = []
            if text:
                blocks.append(("text", text))
            blocks.extend([
                ("tool_name", "todo_write"),
                ("tool_args", cls._todo_args(todos)),
                ("finish", "tool_calls"),
            ])
            return _DummyStream._materialize(blocks)

        # -- tool results and fallback --------------------------------------

        def _chunks_for_tool_result(self, last_msg: dict):
            """Answer a tool result.

            The propose_plan approval opens the canned execution with a
            todo_write call; the todo results that follow advance the list
            and end with the closing summary.  Every other result gets a
            short acknowledgment.
            """
            tool_content = last_msg.get("content", "") or ""
            if tool_content.startswith(_PLAN_APPROVED_MARKER):
                return _DummyStream._chunks_for_todo([
                    (
                        "Run dummy step 1 and report its output",
                        "in_progress",
                    ),
                    (
                        "Run dummy step 2 and report its output",
                        "pending",
                    ),
                ])

            if tool_content.startswith(_TODO_RESULT_MARKER):
                # Count the todo results already in the transcript,
                # including this one: 1 = step 1 done, 2 = the list closed,
                # 3 = the closing summary.
                results = sum(
                    1 for msg in self._messages
                    if msg.get("role") == "tool"
                    and (msg.get("content") or "").startswith(
                        _TODO_RESULT_MARKER
                    )
                )
                if results == 1:
                    return _DummyStream._chunks_for_todo(
                        [
                            (
                                "Run dummy step 1 and report its output",
                                "completed",
                            ),
                            (
                                "Run dummy step 2 and report its output",
                                "in_progress",
                            ),
                        ],
                        text=(
                            "[step-1] Dummy step 1 work output — the "
                            "canned text for this step.\n\n"
                        ),
                    )
                if results == 2:
                    return _DummyStream._chunks_for_todo([
                        (
                            "Run dummy step 1 and report its output",
                            "completed",
                        ),
                        (
                            "Run dummy step 2 and report its output",
                            "completed",
                        ),
                    ])
                if results == 3:
                    blocks: list[tuple[str, str | None]] = [
                        (
                            "text",
                            "Both dummy plan steps completed — the approved "
                            "plan ran end to end. Is there anything else you "
                            "need?\n",
                        ),
                        ("finish", "stop"),
                    ]
                    return _DummyStream._materialize(blocks)

            preview = (
                tool_content[:80] + "..."
                if len(tool_content) > 80
                else tool_content
            )
            blocks = [
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
