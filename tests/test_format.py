"""Presentation helpers — token counts and spans as the console prints them.

The boundaries are the point: the console abbreviates, and an off-by-one
at a unit change reads as a bug on screen ("1000.0K", "0 seconds").
"""

from __future__ import annotations

import pytest

from toddler.utils.format import estimate_tokens, format_span, format_tokens


class TestFormatTokens:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0"),
            (1, "1"),
            (27, "27"),
            (999, "999"),
            # A thousand is the first abbreviation, and the halves round up
            # like the web console's Math.round, not Python's round().
            (1000, "1.0K"),
            (1049, "1.0K"),
            (1050, "1.1K"),
            (12345, "12.3K"),
            (999_949, "999.9K"),
            # The unit is chosen after rounding, so this reads as 1.0M and
            # never as "1000.0K".
            (999_950, "1.0M"),
            (1_234_567, "1.2M"),
        ],
    )
    def test_boundaries(self, n: int, expected: str):
        assert format_tokens(n) == expected


class TestFormatSpan:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "less than a second"),
            (0.2, "less than a second"),
            (0.5, "1 second"),
            (1.0, "1 second"),
            (12.4, "12 seconds"),
            (59.6, "1 minute"),
            (60.0, "1 minute"),
            (125.0, "2 minutes 5 seconds"),
            (3600.0, "60 minutes"),
        ],
    )
    def test_boundaries(self, seconds: float, expected: str):
        assert format_span(seconds) == expected


class TestEstimateTokens:
    def test_empty_buffer(self):
        assert estimate_tokens("") == 0

    def test_ascii_counts_four_characters_per_token(self):
        assert estimate_tokens("x" * 4200) == 1050

    def test_a_partial_token_still_counts(self):
        # Ceiling, so the marker shows a number from the first fragment
        # instead of "0 tokens".
        assert estimate_tokens("x") == 1

    def test_non_ascii_costs_a_token_per_character(self):
        assert estimate_tokens("思考" * 10) == 20

    def test_mixed_ascii_and_non_ascii(self):
        # 8 ASCII characters (2 tokens) + 3 wide ones (3 tokens).
        assert estimate_tokens("thoughts思考默") == 5
