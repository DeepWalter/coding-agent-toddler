"""Settings loader — env vars, optional config file, CLI arg overlay."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from dotenv import load_dotenv

from toddler.config import defaults

# ruff: noqa: E501

# Load .env from ~/.toddler before reading env vars
load_dotenv(dotenv_path=Path.home() / ".toddler" / ".env")


def _env(key: str, default: str | None = None) -> str | None:
    """Read an environment variable; prefer DEEPSEEK_ prefix, fall back to OPENAI_."""
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    val = _env(key)
    return int(val) if val is not None else default


def _env_bool(key: str, default: bool) -> bool:
    val = _env(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


logger = logging.getLogger(__name__)


@dataclass
class Settings:
    """Resolved configuration — defaults ← env vars ← CLI args.

    Instantiate with optional ``cli_args`` from argparse to let CLI flags
    take highest precedence.
    """

    # --- Models ---
    # A slot is a name for a model spec, not a spec itself.  This is the slot
    # a new conversation starts on — what --model and /model select, and what
    # a conversation resolves once and then keeps.
    model: str = field(
        default_factory=lambda: _env("TODDLER_MODEL", defaults.DEFAULT_SLOT)
    )
    # The slot values.  All three ship pointing at the same model, so a fresh
    # install works with one id and retargeting one is an env var away.
    # DEEPSEEK_MODEL is the pre-slot spelling of "the model to use", honored
    # here so that upgrading an install does not silently move it onto
    # something else.
    model_default: str = field(
        default_factory=lambda: (
            _env("TODDLER_DEFAULT_MODEL")
            or _env("DEEPSEEK_MODEL")
            or defaults.DEFAULT_MODEL
        )
    )
    model_pro: str = field(
        default_factory=lambda: _env("TODDLER_PRO_MODEL", defaults.DEFAULT_MODEL)
    )
    model_flash: str = field(
        default_factory=lambda: _env("TODDLER_FLASH_MODEL", defaults.DEFAULT_MODEL)
    )
    base_url: str = field(
        default_factory=lambda: _env("DEEPSEEK_BASE_URL", defaults.DEFAULT_BASE_URL)
    )
    api_key: str = field(
        default_factory=lambda: _env("DEEPSEEK_API_KEY", "")
    )

    # --- Agent ---
    max_iterations: int = field(
        default_factory=lambda: _env_int("TODDLER_MAX_ITERATIONS", defaults.DEFAULT_MAX_ITERATIONS)
    )
    temperature: float = float(
        _env("TODDLER_TEMPERATURE", str(defaults.DEFAULT_TEMPERATURE))
    )
    # Thinking-effort tier: "none" disables thinking, otherwise one of
    # minimal / low / medium / high / xhigh / max / ultra.  ``None`` omits
    # the field and leaves the endpoint's own default in place.
    reasoning_effort: str | None = field(
        default_factory=lambda: _env(
            "TODDLER_EFFORT_LEVEL", defaults.DEFAULT_EFFORT_LEVEL
        )
    )

    # --- Streaming ---
    streaming_enabled: bool = field(
        default_factory=lambda: _env_bool("TODDLER_STREAMING", defaults.STREAMING_ENABLED)
    )
    max_output_lines: int = field(
        default_factory=lambda: _env_int("TODDLER_MAX_OUTPUT_LINES", defaults.MAX_OUTPUT_LINES)
    )
    max_output_panel_height: int = field(
        default_factory=lambda: _env_int(
            "TODDLER_MAX_OUTPUT_PANEL_HEIGHT", defaults.MAX_OUTPUT_PANEL_HEIGHT
        )
    )

    # --- Session ---
    session_dir: Path = field(
        default_factory=lambda: Path(_env("TODDLER_SESSION_DIR", str(defaults.SESSION_DIR))).expanduser()
    )

    # --- Shell ---
    shell_timeout: int = field(
        default_factory=lambda: _env_int("TODDLER_SHELL_TIMEOUT", defaults.SHELL_DEFAULT_TIMEOUT)
    )

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Normalize the selected slot, falling back when it names nothing.

        Settings are constructed at import time (``config.settings.settings``),
        so a stale or misspelled ``TODDLER_MODEL`` must not take the process
        down: warn, and carry the default slot from here on.
        """
        self.model = self.model.strip().lower()
        if self.model not in self.model_slots:
            logger.warning(
                "Unknown model slot %r — falling back to %r (known: %s).",
                self.model, defaults.DEFAULT_SLOT, ", ".join(self.model_slots),
            )
            self.model = defaults.DEFAULT_SLOT

    @property
    def model_slots(self) -> dict[str, str]:
        """The named slots a model selection resolves through."""
        return {
            "default": self.model_default,
            "pro": self.model_pro,
            "flash": self.model_flash,
        }

    @property
    def model_spec(self) -> str:
        """The model spec the selected slot names.

        The spec, not the slot, is the identity: it is what a new
        conversation runs, what the header shows, and what is persisted.
        """
        return self.model_slots[self.model]

    # ------------------------------------------------------------------
    @classmethod
    def from_cli(cls, cli_args: argparse.Namespace) -> Self:
        """Build Settings from defaults + env + CLI Namespace overlay."""
        base = cls()

        # Overlay any CLI arg that was explicitly set (not None / not default)
        cli_overrides: dict[str, Any] = {}
        for field_name in cls._cli_fields():
            val = getattr(cli_args, field_name, None)
            if val is not None:
                cli_overrides[field_name] = val

        return cls(**{**base.__dict__, **cli_overrides})

    @staticmethod
    def _cli_fields() -> list[str]:
        """Field names that can come from CLI args."""
        return [
            "model",
            "base_url",
            "api_key",
            "streaming_enabled",
            "max_iterations",
            "max_output_lines",
            "max_output_panel_height",
            "session_dir",
            "reasoning_effort",
        ]


# Module-level singleton — callers can also construct their own Settings().
settings = Settings()
