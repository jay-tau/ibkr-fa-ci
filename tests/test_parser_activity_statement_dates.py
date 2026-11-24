"""Tests for the shared Activity Statement date helper
(:mod:`ibkr_fa.parser.activity_statement._dates`)."""

from datetime import date

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement._dates import parse_statement_date
from tests._statement_period import ALL_DATES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("18-02-2026", date(2026, 2, 18)),  # DD-MM-YYYY (IBKR India flavor)
        ("2026-02-18", date(2026, 2, 18)),  # YYYY-MM-DD (IBKR LLC / US flavor)
        ("30-04-2025", date(2025, 4, 30)),
        ("2025-12-31", date(2025, 12, 31)),
        ("01-01-2026", date(2026, 1, 1)),
    ],
)
def test_both_formats_accepted(raw: str, expected: date) -> None:
    assert parse_statement_date(raw, section="Dividends", row_index=0, period=ALL_DATES) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "18/02/2026",  # slashes
        "18-2-2026",  # un-padded month
        "february-18-2026",  # textual month
        "30.04.2025",  # dot separator
        "April 30, 2025",  # long form
        "2026-02-18, 10:30:00",  # Trades-style datetime (has time + comma)
        "",  # empty
        "2026-02-18\n",  # trailing newline (fullmatch rejects)
    ],
)
def test_malformed_shape_rejected(raw: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_statement_date(raw, section="Dividends", row_index=0, period=ALL_DATES)
    assert exc.value.column == "Date"
    assert exc.value.message == "must match DD-MM-YYYY or YYYY-MM-DD"
    assert exc.value.raw_value == ""


@pytest.mark.parametrize(
    "raw",
    [
        "30-02-2026",  # DD-MM-YYYY: Feb has no 30th
        "2026-02-30",  # YYYY-MM-DD: Feb has no 30th
        "2026-13-05",  # YYYY-MM-DD: month 13
        "05-13-2026",  # DD-MM-YYYY: month 13 (not silently transposed)
        "00-01-2026",  # day 0
    ],
)
def test_well_shaped_but_invalid_calendar_date_rejected(raw: str) -> None:
    """Inputs that match a shape but are not real calendar dates take
    the distinct ``must be a real calendar date`` branch — they must NOT
    be silently transposed (e.g. ``05-13-2026`` is month 13, not a
    valid ISO 13th-May reinterpretation)."""
    with pytest.raises(ParseError) as exc:
        parse_statement_date(raw, section="Dividends", row_index=0, period=ALL_DATES)
    assert exc.value.column == "Date"
    assert exc.value.message == "must be a real calendar date"
    assert exc.value.raw_value == ""


def test_error_tags_propagate_section_column_and_row_index() -> None:
    """The caller-supplied section / column / row_index are threaded
    onto the ParseError so each sister parser keeps its own diagnostics."""
    with pytest.raises(ParseError) as exc:
        parse_statement_date(
            "bad", section="Withholding Tax", row_index=7, column="Settle Date", period=ALL_DATES
        )
    assert exc.value.section == "Withholding Tax"
    assert exc.value.column == "Settle Date"
    assert exc.value.row_index == 7
    assert exc.value.raw_value == ""
