"""Presentation helpers for quantities that get long — token counts and
durations, as the console labels read them.

The terminal mirrors the web console's helpers (``website/src/utils.ts``,
``ThinkingBlock.vue``) so a thought is described the same way in both: a
rough count while it streams, a span once it is over.
"""

from __future__ import annotations

import math

__all__ = ["estimate_tokens", "format_span", "format_tokens"]


def _round_half_up(value: float) -> int:
    """JS's ``Math.round``, which the web helpers use.

    Python's ``round`` is half-to-even, which would put a 1050-token
    buffer on "1.0K" where the console says "1.1K".
    """
    return math.floor(value + 0.5)


def estimate_tokens(text: str) -> int:
    """Rough token count for a streamed buffer.

    The CLI owns a real tokenizer, but it lives inside the context window
    manager and the live marker repaints ~10x/s, so the estimate follows
    the same rule the web console uses: non-ASCII (CJK, emoji) costs about
    a token per character, ASCII prose about four characters per token.
    The API's own count arrives with the turn's usage.
    """
    wide = sum(1 for ch in text if ord(ch) > 0x7F)
    return math.ceil((len(text) - wide) / 4) + wide


def format_tokens(n: int) -> str:
    """A count as the label prints it: exact under a thousand, then one
    decimal under a K, then an M."""
    if n < 1000:
        return str(n)
    # Round to the printed decimal *before* picking the unit, so a count
    # just under a million reads "1.0M" instead of "1000.0K".
    thousands = _round_half_up(n / 100) / 10
    if thousands < 1000:
        return f"{thousands:.1f}K"
    return f"{_round_half_up(n / 100_000) / 10:.1f}M"


def format_span(seconds: float) -> str:
    """A duration as the label prints it.

    A sub-second thought would round to a nonsensical "0 seconds", and a
    long one reads better in minutes than as "125 seconds".
    """
    secs = _round_half_up(seconds)
    if secs < 1:
        return "less than a second"
    if secs < 60:
        return f"{secs} second" if secs == 1 else f"{secs} seconds"
    mins, rest = divmod(secs, 60)
    minutes = f"{mins} minute" if mins == 1 else f"{mins} minutes"
    return f"{minutes} {rest} seconds" if rest else minutes
