"""Tests for the ``stockplan-fa`` pipeline (Schedule FA Table A3 for a Morgan
Stanley StockPlan holding) and its CLI subcommand. (#365)

Uses synthetic openpyxl workbooks (CG/Loss, Vested Share Holdings, Dividend) +
a stub price fetcher (no network) + a full [stockplan] Schedule FA config.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import functools
import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

import openpyxl
import pytest
from openpyxl.worksheet._reader import WorkSheetParser

import ibkr_fa.pipeline_stockplan_fa as fa_mod
import ibkr_fa.xlsx_checks
from ibkr_fa.cli import (
    _EXIT_DOMAIN_ERROR,
    _STOCKPLAN_CONTRACT_POINTER,
    _run_stockplan_cg_status,
    main,
)
from ibkr_fa.compute.dec31_close import ClosingPriceError
from ibkr_fa.exceptions import ConfigError, IbkrFaError, InvariantError, ParseError
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.stockplan import _xlsx
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_stockplan_fa import (
    StockplanFaError,
    inspect_stockplan_cg_evidence,
    run_stockplan_fa,
)
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import YahooResponseFormatError
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_no_markers
from tests._dec31_close import (
    MARKET_SESSION_REFUSAL,
    assert_markers_absent,
    unsupported_year_refusal,
)
from tests._stockplan_fa_cases import (
    AUDIT_COST,
    FUTURE_VEST_REFUSAL,
    INITIAL_VALUE,
    LEFT_HOLDINGS,
    LEFT_MARKERS,
    LEFT_VESTS,
    LONG_COST_VESTS,
    MARKED_CONFIG,
    MARKERS_IN_INPUT,
    MISMATCH,
    PRIOR_SESSION_ONLY_PIPELINE,
    SP_FUTURE_VEST,
    SP_MARKERS,
    SP_ORDER,
    VEST_A,
    VEST_B,
    a3_row,
    offline_pipeline,
    setup_marked_exports,
    setup_one_vest,
)
from tests._stockplan_fa_cases import SP_MARKED_VESTS as _SP_VESTS
from tests._stockplan_fa_cases import STOCKPLAN_DEC31 as _DEC31
from tests._stockplan_fa_cases import marked_dec30_fetcher as _marked_dec30_fetcher
from tests._stockplan_fa_cases import prior_session_only_fetcher as _prior_session_only
from tests._stockplan_fa_exports import STOCKPLAN_FA_CONFIG as CONFIG
from tests._stockplan_fa_exports import STOCKPLAN_HELD_ACQUISITION as _HELD_ACQ
from tests._stockplan_fa_exports import STOCKPLAN_HELD_SHARES as _HELD_SHARES
from tests._stockplan_fa_exports import STOCKPLAN_VESTS as _VESTS
from tests._stockplan_fa_exports import run_stockplan_fa_cli as _main_fa
from tests._stockplan_fa_exports import setup_stockplan_exports as _setup
from tests._stockplan_fa_exports import stockplan_fa_config as _config
from tests._stockplan_fa_exports import stub_fetcher
from tests._stockplan_fa_exports import write_stockplan_cg_report as _write_cg
from tests._stockplan_fa_exports import write_stockplan_holdings as _write_holdings
from tests._stockplan_report_writers import DIVIDEND_REPORT_HEADER
from tests._stockplan_report_writers import STOCKPLAN_USD_FOOTER as _USD
from tests._yahoo_prices import NO_ZONE_REFUSAL, serve_no_zone_response


def _run(
    input_dir: Path, cfg: Path, out: Path, *, price_fetcher=stub_fetcher, calendar_year: int = 2025
):
    return run_stockplan_fa(
        input_dir=input_dir,
        config_path=cfg,
        output_path=out,
        calendar_year=calendar_year,
        fetch_prices=True,
        price_cache_dir=input_dir / ".cache",
        price_fetcher=price_fetcher,
        price_now=_dt.datetime(2026, 7, 15, tzinfo=_dt.UTC),
    )


# sha256 of the happy-path review CSV, taken before #627 moved the Dec-31
# valuation into the engine, and repinned in #1033 after each changed figure
# was re-derived from the new vests. The CSV ends lines with CRLF, which
# .gitattributes and pre-commit would rewrite in a committed golden file,
# so the bytes are pinned by digest.
_HAPPY_PATH_SHA256 = "e38b78f08503efb74d3c7ff3c24adbfe861567598ae259e260a8a1ffb219a9c6"


def test_happy_path_emits_a3(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    report = _run(input_dir, cfg, out)
    assert report.rows_written == 4
    assert report.held_shares == Decimal("16.158")
    assert report.reconciled_as_of == _dt.date(2025, 11, 14)
    assert report.peak_blank_reasons == ()
    assert report.dividend_inr > 0  # the CY2025 dividend routed to A3
    assert len(report.warnings) == 1
    assert "Schedule FA Table A2 was not generated" in report.warnings[0]
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _HAPPY_PATH_SHA256


@pytest.mark.parametrize(
    ("override_isin", "expected_fetch_symbol"),
    [("US02079K1079", "GOOG-YF"), ("US99999F1012", None)],  # GOOG's ISIN; a made-up one
)
def test_ticker_override_changes_only_the_fetched_symbol(
    tmp_path: Path, override_isin: str, expected_fetch_symbol: str | None
) -> None:
    # An override applies only on an ISIN match, and only to the symbol sent to
    # the price source: the series and the rows stay keyed by GOOG. (#667)
    config = CONFIG + (
        '\n[[prices.ticker_overrides]]\nibkr_ticker = "GOOG"\n'
        f'isin = "{override_isin}"\nyahoo_symbol = "GOOG-YF"\n'
    )
    input_dir, cfg, out = _setup(tmp_path, config=config)
    calls: list[tuple[Ticker, str | None]] = []

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        calls.append((ticker, fetch_symbol))
        return stub_fetcher(ticker, start=start, end=end, now=now)

    report = _run(input_dir, cfg, out, price_fetcher=fetcher)
    assert calls == [(Ticker("GOOG"), expected_fetch_symbol)]
    assert report.peak_blank_reasons == ()
    with out.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert {(r["_audit: Source ticker"], r["_audit: Source ISIN"]) for r in rows} == {
        ("GOOG", "US02079K1079")
    }


def test_price_response_with_no_exchange_zone_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1139: on the default fetcher, a Yahoo response with no exchange zone
    refuses the run after one request, with no price cache and no A3 written."""
    input_dir, cfg, out = _setup(tmp_path)
    urls = serve_no_zone_response(monkeypatch)
    with pytest.raises(YahooResponseFormatError) as exc:
        _run(input_dir, cfg, out, price_fetcher=None)
    assert str(exc.value) == NO_ZONE_REFUSAL
    assert len(urls) == 1
    assert not (input_dir / ".cache").exists()
    assert not out.exists()


def test_cg_evidence_probe_proves_zero_sales(tmp_path: Path) -> None:
    input_dir, cfg, _ = _setup(tmp_path)
    report = inspect_stockplan_cg_evidence(
        input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
    )
    assert report.sales_count == 0
    assert report.report_path.name.startswith("IN - Capital Gain_Loss Report")
    assert report.coverage_start == _dt.date(2025, 6, 2)
    assert report.coverage_end == _dt.date(2026, 5, 4)


def test_cg_evidence_probe_counts_sales(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2025, 11, 1),
        300.0,
        1.0,
        "GOOG",
        300.0,
        _dt.datetime(2025, 8, 14),
    ]
    input_dir, cfg, _ = _setup(tmp_path, sales=[sale])
    assert (
        inspect_stockplan_cg_evidence(
            input_dir=input_dir,
            config_path=cfg,
            financial_year=FinancialYear(2025),
        ).sales_count
        == 1
    )


def test_cg_evidence_probe_refuses_report_run_before_coverage_end(tmp_path: Path) -> None:
    # A "No records found" report run before Mar-31 cannot prove zero FY sales (#680).
    input_dir, cfg, _ = _setup(
        tmp_path, cg_run_on="01-Feb-2026", period="01-Apr-2025 to 31-Mar-2026"
    )
    with pytest.raises(ParseError, match="was not run strictly after its reporting period ended"):
        inspect_stockplan_cg_evidence(
            input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
        )


_NO_SPAN = (
    "IN - Capital Gain_Loss Report.xlsx's reporting period does not span the relevant "
    "account period; sales cannot be proven zero"
)
# The period, opening and run dates the coverage tests use, in every form a
# refusal could print them; none may appear (#1043).
_COVERAGE_DATES = (
    "01-Sep-2025",
    "2025-09-01",
    "02-Jun-2025",
    "2025-06-02",
    "01-May-2026",
    "2026-05-01",
    "04-May-2026",
    "2026-05-04",
    "2025-07-14",
    "2025-04-01",
    "2026-03-31",
    "22-Jun-2026",
    "2026-06-22",
)


def test_cg_evidence_probe_refuses_coverage_after_account_opening(tmp_path: Path) -> None:
    input_dir, cfg, _ = _setup(tmp_path, period="01-Sep-2025 to 01-May-2026")
    with pytest.raises(StockplanFaError) as excinfo:
        inspect_stockplan_cg_evidence(
            input_dir=input_dir,
            config_path=cfg,
            financial_year=FinancialYear(2025),
        )
    assert str(excinfo.value) == _NO_SPAN
    assert_no_markers(str(excinfo.value), *_COVERAGE_DATES)


def test_cg_evidence_probe_refuses_coverage_ending_before_fy_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # "No records found" through 30 March can't rule out a 31 March sale.
    input_dir, cfg, _ = _setup(tmp_path, period="01-Apr-2025 to 30-Mar-2026")
    with pytest.raises(StockplanFaError) as excinfo:
        inspect_stockplan_cg_evidence(
            input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
        )
    assert str(excinfo.value) == _NO_SPAN
    assert_no_markers(str(excinfo.value), "01-Apr-2025", "2025-04-01", "30-Mar-2026", "2026-03-30")

    code = _run_stockplan_cg_status(
        argparse.Namespace(input=input_dir, config=cfg, financial_year=FinancialYear(2025))
    )
    assert code == _EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err == (
        f"error: could not prove StockPlan Schedule CG status: {_NO_SPAN}\n"
    )


def test_fa_refuses_cg_coverage_ending_before_cy_end(tmp_path: Path) -> None:
    # Through 30 December can't rule out a 31 December sale of a filed lot.
    input_dir, cfg, out = _setup(tmp_path, period="02-Jun-2025 to 30-Dec-2025")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == _NO_SPAN
    assert not out.exists()


@pytest.mark.parametrize(
    ("ibkr_opening", "period"),
    [
        ("2025-11-10", "01-Nov-2025 to 01-May-2026"),  # IBKR opened mid-FY
        ("2026-05-01", "01-May-2026 to 01-Jul-2026"),  # IBKR opened after the FY
    ],
)
def test_cg_evidence_probe_ignores_ibkr_opening_date(
    tmp_path: Path, ibkr_opening: str, period: str
) -> None:
    # [account] is a separate IBKR account. Its later opening date must not
    # shrink StockPlan's window: the workbook misses the start of FY 2025-26,
    # so a sale there can't be ruled out. (#600)
    input_dir, cfg, _ = _setup(
        tmp_path,
        config=_config(account_opening=ibkr_opening, stockplan_opening=None),
        period=period,
        cg_run_on="14-Jul-2026",
    )
    with pytest.raises(StockplanFaError, match="sales cannot be proven zero"):
        inspect_stockplan_cg_evidence(
            input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
        )


def test_fa_ignores_ibkr_opening_date_after_cy(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(
        tmp_path,
        config=_config(account_opening="2026-02-01", stockplan_opening=None),
        period="01-Feb-2026 to 01-May-2026",
    )
    with pytest.raises(StockplanFaError, match="sales cannot be proven zero"):
        _run(input_dir, cfg, out)
    assert not out.exists()


@pytest.mark.parametrize("ibkr_opening", ["2020-01-01", "2026-05-01"])
def test_cg_evidence_probe_uses_stockplan_opening_date(tmp_path: Path, ibkr_opening: str) -> None:
    input_dir, cfg, _ = _setup(
        tmp_path,
        config=_config(account_opening=ibkr_opening, stockplan_opening="2025-11-10"),
        period="01-Nov-2025 to 01-May-2026",
    )
    report = inspect_stockplan_cg_evidence(
        input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
    )
    assert report.sales_count == 0


def test_stockplan_opening_after_period_refuses(tmp_path: Path) -> None:
    # No part of FY 2025-26 is left to check, so refuse rather than report the
    # CG leg as "not required". (#600)
    input_dir, cfg, _ = _setup(
        tmp_path,
        config=_config(account_opening="2025-07-14", stockplan_opening="2026-05-01"),
        period="01-Jan-2025 to 01-May-2026",
    )
    with pytest.raises(
        StockplanFaError,
        match=r"^\[stockplan\]\.opening_date is after the period checked, so "
        r"IN - Capital Gain_Loss Report\.xlsx cannot prove zero sales for it; correct the date, "
        r"or do not run StockPlan commands for this period$",
    ) as excinfo:
        inspect_stockplan_cg_evidence(
            input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
        )
    assert_no_markers(str(excinfo.value), *_COVERAGE_DATES, "01-Jan-2025", "2025-01-01")


def test_coverage_refusal_hints_at_stockplan_opening_date(tmp_path: Path) -> None:
    # A StockPlan-only config that relied on [account].opening_date to clip the
    # window now refuses, and says how to bound it. (#600)
    input_dir, cfg, _ = _setup(
        tmp_path, config=_config(account_opening="2025-07-14", stockplan_opening=None)
    )
    with pytest.raises(StockplanFaError) as excinfo:
        inspect_stockplan_cg_evidence(
            input_dir=input_dir, config_path=cfg, financial_year=FinancialYear(2025)
        )
    assert str(excinfo.value) == (
        f"{_NO_SPAN}; if the StockPlan account opened mid-period, set "
        "[stockplan].opening_date to bound the window"
    )
    assert_no_markers(str(excinfo.value), *_COVERAGE_DATES)


def test_vest_after_dec31_excluded_but_reconciles(tmp_path: Path) -> None:
    # A 2026 vest is excluded from the Dec-31 held lots, but the Holdings snapshot
    # (which includes it) still reconciles — the prefix includes all Dec-31 vests.
    vests = [*_VESTS, ("2026-01-14", "V5", 231.05, 4.506, 3.108)]
    all_acq = sum((Decimal(str(f)) * Decimal(str(d)) for _, _, f, _, d in vests), Decimal("0"))
    input_dir, cfg, out = _setup(tmp_path, vests=vests)
    _write_holdings(
        input_dir / "Vested Share Holdings.xlsx",
        shares=Decimal("16.158") + Decimal("3.108"),
        acq=all_acq,
    )
    report = _run(input_dir, cfg, out)
    assert report.rows_written == 4  # only the 4 Dec-31 vests
    assert report.held_shares == Decimal("16.158")
    assert report.reconciled_as_of == _dt.date(2026, 1, 14)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _HAPPY_PATH_SHA256


def test_vest_after_the_selected_date_is_outside_the_holdings(tmp_path: Path) -> None:
    # The Holdings Report is a snapshot as of its Selected Date, which may be
    # before its run date: a later vest is neither in its totals nor held on
    # Dec-31, and the Releases Report, run after the Selected Date, lists it (#1290).
    vests = [*_VESTS, ("2026-03-16", "V5", 231.05, 4.506, 3.108)]
    input_dir, cfg, out = _setup(tmp_path, vests=vests)
    _write_holdings(
        input_dir / "Vested Share Holdings.xlsx", run_on="14-Jul-2026", selected_date="13-Feb-2026"
    )
    report = _run(input_dir, cfg, out)
    assert report.rows_written == 4
    assert report.reconciled_as_of == _dt.date(2025, 11, 14)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _HAPPY_PATH_SHA256


def test_two_vests_same_date(tmp_path: Path) -> None:
    # Two vests on the same date (distinct orders) are both held and both filed.
    vests = [
        ("2025-10-14", "A", 211.95, 5.408, 3.729),
        ("2025-10-14", "B", 211.95, 1.001, 0.689),
    ]
    acq = sum((Decimal(str(f)) * Decimal(str(d)) for _, _, f, _, d in vests), Decimal("0"))
    input_dir, cfg, out = _setup(tmp_path, vests=vests)
    _write_holdings(
        input_dir / "Vested Share Holdings.xlsx",
        shares=Decimal("3.729") + Decimal("0.689"),
        acq=acq,
    )
    report = _run(input_dir, cfg, out)
    assert report.rows_written == 2
    assert report.held_shares == Decimal("4.418")
    # The bytes main wrote for this input before #1290.
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _TWO_VESTS_SHA256


_TWO_VESTS_SHA256 = "fbf9fa28c9e327f2e64f291e0f9f5d7ed5597f4458339ac63414cf804e9e14f9"


# The Holdings reconciliation and year-end freshness refusals (#1144, #1290).
RELEASES_BEFORE_SELECTED_DATE = (
    "the Releases Report was run before the Holdings Report's Selected Date, so a vest the "
    "Holdings Report counts can be missing from it; export the Releases Report on or after "
    "the Selected Date, for example both reports on the same day"
)
VEST_ON_SELECTED_DATE = (
    "a Releases Report vest is dated on the Holdings Report's Selected Date, so the Holdings "
    "Report may or may not include it; re-export the Vested Share Holdings report with a later "
    "Selected Date, and the Releases Report on or after that new Selected Date, for example "
    "both on the same day"
)
STALE_SELECTED_DATE = (
    "StockPlan year-end freshness: the Holdings Report's Selected Date (the 'Quote History' "
    "sheet's Quote Date) is not strictly after Dec-31 of the calendar year, so it can't show "
    "which vests were still held then; re-export the Vested Share Holdings report with a "
    "Selected Date after year-end, and the Releases Report on or after that Selected Date, "
    "for example both on the same day"
)
# The run, reconciliation and year-end dates of the stale-export tests (#1043).
_STALE_DATES = (
    "15-Nov-2025",
    "2025-11-15",
    "2025-11-14",
    "2025-12-31",
    "14-Jul-2026",
    "2026-07-14",
    "22-Jun-2026",
    "2026-06-22",
)


def test_stale_holdings_export_refuses(tmp_path: Path) -> None:
    # A Holdings snapshot as of a day before Dec-31 can't prove the Dec-31
    # position: a stale export could miss late-CY activity.
    input_dir, cfg, out = _setup(tmp_path)
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", run_on="15-Nov-2025")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == STALE_SELECTED_DATE
    assert_no_markers(str(excinfo.value), *_STALE_DATES)


def test_stale_cg_export_refuses(tmp_path: Path) -> None:
    # A Releases Report sheet run before Dec-31 is run before the fresh
    # Holdings Report's Selected Date, so it can miss a vest the Holdings counts.
    input_dir, cfg, out = _setup(tmp_path)
    _write_cg(input_dir / "IN - Capital Gain_Loss Report.xlsx", releases_run_on="15-Nov-2025")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == RELEASES_BEFORE_SELECTED_DATE
    assert_no_markers(str(excinfo.value), *_STALE_DATES)


def _last_vest_on(day: str) -> list[tuple[str, str, float, float, float]]:
    """The fixture vests with the last one moved to ``day``; the Holdings totals
    still include it."""
    return [*_VESTS[:-1], (day, *_VESTS[-1][1:])]


# Neutral markers planted in valid fields of the year-end freshness inputs (#1144).
_PLAN_MARKER = "Synthetic Plan QX"
_EMPLOYEE_MARKER = "SYNTH-EMP-4471"
_ORDER_MARKER = "SYNTH-ORD-8823"
# The first three fixture vests' totals: a Holdings snapshot before the fourth.
_FIRST_THREE = {
    "shares": sum((Decimal(str(d)) for *_, d in _VESTS[:3]), Decimal(0)),
    "acq": sum((Decimal(str(f)) * Decimal(str(d)) for _, _, f, _, d in _VESTS[:3]), Decimal(0)),
}


@pytest.mark.parametrize(
    ("holdings", "releases_run_on", "vests", "message"),
    [
        pytest.param(
            {"run_on": "31-Dec-2025"},
            "31-Dec-2025",
            _VESTS,
            STALE_SELECTED_DATE,
            id="both-on-dec31",
        ),
        pytest.param(
            {"run_on": "31-Dec-2025"},
            "31-Dec-2025",
            _last_vest_on("2025-12-31"),
            VEST_ON_SELECTED_DATE,
            id="both-on-dec31-vest-on-dec31",
        ),
        pytest.param(
            {"run_on": "30-Dec-2025"},
            "30-Dec-2025",
            _last_vest_on("2025-12-30"),
            VEST_ON_SELECTED_DATE,
            id="both-on-dec30-vest-on-dec30",
        ),
        pytest.param(
            {"run_on": "30-Dec-2025"},
            "01-Jan-2026",
            _last_vest_on("2025-12-31"),
            MISMATCH,
            id="holdings-on-dec30-vest-on-dec31",
        ),
        pytest.param(
            {"run_on": "15-Nov-2025"},
            "22-Jun-2026",
            _last_vest_on("2026-01-14"),
            MISMATCH,
            id="holdings-in-nov-vest-in-jan",
        ),
        pytest.param(
            {"run_on": "31-Dec-2025"},
            "01-Jan-2026",
            _VESTS,
            STALE_SELECTED_DATE,
            id="holdings-on-dec31",
        ),
        pytest.param(
            {"run_on": "01-Jan-2026"},
            "31-Dec-2025",
            _VESTS,
            RELEASES_BEFORE_SELECTED_DATE,
            id="releases-on-dec31",
        ),
        pytest.param(
            {"run_on": "14-Jul-2026", "selected_date": "31-Dec-2025"},
            "22-Jun-2026",
            _VESTS,
            STALE_SELECTED_DATE,
            id="selected-dec31-run-after",
        ),
        pytest.param(
            {"run_on": "14-Jul-2026", "selected_date": "01-Nov-2025", **_FIRST_THREE},
            "22-Jun-2026",
            _VESTS,
            STALE_SELECTED_DATE,
            id="selected-before-a-held-vest",
        ),
    ],
)
def test_holdings_not_as_of_after_year_end_refuses(
    tmp_path: Path,
    holdings: dict[str, Any],
    releases_run_on: str,
    vests: list[tuple[str, str, float, float, float]],
    message: str,
) -> None:
    # A snapshot as of Dec-31 or earlier can miss a vest later that day, or a
    # transfer before year-end, even when it reconciles (#1144). Its Selected
    # Date is never after its run date, and a Releases Report run before it
    # refuses, as does a vest on it (#1290). A Releases Report can't list a
    # vest after its own run date (#1176), so a stale one here lists its last
    # vest on its run date at the latest.
    marked = [(vests[0][0], _ORDER_MARKER, *vests[0][2:]), *vests[1:]]
    input_dir, cfg, out = _setup(
        tmp_path, vests=marked, releases_run_on=releases_run_on, releases_prefix=_PLAN_MARKER
    )
    _write_holdings(
        input_dir / "Vested Share Holdings.xlsx",
        title_prefix=_PLAN_MARKER,
        employee=_EMPLOYEE_MARKER,
        **holdings,
    )
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == message
    assert not out.exists()
    run_dates = (
        holdings["run_on"],
        holdings.get("selected_date", holdings["run_on"]),
        releases_run_on,
        "22-Jun-2026",
    )
    assert_no_markers(
        str(excinfo.value),
        _PLAN_MARKER,
        _EMPLOYEE_MARKER,
        _ORDER_MARKER,
        *run_dates,
        *(_dt.datetime.strptime(day, "%d-%b-%Y").date().isoformat() for day in run_dates),
        *(vest[0] for vest in vests),
    )


def test_vest_after_the_run_date_refuses_although_a_prefix_reconciles(tmp_path: Path) -> None:
    # The future vest is outside CY 2025 and the four earlier vests reconcile
    # Holdings, but the Releases Report lists a vest after its own run (#1176).
    input_dir, cfg, out = setup_marked_exports(tmp_path, vests=[*_VESTS, SP_FUTURE_VEST])
    out.write_text("stale")
    with pytest.raises(ParseError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == FUTURE_VEST_REFUSAL.format(row=6)
    assert excinfo.value.raw_value == ""
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert not out.exists()
    assert_no_markers(str(excinfo.value), *SP_MARKERS)


@pytest.mark.parametrize(
    ("holdings", "releases_run_on", "vests", "row"),
    [
        pytest.param(
            {"run_on": "30-Dec-2025"},
            "30-Dec-2025",
            _last_vest_on("2025-12-31"),
            5,
            id="stale-both-vest-on-dec31",
        ),
        pytest.param(
            {"run_on": "15-Nov-2025"},
            "15-Nov-2025",
            _last_vest_on("2026-01-14"),
            5,
            id="stale-both-vest-in-jan",
        ),
        pytest.param(
            {"shares": _HELD_SHARES + 1},
            "22-Jun-2026",
            [*_VESTS, SP_FUTURE_VEST],
            6,
            id="reconciliation-mismatch",
        ),
    ],
)
def test_vest_after_the_run_date_refuses_before_freshness_and_reconciliation(
    tmp_path: Path,
    holdings: dict[str, Any],
    releases_run_on: str,
    vests: list[tuple[str, str, float, float, float]],
    row: int,
) -> None:
    # The parser refuses before the year-end freshness (#1144) and Holdings
    # reconciliation checks run, so its refusal wins over theirs (#1176).
    input_dir, cfg, out = _setup(tmp_path, vests=vests, releases_run_on=releases_run_on)
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", **holdings)
    with pytest.raises(ParseError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == FUTURE_VEST_REFUSAL.format(row=row)


def test_reports_run_after_year_end_are_accepted(tmp_path: Path) -> None:
    # Both reports run on Jan-1 are fresh enough, and the CSV is the happy path's.
    input_dir, cfg, out = _setup(tmp_path, releases_run_on="01-Jan-2026")
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", run_on="01-Jan-2026")
    report = _run(input_dir, cfg, out)
    assert report.rows_written == 4
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _HAPPY_PATH_SHA256


def test_reads_the_releases_sheet_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The Releases parser returns its run date, so the year-end freshness check
    # no longer loads the sheet a second time (#1071).
    input_dir, cfg, out = _setup(tmp_path)
    load = _xlsx.load_sheet_rows
    sheets: list[str] = []

    def recording_load(path: Path, sheet: str, **kwargs: Any) -> list[tuple[object, ...]]:
        sheets.append(sheet)
        return load(path, sheet, **kwargs)

    monkeypatch.setattr(_xlsx, "load_sheet_rows", recording_load)
    _run(input_dir, cfg, out)
    assert sheets == [
        "Releases Report",
        "Capital Gains Report",
        "Holdings Report",
        "Quote History",
        "IN - Dividend Report",
    ]


def test_calendar_year_out_of_range_refuses_without_the_year(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    with pytest.raises(StockplanFaError) as excinfo:
        run_stockplan_fa(
            input_dir=input_dir,
            config_path=cfg,
            output_path=out,
            calendar_year=2187,
            fetch_prices=False,
        )
    assert str(excinfo.value) == "calendar year is out of range [1990, 2099]"
    assert "2187" not in str(excinfo.value)


def test_reconciliation_mismatch_refuses(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    # Corrupt the Holdings anchor so cumulative vests can't reconcile.
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", shares=Decimal("99.0"))
    with pytest.raises(StockplanFaError, match="do not reconcile") as excinfo:
        _run(input_dir, cfg, out)
    assert not out.exists()  # cleared up front, nothing written
    assert_no_markers(str(excinfo.value), "99.0", str(_HELD_ACQ), "16.158")


def test_sales_refuse(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2025, 11, 1),
        300.0,
        1.0,
        "GOOG",
        300.0,
        _dt.datetime(2025, 8, 14),
    ]
    input_dir, cfg, out = _setup(tmp_path, sales=[sale])
    with pytest.raises(StockplanFaError, match="does not yet handle intra-year"):
        _run(input_dir, cfg, out)


def test_incomplete_fa_config_refuses(tmp_path: Path) -> None:
    # Strip the FA fields -> income-only [stockplan.security].
    config = "\n".join(
        line
        for line in CONFIG.splitlines()
        if not line.startswith(("entity_", "listing_", "issuer_", "nature_"))
    )
    input_dir, cfg, out = _setup(tmp_path, config=config)
    with pytest.raises(StockplanFaError, match=r"incomplete \[stockplan.security\]") as exc:
        _run(input_dir, cfg, out)
    assert "sub_category" not in str(exc.value)
    assert "asset_class" not in str(exc.value)


@pytest.mark.parametrize(("key", "value"), [("asset_class", "STK"), ("sub_category", "COMMON")])
def test_removed_lot_config_key_refuses(tmp_path: Path, key: str, value: str) -> None:
    # #629: no lot field reads these keys any more, so an old config that
    # still sets one is refused by name, and a prior run's CSV is cleared.
    config = CONFIG.replace(
        'listing_exchange = "NASDAQ"\n', f'listing_exchange = "NASDAQ"\n{key} = "{value}"\n'
    )
    input_dir, cfg, out = _setup(tmp_path, config=config)
    out.write_text("STALE")
    with pytest.raises(ConfigError) as exc:
        _run(input_dir, cfg, out)
    assert exc.value.field == f"stockplan.security.{key}"
    assert exc.value.message == "is no longer used; delete it from [stockplan.security]"
    assert not out.exists()


def test_no_fetch_prices_refuses(tmp_path: Path) -> None:
    # With no price series (and no broker Dec-31 total), the Dec-31 close can't be
    # resolved — the run REFUSES rather than guess a closing value.
    input_dir, cfg, out = _setup(tmp_path)
    with pytest.raises(IbkrFaError):
        run_stockplan_fa(
            input_dir=input_dir,
            config_path=cfg,
            output_path=out,
            calendar_year=2025,
            fetch_prices=False,
            price_cache_dir=input_dir / ".cache",
        )
    assert not out.exists()


def test_cli_no_usable_close_names_no_netbenefits_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1116: StockPlan has no manual Dec-31 price, so the refusal asks for a
    # Yahoo close and never sends the user to [[netbenefits.price_overrides]].
    input_dir, cfg, out = _setup(tmp_path)
    out.write_text("stale")

    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: no Dec-31 close available for 2025-12-31: there is no statement total, no "
        "override and no usable Yahoo close; StockPlan has no manual Dec-31 price setting, so "
        "the run needs a usable Yahoo close, fetched or from a valid price cache; if the Yahoo "
        f"symbol is wrong, check [[prices.ticker_overrides]]\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert "netbenefits.price_overrides" not in captured.err


def test_split_refusal_carries_no_stockplan_remedy(tmp_path: Path) -> None:
    # #1116: the remedy belongs to the no-source refusal only.
    def split_fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        series = stub_fetcher(ticker, start=start, end=end, now=now)
        return replace(series, has_split_in_range=True)

    input_dir, cfg, out = _setup(tmp_path)
    with pytest.raises(ClosingPriceError) as exc:
        _run(input_dir, cfg, out, price_fetcher=split_fetcher)
    assert str(exc.value) == (
        "split in range for 2025-12-31: Yahoo and any per-share override (x the current, "
        "rebased quantity) are unsafe and there is no broker statement total to fall back on; "
        "the price cache holds this security's closes on the current-split basis, so move its "
        "cache file, and any .lock beside it, out of the price cache directory and rerun with "
        "price fetching on"
    )
    assert not out.exists()


# --- required US market session (#1165) ------------------------------------------


def test_prior_session_close_only_refuses(tmp_path: Path) -> None:
    # Both reports run after Dec-31 (#1144), so only the price can refuse:
    # 2025-12-31 is a session, and the Dec-30 close is not its close.
    input_dir, cfg, out = _setup(tmp_path, config=MARKED_CONFIG)
    out.write_text("stale")
    with pytest.raises(ClosingPriceError) as excinfo:
        _run(input_dir, cfg, out, price_fetcher=_prior_session_only)
    assert str(excinfo.value) == MARKET_SESSION_REFUSAL
    assert not out.exists()
    assert_markers_absent(excinfo.value, *MARKERS_IN_INPUT)


def test_filed_close_is_the_dec31_session_close(tmp_path: Path) -> None:
    # Control: a different, lower Dec-30 close changes nothing filed; the CSV is
    # the happy path's, closing at the Dec-31 session close.
    input_dir, cfg, out = _setup(tmp_path)
    fetched: list[PriceSeries] = []

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        fetched.append(_marked_dec30_fetcher(dec31_bar=True)(ticker, start=start, end=end, now=now))
        return fetched[-1]

    report = _run(input_dir, cfg, out, price_fetcher=fetcher)
    assert report.rows_written == 4
    assert report.peak_blank_reasons == ()
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _HAPPY_PATH_SHA256
    [series] = fetched
    with out.open(encoding="utf-8-sig") as handle:
        closes = {row["_audit: Closing price on Dec 31 (USD)"] for row in csv.DictReader(handle)}
    assert closes == {str(series.closes[_DEC31])}


# ---- #1250: a Yahoo Dec-31 close files its exact closing values ------------

# A 30-significant-digit Dec-31 close in place of the stub's $266.75. For the
# 6.214-share lot, 6.214 x close x 89.47 is 148304.49999999999999999999999959119240.
_LONG_DEC31_CLOSE = "266.750746061031222416282647780"


def test_long_yahoo_close_files_the_exact_closing_value(tmp_path: Path) -> None:
    """#1250: the Yahoo close goes through the exact Dec-31 total and closing
    products, so the 6.214-share lot files ₹148,304. Main rounded 6.214 x close
    to 28 digits, reached 148304.5 and filed ₹148,305. The other lots are
    ordinary and file as on main. #1260: the lot peaks on Dec-31 at the same
    close, through the exact daily peak products, so its peak is ₹148,304 too.
    Main's 28-digit peak products filed ₹148,305, a rupee above the closing
    balance."""
    input_dir, cfg, out = _setup(tmp_path)

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        series = stub_fetcher(ticker, start=start, end=end, now=now)
        return replace(series, closes={**series.closes, _DEC31: Decimal(_LONG_DEC31_CLOSE)})

    report = _run(input_dir, cfg, out, price_fetcher=fetcher)

    assert report.rows_written == 4
    with out.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["_audit: Closing price on Dec 31 (USD)"] for row in rows} == {_LONG_DEC31_CLOSE}
    filed = ["148304", "74152", "88997", "74176"]
    assert [row["Closing balance"] for row in rows] == filed
    assert [row["Peak value of investment during the Period"] for row in rows] == filed


# ---- #1314: each held lot's cost is the exact Price x Net Share Proceeds ----

# (b) with the digit run 91827364 in the Price's places, which a 28-digit
# rounding keeps. Its initial value rounds to ₹100,000,000,000,002, past the
# bound on main too.
_PRICE_MARKER = "91827364"
_MARKED_PRICE = f"574052812.8587{_PRICE_MARKER}67508611"
BOUND_REFUSAL = (
    "InvariantError: FaA3Row.initial_value_inr must round to a whole-rupee amount within "
    "±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)"
)


@pytest.mark.parametrize(
    ("example", "filed", "cost"),
    [
        ("a", "21506173498", "246913587.81285878300803673938"),
        ("a_control", "21506173499", "246913587.8128587830080367394"),
        ("c", "99999999999999", "1168907071887.77907656341320852000"),
    ],
)
def test_held_lot_files_its_exact_cost(tmp_path: Path, example: str, filed: str, cost: str) -> None:
    input_dir, cfg, out = setup_one_vest(tmp_path, *LONG_COST_VESTS[example])
    assert _run(input_dir, cfg, out).rows_written == 1
    row = a3_row(out)
    assert (row[INITIAL_VALUE], row[AUDIT_COST]) == (filed, cost)


def test_held_lot_past_the_bound_refuses(tmp_path: Path) -> None:
    input_dir, cfg, out = setup_one_vest(tmp_path, *LONG_COST_VESTS["b"])
    with pytest.raises(InvariantError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == BOUND_REFUSAL
    assert not out.exists()


@pytest.mark.parametrize(
    ("example", "filed"),
    [("a", "21506173498"), ("a_control", "21506173499"), ("c", "99999999999999")],
)
def test_cli_held_lot_files_its_exact_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    example: str,
    filed: str,
) -> None:
    input_dir, cfg, out = setup_one_vest(tmp_path, *LONG_COST_VESTS[example])
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    assert _main_fa(input_dir, cfg, out) == 3
    assert "error:" not in capsys.readouterr().err
    assert a3_row(out)[INITIAL_VALUE] == filed


def test_cli_held_lot_past_the_bound_refuses_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir, cfg, out = setup_one_vest(tmp_path, *LONG_COST_VESTS["b"])
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    out.write_text("stale")
    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {BOUND_REFUSAL}\n{_STOCKPLAN_CONTRACT_POINTER}\n"


def test_held_lot_past_the_bound_echoes_no_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir, cfg, out = setup_one_vest(tmp_path, "2025-08-14", _MARKED_PRICE, "2000")
    with pytest.raises(InvariantError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == BOUND_REFUSAL
    assert_markers_absent(excinfo.value, _PRICE_MARKER)
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    assert _main_fa(input_dir, cfg, out) == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {BOUND_REFUSAL}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    assert_no_markers(captured.out + captured.err, _PRICE_MARKER)


def test_unsupported_calendar_year_refuses_per_share_pricing(tmp_path: Path) -> None:
    # The fixture moved to CY2024 with every other check passing: the calendar
    # lists no 2024 closures but Christmas, so no Yahoo close can be priced.
    # Before #1165 the close was priced and the run refused only at the writer,
    # which has no portal labels for AY 2025-26.
    vests = [(day.replace("2025-", "2024-"), *rest) for day, *rest in _VESTS]
    input_dir, cfg, out = _setup(
        tmp_path,
        config=_config(account_opening="2024-07-14", stockplan_opening="2024-07-14"),
        vests=vests,
        period="02-Jun-2024 to 04-May-2026",
    )
    out.write_text("stale")
    with pytest.raises(ClosingPriceError) as excinfo:
        _run(input_dir, cfg, out, calendar_year=2024)
    assert str(excinfo.value) == unsupported_year_refusal(2024)
    assert not out.exists()


def test_cli_prior_session_close_only_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The refusal, then the StockPlan contract pointer every refused run prints.
    input_dir, cfg, out = _setup(tmp_path, config=MARKED_CONFIG)
    out.write_text("stale")
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", PRIOR_SESSION_ONLY_PIPELINE)
    code = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )
    assert code == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {MARKET_SESSION_REFUSAL}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    assert_no_markers(captured.out + captured.err, *MARKERS_IN_INPUT)


def test_dividend_coverage_not_spanning_cy_refuses(tmp_path: Path) -> None:
    # A dividend report that doesn't span the whole CY could miss a dividend from
    # the A3 "amount credited" column — refuse.
    input_dir, cfg, out = _setup(tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IN - Dividend Report"
    ws.append(
        [
            "Alphabet, Inc. - IN - Dividend Report (report run on 22-Jun-2026 for "
            "reporting period 01-Jan-2025 to 30-Jun-2025)"
        ]
    )  # only H1 2025
    ws.append(DIVIDEND_REPORT_HEADER)
    ws.append([_dt.datetime(2025, 3, 15), "Dividend Paid", 1.0, "GOOG", None, None, 0.0])
    ws.append([_USD])
    wb.save(input_dir / "IN - Dividend Report.xlsx")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == (
        "IN - Dividend Report.xlsx's reporting period does not span the calendar year; a "
        "dividend could be missing from the A3 'amount credited' column"
    )
    assert_no_markers(
        str(excinfo.value), "01-Jan-2025", "2025-01-01", "30-Jun-2025", "2025-06-30", "2025-12-31"
    )


def test_early_failure_clears_stale_output(tmp_path: Path) -> None:
    # A missing [stockplan] config must clear a prior run's CSV (clear-before-compute).
    input_dir, cfg, out = _setup(
        tmp_path,
        config="[account]\nnumber='X'\nholder_name='Y'\n"
        "opening_date=2025-07-14\nstatus='BeneficialOwner'\n",
    )
    out.write_text("STALE")
    with pytest.raises(StockplanFaError, match=r"no \[stockplan\] block"):
        _run(input_dir, cfg, out)
    assert not out.exists()


def test_partial_write_failure_leaves_no_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mid-write failure must not leave a partial CSV behind.
    input_dir, cfg, out = _setup(tmp_path)

    def _boom(_subcommand: object, _fmt: str):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fa_mod, "output_binding", _boom)
    with pytest.raises(RuntimeError, match="disk full"):
        _run(input_dir, cfg, out)
    assert not out.exists()


def test_cli_stockplan_fa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _input_dir, cfg, out = _setup(tmp_path)
    # The CLI path fetches real prices; run with --no-fetch-prices would refuse on
    # the Dec-31 close, so exercise the dispatch via the happy path is done in the
    # pipeline test. Here assert the subcommand is wired + arg parsing works by
    # driving a refusal (missing input dir) through the CLI exit-code mapping.
    code = main(
        [
            "stockplan-fa",
            "--input",
            str(tmp_path / "nope"),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert code == 4  # domain error (no input dir)
    assert "error:" in capsys.readouterr().err


def test_cli_success_is_manual_fill_while_a2_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = fa_mod.StockplanFaRunReport(
        output_path=tmp_path / "schedule-fa.csv",
        calendar_year=2025,
        rows_written=1,
        held_shares=Decimal("1"),
        reconciled_as_of=_dt.date(2025, 12, 31),
        peak_blank_reasons=(),
        dividend_inr=Decimal("0"),
        warnings=(),
        run_outputs=RunOutputs((), ()),
    )
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", lambda **_kwargs: report)

    code = main(
        [
            "stockplan-fa",
            "--input",
            str(tmp_path),
            "--config",
            str(tmp_path / "user.toml"),
            "--output",
            str(report.output_path),
            "--calendar-year",
            "2025",
        ]
    )

    assert code == 3
    captured = capsys.readouterr()
    assert "Table A2: MANUAL-FILL" in captured.out
    assert "docs/stockplan-contract.md" not in captured.err


def test_cli_names_a_missing_session_close_as_the_blank_peak_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#962: the series lacks Monday 2025-12-29, a session every held lot's
    window spans, so each peak is blank. The summary names that cause, not a
    re-fetch."""
    input_dir, cfg, out = _setup(tmp_path)

    def fetcher(ticker: Ticker, *, start, end, now, fetch_symbol=None):
        series = stub_fetcher(ticker, start=start, end=end, now=now)
        missing = _dt.date(2025, 12, 29)
        return replace(
            series,
            closes={day: close for day, close in series.closes.items() if day != missing},
            highs={day: high for day, high in series.highs.items() if day != missing},
        )

    monkeypatch.setattr(
        "ibkr_fa.cli.run_stockplan_fa",
        functools.partial(
            run_stockplan_fa,
            price_fetcher=fetcher,
            price_now=_dt.datetime(2026, 7, 15, tzinfo=_dt.UTC),
        ),
    )
    code = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(cfg)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert code == 3
    printed = capsys.readouterr().out
    assert (
        "4 A3 Peak value(s): MANUAL-FILL — the peak is blank because a US market session "
        "in the holding window has no close"
    ) in printed
    assert "A ticker override or a re-fetch won't help." in printed
    assert "Re-run with price fetching" not in printed


def test_cli_schema_refusal_is_exit_4_and_clears_stale_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    out.write_text("stale")
    cg_path = input_dir / "IN - Capital Gain_Loss Report.xlsx"
    wb = openpyxl.load_workbook(cg_path)
    wb.create_sheet("New Transactions")
    wb.save(cg_path)

    code = main(
        [
            "stockplan-fa",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )

    assert code == 4
    assert not out.exists()
    err = capsys.readouterr().err
    assert "unexpected sheet" in err
    assert err.splitlines()[-1] == _STOCKPLAN_CONTRACT_POINTER


def test_cli_cell_refusal_does_not_print_the_cell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A holder's name in a numeric Holdings cell refuses with exit 4, naming the
    # sheet, row and column but never the cell (#1043).
    input_dir, cfg, out = _setup(tmp_path)
    holdings_path = input_dir / "Vested Share Holdings.xlsx"
    wb = openpyxl.load_workbook(holdings_path)
    wb["Holdings Report"].cell(row=4, column=4, value="Synthetic Holder Two")
    wb.save(holdings_path)

    code = main(
        [
            "stockplan-fa",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )

    assert code == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert (
        "error: ParseError in section 'Holdings Report' at row 3, column 'Number of Shares': "
        "non-numeric Number of Shares\n"
    ) in captured.err
    assert_no_markers(captured.out + captured.err, "Synthetic Holder Two")


@pytest.mark.parametrize("run_on", ["31-Feb-2026", "01-Xyz-2026"])
@pytest.mark.parametrize(
    "prefix", ["Synthetic Issuer", "Synthetic Issuer (report run on 01-Jan-2099)"]
)
def test_cli_malformed_releases_run_date_refuses_without_the_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], run_on: str, prefix: str
) -> None:
    # A Releases Report title whose run date isn't a real date refuses; it no
    # longer counts as unknown and leaves freshness to the vests (#1071).
    input_dir, cfg, out = _setup(tmp_path, releases_run_on=run_on, releases_prefix=prefix)
    out.write_text("stale")

    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Releases Report' at row 0: malformed Releases Report "
        f"date in IN - Capital Gain_Loss Report.xlsx\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, "31-Feb-2026", "Xyz", "2026", "2099")


def test_cli_sheet_scan_crash_is_an_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A crash in the shared sheet scan is not a malformed export: the CLI prints
    # its class and source line, never its text, and exits 1 (#1071).
    input_dir, cfg, out = _setup(tmp_path)

    def parse(_parser: WorkSheetParser) -> NoReturn:
        raise RuntimeError("Synthetic Holder Two")

    monkeypatch.setattr(WorkSheetParser, "parse", parse)
    lines = Path(ibkr_fa.xlsx_checks.__file__).read_text(encoding="utf-8").splitlines()
    [site] = [n for n, line in enumerate(lines, start=1) if "in parser.parse():" in line]

    assert _main_fa(input_dir, cfg, out) == 1
    captured = capsys.readouterr()
    assert (
        captured.err == f"error: internal error (RuntimeError at ibkr_fa/xlsx_checks.py:{site})\n"
    )
    assert_no_markers(captured.out + captured.err, "Synthetic Holder Two")
    assert not out.exists()


def test_cli_formula_sale_refuses_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A sale row of formulas with no stored values once read as a blank row, so
    # "No records found" proved zero sales; it now refuses as a formula (#1089).
    input_dir, cfg, out = _setup(tmp_path)
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    assert _main_fa(input_dir, cfg, out) == 3
    assert out.exists()
    capsys.readouterr()

    marker = '=IF(FALSE,"Synthetic Holder Two Synthetic Marker Q",1)'
    _write_cg(
        input_dir / "IN - Capital Gain_Loss Report.xlsx",
        sales=[["No records found"], [marker] * 6],
    )

    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Capital Gains Report' at row 4, column '#1': "
        "IN - Capital Gain_Loss Report.xlsx stores a formula in this cell; only stored values "
        f"are supported. Re-export the report.\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, "Synthetic Holder Two", "Synthetic Marker Q")


def test_vest_that_left_before_year_end_refuses(tmp_path: Path) -> None:
    input_dir, cfg, out = setup_marked_exports(tmp_path, vests=LEFT_VESTS, **LEFT_HOLDINGS)
    out.write_text("stale")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == MISMATCH
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert not out.exists()
    assert_no_markers(str(excinfo.value), *LEFT_MARKERS)


@pytest.mark.parametrize("later_date", ["2025-10-14", "2026-01-14"])
def test_vest_the_context_would_round_away_still_counts(tmp_path: Path, later_date: str) -> None:
    # The vest sums are exact, so the tiny vest counts though 28 digits can't
    # hold it, and the Holdings totals, which leave it out, don't reconcile.
    vests = [
        ("2025-07-15", SP_ORDER, 1.00, 100000000.00, 100000000.00),
        (later_date, "SYNTH-ORDER-SP02", 1.00, 1e-20, 1e-20),
    ]
    input_dir, cfg, out = setup_marked_exports(
        tmp_path, vests=vests, shares=Decimal("100000000.00"), acq=Decimal("100000000.00")
    )
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == MISMATCH
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert not out.exists()
    assert_no_markers(str(excinfo.value), *SP_MARKERS[:4], *(vest[0] for vest in vests))


def test_vest_after_year_end_at_another_value_refuses(tmp_path: Path) -> None:
    # Control: with vest B at another value, no set of vests matched before #1290 either.
    input_dir, cfg, out = setup_marked_exports(
        tmp_path, vests=[VEST_A, (*VEST_B[:2], 101.0, *VEST_B[3:])], shares=1, acq=Decimal(101)
    )
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == MISMATCH


@pytest.mark.parametrize(
    ("vests", "holdings", "message"),
    [
        pytest.param(
            _SP_VESTS,
            {"run_on": "14-Jul-2026"},
            RELEASES_BEFORE_SELECTED_DATE,
            id="releases-run-before-the-selected-date",
        ),
        pytest.param(
            [*_SP_VESTS, ("2026-06-22", "SYNTH-ORDER-SP02", 137.2468, 2.0, 1.0)],
            {"shares": _HELD_SHARES + 1, "acq": _HELD_ACQ + Decimal("137.2468")},
            VEST_ON_SELECTED_DATE,
            id="vest-on-the-selected-date",
        ),
    ],
)
def test_selected_date_rule_refuses(
    tmp_path: Path,
    vests: list[tuple[str, str, float, float, float]],
    holdings: dict[str, Any],
    message: str,
) -> None:
    # Before #1290 each of these passed: the Releases Report run before the
    # Holdings Report's Selected Date, and a vest on that date that the Holdings
    # totals include. The vest is after Dec-31, so it would not be held.
    input_dir, cfg, out = setup_marked_exports(tmp_path, vests=vests, **holdings)
    out.write_text("stale")
    with pytest.raises(StockplanFaError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert not out.exists()
    assert_no_markers(
        str(excinfo.value),
        *SP_MARKERS[:4],
        *(vest[0] for vest in vests),
        holdings.get("run_on", "22-Jun-2026"),
    )


def test_cli_vest_that_left_before_year_end_refuses_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With the year-end close cached, main wrote A3 for vest A here.
    input_dir, cfg, out = setup_marked_exports(tmp_path, vests=LEFT_VESTS, **LEFT_HOLDINGS)
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    out.write_text("stale")

    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {MISMATCH}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
    assert_no_markers(captured.out + captured.err, *LEFT_MARKERS)


def test_cli_vest_after_the_run_date_refuses_and_clears_stale_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real parser refusal is exit 4 with no success report and no FA output (#1176).
    input_dir, cfg, out = setup_marked_exports(tmp_path, vests=[SP_FUTURE_VEST, *_VESTS])
    out.write_text("stale")

    assert _main_fa(input_dir, cfg, out) == 4
    assert not out.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: {FUTURE_VEST_REFUSAL.format(row=2)}\n"
        "See docs/stockplan-contract.md for the StockPlan exports ibkr-fa accepts, what each "
        "refusal means, and how to report an export it has not seen.\n"
    )
    assert_no_markers(captured.out + captured.err, *SP_MARKERS)
