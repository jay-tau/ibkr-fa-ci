"""Fidelity NetBenefits refusals chain no exception (#1209).

Each refusal is raised after its handler has ended. Before, a chained error kept
the input: a month-key error names the month token, a decode error holds the
file's bytes and, on Python 3.14, a calendar error names the date's year. Each
test pins the unchanged refusal, then checks that no input marker is anywhere in
its exception graph (suppressed links included), then that it has no cause or
context. A calendar marker can only be printed on 3.14, so the last check guards
those sites on older runtimes.

The inputs are module constants because a rendered traceback prints the line that
calls the parser. No year can match a traceback line number: each is past 9000,
except the data row's 2093, whose traceback only runs through files far shorter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.netbenefits.confirmation_pdf import parse_confirmation
from ibkr_fa.parser.netbenefits.lots_csv import parse_open_lots, read_closed_lots, read_open_lots
from ibkr_fa.parser.netbenefits.statement_pdf import parse_statement
from tests._crash_reports import assert_unchained_refusal
from tests._netbenefits import CLOSED_WITH_ROW_CSV, OPEN_CSV
from tests._netbenefits import CONF_DEC_STREET_ALIGNED as CONF_DEC
from tests._netbenefits_statement_pdf import statement_text as _statement
from tests._text_edits import replace_once as _replace_once

# Input markers. The month token fits the confirmation's ``[A-Z]{3}`` month shape
# but names no month.
_MONTH = "QZM"
_PURCHASE_YEAR = "9106"
# The data row's ``MM-DD-YY`` year ``93`` is read as 2093.
_TRADE_YEAR = "2093"
_LOTS_YEAR = "9107"
_LOTS_BYTES = "QZLOTSBYTES"
_STATEMENT_YEAR = "9108"

_CONFIRMATION = "Fidelity ESPP confirmation"
_OPEN_LOTS = "Fidelity open lots"
_STATEMENT = "Fidelity SPS statement"
_OPEN_SOURCE = "open lots first/View open lots.csv"
_CLOSED_SOURCE = "closed lots first/View closed lots.csv"
_SPS_REPORT = "STOCK PLAN SERVICES REPORT"

# Confirmation dates: ``MMM/DD/YYYY`` (offering period, purchase date) and the
# data row's ``MM-DD-YY`` trade and settlement dates.
_OFFERING_START = "NOV/03/2025 -"
_PURCHASE_DATE = "PLAN on DEC/31/2025."
_BAD_MONTH_START = f"{_MONTH}/03/2025 -"
_BAD_DAY_PURCHASE = f"PLAN on FEB/30/{_PURCHASE_YEAR}."
_CONF_UNKNOWN_MONTH = _replace_once(CONF_DEC, _OFFERING_START, _BAD_MONTH_START)
_CONF_PURCHASE_FEB_30 = _replace_once(CONF_DEC, _PURCHASE_DATE, _BAD_DAY_PURCHASE)
# Both failures: the offering start is read first, so its refusal wins.
_CONF_BOTH = _replace_once(_CONF_UNKNOWN_MONTH, _PURCHASE_DATE, _BAD_DAY_PURCHASE)
_CONF_TRADE_FEB_30 = _replace_once(CONF_DEC, "12-31-25 01-05-26", "02-30-93 01-05-26")
# Control: a leap day at both confirmation date sites, cross-checks consistent.
_CONF_LEAP_DAY = _replace_once(
    _replace_once(
        _replace_once(CONF_DEC, "NOV/03/2025 - DEC/31/2025", "DEC/01/2027 - FEB/29/2028"),
        _PURCHASE_DATE,
        "PLAN on FEB/29/2028.",
    ),
    "12-31-25 01-05-26",
    "02-29-28 03-02-28",
)

# Lots dates: Fidelity's ``Mar-31-2026`` shape on the first data row.
_LOTS_FEB_30 = _replace_once(OPEN_CSV, "Mar-31-2026", f"Feb-30-{_LOTS_YEAR}")
_LOTS_LEAP_DAY = _replace_once(OPEN_CSV, "Mar-31-2026", "Feb-29-2028")


def _non_utf8(text: str) -> bytes:
    """``text`` as UTF-8, its first ``Short`` term cell replaced by the bytes
    marker and an invalid UTF-8 byte."""
    data = text.encode("utf-8")
    assert b",Short\n" in data
    return data.replace(b",Short\n", f",{_LOTS_BYTES}".encode() + b"\xff\n", 1)


_OPEN_BYTES = _non_utf8(OPEN_CSV)
_CLOSED_BYTES = _non_utf8(CLOSED_WITH_ROW_CSV)
# Both failures: the file is decoded before any cell is read.
_OPEN_BYTES_AND_FEB_30 = _non_utf8(_LOTS_FEB_30)

# Statement period: ``Month D, YYYY`` on the report header.
_STATEMENT_FEB_30 = _statement(
    _SPS_REPORT, f"February 30, {_STATEMENT_YEAR} - March 31, {_STATEMENT_YEAR}"
)
_STATEMENT_LEAP_DAY = _statement(_SPS_REPORT, "February 29, 2028 - March 31, 2028")


# --- confirmation_pdf._mdy and _short_date --------------------------------------


def test_confirmation_unknown_month_chains_no_month_key() -> None:
    with pytest.raises(ParseError) as caught:
        parse_confirmation(_CONF_UNKNOWN_MONTH)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_CONFIRMATION,
            row_index=0,
            column="offering start",
            message="offering start is not a valid date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity ESPP confirmation' at row 0, column "
            "'offering start': offering start is not a valid date"
        ),
        markers=(_MONTH,),
    )


def test_confirmation_purchase_feb_30_chains_no_calendar_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_confirmation(_CONF_PURCHASE_FEB_30)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_CONFIRMATION,
            row_index=0,
            column="purchase date",
            message="purchase date is not a valid date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity ESPP confirmation' at row 0, column "
            "'purchase date': purchase date is not a valid date"
        ),
        markers=(_PURCHASE_YEAR,),
    )


def test_confirmation_bad_month_and_bad_day_refuses_on_the_offering_start() -> None:
    with pytest.raises(ParseError) as caught:
        parse_confirmation(_CONF_BOTH)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_CONFIRMATION,
            row_index=0,
            column="offering start",
            message="offering start is not a valid date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity ESPP confirmation' at row 0, column "
            "'offering start': offering start is not a valid date"
        ),
        markers=(_MONTH,),
    )


def test_confirmation_trade_feb_30_chains_no_calendar_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_confirmation(_CONF_TRADE_FEB_30)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_CONFIRMATION,
            row_index=0,
            column="trade date",
            message="trade date is not a valid date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity ESPP confirmation' at row 0, column "
            "'trade date': trade date is not a valid date"
        ),
        markers=(_TRADE_YEAR,),
    )


def test_confirmation_leap_day_control_parses() -> None:
    purchase = parse_confirmation(_CONF_LEAP_DAY)
    assert (
        purchase.offering_period_start,
        purchase.offering_period_end,
        purchase.purchase_date,
        purchase.settlement_date,
    ) == (date(2027, 12, 1), date(2028, 2, 29), date(2028, 2, 29), date(2028, 3, 2))


# --- lots_csv._date and _read_csv_text ------------------------------------------


def test_lots_feb_30_chains_no_calendar_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_open_lots(_LOTS_FEB_30)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_OPEN_LOTS,
            row_index=1,
            column="Date acquired",
            message="not a valid calendar date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity open lots' at row 1, column "
            "'Date acquired': not a valid calendar date"
        ),
        markers=(_LOTS_YEAR,),
    )


def test_lots_leap_day_control_parses() -> None:
    assert parse_open_lots(_LOTS_LEAP_DAY)[0].acquire_date == date(2028, 2, 29)


@pytest.mark.parametrize(
    ("read", "data", "source"),
    [
        pytest.param(read_open_lots, _OPEN_BYTES, _OPEN_SOURCE, id="open"),
        pytest.param(read_closed_lots, _CLOSED_BYTES, _CLOSED_SOURCE, id="closed"),
    ],
)
def test_non_utf8_lots_chain_no_bytes(
    tmp_path: Path, read: Callable[..., object], data: bytes, source: str
) -> None:
    path = tmp_path / "lots.csv"
    path.write_bytes(data)
    with pytest.raises(ParseError) as caught:
        read(path, source=source)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=source,
            row_index=0,
            column=None,
            message="the file is not UTF-8 text",
            raw_value="",
        ),
        text=f"ParseError in section {source!r} at row 0: the file is not UTF-8 text",
        markers=(_LOTS_BYTES,),
    )


def test_non_utf8_lots_with_feb_30_refuses_on_the_encoding(tmp_path: Path) -> None:
    path = tmp_path / "lots.csv"
    path.write_bytes(_OPEN_BYTES_AND_FEB_30)
    with pytest.raises(ParseError) as caught:
        read_open_lots(path, source=_OPEN_SOURCE)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_OPEN_SOURCE,
            row_index=0,
            column=None,
            message="the file is not UTF-8 text",
            raw_value="",
        ),
        text=(
            "ParseError in section 'open lots first/View open lots.csv' at row 0: "
            "the file is not UTF-8 text"
        ),
        markers=(_LOTS_BYTES, _LOTS_YEAR),
    )


# --- statement_pdf._date --------------------------------------------------------


def test_statement_period_feb_30_chains_no_calendar_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_statement(_STATEMENT_FEB_30)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section=_STATEMENT,
            row_index=0,
            column="report header: period start",
            message="not a valid calendar date",
            raw_value="",
        ),
        text=(
            "ParseError in section 'Fidelity SPS statement' at row 0, column "
            "'report header: period start': not a valid calendar date"
        ),
        markers=(_STATEMENT_YEAR,),
    )


def test_statement_leap_day_control_parses() -> None:
    data = parse_statement(_STATEMENT_LEAP_DAY)
    assert (data.period_start, data.period_end) == (date(2028, 2, 29), date(2028, 3, 31))
