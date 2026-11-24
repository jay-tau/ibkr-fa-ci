"""Resolve the Dec-31 closing price of a NetBenefits ESPP lot (#310).

For a Fidelity NetBenefits ESPP lot the Schedule FA Table A3 *closing value* is
``quantity x the Dec-31 per-share close``. Unlike an IBKR ``OpenLot`` (whose
``mark_price`` is broker-stated), Fidelity's year-end statement leaves the
last-day lot **unpriced** ("-"), so the Dec-31 figure must be resolved without
ever silently defaulting a filed number. The precedence, all cross-checked:

1. **STATEMENT** (primary): the broker-stated Dec-31 holding *total* — exact, the
   safest source. ``per_share_usd`` is left ``None`` (only the total is stated).
2. **OVERRIDE**: a manually-supplied per-share price (config
   ``[[netbenefits.price_overrides]]``), dated on the required session.
3. **YAHOO_FALLBACK**: the market close (``prices.PriceSeries``) — used as the
   source ONLY when nothing broker-/user-stated exists, and flagged by its
   ``source`` so the CLI can warn it is market-fed, not broker-stated.

Per-share evidence (a split-safe price series or an override) is priced on the
*required session*: the last regular US market session on or before Dec 31,
from :mod:`ibkr_fa.reference.us_market_calendar`. That needs every closure of
the year listed, so per-share evidence for a year the calendar doesn't support
refuses. A supplied series must have a positive, finite USD close dated on the
required session, with no later bar on or before Dec 31; an override must be
dated on it. Both must sit within the staleness window. Invalid evidence
refuses: it is never ignored, and an invalid override never falls through to
the market close. A statement total with no per-share evidence needs no
calendar.

Every available source is cross-checked against the others on the **total**
value and the function REFUSES (raises :class:`ClosingPriceError`) on any
mismatch, on no source at all, on invalid per-share evidence, or when an
unnormalized split makes the per-share basis unsafe. A normalized
contemporaneous Yahoo series remains usable; a legacy/current-basis split
series disqualifies Yahoo and per-share overrides, leaving only the
split-neutral statement *total*.

This module is a pure compute helper: it takes an already-fetched
:class:`~ibkr_fa.prices.series.PriceSeries` (or ``None`` when the fetch failed)
and primitive override params, so it never touches the network or config. The
CLI advisory and the wiring that sources the statement total (#311 reader) and
prints the fallback warning land in the A3 assembly (#313). The A3 *peak* value
is unchanged and already handled by :mod:`ibkr_fa.compute.fa_a3_peak_value`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from ibkr_fa.compute.exact_inr import (
    checked_addition,
    checked_product,
    checked_subtraction,
    exact_decimal,
)
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import (
    supports_calendar_year,
    us_market_session_on_or_before,
)

_USD: Final[str] = "USD"
_CENT: Final[Decimal] = Decimal("0.01")
# Year-end-specific staleness ceiling. The general peak-value window tolerates a
# 7-day market closure, but the *last trading close on/before Dec 31* is at most
# ~2 calendar days back (Sun Dec 31 -> Fri Dec 29). A tighter 3-day default (one
# day of cushion for a freak adjacent closure) bounds how far back the required
# session may lie.
_DEC31_MAX_STALENESS_DAYS: Final[int] = 3
_MARKET_SESSION_REFUSAL: Final[str] = (
    "Dec-31 market close: the price series has no usable Close for the required US market session"
)
_OVERRIDE_SESSION_REFUSAL: Final[str] = (
    "Dec-31 price override: price_date is not the required US market session"
)
# Per-share rounding envelope for the statement-vs-market cross-check: a broker /
# user figure may round the per-share close to cents, so the legitimate total
# difference scales with the share count (plus a cent of absolute slack). A
# genuine mismatch (wrong security / date / split) is dollars, far above this.
_PER_SHARE_HALF_CENT: Final[Decimal] = Decimal("0.005")


class ClosingPriceError(IbkrFaError):
    """Raised when the Dec-31 closing price cannot be resolved with certainty.

    Carries an actionable message (which source is missing / mismatched and how
    to supply it). Subclasses :class:`IbkrFaError`, so the CLI maps it to the
    domain-error exit code.
    """


class CloseSource(StrEnum):
    """Which source produced the resolved Dec-31 close."""

    STATEMENT = "statement"
    OVERRIDE = "override"
    YAHOO_FALLBACK = "yahoo_fallback"


@dataclass(frozen=True, slots=True, kw_only=True)
class Dec31SessionRoutes:
    """A caller's route for each refusal of evidence for the required session (#1549).

    Each gets the required US market session and returns the text the
    resolver appends to its refusal after ``"; "``. Like
    ``missing_source_remedy``, a route comes from the caller, because only
    the caller knows which settings, if any, can act on the refusal.

    Attributes:
        market_close: For a series with no usable close for the session.
        override_date: For a price override dated off the session.
        mismatch: For two sources whose Dec-31 totals disagree.
    """

    market_close: Callable[[date], str]
    override_date: Callable[[date], str]
    mismatch: Callable[[date], str]


def _refusal(text: str, route: Callable[[date], str] | None, session: date) -> ClosingPriceError:
    """``text``, with ``route``'s text for ``session`` after ``"; "`` when given."""
    return ClosingPriceError(text if route is None else f"{text}; {route(session)}")


@dataclass(frozen=True)
class ResolvedClose:
    """The resolved Dec-31 close for one lot, with provenance for the audit trail.

    Attributes:
        total_usd: The Dec-31 holding **total** in USD — the authoritative input
            to the A3 closing value. For STATEMENT it is the broker-stated total;
            for OVERRIDE / YAHOO_FALLBACK it is the exact ``per_share_usd x quantity``.
            A Fraction statement share returns its review Decimal; its caller
            retains the rational for filing.
        per_share_usd: The per-share close, or ``None`` when the source is
            STATEMENT (the statement evidences only the total, never a per-share).
        source: Which :class:`CloseSource` was used.
        provenance: Human-readable provenance for the audit trail.
    """

    total_usd: Decimal
    per_share_usd: Decimal | None
    source: CloseSource
    provenance: str


def _require_positive_finite(value: Decimal | Fraction, *, what: str) -> None:
    if (isinstance(value, Decimal) and not value.is_finite()) or value <= 0:
        raise ClosingPriceError(f"{what} must be a positive, finite amount")


def _validate_inputs(
    quantity: Decimal,
    statement_total_usd: Decimal | Fraction | None,
    override_per_share_usd: Decimal | None,
    tolerance: Decimal | None,
    max_staleness_days: int,
) -> None:
    _require_positive_finite(quantity, what="quantity")
    if statement_total_usd is not None:
        _require_positive_finite(statement_total_usd, what="statement_total_usd")
    if override_per_share_usd is not None:
        _require_positive_finite(override_per_share_usd, what="override_per_share_usd")
    # A non-finite/negative tolerance would silently defeat the mismatch guard
    # (``abs(diff) > Infinity`` is always False; ``NaN`` raises a low-level error).
    # Zero is allowed (exact-match strictness).
    if tolerance is not None and (not tolerance.is_finite() or tolerance < 0):
        raise ClosingPriceError("tolerance must be a non-negative, finite amount")
    # A negative window would disqualify every candidate and surface as a confusing
    # "no close available" refusal instead of a clear bad-input error. Zero is
    # allowed (exact-date only, no carry-back).
    if max_staleness_days < 0:
        raise ClosingPriceError("max_staleness_days must be >= 0")


def resolve_dec31_close(
    *,
    valuation_date: date,
    quantity: Decimal,
    statement_total_usd: Decimal | Fraction | None,
    series: PriceSeries | None,
    override_per_share_usd: Decimal | None = None,
    override_priced_on: date | None = None,
    override_label: str = "",
    max_staleness_days: int = _DEC31_MAX_STALENESS_DAYS,
    tolerance: Decimal | None = None,
    missing_source_remedy: str | None = None,
    session_routes: Dec31SessionRoutes | None = None,
) -> ResolvedClose:
    """Resolve the Dec-31 closing price for one ESPP lot, refusing on ambiguity.

    Args:
        valuation_date: The valuation date (Dec 31 of the calendar year).
        quantity: Lot share count (> 0).
        statement_total_usd: Broker-stated Dec-31 holding total in USD, or
            ``None`` when the statement leaves the lot unpriced. Primary source.
            A Fraction carries an exact scaled share for the cross-check;
            the result is its review Decimal, and the caller retains the
            rational for filing.
        series: The Yahoo :class:`PriceSeries` (from ``load_price_series``), or
            ``None`` when the fetch failed. Used for the cross-check, and as the
            flagged fallback when nothing broker-/user-stated exists. Unless a
            split makes it unsafe, it must hold a usable close for the required
            session.
        override_per_share_usd: A manual per-share USD price (config
            ``[[netbenefits.price_overrides]]``), or ``None``.
        override_priced_on: The trading date the override is for; must be the
            required session, within ``max_staleness_days`` on/before
            ``valuation_date``.
        override_label: Provenance label for the override (source + reason).
        max_staleness_days: How far before ``valuation_date`` the required
            session may lie, for both Yahoo and the override. Defaults to a
            tight 3 days (the year-end last-trading-day gap is at most ~2 days).
        tolerance: Absolute USD tolerance for the total cross-check. Defaults to
            :func:`per_share_tolerance` (a per-share half-cent envelope).
            Every total, difference and default tolerance is exact (#1250).
        missing_source_remedy: The caller's remedy, appended after ``"; "`` to
            the refusal when no source is usable. The fixed text names no config
            setting, because only the caller knows which one, if any, can
            supply a close. No other refusal carries it.
        session_routes: The caller's routes for the refusals of a series
            with no usable required-session close, an override dated off
            that session, and two disagreeing totals. ``None`` leaves those
            refusals' fixed text alone.

    Returns:
        The :class:`ResolvedClose` with the chosen source and provenance.

    Raises:
        ClosingPriceError: No source is available, two sources disagree beyond
            ``tolerance``, per-share evidence is for an unsupported calendar
            year, the series has no usable required-session close, the override
            is out of window or off the required session, a split in a
            current-split-basis series (only a price-cache file supplies a
            split-unsafe one) leaves no broker statement total to fall back on,
            or an input amount is non-positive/non-finite.
        InvariantError: A per-share total, the default tolerance or a
            cross-check difference can't be computed exactly (a Decimal
            failure, or a digit lost at the active precision widened to at
            least :data:`~ibkr_fa.compute.exact_inr.EXACT_SUM_DIGITS` digits).
    """
    _validate_inputs(
        quantity, statement_total_usd, override_per_share_usd, tolerance, max_staleness_days
    )
    exact_statement_total = None
    if isinstance(statement_total_usd, Fraction):
        exact_statement_total = statement_total_usd
        statement_total_usd = exact_decimal(statement_total_usd)

    # A split on an unnormalized current basis makes per-share sources unsafe;
    # only the split-neutral statement total remains usable.
    if series is not None and not series.split_safe:
        if statement_total_usd is not None:
            return _statement_result(statement_total_usd, valuation_date)
        raise ClosingPriceError(
            f"split in range for {valuation_date.isoformat()}: Yahoo and any per-share override "
            f"(x the current, rebased quantity) are unsafe and there is no broker statement total "
            f"to fall back on; the price cache holds this security's closes on the current-split "
            f"basis, so move its cache file, and any .lock beside it, out of the price cache "
            f"directory and rerun with price fetching on"
        )

    session, override_per_share, yahoo_per_share = _per_share_evidence(
        valuation_date,
        series,
        override_per_share_usd,
        override_priced_on,
        max_staleness_days,
        session_routes,
    )

    # Candidate TOTALS, keyed by source. ALL present sources (incl. the primary
    # statement) are cross-checked pairwise on the total before one is selected:
    # per issue #310 the market feed is a CROSS-CHECK, so a statement that
    # disagrees with Yahoo beyond tolerance must REFUSE (a wrong ticker/date/split
    # would otherwise file an unverified value) — precedence only decides which
    # value is filed once the sources agree. Totals (not per-share) are compared to
    # avoid the statement-division rounding (the statement states a total).
    candidates: dict[CloseSource, Decimal] = {}
    if statement_total_usd is not None:
        candidates[CloseSource.STATEMENT] = statement_total_usd
    if override_per_share is not None:
        candidates[CloseSource.OVERRIDE] = checked_product(
            override_per_share, quantity, at="Dec-31 close, override total"
        )
    if yahoo_per_share is not None:
        candidates[CloseSource.YAHOO_FALLBACK] = checked_product(
            yahoo_per_share, quantity, at="Dec-31 close, Yahoo total"
        )

    if len(candidates) > 1:
        tol = (
            tolerance
            if tolerance is not None
            else per_share_tolerance(quantity, at="Dec-31 close, cross-check tolerance")
        )
        for (src_a, total_a), (src_b, total_b) in itertools.combinations(candidates.items(), 2):
            difference = checked_subtraction(
                total_a, total_b, at="Dec-31 close, cross-check difference"
            )
            # Keep the Decimal precision guard, but compare a carried share exactly.
            mismatch = (
                abs(exact_statement_total - Fraction(total_b)) > tol
                if exact_statement_total is not None and src_a is CloseSource.STATEMENT
                else difference.copy_abs() > tol
            )
            if mismatch:
                # Two totals need per-share evidence, which is priced on ``session``.
                assert session is not None
                raise _refusal(
                    f"Dec-31 close mismatch: the {src_a.value} and {src_b.value} totals differ "
                    "by more than the tolerance; refusing rather than file an unverified "
                    "closing value",
                    None if session_routes is None else session_routes.mismatch,
                    session,
                )

    if CloseSource.STATEMENT in candidates:
        return _statement_result(candidates[CloseSource.STATEMENT], valuation_date)
    if override_per_share is not None:
        return ResolvedClose(
            total_usd=candidates[CloseSource.OVERRIDE],
            per_share_usd=override_per_share,
            source=CloseSource.OVERRIDE,
            provenance=f"manual price override ({override_label or 'unlabelled'})",
        )
    if yahoo_per_share is not None:
        return ResolvedClose(
            total_usd=candidates[CloseSource.YAHOO_FALLBACK],
            per_share_usd=yahoo_per_share,
            source=CloseSource.YAHOO_FALLBACK,
            provenance=(
                f"Yahoo close on/before {valuation_date.isoformat()} "
                f"(market fallback — not broker-stated)"
            ),
        )
    remedy = "" if missing_source_remedy is None else f"; {missing_source_remedy}"
    raise ClosingPriceError(
        f"no Dec-31 close available for {valuation_date.isoformat()}: there is no statement "
        f"total, no override and no usable Yahoo close{remedy}"
    )


def per_share_tolerance(quantity: Decimal, *, at: str) -> Decimal:
    """The exact ``quantity * 0.005 + 0.01``: a per-share half-cent envelope
    plus a cent, for comparing two Dec-31 totals of ``quantity`` shares.

    Refuses as :func:`~ibkr_fa.compute.exact_inr.checked_product` and
    :func:`~ibkr_fa.compute.exact_inr.checked_addition` do, at the fixed
    label ``at``.
    """
    return checked_addition(checked_product(quantity, _PER_SHARE_HALF_CENT, at=at), _CENT, at=at)


def _statement_result(total_usd: Decimal, valuation_date: date) -> ResolvedClose:
    return ResolvedClose(
        total_usd=total_usd,
        per_share_usd=None,
        source=CloseSource.STATEMENT,
        provenance=f"broker statement Dec-31 holding total for {valuation_date.isoformat()}",
    )


def _per_share_evidence(
    valuation_date: date,
    series: PriceSeries | None,
    override_per_share_usd: Decimal | None,
    override_priced_on: date | None,
    max_staleness_days: int,
    routes: Dec31SessionRoutes | None,
) -> tuple[date | None, Decimal | None, Decimal | None]:
    """The required session, and the override's and the series' per-share
    prices for it, each ``None`` when not supplied (the session too, when
    neither is).

    Invalid evidence refuses, so it never silently drops out of the
    cross-check and an invalid override never falls through to the market
    close. The override is checked first. The off-session override and
    unusable-session-close refusals carry their caller routes when given.
    """
    if override_per_share_usd is None and series is None:
        return None, None, None
    session = _required_session(valuation_date)
    override = (
        None
        if override_per_share_usd is None
        else _session_override(
            override_per_share_usd,
            override_priced_on,
            valuation_date,
            session,
            max_staleness_days,
            None if routes is None else routes.override_date,
        )
    )
    market = (
        None
        if series is None
        else _session_close(
            series,
            valuation_date,
            session,
            max_staleness_days,
            None if routes is None else routes.market_close,
        )
    )
    return session, override, market


def _required_session(valuation_date: date) -> date:
    """The last US market session on or before ``valuation_date``; refuses a
    year whose closures the calendar doesn't all list."""
    if not supports_calendar_year(valuation_date.year):
        raise ClosingPriceError(
            "Dec-31 per-share pricing, field calendar_year: calendar year "
            f"{valuation_date.year} is unsupported"
        )
    return us_market_session_on_or_before(valuation_date)


def _session_override(
    per_share_usd: Decimal,
    priced_on: date | None,
    valuation_date: date,
    session: date,
    max_staleness_days: int,
    route: Callable[[date], str] | None,
) -> Decimal:
    """The override's per-share price; refuses one out of window or off
    ``session``, the latter with ``route``.

    Split-unsafe per-share evidence is handled by the caller, so this is only
    reached when there is no series or the series is split-safe.
    """
    if priced_on is None or not (
        timedelta(0) <= valuation_date - priced_on <= timedelta(max_staleness_days)
    ):
        raise ClosingPriceError(
            f"the price override's date is not within {max_staleness_days} days on/before "
            f"{valuation_date.isoformat()}"
        )
    if priced_on != session:
        raise _refusal(_OVERRIDE_SESSION_REFUSAL, route, session)
    return per_share_usd


def _session_close(
    series: PriceSeries,
    valuation_date: date,
    session: date,
    max_staleness_days: int,
    route: Callable[[date], str] | None,
) -> Decimal:
    """The series' close on ``session``, or refuse with ``route``.

    Refuses a non-USD listing, a ``session`` beyond the staleness window, a
    missing ``session`` bar, any bar after ``session`` on or before
    ``valuation_date`` (a bar on a non-session day), or a non-finite /
    non-positive close (a corrupt cache / provider value must never reach a
    filed figure). The split case is handled by the caller before this is
    reached.
    """
    bar_day = series.close_date_on_or_before(valuation_date, max_staleness_days=max_staleness_days)
    close = series.closes[session] if bar_day == session else None
    if series.currency != _USD or close is None or not close.is_finite() or close <= 0:
        raise _refusal(_MARKET_SESSION_REFUSAL, route, session)
    return close
