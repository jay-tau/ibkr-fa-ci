"""Pipeline orchestrator for the Schedule FSI subcommand.

Step 6/7 of #35 (Foreign Source Income — Schedule FSI). Aggregates
the four FSI-eligible sources (foreign dividends, broker interest,
short-term capital gains, long-term capital gains) plus withholding
tax into per-(country, head-of-income) FSI rows and writes them via
the writer registered for the requested format.

See :mod:`ibkr_fa.pipeline` for the broader orchestration contract
(dispatcher, FX-policy selection, cross-AY checks, identity-fallback
warnings); this module wires those primitives for the FSI schedule.
The shared "parse + compute FSI" seam that Schedule TR also consumes
lives in :mod:`ibkr_fa.pipeline_fsi_compute`
(:class:`~ibkr_fa.pipeline_fsi_compute._ScheduleFsiComputeContext` +
:func:`~ibkr_fa.pipeline_fsi_compute._compute_schedule_fsi_for_pipeline`).

v1 limitations (documented in :func:`run_schedule_fsi`):

* **Required Activity Statement sections**: STATEMENT +
  ACCOUNT_INFORMATION + FINANCIAL_INSTRUMENT_INFORMATION + TRADES
  (the CG set). Dividends / WithholdingTax / Interest sections are
  Tier-2 conditional. Documented limitation: dividend-only Activity
  Statements (no Trades section) cannot run through this pipeline
  in v1; users with that profile should use the `schedule-os`
  subcommand and manually transcribe the per-(country, head)
  totals into the portal's Schedule FSI cells.

* **UNDERLYING_ISSUER ADR-routing + WHT**: handled structurally
  (since the #225 fix). Under
  :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`
  routing, the OS dividend aggregator re-routes mapped Indian-ADR
  dividends (INFY / IBN / HDB / WIT / RDY / WNS / YTRA — see
  :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
  from foreign-source to Indian-source attribution. To preserve
  bucket symmetry, the FSI compute layer also EXCLUDES the
  corresponding US-withholding-tax entries (both paid and refund
  rows) from the per-(country, OTHER_SOURCES) aggregation; without
  this exclusion, the orphan Indian-ADR WHT would be silently
  absorbed by unrelated US-source income (e.g., XQAA dividends or
  broker credit-interest) and over-claim Foreign Tax Credit. The
  per-ISIN orphan-WHT guard at the pipeline layer applies the same
  symmetric exclusion to its scope under UNDERLYING_ISSUER mode.
  Non-mapped WHT (e.g., genuine XQAA US WHT) is unaffected and
  still subject to the per-ISIN orphan guard. For users entitled
  to recover the withheld Indian-ADR tax against their Indian tax
  liability, the recovery path is via Schedule TDS /
  Self-Assessment Tax / refund mechanisms — not via the FTC
  pathway (Schedule FSI + Form 67 + §90/§91), which only applies
  to genuinely foreign-source income.

* **Broker domicile**: broker interest is filed as US income, which
  is right only for IBKR LLC. The shared compute refuses, at run
  time, any Activity Statement whose ``Statement`` section does not
  have exactly one ``BrokerName`` row reading
  ``Interactive Brokers LLC`` (#665), with or without interest.
  Statements from other IBKR companies (e.g., IBKR Ireland, IBKR UK)
  are out of v1 scope.

* **WHT FX policy**: Rule 128(5)(ii) — preceding-calendar-month-
  end SBI TTBR per :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`.
  Verified for AY <= 2026-27; AY 2027-28+ migration to Rule 76(7)(b)
  per Rules 2026 is tracked at #207.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.form67 import form67_override_advice
from ibkr_fa.compute.schedule_fsi import require_fsi_write_ready
from ibkr_fa.compute.schedule_fsi_relief import relief_row_lacks_taxpayer_id
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline
from ibkr_fa.pipeline_fsi_models import FsiPerCountryRow, ScheduleFsiRunReport

__all__ = [
    "FsiPerCountryRow",
    "ScheduleFsiRunReport",
    "run_schedule_fsi",
]

_ZERO = Decimal("0")


def run_schedule_fsi(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    financial_year: FinancialYear,
    format_id: str = "review-csv",
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
) -> ScheduleFsiRunReport:
    """Run the Schedule FSI pipeline: IBKR Activity Statement CSV in, FSI review out.

    Schedule FSI ("Foreign Source Income") consolidates all FSI-
    eligible income into per-(country, head-of-income) portal rows.
    The pipeline runs four sister compute aggregators against a
    single Activity Statement parse, plus a withholding-tax
    aggregator, and feeds them into
    :func:`~ibkr_fa.compute.schedule_fsi.compute_schedule_fsi`:

    * Foreign-source ordinary dividends (Schedule OS row 1a(i)) → ``OTHER_SOURCES``.
    * Broker credit-interest (Schedule OS row 1b(ix)) → ``OTHER_SOURCES``,
      attributed to the broker's domicile country (hardcoded ``"US"``
      for IBKR LLC in v1).
    * STCG (Schedule CG Section A) → ``CAPITAL_GAINS``.
    * LTCG (Schedule CG Section B; post-2024-07-23 new regime) →
      ``CAPITAL_GAINS``.
    * Dividends-section WHT → ``foreign_tax_inr`` column layered onto
      matching income rows via the FSI dispatcher's
      ``wht_contributions`` kwarg.

    Pipeline stages, in order, once any earlier ``output_path`` is removed
    (so a refusal leaves no output behind):

    1. ``load_user_config(config_path)`` — parses ``user.toml``.
    2. ``detect_ibkr_csv_format(input_path)`` — must resolve to
       Activity Statement; Flex Query raises (FSI depends on multiple
       Activity Statement sections that Flex doesn't yet expose).
    3. ``_dispatch_activity_statement(...,
       required_sections=_FSI_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, ...)``
       parses Statement + Account Information + FII + Trades + the
       Tier-2 conditional Dividends / Withholding Tax / Interest
       sections.
    4. Cross-check ``AccountInfo.account_id`` against ``user.toml``.
    5. :meth:`~ibkr_fa.fx.context.TaxFxContext.for_fy` — supplies the
       per-head FX policies (Rule 115 dividend, other sources and capital
       gains; Rule 128 WHT) and whether the 1961-Act framework is verified
       for the AY this FY maps to. That verdict does NOT block; it sets
       :attr:`ScheduleFsiRunReport.fx_policy_appropriate_for_ay` and
       (in v1, the same boolean)
       :attr:`ScheduleFsiRunReport.wht_fx_policy_appropriate_for_ay`.
    6. Run each compute aggregator. Aggregators with absent source
       data (e.g., no Dividends section) produce empty results. In
       ``adr_routing_mode=UNDERLYING_ISSUER`` mode (issue #225), the
       WHT aggregator silently skips Indian-ADR rows (ISINs in
       :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
       symmetrically with Schedule OS's dividend rerouting.
    7. Apply the per-ISIN orphan-WHT guard (also scope-aware:
       mapped Indian-ADR WHT is excluded from the guard's check in
       UNDERLYING_ISSUER mode).
    8. Call ``compute_schedule_fsi(...)`` with all four results +
       WHT kwargs.
    9. Refuse, in every format, a REFUSED Form 67 row
       (:func:`~ibkr_fa.compute.form67.require_no_refused_form67_rows`, #601,
       #1480), then a relief the portal's read-only cell would overwrite
       (:func:`~ibkr_fa.compute.schedule_fsi.require_fsi_relief_matches_portal`, #900),
       in ``run-all``'s order.
    10. ``output_binding(Subcommand.FSI, format_id).writer(ctx).write(result, output_path)``.

    Args:
        input_path: Path to the IBKR Activity Statement CSV.
        config_path: Path to ``user.toml``.
        output_path: Destination path for the review CSV / XLSX.
            Parent directory must exist (writer fails loud on a typo).
        financial_year: The :class:`FinancialYear` being reported.
        format_id: FSI output format. Defaults to ``"review-csv"``.
            ``"xlsx"`` produces an Excel workbook.
        allow_masked_account: Opt out of the account-ID cross-check
            when the source statement masks the account ID.
        suppress_cross_ay_warning: Suppress the AY 2027-28+ cross-AY
            verification warning. Default ``False``.
        accept_anonymised_statement: Opt into the #141 fallback path
            for inputs where IBKR's privacy exporter has hidden the
            ``Name`` and/or ``Account`` rows.

    Returns:
        :class:`ScheduleFsiRunReport` summarising the run for the CLI.

    Raises:
        FileNotFoundError: If ``input_path`` or ``config_path`` doesn't exist.
        ConfigError: If ``user.toml`` is malformed, or a Form 67 row is
            REFUSED (#601). Nothing is written.
        ParseError: If the input format is Flex Query, if required
            Activity Statement sections are missing, or if any
            section parser fails.
        UnprovenInterestError: If an Interest row is unproven (#905).
            Nothing is written.
        IbkrFaError: Raised if any in-FY negative-amount (paid) WHT
            entry's (ISIN, payment_date) tuple has no matching in-FY
            foreign-source ordinary dividend in ``os_result.lines``,
            AND that ISIN's INR net is paid AND that country's INR
            net is paid (per-event orphan-WHT cross-source guard
            with three-condition gate). Under
            ``adr_routing_mode=UNDERLYING_ISSUER`` (issue #225), the
            guard's WHT scope symmetrically excludes mapped Indian-
            ADR ISINs so the rerouted-but-not-orphaned case passes
            silently. Positive-amount (reversal) WHT rows carry the
            reversal booking date by IBKR construction and are
            skipped — they reduce the credit, not inflate it. The
            error message names the orphan (ISIN, date) tuple and
            explains the workaround.
        InvariantError: If the WHT aggregator produces an orphan
            fragment (a (country, head) bucket with no matching
            income row), if any FSI row invariant is violated, if a section
            91 credit files as ₹0 (#1177), or if the portal's read-only cell
            would overwrite a row's relief.
        MissingFxRateError: If any compute layer's FX policy cannot
            resolve a rate within its carry-back window.
        OutputCollisionError: If ``output_path`` is ``input_path`` or
            ``config_path``.
        OutputRemovalError: If an earlier ``output_path``, or the output of
            a run that fails during or after its write, cannot be removed.
        UnknownOutputFormatError: If ``format_id`` is not registered
            for FSI.
    """
    remove_previous_outputs((output_path,), (("--input", input_path), ("--config", config_path)))
    context = _compute_schedule_fsi_for_pipeline(
        input_path=input_path,
        config_path=config_path,
        financial_year=financial_year,
        allow_masked_account=allow_masked_account,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
        accept_anonymised_statement=accept_anonymised_statement,
    )
    # In every format, refuse in run-all's order (see the helper).
    require_fsi_write_ready(context.fsi_result.rows, context.form67_result)

    binding = output_binding(Subcommand.FSI, format_id)
    run_outputs = RunOutputs((output_path,), ())
    with run_outputs.removed_on_failure():
        binding.writer(
            RenderContext(assessment_year_start=financial_year.assessment_year_start)
        ).write(context.fsi_result, output_path)

        require(
            output_path.exists(),
            what=(
                "Schedule FSI pipeline post-write: writer must have produced a file at output_path"
            ),
            output_path=str(output_path),
        )

        per_country_rows = tuple(
            FsiPerCountryRow(
                cbdt_country_code=row.cbdt_country_code,
                country_name_portal=row.country_name_portal,
                head_of_income=row.head_of_income,
                income_inr_rounded=row.filed.income_inr,
                foreign_tax_inr_rounded=row.filed.foreign_tax_inr,
                event_count=row.event_count,
            )
            for row in context.fsi_result.rows
        )
        # The totals add the filed whole-rupee rows, as the portal does (#900).
        total_income_rounded = sum((row.income_inr_rounded for row in per_country_rows), _ZERO)
        total_tax_rounded = sum((row.foreign_tax_inr_rounded for row in per_country_rows), _ZERO)

        # #FTC TIN completeness: flag when a country has foreign tax paid
        # (relief context) but no TIN was stamped from [account.foreign_tins].
        # The FSI "Taxpayer Identification Number" is mandatory when claiming
        # §90/90A/91 relief, so the CLI surfaces a non-quietable nudge rather
        # than silently filing a blank mandatory cell.
        has_foreign_tax_without_taxpayer_id = relief_row_lacks_taxpayer_id(context.fsi_result.rows)

        return ScheduleFsiRunReport(
            rows_written=len(context.fsi_result.rows),
            per_country_rows=per_country_rows,
            total_income_inr_rounded=total_income_rounded,
            total_foreign_tax_inr_rounded=total_tax_rounded,
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
            has_unresolved_indian_tax_payable=any(
                row.needs_indian_tax_payable for row in context.fsi_result.rows
            ),
            override_advice=form67_override_advice(context.form67_result.entries),
            run_outputs=run_outputs,
        )
