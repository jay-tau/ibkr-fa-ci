"""Read-time digit limits on NetBenefits numbers and the 1042-S USD flag (#1275).

Each NetBenefits reader passes the numbers it reads through
:func:`~ibkr_fa.number_checks.number_limit_breach`: a quantity may have at most
12 digits before the decimal point and 20 after it, a unit price 9 and 20, a
money amount 15 and 20, and a rate 3 and 6. A number past its limit refuses when it's read, with
the reader's ``ParseError`` naming the file, row and column and the kind's one
fixed text, and no value or chain. The lots CSV and confirmation grammars allow
arbitrary decimal places for shares and prices before the numeric limit applies.
The statement grammar fixes quantity, price and money places, and confirmation
money has 2 places, so their decimal-place limits cannot trip. A number with
exactly its limit's digits still parses.
``--reconcile-1042s-fy-withholding-usd`` is a money amount: past its limit it's
a usage error (exit 2), after the existing shape, sign and whole-cents checks.

Every value is synthetic. Privacy checks name only markers present in that
test's input, including a digit run within each new breaching cell.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.netbenefits.confirmation_pdf import parse_confirmation
from ibkr_fa.parser.netbenefits.lots_csv import EspLot, parse_closed_lots, parse_open_lots
from ibkr_fa.parser.netbenefits.statement_pdf import parse_statement
from tests._crash_reports import (
    assert_no_markers,
    assert_replaced_unchained,
    assert_unchained_refusal,
)
from tests._netbenefits import CLOSED_WITH_ROW_CSV, OPEN_CSV
from tests._netbenefits import CONF_DEC_STREET_ALIGNED as CONF_DEC
from tests._netbenefits_e2e import NB_E2E_PDF_TEXT, nb_e2e_bundle
from tests._netbenefits_statement_pdf import HOLDING_XESP as _HOLDING_XESP
from tests._netbenefits_statement_pdf import MONTHLY
from tests._netbenefits_statement_pdf import TOTAL_CORE_LINE as _TOTAL_CORE_LINE
from tests._netbenefits_statement_pdf import TOTAL_HOLDINGS_LINE as _TOTAL_HOLDINGS_LINE
from tests._netbenefits_statement_pdf import TOTAL_STOCKS_LINE as _TOTAL_STOCKS_LINE
from tests._netbenefits_statement_pdf import UNREAD_TOTALS as _UNREAD_TOTALS
from tests._netbenefits_statement_pdf import XESP_ROW as _XESP_ROW
from tests._netbenefits_statement_pdf import YEAR_END_WHT_LINE as _YEAR_END_WHT_LINE
from tests._netbenefits_statement_pdf import income_statement_text as _income
from tests._netbenefits_statement_pdf import income_summary as _income_summary
from tests._netbenefits_statement_pdf import year_end_text as _year_end
from tests._number_limits import MONEY, PRICE, QUANTITY, RATE
from tests._text_edits import replace_once as _replace_once

# One digit past each limit, as whole digits (the first refusing size) or as
# decimal places, each led by the marker.
_QUANTITY_13 = "7242000000000"
_PRICE_10 = "7242000000"
_MONEY_16 = "7242000000000000"
_PLACES_21 = "724200000000000000001"
# Exactly each limit's digits.
_QUANTITY_AT = "999999999999.99999999999999999999"
_PRICE_AT = "999999999.99999999999999999999"
_MONEY_AT = "999999999999999.99999999999999999999"


def _refusal(read: Callable[[str], object], text: str) -> ParseError:
    with pytest.raises(ParseError) as raised:
        read(text)
    return raised.value


def _assert_read_refusal(
    error: ParseError, *, section: str, row_index: int, column: str, message: str, marker: str
) -> None:
    """``error`` is the reader's fixed digit-limit refusal at this cell, unchained,
    printing no ``marker``."""
    assert_unchained_refusal(
        error,
        ParseError(
            section=section, row_index=row_index, column=column, message=message, raw_value=""
        ),
        f"ParseError in section {section!r} at row {row_index}, column {column!r}: {message}",
        markers=(marker,),
    )


# --- NetBenefits lots CSV -------------------------------------------------------

# The open export's Dec-31 lot (row 2): Quantity, Cost basis, Cost basis/share,
# Value and Gain/loss.
_OPEN_CELLS = ("3.4820", "668.96", "192.12", "626.24", "-42.72")
# The closed export's lot (row 1): Quantity, then (after the sale date) Proceeds,
# Cost basis and Gain/loss.
_CLOSED_QUANTITY = "2.0000"
_CLOSED_MONEY = ("900.00", "755.31", "144.69")


def _open_lots(*cells: str) -> str:
    return _replace_once(OPEN_CSV, ",".join(_OPEN_CELLS), ",".join(cells))


def _closed_lots(quantity: str, *money: str) -> str:
    text = _replace_once(CLOSED_WITH_ROW_CSV, f",{_CLOSED_QUANTITY},", f",{quantity},")
    return _replace_once(text, ",".join(_CLOSED_MONEY), ",".join(money))


@pytest.mark.parametrize(
    ("index", "cell", "column", "message"),
    [
        pytest.param(0, _QUANTITY_13, "Quantity", QUANTITY, id="quantity-whole"),
        pytest.param(0, f"3.{_PLACES_21}", "Quantity", QUANTITY, id="quantity-places"),
        pytest.param(2, _PRICE_10, "Cost basis/share", PRICE, id="price-whole"),
        pytest.param(2, f"192.{_PLACES_21}", "Cost basis/share", PRICE, id="price-places"),
        pytest.param(3, _MONEY_16, "Value", MONEY, id="money-whole"),
        pytest.param(4, f"-42.{_PLACES_21}", "Gain/loss", MONEY, id="money-places"),
    ],
)
def test_open_lots_number_past_its_limit_refuses_when_read(
    index: int, cell: str, column: str, message: str
) -> None:
    cells = list(_OPEN_CELLS)
    cells[index] = cell
    _assert_read_refusal(
        _refusal(parse_open_lots, _open_lots(*cells)),
        section="Fidelity open lots",
        row_index=2,
        column=column,
        message=message,
        marker=cell,
    )


@pytest.mark.parametrize(
    ("quantity", "proceeds", "column", "message", "marker"),
    [
        pytest.param(
            _QUANTITY_13, "900.00", "Quantity", QUANTITY, _QUANTITY_13, id="quantity-whole"
        ),
        pytest.param(_CLOSED_QUANTITY, _MONEY_16, "Proceeds", MONEY, _MONEY_16, id="money-whole"),
    ],
)
def test_closed_lots_number_past_its_limit_refuses_when_read(
    quantity: str, proceeds: str, column: str, message: str, marker: str
) -> None:
    text = _closed_lots(quantity, proceeds, *_CLOSED_MONEY[1:])
    _assert_read_refusal(
        _refusal(parse_closed_lots, text),
        section="Fidelity closed lots",
        row_index=1,
        column=column,
        message=message,
        marker=marker,
    )


@pytest.mark.parametrize(
    "cells",
    [
        # Cost basis/share x Quantity is within the export's tolerance of Cost basis.
        pytest.param((_QUANTITY_AT, "1000000000.00", "0.001", "1000000000.00", "0.00"), id="qty"),
        pytest.param(("1", "999999999.99", _PRICE_AT, "999999999.99", "0.00"), id="price"),
        pytest.param(
            ("1", "1.00", "1.00", _MONEY_AT, "999999999999998.99999999999999999999"), id="money"
        ),
    ],
)
def test_open_lots_number_at_its_limit_parses(cells: tuple[str, ...]) -> None:
    quantity, cost, per_share, *_ = cells
    assert parse_open_lots(_open_lots(*cells))[1] == EspLot(
        acquire_date=parse_open_lots(OPEN_CSV)[1].acquire_date,
        quantity=Decimal(quantity),
        contribution_usd=Decimal(cost),
        cost_per_share_paid=Decimal(per_share),
        fidelity_term="Short",
    )


def test_closed_lots_numbers_at_their_limits_parse() -> None:
    gain = "999999999999998.99999999999999999999"
    [lot] = parse_closed_lots(_closed_lots(_QUANTITY_AT, _MONEY_AT, "1.00", gain))
    assert lot == replace(
        parse_closed_lots(CLOSED_WITH_ROW_CSV)[0],
        quantity=Decimal(_QUANTITY_AT),
        proceeds_usd=Decimal(_MONEY_AT),
        contribution_usd=Decimal("1.00"),
    )


# --- NetBenefits statement PDF text ------------------------------------------------

# The monthly statement's one holding: quantity (3 places), price (4), ending
# market value, cost basis and gain/loss (2 each).
_HOLDING_CELLS = ("3.482", "$187.6400", "$653.36", "$668.96", "-$15.60")


def _monthly(*cells: str) -> str:
    """The monthly statement with its holding's cells set to ``cells``, and its
    Total Stocks and Total Holdings lines and account values to match."""
    row = _XESP_ROW
    for old, new in zip(_HOLDING_CELLS, cells, strict=True):
        row = _replace_once(row, old, new)
    text = _replace_once(MONTHLY, _XESP_ROW, row)
    for old, new in zip(_HOLDING_CELLS[2:], cells[2:], strict=True):
        text = text.replace(old, new)
    return text


@pytest.mark.parametrize(
    ("old", "new", "field", "message", "marker"),
    [
        pytest.param(
            "3.482", f"{_QUANTITY_13}.000", "quantity", QUANTITY, _QUANTITY_13, id="quantity"
        ),
        pytest.param("$187.6400", f"${_PRICE_10}.0000", "price", PRICE, _PRICE_10, id="price"),
        pytest.param(
            "$653.36", f"${_MONEY_16}.00", "ending market value", MONEY, _MONEY_16, id="money"
        ),
        pytest.param(
            "$668.96",
            "$7,242,000,000,000,000.00",
            "cost basis",
            MONEY,
            "7,242,000",
            id="money-grouped",
        ),
    ],
)
def test_statement_holding_number_past_its_limit_refuses_when_read(
    old: str, new: str, field: str, message: str, marker: str
) -> None:
    """The holding row still matches the statement grammar (a quantity prints
    3 places, a price 4, money 2), so the row's own reader refuses the cell."""
    text = _replace_once(MONTHLY, _XESP_ROW, _replace_once(_XESP_ROW, old, new))
    _assert_read_refusal(
        _refusal(parse_statement, text),
        section="Fidelity SPS statement",
        row_index=0,
        column=f"Holdings: {field}",
        message=message,
        marker=marker,
    )


@pytest.mark.parametrize(
    "cells",
    [
        # Each ending market value is within the row's tolerance of quantity x price.
        pytest.param(
            ("999999999999.999", "$0.0010", "$1000000000.00", "$668.96", "$999999331.04"),
            id="quantity",
        ),
        pytest.param(
            ("1.000", "$999999999.9999", "$999999999.99", "$668.96", "$999999331.03"), id="price"
        ),
        pytest.param(
            (
                "1000000.000",
                "$999999999.9999",
                "$999999999999999.99",
                "$999999999999999.00",
                "$0.99",
            ),
            id="money",
        ),
    ],
)
def test_statement_holding_number_at_its_limit_parses(cells: tuple[str, ...]) -> None:
    quantity, price, ending, cost, gain = (cell.replace("$", "") for cell in cells)
    data = parse_statement(_monthly(*cells))
    assert data.holdings == (
        replace(
            _HOLDING_XESP,
            quantity=Decimal(quantity),
            price_per_unit_usd=Decimal(price),
            ending_market_value_usd=Decimal(ending),
            total_cost_basis_usd=Decimal(cost),
            unrealized_gain_loss_usd=Decimal(gain),
        ),
    )
    assert data.account_nav_usd == Decimal(ending)


# These recognized cells do not contribute to the returned model. They still
# refuse absurd values; the original shape and the in-limit control both parse.
_UNUSED_MONEY = (f"${_MONEY_16}.00", "$999999999999999.99", MONEY)
_UNUSED_RATE = ("7242%", "999%", RATE)
_UNUSED_RATE_PLACES = ("0.7242001%", "0.724200%", RATE)
_CORE_STATEMENT = _income()
_CORE_ROW = (
    "FID TREASURY ONLY MMKT FUND CL   unavailable   1.090   $1.0000   $1.09"
    "   not applicable   not applicable   $0.06\n"
)
_CORE_YIELD_LINE = "OUS (XQCP)                                      3.725%\n"
_YIELD_NOTE = "-- 7-day yield: 3.725%\n"
_CORE_WITH_YIELD_NOTE = _replace_once(
    _CORE_STATEMENT, _CORE_YIELD_LINE, _CORE_YIELD_LINE + _YIELD_NOTE
)
_COMMON_TOTAL = _UNREAD_TOTALS["common-stock"]
_OFFERING_TOTAL = _UNREAD_TOTALS["offering-periods"]
_UNUSED_FIELDS = pytest.mark.parametrize(
    ("text", "row", "old", "values", "column"),
    [
        *(
            pytest.param(
                text,
                row,
                old,
                values,
                column,
                id=f"{name}-{edge}",
            )
            for name, text, row, old, column in (
                ("stock-yield", MONTHLY, " 0.895%\n", "0.895%", "Holdings: yield"),
                (
                    "core-yield",
                    _CORE_STATEMENT,
                    _CORE_YIELD_LINE,
                    "3.725%",
                    "Core Account: yield",
                ),
                (
                    "seven-day-yield",
                    _CORE_WITH_YIELD_NOTE,
                    _YIELD_NOTE,
                    "3.725%",
                    "Holdings: yield",
                ),
            )
            for edge, values in (("whole", _UNUSED_RATE), ("places", _UNUSED_RATE_PLACES))
        ),
        pytest.param(
            MONTHLY,
            _XESP_ROW,
            "unavailable",
            _UNUSED_MONEY,
            "Holdings: beginning market value",
            id="stock-beginning",
        ),
        pytest.param(
            _CORE_STATEMENT,
            _CORE_ROW,
            "unavailable",
            _UNUSED_MONEY,
            "Core Account: beginning market value",
            id="core-beginning",
        ),
        pytest.param(
            MONTHLY,
            _TOTAL_STOCKS_LINE,
            "unavailable",
            _UNUSED_MONEY,
            "Holdings: Total Stocks",
            id="stocks-total-beginning",
        ),
        pytest.param(
            MONTHLY,
            _TOTAL_STOCKS_LINE,
            "100%",
            _UNUSED_RATE,
            "Holdings: Total Stocks",
            id="stocks-total-rate-whole",
        ),
        pytest.param(
            MONTHLY,
            _TOTAL_STOCKS_LINE,
            "100%",
            _UNUSED_RATE_PLACES,
            "Holdings: Total Stocks",
            id="stocks-total-rate-places",
        ),
        pytest.param(
            _CORE_STATEMENT,
            _TOTAL_CORE_LINE,
            "unavailable",
            _UNUSED_MONEY,
            "Core Account: Total Core Account",
            id="core-total-beginning",
        ),
        pytest.param(
            _CORE_STATEMENT,
            _TOTAL_CORE_LINE,
            "0%",
            _UNUSED_RATE,
            "Core Account: Total Core Account",
            id="core-total-rate-whole",
        ),
        pytest.param(
            _CORE_STATEMENT,
            _TOTAL_CORE_LINE,
            "0%",
            _UNUSED_RATE_PLACES,
            "Core Account: Total Core Account",
            id="core-total-rate-places",
        ),
        *(
            pytest.param(
                MONTHLY,
                _TOTAL_HOLDINGS_LINE,
                old,
                _UNUSED_MONEY,
                "Holdings: Total Holdings",
                id=f"holdings-total-{name}",
            )
            for name, old in (("cost", "$668.96"), ("gain", "-$15.60"), ("eai", "$5.85"))
        ),
        pytest.param(
            _year_end(_YEAR_END_WHT_LINE + _income_summary()),
            _YEAR_END_WHT_LINE,
            "-0.37",
            (f"-{_MONEY_16}.00", "-999999999999999.99", MONEY),
            "account summary: Taxes Withheld",
            id="year-end-withholding",
        ),
        *(
            pytest.param(
                MONTHLY + _COMMON_TOTAL,
                _COMMON_TOTAL,
                old,
                _UNUSED_MONEY,
                "Total",
                id=f"unread-common-stock-{name}",
            )
            for name, old in (
                ("beginning", "$600.00"),
                ("ending", "$653.36"),
                ("cost", "$668.96"),
                ("gain", "-$15.60"),
                ("eai", "$5.85"),
            )
        ),
        pytest.param(
            MONTHLY + _COMMON_TOTAL,
            _COMMON_TOTAL,
            "100%",
            _UNUSED_RATE,
            "Total",
            id="unread-common-stock-rate-whole",
        ),
        pytest.param(
            MONTHLY + _COMMON_TOTAL,
            _COMMON_TOTAL,
            "100%",
            _UNUSED_RATE_PLACES,
            "Total",
            id="unread-common-stock-rate-places",
        ),
        *(
            pytest.param(
                MONTHLY + _UNREAD_TOTALS[name],
                _UNREAD_TOTALS[name],
                old,
                _UNUSED_MONEY,
                "Total",
                id=f"unread-{name}-{old}",
            )
            for name, old in (
                ("including-other-holdings-one", "$653.36"),
                ("including-other-holdings-two", "$600.00"),
                ("including-other-holdings-two", "$653.36"),
                ("including-other-holdings-dash", "$653.36"),
                ("including-other-holdings-allocation", "$600.00"),
            )
        ),
        *(
            pytest.param(
                MONTHLY + _UNREAD_TOTALS[name],
                _UNREAD_TOTALS[name],
                old,
                (f"${_MONEY_16}", "$999999999999999", MONEY),
                "Total",
                id=f"unread-{name}-whole-dollar-money",
            )
            for name, old in (
                ("including-other-holdings-allocation", "$1,653"),
                ("allocation", "$653"),
                ("allocation-page-marker", "$1,653"),
            )
        ),
        # The two offering amounts are identical; include their fixed footnotes
        # to target them separately.
        pytest.param(
            MONTHLY + _OFFERING_TOTAL,
            _OFFERING_TOTAL,
            "$512.34 1",
            (f"${_MONEY_16}.00 1", "$999999999999999.99 1", MONEY),
            "Total",
            id="offering-first-money",
        ),
        pytest.param(
            MONTHLY + _OFFERING_TOTAL,
            _OFFERING_TOTAL,
            "$512.34 2",
            (f"${_MONEY_16}.00 2", "$999999999999999.99 2", MONEY),
            "Total",
            id="offering-second-money",
        ),
        pytest.param(
            MONTHLY + _OFFERING_TOTAL,
            _OFFERING_TOTAL,
            "$192.1200",
            (f"${_PRICE_10}.1200", "$999999999.9999", PRICE),
            "Total",
            id="offering-price",
        ),
        pytest.param(
            MONTHLY + _UNREAD_TOTALS["offering-periods-quantity"],
            _UNREAD_TOTALS["offering-periods-quantity"],
            "2.667",
            (f"{_QUANTITY_13}.667", "999999999999.999", QUANTITY),
            "Total",
            id="offering-quantity",
        ),
        pytest.param(
            MONTHLY + _UNREAD_TOTALS["offering-periods-quantity"],
            _UNREAD_TOTALS["offering-periods-quantity"],
            "$512.34",
            _UNUSED_MONEY,
            "Total",
            id="offering-quantity-money",
        ),
    ],
)


def _unused_cell_text(text: str, row: str, old: str, new: str) -> str:
    return _replace_once(text, row, _replace_once(row, old, new))


@_UNUSED_FIELDS
def test_recognized_unused_statement_cell_still_refuses_its_first_absurd_size(
    text: str, row: str, old: str, values: tuple[str, str, str], column: str
) -> None:
    breaching, _edge, message = values
    edited = _unused_cell_text(text, row, old, breaching)
    assert "7242" in breaching
    assert "7242" in edited
    _assert_read_refusal(
        _refusal(parse_statement, edited),
        section="Fidelity SPS statement",
        row_index=0,
        column=column,
        message=message,
        marker="7242",
    )


@_UNUSED_FIELDS
def test_recognized_unused_statement_cell_at_its_limit_keeps_the_same_model(
    text: str, row: str, old: str, values: tuple[str, str, str], column: str
) -> None:
    _breaching, edge, _message = values
    assert parse_statement(_unused_cell_text(text, row, old, edge)) == parse_statement(text)


# --- NetBenefits confirmation PDF text ----------------------------------------------

_PURCHASED = "YOU PURCHASED 3.482 AT $192.1200 PURCHASE"
_SHARE_PROCEEDS = "Share Proceeds**   3.482"
_FMV = "Market Value at Purchase¹   $743.30"
_GAIN = "Gain²   $74.34"


def _confirmation(shares: str, price: str, fmv: str, contribution: str, gain: str) -> str:
    text = CONF_DEC
    for old, new in (
        (_PURCHASED, f"YOU PURCHASED {shares} AT ${price} PURCHASE"),
        (_SHARE_PROCEEDS, f"Share Proceeds**   {shares}"),
        (_FMV, f"Market Value at Purchase¹   ${fmv}"),
        (_GAIN, f"Gain²   ${gain}"),
    ):
        text = _replace_once(text, old, new)
    assert text.count("$668.96") == 2
    return text.replace("$668.96", f"${contribution}")


@pytest.mark.parametrize(
    ("old", "new", "field", "message", "marker"),
    [
        pytest.param(
            _PURCHASED,
            f"YOU PURCHASED {_QUANTITY_13} AT $192.1200 PURCHASE",
            "shares",
            QUANTITY,
            _QUANTITY_13,
            id="shares-whole",
        ),
        pytest.param(
            _PURCHASED,
            f"YOU PURCHASED 3.{_PLACES_21} AT $192.1200 PURCHASE",
            "shares",
            QUANTITY,
            _PLACES_21,
            id="shares-places",
        ),
        pytest.param(
            _PURCHASED,
            f"YOU PURCHASED 3.482 AT ${_PRICE_10} PURCHASE",
            "purchase price",
            PRICE,
            _PRICE_10,
            id="price-whole",
        ),
        pytest.param(
            _PURCHASED,
            f"YOU PURCHASED 3.482 AT $192.{_PLACES_21} PURCHASE",
            "purchase price",
            PRICE,
            _PLACES_21,
            id="price-places",
        ),
        pytest.param(
            _FMV,
            f"Market Value at Purchase¹   ${_MONEY_16}.00",
            "Market Value at Purchase",
            MONEY,
            _MONEY_16,
            id="money-whole",
        ),
        pytest.param(
            _SHARE_PROCEEDS,
            f"Share Proceeds**   {_QUANTITY_13}",
            "Share Proceeds",
            QUANTITY,
            _QUANTITY_13,
            id="share-proceeds-whole",
        ),
    ],
)
def test_confirmation_number_past_its_limit_refuses_when_read(
    old: str, new: str, field: str, message: str, marker: str
) -> None:
    _assert_read_refusal(
        _refusal(parse_confirmation, _replace_once(CONF_DEC, old, new)),
        section="Fidelity ESPP confirmation",
        row_index=0,
        column=field,
        message=message,
        marker=marker,
    )


@pytest.mark.parametrize(
    "cells",
    [
        # Shares x price is within the confirmation's tolerance of the
        # contributions, which plus the Gain is the Market Value at Purchase.
        pytest.param(
            (_QUANTITY_AT, "0.001", "1000000074.34", "1000000000.00", "74.34"), id="shares"
        ),
        pytest.param(("1", _PRICE_AT, "1000000074.33", "999999999.99", "74.34"), id="price"),
        pytest.param(
            (
                "1000000",
                "999999999.999999",
                "999999999999999.99",
                "999999999999999.00",
                "0.99",
            ),
            id="money",
        ),
    ],
)
def test_confirmation_number_at_its_limit_parses(cells: tuple[str, ...]) -> None:
    shares, price, fmv, contribution, gain = cells
    assert parse_confirmation(_confirmation(*cells)) == replace(
        parse_confirmation(CONF_DEC),
        shares=Decimal(shares),
        purchase_price_per_share_usd=Decimal(price),
        fmv_total_usd=Decimal(fmv),
        contribution_usd=Decimal(contribution),
        perquisite_usd=Decimal(gain),
    )


# --- netbenefits-fa ---------------------------------------------------------------


def _edit_lots(bundle: Path, _config: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    lots = bundle / "open-lots.csv"
    text = lots.read_text(encoding="utf-8")
    lots.write_text(_replace_once(text, "626.24,-42.72", f"{_MONEY_16},-42.72"), encoding="utf-8")


def _edit_statement(_bundle: Path, _config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = NB_E2E_PDF_TEXT["statement-jan.pdf"]
    edited = _replace_once(
        text, "You Bought 3.482 $192.12000", f"You Bought 3.482 ${_PRICE_10}.00000"
    )
    monkeypatch.setitem(NB_E2E_PDF_TEXT, "statement-jan.pdf", edited)


def _edit_config(_bundle: Path, config: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    text = config.read_text(encoding="utf-8")
    edited = _replace_once(
        text, 'price_per_share_usd = "213.47"', f'price_per_share_usd = "213.{_PLACES_21}"'
    )
    config.write_text(edited, encoding="utf-8")


@pytest.mark.parametrize(
    ("edit", "error", "marker"),
    [
        pytest.param(
            _edit_lots,
            f"ParseError in section 'open lots open-lots.csv' at row 2, column 'Value': {MONEY}",
            _MONEY_16,
            id="lots-csv",
        ),
        pytest.param(
            _edit_statement,
            "ParseError in section 'statement statement-jan.pdf' at row 0, column "
            f"'Securities Bought: price': {PRICE}",
            _PRICE_10,
            id="statement",
        ),
        pytest.param(
            _edit_config,
            f"ConfigError at field 'netbenefits.price_overrides[0].price_per_share_usd': {PRICE}",
            _PLACES_21,
            id="user-toml",
        ),
    ],
)
def test_netbenefits_fa_number_past_its_limit_refuses_and_removes_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[Path, Path, pytest.MonkeyPatch], None],
    error: str,
    marker: str,
) -> None:
    """A cell past its limit in the bundle (or a price override past it in
    user.toml) refuses when it's read: exit 4, the fixed refusal alone on stderr,
    nothing on stdout, no marker printed, and an earlier run's output is gone."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    edit(bundle, config, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-fa-a3.csv").write_text("stale\n", encoding="utf-8")

    rc = main(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config)),
            *("--output", str(out / "schedule-fa-a3.csv"), "--calendar-year", "2025"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {error}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, marker)
    assert list(out.iterdir()) == []


# --- --reconcile-1042s-fy-withholding-usd ---------------------------------------------

_FLAG = "--reconcile-1042s-fy-withholding-usd"
_CENTS = "must have at most 2 decimal places (whole cents)"
_COMMANDS = pytest.mark.parametrize("command", ["netbenefits-income", "netbenefits-all"])


def _argv(command: str, value: str) -> list[str]:
    return [
        command,
        *("--input", "in", "--config", "user.toml", "--output-dir", "out"),
        *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
        *(_FLAG, value),
    ]


def _usage(command: str) -> str:
    [commands] = [
        action.choices
        for action in cli._build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    return commands[command].format_usage()


def _assert_usage_error(
    capsys: pytest.CaptureFixture[str], command: str, value: str, rule: str
) -> None:
    """``value`` is a usage error naming the flag and ``rule``, not the value."""
    markers = (value, *re.findall(r"\d{4,}", value))
    with pytest.raises(argparse.ArgumentTypeError) as parsed:
        cli._parse_usd_2dp(value)
    assert type(parsed.value) is argparse.ArgumentTypeError
    assert str(parsed.value) == rule
    assert parsed.value.args == (rule,)
    assert_replaced_unchained(parsed.value, markers=markers)
    with pytest.raises(SystemExit) as raised:
        main(_argv(command, value))

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{_usage(command)}ibkr-fa {command}: error: argument {_FLAG}: {rule}\n"
    )
    assert_no_markers(captured.err, *markers)


@_COMMANDS
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_MONEY_16, id="16-whole-digits"),
        pytest.param(f"{_MONEY_16}.25", id="16-whole-digits-and-cents"),
        pytest.param("7242E+12", id="exponent"),
        # Was compared with the statement total as a mismatch before #1275.
        pytest.param("1E100", id="1E100"),
    ],
)
def test_reconcile_flag_past_the_money_limit_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], command: str, value: str
) -> None:
    _assert_usage_error(capsys, command, value, MONEY)


@_COMMANDS
@pytest.mark.parametrize("value", ["1.234", f"{_MONEY_16}.001"])
def test_reconcile_flag_past_whole_cents_keeps_its_refusal(
    capsys: pytest.CaptureFixture[str], command: str, value: str
) -> None:
    """The whole-cents check runs first, so a value past both limits keeps it."""
    _assert_usage_error(capsys, command, value, _CENTS)


@_COMMANDS
@pytest.mark.parametrize("value", ["724200000000000", "724200000000000.99", "7242E+11"])
def test_reconcile_flag_with_15_whole_digits_parses(command: str, value: str) -> None:
    args = cli._build_parser().parse_args(_argv(command, value))
    assert args.reconcile_1042s_usd.as_tuple() == Decimal(value).as_tuple()
