"""Read-time digit limits on Morgan Stanley StockPlan numbers (#1276).

StockPlan readers pass their numeric cells through
:func:`~ibkr_fa.number_checks.number_limit_breach`, in ``_xlsx.to_decimal``
right after its non-numeric and non-finite checks and before the caller's own
sign checks: a quantity may have at most 12 digits before the decimal point and
20 after it, a unit price 9 and 20, and a money amount 15 and 20. A number past
its limit refuses when it's read, with the reader's ``ParseError`` naming the
sheet, row and column and the kind's one fixed text, and no value or chain. An
integer cell has no digit limit of its own, and a float cell reads as
``Decimal(str(value))``, so a float near ``1e-300`` has hundreds of decimal
places. The Dividend Report's quote notice is matched by shape and skipped, but
the fallback price it quotes must be within a unit price's limits too.

Read-limit refusal inputs include the first refusing sizes, the issue's
integer, text and tiny-float shapes, and larger values. Nonzero breaching
numbers carry the digit run ``8353``; no refusal may print it. Zero ``Book
Value`` cells at the first refusing sizes are checked against their own text;
larger zero exponents carry the digit run. Each limit is also pinned from the
other side: a value exactly at it is
read (or reaches the check that follows), and a value an existing check refuses
keeps that refusal.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import NamedTuple

import openpyxl
import pytest

from ibkr_fa.cli import _STOCKPLAN_CONTRACT_POINTER, main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from tests._crash_reports import (
    assert_no_markers,
    assert_prints_no_marker,
    assert_unchained_refusal,
)
from tests._number_limits import MONEY, PRICE, QUANTITY
from tests._stockplan_fa_exports import setup_stockplan_exports as setup_fa_exports
from tests._stockplan_holdings import (
    QUOTE_PRICE,
    holdings_cash_row,
    holdings_xqcb_row,
    parse_holdings_report,
    quote_history_rows,
    write_holdings_report,
)
from tests._stockplan_report_writers import (
    STOCKPLAN_DIVIDEND_ISIN,
    STOCKPLAN_REPORT_SYMBOL,
    dividend_paid_row,
    dividend_withholding_row,
    release_vest_row,
    write_dividend_rows_report,
    write_releases_and_gains_report,
)

_MARKER = "8353"
# One digit past each limit as whole digits (the first refusing size), and 21
# decimal places; each holds the marker.
_QUANTITY_13 = "8353000000000"
_PRICE_10 = "8353000000"
_MONEY_16 = "8353000000000000"
_PLACES_21 = "9.000000000000000008353"
# Exactly each limit's digits.
_QUANTITY_AT = "999999999999.99999999999999999999"
_PRICE_AT = "999999999.99999999999999999999"
_MONEY_AT = "999999999999999.99999999999999999999"
# A marked variant of the issue's ``1e-300`` float cell with the same 300
# decimal places: it reads as ``Decimal("8.3538353E-293")``.
_TINY_FLOAT = 8.3538353e-293
assert Decimal(str(_TINY_FLOAT)).as_tuple().exponent == -300
# Floats whose text has 21 and exactly 20 decimal places.
_FLOAT_PLACES_21 = 1.8353e-17
_FLOAT_PLACES_20 = 1.8353e-16

_RELEASES = "Releases Report"
_SALES = "Capital Gains Report"
_HOLDINGS = "Holdings Report"
_QUOTES = "Quote History"
_DIVIDENDS = "IN - Dividend Report"


def _release_row() -> list[object]:
    return release_vest_row("2025-08-14", "RB0000A1", 196.42, 9.012, 6.214)


def _sale_row() -> list[object]:
    return [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        str(STOCKPLAN_REPORT_SYMBOL),
        380.0,
        _dt.datetime(2025, 8, 14),
    ]


def _holding_row() -> list[object]:
    return holdings_xqcb_row(26.374, 5823.47106)


def _dividend_row() -> list[object]:
    return dividend_paid_row("2025-12-10", 2.60)


def _write_releases(tmp_path: Path, row: list[object]) -> Path:
    return write_releases_and_gains_report(tmp_path, vest_rows=[row])


def _write_sales(tmp_path: Path, row: list[object]) -> Path:
    return write_releases_and_gains_report(tmp_path, vest_rows=[], cg_rows=[row])


def _write_holdings(tmp_path: Path, row: list[object], *, price: object = None) -> Path:
    """A Holdings workbook with ``row`` as its stock row, quoted at ``price``
    (default: the fixture's quote price)."""
    quotes = quote_history_rows(
        "XQCB - NASDAQ", price=float(QUOTE_PRICE) if price is None else price
    )
    return write_holdings_report(tmp_path, [holdings_cash_row(), row], quotes=quotes)


def _stock_quote_row() -> list[object]:
    return quote_history_rows("XQCB - NASDAQ")[3]


def _write_quotes(tmp_path: Path, row: list[object]) -> Path:
    """A Holdings workbook whose stock quote is ``row``."""
    return write_holdings_report(
        tmp_path,
        [holdings_cash_row(), _holding_row()],
        quotes=[*quote_history_rows("XQCB - NASDAQ")[:3], row],
    )


def _write_dividends(tmp_path: Path, row: list[object]) -> Path:
    return write_dividend_rows_report(tmp_path, [row])


def _read_releases(path: Path) -> object:
    return parse_stockplan_releases(path, symbol=STOCKPLAN_REPORT_SYMBOL)


def _read_sales(path: Path) -> object:
    return parse_stockplan_capital_gains_report(path, symbol=STOCKPLAN_REPORT_SYMBOL)


def _read_holdings(path: Path) -> object:
    return parse_holdings_report(path)


def _read_dividends(path: Path) -> object:
    return parse_stockplan_dividend_report(
        path, symbol=STOCKPLAN_REPORT_SYMBOL, isin=STOCKPLAN_DIVIDEND_ISIN
    )


class _Column(NamedTuple):
    """A numeric StockPlan column: its row builder and cell, the workbook
    writer and reader, and where its refusal points."""

    row: Callable[[], list[object]]
    index: int
    write: Callable[[Path, list[object]], Path]
    read: Callable[[Path], object]
    section: str
    row_index: int
    name: str
    kind: str

    def path(self, tmp_path: Path, value: object) -> Path:
        """A workbook whose one data row holds ``value`` in this column."""
        row = self.row()
        row[self.index] = value
        return self.write(tmp_path, row)


def _releases(index: int, name: str, kind: str) -> _Column:
    return _Column(_release_row, index, _write_releases, _read_releases, _RELEASES, 2, name, kind)


def _sales(index: int, name: str, kind: str) -> _Column:
    return _Column(_sale_row, index, _write_sales, _read_sales, _SALES, 3, name, kind)


def _holdings(index: int, name: str, kind: str) -> _Column:
    return _Column(_holding_row, index, _write_holdings, _read_holdings, _HOLDINGS, 3, name, kind)


def _quotes(index: int, name: str, kind: str) -> _Column:
    return _Column(_stock_quote_row, index, _write_quotes, _read_holdings, _QUOTES, 3, name, kind)


def _dividends(index: int, name: str, kind: str) -> _Column:
    return _Column(
        _dividend_row, index, _write_dividends, _read_dividends, _DIVIDENDS, 2, name, kind
    )


_RELEASE_PRICE = _releases(5, "Price", PRICE)
_RELEASE_QUANTITY = _releases(6, "Quantity", QUANTITY)
_RELEASE_NET_CASH = _releases(7, "Net Cash Proceeds", MONEY)
_RELEASE_NET_SHARES = _releases(8, "Net Share Proceeds", QUANTITY)
_SALE_PRICE = _sales(1, "Sale Price Per Share", PRICE)
_SALE_SHARES = _sales(2, "Number of Shares Sold", QUANTITY)
_SALE_GROSS = _sales(4, "Total Gross Proceeds", MONEY)
_HOLDING_SHARES = _holdings(3, "Number of Shares", QUANTITY)
_HOLDING_ACQUISITION = _holdings(4, "Acquisition Value", MONEY)
_HOLDING_MARKET = _holdings(5, "Market Value", MONEY)
_QUOTE_PRICE = _quotes(2, "Price", PRICE)
_DIVIDEND_CASH = _dividends(2, "Cash", MONEY)
_BOOK_VALUE = _dividends(6, "Book Value", MONEY)

_COLUMNS = (
    _RELEASE_PRICE,
    _RELEASE_QUANTITY,
    _RELEASE_NET_CASH,
    _RELEASE_NET_SHARES,
    _SALE_PRICE,
    _SALE_SHARES,
    _SALE_GROSS,
    _HOLDING_SHARES,
    _HOLDING_ACQUISITION,
    _HOLDING_MARKET,
    _QUOTE_PRICE,
    _DIVIDEND_CASH,
    _BOOK_VALUE,
)
_FIRST_WHOLE = {QUANTITY: _QUANTITY_13, PRICE: _PRICE_10, MONEY: _MONEY_16}


def _refusal(column: _Column, path: Path) -> ParseError:
    with pytest.raises(ParseError) as raised:
        column.read(path)
    return raised.value


def _assert_read_refusal(
    error: ParseError, column: _Column, message: str, *, markers: tuple[str, ...]
) -> None:
    """``error`` is the reader's fixed refusal at ``column``'s cell, unchained,
    printing none of ``markers``."""
    assert_unchained_refusal(
        error,
        ParseError(
            section=column.section,
            row_index=column.row_index,
            column=column.name,
            message=message,
            raw_value="",
        ),
        f"ParseError in section {column.section!r} at row {column.row_index}, "
        f"column {column.name!r}: {message}",
        markers=markers,
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param(_HOLDING_SHARES, int(_QUANTITY_13), id="integer-cell"),
        pytest.param(_RELEASE_PRICE, _PRICE_10, id="text-cell"),
        pytest.param(_DIVIDEND_CASH, _TINY_FLOAT, id="float-cell-300-places"),
    ],
)
def test_issue_cells_past_their_limits_refuse_when_read(
    tmp_path: Path, column: _Column, value: object
) -> None:
    """The issue's integer cell, text cell and marked 300-place float cell each
    parsed before #1276; each now refuses at its cell."""
    error = _refusal(column, column.path(tmp_path, value))
    _assert_read_refusal(error, column, column.kind, markers=(_MARKER,))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        *(
            pytest.param(column, int(_FIRST_WHOLE[column.kind]), id=f"{column.name}-whole")
            for column in _COLUMNS
        ),
        *(pytest.param(column, _PLACES_21, id=f"{column.name}-places") for column in _COLUMNS),
    ],
)
def test_column_number_one_digit_past_its_limit_refuses_when_read(
    tmp_path: Path, column: _Column, value: object
) -> None:
    """One whole digit (an integer cell) or one decimal place (a text cell)
    past the column's kind refuses before any later check of the row."""
    error = _refusal(column, column.path(tmp_path, value))
    _assert_read_refusal(error, column, column.kind, markers=(_MARKER,))


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        # First refusing zero sizes, checked against their own cell text.
        pytest.param("0E-21", "0E-21", id="zero-21-places"),
        pytest.param("0E+15", "0E+15", id="zero-16-whole-digits"),
        pytest.param("0.000000000000000000000", "0.000000000000000000000", id="zero-text-places"),
        pytest.param(f"0E-{_MARKER}", _MARKER, id="zero-marked-places"),
        pytest.param(f"0E+{_MARKER}", _MARKER, id="zero-marked-whole-digits"),
    ],
)
def test_zero_book_value_past_the_money_limit_refuses(
    tmp_path: Path, value: str, marker: str
) -> None:
    """``Book Value`` must be zero; before #1276 a zero with any number of
    digits passed that check. It now refuses as a money amount past its limit."""
    error = _refusal(_BOOK_VALUE, _BOOK_VALUE.path(tmp_path, value))
    _assert_read_refusal(error, _BOOK_VALUE, MONEY, markers=(marker,))


@pytest.mark.parametrize("value", ["0E-20", "0E+14", "0.00000000000000000000", 0.0, 0])
def test_zero_book_value_at_the_money_limit_parses(tmp_path: Path, value: object) -> None:
    path = _BOOK_VALUE.path(tmp_path, value)
    assert _read_dividends(path) == _read_dividends(_BOOK_VALUE.path(tmp_path, 0.0))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param(_RELEASE_PRICE, f"-{_PRICE_10}", id="negative-release-price"),
        pytest.param(_SALE_PRICE, f"-{_PRICE_10}", id="negative-sale-price"),
        pytest.param(_SALE_SHARES, f"-{_QUANTITY_13}", id="negative-shares-sold"),
        pytest.param(_SALE_SHARES, "-0.000000000000000008353", id="negative-shares-sold-places"),
        pytest.param(_HOLDING_SHARES, f"-{_QUANTITY_13}", id="negative-held-shares"),
        pytest.param(_RELEASE_NET_SHARES, f"-{_PLACES_21}", id="negative-net-shares"),
        pytest.param(_DIVIDEND_CASH, f"-{_MONEY_16}", id="negative-dividend"),
        pytest.param(_RELEASE_NET_CASH, _MONEY_16, id="non-zero-release-cash"),
        pytest.param(_BOOK_VALUE, _MONEY_16, id="non-zero-book-value"),
    ],
)
def test_limit_check_comes_before_the_callers_value_checks(
    tmp_path: Path, column: _Column, value: str
) -> None:
    """The limit ignores the sign and runs in ``to_decimal``, so a value past it
    that the caller's own check would also refuse (a sign, zero or non-zero
    rule) meets the digit limit first."""
    error = _refusal(column, column.path(tmp_path, value))
    _assert_read_refusal(error, column, column.kind, markers=(_MARKER,))


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        pytest.param(
            _RELEASE_PRICE, f"{_PRICE_10}x", "non-numeric Price", id="non-numeric-release-price"
        ),
        pytest.param(
            _HOLDING_MARKET,
            f"{_MONEY_16} USD",
            "non-numeric Market Value",
            id="non-numeric-market-value",
        ),
        pytest.param(_RELEASE_PRICE, f"sNaN{_PRICE_10}", "non-finite Price", id="snan-price"),
        pytest.param(
            _SALE_GROSS,
            f"NaN{_MONEY_16}",
            "non-finite Total Gross Proceeds",
            id="nan-gross",
        ),
        pytest.param(
            _SALE_PRICE,
            f"-{_MARKER}.5",
            "Sale Price Per Share must not be negative",
            id="negative-sale-price",
        ),
        pytest.param(
            _RELEASE_PRICE,
            f"-{_MARKER}.5",
            "Fair Market Value (Price) must be positive",
            id="negative-release-price",
        ),
        pytest.param(
            _SALE_SHARES,
            f"-{_MARKER}",
            "Number of Shares Sold must be positive",
            id="negative-shares-sold",
        ),
        pytest.param(
            _SALE_SHARES, "0.0000", "Number of Shares Sold must be positive", id="zero-sold"
        ),
        pytest.param(
            _DIVIDEND_CASH,
            f"-{_MARKER}.25",
            "'Dividend Paid' must be a positive gross amount",
            id="negative-dividend",
        ),
        pytest.param(
            _BOOK_VALUE,
            f"{_MARKER}.25",
            "Book Value must be zero for the validated StockPlan dividend schema",
            id="non-zero-book-value",
        ),
        pytest.param(
            _RELEASE_NET_CASH,
            f"{_MARKER}.25",
            "non-zero release cash is unsupported",
            id="non-zero-release-cash",
        ),
    ],
)
def test_value_an_existing_check_refuses_keeps_that_refusal(
    tmp_path: Path, column: _Column, value: str, message: str
) -> None:
    """Text that isn't a number, a NaN (even one whose payload is past every
    limit) and an in-limit value of the wrong sign or size keep the refusal
    they met before #1276, at the same cell. A non-numeric refusal keeps its
    suppressed parser context, as before; nothing in the graph prints the cell."""
    error = _refusal(column, column.path(tmp_path, value))
    assert type(error) is ParseError
    assert error == ParseError(
        section=column.section,
        row_index=column.row_index,
        column=column.name,
        message=message,
        raw_value="",
    )
    assert str(error) == (
        f"ParseError in section {column.section!r} at row {column.row_index}, "
        f"column {column.name!r}: {message}"
    )
    assert error.__cause__ is None
    marker = "0000" if value == "0.0000" else _MARKER
    assert marker in value
    assert_prints_no_marker(error, markers=(marker,))


def test_zero_holding_shares_keep_the_positive_holdings_refusal(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as raised:
        _read_holdings(_HOLDING_SHARES.path(tmp_path, "0"))
    assert str(raised.value) == (
        "ParseError in section 'Holdings Report' at row 3, column 'Number of Shares': "
        "the configured stock's holdings must have positive shares, acquisition value, "
        "and market value"
    )


@pytest.mark.parametrize(
    "net_cash", ["0.00000000000000000000", "0E+14", 0.0], ids=["places", "whole", "float"]
)
def test_releases_numbers_at_their_limits_parse(tmp_path: Path, net_cash: object) -> None:
    """Price at a unit price's limits, Quantity and Net Share Proceeds at a
    quantity's, and Net Cash Proceeds a zero at a money amount's."""
    row = _release_row()
    row[5:9] = [_PRICE_AT, _QUANTITY_AT, net_cash, _QUANTITY_AT]
    [vest] = parse_stockplan_releases(
        _write_releases(tmp_path, row), symbol=STOCKPLAN_REPORT_SYMBOL
    )[0]
    [base] = parse_stockplan_releases(
        _write_releases(tmp_path, _release_row()), symbol=STOCKPLAN_REPORT_SYMBOL
    )[0]
    assert vest == replace(
        base,
        fmv_per_share=Decimal(_PRICE_AT),
        gsus_vested=Decimal(_QUANTITY_AT),
        shares_deposited=Decimal(_QUANTITY_AT),
    )


def test_non_zero_release_cash_at_the_limit_reaches_the_zero_cash_check(tmp_path: Path) -> None:
    error = _refusal(_RELEASE_NET_CASH, _RELEASE_NET_CASH.path(tmp_path, _MONEY_AT))
    _assert_read_refusal(
        error,
        _RELEASE_NET_CASH,
        "non-zero release cash is unsupported",
        markers=("999999999999999",),
    )


def test_capital_gains_numbers_at_their_limits_parse(tmp_path: Path) -> None:
    row = _sale_row()
    row[1:3] = [_PRICE_AT, _QUANTITY_AT]
    row[4] = _MONEY_AT
    [sale] = parse_stockplan_capital_gains_report(
        _write_sales(tmp_path, row), symbol=STOCKPLAN_REPORT_SYMBOL
    )[0]
    [base] = parse_stockplan_capital_gains_report(
        _write_sales(tmp_path, _sale_row()), symbol=STOCKPLAN_REPORT_SYMBOL
    )[0]
    assert sale == replace(
        base,
        price_per_share=Decimal(_PRICE_AT),
        shares_sold=Decimal(_QUANTITY_AT),
        gross_proceeds=Decimal(_MONEY_AT),
    )


def test_holdings_numbers_at_their_limits_parse(tmp_path: Path) -> None:
    # Quoted at 1000, the quantity at its limit gives a Market Value at its own:
    # 15 whole digits and 20 places.
    row = _holding_row()
    row[3:6] = [_QUANTITY_AT, _MONEY_AT, "999999999999999.99999999999999999000"]
    holdings = parse_holdings_report(_write_holdings(tmp_path, row, price=1000))
    base = parse_holdings_report(_write_holdings(tmp_path, _holding_row()))
    assert holdings == replace(
        base, shares=Decimal(_QUANTITY_AT), acquisition_value=Decimal(_MONEY_AT)
    )


@pytest.mark.parametrize(
    ("row", "attribute", "value"),
    [
        pytest.param(dividend_paid_row("2025-12-10", 2.60), "amount_gross", _MONEY_AT, id="div"),
        pytest.param(
            dividend_withholding_row("2025-12-10", -0.65), "amount", f"-{_MONEY_AT}", id="wht"
        ),
    ],
)
def test_dividend_cash_at_the_money_limit_parses(
    tmp_path: Path, row: list[object], attribute: str, value: str
) -> None:
    read = partial(
        parse_stockplan_dividend_report,
        symbol=STOCKPLAN_REPORT_SYMBOL,
        isin=STOCKPLAN_DIVIDEND_ISIN,
    )
    base = read(_write_dividends(tmp_path, row))
    row = [*row[:2], value, *row[3:]]
    dividends, withholdings, coverage = read(_write_dividends(tmp_path, row))
    assert coverage == base[2]
    [model] = dividends or withholdings
    [base_model] = base[0] or base[1]
    assert model == replace(base_model, **{attribute: Decimal(value)})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(999999999999, Decimal("999999999999"), id="integer-12-digits"),
        pytest.param(_FLOAT_PLACES_20, Decimal("1.8353E-16"), id="float-20-places"),
        pytest.param("1E-20", Decimal("1E-20"), id="exponent-text-20-places"),
    ],
)
def test_integer_and_float_cells_at_the_quantity_limit_parse(
    tmp_path: Path, value: object, expected: Decimal
) -> None:
    """An integer cell is read with all its digits and a float as its text,
    so each is checked like the same text."""
    [sale] = parse_stockplan_capital_gains_report(
        _SALE_SHARES.path(tmp_path, value), symbol=STOCKPLAN_REPORT_SYMBOL
    )[0]
    assert sale.shares_sold == expected
    assert sale.shares_sold.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(int(_QUANTITY_13), id="integer-13-digits"),
        pytest.param(_FLOAT_PLACES_21, id="float-21-places"),
        pytest.param(float(_QUANTITY_13), id="float-13-whole-digits"),
        pytest.param(f"{_MARKER}E+9", id="exponent-text-13-whole-digits"),
    ],
)
def test_integer_and_float_cells_past_the_quantity_limit_refuse(
    tmp_path: Path, value: object
) -> None:
    error = _refusal(_SALE_SHARES, _SALE_SHARES.path(tmp_path, value))
    _assert_read_refusal(error, _SALE_SHARES, QUANTITY, markers=(_MARKER,))


# --- the Dividend Report's quote notice ---------------------------------------------


def _quote_notice(price: str) -> list[object]:
    return [
        "Unable to retrieve market price for fund: [.XQCB.NASDAQ.] "
        f"on date: 1899/12/31. Returned ${price} USD instead."
    ]


@pytest.mark.parametrize(
    "price",
    [
        pytest.param(_PRICE_10, id="whole"),
        pytest.param("0.000000000000000008353", id="places"),
        pytest.param(f"{_PRICE_10}.25", id="whole-and-cents"),
    ],
)
def test_quote_notice_price_past_the_price_limit_refuses(tmp_path: Path, price: str) -> None:
    """The notice's fallback price is never kept, but a notice quoting a price
    past a unit price's limits refuses at its row, before the data rows."""
    path = write_dividend_rows_report(tmp_path, [_quote_notice(price), _dividend_row()])
    with pytest.raises(ParseError) as raised:
        _read_dividends(path)
    assert_unchained_refusal(
        raised.value,
        ParseError(section=_DIVIDENDS, row_index=2, column=None, message=PRICE, raw_value=""),
        f"ParseError in section {_DIVIDENDS!r} at row 2: {PRICE}",
        markers=(_MARKER,),
    )


@pytest.mark.parametrize("price", ["0.00", "999999999", _PRICE_AT])
def test_quote_notice_price_at_the_price_limit_is_skipped(tmp_path: Path, price: str) -> None:
    path = write_dividend_rows_report(tmp_path, [_quote_notice(price), _dividend_row()])
    assert _read_dividends(path) == _read_dividends(_write_dividends(tmp_path, _dividend_row()))


# --- stockplan-fa ---------------------------------------------------------------------


class _Edit(NamedTuple):
    """A cell of the stockplan-fa fixture exports: workbook, sheet, 1-based
    row and column, the value written, and the refusal's location."""

    workbook: str
    sheet: str
    row: int
    column: int
    value: object
    where: str
    kind: str


_CLI_EDITS = [
    pytest.param(
        _Edit(
            "Vested Share Holdings.xlsx",
            _HOLDINGS,
            4,
            4,
            int(_QUANTITY_13),
            f"section {_HOLDINGS!r} at row 3, column 'Number of Shares'",
            QUANTITY,
        ),
        id="holdings-integer-cell",
    ),
    pytest.param(
        _Edit(
            "IN - Capital Gain_Loss Report.xlsx",
            _RELEASES,
            3,
            6,
            f"196.{_PLACES_21[2:]}",
            f"section {_RELEASES!r} at row 2, column 'Price'",
            PRICE,
        ),
        id="releases-text-cell",
    ),
    pytest.param(
        _Edit(
            "IN - Dividend Report.xlsx",
            _DIVIDENDS,
            3,
            3,
            _TINY_FLOAT,
            f"section {_DIVIDENDS!r} at row 2, column 'Cash'",
            MONEY,
        ),
        id="dividend-float-cell",
    ),
]


@pytest.mark.parametrize("edit", _CLI_EDITS)
def test_stockplan_fa_refuses_a_cell_past_its_limit_and_removes_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: _Edit
) -> None:
    """Exit 4 with the fixed refusal and the contract pointer alone on stderr,
    nothing on stdout, no marker printed, and an earlier run's output gone."""
    input_dir, cfg, out = setup_fa_exports(tmp_path)
    workbook_path = input_dir / edit.workbook
    workbook = openpyxl.load_workbook(workbook_path)
    workbook[edit.sheet].cell(row=edit.row, column=edit.column, value=edit.value)
    workbook.save(workbook_path)
    out.write_text("an earlier run", encoding="utf-8")

    rc = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025", "--no-fetch-prices"),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: ParseError in {edit.where}: {edit.kind}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, _MARKER)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["in", "user.toml"]
