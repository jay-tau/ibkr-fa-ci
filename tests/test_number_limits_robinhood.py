"""Read-time digit limits on the numbers the Robinhood readers take (#1275).

The activity CSV, the brokerage statement and the Form 1042-S packet each
refuse a number past its kind's limits when they read it, with the kind's one
fixed message (:mod:`tests._number_limits`) and no value, cause or context.
A number exactly at its limits still reads with every digit.

Each refusing input carries ``8353`` or the grouped text ``8,353`` inside
the breaching number. Only marker forms present in that input are checked.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Final

import pytest

import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.robinhood import parse_robinhood_activity, read_robinhood_activity
from ibkr_fa.parser.robinhood_1042s import parse_robinhood_1042s_packet
from ibkr_fa.parser.robinhood_statement import parse_robinhood_statement
from tests import _number_limits as limits
from tests._crash_reports import assert_no_markers, assert_unchained_refusal
from tests._robinhood_1042s import form_line as _line
from tests._robinhood_1042s import robinhood_1042s_text as _packet
from tests._robinhood_bundle_stubs import bundle_config_text as _config_text
from tests._robinhood_bundle_stubs import patch_bundle_sources as _patch_sources
from tests._robinhood_bundle_stubs import write_bundle as _write_bundle
from tests._robinhood_cli import OWNED_FILENAMES as _OWNED
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_refusals import MALFORMED_TOTAL_REFUSAL
from tests._robinhood_statement_pages import STATEMENT_ACTIVITY_TOTAL
from tests._robinhood_statement_pages import brokerage_statement as _statement
from tests._robinhood_statement_pages import position_header as _position_header
from tests._robinhood_statement_pages import position_page as _position_page
from tests._robinhood_statement_pages import statement_activity_header as _activity_header
from tests._robinhood_statement_pages import statement_activity_page as _activity_page
from tests._robinhood_statement_pages import statement_line as _fixed
from tests._robinhood_statement_pages import stock_loaned_page as _stock_loaned_page
from tests._robinhood_statements import ADR_FEE_ROW as _ADR_FEE
from tests._robinhood_statements import AMOUNT_COLUMN as _AMOUNT
from tests._robinhood_statements import BUY_ROW as _BUY
from tests._robinhood_statements import DESCRIPTION_COLUMN as _DESCRIPTION
from tests._robinhood_statements import DIVIDEND_ROW as _DIVIDEND
from tests._robinhood_statements import FOREIGN_TAX_ROW as _FOREIGN_TAX
from tests._robinhood_statements import NRA_ROW as _NRA
from tests._robinhood_statements import OPTION_CLOSE_ROW as _OPTION_CLOSE
from tests._robinhood_statements import OPTION_OPEN_ROW as _OPTION_OPEN
from tests._robinhood_statements import PRICE_COLUMN as _PRICE
from tests._robinhood_statements import QUANTITY_COLUMN as _QUANTITY
from tests._robinhood_statements import SELL_ROW as _SELL
from tests._robinhood_statements import TRANSFER_ROW as _TRANSFER
from tests._robinhood_statements import adr_fee_description as _adr_fee
from tests._robinhood_statements import distribution_description as _distribution
from tests._robinhood_statements import loaned_securities_page, widened_table
from tests._robinhood_statements import one_row_activity_csv as _one_row

_MARKERS: Final = ("8353", "8,353")
_ZEROS: Final = "0"


def _refused(
    error: ParseError, section: str, row: int, column: str, message: str, *, input_text: str
) -> None:
    markers = tuple(marker for marker in _MARKERS if marker in input_text)
    assert markers
    assert_unchained_refusal(
        error,
        ParseError(section=section, row_index=row, column=column, message=message, raw_value=""),
        f"ParseError in section {section!r} at row {row}, column {column!r}: {message}",
        markers=markers,
    )


# --- The activity CSV -----------------------------------------------------------

_ACTIVITY: Final = "Robinhood account activity"


def _option(strike: str) -> str:
    return f"OPT 1/17/2025 Call {strike}"


@pytest.mark.parametrize(
    ("index", "cells", "name", "message"),
    [
        # Marked variants with the issue examples' digit/place sizes, then each edge.
        pytest.param(
            _BUY,
            {_QUANTITY: "8353" + _ZEROS * 25},
            "Quantity",
            limits.QUANTITY,
            id="quantity-29-whole",
        ),
        pytest.param(
            _SELL,
            {_QUANTITY: "0." + _ZEROS * 57 + "8353"},
            "Quantity",
            limits.QUANTITY,
            id="quantity-61-places",
        ),
        pytest.param(
            _BUY,
            {_QUANTITY: "1." + _ZEROS * 896 + "8353"},
            "Quantity",
            limits.QUANTITY,
            id="quantity-900-places",
        ),
        pytest.param(
            _BUY,
            {_PRICE: "$8,353,000,000,000,000.00"},
            "Price",
            limits.PRICE,
            id="price-16-whole",
        ),
        pytest.param(
            _BUY,
            {_QUANTITY: "8353" + _ZEROS * 9},
            "Quantity",
            limits.QUANTITY,
            id="quantity-13-whole",
        ),
        pytest.param(
            _TRANSFER,
            {_QUANTITY: "8353" + _ZEROS * 9 + "S"},
            "Quantity",
            limits.QUANTITY,
            id="suffixed-quantity-13-whole",
        ),
        pytest.param(
            _SELL,
            {_QUANTITY: "0." + _ZEROS * 17 + "8353"},
            "Quantity",
            limits.QUANTITY,
            id="quantity-21-places",
        ),
        pytest.param(
            _BUY, {_PRICE: "$8,353,000,000.00"}, "Price", limits.PRICE, id="price-10-whole"
        ),
        pytest.param(
            _BUY,
            {_AMOUNT: "($8,353,000,000,000,000.00)"},
            "Amount",
            limits.MONEY,
            id="parenthesised-amount-16-whole",
        ),
        pytest.param(
            _SELL,
            {_AMOUNT: "$8,353,000,000,000,000.00"},
            "Amount",
            limits.MONEY,
            id="amount-16-whole",
        ),
        pytest.param(
            _OPTION_OPEN,
            {_DESCRIPTION: _option("$8,353,000,000.00")},
            "Description",
            limits.PRICE,
            id="bto-strike-10-whole",
        ),
        pytest.param(
            _OPTION_CLOSE,
            {_DESCRIPTION: _option("$8,353,000,000.00")},
            "Description",
            limits.PRICE,
            id="stc-strike-10-whole",
        ),
        pytest.param(
            _DIVIDEND,
            {
                _DESCRIPTION: _distribution("8353" + _ZEROS * 9, "0.01"),
                _AMOUNT: "$83,530,000,000.00",
            },
            "Description",
            limits.QUANTITY,
            id="cdiv-shares-13-whole",
        ),
        pytest.param(
            _DIVIDEND,
            {_DESCRIPTION: _distribution("2", "1.25" + _ZEROS * 15 + "8353")},
            "Description",
            limits.PRICE,
            id="cdiv-rate-21-places",
        ),
        pytest.param(
            _NRA,
            {_DESCRIPTION: _distribution("2." + _ZEROS * 17 + "8353", "1.25", nra=True)},
            "Description",
            limits.QUANTITY,
            id="nrat-shares-21-places",
        ),
        pytest.param(
            _NRA,
            {_DESCRIPTION: _distribution("2", "1.25" + _ZEROS * 15 + "8353", nra=True)},
            "Description",
            limits.PRICE,
            id="nrat-rate-21-places",
        ),
        pytest.param(
            _NRA,
            {_DESCRIPTION: _distribution("2", "8353" + _ZEROS * 6, nra=True)},
            "Description",
            limits.PRICE,
            id="nrat-rate-10-whole",
        ),
        pytest.param(
            _ADR_FEE,
            {
                _DESCRIPTION: _adr_fee("8353" + _ZEROS * 9, "0.01"),
                _AMOUNT: "($83,530,000,000.00)",
            },
            "Description",
            limits.QUANTITY,
            id="afee-shares-13-whole",
        ),
        pytest.param(
            _ADR_FEE,
            {_DESCRIPTION: _adr_fee("2", "0.01" + _ZEROS * 15 + "8353")},
            "Description",
            limits.PRICE,
            id="afee-rate-21-places",
        ),
        # The Amount must equal the description's amount, and is read first, so
        # only a row whose two amounts disagree reaches the description's limit.
        # Main refused it as that disagreement.
        pytest.param(
            _FOREIGN_TAX,
            {_DESCRIPTION: "Foreign Tax Witholding at $8,353,000,000,000,000.00"},
            "Description",
            limits.MONEY,
            id="dtax-amount-16-whole",
        ),
    ],
)
def test_activity_csv_refuses_a_number_past_its_limits(
    index: int, cells: dict[int, str], name: str, message: str
) -> None:
    """Each number of a one-row activity CSV refuses past its limits, at its
    row and column, with its kind's fixed text. Main read each one with every
    digit."""
    text = _one_row(index, cells)
    with pytest.raises(ParseError) as raised:
        parse_robinhood_activity(text)

    _refused(raised.value, _ACTIVITY, 1, name, message, input_text=text)


@pytest.mark.parametrize(
    ("index", "cells", "expected"),
    [
        pytest.param(
            _BUY,
            {_QUANTITY: "835300000000.00000000000000008353", _PRICE: "$835,300,000.00"},
            {"quantity": "835300000000.00000000000000008353", "price_usd": "835300000.00"},
            id="buy-quantity-and-price",
        ),
        pytest.param(
            _BUY,
            {_AMOUNT: "($835,300,000,000,000.00)"},
            {"amount_usd": "-835300000000000.00"},
            id="parenthesised-amount",
        ),
        pytest.param(
            _TRANSFER,
            {_QUANTITY: "835300000000S"},
            {"quantity": "835300000000"},
            id="suffixed-quantity",
        ),
        pytest.param(
            _OPTION_CLOSE,
            {_DESCRIPTION: _option("$835,300,000.00")},
            {"option_strike_usd": "835300000.00"},
            id="strike",
        ),
        pytest.param(
            _DIVIDEND,
            {
                _DESCRIPTION: _distribution("835300000000", "0.01000000000000000000"),
                _AMOUNT: "$8,353,000,000.00",
            },
            {
                "description_shares": "835300000000",
                "description_rate_usd": "0.01000000000000000000",
            },
            id="cdiv",
        ),
        pytest.param(
            _NRA,
            {
                _DESCRIPTION: _distribution(
                    "835300000000.00000000000000008353",
                    "835300000.00000000000000008353",
                    nra=True,
                )
            },
            {
                "description_shares": "835300000000.00000000000000008353",
                "description_rate_usd": "835300000.00000000000000008353",
            },
            id="nrat",
        ),
        pytest.param(
            _ADR_FEE,
            {
                _DESCRIPTION: _adr_fee("835300000000", "0.01000000000000000000"),
                _AMOUNT: "($8,353,000,000.00)",
            },
            {
                "description_shares": "835300000000",
                "description_rate_usd": "0.01000000000000000000",
            },
            id="afee",
        ),
        pytest.param(
            _FOREIGN_TAX,
            {
                _DESCRIPTION: "Foreign Tax Witholding at $835,300,000,000,000.00",
                _AMOUNT: "($835,300,000,000,000.00)",
            },
            {"description_amount_usd": "835300000000000.00"},
            id="dtax",
        ),
    ],
)
def test_activity_csv_reads_a_number_at_its_limits(
    index: int, cells: dict[int, str], expected: dict[str, str]
) -> None:
    """A quantity of 12 whole digits and 20 places, a price, strike or rate of
    9 whole digits or 20 places, and an amount of 15 whole digits read with
    every digit."""
    (event,) = parse_robinhood_activity(_one_row(index, cells))

    for field, printed in expected.items():
        assert getattr(event, field).as_tuple() == Decimal(printed).as_tuple(), field


# --- The brokerage statement ----------------------------------------------------

_STATEMENT: Final = "Robinhood brokerage statement"
_PRINTED_STARTS: Final = _activity_header()[2]
# The Account Activity columns with Qty, Price and Debit 30 characters wide, so a
# number past its limit fits its column.
_WIDE_STARTS: Final = (0, 100, 112, 125, 139, 151, 181, 211, 241)
_PURCHASE: Final = "Purchase"
_CAPITAL_GAINS: Final = "Capital Gains Distributions $0.00 $0.00"
_PRINTED_POSITION_STARTS: Final = (0, *_position_header("Securities Held in Account")[1])
# The holdings columns with Price 30 characters wide.
_WIDE_POSITION_STARTS: Final = (0, 34, 52, 64, 75, 105, 120, 136, 147)
_HELD_EQUITY: Final = "Example Equity"


def _activity_statement(*, qty: str = "1", price: str = "$5.0000", debit: str = "$5.00") -> str:
    """:func:`_statement` with its Account Activity in the wide columns, and
    the purchase row's Qty, Price and Debit (the section total's debit too)
    replaced."""
    page = _activity_page()
    wide = widened_table(
        page,
        _PRINTED_STARTS,
        _WIDE_STARTS,
        {_PURCHASE: {5: qty, 6: price, 7: debit}, STATEMENT_ACTIVITY_TOTAL: {7: debit}},
        key=lambda cells: cells[0],
        line=lambda starts, cells: _fixed(starts, cells, width=280),
    )
    return _statement().replace(page, wide)


def _position_statement(old: str, new: str) -> str:
    page = _position_page()
    assert page.count(old) == 1
    return _statement().replace(page, page.replace(old, new))


def _wide_position_statement(*, qty: str = "1", price: str = "$100.0000") -> str:
    """:func:`_statement` with its holdings table in the wide columns, and the
    held equity row's Qty and Price replaced."""
    page = _position_page()
    wide = widened_table(
        page,
        _PRINTED_POSITION_STARTS,
        _WIDE_POSITION_STARTS,
        {_HELD_EQUITY: {3: qty, 4: price}},
        key=lambda cells: cells[0],
        line=_fixed,
    )
    return _statement().replace(page, wide)


def _loaned_statement(shares: str, value: str, *, price: str = "$5.0000") -> str:
    """:func:`_statement` with its one Stock Lending loaned row lending
    ``shares`` at ``price``, worth ``value``, the table total too. Price is 25
    characters wide (the printed table's 15 can't hold 10 whole digits)."""
    starts = (0, 55, 72, 91, 116)
    page = loaned_securities_page(
        starts, [("Loaned Example", "LOAN", shares, price, value)], value, line=_fixed
    )
    return _statement().replace(_stock_loaned_page(), page)


def _income_statement(this_period: str) -> str:
    return _statement().replace(_CAPITAL_GAINS, f"Capital Gains Distributions {this_period} $0.00")


def test_wide_activity_columns_read_like_the_printed_ones() -> None:
    """The control for the wide layout the cases below use."""
    printed = parse_robinhood_statement(_statement()).activities
    wide = parse_robinhood_statement(_activity_statement()).activities

    assert [
        (row.description, row.quantity, row.price_usd, row.debit_usd, row.credit_usd)
        for row in wide
    ] == [
        (row.description, row.quantity, row.price_usd, row.debit_usd, row.credit_usd)
        for row in printed
    ]


def test_wide_holdings_and_loaned_columns_read_like_the_printed_ones() -> None:
    """The controls for the wide holdings and Stock Lending layouts."""
    printed = parse_robinhood_statement(_statement())
    wide_held = parse_robinhood_statement(_wide_position_statement())
    wide_loaned = parse_robinhood_statement(_loaned_statement("1", "$5.00"))

    assert [
        (row.description, row.quantity, row.price_usd, row.market_value_usd)
        for row in wide_held.positions
    ] == [
        (row.description, row.quantity, row.price_usd, row.market_value_usd)
        for row in printed.positions
    ]
    assert [
        (row.description, row.shares_lent, row.price_usd, row.market_value_usd)
        for row in wide_loaned.stock_loaned_securities
    ] == [
        (row.description, row.shares_lent, row.price_usd, row.market_value_usd)
        for row in printed.stock_loaned_securities
    ]


@pytest.mark.parametrize(
    ("text", "page", "column", "message"),
    [
        pytest.param(
            _activity_statement(qty="8353" + _ZEROS * 9),
            3,
            "Qty",
            limits.QUANTITY,
            id="qty-13-whole",
        ),
        pytest.param(
            _activity_statement(debit="$5." + _ZEROS * 17 + "8353"),
            3,
            "Debit",
            limits.MONEY,
            id="debit-21-places",
        ),
        pytest.param(
            _activity_statement(qty="1." + _ZEROS * 17 + "8353"),
            3,
            "Qty",
            limits.QUANTITY,
            id="qty-21-places",
        ),
        pytest.param(
            _activity_statement(debit="$8,353,000,000,000,000.00"),
            3,
            "Debit",
            limits.MONEY,
            id="debit-16-whole",
        ),
        pytest.param(
            _activity_statement(price="$8,353,000,000.0000"),
            3,
            "Price",
            limits.PRICE,
            id="price-10-whole",
        ),
        pytest.param(
            _activity_statement(price="$5." + _ZEROS * 17 + "8353"),
            3,
            "Price",
            limits.PRICE,
            id="price-21-places",
        ),
        pytest.param(
            _position_statement("2.50%", "8353.00%"),
            2,
            "Percent",
            limits.RATE,
            id="percent-4-whole",
        ),
        pytest.param(
            _position_statement("2.50%", "2.5008353%"),
            2,
            "Percent",
            limits.RATE,
            id="percent-7-places",
        ),
        pytest.param(
            _position_statement("Call $10.00", "Call $8,353,000,000.00"),
            2,
            "Description",
            limits.PRICE,
            id="strike-10-whole",
        ),
        pytest.param(
            _loaned_statement("8353" + _ZEROS * 9, "$41,765,000,000,000.00"),
            7,
            "Shares Lent",
            limits.QUANTITY,
            id="shares-lent-13-whole",
        ),
        # 0.000000012 shares at the price reproduce the row's $100.00 to the
        # printed quantity's precision.
        pytest.param(
            _wide_position_statement(qty="0.000000012", price="$8,353,000,000.0000"),
            2,
            "Price",
            limits.PRICE,
            id="position-price-10-whole",
        ),
        pytest.param(
            _wide_position_statement(price="$100." + _ZEROS * 17 + "8353"),
            2,
            "Price",
            limits.PRICE,
            id="position-price-21-places",
        ),
        pytest.param(
            _loaned_statement("1", "$8,353,000,000.00", price="$8,353,000,000.0000"),
            7,
            "Price",
            limits.PRICE,
            id="loaned-price-10-whole",
        ),
        pytest.param(
            _loaned_statement("1", "$5.00", price="$5." + _ZEROS * 17 + "8353"),
            7,
            "Price",
            limits.PRICE,
            id="loaned-price-21-places",
        ),
        *[
            pytest.param(
                _position_statement(old, old.rsplit(" ", 1)[0] + f" {percent}%"),
                2,
                "Percent",
                limits.RATE,
                id=f"{label}-{edge}",
            )
            for label, old in (
                ("securities-total", "Total Securities $300.00 100.00%"),
                ("brokerage-total", "Brokerage Cash Balance $5.00 1.54%"),
                ("sweep-total", "Deposit Sweep Balance $20.00 6.15%"),
            )
            for edge, percent in (("4-whole", "8353.00"), ("7-places", "0.0008353"))
        ],
        # The issue's negative example has 30 places, past the money limit's 20.
        pytest.param(
            _income_statement("($1.234567890123456789012345678353)"),
            1,
            "Capital Gains Distributions",
            limits.MONEY,
            id="negative-amount-30-places",
        ),
    ],
)
def test_statement_refuses_a_number_past_its_limits(
    text: str, page: int, column: str, message: str
) -> None:
    """Each statement number refuses past its limits, at its page and column,
    with its kind's fixed text. Main read each one."""
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refused(raised.value, _STATEMENT, page, column, message, input_text=text)


@pytest.mark.parametrize(
    "old",
    (
        "Total Securities $300.00 100.00%",
        "Brokerage Cash Balance $5.00 1.54%",
        "Deposit Sweep Balance $20.00 6.15%",
    ),
)
def test_holdings_total_percent_at_its_limits_keeps_the_statement(old: str) -> None:
    text = _position_statement(old, old.rsplit(" ", 1)[0] + " 999.999999%")
    assert parse_robinhood_statement(text) == parse_robinhood_statement(_statement())


@pytest.mark.parametrize(
    ("old", "label"),
    (
        ("Total Securities $300.00 100.00%", "Total Securities"),
        ("Brokerage Cash Balance $5.00 1.54%", "Brokerage Cash Balance"),
        ("Deposit Sweep Balance $20.00 6.15%", "Deposit Sweep Balance"),
    ),
)
def test_holdings_total_money_shape_still_refuses_before_percent(old: str, label: str) -> None:
    text = _position_statement(old, f"{label} EUR8353.00 8353.00%")
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)
    _refused(raised.value, _STATEMENT, 2, label, MALFORMED_TOTAL_REFUSAL, input_text=text)


def _activity(text: str) -> tuple[Decimal, ...]:
    row = parse_robinhood_statement(text).activities[0]
    assert row.quantity is not None and row.price_usd is not None and row.debit_usd is not None
    return row.quantity, row.price_usd, row.debit_usd


@pytest.mark.parametrize(
    ("text", "read", "expected"),
    [
        pytest.param(
            _activity_statement(
                qty="835300000000", price="$835,300,000.0000", debit="$835,300,000,000,000.00"
            ),
            _activity,
            ("835300000000", "835300000.0000", "835300000000000.00"),
            id="activity-whole-digits",
        ),
        pytest.param(
            _activity_statement(
                qty="1." + _ZEROS * 16 + "8353",
                price="$5." + _ZEROS * 16 + "8353",
                debit="$5." + _ZEROS * 16 + "8353",
            ),
            _activity,
            ("1." + _ZEROS * 16 + "8353", "5." + _ZEROS * 16 + "8353", "5." + _ZEROS * 16 + "8353"),
            id="activity-places",
        ),
        pytest.param(
            _position_statement("2.50%", "835.835300%"),
            lambda text: (parse_robinhood_statement(text).positions[0].yield_percent,),
            ("835.835300",),
            id="percent",
        ),
        pytest.param(
            _position_statement("Call $10.00", "Call $835,300,000.00"),
            lambda text: (parse_robinhood_statement(text).positions[1].option_strike_usd,),
            ("835300000.00",),
            id="strike",
        ),
        pytest.param(
            _loaned_statement("835300000000", "$4,176,500,000,000.00"),
            lambda text: (parse_robinhood_statement(text).stock_loaned_securities[0].shares_lent,),
            ("835300000000",),
            id="shares-lent",
        ),
        pytest.param(
            _wide_position_statement(qty="0.000001", price="$100,000,000.0000"),
            lambda text: (parse_robinhood_statement(text).positions[0].price_usd,),
            ("100000000.0000",),
            id="position-price-whole-digits",
        ),
        pytest.param(
            _wide_position_statement(price="$100." + _ZEROS * 16 + "8353"),
            lambda text: (parse_robinhood_statement(text).positions[0].price_usd,),
            ("100." + _ZEROS * 16 + "8353",),
            id="position-price-places",
        ),
        pytest.param(
            _loaned_statement("1", "$835,300,000.00", price="$835,300,000.0000"),
            lambda text: (parse_robinhood_statement(text).stock_loaned_securities[0].price_usd,),
            ("835300000.0000",),
            id="loaned-price-whole-digits",
        ),
        pytest.param(
            _loaned_statement("1", "$5.00", price="$5." + _ZEROS * 16 + "8353"),
            lambda text: (parse_robinhood_statement(text).stock_loaned_securities[0].price_usd,),
            ("5." + _ZEROS * 16 + "8353",),
            id="loaned-price-places",
        ),
    ],
)
def test_statement_reads_a_number_at_its_limits(
    text: str, read: Callable[[str], tuple[Decimal, ...]], expected: tuple[str, ...]
) -> None:
    """A quantity of 12 whole digits or 20 places, a price of 9 whole digits
    or 20 places, an amount of 15 whole digits or 20 places, and a percent of
    3 whole digits and 6 places read with every digit."""
    assert [value.as_tuple() for value in read(text)] == [
        Decimal(printed).as_tuple() for printed in expected
    ]


def test_a_negative_statement_amount_past_28_digits_keeps_every_digit() -> None:
    """A parenthesised statement amount is negated with ``copy_negate``, so
    under the default 28-digit context an in-limit amount of 30 digits (10
    whole and 20 places) reads with every digit. Main negated it with a unary
    minus, which rounded it to 28 digits."""
    printed = "1,234,567,890.12345678901234567891"
    with localcontext(Context(prec=28)):
        statement = parse_robinhood_statement(_income_statement(f"(${printed})"))

    (row,) = (row for row in statement.income if row.label == "Capital Gains Distributions")
    assert row.this_period_usd.as_tuple() == Decimal("-" + printed.replace(",", "")).as_tuple()


# --- The Form 1042-S packet -----------------------------------------------------

_FORM_1042S: Final = "Robinhood Form 1042-S packet"
_BOX8: Final = "8 Tax withheld by other agents -0-"
_BOX10: Final = f"\n{_line((90, '25.00'))}\n"
_CHAPTER3: Final = "Chapter 3, exemption code 00 - 25.00%"
_CHAPTER4: Final = "Chapter 4, exemption code 00 - 00.00%"
# Box 3b's rate, with the blank after it so a seven-character rate keeps the
# columns that follow in place.
_BOX3B: Final = "25 .00 "


def _box8(amount: str) -> str:
    return f"8 Tax withheld by other agents {amount}"


def _box10(amount: str) -> str:
    """Box 10's value line, the value starting further left so a long one ends
    inside the form's left column."""
    return f"\n{_line((70, amount))}\n"


@pytest.mark.parametrize(
    ("replacements", "page", "column", "message"),
    [
        # Box 10 adds Box 8 up, as on any form.
        pytest.param(
            {
                _BOX8: _box8("8,353,000,000,000,000.00"),
                _BOX10: _box10("8,353,000,000,000,025.00"),
            },
            1,
            "Box 8",
            limits.MONEY,
            id="box8-16-whole",
        ),
        pytest.param(
            {_CHAPTER3: "Chapter 3, exemption code 00 - 8353.00%"},
            5,
            "Chapter 3 rate",
            limits.RATE,
            id="summary-chapter3-rate-4-whole",
        ),
        pytest.param(
            {_CHAPTER4: "Chapter 4, exemption code 00 - 8353.00%"},
            5,
            "Chapter 4 rate",
            limits.RATE,
            id="summary-chapter4-rate-4-whole",
        ),
        # A box rate has at most 3 whole digits and 2 places by its shape, so
        # its shape check refuses a fourth whole digit before the rate limit,
        # as on main.
        pytest.param(
            {_BOX3B: "8353.00"},
            1,
            "Box 3b",
            "Form 1042-S tax rate is malformed",
            id="box3b-rate-4-whole-shape",
        ),
    ],
)
def test_1042s_refuses_a_number_past_its_limits(
    replacements: dict[str, str], page: int, column: str, message: str
) -> None:
    """A 1042-S amount refuses at 16 whole digits, and a summary rate (whose
    shape has no whole-digit bound) at 4, with its kind's fixed text. Main
    read those values; its malformed box-rate control already refused."""
    text = _packet()
    for old, new in replacements.items():
        assert old in text
        text = text.replace(old, new)

    with pytest.raises(ParseError) as raised:
        parse_robinhood_1042s_packet(text)

    _refused(raised.value, _FORM_1042S, page, column, message, input_text=text)


def test_1042s_reads_a_number_at_its_limits() -> None:
    """A 15-whole-digit Box 8 (Box 10 adding it up), and the widest rates the
    box and summary shapes allow, 3 whole digits and 2 places, read with every
    digit."""
    text = (
        _packet()
        .replace(_BOX8, _box8("835,300,000,000,000.00"))
        .replace(_BOX10, _box10("835,300,000,000,025.00"))
        .replace(_BOX3B, "999.99 ")
        .replace(_CHAPTER3, "Chapter 3, exemption code 00 - 999.99%")
    )

    packet = parse_robinhood_1042s_packet(text)

    for copy in packet.copies:
        assert copy.tax_withheld_other_agents.value == Decimal("835300000000000.00")
        assert copy.total_withholding_credit.value == Decimal("835300000000025.00")
        assert copy.chapter3_tax_rate_percent.as_tuple() == Decimal("999.99").as_tuple()
    assert packet.summary.rows[0].chapter3_tax_rate_percent.as_tuple() == (
        Decimal("999.99").as_tuple()
    )


# --- Through the CLI --------------------------------------------------------------


@pytest.mark.parametrize("command", _OWNED)
def test_a_past_limit_activity_cell_stops_every_robinhood_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """The real activity reader, every later bundle step stubbed: a 13-digit
    Quantity exits 4 with the reader's located refusal alone, and the
    command's earlier outputs are gone."""
    _write_bundle(tmp_path / "bundle")
    (tmp_path / "user.toml").write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    monkeypatch.setattr(bundle_mod, "read_robinhood_activity", read_robinhood_activity)
    activity = tmp_path / "bundle" / "activity"
    (activity / "one.csv").write_text(
        _one_row(_BUY, {_QUANTITY: "8353" + _ZEROS * 9}), encoding="utf-8"
    )
    (activity / "two.csv").write_text(_one_row(_BUY, {}), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    for name in _OWNED[command]:
        (out / name).write_bytes(b"stale")

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Robinhood account activity' at row 1, "
        f"column 'Quantity': {limits.QUANTITY}\n"
    )
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, "8353")
    assert _left(tmp_path) == []
