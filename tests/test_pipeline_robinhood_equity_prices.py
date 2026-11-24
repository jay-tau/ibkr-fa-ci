from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import ibkr_fa.pipeline_robinhood_core as core
import ibkr_fa.pipeline_robinhood_equity_prices as equity_prices
from ibkr_fa.cli import _build_parser
from ibkr_fa.compute.robinhood_options import RobinhoodOptionsResult
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import RobinhoodSecurityConfig, TickerOverride, load_user_config
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodInventoryBlocker,
    RobinhoodInventoryBlockerKind,
    RobinhoodInventoryUnprovedFact,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from ibkr_fa.prices.cache import PriceCacheError, read_cache, write_cache, write_cache_lock
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher, YahooResponseFormatError
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._cli_subcommands import subcommands as _commands
from tests._robinhood_equity import SECURITY, held_equity
from tests._robinhood_fa_blocker_lines import ANCHOR, INVENTORY_INCOMPLETE, REFERENCES
from tests._robinhood_option_audit import (
    OPTION_NO_KEY,
    OPTION_NOT_CACHED,
    OPTION_SHORT,
    seed_records,
)
from tests._robinhood_refusals import EQUITY_COVERAGE_REFUSAL, PRICE_CACHE_MOVE
from tests._robinhood_security import configured_securities, robinhood_security
from tests._yahoo_prices import NO_ZONE_REFUSAL, serve_no_zone_response


def _bundle(securities: tuple[RobinhoodSecurityConfig, ...] = (SECURITY,)):
    return SimpleNamespace(
        paired=object(),
        quantities=object(),
        robinhood=SimpleNamespace(),
        securities=RobinhoodSecurityIdentities(securities),
        config=SimpleNamespace(prices=SimpleNamespace(ticker_overrides=())),
    )


def _no_guidance(_guidance: str) -> Path:
    raise AssertionError("no identity guidance is written")


def _series() -> PriceSeries:
    return PriceSeries(
        ticker=Ticker("TEST"),
        currency="USD",
        closes={date(2025, 1, 2): Decimal("10")},
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )


def test_equity_audit_uses_full_year_for_only_required_tickers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured security the replay doesn't require gets no window and
    no price series (#1694)."""
    required = {Ticker("TEST"): frozenset({date(2025, 1, 2)})}
    monkeypatch.setattr(
        equity_prices,
        "robinhood_required_equity_sessions",
        lambda *_args, **_kwargs: required,
    )
    calls = {}

    def load(windows, **_kwargs):
        calls["windows"] = windows
        return {ticker: replace(_series(), ticker=ticker) for ticker in windows}

    monkeypatch.setattr(equity_prices, "load_price_series", load)
    bundle = _bundle((SECURITY, SECOND))
    prices, audit = equity_prices.load_robinhood_equity_prices(
        bundle,
        FinancialYear(2025),
        tmp_path,
        fetch_prices=True,
        price_cache_dir=None,
        price_fetcher=None,
        quantities=bundle.quantities,
    )
    assert set(prices) == {Ticker("TEST")}
    assert calls == {"windows": {Ticker("TEST"): (date(2025, 1, 1), date(2025, 12, 31))}}
    assert (audit.required_sessions, audit.covered_sessions) == (1, 1)
    assert audit.errors == ()


def test_equity_prices_refuse_a_response_with_no_exchange_zone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1139: on the default retrying fetcher, a Yahoo response with no
    exchange zone refuses after one request, with no price cache written."""
    monkeypatch.setattr(
        equity_prices,
        "robinhood_required_equity_sessions",
        lambda *_args, **_kwargs: {Ticker("XQYH"): frozenset({date(2025, 1, 2)})},
    )
    urls = serve_no_zone_response(monkeypatch)
    bundle = _bundle()
    with pytest.raises(YahooResponseFormatError) as exc:
        equity_prices.load_robinhood_equity_prices(
            bundle,
            FinancialYear(2025),
            tmp_path,
            fetch_prices=True,
            price_cache_dir=None,
            price_fetcher=None,
            quantities=bundle.quantities,
        )
    assert str(exc.value) == NO_ZONE_REFUSAL
    assert len(urls) == 1
    assert not (tmp_path / ".ibkr-fa-price-cache").exists()


def test_equity_audit_hard_fails_incomplete_required_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        equity_prices,
        "robinhood_required_equity_sessions",
        lambda *_args, **_kwargs: {Ticker("TEST"): frozenset({date(2025, 1, 2)})},
    )
    monkeypatch.setattr(
        equity_prices,
        "load_price_series",
        lambda *_args, **_kwargs: {},
    )
    bundle = _bundle()
    _prices, audit = equity_prices.load_robinhood_equity_prices(
        bundle,
        FinancialYear(2025),
        tmp_path,
        fetch_prices=False,
        price_cache_dir=None,
        price_fetcher=None,
        quantities=bundle.quantities,
    )
    assert audit.resolved_securities == 0
    assert audit.errors
    assert "0/1 securities" in audit.errors[0]


def test_inventory_blockers_precede_option_and_equity_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = RobinhoodInventoryBlocker(
        account_index=0,
        cusip=None,
        unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
        kind=RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH,
        statement_index=0,
    )
    bundle = SimpleNamespace(
        quantities=SimpleNamespace(blockers=(blocker,), identity_blockers=()),
        identity_source_audit=SimpleNamespace(failure="SEC identity source unavailable"),
        identity_resolution=None,
    )
    monkeypatch.setattr(
        core,
        "resolved_robinhood_quantities",
        lambda _bundle: bundle.quantities,
    )
    monkeypatch.setattr(core, "identity_failure_guidance", lambda _bundle: "")
    monkeypatch.setattr(
        core._option_prices,
        "collect_robinhood_option_price_audit",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("option audit ran")),
    )
    line = ANCHOR.format(references=f"{REFERENCES}account_index 0 statement 1")
    # The source audit's details are no longer printed (#1044); the blocker's
    # line is (#1550).
    with pytest.raises(
        core.RobinhoodPipelineError,
        match=rf"\A{re.escape(INVENTORY_INCOMPLETE)}\n{re.escape(line)}\Z",
    ):
        core.fa_prices(
            cast("RobinhoodBundle", bundle),
            FinancialYear(2025),
            cast("RobinhoodOptionsResult", SimpleNamespace()),
            tmp_path,
            tmp_path / "user.toml",
            fetch_prices=True,
            price_cache_dir=None,
            price_fetcher=None,
            write_guidance=_no_guidance,
        )


def _unsupported_year_case(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RobinhoodBundle, list[object], list[object], DailyCloseFetcher]:
    paired, config, quantities = held_equity(
        opening_date=date(2025, 1, 1),
        movement_date=date(2026, 1, 5),
    )
    bundle = SimpleNamespace(
        paired=paired,
        quantities=quantities,
        robinhood=config,
        securities=configured_securities(config),
        config=SimpleNamespace(prices=SimpleNamespace(ticker_overrides=())),
    )
    loads: list[object] = []
    fetches: list[object] = []
    real_load = equity_prices.load_price_series

    def load(windows, **kwargs):
        loads.append(windows)
        return real_load(windows, **kwargs)

    def fetch(ticker, **_kwargs):
        fetches.append(ticker)
        raise AssertionError("price fetcher ran")

    monkeypatch.setattr(equity_prices, "load_price_series", load)
    return cast("RobinhoodBundle", bundle), loads, fetches, fetch


def test_equity_audit_refuses_unsupported_year_before_loading_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, loads, fetches, fetch = _unsupported_year_case(monkeypatch)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    prices, audit = equity_prices.load_robinhood_equity_prices(
        bundle,
        FinancialYear(2026),
        tmp_path,
        fetch_prices=True,
        price_cache_dir=cache_dir,
        price_fetcher=fetch,
        quantities=bundle.quantities,
    )
    assert prices == {}
    (error,) = audit.errors
    assert "does not support calendar year 2026" in error
    assert (loads, fetches) == ([], [])


def test_unsupported_year_refuses_at_equity_audit_before_loading_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, loads, fetches, fetch = _unsupported_year_case(monkeypatch)
    monkeypatch.setattr(core, "resolved_robinhood_quantities", lambda _bundle: bundle.quantities)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    with pytest.raises(
        core.RobinhoodPipelineError,
        match="does not support calendar year 2026",
    ):
        core.fa_prices(
            bundle,
            FinancialYear(2026),
            cast("RobinhoodOptionsResult", SimpleNamespace(trades=(), valuation_anchors=())),
            tmp_path,
            tmp_path / "user.toml",
            fetch_prices=True,
            price_cache_dir=cache_dir,
            price_fetcher=fetch,
            write_guidance=_no_guidance,
        )
    assert (loads, fetches) == ([], [])


# #1523: each coverage refusal names the fix for the gap the run knows, and
# following it gets past the stop. The account holds one TEST share from
# Monday 29 December 2025: three held sessions.
_HEAD = EQUITY_COVERAGE_REFUSAL + " ({}/{} securities, {}/{} held sessions)."
OFFLINE_MISSING = (
    "A held security has no price series because price fetching was off (--no-fetch-prices) "
    "and no complete price cache was found. Re-run without --no-fetch-prices, or point "
    "--price-cache-dir at a cache directory that holds those closes."
)
ONLINE_MISSING = (
    "A held security has no price series because the price source returned no USD daily "
    "series for the ticker: an unknown, renamed or non-USD ticker (e.g. a security your "
    "broker lists under a different symbol than the price source), or a failed fetch. If "
    "the fetch failed, re-run. If you can VERIFY a USD price-source symbol for the SAME "
    "security (match the ISIN), map it with [[prices.ticker_overrides]] in user.toml so the "
    "peak is computed; a wrong mapping can produce a wrong figure, so confirm the security "
    "first."
)
SHORT = (
    "A held security's price series has no close for a US market session it was held (for "
    "example a ticker that names a different security, a trading halt or a delisting), and "
    "a cached series is reused before any fetch, so a re-run alone returns the same closes. "
    f"{PRICE_CACHE_MOVE}: a fresh fetch can return "
    "closes a series cached earlier lacks. If you can't tell which holding is short, move "
    "every .csv file directly in that directory that has no .csv.lock beside it out of it "
    "instead and re-run the same way; then move back each moved file the re-run didn't "
    "recreate, and re-run once more if it stopped. Leave the directory's subfolders (identity "
    "and entity records, Massive option closes) and each .csv with a .csv.lock beside it "
    "(reviewed closes) where they are. If the ticker names a different security and you can "
    "VERIFY a USD price-source symbol for the SAME security (match the ISIN), map it with "
    "[[prices.ticker_overrides]] in user.toml before that re-run; a wrong mapping can produce "
    "a wrong figure, so confirm the security first. If the fresh series is still short, enter "
    "the Robinhood Schedule FA by hand: closes are never guessed."
)
_SKIPPED = date(2025, 12, 30)
# A second held security (#1530).
SECOND = robinhood_security(symbol="XQSE", cusip="99995A105", isin="US99995A1052")


def _held_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    override: str | None = None,
    securities: tuple[RobinhoodSecurityConfig, ...] = (SECURITY,),
):
    paired, config, quantities = held_equity(
        opening_date=date(2025, 1, 1),
        movement_date=date(2025, 12, 29),
        securities=securities,
    )
    overrides = (
        ()
        if override is None
        else (TickerOverride(SECURITY.instrument.symbol, SECURITY.instrument.isin, override),)
    )
    monkeypatch.setattr(core, "resolved_robinhood_quantities", lambda _bundle: quantities)
    return cast(
        "RobinhoodBundle",
        SimpleNamespace(
            paired=paired,
            quantities=quantities,
            robinhood=config,
            securities=configured_securities(config),
            config=SimpleNamespace(prices=SimpleNamespace(ticker_overrides=overrides)),
        ),
    )


def _fetcher(
    calls: list[str | None], *, short: bool = False, empty: bool = False, filled: bool = False
):
    """A stub price source closing every session at 10, recording each fetch's
    symbol. When ``short``, the TEST holding's own symbol has no 30 December
    close and only a mapped symbol has every close, unless ``filled``: then
    only its first fetch lacks it, as when the source fills a gap after a
    cache was written. When ``empty``, there is no series at all."""
    fetched: list[Ticker] = []

    def fetch(
        ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
    ) -> PriceSeries | None:
        calls.append(fetch_symbol)
        fetched.append(ticker)
        if empty:
            return None
        gap = (
            short
            and ticker == SECURITY.instrument.symbol
            and fetch_symbol is None
            and not (filled and fetched.count(ticker) > 1)
        )
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes={
                day: Decimal("10")
                for day in us_market_sessions(start, end)
                if not (gap and day == _SKIPPED)
            },
            has_split_in_range=False,
            source="synthetic",
            fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
            price_basis=CONTEMPORANEOUS_BASIS,
        )

    return fetch


def _fa_prices(
    bundle: RobinhoodBundle, tmp_path: Path, cache: Path, *, fetch: bool, fetcher: DailyCloseFetcher
):
    return core.fa_prices(
        bundle,
        FinancialYear(2025),
        cast("RobinhoodOptionsResult", SimpleNamespace(trades=(), valuation_anchors=())),
        tmp_path,
        tmp_path / "user.toml",
        fetch_prices=fetch,
        price_cache_dir=cache,
        price_fetcher=fetcher,
        write_guidance=_no_guidance,
    )


def _refusal(
    bundle: RobinhoodBundle, tmp_path: Path, cache: Path, *, fetch: bool, fetcher: DailyCloseFetcher
) -> str:
    with pytest.raises(core.RobinhoodPipelineError) as raised:
        _fa_prices(bundle, tmp_path, cache, fetch=fetch, fetcher=fetcher)
    return str(raised.value)


@pytest.mark.parametrize("follow", ["re-run with fetching", "point at a populated cache"])
def test_offline_refusal_with_no_cache_names_the_fix_that_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, follow: str
) -> None:
    bundle = _held_bundle(monkeypatch)
    calls: list[str | None] = []
    fetch = _fetcher(calls)
    populated = tmp_path / "populated"
    _fa_prices(bundle, tmp_path, populated, fetch=True, fetcher=fetch)
    empty = tmp_path / "empty"
    empty.mkdir()
    calls.clear()

    refusal = _refusal(bundle, tmp_path, empty, fetch=False, fetcher=fetch)

    assert refusal == f"{_HEAD.format(0, 1, 0, 3)} {OFFLINE_MISSING}"
    assert calls == []
    if follow == "re-run with fetching":
        _prices, audit, _options, _quantities = _fa_prices(
            bundle, tmp_path, empty, fetch=True, fetcher=fetch
        )
        assert calls == [None]
    else:
        _prices, audit, _options, _quantities = _fa_prices(
            bundle, tmp_path, populated, fetch=False, fetcher=fetch
        )
        assert calls == []
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 3, 3)


def test_online_refusal_with_no_series_names_the_shared_fix_and_a_rerun_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fetch caches nothing, so the re-run the text names fetches again."""
    bundle = _held_bundle(monkeypatch)
    calls: list[str | None] = []
    cache = tmp_path / "cache"

    refusal = _refusal(bundle, tmp_path, cache, fetch=True, fetcher=_fetcher(calls, empty=True))

    assert refusal == f"{_HEAD.format(0, 1, 0, 3)} {ONLINE_MISSING}"
    _prices, audit, _options, _quantities = _fa_prices(
        bundle, tmp_path, cache, fetch=True, fetcher=_fetcher(calls)
    )
    assert calls == [None, None]
    assert audit.errors == ()


def _move_aside(cache: Path, aside: Path) -> None:
    """Follow the text: move the holding's cache file, and any .lock beside
    it, out of the price cache directory, leaving the rest of it."""
    aside.mkdir()
    for name in ("TEST.csv", "TEST.csv.lock"):
        if (cache / name).exists():
            (cache / name).rename(aside / name)


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_short_cached_series_clears_once_its_file_is_moved_aside_and_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1528: the cache was written before the source filled a gap. The text
    names moving the holding's cache file aside and re-running with price
    fetching, which fetches a fresh, complete series."""
    bundle = _held_bundle(monkeypatch)
    calls: list[str | None] = []
    source = _fetcher(calls, short=True, filled=True)
    cache = tmp_path / "cache"
    refusal = f"{_HEAD.format(1, 1, 2, 3)} {SHORT}"

    assert _refusal(bundle, tmp_path, cache, fetch=True, fetcher=source) == refusal
    assert _refusal(bundle, tmp_path, cache, fetch=fetch, fetcher=source) == refusal
    assert calls == [None]

    _move_aside(cache, tmp_path / "aside")
    _prices, audit, _options, _quantities = _fa_prices(
        bundle, tmp_path, cache, fetch=True, fetcher=source
    )
    assert calls == [None, None]
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 3, 3)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_short_series_names_the_override_fix_and_following_it_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool
) -> None:
    """The source has no close for one held session under the holding's own
    symbol. A re-run, online or offline, reuses the short cache. The text's
    override route moves the cache file and any lock aside first, since a
    locked cache refuses the mapped fetch symbol (#1528)."""
    bundle = _held_bundle(monkeypatch)
    calls: list[str | None] = []
    fetch = _fetcher(calls, short=True)
    cache = tmp_path / "cache"
    refusal = f"{_HEAD.format(1, 1, 2, 3)} {SHORT}"

    assert _refusal(bundle, tmp_path, cache, fetch=True, fetcher=fetch) == refusal
    if locked:
        write_cache_lock(cache / "TEST.csv")
    assert _refusal(bundle, tmp_path, cache, fetch=True, fetcher=fetch) == refusal
    assert _refusal(bundle, tmp_path, cache, fetch=False, fetcher=fetch) == refusal
    assert calls == [None]

    mapped = _held_bundle(monkeypatch, override="XQOV")
    if locked:
        with pytest.raises(PriceCacheError, match="doesn't match this request"):
            _fa_prices(mapped, tmp_path, cache, fetch=True, fetcher=fetch)
    _move_aside(cache, tmp_path / "aside")
    _prices, audit, _options, _quantities = _fa_prices(
        mapped, tmp_path, cache, fetch=True, fetcher=fetch
    )
    assert calls == [None, "XQOV"]
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 3, 3)


def _review(path: Path) -> None:
    """Make a cached series a reviewed one, closing at 20 on every session,
    and lock it."""
    cached = read_cache(path)
    assert cached is not None
    series, start, end, symbol = cached
    reviewed = replace(series, closes=dict.fromkeys(series.closes, Decimal("20")))
    write_cache(path, reviewed, covered_start=start, covered_end=end, fetch_symbol=symbol)
    write_cache_lock(path)


def _short_beside_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fetch: bool, reviewed: bool = False
) -> SimpleNamespace:
    """TEST and SECOND are held and cached in a price cache directory that
    also keeps records in its subfolders. TEST's series was cached before the
    source filled its 30 December close; SECOND's is complete, and when
    ``reviewed`` it is a locked, reviewed series. A re-run, online or as
    ``fetch`` says, refuses the same and names neither holding. A ticker added
    to ``gone`` is one the source no longer returns."""
    bundle = _held_bundle(monkeypatch, securities=(SECURITY, SECOND))
    cache = tmp_path / "cache"
    records = seed_records(cache, "massive-options/record.json")
    calls: list[str | None] = []
    gone: set[Ticker] = set()
    source = _fetcher(calls, short=True, filled=True)

    def fetcher(
        ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
    ) -> PriceSeries | None:
        if ticker in gone:
            calls.append(fetch_symbol)
            return None
        return source(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)

    refusal = f"{_HEAD.format(2, 2, 5, 6)} {SHORT}"
    assert _refusal(bundle, tmp_path, cache, fetch=True, fetcher=fetcher) == refusal
    if reviewed:
        _review(cache / "XQSE.csv")
    assert _refusal(bundle, tmp_path, cache, fetch=fetch, fetcher=fetcher) == refusal
    assert len(calls) == 2
    return SimpleNamespace(
        bundle=bundle, cache=cache, records=records, calls=calls, gone=gone, fetcher=fetcher
    )


def _follow_fallback(case: SimpleNamespace, tmp_path: Path):
    """Follow the text for a filer who can't tell which holding is short
    (#1530): move every .csv file directly in the price cache directory that
    has no .csv.lock beside it aside, re-run with price fetching, move back
    each moved file the re-run didn't recreate, and re-run once more if it
    stopped. Return that re-run's stop, if any, and the last run's prices and
    equity audit."""
    aside = tmp_path / "aside"
    aside.mkdir()
    for path in case.cache.glob("*.csv"):
        if not (case.cache / f"{path.name}.lock").exists():
            path.rename(aside / path.name)
    stop = None
    try:
        result = _fa_prices(case.bundle, tmp_path, case.cache, fetch=True, fetcher=case.fetcher)
    except core.RobinhoodPipelineError as exc:
        stop = str(exc)
    for path in aside.iterdir():
        if not (case.cache / path.name).exists():
            path.rename(case.cache / path.name)
    if stop is not None:
        result = _fa_prices(case.bundle, tmp_path, case.cache, fetch=True, fetcher=case.fetcher)
    prices, audit, _options, _quantities = result
    return stop, prices, audit


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_short_series_beside_another_clears_once_every_unlocked_file_is_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1530: following the text without knowing which holding is short
    fetches both series afresh and gets past the stop; the records in the
    price cache directory's subfolders stay where they are."""
    case = _short_beside_complete(tmp_path, monkeypatch, fetch=fetch)

    stop, _prices, audit = _follow_fallback(case, tmp_path)

    assert stop is None
    assert len(case.calls) == 4
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 6, 6)
    assert {path: path.read_bytes() for path in case.records} == case.records


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_fallback_leaves_a_locked_cache_and_its_reviewed_closes_are_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1530: a lock pins reviewed closes, so the text leaves a locked cache
    and its lock in place: only the unlocked series is fetched afresh, and
    the run uses the reviewed closes, not the source's."""
    case = _short_beside_complete(tmp_path, monkeypatch, fetch=fetch, reviewed=True)
    locked = {path: path.read_bytes() for path in case.cache.glob("XQSE.csv*")}
    assert len(locked) == 2

    stop, prices, audit = _follow_fallback(case, tmp_path)

    assert stop is None
    assert len(case.calls) == 3
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 6, 6)
    assert set(prices[SECOND.instrument.symbol].closes.values()) == {Decimal("20")}
    assert {path: path.read_bytes() for path in locked} == locked
    assert {path: path.read_bytes() for path in case.records} == case.records


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_fallback_moves_back_a_series_the_source_no_longer_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1530: the source no longer returns SECOND's complete series. The re-run
    with fetching writes TEST's fresh series and stops on the missing one;
    moving back the file it didn't recreate and re-running once more, as the
    text says, gets past the stop."""
    case = _short_beside_complete(tmp_path, monkeypatch, fetch=fetch)
    case.gone.add(SECOND.instrument.symbol)

    stop, _prices, audit = _follow_fallback(case, tmp_path)

    assert stop == f"{_HEAD.format(1, 2, 3, 6)} {ONLINE_MISSING}"
    assert [path.name for path in (tmp_path / "aside").iterdir()] == ["TEST.csv"]
    assert len(case.calls) == 4
    assert (audit.errors, audit.covered_sessions, audit.required_sessions) == ((), 6, 6)
    assert {path: path.read_bytes() for path in case.records} == case.records


def test_missing_and_short_series_name_both_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    required = {
        Ticker("TEST"): frozenset({date(2025, 1, 2), date(2025, 1, 3)}),
        Ticker("XQTW"): frozenset({date(2025, 1, 2)}),
    }
    monkeypatch.setattr(
        equity_prices, "robinhood_required_equity_sessions", lambda *_args, **_kwargs: required
    )
    monkeypatch.setattr(
        equity_prices, "load_price_series", lambda *_args, **_kwargs: {Ticker("TEST"): _series()}
    )
    bundle = _bundle()
    _prices, audit = equity_prices.load_robinhood_equity_prices(
        bundle,
        FinancialYear(2025),
        tmp_path,
        fetch_prices=False,
        price_cache_dir=None,
        price_fetcher=None,
        quantities=bundle.quantities,
    )
    assert audit.errors == (f"{_HEAD.format(1, 2, 1, 3)} {OFFLINE_MISSING} {SHORT}",)


def test_every_flag_and_setting_the_refusals_name_is_accepted(tmp_path: Path) -> None:
    """Only robinhood-fa and robinhood-all price Schedule FA; both take every
    flag the equity and option refusals name, and user.toml loads every
    setting they name."""
    text = " ".join(
        (
            OFFLINE_MISSING,
            ONLINE_MISSING,
            SHORT,
            OPTION_NOT_CACHED,
            OPTION_NO_KEY,
            OPTION_SHORT,
        )
    )
    flags = set(re.findall(r"--[a-z][a-z-]*", text))
    assert flags == {"--no-fetch-prices", "--price-cache-dir"}
    commands = _commands(_build_parser())
    for command in ("robinhood-fa", "robinhood-all"):
        assert flags <= commands[command]._option_string_actions.keys()

    assert set(re.findall(r"\[\[?[\w.]+\]\]?", text)) == {"[[prices.ticker_overrides]]", "[prices]"}
    assert "[prices] massive_api_key_file" in text
    config = tmp_path / "user.toml"
    config.write_text(
        '[account]\nholder_name = "Tax Payer"\n\n[prices]\nmassive_api_key_file = "env"\n'
        "\n[[prices.ticker_overrides]]\n"
        f'ibkr_ticker = "TEST"\nisin = "{SECURITY.instrument.isin}"\nyahoo_symbol = "XQOV"\n',
        encoding="utf-8",
    )
    loaded = load_user_config(config).prices
    assert loaded.massive_api_key_file == "env"
    assert [(item.ibkr_ticker, item.yahoo_symbol) for item in loaded.ticker_overrides] == [
        ("TEST", "XQOV")
    ]
