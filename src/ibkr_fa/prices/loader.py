"""Orchestrate price loading for FA peak-value (issue #14).

:func:`load_price_series` is the single entry point the pipeline calls.
Given the set of tickers and their calendar-year windows, it fetches
(or reads from cache) a :class:`PriceSeries` per ticker, returning only
the ones that resolved.

A pre-window lookback buffer is added to each fetch so the compute can
forward-fill a price for a window that starts on a non-trading day
(e.g. a pre-CY lot whose window begins on Jan 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

from ibkr_fa.prices.cache import load_or_fetch
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher, fetch_daily_closes_usd
from ibkr_fa.types import Isin, Ticker

# Days fetched before each window start so the carry-forward in
# PriceSeries.close_on_or_before has a prior close to seed from when a
# window begins on a weekend/holiday.
DEFAULT_LOOKBACK_DAYS: Final[int] = 10


def resolve_override_symbols(
    holdings: Iterable[tuple[Ticker, Isin]],
    overrides: Mapping[tuple[Ticker, Isin], str],
) -> dict[Ticker, str]:
    """Map IBKR ticker -> price-source symbol for ``[prices]`` overrides.

    An override applies to a holding only when BOTH its ticker AND ISIN
    match the override key (the ISIN binding is the tax-safety guard —
    see :class:`ibkr_fa.config.TickerOverride`): a bare ticker map could
    misapply prices to a different security with a reused ticker.
    """
    return {
        ticker: overrides[(ticker, isin)]
        for ticker, isin in holdings
        if (ticker, isin) in overrides
    }


def load_price_series(
    windows: Mapping[Ticker, tuple[date, date]],
    *,
    cache_dir: Path,
    fetch: bool = True,
    fetcher: DailyCloseFetcher | None = None,
    now: datetime | None = None,
    refresh: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fetch_symbols: Mapping[Ticker, str] | None = None,
) -> dict[Ticker, PriceSeries]:
    """Resolve a :class:`PriceSeries` for each ticker window.

    Args:
        windows: ``ticker -> (window_start, window_end)`` — the earliest
            and latest dates the compute may need a close for (the
            pipeline passes the CY span).
        cache_dir: Directory for the per-ticker cache / audit artifacts.
        fetch: When ``False`` (CLI ``--no-fetch-prices``), read only
            complete caches and never call the network. Every cache read
            checks the cache's payload hash, online or not.
        fetcher: Injectable fetch function (tests pass a stub).
        now: Fetch timestamp (defaults to current UTC); injectable for
            deterministic tests.
        refresh: When ``True``, refetch even when a valid unlocked cache
            covers the window. Every existing cache is still checked
            first, and one that fails a check refuses (#1170); a valid
            locked cache is still used.
        lookback_days: Pre-window buffer to seed price carry-forward.
        fetch_symbols: Optional ``ticker -> price-source symbol`` map
            from ``[prices].ticker_overrides`` (ISIN-resolved upstream).
            For a ticker present here, the override symbol is fetched and
            identity-checked while the series stays keyed by the IBKR
            ticker; absent tickers are fetched under their own symbol.

    Returns:
        ``ticker -> PriceSeries`` for every ticker that resolved.

    Raises:
        PriceCacheError: If a price cache or its lock exists but can't be
            used (see :func:`~ibkr_fa.prices.cache.load_or_fetch`).
        PriceSourceContradictionError: If a fetched response has two bars on
            one date. No cache is written (#942).
        YahooResponseFormatError: If a fetched Yahoo response doesn't have the
            accepted shape. No cache is written for that response (#1139).
    """
    # Resolve the default fetcher at call time (not as a parameter
    # default) so tests can neutralise the network by patching
    # ``ibkr_fa.prices.loader.fetch_daily_closes_usd``.
    actual_fetcher = fetcher if fetcher is not None else fetch_daily_closes_usd
    overrides = fetch_symbols or {}
    now = now or datetime.now(UTC)
    resolved: dict[Ticker, PriceSeries] = {}
    for ticker, (window_start, window_end) in windows.items():
        series = load_or_fetch(
            ticker,
            start=window_start - timedelta(days=lookback_days),
            end=window_end,
            cache_dir=cache_dir,
            fetcher=actual_fetcher,
            now=now,
            refresh=refresh,
            fetch=fetch,
            fetch_symbol=overrides.get(ticker),
        )
        if series is not None:
            resolved[ticker] = series
    return resolved
