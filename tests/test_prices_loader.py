"""Tests for :mod:`ibkr_fa.prices.loader` (issue #14).

Verifies the orchestrator: the ``fetch=False`` short-circuit, that only
resolved tickers appear in the result, and that the pre-window lookback
buffer is applied to each fetch (so carry-forward can seed a window
that starts on a non-trading day).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ibkr_fa.prices.cache import _cache_path, write_cache
from ibkr_fa.prices.loader import DEFAULT_LOOKBACK_DAYS, load_price_series
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import Ticker

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _series(ticker: str) -> PriceSeries:
    return PriceSeries(
        ticker=Ticker(ticker),
        currency="USD",
        closes={date(2025, 12, 30): Decimal("100")},
        has_split_in_range=False,
        source="test",
        fetched_at=_NOW,
    )


class _Fetcher:
    def __init__(self, resolves: set[str]) -> None:
        self.resolves = resolves
        self.calls: list[tuple[Ticker, date, date]] = []
        self.fetch_symbols: list[str | None] = []

    def __call__(
        self,
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        self.calls.append((ticker, start, end))
        self.fetch_symbols.append(fetch_symbol)
        # A resolving ticker is one whose effective fetch symbol is in `resolves`.
        effective = fetch_symbol if fetch_symbol is not None else str(ticker)
        return _series(str(ticker)) if effective in self.resolves else None


def test_fetch_false_without_cache_returns_empty(tmp_path: Path) -> None:
    fetcher = _Fetcher({"XQAA"})
    out = load_price_series(
        {Ticker("XQAA"): (date(2025, 1, 1), date(2025, 12, 31))},
        cache_dir=tmp_path,
        fetch=False,
        fetcher=fetcher,
        now=_NOW,
    )
    assert out == {}
    assert fetcher.calls == []


def test_fetch_false_reads_complete_integrity_checked_cache(tmp_path: Path) -> None:
    ticker = Ticker("XQAA")
    write_cache(
        _cache_path(tmp_path, ticker),
        _series("XQAA"),
        covered_start=date(2025, 1, 1) - timedelta(days=DEFAULT_LOOKBACK_DAYS),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _Fetcher({"XQAA"})
    out = load_price_series(
        {ticker: (date(2025, 1, 1), date(2025, 12, 31))},
        cache_dir=tmp_path,
        fetch=False,
        fetcher=fetcher,
        now=_NOW,
    )
    assert out == {ticker: _series("XQAA")}
    assert fetcher.calls == []


def test_only_resolved_tickers_returned(tmp_path: Path) -> None:
    fetcher = _Fetcher({"XQAA"})  # XQAY does not resolve
    out = load_price_series(
        {
            Ticker("XQAA"): (date(2025, 1, 1), date(2025, 12, 31)),
            Ticker("XQAY"): (date(2025, 1, 1), date(2025, 12, 31)),
        },
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=_NOW,
    )
    assert set(out) == {Ticker("XQAA")}


def test_lookback_buffer_applied(tmp_path: Path) -> None:
    fetcher = _Fetcher({"XQAA"})
    load_price_series(
        {Ticker("XQAA"): (date(2025, 1, 1), date(2025, 12, 31))},
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=_NOW,
        lookback_days=10,
    )
    _, fetch_start, fetch_end = fetcher.calls[0]
    assert fetch_start == date(2025, 1, 1) - timedelta(days=10)
    assert fetch_end == date(2025, 12, 31)
    assert DEFAULT_LOOKBACK_DAYS == 10
