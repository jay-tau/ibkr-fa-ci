"""Load and audit identity-bound Robinhood equity closes."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from ibkr_fa.compute.robinhood_a2_peak import (
    RobinhoodA2PeakError,
    RobinhoodA2PeakIdentityError,
    robinhood_required_equity_sessions,
)
from ibkr_fa.models import FaA3PeakBlankReason
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_BLANK_TEXT
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from ibkr_fa.pipeline_robinhood_models import RobinhoodEquityPriceAudit
from ibkr_fa.prices.cache import DEFAULT_PRICE_CACHE_DIR_NAME
from ibkr_fa.prices.loader import load_price_series, resolve_override_symbols
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher, fetch_daily_closes_usd_retrying
from ibkr_fa.types import Ticker

# Robinhood's price cache directory also holds its SEC identity and entity
# records and its Massive option closes, so a remedy moves only the holding's
# own cache file and never empties the directory (#1476). A matching cache is
# reused before any fetch.
MOVE_HOLDING_CACHE_ASIDE = (
    "Move each such holding's cache file, and any .lock beside it, out of the price "
    "cache directory (--price-cache-dir) and re-run with price fetching"
)


def _coverage_fixes(
    required: Mapping[Ticker, frozenset[date]],
    prices: Mapping[Ticker, PriceSeries],
    *,
    fetch: bool,
) -> str:
    """What to do for each gap the equity coverage audit found (#1523).

    A series that covers the window, cached under the same fetch symbol, is
    reused before any fetch (:func:`~ibkr_fa.prices.cache.load_or_fetch`), so
    a re-run with fetching on fills a missing series, and a short one only
    once the holding's cache file, and any lock, is moved aside (#1528). A
    lock would also refuse a new override's fetch symbol, so the override
    route moves it too. The refusal names no holding (#1047), so a filer who
    can't tell which one is short moves every unlocked series aside instead
    (#1530). Locked ones stay, since a lock pins reviewed closes, and a moved
    file the re-run didn't recreate goes back: the re-run fetched no series
    for it. The daily account peak needs every held session's close, and
    there is no manual Robinhood peak input, so a series still short after a
    fresh fetch leaves Schedule FA to the filer.
    """
    fixes: list[str] = []
    if len(prices) != len(required):
        if fetch:
            cause, remedy = FA_A3_PEAK_BLANK_TEXT[FaA3PeakBlankReason.NO_USD_SERIES]
        else:
            cause = FA_A3_PEAK_BLANK_TEXT[FaA3PeakBlankReason.PRICES_NOT_FETCHED][0]
            remedy = (
                "Re-run without --no-fetch-prices, or point --price-cache-dir at a cache "
                "directory that holds those closes."
            )
        fixes.append(f"A held security has no price series because {cause}. {remedy}")
    if any(
        not sessions <= prices[ticker].closes.keys()
        for ticker, sessions in required.items()
        if ticker in prices
    ):
        fixes.append(
            "A held security's price series has no close for a US market session it was "
            "held (for example a ticker that names a different security, a trading halt or "
            "a delisting), and a cached series is reused before any fetch, so a re-run alone "
            f"returns the same closes. {MOVE_HOLDING_CACHE_ASIDE}: a fresh fetch can return "
            "closes a series cached earlier lacks. If you can't tell which holding is short, "
            "move every .csv file directly in that directory that has no .csv.lock beside it "
            "out of it instead and re-run the same way; then move back each moved file the "
            "re-run didn't recreate, and re-run once more if it stopped. Leave the directory's "
            "subfolders (identity and entity records, Massive option closes) and each .csv "
            "with a .csv.lock beside it (reviewed closes) where they are. If the ticker names "
            "a different security and you can VERIFY a USD price-source symbol for the SAME "
            "security (match the ISIN), map it with [[prices.ticker_overrides]] in user.toml "
            "before that re-run; a wrong mapping can produce a wrong figure, so confirm the "
            "security first. If the fresh series is still short, enter the Robinhood Schedule "
            "FA by hand: closes are never guessed."
        )
    return " ".join(fixes)


def load_robinhood_equity_prices(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    input_dir: Path,
    *,
    fetch_prices: bool,
    price_cache_dir: Path | None,
    price_fetcher: DailyCloseFetcher | None,
    quantities: RobinhoodSecurityQuantityResult,
) -> tuple[dict[Ticker, PriceSeries], RobinhoodEquityPriceAudit]:
    start = date(financial_year.start_year, 1, 1)
    end = date(financial_year.start_year, 12, 31)
    try:
        required = robinhood_required_equity_sessions(
            bundle.paired,
            config=bundle.robinhood,
            securities=bundle.securities,
            quantities=quantities,
            calendar_year=financial_year.start_year,
        )
    except RobinhoodA2PeakError as exc:
        return {}, RobinhoodEquityPriceAudit(
            errors=(str(exc),),
            unconfigured_identity=isinstance(exc, RobinhoodA2PeakIdentityError),
        )
    windows = dict.fromkeys(required, (start, end))
    required_symbols = set(required)
    holdings = tuple(
        (item.instrument.symbol, item.instrument.isin)
        for item in bundle.securities.securities
        if item.instrument.symbol in required_symbols
    )
    overrides = {
        (item.ibkr_ticker, item.isin): item.yahoo_symbol
        for item in bundle.config.prices.ticker_overrides
    }
    fetcher = price_fetcher
    if fetcher is None:
        last_request = 0.0

        def fetcher(
            ticker: Ticker,
            *,
            start: date,
            end: date,
            now: datetime,
            fetch_symbol: str | None = None,
        ) -> PriceSeries | None:
            nonlocal last_request
            delay = 0.25 - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
            last_request = time.monotonic()
            return fetch_daily_closes_usd_retrying(
                ticker,
                start=start,
                end=end,
                now=now,
                fetch_symbol=fetch_symbol,
            )

    prices = load_price_series(
        windows,
        cache_dir=price_cache_dir or (input_dir / DEFAULT_PRICE_CACHE_DIR_NAME),
        fetch=fetch_prices,
        fetcher=fetcher,
        fetch_symbols=resolve_override_symbols(holdings, overrides),
    )
    required_count = sum(len(sessions) for sessions in required.values())
    covered_count = sum(
        len(sessions & prices[ticker].closes.keys())
        for ticker, sessions in required.items()
        if ticker in prices
    )
    errors = ()
    if len(prices) != len(required) or covered_count != required_count:
        errors = (
            "Robinhood equity close coverage is incomplete "
            f"({len(prices)}/{len(required)} securities, "
            f"{covered_count}/{required_count} held sessions). "
            + _coverage_fixes(required, prices, fetch=fetch_prices),
        )
    audit = RobinhoodEquityPriceAudit(
        resolved_securities=len(prices),
        required_sessions=required_count,
        covered_sessions=covered_count,
        warnings=(
            (
                "Yahoo equity close audit: complete "
                f"({len(prices)} securities, {covered_count}/{required_count} held sessions)"
            ),
        )
        if not errors and required
        else (),
        errors=errors,
    )
    return prices, audit
