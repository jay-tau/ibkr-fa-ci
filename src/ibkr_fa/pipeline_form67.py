"""Pipeline orchestrator for Form 67 (Statement of income from a
country or specified territory outside India and Foreign Tax Credit).

PR (post-#226 ship) step 3/N of #37 wires Form 67 as a downstream
aggregator of Schedule FSI. The pipeline intentionally reuses
:func:`ibkr_fa.pipeline_fsi_compute._compute_schedule_fsi_for_pipeline`
as the shared seam between "parse + compute FSI" and per-schedule
"write + assemble report" work (same pattern as Schedule TR per PR
#217 step 3/N of #36). Form 67 therefore inherits the FSI parser,
FX-policy, account-check, WHT guard, and orphan-WHT protections
transitively.

The shared compute also builds the per-(country, source-of-income)
Form 67 entries once, via
:func:`ibkr_fa.compute.form67.compute_form67_entries_from_wht`, so
Schedule FSI, Schedule TR and Form 67 show the same relief. This
pipeline reuses the context's ``form67_result`` instead of recomputing
it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

from ibkr_fa.compute.form67 import (
    Form67Readiness,
    form67_override_advice,
    require_no_refused_form67_rows,
)
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext, Subcommand
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.bundle import spec_for, write_output_bundle
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67_models import Form67PerEntryRow, Form67RunReport
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline

__all__ = ["Form67PerEntryRow", "Form67RunReport", "run_form67"]

_ZERO: Final[Decimal] = Decimal("0")


def run_form67(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    financial_year: FinancialYear,
    format_id: str = "portal-csv",
    debug_output_path: Path | None = None,
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
    all_mode_readiness: bool = False,
) -> Form67RunReport:
    """Run the Form 67 pipeline: Activity Statement CSV in, Form 67 review out.

    Form 67 reads Schedule FSI's shared parse+compute seam. That compute
    (parser → FX policy → OS / CG / interest / WHT pre-aggregates → FSI
    rows) also builds the per-(country, source-of-income) Form 67
    entries once, via
    :func:`ibkr_fa.compute.form67.compute_form67_entries_from_wht`, and
    this pipeline reuses that ``form67_result`` instead of recomputing it.

    Cols 9/10 use vendored DTAA data. Cols 7 and 11-13 populate only
    when exact source-level Indian tax is configured; otherwise they
    remain blank for manual completion. Col 8 remains blank. All FSI
    guarantees flow through transitively: Activity Statement input only,
    and the per-ISIN orphan-WHT guard. ADR-routing parity:
    in ``adr_routing_mode=UNDERLYING_ISSUER`` mode, Indian-ADR WHT
    rows (ISINs in
    :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
    are silently skipped by the Form 67 compute layer so they
    don't orphan the depositary country or get silently absorbed
    by unrelated same-country foreign income (issue #225 fix).

    ``format_id`` defaults to the official 15-column ``"portal-csv"``.
    ``debug_output_path`` optionally receives the audit-rich review CSV.

    Readiness (#601) applies to ``"portal-csv"`` and, with
    ``all_mode_readiness``, to ``run-all``'s Form 67 step in any format: a
    REFUSED row raises before anything is written, and a BLANK row withholds
    ``output_path`` and writes the review CSV to ``form-67-debug.csv`` beside
    it instead. Explicit standalone ``"review-csv"`` / ``"xlsx"`` runs write
    every row.
    """
    enforce_readiness = all_mode_readiness or format_id == "portal-csv"
    blank_debug_path = output_path.with_name("form-67-debug.csv")
    targets = tuple(
        dict.fromkeys(
            (
                output_path,
                *(() if debug_output_path is None else (debug_output_path,)),
                *((blank_debug_path,) if enforce_readiness else ()),
            )
        )
    )
    remove_previous_outputs(targets, (("--input", input_path), ("--config", config_path)))
    context = _compute_schedule_fsi_for_pipeline(
        input_path=input_path,
        config_path=config_path,
        financial_year=financial_year,
        allow_masked_account=allow_masked_account,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        accept_anonymised_statement=accept_anonymised_statement,
        report_name="Form 67 foreign-tax-credit reporting",
    )
    # #36/#37: the Form 67 FTC is now computed once inside the shared FSI
    # context (so FSI / TR / Form 67 share one source of truth); reuse it here
    # rather than recomputing the identical entries.
    form67_result = context.form67_result
    if enforce_readiness:
        require_no_refused_form67_rows(form67_result)
    withheld = enforce_readiness and form67_result.readiness is Form67Readiness.BLANK
    if debug_output_path is None and withheld:
        debug_output_path = blank_debug_path
    specs = [] if withheld else [spec_for(Subcommand.FORM67, format_id, form67_result, output_path)]
    if debug_output_path is not None:
        specs.append(spec_for(Subcommand.FORM67, "review-csv", form67_result, debug_output_path))
    written_path = specs[0].path

    run_outputs = RunOutputs(targets, ())
    with run_outputs.removed_on_failure():
        write_output_bundle(
            specs, RenderContext(assessment_year_start=financial_year.assessment_year_start)
        )

        require(
            written_path.exists(),
            what="Form 67 pipeline post-write: writer must have produced the reported file",
            output_path=str(written_path),
        )

        per_entry_rows = tuple(
            Form67PerEntryRow(
                serial_number=entry.serial_number,
                cbdt_country_code=entry.cbdt_country_code,
                country_name_portal=entry.country_name_portal,
                iso_alpha2=entry.iso_alpha2,
                source_of_income=entry.source_of_income,
                income_inr=entry.income_inr,
                foreign_tax_inr=entry.foreign_tax_inr,
                foreign_tax_rate_percent=entry.foreign_tax_rate_percent,
                event_count=entry.event_count,
                tax_paid_rate_percent=entry.tax_paid_rate_percent,
                tax_payable_normal_inr=entry.tax_payable_normal_inr,
                tax_payable_special_inr=entry.tax_payable_special_inr,
                section_90_dtaa_article=entry.section_90_dtaa_article,
                section_90_dtaa_rate_percent=entry.section_90_dtaa_rate_percent,
                eligible_foreign_tax_inr=entry.eligible_foreign_tax_inr,
                treaty_rate_status=entry.treaty_rate_status,
                highest_payment_rate_percent=entry.highest_payment_rate_percent,
                payment_allocation_unproven=entry.payment_allocation_unproven,
                section_90_credit_inr=entry.section_90_credit_inr,
                section_91_credit_inr=entry.section_91_credit_inr,
                total_credit_inr=entry.total_credit_inr,
                readiness=readiness,
            )
            for entry, readiness in zip(
                form67_result.entries, form67_result.row_readiness, strict=True
            )
        )
        # Dual-anchor totals matching the PR #216 Schedule TR convention: the
        # report sums the rows' filed whole-rupee values for the
        # "portal-displayed" totals (#898), and this un-rounded sum is the
        # audit anchor. For n entries with fractional tax values,
        # rounding-then-summing vs summing-then-rounding differ by up to
        # 0.5 * n INR; the dual anchors let the user spot the gap.
        total_foreign_tax_inr_full_precision = sum(
            (entry.foreign_tax_inr for entry in form67_result.entries),
            _ZERO,
        )

        return Form67RunReport(
            rows_written=len(form67_result.entries),
            per_entry_rows=per_entry_rows,
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
            output_path=written_path,
            official_output_withheld=withheld,
            # A READY result, so never with a withheld file.
            upload_has_fractional_rate=(
                format_id == "portal-csv" and form67_result.ready_with_fractional_rate
            ),
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
            cross_fy_reversal_report=form67_result.cross_fy_reversal_report,
            override_advice=form67_override_advice(form67_result.entries),
            run_outputs=run_outputs,
        )
