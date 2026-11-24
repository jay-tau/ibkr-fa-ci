"""End-to-end ``run-all`` tests on the real pipelines (#609).

Unlike ``tests/test_cli_run_all.py``, no handler is monkeypatched: every FY
schedule parses the fixture and writes its real output. The golden test pins
every output byte, both streams and the exit code; the spies count how often
the shared IBKR preamble and the FSI build run.
"""

from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa import cli, pipeline_cg, pipeline_cg_ltcg, pipeline_fsi_compute, pipeline_os
from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline, _FsiReuseScope
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._flex_records import eof_record
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._ibkr_run_all import CY_2025_INPUT as _CY_2025_INPUT
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_run_all import run_all_argv as _run_all_argv
from tests._ibkr_run_all import run_all_both as _run_all_both
from tests._ibkr_run_all import run_all_fy as _run_all
from tests._ibkr_run_all import seed_every_target as _seed_every_target
from tests._ibkr_run_all import spy_on as _spy
from tests._ibkr_run_all import write_e2e_config as _write_config
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._ibkr_user_toml import US_PERSON_USER_TOML as _USER_TOML
from tests._run_all_fakes import RUN_ALL_FY_STEPS

_FIXTURES = Path(__file__).parent / "fixtures"

# SHA-256 of each output file, and of stdout / stderr with the tmp path
# replaced by "<tmp>", as written by main at 2439448. #601: the blank Form 67
# row now withholds form-67.csv and writes form-67-debug.csv (manual fill,
# exit 3); every schedule's file is unchanged. #741: the two CG review CSVs
# gain the SBI TTBR source-date column; nothing else changes. #787: both
# streams drop "or an ambiguous pathway" from the Schedule TR relief text.
# #791: stdout's withheld-file line no longer says every entry's tax payable
# is blank. #924: the config has no [account.foreign_tins], so the stderr
# summary lists Schedule FSI and TR as manual-fill. #1032: the fixture's security
# is synthetic, so the Schedule OS and both CG CSVs name a new ticker and ISIN, and
# its dividend is now USD 2.60 with USD 0.39 withheld: at the 2025-08-31 TTBR of
# 87.70, the dividend income is 228.02 (filed 228) and the foreign tax 34.203
# (filed 34). These amounts update the relevant fields in Schedule OS, FSI, TR,
# Form 67 and stdout; stderr is unchanged. #1074: the Schedule OS review CSV
# gains its 1b(ix) interest total row; nothing else changes. #1359: both
# streams drop "or no positive relief" from the Schedule TR LEFT BLANK text.
# #1363: stdout drops the always-zero "non-ordinary distributions: 0" line from
# the Schedule TR and Form 67 exclusion lists; nothing else changes. #1361: both
# CG review CSVs gain their Table F block before the audit TOTAL; without it
# each hashes to its earlier digest, and nothing else changes. #1427: stderr's
# "INDIAN TAX CALCULATION REQUIRED" warning ends with the new override remedy;
# nothing else changes. #1446: that warning drops its lending-fee by-hand
# clause; nothing else changes.
_GOLDEN_SHA256 = {
    "schedule-os.csv": "b22b7204cbb9e89684ba162a4270f5ba4468c7be9eb6d074f6490156435a965f",
    "schedule-cg.csv": "88178251f1d97581bc9fff21d15c50c88d8d335d3bd5e2a2c3ba6b1cefed0837",
    "schedule-cg-ltcg.csv": "32efdf58f79e5a62285722ae49a64dcb4403fc0e5116e2d2eebb6c7befa89a31",
    "schedule-fsi.csv": "07fcd0f5c889410f51de2332f073b4dce26159660ca9f665e1c004b4862adbd6",
    "schedule-tr.csv": "5e7101149a3ddfa681483667036be9069f8895be3bd030b3b4da9dfd81b35df0",
    "form-67-debug.csv": "9f18327cba3ed37448045af2cc9df602299ce36fea24a642b05d58baeaeeafde",
    "stdout": "fbc84025c68b92969d124131eebd1771505f80a710b62d29dceeaa6e0ee56182",
    "stderr": "43ff6d2600180a53baa3b58d40af2ee8edb4a1130b6b8b9f9d77a8c84a75654f",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_run_all_golden_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run_all(tmp_path, _FY_2025_26_INPUT)

    captured = capsys.readouterr()
    digests = {path.name: _sha256(path.read_bytes()) for path in (tmp_path / "out").iterdir()}
    for name, text in (("stdout", captured.out), ("stderr", captured.err)):
        digests[name] = _sha256(text.replace(str(tmp_path), "<tmp>").encode())
    assert rc == 3
    assert digests == _GOLDEN_SHA256


@pytest.mark.parametrize(
    "period",
    [
        pytest.param("April 1, 2024 - March 31, 2026", id="starts-earlier"),
        pytest.param("April 1, 2025 - June 30, 2026", id="ends-later"),
        pytest.param("January 1, 2020 - June 30, 2026", id="both"),
    ],
)
def test_run_all_statement_containing_the_fy_gives_the_golden_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], period: str
) -> None:
    """#1284: Schedule OS accepts a statement whose period contains the FY,
    as the other FY schedules do, so ``run-all`` on the golden fixture
    widened that way writes every FY file and gives the golden run's exit
    code, files and streams. Before, Schedule OS refused the period, so the
    run exited 4 and removed every output."""
    old = 'Statement,Data,Period,"April 1, 2025 - March 31, 2026"'
    text = _FY_2025_26_INPUT.read_text(encoding="utf-8")
    assert text.count(old) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(text.replace(old, f'Statement,Data,Period,"{period}"'), encoding="utf-8")

    rc = _run_all(tmp_path, statement)

    captured = capsys.readouterr()
    digests = {path.name: _sha256(path.read_bytes()) for path in (tmp_path / "out").iterdir()}
    for name, stream in (("stdout", captured.out), ("stderr", captured.err)):
        digests[name] = _sha256(stream.replace(str(tmp_path), "<tmp>").encode())
    assert rc == 3
    assert digests == _GOLDEN_SHA256


def test_run_all_period_error_stops_the_form67_preflight_before_any_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#601: run-all builds the FSI context for its Form 67 readiness check
    before any schedule writes, so a statement that misses the FY stops the
    run there, with the period error, and no schedule runs."""
    invoke = _spy(monkeypatch, cli, "_invoke_run_all_handler")

    assert _run_all(tmp_path, _CY_2025_INPUT) == 4

    assert invoke.call_count == 0
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []
    err = capsys.readouterr().err
    assert "does not fully contain the Indian financial year 2025-26" in err
    assert "Form 67 foreign-tax-credit reporting" in err


def test_run_all_builds_the_fsi_context_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy(monkeypatch, pipeline_fsi_compute, "compute_income_schedules")
    loads = [
        _spy(monkeypatch, module, "_load_ibkr_activity_input")
        for module in (pipeline_os, pipeline_cg, pipeline_cg_ltcg, pipeline_fsi_compute)
    ]

    assert _run_all(tmp_path, _FY_2025_26_INPUT) == 3

    assert builds.call_count == 1
    assert [load.call_count for load in loads] == [1, 1, 1, 1]
    assert pipeline_fsi_compute._reused_contexts.get() is None


def test_standalone_runs_build_their_own_fsi_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy(monkeypatch, pipeline_fsi_compute, "compute_income_schedules")
    config = _write_config(tmp_path)

    for _ in range(2):
        run_schedule_tr(
            input_path=_FY_2025_26_INPUT,
            config_path=config,
            output_path=tmp_path / "schedule-tr.csv",
            financial_year=FinancialYear(2025),
        )

    assert builds.call_count == 2


def test_fsi_reuse_scope_rebuilds_when_any_key_argument_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy(monkeypatch, pipeline_fsi_compute, "compute_income_schedules")
    input_copy = tmp_path / "statement-copy.csv"
    input_copy.write_bytes(_FY_2025_26_INPUT.read_bytes())
    base: dict[str, Any] = {
        "input_path": _FY_2025_26_INPUT,
        "config_path": _write_config(tmp_path),
        "financial_year": FinancialYear(2025),
    }
    changes: list[dict[str, Any]] = [
        {"input_path": input_copy},
        {"config_path": _write_config(tmp_path, "user-copy.toml")},
        {"allow_masked_account": True},
        {"suppress_cross_ay_warning": True},
        {"accept_anonymised_statement": True},
    ]

    with _FsiReuseScope():
        first = _compute_schedule_fsi_for_pipeline(**base)
        assert _compute_schedule_fsi_for_pipeline(**base, report_name="Form 67 reporting") is first
        for change in changes:
            assert _compute_schedule_fsi_for_pipeline(**(base | change)) is not first
        # The statement does not contain FY 2024-25, so only a rebuild raises.
        other_year: dict[str, Any] = {"financial_year": FinancialYear(2024)}
        with pytest.raises(ParseError):
            _compute_schedule_fsi_for_pipeline(**(base | other_year))

    assert builds.call_count == 1 + len(changes)


# --- Form 67 readiness (#601) ---------------------------------------------------


def _run_all_with(
    tmp_path: Path, *, config_text: str, format_id: str, input_path: Path = _FY_2025_26_INPUT
) -> int:
    config = tmp_path / "user.toml"
    config.write_text(config_text, encoding="utf-8")
    return main(
        [
            "run-all",
            "--fy-input",
            str(input_path),
            "--financial-year",
            "2025-26",
            "--format",
            format_id,
            "--itr-form",
            "ITR-2",
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_run_all_blank_form67_step_is_manual_with_debug_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_id: str
) -> None:
    rc = _run_all_with(tmp_path, config_text=_USER_TOML, format_id=format_id)
    assert rc == 3
    assert sorted(path.name for path in (tmp_path / "out").glob("form-67*")) == [
        "form-67-debug.csv"
    ]
    assert any(
        line.lstrip().startswith(f"{cli._RUN_ALL_STATUS_LABEL['manual_fill']}  form-67")
        for line in capsys.readouterr().err.splitlines()
    )


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("tax_payable", ["0", "0.10"])
def test_run_all_zero_credit_refuses_before_any_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tax_payable: str,
    format_id: str,
) -> None:
    """A ₹0.10 credit files as 0 (#898), so it is refused like a zero credit,
    in each format, before any schedule computes or writes Schedule TR."""
    spies = [_spy(monkeypatch, cli, name) for name in RUN_ALL_FY_STEPS]
    out = tmp_path / "out"
    out.mkdir()
    for stale in ("schedule-os.csv", "schedule-fsi.xlsx", "form-67.csv", "form-67-debug.csv"):
        (out / stale).write_text("stale", encoding="utf-8")
    rc = _run_all_with(
        tmp_path,
        config_text=_USER_TOML
        + '\n[form67]\n[[form67.tax_payable_overrides]]\ncountry = "US"\n'
        + f'source = "DIVIDEND"\ntax_payable_normal_inr = "{tax_payable}"\n',
        format_id=format_id,
    )
    assert rc == 4
    assert [spy.call_count for spy in spies] == [0] * len(RUN_ALL_FY_STEPS)
    assert list(out.iterdir()) == []
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in capsys.readouterr().err


@pytest.mark.parametrize(
    "fixture",
    [
        "e2e_activity_statement_form67_same_year_wht_refund_fy2025_26.csv",
        "e2e_activity_statement_form67_tax_above_income_fy2025_26.csv",
    ],
)
def test_run_all_unproven_source_rate_refuses_before_any_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture: str,
) -> None:
    """A col 7 override on a row with no source-currency rate refuses (#767)."""
    spies = [_spy(monkeypatch, cli, name) for name in RUN_ALL_FY_STEPS]
    rc = _run_all_with(
        tmp_path,
        config_text=_USER_TOML + FORM67_READY_OVERRIDE,
        format_id="review-csv",
        input_path=_FIXTURES / fixture,
    )
    assert rc == 4
    assert [spy.call_count for spy in spies] == [0] * len(RUN_ALL_FY_STEPS)
    assert list((tmp_path / "out").glob("form-67*")) == []
    assert "US/DIVIDEND: the source-currency tax rate can't be established" in (
        capsys.readouterr().err
    )


# --- A console that breaks mid-run (#925) ----------------------------------------


@pytest.mark.parametrize("stderr_closed", [False, True], ids=["stdout", "stdout-and-stderr"])
def test_run_all_process_on_a_closed_pipe_exits_4_and_removes_every_output(
    tmp_path: Path, stderr_closed: bool
) -> None:
    """``run-all ... | head`` after the reader has exited: the first progress
    header fails, every target goes, an earlier run's included, and Python's
    flush at exit doesn't turn exit 4 into 120."""
    out = tmp_path / "out"
    _seed_every_target(out)
    write_ends: list[int] = []

    def closed_pipe() -> int:
        read_end, write_end = os.pipe()
        os.close(read_end)
        write_ends.append(write_end)
        return write_end

    try:
        result = subprocess.run(
            [sys.executable, "-m", "ibkr_fa.cli", *_run_all_argv(tmp_path, _FY_2025_26_INPUT)],
            stdout=closed_pipe(),
            stderr=closed_pipe() if stderr_closed else subprocess.PIPE,
            cwd=tmp_path,
            check=False,
            timeout=120,
        )
    finally:
        for write_end in write_ends:
            os.close(write_end)

    assert result.returncode == 4, result.stderr
    assert list(out.iterdir()) == []
    assert result.stderr == (
        None if stderr_closed else b"error: run-all: a file operation failed (EPIPE)\n"
    )


# --- One statement for both inputs (#927) ----------------------------------------


def test_run_all_fy_statement_as_both_inputs_fails_schedule_fa_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#927: the FY schedules accept the statement, Schedule FA refuses its
    period, and the run removes every target, an earlier run's included."""
    out = tmp_path / "out"
    _seed_every_target(out)

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, _FY_2025_26_INPUT) == 4

    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "period must end on Dec 31, 2025 and start on or before Jan 1, 2025" in err
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]


def test_run_all_total_typed_nav_row_in_the_cy_input_fails_schedule_fa_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1012: the FY schedules succeed, Schedule FA refuses the CY input's
    Total-type Net Asset Value row, and the run removes every target, an
    earlier run's included."""
    out = tmp_path / "out"
    _seed_every_target(out)
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(
        _CY_2025_INPUT.read_text(encoding="utf-8")
        + "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
        "Current Total,Change\n"
        "Net Asset Value,Total,Total,0,150.00,0,150.00,150.00\n",
        encoding="utf-8",
    )

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, cy_input) == 4

    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "is a 'Total' row, but the 'Net Asset Value' section defines no 'SubTotal'" in err
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]


_NAV_FIXTURE = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
_CONTRADICTION_REMEDY = (
    "The statement contradicts itself, so Schedule FA Table A2's closing balance can't be "
    "filed. Re-download the Activity Statement from IBKR or fix the file, then re-run."
)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param(
            "Net Asset Value,Data,Cash,0,100.00,0,100.00,100.00\n",
            "Net Asset Value,Data,Cash,0,50000.00,0,50000.00,50000.00\n",
            "ParseError in section 'Net Asset Value' at row 2, column 'Current Total': the "
            "asset-class rows' Current Total amounts don't add up to this Total row's Current "
            "Total within 0.01, or there are no asset-class rows and the Total isn't zero. "
            f"{_CONTRADICTION_REMEDY}",
            id="rows-disagree-with-total",
        ),
        pytest.param(
            "Change in NAV,Data,Ending Value,2107.60\n",
            "Change in NAV,Data,Ending Value,2207.60\n",
            "ParseError in section 'Change in NAV' at row 0, column 'Field Value': this Ending "
            "Value differs by more than 0.01 from the Net Asset Value figure (its Total row's "
            "Current Total or, with no Total row, the sum of its asset-class rows). "
            f"{_CONTRADICTION_REMEDY}",
            id="ending-value-disagrees-with-total",
        ),
    ],
)
def test_run_all_nav_figures_that_disagree_in_the_cy_input_fail_schedule_fa_and_remove_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, error: str
) -> None:
    """#1309: with a custodian configured, Schedule FA used to leave A2's
    closing balance blank (manual-fill, exit 3). It now refuses the CY
    input, and the run removes every target, an earlier run's included."""
    out = tmp_path / "out"
    _seed_every_target(out)
    text = _NAV_FIXTURE.read_text(encoding="utf-8")
    assert text.count(old) == 1
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(text.replace(old, new), encoding="utf-8")
    argv = [*_run_all_argv(tmp_path, _FY_2025_26_INPUT), "--cy-input", str(cy_input)]
    config = tmp_path / "user.toml"
    config.write_text(config.read_text(encoding="utf-8") + CUSTODIAN_TOML, encoding="utf-8")

    assert main(argv) == 4

    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert error in err
    for figure in ("50000", "2207", "2107"):
        assert figure not in err, figure
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]


def test_run_all_flex_cy_input_without_trades_fails_schedule_fa_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1286: the FY schedules succeed, Schedule FA refuses a Flex Query CY
    input with no Trades section, which can't show the holdings sold during
    the year, and the run removes every target, an earlier run's included."""
    out = tmp_path / "out"
    _seed_every_target(out)
    sections, trades = (
        (_FIXTURES / "e2e_year_end_2025.csv").read_text(encoding="utf-8").rsplit("\n\n", 1)
    )
    assert trades.startswith('"CurrencyPrimary"') and '"TradeDate"' in trades.splitlines()[0]
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(sections + "\n" + eof_record(), encoding="utf-8")

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, cy_input) == 4

    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert (
        f"ParseError in section 'Trades' at row 0: required section 'Trades' not found in "
        f"{cy_input}. Schedule FA must list every holding held during the calendar year"
    ) in err
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]


def test_run_all_flex_cy_input_without_records_fails_schedule_fa_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1313: the FY schedules succeed, Schedule FA refuses a Flex Query CY
    input without header and trailer records, whose period it can't check,
    and the run removes every target, an earlier run's included."""
    out = tmp_path / "out"
    _seed_every_target(out)
    lines = (_FIXTURES / "e2e_year_end_2025.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith('"BOF",') and lines[-1].startswith('"EOF",')
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text("\n".join(lines[1:-1]) + "\n", encoding="utf-8")

    assert _run_all_both(tmp_path, _FY_2025_26_INPUT, cy_input) == 4

    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert (
        "ParseError in section '<file>' at row 0: this Flex Query has no header and trailer "
        "records, so the dates it covers can't be checked"
    ) in err
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    errors = [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]
    assert errors == ["schedule-fa"]


def test_run_all_cy_statement_as_both_inputs_refuses_at_the_preflight_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#927: the Form 67 preflight refuses the period before any schedule runs
    and removes every target, an earlier run's included."""
    invoke = _spy(monkeypatch, cli, "_invoke_run_all_handler")
    out = tmp_path / "out"
    _seed_every_target(out)

    assert _run_all_both(tmp_path, _CY_2025_INPUT, _CY_2025_INPUT) == 4

    assert invoke.call_count == 0
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "does not fully contain the Indian financial year 2025-26" in err
    assert "Form 67 foreign-tax-credit reporting" in err


@pytest.mark.parametrize("earlier_output", [False, True], ids=["fresh", "earlier-output"])
@pytest.mark.parametrize("flag", ["--fy-input", "--cy-input"])
def test_run_all_looping_input_exits_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, earlier_output: bool
) -> None:
    """#927: a symlink loop is no ``RuntimeError`` on Python 3.12. With an
    earlier output, the input-collision guard refuses and removes nothing
    (#673), naming the loop's errno, not its strerror (#1048); otherwise the
    first read of the input fails and the run removes every target."""
    loop = tmp_path / "loop.csv"
    loop.symlink_to(loop.name)
    out = tmp_path / "out"
    kept = _seed_every_target(out) if earlier_output else []
    fy_input, cy_input = (
        (loop, _CY_2025_INPUT) if flag == "--fy-input" else (_FY_2025_26_INPUT, loop)
    )

    assert _run_all_both(tmp_path, fy_input, cy_input) == 4

    assert sorted(path.name for path in out.iterdir()) == kept
    err = capsys.readouterr().err
    if earlier_output:
        assert err == (
            f"error: could not check output target {out / 'schedule-os.csv'} "
            f"against {flag} {loop} (ELOOP)\n"
        )
        assert os.strerror(errno.ELOOP).lower() not in err.lower()
    else:
        # The input's read error, named by its errno name only (#1049), not the guard.
        assert "a file operation failed (ELOOP)\n" in err
        assert os.strerror(errno.ELOOP).lower() not in err.lower()
        assert f"against {flag} {loop}" not in err
