"""Tests for optional Robinhood Massive close-audit orchestration."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_a2_peak import robinhood_required_option_sessions
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionContract,
    RobinhoodOptionsResult,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood import RobinhoodPipelineError
from ibkr_fa.pipeline_robinhood_models import RobinhoodEquityPriceAudit
from ibkr_fa.pipeline_robinhood_option_prices import (
    RobinhoodOptionPriceAudit,
    collect_robinhood_option_price_audit,
)
from ibkr_fa.prices import massive_options
from ibkr_fa.prices.massive_options import MassiveOptionPriceError
from ibkr_fa.prices.series import OptionCloseBar
from tests._massive_option_replies import (
    assert_mr_private,
    mr_bars,
    mr_overview,
)
from tests._robinhood_option_audit import OPTION_NO_KEY as NO_KEY
from tests._robinhood_option_audit import OPTION_NOT_CACHED as NOT_CACHED
from tests._robinhood_option_audit import OPTION_SHORT as SHORT
from tests._robinhood_option_audit import held_call_options as _options
from tests._robinhood_option_audit import massive_key_config as _config
from tests._robinhood_option_audit import seed_records, serve_massive
from tests._robinhood_option_series import option_series
from tests._robinhood_refusals import MASSIVE_COUNT_REFUSAL as _COUNT_REFUSAL


@pytest.mark.parametrize("relative", [False, True])
def test_complete_audit_reports_cache_without_changing_filing_state(
    tmp_path: Path,
    monkeypatch,
    relative: bool,
) -> None:
    """The cache is named as the user typed it, never resolved to an absolute
    path holding their home folder (#1044)."""
    options, contract = _options()
    series = option_series(
        "O:XQAQ250117C00250000",
        "XQAQ",
        contract,
        {
            date(2025, 1, 15): OptionCloseBar(
                date(2025, 1, 15),
                Decimal("1.25"),
                Decimal("2"),
                1,
            ),
            date(2025, 1, 16): OptionCloseBar(
                date(2025, 1, 16),
                Decimal("1.50"),
                Decimal("3"),
                2,
            ),
        },
        artifact_path=tmp_path / "cache.json",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: series,
    )
    typed = Path("prices") if relative else tmp_path / "prices"
    monkeypatch.chdir(tmp_path)
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=typed,
    )
    assert "complete (1 contracts, 2/2 held sessions" in audit.warnings[0]
    assert audit.warnings[0].endswith(f"; private cache {typed / 'massive-options'}")
    if relative:
        assert str(tmp_path) not in audit.warnings[0]


def test_missing_bar_and_provider_error_block_a2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    options, contract = _options()
    incomplete = option_series(
        "O:XQAQ250117C00250000",
        "XQAQ",
        contract,
        {
            date(2025, 1, 15): OptionCloseBar(
                date(2025, 1, 15),
                Decimal("1.25"),
                Decimal("2"),
                1,
            )
        },
        artifact_path=tmp_path / "cache.json",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: incomplete,
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert audit.errors == (
        f"Massive option close coverage is incomplete (1/2 held sessions). {SHORT}",
    )

    def fail(**_kwargs):
        raise MassiveOptionPriceError("synthetic failure")

    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        fail,
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert audit.errors == ("synthetic failure",)


def test_no_fetch_prices_reads_cache_only(tmp_path: Path, monkeypatch) -> None:
    options, _contract = _options()
    calls = []

    def load(**kwargs):
        calls.append(kwargs["fetch"])

    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        load,
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=False,
        price_cache_dir=None,
    )
    assert calls == [False]
    assert audit.errors == (NOT_CACHED,)


def test_carried_option_fetches_prior_session_close(tmp_path: Path, monkeypatch) -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 12, 31),
        right="Call",
        strike_usd=Decimal("250"),
    )
    options = cast(
        "RobinhoodOptionsResult",
        SimpleNamespace(
            trades=(
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2024, 12, 30),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                ),
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2025, 1, 2),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                ),
            ),
            valuation_anchors=(),
            validated_valuation_anchors=(),
        ),
    )
    calls = []

    def load(**kwargs):
        calls.append(kwargs)
        return option_series(
            "O:XQAQ251231C00250000",
            contract.symbol,
            contract,
            {
                date(2024, 12, 31): OptionCloseBar(
                    date(2024, 12, 31),
                    Decimal("1"),
                    Decimal("1"),
                    1,
                )
            },
            artifact_path=tmp_path / "cache.json",
            fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        load,
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert calls[0]["start"] == date(2024, 12, 31)
    assert audit.errors == ()
    assert "1/1 held sessions" in audit.warnings[0]


def test_contract_with_no_calendar_year_sessions_is_not_fetched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2024, 11, 15),
        right="Call",
        strike_usd=Decimal("250"),
    )
    options = cast(
        "RobinhoodOptionsResult",
        SimpleNamespace(
            trades=(
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2024, 10, 1),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                ),
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2024, 10, 8),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                ),
            ),
            valuation_anchors=(),
            validated_valuation_anchors=(),
        ),
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("loader called")),
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert audit.errors == ()
    assert audit.series_by_contract == {}


def test_fy_spanning_lot_is_clamped_to_calendar_year(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2026, 1, 16),
        right="Call",
        strike_usd=Decimal("250"),
    )
    options = cast(
        "RobinhoodOptionsResult",
        SimpleNamespace(
            trades=(
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2025, 12, 29),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                ),
                SimpleNamespace(
                    contract=contract,
                    activity_date=date(2026, 2, 10),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                ),
            ),
            lot_slices=(
                SimpleNamespace(
                    contract=contract,
                    open_date=date(2025, 12, 29),
                    close_date=date(2026, 2, 10),
                ),
            ),
            valuation_anchors=(),
            validated_valuation_anchors=(),
        ),
    )
    bars = {
        day: OptionCloseBar(day, Decimal("1"), Decimal("1"), 1)
        for day in (date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31))
    }
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: option_series(
            "O:XQAQ260116C00250000",
            "XQAQ",
            contract,
            bars,
            artifact_path=tmp_path / "cache.json",
            fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
        ),
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert "3/3 held sessions" in audit.warnings[0]


def test_out_of_calendar_year_contract_is_not_fetched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A contract opened and closed in 2026 reaches the session calculation
    through its trades, which leaves it out of the 2025 replay, so no close is
    fetched for it (#1694)."""
    options, expected_contract = _options()
    out_of_year = RobinhoodOptionContract(
        account_index=0,
        symbol="XQCA",
        expiration=date(2026, 3, 20),
        right="Call",
        strike_usd=Decimal("300"),
    )
    options = cast(
        "RobinhoodOptionsResult",
        SimpleNamespace(
            trades=(
                *options.trades,
                SimpleNamespace(
                    contract=out_of_year,
                    activity_date=date(2026, 1, 5),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                ),
                SimpleNamespace(
                    contract=out_of_year,
                    activity_date=date(2026, 2, 10),
                    quantity=Decimal("1"),
                    kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                ),
            ),
            lot_slices=(
                *options.lot_slices,
                SimpleNamespace(
                    contract=out_of_year,
                    open_date=date(2026, 1, 5),
                    close_date=date(2026, 2, 10),
                ),
            ),
            valuation_anchors=(),
            validated_valuation_anchors=(),
        ),
    )
    sessions_read: list[tuple[frozenset[RobinhoodOptionContract], ...]] = []

    def required_sessions(options, *, calendar_year):
        required = robinhood_required_option_sessions(options, calendar_year=calendar_year)
        sessions_read.append(
            (frozenset(trade.contract for trade in options.trades), frozenset(required))
        )
        return required

    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.robinhood_required_option_sessions",
        required_sessions,
    )
    calls: list[RobinhoodOptionContract] = []

    def load(**kwargs):
        contract = RobinhoodOptionContract(
            account_index=0,
            symbol=kwargs["underlying"],
            expiration=kwargs["expiration"],
            right=kwargs["right"],
            strike_usd=kwargs["strike_usd"],
            multiplier=kwargs["multiplier"],
        )
        calls.append(contract)
        bars = {
            day: OptionCloseBar(day, Decimal("1"), Decimal("1"), 1)
            for day in (date(2025, 1, 15), date(2025, 1, 16))
        }
        return option_series(
            "O:XQAQ250117C00250000",
            contract.symbol,
            contract,
            bars,
            artifact_path=tmp_path / "cache.json",
            fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        load,
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=None,
    )
    assert sessions_read == [
        (frozenset({expected_contract, out_of_year}), frozenset({expected_contract}))
    ]
    assert calls == [expected_contract]
    assert "1 contracts" in audit.warnings[0]


def test_unsupported_year_refuses_before_fetching(tmp_path: Path, monkeypatch) -> None:
    options, _contract = _options()
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("loader called")),
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2026,
        fetch=True,
        price_cache_dir=None,
    )
    assert audit.errors == ("Robinhood daily A2 pricing does not support calendar year 2026",)
    assert audit.series_by_contract == {}
    assert audit.warnings == ()


def test_unsupported_year_without_option_evidence_is_empty(tmp_path: Path, monkeypatch) -> None:
    options = cast(
        "RobinhoodOptionsResult",
        SimpleNamespace(trades=(), valuation_anchors=(), validated_valuation_anchors=()),
    )
    monkeypatch.setattr(
        "ibkr_fa.pipeline_robinhood_option_prices.load_or_fetch_massive_option_closes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("loader called")),
    )
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2026,
        fetch=True,
        price_cache_dir=None,
    )
    assert audit == RobinhoodOptionPriceAudit()


def _no_guidance(_text: str) -> Path:
    raise AssertionError("no identity guidance is written")


@pytest.mark.parametrize("path", ["fetch", "cache"])
def test_real_reader_refusal_reaches_the_audit_and_fa_gate_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The audit records the reader's full located text, and FA refuses with
    exactly that text with F&O warning decoration explicitly disabled."""
    options, _contract = _options("XQMR")
    artifact = serve_massive(monkeypatch, tmp_path, path, mr_overview(), mr_bars(n="1.5"))
    before = None if artifact is None else artifact.read_bytes()
    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=path == "fetch",
        price_cache_dir=tmp_path / "prices",
    )
    assert audit.errors == (_COUNT_REFUSAL,)
    assert audit.series_by_contract == {}

    if artifact is None:
        serve_massive(monkeypatch, tmp_path, path, mr_overview(), mr_bars(n="1.5"))
    monkeypatch.setattr(
        core, "resolved_robinhood_quantities", lambda _bundle: SimpleNamespace(blockers=())
    )
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        core.fa_prices(
            cast("Any", SimpleNamespace(config=_config())),
            FinancialYear(2025),
            cast("Any", options),
            tmp_path,
            tmp_path / "user.toml",
            fetch_prices=path == "fetch",
            price_cache_dir=tmp_path / "prices",
            price_fetcher=None,
            write_guidance=_no_guidance,
        )
    assert str(raised.value) == _COUNT_REFUSAL
    assert_mr_private(raised.value)
    if artifact is None:
        assert not (tmp_path / "prices").exists()
    else:
        assert artifact.read_bytes() == before


def _past_option_stop(
    tmp_path: Path,
    cache: Path,
    *,
    fetch: bool,
    key_file: str | None = "env",
    options: object | None = None,
) -> RobinhoodOptionPriceAudit:
    """``fa_prices`` for the XQMR round trip, or for ``options``; the equity
    audit is stubbed clean."""
    _prices, _equity, audit, _quantities = core.fa_prices(
        cast("Any", SimpleNamespace(config=_config(key_file))),
        FinancialYear(2025),
        cast("Any", options or _options("XQMR")[0]),
        tmp_path,
        tmp_path / "user.toml",
        fetch_prices=fetch,
        price_cache_dir=cache,
        price_fetcher=None,
        write_guidance=_no_guidance,
    )
    return audit


def _stub_quantities_and_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "resolved_robinhood_quantities", lambda _bundle: SimpleNamespace(blockers=())
    )
    monkeypatch.setattr(
        core, "price_series", lambda *_args, **_kwargs: ({}, RobinhoodEquityPriceAudit())
    )


@pytest.mark.parametrize("follow", ["fetch", "cache"])
def test_offline_refusal_with_no_cache_names_the_fix_that_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, follow: str
) -> None:
    """#1523: with fetching off and no cache, the refusal names a re-run with
    fetching (with the key file it needs) or a cache directory that holds the
    closes; either one gets past the option stop."""
    _stub_quantities_and_equity(monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        _past_option_stop(tmp_path, empty, fetch=False)
    assert str(raised.value) == NOT_CACHED

    serve_massive(monkeypatch, tmp_path, follow, mr_overview(), mr_bars())
    if follow == "fetch":
        audit = _past_option_stop(tmp_path, empty, fetch=True)
    else:
        audit = _past_option_stop(tmp_path, tmp_path / "prices", fetch=False)
    assert audit.errors == ()
    assert "2/2 held sessions" in audit.warnings[0]


def test_short_cached_series_refuses_the_same_on_a_rerun_with_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1523: a cached series with no 16 January close is read before any
    fetch, so a re-run with fetching on returns the same closes."""
    bars = json.loads(mr_bars())
    bars["results"] = bars["results"][:1]
    serve_massive(monkeypatch, tmp_path, "cache", mr_overview(), json.dumps(bars).encode())
    monkeypatch.setattr(
        massive_options,
        "_default_url_opener",
        lambda _request, _timeout: (_ for _ in ()).throw(AssertionError("fetched")),
    )
    refusal = f"Massive option close coverage is incomplete (1/2 held sessions). {SHORT}"
    for fetch in (False, True):
        audit = collect_robinhood_option_price_audit(
            options=_options("XQMR")[0],
            config=_config(),
            config_path=tmp_path / "user.toml",
            input_dir=tmp_path,
            calendar_year=2025,
            fetch=fetch,
            price_cache_dir=tmp_path / "prices",
        )
        assert audit.errors == (refusal,)


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_short_cached_series_clears_once_its_file_is_moved_aside_and_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1528: Massive filled the 16 January bar after the series was cached.
    The text names moving the contract's cached file out of the massive-options
    folder and re-running with price fetching, which fetches every close."""
    _stub_quantities_and_equity(monkeypatch)
    bars = json.loads(mr_bars())
    bars["results"] = bars["results"][:1]
    artifact = serve_massive(
        monkeypatch, tmp_path, "cache", mr_overview(), json.dumps(bars).encode()
    )
    assert artifact is not None
    cache = tmp_path / "prices"
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        _past_option_stop(tmp_path, cache, fetch=fetch)
    assert str(raised.value) == (
        f"Massive option close coverage is incomplete (1/2 held sessions). {SHORT}"
    )

    artifact.rename(tmp_path / artifact.name)
    serve_massive(monkeypatch, tmp_path, "fetch", mr_overview(), mr_bars())
    audit = _past_option_stop(tmp_path, cache, fetch=True)
    assert audit.errors == ()
    assert "2/2 held sessions" in audit.warnings[0]


_XQMR_260 = "O:XQMR250117C00260000"


def _struck_at_260(*, short: bool) -> tuple[bytes, bytes]:
    """The XQMR responses for the same call struck at 260; when ``short``,
    the bars have no 16 January close."""
    overview = json.loads(mr_overview())
    overview["results"].update(ticker=_XQMR_260, strike_price=260)
    bars = json.loads(mr_bars())
    bars["ticker"] = _XQMR_260
    if short:
        bars["results"] = bars["results"][:1]
    return json.dumps(overview).encode(), json.dumps(bars).encode()


@pytest.mark.parametrize("fetch", [True, False], ids=["fetching", "no-fetch-prices"])
def test_short_series_among_several_clears_once_every_contract_file_is_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    """#1530: two held XQMR calls, struck at 250 and 260, are cached; the 260
    call's series was cached before Massive filled its 16 January bar. The
    refusal names neither contract, so follow the text for a filer who can't
    tell which is short: move every .json file in the massive-options folder
    aside and re-run with price fetching. Every contract is fetched afresh,
    and nothing else in the price cache directory moves."""
    _stub_quantities_and_equity(monkeypatch)
    cache = tmp_path / "prices"
    records = seed_records(cache, "TEST.csv", "TEST.csv.lock")
    low, high = _options("XQMR")[0], _options("XQMR", "260")[0]
    options = SimpleNamespace(
        trades=(*low.trades, *high.trades),
        lot_slices=(*low.lot_slices, *high.lot_slices),
        valuation_anchors=(),
        validated_valuation_anchors=(),
    )
    serve_massive(
        monkeypatch, tmp_path, "fetch", mr_overview(), mr_bars(), *_struck_at_260(short=True)
    )
    refusal = f"Massive option close coverage is incomplete (3/4 held sessions). {SHORT}"
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        _past_option_stop(tmp_path, cache, fetch=True, options=options)
    assert str(raised.value) == refusal
    folder = cache / "massive-options"
    assert len(list(folder.glob("*.json"))) == 2
    monkeypatch.setattr(
        massive_options,
        "_default_url_opener",
        lambda _request, _timeout: (_ for _ in ()).throw(AssertionError("fetched")),
    )
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        _past_option_stop(tmp_path, cache, fetch=fetch, options=options)
    assert str(raised.value) == refusal

    aside = tmp_path / "aside"
    aside.mkdir()
    for path in folder.glob("*.json"):
        path.rename(aside / path.name)
    serve_massive(
        monkeypatch, tmp_path, "fetch", mr_overview(), mr_bars(), *_struck_at_260(short=False)
    )
    audit = _past_option_stop(tmp_path, cache, fetch=True, options=options)
    assert audit.errors == ()
    assert "complete (2 contracts, 4/4 held sessions" in audit.warnings[0]
    assert {path: path.read_bytes() for path in records} == records


@pytest.mark.parametrize("follow", ["key", "cache"])
def test_online_refusal_without_a_key_file_names_the_setting_and_following_it_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, follow: str
) -> None:
    """#1528: with fetching on, no cached closes and no [prices]
    massive_api_key_file, the refusal names that setting and the populated
    --price-cache-dir route, and sends no request; either route gets past the
    option stop."""
    _stub_quantities_and_equity(monkeypatch)
    monkeypatch.setattr(
        massive_options,
        "_default_url_opener",
        lambda _request, _timeout: (_ for _ in ()).throw(AssertionError("fetched")),
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RobinhoodPipelineError) as raised, core.fno_warning_on_error(False):
        _past_option_stop(tmp_path, empty, fetch=True, key_file=None)
    assert str(raised.value) == NO_KEY

    if follow == "key":
        serve_massive(monkeypatch, tmp_path, "fetch", mr_overview(), mr_bars())
        audit = _past_option_stop(tmp_path, empty, fetch=True, key_file="env")
    else:
        serve_massive(monkeypatch, tmp_path, "cache", mr_overview(), mr_bars())
        audit = _past_option_stop(tmp_path, tmp_path / "prices", fetch=True, key_file=None)
    assert audit.errors == ()
    assert "2/2 held sessions" in audit.warnings[0]
