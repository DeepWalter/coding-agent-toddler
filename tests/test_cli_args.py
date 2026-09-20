"""Argument-parser tests — the flags both entry points share.

``tod`` and ``tod serve`` build separate parsers, so the options they have
in common come from one helper in :mod:`toddler.utils.cli`.  These tests
pin that both parsers still expose that shared set, and that the flags
neither entry point owns stay out of the other's parser — re-inlining a
duplicate in one of them is how the two would drift apart again.

The last group follows ``--reasoning-effort`` past the parser: argv →
:class:`Settings`, which is where the provider's involvement ends — the
effort reaches a request through the turn config (see
``tests/test_model_selection.py``).
"""

from __future__ import annotations

import pytest

from toddler.config.settings import Settings
from toddler.utils.cli import build_argparser, build_serve_argparser

# Defined once in _add_common_args, added to both parsers.  The paired
# value is one the flag must accept — None for the boolean ones.
_SHARED_FLAGS = [
    ("--no-stream", None),
    ("--model", "m"),
    ("--base-url", "http://x"),
    ("--api-key", "k"),
    ("--max-iterations", "7"),
    ("--reasoning-effort", "low"),
    ("--verbose", None),
]


@pytest.fixture(
    params=[build_argparser, build_serve_argparser],
    ids=["tod", "tod-serve"],
)
def parser(request):
    return request.param()


class TestSharedFlags:

    @pytest.mark.parametrize(
        ("flag", "value"), _SHARED_FLAGS, ids=[f[0] for f in _SHARED_FLAGS]
    )
    def test_flag_is_accepted_by_both_parsers(self, parser, flag, value):
        parser.parse_args([flag] if value is None else [flag, value])

    @pytest.mark.parametrize(
        ("argv", "field", "expected"),
        [
            (["--model", "m"], "model", "m"),
            (["--base-url", "http://x"], "base_url", "http://x"),
            (["--api-key", "k"], "api_key", "k"),
            (["--max-iterations", "7"], "max_iterations", 7),
            (["--reasoning-effort", "low"], "reasoning_effort", "low"),
        ],
    )
    def test_value_lands_on_the_matching_settings_field(
        self, parser, argv, field, expected
    ):
        """``Settings.from_cli`` overlays CLI fields by name, so a rename
        on either side would silently stop applying."""
        settings = Settings.from_cli(parser.parse_args(argv))
        assert getattr(settings, field) == expected

    def test_no_stream_keeps_its_namespace_name(self, parser):
        """``main.py`` translates the flag itself — the namespace name is
        the contract that translation depends on."""
        assert parser.parse_args(["--no-stream"]).no_stream is True


class TestParserOwnership:
    """Flags belonging to one entry point must not leak into the other."""

    def test_cli_parser_keeps_its_interactive_flags(self):
        args = build_argparser().parse_args(["--list-sessions", "--plan"])
        assert args.list_sessions is True
        assert args.plan is True

    def test_serve_parser_keeps_its_binding_flags(self):
        args = build_serve_argparser().parse_args(
            ["--host", "0.0.0.0", "--port", "9000"]
        )
        assert args.host == "0.0.0.0"
        assert args.port == 9000

    def test_cli_only_flags_are_rejected_by_the_serve_parser(self):
        with pytest.raises(SystemExit):
            build_serve_argparser().parse_args(["--list-sessions"])

    def test_serve_only_flags_are_rejected_by_the_cli_parser(self):
        with pytest.raises(SystemExit):
            build_argparser().parse_args(["--port", "9000"])


# ============================================================================
# --reasoning-effort — the flag's chain to the request
# ============================================================================


class TestReasoningEffortFlag:
    """The flag's chain to the request.

    It stops at Settings: the provider has no effort of its own, so the
    value travels Settings → the turn's config → the request.  The last
    hop is asserted in ``tests/test_model_selection.py``, which drives a
    real turn; the tier-to-wire mapping is pinned in
    ``tests/test_provider_params.py``.
    """

    def test_flag_reaches_settings(self):
        args = build_argparser().parse_args(
            ["--reasoning-effort", "medium", "do the thing"]
        )
        assert Settings.from_cli(args).reasoning_effort == "medium"

    def test_flag_overrides_the_environment(self, monkeypatch):
        monkeypatch.setenv("TODDLER_REASONING_EFFORT", "low")
        args = build_argparser().parse_args(
            ["--reasoning-effort", "max", "do the thing"]
        )
        assert Settings.from_cli(args).reasoning_effort == "max"

    def test_unknown_tier_is_rejected_at_the_parser(self):
        """A typo fails fast — reaching the provider, it would be silently
        coerced onto a costlier tier."""
        with pytest.raises(SystemExit):
            build_argparser().parse_args(
                ["--reasoning-effort", "hgih", "do the thing"]
            )

    def test_flag_survives_the_cli_overlay_with_the_model(self):
        """Both flags ride one ``from_cli`` merge — the turn config reads
        them together, so a model override must not drop the effort."""
        args = build_argparser().parse_args([
            "--reasoning-effort", "ultra",
            "--model", "deepseek-v4-pro",
            "do the thing",
        ])

        settings = Settings.from_cli(args)
        assert (settings.model, settings.reasoning_effort) == (
            "deepseek-v4-pro", "ultra",
        )
