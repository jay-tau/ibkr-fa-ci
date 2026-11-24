"""Tests for the ``netbenefits-all`` orchestrator (#355).

The three per-command handlers are tested in their own suites; here we pin the
ORCHESTRATION: it runs all three against one bundle, builds each sub-command's
namespace correctly (FA is calendar-year based; income/cg are FY based, and
all-mode includes CG in FSI),
continues past a per-command failure, aggregates the worst exit code, and prints
a status-first summary. Most tests stub all three handlers (``_patch_handlers``)
so they don't re-run the pipelines. Some run real code on synthetic inputs:
those through ``_run_real_income`` run the real Form 67 readiness preflight;
if it succeeds, they run the real income leg unless ``stub_income`` and the
real capital-gains leg when ``stub_cg=False`` (FA stays stubbed, except in the
namespace test, which runs the three real handlers over stubbed pipelines);
the renamed closed-lots test runs all three legs for real; and the Dec-31
close tests run the real FA leg alone.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import ibkr_fa.cli
from ibkr_fa import output
from ibkr_fa.cli import (
    _EXIT_DOMAIN_ERROR,
    _EXIT_GENERIC,
    _EXIT_MANUAL_FILL_REQUIRED,
    _EXIT_OK,
    _RUN_ALL_RULE,
    _RUN_ALL_STATUS_LABEL,
)
from ibkr_fa.exceptions import ConfigError, IbkrFaError
from ibkr_fa.output import CommandFormats, Subcommand
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.parser.netbenefits.statement_pdf import StatementData, StatementWithholding
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from tests._crash_reports import assert_no_markers, crash_pattern
from tests._form67_overrides import FORM67_READY_OVERRIDE as _READY_OVERRIDE
from tests._netbenefits import CG_SHORT_REFUSAL as _CG_SHORT_REFUSAL
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CLOSED_RENAMED_PROCEEDS_CSV,
    CLOSED_RENAMED_PROCEEDS_REFUSAL,
    OPEN_CSV,
)
from tests._netbenefits_e2e import SESSION_REFUSALS, session_refusal_bundle
from tests._netbenefits_income import ACCOUNT_MARKER as _ACCOUNT_MARKER
from tests._netbenefits_income import ALL_COVERAGE_GAP_REFUSAL as _ALL_COVERAGE_GAP_REFUSAL
from tests._netbenefits_income import ALL_SHORT_COVERAGE_REFUSAL as _ALL_SHORT_COVERAGE_REFUSAL
from tests._netbenefits_income import BLANK_DEDUCTOR as _BLANK_DEDUCTOR
from tests._netbenefits_income import CG_COVER_FROM_CORRECTED_DATE as _CG_COVER_FROM_CORRECTED_DATE
from tests._netbenefits_income import CG_COVERING as _CG_COVERING
from tests._netbenefits_income import CG_OPENING_REFUSAL as _CG_OPENING_REFUSAL
from tests._netbenefits_income import CORRECTED_OPENING as _CORRECTED_OPENING
from tests._netbenefits_income import COVERING as _COVERING
from tests._netbenefits_income import GAP_IN_JANUARY as _GAP_IN_JANUARY
from tests._netbenefits_income import IN_FY_REFUSAL as _IN_FY_REFUSAL
from tests._netbenefits_income import INCOME_CONFIG
from tests._netbenefits_income import MISMATCH_1042S_REFUSAL as _1042S_MISMATCH_REFUSAL
from tests._netbenefits_income import MISTAKEN_OPENING as _MISTAKEN_OPENING
from tests._netbenefits_income import QUIET as _QUIET
from tests._netbenefits_income import SHORT_OF_THE_FY_END as _SHORT_OF_THE_FY_END
from tests._netbenefits_income import TAXES_TABLE as _TAXES_TABLE
from tests._netbenefits_income import TEN_DAYS_AFTER as _TEN_DAYS_AFTER
from tests._netbenefits_income import TIN_MARKER as _TIN_MARKER
from tests._netbenefits_income import YE_FILE as _YE_FILE
from tests._netbenefits_income import YE_MARKERS as _YE_MARKERS
from tests._netbenefits_income import event_at as _event_at
from tests._netbenefits_income import income_statement as _statement
from tests._netbenefits_income import january_statements as _january_statements
from tests._netbenefits_income import marked_ready_run as _marked_ready_run
from tests._netbenefits_income import missing_event_refusal as _missing_event_refusal
from tests._netbenefits_income import opening_date_refusal as _opening_date_refusal
from tests._netbenefits_income import patch_statements as _patch_statements
from tests._netbenefits_income import without_withholding_agent as _without_withholding_agent
from tests._netbenefits_income import year_end_refusal as _year_end_refusal
from tests._netbenefits_income import year_end_report as _year_end
from tests._oserror_markers import assert_no_oserror_markers as _assert_no_markers
from tests._oserror_markers import marked_oserror as _marked_oserror
from tests._source_all import INCOME_DEBUG_OUTPUTS as _INCOME_DEBUG
from tests._source_all import INCOME_OUTPUTS as _INCOME
from tests._source_all import NETBENEFITS_ALL_OK as _ALL_OK
from tests._source_all import NETBENEFITS_ALL_OUTPUTS as _ALL_OUTPUTS
from tests._source_all import SUMMARY_FOOTER as _SUMMARY_FOOTER
from tests._source_all import patch_netbenefits_handlers as _patch_handlers
from tests._source_all import real_netbenefits_fa as _real_netbenefits_fa
from tests._source_all import run_netbenefits_all as _run
from tests._source_all import run_netbenefits_all_real_income as _run_real_income
from tests._source_all import write_netbenefits_promised as _write_promised
from tests._source_all import write_stale_netbenefits_outputs as _stale_all_outputs


def test_runs_all_three_and_creates_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    assert rc == _EXIT_OK
    assert set(calls) == {"fa", "income", "cg"}
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert capsys.readouterr().err.splitlines()[-1] == _SUMMARY_FOOTER


def test_worst_exit_code_is_domain_error_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # income errors; FA + CG succeed. The run continues, worst code wins, and
    # the run leaves no output.
    cache = tmp_path / "cache"
    cache.mkdir()
    cached_price = cache / "price.csv"
    cached_price.write_bytes(b"cached price")
    rc, calls, out = _run(
        tmp_path,
        monkeypatch,
        {"fa": _EXIT_OK, "income": _EXIT_DOMAIN_ERROR, "cg": _EXIT_OK},
        extra=("--price-cache-dir", str(cache)),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"fa", "income", "cg"}  # continued past the failure
    assert list(out.iterdir()) == []
    assert calls["fa"].price_cache_dir == cache
    assert cached_price.read_bytes() == b"cached price"
    lines = capsys.readouterr().err.splitlines()
    assert f"  {_RUN_ALL_STATUS_LABEL['error']}  netbenefits-income" in lines
    for name in ("netbenefits-fa", "netbenefits-cg"):
        assert (
            f"  {_RUN_ALL_STATUS_LABEL['ok']}  {name}  (output removed because the run failed)"
            in lines
        )


def test_manual_fill_aggregates_when_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FA's normal success is exit 3 (manual-fill A2 peak); others clean → 3.
    rc, _, out = _run(
        tmp_path,
        monkeypatch,
        {"fa": _EXIT_MANUAL_FILL_REQUIRED, "income": _EXIT_OK, "cg": _EXIT_OK},
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert capsys.readouterr().err.splitlines()[-1] == _SUMMARY_FOOTER


def test_unclearable_target_stops_before_every_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    target = out / "schedule-fa-a3.csv"
    target.mkdir(parents=True)
    for names in ibkr_fa.cli._NETBENEFITS_ALL_TARGETS.values():
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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConfigError(field="form67", message="readiness unavailable"), _EXIT_DOMAIN_ERROR),
        (RuntimeError("boom"), _EXIT_GENERIC),
    ],
    ids=["domain-error", "crash"],
)
def test_readiness_failure_stops_before_every_leg_and_clears_every_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected: int,
) -> None:
    calls = _patch_handlers(monkeypatch, _ALL_OK)
    preflight = Mock(side_effect=error)
    monkeypatch.setattr("ibkr_fa.cli.netbenefits_form67_readiness", preflight)
    out = tmp_path / "out"
    out.mkdir()
    for name in (
        *_ALL_OUTPUTS,
        *_INCOME_DEBUG,
        "schedule-fa-quicko.xlsx",
        "schedule-fa.csv",
        "schedule-fa-a3-a2.csv",
    ):
        (out / name).write_text("stale", encoding="utf-8")

    rc, _, out = _run(
        tmp_path, monkeypatch, _ALL_OK, extra=("--debug", "--quicko-workbook"), patch=False
    )
    assert rc == expected
    preflight.assert_called_once()
    assert calls == {}
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    if expected == _EXIT_GENERIC:
        # The class and source line, never the message (#1049).
        assert re.fullmatch(
            "error: netbenefits-all: the Form 67 readiness check crashed unexpectedly "
            rf"\({crash_pattern(RuntimeError)}\)\n",
            captured.err,
        )
    else:
        assert str(error) in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("key", "missing", "extra"),
    [
        ("cg", "schedule-cg-ltcg.csv", ()),
        ("fa", "schedule-fa-a2.csv", ()),
        ("fa", "schedule-fa-quicko.xlsx", ("--quicko-workbook",)),
        ("income", "form-67-debug.csv", ("--debug",)),
    ],
)
def test_leg_missing_one_of_its_promised_outputs_fails_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    missing: str,
    extra: tuple[str, ...],
) -> None:
    calls = _patch_handlers(monkeypatch, _ALL_OK)

    def writes_all_but_one(args: argparse.Namespace) -> int:
        calls[key] = args
        _write_promised(key, args)
        out = args.output.parent if key == "fa" else args.output_dir
        (out / missing).unlink()
        return _EXIT_OK

    monkeypatch.setattr(
        {
            "fa": "ibkr_fa.cli._run_netbenefits_fa",
            "income": "ibkr_fa.cli._run_netbenefits_income",
            "cg": "ibkr_fa.cli._run_netbenefits_cg",
        }[key],
        writes_all_but_one,
    )
    rc, _, out = _run(tmp_path, monkeypatch, _ALL_OK, extra=extra, patch=False)
    assert rc == _EXIT_DOMAIN_ERROR
    assert set(calls) == {"fa", "income", "cg"}
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert "exited 0 without its promised output" in err
    assert str(out / missing) in err


@pytest.mark.parametrize(
    ("income_code", "expected"),
    [(_EXIT_DOMAIN_ERROR, _EXIT_DOMAIN_ERROR), (None, _EXIT_GENERIC)],
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

        monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_income", crash)
    finished: list[bool] = []
    cg = ibkr_fa.cli._run_netbenefits_cg

    def last_leg(args: argparse.Namespace) -> int:
        finished.append(True)
        return cg(args)

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_cg", last_leg)
    stuck = tmp_path / "out" / "schedule-fa-a3.csv"
    unlink = Path.unlink

    def unlink_fails_after_the_run(path: Path, missing_ok: bool = False) -> None:
        if finished and path == stuck:
            raise _marked_oserror()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_fails_after_the_run)
    rc, _, out = _run(tmp_path, monkeypatch, _ALL_OK, patch=False)
    assert rc == expected
    assert set(calls) == {"fa", "income", "cg"}
    assert [path.name for path in out.iterdir()] == [stuck.name]
    err = capsys.readouterr().err
    assert (
        f"error: the run failed, and its output may still be there: could not remove {stuck} "
        "(EACCES)" in err.splitlines()
    )
    _assert_no_markers(err)
    assert "(output removed because the run failed)" not in err
    assert "(the run failed: do NOT file this output)" in err


def test_fa_namespace_is_calendar_year_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls, out = _run(tmp_path, monkeypatch, _ALL_OK)
    fa = calls["fa"]
    assert fa.output == out / "schedule-fa-a3.csv"
    assert fa.calendar_year == 2026  # FY 2026-27 -> CY 2026
    assert fa.format_id == "filing-csv"
    assert fa.itr_form == "ITR-2"
    assert fa.quicko_workbook is False


def test_fa_format_follows_the_declaration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The FA leg's format is netbenefits-all's declared default (#1206)."""
    changed = CommandFormats((Subcommand.FA, Subcommand.FA_A2), (), "review-csv")
    monkeypatch.setitem(cast(Any, output._COMMAND_FORMATS), "netbenefits-all", changed)
    _, calls, _ = _run(tmp_path, monkeypatch, _ALL_OK)
    assert calls["fa"].format_id == "review-csv"


def test_quicko_workbook_request_reaches_the_fa_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, calls, out = _run(tmp_path, monkeypatch, _ALL_OK, extra=("--quicko-workbook",))
    assert rc == _EXIT_OK
    assert calls["fa"].quicko_workbook is True
    assert (out / "schedule-fa-quicko.xlsx").is_file()


def test_income_and_cg_namespaces_are_fy_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls, out = _run(
        tmp_path,
        monkeypatch,
        _ALL_OK,
        extra=("--reconcile-1042s-fy-withholding-usd", "1.50", "--debug"),
    )
    assert calls["income"].output_dir == out
    assert calls["income"].financial_year.start_year == 2026
    assert calls["income"].reconcile_1042s_usd == Decimal("1.50")
    assert calls["income"].include_capital_gains is True
    assert calls["income"].itr_form == "ITR-2"
    assert calls["income"].debug is True
    assert calls["cg"].output_dir == out
    assert calls["cg"].financial_year.start_year == 2026
    assert calls["cg"].itr_form == "ITR-2"


def test_summary_printed_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, monkeypatch, {"fa": _EXIT_OK, "income": _EXIT_DOMAIN_ERROR, "cg": _EXIT_OK})
    err = capsys.readouterr().err
    assert "netbenefits-all summary" in err
    assert "netbenefits-income" in err


def test_stale_outputs_cleared_before_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: every target is cleared before the first leg runs, so a
    # refused/no-op leg leaves no stale prior-run CSV.
    _patch_handlers(monkeypatch, _ALL_OK)
    present_at_first_leg: list[Path] = []
    fa = ibkr_fa.cli._run_netbenefits_fa

    def first_leg(args: argparse.Namespace) -> int:
        present_at_first_leg.extend(f for f in stale if f.exists())
        return fa(args)

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", first_leg)
    out = tmp_path / "out"
    out.mkdir()
    stale = [
        out / f
        for f in (
            "schedule-fa.csv",
            "schedule-fa-a3.csv",
            "schedule-fa-a2.csv",
            "schedule-fa-quicko.xlsx",
            "schedule-fa-a3-a2.csv",
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
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert present_at_first_leg == []


def test_namespaces_are_complete_for_real_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Form 67 preflight passes, then the three real handlers run over
    stubbed pipelines that record the call and raise a domain error. A handler
    that reads, without a default, an attribute its synthesized Namespace lacks
    raises AttributeError before its pipeline is called: the run exits 1 and
    that stub isn't called. Each stub called once and exit 4 prove every such
    attribute is set."""
    called: list[str] = []

    def stub(leg: str):
        def boom(**_kwargs: object) -> object:
            called.append(leg)
            raise IbkrFaError("stub domain error")

        return boom

    for leg in ("fa", "income", "cg"):
        monkeypatch.setattr(f"ibkr_fa.cli.run_netbenefits_{leg}", stub(leg))
    rc, _calls, _out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_fa=False,
        stub_income=False,
        stub_cg=False,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert sorted(called) == ["cg", "fa", "income"]


def test_survives_unexpected_handler_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-domain crash in one leg must NOT stop the others or suppress the
    # summary; it aggregates to the internal-error code.
    calls = _patch_handlers(monkeypatch, _ALL_OK)

    def crash(args: argparse.Namespace) -> int:
        calls["fa"] = args
        raise RuntimeError("boom")

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", crash)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == _EXIT_GENERIC
    assert set(calls) == {"fa", "income", "cg"}  # income + cg ran after fa crashed
    assert "netbenefits-all summary" in capsys.readouterr().err
    assert list((tmp_path / "out").iterdir()) == []


def test_failed_leg_output_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A leg that writes a (partial) file then returns a failure code must leave
    # no output, so the summary's "ERROR = not produced" stays true.
    _patch_handlers(monkeypatch, _ALL_OK)

    def income_writes_then_fails(args: argparse.Namespace) -> int:
        (args.output_dir / "schedule-os.csv").write_text("partial", encoding="utf-8")
        return _EXIT_DOMAIN_ERROR

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_income", income_writes_then_fails)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []


def test_crashed_leg_partial_output_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_handlers(monkeypatch, _ALL_OK)

    def cg_writes_then_crashes(args: argparse.Namespace) -> int:
        (args.output_dir / "schedule-cg.csv").write_text("partial", encoding="utf-8")
        raise RuntimeError("boom mid-write")

    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_cg", cg_writes_then_crashes)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "user.toml"),
            "--output-dir",
            str(out),
            "--financial-year",
            "2026-27",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == _EXIT_GENERIC
    assert list(out.iterdir()) == []


# --- Form 67 readiness (#601) ---------------------------------------------------

_ZERO_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "0"
"""


# A BLANK row writes no Quicko form, so the withholding agent isn't needed.
@pytest.mark.parametrize(
    "config",
    [INCOME_CONFIG, _without_withholding_agent(INCOME_CONFIG)],
    ids=["withholding-agent", "no-withholding-agent"],
)
def test_blank_form67_income_leg_is_manual_with_debug_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: str,
) -> None:
    rc, _calls, out = _run_real_income(tmp_path, monkeypatch, config=config, stub_income=False)
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert sorted(path.name for path in out.glob("form-67*")) == ["form-67-debug.csv"]
    assert f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  netbenefits-income" in (
        capsys.readouterr().err.splitlines()
    )


@pytest.mark.parametrize(("tin_country", "manual"), [("GB", True), ("US", False)])
def test_blank_relief_tin_income_leg_is_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tin_country: str,
    manual: bool,
) -> None:
    config, statements = _marked_ready_run(tin_country)
    rc, _calls, _out = _run_real_income(
        tmp_path, monkeypatch, config=config, statements=statements, stub_income=False
    )
    assert rc == (_EXIT_MANUAL_FILL_REQUIRED if manual else _EXIT_OK)
    captured = capsys.readouterr()
    status = _RUN_ALL_STATUS_LABEL["manual_fill" if manual else "ok"]
    assert f"  {status}  netbenefits-income" in captured.err.splitlines()
    assert_no_markers(captured.out + captured.err, _TIN_MARKER, _ACCOUNT_MARKER)


@pytest.mark.parametrize("agent", [True, False], ids=["agent", "no-agent"])
def test_zero_credit_refuses_before_any_leg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: bool,
) -> None:
    config = INCOME_CONFIG if agent else _without_withholding_agent(INCOME_CONFIG)
    rc, calls, out = _run_real_income(
        tmp_path, monkeypatch, config=config + _ZERO_OVERRIDE, stub_income=True
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    assert "US/DIVIDEND: the computed credit rounds to ₹0" in capsys.readouterr().err


@pytest.mark.parametrize("agent", [True, False], ids=["agent", "no-agent"])
def test_relief_the_portal_overwrites_refuses_before_any_leg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: bool,
) -> None:
    """#900: USD 0.57 withheld on a USD 1.46 dividend is above the 25% treaty
    rate, so the relief is capped below the tax paid and the portal's
    read-only FSI cell would overwrite it. A missing withholding agent doesn't
    change that (#1327)."""
    statements = dict(_CG_COVERING)
    statements["statement_2026-02.pdf"] = replace(
        statements["statement_2026-02.pdf"],
        withholdings=(StatementWithholding(date(2026, 3, 5), "EXAMPLE CORP", Decimal("-0.57")),),
    )

    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=(INCOME_CONFIG if agent else _without_withholding_agent(INCOME_CONFIG))
        + _READY_OVERRIDE,
        stub_income=True,
        statements=statements,
    )

    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    assert "diverge from the portal's read-only relief" in capsys.readouterr().err


def test_missing_withholding_agent_income_leg_is_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1327: the income leg writes every file with Quicko's deductor value
    blank and shows as manual-fill; the other legs run."""
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=_without_withholding_agent(INCOME_CONFIG) + _READY_OVERRIDE,
        stub_income=False,
    )
    assert rc == _EXIT_MANUAL_FILL_REQUIRED
    assert sorted(calls) == ["cg", "fa"]
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    tr_lines = (out / "schedule-tr.csv").read_text(encoding="utf-8-sig").splitlines()
    assert tr_lines.count("Name of the deductor,,,,") == 1
    err = capsys.readouterr().err.splitlines()
    assert err.count(f"warning: {_BLANK_DEDUCTOR}") == 1
    assert f"  {_RUN_ALL_STATUS_LABEL['manual_fill']}  netbenefits-income" in err


@pytest.mark.parametrize("proceeds_usd", ["300", "0"], ids=["positive", "zero-proceeds"])
def test_in_fy_sale_fails_income_and_cg_and_clears_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proceeds_usd: str,
) -> None:
    """#666: both capital-gains legs refuse an in-FY sale, which ibkr-fa can't yet
    check against the statements; a zero-proceeds row meets this refusal ahead of
    the compute's own (#894)."""
    _stale_all_outputs(tmp_path)
    sale = EspClosedLot(
        acquire_date=date(2025, 12, 1),
        sold_or_transferred_date=date(2026, 3, 20),
        quantity=Decimal("2"),
        proceeds_usd=Decimal(proceeds_usd),
        contribution_usd=Decimal("180"),
        fidelity_term="Short",
    )
    rc, _calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        closed_lots=(sale,),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    err = stderr.splitlines()
    assert err.count(f"error: {_IN_FY_REFUSAL}") == 2
    # #1042: the lot's dates and amounts are not printed.
    assert_no_markers(stderr, "2025-12-01", "2026-03-20", "180")
    for leg in ("netbenefits-income", "netbenefits-cg"):
        assert f"  {_RUN_ALL_STATUS_LABEL['error']}  {leg}" in err


@pytest.mark.parametrize("kind", ["dividend", "withholding"])
def test_opening_date_refusal_names_coverage_through_ten_days_after_the_fy_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """#1478: the income preflight refuses before any leg runs, so the income
    leg, which loads the same records, never reaches its own copy. The advice
    names the coverage the corrected run needs: with capital gains in the
    income leg, through ten days after the FY end."""
    _stale_all_outputs(tmp_path)
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=_MISTAKEN_OPENING + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements=_january_statements(kind),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    refusal = _opening_date_refusal(kind, _CG_COVER_FROM_CORRECTED_DATE)
    assert stderr == f"error: netbenefits-all: {refusal}\n"
    assert_no_markers(stderr, "2026-01-15", "2026-02-01")


def test_following_the_opening_date_advice_clears_both_coverage_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1478, the issue's example, rerun with the opening date corrected to
    January. Statements through the FY end, as netbenefits-income's advice
    says, still fail both capital-gains legs; adding the ten days after it, as
    the netbenefits-all advice says, clears every check: the income and
    capital-gains legs run for real, and the FA leg is a stub."""
    config = _CORRECTED_OPENING + _READY_OVERRIDE
    short, covered = tmp_path / "short", tmp_path / "covered"
    short.mkdir()
    covered.mkdir()
    rc, _calls, _out = _run_real_income(
        short,
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements=_january_statements("dividend"),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err.splitlines().count(f"error: {_CG_SHORT_REFUSAL}") == 2

    april = {"statement_2026-04.pdf": _statement(date(2026, 4, 1), date(2026, 4, 10))}
    rc, calls, out = _run_real_income(
        covered,
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements={**_january_statements("dividend"), **april},
    )
    assert rc == _EXIT_OK
    assert sorted(calls) == ["fa"]
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert "error:" not in capsys.readouterr().err


def test_following_the_withholding_opening_date_advice_meets_the_orphan_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1478, #1486: pins the defensive withholding opening-date refusal and
    what follows it, through the patched parser: no real statement holds a
    withholding with no same-date dividend (see ``_january_statements``). The
    chain a filer can meet is the dividend one, which
    ``test_following_the_opening_date_advice_clears_both_coverage_checks``
    follows. Here the refusal also says that no config change can match the
    withholding. With the date fixed and the window covered, the opening-date
    refusal clears and the preflight refuses the orphan it named."""
    mistaken, corrected = tmp_path / "mistaken", tmp_path / "corrected"
    mistaken.mkdir()
    corrected.mkdir()
    rc, _calls, _out = _run_real_income(
        mistaken,
        monkeypatch,
        config=_MISTAKEN_OPENING + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements=_january_statements("withholding"),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    refusal = _opening_date_refusal("withholding", _CG_COVER_FROM_CORRECTED_DATE)
    assert capsys.readouterr().err == f"error: netbenefits-all: {refusal}\n"

    rc, calls, _out = _run_real_income(
        corrected,
        monkeypatch,
        config=_CORRECTED_OPENING + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements={**_january_statements("withholding"), **_TEN_DAYS_AFTER},
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert capsys.readouterr().err == (
        f"error: netbenefits-all: the withholding at {_event_at(1, 1, _TAXES_TABLE)} "
        "matches 0 in-FY dividend(s); expected exactly one (ambiguous or orphan)\n"
    )


def test_reports_short_of_ten_days_after_the_fy_end_fail_both_capital_gains_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#666: income's own window ends at the FY end, so these reports pass it; the
    capital-gains legs need ten days more."""
    _stale_all_outputs(tmp_path)
    rc, _calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements=_COVERING,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    assert_no_markers(stderr, "XESP", "99999E104", "2025-11-01", "16000")
    err = stderr.splitlines()
    assert err.count(f"error: {_CG_SHORT_REFUSAL}") == 2
    for leg in ("netbenefits-income", "netbenefits-cg"):
        assert f"  {_RUN_ALL_STATUS_LABEL['error']}  {leg}" in err


@pytest.mark.parametrize(
    ("statements", "refusal"),
    [
        pytest.param({}, _ALL_SHORT_COVERAGE_REFUSAL, id="no-reports"),
        pytest.param(
            {name: stmt for name, stmt in _CG_COVERING.items() if name != "statement_2026-01.pdf"},
            _ALL_COVERAGE_GAP_REFUSAL,
            id="gap-inside-the-fy",
        ),
    ],
)
def test_income_coverage_refuses_before_any_real_capital_gains_leg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statements: dict[str, StatementData],
    refusal: str,
) -> None:
    """Pin (#666): real netbenefits-all already refuses these inputs in its
    income readiness check, before running any leg, naming the capital-gains
    window (#1486)."""
    _stale_all_outputs(tmp_path)
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements=statements,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    assert stderr == f"error: netbenefits-all: {refusal}\n"
    assert_no_markers(stderr, "XESP", "99999E104", "2025-11-01", "16000")


@pytest.mark.parametrize(
    ("statements", "refusal", "missing"),
    [
        pytest.param(
            _SHORT_OF_THE_FY_END,
            _ALL_SHORT_COVERAGE_REFUSAL,
            "statement_2026-02.pdf",
            id="short-end",
        ),
        pytest.param(
            _GAP_IN_JANUARY,
            _ALL_COVERAGE_GAP_REFUSAL,
            "statement_2026-01.pdf",
            id="gap-inside-the-window",
        ),
    ],
)
def test_following_the_coverage_refusal_clears_both_coverage_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statements: dict[str, StatementData],
    refusal: str,
    missing: str,
) -> None:
    """#1486: the income coverage refusal names the capital-gains window.
    Statements through the FY end, the window netbenefits-income names, still
    fail both capital-gains legs; supplying the named window (the ``missing``
    report and 1-10 April) clears every check and runs every leg."""
    config = INCOME_CONFIG + _READY_OVERRIDE
    for name in ("first", "income-window", "named-window"):
        (tmp_path / name).mkdir()
    rc, calls, _out = _run_real_income(
        tmp_path / "first",
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements=statements,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert capsys.readouterr().err == f"error: netbenefits-all: {refusal}\n"

    supplied = {**statements, missing: _COVERING[missing]}
    rc, _calls, _out = _run_real_income(
        tmp_path / "income-window",
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements=supplied,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert capsys.readouterr().err.splitlines().count(f"error: {_CG_SHORT_REFUSAL}") == 2

    rc, calls, out = _run_real_income(
        tmp_path / "named-window",
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements={**supplied, **_TEN_DAYS_AFTER},
    )
    assert rc == _EXIT_OK
    assert sorted(calls) == ["fa"]
    assert sorted(path.name for path in out.iterdir()) == _ALL_OUTPUTS
    assert "error:" not in capsys.readouterr().err


def test_opening_after_the_fy_end_fails_both_capital_gains_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#666: income's window is empty then, so its own check passes, but an empty
    capital-gains window can't prove no sale was missed, so the income and
    capital-gains legs both refuse."""
    _stale_all_outputs(tmp_path)
    config = (INCOME_CONFIG + _READY_OVERRIDE).replace(
        "account_opening_date = 2025-11-01", "account_opening_date = 2026-04-15"
    )
    quiet = {
        name: replace(stmt, dividends=(), withholdings=()) for name, stmt in _CG_COVERING.items()
    }
    rc, _calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=config,
        stub_income=False,
        stub_cg=False,
        statements=quiet,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    assert stderr.splitlines().count(f"error: {_CG_OPENING_REFUSAL}") == 2
    assert_no_markers(stderr, "XESP", "99999E104", "2026-04-15", "16000")


def test_1042s_mismatch_fails_income_leg_and_clears_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #918: the FA and CG legs write despite the income refusal; the run removes them.
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=False,
        extra=("--reconcile-1042s-fy-withholding-usd", "0.50"),
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert sorted(calls) == ["cg", "fa"]
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    err = stderr.splitlines()
    assert f"error: {_1042S_MISMATCH_REFUSAL}" in err
    assert f"  {_RUN_ALL_STATUS_LABEL['error']}  netbenefits-income" in err
    # #1042: neither the typed total nor the statement total (USD 0.36) is printed.
    assert_no_markers(stderr, "0.50", "0.36")


def test_overlapping_statements_that_disagree_refuse_before_any_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #943: the March statement leaves out the 5 March income that the
    # overlapping February-March statement lists. #1042: by period start the
    # February-March statement is statement 3 and the March one statement 4.
    out = tmp_path / "out"
    out.mkdir()
    for name in (*_INCOME, *_INCOME_DEBUG):
        (out / name).write_text("stale", encoding="utf-8")
    statements = {
        **_COVERING,
        "statement_2026-03.pdf": _statement(date(2026, 3, 1), date(2026, 3, 31)),
    }
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=True,
        statements=statements,
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert err == f"error: netbenefits-all: {_missing_event_refusal('dividend', 3, 1, 4)}\n"
    assert_no_markers(err, "99999E104", "2026-03-05", "2026-02-01", "2026-03-01")


def test_year_end_income_the_reports_dont_list_refuses_before_any_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1249: the income preflight refuses a year-end report's dividend that no Stock
    Plan Services report lists, before any leg runs, and clears every output."""
    _stale_all_outputs(tmp_path)
    rc, calls, out = _run_real_income(
        tmp_path,
        monkeypatch,
        config=INCOME_CONFIG + _READY_OVERRIDE,
        stub_income=False,
        stub_cg=False,
        statements={**_QUIET, _YE_FILE: _year_end()},
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert calls == {}
    assert list(out.iterdir()) == []
    stderr = capsys.readouterr().err
    assert stderr == f"error: netbenefits-all: {_year_end_refusal('dividend')}\n"
    assert_no_markers(stderr, *_YE_MARKERS)


def test_renamed_closed_lots_export_fails_every_leg_and_clears_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #911: all three legs run for real and each refuses at bundle discovery.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in _COVERING:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch_statements(monkeypatch, _COVERING)
    for name, text in (
        ("View open lots.csv", OPEN_CSV),
        ("View closed lots.csv", CLOSED_EMPTY_CSV),
        ("View closed lots (1).csv", CLOSED_RENAMED_PROCEEDS_CSV),
    ):
        (bundle / name).write_text(text, encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(INCOME_CONFIG + _READY_OVERRIDE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    for name in _ALL_OUTPUTS:
        (out / name).write_text("stale", encoding="utf-8")
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            "--input",
            str(bundle),
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
    assert rc == _EXIT_DOMAIN_ERROR
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err.splitlines()
    assert err.count(f"error: View closed lots (1).csv {CLOSED_RENAMED_PROCEEDS_REFUSAL}") == 3
    # #1041: the renamed header cell is file content.
    assert not any("proceeds (usd)" in line.casefold() for line in err)
    for leg in ("netbenefits-fa", "netbenefits-income", "netbenefits-cg"):
        assert f"  {_RUN_ALL_STATUS_LABEL['error']}  {leg}" in err


@SESSION_REFUSALS
def test_fa_leg_dec31_close_refusal_fails_the_run_and_clears_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
    fetcher: DailyCloseFetcher | None,
    refusal: str,
    markers: tuple[str, ...],
) -> None:
    """#1165: the real FA leg refuses a Dec-31 close off the required US market
    session (income and CG are stubbed). The run exits 4, leaves no output, and
    prints the leg's refusal in the orchestrator's frame."""
    calls = _patch_handlers(monkeypatch, _ALL_OK)
    monkeypatch.setattr("ibkr_fa.cli._run_netbenefits_fa", _real_netbenefits_fa)
    bundle, cfg, price_args = session_refusal_bundle(tmp_path, monkeypatch, override, fetcher)
    _stale_all_outputs(tmp_path)
    out = tmp_path / "out"
    rc = ibkr_fa.cli.main(
        [
            "netbenefits-all",
            *("--input", str(bundle), "--config", str(cfg), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--quicko-workbook"),
            *price_args,
        ]
    )
    assert rc == _EXIT_DOMAIN_ERROR
    assert sorted(calls) == ["cg", "income"]
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    removed = "(output removed because the run failed)"
    assert captured.err == "\n".join(
        (
            f"error: {refusal}",
            _RUN_ALL_RULE,
            "  netbenefits-all summary (FY 2025-26)",
            _RUN_ALL_RULE,
            f"  {_RUN_ALL_STATUS_LABEL['error']}  netbenefits-fa",
            f"  {_RUN_ALL_STATUS_LABEL['ok']}  netbenefits-income  {removed}",
            f"  {_RUN_ALL_STATUS_LABEL['ok']}  netbenefits-cg  {removed}",
            f"{_SUMMARY_FOOTER}\n",
        )
    )
    # Only each leg's banner: no report.
    assert captured.out == "".join(
        f"\n{_RUN_ALL_RULE}\n  netbenefits-all: {leg}\n{_RUN_ALL_RULE}\n"
        for leg in ("netbenefits-fa", "netbenefits-income", "netbenefits-cg")
    )
    assert_no_markers(captured.out + captured.err, *markers)
