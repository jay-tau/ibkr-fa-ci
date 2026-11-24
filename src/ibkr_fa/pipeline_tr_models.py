"""Schedule TR report dataclasses for :mod:`ibkr_fa.pipeline_tr`.

PR #217 step 3/N of #36 adds Schedule TR as a downstream
aggregator of Schedule FSI. These dataclasses mirror the existing
per-schedule run-report split used by Schedule FSI while preserving
TR-specific audit details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_iso_alpha2
from ibkr_fa.models.schedule_tr_row import TaxReliefSection
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import (
    _require_financial_year_end,
    _require_fsi_compute_counts_non_negative,
    _require_identity_fallback_subset,
    _require_manual_fill_flags_need_rows,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)


@dataclass(frozen=True, slots=True)
class TrPerCountryRow:
    """One per-country Schedule TR summary line for a run report.

    Introduced by PR #217 step 3/N of #36 for the Schedule TR
    pipeline. The foreign-tax fields follow the PR #216 dual-anchor
    pattern: ``foreign_tax_inr_total_portal_displayed`` is the sum of
    already-HALF_UP-rounded Schedule FSI portal cells and is what the
    TR portal row displays; ``foreign_tax_inr_total_full_precision``
    preserves the unrounded FSI sum for audit reconciliation only.
    """

    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    foreign_tax_inr_total_portal_displayed: Decimal
    foreign_tax_inr_total_full_precision: Decimal | None
    tax_relief_inr_total: Decimal | None
    section_under_which_claimed: TaxReliefSection | None
    event_count: int

    def __post_init__(self) -> None:
        require(
            self.cbdt_country_code != "",
            what="TrPerCountryRow: cbdt_country_code must be non-empty",
        )
        require(
            self.country_name_portal != "",
            what="TrPerCountryRow: country_name_portal must be non-empty",
        )
        require(
            is_iso_alpha2(self.iso_alpha2),
            what=(
                "TrPerCountryRow: iso_alpha2 must be a 2-letter uppercase "
                "ASCII ISO 3166-1 alpha-2 code (mirrors the upstream "
                "ScheduleTrRow invariant including isalpha() which rejects "
                "digit-bearing strings like 'U1')"
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            self.foreign_tax_inr_total_portal_displayed >= 0,
            what=("TrPerCountryRow: foreign_tax_inr_total_portal_displayed must be >= 0"),
            foreign_tax_inr_total_portal_displayed=str(self.foreign_tax_inr_total_portal_displayed),
        )
        if self.foreign_tax_inr_total_full_precision is not None:
            require(
                self.foreign_tax_inr_total_full_precision >= 0,
                what=(
                    "TrPerCountryRow: foreign_tax_inr_total_full_precision "
                    "must be >= 0 when populated"
                ),
                foreign_tax_inr_total_full_precision=str(self.foreign_tax_inr_total_full_precision),
            )
        if self.tax_relief_inr_total is not None:
            require(
                self.tax_relief_inr_total >= 0,
                what="TrPerCountryRow: tax_relief_inr_total must be >= 0 when populated",
                tax_relief_inr_total=str(self.tax_relief_inr_total),
            )
        require(
            self.event_count >= 1,
            what=(
                "TrPerCountryRow: event_count must be >= 1 (every row "
                "aggregates at least one underlying Schedule FSI row)"
            ),
            event_count=self.event_count,
        )


@dataclass(frozen=True, slots=True)
class ScheduleTrRunReport:
    """Summary of one successful :func:`ibkr_fa.pipeline_tr.run_schedule_tr` call.

    PR #217 step 3/N of #36 wires Schedule TR as a downstream
    aggregation over Schedule FSI. The report mirrors
    :class:`ibkr_fa.pipeline_fsi_models.ScheduleFsiRunReport` for
    upstream-consumption metadata, then adds TR totals and per-country
    rows for CLI / library callers.

    The TR foreign-tax totals deliberately carry two anchors per
    PR #216. ``total_foreign_tax_inr_portal_displayed`` is the sum of
    the TR rows' portal-displayed values; each row is itself a sum of
    per-FSI-row HALF_UP rounded portal cells, so this total is NOT
    re-rounded. ``total_foreign_tax_inr_full_precision`` is the
    audit-only unrounded sum and may differ from the portal-displayed
    total by up to rounding noise across contributing FSI rows.
    """

    rows_written: int
    per_country_rows: tuple[TrPerCountryRow, ...]
    total_foreign_tax_inr_portal_displayed: Decimal
    total_foreign_tax_inr_full_precision: Decimal
    fsi_rows_consumed: int
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
    # True when at least one TR row (every TR row has foreign tax > 0 by
    # invariant, so this is always a relief context) has no foreign
    # Taxpayer Identification Number stamped from ``[account.foreign_tins]``
    # — mandatory when claiming §90/90A/91 relief, so the CLI raises a
    # non-quietable nudge and the output needs manual fill
    # (:attr:`manual_fill_required`).
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
            what="ScheduleTrRunReport: rows_written must be >= 0",
            rows_written=self.rows_written,
        )
        _require_manual_fill_flags_need_rows(
            run_report_class_name="ScheduleTrRunReport",
            rows_label="TR",
            rows_written=self.rows_written,
            has_foreign_tax_without_taxpayer_id=self.has_foreign_tax_without_taxpayer_id,
            has_unresolved_indian_tax_payable=self.has_unresolved_indian_tax_payable,
        )
        require(
            self.rows_written == len(self.per_country_rows),
            what=(
                "ScheduleTrRunReport: per_country_rows length must equal "
                "rows_written (one tuple entry per emitted TR row)"
            ),
            rows_written=self.rows_written,
            per_country_rows_len=len(self.per_country_rows),
        )
        _require_fsi_compute_counts_non_negative(
            run_report_class_name="ScheduleTrRunReport",
            run_report=self,
            leading_count_names=("fsi_rows_consumed",),
        )
        require(
            self.total_foreign_tax_inr_portal_displayed >= 0,
            what=("ScheduleTrRunReport: total_foreign_tax_inr_portal_displayed must be >= 0"),
            total_foreign_tax_inr_portal_displayed=str(self.total_foreign_tax_inr_portal_displayed),
        )
        require(
            self.total_foreign_tax_inr_full_precision >= 0,
            what=("ScheduleTrRunReport: total_foreign_tax_inr_full_precision must be >= 0"),
            total_foreign_tax_inr_full_precision=str(self.total_foreign_tax_inr_full_precision),
        )
        require(
            self.account_id != "",
            what="ScheduleTrRunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="ScheduleTrRunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="ScheduleTrRunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="ScheduleTrRunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="ScheduleTrRunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )


__all__ = ["ScheduleTrRunReport", "TrPerCountryRow"]
