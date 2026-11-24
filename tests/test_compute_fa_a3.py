"""Tests for :mod:`ibkr_fa.compute.fa_a3`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from ibkr_fa.compute import CyOverlap, build_fa_a3_rows, overlapping_lots_for_cy
from ibkr_fa.compute.fa_a3 import (
    FaA3ManualFillGaps,
    fa_a3_manual_fill_gaps,
    held_on,
    split_a3_credit_by_holding,
)
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models import BasisProvenance, ClosedLot, FaA3PeakBlankReason
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.country_codes import CountryCodeNotFoundError
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._fa_lot_inputs import fx_for, make_open_lot
from tests._fx_stubs import CalendarPolicy as _CalendarPolicy
from tests._robinhood_a3_exact import TINY_QUANTITY


def make_closed_lot(
    *,
    symbol: str = "XQAT",
    description: str = "SAMPLE ISSUER AT INC",
    isin: str = "US99990G1085",
    iso_country: str = "US",
    open_date: date = date(2024, 6, 15),
    close_date: date = date(2025, 6, 15),
    cost: Decimal = Decimal("274.60"),
    pnl: Decimal = Decimal("-12.85"),
    gross_proceeds_available: bool = True,
) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker(symbol),
        description=description,
        isin=Isin(isin),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode(iso_country),
        quantity=Decimal("5"),
        cost_basis_money=cost,
        proceeds_money=cost + pnl,
        open_date_time=open_date,
        close_date_time=close_date,
        gross_proceeds_money=cost + pnl if gross_proceeds_available else None,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


# ---- empty + ordering ------------------------------------------------------


def test_empty_overlap_returns_empty_list() -> None:
    overlap = CyOverlap(open_lots=[], closed_lots=[])
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())
    assert rows == []


@pytest.mark.parametrize(
    ("prices_fetched", "reason"),
    [
        (False, FaA3PeakBlankReason.PRICES_NOT_FETCHED),
        (True, FaA3PeakBlankReason.NO_USD_SERIES),
    ],
)
def test_blank_peak_row_records_why_a_ticker_has_no_series(
    prices_fetched: bool, reason: FaA3PeakBlankReason
) -> None:
    """#962: the row carries the reason, split by whether prices were fetched."""
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[make_open_lot()], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        prices_by_ticker={},
        prices_fetched=prices_fetched,
    )
    assert (row.peak_value_inr, row.peak_blank_reason) == (None, reason)


def test_peak_files_the_whole_rupee_of_its_exact_value() -> None:
    """#1260: 6.375 shares peak on 2025-12-29 at a 30-digit close, worth
    ₹20000.4999… at ₹86.15/$, so the row files ₹20,000. Main rounded the
    USD value and its INR conversion to 28 digits, reached ₹20000.5 and
    filed ₹20,001."""
    lot = make_open_lot(
        open_date=date(2025, 12, 29), quantity=Decimal("6.375"), mark_price=Decimal("30")
    )
    series = PriceSeries(
        ticker=Ticker("XQAW"),
        currency="USD",
        closes={
            date(2025, 12, 29): Decimal("36.4171019539562778100212807097"),
            date(2025, 12, 30): Decimal("31"),
            date(2025, 12, 31): Decimal("30"),
        },
        highs={},
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fx = _CalendarPolicy({date(2025, 12, 29): Decimal("86.15"), date(2025, 12, 31): Decimal("85")})
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx,
        prices_by_ticker={Ticker("XQAW"): series},
    )
    assert row.peak_value_inr == Decimal("20000.499999999999999999999998771675625")
    assert row.filed.peak_value_inr == Decimal("20000")


def test_peak_files_the_exact_value_main_refused_at_the_filing_bound() -> None:
    """#1260: 3 shares peak at $33333333333333.1666666666666666 and ₹1/$, worth
    exactly ₹99999999999999.4999999999999998, which files ₹99,999,999,999,999
    within the CBDT 14-digit bound. Main rounded the USD value to
    $99999999999999.50000000000000, which files a rupee past the bound, and
    refused the row. Only the rounding crossed the bound, so the exact value files."""
    lot = make_open_lot(
        open_date=date(2025, 12, 29), quantity=Decimal("3"), mark_price=Decimal("30")
    )
    series = PriceSeries(
        ticker=Ticker("XQAW"),
        currency="USD",
        closes={
            date(2025, 12, 29): Decimal("33333333333333.1666666666666666"),
            date(2025, 12, 30): Decimal("31"),
            date(2025, 12, 31): Decimal("30"),
        },
        highs={},
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fx = _CalendarPolicy({date(2025, 12, 29): Decimal("1"), date(2025, 12, 31): Decimal("1")})
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx,
        prices_by_ticker={Ticker("XQAW"): series},
    )
    assert row.peak_value_inr == Decimal("99999999999999.4999999999999998")
    assert row.filed.peak_value_inr == Decimal("99999999999999")


def test_inr_peak_override_warns() -> None:
    overlap = CyOverlap(open_lots=[], closed_lots=[])
    with pytest.warns(UserWarning, match="peak override active"):
        assert (
            build_fa_a3_rows(
                overlap,
                calendar_year=2025,
                fx_policy=fx_for(),
                peak_currency="inr",
            )
            == []
        )


def test_open_lots_emitted_before_closed_lots() -> None:
    overlap = CyOverlap(
        open_lots=[make_open_lot(symbol="XQAW")],
        closed_lots=[make_closed_lot(symbol="XQAT")],
    )
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())
    assert [r.source_ticker for r in rows] == ["XQAW", "XQAT"]


def test_input_order_preserved_within_partition() -> None:
    overlap = CyOverlap(
        open_lots=[
            make_open_lot(symbol="A"),
            make_open_lot(symbol="B"),
            make_open_lot(symbol="C"),
        ],
        closed_lots=[
            make_closed_lot(symbol="X"),
            make_closed_lot(symbol="Y"),
        ],
    )
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())
    assert [r.source_ticker for r in rows] == ["A", "B", "C", "X", "Y"]


# ---- open lot mapping ------------------------------------------------------


def test_open_lot_row_fields() -> None:
    lot = make_open_lot()
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    [row] = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())

    # Country (from lookup of "US")
    assert row.country_name == "United States of America"
    assert row.country_code == "2"
    assert row.source_country_iso2 == CountryCode("US")
    # #1289: no entry proves an address, so the cell is blank, not the country.
    assert row.address_of_entity == ""

    # Identification
    assert row.name_of_entity == "SAMPLE ISSUER AW INC"
    assert row.source_isin == Isin("US99990E1038")
    assert row.source_ticker == Ticker("XQAW")

    # Generic identity defaults: nature requires verified per-ISIN evidence.
    assert row.zip_code == ""
    assert row.nature_of_entity == ""

    # Dates
    assert row.date_of_acquiring == date(2024, 6, 15)
    assert row.audit_acquisition_date == date(2024, 6, 15)
    assert row.audit_disposal_date is None

    # Monetary (open lot)
    # initial = 386.45 * 83.50 = 32268.575
    assert row.initial_value_inr == Decimal("32268.575")
    # closing = quantity * mark_price * dec31_rate = 15 * 32.70 * 85.00 = 41692.50
    assert row.closing_value_inr == Decimal("41692.50")

    # v1 incomplete columns
    assert row.peak_value_inr is None
    assert row.gross_amount_paid_credited_inr is None
    # Issue #16: gross_proceeds_from_sale_inr is now computed. Open
    # lot has no disposal, so the value is Decimal("0").
    assert row.gross_proceeds_from_sale_inr == Decimal("0")

    # Audit
    assert row.audit_cost_basis_usd == Decimal("386.45")
    assert row.audit_close_price_usd == Decimal("32.70")
    assert row.audit_ttbr_acquisition == Decimal("83.50")
    assert row.audit_ttbr_dec31 == Decimal("85.00")


# ---- closed lot mapping ----------------------------------------------------


def test_closed_lot_row_fields() -> None:
    lot = make_closed_lot()
    overlap = CyOverlap(open_lots=[], closed_lots=[lot])
    [row] = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())

    # Identification (different ISIN/ticker from the open default)
    assert row.name_of_entity == "SAMPLE ISSUER AT INC"
    assert row.source_isin == Isin("US99990G1085")
    assert row.source_ticker == Ticker("XQAT")

    # Dates
    assert row.date_of_acquiring == date(2024, 6, 15)
    assert row.audit_disposal_date == date(2025, 6, 15)

    # Closing balance is 0 for disposed lots (no year-end position)
    assert row.closing_value_inr == Decimal("0")

    # Initial value uses open_date_time FX rate (NOT close date)
    # 274.60 * 83.50 = 22929.10
    assert row.initial_value_inr == Decimal("22929.10")
    # Schedule FA gross proceeds use the calendar-year closing rate.
    # proceeds_money = cost + pnl = 274.60 + (-12.85) = 261.75; 261.75 * 85.00 = 22248.75
    assert row.gross_proceeds_from_sale_inr == Decimal("22248.75")

    # Audit fields that are open-lot-only
    assert row.audit_close_price_usd is None
    assert row.audit_ttbr_dec31 == Decimal("85.00")


def test_closed_lot_without_gross_consideration_is_manual_fill() -> None:
    lot = make_closed_lot(gross_proceeds_available=False)
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[], closed_lots=[lot]),
        calendar_year=2025,
        # No Dec-31 rate: unproven proceeds make no year-end lookup.
        fx_policy=_CalendarPolicy({date(2024, 6, 15): Decimal("83.50")}),
    )
    assert row.gross_proceeds_from_sale_inr is None
    assert row.audit_ttbr_dec31 is None


def test_closed_lot_audit_ttbr_acquisition_uses_open_date() -> None:
    """Regression: a closed lot's audit_ttbr_acquisition must come
    from open_date_time. Common mistake: using close_date_time."""
    fx = _CalendarPolicy(
        {
            date(2024, 1, 15): Decimal("83.10"),
            date(2025, 6, 1): Decimal("84.50"),
            date(2025, 12, 31): Decimal("85.00"),
        }
    )
    lot = make_closed_lot(open_date=date(2024, 1, 15), close_date=date(2025, 6, 1))
    overlap = CyOverlap(open_lots=[], closed_lots=[lot])
    [row] = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert row.audit_ttbr_acquisition == Decimal("83.10")


# A third of $10.00, cut to the 20 digits main's lot allocation kept (#1243).
_THIRD_OF_TEN = Decimal("3.3333333333333333333")


@pytest.mark.parametrize("kind", ["open", "closed"])
def test_exact_cost_files_its_exact_half_up(kind: str) -> None:
    """#1243: a lot costing exactly a third of $10.00 at ₹84.15/$ files Initial value ₹281,
    the exact ₹280.50's HALF_UP; its 20-digit cost gave ₹280.4999…, which filed ₹280."""
    opened = date(2025, 1, 15)
    if kind == "open":
        lot = make_open_lot(open_date=opened, cost=_THIRD_OF_TEN)
        overlap = CyOverlap(
            open_lots=[replace(lot, cost_basis_money_exact=Fraction(10, 3))], closed_lots=[]
        )
    else:
        lot = make_closed_lot(open_date=opened, cost=_THIRD_OF_TEN, pnl=Decimal("1.00"))
        overlap = CyOverlap(
            open_lots=[], closed_lots=[replace(lot, cost_basis_money_exact=Fraction(10, 3))]
        )
    fx = _CalendarPolicy({opened: Decimal("84.15"), date(2025, 12, 31): Decimal("85.00")})
    [row] = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert row.filed.initial_value_inr == Decimal("281")
    assert row.initial_value_inr.compare_total(Decimal("280.5")) == 0


# ---- country handling ------------------------------------------------------


def test_uk_lot_uses_correct_cbdt_code() -> None:
    """Regression for the country-code bug fix: a GB lot must get
    code 44, NOT code 1 (which is Canada in the portal)."""
    lot = make_open_lot(iso_country="GB")
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    [row] = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())
    assert row.country_code == "44"
    assert row.country_name == "United Kingdom"


def test_unknown_country_raises_actionable_error() -> None:
    """A lot from an ISO code we haven't mapped to CBDT must raise
    with a message pointing at the CSV to extend (rather than
    silently produce a wrong country)."""
    lot = make_open_lot(iso_country="ZZ")
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    with pytest.raises(CountryCodeNotFoundError):
        build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx_for())


# ---- FX error propagation --------------------------------------------------


def test_missing_acquisition_fx_rate_propagates() -> None:
    fx = _CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    lot = make_open_lot(open_date=date(2020, 5, 5))
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    with pytest.raises(MissingFxRateError):
        build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    # The first FX call is the acquisition lookup inside initial_value_inr_for_lot.
    assert fx.requested_dates[-1] == date(2020, 5, 5)


def test_missing_year_end_fx_rate_propagates_for_open_lot() -> None:
    """If acquisition rate exists but Dec 31 rate doesn't, the open-
    lot path raises naming the year-end date."""
    fx = _CalendarPolicy({date(2024, 6, 15): Decimal("83.50")})
    lot = make_open_lot()
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    with pytest.raises(MissingFxRateError):
        build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert fx.requested_dates[-1] == date(2025, 12, 31)


def test_closed_lot_gross_proceeds_require_year_end_rate() -> None:
    """A disposal rate cannot substitute for the Schedule FA Dec-31 rate."""
    fx = _CalendarPolicy(
        {
            date(2024, 6, 15): Decimal("83.50"),  # open / acquisition
            date(2025, 6, 15): Decimal("85.75"),  # close / disposal (#16)
        }
    )
    lot = make_closed_lot()  # opened 2024-06-15, closed 2025-06-15
    overlap = CyOverlap(open_lots=[], closed_lots=[lot])
    with pytest.raises(MissingFxRateError):
        build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert fx.requested_dates[-1] == date(2025, 12, 31)


def _zero_proceeds_overlap() -> CyOverlap:
    """#735: a 2025 holding closed out at a price of 0 (proven gross proceeds 0)."""
    lot = make_closed_lot(
        open_date=date(2025, 1, 15),
        close_date=date(2025, 6, 16),
        cost=Decimal("12"),
        pnl=Decimal("-12"),
    )
    assert lot.gross_proceeds_money == 0
    return CyOverlap(open_lots=[], closed_lots=[lot])


@pytest.mark.parametrize(
    "dividends",
    [{}, {Ticker("XQAT"): Decimal("100")}],
    ids=["no-credit", "dividend"],
)
def test_closed_lot_zero_gross_proceeds_require_year_end_rate(
    dividends: dict[Ticker, Decimal],
) -> None:
    """#735: proven zero proceeds need the Dec-31 rate like any other proceeds."""
    fx = _CalendarPolicy({date(2025, 1, 15): Decimal("85")})
    with pytest.raises(MissingFxRateError):
        build_fa_a3_rows(
            _zero_proceeds_overlap(),
            calendar_year=2025,
            fx_policy=fx,
            dividends_by_symbol_inr=dividends,
        )
    assert fx.requested_dates[-1] == date(2025, 12, 31)


def test_closed_lot_zero_gross_proceeds_require_acquisition_rate() -> None:
    fx = _CalendarPolicy({date(2025, 12, 31): Decimal("89")})
    with pytest.raises(MissingFxRateError):
        build_fa_a3_rows(
            _zero_proceeds_overlap(),
            calendar_year=2025,
            fx_policy=fx,
            dividends_by_symbol_inr={},
        )
    assert fx.requested_dates[-1] == date(2025, 1, 15)


def test_closed_lot_zero_gross_proceeds_with_income_audits_dec31_ttbr() -> None:
    fx = _CalendarPolicy({date(2025, 1, 15): Decimal("85"), date(2025, 12, 31): Decimal("89")})
    [row] = build_fa_a3_rows(
        _zero_proceeds_overlap(),
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAT"): Decimal("100")},
    )
    assert row.gross_proceeds_from_sale_inr == 0
    assert row.gross_amount_paid_credited_inr == Decimal("100")
    assert row.audit_ttbr_dec31 == Decimal("89")


# ---- v1 defaults overrideable ----------------------------------------------


def test_source_specific_nature_and_zip() -> None:
    """Single-security source pipelines can provide verified nature text."""
    lot = make_open_lot()
    overlap = CyOverlap(open_lots=[lot], closed_lots=[])
    [row] = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx_for(),
        nature_of_entity="ETF",
        zip_code="94025",
    )
    assert row.nature_of_entity == "ETF"
    assert row.zip_code == "94025"


def test_uses_same_fx_policy_instance_for_all_lots() -> None:
    """Sanity: the same policy instance is reused for every per-lot
    lookup so its internal cache stays effective. (No mutable state
    on the policy needed for this test; just behaviour assertion.)"""
    fx = fx_for()
    overlap = CyOverlap(
        open_lots=[make_open_lot(symbol="A"), make_open_lot(symbol="B")],
        closed_lots=[],
    )
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    # Both rows must reflect the same calendar_year FX rate.
    assert rows[0].audit_ttbr_dec31 == rows[1].audit_ttbr_dec31 == Decimal("85.00")


# ============================================================================
# Construction-time invariants (PR #123, issue #117)
# ============================================================================
# build_fa_a3_rows postcondition: output row count == sum of input
# partition lengths. Free given the straight-line iteration, but the
# assertion locks the contract. Positive coverage from every existing
# test that calls build_fa_a3_rows; smoke test below confirms.


def test_build_fa_a3_rows_postcondition_holds() -> None:
    """Smoke test that the row-count postcondition fires correctly on
    a typical mixed open+closed input. (The negative branch can only
    trigger if a future refactor drops or duplicates rows.)"""
    open_lot = make_open_lot(open_date=date(2025, 6, 15))
    closed_lot = make_closed_lot(
        open_date=date(2024, 6, 15),
        close_date=date(2025, 3, 15),
    )
    overlap = overlapping_lots_for_cy([open_lot], [closed_lot], calendar_year=2025)
    fx = _CalendarPolicy(
        {
            date(2024, 6, 15): Decimal("83.10"),
            date(2025, 3, 15): Decimal("87.40"),
            date(2025, 6, 15): Decimal("85.00"),
            date(2025, 12, 31): Decimal("85.50"),
        }
    )
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert len(rows) == 2  # 1 open + 1 closed


# ============================================================================
# Dividend per-row attribution (issue #31, PR #137 Tier-1 P1.1 + P1.2)
# ============================================================================
#
# Schedule FA Table A3 column 10 ("Total gross amount paid/credited
# with respect to the holding during the period") is per-row. For
# multi-lot symbols, naively assigning the per-symbol total to
# every lot row double-counts at portal-sum time.
#
# Fix (PR #137 Tier-1 P1.1): attribute the full per-symbol total to
# the FIRST lot in input order (oldest acquisition); subsequent
# lots of the same symbol get Decimal("0"). Sum across rows
# preserves the per-symbol total exactly.
#
# Fix (PR #137 Tier-1 P1.2): when dividend data is provided BUT a
# symbol has no dividend in CY, every row for that symbol gets
# Decimal("0") (not None). This satisfies the portal's "whole min
# 0" validation — blank cells get rejected, 0 is accepted.


def test_dividends_single_lot_attribution() -> None:
    """One lot, one dividend: full per-symbol total goes to the
    one row."""
    lot = make_open_lot(symbol="XQAW", open_date=date(2025, 6, 15))
    overlap = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    fx = _CalendarPolicy({date(2025, 6, 15): Decimal("85"), date(2025, 12, 31): Decimal("89")})
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAW"): Decimal("100")},
    )
    assert len(rows) == 1
    assert rows[0].gross_amount_paid_credited_inr == Decimal("100")


def test_closed_lot_with_income_audits_dec31_ttbr() -> None:
    lot = make_closed_lot(
        symbol="XQAW",
        description="SAMPLE ISSUER AW INC",
        isin="US99990E1038",
        open_date=date(2025, 1, 15),
        close_date=date(2025, 6, 15),
    )
    overlap = overlapping_lots_for_cy([], [lot], calendar_year=2025)
    fx = _CalendarPolicy(
        {
            date(2025, 1, 15): Decimal("85"),
            date(2025, 6, 15): Decimal("87"),
            date(2025, 12, 31): Decimal("89"),
        }
    )
    [row] = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAW"): Decimal("100")},
    )
    assert row.audit_ttbr_dec31 == Decimal("89")


def test_dividends_multi_lot_same_symbol_no_double_count() -> None:
    """PR #137 Tier-1 P1.1: two lots of XQAW with $100 total
    dividend. Pre-fix: each row got 100, portal sum = 200 (wrong).
    Post-fix: first lot (input order) gets 100, second gets 0;
    portal sum = 100 (correct)."""
    lot1 = make_open_lot(symbol="XQAW", open_date=date(2025, 1, 15))
    lot2 = make_open_lot(symbol="XQAW", open_date=date(2025, 7, 20))
    overlap = overlapping_lots_for_cy([lot1, lot2], [], calendar_year=2025)
    fx = _CalendarPolicy(
        {
            date(2025, 1, 15): Decimal("85"),
            date(2025, 7, 20): Decimal("87"),
            date(2025, 12, 31): Decimal("89"),
        }
    )
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAW"): Decimal("100")},
    )
    assert len(rows) == 2
    # First lot (input order) gets the full symbol total.
    assert rows[0].gross_amount_paid_credited_inr == Decimal("100")
    # Second lot gets 0 — portal accepts 0 but rejects blank.
    assert rows[1].gross_amount_paid_credited_inr == Decimal("0")
    # Sum across rows preserves the symbol total exactly.
    assert sum(r.gross_amount_paid_credited_inr or Decimal("0") for r in rows) == Decimal("100")


def test_dividends_symbol_without_payment_gets_zero_not_none() -> None:
    """PR #137 Tier-1 P1.2: when dividend data is provided but a
    symbol has no in-CY payment, the row gets Decimal('0'), not
    None. Pre-fix the row would have gotten None — the manual-fill
    warning would have been suppressed (because some other row was
    populated) but the row's cell would be blank in the output CSV
    and the portal would reject it on paste."""
    xqaw_lot = make_open_lot(symbol="XQAW", open_date=date(2025, 6, 15))
    xqah_lot = make_open_lot(
        symbol="XQAH",
        description="SAMPLE ISSUER AH CORP",
        isin="US99992B1070",
        open_date=date(2025, 6, 15),
    )
    overlap = overlapping_lots_for_cy([xqaw_lot, xqah_lot], [], calendar_year=2025)
    fx = _CalendarPolicy({date(2025, 6, 15): Decimal("85"), date(2025, 12, 31): Decimal("89")})
    # Dividend data present but XQAH has no in-CY payment.
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAW"): Decimal("50")},
    )
    assert len(rows) == 2
    # XQAW: populated with its $50 total.
    assert rows[0].gross_amount_paid_credited_inr == Decimal("50")
    # XQAH: gets 0 (not None) so the portal accepts the cell.
    assert rows[1].gross_amount_paid_credited_inr == Decimal("0")


def test_dividends_none_kwarg_leaves_all_rows_none() -> None:
    """When the kwarg is None (no input had dividend data — Flex
    Query, or AS without Dividends section), every row's column 10
    is None and the CLI's manual-fill warning fires for that
    column. Pre-#31 baseline behaviour is preserved."""
    lot = make_open_lot(symbol="XQAW", open_date=date(2025, 6, 15))
    overlap = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    fx = _CalendarPolicy({date(2025, 6, 15): Decimal("85"), date(2025, 12, 31): Decimal("89")})
    rows = build_fa_a3_rows(overlap, calendar_year=2025, fx_policy=fx)
    assert len(rows) == 1
    assert rows[0].gross_amount_paid_credited_inr is None


def test_dividends_empty_dict_kwarg_all_rows_zero() -> None:
    """Low-level compute-layer behaviour: when ``build_fa_a3_rows`` is
    called directly with ``dividends_by_symbol_inr={}`` (an explicit
    empty dict, not ``None``), every row gets ``Decimal('0')`` for
    column 10. Documents the API contract at THIS layer.

    NOTE: As of the PR #137 zero-when-sure change, the pipeline
    (`pipeline_fa.run_fa_a3`) DOES hand an empty dict through here for an
    ACTIVITY STATEMENT with no Dividends section (a full-CY statement
    with no dividend rows is positive evidence of ₹0, surfaced via a CLI
    info note — not silent). It still coerces to ``None`` for a FLEX
    QUERY (no dividend parsing) and for the unreachable "dividends present
    but empty in-CY aggregate" case (defense-in-depth). See
    :func:`tests.test_e2e_activity_statement.test_run_fa_a3_empty_aggregate_coerced_to_none`
    (None path) and ``test_e2e_fa_peak_value_populated_when_prices_fetched``
    (the {} zero path)."""
    lot = make_open_lot(symbol="XQAW", open_date=date(2025, 6, 15))
    overlap = overlapping_lots_for_cy([lot], [], calendar_year=2025)
    fx = _CalendarPolicy({date(2025, 6, 15): Decimal("85"), date(2025, 12, 31): Decimal("89")})
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={},
    )
    assert len(rows) == 1
    assert rows[0].gross_amount_paid_credited_inr == Decimal("0")


def test_dividends_three_lots_two_symbols_correct_attribution() -> None:
    """Mixed case: 2 XQAW lots + 1 XQAH lot, both
    symbols paid dividends. Each symbol's total goes to its first
    lot only; remaining same-symbol lots get 0."""
    xqaw1 = make_open_lot(symbol="XQAW", open_date=date(2025, 1, 15))
    xqaw2 = make_open_lot(symbol="XQAW", open_date=date(2025, 4, 20))
    xqah = make_open_lot(
        symbol="XQAH",
        description="SAMPLE ISSUER AH CORP",
        isin="US99992B1070",
        open_date=date(2025, 2, 10),
    )
    overlap = overlapping_lots_for_cy([xqaw1, xqaw2, xqah], [], calendar_year=2025)
    fx = _CalendarPolicy(
        {
            date(2025, 1, 15): Decimal("85"),
            date(2025, 2, 10): Decimal("85"),
            date(2025, 4, 20): Decimal("86"),
            date(2025, 12, 31): Decimal("89"),
        }
    )
    rows = build_fa_a3_rows(
        overlap,
        calendar_year=2025,
        fx_policy=fx,
        dividends_by_symbol_inr={Ticker("XQAW"): Decimal("100"), Ticker("XQAH"): Decimal("50")},
    )
    assert len(rows) == 3
    assert rows[0].gross_amount_paid_credited_inr == Decimal("100")  # XQAW lot 1
    assert rows[1].gross_amount_paid_credited_inr == Decimal("0")  # XQAW lot 2
    assert rows[2].gross_amount_paid_credited_inr == Decimal("50")  # XQAH
    # Portal sum integrity preserved per-symbol.
    xqaw_sum = sum(
        r.gross_amount_paid_credited_inr or Decimal("0")
        for r in rows
        if str(r.source_ticker) == "XQAW"
    )
    assert xqaw_sum == Decimal("100")


# ---- pro rata by holding date (issue #623) ---------------------------------


@pytest.mark.parametrize(
    ("open_date", "close_date", "expected"),
    [
        (date(2025, 3, 9), None, True),  # opened on the holding date
        (date(2025, 3, 10), None, False),  # bought after it
        (date(2025, 1, 2), date(2025, 3, 9), False),  # closed on it
        (date(2025, 1, 2), date(2025, 3, 10), True),  # closed after it
    ],
)
def test_held_on_boundaries(open_date: date, close_date: date | None, expected: bool) -> None:
    assert held_on(open_date, close_date, date(2025, 3, 9)) is expected


def test_split_by_holding_is_pro_rata_by_quantity() -> None:
    assert split_a3_credit_by_holding(Fraction(100), (Decimal("30"), Decimal("10"))) == (
        Fraction(75),
        Fraction(25),
    )


@pytest.mark.parametrize(
    ("amount", "quantities", "expected"),
    [
        pytest.param(Fraction(100), ("1", "1", "1"), (Fraction(100, 3),) * 3, id="thirds"),
        pytest.param(Fraction(1), ("1", "2"), (Fraction(1, 3), Fraction(2, 3)), id="one-and-two"),
    ],
)
def test_split_by_holding_gives_every_holder_its_exact_share(
    amount: Fraction, quantities: tuple[str, ...], expected: tuple[Fraction, ...]
) -> None:
    """#1243: a third stays a third, with no last-holder remainder, and the shares add back
    exactly."""
    shares = split_a3_credit_by_holding(amount, tuple(map(Decimal, quantities)))

    assert shares == expected
    assert shares is not None
    assert sum(shares, Fraction()) == amount


@pytest.mark.parametrize(
    "quantities",
    [(Decimal(TINY_QUANTITY), Decimal("2")), (Decimal("2"), Decimal(TINY_QUANTITY))],
    ids=["first", "last"],
)
def test_split_by_holding_past_the_digit_cap_is_none(quantities: tuple[Decimal, ...]) -> None:
    """#1243: a quantity with no exact rational gives no split, for the caller to refuse."""
    assert split_a3_credit_by_holding(Fraction(10), quantities) is None


def test_split_by_holding_splits_by_exact_quantities() -> None:
    """#1263: three lots each left holding exactly two thirds of a share by a same-day
    pro-rata sale split a $3.40 credit into exactly a third each, which their 20-digit
    Decimal quantities can't all hold."""
    shares = split_a3_credit_by_holding(Fraction("3.40"), (Fraction(2, 3),) * 3)

    assert shares == (Fraction(17, 15),) * 3
    assert shares is not None
    assert sum(shares, Fraction()) == Fraction("3.40")


def test_split_by_holding_mixes_exact_and_decimal_quantities() -> None:
    """#1263: a ``Fraction`` quantity is taken as is beside Decimal ones, in caller order."""
    shares = split_a3_credit_by_holding(Fraction(3), (Fraction(2, 3), Decimal("1"), Fraction(4, 3)))

    assert shares == (Fraction(2, 3), Fraction(1), Fraction(4, 3))
    assert shares is not None
    assert sum(shares, Fraction()) == Fraction(3)


def test_split_by_holding_with_an_exact_quantity_past_the_digit_cap_is_none() -> None:
    """#1263: an exact quantity beside a Decimal one past the digit cap still gives no split."""
    quantities = (Fraction(2, 3), Decimal(TINY_QUANTITY))
    assert split_a3_credit_by_holding(Fraction(10), quantities) is None


_POSITIVE_TOTAL = (
    "InvariantError: an A3 credit split needs a positive total holder quantity "
    "(context keys: total_quantity)"
)


@pytest.mark.parametrize(
    ("quantities", "message"),
    [
        ((), "InvariantError: an A3 credit split needs at least one holder"),
        ((Decimal("0"),), _POSITIVE_TOTAL),
        ((Decimal("1"), Decimal("-1")), _POSITIVE_TOTAL),
        ((Fraction(0),), _POSITIVE_TOTAL),
        ((Fraction(2, 3), Fraction(-2, 3)), _POSITIVE_TOTAL),
        ((Fraction(1), Decimal("-1")), _POSITIVE_TOTAL),
    ],
    ids=["no-holder", "zero", "nets-to-zero", "exact-zero", "exact-nets-to-zero", "mixed-nets"],
)
def test_split_by_holding_refuses_no_positive_holding(
    quantities: tuple[Decimal | Fraction, ...], message: str
) -> None:
    """#1243, #1263: the refusals keep their messages, for exact quantities too."""
    with pytest.raises(InvariantError) as exc:
        split_a3_credit_by_holding(Fraction(100), quantities)
    assert str(exc.value) == message


# ---- [[fa.entities]] per-issuer overrides ---------------------------------


def test_entity_override_applies_by_isin() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()  # ISIN US99990E1038
    override = FaEntityOverride(
        isin=Isin("US99990E1038"),
        address="100 Sample Way, Suite 10, Exampleton, IL",
        zip_code="99991",
        name="Sample Issuer AW Inc.",
        nature_of_entity="Company",
    )
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        entity_overrides={override.isin: override},
    )
    assert row.name_of_entity == "Sample Issuer AW Inc."
    assert row.address_of_entity == "100 Sample Way, Suite 10, Exampleton, IL"
    assert row.zip_code == "99991"
    assert row.nature_of_entity == "Company"


def test_entity_override_nature_takes_precedence_over_source_default() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()
    override = FaEntityOverride(
        isin=Isin("US99990E1038"),
        address="Exampleton, IL",
        nature_of_entity="Trust",
    )
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        nature_of_entity="Company",
        entity_overrides={override.isin: override},
    )
    assert row.nature_of_entity == "Trust"


def test_resolved_nature_is_independent_from_entity_address_override() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()
    override = FaEntityOverride(
        isin=lot.isin,
        address="Reviewed legal address",
        zip_code="19801",
        nature_of_entity="Reviewed Company",
    )
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        entity_overrides={override.isin: override},
        nature_by_isin={lot.isin: "Corporation"},
    )

    assert row.address_of_entity == "Reviewed legal address"
    assert row.zip_code == "19801"
    assert row.nature_of_entity == "Corporation"


def test_nature_only_evidence_keeps_blank_address_and_zip() -> None:
    lot = make_open_lot()

    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        nature_by_isin={lot.isin: "Corporation"},
    )

    assert row.address_of_entity == ""
    assert row.zip_code == ""
    assert row.nature_of_entity == "Corporation"


def test_entity_override_without_nature_uses_source_default() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()
    override = FaEntityOverride(isin=Isin("US99990E1038"), address="Exampleton, IL")
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        nature_of_entity="Company",
        entity_overrides={override.isin: override},
    )
    assert row.nature_of_entity == "Company"


def test_entity_override_name_omitted_keeps_statement_name() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()
    override = FaEntityOverride(isin=Isin("US99990E1038"), address="Exampleton, IL", zip_code="")
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        entity_overrides={override.isin: override},
    )
    assert row.name_of_entity == "SAMPLE ISSUER AW INC"  # statement name preserved
    assert row.address_of_entity == "Exampleton, IL"
    assert row.zip_code == ""


@pytest.mark.parametrize("closed", [False, True], ids=["open-lot", "closed-lot"])
def test_entity_override_without_address_refuses_its_row(*, closed: bool) -> None:
    """#1449 backstop: an entry without address stops its run before any row
    is built, so one reaching a row, open or closed, is an invariant breach."""
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    overlap = (
        CyOverlap(open_lots=[], closed_lots=[make_closed_lot()])
        if closed
        else CyOverlap(open_lots=[make_open_lot()], closed_lots=[])
    )
    (lot,) = (*overlap.open_lots, *overlap.closed_lots)
    override = FaEntityOverride(isin=lot.isin, name="Sample Issuer AW Inc.")
    with pytest.raises(InvariantError) as exc:
        build_fa_a3_rows(
            overlap,
            calendar_year=2025,
            fx_policy=fx_for(),
            entity_overrides={override.isin: override},
        )
    assert str(exc.value) == (
        "InvariantError: an [[fa.entities]] entry without address reached an A3 row"
    )


def test_entity_override_non_matching_isin_ignored() -> None:
    from ibkr_fa.config import FaEntityOverride  # noqa: PLC0415

    lot = make_open_lot()  # US99990E1038
    override = FaEntityOverride(isin=Isin("US99971J1059"), address="somewhere else")
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[lot], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
        entity_overrides={override.isin: override},
    )
    # No matching ISIN → blank address and ZIP for manual fill (#1289).
    assert row.address_of_entity == ""
    assert row.zip_code == ""


# ---- manual-fill gaps (#626) -----------------------------------------------


def test_manual_fill_gaps_name_each_blank_holding() -> None:
    rows = build_fa_a3_rows(
        CyOverlap(
            open_lots=[make_open_lot()],
            closed_lots=[make_closed_lot(gross_proceeds_available=False)],
        ),
        calendar_year=2025,
        fx_policy=fx_for(),
    )
    xqaw = (Ticker("XQAW"), Isin("US99990E1038"))
    xqat = (Ticker("XQAT"), Isin("US99990G1085"))
    not_fetched = FaA3PeakBlankReason.PRICES_NOT_FETCHED
    assert fa_a3_manual_fill_gaps(rows, dividends_resolved=True) == FaA3ManualFillGaps(
        name_of_entity=frozenset(),
        address_of_entity=frozenset({xqaw, xqat}),
        zip_code=frozenset({xqaw, xqat}),
        nature_of_entity=frozenset({xqaw, xqat}),
        peak=frozenset({(*xqaw, not_fetched), (*xqat, not_fetched)}),
        no_rows=False,
        dividends=False,
        gross_proceeds=frozenset({xqat}),
    )


def test_manual_fill_gaps_skip_filled_identity_and_proceeds() -> None:
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[make_open_lot()], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
    )
    filled = replace(
        row,
        address_of_entity="Reviewed legal address",
        zip_code="19801",
        nature_of_entity="Company",
    )
    assert fa_a3_manual_fill_gaps([filled], dividends_resolved=False) == FaA3ManualFillGaps(
        name_of_entity=frozenset(),
        address_of_entity=frozenset(),
        zip_code=frozenset(),
        nature_of_entity=frozenset(),
        peak=frozenset(
            {(Ticker("XQAW"), Isin("US99990E1038"), FaA3PeakBlankReason.PRICES_NOT_FETCHED)}
        ),
        no_rows=False,
        dividends=True,
        gross_proceeds=frozenset(),
    )


def test_manual_fill_gaps_count_a_whitespace_only_address_or_zip_as_blank() -> None:
    """As the CBDT JSON fragment does: spaces are not a supplied value (#1289)."""
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[make_open_lot()], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
    )
    xqaw = frozenset({(Ticker("XQAW"), Isin("US99990E1038"))})
    spaces = replace(row, address_of_entity="  ", zip_code=" ")
    gaps = fa_a3_manual_fill_gaps([spaces], dividends_resolved=True)
    assert (gaps.address_of_entity, gaps.zip_code) == (xqaw, xqaw)


@pytest.mark.parametrize(
    ("name", "blank"),
    [
        pytest.param(" ", True, id="one-space"),
        pytest.param("   ", True, id="spaces"),
        pytest.param("\u00a0", True, id="no-break-space"),
        pytest.param("\u3000", True, id="ideographic-space"),
        pytest.param("a", False, id="letter"),
        pytest.param("  a  ", False, id="padded-letter"),
    ],
)
def test_manual_fill_gaps_list_a_whitespace_only_name(name: str, *, blank: bool) -> None:
    """#1369: the gaps list a whitespace-only Name of entity's holding; a
    name with any non-whitespace character is not flagged as blank.
    No other gap changes."""
    [row] = build_fa_a3_rows(
        CyOverlap(open_lots=[make_open_lot()], closed_lots=[]),
        calendar_year=2025,
        fx_policy=fx_for(),
    )
    named = fa_a3_manual_fill_gaps([row], dividends_resolved=True)
    assert named.name_of_entity == frozenset()

    gaps = fa_a3_manual_fill_gaps([replace(row, name_of_entity=name)], dividends_resolved=True)

    xqaw = frozenset({(Ticker("XQAW"), Isin("US99990E1038"))})
    assert gaps == replace(named, name_of_entity=xqaw if blank else frozenset())


def test_manual_fill_gaps_zero_rows_still_flag_peak() -> None:
    assert fa_a3_manual_fill_gaps([], dividends_resolved=True) == FaA3ManualFillGaps(
        name_of_entity=frozenset(),
        address_of_entity=frozenset(),
        zip_code=frozenset(),
        nature_of_entity=frozenset(),
        peak=frozenset(),
        no_rows=True,
        dividends=False,
        gross_proceeds=frozenset(),
    )
