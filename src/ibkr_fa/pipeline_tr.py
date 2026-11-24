"""Pipeline orchestrator for Schedule TR (Summary of Tax Relief Claimed).

PR #217 step 3/N of #36 wires Schedule TR as a downstream aggregator
of Schedule FSI. The pipeline intentionally reuses
:func:`ibkr_fa.pipeline_fsi_compute._compute_schedule_fsi_for_pipeline`
as the shared seam between "parse + compute FSI" and per-schedule
"write + assemble report" work (rubber-duck design A). Schedule TR
therefore inherits the FSI parser, FX-policy, account-check, WHT
guard, and orphan-WHT protections transitively before collapsing
FSI rows to per-country foreign-tax totals.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.form67 import form67_override_advice
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.compute.schedule_tr import compute_schedule_tr_from_fsi
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline
from ibkr_fa.pipeline_tr_models import ScheduleTrRunReport, TrPerCountryRow

__all__ = ["ScheduleTrRunReport", "TrPerCountryRow", "run_schedule_tr"]

_ZERO: Final[Decimal] = Decimal("0")


def run_schedule_tr(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    financial_year: FinancialYear,
    format_id: str = "review-csv",
    itr_form: ItrForm = "ITR-2",
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
) -> ScheduleTrRunReport:
    """Run the Schedule TR pipeline: Activity Statement CSV in, TR review out.

    Schedule TR is a downstream aggregation of Schedule FSI: it first
    runs the shared FSI parse + compute seam, then collapses the FSI
    per-(country, head-of-income) rows into one per-country TR row via
    :func:`ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`.

    Tax relief (``tax_relief_inr_total``) and the section under which it
    is claimed (``section_under_which_claimed``) auto-populate per country
    from the Form 67 §90 / §91 credit (issues #36 / #37, PR #277): both
    are filled only when every contributing FSI row had a computed relief,
    under the one section the positive-relief rows resolve to. Both are left
    ``None`` (manual fill) when some row has no computed relief. Positive-relief
    rows on both §90 and §91 refuse with
    :class:`~ibkr_fa.exceptions.InvariantError`. See
    :func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`.
    In every format, a REFUSED Form 67 row refuses first, as in ``run-all``
    (#601, #1480), so a credit that files as ₹0 never reaches TR here; then
    an FSI relief the portal's read-only cell would overwrite refuses (#900),
    both before TR compute or writing.
    All FSI guarantees flow through transitively: Activity
    Statement input only and the per-ISIN orphan-WHT guard. The #225
    Indian-ADR WHT scope-symmetry fix is inherited from the FSI
    compute seam (mapped Indian-ADR WHT is silently skipped in
    UNDERLYING_ISSUER mode). ``format_id`` defaults to
    ``"review-csv"``; ``"xlsx"`` is also registered.
    """
    remove_previous_outputs((output_path,), (("--input", input_path), ("--config", config_path)))
    context = _compute_schedule_fsi_for_pipeline(
        input_path=input_path,
        config_path=config_path,
        financial_year=financial_year,
        allow_masked_account=allow_masked_account,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        accept_anonymised_statement=accept_anonymised_statement,
        report_name="Schedule TR foreign-tax-relief reporting",
    )
    # In every format, refuse in run-all's order (see the helper).
    require_fsi_write_ready(context.fsi_result.rows, context.form67_result)
    tr_result = compute_schedule_tr_from_fsi(context.fsi_result)

    binding = output_binding(Subcommand.TR, format_id)
    run_outputs = RunOutputs((output_path,), ())
    with run_outputs.removed_on_failure():
        binding.writer(
            RenderContext(
                assessment_year_start=financial_year.assessment_year_start, itr_form=itr_form
            )
        ).write(tr_result, output_path)

        require(
            output_path.exists(),
            what=(
                "Schedule TR pipeline post-write: writer must have produced a file at output_path"
            ),
            output_path=str(output_path),
        )

        per_country_rows = tuple(
            TrPerCountryRow(
                cbdt_country_code=row.cbdt_country_code,
                country_name_portal=row.country_name_portal,
                iso_alpha2=row.iso_alpha2,
                foreign_tax_inr_total_portal_displayed=row.foreign_tax_inr_total,
                foreign_tax_inr_total_full_precision=row.foreign_tax_inr_total_full_precision,
                tax_relief_inr_total=row.tax_relief_inr_total,
                section_under_which_claimed=row.section_under_which_claimed,
                event_count=row.event_count,
            )
            for row in tr_result.rows
        )
        total_foreign_tax_inr_portal_displayed = sum(
            (row.foreign_tax_inr_total for row in tr_result.rows),
            _ZERO,
        )
        total_foreign_tax_inr_full_precision = sum(
            (
                row.foreign_tax_inr_total_full_precision
                for row in tr_result.rows
                if row.foreign_tax_inr_total_full_precision is not None
            ),
            _ZERO,
        )

        # #FTC TIN completeness: every TR row has foreign tax > 0 by invariant
        # (relief context), so any row lacking a TIN stamped from
        # [account.foreign_tins] is a mandatory-field gap the CLI must surface.
        has_foreign_tax_without_taxpayer_id = any(
            row.taxpayer_id_number is None for row in tr_result.rows
        )
        emitted_country_keys = {(row.cbdt_country_code, row.iso_alpha2) for row in tr_result.rows}

        return ScheduleTrRunReport(
            rows_written=len(tr_result.rows),
            per_country_rows=per_country_rows,
            total_foreign_tax_inr_portal_displayed=total_foreign_tax_inr_portal_displayed,
            total_foreign_tax_inr_full_precision=total_foreign_tax_inr_full_precision,
            fsi_rows_consumed=len(context.fsi_result.rows),
            os_dividend_rows_consumed=context.os_dividend_rows_consumed,
            os_interest_rows_consumed=context.os_interest_rows_consumed,
            cg_stcg_rows_consumed=context.cg_stcg_rows_consumed,
            cg_ltcg_rows_consumed=context.cg_ltcg_rows_consumed,
            wht_rows_in_fy=context.wht_rows_in_fy,
            excluded_cg_indian_source_count=context.excluded_cg_indian_source_count,
            excluded_cg_out_of_fy_count=context.excluded_cg_out_of_fy_count,
            excluded_ltcg_pre_cutover_count=context.excluded_ltcg_pre_cutover_count,
            excluded_ltcg_indian_source_count=context.excluded_ltcg_indian_source_count,
            excluded_ltcg_out_of_fy_count=context.excluded_ltcg_out_of_fy_count,
            interest_credits_out_of_fy_count=context.interest_credits_out_of_fy_count,
            adr_routing_mode=context.adr_routing_mode,
            output_path=output_path,
            account_id=context.account_id,
            account_id_cross_checked=context.account_id_cross_checked,
            input_format=context.input_format,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            fx_policy_appropriate_for_ay=context.fx_policy_appropriate_for_ay,
            wht_fx_policy_appropriate_for_ay=context.wht_fx_policy_appropriate_for_ay,
            identity_supplied_from_fallback=context.identity_supplied_from_fallback,
            portal_labels_assessment_year_start=financial_year.assessment_year_start,
            portal_labels_used_forward_compat_fallback=is_forward_compat_fallback(
                financial_year.assessment_year_start
            ),
            cross_fy_reversal_report=context.fsi_result.cross_fy_reversal_report,
            has_foreign_tax_without_taxpayer_id=has_foreign_tax_without_taxpayer_id,
            # FSI's rule (ScheduleFsiRow.needs_indian_tax_payable), on the
            # countries TR emits: a row without foreign tax (a capital gain)
            # needs no Indian tax for relief (#1328).
            has_unresolved_indian_tax_payable=any(
                (row.cbdt_country_code, row.iso_alpha2) in emitted_country_keys
                and row.needs_indian_tax_payable
                for row in context.fsi_result.rows
            ),
            override_advice=form67_override_advice(context.form67_result.entries),
            run_outputs=run_outputs,
        )
