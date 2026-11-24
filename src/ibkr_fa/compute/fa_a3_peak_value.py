"""Compute Schedule FA Table A3 ``peak_value_inr`` for one lot (issue #14).

Implements the FA-A3 portal column "Peak value of investment during
the Period". By default, it selects the maximum foreign-currency value
first and then converts that value using the TTBR for its peak date.

**Formula (issue #14)**::

    peak_date = argmax over each calendar day d in the holding window of
                close_usd[d] x quantity
    peak_inr = close_usd[peak_date] x quantity x TTBR(peak_date)

with the holding window::

    [ max(acquired, Jan 1 of cy),  min(disposed - 1 day, Dec 31 of cy) ]

Every ``x`` above, the high basis's close-versus-high comparison and the
floor at the closing value use exact products
(:func:`~ibkr_fa.compute.exact_inr.checked_product`, #1260), so the peak
day, its ties and the filed rupee follow the exact amounts. A lot whose
quantity is a rounded Robinhood pro-rata share keeps its exact quantity
(``quantity_exact``, #1263): the day is selected as above, since scaling
every candidate by the same positive quantity can't change it, and the
peak's INR value and the closing floor use the exact quantity.

**Design decisions** (it's taxes — prefer manual-fill over a wrong
number; see the rubber-duck review on the originating PR):

* DAILY CLOSING price, not intraday high. This matches Schedule-FA
  practitioner convention and the data available from a daily-bar
  price source. Documented so a CA who wants intraday peaks knows to
  override.
* ``peak_currency="native"`` is the default and follows the literal
  sequence in the ITD guidance: find the foreign-currency peak, then
  convert it at that date's TTBR. ``peak_currency="inr"`` preserves the
  opt-in alternative of maximizing ``close x qty x TTBR`` across all
  calendar days.
* Each calendar day takes the close of the latest US market session on or
  before it, so a close carries only across weekends and listed market
  closures, never across a session (#939). Under native
  selection, TTBR is requested for the selected closing-price peak day
  and, with the high basis, the selected intraday-high day (if any).
  The INR override requests it for every candidate day.
* End-of-day valuation: a lot is valued at each day's close *while it is
  held*. On the disposal date the lot has been sold, so the window ends
  the day BEFORE disposal. A lot whose in-CY holding window is empty
  (opened and disposed the same day, or disposed on Jan 1) is left
  blank -> manual-fill rather than a guessed value.
* ``refuse_disposal_day_peak=True`` (Fidelity CMA SPAXX, #622): the close
  window also takes the disposal day, and a peak selected on that day is
  left blank. A peak set only by the disposal day differs between counting and
  not counting that day; no fetched Schedule FA text settles which, so it is never
  filed. Every other peak is the same under both readings.

Returns a :class:`~ibkr_fa.models.FaA3PeakBlankReason` instead of a value
(the FA row keeps ``peak_value_inr`` blank, records the reason, and the CLI
emits a manual-fill warning for that row) whenever the value cannot be
established with certainty (#962):

* ``EMPTY_WINDOW``: the in-year window is empty (checked before the series);
* ``PRICES_NOT_FETCHED`` / ``NO_USD_SERIES``: no price series for the
  ticker, with fetching off / on; or ``NO_USD_SERIES`` for a series whose
  currency is not USD;
* ``SPLIT``: a split falls after the lot's first in-year day and on or
  before the earlier of its disposal date and December 31 (one static lot
  quantity cannot span it), or a split occurred but the series was not
  normalized to contemporaneous per-date prices;
* ``SESSION_WITHOUT_CLOSE``: for some day in the window, the latest session
  on or before it has no close (a dropped bar, a halt or delisting; in a
  year the market calendar does not fully cover, also an unlisted weekday
  market holiday, which counts as a session);
* ``STALE_CLOSE``: no close on or before a day in the window is within the
  staleness ceiling. With the default seven-day ceiling, a series stopping
  mid-window hits ``SESSION_WITHOUT_CLOSE`` first, so staleness occurs when
  the window opens (a series that starts later or ended earlier);
* ``NO_TTBR``: the FX policy cannot source a TTBR for one of those selected
  peak days (native default) or for any candidate day (INR override);
* ``DISPOSAL_DAY_PEAK``, ``SESSION_WITHOUT_HIGH`` and ``HIGH_BELOW_CLOSING``:
  the disposal-day and high-basis refusals described above and in
  :func:`peak_value` / :func:`resolve_fa_a3_peak`.

Raises :class:`NonSessionPriceBarError` instead when a USD series has a bar
on a US non-session (a weekend in any year, or a listed closure) inside the
window the selected basis scans, or as the close carried into that window
(#895). The price source and the market calendar disagree, so neither is
trusted. This check runs before every shortcut that skips the window.

:func:`peak_value` returns the full :class:`PeakValue` (amount + the
day/close/TTBR that drove it, for the audit trail), and
:func:`resolve_fa_a3_peak` is the single rule the row-builder calls for
the INR cell. The ``series`` argument
is the per-ticker :class:`PriceSeries` (the row-builder looks it up by
``lot.symbol``); #14's sketch passed a whole ``prices`` map, but a
single series keeps this function pure and trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from ibkr_fa.compute.closing_value import closing_position_usd
from ibkr_fa.compute.exact_inr import checked_exact_product, checked_product
from ibkr_fa.compute.fa_peak import select_peak
from ibkr_fa.exceptions import IbkrFaError, InvariantError, MissingFxRateError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.models import ClosedLot, FaA3PeakBlankReason, OpenLot
from ibkr_fa.prices.series import DEFAULT_MAX_PRICE_STALENESS_DAYS, PriceSeries
from ibkr_fa.reference.us_market_calendar import (
    is_us_market_session,
    supports_calendar_year,
    us_market_session_on_or_before,
)
from ibkr_fa.types import PeakBasis, PeakCurrency

_USD: str = "USD"
_CANDIDATE_USD = "Schedule FA A3 peak value, candidate USD"
_CLOSE_HIGH_USD = "Schedule FA A3 peak value, close/high USD"
_PEAK_INR = "Schedule FA A3 peak value, INR"
# datetime.date.weekday(): Mon=0 .. Sun=6, so ``< _SATURDAY`` is Mon-Fri.
_SATURDAY = 5


@dataclass(frozen=True, slots=True)
class NonSessionPriceBarError(IbkrFaError):
    """A USD price bar is dated on a day the US market was closed (#895).

    The price source and the US market calendar disagree, so the peak is
    refused rather than valued from either.

    The error holds and prints only whether the day is a weekend or a listed
    closure: the symbol and the date are input (#1047, #1331). Its route
    points at the holding's cache file by that kind of day (#1552). Every
    command that reaches it (schedule-fa, stockplan-fa and robinhood-fa, and
    the run-all, stockplan-all and robinhood-all legs) has --price-cache-dir
    and --no-fetch-prices, and reads the series from ``<ticker>.csv``
    directly in its price cache directory, where a fetch writes it.
    Robinhood option closes reach only Table A2. fidelity-cma-fa refuses
    such a bar in Table A2 first, and netbenefits-fa in its Dec-31 close.

    Args:
        weekend: Whether the bar's day is a weekend (``True``) or a listed US
            market closure (``False``).
    """

    weekend: bool

    def __str__(self) -> str:
        kind, row = (
            ("a weekend day", "a Saturday or Sunday")
            if self.weekend
            else ("a listed US market closure", "a weekday the US market was closed")
        )
        return (
            f"a holding's price series has a bar dated on {kind}; the price source "
            "and the US market calendar disagree, so the Schedule FA Table A3 peak "
            "value is refused. Move each .csv file directly in the price cache directory "
            "(--price-cache-dir; by default a .ibkr-fa-price-cache folder that the command's "
            f"--help locates) with a row dated on {row}, and any .lock beside it, out of that "
            "directory, then re-run with price fetching on (without --no-fetch-prices): a "
            "fresh fetch can return the series without that bar. If the fresh series still "
            "has it, enter this command's Schedule FA by hand."
        )


@dataclass(frozen=True)
class PeakValue:
    """A computed peak value plus the audit trail of which day drove it.

    Attributes:
        amount_inr: Peak INR value, full precision (the row's ``filed``
            value rounds it to whole rupees).
        on_date: The calendar day selected as the peak.
        price_usd: The USD price used on ``on_date`` — the (carried-forward)
            daily close for the close basis, or the daily intraday high for the
            high basis.
        ttbr: The (carried-forward) SBI TT-buying rate used on
            ``on_date``.
    """

    amount_inr: Decimal
    on_date: date
    price_usd: Decimal
    ttbr: Decimal


def _holding_window(
    lot: OpenLot | ClosedLot,
    *,
    calendar_year: int,
    include_disposal_day: bool = False,
) -> tuple[date, date] | None:
    """Return ``(start, end)`` of the in-CY holding window, or ``None``.

    ``start = max(acquired, Jan 1)``; ``end = Dec 31`` for an open lot,
    or ``disposal_date - 1`` (capped at Dec 31) for a lot closed in the
    CY. Returns ``None`` when the window is empty (``end < start``) — a
    same-day open/close, or a disposal on Jan 1 — so the caller falls
    back to manual-fill rather than valuing a lot that was not held at
    any end-of-day in the CY. ``include_disposal_day`` ends a closed lot's
    window on its disposal date instead.
    """
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    start = max(lot.open_date_time, cy_start)
    if not isinstance(lot, ClosedLot):
        raw_end = cy_end
    elif include_disposal_day:
        raw_end = lot.close_date_time
    else:
        raw_end = lot.close_date_time - timedelta(days=1)
    end = min(raw_end, cy_end)
    if end < start:
        return None
    return start, end


def _close_basis_peak(
    lot: OpenLot | ClosedLot,
    *,
    window: tuple[date, date],
    series: PriceSeries,
    fx_policy: FxPolicy,
    max_price_staleness_days: int,
    peak_currency: PeakCurrency,
    refuse_disposal_day_peak: bool,
) -> PeakValue | FaA3PeakBlankReason:
    """Select the close-basis peak over ``window`` and convert it to INR.

    Each day takes the close of the latest session on or before it; blank
    when that session has no close or the latest close is too stale (#939).
    """
    start, end = window
    quantity = lot.quantity

    candidates: dict[date, Decimal] = {}
    day = start
    while day <= end:
        bar_day = series.close_date_on_or_before(day, max_staleness_days=max_price_staleness_days)
        if bar_day is None:
            return FaA3PeakBlankReason.STALE_CLOSE
        if bar_day != us_market_session_on_or_before(day):
            # The latest possible session has no bar: never carry an
            # earlier close across a session (#939).
            return FaA3PeakBlankReason.SESSION_WITHOUT_CLOSE
        candidates[day] = series.closes[bar_day]
        day += timedelta(days=1)
    peak = _select_peak(
        candidates,
        quantity=quantity,
        fx_policy=fx_policy,
        peak_currency=peak_currency,
    )
    if (
        refuse_disposal_day_peak
        and isinstance(peak, PeakValue)
        and isinstance(lot, ClosedLot)
        and peak.on_date == lot.close_date_time
    ):
        return FaA3PeakBlankReason.DISPOSAL_DAY_PEAK
    return peak


def _select_peak(
    candidates: dict[date, Decimal],
    *,
    quantity: Decimal,
    fx_policy: FxPolicy,
    peak_currency: PeakCurrency,
) -> PeakValue | FaA3PeakBlankReason:
    """Select the earliest maximum of non-empty ``candidates`` and convert it to INR."""

    try:
        peak = select_peak(
            (
                (day, checked_product(price, quantity, at=_CANDIDATE_USD))
                for day, price in candidates.items()
            ),
            fx_policy=fx_policy,
            peak_currency=peak_currency,
        )
    except MissingFxRateError:
        return FaA3PeakBlankReason.NO_TTBR
    if peak is None:
        raise InvariantError(what="an A3 peak was selected from no candidate day", context=())
    return PeakValue(
        amount_inr=peak.value_inr,
        on_date=peak.on_date,
        price_usd=candidates[peak.on_date],
        ttbr=peak.ttbr,
    )


def _high_candidate_window(lot: OpenLot | ClosedLot, *, calendar_year: int) -> tuple[date, date]:
    """The window scanned for intraday HIGHs. Unlike the close window it
    INCLUDES the disposal day (a closed lot is held for PART of its
    disposal day, so that day's intraday high can be the period max)."""
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    start = max(lot.open_date_time, cy_start)
    raw_end = lot.close_date_time if isinstance(lot, ClosedLot) else cy_end
    return start, min(raw_end, cy_end)


def _require_session_bars(
    lot: OpenLot | ClosedLot,
    *,
    series: PriceSeries | None,
    calendar_year: int,
    max_price_staleness_days: int,
    basis: PeakBasis,
    refuse_disposal_day_peak: bool,
) -> None:
    """Refuse a bar the selected basis would scan that is on a US non-session.

    Checks every observed bar in the basis's scan window (the close window,
    or the disposal-inclusive high window) plus the seed close carried into
    the close window. A weekend is a non-session in any year and a listed
    closure on its date, whether or not the calendar covers the whole year.
    A missing or non-USD series is not checked.

    Raises:
        NonSessionPriceBarError: for the earliest such bar.
    """
    if series is None or series.currency != _USD:
        return
    close_window = _holding_window(
        lot, calendar_year=calendar_year, include_disposal_day=refuse_disposal_day_peak
    )
    if basis == "close":
        if close_window is None:
            return
        start, end = close_window
    else:
        start, end = _high_candidate_window(lot, calendar_year=calendar_year)
    seed = (
        None
        if close_window is None
        else series.close_date_on_or_before(
            close_window[0], max_staleness_days=max_price_staleness_days
        )
    )
    day = start if seed is None else seed
    while day <= end:
        if day in series.closes and not is_us_market_session(day):
            raise NonSessionPriceBarError(weekend=day.weekday() >= _SATURDAY)
        day += timedelta(days=1)


def _has_in_window_split(
    lot: OpenLot | ClosedLot,
    *,
    series: PriceSeries,
    calendar_year: int,
) -> bool:
    """Whether one static lot quantity spans a split inside the valued window."""

    start, end = _high_candidate_window(lot, calendar_year=calendar_year)
    return any(start < split_day <= end for split_day in series.split_ratios)


def _high_basis_peak(
    lot: OpenLot | ClosedLot,
    *,
    series: PriceSeries,
    fx_policy: FxPolicy,
    calendar_year: int,
    close_peak: PeakValue,
    peak_currency: PeakCurrency,
) -> PeakValue | FaA3PeakBlankReason:
    """Select between the close peak and the maximum session intraday high.

    Every US market session in the (disposal-inclusive) window must have an
    observed valid high, else blank (fail-closed): a missing session high
    may be a dropped bar, so we refuse rather than risk silently dropping a
    real trading-day high. A listed closure needs no bar; a bar on one was
    already refused by :func:`_require_session_bars`. For a calendar year
    the market calendar does not cover, every Mon-Fri day must have a valid
    high instead.
    """
    quantity = lot.quantity
    start, end = _high_candidate_window(lot, calendar_year=calendar_year)
    has_calendar = supports_calendar_year(calendar_year)
    candidates: dict[date, Decimal] = {}
    day = start
    while day <= end:
        needs_high = is_us_market_session(day) if has_calendar else day.weekday() < _SATURDAY
        if needs_high:
            high = series.highs.get(day)
            close = series.closes.get(day)
            if (
                high is None
                or close is None
                or not high.is_finite()
                or not close.is_finite()
                or high < close
                or close <= 0
            ):
                # A session (or, without a calendar, a weekday) with no
                # observed valid high: it may be a dropped bar, so fail
                # closed rather than risk dropping a high.
                return FaA3PeakBlankReason.SESSION_WITHOUT_HIGH
            candidates[day] = high
        day += timedelta(days=1)
    if not candidates:
        # No session in the window (held only across a weekend or a listed
        # closure); no intraday high exists, so the market value is the
        # carried close.
        return close_peak
    best_high = _select_peak(
        candidates,
        quantity=quantity,
        fx_policy=fx_policy,
        peak_currency=peak_currency,
    )
    if isinstance(best_high, FaA3PeakBlankReason):
        return best_high
    close_value = (
        checked_product(close_peak.price_usd, quantity, at=_CLOSE_HIGH_USD)
        if peak_currency == "native"
        else close_peak.amount_inr
    )
    high_value = (
        checked_product(best_high.price_usd, quantity, at=_CLOSE_HIGH_USD)
        if peak_currency == "native"
        else best_high.amount_inr
    )
    if high_value > close_value:
        return best_high
    if high_value < close_value:
        return close_peak
    return min((close_peak, best_high), key=lambda peak: peak.on_date)


def peak_value(
    lot: OpenLot | ClosedLot,
    *,
    series: PriceSeries | None,
    fx_policy: FxPolicy,
    calendar_year: int,
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    basis: PeakBasis = "close",
    peak_currency: PeakCurrency = "native",
    refuse_disposal_day_peak: bool = False,
    prices_fetched: bool = False,
) -> PeakValue | FaA3PeakBlankReason:
    """Compute one lot's peak and convert it to INR (#14).

    ``peak_currency="native"`` (default) selects the maximum foreign-currency
    value before applying that date's TTBR. ``peak_currency="inr"`` selects
    the maximum daily converted value instead.

    ``basis="high"`` is the opt-in intraday reading:
    ``max(close_basis_peak, max over each session of high x qty)`` under the
    native default.
    Reusing the close-basis peak keeps ``high >= close``; under the INR
    override it also retains any FX-driven non-trading-day close candidate.
    It also keeps the close basis's refusals, so a window that starts on a
    weekend or listed closure needs the close of the session before it, which
    the high check below does not scan (#939).
    Highs are NEVER carried forward; instead EVERY US market session in the
    (disposal-inclusive) window must have its own valid high, else the whole
    lot fails closed to manual-fill. This session-completeness gate is the
    guarantee against silently dropping a real trading-day high. Sessions
    come from :mod:`ibkr_fa.reference.us_market_calendar`, and a bar on a
    listed closure also fails closed. For a calendar year that calendar does
    not cover, every Mon-Fri day must have its own valid high, so a window
    spanning a weekday market holiday over-refuses (safe). **Scope: US
    equities via Yahoo — the tool's whole domain.**

    Returns a :class:`FaA3PeakBlankReason` instead whenever the value cannot
    be established with certainty (see the module docstring; plus, for
    ``high``, any session lacking a valid high). ``prices_fetched`` says
    whether a missing ``series`` was looked up online (``NO_USD_SERIES``)
    or only in the cache (``PRICES_NOT_FETCHED``).

    Raises:
        NonSessionPriceBarError: a bar the basis scans is on a US non-session.
    """
    _require_session_bars(
        lot,
        series=series,
        calendar_year=calendar_year,
        max_price_staleness_days=max_price_staleness_days,
        basis=basis,
        refuse_disposal_day_peak=refuse_disposal_day_peak,
    )
    window = _holding_window(
        lot, calendar_year=calendar_year, include_disposal_day=refuse_disposal_day_peak
    )
    if window is None:
        # Not held at any end of day in the CY (incl. a same-day acquire ==
        # dispose closed lot); neither basis can value it.
        return FaA3PeakBlankReason.EMPTY_WINDOW
    if series is None:
        return (
            FaA3PeakBlankReason.NO_USD_SERIES
            if prices_fetched
            else FaA3PeakBlankReason.PRICES_NOT_FETCHED
        )
    if series.currency != _USD:
        return FaA3PeakBlankReason.NO_USD_SERIES
    if not series.split_safe or _has_in_window_split(
        lot, series=series, calendar_year=calendar_year
    ):
        return FaA3PeakBlankReason.SPLIT
    close_peak = _close_basis_peak(
        lot,
        window=window,
        series=series,
        fx_policy=fx_policy,
        max_price_staleness_days=max_price_staleness_days,
        peak_currency=peak_currency,
        refuse_disposal_day_peak=refuse_disposal_day_peak,
    )
    if basis == "close" or isinstance(close_peak, FaA3PeakBlankReason):
        # The high basis can't do better than an unpriceable close window.
        return _exact_quantity_peak(lot, close_peak)
    return _exact_quantity_peak(
        lot,
        _high_basis_peak(
            lot,
            series=series,
            fx_policy=fx_policy,
            calendar_year=calendar_year,
            close_peak=close_peak,
            peak_currency=peak_currency,
        ),
    )


def _exact_quantity_peak(
    lot: OpenLot | ClosedLot, peak: PeakValue | FaA3PeakBlankReason
) -> PeakValue | FaA3PeakBlankReason:
    """``peak`` valued at the lot's exact quantity where ``quantity`` rounds it (#1263).

    The selection and the high basis's comparison scale every candidate by
    the same positive quantity, so the rounded quantity picks the same day.
    """
    if isinstance(peak, FaA3PeakBlankReason) or lot.quantity_exact is None:
        return peak
    price_inr = checked_product(peak.price_usd, peak.ttbr, at=_PEAK_INR)
    return replace(
        peak, amount_inr=checked_exact_product(lot.quantity_exact, price_inr, at=_PEAK_INR)
    )


def _is_open_dec31_lot(lot: OpenLot | ClosedLot, *, calendar_year: int) -> bool:
    """True iff ``lot`` is an OPEN lot acquired ON Dec-31 of the CY, i.e. its
    in-CY holding window is the single day Dec-31. Such a lot was held for
    exactly the year-end valuation day, so its peak == its closing value."""
    if not isinstance(lot, OpenLot):
        return False
    cy_end = date(calendar_year, 12, 31)
    window = _holding_window(lot, calendar_year=calendar_year)
    return window is not None and window == (cy_end, cy_end)


def _closing_exceeds_native_peak(lot: OpenLot, peak_price: Decimal) -> bool:
    """Compare the closing value to the native peak exactly (#1243, #1260, #1263)."""
    if lot.closing_total_usd_exact is not None:
        quantity = Fraction(lot.quantity) if lot.quantity_exact is None else lot.quantity_exact
        return lot.closing_total_usd_exact / quantity > peak_price
    if lot.quantity_exact is not None and lot.closing_total_usd is not None:
        return Fraction(lot.closing_total_usd) / lot.quantity_exact > peak_price
    closing_usd = closing_position_usd(lot)
    return closing_usd > checked_product(lot.quantity, peak_price, at=_CLOSE_HIGH_USD)


def resolve_fa_a3_peak(
    lot: OpenLot | ClosedLot,
    *,
    series: PriceSeries | None,
    fx_policy: FxPolicy,
    calendar_year: int,
    closing_inr: Decimal,
    basis: PeakBasis = "close",
    peak_currency: PeakCurrency = "native",
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    refuse_disposal_day_peak: bool = False,
    prices_fetched: bool = False,
) -> Decimal | FaA3PeakBlankReason:
    """The single peak-resolution rule for FA-A3, applied by every pipeline
    so "once the data is parsed, the rule is the same".

    Fails closed (a :class:`FaA3PeakBlankReason` -> manual-fill) whenever the
    chosen basis cannot be established.

    * CLOSE basis, OPEN lot acquired on Dec-31 -> ``closing_inr`` directly
      (its one held day IS the year-end valuation day; no price series needed,
      and a price-override a cent off the raw close can't move it).
    * For an open lot, compare peak and closing in the configured comparison
      currency. CLOSE basis floors to the known closing value when the fetched
      series is lower; HIGH basis refuses that source inconsistency
      (``HIGH_BELOW_CLOSING``).

    Under native-currency selection, the resulting INR peak may legitimately
    be below the INR closing value because the two dates use different TTBRs.

    Raises:
        NonSessionPriceBarError: as :func:`peak_value`, also before the
            shortcut above.
    """
    if basis == "close" and _is_open_dec31_lot(lot, calendar_year=calendar_year):
        # The shortcut doesn't reach peak_value's check, so run it here.
        _require_session_bars(
            lot,
            series=series,
            calendar_year=calendar_year,
            max_price_staleness_days=max_price_staleness_days,
            basis=basis,
            refuse_disposal_day_peak=refuse_disposal_day_peak,
        )
        return closing_inr
    peak = peak_value(
        lot,
        series=series,
        fx_policy=fx_policy,
        calendar_year=calendar_year,
        max_price_staleness_days=max_price_staleness_days,
        basis=basis,
        peak_currency=peak_currency,
        refuse_disposal_day_peak=refuse_disposal_day_peak,
        prices_fetched=prices_fetched,
    )
    if isinstance(peak, FaA3PeakBlankReason):
        return peak
    closing_exceeds_peak = (
        _closing_exceeds_native_peak(lot, peak.price_usd)
        if peak_currency == "native" and isinstance(lot, OpenLot)
        else closing_inr > peak.amount_inr
    )
    if not closing_exceeds_peak:
        return peak.amount_inr
    return closing_inr if basis == "close" else FaA3PeakBlankReason.HIGH_BELOW_CLOSING
