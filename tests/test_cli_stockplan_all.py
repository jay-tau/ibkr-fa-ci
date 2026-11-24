"""Tests for the ``stockplan-all`` orchestrator (#365).

The per-command handlers are tested in their own suites; here we pin the
ORCHESTRATION: it runs income + FA against one export directory, builds each
sub-command's namespace correctly (FA is calendar-year based; income is FY
based), continues past a per-command failure, aggregates the worst exit code,
and prints a status-first summary. The handlers are stubbed so this doesn't
re-run the pipelines.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest

import ibkr_fa.cli
from ibkr_fa.cli import (
    _EXIT_DOMAIN_ERROR,
    _EXIT_MANUAL_FILL_REQUIRED,
    _EXIT_OK,
    _RUN_ALL_RULE,
    _RUN_ALL_STATUS_LABEL,
    _SOURCE_NOT_REQUIRED,
    _STOCKPLAN_CONTRACT_POINTER,
)
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_stockplan_fa import StockplanCgEvidenceReport, StockplanFaError
from ibkr_fa.pipeline_stockplan_income import _ZERO_SALES_UNPROVEN
from tests._config_toml import NON_UTF8_TOML
from tests._crash_reports import assert_no_markers, crash_pattern
from tests._dec31_close import MARKET_SESSION_REFUSAL
from tests._form67_overrides import FORM67_READY_OVERRIDE as _READY_OVERRIDE
from tests._ftc_warnings import BLANK_RELIEF_TIN_WARNING
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML
from tests._oserror_markers import assert_no_oserror_markers as _assert_no_markers
from tests._oserror_markers import marked_oserror as _marked_oserror
from tests._source_all import INCOME_OUTPUTS as _INCOME
from tests._source_all import STOCKPLAN_ALL_OK as _ALL_OK
from tests._source_all import STOCKPLAN_ALL_OUTPUTS as _ALL_OUTPUTS
from tests._source_all import SUMMARY_FOOTER as _SUMMARY_FOOTER
from tests._source_all import patch_stockplan_handlers as _patch_handlers
from tests._source_all import real_stockplan_cg_status as _real_stockplan_cg_status
from tests._source_all import real_stockplan_fa as _real_stockplan_fa
from tests._source_all import real_stockplan_form67_readiness as _real_stockplan_form67_readiness
from tests._source_all import real_stockplan_income as _real_stockplan_income
from tests._source_all import run_stockplan_all as _run
from tests._source_all import run_stockplan_all_real_income as _run_real_income
from tests._source_all import write_stockplan_promised as _write_promised
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
    a3_row,
    offline_pipeline,
    setup_marked_exports,
    setup_one_vest,
)
from tests._stockplan_fa_exports import STOCKPLAN_FA_CONFIG as FA_CONFIG
from tests._stockplan_fa_exports import STOCKPLAN_VESTS as FA_VESTS
from tests._stockplan_fa_exports import setup_stockplan_exports as _setup_fa_exports
from tests._stockplan_income import STOCKPLAN_INCOME_CONFIG as INCOME_CONFIG


def test_runs_both_and_creates_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The A2 summary line and a proven-unneeded Schedule CG remove nothing.
    rc, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert set(calls) == {"income", "fa", "cg"}
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert capsys.readouterr().err.splitlines()[-1] == _SUMMARY_FOOTER


def test_worst_exit_code_wins_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # income errors; FA succeeds. The run continues, worst code wins, and the
    # run leaves no output.
    cache = tmp_path / "cache"
    cache.mkdir()
    cached_price = cache / "price.csv"
    cached_price.write_bytes(b"cached price")
    rc, calls, out = _run(
        tmp_path,
        monkeypatch,
        {"income": _EXIT_DOMAIN_ERROR, "fa": _EXIT_OK, "cg": _SOURCE_NOT_REQUIRED},
        extra=("--price-cache-dir", str(cache)),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"income", "fa", "cg"}  # continued past the failure
    assert list(out.iterdir()) == []
    assert calls["fa"].price_cache_dir == cache
    assert cached_price.read_bytes() == b"cached price"
    lines = capsys.readouterr().err.splitlines()
    assert (
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-fa  (output removed because the run failed)"
        in lines
    )
    assert "  not required (zero sales proven)  Schedule CG" in lines


def test_manual_fill_aggregates_when_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _, out = _run(
        tmp_path,
        monkeypatch,
        {
            "income": _EXIT_OK,
            "fa": _EXIT_MANUAL_FILL_REQUIRED,
            "cg": _SOURCE_NOT_REQUIRED,
        },
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert capsys.readouterr().err.splitlines()[-1] == _SUMMARY_FOOTER


def test_unclearable_target_stops_before_every_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    target = out / "schedule-os.csv"
    target.mkdir(parents=True)
    for names in ibkr_fa.cli._STOCKPLAN_ALL_TARGETS.values():
        for name in names:
            if name != target.name:
                (out / name).write_text("stale", encoding="utf-8")
    rc, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == [target]
    captured = capsys.readouterr()
    # The errno name, not its strerror (#1048).
    assert f"error: could not remove the previous output {target} (EISDIR)" in (
        captured.err.splitlines()
    )
    assert "is a directory" not in captured.err.lower()
    assert captured.out == ""


def test_cg_error_overrides_a2_manual_fill_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sales or unproven CG evidence fail the run, so no income or FA output stays.
    rc, calls, out = _run(
        tmp_path,
        monkeypatch,
        {"income": _EXIT_OK, "fa": _EXIT_OK, "cg": _EXIT_DOMAIN_ERROR},
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"income", "fa", "cg"}
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("code", [_EXIT_OK, _EXIT_MANUAL_FILL_REQUIRED])
def test_success_without_its_output_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    calls = _patch_handlers(monkeypatch, _ALL_OK)

    def fa_without_output(args: argparse.Namespace) -> int:
        calls["fa"] = args
        return code

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", fa_without_output)
    rc, _, out = _run(tmp_path, monkeypatch, _ALL_OK, patch=False)
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"income", "fa", "cg"}
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert f"exited {code} without its promised output" in err
    assert str(out / "schedule-fa.csv") in err
    assert f"  {_RUN_ALL_STATUS_LABEL['error']}  stockplan-fa" in err.splitlines()


def test_income_debug_outputs_are_promised_under_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fake income leg writes only the four filing CSVs.
    rc, _, out = _run(tmp_path, monkeypatch, _ALL_OK, extra=("--debug",))
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    assert str(out / "schedule-os-debug.csv") in capsys.readouterr().err


@pytest.mark.parametrize(
    ("income_code", "expected"),
    [(_EXIT_DOMAIN_ERROR, _EXIT_DOMAIN_ERROR), (None, ibkr_fa.cli._EXIT_GENERIC)],
    ids=["domain-error", "crash"],
)
def test_failed_removal_names_the_file_and_removes_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    income_code: int | None,
    expected: int,
) -> None:
    """The fault hits only the removal after the last leg, not the clearing
    before the legs run."""
    calls = _patch_handlers(monkeypatch, {**_ALL_OK, "income": income_code or _EXIT_OK})
    if income_code is None:

        def crash(args: argparse.Namespace) -> int:
            calls["income"] = args
            raise RuntimeError("boom")

        monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", crash)
    finished: list[bool] = []
    cg = ibkr_fa.cli._run_stockplan_cg_status

    def last_leg(args: argparse.Namespace) -> int:
        finished.append(True)
        return cg(args)

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", last_leg)
    stuck = tmp_path / "out" / "schedule-fa.csv"
    unlink = Path.unlink

    def unlink_fails_after_the_run(path: Path, missing_ok: bool = False) -> None:
        if finished and path == stuck:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_fails_after_the_run)
    rc, _, out = _run(tmp_path, monkeypatch, _ALL_OK, patch=False)
    assert rc == expected
    assert set(calls) == {"income", "fa", "cg"}
    assert [path.name for path in out.iterdir()] == [stuck.name]
    err = capsys.readouterr().err
    assert (
        f"error: the run failed, and its output may still be there: could not remove {stuck} "
        "(EACCES)" in err.splitlines()
    )
    _assert_no_markers(err)
    assert (
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-fa  (the run failed: do NOT file this output)"
        in err.splitlines()
    )


def test_fa_namespace_is_calendar_year_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    fa = calls["fa"]
    assert fa.output == out / "schedule-fa.csv"
    assert fa.calendar_year == 2025  # FY 2025-26 -> CY 2025


def test_income_namespace_is_fy_based(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    assert calls["income"].output_dir == out
    assert calls["income"].financial_year.start_year == 2025
    assert calls["income"].itr_form == "ITR-2"


def test_passthroughs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_handlers(monkeypatch, _ALL_OK)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    out = tmp_path / "out"
    ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
            "--price-cache-dir",
            str(tmp_path / "cache"),
            "--suppress-cross-ay-warning",
            "--debug",
        ]
    )
    assert calls["fa"].no_fetch_prices is True
    assert calls["fa"].price_cache_dir == tmp_path / "cache"
    assert calls["income"].suppress_cross_ay_warning is True
    assert calls["income"].debug is True


def test_crash_in_handler_continues_and_returns_generic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A crash (Exception) in one leg does not stop the other leg.
    out = tmp_path / "out"

    def income_crashes(args: argparse.Namespace) -> int:
        raise ValueError("simulated crash")

    def fa_ok(args: argparse.Namespace) -> int:
        _write_promised("fa", args)
        return 0

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", income_crashes)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", fa_ok)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", lambda _args: _SOURCE_NOT_REQUIRED)
    monkeypatch.setattr(
        "ibkr_fa.cli.stockplan_form67_readiness", lambda **_kwargs: Form67Readiness.READY
    )
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == ibkr_fa.cli._EXIT_GENERIC
    captured = capsys.readouterr()
    # The class and source line, never the message (#1049).
    assert re.search(
        rf"^error: stockplan-all: stockplan-income crashed unexpectedly "
        rf"\({crash_pattern(ValueError)}\)$",
        captured.err,
        re.MULTILINE,
    )
    assert "simulated crash" not in captured.err
    assert "docs/stockplan-contract.md" not in captured.err
    assert "stockplan-all: stockplan-fa" in captured.out  # FA still ran
    assert list(out.iterdir()) == []


def test_summary_printed_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(
        tmp_path,
        monkeypatch,
        {"income": _EXIT_DOMAIN_ERROR, "fa": _EXIT_OK, "cg": _SOURCE_NOT_REQUIRED},
    )
    err = capsys.readouterr().err
    assert "stockplan-all summary" in err
    assert "stockplan-income" in err
    assert "manual-fill" in err
    assert "Schedule FA Table A2" in err
    assert "not required (zero sales proven)" in err
    assert "Schedule CG" in err


def test_stale_outputs_cleared_before_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: every target is cleared before the first leg runs, so a
    # refused/no-op leg leaves no stale prior-run CSV.
    _patch_handlers(monkeypatch, _ALL_OK)
    present_at_first_leg: list[Path] = []
    income = ibkr_fa.cli._run_stockplan_income

    def first_leg(args: argparse.Namespace) -> int:
        present_at_first_leg.extend(f for f in stale if f.exists())
        return income(args)

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", first_leg)
    out = tmp_path / "out"
    out.mkdir()
    stale = [
        out / f
        for f in (
            "schedule-fa.csv",
            "schedule-os.csv",
            "schedule-fsi.csv",
            "schedule-tr.csv",
            "form-67.csv",
            "schedule-os-debug.csv",
            "schedule-fsi-debug.csv",
            "schedule-tr-debug.csv",
            "form-67-debug.csv",
            "schedule-cg.csv",
            "schedule-cg-ltcg.csv",
        )
    ]
    for f in stale:
        f.write_text("stale", encoding="utf-8")
    manual_a2 = out / "schedule-fa-a2.csv"
    manual_a2.write_text("user prepared", encoding="utf-8")
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert present_at_first_leg == []
    assert manual_a2.read_text(encoding="utf-8") == "user prepared"


def test_failed_leg_output_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A leg that errors AND wrote a partial file must have that file removed so
    # the summary's "ERROR = not produced" stays true.
    out = tmp_path / "out"

    def income_ok(args: argparse.Namespace) -> int:
        _write_promised("income", args)
        return _EXIT_OK

    def fa_writes_then_errors(args: argparse.Namespace) -> int:
        (out / "schedule-fa.csv").write_text("partial", encoding="utf-8")
        return _EXIT_DOMAIN_ERROR

    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", income_ok)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", fa_writes_then_errors)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", lambda _args: _SOURCE_NOT_REQUIRED)
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []


def test_cg_status_zero_sales_is_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ibkr_fa.cli,
        "inspect_stockplan_cg_evidence",
        lambda **_kwargs: StockplanCgEvidenceReport(
            tmp_path / "cg.xlsx", 0, date(2025, 4, 1), date(2026, 3, 31)
        ),
    )
    code = ibkr_fa.cli._run_stockplan_cg_status(
        argparse.Namespace(
            input=tmp_path,
            config=tmp_path / "user.toml",
            financial_year=FinancialYear(2025),
        )
    )
    assert code == _SOURCE_NOT_REQUIRED


def test_cg_status_sales_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ibkr_fa.cli,
        "inspect_stockplan_cg_evidence",
        lambda **_kwargs: StockplanCgEvidenceReport(
            tmp_path / "cg.xlsx", 2, date(2025, 4, 1), date(2026, 3, 31)
        ),
    )
    code = ibkr_fa.cli._run_stockplan_cg_status(
        argparse.Namespace(
            input=tmp_path,
            config=tmp_path / "user.toml",
            financial_year=FinancialYear(2025),
        )
    )
    assert code == _EXIT_DOMAIN_ERROR
    assert "contains 2 sale(s)" in capsys.readouterr().err


def test_cg_status_missing_or_truncated_evidence_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(**_kwargs: object) -> StockplanCgEvidenceReport:
        raise StockplanFaError("missing or truncated report")

    monkeypatch.setattr(ibkr_fa.cli, "inspect_stockplan_cg_evidence", refuse)
    code = ibkr_fa.cli._run_stockplan_cg_status(
        argparse.Namespace(
            input=tmp_path,
            config=tmp_path / "user.toml",
            financial_year=FinancialYear(2025),
        )
    )
    assert code == _EXIT_DOMAIN_ERROR
    assert "could not prove StockPlan Schedule CG status" in capsys.readouterr().err


def test_unverified_issuer_stops_the_run_before_any_leg(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real handlers, not stubs: the Form 67 readiness preflight (#601) reads the
    # income config first, so an unverified issuer (#679) stops the run before
    # any leg runs or writes.
    config = FA_CONFIG.replace('symbol = "GOOG"', 'symbol = "XESP"').replace(
        'isin = "US02079K1079"', 'isin = "US99999E1047"'
    )
    input_dir, cfg, _ = _setup_fa_exports(tmp_path, config=config)
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    captured = capsys.readouterr()
    assert "stockplan-all: stockplan-" not in captured.out
    assert captured.err.count("ConfigError at field 'stockplan.security'") == 1
    assert captured.err.count("docs/stockplan-contract.md") == 1
    assert captured.err.splitlines()[-1] == _STOCKPLAN_CONTRACT_POINTER
    assert list((tmp_path / "out").iterdir()) == []


def test_non_utf8_config_refuses_in_the_readiness_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real handlers: the readiness check loads the config first, so a user.toml
    # that isn't UTF-8 is a refusal, not a readiness crash (#934).
    input_dir, cfg, _ = _setup_fa_exports(tmp_path)
    cfg.write_bytes(NON_UTF8_TOML)
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-fa.csv").write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    captured = capsys.readouterr()
    assert "stockplan-all: stockplan-" not in captured.out
    assert captured.err.count("ConfigError at field '<file>': must be UTF-8 text") == 1
    assert "crashed unexpectedly" not in captured.err
    assert captured.err.splitlines()[-1] == _STOCKPLAN_CONTRACT_POINTER
    assert list(out.iterdir()) == []


def _date_rows_before_coverage(report: Path) -> None:
    """The #912 example: the title covers FY 2025-26, but both rows are dated 15-Mar-2025."""
    workbook = openpyxl.load_workbook(report)
    sheet = workbook["IN - Dividend Report"]
    sheet["A1"] = (
        "Alphabet, Inc. - IN - Dividend Report (report run on 02-Apr-2026 for "
        "reporting period 01-Apr-2025 to 31-Mar-2026)"
    )
    for row, cash in ((3, 10.00), (4, -2.50)):
        sheet[f"A{row}"] = datetime(2025, 3, 15)
        sheet[f"C{row}"] = cash
    workbook.save(report)


@pytest.mark.parametrize(
    ("command", "stale"),
    [
        ("stockplan-income", ("schedule-os.csv",)),
        ("stockplan-fa", ("schedule-fa.csv",)),
        ("stockplan-all", ("schedule-os.csv", "schedule-fa.csv")),
    ],
)
def test_dividend_row_outside_coverage_refuses_and_clears_stale_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, stale: tuple[str, ...]
) -> None:
    # Real handlers: each caller refuses the parser's coverage contradiction
    # with exit 4 and leaves no output (#912).
    input_dir, cfg, _ = _setup_fa_exports(tmp_path)
    _date_rows_before_coverage(input_dir / "IN - Dividend Report.xlsx")
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        (out / name).write_text("stale", encoding="utf-8")
    fy = ("--financial-year", "2025-26", "--itr-form", "ITR-2")
    outputs = {
        "stockplan-income": ("--output-dir", str(out), *fy),
        "stockplan-fa": (
            "--output",
            str(out / "schedule-fa.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ),
        "stockplan-all": ("--output-dir", str(out), *fy, "--no-fetch-prices"),
    }
    rc = ibkr_fa.cli.main(
        [command, "--input", str(input_dir), "--config", str(cfg), *outputs[command]]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert (
        "row 2, column 'Entry Date': 'Dividend Paid' is dated outside the report's "
        "stated reporting period"
    ) in err
    assert "2025-03-15" not in err
    assert err.splitlines()[-1] == _STOCKPLAN_CONTRACT_POINTER


def test_fa_leg_prior_session_close_refusal_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real stockplan-fa leg over a series with only a Dec-30 close (#1165);
    # income and Schedule CG are stubbed to succeed, so the frame is the FA leg's.
    _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", _real_stockplan_fa)
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", PRIOR_SESSION_ONLY_PIPELINE)
    input_dir, cfg, _ = _setup_fa_exports(tmp_path, config=MARKED_CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    for names in ibkr_fa.cli._STOCKPLAN_ALL_TARGETS.values():
        for name in names:
            (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == "".join(
        f"\n{_RUN_ALL_RULE}\n  stockplan-all: {name}\n{_RUN_ALL_RULE}\n"
        for name in ("stockplan-income", "stockplan-fa", "Schedule CG")
    )
    assert captured.err == (
        f"error: {MARKET_SESSION_REFUSAL}\n"
        f"{_RUN_ALL_RULE}\n"
        "  stockplan-all summary (FY 2025-26)\n"
        f"{_RUN_ALL_RULE}\n"
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-income  "
        "(output removed because the run failed)\n"
        f"  {_RUN_ALL_STATUS_LABEL['error']}  stockplan-fa\n"
        "  not required (zero sales proven)  Schedule CG\n"
        f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  Schedule FA Table A2 "
        "(complete daily cash/NAV evidence unavailable)\n"
        f"{_SUMMARY_FOOTER}\n"
        f"{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, *MARKERS_IN_INPUT)


def test_fa_leg_files_the_exact_lot_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1314: the real stockplan-fa leg over one vest whose cost needs 29
    # digits; income and Schedule CG are stubbed to succeed. Main filed
    # ₹21,506,173,499 from the 28-digit cost.
    _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", _real_stockplan_fa)
    input_dir, cfg, _ = setup_one_vest(tmp_path, *LONG_COST_VESTS["a"])
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert "error:" not in capsys.readouterr().err
    row = a3_row(out / "schedule-fa.csv")
    assert (row[INITIAL_VALUE], row[AUDIT_COST]) == (
        "21506173498",
        "246913587.81285878300803673938",
    )


# A sale-sheet date the day after its 22-Jun-2026 run date (#1176).
_AFTER_THE_RUN = datetime(2026, 6, 23)
_SALE_AFTER_THE_RUN = (
    "ParseError in section 'Capital Gains Report' at row 3, column 'Sale Date': "
    "sale date must not be after this sheet's report run date"
)
_ACQUISITION_AFTER_THE_RUN = (
    "ParseError in section 'Capital Gains Report' at row 3, column 'Acquisition Date': "
    "acquisition date must not be after this sheet's report run date"
)
_CG_PROOF_REFUSAL = "could not prove StockPlan Schedule CG status: "


@pytest.mark.parametrize(
    ("exports", "fa_error", "cg_error"),
    [
        pytest.param(
            {"vests": [SP_FUTURE_VEST, *FA_VESTS]},
            FUTURE_VEST_REFUSAL.format(row=2),
            None,
            id="vest",
        ),
        pytest.param(
            {"sales": [[_AFTER_THE_RUN, 137.2468, 1.0, "GOOG", 137.2468, datetime(2025, 8, 14)]]},
            _SALE_AFTER_THE_RUN,
            _SALE_AFTER_THE_RUN,
            id="sale",
        ),
        pytest.param(
            {"sales": [[datetime(2026, 2, 10), 137.2468, 1.0, "GOOG", 137.2468, _AFTER_THE_RUN]]},
            _ACQUISITION_AFTER_THE_RUN,
            _ACQUISITION_AFTER_THE_RUN,
            id="acquisition",
        ),
    ],
)
def test_event_after_the_run_date_fails_the_run_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exports: dict[str, Any],
    fa_error: str,
    cg_error: str | None,
) -> None:
    # The real stockplan-fa leg reads both parser sheets and the real Schedule CG
    # leg the sale sheet; income is stubbed to succeed (#1176).
    _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", _real_stockplan_fa)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", _real_stockplan_cg_status)
    input_dir, cfg, _ = setup_marked_exports(tmp_path, **exports)
    out = tmp_path / "out"
    out.mkdir()
    for names in ibkr_fa.cli._STOCKPLAN_ALL_TARGETS.values():
        for name in names:
            (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == "".join(
        f"\n{_RUN_ALL_RULE}\n  stockplan-all: {name}\n{_RUN_ALL_RULE}\n"
        for name in ("stockplan-income", "stockplan-fa", "Schedule CG")
    )
    cg_lines = (
        ("", "  not required (zero sales proven)  Schedule CG\n")
        if cg_error is None
        else (
            f"error: {_CG_PROOF_REFUSAL}{cg_error}\n",
            f"  {_RUN_ALL_STATUS_LABEL['error']}  Schedule CG\n",
        )
    )
    assert captured.err == (
        f"error: {fa_error}\n"
        f"{cg_lines[0]}"
        f"{_RUN_ALL_RULE}\n"
        "  stockplan-all summary (FY 2025-26)\n"
        f"{_RUN_ALL_RULE}\n"
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-income  "
        "(output removed because the run failed)\n"
        f"  {_RUN_ALL_STATUS_LABEL['error']}  stockplan-fa\n"
        f"{cg_lines[1]}"
        f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  Schedule FA Table A2 "
        "(complete daily cash/NAV evidence unavailable)\n"
        f"{_SUMMARY_FOOTER}\n"
        f"{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, *SP_MARKERS)


def test_vest_that_left_before_year_end_fails_the_fa_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #1290: the real stockplan-fa and Schedule CG legs; income is stubbed to
    # succeed. With the year-end close cached, main filed vest A as held.
    _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_fa", _real_stockplan_fa)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", _real_stockplan_cg_status)
    input_dir, cfg, _ = setup_marked_exports(tmp_path, vests=LEFT_VESTS, **LEFT_HOLDINGS)
    monkeypatch.setattr("ibkr_fa.cli.run_stockplan_fa", offline_pipeline(input_dir))
    out = tmp_path / "out"
    out.mkdir()
    for names in ibkr_fa.cli._STOCKPLAN_ALL_TARGETS.values():
        for name in names:
            (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: {MISMATCH}\n"
        f"{_RUN_ALL_RULE}\n"
        "  stockplan-all summary (FY 2025-26)\n"
        f"{_RUN_ALL_RULE}\n"
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-income  "
        "(output removed because the run failed)\n"
        f"  {_RUN_ALL_STATUS_LABEL['error']}  stockplan-fa\n"
        "  not required (zero sales proven)  Schedule CG\n"
        f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  Schedule FA Table A2 "
        "(complete daily cash/NAV evidence unavailable)\n"
        f"{_SUMMARY_FOOTER}\n"
        f"{_STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert_no_markers(captured.out + captured.err, *LEFT_MARKERS)


# --- Form 67 readiness (#601) ---------------------------------------------------

_ZERO_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "0"
"""


def test_blank_form67_income_leg_is_manual_with_debug_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _calls, out = _run_real_income(
        tmp_path, monkeypatch, config=INCOME_CONFIG, stub_income=False
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert sorted(path.name for path in out.glob("form-67*")) == ["form-67-debug.csv"]
    assert f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  stockplan-income" in (
        capsys.readouterr().err.splitlines()
    )


@pytest.mark.parametrize(
    ("tin_country", "status", "warned"), [("US", "ok", 0), ("GB", "manual_fill", 1)]
)
def test_blank_relief_tin_makes_the_income_leg_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tin_country: str,
    status: str,
    warned: int,
) -> None:
    config = INCOME_CONFIG.replace('"MS-STOCKPLAN-1"', '"999900001"').replace(
        'US = "123456789"', f'{tin_country} = "999-00-0001"'
    )
    rc, _calls, _out = _run_real_income(
        tmp_path, monkeypatch, config=config + _READY_OVERRIDE, stub_income=False
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    captured = capsys.readouterr()
    assert f"  {_RUN_ALL_STATUS_LABEL[status]}  stockplan-income" in captured.err.splitlines()
    assert (captured.out + captured.err).count(BLANK_RELIEF_TIN_WARNING) == warned
    assert_no_markers(captured.out + captured.err, "999-00-0001", "999900001")


def test_zero_credit_refuses_before_any_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, calls, out = _run_real_income(
        tmp_path, monkeypatch, config=INCOME_CONFIG + _ZERO_OVERRIDE, stub_income=True
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in capsys.readouterr().err


def test_relief_the_portal_overwrites_refuses_before_any_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#900: USD 0.97 withheld on a USD 2.60 dividend is above the 25% treaty
    rate, so the relief is capped below the tax paid and the portal's
    read-only FSI cell would overwrite it."""
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=True,
        pairs=(("2025-12-10", 2.60, -0.97),),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    assert "diverge from the portal's read-only relief" in capsys.readouterr().err


# --- Zero sales (#882) ------------------------------------------------------------

_SALE: list[object] = [datetime(2025, 11, 1), 300.0, 1.0, "GOOG", 300.0, datetime(2025, 8, 14)]


def _remove(report: Path) -> None:
    report.unlink()


def _rename_a_header(report: Path) -> None:
    workbook = openpyxl.load_workbook(report)
    workbook["Capital Gains Report"]["A3"] = "Sale Day"
    workbook.save(report)


def _run_real_cg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ready: bool,
    damage: Callable[[Path], None] | None = None,
    **cg_kwargs: object,
) -> tuple[int, dict[str, argparse.Namespace], Path]:
    """Run stockplan-all over synthetic exports and a config with a US TIN, using
    the real Form 67 readiness, income and Schedule CG steps; only stockplan-fa
    is stubbed and recorded."""
    calls = _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli.stockplan_form67_readiness", _real_stockplan_form67_readiness)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_income", _real_stockplan_income)
    monkeypatch.setattr("ibkr_fa.cli._run_stockplan_cg_status", _real_stockplan_cg_status)
    input_dir, cfg, _ = _setup_fa_exports(
        tmp_path,
        config=FA_CONFIG + US_FOREIGN_TIN_TOML + (_READY_OVERRIDE if ready else ""),
        **cg_kwargs,
    )
    if damage is not None:
        damage(input_dir / "IN - Capital Gain_Loss Report.xlsx")
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "stockplan-all",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )
    return rc, calls, out


@pytest.mark.parametrize(
    ("ready", "income_status", "form67_file"),
    [(True, "ok", "form-67.csv"), (False, "manual_fill", "form-67-debug.csv")],
)
def test_proven_zero_sales_leave_the_income_status_to_form67(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: bool,
    income_status: str,
    form67_file: str,
) -> None:
    # The Schedule CG step proves zero sales, so the income leg neither warns
    # nor changes its status.
    rc, calls, out = _run_real_cg(tmp_path, monkeypatch, ready=ready)
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert set(calls) == {"fa"}
    assert sorted(path.name for path in out.iterdir()) == sorted(
        (*_INCOME[:3], form67_file, "schedule-fa.csv")
    )
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert f"  {_RUN_ALL_STATUS_LABEL[income_status]}  stockplan-income" in err
    assert "  not required (zero sales proven)  Schedule CG" in err
    assert _ZERO_SALES_UNPROVEN not in captured.err
    assert (captured.out + captured.err).count("zero sales proven") == 1


@pytest.mark.parametrize(
    ("damage", "cg_kwargs", "error"),
    [
        pytest.param(None, {"sales": [_SALE]}, "contains 1 sale(s)", id="sale"),
        pytest.param(_remove, {}, "no 'IN - Capital Gain_Loss Report' .xlsx found", id="missing"),
        pytest.param(_rename_a_header, {}, "expected exact header row", id="malformed"),
        pytest.param(
            None,
            {"period": "01-Sep-2025 to 01-May-2026"},
            "sales cannot be proven zero",
            id="period-not-covered",
        ),
    ],
)
def test_unproven_or_nonzero_sales_fail_the_run_with_the_income_status_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], None] | None,
    cg_kwargs: dict[str, object],
    error: str,
) -> None:
    rc, calls, out = _run_real_cg(tmp_path, monkeypatch, ready=True, damage=damage, **cg_kwargs)
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"fa"}
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert (
        f"  {_RUN_ALL_STATUS_LABEL['ok']}  stockplan-income  "
        "(output removed because the run failed)"
    ) in err
    assert f"  {_RUN_ALL_STATUS_LABEL['error']}  Schedule CG" in err
    assert error in captured.err
    assert _ZERO_SALES_UNPROVEN not in captured.err
    assert "zero sales proven" not in captured.out + captured.err
