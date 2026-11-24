"""Tests for the exception hierarchy."""

from __future__ import annotations

from datetime import date

import pytest

from ibkr_fa.exceptions import (
    ConfigError,
    IbkrFaError,
    MissingFxRateError,
    ParseError,
)


def test_parse_error_with_column() -> None:
    err = ParseError(
        section="Open Positions",
        row_index=3,
        column="Quantity",
        message="not a number",
        raw_value="abc",
    )
    s = str(err)
    assert s == "ParseError in section 'Open Positions' at row 3, column 'Quantity': not a number"
    assert "abc" not in s


def test_parse_error_without_column_omits_column_clause() -> None:
    err = ParseError(
        section="Trades",
        row_index=0,
        column=None,
        message="row has wrong number of fields",
        raw_value="x,y",
    )
    s = str(err)
    assert s == "ParseError in section 'Trades' at row 0: row has wrong number of fields"
    assert "x,y" not in s


def test_parse_error_str_ends_at_the_message() -> None:
    """#1069: ``str()`` never prints ``raw_value``, so a non-empty value
    can't reach the message."""
    expected = "ParseError in section 'S' at row 1, column 'C': m"
    empty = ParseError(section="S", row_index=1, column="C", message="m", raw_value="")
    assert str(empty) == expected
    named = ParseError(
        section="S", row_index=1, column="C", message="m", raw_value="Synthetic Holder Two"
    )
    assert str(named) == expected
    assert "Synthetic Holder Two" not in str(named)


def test_missing_fx_rate_error_with_last_available() -> None:
    """The message names the currency, the latest prior archive rate
    date and the carry-forward limit (#1047). The error has no
    requested (event) date to print (#1368)."""
    err = MissingFxRateError(
        currency="USD",
        last_available=date(2026, 12, 1),
        max_carry_forward_days=14,
    )
    s = str(err)
    assert s == (
        "MissingFxRateError: no USD rate available; latest available prior rate is "
        "2026-12-01, beyond the carry-forward limit of 14 days. The bundled TTBR "
        "archive may need updating; see https://github.com/jay-tau/ibkr-fa for the "
        "latest snapshot."
    )


def test_missing_fx_rate_error_with_no_prior_rate() -> None:
    err = MissingFxRateError(
        currency="USD",
        last_available=None,
        max_carry_forward_days=14,
    )
    s = str(err)
    assert s == (
        "MissingFxRateError: no USD rate available; no prior rate available in the "
        "bundled archive. The bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )


def test_missing_fx_rate_error_renders_the_policy_kind() -> None:
    err = MissingFxRateError(
        currency="USD",
        last_available=date(2025, 3, 29),
        max_carry_forward_days=0,
        policy_kind="Schedule FA exact-date FX lookup",
    )
    s = str(err)
    assert s == (
        "MissingFxRateError: no USD rate available [Schedule FA exact-date FX lookup]; "
        "latest available prior rate is 2025-03-29, beyond the carry-forward limit of "
        "0 days. The bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )


def test_config_error_message_format() -> None:
    err = ConfigError(field="account.opening_date", message="must be ISO date")
    assert str(err) == "ConfigError at field 'account.opening_date': must be ISO date"


@pytest.mark.parametrize(
    "exc",
    [
        ParseError(section="x", row_index=0, column=None, message="m", raw_value="r"),
        MissingFxRateError(
            currency="USD",
            last_available=None,
            max_carry_forward_days=1,
        ),
        ConfigError(field="x", message="m"),
    ],
)
def test_all_exceptions_descend_from_ibkr_fa_error(exc: IbkrFaError) -> None:
    assert isinstance(exc, IbkrFaError)
    assert isinstance(exc, Exception)


def test_exceptions_are_frozen() -> None:
    err = ConfigError(field="x", message="m")
    with pytest.raises(AttributeError):
        err.field = "y"  # ty: ignore[invalid-assignment]


def test_can_catch_specific_subclass_via_base() -> None:
    with pytest.raises(IbkrFaError):
        raise ParseError(section="x", row_index=0, column=None, message="m", raw_value="r")
