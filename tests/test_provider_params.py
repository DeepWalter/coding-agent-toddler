"""Request-parameter tests — the per-family wire shape.

Covers :meth:`OpenAICompatibleProvider._parse_params` and the kwargs that
actually reach ``chat.completions.create``.  The two API families disagree
on both the token-budget key (``max_tokens`` vs ``max_completion_tokens``)
and the thinking knob (DeepSeek's ``low``/``high``/``max`` tiers vs the
OpenAI ``reasoning_effort`` scale), so each branch is pinned separately —
along with the fallbacks for values the endpoint cannot express.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest

from toddler.config.settings import Settings
from toddler.llm import Message
from toddler.llm.provider import OpenAICompatibleProvider

_DEEPSEEK = "deepseek-v4-pro"
_OPENAI = "gpt-5"


# ============================================================================
# Stub client — captures every create() call
# ============================================================================


class _ChunkStream:
    """Async iterable over canned OpenAI chunk objects."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _install_stub(monkeypatch, *, chunks=None, response=None):
    """Replace ``toddler.llm.provider.AsyncOpenAI`` with a stub client.

    ``create`` records its kwargs, then returns an async iterable when
    called with ``stream=True`` and *response* otherwise.
    """
    class _StubCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("stream"):
                return _ChunkStream(chunks or [])
            return response

    completions = _StubCompletions()

    class _StubClient:
        def __init__(
            self, base_url=None, api_key=None, http_client=None,
        ) -> None:
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("toddler.llm.provider.AsyncOpenAI", _StubClient)
    return completions


def _make_provider(
    model: str = _DEEPSEEK, **settings_overrides
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model=model,
            **settings_overrides,
        )
    )


def _parse(
    model: str,
    *,
    effort: str | None = None,
    response_format: dict | None = None,
    max_completion_tokens: int = 2048,
) -> dict:
    """Call ``_parse_params`` with the boilerplate filled in."""
    return OpenAICompatibleProvider._parse_params(
        model,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=effort,
        response_format=response_format,
        temperature=0.0,
    )


async def _drain(stream) -> list:
    """Exhaust a streaming call and return its events."""
    return [evt async for evt in stream]


def _text_response(content: str = "ok") -> SimpleNamespace:
    """A minimal non-streaming completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=None),
            finish_reason="stop",
        )],
        usage=None,
    )


# ============================================================================
# _parse_params — token budget
# ============================================================================


class TestTokenBudget:
    """Each family gets the key its endpoint accepts."""

    def test_deepseek_takes_max_tokens(self):
        kwargs = _parse(_DEEPSEEK)
        assert kwargs == {"temperature": 0.0, "max_tokens": 2048}

    def test_other_endpoints_take_max_completion_tokens(self):
        kwargs = _parse(_OPENAI)
        assert kwargs == {"temperature": 0.0, "max_completion_tokens": 2048}

    def test_model_match_is_case_insensitive(self):
        assert "max_tokens" in _parse("DeepSeek-V4-Pro")


# ============================================================================
# _parse_params — the SDK's signature is the arbiter of the key names
# ============================================================================


class TestSdkCompatibility:
    """``create()`` has no ``**kwargs`` catch-all, so a key it does not
    declare is an immediate ``TypeError`` — the failure mode that makes a
    plausible-looking name like ``reason_effort`` useless."""

    def test_every_emitted_key_is_accepted_by_the_sdk(self):
        from openai.resources.chat.completions import AsyncCompletions

        create = inspect.signature(AsyncCompletions.create)
        for model in (_DEEPSEEK, _OPENAI):
            for effort in (None, "high", "none"):
                kwargs = _parse(
                    model,
                    effort=effort,
                    response_format={"type": "json_object"},
                )
                # Unknown keys raise TypeError; known ones bind partially.
                create.bind_partial(**kwargs)


# ============================================================================
# _parse_params — thinking effort
# ============================================================================


class TestReasoningEffort:
    """Effort tiers are translated, not passed through blindly."""

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "high"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "max"),
            ("ultra", "max"),
        ],
    )
    def test_deepseek_tiers_collapse_onto_its_scale(self, tier, expected):
        assert _parse(_DEEPSEEK, effort=tier)["reasoning_effort"] == expected

    def test_deepseek_tier_match_is_case_insensitive(self):
        assert _parse(_DEEPSEEK, effort="MEDIUM")["reasoning_effort"] == "high"

    def test_deepseek_none_disables_thinking(self):
        """``none`` is not a tier — it flips the separate thinking field."""
        kwargs = _parse(_DEEPSEEK, effort="none")
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in kwargs

    def test_other_endpoints_get_the_tier_untranslated(self):
        kwargs = _parse(_OPENAI, effort="minimal")
        assert kwargs["reasoning_effort"] == "minimal"

    @pytest.mark.parametrize("model", [_DEEPSEEK, _OPENAI])
    def test_omitted_when_none(self, model):
        """No effort configured leaves the endpoint's own default alone."""
        kwargs = _parse(model, effort=None)
        assert "reasoning_effort" not in kwargs
        assert "extra_body" not in kwargs


# ============================================================================
# _parse_params — response format
# ============================================================================


class TestResponseFormat:
    """DeepSeek has no guided decoding — the schema is dropped, not sent."""

    def test_deepseek_downgrades_json_schema(self):
        kwargs = _parse(
            _DEEPSEEK,
            response_format={"type": "json_schema", "schema": {"a": 1}},
        )
        assert kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.parametrize("rf_type", ["text", "json_object"])
    def test_deepseek_accepts_the_plain_types(self, rf_type):
        kwargs = _parse(_DEEPSEEK, response_format={"type": rf_type})
        assert kwargs["response_format"] == {"type": rf_type}

    def test_deepseek_unknown_type_warns_and_omits(self, caplog):
        with caplog.at_level(logging.WARNING, logger="toddler.llm.provider"):
            kwargs = _parse(_DEEPSEEK, response_format={"type": "xml"})
        assert "response_format" not in kwargs
        assert "xml" in caplog.text

    def test_other_endpoints_get_the_format_verbatim(self):
        rf = {"type": "json_schema", "schema": {"a": 1}}
        assert _parse(_OPENAI, response_format=rf)["response_format"] is rf


# ============================================================================
# generate — what actually reaches create()
# ============================================================================


class TestGenerateWire:
    """The end-to-end kwargs, including the streaming/non-streaming split."""

    @pytest.mark.asyncio
    async def test_streaming_call_carries_every_parameter(self, monkeypatch):
        completions = _install_stub(monkeypatch, chunks=[])
        provider = _make_provider()

        events = await _drain(await provider.generate(
            [Message.user("hi")], [], stream=True,
        ))

        # A duplicated ``model`` kwarg would be swallowed into an error
        # event by the streaming path's except — assert on the call itself.
        assert [e.type for e in events] == ["message_start", "message_stop"]
        assert len(completions.calls) == 1
        call = completions.calls[0]
        assert call["model"] == _DEEPSEEK
        assert call["messages"] == [{"role": "user", "content": "hi"}]
        assert call["max_tokens"] == 4096
        assert "max_completion_tokens" not in call
        assert call["stream"] is True
        assert call["stream_options"] == {"include_usage": True}

    @pytest.mark.asyncio
    async def test_non_streaming_call_is_not_a_stream(self, monkeypatch):
        completions = _install_stub(
            monkeypatch, response=_text_response("done"),
        )
        provider = _make_provider(_OPENAI)

        result = await provider.generate(
            [Message.user("hi")], [], stream=False,
            max_completion_tokens=512,
        )

        assert result.messages[0].content == "done"
        call = completions.calls[0]
        assert call["stream"] is False
        assert call["max_completion_tokens"] == 512

    @pytest.mark.asyncio
    async def test_explicit_effort_overrides_the_settings_default(
        self, monkeypatch
    ):
        completions = _install_stub(monkeypatch, chunks=[])
        provider = _make_provider(reasoning_effort="high")

        await _drain(await provider.generate(
            [Message.user("hi")], [], stream=True, reasoning_effort="minimal",
        ))

        assert completions.calls[0]["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_settings_effort_is_the_fallback(self, monkeypatch):
        completions = _install_stub(monkeypatch, chunks=[])
        provider = _make_provider(reasoning_effort="medium")

        assert provider.effort == "medium"
        await _drain(await provider.generate(
            [Message.user("hi")], [], stream=True,
        ))

        assert completions.calls[0]["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_no_effort_configured_sends_no_effort(self, monkeypatch):
        completions = _install_stub(monkeypatch, chunks=[])
        provider = _make_provider()

        await _drain(await provider.generate(
            [Message.user("hi")], [], stream=True,
        ))

        assert provider.effort is None
        assert "reasoning_effort" not in completions.calls[0]


# ============================================================================
# generate_compact — same budget key, no format or effort hints
# ============================================================================


class TestGenerateCompact:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "budget_key"),
        [(_DEEPSEEK, "max_tokens"), (_OPENAI, "max_completion_tokens")],
    )
    async def test_compact_uses_the_family_budget_key(
        self, monkeypatch, model, budget_key
    ):
        completions = _install_stub(
            monkeypatch, response=_text_response("summary"),
        )
        provider = _make_provider(model)

        assert await provider.generate_compact("summarize") == "summary"

        call = completions.calls[0]
        assert call[budget_key] == 1024
        assert call["stream"] is False
        assert "reasoning_effort" not in call
        assert "response_format" not in call

    @pytest.mark.asyncio
    async def test_compact_ignores_the_configured_effort(self, monkeypatch):
        """A cheap summary call must not inherit a thinking-effort setting."""
        completions = _install_stub(
            monkeypatch, response=_text_response("summary"),
        )
        provider = _make_provider(reasoning_effort="max")

        await provider.generate_compact("summarize")

        assert "reasoning_effort" not in completions.calls[0]
