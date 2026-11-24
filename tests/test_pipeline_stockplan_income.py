"""Tests for the ``stockplan-income`` pipeline (Schedule OS/FSI/TR/Form 67 for a
Morgan Stanley StockPlan dividend) and its CLI subcommand.

Uses a synthetic ``IN - Dividend Report`` .xlsx (openpyxl) plus a synthetic
user.toml with a ``[stockplan]`` block — the parser reads xlsx directly, so no
monkeypatching is needed.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as _dt
import errno
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest

import ibkr_fa.pipeline_stockplan_income as sp_mod
from ibkr_fa.cli import _STOCKPLAN_CONTRACT_POINTER, main
from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Readiness
from ibkr_fa.exceptions import ConfigError, ParseError
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome
from ibkr_fa.output.base import OutputBinding, Subcommand
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_stockplan_income import (
    StockplanIncomeError,
    run_stockplan_income,
)
from tests._crash_reports import assert_no_markers
from tests._form67_overrides import FORM67_READY_OVERRIDE as _READY_OVERRIDE
from tests._ftc_warnings import BLANK_RELIEF_TIN_WARNING
from tests._stockplan_income import STOCKPLAN_INCOME_CONFIG as CONFIG
from tests._stockplan_income import run_income_cli as _main
from tests._stockplan_income import setup_income_run as _setup
from tests._stockplan_income import write_dividend_report as _write_report
from tests._stockplan_report_writers import DIVIDEND_REPORT_HEADER as _HEADER
from tests._stockplan_report_writers import STOCKPLAN_USD_FOOTER
from tests._writer_stubs import StubWriter, patch_writers

FY = FinancialYear(start_year=2025)  # FY 2025-26 / AY 2026-27
_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def _run(
    input_dir: Path,
    cfg: Path,
    out: Path,
    *,
    debug: bool = False,
    cg_step_checks_sales: bool = False,
):
    return run_stockplan_income(
        input_dir=input_dir,
        config_path=cfg,
        output_dir=out,
        financial_year=FY,
        debug=debug,
        cg_step_checks_sales=cg_step_checks_sales,
    )


def test_happy_path_emits_four_csvs(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path, config=CONFIG + _READY_OVERRIDE)
    report = _run(input_dir, cfg, out)
    assert report.dividend_count == 2
    assert report.withholding_count == 2
    assert report.os_foreign_rows == 2
    assert report.fsi_rows == 1
    assert report.withholding_total_usd == Decimal("1.68")  # 0.65 + 1.03 in FY 2025-26
    assert report.manual_fill_warnings == (sp_mod._ZERO_SALES_UNPROVEN,)
    assert report.form67_readiness is Form67Readiness.READY
    assert report.manual_fill_required
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]


@pytest.mark.parametrize(
    ("tin_country", "warnings"), [("US", ()), ("GB", (BLANK_RELIEF_TIN_WARNING,))]
)
def test_blank_relief_tin_is_manual_fill_once_sales_are_proven(
    tmp_path: Path, tin_country: str, warnings: tuple[str, ...]
) -> None:
    config = CONFIG.replace('US = "123456789"', f'{tin_country} = "123456789"')
    input_dir, cfg, out = _setup(tmp_path, config=config + _READY_OVERRIDE)
    report = _run(input_dir, cfg, out, cg_step_checks_sales=True)
    assert report.form67_readiness is Form67Readiness.READY
    assert report.manual_fill_required is bool(warnings)
    assert report.manual_fill_warnings == warnings


def test_missing_indian_tax_is_manual_fill(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    report = _run(input_dir, cfg, out)
    assert report.form67_readiness is Form67Readiness.BLANK
    assert len(report.manual_fill_warnings) == 2
    assert "INDIAN TAX CALCULATION REQUIRED" in report.manual_fill_warnings[0]
    assert report.manual_fill_warnings[1] == sp_mod._ZERO_SALES_UNPROVEN


def test_debug_sidecars_are_unified(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path, config=CONFIG + _READY_OVERRIDE)
    _run(input_dir, cfg, out, debug=True)
    with (out / "schedule-os.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert len(next(csv.reader(handle))) == 7
    with (out / "schedule-fsi.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert len(next(csv.reader(handle))) == 2  # vertical label, value
    with (out / "schedule-tr.csv").open(newline="", encoding="utf-8-sig") as handle:
        tr_rows = list(csv.reader(handle))
    assert len(tr_rows[0]) == 5
    assert not any(row[0].startswith("Quicko") for row in tr_rows)
    with (out / "form-67.csv").open(newline="", encoding="utf-8") as handle:
        assert len(next(csv.reader(handle))) == 15
    for filename, width in (
        ("schedule-os-debug.csv", 11),
        ("schedule-fsi-debug.csv", 13),
        ("schedule-tr-debug.csv", 9),
        ("form-67-debug.csv", 17),
    ):
        with (out / filename).open(newline="", encoding="utf-8-sig") as handle:
            assert len(next(csv.reader(handle))) == width

    _run(input_dir, cfg, out)
    for filename in (
        "schedule-os-debug.csv",
        "schedule-fsi-debug.csv",
        "schedule-tr-debug.csv",
        "form-67-debug.csv",
    ):
        assert not (out / filename).exists()


def test_no_records_sentinel_emits_authoritative_zero_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    _write_report(input_dir, zero_activity=True)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()

    report = _run(input_dir, cfg, out)

    assert report.dividend_count == 0
    assert report.withholding_count == 0
    assert report.os_foreign_rows == 0
    assert report.fsi_rows == 0
    for name in ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv", "form-67.csv"):
        assert (out / name).is_file()


def test_cli_unverified_issuer_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Only Alphabet GOOG exports are verified: refuse, don't warn and file (#679).
    input_dir = tmp_path / "in"
    _write_report(input_dir, symbol="XESP")
    cfg = tmp_path / "user.toml"
    cfg.write_text(
        CONFIG.replace('symbol = "GOOG"', 'symbol = "XESP"').replace(
            'isin = "US02079K1079"', 'isin = "US99999E1047"'
        )
    )
    out = tmp_path / "out"
    out.mkdir()

    code = main(
        [
            "stockplan-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert (
        "error: ConfigError at field 'stockplan.security': Morgan Stanley StockPlan exports "
        "are verified only for Alphabet GOOG (US02079K1079); refusing the configured issuer "
        "until its real exports are reviewed (#382)."
    ) in err
    assert "XESP" not in err
    assert "US99999E1047" not in err
    assert err.splitlines()[-1] == _STOCKPLAN_CONTRACT_POINTER
    assert sp_mod._ZERO_SALES_UNPROVEN not in err


def test_missing_stockplan_config_refuses(tmp_path: Path) -> None:
    config = CONFIG.split("[stockplan.security]", maxsplit=1)[0]
    input_dir, cfg, out = _setup(tmp_path, config=config)
    with pytest.raises(StockplanIncomeError, match=r"no \[stockplan\] block"):
        _run(input_dir, cfg, out)


def test_coverage_not_spanning_fy_refuses(tmp_path: Path) -> None:
    # Report coverage ends before FY end -> a dividend could be missing. Zero
    # activity, so no row falls outside the stated coverage (#912).
    input_dir = tmp_path / "in"
    _write_report(
        input_dir, period="reporting period 01-Apr-2025 to 30-Nov-2025", zero_activity=True
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(StockplanIncomeError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == (
        "IN - Dividend Report.xlsx's reporting period does not span the financial year; "
        "a dividend could be missing"
    )
    assert_no_markers(
        str(excinfo.value), "01-Apr-2025", "2025-04-01", "30-Nov-2025", "2025-11-30", "2026-03-31"
    )


def test_coverage_starting_after_fy_start_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The account opened years before the FY, so a 1 April dividend could be
    # missing from a report that starts on 2 April. Zero activity, so no row
    # falls outside the stated coverage (#912).
    config = CONFIG.replace("opening_date = 2025-07-14", "opening_date = 2020-01-01")
    assert config != CONFIG
    input_dir = tmp_path / "in"
    _write_report(
        input_dir, period="reporting period 02-Apr-2025 to 31-Mar-2026", zero_activity=True
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(config)
    out = tmp_path / "out"
    out.mkdir()
    message = (
        "IN - Dividend Report.xlsx's reporting period does not span the financial year; "
        "a dividend could be missing"
    )
    with pytest.raises(StockplanIncomeError) as excinfo:
        _run(input_dir, cfg, out)
    assert str(excinfo.value) == message
    assert list(out.iterdir()) == []
    assert_no_markers(str(excinfo.value), "02-Apr-2025", "2025-04-02", "31-Mar-2026", "2026-03-31")

    assert _main(input_dir, cfg, out) == 4
    assert list(out.iterdir()) == []
    assert f"error: {message}\n" in capsys.readouterr().err


def test_report_run_before_coverage_end_refuses(tmp_path: Path) -> None:
    # A report run on 01-Feb-2026 can't hold a dividend paid later in the FY,
    # whatever coverage its title claims (#682).
    input_dir = tmp_path / "in"
    _write_report(
        input_dir, run_on="01-Feb-2026", period="reporting period 01-Jan-1900 to 31-Mar-2026"
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ParseError, match="was not run strictly after its reporting period ended"):
        _run(input_dir, cfg, out)
    assert list(out.iterdir()) == []


def test_no_report_refuses(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(StockplanIncomeError, match=r"no 'IN - Dividend Report"):
        _run(input_dir, cfg, out)


def test_ambiguous_report_refuses(tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    _write_report(input_dir, name="IN - Dividend Report.xlsx")
    _write_report(input_dir, name="IN - Dividend Report - Copy.xlsx")
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(StockplanIncomeError, match="ambiguous"):
        _run(input_dir, cfg, out)


def test_no_ftc_warning_when_no_withholding(tmp_path: Path) -> None:
    # Dividends but no withholding, and a blank US TIN -> no TIN warning and no
    # manual fill (mirrors #361).
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IN - Dividend Report"
    ws.append(
        [
            "Alphabet, Inc. - IN - Dividend Report (report run on 22-Jun-2026 for "
            "reporting period 01-Jan-1900 to 21-Jun-2026)"
        ]
    )
    ws.append(_HEADER)
    ws.append([_dt.datetime(2025, 12, 10), "Dividend Paid", 2.60, "GOOG", None, None, 0.0])
    ws.append([STOCKPLAN_USD_FOOTER])
    wb.save(input_dir / "IN - Dividend Report.xlsx")
    cfg = tmp_path / "user.toml"
    cfg.write_text(
        '[account]\nnumber = "X"\nholder_name = "Y"\nopening_date = 2025-07-14\n'
        'status = "BeneficialOwner"\n\n[stockplan.security]\nsymbol = "GOOG"\n'
        'isin = "US02079K1079"\n'
    )
    out = tmp_path / "out"
    out.mkdir()
    report = _run(input_dir, cfg, out, cg_step_checks_sales=True)
    assert report.withholding_count == 0
    assert report.fsi_rows == 1
    assert report.manual_fill_warnings == ()
    assert not report.manual_fill_required


def test_early_failure_clears_stale_csvs(tmp_path: Path) -> None:
    # A prior run's CSVs must not survive an EARLY failure (e.g. missing report).
    input_dir = tmp_path / "in"
    input_dir.mkdir()  # empty -> discovery will refuse
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG)
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "schedule-os.csv"
    stale.write_text("STALE from a previous run")
    with pytest.raises(StockplanIncomeError, match=r"no 'IN - Dividend Report"):
        _run(input_dir, cfg, out)
    assert not stale.exists()


def test_missing_config_clears_stale_csvs(tmp_path: Path) -> None:
    # Even a missing [stockplan] block (before config validation) must clear stale output.
    config = CONFIG.split("[stockplan.security]", maxsplit=1)[0]
    input_dir, cfg, out = _setup(tmp_path, config=config)
    stale = out / "form-67.csv"
    stale.write_text("STALE")
    with pytest.raises(StockplanIncomeError, match=r"no \[stockplan\] block"):
        _run(input_dir, cfg, out)
    assert not stale.exists()


@_PERMISSIONS_ENFORCED
def test_unsearchable_output_folder_raises_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """#1238: the output-folder check raises EACCES instead of calling the folder missing."""
    loads: list[object] = []
    monkeypatch.setattr(sp_mod, "_load_dividends", lambda *args: loads.append(args))
    input_dir, cfg, _ = _setup(tmp_path)
    blocked = tmp_path / "blocked"
    out = blocked / "out"
    out.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        _run(input_dir, cfg, out)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(out)!r}"
    blocked.chmod(0o700)
    assert list(out.iterdir()) == []
    assert loads == []


def test_interrupted_partial_write_leaves_no_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If a writer fails mid-run, no partial set of fresh CSVs may remain.
    input_dir, cfg, out = _setup(tmp_path)

    class _Boom(StubWriter):
        def write(self, _payload: object, _path: Path) -> None:
            raise KeyboardInterrupt

    patch_writers(monkeypatch, Subcommand.TR, _Boom)
    with pytest.raises(KeyboardInterrupt):
        _run(input_dir, cfg, out)
    assert list(out.glob("*.csv")) == []


@pytest.mark.parametrize(
    ("config", "zero_activity", "form67_file", "form67_output"),
    [
        pytest.param(
            CONFIG + _READY_OVERRIDE, False, "form-67.csv", "official Form 67 CSV", id="ready"
        ),
        pytest.param(CONFIG, True, "form-67.csv", "official Form 67 CSV", id="no-form67-rows"),
        pytest.param(CONFIG, False, "form-67-debug.csv", "Form 67 debug CSV", id="blank"),
    ],
)
def test_cli_standalone_run_flags_unproven_sales(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: str,
    zero_activity: bool,
    form67_file: str,
    form67_output: str,
) -> None:
    # It never reads the Capital Gain/Loss workbook, so a run that writes its
    # files still exits 3 and says why (#882).
    input_dir = tmp_path / "in"
    _write_report(input_dir, zero_activity=zero_activity)
    cfg = tmp_path / "user.toml"
    cfg.write_text(config)
    out = tmp_path / "out"
    out.mkdir()
    assert _main(input_dir, cfg, out) == 3
    assert sorted(path.name for path in out.iterdir()) == sorted(
        (form67_file, "schedule-fsi.csv", "schedule-os.csv", "schedule-tr.csv")
    )
    captured = capsys.readouterr()
    assert f"Schedule OS / FSI / TR filing CSVs and {form67_output}" in captured.out
    assert captured.err.splitlines().count(f"warning: {sp_mod._ZERO_SALES_UNPROVEN}") == 1
    assert "docs/stockplan-contract.md" not in captured.err


# --- Form 67 readiness (#601) ---------------------------------------------------

_ZERO_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "0"
"""


def _refuse_every_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> None:
        raise AssertionError("a normal output write was attempted")

    monkeypatch.setattr(OutputBinding, "writer", refuse)


def test_blank_form67_row_withholds_official_csv_and_writes_debug(tmp_path: Path) -> None:
    input_dir, cfg, out = _setup(tmp_path)
    _run(input_dir, cfg, out)
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67-debug.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]


def test_zero_credit_override_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir, cfg, out = _setup(tmp_path, config=CONFIG + _ZERO_OVERRIDE)
    _refuse_every_write(monkeypatch)
    with pytest.raises(ConfigError, match=r"US/DIVIDEND: the computed credit rounds to ₹0"):
        _run(input_dir, cfg, out)
    assert list(out.iterdir()) == []


def test_cli_refusal_exits_4_without_the_sales_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_dir, cfg, out = _setup(tmp_path, config=CONFIG + _ZERO_OVERRIDE)
    assert _main(input_dir, cfg, out) == 4
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in err
    assert sp_mod._ZERO_SALES_UNPROVEN not in err


def test_unvendored_country_row_refuses_before_any_write_naming_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The issuer gate admits only GOOG, so a CH row can't reach StockPlan's
    # Form 67 today; inject the partial result the compute would produce for a
    # CH-ISIN security with a CH override (no vendored CH DTAA row, #249).
    input_dir, cfg, out = _setup(tmp_path)
    real_compute = sp_mod.compute_income_schedules

    def compute_with_ch_row(*args: Any, **kwargs: Any) -> Any:
        result = real_compute(*args, **kwargs)
        ch_row = Form67Entry(
            serial_number=1,
            cbdt_country_code="41",
            country_name_portal="SWITZERLAND",
            iso_alpha2="CH",
            source_of_income=Form67SourceOfIncome.DIVIDEND,
            income_inr=Decimal("1000"),
            foreign_tax_inr=Decimal("350"),
            foreign_tax_rate_percent=Decimal("35"),
            event_count=1,
            tax_payable_normal_inr=Decimal("300"),
        )
        return dataclasses.replace(result, form67_result=Form67ComputeResult(entries=(ch_row,)))

    monkeypatch.setattr(sp_mod, "compute_income_schedules", compute_with_ch_row)
    _refuse_every_write(monkeypatch)
    with pytest.raises(
        ConfigError, match=r"CH/DIVIDEND: CH has no row in the vendored DTAA dividend table"
    ):
        _run(input_dir, cfg, out)
    assert list(out.iterdir()) == []
