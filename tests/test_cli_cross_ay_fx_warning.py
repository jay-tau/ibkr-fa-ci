"""The cross-AY FX warning that five report commands print on stderr (#1645).

When a run's FX policy has not been verified for its assessment year,
``netbenefits-income``, ``stockplan-income`` and ``netbenefits-cg`` print one
sentence, and ``fidelity-cma-income`` and ``fidelity-cma-cg`` another, as the
first stderr line, before every other warning or advisory. A verified run
prints neither. Each test stubs the command's pipeline with a synthetic report
that also carries a later stderr line.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import FidelityCmaCgRunReport
from ibkr_fa.pipeline_fidelity_cma_income import FidelityCmaIncomeRunReport
from ibkr_fa.pipeline_netbenefits_cg import NetBenefitsCgRunReport
from ibkr_fa.pipeline_netbenefits_income import NetBenefitsIncomeRunReport
from ibkr_fa.pipeline_stockplan_income import StockplanIncomeRunReport

# Written out here, not read from ``ibkr_fa.cli``, so a changed word fails.
_NETBENEFITS_WARNING = (
    "warning: the FX policy has not been verified for this assessment year; "
    "pass --suppress-cross-ay-warning once you have independently verified it (see #126)."
)
_CMA_WARNING = (
    "warning: the FX policy has not been verified for this assessment year; "
    "pass --suppress-cross-ay-warning after independent verification."
)
_FY = FinancialYear(2025)
_MANUAL_FILL = "a synthetic manual-fill line"


def _netbenefits_income(output_dir: Path, verified: bool) -> NetBenefitsIncomeRunReport:
    return NetBenefitsIncomeRunReport(
        output_dir=output_dir,
        financial_year=_FY,
        os_foreign_rows=0,
        fsi_rows=0,
        tr_rows=0,
        form67_entries=0,
        dividend_count=0,
        withholding_count=0,
        cg_stcg_rows=0,
        cg_ltcg_rows=0,
        manual_fill_warnings=(_MANUAL_FILL,),
        fx_policy_appropriate_for_ay=verified,
        form67_readiness=Form67Readiness.READY,
        form67_ready_with_fractional_rate=False,
        relief_taxpayer_id_blank=False,
        quicko_deductor_blank=False,
        run_outputs=RunOutputs((), ()),
    )


def _stockplan_income(output_dir: Path, verified: bool) -> StockplanIncomeRunReport:
    return StockplanIncomeRunReport(
        output_dir=output_dir,
        financial_year=_FY,
        os_foreign_rows=0,
        fsi_rows=0,
        tr_rows=0,
        form67_entries=0,
        dividend_count=0,
        withholding_count=0,
        manual_fill_warnings=(_MANUAL_FILL,),
        fx_policy_appropriate_for_ay=verified,
        withholding_total_usd=Decimal("0"),
        form67_readiness=Form67Readiness.READY,
        manual_fill_required=True,
        run_outputs=RunOutputs((), ()),
    )


def _netbenefits_cg(output_dir: Path, verified: bool) -> NetBenefitsCgRunReport:
    """No in-FY rows and one out-of-FY disposal, so the out-of-FY warning follows."""
    return NetBenefitsCgRunReport(
        output_dir=output_dir,
        financial_year=_FY,
        stcg_rows=0,
        ltcg_rows=0,
        total_stcg_inr_rounded=Decimal("0"),
        total_ltcg_inr_rounded=Decimal("0"),
        out_of_fy_disposals=1,
        fx_policy_appropriate_for_ay=verified,
        run_outputs=RunOutputs((), ()),
        table_f_loss_across_bands=(),
    )


def _cma_income(output_dir: Path, verified: bool) -> FidelityCmaIncomeRunReport:
    return FidelityCmaIncomeRunReport(
        output_dir=output_dir,
        financial_year=_FY,
        dividend_count=0,
        interest_count=0,
        interest_total_inr_rounded=Decimal("0"),
        actual_withholding_count=0,
        eligible_withholding_count=0,
        blocked_refund_years=(),
        os_foreign_rows=0,
        os_interest_rows=0,
        fsi_rows=0,
        tr_rows=0,
        form67_entries=0,
        cg_stcg_rows=0,
        cg_ltcg_rows=0,
        manual_fill_warnings=(_MANUAL_FILL,),
        fx_policy_appropriate_for_ay=verified,
        form67_readiness=Form67Readiness.READY,
        warnings=("a synthetic advisory",),
        run_outputs=RunOutputs((), ()),
    )


def _cma_cg(output_dir: Path, verified: bool) -> FidelityCmaCgRunReport:
    """No in-FY rows and one out-of-FY disposal, so the out-of-FY warning follows."""
    return FidelityCmaCgRunReport(
        output_dir=output_dir,
        financial_year=_FY,
        statement_count=1,
        sale_count=0,
        closed_lot_count=0,
        stcg_rows=0,
        ltcg_rows=0,
        out_of_fy_disposals=1,
        total_stcg_inr_rounded=Decimal("0"),
        fx_policy_appropriate_for_ay=verified,
        run_outputs=RunOutputs((), ()),
        table_f_loss_across_bands=(),
    )


# Each command: the pipeline its handler calls, its report and its warning.
_COMMANDS: dict[str, tuple[str, Callable[[Path, bool], object], str]] = {
    "netbenefits-income": ("run_netbenefits_income", _netbenefits_income, _NETBENEFITS_WARNING),
    "stockplan-income": ("run_stockplan_income", _stockplan_income, _NETBENEFITS_WARNING),
    "netbenefits-cg": ("run_netbenefits_cg", _netbenefits_cg, _NETBENEFITS_WARNING),
    "fidelity-cma-income": ("run_fidelity_cma_income", _cma_income, _CMA_WARNING),
    "fidelity-cma-cg": ("run_fidelity_cma_cg", _cma_cg, _CMA_WARNING),
}


def _run(command: str, output_dir: Path) -> None:
    tax_forms = ["--tax-forms", "tax-forms"] if command == "fidelity-cma-income" else []
    main(
        [
            command,
            *("--input", "statements"),
            *tax_forms,
            *("--config", "user.toml"),
            *("--output-dir", str(output_dir)),
            *("--financial-year", "2025-26"),
            *("--itr-form", "ITR-2"),
        ]
    )


@pytest.mark.parametrize("command", _COMMANDS)
def test_an_unverified_fx_policy_prints_the_warning_first(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipeline, report, warning = _COMMANDS[command]
    unverified = report(tmp_path, False)
    monkeypatch.setattr(f"ibkr_fa.cli.{pipeline}", lambda **_kwargs: unverified)

    _run(command, tmp_path)

    lines = capsys.readouterr().err.splitlines()
    assert warning in lines
    assert lines[0] == warning


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_verified_fx_policy_prints_no_warning(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipeline, report, _warning = _COMMANDS[command]
    verified = report(tmp_path, True)
    monkeypatch.setattr(f"ibkr_fa.cli.{pipeline}", lambda **_kwargs: verified)

    _run(command, tmp_path)

    err = capsys.readouterr().err
    assert _NETBENEFITS_WARNING not in err
    assert _CMA_WARNING not in err
