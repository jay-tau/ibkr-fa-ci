"""A blank "high" peak's remedy names the run that fills it (#1474).

Under ``[schedule_fa] peak_basis = "high"``, an A3 peak needs an intraday high
for every US market session in the holding window. A price cache that matches
the request is reused before any fetch, even when it holds no highs: one an
earlier fetch wrote with blank high cells, or a legacy (v1, close-only) one.
Then the peak is blank (``SESSION_WITHOUT_HIGH``), and the next online run
leaves it blank too. Following the remedy, an online run with an empty
``--price-cache-dir`` or with each holding's cache file moved aside fetches the
highs and fills the peak.

``schedule-fa`` on the CY 2025 end-to-end statement (two holdings, in the
review CSV's hint and the CBDT JSON fragment's gap) and ``stockplan-fa`` (in its
summary) run with a stubbed fetcher (no network). Every command that can print
the remedy accepts the flag it names.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML
from tests._ibkr_run_all import CY_2025_INPUT
from tests._ibkr_schedule_fa_inputs import CY_2025_NAV_INPUT, SCHEDULE_FA_CONFIG
from tests._stockplan_fa_exports import STOCKPLAN_FA_CONFIG, setup_stockplan_exports

_CAUSE = (
    'peak_basis = "high" needs a valid intraday high for every US market session in '
    "the holding window, and one is missing"
)
_REMEDY = (
    "Re-run with price fetching and an empty --price-cache-dir (or first move each such "
    "holding's cache file, and any .lock beside it, out of the price cache directory: a "
    'cached series is reused even without highs), set [schedule_fa] peak_basis = "close", '
    "or enter it by hand. A ticker override won't help."
)
_HIGH = '\n[schedule_fa]\npeak_basis = "high"\n'
_JSON_FLAGS = ("--format", "cbdt-json-fragment", "--itr-form", "ITR-2")


@dataclass
class _Fetcher:
    """A stub price source: a 300.00 close on every US market session in the
    request, with a 301.00 high when ``highs``. Records each ticker fetched."""

    highs: bool
    calls: list[Ticker] = field(default_factory=list)

    def __call__(
        self, ticker: Ticker, *, start: date, end: date, now: datetime, fetch_symbol=None
    ) -> PriceSeries:
        self.calls.append(ticker)
        sessions = us_market_sessions(start, end)
        return PriceSeries(
            ticker=ticker,
            currency="USD",
            closes=dict.fromkeys(sessions, Decimal("300.00")),
            highs=dict.fromkeys(sessions, Decimal("301.00")) if self.highs else {},
            has_split_in_range=False,
            source="stub",
            fetched_at=now,
        )


def _to_legacy(path: Path) -> None:
    """Rewrite a cache with blank high cells in the legacy v1 layout: the
    ``date,close_usd`` header, no high column, and its payload hash re-made."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    (hash_index,) = [i for i, line in enumerate(lines) if line.startswith("# payload_sha256=")]
    rest = lines[:hash_index] + lines[hash_index + 1 :]
    header = rest.index("date,close_usd,high_usd\n")
    assert all(line.endswith(",\n") for line in rest[header + 1 :])
    legacy = [
        *rest[:header],
        "date,close_usd\n",
        *(line.removesuffix(",\n") + "\n" for line in rest[header + 1 :]),
    ]
    digest = hashlib.sha256("".join(legacy).encode()).hexdigest()
    legacy.insert(hash_index, f"# payload_sha256={digest}\n")
    path.write_text("".join(legacy), encoding="utf-8")


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


_CACHES = pytest.mark.parametrize("legacy", [False, True], ids=["blank-highs", "legacy-v1"])
_REMEDIES = pytest.mark.parametrize("remedy", ["empty-cache-dir", "cache-moved-aside"])


def _follow(remedy: str, tmp_path: Path, cache_dir: Path) -> Path:
    """Follow ``remedy``: the --price-cache-dir the rerun uses."""
    if remedy == "empty-cache-dir":
        return _mkdir(tmp_path / "empty-cache")
    aside = _mkdir(tmp_path / "aside")
    for cached in cache_dir.iterdir():
        cached.rename(aside / cached.name)
    return cache_dir


def _seed(run: Callable[[Path], object], cache_dir: Path, *, legacy: bool) -> list[Ticker]:
    """Run once with a price source that has no highs, so the run caches each
    holding's closes with blank highs; in the legacy layout if ``legacy``."""
    seeding = _Fetcher(highs=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", seeding)
        run(cache_dir)
    assert seeding.calls
    if legacy:
        for cached in cache_dir.iterdir():
            _to_legacy(cached)
    return seeding.calls


# --- IBKR schedule-fa ------------------------------------------------------------


def _schedule_fa(
    tmp_path: Path, statement: Path, config_text: str, output: str, *extra: str
) -> Callable[[Path], int]:
    config = tmp_path / "user.toml"
    config.write_text(config_text, encoding="utf-8")

    def run(cache_dir: Path) -> int:
        return main(
            [
                "schedule-fa",
                *("--input", str(statement), "--config", str(config)),
                *("--output", str(tmp_path / output), "--calendar-year", "2025"),
                *("--price-cache-dir", str(cache_dir), *extra),
            ]
        )

    return run


def _peaks(tmp_path: Path) -> list[str]:
    """Each Table A3 row's peak cell."""
    with (tmp_path / "schedule-fa.csv").open(newline="", encoding="utf-8") as handle:
        return [row["Peak value of investment during the Period"] for row in csv.DictReader(handle)]


@_REMEDIES
@_CACHES
def test_schedule_fa_hint_names_the_rerun_that_fills_a_high_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
    remedy: str,
) -> None:
    run = _schedule_fa(
        tmp_path, CY_2025_INPUT, ACTIVITY_STATEMENT_E2E_USER_TOML + _HIGH, "schedule-fa.csv"
    )
    cache_dir = tmp_path / "cache"
    tickers = _seed(run, cache_dir, legacy=legacy)
    capsys.readouterr()
    fetcher = _Fetcher(highs=True)
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetcher)

    run(cache_dir)

    assert fetcher.calls == []
    assert _peaks(tmp_path) == ["", "", ""]
    printed = capsys.readouterr().out.splitlines()
    hint = printed.index(f"   Peak value is blank for these holdings because {_CAUSE}:")
    assert printed[hint + 1 : hint + 4] == [
        "     - XQBB (US99995B1044)",
        "     - XQBE (US99995D1000)",
        f"       {_REMEDY}",
    ]

    run(_follow(remedy, tmp_path, cache_dir))

    assert fetcher.calls == tickers
    assert all(peak for peak in _peaks(tmp_path))
    assert "Peak value is blank" not in capsys.readouterr().out


@_CACHES
def test_schedule_fa_json_gap_names_the_rerun_that_fills_a_high_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
) -> None:
    """With every other cell supplied (the CBDT tests' NAV statement and
    config), the fragment is withheld only for the three blank peaks, each with
    the same remedy. The rerun with an empty --price-cache-dir writes it."""
    assert SCHEDULE_FA_CONFIG.count("[schedule_fa]\n") == 1
    config = SCHEDULE_FA_CONFIG.replace("[schedule_fa]\n", '[schedule_fa]\npeak_basis = "high"\n')
    run = _schedule_fa(tmp_path, CY_2025_NAV_INPUT, config, "schedule-fa.json", *_JSON_FLAGS)
    cache_dir = tmp_path / "cache"
    _seed(run, cache_dir, legacy=legacy)
    capsys.readouterr()
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", _Fetcher(highs=True))
    gap = (
        f"PeakBalanceDuringPeriod: the peak is blank because {_CAUSE}. {_REMEDY} No config "
        "value can supply an A3 peak itself, so to enter it by hand use the review CSV or "
        "xlsx output instead"
    )

    assert run(cache_dir) == 3
    assert capsys.readouterr().err == (
        "error: Refusing to emit the CBDT Schedule FA JSON fragment — it would file an "
        "incomplete disclosure. Supply the following and re-run:\n"
        + "".join(f"  - A3 row {n} {gap}\n" for n in (1, 2, 3))
    )

    assert run(_follow("empty-cache-dir", tmp_path, cache_dir)) == 0
    assert (tmp_path / "schedule-fa.json").exists()


# --- StockPlan -------------------------------------------------------------------


@_REMEDIES
@_CACHES
def test_stockplan_fa_summary_names_the_rerun_that_fills_a_high_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
    remedy: str,
) -> None:
    input_dir, cfg, out = setup_stockplan_exports(tmp_path, config=STOCKPLAN_FA_CONFIG + _HIGH)

    def run(cache_dir: Path) -> int:
        return main(
            [
                "stockplan-fa",
                *("--input", str(input_dir), "--config", str(cfg)),
                *("--output", str(out), "--calendar-year", "2025"),
                *("--price-cache-dir", str(cache_dir)),
            ]
        )

    cache_dir = tmp_path / "cache"
    tickers = _seed(run, cache_dir, legacy=legacy)
    capsys.readouterr()
    fetcher = _Fetcher(highs=True)
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetcher)

    run(cache_dir)

    assert fetcher.calls == []
    assert (
        f"  4 A3 Peak value(s): MANUAL-FILL — the peak is blank because {_CAUSE}. {_REMEDY}"
    ) in capsys.readouterr().out.splitlines()

    run(_follow(remedy, tmp_path, cache_dir))

    assert fetcher.calls == tickers
    assert "Peak value(s): MANUAL-FILL" not in capsys.readouterr().out


# --- The flag ----------------------------------------------------------------------


_CY = ("--config", "user.toml", "--calendar-year", "2025")
_FY = ("--config", "user.toml", "--financial-year", "2025-26", "--itr-form", "ITR-2")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ["schedule-fa", "--input", "s.csv", "--output", "o.csv", *_CY], id="schedule-fa"
        ),
        pytest.param(["run-all", "--fy-input", "s.csv", "--output-dir", "o", *_FY], id="run-all"),
        pytest.param(
            ["stockplan-fa", "--input", "d", "--output", "o.csv", *_CY], id="stockplan-fa"
        ),
        pytest.param(
            ["stockplan-all", "--input", "d", "--output-dir", "o", *_FY], id="stockplan-all"
        ),
        pytest.param(
            ["netbenefits-fa", "--input", "d", "--output", "o.csv", *_CY], id="netbenefits-fa"
        ),
        pytest.param(
            ["robinhood-fa", "--input", "d", "--output-dir", "o", *_FY], id="robinhood-fa"
        ),
    ],
)
def test_every_command_printing_the_remedy_takes_the_flag_it_names(argv: list[str]) -> None:
    """The commands whose output can hold the remedy: IBKR's Schedule FA hint
    (schedule-fa, run-all), the StockPlan FA summary (stockplan-fa,
    stockplan-all) and the CBDT JSON fragment's gap (schedule-fa,
    netbenefits-fa, robinhood-fa)."""
    args = cli._build_parser().parse_args([*argv, "--price-cache-dir", "empty-cache"])

    assert args.price_cache_dir == Path("empty-cache")
