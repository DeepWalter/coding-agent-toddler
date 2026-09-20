"""Model naming and selection — slots, the ``[1m]`` notation, budgets.

The layer between ``tests/test_cli_args.py`` (a flag reaches Settings) and
``tests/test_provider_params.py`` (a call reaches the wire): what a slot name
resolves to, what a spec means, and what a turn's config derives from the two.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from toddler.config import defaults
from toddler.config.models import (
    TurnConfig,
    completion_budget_for,
    context_length_for,
    resolve_slot,
    split_spec,
    wire_model,
)
from toddler.config.settings import Settings

_MODEL_ENV_VARS = (
    "TODDLER_MODEL",
    "TODDLER_DEFAULT_MODEL",
    "TODDLER_PRO_MODEL",
    "TODDLER_FLASH_MODEL",
    "DEEPSEEK_MODEL",
)


# ============================================================================
# The notation
# ============================================================================


class TestSpecNotation:

    def test_split_reads_the_suffix(self):
        assert split_spec("deepseek-flash[1m]") == ("deepseek-flash", "1m")

    def test_split_without_a_suffix(self):
        assert split_spec("deepseek-flash") == ("deepseek-flash", None)

    def test_wire_model_strips_the_notation(self):
        """The suffix is Toddler's own — it never reaches the endpoint."""
        assert wire_model("deepseek-v4-pro[1m]") == "deepseek-v4-pro"

    def test_a_known_suffix_names_its_window(self):
        assert context_length_for("deepseek-flash[1m]") == 1_000_000

    def test_the_suffix_is_case_insensitive(self):
        assert context_length_for("deepseek-flash[1M]") == 1_000_000

    def test_no_suffix_takes_the_default_window(self):
        assert context_length_for("deepseek-flash") == 200_000

    def test_an_unknown_suffix_warns_and_takes_the_default(self, caplog):
        assert context_length_for("deepseek-flash[banana]") == 200_000
        assert "banana" in caplog.text

    def test_an_unknown_suffix_is_still_stripped_from_the_wire(self):
        """A bracket is never legal in a model id, so stripping is right
        whatever the suffix meant; the window is the part being guessed."""
        assert wire_model("deepseek-flash[banana]") == "deepseek-flash"


# ============================================================================
# The budgets
# ============================================================================


class TestCompletionBudgets:

    @pytest.mark.parametrize(
        ("effort", "expected"),
        [
            ("none", 4_096),
            ("minimal", 32_768),
            ("low", 32_768),
            ("medium", 32_768),
            ("high", 32_768),
            ("xhigh", 32_768),
            ("max", 65_536),
            ("ultra", 65_536),
        ],
    )
    def test_the_tier_decides_the_room_for_thinking(self, effort, expected):
        assert completion_budget_for(effort) == expected

    def test_an_unset_effort_takes_the_middle_budget(self):
        """``None`` leaves the endpoint's own default in place, so the local
        number is a middle guess rather than the top of the scale."""
        assert completion_budget_for(None) == 32_768

    def test_an_unknown_tier_takes_the_top_budget(self):
        """The provider coerces an unknown tier to its own maximum, so the
        budget here has to describe what the endpoint will actually do."""
        assert completion_budget_for("hgih") == 65_536

    def test_the_scale_comes_from_the_shared_tier_list(self):
        """``max`` is not the last tier — a tier added above it takes the top
        budget without a second edit."""
        assert defaults.REASONING_EFFORT_TIERS[-1] == "ultra"


# ============================================================================
# TurnConfig
# ============================================================================


class TestTurnConfig:

    def test_derives_every_member_from_the_spec(self):
        config = TurnConfig(spec="deepseek-v4-pro[1m]", reasoning_effort="high")
        assert config.model == "deepseek-v4-pro"
        assert config.max_context_tokens == 1_000_000
        assert config.max_completion_tokens == 32_768

    def test_a_spec_without_notation_is_its_own_wire_id(self):
        config = TurnConfig(spec="deepseek-flash")
        assert config.model == "deepseek-flash"
        assert config.max_context_tokens == 200_000

    def test_effort_is_part_of_the_identity(self):
        """``dataclasses.replace`` is the whole of a selection change, so two
        configs differing only in effort must not compare equal."""
        base = TurnConfig(spec="deepseek-flash")
        quiet = replace(base, reasoning_effort="none")
        assert quiet != base
        assert quiet.model == base.model
        assert quiet.max_completion_tokens == 4_096


# ============================================================================
# Slots
# ============================================================================


class TestSlots:

    @pytest.fixture
    def clean_env(self, monkeypatch):
        """Drop every variable that could name a model."""
        for var in _MODEL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_every_slot_ships_with_the_same_model(self, clean_env):
        """A fresh install works with one id; retargeting is an env var."""
        settings = Settings()
        assert set(settings.model_slots.values()) == {defaults.DEFAULT_MODEL}
        assert settings.model == "default"
        assert settings.model_spec == "deepseek-flash"

    def test_a_slot_reads_its_own_variable(self, monkeypatch):
        monkeypatch.setenv("TODDLER_PRO_MODEL", "deepseek-v4-pro[1m]")
        settings = Settings(model="pro")
        assert settings.model_spec == "deepseek-v4-pro[1m]"
        config = TurnConfig(
            spec=settings.model_spec,
            reasoning_effort=settings.reasoning_effort,
        )
        assert config.model == "deepseek-v4-pro"
        assert config.max_context_tokens == 1_000_000

    def test_deepseek_model_values_the_default_slot(self, monkeypatch, clean_env):
        """The pre-slot spelling of "the model to use" still selects one, so
        an existing install is not moved onto something else."""
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        assert Settings().model_spec == "deepseek-v4-pro"

    def test_toddler_default_model_wins_over_deepseek_model(
        self, monkeypatch, clean_env,
    ):
        monkeypatch.setenv("TODDLER_DEFAULT_MODEL", "deepseek-flash[1m]")
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        assert Settings().model_spec == "deepseek-flash[1m]"

    def test_an_unknown_slot_warns_and_falls_back(self, caplog):
        """Settings are built at import time, so a stale value must not take
        the process down."""
        settings = Settings(model="flash-2")
        assert settings.model == "default"
        assert "flash-2" in caplog.text

    def test_a_slot_is_case_and_space_insensitive(self, clean_env):
        assert Settings(model=" Pro ").model == "pro"

    def test_resolve_slot_names_the_spec(self):
        slots = {"default": "a", "pro": "b", "flash": "c"}
        assert resolve_slot("Pro", slots) == "b"

    def test_resolve_slot_rejects_an_unknown_name(self):
        """A literal vendor id is not a selection."""
        with pytest.raises(ValueError, match="gpt-4o"):
            resolve_slot("gpt-4o", {"default": "a", "pro": "b", "flash": "c"})
