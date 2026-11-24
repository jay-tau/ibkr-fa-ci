"""Form 67 report dataclasses for :mod:`ibkr_fa.pipeline_form67`.

PR (post-#226 ship) step 3/N of #37 adds the Form 67 pipeline.
These dataclasses mirror the existing per-schedule run-report split
used by Schedule TR (:mod:`ibkr_fa.pipeline_tr_models`) while
preserving Form 67-specific audit details: per-(country, source-of-
income) granularity (NOT per-country alone) and the §90/§91 mutex.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.form67 import Form67Readiness, Form67RefusalReason, Form67RowReadiness
from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.invariants import require
from ibkr_fa.models._dtaa_article import bare_article_number
from ibkr_fa.models._identifier_checks import is_cbdt_country_code, is_iso_alpha2
from ibkr_fa.models.form67_entry import (
    Form67FiledValues,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
    form67_filed_values,
    require_treaty_rate_status,
)
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import (
    _require_financial_year_end,
    _require_fsi_compute_counts_non_negative,
    _require_identity_fallback_subset,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)


@dataclass(frozen=True, slots=True)
class Form67PerEntryRow:
    """One per-(country, source-of-income) Form 67 entry for a run report.

    Introduced by PR (post-#226 ship) step 3/N of #37 for the Form 67
    pipeline. Form 67 is per-(country, source-of-income) aggregate
    (NOT per-country alone like Schedule TR), so this row carries the
    ``source_of_income`` discriminator alongside the country fields.

    The relief fields (``tax_payable_normal_inr``,
    ``tax_payable_special_inr``, ``section_90_dtaa_article``,
    ``section_90_dtaa_rate_percent``, ``eligible_foreign_tax_inr``,
    ``section_90_credit_inr``, ``section_91_credit_inr``,
    ``total_credit_inr``) are copied from the
    :class:`~ibkr_fa.models.form67_entry.Form67Entry` and are ``None``
    wherever compute left that column blank, per the "not computed;
    user must fill" contract (PR #212 for FSI, PR #215 for TR, PR #221
    for Form 67). :mod:`ibkr_fa.compute.form67` owns the fill rules;
    ``tax_payable_special_inr`` (col 8) is never computed.

    ``treaty_rate_status``, ``highest_payment_rate_percent`` and
    ``payment_allocation_unproven`` are copied from the entry, where compute
    decided them, and are held to the same invariant (#767, #890).

    ``readiness`` is the entry's compute-decided
    :class:`~ibkr_fa.compute.form67.Form67RowReadiness`, so the CLI can say
    which entries lack a credit, and why, without re-deriving it (#791). It
    must agree with col 7, the filed claimed credit and col 13.

    :attr:`filed` rounds the row's values to what Form 67 files, the same way
    :attr:`~ibkr_fa.models.form67_entry.Form67Entry.filed` does (#898).
    ``section_90_dtaa_article`` must be shaped like ``Article 10(2)(b)``,
    checked after every other rule (#1079).
    """

    serial_number: int
    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    source_of_income: Form67SourceOfIncome
    income_inr: Decimal
    foreign_tax_inr: Decimal
    foreign_tax_rate_percent: Decimal
    event_count: int
    tax_payable_normal_inr: Decimal | None
    tax_payable_special_inr: Decimal | None
    section_90_dtaa_article: str | None
    section_90_dtaa_rate_percent: Decimal | None
    section_90_credit_inr: Decimal | None
    section_91_credit_inr: Decimal | None
    total_credit_inr: Decimal | None
    tax_paid_rate_percent: Decimal | None = None
    eligible_foreign_tax_inr: Decimal | None = None
    treaty_rate_status: Form67TreatyRateStatus | None = None
    highest_payment_rate_percent: Decimal | None = None
    payment_allocation_unproven: bool = False
    readiness: Form67RowReadiness = field(kw_only=True)

    @property
    def filed(self) -> Form67FiledValues:
        """The whole-rupee amounts, two-place rates and DTAA citation Form 67 files
        for this row."""
        return form67_filed_values(self)

    def __post_init__(self) -> None:
        require(
            self.serial_number >= 1,
            what="Form67PerEntryRow: serial_number must be >= 1 (1-indexed per the portal form)",
            serial_number=self.serial_number,
        )
        require(
            is_cbdt_country_code(self.cbdt_country_code),
            what=(
                "Form67PerEntryRow: cbdt_country_code must be a 1-4 character "
                "ASCII-decimal numeric string (matches the upstream Form67Entry "
                "invariant)"
            ),
            cbdt_country_code=self.cbdt_country_code,
        )
        require(
            self.country_name_portal != "",
            what="Form67PerEntryRow: country_name_portal must be non-empty",
        )
        require(
            is_iso_alpha2(self.iso_alpha2),
            what=(
                "Form67PerEntryRow: iso_alpha2 must be a 2-letter uppercase "
                "ASCII ISO 3166-1 alpha-2 code (mirrors the upstream "
                "Form67Entry invariant)"
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            self.income_inr > 0,
            what=(
                "Form67PerEntryRow: income_inr must be > 0 (Form 67 cannot "
                "claim relief on zero foreign income per Rule 128(5)(i))"
            ),
            income_inr=str(self.income_inr),
        )
        require(
            self.foreign_tax_inr > 0,
            what="Form67PerEntryRow: foreign_tax_inr must be > 0",
            foreign_tax_inr=str(self.foreign_tax_inr),
        )
        require(
            self.foreign_tax_rate_percent > 0,
            what="Form67PerEntryRow: foreign_tax_rate_percent must be > 0",
            foreign_tax_rate_percent=str(self.foreign_tax_rate_percent),
        )
        require(
            self.event_count >= 1,
            what=(
                "Form67PerEntryRow: event_count must be >= 1 (every entry "
                "aggregates at least one underlying WHT event)"
            ),
            event_count=self.event_count,
        )
        if self.tax_paid_rate_percent is not None:
            require(
                Decimal("0") < self.tax_paid_rate_percent <= Decimal("100"),
                what="Form67PerEntryRow.tax_paid_rate_percent must be in (0, 100]",
                tax_paid_rate_percent=self.tax_paid_rate_percent,
            )
        require_treaty_rate_status(
            "Form67PerEntryRow",
            treaty_rate_status=self.treaty_rate_status,
            section_90_dtaa_rate_percent=self.section_90_dtaa_rate_percent,
            tax_paid_rate_percent=self.tax_paid_rate_percent,
            highest_payment_rate_percent=self.highest_payment_rate_percent,
            payment_allocation_unproven=self.payment_allocation_unproven,
            eligible_foreign_tax_inr=self.eligible_foreign_tax_inr,
            foreign_tax_inr=self.foreign_tax_inr,
            section_90_credit_inr=self.section_90_credit_inr,
            total_credit_inr=self.total_credit_inr,
        )
        _require_readiness_matches_relief(self)
        bare_article_number(
            self.section_90_dtaa_article,
            field="Form67PerEntryRow.section_90_dtaa_article (col 9)",
        )


def _require_readiness_matches_relief(row: Form67PerEntryRow) -> None:
    """Pin ``row.readiness`` to col 7, the claimed credit and col 13 (#791).

    Repeats the credit-column rules
    :class:`~ibkr_fa.models.form67_entry.Form67Entry` enforces, then the
    readiness rules :func:`~ibkr_fa.compute.form67.form67_row_readiness`
    applies to the filed credit (#898), so a report reader can trust the
    readiness.
    """
    readiness = row.readiness
    require(
        isinstance(readiness, Form67RowReadiness)
        and isinstance(readiness.readiness, Form67Readiness)
        and (
            isinstance(readiness.reason, Form67RefusalReason)
            if readiness.readiness is Form67Readiness.REFUSED
            else readiness.reason is None
        ),
        what=(
            "Form67PerEntryRow.readiness must be a Form67RowReadiness whose reason is a "
            "Form67RefusalReason exactly when it is REFUSED"
        ),
        readiness=readiness,
    )
    amounts = {
        "tax_payable_normal_inr": row.tax_payable_normal_inr,
        "section_90_credit_inr": row.section_90_credit_inr,
        "section_91_credit_inr": row.section_91_credit_inr,
        "total_credit_inr": row.total_credit_inr,
    }
    require(
        all(
            amount is None or (isinstance(amount, Decimal) and amount.is_finite())
            for amount in amounts.values()
        ),
        what=(
            "Form67PerEntryRow: col 7, the section 90 and 91 credits and col 13 must each "
            "be None or a finite Decimal"
        ),
        **amounts,
    )
    tax_payable = row.tax_payable_normal_inr
    require(
        tax_payable is None or tax_payable >= 0,
        what="Form67PerEntryRow.tax_payable_normal_inr must be >= 0 when provided",
        tax_payable_normal_inr=tax_payable,
    )
    require(
        row.section_90_credit_inr is None or row.section_91_credit_inr is None,
        what=(
            "Form67PerEntryRow: at most one of section_90_credit_inr / "
            "section_91_credit_inr may be set"
        ),
        section_90_credit_inr=row.section_90_credit_inr,
        section_91_credit_inr=row.section_91_credit_inr,
    )
    article = row.section_90_dtaa_article
    if row.section_90_credit_inr is not None:
        require(
            isinstance(article, str)
            and article.strip() != ""
            and row.section_90_dtaa_rate_percent is not None,
            what=(
                "Form67PerEntryRow: a section 90 credit needs a non-blank "
                "section_90_dtaa_article and a section_90_dtaa_rate_percent"
            ),
            section_90_dtaa_article=article,
            section_90_dtaa_rate_percent=row.section_90_dtaa_rate_percent,
        )
    if row.section_91_credit_inr is not None:
        require(
            article is None and row.section_90_dtaa_rate_percent is None,
            what=(
                "Form67PerEntryRow: a section 91 credit needs section_90_dtaa_article "
                "and section_90_dtaa_rate_percent to be None"
            ),
            section_90_dtaa_article=article,
            section_90_dtaa_rate_percent=row.section_90_dtaa_rate_percent,
        )
    claimed = (
        row.section_90_credit_inr
        if row.section_90_credit_inr is not None
        else row.section_91_credit_inr
    )
    require(
        row.total_credit_inr == claimed,
        what="Form67PerEntryRow.total_credit_inr must equal the claimed section 90 or 91 credit",
        total_credit_inr=row.total_credit_inr,
        claimed_credit=claimed,
    )
    filed_claimed = row.filed.claimed_credit_inr
    if readiness.readiness is Form67Readiness.READY:
        matches = tax_payable is not None and filed_claimed is not None and filed_claimed > 0
    elif readiness.readiness is Form67Readiness.BLANK:
        matches = tax_payable is None and claimed is None
    elif readiness.reason is Form67RefusalReason.ZERO_CREDIT:
        matches = (
            tax_payable is not None and claimed is not None and claimed >= 0 and filed_claimed == 0
        )
    else:
        matches = tax_payable is not None and claimed is None
    require(
        matches,
        what=(
            "Form67PerEntryRow.readiness doesn't match col 7 and the filed claimed credit: "
            "READY needs both and a credit of at least ₹1, BLANK neither, ZERO_CREDIT both "
            "and a credit that rounds to ₹0, and any other refusal col 7 alone"
        ),
        readiness=readiness,
        tax_payable_normal_inr=tax_payable,
        claimed_credit=claimed,
        filed_claimed_credit=filed_claimed,
    )


@dataclass(frozen=True, slots=True)
class Form67RunReport:
    """Summary of one successful :func:`ibkr_fa.pipeline_form67.run_form67` call.

    PR (post-#226 ship) step 3/N of #37 wires Form 67 as a downstream
    aggregation over Schedule FSI's shared parse+compute seam. The
    report mirrors :class:`ibkr_fa.pipeline_tr_models.ScheduleTrRunReport`
    for upstream-consumption metadata, then adds Form 67 totals and
    per-entry rows for CLI / library callers.

    Two cross-form-reconciliation totals are exposed:
    :attr:`total_foreign_tax_inr_portal_displayed` is the sum of the rows'
    filed col-5 values (whole rupees per entry, #898);
    ``total_foreign_tax_inr_full_precision`` is the audit-only
    unrounded sum and may differ by up to rounding noise. The two
    totals mirror Schedule TR's PR #216 dual-anchor pattern.
    :attr:`total_income_inr_portal_displayed` sums the filed col-4 values.
    """

    rows_written: int
    per_entry_rows: tuple[Form67PerEntryRow, ...]
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
    # #601: True when a BLANK row withheld the official file and
    # ``output_path`` is the review CSV left for manual completion.
    official_output_withheld: bool = field(kw_only=True)
    # #1447: True when ``output_path`` is the upload CSV and a row files a
    # fractional Rate(%), which the filer enters by hand after upload.
    upload_has_fractional_rate: bool = field(kw_only=True)
    portal_labels_used_forward_compat_fallback: bool = False
    cross_fy_reversal_report: WhtCrossFyReversalReport = field(
        default_factory=WhtCrossFyReversalReport
    )
    # #1474: :func:`~ibkr_fa.compute.form67.form67_override_advice` of the run's
    # Form 67 entries, which the missing Indian tax warning gives in place of its
    # generic override sentence.
    override_advice: tuple[str, ...] = ()
    run_outputs: RunOutputs = field(kw_only=True)

    def __post_init__(self) -> None:
        require(
            self.rows_written >= 0,
            what="Form67RunReport: rows_written must be >= 0",
            rows_written=self.rows_written,
        )
        require(
            self.rows_written == len(self.per_entry_rows),
            what=(
                "Form67RunReport: per_entry_rows length must equal "
                "rows_written (one tuple entry per emitted Form 67 row)"
            ),
            rows_written=self.rows_written,
            per_entry_rows_len=len(self.per_entry_rows),
        )
        _require_fsi_compute_counts_non_negative(
            run_report_class_name="Form67RunReport",
            run_report=self,
            leading_count_names=("fsi_rows_consumed",),
        )
        require(
            self.total_foreign_tax_inr_full_precision >= 0,
            what="Form67RunReport: total_foreign_tax_inr_full_precision must be >= 0",
            total_foreign_tax_inr_full_precision=str(self.total_foreign_tax_inr_full_precision),
        )
        require(
            self.account_id != "",
            what="Form67RunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="Form67RunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="Form67RunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="Form67RunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="Form67RunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )

    @property
    def total_foreign_tax_inr_portal_displayed(self) -> Decimal:
        """The sum of the rows' filed foreign tax (col 5), in whole rupees."""
        return sum((row.filed.foreign_tax_inr for row in self.per_entry_rows), Decimal("0"))

    @property
    def total_income_inr_portal_displayed(self) -> Decimal:
        """The sum of the rows' filed income (col 4), in whole rupees."""
        return sum((row.filed.income_inr for row in self.per_entry_rows), Decimal("0"))

    @property
    def has_unresolved_indian_tax_payable(self) -> bool:
        """Whether any entry leaves col 7 (Indian tax payable) blank: the case
        in which ``portal_import_warnings`` adds its missing-tax warning."""
        return any(row.tax_payable_normal_inr is None for row in self.per_entry_rows)

    @property
    def manual_fill_required(self) -> bool:
        """The official file is withheld (#601), or the upload files a fractional
        Rate(%) (#1447)."""
        return self.official_output_withheld or self.upload_has_fractional_rate


__all__ = ["Form67PerEntryRow", "Form67RunReport"]
