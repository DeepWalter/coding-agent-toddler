"""EditFile — the position a replacement starts at.

The web card uses it to reveal the exact occurrence after the edit:
``new_string`` may already occur elsewhere in the file, and a text search
would land on the wrong one.  Line is 1-based, column 0-based, both measured
in the file as the call read it (universal newlines).
"""

from __future__ import annotations

import pytest

from toddler.tools.files import EditFile

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("# head\n\nold one\nold two\ntail\n", encoding="utf-8")
    return path


# ============================================================================
# Recorded position
# ============================================================================


class TestRecordedPosition:
    async def test_the_replacement_start_is_recorded_as_line_and_column(
        self, seeded,
    ):
        result = await EditFile().execute(
            file_path=str(seeded),
            old_string="old one\nold two",
            new_string="new one\nnew two",
        )

        assert result.success
        assert result.metadata["line_start"] == 3
        assert result.metadata["column_start"] == 0

    async def test_a_match_inside_a_line_records_its_column(self, seeded):
        result = await EditFile().execute(
            file_path=str(seeded),
            old_string="one",
            new_string="ONE",
        )

        assert result.success
        assert result.metadata["line_start"] == 3
        assert result.metadata["column_start"] == 4  # after "old "

    async def test_replace_all_records_the_first_occurrence(self, seeded):
        result = await EditFile().execute(
            file_path=str(seeded),
            old_string="old",
            new_string="OLD",
            replace_all=True,
        )

        assert result.success
        assert result.metadata["occurrences_replaced"] == 2
        assert result.metadata["line_start"] == 3
        assert result.metadata["column_start"] == 0
