"""Immutable per-ticker daily-close price series for FA peak-value (#14).

A :class:`PriceSeries` holds dividend-unadjusted daily market prices in
the security's quote currency, keyed by the **exchange-local trading
date**. Yahoo series are normalized back to the basis that existed on
each trading date, so a later split cannot rewrite a filed historical
value.

It is the decoupling boundary between the price *provider*
(:mod:`ibkr_fa.prices.yahoo` / :mod:`ibkr_fa.prices.cache`) and the
pure *compute* (:mod:`ibkr_fa.compute.fa_a3_peak_value`): the compute
depends only on this structure, so its tests inject a hand-built
series and never touch the network.

Why ``quote.close`` and not Yahoo's ``adjclose``: ``adjclose`` also
adjusts for dividends, which is wrong for a market-value disclosure.
Yahoo's ``quote.close`` is only a starting point because it is
retroactively split-rebased. The provider reverses every later split
for each bar, exactly (#1307), and records the dated ratios as provenance.

:class:`MassiveOptionCloseSeries` is the same boundary for one option
contract's Massive daily closes (:mod:`ibkr_fa.prices.massive_options`),
so the Robinhood compute never imports the fetcher (#634).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.invariants import require
from ibkr_fa.types import Ticker

# US equity markets never close for more than a handful of consecutive
# calendar days in normal operation: a public holiday adjoining a
# weekend is ~3-4 days, and the rare historical exceptions (the 9/11
# closure was 4 trading days / ~6 calendar days; weather closures) are
# still under a week. A gap LONGER than this between a requested
# valuation date and the most recent available close therefore signals
# a data problem (delisting, trading halt, missing rows, or an
# IBKR<->Yahoo ticker mismatch) rather than a normal market closure, so
# ``close_on_or_before`` refuses to carry a stale price across it. A
# shorter gap is no proof of a closure, since a dropped session bar looks
# the same: the FA-A3 close basis also requires the latest session's own
# close (see ``ibkr_fa.compute.fa_a3_peak_value``, #939).
DEFAULT_MAX_PRICE_STALENESS_DAYS: Final[int] = 7
CURRENT_SPLIT_BASIS: Final[str] = "current-split-basis"
CONTEMPORANEOUS_BASIS: Final[str] = "contemporaneous"
_PRICE_BASES: Final[frozenset[str]] = frozenset({CURRENT_SPLIT_BASIS, CONTEMPORANEOUS_BASIS})


@dataclass(frozen=True, slots=True)
class PriceSourceContradictionError(IbkrFaError):
    """A price source returned two bars for one date (#942).

    Two bars for one date contradict each other, so the response is refused
    rather than read as missing prices or valued from either bar. The error
    names the source but keeps neither the symbol nor the date, which are
    input (#1047, #1368).

    Args:
        price_source: The source that returned the bars, e.g. ``"Yahoo"``.
    """

    price_source: str

    def __str__(self) -> str:
        return (
            f"{self.price_source} returned two price bars for one date for a holding; "
            "the price source contradicts itself, so its prices are refused"
        )


@dataclass(frozen=True)
class PriceSeries:
    """Daily market prices for one ticker over a contiguous span.

    Attributes:
        ticker: The exchange symbol the series is for.
        currency: ISO 4217 quote currency reported by the provider.
            v1 only values USD-quoted holdings; the compute refuses any
            other currency (-> manual-fill).
        closes: ``date -> Decimal`` closing price, keyed by the
            exchange-local trading date. Only trading days are present;
            weekends/holidays are absent and resolved by
            :meth:`close_on_or_before` (carry-forward).
        highs: ``date -> Decimal`` raw daily HIGH, a subset of ``closes``'
            dates (only bars with a valid ``high >= close``). Powers the
            opt-in FA-A3 ``peak_basis = "high"`` (intraday peak). Empty when
            the provider/cache carried no highs (e.g. a legacy v1 cache) — the
            high basis then fails closed to manual-fill. Highs are NEVER
            carried forward (an intraday high is a specific trading-day event,
            unlike a close which marks the last known value).
        has_split_in_range: ``True`` if the provider observed a stock
            split anywhere in the fetched span.
        split_ratios: ``split date -> numerator / denominator`` provenance,
            a Decimal at the decimal context's precision (28 digits by
            default): a ratio with no exact decimal, such as a 1-for-3
            split's third, is rounded. The Yahoo reader normalizes bars
            with the exact ratios, never these (#1307); the compute reads
            only their dates.
        price_basis: ``"contemporaneous"`` when every price is on the
            basis that existed on its date; legacy/current-basis series
            use ``"current-split-basis"``.
        source: Human-readable provenance (e.g. the request URL) for
            the audit trail.
        fetched_at: Timestamp the series was retrieved, for the audit
            trail / cache provenance.
    """

    ticker: Ticker
    currency: str
    closes: Mapping[date, Decimal]
    has_split_in_range: bool
    source: str
    fetched_at: datetime
    highs: Mapping[date, Decimal] = field(default_factory=dict)
    split_ratios: Mapping[date, Decimal] = field(default_factory=dict)
    price_basis: str = CURRENT_SPLIT_BASIS

    _sorted_dates: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sorted_dates", tuple(sorted(self.closes)))
        # Invariant: a high only for a known trading day. A high without a
        # matching close would be un-anchored and could never be validated
        # against its own close, so reject it loudly rather than value it.
        require(
            self.highs.keys() <= self.closes.keys(),
            what="PriceSeries.highs contains dates absent from closes",
        )
        require(
            self.price_basis in _PRICE_BASES,
            what="PriceSeries.price_basis is unsupported",
        )
        require(
            all(ratio.is_finite() and ratio > 0 for ratio in self.split_ratios.values()),
            what="PriceSeries.split_ratios contains an invalid ratio",
        )
        require(
            not self.split_ratios or self.has_split_in_range,
            what="PriceSeries split ratios contradict the split flag",
        )

    @property
    def split_safe(self) -> bool:
        """Whether per-share prices remain compatible with dated quantities."""

        return not self.has_split_in_range or self.price_basis == CONTEMPORANEOUS_BASIS

    def close_date_on_or_before(
        self,
        day: date,
        *,
        max_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    ) -> date | None:
        """Return the date of the bar :meth:`close_on_or_before` reads, or ``None``."""
        idx = bisect.bisect_right(self._sorted_dates, day)
        if idx == 0:
            return None
        last = self._sorted_dates[idx - 1]
        if (day - last) > timedelta(days=max_staleness_days):
            return None
        return last

    def close_on_or_before(
        self,
        day: date,
        *,
        max_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    ) -> Decimal | None:
        """Return the most recent close on a trading date ``<= day``.

        Forward-fills across weekends/holidays: returns the close of
        the latest trading date at or before ``day``. Returns ``None``
        when there is no such date (``day`` precedes the series) or the
        nearest prior close is more than ``max_staleness_days`` calendar
        days old (a gap too large to be a normal market closure — the
        caller treats this as "cannot value" -> manual-fill).
        """
        last = self.close_date_on_or_before(day, max_staleness_days=max_staleness_days)
        return None if last is None else self.closes[last]


@dataclass(frozen=True, slots=True)
class OptionCloseBar:
    day: date
    close_usd: Decimal
    volume: Decimal
    transactions: int


@dataclass(frozen=True, slots=True)
class MassiveOptionCloseSeries:
    massive_ticker: str
    underlying: str
    expiration: date
    strike_usd: Decimal
    multiplier: Decimal
    bars: Mapping[date, OptionCloseBar]
    artifact_path: Path
    fetched_at: datetime
