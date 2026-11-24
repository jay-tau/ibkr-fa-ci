"""A price cache the reader refuses stops the NetBenefits and StockPlan
Schedule FA commands (#1170).

Each run puts a correctly hashed cache where the command's real FA pipeline
reads the held security's prices, offline and online: one whose table header
swaps ``close_usd`` and ``high_usd``, and one whose Dec-31 high is below its
close, under ``schedule_fa.peak_basis = "close"`` and ``"high"``. The real
reader refuses it: exit 4, the complete cache message, no new or stale FA
output, the price source never called, and the cache folder untouched. Neither
a marker inside the cache nor its symbol-built file name is printed. The
``*-all`` runs keep their failed-leg contract: every leg runs, the summary
marks the FA leg ERROR and the run removes every output.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from tests._crash_reports import assert_no_markers
from tests._netbenefits_e2e import nb_e2e_bundle
from tests._price_cache import (
    CACHE_MARKERS,
    HIGH_CACHE_MARKERS,
    high_below_close_message,
    unsupported_layout_message,
    write_high_below_close_cache,
    write_unsupported_layout_cache,
)
from tests._source_all import SUMMARY_FOOTER as _SUMMARY_FOOTER
from tests._source_commands import SOURCE_FY_ARGS
from tests._stockplan_fa_exports import setup_stockplan_exports


class _Cache(NamedTuple):
    """A refused cache kind: its writer, its complete message and its markers."""

    write: Callable[[Path, str], Path]
    message: Callable[[Path], str]
    markers: tuple[str, ...]


_LAYOUT = _Cache(write_unsupported_layout_cache, unsupported_layout_message, CACHE_MARKERS)
_HIGH_BELOW_CLOSE = _Cache(
    write_high_below_close_cache, high_below_close_message, HIGH_CACHE_MARKERS
)

# The cache kind and the schedule_fa.peak_basis added to the config (None: the default).
_CACHES = pytest.mark.parametrize(
    ("cache", "basis"),
    [
        pytest.param(_LAYOUT, None, id="unsupported-layout"),
        pytest.param(_HIGH_BELOW_CLOSE, "close", id="high-below-close-close-basis"),
        pytest.param(_HIGH_BELOW_CLOSE, "high", id="high-below-close-high-basis"),
    ],
)
_FETCH = pytest.mark.parametrize("fetch", [False, True], ids=["offline", "online"])
_LABEL = cli._RUN_ALL_STATUS_LABEL
_REMOVED = "(output removed because the run failed)"


def _entries(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in folder.iterdir()}


def _refused_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    *,
    config: Path,
    cache: _Cache,
    basis: str | None,
    ticker: str,
    stale: tuple[str, ...],
    fetch: bool,
) -> tuple[str, str, str]:
    """Run ``argv`` with a ``cache`` kind for ``ticker`` in ``tmp_path/cache``,
    ``basis`` set in ``config`` and each ``stale`` file in ``tmp_path/out``
    holding an earlier run's bytes.

    Checks exit 4, an empty ``out/``, an unchanged cache folder, a price source
    never called and no cache marker or file name printed; returns stdout,
    stderr and the cache message.
    """
    if basis is not None:
        text = config.read_text(encoding="utf-8")
        assert "[schedule_fa]" not in text
        config.write_text(f'{text}\n[schedule_fa]\npeak_basis = "{basis}"\n', encoding="utf-8")
    folder = tmp_path / "cache"
    path = cache.write(folder, ticker)
    before = _entries(folder)

    def fetch_prices(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("the price source was called")

    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetch_prices)
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        (out / name).write_text("stale\n", encoding="utf-8")

    rc = main([*argv, "--price-cache-dir", str(folder), *(() if fetch else ("--no-fetch-prices",))])

    assert rc == 4
    captured = capsys.readouterr()
    assert list(out.iterdir()) == []
    assert _entries(folder) == before
    assert_no_markers(captured.out + captured.err, *cache.markers, path.name)
    return captured.out, captured.err, cache.message(folder)


def _banners(orchestrator: str, *legs: str) -> str:
    """The stdout banners of consecutive ``legs``: a leg between two printed nothing."""
    rule = cli._RUN_ALL_RULE
    return "".join(f"\n{rule}\n  {orchestrator}: {leg}\n{rule}\n" for leg in legs)


def _summary(orchestrator: str, *lines: str) -> str:
    rule = cli._RUN_ALL_RULE
    title = f"  {orchestrator} summary (FY 2025-26)"
    return "\n".join((rule, title, rule, *lines, _SUMMARY_FOOTER)) + "\n"


def _errors(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("error:")]


@_CACHES
@_FETCH
def test_netbenefits_fa_refuses_the_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cache: _Cache,
    basis: str | None,
    fetch: bool,
) -> None:
    """Under --no-fetch-prices the config's Dec-31 price override would value the
    lot, so only the cache read refuses the run."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)

    stdout, stderr, message = _refused_run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config)),
            *("--output", str(tmp_path / "out" / "schedule-fa-a3.csv"), "--calendar-year", "2025"),
        ],
        config=config,
        cache=cache,
        basis=basis,
        ticker="XESP",
        stale=(
            "schedule-fa-a3.csv",
            "schedule-fa-a2.csv",
            "schedule-fa-a3-a2.csv",
            "schedule-fa-quicko.xlsx",
        ),
        fetch=fetch,
    )

    assert stderr == f"error: {message}\n"
    assert stdout == ""


@_CACHES
@_FETCH
def test_netbenefits_all_fa_leg_refuses_the_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cache: _Cache,
    basis: str | None,
    fetch: bool,
) -> None:
    """The real FA leg refuses first; income and CG still run and write, and the
    failed run removes their outputs and the stale FA ones."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)

    stdout, stderr, message = _refused_run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "netbenefits-all",
            *("--input", str(bundle), "--config", str(config)),
            *("--output-dir", str(tmp_path / "out"), *SOURCE_FY_ARGS),
        ],
        config=config,
        cache=cache,
        basis=basis,
        ticker="XESP",
        stale=(
            "schedule-fa-a3.csv",
            "schedule-fa-a2.csv",
            "schedule-fa-quicko.xlsx",
            "schedule-fa.csv",
            "schedule-fa-a3-a2.csv",
        ),
        fetch=fetch,
    )

    assert stderr.startswith(f"error: {message}\n")
    assert _errors(stderr) == [f"error: {message}"]
    assert stderr.endswith(
        _summary(
            "netbenefits-all",
            f"  {_LABEL['error']}  netbenefits-fa",
            f"  {_LABEL['manual_fill']}  netbenefits-income  {_REMOVED}",
            f"  {_LABEL['ok']}  netbenefits-cg  {_REMOVED}",
        )
    )
    assert _banners("netbenefits-all", "netbenefits-fa", "netbenefits-income") in stdout


@_CACHES
@_FETCH
def test_stockplan_fa_refuses_the_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cache: _Cache,
    basis: str | None,
    fetch: bool,
) -> None:
    input_dir, config, _ = setup_stockplan_exports(tmp_path)

    stdout, stderr, message = _refused_run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config)),
            *("--output", str(tmp_path / "out" / "schedule-fa.csv"), "--calendar-year", "2025"),
        ],
        config=config,
        cache=cache,
        basis=basis,
        ticker="GOOG",
        stale=("schedule-fa.csv",),
        fetch=fetch,
    )

    assert stderr == f"error: {message}\n{cli._STOCKPLAN_CONTRACT_POINTER}\n"
    assert stdout == ""


@_CACHES
@_FETCH
def test_stockplan_all_fa_leg_refuses_the_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cache: _Cache,
    basis: str | None,
    fetch: bool,
) -> None:
    """Every leg is real: income writes before the FA leg refuses, Schedule CG
    still proves zero sales, and the failed run removes every output."""
    input_dir, config, _ = setup_stockplan_exports(tmp_path)

    stdout, stderr, message = _refused_run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(config)),
            *("--output-dir", str(tmp_path / "out"), *SOURCE_FY_ARGS),
        ],
        config=config,
        cache=cache,
        basis=basis,
        ticker="GOOG",
        stale=("schedule-fa.csv",),
        fetch=fetch,
    )

    assert _errors(stderr) == [f"error: {message}"]
    assert stderr.endswith(
        _summary(
            "stockplan-all",
            f"  {_LABEL['manual_fill']}  stockplan-income  {_REMOVED}",
            f"  {_LABEL['error']}  stockplan-fa",
            "  not required (zero sales proven)  Schedule CG",
            f"  {_LABEL['manual_fill']}  Schedule FA Table A2 "
            "(complete daily cash/NAV evidence unavailable)",
        )
        + f"{cli._STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert _banners("stockplan-all", "stockplan-fa", "Schedule CG") in stdout
