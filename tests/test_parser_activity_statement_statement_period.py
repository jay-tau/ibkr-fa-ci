"""Tests for :mod:`ibkr_fa.parser.activity_statement.statement_period`."""

from __future__ import annotations

from datetime import date

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement.statement_period import (
    StatementPeriod,
    parse_statement_broker_names,
    parse_statement_period,
)
from tests._statement_period import statement_field_row as make_row

# ---- Happy-path parsing --------------------------------------------------


def test_full_cy_period_parsed() -> None:
    rows = [
        make_row("BrokerName", "Interactive Brokers LLC"),
        make_row("Period", "January 1, 2025 - December 31, 2025"),
    ]
    period = parse_statement_period(rows)
    assert period == StatementPeriod(date(2025, 1, 1), date(2025, 12, 31))


def test_fy_aligned_period_parses_successfully() -> None:
    """Apr-Mar FY period parses fine (validation happens separately)."""
    rows = [make_row("Period", "April 1, 2025 - March 31, 2026")]
    period = parse_statement_period(rows)
    assert period.start_date == date(2025, 4, 1)
    assert period.end_date == date(2026, 3, 31)


def test_extra_whitespace_around_separator() -> None:
    rows = [make_row("Period", "  January 1, 2025   -   December 31, 2025  ")]
    period = parse_statement_period(rows)
    assert period == StatementPeriod(date(2025, 1, 1), date(2025, 12, 31))


def test_month_name_case_insensitive() -> None:
    """IBKR capitalises consistently, but lower-case shouldn't fail
    (e.g., a locale variant or someone manually copying the field)."""
    rows = [make_row("Period", "january 1, 2025 - december 31, 2025")]
    period = parse_statement_period(rows)
    assert period.start_date == date(2025, 1, 1)


# ---- Rejection: parsing errors -------------------------------------------


def test_missing_period_field() -> None:
    rows = [make_row("BrokerName", "IBKR")]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert exc.value.column == "Period"
    assert "missing" in exc.value.message


def test_empty_rows() -> None:
    with pytest.raises(ParseError) as exc:
        parse_statement_period([])
    assert exc.value.column == "Period"


def test_malformed_period_shape() -> None:
    rows = [make_row("Period", "2025-01-01 to 2025-12-31")]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert exc.value.column == "Period"
    assert "expected shape" in exc.value.message


def test_unknown_month_name() -> None:
    rows = [make_row("Period", "Janurary 1, 2025 - December 31, 2025")]  # typo
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert "unrecognised month" in exc.value.message
    assert "janurary" not in str(exc.value).lower()


def test_invalid_calendar_date() -> None:
    """February 30 is not a real date — the parser must reject it
    rather than silently round."""
    rows = [make_row("Period", "February 30, 2025 - December 31, 2025")]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert "isn't a real calendar date" in exc.value.message


def test_reverse_range_rejected() -> None:
    rows = [make_row("Period", "December 31, 2025 - January 1, 2025")]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert "reverse range" in exc.value.message


def test_row_missing_field_name_column() -> None:
    """Defensive: if the section's column tuple changes upstream the
    parser must raise a clean error, not KeyError."""
    rows = [{"FieldName": "Period", "Field Value": "ignored"}]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert exc.value.column == "Field Name"


def test_row_after_period_missing_field_value_column() -> None:
    """#1092: every row is read, not just those up to the Period row."""
    rows = [
        make_row("Period", "January 1, 2025 - December 31, 2025"),
        {"Field Name": "WhenGenerated"},
    ]
    with pytest.raises(ParseError) as exc:
        parse_statement_period(rows)
    assert (exc.value.row_index, exc.value.column) == (1, "Field Value")
    assert exc.value.message == (
        "Statement section row is missing the 'Field Value' column; expected columns "
        "('Field Name', 'Field Value')."
    )


# ---- BrokerName ----------------------------------------------------------


def test_broker_names_returned_as_written_in_file_order() -> None:
    rows = [
        make_row("BrokerName", "Interactive Brokers LLC"),
        make_row("Period", "January 1, 2025 - December 31, 2025"),
        make_row("BrokerName", ""),
    ]
    assert parse_statement_broker_names(rows) == ("Interactive Brokers LLC", "")


def test_broker_names_empty_when_no_broker_name_row() -> None:
    rows = [make_row("Period", "January 1, 2025 - December 31, 2025")]
    assert parse_statement_broker_names(rows) == ()


def test_broker_names_row_missing_field_value_column() -> None:
    with pytest.raises(ParseError) as exc:
        parse_statement_broker_names([{"Field Name": "BrokerName"}])
    assert exc.value.column == "Field Value"
