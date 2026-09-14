"""Utility functions used across the Toddler package."""

from toddler.utils.cli import build_argparser, build_serve_argparser
from toddler.utils.format import estimate_tokens, format_span, format_tokens
from toddler.utils.logging import setup_logging

__all__ = [
    "build_argparser",
    "build_serve_argparser",
    "estimate_tokens",
    "format_span",
    "format_tokens",
    "setup_logging",
]
