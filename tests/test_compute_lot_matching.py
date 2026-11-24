"""Tests for :mod:`ibkr_fa.compute.lot_matching`.

Covers the quantity-only FIFO pool shared by CMA and options (#639),
the Activity Statement lots pinned before the matcher moved out of the
parser, and the Activity Statement matcher itself (#104, #640).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.compute.lot_matching import (
    BrokerBasisError,
    FifoConsumption,
    FifoPool,
    require_indian_basis,
)
from ibkr_fa.exceptions import InvariantError, LotMatchingError
from ibkr_fa.models import (
    BasisProvenance,
    ClosedLot,
    ExecutionEvent,
    ExecutionKind,
    OpenLot,
    Side,
    StatutoryInrCostBasis,
)
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    split_activity_statement_sections,
)
from ibkr_fa.pipeline import _dispatch_activity_statement
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM, TOO_MANY_DIGITS
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE
from tests._lot_matching_inputs import SAMPLE_XQBB as _XQBB
from tests._lot_matching_inputs import MatchInstrument as _Instrument
from tests._lot_matching_inputs import close_execution as _close
from tests._lot_matching_inputs import match_lots as _match
from tests._lot_matching_inputs import open_execution as _open

_FIXTURES = Path(__file__).parent / "fixtures"
_EXPECTED_LOTS = _FIXTURES / "lot_matching" / "activity_statement_lots.json"

# The fixed label a caller passes ``consume`` to name its row (#1273).
_POOL_AT = "Synthetic pool, close row 1, column quantity"


def _pool(*entries: tuple[str, str]) -> FifoPool[str]:
    pool: FifoPool[str] = FifoPool()
    for item, quantity in entries:
        pool.add(item, Decimal(quantity))
    return pool


def test_consume_splits_across_three_entries() -> None:
    pool = _pool(("a", "2"), ("b", "3"), ("c", "4"), ("d", "5"))

    assert pool.consume(Decimal("7"), at=_POOL_AT) == FifoConsumption(
        allocations=(("a", Decimal("2")), ("b", Decimal("3")), ("c", Decimal("2"))),
        shortfall=Decimal(0),
    )
    assert pool.remaining() == (("c", Decimal("2")), ("d", Decimal("5")))


def test_exact_take_removes_the_entry() -> None:
    pool = _pool(("a", "2"), ("b", "3"))

    assert pool.consume(Decimal("2"), at=_POOL_AT).allocations == (("a", Decimal("2")),)
    assert pool.remaining() == (("b", Decimal("3")),)


def test_partial_take_leaves_the_remainder_at_the_head() -> None:
    pool = _pool(("a", "2.5"), ("b", "3"))

    assert pool.consume(Decimal("1"), at=_POOL_AT).allocations == (("a", Decimal("1")),)
    assert pool.remaining() == (("a", Decimal("1.5")), ("b", Decimal("3")))


def test_shortfall_takes_nothing_and_leaves_the_pool_unchanged() -> None:
    pool = _pool(("a", "2"), ("b", "3"))

    assert pool.consume(Decimal("6"), at=_POOL_AT) == FifoConsumption(
        allocations=(), shortfall=Decimal("1")
    )
    assert pool.remaining() == (("a", Decimal("2")), ("b", Decimal("3")))


def test_empty_pool_reports_the_whole_request_as_shortfall() -> None:
    pool = _pool()

    assert pool.consume(Decimal("1"), at=_POOL_AT) == FifoConsumption(
        allocations=(), shortfall=Decimal("1")
    )
    assert pool.remaining() == ()


def test_remaining_keeps_insertion_order() -> None:
    pool = _pool(("b", "1"), ("a", "2"), ("c", "3"))

    assert [item for item, _ in pool.remaining()] == ["b", "a", "c"]


def test_tie_keeps_the_request_exponent() -> None:
    pool = _pool(("a", "2.0"))

    ((_, taken),) = pool.consume(Decimal("2"), at=_POOL_AT).allocations
    assert str(taken) == "2"
    assert pool.remaining() == ()


def test_tie_can_keep_the_open_exponent() -> None:
    pool = _pool(("a", "2.0"))

    ((_, taken),) = pool.consume(Decimal("2"), at=_POOL_AT, prefer_open_on_tie=True).allocations
    assert str(taken) == "2.0"
    assert pool.remaining() == ()


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_add_rejects_non_positive_or_non_finite_quantity(quantity: str) -> None:
    pool = _pool(("a", "1"))

    with pytest.raises(InvariantError, match="FIFO quantity must be positive and finite"):
        pool.add("b", Decimal(quantity))
    assert pool.remaining() == (("a", Decimal("1")),)


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_consume_rejects_non_positive_or_non_finite_quantity(quantity: str) -> None:
    pool = _pool(("a", "1"))

    with pytest.raises(InvariantError, match="FIFO quantity must be positive and finite"):
        pool.consume(Decimal(quantity), at=_POOL_AT)
    assert pool.remaining() == (("a", Decimal("1")),)


_TEN_TO_30 = "1" + "0" * 30  # 31 digits; minus 1 it needs 30


def test_consume_subtracts_exactly_past_the_context_precision() -> None:
    """Fails on 0d3547b: 10^30 - 1 rounded back to 10^30 at 28 digits, so
    the pool kept 10^30 and a second 10^30 request matched in full."""
    pool = _pool(("a", _TEN_TO_30))

    assert pool.consume(Decimal(1), at=_POOL_AT).allocations == (("a", Decimal(1)),)
    [(_, left)] = pool.remaining()
    assert left.compare_total(Decimal("9" * 30)) == 0

    consumption = pool.consume(Decimal(_TEN_TO_30), at=_POOL_AT)
    assert consumption.allocations == ()
    assert consumption.shortfall.compare_total(Decimal(1)) == 0
    [(_, left)] = pool.remaining()
    assert left.compare_total(Decimal("9" * 30)) == 0


@pytest.mark.parametrize(
    ("entries", "request_quantity"),
    [
        # The entry's remainder 1E+600 - 1E-500 needs 1,101 digits.
        pytest.param((("ZQXPA", "1E+600"),), "1E-500", id="entry-remainder"),
        # The first entry empties exactly; the request's remainder
        # 1E+600 - 1E-500 is the one past the cap.
        pytest.param((("ZQXPA", "1E-500"), ("ZQXPB", "1E+600")), "1E+600", id="running-remainder"),
    ],
)
def test_consume_refuses_a_difference_past_the_digit_cap_before_the_pool_changes(
    entries: tuple[tuple[str, str], ...], request_quantity: str
) -> None:
    """Fails on 0d3547b (whose ``consume`` also takes no ``at``): the
    difference rounded to 1E+600 at 28 digits, so an entry kept all it held
    after giving 1E-500 away, or a request got 1E-500 more than it asked
    for. The refusal is fixed text with no context and no chain."""
    pool = _pool(*entries)
    before = pool.remaining()

    with pytest.raises(InvariantError) as exc:
        pool.consume(Decimal(request_quantity), at=_POOL_AT)
    error = exc.value
    assert str(error) == f"InvariantError: {_POOL_AT}: {INEXACT_SUM}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error), str(error.args)):
        _assert_absent(text, *(item for item, _ in entries), "E+600", "E-500")
    assert [(item, str(left)) for item, left in pool.remaining()] == [
        (item, str(held)) for item, held in before
    ]


# ---- Activity Statement characterisation (#640) ---------------------------


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _snapshot(lot: OpenLot | ClosedLot) -> dict[str, str | None]:
    """Every field as text; ``str`` keeps each Decimal's exponent."""
    return {field.name: _text(getattr(lot, field.name)) for field in fields(lot)}


def _expected_lots() -> dict[str, dict[str, list[dict[str, str | None]]]]:
    return json.loads(_EXPECTED_LOTS.read_text(encoding="utf-8"))


def test_expected_lots_cover_every_activity_statement_fixture_with_trades() -> None:
    with_trades = {
        path.name
        for path in _FIXTURES.glob("e2e_activity_statement_*.csv")
        if KnownActivitySection.TRADES in split_activity_statement_sections(path)
    }

    assert set(_expected_lots()) == with_trades


@pytest.mark.parametrize("fixture", sorted(_expected_lots()))
def test_activity_statement_lots_match_pinned_output(fixture: str) -> None:
    extract = _dispatch_activity_statement(
        _FIXTURES / fixture,
        period_validator=lambda _period: None,
        fallback_account_holder_name="Test Holder",
        fallback_account_id="U1234567",
    )

    assert {
        "open_lots": [_snapshot(lot) for lot in extract.open_lots],
        "closed_lots": [_snapshot(lot) for lot in extract.closed_lots],
    } == _expected_lots()[fixture]


# ---- Activity Statement matcher (#104, #640) -------------------------------


def _prices(**close_prices: str) -> dict[Ticker, Decimal]:
    return {Ticker(symbol): Decimal(price) for symbol, price in close_prices.items()}


# Fixed refusal texts (#1047): each names the Trades row and the rule, never
# the symbol, ISIN, dates or quantities.
_SHORTFALL_MESSAGE = (
    "the closing trade closes more shares than the earlier opening trades of its "
    "symbol hold, so part of it can't be FIFO-matched. The FII section may be "
    "truncated, or the opening trades pre-date this Activity Statement's period — "
    "check Open Positions reconciliation upstream."
)
_SHORT_COVER_MESSAGE = (
    "the closing trade has a positive Quantity: a closing BUY (buy-to-cover of a "
    "SHORT position). v1 does not support closing SHORT positions on Schedule FA; "
    "see https://github.com/jay-tau/ibkr-fa/issues/109."
)
_NO_DEC31_CLOSE_MESSAGE = (
    "the opening trade leaves a surviving open lot after FIFO matching, but its "
    "symbol does not appear in the Open Positions section, so the Dec 31 close "
    "price can't be determined. See https://github.com/jay-tau/ibkr-fa/issues/133."
)


def _short_open_message(closed_row: int, opened_row: int) -> str:
    return (
        f"the closing trade at row {closed_row} matched FIFO-first against an "
        f"opening SHORT lot at row {opened_row}. v1 does not support closing SHORT "
        "positions on Schedule FA; see https://github.com/jay-tau/ibkr-fa/issues/109."
    )


def _rendered(row_index: int, message: str) -> str:
    return f"LotMatchingError in section 'Trades' at row {row_index}: {message}"


def _assert_absent(text: str, *values: str) -> None:
    for value in values:
        assert value.lower() not in text.lower(), value


# A column-shifted Trades row can put the holder's name and account into the
# Symbol cell; the refusals must not echo it, nor the ISIN, the trade dates
# or the share counts (#1047).
_LEAKY = _Instrument("Synthetic Holder Two U1234567", "US99971T1035", "Synthetic Holder Two")
_LEAKY_VALUES = (
    "Synthetic Holder Two",
    "U1234567",
    "US99971T1035",
    "2025-03-01",
    "2025-06-02",
    "1234567.89",
    "1234.5",
    # 1234567.89 - 1234.5: the shortfall or the surviving residual.
    "1233333.39",
)


def test_c_code_paired_with_o_now_parses_as_closure() -> None:
    """Post-#104: an O row and a C row on the same symbol FIFO-match
    into one ClosedLot."""
    open_lots, closed_lots = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "852.40"),
            # |Basis| 852.40 + Realized P/L 100.
            _close(1, "2025-09-15 09:30:00", "10", "952.40"),
        ],
        {},  # symbol fully closed; no Dec 31 lookup needed
    )
    assert open_lots == []
    [cl] = closed_lots
    assert cl.quantity == Decimal("10")
    assert cl.cost_basis_money == Decimal("852.40")
    assert cl.proceeds_money - cl.cost_basis_money == Decimal("100")
    assert cl.proceeds_money == Decimal("952.40")


def test_closure_populates_gross_proceeds_from_t_price() -> None:
    """#348: a matched closure carries gross_proceeds_money =
    |quantity| * T.Price — the raw pre-commission sale consideration,
    distinct from (and >=) the net proceeds_money. Data is kept
    internally consistent (net = gross - commission): 10 sh sold at
    95.40 gross = 954.00; realized 99.00 over basis 850.00 => net
    949.00, i.e. 5.00 commission."""
    _, closed_lots = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "850.00"),
            _close(1, "2025-09-15 09:30:00", "10", "949.00", trade_price="95.40"),
        ],
        {},
    )
    [cl] = closed_lots
    assert cl.gross_proceeds_money is not None
    assert cl.gross_proceeds_money == Decimal("10") * Decimal("95.40")
    assert cl.proceeds_money == Decimal("949.00")
    assert cl.gross_proceeds_money > cl.proceeds_money


def test_closure_zero_t_price_worthless_removal_files_gross_zero() -> None:
    """#348 non-regression: a worthless-security removal / delisting
    closes at T.Price 0 (the investor realizes the full loss). This is a
    valid disposal — it must match and file gross proceeds 0, NOT be
    refused."""
    _, closed_lots = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "850.00"),
            # Total loss: |Basis| 850.00 + Realized P/L -850.00 => net 0.
            _close(1, "2025-09-15 09:30:00", "10", "0.00", trade_price="0"),
        ],
        {},
    )
    [cl] = closed_lots
    assert cl.gross_proceeds_money == Decimal("0")
    assert cl.proceeds_money == Decimal("0")


def test_open_lot_field_mapping() -> None:
    """Every OpenLot field comes from the open event, the Dec 31 close
    and the report date."""
    [lot], _ = _match(
        [_open(0, "2025-06-15 14:45:00", "10", "852.40")],
        _prices(XQBB="95.40"),
    )
    assert lot == OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQBB"),
        description="SAMPLE ISSUER BB INC",
        isin=Isin("US99995B1044"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("95.40"),
        cost_basis_money=Decimal("852.40"),
        side=Side.LONG,
        # Date/Time's date component, with the time discarded.
        open_date_time=date(2025, 6, 15),
    )


def test_open_lot_preserves_full_decimal_precision() -> None:
    """Cost with many decimal places must round-trip exactly — no
    float coercion, no premature rounding."""
    [lot], _ = _match(
        [_open(0, "2025-06-15 09:30:00", "0.25", "23.45678")],
        _prices(XQBB="95.40"),
    )
    assert lot.cost_basis_money == Decimal("23.45678")


def test_open_lots_keep_input_order() -> None:
    """Open lots come out in input order, even when FIFO consumes
    them in another order. Here row 1 is the earlier open, so the
    close drains it first."""
    open_lots, closed_lots = _match(
        [
            _open(0, "2025-03-01 09:00:00", "5", "500"),
            _open(1, "2025-01-01 09:00:00", "5", "400"),
            _close(2, "2025-06-01 09:00:00", "2", "200"),
            _open(
                3,
                "2025-02-01 09:00:00",
                "1",
                "90",
                instrument=_Instrument("BBB", "US99971J1051"),
            ),
        ],
        _prices(XQBB="95.40", BBB="95.40"),
    )
    [cl] = closed_lots
    assert cl.open_date_time == date(2025, 1, 1)
    assert [(str(lot.symbol), lot.open_date_time, lot.quantity) for lot in open_lots] == [
        ("XQBB", date(2025, 3, 1), Decimal("5")),
        ("XQBB", date(2025, 1, 1), Decimal("3")),
        ("BBB", date(2025, 2, 1), Decimal("1")),
    ]


def test_negative_quantity_opening_short_creates_short_lot() -> None:
    """#109: an opening SHORT survives as a Side.SHORT OpenLot with
    the "absolute value + Side enum" convention. Downstream
    overlapping_lots_for_cy filters it out of Schedule FA output."""
    [lot], _ = _match(
        [_open(0, "2025-06-15 09:30:00", "5", "426.20", side=Side.SHORT)],
        _prices(XQBB="95.40"),
    )
    assert lot.side == Side.SHORT
    assert lot.quantity == Decimal("5")  # absolute value
    assert lot.cost_basis_money == Decimal("426.20")  # absolute value
    # mark_price also positive (compute layer treats SHORT-lot
    # economics via the Side enum, not by sign).
    assert lot.mark_price == Decimal("95.40")


# ---- Dec 31 close-price wiring (issue #133) --------------------------------


def test_mark_price_uses_dec31_close_not_trade_day_close_price() -> None:
    """**The #133 regression test.** A surviving lot is marked at the
    Dec 31 close from Open Positions; events carry no trade-day
    ``C. Price`` at all.

    Scenario: a Feb-acquired lot held all year, Dec 31 close $100.
    Schedule FA Closing Balance must be qty * $100."""
    [lot], _ = _match(
        [_open(0, "2025-02-15 09:30:00", "10", "852.40")],
        _prices(XQBB="100"),
    )
    assert lot.mark_price == Decimal("100")
    # The Dec 31 close only affects mark_price.
    assert lot.cost_basis_money == Decimal("852.40")


def test_mark_price_degenerate_case_acquisition_on_dec_31() -> None:
    """Sanity: a lot acquired ON Dec 31 is marked at the same Dec 31
    close."""
    [lot], _ = _match(
        [_open(0, "2025-12-31 09:30:00", "5", "852.40")],
        _prices(XQBB="100"),
    )
    assert lot.mark_price == Decimal("100")


def test_symbol_in_trades_but_missing_from_dec31_lookup_raises() -> None:
    """If an opening trade SURVIVES (no closure consumes it) and the
    symbol is missing from Open Positions, the Dec 31 close can't be
    looked up. Refuse loudly with a #133 pointer. The lookup is only
    consulted for surviving opens."""
    with pytest.raises(LotMatchingError) as exc:
        _match([_open(0, "2025-06-15 09:30:00", "10", "852.40")], {})
    assert exc.value.row_index == 0
    assert str(exc.value) == _rendered(0, _NO_DEC31_CLOSE_MESSAGE)


# ---- FIFO closure matching (issue #104) ------------------------------------


_XQBL = _Instrument("XQBL", "US99995R1095", "SAMPLE ISSUER BL CORP", "NYSE")


def test_fifo_full_drain_multi_open_matches_basis_split() -> None:
    """Two opens (30 + 25 shares) drained by a single close for 55
    shares. Asserts two ClosedLot slices, basis split proportionally
    to the per-open basis, and no surviving open lot."""
    opens, closes = _match(
        [
            _open(0, "2025-11-10 10:15:00", "30", "372.00", instrument=_XQBL),
            _open(1, "2025-11-12 11:20:00", "25", "320.00", instrument=_XQBL),
            # |Basis| 692.00 + Realized P/L 324.40 (55 x 18.50 = 1017.50
            # gross, less 1.10 commission).
            _close(
                2,
                "2025-12-08 14:05:00",
                "55",
                "1016.40",
                trade_price="18.50",
                instrument=_XQBL,
            ),
        ],
        {},
    )
    assert opens == []
    assert len(closes) == 2
    assert all(str(c.symbol) == "XQBL" for c in closes)
    # First slice drains the first open (30 shares), second slice the
    # second open (25 shares).
    assert closes[0].quantity == Decimal("30")
    assert closes[0].cost_basis_money == Decimal("372.00")
    assert closes[1].quantity == Decimal("25")
    assert closes[1].cost_basis_money == Decimal("320.00")
    # Sum of slice basis equals |C row Basis|.
    assert closes[0].cost_basis_money + closes[1].cost_basis_money == Decimal("692.00")
    # Sum of slice realized PnL equals C row Realized P/L.
    total_realized = sum((c.proceeds_money - c.cost_basis_money for c in closes), Decimal(0))
    assert abs(total_realized - Decimal("324.40")) <= Decimal("0.0000001")


def test_fifo_partial_drain_emits_closed_slice_and_residual_open() -> None:
    """Closure consumes only part of a single open; expect one
    ClosedLot slice plus one surviving OpenLot with the residual
    quantity and proportional residual basis."""
    opens, closes = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "1000.00"),
            # |Basis| 600.00 + Realized P/L 120.
            _close(1, "2025-09-15 09:30:00", "6", "720.00"),
        ],
        _prices(XQBB="130"),
    )
    [cl] = closes
    assert cl.quantity == Decimal("6")
    assert cl.cost_basis_money == Decimal("600")
    assert cl.proceeds_money - cl.cost_basis_money == Decimal("120")
    assert cl.proceeds_money == Decimal("720")
    [op] = opens
    assert op.quantity == Decimal("4")
    assert op.cost_basis_money == Decimal("400")
    assert op.mark_price == Decimal("130")


def test_fifo_closure_spans_two_opens_respects_input_order() -> None:
    """3 opens, 1 closure spanning the first two; assert FIFO
    drains open #1 fully then takes part of open #2, leaving open
    #3 intact."""
    opens, closes = _match(
        [
            _open(0, "2025-01-10 09:30:00", "5", "500"),
            _open(1, "2025-02-10 09:30:00", "5", "600"),
            _open(2, "2025-03-10 09:30:00", "5", "700"),
            # |Basis| 740 + Realized P/L 70.
            _close(3, "2025-06-10 09:30:00", "7", "810"),
        ],
        _prices(XQBB="100"),
    )
    assert len(closes) == 2
    # Slice 1: full first open (5 shares, basis 500)
    assert closes[0].quantity == Decimal("5")
    assert closes[0].cost_basis_money == Decimal("500")
    # Slice 2: 2 shares of second open at 600/5 * 2 = 240
    assert closes[1].quantity == Decimal("2")
    assert closes[1].cost_basis_money == Decimal("240")
    # Surviving: 3 shares of open #2 + 5 shares of open #3 = 2 OpenLots
    assert [o.quantity for o in opens] == [Decimal("3"), Decimal("5")]


def test_fifo_uses_actual_open_cost_ignoring_broker_close_basis() -> None:
    """India taxes each disposal on the ACTUAL FIFO acquisition cost.
    The close event carries only net proceeds (here the broker's close
    ``|Basis|`` 450 + Realized P/L 10 = 460), so the 5-share slice's
    cost is 500 (5 x the open's 100 per share), NOT 450.
    ``tests/test_parser_activity_statement_trades.py`` pins that the
    parser carries no close-side cost."""
    _open_lots, closed_lots = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "1000"),
            _close(1, "2025-09-15 09:30:00", "5", "460.00"),
        ],
        _prices(XQBB="100"),
    )
    [lot] = closed_lots
    # Actual FIFO acquisition cost (5 x 100) — NOT the broker close |Basis| (450).
    assert lot.cost_basis_money == Decimal("500")
    # Proceeds = (|Basis| + Realized P/L) = 460, matching-independent.
    assert lot.proceeds_money == Decimal("460")
    assert lot.proceeds_money - lot.cost_basis_money == Decimal("-40")


def test_fifo_insufficient_opens_names_the_row_not_the_symbol_or_quantities() -> None:
    """Close wants 20 shares but only 10 shares of opens are
    available; the error names the row and points at a truncated FII
    section, but not the symbol or the share counts (#1047)."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(0, "2025-06-15 09:30:00", "10", "1000"),
                _close(1, "2025-09-15 09:30:00", "20", "2000"),
            ],
            _prices(XQBB="95.40"),
        )
    assert exc.value.row_index == 1
    assert str(exc.value) == _rendered(1, _SHORTFALL_MESSAGE)
    # The old text printed the symbol, the 20 shares closed, the 10
    # available and the 10 unmatched.
    _assert_absent(str(exc.value), "XQBB", "10", "20")


def test_close_without_any_open_points_at_truncated_input() -> None:
    """A close with no opens at all gets the shortfall refusal, which
    points the reader at a truncated statement without printing the
    zero available shares (#1047)."""
    with pytest.raises(LotMatchingError) as exc:
        _match([_close(0, "2025-06-15 09:30:00", "10", "852.40")], _prices(XQBB="95.40"))
    assert exc.value.row_index == 0
    assert str(exc.value) == _rendered(0, _SHORTFALL_MESSAGE)
    assert "share(s)" not in str(exc.value)


def test_fifo_survivor_dec31_lookup_skipped_for_fully_closed_symbols() -> None:
    """Two symbols, one fully closed and one surviving. Only the
    surviving symbol requires a dec31 entry; the fully-closed one
    legitimately drops out of Open Positions."""
    a = _Instrument("A", "US99971J1050")
    b = _Instrument("B", "US99971J1051")
    opens, closes = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "100", instrument=a),
            _close(1, "2025-09-15 09:30:00", "10", "100", instrument=a),
            _open(2, "2025-06-15 09:30:00", "5", "50", instrument=b),
        ],
        # Only B (survivor) has a Dec 31 close; A must not need one.
        _prices(B="12"),
    )
    [closed] = closes
    assert str(closed.symbol) == "A"
    [opened] = opens
    assert str(opened.symbol) == "B"


def test_fifo_survivor_missing_from_dec31_raises_133_pointer() -> None:
    """If a SURVIVING open has no dec31 entry, raise with #133."""
    with pytest.raises(LotMatchingError) as exc:
        _match([_open(0, "2025-06-15 09:30:00", "10", "1000")], {})
    assert str(exc.value) == _rendered(0, _NO_DEC31_CLOSE_MESSAGE)


def test_closed_lot_field_shape_carries_instrument_data_verbatim() -> None:
    """ClosedLot fields must match the open they were spawned
    from: issuer_country_code derived from ISIN[:2], dates as
    ``date`` (not datetime), description verbatim, no stated
    security class."""
    cayman = _Instrument("XQBM", "KY99995S1077", "SAMPLE ISSUER BM LTD")
    _, closes = _match(
        [
            _open(0, "2025-06-15 09:30:00", "7", "1400.70", instrument=cayman),
            # |Basis| 1400.70 + Realized P/L -378.00.
            _close(1, "2025-12-08 15:10:00", "7", "1022.70", instrument=cayman),
        ],
        {},
    )
    [cl] = closes
    assert cl.issuer_country_code == CountryCode("KY")  # Cayman from ISIN prefix
    assert cl.description == "SAMPLE ISSUER BM LTD"
    assert cl.sub_category is None
    assert cl.isin == Isin("KY99995S1077")
    assert cl.currency_primary == "USD"
    assert cl.listing_exchange == "NASDAQ"
    # Dates: ``date`` (not datetime), time component discarded.
    assert cl.open_date_time == date(2025, 6, 15)
    assert cl.close_date_time == date(2025, 12, 8)
    assert not hasattr(cl.open_date_time, "hour")


def test_fifo_multiple_closures_skip_already_consumed_opens() -> None:
    """When the first closure fully drains an open, a subsequent
    closure on the same symbol must skip that drained open and
    move to the next one."""
    opens, closes = _match(
        [
            _open(0, "2025-02-01 09:30:00", "5", "500"),
            _open(1, "2025-03-01 09:30:00", "5", "600"),
            # First closure fully drains open #1 (|Basis| 500 + P/L 50).
            _close(2, "2025-06-01 09:30:00", "5", "550"),
            # Second closure must skip drained open #1 and consume open #2
            # (|Basis| 360 + P/L 30).
            _close(3, "2025-07-01 09:30:00", "3", "390"),
        ],
        _prices(XQBB="130"),
    )
    assert len(closes) == 2
    # Second closure drained 3 shares of open #2 (basis 600/5 * 3 = 360).
    assert closes[1].quantity == Decimal("3")
    assert closes[1].cost_basis_money == Decimal("360")
    # Surviving open: 2 shares of open #2.
    [op] = opens
    assert op.quantity == Decimal("2")
    assert op.cost_basis_money == Decimal("240")


def test_closing_buy_against_short_open_rejected_109_pointer() -> None:
    """A closing BUY (buy-to-cover) of an opening SHORT is refused with
    a #109 pointer naming the row, not the symbol or the Quantity
    (#1047)."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(0, "2025-06-15 09:30:00", "10", "1000", side=Side.SHORT),
                _close(1, "2025-09-15 09:30:00", "5", "500", side=Side.SHORT),
            ],
            _prices(XQBB="95.40"),
        )
    assert exc.value.row_index == 1
    assert str(exc.value) == _rendered(1, _SHORT_COVER_MESSAGE)
    _assert_absent(str(exc.value), "XQBB", "Quantity=")


@pytest.mark.parametrize(
    "opens",
    [
        pytest.param([], id="no-opens"),
        pytest.param([_open(0, "2025-06-15 09:30:00", "10", "1000")], id="long-open"),
    ],
)
def test_closing_buy_is_refused_before_matching(opens: list[ExecutionEvent]) -> None:
    """A closing BUY with no SHORT open before it is still a #109
    refusal: not a shortfall against no opens, and never matched
    against a LONG open."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [*opens, _close(1, "2025-09-15 09:30:00", "5", "500", side=Side.SHORT)],
            _prices(XQBB="95.40"),
        )
    assert exc.value.row_index == 1
    assert str(exc.value) == _rendered(1, _SHORT_COVER_MESSAGE)


def test_fifo_negative_qty_closure_against_short_open_raises_109() -> None:
    """Defence-in-depth: a sell close whose FIFO walk reaches a SHORT
    open is refused with a #109 pointer."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(0, "2025-06-15 09:30:00", "10", "1000", side=Side.SHORT),
                _close(1, "2025-09-15 09:30:00", "5", "500"),
            ],
            _prices(XQBB="95.40"),
        )
    assert exc.value.row_index == 1
    assert str(exc.value) == _rendered(1, _short_open_message(1, 0))
    assert "XQBB" not in str(exc.value)


def test_short_open_refusal_names_both_rows_and_not_the_symbol() -> None:
    """#1047: the SHORT-opening refusal keeps both locations, the
    closing row and the opening SHORT row, and drops only the symbol,
    here a column-shifted cell holding a name and an account."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(
                    4,
                    "2025-03-01 09:30:00",
                    "1234567.89",
                    "1000",
                    instrument=_LEAKY,
                    side=Side.SHORT,
                ),
                _close(9, "2025-06-02 09:30:00", "1234.5", "500", instrument=_LEAKY),
            ],
            {},
        )
    assert exc.value.row_index == 9
    assert str(exc.value) == (
        "LotMatchingError in section 'Trades' at row 9: the closing trade at row 9 "
        "matched FIFO-first against an opening SHORT lot at row 4. v1 does not "
        "support closing SHORT positions on Schedule FA; see "
        "https://github.com/jay-tau/ibkr-fa/issues/109."
    )
    _assert_absent(str(exc.value), *_LEAKY_VALUES)


def test_fifo_close_never_reaches_a_later_short_open() -> None:
    """A close only ever reaches the opens before it, so a SHORT open
    after it is not a #109 refusal: the close is short of earlier
    opens."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(0, "2025-01-10 09:30:00", "5", "500"),
                _close(1, "2025-06-10 09:30:00", "10", "1000"),
                _open(2, "2025-09-10 09:30:00", "3", "300", side=Side.SHORT),
            ],
            _prices(XQBB="95.40"),
        )
    assert exc.value.row_index == 1
    assert str(exc.value) == _rendered(1, _SHORTFALL_MESSAGE)
    assert "issues/109" not in str(exc.value)


def test_fifo_per_slice_realized_uses_uniform_close_price_not_quantity_proportion() -> None:
    """Tier-1 PR #161 correctness review (Claude Opus 4.7 xhigh):
    when matched opens have different per-share basis, the per-slice
    proceeds_money, and so the realized PnL, must be derived from the
    closure's uniform close-per-share price, not from a
    quantity-proportional split of the total realized PnL.

    Setup: open #1 = 6 shares @ basis 600 (= $100/share), open #2 =
    4 shares @ basis 440 (= $110/share). Closure: 10 shares closed at
    a uniform $124/share (total proceeds = $1240; basis relieved =
    $1040; realized PnL = $200).

    Per-share close = $124. Correct per-slice values:
      slice 1 (6 sh): proceeds = 744, basis = 600, realized = 144
      slice 2 (4 sh): proceeds = 496, basis = 440, realized =  56

    Wrong (quantity-proportional realized) values:
      slice 1 (6 sh): realized = 200 * 6/10 = 120, proceeds = 720
      slice 2 (4 sh): realized = 200 * 4/10 = 80,  proceeds = 520
    """
    _, closes = _match(
        [
            _open(0, "2025-03-01 10:00:00", "6", "600"),
            _open(1, "2025-04-01 10:00:00", "4", "440"),
            _close(2, "2025-09-01 10:00:00", "10", "1240"),
        ],
        {},
    )
    [s1, s2] = closes
    assert s1.quantity == Decimal("6")
    assert s1.cost_basis_money == Decimal("600")
    assert s1.proceeds_money == Decimal("744")
    assert s1.proceeds_money - s1.cost_basis_money == Decimal("144")
    assert s2.quantity == Decimal("4")
    assert s2.cost_basis_money == Decimal("440")
    assert s2.proceeds_money == Decimal("496")
    assert s2.proceeds_money - s2.cost_basis_money == Decimal("56")
    # Sum identities preserved.
    assert s1.proceeds_money + s2.proceeds_money == Decimal("1240")
    assert s1.proceeds_money + s2.proceeds_money - s1.cost_basis_money - s2.cost_basis_money == (
        Decimal("200")
    )


def test_fifo_loss_case_proceeds_below_basis() -> None:
    """A loss: 10 shares opened at $100/share (basis 1000), closed at
    $90/share (net proceeds 1000 + -100 = 900). Expected slice:
    quantity 10, cost_basis 1000, proceeds 900, a realized loss of
    100."""
    _, closes = _match(
        [
            _open(0, "2025-03-01 10:00:00", "10", "1000"),
            _close(1, "2025-09-01 10:00:00", "10", "900"),
        ],
        {},
    )
    [cl] = closes
    assert cl.quantity == Decimal("10")
    assert cl.cost_basis_money == Decimal("1000")
    assert cl.proceeds_money == Decimal("900")
    assert cl.proceeds_money - cl.cost_basis_money == Decimal("-100")


def test_fifo_refuses_to_match_close_against_later_same_day_open() -> None:
    """Tier-1 PR #161 quality review (GPT-5.5): a closing trade at
    09:30 must not be matched against an opening trade at 10:30 on
    the same day. The matcher surfaces the insufficient-opens
    refusal instead of allocating against a later open."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _close(0, "2025-06-15 09:30:00", "5", "700"),
                _open(1, "2025-06-15 10:30:00", "10", "1000"),
            ],
            _prices(XQBB="130"),
        )
    assert exc.value.row_index == 0
    assert str(exc.value) == _rendered(0, _SHORTFALL_MESSAGE)
    assert "XQBB" not in str(exc.value)


def test_fifo_refuses_to_match_close_against_same_second_later_row_open() -> None:
    """Tier-1 PR #161 r2 quality review (GPT-5.5): a close and an
    open with the exact same Date/Time must NOT be matched when the
    close appears first in input row order. The (date_time,
    row_index) tuple ordering catches this tie."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _close(0, "2025-06-15 09:30:00", "5", "700"),
                _open(1, "2025-06-15 09:30:00", "10", "1000"),
            ],
            _prices(XQBB="130"),
        )
    assert exc.value.row_index == 0
    assert str(exc.value) == _rendered(0, _SHORTFALL_MESSAGE)
    assert "XQBB" not in str(exc.value)


def test_closed_lots_group_by_symbol_in_first_close_input_order() -> None:
    """Closed lots come out per symbol, in the order each symbol's
    first close appears in the input; within a symbol, closes run in
    ``(datetime, row)`` order. B's close is the first close row, so
    B's slices come first even though A's close is earlier in time."""
    a = _Instrument("A", "US99971J1050")
    b = _Instrument("B", "US99971J1051")
    _, closes = _match(
        [
            _open(0, "2025-01-01 09:30:00", "4", "40", instrument=a),
            _open(1, "2025-01-01 09:30:00", "4", "80", instrument=b),
            _close(2, "2025-08-01 09:30:00", "1", "30", instrument=b),
            _close(3, "2025-03-01 09:30:00", "1", "10", instrument=a),
            _close(4, "2025-05-01 09:30:00", "1", "20", instrument=b),
        ],
        _prices(A="1", B="1"),
    )
    assert [(str(cl.symbol), cl.close_date_time) for cl in closes] == [
        ("B", date(2025, 5, 1)),
        ("B", date(2025, 8, 1)),
        ("A", date(2025, 3, 1)),
    ]


def test_fifo_slice_keeps_the_open_quantity_exponent_on_a_tie() -> None:
    """When a close takes exactly what an open holds, the slice keeps
    the open's Decimal exponent, as the pre-#640 parser did."""
    _, [cl] = _match(
        [
            _open(0, "2025-06-15 09:30:00", "2.0", "100"),
            _close(1, "2025-09-15 09:30:00", "2", "120"),
        ],
        {},
    )
    assert str(cl.quantity) == "2.0"


def test_match_fifo_rejects_duplicate_row_indices() -> None:
    with pytest.raises(InvariantError, match="one event per Trades row index"):
        _match(
            [
                _open(0, "2025-06-15 09:30:00", "10", "100"),
                _open(0, "2025-06-16 09:30:00", "10", "100"),
            ],
            _prices(XQBB="1"),
        )


def test_lot_matching_error_names_the_trades_row() -> None:
    assert (
        str(LotMatchingError(row_index=3, message="refused"))
        == "LotMatchingError in section 'Trades' at row 3: refused"
    )


@pytest.mark.parametrize(
    ("events", "row_index", "message"),
    [
        pytest.param(
            [
                _open(0, "2025-03-01 09:30:00", "1234.5", "1000", instrument=_LEAKY),
                _close(1, "2025-06-02 09:30:00", "1234567.89", "2000", instrument=_LEAKY),
            ],
            1,
            _SHORTFALL_MESSAGE,
            id="shortfall",
        ),
        pytest.param(
            [
                _open(0, "2025-03-01 09:30:00", "1234.5", "1000", instrument=_LEAKY),
                _close(
                    1,
                    "2025-06-02 09:30:00",
                    "1234567.89",
                    "2000",
                    instrument=_LEAKY,
                    side=Side.SHORT,
                ),
            ],
            1,
            _SHORT_COVER_MESSAGE,
            id="closing-buy",
        ),
        pytest.param(
            [
                _open(0, "2025-03-01 09:30:00", "1234567.89", "1000", instrument=_LEAKY),
                _close(1, "2025-06-02 09:30:00", "1234.5", "500", instrument=_LEAKY),
            ],
            0,
            _NO_DEC31_CLOSE_MESSAGE,
            id="no-dec31-close",
        ),
    ],
)
def test_lot_matching_refusals_print_no_row_values(
    events: list[ExecutionEvent], row_index: int, message: str
) -> None:
    """Each refusal is its exact fixed text after the Trades-row prefix
    (#1047); the SHORT-opening one has its own test above."""
    with pytest.raises(LotMatchingError) as exc:
        _match(events, {})
    assert exc.value.row_index == row_index
    assert str(exc.value) == _rendered(row_index, message)
    _assert_absent(str(exc.value), *_LEAKY_VALUES)


# ---- exact quantities and shares (#1273) ------------------------------------


def _exact_cost(lot: OpenLot | ClosedLot) -> Fraction:
    """The lot's exact cost: its rational where it carries one, else its Decimal."""
    exact = lot.cost_basis_money_exact
    return Fraction(lot.cost_basis_money) if exact is None else exact


def _exact_proceeds(lot: ClosedLot) -> Fraction:
    """The slice's exact proceeds: its rational where it carries one, else its Decimal."""
    exact = lot.proceeds_money_exact
    return Fraction(lot.proceeds_money) if exact is None else exact


def test_fifo_sale_past_a_30_digit_holding_refuses_with_the_shortfall() -> None:
    """Issue #1273 Example 3. Fails on 0d3547b: 10^30 - 1 rounded back to
    10^30, so the second sale matched in full: 10^30 + 1 shares sold from a
    10^30 purchase, with slice costs adding to more than $1,000.00."""
    with pytest.raises(LotMatchingError) as exc:
        _match(
            [
                _open(0, "2025-03-01 09:30:00", _TEN_TO_30, "1000.00"),
                _close(1, "2025-06-02 09:30:00", "1", "1.00"),
                _close(2, "2025-09-01 09:30:00", _TEN_TO_30, "2000.00"),
            ],
            _prices(XQBB="1"),
        )
    assert exc.value.row_index == 2
    assert str(exc.value) == _rendered(2, _SHORTFALL_MESSAGE)


def test_fifo_survivor_of_a_30_digit_holding_keeps_the_exact_remainder() -> None:
    """Issue #1273 Example 3 with only the 1-share sale. Fails on 0d3547b:
    the surviving lot kept 10^30 shares at the full $1,000.00 cost."""
    [survivor], [sold] = _match(
        [
            _open(0, "2025-03-01 09:30:00", _TEN_TO_30, "1000.00"),
            _close(1, "2025-06-02 09:30:00", "1", "1.00"),
        ],
        _prices(XQBB="1"),
    )
    assert sold.quantity.compare_total(Decimal(1)) == 0
    assert survivor.quantity.compare_total(Decimal("9" * 30)) == 0
    assert _exact_cost(survivor) == 1000 - Fraction(1, 10**27)
    assert _exact_cost(sold) + _exact_cost(survivor) == 1000


def test_fifo_split_sale_proceeds_and_costs_add_back_exactly() -> None:
    """Issue #1273 split sale: $10.00 for 3 shares against 1- and 2-share
    buys. Fails on 0d3547b: the slices' proceeds were 28-digit thirds,
    3.333…3 and 6.666…6, adding to $9.999999999999999999999999999."""
    _, closes = _match(
        [
            _open(0, "2025-03-01 09:30:00", "1", "2.15"),
            _open(1, "2025-04-01 09:30:00", "2", "4.31"),
            _close(2, "2025-09-01 09:30:00", "3", "10.00"),
        ],
        {},
    )
    assert [lot.quantity for lot in closes] == [Decimal(1), Decimal(2)]
    assert sum(_exact_proceeds(lot) for lot in closes) == 10
    assert [_exact_cost(lot) for lot in closes] == [Fraction("2.15"), Fraction("4.31")]


def test_fifo_even_shares_keep_the_decimal_text_and_carry_no_rational() -> None:
    """Control (passes on 0d3547b): a share that divides evenly keeps
    main's Decimal, exponent included, and no rational. 2 x (1.00 / 8)
    is 0.250, where 1.00 x 2 / 8 would be 0.25; 1.000 x 2 / 8 is 0.250,
    where the exact quarter's Decimal would be 0.25."""
    opens, closes = _match(
        [
            _open(0, "2025-03-01 09:30:00", "2", "2.00"),
            _open(1, "2025-03-02 09:30:00", "6", "6.00"),
            _close(2, "2025-06-02 09:30:00", "8", "1.00"),
            _open(3, "2025-07-01 09:30:00", "8", "1.000"),
            _close(4, "2025-09-01 09:30:00", "2", "0.50"),
        ],
        _prices(XQBB="1"),
    )
    assert [
        (str(lot.quantity), str(lot.cost_basis_money), str(lot.proceeds_money)) for lot in closes
    ] == [("2", "2.00", "0.250"), ("6", "6.00", "0.750"), ("2", "0.250", "0.50")]
    assert [(str(lot.quantity), str(lot.cost_basis_money)) for lot in opens] == [("6", "0.750")]
    assert all(
        lot.cost_basis_money_exact is None and lot.proceeds_money_exact is None for lot in closes
    )
    assert all(lot.cost_basis_money_exact is None for lot in opens)


def test_fifo_third_shares_carry_the_exact_rational() -> None:
    """Fails on 0d3547b: a third's cost and proceeds were 28-digit
    half-even Decimals with no rational (the 2-share cost was
    6.666666666666666666666666667). Now each is ``exact_decimal``'s
    ROUND_DOWN cut of the exact share, which it carries as well."""
    opens, closes = _match(
        [
            _open(0, "2025-03-01 09:30:00", "1", "1.00"),
            _open(1, "2025-04-01 09:30:00", "3", "10.00"),
            _close(2, "2025-09-01 09:30:00", "3", "10.00"),
        ],
        _prices(XQBB="1"),
    )
    third, two_thirds = "3.333333333333333333333333333", "6.666666666666666666666666666"
    assert (str(exact_decimal(Fraction(10, 3))), str(exact_decimal(Fraction(20, 3)))) == (
        third,
        two_thirds,
    )
    assert [
        (
            str(lot.cost_basis_money),
            lot.cost_basis_money_exact,
            str(lot.proceeds_money),
            lot.proceeds_money_exact,
        )
        for lot in closes
    ] == [
        ("1.00", None, third, Fraction(10, 3)),
        (two_thirds, Fraction(20, 3), two_thirds, Fraction(20, 3)),
    ]
    [survivor] = opens
    assert (str(survivor.quantity), str(survivor.cost_basis_money)) == ("1", third)
    assert survivor.cost_basis_money_exact == Fraction(10, 3)


_SHARE_CAP_MESSAGE = f"a lot's cost or proceeds share {TOO_MANY_DIGITS}"
_PAST_SHARE_CAP = "1E-1001"  # more places than exact_fraction takes


@pytest.mark.parametrize(
    ("events", "row_index"),
    [
        pytest.param(
            [
                _open(4, "2025-03-01 09:30:00", "1234567.89", _PAST_SHARE_CAP, instrument=_LEAKY),
                _close(9, "2025-06-02 09:30:00", "1234.5", "500", instrument=_LEAKY),
            ],
            9,
            id="slice-cost-at-the-close",
        ),
        pytest.param(
            [
                _open(4, "2025-03-01 09:30:00", "1234567.89", "1000", instrument=_LEAKY),
                _close(9, "2025-06-02 09:30:00", "1234.5", _PAST_SHARE_CAP, instrument=_LEAKY),
            ],
            9,
            id="slice-proceeds-at-the-close",
        ),
        pytest.param(
            [
                _open(4, "2025-03-01 09:30:00", "1234.5", "1000", instrument=_LEAKY),
                _open(6, "2025-03-01 09:30:00", "1234567.89", _PAST_SHARE_CAP, instrument=_LEAKY),
                _close(9, "2025-06-02 09:30:00", "1234.5", "500", instrument=_LEAKY),
            ],
            6,
            id="open-lot-cost-at-the-open",
        ),
    ],
)
def test_fifo_share_past_the_digit_cap_refuses_at_its_trades_row(
    events: list[ExecutionEvent], row_index: int
) -> None:
    """Fails on 0d3547b: the share was divided at 28 digits and filed. A
    slice's share refuses at its closing row, a surviving lot's at its
    opening row (#1273), with fixed text and no chain."""
    with pytest.raises(LotMatchingError) as exc:
        _match(events, {Ticker(_LEAKY.symbol): Decimal("95.40")})
    error = exc.value
    assert type(error) is LotMatchingError
    assert error.row_index == row_index
    assert str(error) == _rendered(row_index, _SHARE_CAP_MESSAGE)
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error), str(error.args)):
        _assert_absent(text, *_LEAKY_VALUES[:-1], "E-1001", "95.40")


def test_fifo_gross_proceeds_keep_every_digit_of_the_product() -> None:
    """Fails on 0d3547b: 3 x 9.999999999999999999999999999 rounded to
    30.00000000000000000000000000 at 28 digits."""
    _, [lot] = _match(
        [
            _open(0, "2025-03-01 09:30:00", "3", "30.00"),
            _close(
                1, "2025-06-02 09:30:00", "3", "29.99", trade_price="9.999999999999999999999999999"
            ),
        ],
        {},
    )
    assert lot.gross_proceeds_money is not None
    assert lot.gross_proceeds_money.compare_total(Decimal("29.999999999999999999999999997")) == 0


@pytest.mark.parametrize(
    ("events", "expected", "markers"),
    [
        pytest.param(
            [
                _open(4, "2025-03-01 09:30:00", "1E+600", "1000", instrument=_LEAKY),
                _close(9, "2025-06-02 09:30:00", "1E-500", "500", instrument=_LEAKY),
            ],
            f"FIFO lot matching, Trades row 9, column quantity: {INEXACT_SUM}",
            ("E+600", "E-500"),
            # The open's remainder 1E+600 - 1E-500 needs 1,101 digits.
            id="quantity",
        ),
        pytest.param(
            [
                _open(4, "2025-03-01 09:30:00", "1234567.89", "1000", instrument=_LEAKY),
                _close(
                    9,
                    "2025-06-02 09:30:00",
                    "1234.5",
                    "500",
                    trade_price="9" * 1000,
                    instrument=_LEAKY,
                ),
            ],
            f"FIFO lot matching, Trades row 9, column gross_proceeds_money: {INEXACT_PRODUCT}",
            ("1234567.89", "1234.5", "9" * 10),
            # 1234.5 x (10^1000 - 1) needs 1,005 digits.
            id="gross-proceeds",
        ),
    ],
)
def test_fifo_arithmetic_past_the_digit_cap_refuses_at_the_closing_row(
    events: list[ExecutionEvent], expected: str, markers: tuple[str, ...]
) -> None:
    """Fails on 0d3547b: the open's remainder or the gross proceeds
    rounded to 28 digits and filed. Each refusal is fixed text naming the
    closing row and column, with no context and no chain (#1273)."""
    with pytest.raises(InvariantError) as exc:
        _match(events, {Ticker(_LEAKY.symbol): Decimal("95.40")})
    error = exc.value
    assert str(error) == f"InvariantError: {expected}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error), str(error.args)):
        _assert_absent(text, *_LEAKY_VALUES[:5], *markers, "95.40")


@given(st.data())
def test_fifo_property_conserves_quantity_and_cost_and_never_matches_a_later_open(
    data: st.DataObject,
) -> None:
    """Random valid per-symbol sequences, with same-second ties and a
    shuffled input order. Each open's description and each close's
    ``T. Price`` name its row, so every slice can be traced back.

    Every open's slices and survivor add back exactly to its quantity and
    cost, and every close's slices to its quantity and net proceeds
    (#1273), with random net proceeds. Fails on 0d3547b: its 28-digit
    shares added back only within a gap, which this test once allowed up
    to $0.01 of cost, with every close's net proceeds $0."""
    steps: list[tuple[_Instrument, ExecutionKind, datetime, Decimal]] = []
    for instrument in (_XQBB, _Instrument("BBB", "US99971J1051")):
        when = datetime(2025, 1, 1, 9, 30)
        held = Decimal(0)
        for _ in range(data.draw(st.integers(min_value=1, max_value=6), label="steps")):
            when += data.draw(
                st.sampled_from([timedelta(0), timedelta(seconds=1), timedelta(days=1)])
            )
            if held > 0 and data.draw(st.booleans(), label="close"):
                quantity = data.draw(
                    st.decimals(min_value=Decimal("0.0001"), max_value=held, places=4)
                )
                held -= quantity
                steps.append((instrument, ExecutionKind.CLOSE, when, quantity))
            else:
                quantity = data.draw(
                    st.decimals(min_value=Decimal("0.0001"), max_value=Decimal(500), places=4)
                )
                held += quantity
                steps.append((instrument, ExecutionKind.OPEN, when, quantity))

    # Shuffle the input order, then give same-symbol same-time steps
    # their rows in step order: the row index breaks those ties.
    rows = list(data.draw(st.permutations(range(len(steps))), label="input order"))
    ties: dict[tuple[str, datetime], list[int]] = defaultdict(list)
    for step, (instrument, _kind, when, _quantity) in enumerate(steps):
        ties[instrument.symbol, when].append(step)
    for tied in ties.values():
        for step, row in zip(tied, sorted(rows[step] for step in tied), strict=True):
            rows[step] = row

    events: list[ExecutionEvent] = []
    costs: dict[int, Decimal] = {}
    nets: dict[int, Decimal] = {}
    for step, (instrument, kind, when, quantity) in enumerate(steps):
        row = rows[step]
        if kind is ExecutionKind.OPEN:
            costs[row] = data.draw(
                st.decimals(min_value=Decimal(0), max_value=Decimal(100_000), places=6)
            )
            events.append(
                _open(
                    row,
                    when.isoformat(),
                    str(quantity),
                    str(costs[row]),
                    instrument=_Instrument(instrument.symbol, instrument.isin, f"row {row}"),
                )
            )
        else:
            nets[row] = data.draw(
                st.decimals(min_value=Decimal("0.01"), max_value=Decimal(100_000), places=2)
            )
            events.append(
                _close(
                    row,
                    when.isoformat(),
                    str(quantity),
                    str(nets[row]),
                    trade_price=str(row + 1),
                    instrument=instrument,
                )
            )
    events.sort(key=lambda event: event.row_index)
    by_row = {event.row_index: event for event in events}

    open_lots, closed_lots = _match(events, _prices(XQBB="1", BBB="1"))

    # Per open (by its description) and per close (by T. Price), exactly.
    opened_quantity: dict[int, Fraction] = defaultdict(Fraction)
    opened_cost: dict[int, Fraction] = defaultdict(Fraction)
    closed_quantity: dict[int, Fraction] = defaultdict(Fraction)
    closed_proceeds: dict[int, Fraction] = defaultdict(Fraction)
    for lot in [*closed_lots, *open_lots]:
        opened_row = int(lot.description.removeprefix("row "))
        opened_quantity[opened_row] += Fraction(lot.quantity)
        opened_cost[opened_row] += _exact_cost(lot)
    for lot in closed_lots:
        assert lot.gross_proceeds_money is not None
        price = Fraction(lot.gross_proceeds_money) / Fraction(lot.quantity)
        assert price.denominator == 1
        closed_row = int(price) - 1
        closed_quantity[closed_row] += Fraction(lot.quantity)
        closed_proceeds[closed_row] += _exact_proceeds(lot)
        opened = by_row[int(lot.description.removeprefix("row "))]
        closed = by_row[closed_row]
        assert (opened.trade_date_time, opened.row_index) < (
            closed.trade_date_time,
            closed.row_index,
        )
    assert opened_quantity == {row: Fraction(by_row[row].quantity) for row in costs}
    assert opened_cost == {row: Fraction(cost) for row, cost in costs.items()}
    assert closed_quantity == {row: Fraction(by_row[row].quantity) for row in nets}
    assert closed_proceeds == {row: Fraction(net) for row, net in nets.items()}


# ---- broker-matched basis guard (#599, #640) --------------------------------


def _closed_lot(
    provenance: BasisProvenance, *, opened: date, holding: date | None = None
) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQBD"),
        description="SAMPLE ISSUER BD INC",
        isin=Isin("US99995A1060"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("4"),
        cost_basis_money=Decimal("100"),
        proceeds_money=Decimal("120"),
        open_date_time=opened,
        close_date_time=date(2026, 5, 1),
        basis_provenance=provenance,
        holding_period_date_time=holding,
        cost_basis_inr_override=(
            StatutoryInrCostBasis(amount_inr=Decimal("8000"), source="synthetic")
            if provenance is BasisProvenance.S49_2AA
            else None
        ),
    )


@pytest.mark.parametrize("holding", [date(2026, 3, 2), date(2026, 4, 21)])
def test_require_indian_basis_refuses_a_shifted_broker_matched_lot(holding: date) -> None:
    """The refusal is fixed text: it prints neither the lot's symbol and
    ISIN nor its open, holding-period and close dates (#1047)."""
    lots = [
        _closed_lot(BasisProvenance.ENGINE_FIFO, opened=date(2026, 1, 2)),
        _closed_lot(BasisProvenance.BROKER_MATCHED, opened=date(2026, 4, 20), holding=holding),
    ]
    with pytest.raises(BrokerBasisError) as exc:
        require_indian_basis(lots)
    assert str(exc.value) == BROKER_BASIS_MESSAGE
    _assert_absent(
        str(exc.value), "XQBD", "US99995A1060", "2026-04-20", holding.isoformat(), "2026-05-01"
    )


def test_require_indian_basis_accepts_unshifted_and_engine_lots() -> None:
    require_indian_basis(
        [
            _closed_lot(
                BasisProvenance.BROKER_MATCHED, opened=date(2026, 4, 20), holding=date(2026, 4, 20)
            ),
            _closed_lot(BasisProvenance.ENGINE_FIFO, opened=date(2026, 4, 20)),
            _closed_lot(BasisProvenance.S49_2AA, opened=date(2026, 4, 20)),
        ]
    )


def test_match_fifo_tags_lots_engine_fifo() -> None:
    _, [cl] = _match(
        [
            _open(0, "2025-06-15 09:30:00", "10", "100"),
            _close(1, "2025-09-15 09:30:00", "10", "120"),
        ],
        {},
    )
    assert cl.basis_provenance is BasisProvenance.ENGINE_FIFO
    assert cl.holding_period_date_time is None
