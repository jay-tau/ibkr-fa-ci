"""Pipeline orchestrator for the Schedule CG STCG subcommand.

Extracted from :mod:`ibkr_fa.pipeline` (issue #155 splits the
2700+-line monolith into per-subcommand modules with a thin shared
core). Logic is byte-for-byte identical to the pre-extraction code;
this module just owns the dataclass + entry-point for the STCG
(short-term capital gains, holding period <= 24 months for foreign
equities per Section 2(42A)) report.

See :mod:`ibkr_fa.pipeline` for the broader orchestration contract
(dispatcher, FX-policy selection, cross-AY checks, identity-fallback
warnings); this module just wires those primitives for the STCG
schedule. The LTCG sister lives in :mod:`ibkr_fa.pipeline_cg_ltcg`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_portal_amounts import require_schedule_cg_filed_amounts
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase
from ibkr_fa.compute.schedule_os import require_no_non_ordinary_distributions
from ibkr_fa.compute.source_coverage import require_financial_year_contained_in_period
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.portal_labels import is_forward_compat_fallback
from ibkr_fa.parser.format_detect import IbkrCsvFormat

# Shared helpers / constants intentionally imported from the
# orchestrator core. These remain pipeline.py private members until
# the broader #155 split lands their final home; the leading
# underscores are tolerated here because both modules ship together
# and the import boundary is owned by the same author.
# All six names come through pipeline.py, which re-exports the CG
# required-section set from pipeline_dispatch.py and FinancialYear from
# models.tax_year, and defines the four ``_require_*`` run-report validators.
from ibkr_fa.pipeline import (
    _CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    FinancialYear,
    _require_financial_year_end,
    _require_identity_fallback_subset,
    _require_masked_account_id_if_unchecked,
    _require_plausible_assessment_year_start,
)
from ibkr_fa.pipeline_ibkr_input import _load_ibkr_activity_input


@dataclass(frozen=True, slots=True)
class ScheduleCgRunReport:
    """Summary of one successful :func:`run_schedule_cg` call.

    Schedule CG STCG is the foreign-source short-term capital-gains
    reporter that feeds ITR-2 Schedule CG Section A (foreign equity
    pathway). Mirrors :class:`~ibkr_fa.pipeline_os_models.ScheduleOsRunReport`
    shape for CLI-side rendering consistency.

    Attributes:
        stcg_rows_written: Number of in-FY, non-IN, <= 24-month STCG
            rows emitted to ``output_path``. >= 0.
        excluded_ltcg_deferred_count: Closed lots with holding period
            > 24 months (LTCG; see ``schedule-cg-ltcg``). >= 0.
        excluded_indian_source_count: Closed lots with ISIN prefix
            ``"IN"`` (domestic CG; out of v1 scope). >= 0.
        excluded_out_of_fy_count: Closed lots whose ``close_date`` is
            outside the reporting FY window. >= 0.
        total_realized_gain_inr_rounded: The filed STCG total,
            ``result.portal_amounts.filed_total_inr``: rounded
            consideration less rounded cost (#615). May be NEGATIVE —
            a net STCG loss is valid (carry-forward 8 AYs per Section 74).
        output_path: The path the writer wrote to, as the caller gave it (not resolved).
        account_id: The IBKR account number parsed from the input.
            May be a mask string when ``allow_masked_account=True``.
        account_id_cross_checked: Whether the parsed account ID was
            cross-checked against ``user.toml``.
        input_format: Which IBKR CSV serialisation was recognised.
            Activity Statement only in v1 — Flex Query inputs raise
            (CG depends on Trades section's FIFO-matched closures).
        financial_year_start: First day of the reporting FY.
        financial_year_end: Last day of the reporting FY.
        fx_policy_appropriate_for_ay: Whether
            Rule115CapitalGainsPolicy is verified appropriate for the
            AY this FY maps to. ``True`` for AY <= 2026-27. ``False``
            for AY >= 2027-28 (the Rules 2026 framework preserves
            Rule 115(1), Explanation (2)(f) verbatim per Rule 206 Table; carry-back
            fallback pending primary-source verification at #126).
        identity_supplied_from_fallback: Frozenset of Activity
            Statement field names (``"Name"`` / ``"Account"``) that
            the dispatcher backfilled from ``user.toml`` under the
            #141 ``--accept-anonymised-statement`` flag because the
            IBKR export had hidden them. Empty in the normal case.
            When non-empty, the CLI surfaces a loud user-attested-
            identity warning to stderr.
        portal_labels_assessment_year_start: 4-digit AY start year
            whose portal-labels snapshot the writer used for this
            run (issue #65). Derived from
            ``FinancialYear.assessment_year_start``. The pipeline
            passes this through to the writer so the CSV's portal-
            facing cells use labels verified for the AY this run
            files into (not the process-wide default).
        portal_labels_used_forward_compat_fallback: ``True`` iff
            the writer's snapshot lookup hit the +1 forward-compat
            fallback in
            :func:`ibkr_fa.output.portal_labels.for_ay`. CLI
            surfaces a "labels not yet verified for this AY"
            warning to the user when ``True``. ``False`` in the
            normal case (registered snapshot found exactly).
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
        table_f_loss_across_bands: ``((3, <output file name>),)`` when the
            review file leaves Schedule CG Table F row 3's date bands blank
            because a band nets a loss and two or more bands net gains, so
            the filer enters them (#1361); else empty.
    """

    stcg_rows_written: int
    excluded_ltcg_deferred_count: int
    excluded_indian_source_count: int
    excluded_out_of_fy_count: int
    total_realized_gain_inr_rounded: Decimal
    output_path: Path
    account_id: str
    account_id_cross_checked: bool
    input_format: IbkrCsvFormat
    financial_year_start: date
    financial_year_end: date
    fx_policy_appropriate_for_ay: bool
    identity_supplied_from_fallback: frozenset[str] = frozenset()
    # Issue #65: per-AY portal labels tracking. See
    # :class:`~ibkr_fa.pipeline_os.ScheduleOsRunReport` for the full
    # contract — identical semantics here.
    portal_labels_assessment_year_start: int = field(kw_only=True)
    portal_labels_used_forward_compat_fallback: bool = False
    run_outputs: RunOutputs = field(kw_only=True)
    table_f_loss_across_bands: tuple[tuple[int, str], ...] = field(kw_only=True)

    def __post_init__(self) -> None:
        require(
            self.stcg_rows_written >= 0,
            what="ScheduleCgRunReport: stcg_rows_written must be >= 0",
            stcg_rows_written=self.stcg_rows_written,
        )
        require(
            self.excluded_ltcg_deferred_count >= 0,
            what="ScheduleCgRunReport: excluded_ltcg_deferred_count must be >= 0",
            excluded_ltcg_deferred_count=self.excluded_ltcg_deferred_count,
        )
        require(
            self.excluded_indian_source_count >= 0,
            what="ScheduleCgRunReport: excluded_indian_source_count must be >= 0",
            excluded_indian_source_count=self.excluded_indian_source_count,
        )
        require(
            self.excluded_out_of_fy_count >= 0,
            what="ScheduleCgRunReport: excluded_out_of_fy_count must be >= 0",
            excluded_out_of_fy_count=self.excluded_out_of_fy_count,
        )
        # NOTE: total_realized_gain_inr_rounded may be NEGATIVE.
        # Unlike Schedule OS (gross dividend, always >= 0), Schedule
        # CG STCG legitimately contains net losses (Section 74
        # carry-forward).
        require(
            self.account_id != "",
            what="ScheduleCgRunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="ScheduleCgRunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="ScheduleCgRunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="ScheduleCgRunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="ScheduleCgRunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )


def run_schedule_cg(
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
) -> ScheduleCgRunReport:
    """Run the Schedule CG STCG pipeline: IBKR Activity Statement CSV in, review CSV out.

    Schedule CG STCG reports foreign-source short-term capital gains
    (holding period <= 24 months per Section 2(42A) for foreign equities).
    The pipeline parses the Trades section, runs FIFO matching to produce
    a :class:`~ibkr_fa.models.ClosedLot` stream, applies Rule 115 FX
    conversion once per disposed lot at the transfer-event specified date
    (preceding-month-end TTBR of the close date), partitions the resulting
    rows into four buckets (STCG / LTCG-deferred / IN-domestic /
    out-of-FY), and serialises everything to a review CSV.

    Pipeline stages, in order, once any earlier ``output_path`` is removed
    (so a refusal leaves no output behind):

    1. :func:`~ibkr_fa.pipeline_ibkr_input._load_ibkr_activity_input` —
       parses ``user.toml``, refuses Flex Query input (the CG pathway
       depends on the Trades-section FIFO-matched closures introduced by
       #104, which the Flex parser doesn't currently produce in the same
       shape), dispatches with ``required_sections=_CG_DEFAULT_...`` and
       cross-checks ``AccountInfo.account_id`` against ``user.toml``.
       Dispatch parses Statement + Account Information + FII + Trades
       (Open Positions parsed when present; absent in the closure-only
       case #162). ``match_fifo`` FIFO-matches the Trades C-rows
       against earlier O-rows to produce ``closed_lots``.
    2. :meth:`~ibkr_fa.fx.context.TaxFxContext.for_fy` — supplies the
       capital-gains FX policy and the cross-AY verification status
       (same gate as Schedule OS). Then
       :func:`~ibkr_fa.compute.schedule_os.require_no_non_ordinary_distributions`
       refuses a return of capital, long-term capital gain or cash in
       lieu row on any date (#1310).
    3. ``compute_schedule_cg_stcg_lines(closed_lots, ...)`` — partitions
       into the four buckets and applies a single Rule 115 FX
       conversion to STCG rows at the transfer-event specified date.
    4. ``output_binding(Subcommand.CG, format_id).writer(ctx).write(result, output_path)`` —
       emits the review CSV with detail rows + audit blocks + the Table F
       block (#1361) + TOTAL.

    Args:
        input_path: Path to the IBKR Activity Statement CSV.
        config_path: Path to ``user.toml``.
        output_path: Destination path for the review CSV.
        financial_year: The :class:`FinancialYear` being reported.
        format_id: CG output format. Defaults to ``"review-csv"``.
        itr_form: Return form whose Schedule CG destination label is emitted.
        allow_masked_account: Opt out of the account-ID cross-check
            when the source statement masks the account ID.
        suppress_cross_ay_warning: Suppress the AY 2027-28+ cross-AY
            verification warning.
        accept_anonymised_statement: Opt into the #141 fallback path
            for inputs where IBKR's privacy exporter has hidden the
            ``Name`` and/or ``Account`` rows. Default ``False``.
            When ``True``, dispatcher backfills from
            ``config.account.holder_name`` /
            ``config.account.number`` and records in
            :attr:`ScheduleCgRunReport.identity_supplied_from_fallback`.

    Returns:
        :class:`ScheduleCgRunReport` summarising the run for the CLI.

    Raises:
        FileNotFoundError: If ``input_path`` or ``config_path`` doesn't exist.
        ConfigError: If ``user.toml`` is malformed, or if the parsed
            account ID doesn't match.
        ParseError: If the input format is Flex Query, if required
            Activity Statement sections are missing, if the statement
            period doesn't span the requested FY, or if the Trades
            FIFO matching fails (insufficient opens, non-FIFO basis,
            etc.).
        NonOrdinaryDistributionError: If the statement's Dividends section
            has a return of capital, long-term capital gain or cash in lieu
            row, on any date (#1310).
        MissingFxRateError: If the transfer-event specified date
            lacks a Rule115CapitalGainsPolicy rate within the
            carry-back window.
        InvariantError: If the filed consideration, cost or balance falls
            outside the CBDT 14-digit bound; raised before any writer runs.
        OutputCollisionError: If ``output_path`` is ``input_path`` or
            ``config_path``.
        OutputRemovalError: If an earlier ``output_path``, or the output of
            a run that fails during or after its write, cannot be removed.
        UnknownOutputFormatError: If ``format_id`` is not registered for CG.
    """
    remove_previous_outputs((output_path,), (("--input", input_path), ("--config", config_path)))
    ibkr_input = _load_ibkr_activity_input(
        input_path=input_path,
        config_path=config_path,
        period_validator=functools.partial(
            require_financial_year_contained_in_period,
            financial_year=financial_year,
            report_name="Schedule CG capital-gains reporting",
        ),
        broker_validator=None,
        required_sections=_CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        unsupported_format_message=lambda input_format: (
            f"Schedule CG pipeline requires an IBKR Activity Statement "
            f"input; got {input_format.value!r}. Flex Query inputs are "
            f"unsupported because the Schedule CG compute layer depends "
            f"on the Trades-section FIFO-matched ClosedLot stream that "
            f"Flex doesn't currently produce in the same shape (see #104)."
        ),
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
        check_income_against_nav=False,
    )

    fx_ctx = TaxFxContext.for_fy(
        financial_year,
        ibkr_input.config.fx,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
    )
    # #1310: on any date, since one before the FY can change a lot sold in it.
    require_no_non_ordinary_distributions(ibkr_input.extract.dividends)
    result: ScheduleCgStcgComputeResult = compute_schedule_cg_stcg_lines(
        ibkr_input.extract.closed_lots,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.capital_gains,
    )
    require_schedule_cg_filed_amounts(result.stcg_lines, schedule="STCG")

    binding = output_binding(Subcommand.CG, format_id)
    run_outputs = RunOutputs((output_path,), ())
    with run_outputs.removed_on_failure():
        binding.writer(
            RenderContext(
                assessment_year_start=financial_year.assessment_year_start, itr_form=itr_form
            )
        ).write(result, output_path)

        require(
            output_path.exists(),
            what=(
                "Schedule CG pipeline post-write: writer must have produced a file at output_path"
            ),
            output_path=str(output_path),
        )

        return ScheduleCgRunReport(
            stcg_rows_written=len(result.stcg_lines),
            excluded_ltcg_deferred_count=len(result.excluded_ltcg_deferred),
            excluded_indian_source_count=len(result.excluded_indian_source),
            excluded_out_of_fy_count=len(result.excluded_out_of_fy),
            total_realized_gain_inr_rounded=result.portal_amounts.filed_total_inr,
            output_path=output_path,
            account_id=str(ibkr_input.extract.account_info.account_id),
            account_id_cross_checked=ibkr_input.account_id_cross_checked,
            input_format=ibkr_input.input_format,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            fx_policy_appropriate_for_ay=fx_ctx.fx_policy_appropriate_for_ay,
            identity_supplied_from_fallback=ibkr_input.extract.identity_fallback_used,
            portal_labels_assessment_year_start=financial_year.assessment_year_start,
            portal_labels_used_forward_compat_fallback=is_forward_compat_fallback(
                financial_year.assessment_year_start
            ),
            run_outputs=run_outputs,
            table_f_loss_across_bands=(
                ((3, output_path.name),)
                if result.table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
                else ()
            ),
        )
