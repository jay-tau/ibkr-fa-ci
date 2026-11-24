"""Pipeline orchestrator for the Schedule CG LTCG subcommand.

Extracted from :mod:`ibkr_fa.pipeline` (issue #155 splits the
2700+-line monolith into per-subcommand modules with a thin shared
core). Logic is byte-for-byte identical to the pre-extraction code;
this module just owns the dataclass + entry-point for the LTCG
(long-term capital gains, new regime, post-2024-07-23) report.

See :mod:`ibkr_fa.pipeline` for the broader orchestration contract
(dispatcher, FX-policy selection, cross-AY checks, identity-fallback
warnings); this module just wires those primitives for the LTCG
schedule.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
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
class ScheduleCgLtcgRunReport:
    """Summary of one successful :func:`run_schedule_cg_ltcg` call.

    Schedule CG LTCG is the long-term capital-gains reporter for the
    new regime (12.5% without indexation, disposals on or after
    2024-07-23 per Finance (No. 2) Act, 2024). Mirrors the
    :class:`~ibkr_fa.pipeline_cg.ScheduleCgRunReport` shape for
    CLI-side rendering consistency.

    Attributes:
        ltcg_rows_written: Number of in-FY, non-IN, > 24mo,
            post-2024-07-23 LTCG rows emitted. >= 0.
        excluded_stcg_count: Closed lots with holding period
            <= 24 months (handled by schedule-cg). >= 0.
        excluded_pre_2024_07_23_count: Long-term lots disposed before
            2024-07-23 (old regime needs indexation; out of v1
            scope). >= 0.
        excluded_indian_source_count: Closed lots with ISIN prefix
            ``"IN"`` (domestic CG; defense-in-depth bucket; unreachable
            through current parser path). >= 0.
        excluded_out_of_fy_count: Closed lots whose ``close_date`` is
            outside the reporting FY window. >= 0.
        total_realized_gain_inr_rounded: The filed LTCG total,
            ``result.portal_amounts.filed_total_inr``: rounded
            consideration less rounded cost (#615). May be NEGATIVE — a net
            LTCG loss is valid (Section 74's 8-AY carry-forward
            applies to long-term losses too, but they can only set
            off against long-term gains in subsequent AYs).
        output_path: The path the writer wrote to, as the caller gave it (not resolved).
        account_id: The IBKR account number parsed from the input.
            May be a mask string when ``allow_masked_account=True``.
        account_id_cross_checked: Whether the parsed account ID was
            cross-checked against ``user.toml``.
        input_format: Which IBKR CSV serialisation was recognised.
            Activity Statement only in v1.
        financial_year_start: First day of the reporting FY.
        financial_year_end: Last day of the reporting FY.
        fx_policy_appropriate_for_ay: Whether the FX policy is
            verified appropriate for the AY this FY maps to (same
            gate as the STCG and OS reports).
        identity_supplied_from_fallback: Frozenset of Activity
            Statement field names (``"Name"`` / ``"Account"``) that
            the dispatcher backfilled from ``user.toml`` under the
            #141 ``--accept-anonymised-statement`` flag because the
            IBKR export had hidden them. Empty in the normal case.
            When non-empty, the CLI surfaces a loud user-attested-
            identity warning to stderr.
        portal_labels_assessment_year_start: 4-digit AY start year
            whose portal-labels snapshot the writer used for this
            run (issue #65). See
            :class:`~ibkr_fa.pipeline_os.ScheduleOsRunReport` for
            the full contract.
        portal_labels_used_forward_compat_fallback: ``True`` iff
            the writer's snapshot lookup hit the +1 forward-compat
            fallback. CLI surfaces a "labels not yet verified for
            this AY" warning when ``True``.
        run_outputs: Every output the run may have published, which the CLI
            attempts to remove if rendering or printing its report fails (#832, #1227).
        table_f_loss_across_bands: ``((5, <output file name>),)`` when the
            review file leaves Schedule CG Table F row 5's date bands blank
            because a band nets a loss and two or more bands net gains, so
            the filer enters them (#1361); else empty.
    """

    ltcg_rows_written: int
    excluded_stcg_count: int
    excluded_pre_2024_07_23_count: int
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
            self.ltcg_rows_written >= 0,
            what="ScheduleCgLtcgRunReport: ltcg_rows_written must be >= 0",
            ltcg_rows_written=self.ltcg_rows_written,
        )
        require(
            self.excluded_stcg_count >= 0,
            what="ScheduleCgLtcgRunReport: excluded_stcg_count must be >= 0",
            excluded_stcg_count=self.excluded_stcg_count,
        )
        require(
            self.excluded_pre_2024_07_23_count >= 0,
            what="ScheduleCgLtcgRunReport: excluded_pre_2024_07_23_count must be >= 0",
            excluded_pre_2024_07_23_count=self.excluded_pre_2024_07_23_count,
        )
        require(
            self.excluded_indian_source_count >= 0,
            what="ScheduleCgLtcgRunReport: excluded_indian_source_count must be >= 0",
            excluded_indian_source_count=self.excluded_indian_source_count,
        )
        require(
            self.excluded_out_of_fy_count >= 0,
            what="ScheduleCgLtcgRunReport: excluded_out_of_fy_count must be >= 0",
            excluded_out_of_fy_count=self.excluded_out_of_fy_count,
        )
        # total_realized_gain_inr_rounded may be NEGATIVE (long-term
        # capital LOSS — Section 74 8-AY carry-forward applies, but
        # can only set off against long-term gains).
        require(
            self.account_id != "",
            what="ScheduleCgLtcgRunReport: account_id must be non-empty",
        )
        _require_financial_year_end(
            run_report_class_name="ScheduleCgLtcgRunReport",
            financial_year_start=self.financial_year_start,
            financial_year_end=self.financial_year_end,
        )
        _require_masked_account_id_if_unchecked(
            run_report_class_name="ScheduleCgLtcgRunReport",
            account_id=self.account_id,
            account_id_cross_checked=self.account_id_cross_checked,
        )
        _require_identity_fallback_subset(
            run_report_class_name="ScheduleCgLtcgRunReport",
            identity_supplied_from_fallback=self.identity_supplied_from_fallback,
        )
        _require_plausible_assessment_year_start(
            run_report_class_name="ScheduleCgLtcgRunReport",
            portal_labels_assessment_year_start=self.portal_labels_assessment_year_start,
        )


def run_schedule_cg_ltcg(
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
) -> ScheduleCgLtcgRunReport:
    """Run the Schedule CG LTCG pipeline: IBKR Activity Statement CSV in, review CSV out.

    Schedule CG LTCG reports foreign-source long-term capital gains
    (holding period > 24 calendar months per Section 2(42A) for
    foreign equities) under the **new regime** (12.5% without
    indexation, disposals on or after 2024-07-23 per Finance (No. 2)
    Act, 2024). Pre-2024-07-23 disposals require indexation via
    CBDT-published Cost Inflation Index data and are routed to a
    dedicated audit bucket (out of v1 scope).

    The pipeline parses the Trades section, runs FIFO matching to
    produce a :class:`~ibkr_fa.models.ClosedLot` stream, applies Rule
    115(1), Explanation (2)(f), FX conversion once per disposed lot
    at the transfer-event specified date, partitions the rows into FIVE buckets (LTCG /
    STCG-deferred / pre-2024-07-23-deferred / IN-domestic /
    out-of-FY), and serialises everything to a review CSV.

    Args:
        input_path: Path to the IBKR Activity Statement CSV.
        config_path: Path to ``user.toml``.
        output_path: Destination path for the review CSV.
        financial_year: The :class:`FinancialYear` being reported.
        format_id: CG-LTCG output format. Defaults to ``"review-csv"``.
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
            :attr:`ScheduleCgLtcgRunReport.identity_supplied_from_fallback`.

    Returns:
        :class:`ScheduleCgLtcgRunReport` summarising the run.

    Raises:
        FileNotFoundError: If ``input_path`` or ``config_path`` doesn't exist.
        ConfigError: If ``user.toml`` is malformed, or if the parsed
            account ID doesn't match.
        ParseError: If the input format is Flex Query, if required
            Activity Statement sections are missing, if the statement
            period doesn't span / contain the requested FY, or if the
            Trades FIFO matching fails.
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
        UnknownOutputFormatError: If ``format_id`` is not registered
            for CG-LTCG.
    """
    remove_previous_outputs((output_path,), (("--input", input_path), ("--config", config_path)))
    ibkr_input = _load_ibkr_activity_input(
        input_path=input_path,
        config_path=config_path,
        period_validator=functools.partial(
            require_financial_year_contained_in_period,
            financial_year=financial_year,
            report_name="Schedule CG capital-gains reporting (LTCG)",
        ),
        broker_validator=None,
        required_sections=_CG_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        unsupported_format_message=lambda input_format: (
            f"Schedule CG LTCG pipeline requires an IBKR Activity "
            f"Statement input; got {input_format.value!r}. Flex Query "
            f"inputs are unsupported because the CG pathway depends "
            f"on the Trades-section FIFO-matched ClosedLot stream "
            f"that Flex doesn't currently produce in the same shape "
            f"(see #104)."
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
    result: ScheduleCgLtcgComputeResult = compute_schedule_cg_ltcg_lines(
        ibkr_input.extract.closed_lots,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.capital_gains,
    )
    require_schedule_cg_filed_amounts(result.ltcg_lines, schedule="LTCG")

    binding = output_binding(Subcommand.CG_LTCG, format_id)
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
                "Schedule CG LTCG pipeline post-write: writer must have produced a file at "
                "output_path"
            ),
            output_path=str(output_path),
        )

        return ScheduleCgLtcgRunReport(
            ltcg_rows_written=len(result.ltcg_lines),
            excluded_stcg_count=len(result.excluded_stcg),
            excluded_pre_2024_07_23_count=len(result.excluded_pre_2024_07_23),
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
                ((5, output_path.name),)
                if result.table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
                else ()
            ),
        )
