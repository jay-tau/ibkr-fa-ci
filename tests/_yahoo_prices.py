"""The Yahoo chart payloads, clock, price fetchers and refusal texts that the
Yahoo price tests and the tests on the default price path share. Every value
is synthetic, and no fetcher touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from ibkr_fa.prices import yahoo
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import (
    DailyCloseFetcher,
    _YahooTransportResult,
    fetch_daily_closes_usd,
    fetch_daily_closes_usd_retrying,
)
from ibkr_fa.types import Ticker

# The Yahoo price tests' clock, 1 June 2026 00:00 UTC. The price cache tests
# use CACHE_NOW (tests/_price_cache.py), 12:00 UTC the same day.
YAHOO_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def bar_epoch(day: date) -> int:
    # 18:00 UTC == early afternoon in America/New_York for any date,
    # so the bar resolves to `day` regardless of DST.
    return int(datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC).timestamp())


def chart_payload(
    *,
    currency: str = "USD",
    tz: str = "America/New_York",
    symbol: str = "X",
    bars: dict[date, float | None] | None = None,
    highs: dict[date, float | None] | None = None,
    adj: dict[date, float] | None = None,
    splits: dict | None = None,
    error: object = None,
    result: object = "default",
) -> bytes:
    bars = bars or {}
    timestamps = [bar_epoch(d) for d in bars]
    closes = list(bars.values())
    quote: dict = {"close": closes}
    if highs is not None:
        quote["high"] = [highs.get(d) for d in bars]
    res: dict = {
        "meta": {
            "currency": currency,
            "exchangeTimezoneName": tz,
            "symbol": symbol,
            "instrumentType": "EQUITY",
        },
        "timestamp": timestamps,
        "indicators": {"quote": [quote]},
    }
    if adj is not None:
        res["indicators"]["adjclose"] = [{"adjclose": [adj[d] for d in bars]}]
    if splits is not None:
        res["events"] = {"splits": splits}
    results = [res] if result == "default" else result
    return json.dumps({"chart": {"result": results, "error": error}}).encode()


def payload_opener(payload: bytes | None):
    def _open(url: str, timeout: float) -> bytes | None:
        return payload

    return _open


def fetch_payload(payload: bytes | None, *, start: date, end: date):
    return fetch_daily_closes_usd(
        Ticker("X"), start=start, end=end, now=YAHOO_NOW, url_opener=payload_opener(payload)
    )


NOT_JSON_REFUSAL = "Yahoo price response: the body isn't JSON"

SPLIT_DAY = date(2025, 6, 2)
SPLIT_KEY = str(bar_epoch(SPLIT_DAY))

EXACT_DAY = date(2025, 3, 3)


def one_split(numerator: float, denominator: float) -> dict:
    """``events.splits`` with one split on 2 June 2025."""
    return {
        SPLIT_KEY: {
            "date": bar_epoch(SPLIT_DAY),
            "numerator": numerator,
            "denominator": denominator,
        }
    }


def split_fetcher(
    close: float, high: float, numerator: float, denominator: float
) -> DailyCloseFetcher:
    """A price fetcher that runs the Yahoo parser on one 3 March 2025 bar with
    ``close`` and ``high``, and a ``numerator``-for-``denominator`` split on
    2 June 2025."""
    payload = chart_payload(
        symbol="XQAA",
        bars={EXACT_DAY: close},
        highs={EXACT_DAY: high},
        splits=one_split(numerator, denominator),
    )

    def fetch(
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        return fetch_daily_closes_usd(
            ticker,
            start=start,
            end=end,
            now=now,
            fetch_symbol=fetch_symbol,
            url_opener=payload_opener(payload),
        )

    return fetch


NO_ZONE_REFUSAL = (
    "Yahoo price response, meta.exchangeTimezoneName: "
    "the exchange timezone is missing or not a known zone"
)


def without_zone(result: dict) -> None:
    del result["meta"]["exchangeTimezoneName"]


MONDAY = date(2025, 6, 2)
TUESDAY = date(2025, 6, 3)


def epoch_at(day: date, hour: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp())


def stamped_payload(bars: list[tuple[object, float | None]], *, symbol: str = "X") -> bytes:
    """A chart response with explicit ``(timestamp, close)`` bars.

    ``chart_payload`` keys bars by date, so it can't hold two bars on one date.
    """
    payload = json.loads(chart_payload(symbol=symbol))
    result = payload["chart"]["result"][0]
    result["timestamp"] = [stamp for stamp, _ in bars]
    result["indicators"]["quote"][0]["close"] = [close for _, close in bars]
    return json.dumps(payload).encode()


def repeated_date_payload(symbol: str = "X") -> bytes:
    """The #942 example: a Monday 2 June 2025 bar of $100 at 14:00 UTC, then one
    of $10 at 18:00 UTC (both that New York date), and a Tuesday bar of $20."""
    return stamped_payload(
        [
            (epoch_at(MONDAY, 14), 100.0),
            (epoch_at(MONDAY, 18), 10.0),
            (epoch_at(TUESDAY, 18), 20.0),
        ],
        symbol=symbol,
    )


def repeated_date_fetcher(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime,
    fetch_symbol: str | None = None,
) -> PriceSeries | None:
    """A price fetcher that runs the Yahoo parser on :func:`repeated_date_payload`."""
    return fetch_daily_closes_usd(
        ticker,
        start=start,
        end=end,
        now=now,
        fetch_symbol=fetch_symbol,
        url_opener=payload_opener(repeated_date_payload(fetch_symbol or str(ticker))),
    )


def serve_no_zone_response(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer every default-path Yahoo fetch, through the real fetchers, with a
    response for the requested symbol that has no exchange zone; returns the
    URLs requested."""
    urls: list[str] = []

    def open_url(url: str, _timeout: float) -> _YahooTransportResult:
        urls.append(url)
        symbol = url.split("/chart/", 1)[1].split("?", 1)[0]
        payload = json.loads(chart_payload(symbol=symbol, bars={date(2025, 6, 2): 4.8217}))
        without_zone(payload["chart"]["result"][0])
        return _YahooTransportResult(json.dumps(payload).encode())

    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetch_daily_closes_usd)
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_equity_prices.fetch_daily_closes_usd_retrying",
        fetch_daily_closes_usd_retrying,
    )
    monkeypatch.setattr(yahoo, "_default_url_opener_result", open_url)
    return urls
