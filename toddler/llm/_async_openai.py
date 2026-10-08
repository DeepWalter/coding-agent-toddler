"""AsyncOpenAI import helper — conditionally provides real or dummy client.

When ``TEST=cli``, returns a :class:`DummyAsyncOpenAI` that simulates
``chat.completions.create`` with ``stream=True`` using canned responses.
Otherwise returns the real ``openai.AsyncOpenAI``.
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

                    Only ``stream=True`` is implemented.  The canned
                    response is chosen by inspecting the last message:

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
                    * ``role == "tool"`` — stream a short acknowledgment and
                      finish.

                    The request-shaping parameters (token budget, effort,
                    format) are accepted and ignored — the canned responses
                    do not vary with them.
                    """
                    if not stream:
                        raise NotImplementedError(
                            "DummyAsyncOpenAI only supports stream=True"
                        )
                    return _DummyStream(messages)


    # ======================================================================
    # DummyStream — the async iterable returned by create()
    # ======================================================================

    class _DummyStream:
        """Async iterable that yields simulated OpenAI streaming chunks.

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
            """Inspect the last message and return the matching chunk list."""
            last_msg = self._messages[-1] if self._messages else {}
            role = last_msg.get("role", "")
            content = last_msg.get("content", "")

            if role == "tool":
                return self._chunks_for_tool_result(last_msg)
            elif content == "reason":
                return self._chunks_for_reasoning()
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

        @staticmethod
        def _chunks_for_tool_result(last_msg: dict):
            """Stream a short acknowledgment after a tool result."""
            tool_content = last_msg.get("content", "")
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

else:
    from openai import AsyncOpenAI  # noqa: F401
