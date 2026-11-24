"""StockPlan parser refusals echo no cell, row, sheet name, amount or date (#1043).

A column-shifted or malformed row can put the holder's name or account number
in any cell, and errors end up in screenshots and pasted issues. So every
StockPlan parser refusal names its sheet and row, the column (or column number)
when applicable, and the rule broken, in fixed text, with an empty ``raw_value``.
A workbook refusal names the file and the expected sheets, never a sheet name the workbook
provides, and an ``OSError`` gives its errno name, never its text or file name.
Each case pins the exact message and location, and checks that the listed
synthetic markers and payload values are absent from ``str(error)``. Every
workbook here is synthetic.
"""

from __future__ import annotations

import datetime as _dt
import errno
import re
import warnings
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import NoReturn
from xml.etree.ElementTree import ParseError as XMLParseError
from zipfile import BadZipFile, ZipFile

import openpyxl
import pytest
from openpyxl.utils.cell import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet._reader import WorkSheetParser

import ibkr_fa.parser.stockplan._xlsx as xlsx_mod
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from ibkr_fa.parser.stockplan.holdings import parse_stockplan_holdings
from ibkr_fa.xlsx_checks import ALLOWED_WARNING
from tests._crash_reports import assert_no_markers, assert_replaced_unchained
from tests._stockplan_holdings import market_value
from tests._stockplan_no_echo_books import (
    _CASH_QUOTE,
    _HOLDINGS_TITLE,
    _NO_RECORDS,
    _QUOTE_HEADER,
    _QUOTE_TITLE,
    _QUOTES,
    _QUOTES_SHEET,
    _STOCK_QUOTE,
)
from tests._stockplan_no_echo_books import BOOK_ISIN as _ISIN
from tests._stockplan_no_echo_books import BOOK_PERIOD as _PERIOD
from tests._stockplan_no_echo_books import BOOK_RUN_ON as _RUN_ON
from tests._stockplan_no_echo_books import BOOK_SYMBOL as _SYMBOL
from tests._stockplan_no_echo_books import CAPITAL_GAINS_FILE as _CG_FILE
from tests._stockplan_no_echo_books import CAPITAL_GAINS_SHEET as _CG_SHEET
from tests._stockplan_no_echo_books import DIVIDEND_FILE as _DIV_FILE
from tests._stockplan_no_echo_books import DIVIDEND_SHEET as _DIV_SHEET
from tests._stockplan_no_echo_books import HOLDINGS_FILE as _HOLDINGS_FILE
from tests._stockplan_no_echo_books import HOLDINGS_SHEET as _HOLDINGS_SHEET
from tests._stockplan_no_echo_books import RELEASES_SHEET as _RELEASES_SHEET
from tests._stockplan_no_echo_books import STOCKPLAN_PARSERS as _PARSERS
from tests._stockplan_no_echo_books import book_withholding_row as _wht_row
from tests._stockplan_no_echo_books import capital_gains_book as _cg_book
from tests._stockplan_no_echo_books import cash_holding_row as _cash_row
from tests._stockplan_no_echo_books import dividend_book as _dividend_book
from tests._stockplan_no_echo_books import dividend_row as _div_row
from tests._stockplan_no_echo_books import dividend_title as _dividend_title
from tests._stockplan_no_echo_books import holdings_book as _holdings_book
from tests._stockplan_no_echo_books import (
    refusal_without_escaped_warnings as _refusal_without_escaped_warnings,
)
from tests._stockplan_no_echo_books import sale_row as _sale_row
from tests._stockplan_no_echo_books import stock_holding_row as _stock_row
from tests._stockplan_no_echo_books import vest_row as _vest_row
from tests._stockplan_report_writers import (
    CAPITAL_GAINS_SECTION_ROW,
    DIVIDEND_REPORT_HEADER,
    STOCKPLAN_USD_FOOTER,
)
from tests._xlsx_package_edits import date_styled_overflow as _date_styled_overflow
from tests._xlsx_package_edits import edit_sheets as _edit_sheets
from tests._xlsx_package_edits import replace_once_in_sheet as _replace_once
from tests._xlsx_package_edits import set_cells as _set_cells
from tests._xlsx_package_edits import store_row_out_of_order as _out_of_order
from tests._xlsx_package_edits import worksheets as _worksheets

_HOLDER = "Synthetic Holder Two"
_ACCOUNT = "U1234567"
_STRERROR = "Synthetic Strerror Marker"
# ``ANN`` sits inside many words, so it is checked as a whole token.
_MARKERS = (_HOLDER, _ACCOUNT, _STRERROR, "ANN")
_MARKER_SHEET = f"{_HOLDER} {_ACCOUNT}"
_NAN = "NaN1234567"
_AMOUNT = 7654.32

# The day after both Capital Gain/Loss sheets' run date (#1176).
_AFTER_THE_RUN = _dt.datetime(2026, 4, 3)

# Synthetic payload values checked in addition to the markers; fixed schema
# constants may appear in refusals.
_VALUES = (
    "Synthetic Issuer",
    "Synthetic Plan",
    "E1234567",
    "E7654321",
    "RB0000A1",
    _NAN,
    str(_AMOUNT),
    "345.67",
    "51.85",
    "4.18",
    "26.374",
    "5823.47",
    "196.42",
    "9.012",
    "6.214",
    "190.25",
    "2.375",
    "475.63",
    "02-Apr-2026",
    "2026-04-02",
    "01-Apr-2025",
    "2025-04-01",
    "31-Mar-2026",
    "2026-03-31",
    "01-Feb-2026",
    "2026-02-01",
    "31-Apr-2025",
    "14-Jul-2026",
    "2026-07-14",
    "2025-12-10",
    "2026-04-15",
    "2025-08-14",
    "2026-02-10",
)

_IDENTITY_COLUMN = "Employee Number / Savings Plan Name"
_CASH_COLUMN = "Number of Shares / Acquisition Value / Market Value"

_NOT_AFTER = (
    "{file} was not run strictly after its reporting period ended, so it can miss later "
    "{missed}; re-export the report after the period ends"
)
_CONFIGURED = "does not match the configured [stockplan.security] symbol"


def _with(row: list[object], index: int, value: object) -> list[object]:
    """``row`` with cell ``index`` replaced by ``value``."""
    return [value if i == index else cell for i, cell in enumerate(row)]


def _dividend(
    *data: list[object],
    title: str | None = None,
    header: tuple[object, ...] = tuple(DIVIDEND_REPORT_HEADER),
    footer: bool = True,
    sheet: str = _DIV_SHEET,
    extra_sheet: str | None = None,
) -> Callable[[Path], object]:
    def run(tmp_path: Path) -> object:
        path = _dividend_book(
            tmp_path,
            *data,
            title=title,
            header=header,
            footer=footer,
            sheet=sheet,
            extra_sheet=extra_sheet,
        )
        return parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)

    return run


def _holdings(
    *data: list[object],
    title: str = _HOLDINGS_TITLE,
    quotes: Sequence[list[object]] | None = _QUOTES,
) -> Callable[[Path], object]:
    def run(tmp_path: Path) -> object:
        path = _holdings_book(tmp_path, *data, title=title, quotes=quotes)
        return parse_stockplan_holdings(path, symbol=_SYMBOL, listing_exchange="NASDAQ")

    return run


def _quotes(*rows: list[object]) -> Callable[[Path], object]:
    """A Holdings parse whose ``Quote History`` data rows are ``rows``."""
    return _holdings(_cash_row(), _stock_row(), quotes=(_QUOTE_TITLE, _QUOTE_HEADER, *rows))


def _releases(
    *vests: list[object], title: str | None = None, cg_sheet: str = _CG_SHEET
) -> Callable[[Path], object]:
    def run(tmp_path: Path) -> object:
        path = _cg_book(tmp_path, vests=vests, releases_title=title, cg_sheet=cg_sheet)
        return parse_stockplan_releases(path, symbol=_SYMBOL)

    return run


def _sales(
    *sales: list[object],
    run_on: str = _RUN_ON,
    period: str = _PERIOD,
    section: tuple[object, ...] = tuple(CAPITAL_GAINS_SECTION_ROW),
) -> Callable[[Path], object]:
    def run(tmp_path: Path) -> object:
        path = _cg_book(
            tmp_path,
            vests=(_vest_row(),),
            sales=sales,
            run_on=run_on,
            period=period,
            section=section,
        )
        return parse_stockplan_capital_gains_report(path, symbol=_SYMBOL)

    return run


def _cg_title(title: str) -> Callable[[Path], object]:
    def run(tmp_path: Path) -> object:
        path = _cg_book(tmp_path, vests=(_vest_row(),))
        workbook = openpyxl.load_workbook(path)
        workbook[_CG_SHEET].cell(row=1, column=1, value=title)
        workbook.save(path)
        return parse_stockplan_capital_gains_report(path, symbol=_SYMBOL)

    return run


def _numeric_cell_holding_text(tmp_path: Path) -> object:
    """A numeric cell whose stored value is text: openpyxl's error quotes it."""
    path = _dividend_book(tmp_path, _div_row())
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet = payloads["xl/worksheets/sheet1.xml"].decode("utf-8")
    edited = sheet.replace('<c r="G3" t="n"><v>0</v></c>', f'<c r="G3" t="n"><v>{_HOLDER}</v></c>')
    assert edited != sheet
    payloads["xl/worksheets/sheet1.xml"] = edited.encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    return parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def _not_a_workbook(tmp_path: Path) -> object:
    path = tmp_path / _DIV_FILE
    path.write_bytes(f"{_HOLDER} {_ACCOUNT}".encode())
    return parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


@dataclass(frozen=True, slots=True)
class _Case:
    id: str
    run: Callable[[Path], object]
    section: str
    row: int
    column: str | None
    message: str
    markers: tuple[str, ...] = (*_MARKERS, *_VALUES)
    crash: type[BaseException] | None = None
    graph_markers: tuple[str, ...] = ()


def _div(
    case_id: str,
    run: Callable[[Path], object],
    row: int,
    column: str | None,
    message: str,
    *,
    crash: type[BaseException] | None = None,
    graph_markers: tuple[str, ...] = (),
) -> _Case:
    return _Case(
        case_id, run, _DIV_SHEET, row, column, message, crash=crash, graph_markers=graph_markers
    )


def _hold(
    case_id: str,
    run: Callable[[Path], object],
    row: int,
    column: str | None,
    message: str,
    *,
    markers: tuple[str, ...] = (*_MARKERS, *_VALUES),
) -> _Case:
    return _Case(case_id, run, _HOLDINGS_SHEET, row, column, message, markers=markers)


def _quote(
    case_id: str,
    run: Callable[[Path], object],
    row: int,
    column: str | None,
    message: str,
    *,
    markers: tuple[str, ...],
) -> _Case:
    return _Case(case_id, run, _QUOTES_SHEET, row, column, message, markers=markers)


def _rel(
    case_id: str, run: Callable[[Path], object], row: int, column: str | None, message: str
) -> _Case:
    return _Case(case_id, run, _RELEASES_SHEET, row, column, message)


def _cg(
    case_id: str,
    run: Callable[[Path], object],
    row: int,
    column: str | None,
    message: str,
    *,
    crash: type[BaseException] | None = None,
    graph_markers: tuple[str, ...] = (),
) -> _Case:
    return _Case(
        case_id, run, _CG_SHEET, row, column, message, crash=crash, graph_markers=graph_markers
    )


_WORKBOOK_CASES = (
    _div(
        "not-a-workbook",
        _not_a_workbook,
        0,
        None,
        f"could not open {_DIV_FILE} as an .xlsx workbook",
        crash=BadZipFile,
    ),
    _div(
        "openpyxl-quotes-a-cell",
        _numeric_cell_holding_text,
        0,
        None,
        f"could not read the {_DIV_SHEET!r} sheet's rows from {_DIV_FILE}",
        crash=ValueError,
        graph_markers=(_HOLDER,),
    ),
    _div(
        "sheet-renamed-to-a-marker",
        _dividend(_div_row(), sheet=_MARKER_SHEET),
        0,
        None,
        f"{_DIV_FILE} has no {_DIV_SHEET!r} sheet",
    ),
    _div(
        "extra-marker-sheet",
        _dividend(_div_row(), extra_sheet=_MARKER_SHEET),
        0,
        None,
        f"{_DIV_FILE} has 1 unexpected sheet(s); expected only ['IN - Dividend Report', "
        "'Quote History']",
    ),
    _rel(
        "required-sheet-renamed-to-a-marker",
        _releases(_vest_row(), cg_sheet=_MARKER_SHEET),
        0,
        None,
        f"{_CG_FILE} is missing required sheet(s): ['Capital Gains Report']",
    ),
)

_SHARED_HELPER_CASES = (
    _div(
        "title-changed",
        _dividend(_div_row(), title=_MARKER_SHEET),
        0,
        None,
        f"expected StockPlan dividend-report title with reporting period in {_DIV_FILE}",
    ),
    _div(
        "header-changed",
        _dividend(
            _div_row(), header=(*DIVIDEND_REPORT_HEADER[:2], _HOLDER, *DIVIDEND_REPORT_HEADER[3:])
        ),
        1,
        None,
        f"expected exact header row {tuple(DIVIDEND_REPORT_HEADER)!r} in {_DIV_FILE}",
    ),
    _div(
        "trailing-cell",
        _dividend([*_div_row(), _HOLDER]),
        2,
        "#8",
        "unexpected populated column beyond the known 7-column schema",
    ),
    _div(
        "unknown-row",
        _dividend([_HOLDER]),
        2,
        None,
        "unknown non-data row; the StockPlan report schema may have changed",
    ),
    _div("text-in-cash", _dividend(_div_row(cash=_ACCOUNT)), 2, "Cash", "non-numeric Cash"),
    _div("non-finite-cash", _dividend(_div_row(cash=_NAN)), 2, "Cash", "non-finite Cash"),
    _div(
        "text-in-entry-date",
        _dividend(_with(_div_row(), 0, _HOLDER)),
        2,
        "Entry Date",
        "data row has a missing or non-date Entry Date",
    ),
    _div(
        "no-usd-statement",
        _dividend(_div_row(), _wht_row(), footer=False),
        0,
        None,
        f"could not uniquely confirm {_DIV_FILE} is denominated in US Dollars (expected "
        f"exactly one {STOCKPLAN_USD_FOOTER!r} statement, found 0)",
    ),
)

_DIVIDEND_CASES = (
    _div(
        "malformed-period-date",
        _dividend(_div_row(), title=_dividend_title(period="31-Apr-2025 to 31-Mar-2026")),
        0,
        None,
        f"malformed Dividend Report date in {_DIV_FILE}",
        crash=ValueError,
        graph_markers=("2025",),
    ),
    _div(
        "run-on-the-period-end",
        _dividend(_div_row(), title=_dividend_title(run_on="31-Mar-2026")),
        0,
        None,
        _NOT_AFTER.format(file=_DIV_FILE, missed="dividends"),
    ),
    _div(
        "run-before-the-period-end",
        _dividend(_div_row(), title=_dividend_title(run_on="01-Feb-2026")),
        0,
        None,
        _NOT_AFTER.format(file=_DIV_FILE, missed="dividends"),
    ),
    _div(
        "dated-outside-the-period",
        _dividend(_with(_div_row(), 0, _dt.datetime(2026, 4, 15))),
        2,
        "Entry Date",
        "'Dividend Paid' is dated outside the report's stated reporting period",
    ),
    _div(
        "duplicate-dividend-date",
        _dividend(_div_row(), _div_row()),
        3,
        "Entry Date",
        "duplicate 'Dividend Paid' for an Entry Date an earlier row already has",
    ),
    _div(
        "duplicate-withholding-date",
        _dividend(_div_row(), _wht_row(), _wht_row()),
        4,
        "Entry Date",
        "duplicate 'IRS Nonresident Alien Withholding' for an Entry Date an earlier row "
        "already has",
    ),
    _div(
        "no-records-row-with-data",
        _dividend([_NO_RECORDS, _ACCOUNT]),
        2,
        None,
        "'No records found' row also carries data",
    ),
    _div(
        "duplicate-no-records-row",
        _dividend([_NO_RECORDS], [_NO_RECORDS]),
        3,
        None,
        "duplicate 'No records found' row",
    ),
    _div(
        "units-populated",
        _dividend(_with(_div_row(), 4, _HOLDER)),
        2,
        "Number of Units or Shares",
        "Number of Units or Shares is populated but its StockPlan semantics are unsupported",
    ),
    _div(
        "unit-price-populated",
        _dividend(_with(_div_row(), 5, _AMOUNT)),
        2,
        "Unit or Share Price",
        "Unit or Share Price is populated but its StockPlan semantics are unsupported",
    ),
    _div(
        "nonzero-book-value",
        _dividend(_with(_div_row(), 6, _AMOUNT)),
        2,
        "Book Value",
        "Book Value must be zero for the validated StockPlan dividend schema",
    ),
    _div(
        "marker-activity",
        _dividend(_div_row(activity=_ACCOUNT)),
        2,
        "Activity",
        "unrecognised dividend-report Activity; expected one of ['Dividend Paid', "
        "'IRS Nonresident Alien Withholding']",
    ),
    _div(
        "marker-symbol",
        _dividend(_with(_div_row(), 3, "ANN")),
        2,
        "Symbol",
        f"row Symbol {_CONFIGURED}",
    ),
    _div(
        "negative-dividend",
        _dividend(_div_row(cash=-_AMOUNT)),
        2,
        "Cash",
        "'Dividend Paid' must be a positive gross amount",
    ),
    _div(
        "positive-withholding",
        _dividend(_wht_row(cash=_AMOUNT)),
        2,
        "Cash",
        "'IRS Nonresident Alien Withholding' must be a negative (withheld) amount",
    ),
    _div(
        "no-records-and-dividends",
        _dividend([_NO_RECORDS], _div_row()),
        1,
        None,
        "report has both 'No records found' and dividend/withholding rows",
    ),
    _div(
        "no-rows-and-no-sentinel",
        _dividend(),
        1,
        None,
        f"{_DIV_FILE} has no dividend/withholding rows and no 'No records found' sentinel; "
        "cannot prove zero activity",
    ),
)

_HOLDINGS_CASES = (
    _hold(
        "title-changed",
        _holdings(_cash_row(), _stock_row(), title=_MARKER_SHEET),
        0,
        None,
        f"expected StockPlan holdings-report title in {_HOLDINGS_FILE}",
    ),
    _hold(
        "text-in-shares",
        _holdings(_cash_row(), _with(_stock_row(), 3, _HOLDER)),
        3,
        "Number of Shares",
        "non-numeric Number of Shares",
    ),
    _hold(
        "negative-cash",
        _holdings(_with(_cash_row(), 3, -_AMOUNT), _stock_row()),
        2,
        _CASH_COLUMN,
        "Cash - USD values must not be negative",
    ),
    _hold(
        "unequal-cash",
        _holdings(_with(_cash_row(), 4, _AMOUNT), _stock_row()),
        2,
        _CASH_COLUMN,
        "Cash - USD shares, acquisition value, and market value must match",
    ),
    _hold(
        "blank-plan",
        _holdings(_with(_cash_row(), 1, ""), _stock_row()),
        2,
        _IDENTITY_COLUMN,
        "Holdings row must carry a nonblank employee and plan identity",
    ),
    _hold(
        "two-employees",
        _holdings(_cash_row("E1234567"), _stock_row("E7654321")),
        3,
        _IDENTITY_COLUMN,
        "Holdings rows have inconsistent employee/plan identities",
    ),
    _hold(
        "marker-fund",
        _holdings(_cash_row(), _with(_stock_row(), 2, _MARKER_SHEET)),
        3,
        "Fund Name",
        "unexpected holding; expected only 'Cash - USD' and the configured "
        "[stockplan.security] symbol and listing exchange",
    ),
    _hold(
        "negative-market-value",
        _holdings(_cash_row(), _with(_stock_row(), 5, -_AMOUNT)),
        3,
        "Number of Shares",
        "the configured stock's holdings must have positive shares, acquisition value, and "
        "market value",
    ),
    _hold(
        "no-cash-row",
        _holdings(_stock_row()),
        1,
        "Fund Name",
        "expected exactly one 'Cash - USD' row, found 0",
    ),
    _hold(
        "no-stock-row",
        _holdings(_cash_row()),
        1,
        "Fund Name",
        "expected exactly one holdings row for the configured stock, found 0",
    ),
)

# #1290: the Quote History sheet gives the Holdings Report's Selected Date.
_QUOTES_CASES = (
    _quote(
        "missing-sheet",
        _holdings(_cash_row(), _stock_row(), quotes=None),
        0,
        None,
        f"{_HOLDINGS_FILE} has no 'Quote History' sheet",
        markers=("E1234567", str(market_value(26.374))),
    ),
    _quote(
        "title-changed",
        _holdings(
            _cash_row(),
            _stock_row(),
            quotes=(
                _with(_QUOTE_TITLE, 0, _MARKER_SHEET),
                _QUOTE_HEADER,
                _CASH_QUOTE,
                _STOCK_QUOTE,
            ),
        ),
        0,
        None,
        f"expected title row in {_HOLDINGS_FILE}",
        markers=(_HOLDER, _ACCOUNT),
    ),
    _quote(
        "header-changed",
        _holdings(
            _cash_row(),
            _stock_row(),
            quotes=(_QUOTE_TITLE, _with(_QUOTE_HEADER, 1, _HOLDER), _CASH_QUOTE, _STOCK_QUOTE),
        ),
        1,
        None,
        f"expected exact header row ('Fund', 'Quote Date', 'Price') in {_HOLDINGS_FILE}",
        markers=(_HOLDER,),
    ),
    _quote(
        "marker-past-three-columns",
        _quotes(_CASH_QUOTE, [*_STOCK_QUOTE, _HOLDER]),
        3,
        "#4",
        "unexpected populated column beyond the known 3-column schema",
        markers=(_HOLDER,),
    ),
    _quote(
        "marker-text-row",
        _quotes(_CASH_QUOTE, _STOCK_QUOTE, [_MARKER_SHEET]),
        4,
        None,
        "unknown non-data row; the StockPlan report schema may have changed",
        markers=(_HOLDER, _ACCOUNT),
    ),
    _quote(
        "marker-fund",
        _quotes(_CASH_QUOTE, _with(_STOCK_QUOTE, 0, _MARKER_SHEET)),
        3,
        "Fund",
        "unexpected quote; expected only 'Cash - USD' and the configured "
        "[stockplan.security] symbol and listing exchange",
        markers=(_HOLDER, _ACCOUNT),
    ),
    _quote(
        "no-stock-quote",
        _quotes(_CASH_QUOTE),
        1,
        "Fund",
        "expected exactly one 'Cash - USD' quote and one quote for the configured stock",
        markers=("E1234567", str(market_value(26.374))),
    ),
    _quote(
        "marker-quote-date",
        _quotes(_CASH_QUOTE, _with(_STOCK_QUOTE, 1, _HOLDER)),
        3,
        "Quote Date",
        "data row has a missing or non-date Quote Date",
        markers=(_HOLDER,),
    ),
    _quote(
        "marker-price",
        _quotes(_CASH_QUOTE, _with(_STOCK_QUOTE, 2, _HOLDER)),
        3,
        "Price",
        "non-numeric Price",
        markers=(_HOLDER,),
    ),
    _quote(
        "cash-price",
        _quotes(_with(_CASH_QUOTE, 2, _AMOUNT), _STOCK_QUOTE),
        2,
        "Price",
        "the 'Cash - USD' quote must be 1",
        markers=(str(_AMOUNT),),
    ),
    _quote(
        "two-quote-dates",
        _quotes(_with(_CASH_QUOTE, 1, _dt.datetime(2026, 7, 9)), _STOCK_QUOTE),
        3,
        "Quote Date",
        "the 'Cash - USD' and stock quotes must share one Quote Date",
        markers=("2026-07-09", "09-Jul-2026", "2026-07-10", "10-Jul-2026"),
    ),
    _quote(
        "after-the-run-date",
        _quotes(*(_with(quote, 1, _dt.datetime(2026, 7, 15)) for quote in _QUOTES[2:])),
        3,
        "Quote Date",
        "the Quote Date must not be after the Holdings Report's run date",
        markers=("2026-07-15", "15-Jul-2026"),
    ),
    _hold(
        "market-value-off-the-quote",
        _holdings(_cash_row(), _with(_stock_row(), 5, _AMOUNT)),
        3,
        "Market Value",
        "Market Value must equal the 'Quote History' sheet's Price x Number of Shares exactly",
        markers=(str(_AMOUNT),),
    ),
)

# Values present in all three future-date workbooks, including the Releases row.
_AFTER_RUN_MARKERS = (
    "Synthetic Issuer",
    "XQCB",
    "196.42",
    "9.012",
    "6.214",
    _RUN_ON,
    "2026-04-02",
    "01-Apr-2025",
    "2025-04-01",
    "31-Mar-2026",
    "2026-03-31",
    "03-Apr-2026",
    "2026-04-03",
    _CG_FILE,
)

_RELEASES_CASES = (
    _rel(
        "title-changed",
        _releases(_vest_row(), title=_MARKER_SHEET),
        0,
        None,
        f"expected StockPlan releases-report title in {_CG_FILE}",
    ),
    _rel(
        "blank-order-number",
        _releases(_vest_row(order="")),
        2,
        "Order Number",
        "Release row has a blank Order Number",
    ),
    _rel(
        "marker-type",
        _releases(_with(_vest_row(), 3, _HOLDER)),
        2,
        "Type",
        "unexpected row Type; only 'Release' rows are supported",
    ),
    _rel(
        "marker-status",
        _releases(_with(_vest_row(), 4, _ACCOUNT)),
        2,
        "Status",
        "vest Status is not 'Complete'",
    ),
    _rel(
        "marker-plan", _releases(_with(_vest_row(), 2, "ANN")), 2, "Plan", f"row Plan {_CONFIGURED}"
    ),
    _rel(
        "duplicate-order-number",
        _releases(_vest_row(order=_ACCOUNT), _vest_row(order=_ACCOUNT)),
        3,
        "Order Number",
        "duplicate vest Order Number",
    ),
    _rel(
        "release-cash",
        _releases(_with(_vest_row(), 7, _AMOUNT)),
        2,
        "Net Cash Proceeds",
        "non-zero release cash is unsupported",
    ),
    _rel(
        "marker-tax-payment-method",
        _releases(_with(_vest_row(), 9, _HOLDER)),
        2,
        "Tax Payment Method",
        "unexpected Tax Payment Method; only 'Fractional Shares' is validated",
    ),
    _rel(
        "negative-price",
        _releases(_with(_vest_row(), 5, -_AMOUNT)),
        2,
        "Price",
        "Fair Market Value (Price) must be positive",
    ),
    _rel(
        "negative-shares-deposited",
        _releases(_with(_vest_row(), 8, -_AMOUNT)),
        2,
        "Net Share Proceeds",
        "Net Share Proceeds (shares deposited) must be positive",
    ),
    _rel(
        "deposited-above-vested",
        _releases(_with(_vest_row(), 8, _AMOUNT)),
        2,
        "Net Share Proceeds",
        "shares deposited must be > 0 and <= GSUs vested",
    ),
    _Case(
        "vest-after-the-run-date",
        _releases(_with(_vest_row(order=_ACCOUNT), 0, _AFTER_THE_RUN)),
        _RELEASES_SHEET,
        2,
        "Vest Date",
        "completed vest date must not be after this sheet's report run date",
        markers=(*_AFTER_RUN_MARKERS, _ACCOUNT),
    ),
)

_CAPITAL_GAINS_CASES = (
    _cg(
        "title-changed",
        _cg_title(_MARKER_SHEET),
        0,
        None,
        f"expected StockPlan capital-gains-report title in {_CG_FILE}",
    ),
    _cg(
        "section-row-changed",
        _sales([_NO_RECORDS], section=(_HOLDER, *CAPITAL_GAINS_SECTION_ROW[1:])),
        1,
        None,
        "expected exact Sale Details / Acquisition Details section row "
        f"{tuple(CAPITAL_GAINS_SECTION_ROW)!r} in {_CG_FILE}",
    ),
    _cg(
        "malformed-period-date",
        _sales([_NO_RECORDS], period="31-Apr-2025 to 31-Mar-2026"),
        0,
        None,
        f"malformed Capital Gains report date in {_CG_FILE}",
        crash=ValueError,
        graph_markers=("2025",),
    ),
    _cg(
        "inverted-period",
        _sales([_NO_RECORDS], period="31-Mar-2026 to 01-Apr-2025"),
        0,
        None,
        "Capital Gains reporting period ends before it starts",
    ),
    _cg(
        "run-on-the-period-end",
        _sales([_NO_RECORDS], run_on="31-Mar-2026"),
        0,
        None,
        _NOT_AFTER.format(file=_CG_FILE, missed="sales"),
    ),
    _cg(
        "run-before-the-period-end",
        _sales([_NO_RECORDS], run_on="01-Feb-2026"),
        0,
        None,
        _NOT_AFTER.format(file=_CG_FILE, missed="sales"),
    ),
    _cg(
        "no-records-row-with-data",
        _sales([_NO_RECORDS, _HOLDER]),
        3,
        None,
        "'No records found' row also carries sale data — ambiguous/corrupt",
    ),
    _cg(
        "duplicate-no-records-row",
        _sales([_NO_RECORDS], [_NO_RECORDS]),
        4,
        None,
        "duplicate 'No records found' row",
    ),
    _cg(
        "text-in-sale-date",
        _sales(_with(_sale_row(), 0, _ACCOUNT)),
        3,
        "Sale Date",
        "data row has a missing or non-date Sale Date",
    ),
    _cg(
        "marker-share-class",
        _sales(_with(_sale_row(), 3, _ACCOUNT)),
        3,
        "Share Class",
        f"row Share Class {_CONFIGURED}",
    ),
    _cg(
        "negative-shares-sold",
        _sales(_with(_sale_row(), 2, -_AMOUNT)),
        3,
        "Number of Shares Sold",
        "Number of Shares Sold must be positive",
    ),
    _cg(
        "negative-sale-price",
        _sales(_with(_sale_row(), 1, -_AMOUNT)),
        3,
        "Sale Price Per Share",
        "Sale Price Per Share must not be negative",
    ),
    _cg(
        "negative-gross-proceeds",
        _sales(_with(_sale_row(), 4, -_AMOUNT)),
        3,
        "Total Gross Proceeds",
        "Total Gross Proceeds must not be negative",
    ),
    _cg(
        "no-records-and-sales",
        _sales([_NO_RECORDS], _sale_row()),
        2,
        None,
        f"{_CG_FILE} has BOTH a 'No records found' sentinel and sale row(s) — "
        "ambiguous/corrupt; refusing rather than risk under-reporting sales",
    ),
    _cg(
        "no-sales-and-no-sentinel",
        _sales(),
        2,
        None,
        f"{_CG_FILE} has no sale rows AND no 'No records found' sentinel — cannot confirm "
        "zero sales (possibly truncated); refusing rather than assume none",
    ),
    _Case(
        "sale-after-the-run-date",
        _sales(_with(_sale_row(), 0, _AFTER_THE_RUN)),
        _CG_SHEET,
        3,
        "Sale Date",
        "sale date must not be after this sheet's report run date",
        markers=(*_AFTER_RUN_MARKERS, "RB0000A1", "190.25", "2.375", "475.63", "2025-08-14"),
    ),
    _Case(
        "acquisition-after-the-run-date",
        _sales(_with(_sale_row(), 5, _AFTER_THE_RUN)),
        _CG_SHEET,
        3,
        "Acquisition Date",
        "acquisition date must not be after this sheet's report run date",
        markers=(
            *_AFTER_RUN_MARKERS,
            "RB0000A1",
            "190.25",
            "2.375",
            "475.63",
            "2025-08-14",
            "2026-02-10",
        ),
    ),
)

_CASES = (
    *_WORKBOOK_CASES,
    *_SHARED_HELPER_CASES,
    *_DIVIDEND_CASES,
    *_HOLDINGS_CASES,
    *_QUOTES_CASES,
    *_RELEASES_CASES,
    *_CAPITAL_GAINS_CASES,
)


def _assert_fixed(
    error: ParseError,
    tmp_path: Path,
    *,
    section: str,
    row: int,
    column: str | None,
    message: str,
    markers: tuple[str, ...] = (*_MARKERS, *_VALUES),
    crash: type[BaseException] | None = None,
    graph_markers: tuple[str, ...] = (),
) -> None:
    """``error`` is the fixed refusal at the location given, with no value and no chain.

    With ``crash``, the refusal replaced a crash of that class: none of
    ``graph_markers`` is anywhere in its exception graph, it has no context,
    and its only cause is the content-free stand-in (#1226).
    """
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        section,
        row,
        column,
        message,
        "",
    )
    if crash is None:
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__
    else:
        assert_replaced_unchained(error, crash, markers=graph_markers)
    text = str(error)
    assert_no_markers(text, *markers)
    assert str(tmp_path) not in text


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.section}:{case.id}")
def test_refusal_is_fixed_text_at_its_location(tmp_path: Path, case: _Case) -> None:
    with pytest.raises(ParseError) as excinfo:
        case.run(tmp_path)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=case.section,
        row=case.row,
        column=case.column,
        message=case.message,
        markers=case.markers,
        crash=case.crash,
        graph_markers=case.graph_markers,
    )


def test_cases_cover_distinct_sites() -> None:
    assert len({(case.section, case.id) for case in _CASES}) == len(_CASES)


def test_valid_synthetic_workbooks_still_parse(tmp_path: Path) -> None:
    dividends, withholdings, coverage = parse_stockplan_dividend_report(
        _dividend_book(tmp_path, _div_row(), _wht_row()), symbol=_SYMBOL, isin=_ISIN
    )
    assert (len(dividends), len(withholdings)) == (1, 1)
    assert coverage == (_dt.date(2025, 4, 1), _dt.date(2026, 3, 31))
    holdings = parse_stockplan_holdings(
        _holdings_book(tmp_path, _cash_row(), _stock_row()),
        symbol=_SYMBOL,
        listing_exchange="NASDAQ",
    )
    assert (holdings.shares, holdings.selected_date) == (Decimal("26.374"), _dt.date(2026, 7, 10))
    workbook = _cg_book(tmp_path, vests=(_vest_row(),), sales=(_sale_row(),))
    vests, run_on = parse_stockplan_releases(workbook, symbol=_SYMBOL)
    assert (len(vests), run_on) == (1, _dt.date(2026, 4, 2))
    sales, cg_coverage = parse_stockplan_capital_gains_report(workbook, symbol=_SYMBOL)
    assert len(sales) == 1
    assert cg_coverage == coverage


def test_openpyxl_open_failure_text_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dividend_book(tmp_path, _div_row())

    def fail(*_args: object, **_kwargs: object) -> object:
        raise XMLParseError(f"mismatched tag near {_HOLDER} {_ACCOUNT}")

    monkeypatch.setattr(xlsx_mod.openpyxl, "load_workbook", fail)
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"could not open {_DIV_FILE} as an .xlsx workbook",
        crash=XMLParseError,
        graph_markers=(_HOLDER, _ACCOUNT),
    )


@pytest.mark.parametrize(
    ("error", "name"),
    [
        pytest.param(OSError(errno.EACCES, _STRERROR, f"{_HOLDER}/x"), "EACCES", id="mapped-errno"),
        pytest.param(OSError(_STRERROR), "unknown error", id="no-errno"),
        pytest.param(OSError(99999, _STRERROR, f"{_HOLDER}/x"), "unknown error", id="unmapped"),
    ],
)
def test_os_error_opening_the_workbook_gives_its_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, name: str
) -> None:
    path = _dividend_book(tmp_path, _div_row())

    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(xlsx_mod.openpyxl, "load_workbook", fail)
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"could not open {_DIV_FILE} ({name})",
    )


def _fail_sheet_scan(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    """Make the sheet scan's parser raise ``failure``; the real
    ``require_cells_in_order`` still runs (#1071)."""

    def parse(_parser: WorkSheetParser) -> NoReturn:
        raise failure

    monkeypatch.setattr(WorkSheetParser, "parse", parse)


@pytest.mark.parametrize(
    ("error", "name"),
    [
        pytest.param(OSError(errno.EIO, _STRERROR, f"{_HOLDER}/x"), "EIO", id="mapped-errno"),
        pytest.param(OSError(None, _STRERROR, f"{_HOLDER}/x"), "unknown error", id="no-errno"),
        pytest.param(OSError(99999, _STRERROR, f"{_HOLDER}/x"), "unknown error", id="unmapped"),
    ],
)
def test_os_error_reading_the_sheet_gives_its_errno_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, name: str
) -> None:
    path = _dividend_book(tmp_path, _div_row())
    _fail_sheet_scan(monkeypatch, error)
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"could not read the {_DIV_SHEET!r} sheet's rows from {_DIV_FILE} ({name})",
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError(_STRERROR), id="value-error"),
        pytest.param(IndexError(_STRERROR), id="index-error"),
        pytest.param(TypeError(_STRERROR), id="type-error"),
        pytest.param(XMLParseError(_STRERROR), id="xml-parse-error"),
        pytest.param(BadZipFile(_STRERROR), id="bad-zip"),
        pytest.param(zlib.error(_STRERROR), id="deflate-error"),
    ],
)
def test_format_failure_reading_the_sheet_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    path = _dividend_book(tmp_path, _div_row())
    _fail_sheet_scan(monkeypatch, error)
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    # The scan raises every format failure as a ValueError.
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"could not read the {_DIV_SHEET!r} sheet's rows from {_DIV_FILE}",
        crash=ValueError,
        graph_markers=(_STRERROR,),
    )


@pytest.mark.parametrize("kind", [RuntimeError, AssertionError])
def test_crash_reading_the_sheet_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: type[Exception]
) -> None:
    # A crash in the scan is not a malformed sheet: it reaches the CLI's
    # internal-error line (see test_pipeline_stockplan_fa.py) (#1071).
    path = _dividend_book(tmp_path, _div_row())
    failure = kind(_STRERROR)
    _fail_sheet_scan(monkeypatch, failure)
    with pytest.raises(kind) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert excinfo.value is failure


# openpyxl's warnings can quote workbook text, and its out-of-range date cell reads
# as the text "#VALUE!", so any warning but the allowed one refuses (#1103).
_WARNING_MARKER = f"{_HOLDER} {_ACCOUNT}"
_OVERFLOW_TEXTS = ("1e+100", "1e100")


def _unreadable(sheet: str, file: str) -> str:
    return f"could not read the {sheet!r} sheet's rows from {file}"


def _parse_dividend(path: Path) -> object:
    return parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def _parse_holdings(path: Path) -> object:
    return parse_stockplan_holdings(path, symbol=_SYMBOL, listing_exchange="NASDAQ")


def _parse_releases(path: Path) -> object:
    return parse_stockplan_releases(path, symbol=_SYMBOL)


@pytest.mark.parametrize(
    ("build", "parse", "sheet", "coordinates", "file"),
    [
        # Both rows, so the two rows' identities still agree.
        pytest.param(
            lambda tmp: _holdings_book(tmp, _cash_row(), _stock_row()),
            _parse_holdings,
            _HOLDINGS_SHEET,
            ("A3", "A4"),
            _HOLDINGS_FILE,
            id="holdings-employee-number",
        ),
        pytest.param(
            lambda tmp: _cg_book(tmp, vests=(_vest_row(),)),
            _parse_releases,
            _RELEASES_SHEET,
            ("B3",),
            _CG_FILE,
            id="releases-order-number",
        ),
        pytest.param(
            lambda tmp: _dividend_book(tmp, _div_row()),
            _parse_dividend,
            _DIV_SHEET,
            ("A3",),
            _DIV_FILE,
            id="dividend-entry-date",
        ),
    ],
)
def test_date_styled_number_out_of_range_refuses_in_fixed_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build: Callable[[Path], Path],
    parse: Callable[[Path], object],
    sheet: str,
    coordinates: tuple[str, ...],
    file: str,
) -> None:
    # openpyxl warns with the cell's number, then reads it as "#VALUE!", which a
    # free-text column would accept.
    path = _date_styled_overflow(build(tmp_path), sheet, *coordinates)

    error = _refusal_without_escaped_warnings(capsys, lambda: parse(path))

    _assert_fixed(
        error, tmp_path, section=sheet, row=0, column=None, message=_unreadable(sheet, file)
    )
    assert_no_markers(str(error), *_OVERFLOW_TEXTS)


def test_unusable_print_area_refuses_in_fixed_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # openpyxl warns with the defined name's text while it loads the workbook.
    path = _dividend_book(tmp_path, _div_row())
    workbook = openpyxl.load_workbook(path)
    workbook[_DIV_SHEET].defined_names["_xlnm.Print_Area"] = DefinedName(
        "_xlnm.Print_Area", attr_text=_WARNING_MARKER
    )
    workbook.save(path)

    error = _refusal_without_escaped_warnings(capsys, lambda: _parse_dividend(path))

    _assert_fixed(
        error,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=_unreadable(_DIV_SHEET, _DIV_FILE),
    )


def test_any_warning_while_reading_the_sheet_refuses_in_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _dividend_book(tmp_path, _div_row())

    def warn_then_nothing(
        _parser: WorkSheetParser,
    ) -> Iterator[tuple[int, list[dict[str, object]]]]:
        warnings.warn(_WARNING_MARKER, stacklevel=1)
        yield from ()

    monkeypatch.setattr(WorkSheetParser, "parse", warn_then_nothing)

    error = _refusal_without_escaped_warnings(capsys, lambda: _parse_dividend(path))

    _assert_fixed(
        error,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=_unreadable(_DIV_SHEET, _DIV_FILE),
    )


def _without_named_styles(path: Path) -> Path:
    """Drop the workbook's named cell styles, so openpyxl raises the allowed warning."""
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    styles = payloads["xl/styles.xml"].decode("utf-8")
    edited = re.sub(r"<cellStyles\b.*?</cellStyles>", "", styles, flags=re.DOTALL)
    assert edited != styles
    payloads["xl/styles.xml"] = edited.encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        openpyxl.load_workbook(path, read_only=True).close()
    assert [(w.category, str(w.message)) for w in raised] == [(UserWarning, ALLOWED_WARNING)]
    return path


def test_workbook_without_named_styles_still_parses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Owner decision (#1103): openpyxl's fixed "no default style" warning is allowed.
    dividends = _without_named_styles(_dividend_book(tmp_path, _div_row(), _wht_row()))
    holdings = _without_named_styles(_holdings_book(tmp_path, _cash_row(), _stock_row()))
    capital_gains = _without_named_styles(
        _cg_book(tmp_path, vests=(_vest_row(),), sales=(_sale_row(),))
    )

    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        dividend_rows, withholding_rows, _ = parse_stockplan_dividend_report(
            dividends, symbol=_SYMBOL, isin=_ISIN
        )
        holding = parse_stockplan_holdings(holdings, symbol=_SYMBOL, listing_exchange="NASDAQ")
        vests, _ = parse_stockplan_releases(capital_gains, symbol=_SYMBOL)
        sales, _ = parse_stockplan_capital_gains_report(capital_gains, symbol=_SYMBOL)

    assert escaped == []
    assert capsys.readouterr().err == ""
    assert (len(dividend_rows), len(withholding_rows), len(vests), len(sales)) == (1, 1, 1, 1)
    assert holding.shares == Decimal("26.374")


_BAD_RUN_DATES = ("31-Feb-2026", "01-Xyz-2026")
# The crash each bad run date's refusal replaces: a calendar or a month-key error.
_BAD_RUN_DATE_CRASH = {"31-Feb-2026": ValueError, "01-Xyz-2026": KeyError}
_BAD_RUN_DATE_MARKERS = {"31-Feb-2026": ("2026",), "01-Xyz-2026": ("Xyz",)}
# A valid run date in the title's prefix never stands in for the closing one.
_RUN_DATE_PREFIXES = ("Synthetic Issuer", "Synthetic Issuer (report run on 01-Jan-2099)")


def _assert_malformed_run_date(
    error: ParseError, tmp_path: Path, *, sheet: str, file: str, run_on: str
) -> None:
    _assert_fixed(
        error,
        tmp_path,
        section=sheet,
        row=0,
        column=None,
        message=f"malformed {sheet} date in {file}",
        crash=_BAD_RUN_DATE_CRASH[run_on],
        graph_markers=_BAD_RUN_DATE_MARKERS[run_on],
    )
    assert_no_markers(str(error), "31-Feb-2026", "Xyz", "2026", "01-Jan-2099", "2099")


@pytest.mark.parametrize("run_on", _BAD_RUN_DATES)
@pytest.mark.parametrize("prefix", _RUN_DATE_PREFIXES)
def test_malformed_holdings_run_date_refuses(tmp_path: Path, prefix: str, run_on: str) -> None:
    title = f"{prefix} - Holdings Report (report run on {run_on})"
    with pytest.raises(ParseError) as excinfo:
        _holdings(_cash_row(), _stock_row(), title=title)(tmp_path)
    _assert_malformed_run_date(
        excinfo.value, tmp_path, sheet=_HOLDINGS_SHEET, file=_HOLDINGS_FILE, run_on=run_on
    )


@pytest.mark.parametrize("run_on", _BAD_RUN_DATES)
def test_malformed_holdings_run_date_ignores_a_later_run_date(tmp_path: Path, run_on: str) -> None:
    # Both rows' plan names hold a valid run date phrase, which the parser once
    # took as the run date when the title's did not parse.
    plan = "Synthetic Plan report run on 01-Jan-2099"
    title = f"Synthetic Issuer - Holdings Report (report run on {run_on})"
    run = _holdings(_with(_cash_row(), 1, plan), _with(_stock_row(), 1, plan), title=title)
    with pytest.raises(ParseError) as excinfo:
        run(tmp_path)
    _assert_malformed_run_date(
        excinfo.value, tmp_path, sheet=_HOLDINGS_SHEET, file=_HOLDINGS_FILE, run_on=run_on
    )


@pytest.mark.parametrize("run_on", _BAD_RUN_DATES)
def test_malformed_dividend_run_date_ignores_a_valid_clause_in_the_prefix(
    tmp_path: Path, run_on: str
) -> None:
    # The prefix holds a whole valid clause, which the parser once read instead.
    title = (
        f"Synthetic Issuer (report run on 01-Jan-2099 for reporting period {_PERIOD})"
        f" - IN - Dividend Report (report run on {run_on} for reporting period {_PERIOD})"
    )
    with pytest.raises(ParseError) as excinfo:
        _dividend(_div_row(), _wht_row(), title=title)(tmp_path)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"malformed Dividend Report date in {_DIV_FILE}",
        crash=_BAD_RUN_DATE_CRASH[run_on],
        graph_markers=_BAD_RUN_DATE_MARKERS[run_on],
    )
    assert_no_markers(str(excinfo.value), "31-Feb-2026", "Xyz", "2026", "01-Jan-2099", "2099")


@pytest.mark.parametrize("run_on", _BAD_RUN_DATES)
@pytest.mark.parametrize("prefix", _RUN_DATE_PREFIXES)
def test_malformed_releases_run_date_refuses(tmp_path: Path, prefix: str, run_on: str) -> None:
    title = f"{prefix} - Releases Report (report run on {run_on})"
    with pytest.raises(ParseError) as excinfo:
        _releases(_vest_row(), title=title)(tmp_path)
    _assert_malformed_run_date(
        excinfo.value, tmp_path, sheet=_RELEASES_SHEET, file=_CG_FILE, run_on=run_on
    )


# A formula cell refuses wherever the sheet stores it, with or without a stored
# value; the refusal names the cell, never the formula or its value (#1089).
_MARKER_Q = "Synthetic Marker Q"
_STORED = "999.99"
_FORMULA_MARKERS = (_HOLDER, _MARKER_Q, _STORED)
_FORMULA_FILES = {
    _DIV_SHEET: _DIV_FILE,
    _HOLDINGS_SHEET: _HOLDINGS_FILE,
    _QUOTES_SHEET: _HOLDINGS_FILE,
    _RELEASES_SHEET: _CG_FILE,
    _CG_SHEET: _CG_FILE,
}


def _formula(value: object) -> str:
    """A formula that evaluates to ``value``; its text holds two markers."""
    if isinstance(value, _dt.datetime):
        literal = f"DATE({value.year},{value.month},{value.day})"
    elif isinstance(value, str):
        literal = f'"{value}"'
    else:
        literal = str(value)
    return f'=IF(FALSE,"{_HOLDER} {_MARKER_Q}",{literal})'


def _formula_row(row: list[object]) -> list[object]:
    """``row`` with every populated cell replaced by a formula giving its value."""
    return [None if value is None else _formula(value) for value in row]


def _store_values(path: Path) -> Path:
    """Store ``999.99`` as every formula cell's value (openpyxl stores none)."""
    found: list[int] = []

    def store(_name: str, sheet: str) -> str:
        found.append(sheet.count("</f><v />"))
        return sheet.replace("</f><v />", f"</f><v>{_STORED}</v>")

    _edit_sheets(path, store)
    assert sum(found) > 0
    return path


def _past_dimension(**cells: object) -> Callable[[Path], Path]:
    """A Capital Gains Report with ``cells`` set in row 7, past its declared dimension."""

    def build(tmp_path: Path) -> Path:
        # Row 7 is past the sheet's footer row 5; the <dimension> tag is cut to end there.
        path = _cg_book(tmp_path, vests=(_vest_row(),), sales=(_sale_row(),))
        _set_cells(path, _CG_SHEET, **cells)
        with ZipFile(path) as package:
            assert '<dimension ref="A1:F7" />' in package.read("xl/worksheets/sheet2.xml").decode()
        return _edit_sheets(
            path, _replace_once('<dimension ref="A1:F7" />', '<dimension ref="A1:F5" />')
        )

    return build


def _gross_proceeds_formula(tmp_path: Path) -> Path:
    # Total Gross Proceeds (E4) is a formula of B4 and C4, which give 50.
    proceeds = f'=IF(FALSE,"{_HOLDER} {_MARKER_Q}",B4*C4)'
    sale: list[object] = [_dt.datetime(2026, 2, 10), 20.0, 2.5, "XQCB", proceeds, _sale_row()[5]]
    return _cg_book(tmp_path, vests=(_vest_row(),), sales=(sale,))


@dataclass(frozen=True, slots=True)
class _FormulaCase:
    id: str
    book: Callable[[Path], Path]
    section: str
    row: int
    column: str


_FORMULA_CASES = (
    _FormulaCase(
        "no-records-plus-formula-sale",
        lambda tmp_path: _cg_book(
            tmp_path, vests=(_vest_row(),), sales=([_NO_RECORDS], _formula_row(_sale_row()))
        ),
        _CG_SHEET,
        4,
        "#1",
    ),
    _FormulaCase(
        "sale-plus-formula-sale",
        lambda tmp_path: _cg_book(
            tmp_path, vests=(_vest_row(),), sales=(_sale_row(), _formula_row(_sale_row()))
        ),
        _CG_SHEET,
        4,
        "#1",
    ),
    _FormulaCase(
        "vest-plus-formula-vest",
        lambda tmp_path: _cg_book(
            tmp_path, vests=(_vest_row(), _formula_row(_vest_row("RB0000A2")))
        ),
        _RELEASES_SHEET,
        3,
        "#1",
    ),
    _FormulaCase(
        "dividend-plus-formula-dividend",
        lambda tmp_path: _dividend_book(tmp_path, _div_row(), _formula_row(_div_row())),
        _DIV_SHEET,
        3,
        "#1",
    ),
    _FormulaCase(
        "formula-in-must-be-blank-units",
        lambda tmp_path: _dividend_book(tmp_path, _with(_div_row(), 4, _formula(3))),
        _DIV_SHEET,
        2,
        "#5",
    ),
    _FormulaCase(
        "formula-past-six-column-schema",
        lambda tmp_path: _cg_book(
            tmp_path, vests=(_vest_row(),), sales=([*_sale_row(), _formula(_AMOUNT)],)
        ),
        _CG_SHEET,
        3,
        "#7",
    ),
    _FormulaCase(
        "duplicate-formula-stock-row",
        lambda tmp_path: _holdings_book(
            tmp_path, _cash_row(), _stock_row(), _formula_row(_stock_row())
        ),
        _HOLDINGS_SHEET,
        4,
        "#1",
    ),
    _FormulaCase("gross-proceeds-formula", _gross_proceeds_formula, _CG_SHEET, 3, "#5"),
    _FormulaCase(
        "quote-price-formula",
        lambda tmp_path: _holdings_book(
            tmp_path,
            _cash_row(),
            _stock_row(),
            quotes=(*_QUOTES[:3], _with(_STOCK_QUOTE, 2, _formula(_STOCK_QUOTE[2]))),
        ),
        _QUOTES_SHEET,
        3,
        "#3",
    ),
    _FormulaCase(
        "title-row",
        lambda tmp_path: _set_cells(
            _dividend_book(tmp_path, _div_row()), _DIV_SHEET, B1=_formula("Synthetic Issuer")
        ),
        _DIV_SHEET,
        0,
        "#2",
    ),
    _FormulaCase(
        "header-row",
        lambda tmp_path: _set_cells(
            _holdings_book(tmp_path, _cash_row(), _stock_row()),
            _HOLDINGS_SHEET,
            G2=_formula("Synthetic Plan"),
        ),
        _HOLDINGS_SHEET,
        1,
        "#7",
    ),
    _FormulaCase(
        "footer-row",
        lambda tmp_path: _set_cells(
            _cg_book(tmp_path, vests=(_vest_row(),)),
            _RELEASES_SHEET,
            B4=_formula(STOCKPLAN_USD_FOOTER),
        ),
        _RELEASES_SHEET,
        3,
        "#2",
    ),
    _FormulaCase(
        "past-declared-dimension", _past_dimension(A7=_formula(_AMOUNT)), _CG_SHEET, 6, "#1"
    ),
)


@pytest.mark.parametrize("stored", [False, True], ids=["no-stored-value", "stored-value"])
@pytest.mark.parametrize("case", _FORMULA_CASES, ids=lambda case: f"{case.section}:{case.id}")
def test_formula_cell_refuses_at_its_location(
    tmp_path: Path, case: _FormulaCase, stored: bool
) -> None:
    path = case.book(tmp_path)
    if stored:
        _store_values(path)
    with pytest.raises(ParseError) as excinfo:
        _PARSERS[case.section](path)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=case.section,
        row=case.row,
        column=case.column,
        message=(
            f"{_FORMULA_FILES[case.section]} stores a formula in this cell; only stored "
            "values are supported. Re-export the report."
        ),
        markers=(_HOLDER, _MARKER_Q, str(market_value(26.374)))
        if case.section == _QUOTES_SHEET
        else (*_MARKERS, *_VALUES),
    )
    formula_markers = (
        _FORMULA_MARKERS[:2] if case.section == _QUOTES_SHEET and not stored else _FORMULA_MARKERS
    )
    assert_no_markers(str(excinfo.value), *formula_markers)


def test_formula_cases_cover_every_sheet() -> None:
    assert {case.section for case in _FORMULA_CASES} == set(_PARSERS)


def test_text_starting_with_equals_is_not_a_formula(tmp_path: Path) -> None:
    # "=A1" stored as text reads as that text, as before: both rows' plan names
    # are nonblank and agree, so the report parses.
    path = _holdings_book(tmp_path, _cash_row(), _stock_row())
    workbook = openpyxl.load_workbook(path)
    for coordinate in ("B3", "B4"):
        workbook[_HOLDINGS_SHEET][coordinate].value = "=A1"
        workbook[_HOLDINGS_SHEET][coordinate].data_type = "s"
    workbook.save(path)
    with ZipFile(path) as package:
        assert "<f>" not in package.read("xl/worksheets/sheet1.xml").decode()

    rows = xlsx_mod.load_sheet_rows(
        path, _HOLDINGS_SHEET, required_sheets={_HOLDINGS_SHEET}, optional_sheets={_QUOTES_SHEET}
    )
    assert (rows[2][1], rows[3][1]) == ("=A1", "=A1")
    holdings = parse_stockplan_holdings(path, symbol=_SYMBOL, listing_exchange="NASDAQ")
    assert holdings.shares == Decimal("26.374")


@pytest.mark.parametrize(
    ("master", "dependent", "graph_markers"),
    [
        pytest.param(
            f'"{_HOLDER} {_MARKER_Q}', "<f>2</f>", (_HOLDER, _MARKER_Q), id="tokenizer-error"
        ),
        pytest.param(
            f'1)+LEN("{_HOLDER} {_MARKER_Q}")', "<f>2</f>", (), id="tokenizer-index-error"
        ),
        pytest.param(
            f'A1+LEN("{_HOLDER} {_MARKER_Q}")', '<f t="shared" si="0" />', (), id="translator-error"
        ),
    ],
)
def test_malformed_shared_formula_refuses_as_a_format_failure(
    tmp_path: Path, master: str, dependent: str, graph_markers: tuple[str, ...]
) -> None:
    # H3 holds a shared formula's master and B4 another formula. openpyxl raises
    # decoding the master, or translating it to B4 (six columns left of A1),
    # while the sheet scan reads it.
    path = _set_cells(_dividend_book(tmp_path, _div_row()), _DIV_SHEET, H3="=1", B4="=2")
    before = _worksheets(path)
    _edit_sheets(path, _replace_once("<f>1</f>", f'<f t="shared" ref="H3:H4" si="0">{master}</f>'))
    assert _worksheets(path) != before
    # The tokenizer cases keep B4's "<f>2</f>", so this edit may change nothing.
    _edit_sheets(path, _replace_once("<f>2</f>", dependent))
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=_DIV_SHEET,
        row=0,
        column=None,
        message=f"could not read the {_DIV_SHEET!r} sheet's rows from {_DIV_FILE}",
        crash=ValueError,
        graph_markers=graph_markers,
    )
    assert_no_markers(str(excinfo.value), _HOLDER, _MARKER_Q)


# A sheet with no formula refuses at the first cell, in stored order, that stores a
# spreadsheet error value. The refusal names the cell, never the file name, the error
# code or a value (#1133). openpyxl stores an error code's text as an error value.
_STORED_ERROR = "This cell stores a spreadsheet error value. Re-export the report."
_ERROR_CODES = ("#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NUM!")
_FILE_MARKER = "SyntheticAccount123"


def _with_cells(
    book: Callable[[Path], Path], sheet: str, **cells: object
) -> Callable[[Path], Path]:
    return lambda tmp_path: _set_cells(book(tmp_path), sheet, **cells)


def _holdings_pair(tmp_path: Path) -> Path:
    return _holdings_book(tmp_path, _cash_row(), _stock_row())


def _one_dividend(tmp_path: Path) -> Path:
    return _dividend_book(tmp_path, _div_row())


def _one_vest(tmp_path: Path) -> Path:
    return _cg_book(tmp_path, vests=(_vest_row(),))


def _vest_and_sale(tmp_path: Path) -> Path:
    return _cg_book(tmp_path, vests=(_vest_row(),), sales=(_sale_row(),))


@dataclass(frozen=True, slots=True)
class _ErrorCase:
    id: str
    book: Callable[[Path], Path]
    section: str
    row: int
    column: str
    codes: tuple[str, ...]


# Each case stores a marker (_HOLDER, or the Releases row's _AMOUNT price, since every
# other Releases column must hold a fixed value) in another cell of the error cell's row.
_ERROR_CASES = (
    # #1290: the Quote History sheet is read too.
    _ErrorCase(
        "quote-date",
        _with_cells(_holdings_pair, _QUOTES_SHEET, B4="#N/A", A5=_HOLDER),
        _QUOTES_SHEET,
        3,
        "#2",
        ("#N/A",),
    ),
    # The three free-text fields that took any nonblank text: each of these parsed before.
    _ErrorCase(
        "employee-number-on-every-row",
        _with_cells(_holdings_pair, _HOLDINGS_SHEET, A3="#N/A", A4="#N/A", B3=_HOLDER, B4=_HOLDER),
        _HOLDINGS_SHEET,
        2,
        "#1",
        ("#N/A",),
    ),
    _ErrorCase(
        "plan-name-on-every-row",
        _with_cells(_holdings_pair, _HOLDINGS_SHEET, B3="#N/A", B4="#N/A", A3=_HOLDER, A4=_HOLDER),
        _HOLDINGS_SHEET,
        2,
        "#2",
        ("#N/A",),
    ),
    _ErrorCase(
        "order-number",
        _with_cells(
            lambda tmp_path: _cg_book(tmp_path, vests=(_with(_vest_row(), 5, _AMOUNT),)),
            _RELEASES_SHEET,
            B3="#VALUE!",
        ),
        _RELEASES_SHEET,
        2,
        "#2",
        ("#VALUE!",),
    ),
    # Refused before this check too, later, as "non-numeric Number of Shares".
    _ErrorCase(
        "number-column",
        _with_cells(_holdings_pair, _HOLDINGS_SHEET, D3="#N/A", B3=_HOLDER),
        _HOLDINGS_SHEET,
        2,
        "#4",
        ("#N/A",),
    ),
    _ErrorCase(
        "title-row",
        _with_cells(_one_dividend, _DIV_SHEET, B1="#REF!", C1=_HOLDER),
        _DIV_SHEET,
        0,
        "#2",
        ("#REF!",),
    ),
    _ErrorCase(
        "header-row",
        _with_cells(_vest_and_sale, _CG_SHEET, F3="#DIV/0!", A3=_HOLDER),
        _CG_SHEET,
        2,
        "#6",
        ("#DIV/0!",),
    ),
    _ErrorCase(
        "data-row",
        _with_cells(_one_dividend, _DIV_SHEET, C3="#NUM!", B3=_HOLDER),
        _DIV_SHEET,
        2,
        "#3",
        ("#NUM!",),
    ),
    _ErrorCase(
        "footer-row",
        _with_cells(_one_vest, _RELEASES_SHEET, B4="#NUM!", C4=_HOLDER),
        _RELEASES_SHEET,
        3,
        "#2",
        ("#NUM!",),
    ),
    _ErrorCase(
        "past-declared-dimension",
        _past_dimension(A7="#VALUE!", B7=_HOLDER),
        _CG_SHEET,
        6,
        "#1",
        ("#VALUE!",),
    ),
    # The first in stored order, though the second is further left.
    _ErrorCase(
        "first-of-two",
        _with_cells(_one_dividend, _DIV_SHEET, G3="#REF!", A4="#N/A", F3=_HOLDER),
        _DIV_SHEET,
        2,
        "#7",
        ("#REF!", "#N/A"),
    ),
    # openpyxl's one allowed warning doesn't replace the refusal (#1103).
    _ErrorCase(
        "with-the-allowed-warning",
        lambda tmp_path: _without_named_styles(
            _with_cells(_one_dividend, _DIV_SHEET, B3="#N/A", D3=_HOLDER)(tmp_path)
        ),
        _DIV_SHEET,
        2,
        "#2",
        ("#N/A",),
    ),
)


@pytest.mark.parametrize("case", _ERROR_CASES, ids=lambda case: f"{case.section}:{case.id}")
def test_error_value_cell_refuses_at_its_location(tmp_path: Path, case: _ErrorCase) -> None:
    built = case.book(tmp_path)
    path = built.rename(built.with_name(f"{_FILE_MARKER} {built.name}"))
    with pytest.raises(ParseError) as excinfo:
        _PARSERS[case.section](path)
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=case.section,
        row=case.row,
        column=case.column,
        message=_STORED_ERROR,
        markers=(_HOLDER,) if case.section == _QUOTES_SHEET else (*_MARKERS, *_VALUES),
    )
    coordinate = f"{get_column_letter(int(case.column[1:]))}{case.row + 1}"
    assert_no_markers(str(excinfo.value), _FILE_MARKER, coordinate, *case.codes)


def test_error_cases_cover_every_sheet_and_code() -> None:
    assert {case.section for case in _ERROR_CASES} == set(_PARSERS)
    assert {code for case in _ERROR_CASES for code in case.codes} == set(_ERROR_CODES)


def _cached_error(path: Path) -> Path:
    """Make Holdings B4's formula a formula with a cached ``#N/A`` error."""
    _edit_sheets(path, _replace_once('<c r="B4">', '<c r="B4" t="e">'))
    _edit_sheets(path, _replace_once("</f><v />", "</f><v>#N/A</v>"))
    with ZipFile(path) as package:
        sheet = package.read("xl/worksheets/sheet1.xml").decode()
    assert re.search(r'<c r="B4" t="e"><f>[^<]*</f><v>#N/A</v></c>', sheet)
    return path


@pytest.mark.parametrize(
    ("book", "section", "row", "column", "message", "crash"),
    [
        pytest.param(
            _with_cells(
                _holdings_pair,
                _HOLDINGS_SHEET,
                B3="#N/A",
                A3=_HOLDER,
                B4=_formula("Synthetic Plan"),
            ),
            _HOLDINGS_SHEET,
            3,
            "#2",
            f"{_HOLDINGS_FILE} stores a formula in this cell; only stored values are "
            "supported. Re-export the report.",
            None,
            id="formula-after-an-error-cell",
        ),
        pytest.param(
            lambda tmp_path: _cached_error(
                _with_cells(
                    _holdings_pair, _HOLDINGS_SHEET, B4=_formula("Synthetic Plan"), A4=_HOLDER
                )(tmp_path)
            ),
            _HOLDINGS_SHEET,
            3,
            "#2",
            f"{_HOLDINGS_FILE} stores a formula in this cell; only stored values are "
            "supported. Re-export the report.",
            None,
            id="formula-with-a-cached-error",
        ),
        pytest.param(
            lambda tmp_path: _out_of_order(
                _with_cells(_one_dividend, _DIV_SHEET, B3="#N/A", D3=_HOLDER)(tmp_path)
            ),
            _DIV_SHEET,
            0,
            None,
            _unreadable(_DIV_SHEET, _DIV_FILE),
            ValueError,
            id="error-cell-then-malformed-order",
        ),
        pytest.param(
            lambda tmp_path: _out_of_order(
                _with_cells(_one_dividend, _DIV_SHEET, B3=_formula("#N/A"), D3=_HOLDER)(tmp_path)
            ),
            _DIV_SHEET,
            0,
            None,
            _unreadable(_DIV_SHEET, _DIV_FILE),
            ValueError,
            id="formula-then-malformed-order",
        ),
        pytest.param(
            lambda tmp_path: _date_styled_overflow(
                _with_cells(_one_dividend, _DIV_SHEET, B3="#N/A", D3=_HOLDER)(tmp_path),
                _DIV_SHEET,
                "A4",
            ),
            _DIV_SHEET,
            0,
            None,
            _unreadable(_DIV_SHEET, _DIV_FILE),
            None,
            id="error-cell-and-a-non-allowed-warning",
        ),
    ],
)
def test_earlier_refusals_still_win_over_an_error_cell(
    tmp_path: Path,
    book: Callable[[Path], Path],
    section: str,
    row: int,
    column: str | None,
    message: str,
    crash: type[BaseException] | None,
) -> None:
    with pytest.raises(ParseError) as excinfo:
        _PARSERS[section](book(tmp_path))
    _assert_fixed(
        excinfo.value,
        tmp_path,
        section=section,
        row=row,
        column=column,
        message=message,
        crash=crash,
    )
    assert_no_markers(str(excinfo.value), "#N/A")


def test_cell_encodings_other_than_an_error_value_load_as_before(tmp_path: Path) -> None:
    # Inline and shared strings, both holding the text "#N/A", a number, a boolean,
    # a date and an empty t="str" cell.
    path = tmp_path / "book.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.append(["Inline", "#N/A", "S0", "S1", 1.5, True, _dt.datetime(2025, 12, 10), "E"])
    worksheet["B1"].data_type = "s"
    workbook.save(path)
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet = payloads["xl/worksheets/sheet1.xml"].decode("utf-8")
    for old, new in (
        ('<c r="C1" t="inlineStr"><is><t>S0</t></is></c>', '<c r="C1" t="s"><v>0</v></c>'),
        ('<c r="D1" t="inlineStr"><is><t>S1</t></is></c>', '<c r="D1" t="s"><v>1</v></c>'),
        ('<c r="H1" t="inlineStr"><is><t>E</t></is></c>', '<c r="H1" t="str" />'),
    ):
        assert sheet.count(old) == 1
        sheet = sheet.replace(old, new)
    payloads["xl/worksheets/sheet1.xml"] = sheet.encode("utf-8")
    types = payloads["[Content_Types].xml"].decode("utf-8")
    payloads["[Content_Types].xml"] = types.replace(
        "</Types>",
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml" /></Types>',
    ).encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
        package.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Shared</t></si><si><t>#N/A</t></si></sst>",
        )

    rows = xlsx_mod.load_sheet_rows(path, worksheet.title, required_sheets={worksheet.title})

    assert rows == [
        ("Inline", "#N/A", "Shared", "#N/A", 1.5, True, _dt.datetime(2025, 12, 10), None)
    ]
