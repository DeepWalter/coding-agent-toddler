"""Model naming and selection — slots, the ``[1m]`` notation, budgets.

The layer between ``tests/test_cli_args.py`` (a flag reaches Settings) and
``tests/test_provider_params.py`` (a call reaches the wire): what a slot name
resolves to, what a spec means, and what a turn's config derives from the two.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace

import pytest

from tests.mocks import MockLLMProvider, text_response, tool_use_response
from toddler.agent.events import ToolCallEnd
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
from toddler.main import is_supported_model, main, require_supported_models
from toddler.session.database import SQLiteDatabase
from toddler.session.manager import SessionManager
from toddler.session.storage import StorageManager

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


# ============================================================================
# The conversation's selection
# ============================================================================


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Three distinguishable slots, so a test can tell which one is live."""
    return Settings(
        session_dir=tmp_path,
        streaming_enabled=False,
        model="default",
        model_default="default-model",
        model_pro="pro-model",
        model_flash="flash-model[1m]",
        reasoning_effort="high",
    )


@pytest.fixture
def storage_mgr(tmp_path) -> StorageManager:
    db = SQLiteDatabase(tmp_path / "selection.db")
    db.open()
    return StorageManager(db)


@pytest.fixture
def llm() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
async def mgr(settings, storage_mgr, llm, tmp_path) -> SessionManager:
    mgr = SessionManager(settings, storage_mgr, llm, repo_root=tmp_path)
    await mgr.resolve()
    return mgr


async def _run_turn(mgr: SessionManager) -> None:
    """Drive one complete turn and persist it (the mock answers "Done.")."""
    async for _ in mgr.process_turn("say hi"):
        pass
    await mgr.save()


class TestSelection:

    async def test_a_fresh_conversation_runs_the_selected_slot(self, mgr):
        assert mgr.model == "default-model"
        assert mgr.effort == "high"

    async def test_a_row_without_a_model_falls_back_to_the_slot(self, mgr):
        """A fresh row names none until the first save; pre-v4 rows never do."""
        assert mgr.conversation.model is None
        assert mgr.model == "default-model"

    async def test_the_window_follows_the_selection(self, mgr):
        """The notation picks the window, the wire id is stripped from it."""
        mgr.set_model("flash")

        config = mgr.context.config
        assert config.spec == "flash-model[1m]"
        assert config.model == "flash-model"
        assert config.max_context_tokens == 1_000_000

    async def test_the_turn_sends_the_wire_id_not_the_spec(self, mgr, llm):
        """The notation is local: the endpoint is asked for the stripped id
        while the header and the row keep the spec."""
        mgr.set_model("flash")

        await _run_turn(mgr)

        assert llm.call_configs == [("flash-model", "high")]
        assert mgr.model == "flash-model[1m]"
        assert mgr.conversation.model == "flash-model[1m]"

    async def test_switching_the_model_stamps_the_row_and_drops_the_count(
        self, mgr, storage_mgr,
    ):
        """The count was computed by the old encoding, so it goes with the
        model that produced it."""
        conv = mgr.conversation
        conv.total_tokens = 1234
        storage_mgr.update_conversation(conv)

        mgr.set_model("pro")

        reloaded = storage_mgr.get_conversation(conv.id)
        assert reloaded.model == "pro-model"
        assert reloaded.total_tokens == 0
        assert reloaded.reasoning_effort == "high"

    async def test_switching_the_effort_keeps_the_count(self, mgr, storage_mgr):
        conv = mgr.conversation
        conv.total_tokens = 1234
        storage_mgr.update_conversation(conv)

        mgr.set_effort("none")

        reloaded = storage_mgr.get_conversation(conv.id)
        assert reloaded.reasoning_effort == "none"
        assert reloaded.model == "default-model"
        assert reloaded.total_tokens == 1234

    async def test_an_unknown_slot_is_refused(self, mgr):
        with pytest.raises(ValueError, match="gpt-4o"):
            mgr.set_model("gpt-4o")
        assert mgr.model == "default-model"

    async def test_clear_carries_the_selection_forward(self, mgr):
        """``/clear`` starts a new conversation, not a new model."""
        await _run_turn(mgr)
        mgr.set_model("flash")

        await mgr.new_conversation()

        assert mgr.model == "flash-model[1m]"
        assert mgr.conversation.model == "flash-model[1m]"

    async def test_a_reload_restores_the_pair(
        self, mgr, settings, storage_mgr, tmp_path,
    ):
        """What a restart does: a second manager over the same database."""
        await _run_turn(mgr)
        mgr.set_model("pro")
        mgr.set_effort("low")
        session_id = mgr.session.id

        reloaded = SessionManager(
            settings, storage_mgr, MockLLMProvider(), repo_root=tmp_path,
        )
        await reloaded.resolve(session_id=session_id)

        assert (reloaded.model, reloaded.effort) == ("pro-model", "low")

    async def test_a_turn_keeps_its_model_when_the_selection_changes(
        self, settings, storage_mgr, tmp_path,
    ):
        """The pin: switching mid-turn re-keys the accounting but must not
        move the model underneath the requests still to come."""
        llm = MockLLMProvider([
            tool_use_response("no_such_tool", {}, tool_id="call_x"),
            text_response("done"),
        ])
        mgr = SessionManager(settings, storage_mgr, llm, repo_root=tmp_path)
        await mgr.resolve()

        async for event in mgr.process_turn("go"):
            if isinstance(event, ToolCallEnd):
                mgr.set_model("pro")

        assert len(llm.call_configs) == 2
        assert llm.call_configs[0] == llm.call_configs[1] == (
            "default-model", "high",
        )
        # The selection is the new one — it is what the *next* turn reads.
        assert mgr.model == "pro-model"


# ============================================================================
# The slot a model was picked by — provenance, not identity
# ============================================================================


class TestModelSlotProvenance:
    """``model_slot`` records which slot row the user pointed at.

    The spec cannot answer that: two slots routinely name the same model,
    and on a fresh install all three do.  A picker highlighting "the row
    you picked" needs the name, so it is stored beside the spec — while the
    spec stays what runs.
    """

    async def test_a_fresh_conversation_reports_the_settings_slot(self, mgr):
        assert mgr.conversation.model is None
        assert mgr.model_slot == "default"

    async def test_the_picked_slot_is_stamped_and_survives_a_reload(
        self, mgr, settings, storage_mgr, tmp_path,
    ):
        await _run_turn(mgr)
        mgr.set_model("pro")
        assert mgr.conversation.model_slot == "pro"
        session_id = mgr.session.id

        reloaded = SessionManager(
            settings, storage_mgr, MockLLMProvider(), repo_root=tmp_path,
        )
        await reloaded.resolve(session_id=session_id)

        assert reloaded.model_slot == "pro"

    async def test_a_slot_reached_by_a_different_casing_is_stored_canonical(
        self, mgr,
    ):
        """A picker compares a row's name against this, so a stray casing
        must not make the stored slot unmatchable."""
        mgr.set_model("  PRO  ")

        assert mgr.conversation.model_slot == "pro"

    async def test_two_slots_naming_one_model_are_told_apart(
        self, storage_mgr, llm, tmp_path,
    ):
        """The whole reason the field exists: switching between slots that
        resolve to the same spec changes nothing about the model — and the
        stored slot still follows the pick."""
        settings = Settings(
            session_dir=tmp_path,
            streaming_enabled=False,
            model="default",
            model_default="same-model",
            model_pro="same-model",
            model_flash="other-model",
            reasoning_effort="high",
        )
        mgr = SessionManager(settings, storage_mgr, llm, repo_root=tmp_path)
        await mgr.resolve()
        await _run_turn(mgr)

        mgr.set_model("pro")

        assert mgr.model == "same-model"
        assert mgr.model_slot == "pro"
        assert storage_mgr.get_conversation(
            mgr.conversation.id,
        ).model_slot == "pro"

    async def test_an_effort_switch_leaves_the_slot_alone(self, mgr):
        await _run_turn(mgr)
        mgr.set_model("flash")
        before = mgr.conversation.total_tokens

        mgr.set_effort("low")

        assert mgr.conversation.model_slot == "flash"
        # And it did not re-key the accounting on the way past.
        assert mgr.conversation.total_tokens == before

    async def test_clear_carries_the_slot_forward(self, mgr):
        await _run_turn(mgr)
        mgr.set_model("flash")

        await mgr.new_conversation()

        assert mgr.model_slot == "flash"
        assert mgr.conversation.model_slot == "flash"

    async def test_a_row_without_a_slot_reports_the_settings_slot(
        self, mgr, storage_mgr,
    ):
        """A pre-v5 row names none — the slot is unknowable, so the answer
        is the slot the row's *absence* of a model already falls back to."""
        await _run_turn(mgr)
        conv = mgr.conversation
        conv.model_slot = None
        storage_mgr.update_conversation(conv)

        assert mgr.model_slot == "default"


# ============================================================================
# Startup — what this build admits to serving
# ============================================================================


class TestServedFamilies:
    """Only DeepSeek-family models get past startup.

    The wire shape has an OpenAI branch, but the reasoning echo-back is
    DeepSeek's requirement and knows no other dialect: a foreign model would
    run until the first turn whose history carries reasoning, then die
    mid-turn with a half-written conversation behind it.
    """

    def test_deepseek_slots_pass(self):
        require_supported_models({
            "default": "deepseek-v4-flash",
            "pro": "deepseek-v4-pro[1m]",
            "flash": "deepseek-flash",
        })

    def test_the_notation_does_not_hide_the_family(self):
        assert is_supported_model("deepseek-v4-pro[1m]") is True
        assert is_supported_model("gpt-5[1m]") is False

    def test_a_foreign_slot_is_refused(self):
        with pytest.raises(ValueError, match="gpt-5"):
            require_supported_models({"pro": "gpt-5"})

    def test_every_offending_slot_is_named(self):
        """The message has to say which slot to fix, not merely that one is
        wrong — all three are checked, since /model can reach any of them."""
        with pytest.raises(ValueError) as excinfo:
            require_supported_models({
                "default": "gpt-5",
                "pro": "claude-sonnet-5",
                "flash": "deepseek-flash",
            })

        message = str(excinfo.value)
        assert "default=gpt-5" in message
        assert "pro=claude-sonnet-5" in message
        assert "flash" not in message


class TestStartup:
    """The refusal happens on the way in, not on the request that needs it."""

    @pytest.fixture
    def quiet_logging(self, monkeypatch):
        """Keep ``main()`` from installing handlers on the root logger —
        they would outlive this process's capture and follow every later
        test."""
        monkeypatch.setattr("toddler.main.setup_logging", lambda **_: None)

    @pytest.mark.parametrize("argv", [["tod", "hi"], ["tod", "serve"]])
    def test_a_foreign_slot_stops_startup(
        self, monkeypatch, tmp_path, caplog, quiet_logging, argv,
    ):
        """Both entry points — the check sits ahead of the dispatch, so the
        serve branch cannot start a server around it."""
        for var in _MODEL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("TODDLER_SESSION_DIR", str(tmp_path))
        monkeypatch.setenv("TODDLER_PRO_MODEL", "gpt-5")
        monkeypatch.setattr(sys, "argv", argv)
        caplog.set_level(logging.ERROR)

        # If the gate ever moves below the dispatch, fail loudly rather than
        # block the suite on a real server or a real API call.
        import toddler.web.server as server

        monkeypatch.setattr(
            server, "run_server", lambda *a, **k: pytest.fail("serve started"),
        )

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 2
        assert "pro=gpt-5" in caplog.text
