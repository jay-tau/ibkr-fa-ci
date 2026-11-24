"""The Robinhood readers' own checks keep every digit (#1295).

The activity CSV checks a dividend's or ADR fee's Amount against the
description's shares times its per-share rate, and the brokerage statement
checks its printed totals, running balances and row market values against
the cells they cover. Each check now computes exactly, so an in-limit cell
that needs more than Decimal's default 28 digits can neither pass a value its
cells don't give nor refuse one they do. The long-arithmetic cases run at the
default context and at 16 digits, with controls off by the smallest printed
unit. Separate narrow-context and helper cases cover precision refusals and
the exact construction of row tolerances.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from collections.abc import Callable, Iterator
from decimal import ROUND_UP, Context, Decimal, DecimalTuple, localcontext
from typing import Final, NamedTuple

import pytest

import ibkr_fa.parser.robinhood_statement.parser as statement_parser
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.robinhood import parse_robinhood_activity
from ibkr_fa.parser.robinhood_statement import parse_robinhood_statement
from tests._crash_reports import assert_unchained_refusal
from tests._robinhood_statement_pages import LEDGER_LABELS, PENDING_TOTAL, STATEMENT_ACTIVITY_TOTAL
from tests._robinhood_statement_pages import LEDGERS as _LEDGERS
from tests._robinhood_statement_pages import brokerage_statement as _statement
from tests._robinhood_statement_pages import position_page as _position_page
from tests._robinhood_statement_pages import statement_activity_header as _activity_header
from tests._robinhood_statement_pages import statement_activity_page as _activity_page
from tests._robinhood_statement_pages import statement_ledger as _ledger
from tests._robinhood_statement_pages import statement_line as _fixed
from tests._robinhood_statement_pages import stock_loaned_page as _stock_loaned_page
from tests._robinhood_statements import ADR_FEE_ROW as _ADR_FEE
from tests._robinhood_statements import AMOUNT_COLUMN as _AMOUNT
from tests._robinhood_statements import DESCRIPTION_COLUMN as _DESCRIPTION
from tests._robinhood_statements import DIVIDEND_ROW as _DIVIDEND
from tests._robinhood_statements import adr_fee_description as _adr_fee
from tests._robinhood_statements import distribution_description as _distribution
from tests._robinhood_statements import loaned_securities_page, widened_table
from tests._robinhood_statements import one_row_activity_csv as _one_row

_CONTEXTS: Final = pytest.mark.parametrize(
    "context", [None, Context(prec=16)], ids=["default-context", "prec-16"]
)
_NO_MARKERS: Final[tuple[str, ...]] = ()


def _refusal(error: ParseError, section: str, row: int, column: str | None, message: str) -> None:
    location = f"row {row}" if column is None else f"row {row}, column {column!r}"
    assert_unchained_refusal(
        error,
        ParseError(section=section, row_index=row, column=column, message=message, raw_value=""),
        f"ParseError in section {section!r} at {location}: {message}",
        markers=_NO_MARKERS,
    )


# --- The activity CSV's dividend and ADR-fee check -------------------------------

_ACTIVITY: Final = "Robinhood account activity"
_MISMATCH: Final = "description arithmetic does not match the amount"
_PRECISION: Final = "description arithmetic exceeds the supported precision"
# 0.000000003 shares at this rate is exactly 1.23499999999999999999999999998,
# so $1.23; rounded to 28 digits first it is 1.235, so $1.24.
_SHARES: Final = "0.000000003"
_RATE: Final = "411666666.66666666666666666666"
_DISTRIBUTIONS: Final = pytest.mark.parametrize(
    ("index", "description", "amount"),
    [
        pytest.param(_DIVIDEND, _distribution, "${}", id="cdiv"),
        pytest.param(_ADR_FEE, _adr_fee, "(${})", id="afee"),
    ],
)


@_DISTRIBUTIONS
@_CONTEXTS
def test_the_amount_is_the_exact_product_rounded_to_cents(
    index: int, description: Callable[[str, str], str], amount: str, context: Context | None
) -> None:
    """Main refused it."""
    text = _one_row(
        index, {_DESCRIPTION: description(_SHARES, _RATE), _AMOUNT: amount.format("1.23")}
    )
    with localcontext(context):
        (event,) = parse_robinhood_activity(text)

    assert (event.description_shares, event.description_rate_usd) == (
        Decimal(_SHARES),
        Decimal(_RATE),
    )
    assert event.amount_usd is not None
    assert event.amount_usd.copy_abs() == Decimal("1.23")


@_DISTRIBUTIONS
@pytest.mark.parametrize("cents", ["1.24", "1.22"], ids=["rounded-product", "cent-below"])
@_CONTEXTS
def test_an_amount_a_cent_off_the_exact_product_refuses(
    index: int,
    description: Callable[[str, str], str],
    amount: str,
    cents: str,
    context: Context | None,
) -> None:
    """Main accepted $1.24, the rounded product's cents."""
    text = _one_row(
        index, {_DESCRIPTION: description(_SHARES, _RATE), _AMOUNT: amount.format(cents)}
    )
    with localcontext(context), pytest.raises(ParseError) as raised:
        parse_robinhood_activity(text)

    _refusal(raised.value, _ACTIVITY, 1, "Amount", _MISMATCH)


@_DISTRIBUTIONS
def test_a_product_past_the_contexts_exponent_limit_refuses(
    index: int, description: Callable[[str, str], str], amount: str
) -> None:
    """Unreachable at the default context: in-limit cells give a product
    below 10^21. Under a context whose exponent limit is 10, 10^5 shares at
    10^6 overflow; main let the raw ``Overflow`` escape."""
    text = _one_row(
        index, {_DESCRIPTION: description("100000", "1000000"), _AMOUNT: amount.format("1.00")}
    )
    with localcontext(Context(Emax=10)), pytest.raises(ParseError) as raised:
        parse_robinhood_activity(text)

    _refusal(raised.value, _ACTIVITY, 1, "Description", _PRECISION)


@_DISTRIBUTIONS
def test_cents_rounding_past_the_contexts_precision_refuses_unchained(
    index: int, description: Callable[[str, str], str], amount: str
) -> None:
    text = _one_row(index, {_DESCRIPTION: description("1", "1.23"), _AMOUNT: amount.format("1.23")})
    with localcontext(Context(prec=2)), pytest.raises(ParseError) as raised:
        parse_robinhood_activity(text)

    _refusal(raised.value, _ACTIVITY, 1, "Description", _PRECISION)


# --- The brokerage statement's checks ---------------------------------------------

_STATEMENT: Final = "Robinhood brokerage statement"
_WIDTH: Final = 320


def _exactly(*terms: str | int | Decimal) -> Decimal:
    """The sum of ``terms``, at a precision that keeps every digit."""
    with localcontext(Context(prec=100)):
        return sum((Decimal(term) for term in terms), Decimal())


def _usd(value: Decimal) -> str:
    return f"${value:,f}"


# A 30-digit amount, and the smallest printed unit of every long amount here.
_LONG: Final = Decimal("1000000000.00000000000000000001")
_UNIT: Final = Decimal("1E-20")


def _row(starts: tuple[int, ...], cells: tuple[str, ...]) -> str:
    """:func:`_fixed`, with each cell inside its column."""
    widths = [end - start for start, end in itertools.pairwise(starts)]
    assert all(len(cell) <= width for cell, width in zip(cells, widths, strict=False)), cells
    return _fixed(starts, cells, width=_WIDTH)


def _widen(
    page: str,
    starts: tuple[int, ...],
    wide: tuple[int, ...],
    edits: dict[str, dict[int, str]],
) -> str:
    """``page`` with its table columns moved from ``starts`` to ``wide``, and
    in each row whose first nonblank cell is a key of ``edits``, those cells
    replaced."""
    return widened_table(
        page,
        starts,
        wide,
        edits,
        key=lambda cells: next((cell for cell in cells if cell), ""),
        line=_row,
    )


def _statement_with(*replacements: tuple[str, str]) -> str:
    """:func:`_statement` with each ``(old, new)`` replaced once."""
    text = _statement()
    for old, new in replacements:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


_HOLDINGS_STARTS: Final = (0, 34, 52, 64, 75, 88, 103, 119, 130)
# Qty, Price, Mkt Value and Est. Dividend 45 characters wide.
_WIDE_HOLDINGS: Final = (0, 34, 52, 64, 109, 154, 199, 244, 255)
# Qty and Price 30 characters wide, Debit 40, Credit after them.
_WIDE_ACTIVITY: Final = (0, 100, 112, 125, 139, 151, 181, 211, 251)
# Qty and Price 30 characters wide, Debit 40, Credit after them.
_WIDE_PENDING: Final = (0, 42, 55, 69, 82, 95, 125, 155, 195)
_WIDE_LEDGER: Final = (0, 47, 63, 103, 143)
# Shares Lent and Price 40 characters wide.
_WIDE_LOANED: Final = (0, 55, 72, 112, 152)
_BANK_ROW: Final = "Example Program Bank" + " " * 41
_DEBIT, _CREDIT = 7, 8


def _holdings(**cells: str) -> tuple[str, str]:
    """The replacement for the holdings page, with the held equity row's
    ``quantity``, ``price``, ``value`` or ``dividend`` cells replaced."""
    columns = {"quantity": 3, "price": 4, "value": 5, "dividend": 6}
    edits = {"Example Equity": {columns[name]: value for name, value in cells.items()}}
    page = _position_page()
    return page, _widen(page, _HOLDINGS_STARTS, _WIDE_HOLDINGS, edits)


def _activity(edits: dict[str, dict[int, str]], *, pending: bool = False) -> tuple[str, str]:
    """The replacement for the activity or pending-trades page."""
    page = _activity_page(pending=pending)
    wide = _WIDE_PENDING if pending else _WIDE_ACTIVITY
    return page, _widen(page, _activity_header(pending=pending)[2], wide, edits)


# A ledger movement credits _LONG to the opening $10.00, and the closing row
# debits the balance back to $0.00.
_MOVED: Final = _exactly(_LONG, 10)


def _ledger_with(
    kind: str, *, balance: Decimal = _MOVED, debit: Decimal = _MOVED, credit: Decimal = _LONG
) -> tuple[str, str]:
    """The replacement for a ledger page, with the movement's printed
    ``balance`` and the total's printed ``debit`` and ``credit``."""
    heading, opening, closing, total = _LEDGERS[kind]
    rows = (
        LEDGER_LABELS,
        (opening, "1/1/2025", "", "", "$10.00"),
        ("Movement", "1/10/2025", "", _usd(_LONG), _usd(balance)),
        (closing, "1/31/2025", _usd(_MOVED), "", "$0.00"),
        (total, "", _usd(debit), _usd(credit), ""),
    )
    lines = (_row(_WIDE_LEDGER, row) for row in rows)
    return _ledger(kind), "\n".join((heading, *lines))


def _loaned(
    price: str, value: str, total: str | None = None, *, shares: str = "1"
) -> tuple[str, str]:
    """The replacement for the Stock Lending page: ``shares`` at ``price``,
    worth ``value``, and ``total`` (``value`` by default)."""
    page = loaned_securities_page(
        _WIDE_LOANED, [("Loaned Example", "LOAN", shares, price, value)], total or value, line=_row
    )
    return _stock_loaned_page(), page


def _holdings_value(
    price: str,
    value: str,
    *,
    quantity: str = "1",
    summary: Decimal | None = None,
    holdings: Decimal | None = None,
) -> str:
    """The held equity row of ``quantity`` at ``price`` and ``value``, the Account Summary's
    closing Total Securities ``summary`` and the holdings ``Total Securities``
    ``holdings`` (both the rows' sum by default), and each portfolio value
    that follows: $25.00 of cash more."""
    rows = _exactly(value.lstrip("$").replace(",", ""), 200)
    summary = rows if summary is None else summary
    holdings = rows if holdings is None else holdings
    return _statement_with(
        _holdings(quantity=quantity, price=price, value=value),
        ("Total Securities * $250.00 $300.00", f"Total Securities * $250.00 {_usd(summary)}"),
        (
            "Portfolio Value $260.00 $325.00",
            f"Portfolio Value $260.00 {_usd(_exactly(summary, 25))}",
        ),
        ("Total Securities $300.00 100.00%", f"Total Securities {_usd(holdings)} 100.00%"),
        (
            "Total Priced Portfolio $325.00",
            f"Total Priced Portfolio {_usd(_exactly(holdings, 25))}",
        ),
    )


class _Check(NamedTuple):
    build: Callable[[Decimal], str]  # the statement printing this value
    exact: Decimal  # the value the statement's rows give
    page: int  # the refusal of any other value: page, column, message
    column: str | None
    message: str
    kept: bool = True  # whether the parsed statement keeps the long amounts


_HELD_PRICE: Final = "$100,000,000.0000"
_HELD_VALUE: Final = "$100,000,000.00000000000000000001"
_ISSUE_DEBIT: Final = Decimal("123456789.12345678901234567891")
_ACTIVITY_SUMS: Final = "Account Activity debit/credit sums do not match the source total"
_PENDING_SUMS: Final = "pending-trade debit/credit sums do not match the source total"
_HOLDINGS_TOTAL: Final = "holdings total does not equal the rows it covers"
_SWEEP, _COLLATERAL = (_LEDGERS[kind][0] for kind in ("sweep", "collateral"))

# Each check, at a printed value whose sum needs more than 28 digits.
_CHECKS: Final = {
    "account-summary-closing": _Check(
        lambda value: _statement_with(
            ("Brokerage Cash Balance $5.00 $5.00", f"Brokerage Cash Balance $5.00 {_usd(_LONG)}"),
            ("Portfolio Value $260.00 $325.00", f"Portfolio Value $260.00 {_usd(value)}"),
        ),
        _exactly(_LONG, 20, 300),
        1,
        None,
        "closing Account Summary components do not equal portfolio value",
    ),
    "account-summary-opening": _Check(
        lambda value: _statement_with(
            ("Brokerage Cash Balance $5.00 $5.00", f"Brokerage Cash Balance {_usd(_LONG)} $5.00"),
            ("Portfolio Value $260.00 $325.00", f"Portfolio Value {_usd(value)} $325.00"),
        ),
        _exactly(_LONG, 5, 250),
        1,
        None,
        "opening Account Summary components do not equal portfolio value",
    ),
    "total-priced-portfolio": _Check(
        lambda value: _statement_with(
            ("Brokerage Cash Balance $5.00 1.54%", f"Brokerage Cash Balance {_usd(_LONG)} 1.54%"),
            ("Total Priced Portfolio $325.00", f"Total Priced Portfolio {_usd(value)}"),
        ),
        _exactly(_LONG, 20, 300),
        2,
        "Total Priced Portfolio",
        _HOLDINGS_TOTAL,
        kept=False,
    ),
    "holdings-market-value": _Check(
        lambda value: _holdings_value(_HELD_PRICE, _HELD_VALUE, holdings=value),
        _exactly(_HELD_VALUE[1:].replace(",", ""), 200),
        2,
        "Total Securities",
        _HOLDINGS_TOTAL,
    ),
    "account-summary-total-securities": _Check(
        lambda value: _holdings_value(_HELD_PRICE, _HELD_VALUE, summary=value),
        _exactly(_HELD_VALUE[1:].replace(",", ""), 200),
        0,
        None,
        "summed position values do not equal closing Total Securities",
    ),
    "holdings-estimated-dividend": _Check(
        lambda value: _statement_with(
            _holdings(dividend=_usd(_LONG)),
            ("Total Securities $300.00 100.00%", f"Total Securities $300.00 {_usd(value)} 100.00%"),
        ),
        _LONG,
        2,
        "Total Securities",
        _HOLDINGS_TOTAL,
    ),
    # The issue's example.
    "account-activity-debit": _Check(
        lambda value: _statement_with(
            _activity(
                {
                    "Purchase": {_DEBIT: _usd(_ISSUE_DEBIT)},
                    STATEMENT_ACTIVITY_TOTAL: {_DEBIT: _usd(value)},
                }
            )
        ),
        _ISSUE_DEBIT,
        0,
        None,
        _ACTIVITY_SUMS,
    ),
    "account-activity-credit": _Check(
        lambda value: _statement_with(
            _activity(
                {"CASH": {_CREDIT: _usd(_LONG)}, STATEMENT_ACTIVITY_TOTAL: {_CREDIT: _usd(value)}}
            )
        ),
        _LONG,
        0,
        None,
        _ACTIVITY_SUMS,
    ),
    "pending-trade-debit": _Check(
        lambda value: _statement_with(
            _activity(
                {"Pending Buy": {_DEBIT: _usd(_LONG)}, PENDING_TOTAL: {_DEBIT: _usd(value)}},
                pending=True,
            )
        ),
        _LONG,
        0,
        None,
        _PENDING_SUMS,
    ),
    "pending-trade-credit": _Check(
        lambda value: _statement_with(
            _activity(
                {
                    "Pending Buy": {0: "Pending Sell", 2: "Sell", _DEBIT: "", _CREDIT: _usd(_LONG)},
                    PENDING_TOTAL: {_DEBIT: "$0.00", _CREDIT: _usd(value)},
                },
                pending=True,
            )
        ),
        _LONG,
        0,
        None,
        _PENDING_SUMS,
    ),
    **{
        f"{kind}-{name}": _Check(
            lambda value, kind=kind, name=name: _statement_with(
                _ledger_with(kind, **{name: value})
            ),
            _LONG if name == "credit" else _MOVED,
            0,
            None,
            f"{heading} running balance does not reconcile"
            if name == "balance"
            else f"{heading} source totals do not reconcile",
        )
        for kind, heading in (("sweep", _SWEEP), ("collateral", _COLLATERAL))
        for name in ("balance", "debit", "credit")
    },
    "stock-lending-total": _Check(
        lambda value: _statement_with(_loaned(_HELD_PRICE, _HELD_VALUE, _usd(value))),
        Decimal(_HELD_VALUE[1:].replace(",", "")),
        7,
        None,
        "Stock Lending market-value total does not reconcile",
    ),
    "program-banks": _Check(
        lambda value: _statement_with(
            ("Deposit Sweep Balance $5.00 $20.00", f"Deposit Sweep Balance $5.00 {_usd(_LONG)}"),
            (
                "Portfolio Value $260.00 $325.00",
                f"Portfolio Value $260.00 {_usd(_exactly(_LONG, 305))}",
            ),
            (_BANK_ROW + "$20.00", _BANK_ROW + _usd(value)),
        ),
        _LONG,
        0,
        None,
        "Program Bank balances do not equal closing Deposit Sweep balance",
    ),
}


def _values(value: object) -> Iterator[DecimalTuple]:
    """Every Decimal in a parsed statement, as its digit tuple."""
    if isinstance(value, Decimal):
        yield value.as_tuple()
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _values(getattr(value, item.name))
    elif isinstance(value, tuple | list):
        for item in value:
            yield from _values(item)


def _long_amounts(text: str) -> set[DecimalTuple]:
    """Every amount of more than 28 digits in ``text``, as its digit tuple."""
    amounts = (token.strip("$").replace(",", "") for token in text.split())
    return {
        Decimal(amount).as_tuple()
        for amount in amounts
        if re.fullmatch(r"[0-9]+\.[0-9]+", amount) and len(amount) > 29
    }


@pytest.mark.parametrize("check", _CHECKS)
@_CONTEXTS
def test_a_check_past_28_digits_parses_with_every_digit(
    check: str, context: Context | None
) -> None:
    """Main refused each one: at this check, or at an earlier one of the same
    statement. The statement keeps every long amount it prints with every
    digit, except the Total Priced Portfolio rows, which it doesn't keep."""
    case = _CHECKS[check]
    text = case.build(case.exact)
    with localcontext(context):
        statement = parse_robinhood_statement(text)

    printed = _long_amounts(text)
    assert printed
    if case.kept:
        assert printed <= set(_values(statement))


@pytest.mark.parametrize("check", _CHECKS)
@pytest.mark.parametrize("offset", [_UNIT, -_UNIT], ids=["above", "below"])
@_CONTEXTS
def test_a_check_still_refuses_a_value_off_by_its_last_printed_place(
    check: str, offset: Decimal, context: Context | None
) -> None:
    """Main accepted several values one place below: its 28-digit sum had
    dropped that place."""
    case = _CHECKS[check]
    text = case.build(_exactly(case.exact, offset))
    with localcontext(context), pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refusal(raised.value, _STATEMENT, case.page, case.column, case.message)


@pytest.mark.parametrize(
    ("changes", "opening", "closing"),
    [
        pytest.param(
            (
                (
                    "Brokerage Cash Balance $5.00 $5.00",
                    "Brokerage Cash Balance $990,000,000,000.00 $5.00",
                ),
                (
                    "Deposit Sweep Balance $5.00 $20.00",
                    "Deposit Sweep Balance -$990,000,000,000.00 $20.00",
                ),
                ("Portfolio Value $260.00 $325.00", "Portfolio Value $250.00 $325.00"),
            ),
            Decimal("250.00"),
            Decimal("325.00"),
            id="opening",
        ),
        pytest.param(
            (
                (
                    "Brokerage Cash Balance $5.00 $5.00",
                    "Brokerage Cash Balance $5.00 $110,000,000,000.00",
                ),
                (
                    "Deposit Sweep Balance $5.00 $20.00",
                    "Deposit Sweep Balance $5.00 -$99,000,000,000.00",
                ),
                ("Portfolio Value $260.00 $325.00", "Portfolio Value $260.00 $11,000,000,300.00"),
                (_BANK_ROW + "$20.00", _BANK_ROW + "-$99,000,000,000.00"),
            ),
            Decimal("260.00"),
            Decimal("11000000300.00"),
            id="closing",
        ),
    ],
)
def test_account_summary_keeps_its_original_first_operand(changes, opening, closing) -> None:
    """Compatibility controls: main cancels cash and sweep exactly before adding securities."""
    text = _statement_with(*changes)
    with localcontext(Context(Emax=10)):
        statement = parse_robinhood_statement(text)

    summary = statement.account_summary
    assert (summary.opening_portfolio_value_usd, summary.closing_portfolio_value_usd) == (
        opening,
        closing,
    )


def test_portfolio_cash_subtotal_overflow_refuses_before_securities_can_cancel_it() -> None:
    text = _holdings_value("$990,000,000.00", "$99,000,000,000.00", quantity="100")
    for before, after in (
        (
            "Brokerage Cash Balance $5.00 1.54%",
            "Brokerage Cash Balance -$99,000,000,200.00 1.54%",
        ),
        (
            "Deposit Sweep Balance $20.00 6.15%",
            "Deposit Sweep Balance -$99,000,000,200.00 6.15%",
        ),
        (
            "Total Priced Portfolio $99,000,000,225.00",
            "Total Priced Portfolio -$99,000,000,200.00",
        ),
    ):
        assert text.count(before) == 1
        text = text.replace(before, after)
    with localcontext(Context(Emax=10)), pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refusal(
        raised.value,
        _STATEMENT,
        2,
        "Total Priced Portfolio",
        "Total Priced Portfolio sum exceeds the supported precision",
    )


# Row checks: 1 share at a 9-digit price with 20 places, against a value 0.01
# below, so the tolerance is 0.01 plus half the price's last place.
_ROW_PAST: Final = ("$123,456,789.12345678901234567891", "$123,456,789.11345678901234567890")
_ROW_WITHIN: Final = ("$123,456,789.12345678901234567896", "$123,456,789.11345678901234567896")


@_CONTEXTS
def test_a_position_past_its_tolerance_refuses(context: Context | None) -> None:
    """The exact difference is 0.01000000000000000001, past the tolerance of
    0.010000000000000000005; main's 28-digit product dropped the price's last
    digit, so it parsed."""
    text = _holdings_value(*_ROW_PAST)
    with localcontext(context), pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refusal(
        raised.value,
        _STATEMENT,
        2,
        "Mkt Value",
        "position quantity and price do not reproduce market value",
    )


@_CONTEXTS
def test_a_position_within_its_tolerance_parses(context: Context | None) -> None:
    """The exact difference is 0.01; main's product rounded the price's last
    digits up, past the tolerance."""
    text = _holdings_value(*_ROW_WITHIN)
    with localcontext(context):
        statement = parse_robinhood_statement(text)

    assert statement.positions[0].market_value_usd == Decimal(
        _ROW_WITHIN[1].lstrip("$").replace(",", "")
    )


@_CONTEXTS
def test_a_stock_lending_row_past_its_tolerance_refuses(context: Context | None) -> None:
    """The issue's example, which main parsed."""
    text = _statement_with(_loaned(*_ROW_PAST))
    with localcontext(context), pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refusal(raised.value, _STATEMENT, 7, None, "Stock Lending market value does not reconcile")


@_CONTEXTS
def test_a_stock_lending_row_within_its_tolerance_parses(context: Context | None) -> None:
    """Main refused it."""
    text = _statement_with(_loaned(*_ROW_WITHIN))
    with localcontext(context):
        statement = parse_robinhood_statement(text)

    (row,) = statement.stock_loaned_securities
    assert (row.price_usd, row.market_value_usd) == tuple(
        Decimal(cell.lstrip("$").replace(",", "")) for cell in _ROW_WITHIN
    )


# Rows at the edge of their tolerance, each against a value just past it and
# one 1E-20 higher, just within it. Rounding any one of the row check's
# products, sums or magnitudes to 28 digits moves the first within it.
_EDGES: Final = pytest.mark.parametrize(
    ("shares", "price", "past", "within"),
    [
        # 1.5 shares at a 9-digit price with 20 places: the tolerance, 0.05 of
        # the price and a cent, and the difference each need 30 digits.
        pytest.param(
            "1.5",
            "$987,654,321.55555555555555555551",
            "$1,432,098,766.24555555555555555548",
            "$1,432,098,766.24555555555555555549",
            id="long-price",
        ),
        # 32-digit shares at a price printed to 20 places: half its last place
        # times the shares needs 33 digits, and the difference is past the
        # tolerance by 5E-41.
        pytest.param(
            "199999999999.99999999999999999999",
            "$1,234.00000000000000000000",
            "$246,799,999,999,999.98999999899999998149",
            "$246,799,999,999,999.98999999899999998150",
            id="long-shares",
        ),
    ],
)
_ROWS: Final = pytest.mark.parametrize(
    ("build", "page", "column", "message"),
    [
        pytest.param(
            lambda shares, price, value: _holdings_value(price, value, quantity=shares),
            2,
            "Mkt Value",
            "position quantity and price do not reproduce market value",
            id="position",
        ),
        pytest.param(
            lambda shares, price, value: _statement_with(_loaned(price, value, shares=shares)),
            7,
            None,
            "Stock Lending market value does not reconcile",
            id="stock-lending",
        ),
    ],
)


@_ROWS
def test_a_rows_half_place_tolerance_does_not_round_to_the_active_context(
    build: Callable[[str, str, str], str], page: int, column: str | None, message: str
) -> None:
    # The exact tolerance is 0.01005; rounding half a price unit up to 0.01
    # under this context would let the 0.015 difference pass.
    with localcontext(Context(prec=1, Emin=-2, rounding=ROUND_UP)):
        parse_robinhood_statement(build("1", "$123.4567", "$123.4467"))
        with pytest.raises(ParseError) as raised:
            parse_robinhood_statement(build("1", "$123.4567", "$123.4417"))

    _refusal(raised.value, _STATEMENT, page, column, message)


def test_a_position_product_past_the_exponent_limit_refuses_at_its_value_column() -> None:
    text = _holdings_value("$1,000,000.00", "$100.00", quantity="100000")
    with localcontext(Context(Emax=10)), pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    _refusal(
        raised.value,
        _STATEMENT,
        2,
        "Mkt Value",
        "position market value check exceeds the supported precision",
    )


@_EDGES
@_ROWS
@_CONTEXTS
def test_a_row_at_the_edge_of_a_long_tolerance_is_checked_exactly(
    shares: str,
    price: str,
    past: str,
    within: str,
    build: Callable[[str, str, str], str],
    page: int,
    column: str | None,
    message: str,
    context: Context | None,
) -> None:
    with localcontext(context):
        statement = parse_robinhood_statement(build(shares, price, within))
        with pytest.raises(ParseError) as raised:
            parse_robinhood_statement(build(shares, price, past))

    assert _long_amounts(build(shares, price, within)) <= set(_values(statement))
    _refusal(raised.value, _STATEMENT, page, column, message)


class _UnaddableDecimal(Decimal):
    def __radd__(self, other: object) -> Decimal:
        raise MemoryError("synthetic allocation failure")


@pytest.mark.parametrize(
    ("compute", "page", "column"),
    [
        pytest.param(
            lambda: statement_parser._exact_sum(
                (Decimal("9E+999999"), Decimal("9E+999999")), at="Example sum"
            ),
            0,
            None,
            id="sum-overflow",
        ),
        pytest.param(
            lambda: statement_parser._exact_product(
                Decimal("9E+999999"), Decimal(10), at="Example sum", page=7, column="Mkt Value"
            ),
            7,
            "Mkt Value",
            id="product-overflow",
        ),
        # Without the Inexact trap it would underflow to zero.
        pytest.param(
            lambda: statement_parser._exact_product(
                Decimal("1E-999999999999999999"), Decimal("1E-999999999999999999"), at="Example sum"
            ),
            0,
            None,
            id="product-underflow",
        ),
        pytest.param(
            lambda: statement_parser._exact_sum(
                (_UnaddableDecimal("0.01"),), at="Example sum", page=2
            ),
            2,
            None,
            id="out-of-memory",
        ),
    ],
)
def test_arithmetic_that_cannot_be_exact_refuses_with_its_fixed_label(
    compute, page: int, column: str | None
) -> None:
    """Unreachable through the read-time limits: in-limit cells give products
    of about 62 digits at most."""
    with pytest.raises(ParseError) as raised:
        compute()

    _refusal(raised.value, _STATEMENT, page, column, "Example sum exceeds the supported precision")
