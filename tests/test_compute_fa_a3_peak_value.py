"""Tests for :mod:`ibkr_fa.compute.fa_a3_peak_value` (issue #14).

Covers native-currency peak selection, the optional maximum-daily-INR
selection, FX/price carry-forward behaviour, and
every "cannot value with certainty -> None (manual-fill)" refusal path
identified in the design rubber-duck: missing series, non-USD currency,
split-in-range, price staleness gap, missing TTBR, and empty holding
windows.
"""

from __future__ import annotations

import functools
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from typing import ClassVar

import pytest

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.compute.fa_a3_peak_value import (
    NonSessionPriceBarError,
    PeakValue,
    peak_value,
    resolve_fa_a3_peak,
)
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models import BasisProvenance, ClosedLot, FaA3PeakBlankReason, OpenLot, Side
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.types import CountryCode, Isin, PeakBasis, PeakCurrency, Ticker
from tests._crash_reports import assert_prints_no_marker
from tests._exact_refusals import INEXACT_PRODUCT

# A symbol shaped like an account number, for the #1047 and #1331 absent checks.
_MARKER = Ticker("U1234567")


def _assert_non_session_refusal(
    error: NonSessionPriceBarError, *, weekend: bool, day: date
) -> None:
    """``error`` prints only the kind of day and holds only ``weekend``: the
    lot's symbol (:data:`_MARKER`) and the bar's ``day`` are absent from its
    ``str``, ``repr``, args, fields and chain (#1047, #1331)."""
    kind, row = (
        ("a weekend day", "a Saturday or Sunday")
        if weekend
        else ("a listed US market closure", "a weekday the US market was closed")
    )
    assert str(error) == (
        f"a holding's price series has a bar dated on {kind}; the price source and the US "
        "market calendar disagree, so the Schedule FA Table A3 peak value is refused. Move "
        "each .csv file directly in the price cache directory (--price-cache-dir; by default a "
        ".ibkr-fa-price-cache folder that the command's --help locates) with a row dated on "
        f"{row}, and any .lock beside it, out of that directory, then re-run with price "
        "fetching on (without --no-fetch-prices): a fresh fetch can return the series without "
        "that bar. If the fresh series still has it, enter this command's Schedule FA by hand."
    )
    assert_prints_no_marker(
        error, markers=(_MARKER, day.isoformat(), f"{day.year}, {day.month}, {day.day}")
    )
    assert error == NonSessionPriceBarError(weekend=weekend)


class _CarryForwardPolicy:
    """FxPolicy double mimicking FaExactDatePolicy carry-forward."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rates: dict[date, Decimal], *, max_days: int = 14) -> None:
        self._rates = rates
        self._sorted = sorted(rates)
        self._max_days = max_days

    def rate_for(self, event_date: date) -> Decimal:
        if event_date in self._rates:
            return self._rates[event_date]
        prior = [d for d in self._sorted if d < event_date]
        if prior and (event_date - prior[-1]).days <= self._max_days:
            return self._rates[prior[-1]]
        raise MissingFxRateError(
            currency=self.currency,
            last_available=prior[-1] if prior else None,
            max_carry_forward_days=self._max_days,
        )


class _StrictPolicy:
    """FxPolicy double that raises for any unlisted date (no carry)."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rates: dict[date, Decimal]) -> None:
        self._rates = rates

    def rate_for(self, event_date: date) -> Decimal:
        try:
            return self._rates[event_date]
        except KeyError as exc:
            raise MissingFxRateError(
                currency=self.currency,
                last_available=None,
                max_carry_forward_days=0,
            ) from exc


def _series(
    closes: dict[date, Decimal],
    *,
    currency: str = "USD",
    has_split_in_range: bool = False,
    highs: dict[date, Decimal] | None = None,
    split_ratios: dict[date, Decimal] | None = None,
    price_basis: str = "current-split-basis",
) -> PriceSeries:
    return PriceSeries(
        ticker=Ticker("TEST"),
        currency=currency,
        closes=closes,
        highs=highs or {},
        has_split_in_range=has_split_in_range,
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        split_ratios=split_ratios or {},
        price_basis=price_basis,
    )


def _open_lot(
    *,
    open_date: date,
    quantity: Decimal = Decimal("1"),
    report_date: date = date(2025, 12, 31),
    mark_price: Decimal = Decimal("100"),
) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("TEST"),
        description="TEST CO",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=report_date,
        quantity=quantity,
        mark_price=mark_price,
        cost_basis_money=quantity * Decimal("90"),
        side=Side.LONG,
        open_date_time=open_date,
    )


def _closed_lot(
    *, open_date: date, close_date: date, quantity: Decimal = Decimal("1")
) -> ClosedLot:
    cost = quantity * Decimal("90")
    proceeds = quantity * Decimal("100")
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("TEST"),
        description="TEST CO",
        isin=Isin("US99990E1038"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=quantity,
        cost_basis_money=cost,
        proceeds_money=proceeds,
        open_date_time=open_date,
        close_date_time=close_date,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


def _peak_inr(lot: OpenLot | ClosedLot, **kw) -> Decimal | FaA3PeakBlankReason:
    """``peak_value(...).amount_inr``, or the reason the peak is unresolved."""
    result = peak_value(lot, **kw)
    return result if isinstance(result, FaA3PeakBlankReason) else result.amount_inr


# ---- formula / happy path -------------------------------------------------


def test_peak_defaults_to_native_currency_before_conversion() -> None:
    lot = _closed_lot(open_date=date(2025, 3, 3), close_date=date(2025, 3, 6))
    series = _series(
        {
            date(2025, 3, 3): Decimal("100"),
            date(2025, 3, 4): Decimal("110"),  # highest USD price
            date(2025, 3, 5): Decimal("105"),
        }
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 3, 3): Decimal("80"),
            date(2025, 3, 4): Decimal("80"),
            date(2025, 3, 5): Decimal("90"),  # weak INR -> highest product
        }
    )
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.amount_inr == Decimal("110") * Decimal("80")
    assert result.on_date == date(2025, 3, 4)
    assert result.price_usd == Decimal("110")
    assert result.ttbr == Decimal("80")


def test_inr_override_selects_maximum_daily_converted_value() -> None:
    lot = _closed_lot(open_date=date(2025, 3, 3), close_date=date(2025, 3, 6))
    series = _series(
        {
            date(2025, 3, 3): Decimal("100"),
            date(2025, 3, 4): Decimal("110"),
            date(2025, 3, 5): Decimal("105"),
        }
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 3, 3): Decimal("80"),
            date(2025, 3, 4): Decimal("80"),
            date(2025, 3, 5): Decimal("90"),
        }
    )
    result = peak_value(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        peak_currency="inr",
    )
    assert isinstance(result, PeakValue)
    assert (result.amount_inr, result.on_date) == (
        Decimal("105") * Decimal("90"),
        date(2025, 3, 5),
    )


def test_peak_scales_with_quantity() -> None:
    lot = _closed_lot(
        open_date=date(2025, 3, 3), close_date=date(2025, 3, 5), quantity=Decimal("2.5")
    )
    series = _series({date(2025, 3, 3): Decimal("100"), date(2025, 3, 4): Decimal("100")})
    fx = _CarryForwardPolicy({date(2025, 3, 3): Decimal("80"), date(2025, 3, 4): Decimal("80")})
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.amount_inr == Decimal("100") * Decimal("2.5") * Decimal("80")


def test_price_and_ttbr_carry_forward_over_weekend() -> None:
    """Fri close + Fri TTBR carry forward across Sat/Sun (#14 conservative)."""
    lot = _closed_lot(open_date=date(2025, 3, 7), close_date=date(2025, 3, 11))  # Fri..Tue
    series = _series(
        {
            date(2025, 3, 7): Decimal("120"),  # Friday
            date(2025, 3, 10): Decimal("118"),  # Monday
        }
    )
    fx = _CarryForwardPolicy({date(2025, 3, 7): Decimal("85"), date(2025, 3, 10): Decimal("86")})
    # Window [Fri 7, Mon 10]. Sat/Sun carry Fri close 120 & Fri rate 85
    # -> 10200, which beats Mon 118*86=10148.
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.amount_inr == Decimal("120") * Decimal("85")
    assert result.on_date == date(2025, 3, 7)


def test_open_lot_window_runs_to_dec31() -> None:
    lot = _open_lot(open_date=date(2025, 12, 29))
    series = _series(
        {
            date(2025, 12, 29): Decimal("200"),
            date(2025, 12, 30): Decimal("260"),  # peak
            date(2025, 12, 31): Decimal("210"),
        }
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 12, 29): Decimal("85"),
            date(2025, 12, 30): Decimal("85"),
            date(2025, 12, 31): Decimal("85"),
        }
    )
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.on_date == date(2025, 12, 30)
    assert result.amount_inr == Decimal("260") * Decimal("85")


def test_pre_cy_lot_window_starts_jan1_seeded_by_prior_close() -> None:
    """A lot opened last year: window starts Jan 1, seeded by the Dec 31 close."""
    lot = _open_lot(open_date=date(2024, 11, 15))
    closes = {
        date(2024, 12, 30): Decimal("150"),
        date(2024, 12, 31): Decimal("155"),  # seeds Jan 1 (holiday)
        date(2025, 1, 2): Decimal("151"),
    }
    # CY window is Jan 1..Dec 31 2025; provide rates Jan1..Jan2 then the
    # series only covers early Jan, so the rest of the year would be
    # stale -> use a closed lot to bound the window instead.
    closed = _closed_lot(open_date=date(2024, 11, 15), close_date=date(2025, 1, 3))
    fx = _CarryForwardPolicy({date(2024, 12, 30): Decimal("85"), date(2025, 1, 2): Decimal("85")})
    result = peak_value(closed, series=_series(closes), fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    # Window [Jan 1, Jan 2]. Jan 1 carries the Dec 31 close 155; Jan 2 = 151.
    assert (result.on_date, result.price_usd) == (date(2025, 1, 1), Decimal("155"))
    assert result.amount_inr == Decimal("155") * Decimal("85")
    assert lot.open_date_time.year == 2024  # sanity on the helper
    # Dec 31 was a session, so without its bar the Dec 30 close is not carried (#939).
    del closes[date(2024, 12, 31)]
    assert (
        peak_value(closed, series=_series(closes), fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


def test_single_day_window_open_dec31() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    series = _series({date(2025, 12, 31): Decimal("300")})
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("84")})
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.amount_inr == Decimal("300") * Decimal("84")


def test_closed_lot_window_ends_day_before_disposal() -> None:
    """The disposal date itself is excluded (EOD-held convention)."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 4))
    series = _series(
        {
            date(2025, 6, 2): Decimal("100"),
            date(2025, 6, 3): Decimal("101"),
            date(2025, 6, 4): Decimal("999"),  # disposal day — must be ignored
        }
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 6, 2): Decimal("80"),
            date(2025, 6, 3): Decimal("80"),
            date(2025, 6, 4): Decimal("80"),
        }
    )
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.on_date == date(2025, 6, 3)
    assert result.amount_inr == Decimal("101") * Decimal("80")


# ---- refusal paths (-> reason / manual-fill) ------------------------------


@pytest.mark.parametrize(
    ("prices_fetched", "reason"),
    [
        (False, FaA3PeakBlankReason.PRICES_NOT_FETCHED),
        (True, FaA3PeakBlankReason.NO_USD_SERIES),
    ],
)
def test_none_series_reason_follows_whether_prices_were_fetched(
    prices_fetched: bool, reason: FaA3PeakBlankReason
) -> None:
    lot = _open_lot(open_date=date(2025, 12, 29))
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("85")})
    assert (
        peak_value(
            lot, series=None, fx_policy=fx, calendar_year=2025, prices_fetched=prices_fetched
        )
        is reason
    )
    assert (
        resolve_fa_a3_peak(
            lot,
            series=None,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("8500"),
            prices_fetched=prices_fetched,
        )
        is reason
    )


def test_peak_value_without_a_fetch_flag_reads_as_not_fetched() -> None:
    lot = _open_lot(open_date=date(2025, 12, 29))
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("85")})
    assert (
        peak_value(lot, series=None, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.PRICES_NOT_FETCHED
    )


def test_non_usd_currency_returns_none() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    series = _series({date(2025, 12, 31): Decimal("300")}, currency="GBP")
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("84")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.NO_USD_SERIES
    )


def test_split_in_range_returns_none() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    series = _series({date(2025, 12, 31): Decimal("300")}, has_split_in_range=True)
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("84")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.SPLIT
    )


def test_contemporaneous_series_still_refuses_split_inside_holding_window() -> None:
    lot = _open_lot(open_date=date(2025, 1, 2), quantity=Decimal("100"))
    series = _series(
        {
            date(2025, 1, 2): Decimal("400"),
            date(2025, 6, 10): Decimal("100"),
        },
        has_split_in_range=True,
        split_ratios={date(2025, 6, 10): Decimal("4")},
        price_basis=CONTEMPORANEOUS_BASIS,
    )
    fx = _CarryForwardPolicy({date(2025, 1, 2): Decimal("80")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.SPLIT
    )


def _split_boundary_peak(
    split_day: date, *, basis: PeakBasis, price_basis: str = CONTEMPORANEOUS_BASIS
) -> PeakValue | FaA3PeakBlankReason:
    """The peak of a lot held 2025-06-02 to 2025-06-05 with one split on ``split_day``."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 5))
    days = [date(2025, 6, day) for day in range(2, 6)]
    series = _series(
        dict.fromkeys(days, Decimal("100")),
        highs=dict.fromkeys(days, Decimal("101")),
        has_split_in_range=True,
        split_ratios={split_day: Decimal("2")},
        price_basis=price_basis,
    )
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    return peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis=basis)


@pytest.mark.parametrize("basis", ["close", "high"])
@pytest.mark.parametrize(
    "split_day",
    [
        date(2025, 6, 1),  # before acquisition
        date(2025, 6, 2),  # on the first in-year day
        date(2025, 6, 6),  # after disposal
    ],
)
def test_contemporaneous_split_outside_lot_window_keeps_peak(
    split_day: date, basis: PeakBasis
) -> None:
    assert isinstance(_split_boundary_peak(split_day, basis=basis), PeakValue)


@pytest.mark.parametrize("basis", ["close", "high"])
@pytest.mark.parametrize(
    "split_day",
    [
        date(2025, 6, 3),  # after the first in-year day
        date(2025, 6, 5),  # on the disposal date
    ],
)
def test_contemporaneous_split_inside_lot_window_refuses(split_day: date, basis: PeakBasis) -> None:
    assert _split_boundary_peak(split_day, basis=basis) is FaA3PeakBlankReason.SPLIT


@pytest.mark.parametrize("basis", ["close", "high"])
def test_current_split_basis_series_refuses_split_after_disposal(basis: PeakBasis) -> None:
    assert (
        _split_boundary_peak(date(2025, 6, 6), basis=basis, price_basis="current-split-basis")
        is FaA3PeakBlankReason.SPLIT
    )


def test_price_staleness_gap_returns_none() -> None:
    """A gap larger than the staleness ceiling cannot be carried forward."""
    lot = _closed_lot(open_date=date(2025, 2, 20), close_date=date(2025, 2, 21))
    # Nearest prior close is 30 days before the window day -> too stale.
    series = _series({date(2025, 1, 21): Decimal("100")})
    fx = _CarryForwardPolicy({date(2025, 2, 20): Decimal("80")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.STALE_CLOSE
    )


def test_no_close_before_window_returns_none() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 3))
    series = _series({date(2025, 8, 1): Decimal("100")})  # only after the window
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.STALE_CLOSE
    )


def test_native_peak_needs_ttbr_only_on_selected_day() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 4))
    series = _series({date(2025, 6, 2): Decimal("100"), date(2025, 6, 3): Decimal("100")})
    fx = _StrictPolicy({date(2025, 6, 2): Decimal("80")})  # 6/3 missing -> raises
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert isinstance(result, PeakValue)
    assert result.on_date == date(2025, 6, 2)
    assert (
        peak_value(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            peak_currency="inr",
        )
        is FaA3PeakBlankReason.NO_TTBR
    )


def test_native_peak_missing_selected_ttbr_fails_closed() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 4))
    fx = _StrictPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        peak_value(
            lot,
            series=_series(
                {
                    date(2025, 6, 2): Decimal("100"),
                    date(2025, 6, 3): Decimal("110"),
                }
            ),
            fx_policy=fx,
            calendar_year=2025,
        )
        is FaA3PeakBlankReason.NO_TTBR
    )
    assert (
        peak_value(
            lot,
            series=_series(
                {
                    date(2025, 6, 2): Decimal("110"),
                    date(2025, 6, 3): Decimal("100"),
                    date(2025, 6, 4): Decimal("100"),
                },
                highs={
                    date(2025, 6, 2): Decimal("111"),
                    date(2025, 6, 3): Decimal("120"),
                    date(2025, 6, 4): Decimal("105"),
                },
            ),
            fx_policy=fx,
            calendar_year=2025,
            basis="high",
        )
        is FaA3PeakBlankReason.NO_TTBR
    )


def test_same_day_open_and_close_is_empty_window() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 2))
    series = _series({date(2025, 6, 2): Decimal("100")})
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.EMPTY_WINDOW
    )


def test_disposed_on_jan1_is_empty_window() -> None:
    lot = _closed_lot(open_date=date(2024, 12, 1), close_date=date(2025, 1, 1))
    series = _series({date(2024, 12, 30): Decimal("100")})
    fx = _CarryForwardPolicy({date(2024, 12, 30): Decimal("80")})
    assert (
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
        is FaA3PeakBlankReason.EMPTY_WINDOW
    )


# ---- refuse_disposal_day_peak (Fidelity CMA, #622) -------------------------

_DISPOSAL_LOT = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 4))


@pytest.mark.parametrize(
    ("disposal_close", "disposal_ttbr", "peak_currency", "expected_date"),
    [
        # The disposal day is the strict maximum: refused in both currencies.
        (Decimal("102"), Decimal("80"), "native", None),
        (Decimal("102"), Decimal("80"), "inr", None),
        # Only its TTBR is higher: a native tie keeps 06-03, INR selection
        # makes the disposal day the strict maximum.
        (Decimal("101"), Decimal("81"), "native", date(2025, 6, 3)),
        (Decimal("101"), Decimal("81"), "inr", None),
        # A tie with the earlier maximum keeps the earlier winner.
        (Decimal("101"), Decimal("80"), "native", date(2025, 6, 3)),
        (Decimal("101"), Decimal("80"), "inr", date(2025, 6, 3)),
    ],
)
def test_refuse_disposal_day_peak_refuses_only_a_disposal_day_winner(
    disposal_close: Decimal,
    disposal_ttbr: Decimal,
    peak_currency: PeakCurrency,
    expected_date: date | None,
) -> None:
    series = _series(
        {
            date(2025, 6, 2): Decimal("100"),
            date(2025, 6, 3): Decimal("101"),
            date(2025, 6, 4): disposal_close,
        }
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 6, 2): Decimal("80"),
            date(2025, 6, 3): Decimal("80"),
            date(2025, 6, 4): disposal_ttbr,
        }
    )
    result = peak_value(
        _DISPOSAL_LOT,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        peak_currency=peak_currency,
        refuse_disposal_day_peak=True,
    )
    if expected_date is None:
        assert result is FaA3PeakBlankReason.DISPOSAL_DAY_PEAK
    else:
        assert isinstance(result, PeakValue)
        assert (result.on_date, result.amount_inr) == (
            expected_date,
            Decimal("101") * Decimal("80"),
        )
    # The default window ignores the disposal day and always files 06-03.
    default = peak_value(
        _DISPOSAL_LOT,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        peak_currency=peak_currency,
    )
    assert isinstance(default, PeakValue)
    assert default.on_date == date(2025, 6, 3)


@pytest.mark.parametrize(
    ("open_date", "close_date"),
    [(date(2025, 6, 2), date(2025, 6, 2)), (date(2024, 12, 1), date(2025, 1, 1))],
    ids=["same-day", "disposed-on-jan1"],
)
def test_refuse_disposal_day_peak_refuses_a_disposal_only_window(
    open_date: date, close_date: date
) -> None:
    lot = _closed_lot(open_date=open_date, close_date=close_date)
    series = _series({date(2024, 12, 31): Decimal("100"), date(2025, 6, 2): Decimal("100")})
    fx = _CarryForwardPolicy({date(2024, 12, 31): Decimal("80"), date(2025, 6, 2): Decimal("80")})
    assert (
        peak_value(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            refuse_disposal_day_peak=True,
        )
        is FaA3PeakBlankReason.DISPOSAL_DAY_PEAK
    )


def test_refuse_disposal_day_peak_leaves_open_lots_and_reaches_the_resolver() -> None:
    open_lot = _open_lot(open_date=date(2025, 12, 29), mark_price=Decimal("100"))
    series = _series(
        {
            date(2025, 6, 2): Decimal("100"),
            date(2025, 6, 3): Decimal("101"),
            date(2025, 6, 4): Decimal("102"),
            date(2025, 12, 29): Decimal("100"),
            date(2025, 12, 30): Decimal("103"),
            date(2025, 12, 31): Decimal("100"),
        }
    )
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80"), date(2025, 12, 29): Decimal("85")})

    def resolve(lot: OpenLot | ClosedLot, *, refuse: bool) -> Decimal | FaA3PeakBlankReason:
        return resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("0") if isinstance(lot, ClosedLot) else Decimal("8500"),
            refuse_disposal_day_peak=refuse,
        )

    assert resolve(open_lot, refuse=True) == resolve(open_lot, refuse=False) == Decimal("8755")
    assert resolve(_DISPOSAL_LOT, refuse=False) == Decimal("8080")
    assert resolve(_DISPOSAL_LOT, refuse=True) is FaA3PeakBlankReason.DISPOSAL_DAY_PEAK


# ==========================================================================
# HIGH basis (schedule_fa.peak_basis = "high") — panel-reviewed (4 rounds)
# ==========================================================================

# A full Mon-Fri week of trading bars (2025-06-02 .. 06-06).
_WEEK = [date(2025, 6, d) for d in (2, 3, 4, 5, 6)]
_WEEK_CLOSES = {d: Decimal("100") + Decimal(i) for i, d in enumerate(_WEEK)}
_WEEK_HIGHS = {d: c + Decimal("10") for d, c in _WEEK_CLOSES.items()}


def test_high_basis_dec31_open_lot_uses_intraday_high() -> None:
    """The primary ESPP case: a Dec-31 open lot -> peak = Dec-31 HIGH x qty x TTBR
    (strictly above the close-basis peak)."""
    lot = _open_lot(open_date=date(2025, 12, 31), quantity=Decimal("2"))
    series = _series(
        {date(2025, 12, 31): Decimal("100")},
        highs={date(2025, 12, 31): Decimal("105")},
    )
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("90")})
    high = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    close = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="close")
    assert high == Decimal("105") * Decimal("2") * Decimal("90")  # 18900
    assert close == Decimal("100") * Decimal("2") * Decimal("90")  # 18000
    assert isinstance(high, Decimal) and isinstance(close, Decimal)
    assert high > close


def test_high_basis_missing_dec31_high_fails_closed() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    series = _series({date(2025, 12, 31): Decimal("100")})  # close only, no high
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("90")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
    )


def test_high_basis_no_series_fails_closed() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("90")})
    assert (
        _peak_inr(lot, series=None, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.PRICES_NOT_FETCHED
    )


def test_high_basis_closed_lot_includes_disposal_day() -> None:
    """The high window is disposal-INCLUSIVE (panel R2): a closed lot is held for
    part of its disposal day, so that day's high (here the period max) counts."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 6))
    series = _series(_WEEK_CLOSES, highs=_WEEK_HIGHS)
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    # Disposal-day (06-06) high 114 is the max; excluding it would give 113.
    peak = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert peak == Decimal("114") * Decimal("80")


def test_high_basis_dropped_midweek_bar_fails_closed() -> None:
    """R3 regression lock: a dropped MID-WEEK bar (a 2-day gap that a
    gap-threshold heuristic would wave through) fails closed."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 6))
    highs = dict(_WEEK_HIGHS)
    del highs[date(2025, 6, 4)]  # Wed high dropped; Wed close still present
    series = _series(_WEEK_CLOSES, highs=highs)
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
    )


def test_high_basis_high_below_close_fails_closed() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 6))
    highs = dict(_WEEK_HIGHS)
    highs[date(2025, 6, 4)] = _WEEK_CLOSES[date(2025, 6, 4)] - Decimal("1")  # high < close
    series = _series(_WEEK_CLOSES, highs=highs)
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
    )


def test_high_basis_missing_session_bar_fails_closed() -> None:
    """06-04 (Wed) is a normal session with no bar at all, so it may be a
    dropped trading day: refuse."""
    days = [date(2025, 6, d) for d in (2, 3, 5, 6)]  # 06-04 (Wed) absent entirely
    closes = {d: Decimal("100") for d in days}
    highs = {d: Decimal("110") for d in days}
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 6))
    series = _series(closes, highs=highs)
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


def test_high_basis_window_spanning_listed_closure_is_valued() -> None:
    """2025-07-04 (Fri) is a listed NYSE closure, so it needs no bar. The close
    basis carries Thursday's close across it (#939)."""
    lot = _closed_lot(open_date=date(2025, 7, 2), close_date=date(2025, 7, 7))
    series = _series(
        {
            date(2025, 7, 2): Decimal("100"),
            date(2025, 7, 3): Decimal("101"),
            date(2025, 7, 7): Decimal("102"),
        },
        highs={
            date(2025, 7, 2): Decimal("110"),
            date(2025, 7, 3): Decimal("115"),
            date(2025, 7, 7): Decimal("112"),
        },
    )
    fx = _CarryForwardPolicy({date(2025, 7, 2): Decimal("80")})
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert isinstance(result, PeakValue)
    assert (result.on_date, result.price_usd) == (date(2025, 7, 3), Decimal("115"))
    assert result.amount_inr == Decimal("115") * Decimal("80")
    close = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis="close")
    assert isinstance(close, PeakValue)
    assert (close.on_date, close.price_usd) == (date(2025, 7, 3), Decimal("101"))


@pytest.mark.parametrize("basis", ["close", "high"])
def test_bar_on_listed_closure_refuses(basis: PeakBasis) -> None:
    """A bar on 2025-07-04 contradicts the calendar, so refuse (owner decision
    on #620, a typed error since #895) rather than trust or drop either source.
    The error says it is a listed closure, without the symbol or the date
    (#1047, #1331)."""
    days = [date(2025, 7, d) for d in (2, 3, 4, 7)]
    lot = replace(
        _closed_lot(open_date=date(2025, 7, 2), close_date=date(2025, 7, 7)), symbol=_MARKER
    )
    series = replace(
        _series({d: Decimal("100") for d in days}, highs={d: Decimal("110") for d in days}),
        ticker=_MARKER,
    )
    fx = _CarryForwardPolicy({date(2025, 7, 2): Decimal("80")})
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis=basis)
    _assert_non_session_refusal(exc.value, weekend=False, day=date(2025, 7, 4))


@pytest.mark.parametrize("basis", ["close", "high"])
def test_uncovered_year_needs_a_bar_on_an_unlisted_holiday(basis: PeakBasis) -> None:
    """The calendar does not cover CY2024, so 2024-07-04 (Thu) counts as a
    session: with no bar it fails closed under either basis (#939)."""
    days = [date(2024, 7, d) for d in (2, 3, 5)]
    lot = _closed_lot(open_date=date(2024, 7, 2), close_date=date(2024, 7, 5))
    series = _series({d: Decimal("100") for d in days}, highs={d: Decimal("110") for d in days})
    fx = _CarryForwardPolicy({date(2024, 7, 2): Decimal("80")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2024, basis=basis)
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


def test_high_basis_window_from_new_year_closure_is_valued() -> None:
    """A lot held on 2025-01-01 (Wed, a listed closure) needs no bar that day."""
    lot = _closed_lot(open_date=date(2024, 12, 20), close_date=date(2025, 1, 3))
    series = _series(
        {
            date(2024, 12, 31): Decimal("100"),
            date(2025, 1, 2): Decimal("101"),
            date(2025, 1, 3): Decimal("102"),
        },
        highs={
            date(2024, 12, 31): Decimal("120"),
            date(2025, 1, 2): Decimal("108"),
            date(2025, 1, 3): Decimal("106"),
        },
    )
    fx = _CarryForwardPolicy({date(2024, 12, 31): Decimal("80")})
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert isinstance(result, PeakValue)
    assert (result.on_date, result.price_usd) == (date(2025, 1, 2), Decimal("108"))
    assert result.amount_inr == Decimal("108") * Decimal("80")


def test_high_basis_weekend_only_window_falls_back_to_close_peak() -> None:
    """A window with NO weekday (held only across a weekend) has no intraday high;
    return the (carried-close) close-basis peak, not a refusal."""
    # Acquired Sat 06-07, disposed Sun 06-08 -> high window [06-07, 06-08] (no weekday).
    lot = _closed_lot(open_date=date(2025, 6, 7), close_date=date(2025, 6, 8))
    series = _series({date(2025, 6, 6): Decimal("100")}, highs={date(2025, 6, 6): Decimal("110")})
    fx = _CarryForwardPolicy({date(2025, 6, 6): Decimal("80")})
    close = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="close")
    high = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert high == close  # carried Friday close; no weekend intraday high exists
    assert isinstance(high, Decimal)


def test_high_basis_same_day_round_trip_fails_closed() -> None:
    """Same-day acquire==dispose (intraday round-trip): the close window is empty,
    so the high basis also fails closed to manual-fill (documented, panel R4)."""
    lot = _closed_lot(open_date=date(2025, 6, 4), close_date=date(2025, 6, 4))
    series = _series({date(2025, 6, 4): Decimal("100")}, highs={date(2025, 6, 4): Decimal("110")})
    fx = _CarryForwardPolicy({date(2025, 6, 4): Decimal("80")})
    assert (
        _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
        is FaA3PeakBlankReason.EMPTY_WINDOW
    )


def test_native_peak_ignores_fx_only_weekend_spike() -> None:
    # Held Fri 06-06 .. disposed Tue 06-10; the weekend 06-07/06-08 carries the
    # Friday close, and TTBR spikes on Sat 06-07.
    lot = _closed_lot(open_date=date(2025, 6, 6), close_date=date(2025, 6, 10))
    closes = {
        date(2025, 6, 6): Decimal("100"),
        date(2025, 6, 9): Decimal("100"),
        date(2025, 6, 10): Decimal("100"),
    }
    highs = {d: Decimal("101") for d in closes}  # only a hair above close
    series = _series(closes, highs=highs)
    fx = _CarryForwardPolicy(
        {
            date(2025, 6, 6): Decimal("80"),
            date(2025, 6, 7): Decimal("200"),  # weekend TTBR spike
            date(2025, 6, 9): Decimal("80"),
            date(2025, 6, 10): Decimal("80"),
        }
    )
    close = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="close")
    high = _peak_inr(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert close == Decimal("100") * Decimal("80")
    assert high == Decimal("101") * Decimal("80")


def test_native_close_high_tie_uses_earlier_peak_date() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 7), close_date=date(2025, 6, 10))
    series = _series(
        {
            date(2025, 6, 6): Decimal("120"),
            date(2025, 6, 9): Decimal("100"),
            date(2025, 6, 10): Decimal("100"),
        },
        highs={
            date(2025, 6, 9): Decimal("120"),
            date(2025, 6, 10): Decimal("110"),
        },
    )
    fx = _CarryForwardPolicy(
        {
            date(2025, 6, 6): Decimal("80"),
            date(2025, 6, 9): Decimal("90"),
        }
    )
    result = peak_value(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        basis="high",
    )
    assert isinstance(result, PeakValue)
    assert (result.on_date, result.amount_inr) == (
        date(2025, 6, 7),
        Decimal("120") * Decimal("80"),
    )


def test_inr_override_can_select_fx_only_weekend_spike() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 6), close_date=date(2025, 6, 10))
    closes = {
        date(2025, 6, 6): Decimal("100"),
        date(2025, 6, 9): Decimal("100"),
        date(2025, 6, 10): Decimal("100"),
    }
    series = _series(closes, highs={day: Decimal("101") for day in closes})
    fx = _CarryForwardPolicy(
        {
            date(2025, 6, 6): Decimal("80"),
            date(2025, 6, 7): Decimal("200"),
            date(2025, 6, 9): Decimal("80"),
            date(2025, 6, 10): Decimal("80"),
        }
    )
    close = _peak_inr(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        basis="close",
        peak_currency="inr",
    )
    high = _peak_inr(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        basis="high",
        peak_currency="inr",
    )
    assert close == high == Decimal("100") * Decimal("200")


# ==========================================================================
# resolve_fa_a3_peak — the single peak-resolution rule (floor / refuse / Dec-31)
# ==========================================================================


def test_resolver_close_basis_dec31_open_lot_returns_closing_regardless_of_series() -> None:
    """After checking supplied bars, CLOSE basis + Dec-31 open lot uses closing,
    so a price a cent off the raw close can't move it (panel R1/R2 B1)."""
    lot = _open_lot(open_date=date(2025, 12, 31), quantity=Decimal("1"))
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("90")})
    closing = Decimal("8271.00")
    # Series close differs from the resolved closing (both a cent above and below).
    for other_close in (Decimal("91.00"), Decimal("93.00")):
        series = _series(
            {date(2025, 12, 31): other_close},
            highs={date(2025, 12, 31): other_close + Decimal("1")},
        )
        assert (
            resolve_fa_a3_peak(
                lot,
                series=series,
                fx_policy=fx,
                calendar_year=2025,
                closing_inr=closing,
                basis="close",
            )
            == closing
        )


def test_resolver_close_basis_dec31_open_lot_no_fetch_returns_closing() -> None:
    lot = _open_lot(open_date=date(2025, 12, 31))
    fx = _CarryForwardPolicy({date(2025, 12, 31): Decimal("90")})
    assert resolve_fa_a3_peak(
        lot,
        series=None,
        fx_policy=fx,
        calendar_year=2025,
        closing_inr=Decimal("5000"),
        basis="close",
    ) == Decimal("5000")


def test_resolver_close_basis_floors_at_closing() -> None:
    """A non-Dec-31 open lot whose Yahoo close peak sits below the (IBKR-mark)
    closing floors up to closing (panel R2 Opus N1)."""
    lot = _open_lot(
        open_date=date(2025, 12, 30),
        mark_price=Decimal("110"),
    )  # not Dec-31 -> no short-circuit
    series = _series(
        {date(2025, 12, 30): Decimal("100"), date(2025, 12, 31): Decimal("100")},
        highs={date(2025, 12, 30): Decimal("101"), date(2025, 12, 31): Decimal("101")},
    )
    fx = _CarryForwardPolicy({date(2025, 12, 30): Decimal("90")})
    # Fetched native peak is below the broker's Dec-31 mark -> use closing.
    assert resolve_fa_a3_peak(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        closing_inr=Decimal("9900"),
        basis="close",
    ) == Decimal("9900")


def test_resolver_high_basis_below_closing_refuses_not_floors() -> None:
    """HIGH basis refuses (not floors) when the computed high < closing — flooring
    a HIGH down to a CLOSE value would understate it (panel R2 GPT B2)."""
    lot = _open_lot(
        open_date=date(2025, 12, 30),
        mark_price=Decimal("110"),
    )  # not Dec-31 -> no short-circuit
    series = _series(
        {date(2025, 12, 30): Decimal("100"), date(2025, 12, 31): Decimal("100")},
        highs={date(2025, 12, 30): Decimal("101"), date(2025, 12, 31): Decimal("101")},
    )
    fx = _CarryForwardPolicy({date(2025, 12, 30): Decimal("90")})
    # Fetched native high is below the broker's Dec-31 mark -> refuse.
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("9900"),
            basis="high",
        )
        is FaA3PeakBlankReason.HIGH_BELOW_CLOSING
    )


def test_native_peak_inr_may_be_below_closing_inr_after_fx_conversion() -> None:
    lot = _open_lot(open_date=date(2025, 12, 30), mark_price=Decimal("100"))
    series = _series(
        {
            date(2025, 12, 30): Decimal("110"),
            date(2025, 12, 31): Decimal("100"),
        }
    )
    fx = _StrictPolicy(
        {
            date(2025, 12, 30): Decimal("80"),
            date(2025, 12, 31): Decimal("100"),
        }
    )
    assert resolve_fa_a3_peak(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        closing_inr=Decimal("10000"),
    ) == Decimal("8800")


# $1.00 over 3 shares, as the 28-digit mark a division gives (#1242).
_THIRD = Decimal("0.3333333333333333333333333333")


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("close", Decimal("82.5")), ("high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING)],
    ids=["close", "high"],
)
def test_resolver_floor_compares_the_statement_total(
    basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """#1242: a peak price equal to the 28-digit mark matched 3 x the mark, so
    the old resolver kept that peak (₹82.4999…, filed ₹82) even when given
    the exact ₹82.50 closing. The $1.00 total exceeds it: the close basis
    floors at the closing value and the high basis leaves the peak blank."""
    lot = replace(
        _open_lot(open_date=date(2025, 12, 30), quantity=Decimal("3"), mark_price=_THIRD),
        closing_total_usd=Decimal("1.00"),
    )
    days = (date(2025, 12, 30), date(2025, 12, 31))
    series = _series(dict.fromkeys(days, _THIRD), highs=dict.fromkeys(days, _THIRD))
    fx = _CarryForwardPolicy({date(2025, 12, 30): Decimal("82.50")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("82.5"),
            basis=basis,
        )
        == expected
    )


# A third of a $10.00 statement total, cut to 20 digits (#1243).
_THIRD_OF_TEN = Decimal("3.3333333333333333333")


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("close", Decimal("280.5")), ("high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING)],
    ids=["close", "high"],
)
def test_resolver_floor_compares_the_exact_statement_share(
    basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """#1243: a peak price equal to the share's Decimal cut matched that cut, so the resolver
    kept that peak (₹280.4999…, filed ₹280) even when given the exact ₹280.50 closing. The
    exact share $10/3 exceeds it: the close basis floors at the closing value and the high
    basis leaves the peak blank."""
    lot = replace(
        _open_lot(open_date=date(2025, 12, 30), mark_price=_THIRD_OF_TEN),
        closing_total_usd=_THIRD_OF_TEN,
        closing_total_usd_exact=Fraction(10, 3),
    )
    days = (date(2025, 12, 30), date(2025, 12, 31))
    series = _series(dict.fromkeys(days, _THIRD_OF_TEN), highs=dict.fromkeys(days, _THIRD_OF_TEN))
    fx = _CarryForwardPolicy({date(2025, 12, 30): Decimal("84.15")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("280.5"),
            basis=basis,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("close", Decimal("93.5")), ("high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING)],
    ids=["close", "high"],
)
def test_exact_closing_floor_does_not_round_the_peak_product(
    basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """#1243: rounding quantity times price reverses the exact closing comparison.

    Different peak-day and Dec-31 rates make a missed floor file the wrong peak;
    the high basis must instead leave the inconsistent peak blank.
    """
    with localcontext(Context(prec=60)):
        share = Fraction(2, 3)
        quantity = Decimal(67)
        price = exact_decimal(share / 67)
        assert Fraction(quantity) * Fraction(price) < share < Fraction(quantity * price)
        lot = replace(
            _open_lot(open_date=date(2025, 12, 30), quantity=quantity, mark_price=price),
            closing_total_usd=exact_decimal(share),
            closing_total_usd_exact=share,
        )
        days = (date(2025, 12, 30), date(2025, 12, 31))
        series = _series(dict.fromkeys(days, price), highs=dict.fromkeys(days, price))
        fx = _CarryForwardPolicy({days[0]: Decimal(80), days[1]: Decimal("140.25")})
        assert (
            resolve_fa_a3_peak(
                lot,
                series=series,
                fx_policy=fx,
                calendar_year=2025,
                closing_inr=Decimal("93.5"),
                basis=basis,
            )
            == expected
        )


def test_resolver_closed_lot_not_short_circuited_to_zero_closing() -> None:
    """A ClosedLot has closing 0; it must NOT be short-circuited (that would emit
    peak=0). It flows through the price-series branch -> max(p, 0) = p."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 6))
    series = _series(_WEEK_CLOSES, highs=_WEEK_HIGHS)
    fx = _CarryForwardPolicy({date(2025, 6, 2): Decimal("80")})
    resolved = resolve_fa_a3_peak(
        lot,
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        closing_inr=Decimal("0"),
        basis="close",
    )
    assert isinstance(resolved, Decimal)
    assert resolved > 0  # the computed close-basis peak, not 0


# ==========================================================================
# #895: a bar on a US non-session refuses with a typed error
# ==========================================================================

_FLAT_FX = _CarryForwardPolicy({date(2023, 12, 1): Decimal("80")}, max_days=1000)
_SATURDAY_BAR = date(2025, 6, 7)


def _june_series(*, currency: str = "USD") -> PriceSeries:
    """Weekday closes of $100 and highs of $110, and a Saturday $120 / $130 bar."""
    sessions = (date(2025, 6, 6), date(2025, 6, 9), date(2025, 6, 10))
    return _series(
        {d: Decimal("100") for d in sessions} | {_SATURDAY_BAR: Decimal("120")},
        highs={d: Decimal("110") for d in sessions} | {_SATURDAY_BAR: Decimal("130")},
        currency=currency,
    )


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
@pytest.mark.parametrize("basis", ["close", "high"])
@pytest.mark.parametrize(
    "open_date",
    [date(2025, 6, 6), date(2025, 6, 8)],
    ids=["bar-in-window", "bar-carried-into-window"],
)
def test_weekend_bar_refuses(
    open_date: date, basis: PeakBasis, peak_currency: PeakCurrency
) -> None:
    """Without the check, both lots file ₹9,600 from the Saturday $120 close.
    The error says it is a weekend day, without the symbol or the date
    (#1047, #1331)."""
    lot = replace(_closed_lot(open_date=open_date, close_date=date(2025, 6, 10)), symbol=_MARKER)
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(
            lot,
            series=replace(_june_series(), ticker=_MARKER),
            fx_policy=_FLAT_FX,
            calendar_year=2025,
            basis=basis,
            peak_currency=peak_currency,
        )
    _assert_non_session_refusal(exc.value, weekend=True, day=_SATURDAY_BAR)


@pytest.mark.parametrize("basis", ["close", "high"])
def test_weekend_bar_refuses_in_a_year_the_calendar_does_not_cover(basis: PeakBasis) -> None:
    saturday = date(2024, 6, 8)
    sessions = (date(2024, 6, 7), date(2024, 6, 10), date(2024, 6, 11))
    lot = _closed_lot(open_date=date(2024, 6, 7), close_date=date(2024, 6, 11))
    series = _series(
        {d: Decimal("100") for d in sessions} | {saturday: Decimal("120")},
        highs={d: Decimal("110") for d in sessions} | {saturday: Decimal("130")},
    )
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2024, basis=basis)
    assert exc.value == NonSessionPriceBarError(weekend=True)


@pytest.mark.parametrize(
    ("open_date", "disposal_day", "weekend"),
    [(date(2025, 6, 6), date(2025, 6, 7), True), (date(2025, 7, 3), date(2025, 7, 4), False)],
    ids=["weekend", "listed-closure"],
)
def test_disposal_day_bar_refuses_the_high_basis_only(
    open_date: date, disposal_day: date, weekend: bool
) -> None:
    """Before #895 the weekend case files ₹8,800 from the preceding
    session's $110 high; the holiday case returns None. The close window
    ends the day before disposal, so the close basis never scans that bar."""
    lot = _closed_lot(open_date=open_date, close_date=disposal_day)
    series = _series(
        {open_date: Decimal("100"), disposal_day: Decimal("120")},
        highs={open_date: Decimal("110"), disposal_day: Decimal("130")},
    )
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis="high")
    assert exc.value == NonSessionPriceBarError(weekend=weekend)
    close = _peak_inr(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis="close")
    assert close == Decimal("100") * Decimal("80")


def test_same_day_weekend_lot_refuses_the_high_basis_before_the_empty_close_window() -> None:
    lot = _closed_lot(open_date=_SATURDAY_BAR, close_date=_SATURDAY_BAR)
    series = _series({_SATURDAY_BAR: Decimal("120")}, highs={_SATURDAY_BAR: Decimal("130")})
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis="high")
    assert exc.value == NonSessionPriceBarError(weekend=True)
    # The close basis scans only its (empty) close window: manual-fill.
    assert (
        _peak_inr(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis="close")
        is FaA3PeakBlankReason.EMPTY_WINDOW
    )


def test_resolver_dec31_shortcut_refuses_a_non_session_dec31_bar() -> None:
    sunday = date(2023, 12, 31)
    lot = _open_lot(open_date=sunday, report_date=sunday)
    series = _series({date(2023, 12, 29): Decimal("100"), sunday: Decimal("100")})
    resolve = functools.partial(
        resolve_fa_a3_peak, lot, fx_policy=_FLAT_FX, calendar_year=2023, closing_inr=Decimal("8000")
    )
    with pytest.raises(NonSessionPriceBarError) as exc:
        resolve(series=series)
    assert exc.value == NonSessionPriceBarError(weekend=True)
    assert resolve(series=None) == Decimal("8000")


@pytest.mark.parametrize("basis", ["close", "high"])
def test_carried_christmas_close_refuses(basis: PeakBasis) -> None:
    """The 2024-12-25 close is carried to 2025-01-01 at the 7-day limit, and is
    a listed closure although the calendar does not cover 2024."""
    christmas = date(2024, 12, 25)
    lot = _closed_lot(open_date=date(2024, 12, 20), close_date=date(2025, 1, 2))
    series = _series(
        {
            date(2024, 12, 24): Decimal("100"),
            christmas: Decimal("120"),
            date(2025, 1, 2): Decimal("100"),
        },
        highs={date(2025, 1, 2): Decimal("110")},
    )
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis=basis)
    assert exc.value == NonSessionPriceBarError(weekend=False)


@pytest.mark.parametrize(
    ("basis", "expected"), [("close", Decimal("8000")), ("high", Decimal("8800"))]
)
def test_non_session_bars_outside_the_scan_window_are_not_checked(
    basis: PeakBasis, expected: Decimal
) -> None:
    """Saturday bars before the carried seed and after the window are ignored."""
    lot = _closed_lot(open_date=date(2025, 6, 9), close_date=date(2025, 6, 12))
    sessions = [date(2025, 6, d) for d in (6, 9, 10, 11, 12)]
    series = _series(
        {d: Decimal("100") for d in sessions}
        | {_SATURDAY_BAR: Decimal("120"), date(2025, 6, 14): Decimal("120")},
        highs={d: Decimal("110") for d in sessions},
    )
    assert (
        _peak_inr(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis=basis)
        == expected
    )


@pytest.mark.parametrize("basis", ["close", "high"])
def test_non_usd_series_stays_manual_fill(basis: PeakBasis) -> None:
    """The US calendar governs USD series only; a non-USD one is not valued."""
    lot = _closed_lot(open_date=date(2025, 6, 6), close_date=date(2025, 6, 10))
    series = _june_series(currency="EUR")
    assert (
        _peak_inr(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis=basis)
        is FaA3PeakBlankReason.NO_USD_SERIES
    )


@pytest.mark.parametrize("split", ["unsafe-basis", "in-window"])
def test_non_session_bar_refuses_before_split_manual_fill(split: str) -> None:
    lot = _closed_lot(open_date=date(2025, 6, 6), close_date=date(2025, 6, 10))
    series = replace(
        _june_series(),
        has_split_in_range=True,
        price_basis=CONTEMPORANEOUS_BASIS if split == "in-window" else "current-split-basis",
        split_ratios={date(2025, 6, 9): Decimal("2")} if split == "in-window" else {},
    )
    with pytest.raises(NonSessionPriceBarError):
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025)


@pytest.mark.parametrize(
    ("bars", "weekend"),
    [((date(2025, 6, 28), date(2025, 7, 4)), True), ((date(2025, 7, 4), date(2025, 7, 5)), False)],
    ids=["weekend-first", "closure-first"],
)
def test_non_session_error_says_the_earliest_bars_kind(
    bars: tuple[date, date], weekend: bool
) -> None:
    """With a weekend bar and a listed-closure bar in the window, the error
    says which kind the earliest one is (#1331)."""
    lot = _closed_lot(open_date=date(2025, 6, 27), close_date=date(2025, 7, 8))
    sessions = [date(2025, 6, 27), date(2025, 6, 30), *(date(2025, 7, d) for d in (1, 2, 3, 7, 8))]
    series = _series(dict.fromkeys((*sessions, *bars), Decimal("100")))
    with pytest.raises(NonSessionPriceBarError) as exc:
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025)
    assert exc.value == NonSessionPriceBarError(weekend=weekend)


@pytest.mark.parametrize("basis", ["close", "high"])
def test_stale_non_session_seed_is_not_carried_into_the_window(basis: PeakBasis) -> None:
    lot = _closed_lot(open_date=date(2025, 6, 15), close_date=date(2025, 6, 17))
    series = _series(
        {_SATURDAY_BAR: Decimal("120"), date(2025, 6, 16): Decimal("100")},
        highs={date(2025, 6, 16): Decimal("110")},
    )
    # June 7 is eight days before the window, beyond the seven-day carry limit.
    assert (
        peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis=basis)
        is FaA3PeakBlankReason.STALE_CLOSE
    )


# ==========================================================================
# #939: a close is carried only across known closures, never across a session
# ==========================================================================


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_close_basis_missing_session_close_is_not_carried(peak_currency: PeakCurrency) -> None:
    """Tuesday 2025-06-03 is a session with no bar; suppose its close was $200.
    Carrying Monday's $100 would file ₹8,800 from Wednesday's $110, not ₹16,000."""
    lot = _closed_lot(open_date=date(2025, 6, 2), close_date=date(2025, 6, 5))
    series = _series({date(2025, 6, 2): Decimal("100"), date(2025, 6, 4): Decimal("110")})
    assert (
        _peak_inr(
            lot,
            series=series,
            fx_policy=_FLAT_FX,
            calendar_year=2025,
            peak_currency=peak_currency,
        )
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


@pytest.mark.parametrize("basis", ["close", "high"])
def test_weekend_window_needs_the_friday_close(basis: PeakBasis) -> None:
    """Held Saturday 2025-06-07 to Sunday 06-08: the close carried in must be
    Friday's. Thursday's is within seven days but Friday was a session."""
    lot = _closed_lot(open_date=date(2025, 6, 7), close_date=date(2025, 6, 8))
    thursday = date(2025, 6, 5)
    series = _series({thursday: Decimal("100")}, highs={thursday: Decimal("110")})
    assert (
        _peak_inr(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, basis=basis)
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


def test_listed_closure_carries_in_a_year_the_calendar_does_not_cover() -> None:
    """2024-12-25 is listed, so it carries the 2024-12-24 close in CY2024."""
    lot = _closed_lot(open_date=date(2024, 12, 23), close_date=date(2024, 12, 27))
    series = _series(
        {
            date(2024, 12, 23): Decimal("100"),
            date(2024, 12, 24): Decimal("110"),
            date(2024, 12, 26): Decimal("105"),
        }
    )
    result = peak_value(lot, series=series, fx_policy=_FLAT_FX, calendar_year=2024)
    assert isinstance(result, PeakValue)
    assert (result.on_date, result.price_usd) == (date(2024, 12, 24), Decimal("110"))


def test_staleness_ceiling_still_applies_across_a_weekend() -> None:
    lot = _closed_lot(open_date=date(2025, 6, 6), close_date=date(2025, 6, 10))
    series = _series({date(2025, 6, 6): Decimal("100"), date(2025, 6, 9): Decimal("100")})
    peak = functools.partial(peak_value, lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025)
    assert isinstance(peak(), PeakValue)
    # Sunday is two days after Friday's close.
    assert peak(max_price_staleness_days=1) is FaA3PeakBlankReason.STALE_CLOSE


def test_resolver_open_lot_missing_session_is_manual_fill_not_the_closing_floor() -> None:
    """The fetched close peak is below the $110 mark, so it used to floor to
    the closing value; a missing 2025-12-30 bar now leaves it blank."""
    lot = _open_lot(open_date=date(2025, 12, 29), mark_price=Decimal("110"))
    series = _series({date(2025, 12, 29): Decimal("100"), date(2025, 12, 31): Decimal("100")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=_FLAT_FX,
            calendar_year=2025,
            closing_inr=Decimal("8800"),
        )
        is FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
    )


# ==========================================================================
# #1260: the daily peak products are exact
# ==========================================================================

# 40 shares at $2.50 and at $2.500000000000000000000000000025 are worth $100 and
# $100.000000000000000000000000001, which round to the same 28 digits.
_QUANTITY_40 = Decimal("40")
_PRICE = Decimal("2.5")
_PRICE_A_HAIR_ABOVE = Decimal("2.500000000000000000000000000025")
# The larger value at ₹90/$, to every digit.
_INR_A_HAIR_ABOVE = Decimal("9000.00000000000000000000000009")


@pytest.mark.parametrize(
    "context",
    [None, Context(prec=3, traps=[Inexact, Rounded])],
    ids=["default-context", "hostile-context"],
)
def test_candidates_that_tie_only_when_rounded_pick_the_exact_maximum(
    context: Context | None,
) -> None:
    """Main rounded each day's close x quantity to 28 digits, so the two days tied
    and the earlier one (₹8,000 at ₹80/$) won. The exact values differ, so the
    later day wins at its ₹90/$, in both tested decimal contexts."""
    lot = _closed_lot(
        open_date=date(2025, 6, 2), close_date=date(2025, 6, 5), quantity=_QUANTITY_40
    )
    series = _series(
        {
            date(2025, 6, 2): _PRICE,
            date(2025, 6, 3): _PRICE_A_HAIR_ABOVE,
            date(2025, 6, 4): Decimal("2"),
        }
    )
    fx = _StrictPolicy({date(2025, 6, 2): Decimal("80"), date(2025, 6, 3): Decimal("90")})
    with localcontext(context or Context()):
        result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025)
    assert result == PeakValue(
        amount_inr=_INR_A_HAIR_ABOVE,
        on_date=date(2025, 6, 3),
        price_usd=_PRICE_A_HAIR_ABOVE,
        ttbr=Decimal("90"),
    )


@pytest.mark.parametrize(
    ("close", "high", "high_inr"),
    [
        (_PRICE, _PRICE_A_HAIR_ABOVE, _INR_A_HAIR_ABOVE),
        (
            Decimal("2.50000000000000000000000000225"),
            Decimal("2.5000000000000000000000000025"),
            Decimal("9000.000000000000000000000009"),
        ),
    ],
    ids=["the-high-value-rounds", "the-close-value-rounds"],
)
def test_high_above_the_close_peak_only_exactly_still_wins(
    close: Decimal, high: Decimal, high_inr: Decimal
) -> None:
    """The close peak (carried to Saturday 06-07) and Monday's high give values
    for 40 shares that tie once rounded to 28 digits, so main took the earlier
    close peak. Exactly, the high is larger and wins. In the second case the
    high's value has 28 digits and the close's value rounds up to it."""
    lot = _closed_lot(
        open_date=date(2025, 6, 7), close_date=date(2025, 6, 10), quantity=_QUANTITY_40
    )
    series = _series(
        {
            date(2025, 6, 6): close,
            date(2025, 6, 9): Decimal("2"),
            date(2025, 6, 10): Decimal("2"),
        },
        highs={date(2025, 6, 9): high, date(2025, 6, 10): Decimal("2")},
    )
    fx = _CarryForwardPolicy({date(2025, 6, 6): Decimal("80"), date(2025, 6, 9): Decimal("90")})
    result = peak_value(lot, series=series, fx_policy=fx, calendar_year=2025, basis="high")
    assert result == PeakValue(
        amount_inr=high_inr, on_date=date(2025, 6, 9), price_usd=high, ttbr=Decimal("90")
    )


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("close", Decimal("8500")), ("high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING)],
    ids=["close", "high"],
)
def test_floor_compares_the_exact_marked_value(
    basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """A mark a hair above every fetched close: 40 x the mark ties 40 x $2.50 once
    rounded, so main kept the ₹8,000 peak. Exactly, the closing value is larger:
    the close basis floors at it and the high basis leaves the peak blank."""
    lot = _open_lot(
        open_date=date(2025, 12, 30), quantity=_QUANTITY_40, mark_price=_PRICE_A_HAIR_ABOVE
    )
    days = (date(2025, 12, 30), date(2025, 12, 31))
    series = _series(dict.fromkeys(days, _PRICE), highs=dict.fromkeys(days, _PRICE))
    fx = _CarryForwardPolicy({days[0]: Decimal("80"), days[1]: Decimal("85")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("8500"),
            basis=basis,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("close", Decimal("82.5")), ("high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING)],
    ids=["close", "high"],
)
def test_floor_compares_the_statement_total_with_the_exact_peak_value(
    basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """3 x a 29-digit close is $0.99999999999999999999999999996, which rounds to
    $1.000000000000000000000000000 at 28 digits, so main found the $1.00 statement
    total no larger and kept the ₹80 peak. Exactly, the total is larger: the close
    basis floors at it and the high basis leaves the peak blank."""
    price = Decimal("0.33333333333333333333333333332")
    lot = replace(
        _open_lot(open_date=date(2025, 12, 30), quantity=Decimal("3"), mark_price=_THIRD),
        closing_total_usd=Decimal("1.00"),
    )
    days = (date(2025, 12, 30), date(2025, 12, 31))
    series = _series(dict.fromkeys(days, price), highs=dict.fromkeys(days, price))
    fx = _CarryForwardPolicy({days[0]: Decimal("80"), days[1]: Decimal("82.50")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("82.5"),
            basis=basis,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("price", "marker"),
    [("47391" * 200, "47391"), ("9E+999999", "999999")],
    ids=["past-1000-digits", "past-the-largest-exponent"],
)
@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_candidate_value_refuses_when_it_cannot_be_exact(
    price: str, marker: str, peak_currency: PeakCurrency
) -> None:
    """A close whose value for 3 shares needs more than 1000 digits, or overflows,
    refuses with a fixed message, no context and no chained exception. Main
    rounded the long product and raised a raw Overflow for the other."""
    lot = _closed_lot(
        open_date=date(2025, 6, 2), close_date=date(2025, 6, 4), quantity=Decimal("3")
    )
    series = _series(dict.fromkeys((date(2025, 6, 2), date(2025, 6, 3)), Decimal(price)))
    with pytest.raises(InvariantError) as caught:
        peak_value(
            lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, peak_currency=peak_currency
        )
    assert str(caught.value) == (
        f"InvariantError: Schedule FA A3 peak value, candidate USD: {INEXACT_PRODUCT}"
    )
    assert caught.value.context == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_prints_no_marker(caught.value, markers=(marker,))


# ==========================================================================
# #1263: a lot's exact quantity after a same-day pro-rata sale
# ==========================================================================


def _sixty_digit_cut(value: Fraction) -> Decimal:
    """``value`` cut to the 60 digits a Robinhood A3 lot carries (#1263)."""
    with localcontext(Context(prec=60)):
        return exact_decimal(value)


# Two thirds of a share, left in each of three same-day lots after a pro-rata sale.
_TWO_THIRDS = Fraction(2, 3)
_TWO_THIRDS_CUT = _sixty_digit_cut(_TWO_THIRDS)
_DEC_30, _DEC_31 = date(2025, 12, 30), date(2025, 12, 31)


def _two_thirds_lot(kind: str, *, exact: bool) -> OpenLot | ClosedLot:
    """A lot of the 60-digit cut of two thirds, with its exact quantity if ``exact``."""
    lot = (
        _open_lot(open_date=_DEC_30, quantity=_TWO_THIRDS_CUT, mark_price=Decimal("1.70"))
        if kind == "open"
        else _closed_lot(open_date=_DEC_30, close_date=_DEC_31, quantity=_TWO_THIRDS_CUT)
    )
    return replace(lot, quantity_exact=_TWO_THIRDS) if exact else lot


@pytest.mark.parametrize("basis", ["close", "high"])
@pytest.mark.parametrize("kind", ["open", "closed"])
def test_peak_values_the_exact_quantity(kind: str, basis: PeakBasis) -> None:
    """#1263: two thirds of a share at a $1.70 close and ₹82.50/$ is worth exactly ₹93.50,
    filed ₹94. The lot's 60-digit quantity gave ₹93.4999…, filed ₹93."""
    series = _series(
        dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
        highs=dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
    )
    fx = _CarryForwardPolicy({_DEC_30: Decimal("82.50")})
    result = peak_value(
        _two_thirds_lot(kind, exact=True),
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        basis=basis,
    )
    assert isinstance(result, PeakValue)
    assert to_whole_rupees(result.amount_inr) == Decimal("94")
    assert result == PeakValue(
        amount_inr=exact_decimal(_TWO_THIRDS * Fraction("1.70") * Fraction("82.50")),
        on_date=_DEC_30,
        price_usd=Decimal("1.70"),
        ttbr=Decimal("82.50"),
    )
    assert result.amount_inr == Decimal("93.5")


@pytest.mark.parametrize("basis", ["close", "high"])
@pytest.mark.parametrize("kind", ["open", "closed"])
def test_peak_without_an_exact_quantity_keeps_the_decimal_product(
    kind: str, basis: PeakBasis
) -> None:
    """#1263 control: the same lot without its exact quantity keeps today's value, the
    exact product of its 60-digit quantity, filed ₹93."""
    series = _series(
        dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
        highs=dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
    )
    fx = _CarryForwardPolicy({_DEC_30: Decimal("82.50")})
    result = peak_value(
        _two_thirds_lot(kind, exact=False),
        series=series,
        fx_policy=fx,
        calendar_year=2025,
        basis=basis,
    )
    assert isinstance(result, PeakValue)
    assert to_whole_rupees(result.amount_inr) == Decimal("93")
    assert Fraction(result.amount_inr) == (
        Fraction(_TWO_THIRDS_CUT) * Fraction("1.70") * Fraction("82.50")
    )
    assert (result.on_date, result.price_usd, result.ttbr) == (
        _DEC_30,
        Decimal("1.70"),
        Decimal("82.50"),
    )


# A closed lot held Mon 2025-06-02 to Fri 06-06. Each basis and selection currency
# has an earliest-maximum tie: native closes tie on 06-02 and 06-04, INR closes on
# 06-03 and 06-04 (₹136), and highs, native or INR, on 06-04 and 06-06.
_TIE_DAYS = [date(2025, 6, d) for d in (2, 3, 4, 5, 6)]
_TIE_CLOSES = dict(
    zip(_TIE_DAYS, map(Decimal, ("1.70", "1.60", "1.70", "1.50", "1.00")), strict=True)
)
_TIE_HIGHS = dict(
    zip(_TIE_DAYS, map(Decimal, ("1.70", "1.60", "1.90", "1.50", "1.90")), strict=True)
)
_TIE_RATES = dict(zip(_TIE_DAYS, map(Decimal, ("75", "85", "80", "80", "80")), strict=True))


@pytest.mark.parametrize(
    ("basis", "peak_currency", "on_date", "price", "ttbr"),
    [
        ("close", "native", date(2025, 6, 2), "1.70", "75"),
        ("close", "inr", date(2025, 6, 3), "1.60", "85"),
        ("high", "native", date(2025, 6, 4), "1.90", "80"),
        ("high", "inr", date(2025, 6, 4), "1.90", "80"),
    ],
    ids=["close-native", "close-inr", "high-native", "high-inr"],
)
def test_exact_quantity_keeps_the_peak_day(
    basis: PeakBasis, peak_currency: PeakCurrency, on_date: date, price: str, ttbr: str
) -> None:
    """#1263: the exact quantity values the day the Decimal quantity selects, ties
    included: scaling every candidate by one positive quantity can't move the peak."""
    lot = _closed_lot(open_date=_TIE_DAYS[0], close_date=_TIE_DAYS[-1], quantity=_TWO_THIRDS_CUT)
    series = _series(_TIE_CLOSES, highs=_TIE_HIGHS)

    def peak(lot: ClosedLot) -> PeakValue | FaA3PeakBlankReason:
        return peak_value(
            lot,
            series=series,
            fx_policy=_StrictPolicy(_TIE_RATES),
            calendar_year=2025,
            basis=basis,
            peak_currency=peak_currency,
        )

    exact = peak(replace(lot, quantity_exact=_TWO_THIRDS))
    assert exact == PeakValue(
        amount_inr=exact_decimal(_TWO_THIRDS * Fraction(price) * Fraction(ttbr)),
        on_date=on_date,
        price_usd=Decimal(price),
        ttbr=Decimal(ttbr),
    )
    control = peak(lot)
    assert isinstance(control, PeakValue)
    assert (control.on_date, control.price_usd, control.ttbr) == (
        on_date,
        Decimal(price),
        Decimal(ttbr),
    )


@pytest.mark.parametrize("closing_total", [None, Decimal("1")], ids=["marked", "total"])
def test_native_floor_without_exact_values_keeps_decimal_quantity(
    monkeypatch: pytest.MonkeyPatch, closing_total: Decimal | None
) -> None:
    """Ordinary quantities need no potentially enormous Fraction denominator."""
    price = Decimal("1E+10000")
    lot = replace(
        _open_lot(open_date=_DEC_30, quantity=Decimal("1E-10000"), mark_price=price),
        closing_total_usd=closing_total,
    )

    def unexpected_fraction(_value):
        pytest.fail("an ordinary native floor must not construct a Fraction")

    monkeypatch.setattr("ibkr_fa.compute.fa_a3_peak_value.Fraction", unexpected_fraction)

    assert resolve_fa_a3_peak(
        lot,
        series=_series(dict.fromkeys((_DEC_30, _DEC_31), price)),
        fx_policy=_CarryForwardPolicy(dict.fromkeys((_DEC_30, _DEC_31), Decimal("1"))),
        calendar_year=2025,
        closing_inr=Decimal("1"),
    ) == Decimal("1")


# The Dec-31 closing at ₹80/$ of a third of a $3.40 statement total.
_THIRD_OF_340_INR = exact_decimal(Fraction(17, 15) * 80)


@pytest.mark.parametrize(
    ("exact", "basis", "expected"),
    [
        (True, "close", Decimal("93.5")),
        (True, "high", Decimal("93.5")),
        (False, "close", _THIRD_OF_340_INR),
        (False, "high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING),
    ],
    ids=["exact-close", "exact-high", "control-close", "control-high"],
)
def test_floor_divides_the_exact_closing_share_by_the_exact_quantity(
    exact: bool, basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """#1263: two thirds of a share's exact third of a $3.40 statement total is $1.70 a
    share, exactly the peak price, so the ₹93.50 peak (filed ₹94) stands. Divided by
    the 60-digit quantity, the closing is a hair above $1.70: without the exact
    quantity the close basis floors at the closing and the high basis leaves the
    peak blank."""
    lot = replace(
        _two_thirds_lot("open", exact=exact),
        closing_total_usd=_sixty_digit_cut(Fraction(17, 15)),
        closing_total_usd_exact=Fraction(17, 15),
    )
    series = _series(
        dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
        highs=dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.70")),
    )
    fx = _CarryForwardPolicy({_DEC_30: Decimal("82.50"), _DEC_31: Decimal("80")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=_THIRD_OF_340_INR,
            basis=basis,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("exact", "basis", "expected"),
    [
        (True, "close", Decimal("82.5")),
        (True, "high", Decimal("82.5")),
        (False, "close", Decimal("80")),
        (False, "high", FaA3PeakBlankReason.HIGH_BELOW_CLOSING),
    ],
    ids=["exact-close", "exact-high", "control-close", "control-high"],
)
def test_floor_divides_a_lone_closing_total_by_the_exact_quantity(
    exact: bool, basis: PeakBasis, expected: Decimal | FaA3PeakBlankReason
) -> None:
    """#1263: a $1.00 statement share that its Decimal holds, over two thirds of a share,
    is $1.50 a share, exactly the peak price, so the ₹82.50 peak (filed ₹83) stands.
    Without the exact quantity, the 60-digit quantity times $1.50 is below $1.00: the
    close basis floors at the ₹80 closing and the high basis leaves the peak blank."""
    lot = replace(_two_thirds_lot("open", exact=exact), closing_total_usd=Decimal("1.00"))
    series = _series(
        dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.50")),
        highs=dict.fromkeys((_DEC_30, _DEC_31), Decimal("1.50")),
    )
    fx = _CarryForwardPolicy({_DEC_30: Decimal("82.50"), _DEC_31: Decimal("80")})
    assert (
        resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx,
            calendar_year=2025,
            closing_inr=Decimal("80"),
            basis=basis,
        )
        == expected
    )


# A close five places past the digit cap; its digits are the absence marker.
_CLOSE_PAST_THE_CAP = Decimal("47391E-1005")


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_exact_quantity_peak_refuses_a_price_with_no_exact_rational(
    peak_currency: PeakCurrency,
) -> None:
    """#1263: a close whose INR price has more places than exact arithmetic allows can't
    be scaled by the exact quantity, so the peak refuses with a fixed message, no
    context and no chained exception. The same lot without its exact quantity
    values that close."""
    lot = _closed_lot(open_date=_TIE_DAYS[0], close_date=_TIE_DAYS[2], quantity=_TWO_THIRDS_CUT)
    series = _series(dict.fromkeys(_TIE_DAYS[:2], _CLOSE_PAST_THE_CAP))

    def peak(lot: ClosedLot) -> PeakValue | FaA3PeakBlankReason:
        return peak_value(
            lot, series=series, fx_policy=_FLAT_FX, calendar_year=2025, peak_currency=peak_currency
        )

    assert isinstance(peak(lot), PeakValue)
    with pytest.raises(InvariantError) as caught:
        peak(replace(lot, quantity_exact=_TWO_THIRDS))
    assert str(caught.value) == (
        f"InvariantError: Schedule FA A3 peak value, INR: {INEXACT_PRODUCT}"
    )
    assert caught.value.context == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_prints_no_marker(caught.value, markers=("47391",))
