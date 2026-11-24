"""Pipeline orchestrator for the Schedule OS subcommand.

Extracted from :mod:`ibkr_fa.pipeline` (issue #155 splits the
2700+-line monolith into per-subcommand modules with a thin shared
core). Logic is byte-for-byte identical to the pre-extraction code;
this module just owns the dataclass + entry-point for the Schedule
OS report (row 1a(i) ordinary dividends + row 1b(ix) other interest).

See :mod:`ibkr_fa.pipeline` for the broader orchestration contract
(dispatcher, FX-policy selection, cross-AY checks, identity-fallback
warnings); this module just wires those primitives for the OS
schedule.
"""

from __future__ import annotations

import functools
from pathlib import Path

from ibkr_fa.compute.income_engine import compute_schedule_os_result
from ibkr_fa.compute.schedule_os_interest import require_no_unproven_interest
from ibkr_fa.compute.source_coverage import require_financial_year_contained_in_period
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.invariants import require
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output._cleanup import RunOutputs, remove_previous_outputs
from ibkr_fa.output.portal_labels import is_forward_compat_fallback

# Both names come through pipeline.py, which re-exports the OS required-section
# set from pipeline_dispatch.py and FinancialYear from models.tax_year.
from ibkr_fa.pipeline import (
    _OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
    FinancialYear,
)
from ibkr_fa.pipeline_ibkr_input import _load_ibkr_activity_input

# ScheduleOsRunReport lives in pipeline_os_models.py. Re-exported here so legacy
# ``from ibkr_fa.pipeline_os import ScheduleOsRunReport`` callers
# (CLI + tests) still work; new code SHOULD import from
# ibkr_fa.pipeline_os_models directly.
from ibkr_fa.pipeline_os_models import ScheduleOsRunReport

__all__ = ["ScheduleOsRunReport", "run_schedule_os"]


def run_schedule_os(
    *,
    input_path: Path,
    config_path: Path,
    output_path: Path,
    financial_year: FinancialYear,
    format_id: str = "review-csv",
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
) -> ScheduleOsRunReport:
    """Run the Schedule OS pipeline: IBKR Activity Statement CSV in, review CSV out.

    Schedule OS reports supported ordinary dividends in one row 1a(i)
    total. The pipeline retains Indian/foreign source groups for
    FSI/TR/Form 67, applies the same Rule 115(1), Explanation (2)(e) conversion to both,
    sums at full precision, and rounds once.

    Pipeline stages, in order, once any earlier ``output_path`` is removed
    (so a refusal leaves no output behind):

    1. :func:`~ibkr_fa.pipeline_ibkr_input._load_ibkr_activity_input` —
       parses ``user.toml``, refuses Flex Query input (Schedule OS
       depends on the Dividends section that Flex doesn't yet expose),
       dispatches with ``required_sections=_OS_DEFAULT_...`` and
       ``period_validator=require_financial_year_contained_in_period(...,
       financial_year=fy)`` (the statement may be wider than the FY, #1284),
       and cross-checks ``AccountInfo.account_id`` against ``user.toml``
       (same safety net as :func:`~ibkr_fa.pipeline_fa.run_fa_a3`).
       Dispatch parses Statement + Account Information (Tier-1
       mandatory) and conditionally parses Dividends + Withholding Tax
       (Tier-2). TRADES is absent from the OS required set so the FII +
       Trades + reconciliation block is skipped entirely. When the
       statement prints ``Change in NAV``, its income fields are checked
       against those sections and Interest (#1279).
    2. :meth:`~ibkr_fa.fx.context.TaxFxContext.for_fy` — its verdict says
       whether the 1961-Act Rule 115 framework is verified appropriate
       for the AY this FY maps to (FY ``N``-``N+1`` → AY-start ``N+1``).
       Does NOT block; sets
       :attr:`ScheduleOsRunReport.fx_policy_appropriate_for_ay`.
    3. :func:`~ibkr_fa.compute.income_engine.compute_schedule_os_result`
       converts with the context's Rule 115 policies and computes the
       foreign-source, Indian-source and excluded audit groups plus
       1b(ix) interest.
    4. Write one globally sorted ordinary-dividend block and one 1a(i)
       portal total.

    Cross-AY warning rendering is the CLI's responsibility — the pipeline
    only sets :attr:`ScheduleOsRunReport.fx_policy_appropriate_for_ay`.

    Args:
        input_path: Path to the IBKR Activity Statement CSV. Flex Query
            inputs raise :class:`ParseError`.
        config_path: Path to ``user.toml``.
        output_path: Destination path for the review CSV. Parent
            directory must exist (writer fails loud on a typo).
        financial_year: The :class:`FinancialYear` being reported.
            Drives the check that the statement period contains the FY,
            the compute layer's in-FY filter, and the cross-AY FX-policy check.
        format_id: OS output format. Defaults to ``"review-csv"`` (the
            only OS format shipped in v1). Future formats register via
            :func:`ibkr_fa.output.register` and a catalog entry.
        allow_masked_account: Opt out of the account-ID cross-check
            when the source statement masks the account ID. See
            :func:`~ibkr_fa.pipeline_fa.run_fa_a3` for the safety-net semantics.
        suppress_cross_ay_warning: Suppress the AY 2027-28+ cross-AY
            verification warning. Default ``False``. No effect for
            AY <= 2026-27 where the 1961-Act framework is verified.
        accept_anonymised_statement: Opt into the #141 fallback path
            for inputs where IBKR's privacy exporter has hidden the
            ``Name`` and/or ``Account`` rows from the Account
            Information section. Default ``False`` (hard-fail on
            missing identity fields, preserving the safety net).
            When ``True``, dispatcher backfills the absent fields
            from ``config.account.holder_name`` /
            ``config.account.number`` and records the substituted
            field names in
            :attr:`ScheduleOsRunReport.identity_supplied_from_fallback`.

    Returns:
        :class:`ScheduleOsRunReport` summarising the run for the CLI.

    Raises:
        FileNotFoundError: If ``input_path`` or ``config_path`` doesn't exist.
        ConfigError: If ``user.toml`` is malformed, or if the parsed
            account ID doesn't match (see :func:`_cross_check_account_id`).
        ParseError: If the input format is Flex Query (Schedule OS
            depends on the Dividends section), if required Activity
            Statement sections are missing, if the statement period
            doesn't contain the requested FY, or if the Dividends section
            is malformed.
        ChangeInNavIncomeError: If the statement's ``Change in NAV``
            contradicts its Dividends, Withholding Tax or Interest
            section, including a missing one (#1279). Nothing is written.
        UnprovenInterestError: If an Interest row is unproven (an
            accrual-labelled row, a positive row without IBKR's
            credit-interest wording, or a negative or zero row that isn't
            debit interest; #905, #1280). Nothing is written.
        UnrecognisedDistributionTypeError: If a dividend's distribution
            type matches neither the allow nor the reject list. Fail-fast
            on novel types — silent classification misses would corrupt
            treaty-rate reporting.
        PaymentInLieuOfDividendError: If an in-FY dividend row is a
            Payment In Lieu Of Dividend (#1281). Nothing is written.
        NonOrdinaryDistributionError: If an in-FY dividend row is a
            Return Of Capital, Long-Term Capital Gain or Cash in Lieu
            (#1310). Nothing is written.
        MissingFxRateError: If a payment date lacks a Rule115Policy
            rate within the carry-back window.
        OutputCollisionError: If ``output_path`` is ``input_path`` or
            ``config_path``.
        OutputRemovalError: If an earlier ``output_path``, or the output of
            a run that fails during or after its write, cannot be removed.
        UnknownOutputFormatError: If ``format_id`` is not registered for OS.
    """
    remove_previous_outputs((output_path,), (("--input", input_path), ("--config", config_path)))
    ibkr_input = _load_ibkr_activity_input(
        input_path=input_path,
        config_path=config_path,
        period_validator=functools.partial(
            require_financial_year_contained_in_period,
            financial_year=financial_year,
            report_name="Schedule OS dividend reporting",
            needs_lot_history=False,
        ),
        broker_validator=None,
        required_sections=_OS_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        unsupported_format_message=lambda input_format: (
            f"Schedule OS pipeline requires an IBKR Activity Statement "
            f"input; got {input_format.value!r}. Flex Query inputs are "
            f"unsupported because the Schedule OS compute layer depends "
            f"on the Dividends section that Flex doesn't currently "
            f"expose (sister gap to #138)."
        ),
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
        check_income_against_nav=True,
    )

    require_no_unproven_interest(ibkr_input.extract.interest_non_credits)
    fx_ctx = TaxFxContext.for_fy(
        financial_year,
        ibkr_input.config.fx,
        suppress_cross_ay_warning=suppress_cross_ay_warning,
    )
    result, interest_result, _substitute_result, _other_result = compute_schedule_os_result(
        ibkr_input.config,
        financial_year,
        fx_ctx=fx_ctx,
        dividends=ibkr_input.extract.dividends,
        interest_credits=ibkr_input.extract.interest_credits,
    )

    binding = output_binding(Subcommand.OS, format_id)
    run_outputs = RunOutputs((output_path,), ())
    with run_outputs.removed_on_failure():
        binding.writer(
            RenderContext(
                assessment_year_start=financial_year.assessment_year_start,
                source_label="Interactive Brokers",
            )
        ).write(result, output_path)

        require(
            output_path.exists(),
            what=(
                "Schedule OS pipeline post-write: writer must have produced a file at output_path"
            ),
            output_path=str(output_path),
        )

        # adr_reroutes_count: count of domestic_lines whose adr_source_isin
        # is populated (issue #183 underlying-issuer re-routing). The
        # remaining domestic_lines are direct-Indian-share IN-prefix-ISIN
        # rows (issue #158); their adr_source_isin is None.
        adr_reroutes_count = sum(
            1 for line in result.domestic_lines if line.adr_source_isin is not None
        )

        return ScheduleOsRunReport(
            ordinary_dividend_rows_written=len(result.lines) + len(result.domestic_lines),
            indian_source_dividend_rows_written=len(result.domestic_lines),
            ordinary_dividend_total_inr_rounded=result.ordinary_dividend_total_inr_rounded,
            adr_reroutes_count=adr_reroutes_count,
            adr_routing_mode=ibkr_input.config.os.schedule_os_indian_adr_routing,
            output_path=output_path,
            account_id=str(ibkr_input.extract.account_info.account_id),
            account_id_cross_checked=ibkr_input.account_id_cross_checked,
            input_format=ibkr_input.input_format,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            fx_policy_appropriate_for_ay=fx_ctx.fx_policy_appropriate_for_ay,
            identity_supplied_from_fallback=ibkr_input.extract.identity_fallback_used,
            interest_total_inr_rounded=interest_result.total_inr_rounded,
            interest_lines_count=len(interest_result.lines),
            interest_credits_out_of_fy_count=interest_result.excluded_out_of_fy_count,
            portal_labels_assessment_year_start=financial_year.assessment_year_start,
            portal_labels_used_forward_compat_fallback=is_forward_compat_fallback(
                financial_year.assessment_year_start
            ),
            run_outputs=run_outputs,
        )
