"""Schedule OS report dataclass extracted from :mod:`ibkr_fa.pipeline_os`.

Mirrors the :mod:`ibkr_fa.pipeline_fa_models` extraction pattern from
the #155 split: the run-report dataclass lives in a sister ``_models``
module and the orchestrator re-exports it for backward compatibility
with existing test and CLI imports.

:class:`ScheduleOsRunReport` is re-exported from
:mod:`ibkr_fa.pipeline_os` so ``from ibkr_fa.pipeline_os import
ScheduleOsRunReport`` continues to work. New code SHOULD import from
this module directly:

.. code-block:: python

    from ibkr_fa.pipeline_os_models import ScheduleOsRunReport

See :mod:`ibkr_fa.pipeline_os` for the :func:`run_schedule_os`
orchestration that constructs this dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.invariants import require
from ibkr_fa.output._cleanup import RunOutputs
from ibkr_fa.parser.format_detect import IbkrCsvFormat

# Shared RunReport invariant helpers stay in pipeline.py (the
# per-schedule RunReport classes share them).
from ibkr_fa.pipeline import (
    _require_financial_year_end,
    _require_identity_fallback_subset,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)


@dataclass(frozen=True, slots=True)
class ScheduleOsRunReport:
    """Summary of one successful :func:`run_schedule_os` call.

    Schedule OS reports every supported ordinary portfolio dividend in
    row 1a(i). Indian/foreign source remains a separate audit dimension
    used by FSI, TR, and Form 67.

    Attributes:
        ordinary_dividend_rows_written: Total supported ordinary-dividend
            detail rows written to the single 1a(i) review block.
        indian_source_dividend_rows_written: Indian-source subset of those
            rows. Foreign-source count is the total minus this value.
        ordinary_dividend_total_inr_rounded: The single portal-1a(i)
            value, summed across all source countries at full precision
            then rounded HALF_UP to whole rupees.
        adr_reroutes_count: Number of dividend rows re-routed from
            the             foreign-source attribution group to the Indian-source group
            via the issue #183 underlying-issuer mapping
            (``user.toml`` ``[os].schedule_os_indian_adr_routing =
            "underlying-issuer"``). Always 0 in the default
            depositary-routing mode. When > 0, the CLI surfaces a
            stderr advisory explaining which ADRs were re-routed so
            the user can verify the re-routing matches their CA's
            expectation.             Subset of :attr:`indian_source_dividend_rows_written`
            (IN-prefix direct-share rows count toward the Indian-source
            group but not toward
            :attr:`adr_reroutes_count`). >= 0.
        output_path: The path the writer wrote to, as the caller gave it (not resolved).
        account_id: The IBKR account number parsed from the input's
            ``Account Information`` section. Non-empty. May be a
            mask string (e.g., ``"U**********"``) when the source
            statement masked the account ID and the user passed
            ``allow_masked_account=True``.
        account_id_cross_checked: Whether the parsed account ID was
            cross-checked against ``user.toml`` ``[account].number``.
            ``False`` only when the user opted out via
            ``allow_masked_account=True`` on a masked-ID statement.
        input_format: Which IBKR CSV serialisation the input was
            recognised as. Activity Statement only in v1 — Flex
            Query inputs raise (Schedule OS depends on the
            Dividends + Statement-period sections that Flex doesn't
            currently expose).
        financial_year_start: First day of the reporting FY
            (April 1 of ``FinancialYear.start_year``). Echoed back
            for the CLI's audit trail.
        financial_year_end: Last day of the reporting FY (March 31
            of ``FinancialYear.end_year``). Must equal
            ``financial_year_start`` plus one year minus one day —
            an explicit cross-field invariant guards against
            internal-construction bugs.
        fx_policy_appropriate_for_ay: Whether Rule115Policy is
            verified appropriate for the AY this FY maps to. ``True``
            for AY <= 2026-27 (FY <= 2025-26) via Section 536's
            transitional saving clause. ``False`` for AY >= 2027-28
            (FY >= 2026-27) — the Rules 2026 framework preserves the
            Rule 115(1), Explanation (2)(e) preceding-month-end semantic via Rule
            206(2)(b)(i) Sl. No. 5 but the carry-back fallback across
            non-publication days remains CBDT-FAQ-based pending
            primary-source verification (#126).
        identity_supplied_from_fallback: Frozenset of Activity
            Statement field names (``"Name"`` / ``"Account"``) that
            the dispatcher backfilled from ``user.toml`` under the
            #141 ``--accept-anonymised-statement`` flag because the
            IBKR export had hidden them. Empty in the normal case.
            When non-empty, the CLI surfaces a loud user-attested-
            identity warning to stderr.
        interest_total_inr_rounded: Sum of in-FY foreign cash-
            balance interest credits (from the IBKR Activity
            Statement Interest section, parsed via #139 and computed
            via :mod:`~ibkr_fa.compute.schedule_os_interest`),
            FX-converted via Rule 115(1), Explanation (2)(c) — the SBI
            TTBR of the last day of the assessee's previous year
            (= FY-end date for an Indian individual taxpayer; single
            rate for every credit in the FY), rounded HALF_UP to
            whole rupees per CBDT FAQ. This is the value the user
            pastes into Schedule OS row **1b(ix)** on every run.
            Always ``>= Decimal("0")``.
            Zero when the Activity Statement has no Interest section,
            when every interest credit fell outside the requested
            FY, or when every row was a debit / accrual
            (parser-skipped). Surfaced via the CLI verbose stdout
            line labelled with the exact 1b(ix) portal field, and as
            the 1b(ix) total row in every output format; v1 does not
            emit a per-credit interest detail block in the review file.
        interest_lines_count: Number of in-FY
            :class:`~ibkr_fa.compute.ScheduleOsInterestLine` records
            produced by the interest compute layer. Equal to
            ``len(interest_result.lines)``. CLI suppresses the
            entire 1b(ix) advisory when this is zero
            (silent-skip discipline). >= 0.
        interest_credits_out_of_fy_count: Number of parsed
            :class:`~ibkr_fa.models.InterestCredit` records whose
            ``credit_date`` fell outside ``[financial_year_start,
            financial_year_end]`` and was silently filtered by the
            interest compute layer. Surfaced so the CLI can advise
            the user when their statement spanned periods other
            than the requested FY (common when the IBKR Activity
            Statement is a calendar-year export). >= 0.
        portal_labels_assessment_year_start: 4-digit AY start year
            whose portal-labels snapshot the writer used for this
            run (issue #65). Derived from
            ``FinancialYear.assessment_year_start``. Pipelines
            thread this into the writer so the CSV's portal cells
            use AY-verified labels.
        portal_labels_used_forward_compat_fallback: ``True`` iff
            the writer's snapshot lookup hit the +1 forward-compat
            fallback in
            :func:`ibkr_fa.output.portal_labels.for_ay`. CLI
            surfaces a "labels not yet verified for this AY"
            warning when ``True``.
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
    """

    ordinary_dividend_rows_written: int
    indian_source_dividend_rows_written: int
    ordinary_dividend_total_inr_rounded: Decimal
    adr_reroutes_count: int
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode
    output_path: Path
    account_id: str
    account_id_cross_checked: bool
    input_format: IbkrCsvFormat
    financial_year_start: date
    financial_year_end: date
    fx_policy_appropriate_for_ay: bool
    identity_supplied_from_fallback: frozenset[str] = frozenset()
    # #32: in-FY foreign cash-balance interest aggregate for Schedule
    # OS row 1b(ix) residual other interest. Computed by
    # compute_schedule_os_interest_lines from the dispatcher's
    # interest_credits list, with Rule 115(1), Explanation (2)(c) FX
    # conversion: SBI TTBR of the last day of the assessee's previous
    # year (FY-end for an Indian individual taxpayer) — a single
    # specified date for every credit in the FY (NOT per-event
    # preceding-month-end, which is (2)(e) for dividends; this
    # distinction was a T5 codex P1 finding on PR #188 round 2).
    # Surfaced to the user via the CLI's schedule-os safety-warning
    # block so they know which rupee total to enter into ITR-2 cell
    # 1b(ix). Zero (and a zero count) for accounts whose Interest
    # section is absent or whose credits all fell outside the
    # requested FY. Sister fields for the in-FY detail-row count
    # (interest_lines_count) and the out-of-FY skip count
    # (interest_credits_out_of_fy_count) let the CLI distinguish
    # "no Interest section" from "Interest section present but no
    # in-FY credits" without re-parsing.
    interest_total_inr_rounded: Decimal = Decimal("0")
    interest_lines_count: int = 0
    interest_credits_out_of_fy_count: int = 0
    # Issue #65: per-AY portal labels tracking (see docstring above).
    portal_labels_assessment_year_start: int = field(kw_only=True)
    portal_labels_used_forward_compat_fallback: bool = False
    run_outputs: RunOutputs = field(kw_only=True)

    def __post_init__(self) -> None:
        require(
            self.ordinary_dividend_rows_written >= 0,
            what="ScheduleOsRunReport: ordinary_dividend_rows_written must be >= 0",
            ordinary_dividend_rows_written=self.ordinary_dividend_rows_written,
        )
        require(
            self.indian_source_dividend_rows_written >= 0,
            what="ScheduleOsRunReport: indian_source_dividend_rows_written must be >= 0",
            indian_source_dividend_rows_written=self.indian_source_dividend_rows_written,
        )
        require(
            self.indian_source_dividend_rows_written <= self.ordinary_dividend_rows_written,
            what=(
                "ScheduleOsRunReport: indian_source_dividend_rows_written "
                "must be <= ordinary_dividend_rows_written"
            ),
        )
        require(
            self.ordinary_dividend_total_inr_rounded >= Decimal("0"),
            what="ScheduleOsRunReport: ordinary_dividend_total_inr_rounded must be >= 0",
            ordinary_dividend_total_inr_rounded=str(self.ordinary_dividend_total_inr_rounded),
        )
        require(
            self.interest_total_inr_rounded >= Decimal("0"),
            what="ScheduleOsRunReport: interest_total_inr_rounded must be >= 0",
            interest_total_inr_rounded=str(self.interest_total_inr_rounded),
        )
        require(
            self.interest_lines_count >= 0,
            what="ScheduleOsRunReport: interest_lines_count must be >= 0",
            interest_lines_count=self.interest_lines_count,
        )
        require(
            self.interest_credits_out_of_fy_count >= 0,
            what=("ScheduleOsRunReport: interest_credits_out_of_fy_count must be >= 0"),
            interest_credits_out_of_fy_count=self.interest_credits_out_of_fy_count,
        )
        require(
            self.adr_reroutes_count >= 0,
            what="ScheduleOsRunReport: adr_reroutes_count must be >= 0",
            adr_reroutes_count=self.adr_reroutes_count,
        )
        require(
            self.adr_reroutes_count <= self.indian_source_dividend_rows_written,
            what=(
                "ScheduleOsRunReport: adr_reroutes_count must be <= "
                "indian_source_dividend_rows_written (re-routed ADRs are a "
                "subset of the Indian-source group; the rest are direct-Indian-share "
                "IN-prefix-ISIN rows)"
            ),
            adr_reroutes_count=self.adr_reroutes_count,
            indian_source_dividend_rows_written=self.indian_source_dividend_rows_written,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="ScheduleOsRunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )
        require(
            self.account_id != "",
            what="ScheduleOsRunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="ScheduleOsRunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="ScheduleOsRunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="ScheduleOsRunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
