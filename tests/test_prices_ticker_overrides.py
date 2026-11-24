"""Tests for [prices].ticker_overrides threading (FA peak value).

Covers the ISIN-bound override resolution in the FA pipeline, the
cache fetch-symbol invalidation guard, and the yahoo fetcher's
fetch-symbol / identity-guard interaction. The network is never
touched (the conftest neutralises the default fetcher; these tests use
the resolver directly or inject stubs).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.config import TickerOverride
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.prices import resolve_override_symbols
from ibkr_fa.prices.cache import _cache_path, load_or_fetch, write_cache
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import fetch_daily_closes_usd
from ibkr_fa.types import CountryCode, Isin, Ticker

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _lot(*, symbol: str, isin: str) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker(symbol),
        description=f"{symbol} CORP",
        isin=Isin(isin),
        issuer_country_code=CountryCode(isin[:2]),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        cost_basis_money=Decimal("900"),
        side=Side.LONG,
        open_date_time=date(2025, 6, 1),
    )


# ---- resolve_override_symbols (ISIN-bound) --------------------------------


def _override(symbol: str = "XQAL", isin: str = "CA99991A1003", yahoo: str = "XQAM"):
    return TickerOverride(ibkr_ticker=Ticker(symbol), isin=Isin(isin), yahoo_symbol=yahoo)


def _overrides_dict(*ovs: TickerOverride) -> dict[tuple[Ticker, Isin], str]:
    return {(o.ibkr_ticker, o.isin): o.yahoo_symbol for o in ovs}


def test_resolve_applies_override_only_on_ticker_and_isin_match() -> None:
    holdings = [(Ticker("XQAL"), Isin("CA99991A1003"))]
    assert resolve_override_symbols(holdings, _overrides_dict(_override())) == {
        Ticker("XQAL"): "XQAM"
    }


def test_resolve_skips_override_when_isin_differs() -> None:
    # Same ticker, DIFFERENT ISIN than the override → must NOT apply
    # (the ISIN binding is the tax-safety guard).
    holdings = [(Ticker("XQAL"), Isin("US99971T1035"))]
    assert resolve_override_symbols(holdings, _overrides_dict(_override())) == {}


def test_resolve_empty_when_no_overrides() -> None:
    holdings = [(Ticker("XQAL"), Isin("CA99991A1003"))]
    assert resolve_override_symbols(holdings, {}) == {}


def test_resolve_ignores_override_for_unheld_security() -> None:
    holdings = [(Ticker("XQAW"), Isin("US99990E1038"))]
    assert resolve_override_symbols(holdings, _overrides_dict(_override())) == {}


def test_resolve_integrates_with_fa_lots() -> None:
    # Sanity: the (ticker, isin) tuples the FA pipeline feeds resolve
    # correctly from OpenLot objects.
    overlap = CyOverlap(open_lots=[_lot(symbol="XQAL", isin="CA99991A1003")], closed_lots=[])
    holdings = [(lot.symbol, lot.isin) for lot in overlap.open_lots]
    assert resolve_override_symbols(holdings, _overrides_dict(_override())) == {
        Ticker("XQAL"): "XQAM"
    }


# ---- cache fetch-symbol invalidation --------------------------------------


def _series(ticker: str) -> PriceSeries:
    return PriceSeries(
        ticker=Ticker(ticker),
        currency="USD",
        closes={date(2025, 6, 2): Decimal("100")},
        has_split_in_range=False,
        source="test",
        fetched_at=_NOW,
    )


class _StubFetcher:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def __call__(self, ticker, *, start, end, now, fetch_symbol=None):
        self.calls.append(fetch_symbol)
        return _series(str(ticker))


def test_cache_reused_when_fetch_symbol_matches(tmp_path: Path) -> None:
    write_cache(
        _cache_path(tmp_path, Ticker("XQAL")),
        _series("XQAL"),
        covered_start=date(2025, 1, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAM",
    )
    fetcher = _StubFetcher()
    out = load_or_fetch(
        Ticker("XQAL"),
        start=date(2025, 6, 1),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=_NOW,
        fetch_symbol="XQAM",
    )
    assert out is not None
    assert fetcher.calls == []  # served from cache (same fetch_symbol)


def test_cache_refetched_when_fetch_symbol_changes(tmp_path: Path) -> None:
    # Cache produced under fetch_symbol="XQAM"; a run now requesting the
    # bare ticker (override removed) must NOT reuse it.
    write_cache(
        _cache_path(tmp_path, Ticker("XQAL")),
        _series("XQAL"),
        covered_start=date(2025, 1, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAM",
    )
    fetcher = _StubFetcher()
    load_or_fetch(
        Ticker("XQAL"),
        start=date(2025, 6, 1),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=_NOW,
        fetch_symbol=None,  # override removed
    )
    assert fetcher.calls == [None]  # refetched, not served from cache


# ---- yahoo fetch_symbol + identity guard ----------------------------------


def _opener(payload: bytes):
    def _open(url: str, timeout: float) -> bytes:
        # The override symbol must appear in the requested URL.
        assert "/chart/XQAM?" in url
        return payload

    return _open


def _chart(symbol: str, currency: str = "USD") -> bytes:
    ts = int(datetime(2025, 6, 2, 18, tzinfo=UTC).timestamp())
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": currency,
                            "exchangeTimezoneName": "America/New_York",
                            "symbol": symbol,
                        },
                        "timestamp": [ts],
                        "indicators": {"quote": [{"close": [12.5]}]},
                    }
                ],
                "error": None,
            }
        }
    ).encode()


def test_fetch_symbol_drives_url_and_identity_check_against_override() -> None:
    # XQAL holding, fetch_symbol=XQAM, Yahoo returns symbol=XQAM → identity
    # passes (checked against the override target, not the IBKR ticker),
    # and the series is labelled with the IBKR ticker.
    series = fetch_daily_closes_usd(
        Ticker("XQAL"),
        start=date(2025, 6, 1),
        end=date(2025, 12, 31),
        now=_NOW,
        url_opener=_opener(_chart("XQAM")),
        fetch_symbol="XQAM",
    )
    assert series is not None
    assert series.ticker == Ticker("XQAL")  # labelled by holding identity
    assert series.currency == "USD"


def test_fetch_symbol_identity_guard_rejects_wrong_resolved_symbol() -> None:
    # Override says XQAM, but Yahoo resolves to something else → refuse.
    series = fetch_daily_closes_usd(
        Ticker("XQAL"),
        start=date(2025, 6, 1),
        end=date(2025, 12, 31),
        now=_NOW,
        url_opener=_opener(_chart("WRONGCO")),
        fetch_symbol="XQAM",
    )
    assert series is None


def test_fetch_symbol_currency_guard_still_applies() -> None:
    # A mistaken override to a CAD listing is still caught downstream by
    # the compute's currency guard; here we assert the series carries the
    # non-USD currency so the compute will refuse it.
    series = fetch_daily_closes_usd(
        Ticker("XQAL"),
        start=date(2025, 6, 1),
        end=date(2025, 12, 31),
        now=_NOW,
        url_opener=_opener(_chart("XQAM", currency="CAD")),
        fetch_symbol="XQAM",
    )
    assert series is not None
    assert series.currency == "CAD"  # compute layer refuses non-USD
