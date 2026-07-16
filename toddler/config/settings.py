"""Settings loader — env vars, optional config file, CLI arg overlay."""

from __future__ import annotations

import argparse
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


@dataclass
class Settings:
    """Resolved configuration — defaults ← env vars ← CLI args.

    Instantiate with optional ``cli_args`` from argparse to let CLI flags
    take highest precedence.
    """

    # --- LLM ---
    model: str = field(
        default_factory=lambda: _env("DEEPSEEK_MODEL", defaults.DEFAULT_MODEL)
    )
    base_url: str = field(
        default_factory=lambda: _env("DEEPSEEK_BASE_URL", defaults.DEFAULT_BASE_URL)
    )
    api_key: str = field(
        default_factory=lambda: _env("DEEPSEEK_API_KEY", "")
    )
    context_window: int = field(
        default_factory=lambda: _env_int("TODDLER_CONTEXT_WINDOW", defaults.DEFAULT_CONTEXT_WINDOW)
    )

    # --- Agent ---
    max_iterations: int = field(
        default_factory=lambda: _env_int("TODDLER_MAX_ITERATIONS", defaults.DEFAULT_MAX_ITERATIONS)
    )
    max_tokens_per_response: int = field(
        default_factory=lambda: _env_int("TODDLER_MAX_TOKENS", defaults.DEFAULT_MAX_TOKENS_PER_RESPONSE)
    )
    temperature: float = float(
        _env("TODDLER_TEMPERATURE", str(defaults.DEFAULT_TEMPERATURE))
    )

    # --- Permissions ---
    auto_approve_read: bool = field(
        default_factory=lambda: _env_bool("TODDLER_AUTO_APPROVE_READ", defaults.AUTO_APPROVE_READ)
    )
    confirm_write: bool = field(
        default_factory=lambda: _env_bool("TODDLER_CONFIRM_WRITE", defaults.CONFIRM_WRITE)
    )
    confirm_shell_dangerous: bool = field(
        default_factory=lambda: _env_bool("TODDLER_CONFIRM_SHELL_DANGEROUS", defaults.CONFIRM_SHELL_DANGEROUS)
    )

    # --- Streaming ---
    streaming_enabled: bool = field(
        default_factory=lambda: _env_bool("TODDLER_STREAMING", defaults.STREAMING_ENABLED)
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
            "session_dir",
        ]


# Module-level singleton — callers can also construct their own Settings().
settings = Settings()
