"""A price cache the reader refuses stops IBKR Schedule FA (#1170).

The cache of one holding the statement prices is correctly hashed, but either
its table header swaps ``close_usd`` and ``high_usd``, or its one row has a
high below its close (under the close and the high peak basis). Standalone
``schedule-fa`` and ``run-all``'s real Schedule FA leg read it through the real
cache reader, offline and online. The reader refuses it before any price is
used or fetched: exit 4, its complete message, no FA output (an earlier run's
included) and the cache folder unchanged. Neither stream prints the cache's
markers or its symbol-built file name.

Standalone ``schedule-fa`` also fetches that holding's Yahoo series under a
later 1-for-3 split through the real fetcher and cache (#1307): the exact
closes it writes read back on the next run, and a close or high with no exact
decimal within a unit price's limits refuses the fetch.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import NamedTuple

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.output.fa_manual_fill import FA_A3_PEAK_VALUE_COLUMN
from ibkr_fa.prices import yahoo
from ibkr_fa.prices.yahoo import (
    YahooPriceFailureKind,
    _YahooTransportResult,
    fetch_daily_closes_usd,
)
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML
from tests._crash_reports import assert_no_markers
from tests._ibkr_run_all import CY_2025_INPUT, FY_2025_26_INPUT, run_all_argv, seed_every_target
from tests._price_cache import (
    CACHE_MARKERS,
    HIGH_CACHE_MARKERS,
    high_below_close_message,
    unsupported_layout_message,
    write_high_below_close_cache,
    write_unsupported_layout_cache,
)
from tests._price_cache import CY_2025_FIRST_PRICED_TICKER as _TICKER
from tests._price_cache import price_flags as _price_flags
from tests._yahoo_prices import bar_epoch as _epoch
from tests._yahoo_prices import chart_payload as _payload

_ONLINE = pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])


class _Cache(NamedTuple):
    """A refused cache kind: its writer, its complete refusal and its markers."""

    write: Callable[[Path, str], Path]
    message: Callable[[Path], str]
    markers: tuple[str, ...]


_LAYOUT = _Cache(write_unsupported_layout_cache, unsupported_layout_message, CACHE_MARKERS)
_HIGH_BELOW_CLOSE = _Cache(
    write_high_below_close_cache, high_below_close_message, HIGH_CACHE_MARKERS
)
# Each cache kind, with the [schedule_fa] peak_basis the config sets (None: unset).
_CACHES = pytest.mark.parametrize(
    ("cache_kind", "peak_basis"),
    [
        pytest.param(_LAYOUT, None, id="unsupported-layout"),
        pytest.param(_HIGH_BELOW_CLOSE, "close", id="high-below-close-close-basis"),
        pytest.param(_HIGH_BELOW_CLOSE, "high", id="high-below-close-high-basis"),
    ],
)


def _with_peak_basis(config_text: str, peak_basis: str | None) -> str:
    """``config_text``, which has no [schedule_fa] table, with ``peak_basis`` set."""
    assert "[schedule_fa]" not in config_text
    if peak_basis is None:
        return config_text
    return f'{config_text}\n[schedule_fa]\npeak_basis = "{peak_basis}"\n'


def _entries(cache_dir: Path) -> dict[str, bytes]:
    """Every entry of ``cache_dir`` with its bytes."""
    return {path.name: path.read_bytes() for path in cache_dir.iterdir()}


@_CACHES
@_ONLINE
def test_schedule_fa_refuses_a_cache_the_reader_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    online: bool,
    cache_kind: _Cache,
    peak_basis: str | None,
) -> None:
    config = tmp_path / "user.toml"
    config.write_text(
        _with_peak_basis(ACTIVITY_STATEMENT_E2E_USER_TOML, peak_basis), encoding="utf-8"
    )
    cache_dir = tmp_path / "cache"
    cache = cache_kind.write(cache_dir, _TICKER)
    before = _entries(cache_dir)
    out = tmp_path / "out"
    out.mkdir()
    output = out / "schedule-fa.csv"
    for stale in (output, out / "schedule-fa-a2.csv"):
        stale.write_bytes(b"an earlier run")

    rc = main(
        [
            "schedule-fa",
            *("--input", str(CY_2025_INPUT), "--config", str(config)),
            *("--output", str(output), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
            *_price_flags(monkeypatch, online=online),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {cache_kind.message(cache_dir)}\n"
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert _entries(cache_dir) == before
    assert_no_markers(captured.out + captured.err, *cache_kind.markers, cache.name)


@_CACHES
@_ONLINE
def test_run_all_schedule_fa_leg_refuses_a_cache_the_reader_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    online: bool,
    cache_kind: _Cache,
    peak_basis: str | None,
) -> None:
    """The FY schedules write their outputs; the real Schedule FA leg refuses
    the cache, and the run removes every target, an earlier run's included."""
    out = tmp_path / "out"
    seed_every_target(out)
    cache_dir = tmp_path / "cache"
    cache = cache_kind.write(cache_dir, _TICKER)
    before = _entries(cache_dir)
    argv = run_all_argv(tmp_path, FY_2025_26_INPUT)
    config = Path(argv[argv.index("--config") + 1])
    config.write_text(
        _with_peak_basis(config.read_text(encoding="utf-8"), peak_basis), encoding="utf-8"
    )

    rc = main(
        [
            *argv,
            *("--cy-input", str(CY_2025_INPUT)),
            *("--price-cache-dir", str(cache_dir)),
            *_price_flags(monkeypatch, online=online),
        ]
    )

    assert rc == 4
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert f"error: {cache_kind.message(cache_dir)}" in captured.err.splitlines()
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [
        line.split()[1]
        for line in captured.err.splitlines()
        if line.lstrip().startswith(error_label)
    ]
    assert errors == ["schedule-fa"]
    assert _entries(cache_dir) == before
    assert_no_markers(captured.out + captured.err, *cache_kind.markers, cache.name)


# ---- #1307: the holding's Yahoo series under a later 1-for-3 split ----------

_SPLIT_DAY = date(2026, 2, 2)
_SESSIONS = us_market_sessions(date(2024, 12, 1), date(2026, 5, 29))


def _serve_split_series(
    monkeypatch: pytest.MonkeyPatch, close: float, high: float, numerator: int, denominator: int
) -> list[str]:
    """Answer every default-path Yahoo fetch through the real fetcher: the
    holding gets ``close`` and ``high`` on every U.S. session from December
    2024 to May 2026, with a ``numerator``-for-``denominator`` split on
    2 February 2026; any other symbol fails in transport. Returns the symbols
    requested."""
    payload = _payload(
        symbol=_TICKER,
        bars=dict.fromkeys(_SESSIONS, close),
        highs=dict.fromkeys(_SESSIONS, high),
        splits={
            str(_epoch(_SPLIT_DAY)): {
                "date": _epoch(_SPLIT_DAY),
                "numerator": numerator,
                "denominator": denominator,
            }
        },
    )
    requested: list[str] = []

    def open_url(url: str, _timeout: float) -> _YahooTransportResult:
        symbol = url.split("/chart/", 1)[1].split("?", 1)[0]
        requested.append(symbol)
        if symbol != _TICKER:
            return _YahooTransportResult(None, YahooPriceFailureKind.TRANSPORT)
        return _YahooTransportResult(payload)

    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetch_daily_closes_usd)
    monkeypatch.setattr(yahoo, "_default_url_opener_result", open_url)
    return requested


def _schedule_fa(tmp_path: Path, output: Path, cache_dir: Path) -> int:
    config = tmp_path / "user.toml"
    config.write_text(ACTIVITY_STATEMENT_E2E_USER_TOML, encoding="utf-8")
    return main(
        [
            "schedule-fa",
            *("--input", str(CY_2025_INPUT), "--config", str(config)),
            *("--output", str(output), "--calendar-year", "2025"),
            *("--price-cache-dir", str(cache_dir)),
        ]
    )


@pytest.mark.parametrize(
    ("close", "numerator", "denominator"),
    [
        pytest.param(286.2, 1, 3, id="1-for-3"),
        pytest.param(954.0, 1, 10, id="1-for-10"),
    ],
)
def test_schedule_fa_files_the_exact_peak_under_a_later_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close: float,
    numerator: int,
    denominator: int,
) -> None:
    """#1307, the issue's Example 2: the statement holds XQBB lots of 4 and 6
    shares with a Dec-31 close of 95.40, and every close on its own day's basis
    is exactly 95.40. On main the 1-for-3 closes were
    ``95.39999999999999999999999999``, below the statement's close, so the
    close basis floored each peak to its closing value (₹34,142 and ₹51,213),
    and the second run refused the cache the first wrote. Now both runs file
    the earliest highest close at its own TTBR, as the exact 1-for-10 control
    does, and the second reads the cache without fetching."""
    requested = _serve_split_series(monkeypatch, close, close, numerator, denominator)
    for run in (1, 2):
        output = tmp_path / f"schedule-fa-{run}.csv"
        assert _schedule_fa(tmp_path, output, tmp_path / "cache") == 3
        with output.open(encoding="utf-8-sig", newline="") as handle:
            peaks = [
                row[FA_A3_PEAK_VALUE_COLUMN]
                for row in csv.DictReader(handle)
                if row["_audit: Source ticker"] == _TICKER
            ]
        assert peaks == ["32799", "49112"]
    assert requested.count(_TICKER) == 1


# The refused value, and a digit run inside it.
_SPLIT_MARKERS = ("286.21", "286")


@pytest.mark.parametrize(
    ("close", "high", "field"),
    [
        pytest.param(286.21, 286.21, "close", id="close"),
        pytest.param(286.2, 286.21, "high", id="high"),
    ],
)
def test_schedule_fa_refuses_a_split_adjusted_value_with_no_exact_decimal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    close: float,
    high: float,
    field: str,
) -> None:
    """#1307: under the 1-for-3 split, 286.21 has no exact decimal within a
    unit price's limits. On main the first run filed peaks from 28-digit values
    and every later run refused its cache; now the fetch refuses: exit 4, the
    fixed message, no FA output and no cache. Neither stream prints the value."""
    for marker in _SPLIT_MARKERS:
        assert marker in str(close if field == "close" else high)
    _serve_split_series(monkeypatch, close, high, 1, 3)
    out = tmp_path / "out"
    out.mkdir()
    cache_dir = tmp_path / "cache"

    rc = _schedule_fa(tmp_path, out / "schedule-fa.csv", cache_dir)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: Yahoo price response, indicators.quote[0].{field}[0]: the {field} on its own "
        "day's split basis has no exact value that fits: a unit price may have at most 9 "
        "digits before the decimal point and 20 after it\n"
    )
    assert captured.out == ""
    assert list(out.iterdir()) == []
    assert not cache_dir.exists()
    assert_no_markers(captured.out + captured.err, *_SPLIT_MARKERS)
