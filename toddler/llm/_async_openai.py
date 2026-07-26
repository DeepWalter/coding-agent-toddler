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
from types import SimpleNamespace as _NS

__all__ = ["AsyncOpenAI"]

# ---------------------------------------------------------------------------
# Path to *this* file — used by the "text" / "write" canned responses
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
_THIS_CONTENT = _THIS_FILE.read_text(encoding="utf-8")

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
                    max_tokens=4096,
                    temperature=0.0,
                    stream=False,
                    stream_options=None,
                ):
                    """Simulate ``chat.completions.create``.

                    Only ``stream=True`` is implemented.  The canned
                    response is chosen by inspecting the last message:

                    * ``"text"`` — stream the content of *this* file.
                    * ``"read"`` — emit a ``shell`` tool call that counts
                      ``*.py`` files.
                    * ``"write"`` — emit a ``write_file`` tool call that
                      writes this file's content to
                      ``~/.toddler/test_write.py``.
                    * ``role == "tool"`` — stream a short acknowledgment and
                      finish.
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
            self._chunks: list[_NS] = []
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
            elif content == "text":
                return self._chunks_for_text()
            elif content == "read":
                return self._chunks_for_read()
            elif content == "write":
                return self._chunks_for_write()
            else:
                return self._chunks_for_unknown(content)

        # -- canned responses -----------------------------------------------

        @staticmethod
        def _chunks_for_text():
            """Stream the content of *this* file."""
            blocks: list[tuple[str, str | None]] = []
            for line in _THIS_CONTENT.splitlines(keepends=True):
                blocks.append(("text", line))
            blocks.append(("finish", "stop"))
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_read():
            """Emit a ``shell`` tool call to count ``*.py`` files."""
            blocks: list[tuple[str, str | None]] = [
                ("tool_name", "shell"),
                (
                    "tool_args",
                    '{"command": "find . -name '
                    '\'*.py\' -type f | wc -l"}',
                ),
                ("finish", "tool_calls"),
            ]
            return _DummyStream._materialize(blocks)

        @staticmethod
        def _chunks_for_write():
            """Emit a ``write_file`` tool call to copy this file to
            ``~/.toddler/test_write.py``."""
            args = json.dumps(
                {
                    "file_path": "~/.toddler/test_write.py",
                    "content": _THIS_CONTENT,
                },
                ensure_ascii=False,
            )
            blocks: list[tuple[str, str | None]] = [
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
        ) -> list[_NS]:
            """Convert *blocks* into a list of chunk ``SimpleNamespace``
            objects.

            Each block is a ``(kind, payload)`` pair:

            ==============  ================================================
            ``"text"``      text content to stream
            ``"tool_name"`` name of the tool being called
            ``"tool_args"`` JSON fragment of the tool arguments
            ``"finish"``    finish reason string (e.g. ``"stop"``)
            ==============  ================================================
            """
            chunks: list[_NS] = []
            tool_id: str | None = None
            tool_idx = 0

            for kind, payload in blocks:
                if kind == "text":
                    delta = _NS(content=payload, tool_calls=None)
                    choice = _NS(delta=delta, finish_reason=None)
                    chunks.append(_NS(choices=[choice], usage=None))

                elif kind == "tool_name":
                    tool_id = f"fake_{uuid.uuid4().hex[:12]}"
                    func = _NS(name=payload, arguments=None)
                    tc = _NS(index=tool_idx, id=tool_id, function=func)
                    delta = _NS(content=None, tool_calls=[tc])
                    choice = _NS(delta=delta, finish_reason=None)
                    chunks.append(_NS(choices=[choice], usage=None))

                elif kind == "tool_args":
                    func = _NS(name=None, arguments=payload)
                    tc = _NS(index=tool_idx, id=None, function=func)
                    delta = _NS(content=None, tool_calls=[tc])
                    choice = _NS(delta=delta, finish_reason=None)
                    chunks.append(_NS(choices=[choice], usage=None))

                elif kind == "finish":
                    usage = _NS(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                    delta = _NS(content=None, tool_calls=None)
                    choice = _NS(delta=delta, finish_reason=payload)
                    chunks.append(_NS(choices=[choice], usage=usage))

            return chunks

else:
    from openai import AsyncOpenAI  # noqa: F401
