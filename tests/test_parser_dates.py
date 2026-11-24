"""Tests for :mod:`ibkr_fa.parser._dates` shared date helpers."""

from __future__ import annotations

from datetime import date

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser._dates import parse_yyyymmdd, parse_yyyymmdd_with_time

# ---- parse_yyyymmdd (bare YYYYMMDD) ----------------------------------------


def test_parse_yyyymmdd_accepts_valid_date() -> None:
    assert parse_yyyymmdd("20260818", section="Sec", row_index=0, column="ReportDate") == date(
        2026, 8, 18
    )


def test_parse_yyyymmdd_rejects_with_time_suffix() -> None:
    """Bare-date contract: ``;HHMMSS`` must NOT be silently accepted."""
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd("20260818;104512", section="Sec", row_index=0, column="ReportDate")
    assert "8 digits" in exc.value.message


@pytest.mark.parametrize(
    "raw",
    ["", "2026081", "202608188", "2026-08-18", "abcd0818", "20260818 ", " 20260818"],
)
def test_parse_yyyymmdd_rejects_non_8_digit(raw: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd(raw, section="Sec", row_index=3, column="ReportDate")
    assert exc.value.section == "Sec"
    assert exc.value.row_index == 3
    assert exc.value.column == "ReportDate"
    assert exc.value.message == "must be exactly 8 digits in YYYYMMDD format"
    assert exc.value.raw_value == ""


def test_parse_yyyymmdd_rejects_impossible_calendar_date() -> None:
    """Syntactically valid 8 digits but Feb 30 isn't a real date."""
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd("20260230", section="Sec", row_index=0, column="OpenDateTime")
    assert exc.value.message == "must be a real calendar date"
    assert exc.value.raw_value == ""


# ---- parse_yyyymmdd_with_time (YYYYMMDD;HHMMSS) ----------------------------


def test_parse_yyyymmdd_with_time_accepts_valid() -> None:
    assert parse_yyyymmdd_with_time(
        "20260818;104512", section="Sec", row_index=0, column="OpenDateTime"
    ) == date(2026, 8, 18)


def test_parse_yyyymmdd_with_time_discards_time_component() -> None:
    """Two rows differing only in time must produce equal dates."""
    morning = parse_yyyymmdd_with_time("20260818;104512", section="Sec", row_index=0, column="X")
    afternoon = parse_yyyymmdd_with_time("20260818;151500", section="Sec", row_index=0, column="X")
    assert morning == afternoon == date(2026, 8, 18)


def test_parse_yyyymmdd_with_time_rejects_bare_date() -> None:
    """Strict-shape contract: bare ``YYYYMMDD`` must NOT be accepted by
    the with-time helper. Open Positions LOT uses the bare form; if a
    caller mixed the helpers that error would be silently swallowed."""
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd_with_time("20260818", section="Sec", row_index=0, column="X")
    assert "YYYYMMDD;HHMMSS" in exc.value.message


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "20260818;",  # missing time half
        "20260818;9999",  # time too short
        "20260818;9999999",  # time too long
        "20260818T104512",  # wrong separator (ISO-style)
        "20260818:104512",  # colon, not semicolon
        "20260818 104512",  # space, not semicolon
        ";20260818104512",  # semicolon at wrong position
        "abcd0818;104512",  # non-digit date
        "20260818;abcdef",  # non-digit time
    ],
)
def test_parse_yyyymmdd_with_time_rejects_malformed_shape(raw: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd_with_time(raw, section="Sec", row_index=2, column="X")
    assert exc.value.row_index == 2
    assert exc.value.message == "must be in YYYYMMDD;HHMMSS format (e.g. '20250610;143015')"
    assert exc.value.raw_value == ""


def test_parse_yyyymmdd_with_time_rejects_impossible_date() -> None:
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd_with_time(
            "20260230;000000", section="Sec", row_index=0, column="OpenDateTime"
        )
    assert exc.value.message == "must be a real calendar date"
    assert exc.value.raw_value == ""


def test_parse_yyyymmdd_with_time_rejects_impossible_time() -> None:
    """Hour=24, minute=99 etc. must fail loudly (not just silently
    drop the time component)."""
    with pytest.raises(ParseError) as exc:
        parse_yyyymmdd_with_time(
            "20260818;249999", section="Sec", row_index=0, column="OpenDateTime"
        )
    assert exc.value.message == "must have a real wall-clock time in HHMMSS"
    assert exc.value.raw_value == ""


def test_parse_yyyymmdd_with_time_accepts_midnight() -> None:
    """``000000`` (midnight) is a valid wall-clock time."""
    assert parse_yyyymmdd_with_time(
        "20260818;000000", section="Sec", row_index=0, column="X"
    ) == date(2026, 8, 18)


def test_parse_yyyymmdd_with_time_accepts_end_of_day() -> None:
    """``235959`` (one second before midnight) is a valid wall-clock time."""
    assert parse_yyyymmdd_with_time(
        "20260818;235959", section="Sec", row_index=0, column="X"
    ) == date(2026, 8, 18)
