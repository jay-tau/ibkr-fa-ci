"""A price cache the reader refuses stops the Robinhood and Fidelity CMA
Schedule FA commands (#1170).

Each run finds a correctly hashed cache where the real price-cache reader
looks for a holding the command prices: one whose table header swaps
``close_usd`` and ``high_usd``, or one whose row has a high below its close
(under each peak basis the command supports). The reader refuses it before
anything is fetched or valued: exit 4 with the complete cache refusal, no new
or stale Schedule FA output, the cache folder untouched, and neither the
cache's markers nor its symbol-built file name printed.

Robinhood's bundle is synthetic (its statements are PDFs): ``_load`` returns
a reconciled synthetic account holding one configured equity, and the FA leg
runs for real from there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import NamedTuple

import pytest

import ibkr_fa.pipeline_fidelity_cma_fa as cma_fa
import ibkr_fa.pipeline_robinhood as robinhood_pipeline
import ibkr_fa.pipeline_robinhood_equity_prices as robinhood_equity_prices
import ibkr_fa.pipeline_robinhood_outputs as robinhood_outputs
from ibkr_fa.cli import main
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeResult
from tests._crash_reports import assert_no_markers
from tests._fidelity_cma_bundle import cma_inputs  # noqa: F401 - fixture
from tests._price_cache import (
    CACHE_MARKERS,
    HIGH_CACHE_MARKERS,
    high_below_close_message,
    unsupported_layout_message,
    write_high_below_close_cache,
    write_unsupported_layout_cache,
)
from tests._price_cache import price_source_called as _price_source_called
from tests._robinhood_cli import LEG_LINE
from tests._robinhood_fa_inputs import FA_SYMBOL, fa_paired
from tests._robinhood_synthetic_fa import stub_all_but_fa, synthetic_bundle

_FETCH = pytest.mark.parametrize("fetch", ["offline", "online"])


class _CacheKind(NamedTuple):
    """An unusable cache: its writer, its complete refusal and its markers."""

    write: Callable[[Path, str], Path]
    message: Callable[[Path], str]
    markers: tuple[str, ...]


_LAYOUT = _CacheKind(write_unsupported_layout_cache, unsupported_layout_message, CACHE_MARKERS)
_HIGH_BELOW_CLOSE = _CacheKind(
    write_high_below_close_cache, high_below_close_message, HIGH_CACHE_MARKERS
)
# Each cache kind, with the ``schedule_fa.peak_basis`` the run's config sets
# (``None``: no ``[schedule_fa]`` table). The high-below-close cache runs under
# each basis the command supports.
_LAYOUT_CASE = pytest.param(_LAYOUT, None, id="layout")
_CLOSE_BASIS_CASE = pytest.param(_HIGH_BELOW_CLOSE, "close", id="high-below-close-close-basis")
_HIGH_BASIS_CASE = pytest.param(_HIGH_BELOW_CLOSE, "high", id="high-below-close-high-basis")
_ROBINHOOD_CACHES = pytest.mark.parametrize(
    ("kind", "basis"), [_LAYOUT_CASE, _CLOSE_BASIS_CASE, _HIGH_BASIS_CASE]
)


def _with_basis(config: str, basis: str | None) -> str:
    """``config`` plus a ``[schedule_fa]`` table setting ``basis``, if any;
    ``config`` has no such table."""
    assert "[schedule_fa]" not in config
    return config if basis is None else f'{config}\n[schedule_fa]\npeak_basis = "{basis}"\n'


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root``: a file's bytes, ``None`` for a folder."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _assert_private(text: str, kind: _CacheKind, cache: Path) -> None:
    """``text`` prints no marker the cache holds, nor its symbol-built name."""
    assert_no_markers(text, *kind.markers, cache.name)


# --- Robinhood ----------------------------------------------------------------

# The synthetic holder's only Robinhood equity: account 111111111 holds two
# shares at the end of 2025, so the FA leg prices it for the whole year.
_ROBINHOOD_SYMBOL = FA_SYMBOL


def _robinhood_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    fetch: str,
    kind: _CacheKind,
    basis: str | None,
) -> tuple[list[str], Path, Path, Path]:
    """The argv, output folder, typed cache folder and unusable cache of a
    ``command`` run whose bundle holds the synthetic equity. The output folder
    holds stale Schedule FA files."""
    config = tmp_path / "user.toml"
    config.write_text(
        _with_basis('[account]\nholder_name = "Tax Payer"\n', basis), encoding="utf-8"
    )
    bundle = synthetic_bundle(config, fa_paired(sold=False, dividend=False))
    monkeypatch.setattr(robinhood_pipeline, "_load", lambda **_kwargs: bundle)
    # The pipeline's own default fetcher wraps this one.
    monkeypatch.setattr(
        robinhood_equity_prices, "fetch_daily_closes_usd_retrying", _price_source_called
    )
    cache_dir = tmp_path / "cache"
    cache = kind.write(cache_dir, _ROBINHOOD_SYMBOL)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)  # Robinhood refuses an output folder others can read
    for name in robinhood_outputs.FA_FILENAMES:
        (out / name).write_bytes(b"STALE")
    argv = [
        command,
        *("--input", str(tmp_path / "bundle"), "--config", str(config)),
        *("--output-dir", str(out), "--financial-year", "2025-26", "--itr-form", "ITR-2"),
        *("--price-cache-dir", str(cache_dir)),
        *(("--no-fetch-prices",) if fetch == "offline" else ()),
    ]
    return argv, out, cache_dir, cache


@_ROBINHOOD_CACHES
@_FETCH
def test_robinhood_fa_refuses_an_unusable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fetch: str,
    kind: _CacheKind,
    basis: str | None,
) -> None:
    argv, out, cache_dir, cache = _robinhood_case(
        tmp_path, monkeypatch, "robinhood-fa", fetch, kind, basis
    )
    # The bundle's source income has Schedule OS blockers, which stop Schedule
    # FA before any price since #1574; with none the run reaches the cache.
    monkeypatch.setattr(
        robinhood_pipeline,
        "_compute_source_income",
        lambda *_args: RobinhoodIncomeResult(
            **{field.name: () for field in fields(RobinhoodIncomeResult)}
        ),
    )
    before = _tree(cache_dir)

    assert main(argv) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {kind.message(cache_dir)}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert _tree(cache_dir) == before
    _assert_private(captured.out + captured.err, kind, cache)


@_ROBINHOOD_CACHES
@_FETCH
def test_robinhood_all_fa_leg_refuses_an_unusable_cache_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fetch: str,
    kind: _CacheKind,
    basis: str | None,
) -> None:
    """The other legs are stubbed and write their outputs, as in
    ``test_all_failed_leg_removes_every_output``; the FA leg prices the bundle
    for real and records the cache refusal as its reason."""
    written = stub_all_but_fa(monkeypatch, tmp_path / "out")
    argv, out, cache_dir, cache = _robinhood_case(
        tmp_path, monkeypatch, "robinhood-all", fetch, kind, basis
    )
    before = _tree(cache_dir)

    assert main([*argv, "--debug"]) == 4

    captured = capsys.readouterr()
    assert captured.err == ""
    assert LEG_LINE.findall(captured.out) == [
        ("ok", "equity-cg"),
        ("ok", "schedule-os"),
        ("ok", "income-ftc"),
        ("error", "schedule-fa"),
        ("error", "fa-a1"),
        ("not-required", "options"),
        ("not-required", "product-scope"),
    ]
    assert (
        f"  error        schedule-fa\n                 -> {kind.message(cache_dir)}\n"
        in captured.out
    )
    # Each successful writing leg wrote; nothing is left, including the stale FA files.
    assert {path.name for path in written} == {
        "schedule-cg.xlsx",
        "schedule-cg-debug.csv",
        "schedule-cg-ltcg-debug.csv",
        "schedule-os.xlsx",
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67.csv",
    }
    assert list(out.iterdir()) == []
    assert not any(str(path) in captured.out for path in written)
    assert captured.out.count("-> output removed because the run failed") == 3
    assert _tree(cache_dir) == before
    _assert_private(captured.out + captured.err, kind, cache)


# --- Fidelity CMA -------------------------------------------------------------


# fidelity-cma-fa refuses peak_basis = "high" before it reads a price.
@pytest.mark.parametrize(("kind", "basis"), [_LAYOUT_CASE, _CLOSE_BASIS_CASE])
def test_fidelity_cma_fa_refuses_an_unusable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
    kind: _CacheKind,
    basis: str | None,
) -> None:
    """Online only: the command has no ``--no-fetch-prices``."""
    statements, _tax_forms, config = request.getfixturevalue("cma_inputs")
    config.write_text(_with_basis(config.read_text(encoding="utf-8"), basis), encoding="utf-8")
    monkeypatch.setattr(cma_fa, "fetch_nasdaq_mutual_fund_daily_closes_usd", _price_source_called)
    price_cache = tmp_path / "price-cache"
    folder = price_cache / "nasdaq-mutual-funds"  # where the command's reader looks
    cache = kind.write(folder, "SPAXX")
    before = _tree(price_cache)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("schedule-fa.csv", "schedule-fa-a2.csv"):
        (out / name).write_bytes(b"STALE")

    code = main(
        [
            "fidelity-cma-fa",
            *("--input", str(statements), "--config", str(config)),
            *("--output", str(out / "schedule-fa.csv"), "--calendar-year", "2025"),
            *("--price-cache-dir", str(price_cache)),
        ]
    )

    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {kind.message(folder)}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert _tree(price_cache) == before
    _assert_private(captured.out + captured.err, kind, cache)
