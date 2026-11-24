"""#1209: an invalid CMA report-period date refuses without chaining its
calendar error.

Python 3.14's calendar error names the supplied year, so a chained
``ValueError`` kept it. Each date below passes the period regex and fails only
the calendar; the refusal keeps its class, fields and text, holds no year
marker anywhere in its exception graph and chains nothing. The year markers
are 9000 or later, so no traceback line number can match one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.fidelity_cma import parse_statement, parse_year_end_income_summary
from tests._crash_reports import assert_unchained_refusal
from tests._fidelity_cma import _ACCOUNT
from tests._fidelity_cma import YEAR_END_TEXT as _YEAR_END
from tests._fidelity_cma import monthly_statement_text as _statement

_SECTION = "Fidelity CMA monthly statement"
_INVALID_PERIOD = "statement contains an invalid period date"
_REFUSAL = ParseError(
    section=_SECTION, row_index=0, column=None, message=_INVALID_PERIOD, raw_value=""
)
_REFUSAL_TEXT = f"ParseError in section {_SECTION!r} at row 0: {_INVALID_PERIOD}"

# Year markers: the invalid period dates' years.
_YEAR = "9109"
_END_YEAR = "9207"

# A configured account the synthetic statement doesn't hold.
_OTHER_ACCOUNT = "Z99-999998"


def _february(period: str) -> str:
    """The synthetic January 2026 statement moved to February 9109 (not a leap
    year), with ``period`` as every page's report period."""
    text = _statement().replace("January 1, 2026 - January 31, 2026", period)
    text = text.replace("Jan 1, 2026", "Feb 1, 9109").replace("Jan 31, 2026", "Feb 28, 9109")
    text = re.sub(r"\b01/31\b", "02/28", text)
    return re.sub(r"\b01/([0-9]{2})\b", r"02/\1", text)


def _year_end(period: str) -> str:
    """The synthetic 2025 year-end report moved to 9109, with ``period`` as
    every page's report period."""
    return _YEAR_END.replace("2025", "9109").replace("January 1, 9109 - December 31, 9109", period)


_VALID_FEBRUARY = _february(f"February 1, {_YEAR} - February 28, {_YEAR}")
_BAD_START = _february(f"February 30, {_YEAR} - February 28, {_YEAR}")
_BAD_END = _february(f"February 1, {_YEAR} - February 30, {_YEAR}")
# Both period dates fail the calendar, each in its own year.
_BAD_START_AND_END = _february(f"February 30, {_YEAR} - February 31, {_END_YEAR}")
_BAD_YEAR_END_START = _year_end(f"February 30, {_YEAR} - December 31, {_YEAR}")


def _monthly(text: str, account: str = _ACCOUNT) -> object:
    return parse_statement(text, expected_currency="USD", expected_account_id=account)


def _annual(text: str) -> object:
    return parse_year_end_income_summary(
        text, expected_currency="USD", expected_account_id=_ACCOUNT
    )


def test_last_day_of_february_parses_as_the_period_end() -> None:
    statement = parse_statement(
        _VALID_FEBRUARY, expected_currency="USD", expected_account_id=_ACCOUNT
    )
    assert (statement.period_start, statement.period_end) == (date(9109, 2, 1), date(9109, 2, 28))


def test_year_end_period_in_the_marker_year_parses() -> None:
    summary = parse_year_end_income_summary(
        _year_end(f"January 1, {_YEAR} - December 31, {_YEAR}"),
        expected_currency="USD",
        expected_account_id=_ACCOUNT,
    )
    assert summary.tax_year == 9109


@pytest.mark.parametrize(
    ("parse", "text"),
    [
        pytest.param(_monthly, _BAD_START, id="monthly-start"),
        pytest.param(_monthly, _BAD_END, id="monthly-end"),
        pytest.param(_annual, _BAD_YEAR_END_START, id="year-end-start"),
    ],
)
def test_invalid_period_date_refuses_without_a_chain(
    parse: Callable[[str], object], text: str
) -> None:
    with pytest.raises(ParseError) as caught:
        parse(text)
    assert_unchained_refusal(caught.value, _REFUSAL, _REFUSAL_TEXT, markers=(_YEAR,))


def test_invalid_period_start_refuses_before_the_end_and_the_account() -> None:
    """The start date refuses first, ahead of the invalid end date and the
    account check, which on its own refuses with its own text."""
    with pytest.raises(ParseError) as account_only:
        _monthly(_VALID_FEBRUARY, _OTHER_ACCOUNT)
    assert account_only.value.message == "statement account does not match the configured account"

    with pytest.raises(ParseError) as caught:
        _monthly(_BAD_START_AND_END, _OTHER_ACCOUNT)
    assert_unchained_refusal(caught.value, _REFUSAL, _REFUSAL_TEXT, markers=(_YEAR,))
