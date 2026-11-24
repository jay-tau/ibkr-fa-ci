"""Synthetic StockPlan workbooks shared by the StockPlan no-echo and refusal chain tests.

The report constants, sheet and file names, row builders and workbook builders
for the Dividend, Holdings and Capital Gain/Loss exports, the StockPlan parser
for each sheet, and a check that a parse refuses with no escaped warning
and nothing on stderr. Every workbook here is synthetic.
"""

from __future__ import annotations

import datetime as _dt
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path

import openpyxl
import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from ibkr_fa.parser.stockplan.holdings import parse_stockplan_holdings
from ibkr_fa.types import Isin, Ticker
from tests._stockplan_holdings import HOLDINGS_HEADER, market_value, quote_history_rows
from tests._stockplan_report_writers import (
    CAPITAL_GAINS_HEADER,
    CAPITAL_GAINS_SECTION_ROW,
    DIVIDEND_REPORT_HEADER,
    RELEASES_HEADER,
    STOCKPLAN_USD_FOOTER,
)

BOOK_SYMBOL = Ticker("XQCB")
BOOK_ISIN = Isin("US99971P1012")
_NO_RECORDS = "No records found"
BOOK_RUN_ON = "02-Apr-2026"
BOOK_PERIOD = "01-Apr-2025 to 31-Mar-2026"

DIVIDEND_SHEET = "IN - Dividend Report"
DIVIDEND_FILE = "IN - Dividend Report.xlsx"
_DIVIDEND = "Dividend Paid"
_WITHHOLDING = "IRS Nonresident Alien Withholding"

HOLDINGS_SHEET = "Holdings Report"
HOLDINGS_FILE = "Vested Share Holdings.xlsx"
_HOLDINGS_TITLE = "Synthetic Issuer - Holdings Report (report run on 14-Jul-2026)"
_HOLDINGS_FOOTER = (
    "Market Value Definition = Market Price as at Selected Date * Total Held Shares"
    "Acquisition Value Definition = Fair Market Value at Vest/Exercise * Total Held Shares"
)
_QUOTES_SHEET = "Quote History"
_QUOTE_TITLE, _QUOTE_HEADER, _CASH_QUOTE, _STOCK_QUOTE = quote_history_rows("XQCB - NASDAQ")
_QUOTES = (_QUOTE_TITLE, _QUOTE_HEADER, _CASH_QUOTE, _STOCK_QUOTE)

RELEASES_SHEET = "Releases Report"
CAPITAL_GAINS_SHEET = "Capital Gains Report"
CAPITAL_GAINS_FILE = "IN - Capital Gain_Loss Report.xlsx"


def _save(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        worksheet = workbook.create_sheet(title)
        for row in rows:
            worksheet.append(row)
    workbook.save(path)
    return path


def dividend_row(activity: str = _DIVIDEND, cash: object = 345.67) -> list[object]:
    return [_dt.datetime(2025, 12, 10), activity, cash, "XQCB", None, None, 0.0]


def book_withholding_row(cash: object = -51.85) -> list[object]:
    return dividend_row(_WITHHOLDING, cash)


def dividend_title(*, run_on: str = BOOK_RUN_ON, period: str = BOOK_PERIOD) -> str:
    return (
        f"Synthetic Issuer - IN - Dividend Report (report run on {run_on} "
        f"for reporting period {period})"
    )


def dividend_book(
    tmp_path: Path,
    *data: list[object],
    title: str | None = None,
    header: tuple[object, ...] = tuple(DIVIDEND_REPORT_HEADER),
    footer: bool = True,
    sheet: str = DIVIDEND_SHEET,
    extra_sheet: str | None = None,
) -> Path:
    rows: list[list[object]] = [[title or dividend_title()], list(header), *data]
    if footer:
        rows.append([STOCKPLAN_USD_FOOTER])
    sheets: dict[str, list[list[object]]] = {sheet: rows}
    if extra_sheet is not None:
        sheets[extra_sheet] = [["x"]]
    return _save(tmp_path / DIVIDEND_FILE, sheets)


def cash_holding_row(employee: str = "E1234567") -> list[object]:
    return [employee, "Synthetic Plan", "Cash - USD", 4.18, 4.18, 4.18]


def stock_holding_row(employee: str = "E1234567") -> list[object]:
    return [employee, "Synthetic Plan", "XQCB - NASDAQ", 26.374, 5823.47, market_value(26.374)]


def holdings_book(
    tmp_path: Path,
    *data: list[object],
    title: str = _HOLDINGS_TITLE,
    quotes: Sequence[list[object]] | None = _QUOTES,
) -> Path:
    """A Holdings workbook; ``quotes`` is its ``Quote History`` sheet, or None for none."""
    rows: list[list[object]] = [[title], list(HOLDINGS_HEADER), *data, [_HOLDINGS_FOOTER]]
    sheets = {HOLDINGS_SHEET: rows}
    if quotes is not None:
        sheets[_QUOTES_SHEET] = list(quotes)
    return _save(tmp_path / HOLDINGS_FILE, sheets)


def vest_row(order: str = "RB0000A1") -> list[object]:
    return [
        _dt.datetime(2025, 8, 14),
        order,
        "XQCB",
        "Release",
        "Complete",
        196.42,
        9.012,
        0.0,
        6.214,
        "Fractional Shares",
    ]


def sale_row() -> list[object]:
    return [_dt.datetime(2026, 2, 10), 190.25, 2.375, "XQCB", 475.63, _dt.datetime(2025, 8, 14)]


def capital_gains_book(
    tmp_path: Path,
    *,
    vests: tuple[list[object], ...] = (),
    sales: tuple[list[object], ...] = ([_NO_RECORDS],),
    releases_title: str | None = None,
    run_on: str = BOOK_RUN_ON,
    period: str = BOOK_PERIOD,
    section: tuple[object, ...] = tuple(CAPITAL_GAINS_SECTION_ROW),
    cg_sheet: str = CAPITAL_GAINS_SHEET,
) -> Path:
    releases_title = releases_title or (
        "Synthetic Issuer - Releases Report (report run on 02-Apr-2026)"
    )
    cg_title = (
        f"Synthetic Issuer - Capital Gains Report (report run on {run_on} "
        f"for reporting period {period})"
    )
    releases: list[list[object]] = [
        [releases_title],
        list(RELEASES_HEADER),
        *vests,
        [STOCKPLAN_USD_FOOTER],
    ]
    sales_sheet: list[list[object]] = [
        [cg_title],
        list(section),
        list(CAPITAL_GAINS_HEADER),
        *sales,
        [STOCKPLAN_USD_FOOTER],
    ]
    return _save(tmp_path / CAPITAL_GAINS_FILE, {RELEASES_SHEET: releases, cg_sheet: sales_sheet})


STOCKPLAN_PARSERS: dict[str, Callable[[Path], object]] = {
    DIVIDEND_SHEET: lambda path: parse_stockplan_dividend_report(
        path, symbol=BOOK_SYMBOL, isin=BOOK_ISIN
    ),
    HOLDINGS_SHEET: lambda path: parse_stockplan_holdings(
        path, symbol=BOOK_SYMBOL, listing_exchange="NASDAQ"
    ),
    _QUOTES_SHEET: lambda path: parse_stockplan_holdings(
        path, symbol=BOOK_SYMBOL, listing_exchange="NASDAQ"
    ),
    RELEASES_SHEET: lambda path: parse_stockplan_releases(path, symbol=BOOK_SYMBOL),
    CAPITAL_GAINS_SHEET: lambda path: parse_stockplan_capital_gains_report(
        path, symbol=BOOK_SYMBOL
    ),
}


def refusal_without_escaped_warnings(
    capsys: pytest.CaptureFixture[str], parse: Callable[[], object]
) -> ParseError:
    """The ``ParseError`` ``parse`` raises; no warning escapes and nothing reaches stderr."""
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        with pytest.raises(ParseError) as excinfo:
            parse()
    assert escaped == []
    assert capsys.readouterr().err == ""
    return excinfo.value
