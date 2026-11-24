"""Tests for the Morgan Stanley StockPlan "Holdings Report" parser (#365)."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from ibkr_fa.exceptions import ParseError
from tests._stockplan_holdings import HOLDINGS_QUOTES as _QUOTES
from tests._stockplan_holdings import QUOTE_DATE, QUOTE_PRICE, market_value, quote_history_rows
from tests._stockplan_holdings import holdings_cash_row as _cash_row
from tests._stockplan_holdings import holdings_xqcb_row as _xqcb_row
from tests._stockplan_holdings import parse_holdings_report as _parse
from tests._stockplan_holdings import write_holdings_report as _write


def test_parses_holdings_for_symbol(tmp_path: Path) -> None:
    path = _write(tmp_path, [_cash_row(), _xqcb_row(26.374, 5823.47106)])
    holdings = _parse(path)
    assert holdings.shares == Decimal("26.374")
    assert holdings.acquisition_value == Decimal("5823.47106")
    assert holdings.selected_date == QUOTE_DATE


def test_validates_cash_row(tmp_path: Path) -> None:
    path = _write(tmp_path, [_cash_row(), _xqcb_row(26.374, 5823.47106)])
    assert _parse(path).shares == Decimal("26.374")


def test_missing_symbol_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_cash_row()])  # no XQCB row
    with pytest.raises(
        ParseError, match="expected exactly one holdings row for the configured stock, found 0"
    ):
        _parse(path)


def test_duplicate_symbol_row_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_cash_row(), _xqcb_row(26.374, 5823.47106), _xqcb_row(10.0, 3000.0)],
    )
    with pytest.raises(ParseError, match="found 2"):
        _parse(path)


def test_nonpositive_holdings_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(0.0, 0.0)])
    with pytest.raises(ParseError, match="must have positive shares"):
        _parse(path)


def test_missing_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)], sheet="Wrong Sheet")
    with pytest.raises(ParseError, match="has no 'Holdings Report' sheet"):
        _parse(path)


def test_extra_header_column_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)])
    wb = openpyxl.load_workbook(path)
    wb["Holdings Report"].cell(row=2, column=7, value="New Column")
    wb.save(path)
    with pytest.raises(ParseError, match="expected exact header"):
        _parse(path)


def test_extra_data_column_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [[*_xqcb_row(26.374, 5823.47106), "new value"]])
    with pytest.raises(ParseError, match="unexpected populated column"):
        _parse(path)


def test_unknown_text_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106), ["New holdings section"]])
    with pytest.raises(ParseError, match="unknown non-data row") as excinfo:
        _parse(path)
    assert excinfo.value.row_index == 3
    assert "New holdings section" not in str(excinfo.value)


def test_changed_definition_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)])
    wb = openpyxl.load_workbook(path)
    wb["Holdings Report"].cell(row=4, column=1, value="Definitions changed")
    wb.save(path)
    with pytest.raises(ParseError, match="unknown non-data row") as excinfo:
        _parse(path)
    assert excinfo.value.row_index == 3
    assert "Definitions changed" not in str(excinfo.value)


def test_unexpected_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)])
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("New Holdings")
    wb.save(path)
    with pytest.raises(ParseError, match="unexpected sheet"):
        _parse(path)


def test_missing_quote_history_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_cash_row(), _xqcb_row(26.374, 5823.47106)], quotes=None)
    with pytest.raises(ParseError) as excinfo:
        _parse(path)
    assert str(excinfo.value) == (
        "ParseError in section 'Quote History' at row 0: "
        "Vested Share Holdings.xlsx has no 'Quote History' sheet"
    )


def test_holdings_sheet_refusals_come_before_the_quote_history_check(tmp_path: Path) -> None:
    # The Quote History sheet is read only after the Holdings Report sheet
    # passes, so a workbook without it keeps that sheet's refusal (#1290).
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)], quotes=None)
    with pytest.raises(ParseError, match="expected exactly one 'Cash - USD' row, found 0"):
        _parse(path)


def _quote_refusal(
    tmp_path: Path, quotes: Sequence[list[object]], holding: list[object] | None = None
) -> str:
    holding = holding if holding is not None else _xqcb_row(26.374, 5823.47106)
    path = _write(tmp_path, [_cash_row(), holding], quotes=quotes)
    with pytest.raises(ParseError) as excinfo:
        _parse(path)
    assert excinfo.value.raw_value == ""
    return str(excinfo.value)


_CASH_QUOTE = ["Cash - USD", _dt.datetime(2026, 7, 10), 1.0]
_STOCK_QUOTE = ["XQCB - NASDAQ", _dt.datetime(2026, 7, 10), float(QUOTE_PRICE)]
_QUOTE_HEAD = [["Quote History"], ["Fund", "Quote Date", "Price"]]


@pytest.mark.parametrize(
    ("quotes", "message"),
    [
        pytest.param(
            [["Quotes"], *_QUOTE_HEAD[1:], _CASH_QUOTE, _STOCK_QUOTE],
            "at row 0: expected title row in Vested Share Holdings.xlsx",
            id="title",
        ),
        pytest.param(
            [_QUOTE_HEAD[0], ["Fund", "Date", "Price"], _CASH_QUOTE, _STOCK_QUOTE],
            "at row 1: expected exact header row ('Fund', 'Quote Date', 'Price') in "
            "Vested Share Holdings.xlsx",
            id="header",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, [*_STOCK_QUOTE, "USD"]],
            "at row 3, column '#4': unexpected populated column beyond the known 3-column schema",
            id="extra-column",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, _STOCK_QUOTE, ["All currencies are in US Dollars"]],
            "at row 4: unknown non-data row; the StockPlan report schema may have changed",
            id="text-row",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, _STOCK_QUOTE, ["XQBN - NASDAQ", *_STOCK_QUOTE[1:]]],
            "at row 4, column 'Fund': unexpected quote; expected only 'Cash - USD' and the "
            "configured [stockplan.security] symbol and listing exchange",
            id="other-fund",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE],
            "at row 1, column 'Fund': expected exactly one 'Cash - USD' quote and one quote "
            "for the configured stock",
            id="no-stock-quote",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, _CASH_QUOTE, _STOCK_QUOTE],
            "at row 1, column 'Fund': expected exactly one 'Cash - USD' quote and one quote "
            "for the configured stock",
            id="two-cash-quotes",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, [_STOCK_QUOTE[0], "10-Jul-2026", _STOCK_QUOTE[2]]],
            "at row 3, column 'Quote Date': data row has a missing or non-date Quote Date",
            id="text-date",
        ),
        pytest.param(
            [*_QUOTE_HEAD, _CASH_QUOTE, [*_STOCK_QUOTE[:2], "price"]],
            "at row 3, column 'Price': non-numeric Price",
            id="text-price",
        ),
        pytest.param(
            [*_QUOTE_HEAD, [*_CASH_QUOTE[:2], 1.01], _STOCK_QUOTE],
            "at row 2, column 'Price': the 'Cash - USD' quote must be 1",
            id="cash-price",
        ),
        pytest.param(
            [*_QUOTE_HEAD, [_CASH_QUOTE[0], _dt.datetime(2026, 7, 9), 1.0], _STOCK_QUOTE],
            "at row 3, column 'Quote Date': the 'Cash - USD' and stock quotes must share one "
            "Quote Date",
            id="two-dates",
        ),
        pytest.param(
            quote_history_rows("XQCB - NASDAQ", quote_date=_dt.date(2026, 7, 15)),
            "at row 3, column 'Quote Date': the Quote Date must not be after the Holdings "
            "Report's run date",
            id="after-the-run-date",
        ),
    ],
)
def test_malformed_quote_history_refuses(
    tmp_path: Path, quotes: list[list[object]], message: str
) -> None:
    assert _quote_refusal(tmp_path, quotes) == f"ParseError in section 'Quote History' {message}"


def test_quote_date_on_the_run_date_is_the_selected_date(tmp_path: Path) -> None:
    quotes = quote_history_rows("XQCB - NASDAQ", quote_date=_dt.date(2026, 7, 14))
    path = _write(tmp_path, [_cash_row(), _xqcb_row(26.374, 5823.47106)], quotes=quotes)
    assert _parse(path).selected_date == _dt.date(2026, 7, 14)


_MARKET_VALUE_REFUSAL = (
    "ParseError in section 'Holdings Report' at row 3, column 'Market Value': Market Value "
    "must equal the 'Quote History' sheet's Price x Number of Shares exactly"
)


def test_market_value_off_the_quote_price_refuses(tmp_path: Path) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[5] = market_value(26.374) + 0.01
    assert _quote_refusal(tmp_path, _QUOTES, holding) == _MARKET_VALUE_REFUSAL


def test_market_value_check_keeps_every_digit(tmp_path: Path) -> None:
    # Price x shares has 41 significant digits; rounded to the default 28 it
    # would equal this Market Value, so the check must not round.
    tiny = "1.00000000000000000001"
    holding = _xqcb_row(26.374, 5823.47106)
    holding[3], holding[5] = tiny, "1.00000000000000000002"
    assert (
        _quote_refusal(tmp_path, quote_history_rows("XQCB - NASDAQ", price=tiny), holding)
        == _MARKET_VALUE_REFUSAL
    )


def test_additional_security_refuses(tmp_path: Path) -> None:
    other = _xqcb_row(1.0, 100.0)
    other[2] = "XQBN - NASDAQ"
    path = _write(tmp_path, [_cash_row(), _xqcb_row(26.374, 5823.47106), other])
    with pytest.raises(ParseError, match="unexpected holding;") as excinfo:
        _parse(path)
    assert excinfo.value.row_index == 4
    assert "XQBN" not in str(excinfo.value)


def test_wrong_exchange_refuses(tmp_path: Path) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[2] = "XQCB - NYSE"
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="unexpected holding;") as excinfo:
        _parse(path)
    assert excinfo.value.row_index == 3
    assert "NYSE" not in str(excinfo.value)


def test_inconsistent_employee_identity_refuses(tmp_path: Path) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[0] = "different employee"
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="inconsistent employee/plan identities"):
        _parse(path)


def test_blank_plan_identity_refuses(tmp_path: Path) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[1] = ""
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="nonblank employee and plan identity"):
        _parse(path)


def test_nonpositive_market_value_refuses(tmp_path: Path) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[5] = 0.0
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="positive shares, acquisition value, and market value"):
        _parse(path)


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_number_refuses(tmp_path: Path, text: str) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[5] = text
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="non-finite Market Value") as excinfo:
        _parse(path)
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Holdings Report",
        3,
        "Market Value",
        "",
    )


@pytest.mark.parametrize("flag", [True, False])
def test_bool_number_cell_refuses(tmp_path: Path, flag: bool) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[5] = flag
    path = _write(tmp_path, [_cash_row(), holding])
    with pytest.raises(ParseError, match="non-numeric Market Value") as excinfo:
        _parse(path)
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Holdings Report",
        3,
        "Market Value",
        "",
    )


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("1_000", "1000"),
        ("1e3", "1E+3"),
        (" 12 ", "12"),
        ("+5", "5"),
        (7, "7"),
        (26.374, "26.374"),
    ],
)
def test_finite_number_spellings_still_parse(tmp_path: Path, cell: object, expected: str) -> None:
    holding = _xqcb_row(26.374, 5823.47106)
    holding[3] = cell
    holding[5] = market_value(expected)
    path = _write(tmp_path, [_cash_row(), holding])
    assert str(_parse(path).shares) == expected


def test_negative_cash_refuses(tmp_path: Path) -> None:
    cash = _cash_row()
    cash[3:6] = [-1.0, -1.0, -1.0]
    path = _write(tmp_path, [cash, _xqcb_row(26.374, 5823.47106)])
    with pytest.raises(ParseError, match="Cash - USD values must not be negative"):
        _parse(path)


def test_mismatched_cash_values_refuse(tmp_path: Path) -> None:
    cash = _cash_row()
    cash[5] = 7.0
    path = _write(tmp_path, [cash, _xqcb_row(26.374, 5823.47106)])
    with pytest.raises(ParseError, match=r"Cash - USD shares, acquisition value.*must match"):
        _parse(path)


def test_missing_cash_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_xqcb_row(26.374, 5823.47106)])
    with pytest.raises(ParseError, match="expected exactly one 'Cash - USD' row, found 0"):
        _parse(path)


def test_duplicate_cash_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_cash_row(), _cash_row(), _xqcb_row(26.374, 5823.47106)])
    with pytest.raises(ParseError, match="expected exactly one 'Cash - USD' row, found 2"):
        _parse(path)
