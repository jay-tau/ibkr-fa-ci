"""Tests for valuing a HoldingLot at its resolved Dec-31 close (#627)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import cast

import pytest

from ibkr_fa.compute import build_fa_a3_rows, overlapping_lots_for_cy
from ibkr_fa.compute.dec31_close import CloseSource, ResolvedClose, resolve_dec31_close
from ibkr_fa.compute.holding_valuation import value_holding_lot
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import FaA3Row, OpenLot, Side
from ibkr_fa.models.holding_lot import HoldingLot
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import previous_us_market_session, us_market_sessions
from ibkr_fa.types import CountryCode, Isin, PeakBasis, Ticker
from tests._fx_stubs import AuditedFxStub

_DEC31 = date(2025, 12, 31)
_VEST_DATE = date(2025, 8, 14)
_FMV = Decimal("196.42")
_SHARES = Decimal("6.214")
_CLOSE = Decimal("200.75")


def _lot(**overrides: object) -> HoldingLot:
    fields: dict[str, object] = {
        "currency": "USD",
        "symbol": Ticker("XQCB"),
        "description": "SAMPLE ISSUER CB Inc.",
        "isin": Isin("US99971P1012"),
        "issuer_country_code": CountryCode("US"),
        "quantity": _SHARES,
        "cost_basis_money": _FMV * _SHARES,
        "acquisition_date": _VEST_DATE,
        "side": Side.LONG,
    }
    fields.update(overrides)
    return HoldingLot(**fields)  # type: ignore[arg-type]


def _per_share_close(source: CloseSource, quantity: Decimal = _SHARES) -> ResolvedClose:
    return ResolvedClose(
        total_usd=_CLOSE * quantity, per_share_usd=_CLOSE, source=source, provenance="test"
    )


@pytest.mark.parametrize("source", [CloseSource.YAHOO_FALLBACK, CloseSource.OVERRIDE])
def test_per_share_close_is_the_mark(source: CloseSource) -> None:
    valued = value_holding_lot(_lot(), report_date=_DEC31, close=_per_share_close(source))
    assert valued.mark_price == _CLOSE
    assert valued.report_date == _DEC31
    assert valued.closing_total_usd is None


def _statement_close(total: Decimal) -> ResolvedClose:
    return ResolvedClose(
        total_usd=total, per_share_usd=None, source=CloseSource.STATEMENT, provenance="test"
    )


def test_statement_total_is_divided_by_quantity() -> None:
    total = Decimal("1247.46")
    valued = value_holding_lot(_lot(), report_date=_DEC31, close=_statement_close(total))
    assert valued.mark_price == total / _SHARES
    assert valued.closing_total_usd == total


@pytest.mark.parametrize("quantity_delta", [Decimal("1"), Decimal("0.000001")])
def test_per_share_close_for_another_quantity_refused(quantity_delta: Decimal) -> None:
    """The product must match exactly, even when the difference is below a cent."""
    close = _per_share_close(CloseSource.YAHOO_FALLBACK, quantity=_SHARES + quantity_delta)
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(_lot(), report_date=_DEC31, close=close)
    assert "needs a per-share close resolved for lot.quantity" in exc.value.what


# A 30-digit per-share close: 6.214 x it needs 34 digits.
_LONG_CLOSE = Decimal("200.750000000000000000000000001")
_PER_SHARE_REFUSAL = (
    "InvariantError: value_holding_lot needs a per-share close resolved for lot.quantity "
    "(context keys: quantity, per_share_usd, total_usd)"
)


def _long_per_share_close(total: str, per_share: Decimal = _LONG_CLOSE) -> ResolvedClose:
    return ResolvedClose(
        total_usd=Decimal(total),
        per_share_usd=per_share,
        source=CloseSource.OVERRIDE,
        provenance="test",
    )


def test_per_share_close_with_the_exact_product_passes() -> None:
    """The total is the exact 34-digit 6.214 x the close (#1250), as
    resolve_dec31_close now gives it. Main compared it with its 28-digit
    product, 1247.460500000000000000000000, and refused."""
    close = _long_per_share_close("1247.460500000000000000000000006214")
    valued = value_holding_lot(_lot(), report_date=_DEC31, close=close)
    assert valued.mark_price == _LONG_CLOSE
    assert valued.closing_total_usd is None


def _assert_per_share_refusal(exc: pytest.ExceptionInfo[InvariantError]) -> None:
    assert str(exc.value) == _PER_SHARE_REFUSAL
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_per_share_close_with_the_rounded_product_refused() -> None:
    """Main's 28-digit product is not the exact one, so the close wasn't
    resolved for this quantity at full precision (#1250). Main's check
    rounded the same way and passed it."""
    close = _long_per_share_close("1247.460500000000000000000000")
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(_lot(), report_date=_DEC31, close=close)
    _assert_per_share_refusal(exc)


def test_per_share_close_whose_product_cannot_be_exact_refused() -> None:
    """6.214 x (1E+998 + 1) needs 1002 digits, past the 1000-digit cap, so
    the check can't prove the total and refuses (#1250). Main's 28-digit
    product equals the 6.214E+998 total, so main passed it."""
    close = _long_per_share_close("6.214E+998", per_share=Decimal("1" + "0" * 997 + "1"))
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(_lot(), report_date=_DEC31, close=close)
    _assert_per_share_refusal(exc)


def test_missing_total_cannot_validate_an_unavailable_product() -> None:
    """Preservation: an unvalidated direct close must not pass None == None."""
    close = replace(
        _long_per_share_close("6.214E+998", per_share=Decimal("1" + "0" * 997 + "1")),
        total_usd=cast(Decimal, None),
    )
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(_lot(), report_date=_DEC31, close=close)
    _assert_per_share_refusal(exc)


def test_non_usd_lot_refused() -> None:
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(
            _lot(currency="EUR"),
            report_date=_DEC31,
            close=_per_share_close(CloseSource.YAHOO_FALLBACK),
        )
    assert "needs a USD lot" in exc.value.what


def test_report_date_before_acquisition_refused() -> None:
    with pytest.raises(InvariantError) as exc:
        value_holding_lot(
            _lot(),
            report_date=_VEST_DATE.replace(day=13),
            close=_per_share_close(CloseSource.YAHOO_FALLBACK),
        )
    assert "open_date_time must be on or before report_date" in exc.value.what


def test_matches_the_pre_627_stockplan_open_lot() -> None:
    # The OpenLot the StockPlan pipeline built itself before #627, for
    # the same vest and Yahoo close, less the fields #629 removed.
    expected = OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQCB"),
        description="SAMPLE ISSUER CB Inc.",
        isin=Isin("US99971P1012"),
        issuer_country_code=CountryCode("US"),
        report_date=_DEC31,
        quantity=_SHARES,
        mark_price=_CLOSE,
        cost_basis_money=_FMV * _SHARES,
        side=Side.LONG,
        open_date_time=_VEST_DATE,
    )
    close = _per_share_close(CloseSource.YAHOO_FALLBACK)
    assert value_holding_lot(_lot(), report_date=_DEC31, close=close) == expected


# ---- #1242: a statement total files its exact closing value ------------------

_RATE = Decimal("82.50")
_THREE = Decimal("3")
_ONE_DOLLAR = Decimal("1.00")


def _series(price: str) -> dict[Ticker, PriceSeries]:
    """A close of ``price`` on every US market session of 2025, from the one
    that seeds January 1."""
    sessions = us_market_sessions(previous_us_market_session(date(2025, 1, 1)), _DEC31)
    return {
        Ticker("XQCB"): PriceSeries(
            ticker=Ticker("XQCB"),
            currency="USD",
            closes=dict.fromkeys(sessions, Decimal(price)),
            has_split_in_range=False,
            source="synthetic",
            fetched_at=datetime(2026, 1, 1),
        )
    }


# The two shapes value_holding_lot values: a NetBenefits ESPP purchase, always
# bought on Dec-31, and a StockPlan vest held from mid-year. Neither pipeline
# resolves a statement total today (both pass statement_total_usd=None), so
# they reach this path only through value_holding_lot.
_SHARED_CALLER_LOTS = pytest.mark.parametrize(
    "acquired", [_DEC31, _VEST_DATE], ids=["netbenefits-espp-dec31", "stockplan-vest"]
)


@_SHARED_CALLER_LOTS
def test_statement_total_files_the_exact_closing_and_peak(acquired: date) -> None:
    """3 shares worth $1.00 at ₹82.50/$ file ₹83 closing and ₹83 peak: the
    exact ₹82.50, not 3 x the 28-digit quotient (₹82.4999…, filed ₹82). The
    Dec-31 purchase takes its closing value as its peak; the vest's Yahoo
    peak ($0.9999) sits below the total, so it floors at the closing value."""
    lot = _lot(quantity=_THREE, cost_basis_money=Decimal("0.99"), acquisition_date=acquired)
    valued = value_holding_lot(lot, report_date=_DEC31, close=_statement_close(_ONE_DOLLAR))
    [row] = build_fa_a3_rows(
        overlapping_lots_for_cy([valued], [], calendar_year=2025),
        calendar_year=2025,
        fx_policy=AuditedFxStub(_RATE),
        prices_by_ticker=_series("0.3333"),
    )
    assert row.closing_value_inr == row.peak_value_inr == Decimal("82.5")
    assert row.filed.closing_value_inr == row.filed.peak_value_inr == Decimal("83")
    assert row.audit_close_price_usd == valued.mark_price == _ONE_DOLLAR / _THREE


@_SHARED_CALLER_LOTS
def test_per_share_close_files_unchanged(acquired: date) -> None:
    """Control: a per-share close keeps quantity x mark, as before #1242."""
    lot = _lot(quantity=_THREE, cost_basis_money=Decimal("0.99"), acquisition_date=acquired)
    close = ResolvedClose(
        total_usd=Decimal("1.02"),
        per_share_usd=Decimal("0.34"),
        source=CloseSource.YAHOO_FALLBACK,
        provenance="test",
    )
    valued = value_holding_lot(lot, report_date=_DEC31, close=close)
    [row] = build_fa_a3_rows(
        overlapping_lots_for_cy([valued], [], calendar_year=2025),
        calendar_year=2025,
        fx_policy=AuditedFxStub(_RATE),
        prices_by_ticker=_series("0.34"),
    )
    assert row.closing_value_inr == row.peak_value_inr == Decimal("84.15")


def test_statement_total_files_the_exact_closing_main_refused_at_the_filing_bound() -> None:
    """The exact total x rate is just below the cutoff and files
    ₹99,999,999,999,999 closing and peak. #1242 kept main's refusal by
    re-checking the old reconstructed close, whose rounded quotient
    arithmetic reaches the cutoff; #1250 drops that re-check because the
    refusal was a rounding artefact (the owner's "exact, never round" rule,
    #1166), so the input now passes. Main refused it."""
    lot = _lot(quantity=_THREE, cost_basis_money=Decimal("0.99"), acquisition_date=_DEC31)
    close = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=lot.quantity,
        statement_total_usd=Decimal(
            "1212121212121.20606060606060606060606060606060606060606060606"
        ),
        series=None,
    )
    with localcontext() as context:
        context.prec = 28
        valued = value_holding_lot(lot, report_date=_DEC31, close=close)
        [row] = build_fa_a3_rows(
            overlapping_lots_for_cy([valued], [], calendar_year=2025),
            calendar_year=2025,
            fx_policy=AuditedFxStub(_RATE),
        )
    exact = Decimal("99999999999999.4999999999999999999999999999999999999999999999500")
    assert row.closing_value_inr.compare_total(exact) == 0
    assert row.peak_value_inr is not None
    assert row.peak_value_inr.compare_total(exact) == 0
    assert row.filed.closing_value_inr == row.filed.peak_value_inr == Decimal("99999999999999")


def test_statement_total_below_the_filing_bound_still_passes() -> None:
    """A nearby total that passed main still files the maximum allowed value."""
    lot = _lot(quantity=_THREE, cost_basis_money=Decimal("0.99"), acquisition_date=_DEC31)
    close = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=lot.quantity,
        statement_total_usd=Decimal("1212121212121.2"),
        series=None,
    )
    with localcontext() as context:
        context.prec = 28
        valued = value_holding_lot(lot, report_date=_DEC31, close=close)
        [row] = build_fa_a3_rows(
            overlapping_lots_for_cy([valued], [], calendar_year=2025),
            calendar_year=2025,
            fx_policy=AuditedFxStub(_RATE),
        )
    assert row.closing_value_inr == row.peak_value_inr == Decimal("99999999999999.0")
    assert row.filed.closing_value_inr == row.filed.peak_value_inr == Decimal("99999999999999")


def _statement_peak_bound_row(basis: PeakBasis, price: Decimal) -> FaA3Row:
    """A closing below the bound and a native peak whose earlier FX is higher."""
    acquired = date(2025, 12, 30)
    lot = _lot(quantity=_THREE, cost_basis_money=Decimal("0.99"), acquisition_date=acquired)
    series = _series(str(price))[lot.symbol]
    series = replace(series, highs=dict(series.closes))
    close = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=lot.quantity,
        statement_total_usd=Decimal("1000000000000"),
        series=series,
    )
    with localcontext() as context:
        context.prec = 28
        valued = value_holding_lot(lot, report_date=_DEC31, close=close)
        [row] = build_fa_a3_rows(
            overlapping_lots_for_cy([valued], [], calendar_year=2025),
            calendar_year=2025,
            fx_policy=AuditedFxStub({acquired: Decimal("100"), _DEC31: Decimal("80")}),
            prices_by_ticker={lot.symbol: series},
            peak_basis=basis,
        )
    return row


@pytest.mark.parametrize("basis", ["close", "high"])
def test_statement_total_floors_or_blanks_the_over_bound_peak_main_refused(
    basis: PeakBasis,
) -> None:
    """The over-bound candidate is floored to the closing value or blanked,
    as in the ``...3332`` control below. #1242 kept main's refusal by
    re-checking the rounded quotient arithmetic against the bound; #1250
    drops that re-check because the refusal was a rounding artefact (the
    owner's "exact, never round" rule, #1166), so the input now passes.
    Main refused it."""
    row = _statement_peak_bound_row(basis, Decimal("333333333333.3333333333333333"))
    assert row.filed.closing_value_inr == Decimal("80000000000000")
    assert row.filed.peak_value_inr == {"close": Decimal("80000000000000"), "high": None}[basis]


@pytest.mark.parametrize("basis", ["close", "high"])
def test_statement_total_keeps_an_existing_peak_floor_or_blank(basis: PeakBasis) -> None:
    """An over-bound candidate main already replaced or blanked still passes."""
    row = _statement_peak_bound_row(basis, Decimal("333333333333.3333333333333332"))
    assert row.filed.closing_value_inr == Decimal("80000000000000")
    assert row.filed.peak_value_inr == {"close": Decimal("80000000000000"), "high": None}[basis]
