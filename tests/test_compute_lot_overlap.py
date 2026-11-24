"""Tests for :mod:`ibkr_fa.compute.lot_overlap`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute import CyOverlap, overlapping_lots_for_cy
from ibkr_fa.compute.lot_matching import BrokerBasisError
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._lot_matching_inputs import BROKER_BASIS_MESSAGE


def make_open_lot(
    *,
    open_date: date = date(2024, 6, 15),
    side: Side = Side.LONG,
    symbol: str = "XQAW",
) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker(symbol),
        description=f"{symbol} (test)",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        cost_basis_money=Decimal("800"),
        side=side,
        open_date_time=open_date,
    )


def make_closed_lot(
    *,
    open_date: date,
    close_date: date,
    symbol: str = "XQAT",
) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker(symbol),
        description=f"{symbol} (test)",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("5"),
        cost_basis_money=Decimal("274.60"),
        proceeds_money=Decimal("261.75"),
        open_date_time=open_date,
        close_date_time=close_date,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


# ---- empty inputs ----------------------------------------------------------


def test_empty_inputs_return_empty_overlap() -> None:
    result = overlapping_lots_for_cy([], [], calendar_year=2025)
    assert result == CyOverlap(open_lots=[], closed_lots=[])


# ---- open-lot filtering ----------------------------------------------------


def test_long_open_lot_within_cy_kept() -> None:
    lot = make_open_lot(open_date=date(2025, 6, 15))
    result = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    assert result.open_lots == [lot]
    assert result.closed_lots == []


def test_long_open_lot_acquired_in_prior_year_kept() -> None:
    """A lot acquired before CY but still open at year-end (i.e., the
    pipeline's contract guarantees open_date_time <= ReportDate ==
    Dec 31 of CY) overlaps CY."""
    lot = make_open_lot(open_date=date(2020, 1, 6))
    result = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    assert result.open_lots == [lot]


def test_short_open_lot_dropped() -> None:
    """SHORT positions are liabilities, not foreign assets."""
    long_lot = make_open_lot(side=Side.LONG, symbol="AAA")
    short_lot = make_open_lot(side=Side.SHORT, symbol="BBB")
    result = overlapping_lots_for_cy([long_lot, short_lot], [], calendar_year=2025)
    assert result.open_lots == [long_lot]


def test_open_lot_acquired_after_cy_excluded() -> None:
    """PR #122 (correctness invariants): the malformed shape this
    test originally guarded against — an OpenLot whose
    open_date_time is after its report_date — is now rejected at
    construction time by OpenLot.__post_init__. The
    overlapping_lots_for_cy filter is therefore defended at a
    stricter layer; this test now verifies the construction itself
    is impossible."""
    with pytest.raises(InvariantError) as exc:
        # Default factory report_date is Dec 31 of 2025; open_date
        # in 2026 violates the open<=report invariant.
        make_open_lot(open_date=date(2026, 1, 5))
    assert "open_date_time must be on or before report_date" in exc.value.what


def test_open_lot_acquired_on_dec_31_of_cy_kept() -> None:
    """Inclusive boundary: a lot acquired on Dec 31 of CY overlapped
    CY for one day and is in scope."""
    lot = make_open_lot(open_date=date(2025, 12, 31))
    result = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    assert result.open_lots == [lot]


def test_open_lots_input_order_preserved() -> None:
    a = make_open_lot(symbol="AAA", open_date=date(2024, 1, 1))
    b = make_open_lot(symbol="BBB", open_date=date(2025, 6, 1))
    c = make_open_lot(symbol="CCC", open_date=date(2025, 11, 1))
    result = overlapping_lots_for_cy([a, b, c], [], calendar_year=2025)
    assert [lot.symbol for lot in result.open_lots] == ["AAA", "BBB", "CCC"]


# ---- closed-lot filtering --------------------------------------------------


def test_closed_lot_entirely_within_cy_kept() -> None:
    lot = make_closed_lot(open_date=date(2025, 3, 1), close_date=date(2025, 9, 1))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == [lot]


def test_closed_lot_spanning_year_boundary_kept() -> None:
    """Acquired in 2024, closed in 2025 — overlapped CY=2025."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2025, 3, 15))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == [lot]


def test_closed_lot_entirely_in_prior_year_excluded() -> None:
    """Acquired and closed in 2024 — no overlap with CY=2025."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2024, 11, 1))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lot_entirely_in_future_year_excluded() -> None:
    lot = make_closed_lot(open_date=date(2026, 2, 1), close_date=date(2026, 6, 1))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lot_disposed_after_cy_excluded_to_avoid_double_count() -> None:
    """Codex P2 catch: a lot acquired before CY and disposed AFTER CY
    end was being included in closed_overlap. But the same holding is
    in the Dec-31 open-positions snapshot too (because it was open at
    year-end). Including both would double-count downstream. The
    closed-lot predicate now caps close_date_time at Dec 31 of CY so
    such a lot is represented exactly once via the open-lot path."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2026, 3, 15))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lot_closed_on_jan_1_of_cy_kept() -> None:
    """Inclusive boundary: a lot closed on Jan 1 of CY overlapped CY
    for one day and is in scope."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2025, 1, 1))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == [lot]


def test_closed_lot_opened_on_dec_31_of_cy_excluded_to_avoid_double_count() -> None:
    """A lot opened Dec 31 of CY and closed in a later year would be
    in BOTH the year-end open snapshot AND a closed-lot row whose
    [open, close] interval intersects CY. The closed-lot cap at
    cy_end drops it here so the open-lot path is the single source.
    A lot opened Dec 31 of CY and closed the SAME day is the only
    case where Dec-31-open+closed-in-CY can co-exist; that is
    correctly retained (covered by
    test_closed_lot_closed_on_dec_31_of_cy_kept)."""
    lot = make_closed_lot(open_date=date(2025, 12, 31), close_date=date(2026, 1, 5))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lot_closed_on_dec_31_of_prior_year_excluded() -> None:
    """Off-by-one: closed Dec 31 2024 means LAST overlap was 2024;
    CY=2025 sees no overlap."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2024, 12, 31))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lot_closed_on_dec_31_of_cy_kept() -> None:
    """Inclusive boundary on the cy_end side: closed exactly on Dec 31
    of CY → still in scope (and not in the year-end open snapshot)."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2025, 12, 31))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == [lot]


def test_closed_lot_closed_on_jan_1_of_next_year_excluded() -> None:
    """Off-by-one on the cy_end side: closed Jan 1 of next year is
    AFTER CY. The open snapshot (Dec 31 of CY) carries this lot, so
    excluding from closed_overlap prevents double counting."""
    lot = make_closed_lot(open_date=date(2024, 6, 1), close_date=date(2026, 1, 1))
    result = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert result.closed_lots == []


def test_closed_lots_input_order_preserved() -> None:
    a = make_closed_lot(symbol="AAA", open_date=date(2024, 1, 1), close_date=date(2025, 3, 1))
    b = make_closed_lot(symbol="BBB", open_date=date(2025, 4, 1), close_date=date(2025, 9, 1))
    c = make_closed_lot(symbol="CCC", open_date=date(2025, 10, 1), close_date=date(2025, 12, 1))
    result = overlapping_lots_for_cy([], [a, b, c], calendar_year=2025)
    assert [lot.symbol for lot in result.closed_lots] == ["AAA", "BBB", "CCC"]


# ---- combined ---------------------------------------------------------------


def test_combined_inputs_partition_correctly() -> None:
    """Mixed input: some open lots (long+short, in/out of window) and
    some closed lots (in/out of overlap). Verify both partitions
    apply their respective predicates independently.

    Note: a future-open lot (open_date > report_date) can no longer
    be constructed since PR #122 added that invariant to OpenLot;
    the future-lot branch is covered separately by
    test_open_lot_acquired_after_cy_excluded."""
    open_long_in = make_open_lot(symbol="OL_IN", open_date=date(2025, 5, 1))
    open_short = make_open_lot(symbol="OL_SHORT", side=Side.SHORT)
    closed_in = make_closed_lot(
        symbol="CL_IN", open_date=date(2024, 6, 1), close_date=date(2025, 3, 1)
    )
    closed_old = make_closed_lot(
        symbol="CL_OLD", open_date=date(2023, 1, 1), close_date=date(2023, 12, 1)
    )
    result = overlapping_lots_for_cy(
        [open_long_in, open_short],
        [closed_in, closed_old],
        calendar_year=2025,
    )
    assert [lot.symbol for lot in result.open_lots] == ["OL_IN"]
    assert [lot.symbol for lot in result.closed_lots] == ["CL_IN"]


def test_cyoverlap_dataclass_is_frozen() -> None:
    """CyOverlap should be immutable so callers can pass it around
    without surprise mutation."""
    result = overlapping_lots_for_cy([], [], calendar_year=2025)
    with pytest.raises((AttributeError, TypeError)):
        result.open_lots = []  # ty: ignore[invalid-assignment]


def test_consumes_iterables_not_just_lists() -> None:
    """Inputs are typed Iterable, so a generator should work."""
    open_gen = (make_open_lot(open_date=date(2025, 6, i)) for i in (1, 5, 10))
    closed_gen = (
        make_closed_lot(open_date=date(2025, m, 1), close_date=date(2025, m, 28)) for m in (3, 4, 5)
    )
    result = overlapping_lots_for_cy(open_gen, closed_gen, calendar_year=2025)
    assert len(result.open_lots) == 3
    assert len(result.closed_lots) == 3


# ============================================================================
# Construction-time invariants (PR #123, issue #117)
# ============================================================================
# The postconditions on overlapping_lots_for_cy are free given the
# straight-line comprehensions, so a positive happy-path test (any
# call that produces non-empty output) covers them. The negative
# branches can only fire if a future refactor weakens the
# comprehension predicates. No additional tests needed; existing
# coverage exercises the happy paths.


def test_overlap_postconditions_hold_on_typical_input() -> None:
    """Smoke test that the postcondition assertions don't fire on
    valid input. Sanity check after PR #123 added them."""
    long_lot = make_open_lot(symbol="LONG", side=Side.LONG)
    short_lot = make_open_lot(symbol="SHORT", side=Side.SHORT)
    closed = make_closed_lot(
        symbol="CLOSED",
        open_date=date(2024, 6, 1),
        close_date=date(2025, 3, 1),
    )
    result = overlapping_lots_for_cy([long_lot, short_lot], [closed], calendar_year=2025)
    assert len(result.open_lots) == 1
    assert len(result.closed_lots) == 1


def test_shifted_broker_matched_lot_refused_even_outside_the_cy() -> None:
    """#599 runs on every input lot before the CY filter (#640): a
    broker-matched lot closed in another year is still refused."""
    lot = replace(
        make_closed_lot(open_date=date(2023, 5, 17), close_date=date(2023, 6, 1)),
        basis_provenance=BasisProvenance.BROKER_MATCHED,
        holding_period_date_time=date(2023, 2, 14),
    )
    with pytest.raises(BrokerBasisError) as exc:
        overlapping_lots_for_cy([], [lot], calendar_year=2025)
    assert str(exc.value) == BROKER_BASIS_MESSAGE
    # The old text printed the holding-period date; none of the lot's
    # dates, symbol or ISIN are printed now (#1047).
    for value in ("2023-02-14", "2023-05-17", "2023-06-01", "XQAT", "US99990G1085"):
        assert value.lower() not in str(exc.value).lower()
