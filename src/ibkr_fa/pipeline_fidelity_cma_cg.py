"""Fidelity CMA SPAXX Schedule CG pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.fidelity_cma_capital_gains import (
    FidelityCmaCgError,
    compute_fidelity_cma_capital_gains,
    in_fy_spaxx_sale_count,
)
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase
from ibkr_fa.config import load_user_config
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.output import RenderContext, Subcommand
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.parser.fidelity_cma import (
    CmaStatementBundle,
    load_fidelity_cma_statements,
)
from ibkr_fa.path_checks import is_dir
from ibkr_fa.pipeline import (
    FinancialYear,
)
from ibkr_fa.pipeline_fidelity_cma_income import (
    CARRIED_HOLDING_GAP,
    LATER_HOLDING_GAP,
    coverage_refusal,
    early_end_gap,
    fidelity_cma_inputs,
    late_start_gap,
)

_OUTPUT_FILENAMES = ("schedule-cg.csv", "schedule-cg-ltcg.csv")


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaCgRunReport:
    """Summary of one successful :func:`run_fidelity_cma_cg` call.

    ``table_f_loss_across_bands`` holds ``(Table F row, file name)`` for each
    review CSV that leaves its Schedule CG Table F date bands blank because a
    band nets a loss and two or more bands net gains, so the filer enters
    them (#1361).
    """

    output_dir: Path
    financial_year: FinancialYear
    statement_count: int
    sale_count: int
    closed_lot_count: int
    stcg_rows: int
    ltcg_rows: int
    out_of_fy_disposals: int
    total_stcg_inr_rounded: Decimal
    fx_policy_appropriate_for_ay: bool
    run_outputs: RunOutputs = field(kw_only=True)
    table_f_loss_across_bands: tuple[tuple[int, str], ...] = field(kw_only=True)


def _require_fy_coverage(
    bundle: CmaStatementBundle,
    financial_year: FinancialYear,
) -> None:
    statements = bundle.statements
    first = statements[0]
    end = f"31 March {financial_year.end_year}"
    gaps = []
    # A later start is an account opening during the FY; one after the FY
    # has no FY history to attest a nil result from.
    late = not bundle.covers_from(financial_year.start_date)
    if late:
        gaps.append(late_start_gap(financial_year))
    elif first.period_start > financial_year.end_date:
        gaps.append(f"the account opened after {end}, so it has no FY {financial_year} history")
    if statements[-1].period_end < financial_year.end_date:
        gaps.append(early_end_gap(end))
    # The run always FIFO-matches SPAXX, which needs history from before any
    # carried holding; only a carried balance has earlier months to add (#1515).
    if gaps and first.beginning_account_value_usd is not None:
        if late:
            gaps.append(LATER_HOLDING_GAP)
        elif first.position.beginning_market_value_usd is not None:
            gaps.append(CARRIED_HOLDING_GAP)
    if gaps:
        raise FidelityCmaCgError(
            coverage_refusal(financial_year, opened="later in the FY", end=end, gaps=gaps)
        )


def run_fidelity_cma_cg(
    *,
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    financial_year: FinancialYear,
    itr_form: ItrForm = "ITR-2",
    suppress_cross_ay_warning: bool = False,
) -> FidelityCmaCgRunReport:
    """Emit Schedule CG review CSVs for observed SPAXX redemptions."""
    if not is_dir(output_dir):
        raise FidelityCmaCgError("output path must be a directory")
    targets = tuple(output_dir / filename for filename in _OUTPUT_FILENAMES)
    remove_previous_outputs(targets, fidelity_cma_inputs(input_dir, config_path))

    config = load_user_config(config_path)
    if config.fidelity_cma is None:
        raise FidelityCmaCgError("missing [fidelity_cma] config")
    bundle, _year_ends, _record = load_fidelity_cma_statements(
        input_dir,
        expected_account_id=config.fidelity_cma.account_id,
        spaxx_isin=config.fidelity_cma.spaxx_isin,
    )
    _require_fy_coverage(bundle, financial_year)
    fx_ctx = TaxFxContext.for_fy(
        financial_year, config.fx, suppress_cross_ay_warning=suppress_cross_ay_warning
    )
    gains = compute_fidelity_cma_capital_gains(
        bundle, spaxx_isin=config.fidelity_cma.spaxx_isin, fx_ctx=fx_ctx
    )
    stcg = gains.stcg

    specs = (
        spec_for(Subcommand.CG, "review-csv", stcg, output_dir / _OUTPUT_FILENAMES[0]),
        spec_for(Subcommand.CG_LTCG, "review-csv", gains.ltcg, output_dir / _OUTPUT_FILENAMES[1]),
    )
    ctx = RenderContext(
        assessment_year_start=financial_year.assessment_year_start, itr_form=itr_form
    )
    # If anything raises from the first write to the report, remove both CSVs.
    run_outputs = RunOutputs(targets, ())
    with run_outputs.removed_on_failure():
        write_output_bundle(specs, ctx)

        return FidelityCmaCgRunReport(
            output_dir=output_dir,
            financial_year=financial_year,
            statement_count=len(bundle.statements),
            sale_count=in_fy_spaxx_sale_count(bundle, financial_year),
            closed_lot_count=len(gains.closed_lots),
            stcg_rows=len(stcg.stcg_lines),
            ltcg_rows=0,
            out_of_fy_disposals=len(stcg.excluded_out_of_fy),
            total_stcg_inr_rounded=stcg.portal_amounts.filed_total_inr,
            fx_policy_appropriate_for_ay=fx_ctx.fx_policy_appropriate_for_ay,
            run_outputs=run_outputs,
            table_f_loss_across_bands=tuple(
                (row, path.name)
                for row, section, path in ((3, stcg, targets[0]), (5, gains.ltcg, targets[1]))
                if section.table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
            ),
        )
