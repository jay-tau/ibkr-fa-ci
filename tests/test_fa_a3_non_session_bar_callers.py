"""Every Schedule FA Table A3 caller refuses a bar on a US non-session (#895).

Each command runs on its synthetic test bundle with prices that gain one bar on
Saturday 2025-12-27 or on Christmas 2025-12-25, a listed closure, inside a held
window. The run refuses with :class:`NonSessionPriceBarError` (exit 4 from the
CLI) and leaves no output: an earlier run's outputs are removed and nothing new
is written. The refusal prints neither the symbol nor the bar date (#1047), and
gives the route: move the cache file with that bar aside and re-run with price
fetching, else enter the Schedule FA by hand (#1552). NetBenefits lots are
bought on December 31, so their series ends on the bar, the close carried into
December 31; netbenefits-fa refuses that close first, with
:class:`ClosingPriceError`, as no close for the required US market session
(#1165). fidelity-cma-fa refuses the bar earlier, while it prices Table A2 for
the whole year (#1135).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli
from ibkr_fa import pipeline_robinhood as robinhood_pipeline
from ibkr_fa import pipeline_robinhood_outputs as robinhood_outputs
from ibkr_fa.compute.dec31_close import ClosingPriceError
from ibkr_fa.compute.fa_a3_peak_value import NonSessionPriceBarError
from ibkr_fa.compute.fidelity_cma_fa import FidelityCmaFaError
from ibkr_fa.compute.robinhood_fa import compute_robinhood_fa
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_fidelity_cma_fa import run_fidelity_cma_fa
from ibkr_fa.pipeline_netbenefits_fa import run_netbenefits_fa
from ibkr_fa.pipeline_robinhood import run_robinhood_fa
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from ibkr_fa.prices.cache import write_cache_lock
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.reference.us_market_calendar import (
    is_us_market_session,
    us_market_session_on_or_before,
    us_market_sessions,
)
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_prints_no_marker
from tests._dec31_close import assert_markers_absent
from tests._fidelity_cma_bundle import (
    cma_inputs,  # noqa: F401 - fixture
    spaxx_nav,
)
from tests._fidelity_cma_fa_inputs import NON_SESSION_CLOSE
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher
from tests._ibkr_standalone_commands import write_standalone_inputs as _write_inputs
from tests._netbenefits_e2e import nb_e2e_bundle
from tests._netbenefits_fa import NB_MARKET_SESSION_REFUSAL
from tests._output_removal_failures import CLEANUP_LAYERS as _LAYERS
from tests._robinhood_cli import FINANCIAL_YEAR
from tests._robinhood_fa_inputs import (
    FlatFx,
    fa_config,
    fa_entity,
    fa_income,
    fa_options,
    fa_paired,
    fa_price_series,
)
from tests._robinhood_pipeline_stubs import json_fa, stub_successful_all
from tests._robinhood_security import configured_securities
from tests._stockplan_fa_exports import setup_stockplan_exports, stub_fetcher

_SATURDAY = date(2025, 12, 27)
_CHRISTMAS = date(2025, 12, 25)  # a Thursday, a listed US market closure
_BAR_DAYS = {"weekend": _SATURDAY, "listed-closure": _CHRISTMAS}
_NOW = datetime(2026, 6, 1, tzinfo=UTC)
# The refusal names the kind of day only: no symbol and no date (#1047). Its
# route finds the holding's cache file by that kind of day (#1552).
_REFUSALS = {
    _SATURDAY: (
        "a holding's price series has a bar dated on a weekend day; the price source and the "
        "US market calendar disagree, so the Schedule FA Table A3 peak value is refused. Move "
        "each .csv file directly in the price cache directory (--price-cache-dir; by default a "
        ".ibkr-fa-price-cache folder that the command's --help locates) with a row dated on a "
        "Saturday or Sunday, and any .lock beside it, out of that directory, then re-run with "
        "price fetching on (without --no-fetch-prices): a fresh fetch can return the series "
        "without that bar. If the fresh series still has it, enter this command's Schedule FA "
        "by hand."
    ),
    _CHRISTMAS: (
        "a holding's price series has a bar dated on a listed US market closure; the price "
        "source and the US market calendar disagree, so the Schedule FA Table A3 peak value is "
        "refused. Move each .csv file directly in the price cache directory "
        "(--price-cache-dir; by default a .ibkr-fa-price-cache folder that the command's "
        "--help locates) with a row dated on a weekday the US market was closed, and any .lock "
        "beside it, out of that directory, then re-run with price fetching on (without "
        "--no-fetch-prices): a fresh fetch can return the series without that bar. If the "
        "fresh series still has it, enter this command's Schedule FA by hand."
    ),
}


def _markers(day: date) -> tuple[str, str]:
    """``day``'s ``str`` and ``repr`` forms, for the #1331 absent checks."""
    return day.isoformat(), f"{day.year}, {day.month}, {day.day}"


def _with_bar(
    fetcher: DailyCloseFetcher, bar_day: date, symbols: list[str] | None = None
) -> DailyCloseFetcher:
    """``fetcher``'s series plus a bar on ``bar_day`` at the close of the
    session before it; each symbol it prices is added to ``symbols`` when
    supplied."""

    def fetch(
        ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
    ) -> PriceSeries:
        if symbols is not None:
            symbols.append(ticker)
        series = fetcher(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
        assert series is not None
        before = series.closes[us_market_session_on_or_before(bar_day)]
        return replace(series, closes={**series.closes, bar_day: before})

    return fetch


def _ending_on(bar_day: date) -> DailyCloseFetcher:
    """Sessions through the one before ``bar_day``, then the ``bar_day`` bar
    and nothing after, so its close is carried to December 31."""

    def fetch(
        ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol: str | None = None
    ) -> PriceSeries:
        del end, fetch_symbol
        sessions = (*us_market_sessions(start, us_market_session_on_or_before(bar_day)), bar_day)
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=dict.fromkeys(sessions, Decimal("100")),
            has_split_in_range=False,
            source="synthetic",
            fetched_at=now,
        )

    return fetch


@dataclass(frozen=True)
class _Run:
    argv: list[str]
    entry: Callable[[], object]
    out: Path  # holds the run's outputs and nothing else
    outputs: tuple[str, ...]
    symbols: list[str] = field(default_factory=list)  # the symbols the run priced


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def _schedule_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _: Any, bar_day: date = _SATURDAY
) -> _Run:
    statement, config = _write_inputs(tmp_path, "schedule-fa")
    symbols: list[str] = []
    run = partial(
        run_fa_a3,
        price_fetcher=_with_bar(flat_close_fetcher(), bar_day, symbols),
        price_now=_NOW,
    )
    monkeypatch.setattr(cli, "run_fa_a3", run)
    output = _out(tmp_path) / "schedule-fa.csv"
    return _Run(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025"),
        ],
        partial(
            run,
            input_path=statement,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            fetch_prices=True,
        ),
        output.parent,
        ("schedule-fa.csv", "schedule-fa-a2.csv"),
        symbols,
    )


def _stockplan_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _: Any, bar_day: date = _SATURDAY
) -> _Run:
    input_dir, config, _ = setup_stockplan_exports(tmp_path)
    symbols: list[str] = []
    run = partial(
        run_stockplan_fa,
        price_fetcher=_with_bar(stub_fetcher, bar_day, symbols),
        price_now=_NOW,
    )
    monkeypatch.setattr(cli, "run_stockplan_fa", run)
    output = _out(tmp_path) / "schedule-fa.csv"
    return _Run(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025"),
        ],
        partial(
            run,
            input_dir=input_dir,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            fetch_prices=True,
        ),
        output.parent,
        ("schedule-fa.csv",),
        symbols,
    )


def _netbenefits_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _: Any, bar_day: date = _SATURDAY
) -> _Run:
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    run = partial(run_netbenefits_fa, price_fetcher=_ending_on(bar_day), price_now=_NOW)
    monkeypatch.setattr(cli, "run_netbenefits_fa", run)
    output = _out(tmp_path) / "schedule-fa-a3.csv"
    return _Run(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025"),
        ],
        partial(
            run,
            input_dir=bundle,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            fetch_prices=True,
        ),
        output.parent,
        ("schedule-fa-a3.csv", "schedule-fa-a2.csv"),
    )


def _fidelity_cma_fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    bar_day: date = _SATURDAY,
) -> _Run:
    statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
    run = partial(
        run_fidelity_cma_fa,
        price_fetcher=_with_bar(spaxx_nav, bar_day),
        price_now=_NOW,
    )
    monkeypatch.setattr(cli, "run_fidelity_cma_fa", run)
    output = _out(tmp_path) / "schedule-fa.csv"
    price_cache = tmp_path / "price-cache"
    return _Run(
        [
            "fidelity-cma-fa",
            *("--input", str(statements), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025", "--price-cache-dir", str(price_cache)),
        ],
        partial(
            run,
            input_dir=statements,
            config_path=config,
            output_path=output,
            calendar_year=2025,
            price_cache_dir=price_cache,
        ),
        output.parent,
        ("schedule-fa.csv", "schedule-fa-a2.csv"),
    )


def _robinhood_fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _: Any, bar_day: date = _SATURDAY
) -> _Run:
    """Every other leg is stubbed; the FA leg runs the real Robinhood A3 compute."""
    stub_successful_all(monkeypatch, json_fa())
    paired, accounts = fa_paired(sold=True, dividend=True)
    ((symbol, series),) = fa_price_series().items()
    prices = {symbol: replace(series, closes={**series.closes, bar_day: Decimal("15")})}
    config = fa_config(accounts)

    def compute_fa(*_args: object, **_kwargs: object) -> object:
        return compute_robinhood_fa(
            paired,
            config=config,
            securities=configured_securities(config),
            income=fa_income(dividend=True),
            options=fa_options(),
            calendar_year=2025,
            fx_policy=FlatFx(),
            prices_by_ticker=prices,
            entity_overrides=fa_entity(),
        )

    monkeypatch.setattr(robinhood_pipeline, "_compute_fa", compute_fa)
    out = _out(tmp_path)
    out.chmod(0o700)  # Robinhood refuses an output directory others can read
    return _Run(
        [
            "robinhood-fa",
            *("--input", str(tmp_path / "bundle"), "--config", str(tmp_path / "user.toml")),
            *("--output-dir", str(out), "--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ],
        partial(
            run_robinhood_fa,
            input_dir=tmp_path / "bundle",
            config_path=tmp_path / "user.toml",
            output_dir=out,
            financial_year=FINANCIAL_YEAR,
            itr_form="ITR-2",
        ),
        out,
        robinhood_outputs.FA_FILENAMES,
        [symbol],
    )


_COMMANDS = {
    "schedule-fa": _schedule_fa,
    "stockplan-fa": _stockplan_fa,
    "netbenefits-fa": _netbenefits_fa,
    "robinhood-fa": _robinhood_fa,
}


@pytest.mark.parametrize("bar", _BAR_DAYS)
@pytest.mark.parametrize("layer", _LAYERS)
@pytest.mark.parametrize("name", _COMMANDS)
def test_non_session_bar_refuses_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    layer: str,
    bar: str,
) -> None:
    bar_day = _BAR_DAYS[bar]
    run = _COMMANDS[name](tmp_path, monkeypatch, request, bar_day)
    for output in run.outputs:
        (run.out / output).write_bytes(b"STALE")
    dec31_close = name == "netbenefits-fa"

    if layer == "cli":
        assert cli.main(run.argv) == 4
        err = capsys.readouterr().err
        refusal = NB_MARKET_SESSION_REFUSAL if dec31_close else _REFUSALS[bar_day]
        assert err.splitlines()[0] == f"error: {refusal}"
        assert bar_day.isoformat() not in err
    elif dec31_close:
        with pytest.raises(ClosingPriceError) as closing:
            run.entry()
        assert str(closing.value) == NB_MARKET_SESSION_REFUSAL
        # XESP is the NetBenefits bundle's symbol.
        assert_markers_absent(closing.value, "XESP", bar_day.isoformat())
    else:
        with pytest.raises(NonSessionPriceBarError) as exc:
            run.entry()
        assert str(exc.value) == _REFUSALS[bar_day]
        assert run.symbols
        assert_prints_no_marker(exc.value, markers=(*run.symbols, *_markers(bar_day)))
        assert exc.value == NonSessionPriceBarError(weekend=bar_day == _SATURDAY)

    assert list(run.out.iterdir()) == []


@pytest.mark.parametrize("bar", _BAR_DAYS)
@pytest.mark.parametrize("layer", _LAYERS)
def test_fidelity_cma_fa_refuses_the_bar_in_table_a2_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    layer: str,
    bar: str,
) -> None:
    run = _fidelity_cma_fa(tmp_path, monkeypatch, request, _BAR_DAYS[bar])
    for output in run.outputs:
        (run.out / output).write_bytes(b"STALE")

    if layer == "cli":
        assert cli.main(run.argv) == 4
        assert capsys.readouterr().err == f"error: {NON_SESSION_CLOSE}\n"
    else:
        with pytest.raises(FidelityCmaFaError) as exc:
            run.entry()
        assert str(exc.value) == NON_SESSION_CLOSE

    assert list(run.out.iterdir()) == []


# The commands whose Table A3 reads its series through the price cache, each
# with its fetcher double, which has no non-session bar.
_CACHED: dict[str, tuple[str, Callable[..., object], DailyCloseFetcher]] = {
    "schedule-fa": ("run_fa_a3", run_fa_a3, flat_close_fetcher()),
    "stockplan-fa": ("run_stockplan_fa", run_stockplan_fa, stub_fetcher),
}


def _files_the_route_names(cache: Path, *, weekend: bool) -> list[Path]:
    """Each .csv file directly in ``cache`` with a row dated on a Saturday or
    Sunday (``weekend``) or on a weekday the US market was closed."""

    def named(line: str) -> bool:
        try:
            day = date.fromisoformat(line.split(",", 1)[0])
        except ValueError:  # a comment, metadata or the header
            return False
        return (
            day.weekday() >= 5 if weekend else day.weekday() < 5 and not is_us_market_session(day)
        )

    return [
        path
        for path in sorted(cache.glob("*.csv"))
        if any(named(line) for line in path.read_text(encoding="utf-8").splitlines())
    ]


@pytest.mark.parametrize("locked", [False, True], ids=["cache", "locked-cache"])
@pytest.mark.parametrize("bar", _BAR_DAYS)
@pytest.mark.parametrize("name", _CACHED)
def test_moving_the_cache_file_with_the_bar_aside_as_the_refusal_says(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
    name: str,
    bar: str,
    locked: bool,
) -> None:
    """#1552: the source first returns the bar. That run caches the series,
    locked or not, and refuses; a re-run reuses the cache and refuses again,
    though the source no longer returns the bar. After moving each cache file
    the refusal describes, and its lock, out of the price cache directory: a
    source that still returns the bar refuses with the same text, so the
    by-hand route applies; one that doesn't writes the outputs of a run whose
    source never had the bar."""
    bar_day = _BAR_DAYS[bar]
    attr, entry, clean = _CACHED[name]
    run = _COMMANDS[name](tmp_path, monkeypatch, request, bar_day)
    source = {"fetcher": clean}

    def fetch(ticker: Ticker, **kwargs: Any) -> PriceSeries | None:
        return source["fetcher"](ticker, **kwargs)

    monkeypatch.setattr(cli, attr, partial(entry, price_fetcher=fetch, price_now=_NOW))
    cache = tmp_path / "price-cache"

    def rerun(fetcher: DailyCloseFetcher, cache_dir: Path = cache) -> tuple[int, str]:
        source["fetcher"] = fetcher
        code = cli.main([*run.argv, "--price-cache-dir", str(cache_dir)])
        return code, capsys.readouterr().err.partition("\n")[0]

    def lock_and_move_aside(attempt: int) -> None:
        files = _files_the_route_names(cache, weekend=bar_day == _SATURDAY)
        assert files
        aside = tmp_path / f"aside-{attempt}"
        aside.mkdir()
        for path in files:
            moved = [path]
            if locked:
                moved.append(write_cache_lock(path))
            for item in moved:
                item.rename(aside / item.name)

    control_code, _ = rerun(clean, tmp_path / "control-cache")
    assert control_code != 4
    control = {output: (run.out / output).read_bytes() for output in run.outputs}
    refused = (4, f"error: {_REFUSALS[bar_day]}")

    assert rerun(_with_bar(clean, bar_day)) == refused
    if locked:
        for path in cache.glob("*.csv"):
            write_cache_lock(path)
    assert rerun(clean) == refused
    lock_and_move_aside(1)
    assert rerun(_with_bar(clean, bar_day)) == refused
    lock_and_move_aside(2)
    assert rerun(clean)[0] == control_code
    assert {output: (run.out / output).read_bytes() for output in run.outputs} == control
