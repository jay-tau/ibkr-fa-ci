"""Schedule FSI report dataclass extracted from :mod:`ibkr_fa.pipeline_fsi`.

Mirrors the :mod:`ibkr_fa.pipeline_os_models` / :mod:`ibkr_fa.pipeline_fa_models`
extraction pattern from the #155 split: the run-report dataclass lives
in a sister ``_models`` module and the orchestrator re-exports it for
backward compatibility with existing test and CLI imports.

:class:`ScheduleFsiRunReport` is re-exported from
:mod:`ibkr_fa.pipeline_fsi` so ``from ibkr_fa.pipeline_fsi import
ScheduleFsiRunReport`` continues to work. New code SHOULD import from
this module directly:

.. code-block:: python

    from ibkr_fa.pipeline_fsi_models import ScheduleFsiRunReport

See :mod:`ibkr_fa.pipeline_fsi` for the :func:`run_schedule_fsi`
orchestration that constructs this dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.invariants import require
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat

# Shared RunReport invariant helpers stay in pipeline.py (the
# per-schedule RunReport classes share them).
from ibkr_fa.pipeline import (
    _require_financial_year_end,
    _require_fsi_compute_counts_non_negative,
    _require_identity_fallback_subset,
    _require_manual_fill_flags_need_rows,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)


@dataclass(frozen=True, slots=True)
class FsiPerCountryRow:
    """One per-(country, head-of-income) summary line for the run report.

    Mirrors the writer's per-row output but rounded to whole rupees
    for the CLI's success-summary rendering. The writer's full-precision
    values remain the source of truth; this dataclass exists to give
    the CLI a stable, machine-parseable surface (used by --quiet's
    per-row CSV output).

    Attributes:
        cbdt_country_code: CBDT 3-digit numeric country code.
        country_name_portal: Portal ALL-CAPS display name.
        head_of_income: ITR-2 head taxonomy bucket.
        income_inr_rounded: The filed HALF_UP whole-rupee income amount
            (:attr:`~ibkr_fa.models.schedule_fsi_row.ScheduleFsiRow.filed`).
        foreign_tax_inr_rounded: The filed HALF_UP whole-rupee foreign
            tax amount (always >= 0; reversals netted upstream).
        event_count: Number of underlying source events aggregated.
    """

    cbdt_country_code: str
    country_name_portal: str
    head_of_income: HeadOfIncome
    income_inr_rounded: Decimal
    foreign_tax_inr_rounded: Decimal
    event_count: int


@dataclass(frozen=True, slots=True)
class ScheduleFsiRunReport:
    """Summary of one successful :func:`run_schedule_fsi` call.

    Schedule FSI aggregates foreign-source income across all reportable
    heads — foreign dividends + broker credit-interest under
    OTHER_SOURCES, plus STCG + LTCG under CAPITAL_GAINS — into per-
    (country, head-of-income) rows, with foreign withholding tax
    layered onto matching dividend rows. The report carries every
    value the CLI surfaces plus the ones needed to advise on cross-AY
    verification.

    Attributes:
        rows_written: Number of FSI rows emitted to ``output_path``
            (equal to ``len(ScheduleFsiComputeResult.rows)``). May be
            0 for an FY with no qualifying foreign income.
        per_country_rows: Tuple of :class:`FsiPerCountryRow` summaries,
            one per emitted FSI row, in the same order as the writer's
            output. Used by the CLI's --quiet per-row CSV output and
            for any post-run aggregation a caller wants to do without
            re-reading the CSV.
        total_income_inr_rounded: Sum of the emitted rows' filed
            whole-rupee income (#900). Always >= 0 (FSI rows reject
            negative income).
        total_foreign_tax_inr_rounded: Sum of the emitted rows' filed
            whole-rupee foreign tax. Always >= 0.
        os_dividend_rows_consumed: Number of foreign-source ordinary
            dividend rows consumed from the OS compute layer.
        os_interest_rows_consumed: Number of broker credit-interest
            events consumed from the OS interest compute layer.
        cg_stcg_rows_consumed: Number of foreign-source STCG rows
            consumed from the CG STCG compute layer.
        cg_ltcg_rows_consumed: Number of foreign-source LTCG rows
            consumed from the CG LTCG compute layer.
        wht_rows_in_fy: Number of in-FY WHT rows the pipeline ingested
            (post FY-filter). NOT the number of rows that contributed
            to ``foreign_tax_inr`` on the emitted FSI rows — when a
            country's reversals net to a non-positive amount, the
            compute layer drops the contribution but every constituent
            WHT row is still counted here. Use this counter to verify
            the pipeline's FY filter is doing the expected work; for
            net-tax-contributed counts inspect the emitted FSI rows'
            ``foreign_tax_inr`` directly.
        excluded_cg_indian_source_count: Closed lots with ISIN
            prefix "IN" excluded from the CG STCG aggregator.
        excluded_cg_out_of_fy_count: Closed lots whose close_date is
            outside the reporting FY (from the CG STCG aggregator).
        excluded_ltcg_pre_cutover_count: Closed LTCG lots whose
            disposal is before the 2024-07-23 new-regime cutover
            (excluded from the CG LTCG aggregator).
        excluded_ltcg_indian_source_count: Closed LTCG lots with
            ISIN prefix "IN" excluded from the CG LTCG aggregator.
        excluded_ltcg_out_of_fy_count: Closed LTCG lots whose close
            date is outside the reporting FY.
        interest_credits_out_of_fy_count: Broker credit-interest
            events whose date is outside the reporting FY.
        adr_routing_mode: The ADR routing mode the user's config
            selected for Schedule OS. Surfaced here so the CLI can
            describe the routing in the success summary, and so
            audit consumers can verify the symmetric Indian-ADR
            WHT exclusion was in effect for UNDERLYING_ISSUER
            runs (see :func:`run_schedule_fsi` for the structural
            #225 fix semantics).
        output_path: The path the writer wrote to, as the caller gave it (not resolved).
        account_id: The IBKR account number parsed from the input.
            May be a mask string when ``allow_masked_account=True``.
        account_id_cross_checked: Whether the parsed account ID was
            cross-checked against ``user.toml``.
        input_format: Which IBKR CSV serialisation was recognised.
            Activity Statement only in v1.
        financial_year_start: First day of the reporting FY.
        financial_year_end: Last day of the reporting FY.
        fx_policy_appropriate_for_ay: Whether the Rule 115 income FX
            policy is verified appropriate for the AY this FY maps to.
            ``True`` for AY <= 2026-27.
        wht_fx_policy_appropriate_for_ay: Whether the Rule 128 WHT FX
            policy is verified appropriate for the AY this FY maps to.
            Evaluated against the same AY boundary as the income FX
            policy in v1, but tracked separately so the CLI can
            differentiate the warning text (Rule 115 → Rule 206;
            Rule 128 → Rule 76 per Rules 2026).
        identity_supplied_from_fallback: Frozenset of Activity
            Statement field names backfilled from user.toml under
            ``--accept-anonymised-statement`` (#141).
        portal_labels_assessment_year_start: 4-digit AY start year
            whose portal-labels snapshot the writer used.
        portal_labels_used_forward_compat_fallback: ``True`` iff the
            writer's snapshot lookup hit the +1 forward-compat fallback.
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
    """

    rows_written: int
    per_country_rows: tuple[FsiPerCountryRow, ...]
    total_income_inr_rounded: Decimal
    total_foreign_tax_inr_rounded: Decimal
    os_dividend_rows_consumed: int
    os_interest_rows_consumed: int
    cg_stcg_rows_consumed: int
    cg_ltcg_rows_consumed: int
    wht_rows_in_fy: int
    excluded_cg_indian_source_count: int
    excluded_cg_out_of_fy_count: int
    excluded_ltcg_pre_cutover_count: int
    excluded_ltcg_indian_source_count: int
    excluded_ltcg_out_of_fy_count: int
    interest_credits_out_of_fy_count: int
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode
    output_path: Path
    account_id: str
    account_id_cross_checked: bool
    input_format: IbkrCsvFormat
    financial_year_start: date
    financial_year_end: date
    fx_policy_appropriate_for_ay: bool
    wht_fx_policy_appropriate_for_ay: bool
    identity_supplied_from_fallback: frozenset[str] = frozenset()
    portal_labels_assessment_year_start: int = field(kw_only=True)
    portal_labels_used_forward_compat_fallback: bool = False
    cross_fy_reversal_report: WhtCrossFyReversalReport = field(
        default_factory=WhtCrossFyReversalReport
    )
    # True when at least one country has foreign tax paid (relief context)
    # but no foreign Taxpayer Identification Number stamped from
    # ``[account.foreign_tins]`` — the FSI TIN is mandatory when claiming
    # §90/90A/91 relief, so the CLI raises a non-quietable nudge and the
    # output needs manual fill (:attr:`manual_fill_required`).
    has_foreign_tax_without_taxpayer_id: bool = False
    has_unresolved_indian_tax_payable: bool = False
    # #1474: :func:`~ibkr_fa.compute.form67.form67_override_advice` of the run's
    # Form 67 entries, which the missing Indian tax warning gives in place of its
    # generic override sentence.
    override_advice: tuple[str, ...] = ()
    run_outputs: RunOutputs = field(kw_only=True)

    @property
    def manual_fill_required(self) -> bool:
        """Whether a blank cell needs manual fill: a relief row's foreign TIN,
        or the Indian tax payable of a row with foreign tax (#1328)."""
        return self.has_foreign_tax_without_taxpayer_id or self.has_unresolved_indian_tax_payable

    def __post_init__(self) -> None:
        require(
            self.rows_written >= 0,
            what="ScheduleFsiRunReport: rows_written must be >= 0",
            rows_written=self.rows_written,
        )
        _require_manual_fill_flags_need_rows(
            run_report_class_name="ScheduleFsiRunReport",
            rows_label="FSI",
            rows_written=self.rows_written,
            has_foreign_tax_without_taxpayer_id=self.has_foreign_tax_without_taxpayer_id,
            has_unresolved_indian_tax_payable=self.has_unresolved_indian_tax_payable,
        )
        require(
            self.rows_written == len(self.per_country_rows),
            what=(
                "ScheduleFsiRunReport: per_country_rows length must equal "
                "rows_written (one tuple entry per emitted FSI row)"
            ),
            rows_written=self.rows_written,
            per_country_rows_len=len(self.per_country_rows),
        )
        _require_fsi_compute_counts_non_negative(
            run_report_class_name="ScheduleFsiRunReport",
            run_report=self,
        )
        require(
            self.total_income_inr_rounded >= 0,
            what="ScheduleFsiRunReport: total_income_inr_rounded must be >= 0",
            total_income_inr_rounded=str(self.total_income_inr_rounded),
        )
        require(
            self.total_foreign_tax_inr_rounded >= 0,
            what="ScheduleFsiRunReport: total_foreign_tax_inr_rounded must be >= 0",
            total_foreign_tax_inr_rounded=str(self.total_foreign_tax_inr_rounded),
        )
        require(
            self.account_id != "",
            what="ScheduleFsiRunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="ScheduleFsiRunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="ScheduleFsiRunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="ScheduleFsiRunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="ScheduleFsiRunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )


__all__ = ["FsiPerCountryRow", "ScheduleFsiRunReport"]
