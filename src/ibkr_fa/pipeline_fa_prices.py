"""Schedule FA Table A3 daily-close price loading.

Extracted from :mod:`ibkr_fa.pipeline_fa`. ``load_fa_price_series`` resolves the
per-ticker daily-close series used for the FA peak-value computation (issue #14).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.config import TickerOverride
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.prices import load_price_series, resolve_override_symbols
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.types import Isin, Ticker


class TickerIsinCollisionError(IbkrFaError):
    """One ticker names two ISINs among the lots Schedule FA prices (#916)."""


def load_fa_price_series(
    overlap: CyOverlap,
    *,
    calendar_year: int,
    input_path: Path,
    fetch_prices: bool,
    price_cache_dir: Path | None,
    price_fetcher: DailyCloseFetcher | None,
    price_now: datetime | None,
    ticker_overrides: tuple[TickerOverride, ...] = (),
) -> Mapping[Ticker, PriceSeries]:
    """Resolve per-ticker daily-close series for FA peak value (#14).

    With ``fetch_prices`` False, only complete caches are read and the
    network is never called. Every cache read checks the cache's payload
    hash. The cache dir (immutable price audit artifact) defaults next to
    the input statement. ``ticker_overrides`` (``[prices]`` config) redirect the
    price-source symbol via the ISIN-bound
    :func:`resolve_override_symbols`.

    Raises:
        TickerIsinCollisionError: If one symbol names more than one ISIN
            among the lots priced. Every lot of a ticker reads one
            series, so two securities would share it. Raised before any
            price is fetched or read from the cache.
        PriceCacheError: If a price cache or its lock exists but can't be
            used. The cache is neither refetched nor overwritten (#940).
        PriceSourceContradictionError: If a fetched response has two bars
            on one date. No cache is written (#942).
        YahooResponseFormatError: If a fetched Yahoo response doesn't have
            the accepted shape. No cache is written for that response (#1139).
    """
    cy_window = (date(calendar_year, 1, 1), date(calendar_year, 12, 31))
    lots = (*overlap.open_lots, *overlap.closed_lots)
    isins_by_symbol: dict[Ticker, set[Isin]] = {}
    for lot in lots:
        isins_by_symbol.setdefault(lot.symbol, set()).add(lot.isin)
    collisions = sum(1 for isins in isins_by_symbol.values() if len(isins) > 1)
    if collisions:
        raise TickerIsinCollisionError(
            "Schedule FA prices every lot of a ticker from one daily-close series, "
            f"but {collisions} ticker(s) name more than one ISIN among the lots "
            f"priced for CY {calendar_year}; check your lots for a symbol shared by "
            "two securities."
        )
    windows: dict[Ticker, tuple[date, date]] = dict.fromkeys(
        (lot.symbol for lot in lots), cy_window
    )
    fetch_symbols = resolve_override_symbols(
        ((lot.symbol, lot.isin) for lot in lots),
        {(o.ibkr_ticker, o.isin): o.yahoo_symbol for o in ticker_overrides},
    )
    cache_dir = price_cache_dir or (input_path.parent / DEFAULT_PRICE_CACHE_DIR_NAME)
    return load_price_series(
        windows,
        cache_dir=cache_dir,
        fetch=fetch_prices,
        fetcher=price_fetcher,
        now=price_now,
        fetch_symbols=fetch_symbols,
    )
