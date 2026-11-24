"""Exact IBKR parser arithmetic (#1311).

Owner rule (#1166): exact, never round. The Activity Statement Trades parser
keeps every digit of each trade's quantity, ``|Basis|`` and net proceeds
``|Basis| + Realized P/L``, and the Open Positions reconciliation adds, negates
and compares the reconstructed quantities exactly. The Flex closed-lot
proceeds ``CostBasis + FifoPnlRealized`` and the Flex ``PositionValue`` check
keep every digit too. The long-value regressions use in-limit cells (#1276)
whose arithmetic needs more than Decimal's default 28 digits, so before
#1311 it rounded. Controls and precision probes also use ordinary values.

In-limit cells give products of 61 digits at most and sums far below
``MAX_PREC`` and the default exponent limits. Precision refusals are reached
here under a narrower exponent limit (``Emax=10``). The helper's overflow
and underflow probes deliberately exceed cell limits; its out-of-memory
probe uses a Decimal that can't multiply.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import Final

import pytest

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.exceptions import IbkrFaError, LotMatchingError, ParseError
from ibkr_fa.models import ClosedLot, ExecutionEvent, ExecutionKind, OpenLot
from ibkr_fa.parser import parse_closed_lots, parse_open_positions
from ibkr_fa.parser._exact import distance_from_product, exactly
from ibkr_fa.parser.activity_statement import (
    build_instrument_lookup,
    parse_activity_statement_trades,
    validate_reconciliation,
)
from ibkr_fa.types import Ticker
from tests._activity_statement_rows import RECONCILIATION_REFUSAL as _RECONCILIATION
from tests._activity_statement_rows import fifo_open_lots as _open_lots
from tests._activity_statement_rows import make_fii_row, make_trade_row
from tests._activity_statement_rows import open_position_row as _make_op_row
from tests._crash_reports import assert_replaced_unchained, assert_unchained_refusal
from tests._flex_lot_rows import closing_execution_for, make_closed_lot_row
from tests._flex_section_rows import make_open_position_row as make_row
from tests._statement_period import ALL_DATES

# A neutral digit run, written contiguously into each refused value.
_MARKER: Final = "7362509"
# Long-value regressions exercise both the default context and a much
# narrower precision.
_CONTEXTS: Final = pytest.mark.parametrize(
    "context", [None, Context(prec=3)], ids=["default-context", "prec-3"]
)
# Narrower exponent limits than the default: the only way, short of running
# out of memory, to make the exact arithmetic of in-limit cells fail.
_NARROW: Final = Context(Emax=10)
_POSITION_VALUE: Final = "must equal Quantity * MarkPrice within 0.01"


@pytest.fixture(autouse=True)
def _no_output(capsys: pytest.CaptureFixture[str]) -> Iterator[None]:
    """Parser and helper calls print nothing, including refused input."""
    yield
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _trades(rows: list[dict[str, str]]) -> list[ExecutionEvent]:
    """Parse ``rows`` against the default instrument, with every date in period."""
    return parse_activity_statement_trades(
        rows, instruments=build_instrument_lookup([make_fii_row()]), period=ALL_DATES
    )


def _close(*, quantity: str, basis: str, realized_pnl: str) -> dict[str, str]:
    """A closing Trades row selling ``quantity`` (a negative number)."""
    return {
        **make_trade_row(quantity=quantity, basis=basis, code="C"),
        "Realized P/L": realized_pnl,
    }


# ---- Trades quantity, Basis and net proceeds ------------------------------------


@_CONTEXTS
def test_trade_quantity_basis_and_net_proceeds_keep_every_digit(context: Context | None) -> None:
    """Fails on aa7ed98: ``abs()`` and ``+`` rounded each value to 28 digits,
    so the quantity became 100000000.0000000000000000000, the Basis
    123456789.5246179966044142615 and the net proceeds the same."""
    rows = [
        make_trade_row(
            quantity="100000000.00000000000000000001", basis="123456789.52461799660441426146"
        ),
        _close(
            quantity="-100000000.00000000000000000001",
            basis="-123456789.52461799660441426146",
            realized_pnl="0.00000000000000000001",
        ),
    ]
    with localcontext(context):
        opened, closed = _trades(rows)

    assert opened.kind is ExecutionKind.OPEN
    assert str(opened.quantity) == "100000000.00000000000000000001"
    assert str(opened.acquisition_cost) == "123456789.52461799660441426146"
    assert closed.kind is ExecutionKind.CLOSE
    assert str(closed.quantity) == "100000000.00000000000000000001"
    assert str(closed.net_proceeds) == "123456789.52461799660441426147"


def test_issue_closing_basis_parses_to_its_exact_net_proceeds() -> None:
    """The issue's item 1 sale: Basis -123456789.52461799660441426146 (9 whole
    digits, 20 places) and Realized P/L 0. Fails on aa7ed98, which gave net
    proceeds of 123456789.5246179966044142615."""
    (closed,) = _trades(
        [_close(quantity="-1", basis="-123456789.52461799660441426146", realized_pnl="0")]
    )

    assert str(closed.net_proceeds) == "123456789.52461799660441426146"


def test_net_proceeds_past_the_exponent_limit_refuse_unchained() -> None:
    """Under ``Emax=10``, |Basis| + Realized P/L = 147250180000 overflows. The
    refusal is fixed text at the row's Realized P/L, with no value or chain."""
    row = _close(quantity="-1", basis=f"-{_MARKER}0000", realized_pnl=f"{_MARKER}0000")
    (closed,) = _trades([row])
    assert closed.net_proceeds == Decimal("147250180000")

    with localcontext(_NARROW), pytest.raises(ParseError) as raised:
        _trades([make_trade_row(), row])

    expected = ParseError(
        section="Trades",
        row_index=1,
        column="Realized P/L",
        message="the net proceeds arithmetic exceeds the supported precision",
        raw_value="",
    )
    assert_unchained_refusal(
        raised.value,
        expected,
        "ParseError in section 'Trades' at row 1, column 'Realized P/L': "
        "the net proceeds arithmetic exceeds the supported precision",
        markers=(_MARKER,),
    )


# ---- FIFO matching past an open SHORT lot --------------------------------------


def _day(day: int) -> str:
    return f"2025-06-{day}, 09:30:00"


def _match(rows: list[dict[str, str]]) -> tuple[list[OpenLot], list[ClosedLot]]:
    return match_fifo(
        _trades(rows),
        dec31_close_prices={Ticker("XQAW"): Decimal("45.60")},
        report_date=date(2025, 12, 31),
    )


def test_a_sale_of_a_remainder_rounding_dropped_does_not_reach_a_short_lot() -> None:
    """A sale that closes exactly what is left of its buys. Fails on aa7ed98:
    the buy of 100000000.00000000000000000002 rounded to 100000000, so the
    first sale closed it all, and the second sale's 2E-20 share reached the
    later open SHORT lot and refused (#109). Exact, it closes the remainder
    and the SHORT lot stays open."""
    open_lots, closed_lots = _match(
        [
            make_trade_row(date_time=_day(15), quantity="100000000.00000000000000000002"),
            _close(quantity="-100000000", basis="-412.50", realized_pnl="0")
            | {"Date/Time": _day(16)},
            make_trade_row(date_time=_day(17), quantity="-5", basis="-206.25"),
            _close(quantity="-0.00000000000000000002", basis="-0.01", realized_pnl="0")
            | {"Date/Time": _day(18)},
        ]
    )

    assert [str(lot.quantity) for lot in closed_lots] == ["100000000", "2E-20"]
    assert [(str(lot.quantity), lot.side.value) for lot in open_lots] == [("5", "Short")]


@pytest.mark.parametrize(
    ("bought", "sold"),
    [
        ("100000000", "-100000000.00000000000000000001"),
        (f"{_MARKER}0000", f"-{_MARKER}0000.00000000000000000001"),
    ],
    ids=["plain", "marker"],
)
def test_a_sale_past_its_buys_only_past_28_digits_reaches_a_short_lot_and_refuses(
    bought: str, sold: str
) -> None:
    """A sale 1E-20 share past its long buy, with a SHORT lot opened in
    between. Fails on aa7ed98: the sale rounded to the long buy and closed it
    exactly. Exact, its FIFO walk reaches the SHORT lot, so it refuses with
    #109's message, with no value printed."""
    with pytest.raises(LotMatchingError) as raised:
        _match(
            [
                make_trade_row(date_time=_day(15), quantity=bought),
                make_trade_row(date_time=_day(16), quantity="-5", basis="-206.25"),
                _close(quantity=sold, basis="-412.50", realized_pnl="0") | {"Date/Time": _day(17)},
            ]
        )

    message = (
        "the closing trade at row 2 matched FIFO-first against an opening SHORT lot at row 1. "
        "v1 does not support closing SHORT positions on Schedule FA; see "
        "https://github.com/jay-tau/ibkr-fa/issues/109."
    )
    assert_unchained_refusal(
        raised.value,
        LotMatchingError(row_index=2, message=message),
        f"LotMatchingError in section 'Trades' at row 2: {message}",
        markers=(_MARKER,) if _MARKER in sold else (),
    )


# ---- Open Positions reconciliation --------------------------------------------


def _reconcile(
    quantities: list[str], summary_quantity: str, context: Context | None = None
) -> None:
    """Buy each of ``quantities`` (negative: a short sale), then reconcile the
    surviving lots with one Summary row of ``summary_quantity``."""
    lots = _open_lots(
        [
            make_trade_row(quantity=quantity, basis="-412.50" if quantity[0] == "-" else "412.50")
            for quantity in quantities
        ],
        instruments=build_instrument_lookup([make_fii_row()]),
    )
    with localcontext(context):
        validate_reconciliation(lots, [_make_op_row(symbol="XQAW", quantity=summary_quantity)])


def _assert_reconciliation_refusal(error: ParseError, *markers: str) -> None:
    expected = ParseError(
        section="Open Positions",
        row_index=0,
        column="Quantity",
        message=_RECONCILIATION,
        raw_value="",
    )
    assert_unchained_refusal(
        error,
        expected,
        f"ParseError in section 'Open Positions' at row 0, column 'Quantity': {_RECONCILIATION}",
        markers=markers,
    )


@_CONTEXTS
def test_reconciliation_past_the_tolerance_only_past_28_digits_refuses(
    context: Context | None,
) -> None:
    """The issue's item 2, first example. Fails on aa7ed98: the sum rounded to
    100000000000.0001000000000000, within 0.0001 of the Summary row, though the
    exact difference is 0.00010000000000000001."""
    with pytest.raises(ParseError) as raised:
        _reconcile(["100000000000", "0.00010000000000000001"], "100000000000", context)

    _assert_reconciliation_refusal(raised.value)


@_CONTEXTS
def test_reconciliation_within_the_tolerance_only_exactly_passes(context: Context | None) -> None:
    """The issue's item 2, second example. At default precision it fails on
    aa7ed98: each running sum rounded up to a difference of 0.0001000000000001
    and refused, though the exact difference is 0.00009999999999999999."""
    _reconcile(
        [
            "100000000000",
            "0.00003333333333335000",
            "0.00003333333333335000",
            "0.00003333333333329999",
        ],
        "100000000000",
        context,
    )


@pytest.mark.parametrize(
    ("summary_quantity", "accepted"),
    [
        ("-100000000000.0001", True),
        ("-100000000000", False),
    ],
)
def test_a_short_lot_is_negated_exactly(summary_quantity: str, accepted: bool) -> None:
    """A short lot of 100000000000.00010000000000000001 shares (32 digits)
    reconciles to -100000000000.0001 (1E-20 off) and refuses against
    -100000000000 (0.00010000000000000001 off). Unary minus would round the
    lot to 28 digits, and the second would pass. The mismatch case fails on
    aa7ed98, which rounded the quantity when it was read."""
    quantities = ["-100000000000.00010000000000000001"]
    if accepted:
        _reconcile(quantities, summary_quantity)
        return
    with pytest.raises(ParseError) as raised:
        _reconcile(quantities, summary_quantity)
    _assert_reconciliation_refusal(raised.value)


@pytest.mark.parametrize(
    ("quantity", "accepted"),
    [
        ("10.0001", True),
        ("10.00010000000000000001", False),
        ("9.9999", True),
        ("9.99989999999999999999", False),
    ],
)
def test_reconciliation_refuses_one_unit_past_its_tolerance(quantity: str, accepted: bool) -> None:
    """Control: against a Summary Quantity of 10, a lot 0.0001 off either way
    reconciles, and one off by one more unit in the 20th place refuses with
    the unchanged message."""
    if accepted:
        _reconcile([quantity], "10")
        return
    with pytest.raises(ParseError) as raised:
        _reconcile([quantity], "10")
    _assert_reconciliation_refusal(raised.value)


@pytest.mark.parametrize(
    "quantities",
    [
        pytest.param([f"{_MARKER}0000", "0.00010000000000000001"], id="two-lots"),
        pytest.param([f"{_MARKER}0000.00010000000000000001"], id="one-lot"),
    ],
)
def test_reconciliation_refusal_prints_no_quantity(quantities: list[str]) -> None:
    """Lots 0.00010000000000000001 past a Summary Quantity of 73625090000,
    whose digits hold a marker, refuse with fixed text: the marker is in no
    field, string or chain. Fails on aa7ed98, whose rounded sum, or rounded
    32-digit lot, came within 0.0001."""
    with pytest.raises(ParseError) as raised:
        _reconcile(quantities, f"{_MARKER}0000")

    _assert_reconciliation_refusal(raised.value, _MARKER)


def test_reconciliation_past_the_exponent_limit_refuses_unchained() -> None:
    """Under ``Emax=10``, three lots of 73625090000 shares less a Summary
    Quantity of 73625090000 come to 147250180000, which overflows. The refusal
    is fixed text at the Summary row's Quantity, with no value or chain. Under the
    default context the same rows refuse as a mismatch."""
    summary_quantity = f"{_MARKER}0000"
    quantities = [summary_quantity] * 3
    with pytest.raises(ParseError) as mismatch:
        _reconcile(quantities, summary_quantity)
    _assert_reconciliation_refusal(mismatch.value, _MARKER)

    with pytest.raises(ParseError) as raised:
        _reconcile(quantities, summary_quantity, _NARROW)

    expected = ParseError(
        section="Open Positions",
        row_index=0,
        column="Quantity",
        message="the reconciliation arithmetic exceeds the supported precision",
        raw_value="",
    )
    assert_unchained_refusal(
        raised.value,
        expected,
        "ParseError in section 'Open Positions' at row 0, column 'Quantity': "
        "the reconciliation arithmetic exceeds the supported precision",
        markers=(_MARKER,),
    )


# ---- Flex closed-lot proceeds ----------------------------------------------------


def _closed_lots(**cells: str) -> list[ClosedLot]:
    lot = make_closed_lot_row(Quantity="1", **cells)
    return parse_closed_lots([closing_execution_for(lot), lot])


@_CONTEXTS
def test_flex_closed_lot_proceeds_keep_every_digit(context: Context | None) -> None:
    """The issue's item 3. Fails on aa7ed98, which gave proceeds of
    123456789.1234567890123456789."""
    with localcontext(context):
        (lot,) = _closed_lots(
            CostBasis="123456789.12345678901234567891", FifoPnlRealized="0.00000000000000000001"
        )

    assert str(lot.cost_basis_money) == "123456789.12345678901234567891"
    assert str(lot.proceeds_money) == "123456789.12345678901234567892"


def test_flex_closed_lot_proceeds_past_the_exponent_limit_refuse_unchained() -> None:
    """Under ``Emax=10``, CostBasis + FifoPnlRealized = 147250180000
    overflows. The refusal is fixed text at the CLOSED_LOT row's
    FifoPnlRealized, with no value or chain."""
    cells = {"CostBasis": f"{_MARKER}0000", "FifoPnlRealized": f"{_MARKER}0000"}
    (lot,) = _closed_lots(**cells)
    assert lot.proceeds_money == Decimal("147250180000")

    with localcontext(_NARROW), pytest.raises(ParseError) as raised:
        _closed_lots(**cells)

    expected = ParseError(
        section="Trades",
        row_index=1,
        column="FifoPnlRealized",
        message="the proceeds arithmetic exceeds the supported precision",
        raw_value="",
    )
    assert_unchained_refusal(
        raised.value,
        expected,
        "ParseError in section 'Trades' at row 1, column 'FifoPnlRealized': "
        "the proceeds arithmetic exceeds the supported precision",
        markers=(_MARKER,),
    )


# ---- Flex Open Positions PositionValue ---------------------------------------------


def _open_position(**cells: str) -> OpenLot:
    (lot,) = parse_open_positions([make_row(**cells)])
    return lot


def _assert_position_value_refusal(error: ParseError, *markers: str) -> None:
    expected = ParseError(
        section="Open Positions (LOT)",
        row_index=0,
        column="PositionValue",
        message=_POSITION_VALUE,
        raw_value="",
    )
    assert_unchained_refusal(
        error,
        expected,
        f"ParseError in section 'Open Positions (LOT)' at row 0, column 'PositionValue': "
        f"{_POSITION_VALUE}",
        markers=markers,
    )


@_CONTEXTS
def test_position_value_past_the_tolerance_only_past_28_digits_refuses(
    context: Context | None,
) -> None:
    """The issue's item 4, first row. At default precision it fails on aa7ed98:
    the difference is 0.01000000000000000001, but the 28-digit product rounded it to 0.01."""
    with localcontext(context), pytest.raises(ParseError) as raised:
        _open_position(
            Quantity="1",
            MarkPrice="123456789.12345678901234567891",
            PositionValue="123456789.11345678901234567890",
        )

    _assert_position_value_refusal(raised.value)


@_CONTEXTS
def test_position_value_within_the_tolerance_only_exactly_parses(context: Context | None) -> None:
    """The issue's item 4, second row. Fails on aa7ed98: the exact difference
    is 0.01, but the 28-digit product rounded the mark up past it."""
    with localcontext(context):
        lot = _open_position(
            Quantity="1",
            MarkPrice="123456789.12345678901234567895",
            PositionValue="123456789.11345678901234567895",
        )

    assert str(lot.mark_price) == "123456789.12345678901234567895"


@pytest.mark.parametrize(
    ("position_value", "accepted"),
    [
        ("1127.51", True),
        ("1127.51000000000000000001", False),
        ("1127.49", True),
        ("1127.48999999999999999999", False),
    ],
)
def test_position_value_refuses_one_unit_past_its_tolerance(
    position_value: str, accepted: bool
) -> None:
    """Control: 25 shares at 45.10 are 1127.50. A PositionValue 0.01 off
    either way parses, and one off by one more unit in the 20th place
    refuses with the unchanged message."""
    if accepted:
        _open_position(PositionValue=position_value)
        return
    with pytest.raises(ParseError) as raised:
        _open_position(PositionValue=position_value)
    _assert_position_value_refusal(raised.value)


def test_position_value_refusal_prints_no_value() -> None:
    """A row 0.01000000000000000001 off, with a digit-run marker in MarkPrice
    and PositionValue, refuses with fixed text: the marker is in no field,
    string or chain. Fails on aa7ed98, whose rounded product passed it."""
    with pytest.raises(ParseError) as raised:
        _open_position(
            Quantity="1",
            MarkPrice=f"123456789.11{_MARKER}00000000001",
            PositionValue=f"123456789.10{_MARKER}00000000000",
        )

    _assert_position_value_refusal(raised.value, _MARKER)


def test_position_value_past_the_exponent_limit_refuses_unchained() -> None:
    """Under ``Emax=10``, 7362509 shares at 100000 are 736250900000,
    which overflows. The refusal is fixed text at the row's PositionValue,
    with no value or chain."""
    cells = {"Quantity": _MARKER, "MarkPrice": "100000", "PositionValue": f"{_MARKER}00000"}
    _open_position(**cells)

    with localcontext(_NARROW), pytest.raises(ParseError) as raised:
        _open_position(**cells)

    expected = ParseError(
        section="Open Positions (LOT)",
        row_index=0,
        column="PositionValue",
        message="the PositionValue arithmetic exceeds the supported precision",
        raw_value="",
    )
    assert_unchained_refusal(
        raised.value,
        expected,
        "ParseError in section 'Open Positions (LOT)' at row 0, column 'PositionValue': "
        "the PositionValue arithmetic exceeds the supported precision",
        markers=(_MARKER,),
    )


# ---- parser/_exact.distance_from_product ------------------------------------------


class _RefusedError(IbkrFaError):
    """A stand-in for a caller's own refusal."""


def _recording_refusal() -> tuple[list[bool], Callable[[bool], IbkrFaError]]:
    calls: list[bool] = []

    def refusal(out_of_memory: bool) -> IbkrFaError:
        calls.append(out_of_memory)
        return _RefusedError("refused")

    return calls, refusal


@_CONTEXTS
@pytest.mark.parametrize(
    ("value", "quantity", "price", "distance"),
    [
        (
            "123456789.11345678901234567890",
            "1",
            "123456789.12345678901234567891",
            "0.01000000000000000001",
        ),
        (
            "123456789.13345678901234567892",
            "1",
            "123456789.12345678901234567891",
            "0.01000000000000000001",
        ),
        (
            "-0.00000000000000000001",
            "-123456789012.12345678901234567891",
            "123456789.12345678901234567891",
            "15241578766928822799.9631166020312605936526596557677488187881",
        ),
    ],
    ids=["below", "above", "short"],
)
def test_distance_from_product_keeps_every_digit(
    context: Context | None, value: str, quantity: str, price: str, distance: str
) -> None:
    """``|value - quantity * price|``, exact under any precision and either
    side of the product."""
    calls, refusal = _recording_refusal()
    with localcontext(context):
        result = distance_from_product(
            Decimal(value), Decimal(quantity), Decimal(price), refusal=refusal
        )

    assert result == Decimal(distance)
    assert Fraction(result) == abs(Fraction(value) - Fraction(quantity) * Fraction(price))
    assert calls == []


class _UnmultipliableDecimal(Decimal):
    def __mul__(self, other: object) -> Decimal:
        raise MemoryError(f"synthetic allocation failure {_MARKER}")


@pytest.mark.parametrize(
    ("value", "quantity", "price", "out_of_memory"),
    [
        pytest.param(Decimal(1), Decimal(f"{_MARKER}E+999993"), Decimal(10), False, id="overflow"),
        # Without the Inexact trap it would underflow to zero.
        pytest.param(
            Decimal(0),
            Decimal(f"{_MARKER}E-999999999999999999"),
            Decimal(f"{_MARKER}E-999999999999999999"),
            False,
            id="underflow",
        ),
        pytest.param(
            Decimal(1), _UnmultipliableDecimal(_MARKER), Decimal(3), True, id="out-of-memory"
        ),
    ],
)
def test_distance_from_product_refuses_unchained(
    value: Decimal, quantity: Decimal, price: Decimal, out_of_memory: bool
) -> None:
    """Where the product or difference can't be kept, it raises the caller's
    refusal, told whether memory ran out, with no exception chained."""
    calls, refusal = _recording_refusal()
    with pytest.raises(_RefusedError) as raised:
        distance_from_product(value, quantity, price, refusal=refusal)

    assert calls == [out_of_memory]
    assert str(raised.value) == "refused"
    assert_replaced_unchained(raised.value, markers=(_MARKER,))


def test_exactly_still_adds_with_every_digit() -> None:
    """Control: the shared helper the parsers' sums use is unchanged."""
    _calls, refusal = _recording_refusal()
    with localcontext(Context(prec=3)):
        total = exactly(
            operator.add,
            Decimal("123456789.12345678901234567891"),
            Decimal("0.00000000000000000001"),
            refusal=refusal,
        )
    assert str(total) == "123456789.12345678901234567892"
