"""Internal compute helper shared between Schedule FSI and Schedule TR pipelines.

PR #217 step 3/N of #36 introduced Schedule TR as a downstream
aggregator of Schedule FSI. Both pipelines need to perform the
identical parse + compute work (load config, AY/FX-policy check,
format detect, dispatch Activity Statement, account cross-check,
FX-policy construction, per-aggregator compute runs, per-ISIN
orphan-WHT guard, ``compute_schedule_fsi``) before writing +
assembling a per-schedule run report.

This module owns the shared compute seam: it produces a
:class:`_ScheduleFsiComputeContext` that bundles the FSI compute
result with every upstream-count / metadata field the consuming
per-schedule pipeline needs. The seam lives in its own module
(rather than inline in :mod:`ibkr_fa.pipeline_fsi`) because several
per-schedule pipelines share it.

The leading-underscore naming signals that these are an internal
contract between the per-schedule orchestrators
(:func:`ibkr_fa.pipeline_fsi.run_schedule_fsi`,
:func:`ibkr_fa.pipeline_tr.run_schedule_tr`,
:func:`ibkr_fa.pipeline_form67.run_form67`), plus the CLI's run-all
Form 67 readiness pre-check. Downstream callers should always go
through one of those orchestrators.

Issue #225 fix (PR #251): the v1 broad guard that blocked
the combination of ``adr_routing_mode=UNDERLYING_ISSUER`` and any
in-FY WHT has been removed. The compute layer now skips Indian-ADR
WHT rows symmetrically with the Schedule OS dividend rerouting,
and the per-ISIN orphan guard below excludes those same mapped
Indian-ADR ISINs from its check so the rerouted-but-not-orphaned
case passes silently.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.income_engine import compute_income_schedules
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_os_interest import require_no_unproven_interest
from ibkr_fa.compute.source_coverage import require_financial_year_contained_in_period
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import ParseError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.parser.format_detect import IbkrCsvFormat
from ibkr_fa.pipeline import _FSI_DEFAULT_REQUIRED_ACTIVITY_SECTIONS, FinancialYear
from ibkr_fa.pipeline_ibkr_input import _load_ibkr_activity_input

__all__ = [
    "_FsiReuseScope",
    "_ScheduleFsiComputeContext",
    "_compute_schedule_fsi_for_pipeline",
]

# The IBKR adapter tells the shared engine that its interest payer is US
# (#698), which is right only for this IBKR company (#665).
_SUPPORTED_BROKER_NAME: Final[str] = "Interactive Brokers LLC"


def _require_ibkr_llc(broker_names: tuple[str, ...]) -> None:
    """Refuse a statement unless its only ``BrokerName`` is IBKR LLC (#665).

    Any other IBKR company, and any missing, blank or repeated
    ``BrokerName``, is refused whether or not the statement has
    interest, rather than guessing which country the interest arose in.
    The message never prints the cell, which is input (#1047).
    """
    if broker_names == (_SUPPORTED_BROKER_NAME,):
        return
    if not broker_names:
        found = "has no BrokerName row"
    elif len(broker_names) > 1:
        found = f"has {len(broker_names)} BrokerName rows"
    elif not broker_names[0].strip():
        found = "has a blank BrokerName"
    else:
        found = "names another broker"
    raise ParseError(
        section="Statement",
        row_index=0,
        column="BrokerName",
        message=(
            f"The Activity Statement {found}; only Interactive Brokers LLC "
            f"statements are supported."
        ),
        raw_value="",
    )


@dataclass(frozen=True, slots=True)
class _ScheduleFsiComputeContext:
    """Internal shared Schedule FSI compute context.

    PR #217 (Schedule TR pipeline; step 3/N of #36) introduced this
    bundle as the seam between shared "parse + compute FSI" work and
    per-pipeline "write + assemble report" work. It carries the FSI
    and Form 67 compute results plus upstream counts, AY/FX flags,
    account metadata, and identity-fallback state; writer-specific and
    row-derived aggregates stay with each schedule pipeline.
    """

    fsi_result: ScheduleFsiComputeResult
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
    account_id: str
    account_id_cross_checked: bool
    input_format: IbkrCsvFormat
    fx_policy_appropriate_for_ay: bool
    wht_fx_policy_appropriate_for_ay: bool
    identity_supplied_from_fallback: frozenset[str]
    # Issue #36/#37: the Form 67 FTC, computed once and reused as the single
    # source of truth for the §90/§91 relief across FSI / TR / Form 67.
    form67_result: Form67ComputeResult


_reused_contexts: ContextVar[dict[tuple[object, ...], _ScheduleFsiComputeContext] | None] = (
    ContextVar("_reused_contexts", default=None)
)


class _FsiReuseScope:
    """Build the Schedule FSI context once per ``run-all`` (#609).

    Inside the ``with`` block, :func:`_compute_schedule_fsi_for_pipeline`
    reuses each successful context; a failed build is retried by the next
    step. Not a ``contextmanager`` generator: those turn this package's
    frozen exceptions into ``TypeError``.
    """

    def __enter__(self) -> None:
        self._token = _reused_contexts.set({})

    def __exit__(self, *_exc_info: object) -> None:
        _reused_contexts.reset(self._token)


def _compute_schedule_fsi_for_pipeline(
    *,
    input_path: Path,
    config_path: Path,
    financial_year: FinancialYear,
    allow_masked_account: bool = False,
    suppress_cross_ay_warning: bool = False,
    accept_anonymised_statement: bool = False,
    report_name: str = "Schedule FSI reporting",
) -> _ScheduleFsiComputeContext:
    """Return the shared Schedule FSI compute context for the IBKR pipelines.

    Parses an IBKR Activity Statement, then runs the broker-agnostic compute via
    :func:`~ibkr_fa.compute.income_engine.compute_income_schedules` and wraps
    the result with the IBKR metadata (account id, input format,
    identity-fallback) that the per-schedule
    pipelines (:func:`ibkr_fa.pipeline_fsi.run_schedule_fsi`,
    :func:`ibkr_fa.pipeline_tr.run_schedule_tr`, Form 67) surface in their
    reports. ``report_name`` identifies the calling schedule in period-validation
    error messages. Statements not from IBKR LLC are refused
    (:func:`_require_ibkr_llc`), and so is a statement with an unproven
    Interest row (:class:`~ibkr_fa.compute.schedule_os_interest.UnprovenInterestError`,
    #905) or whose ``Change in NAV`` contradicts its income sections
    (:class:`~ibkr_fa.parser.activity_statement.change_in_nav_income.ChangeInNavIncomeError`,
    #1279). Inside :class:`_FsiReuseScope`, a call
    whose arguments other than ``report_name`` match an earlier successful
    call returns that call's context.
    """
    reused = _reused_contexts.get()
    key = (
        input_path.absolute(),
        config_path.absolute(),
        financial_year,
        allow_masked_account,
        suppress_cross_ay_warning,
        accept_anonymised_statement,
    )
    if reused is not None and key in reused:
        return reused[key]

    ibkr_input = _load_ibkr_activity_input(
        input_path=input_path,
        config_path=config_path,
        period_validator=functools.partial(
            require_financial_year_contained_in_period,
            financial_year=financial_year,
            report_name=report_name,
        ),
        broker_validator=_require_ibkr_llc,
        required_sections=_FSI_DEFAULT_REQUIRED_ACTIVITY_SECTIONS,
        unsupported_format_message=lambda input_format: (
            f"Schedule FSI pipeline requires an IBKR Activity Statement "
            f"input; got {input_format.value!r}. Flex Query inputs are "
            f"unsupported because Schedule FSI depends on multiple "
            f"Activity Statement sections (Dividends, Trades, Interest, "
            f"Withholding Tax) that Flex doesn't currently expose in "
            f"the same shape (#12 / #104)."
        ),
        allow_masked_account=allow_masked_account,
        accept_anonymised_statement=accept_anonymised_statement,
        check_income_against_nav=True,
    )
    extract = ibkr_input.extract
    require_no_unproven_interest(extract.interest_non_credits)

    result = compute_income_schedules(
        ibkr_input.config,
        financial_year,
        fx_ctx=TaxFxContext.for_fy(
            financial_year,
            ibkr_input.config.fx,
            suppress_cross_ay_warning=suppress_cross_ay_warning,
        ),
        dividends=extract.dividends,
        withholding_taxes=extract.withholding_tax,
        closed_lots=extract.closed_lots,
        interest_credits=extract.interest_credits,
        # US: broker_validator=_require_ibkr_llc above refuses every statement
        # whose only BrokerName isn't Interactive Brokers LLC (#665).
        interest_payer_country_iso2="US",
    )

    context = _ScheduleFsiComputeContext(
        fsi_result=result.fsi_result,
        os_dividend_rows_consumed=result.os_dividend_rows_consumed,
        os_interest_rows_consumed=result.os_interest_rows_consumed,
        cg_stcg_rows_consumed=result.cg_stcg_rows_consumed,
        cg_ltcg_rows_consumed=result.cg_ltcg_rows_consumed,
        wht_rows_in_fy=result.wht_rows_in_fy,
        excluded_cg_indian_source_count=result.excluded_cg_indian_source_count,
        excluded_cg_out_of_fy_count=result.excluded_cg_out_of_fy_count,
        excluded_ltcg_pre_cutover_count=result.excluded_ltcg_pre_cutover_count,
        excluded_ltcg_indian_source_count=result.excluded_ltcg_indian_source_count,
        excluded_ltcg_out_of_fy_count=result.excluded_ltcg_out_of_fy_count,
        interest_credits_out_of_fy_count=result.interest_credits_out_of_fy_count,
        adr_routing_mode=result.adr_routing_mode,
        account_id=str(extract.account_info.account_id),
        account_id_cross_checked=ibkr_input.account_id_cross_checked,
        input_format=ibkr_input.input_format,
        fx_policy_appropriate_for_ay=result.fx_policy_appropriate_for_ay,
        wht_fx_policy_appropriate_for_ay=result.wht_fx_policy_appropriate_for_ay,
        identity_supplied_from_fallback=extract.identity_fallback_used,
        form67_result=result.form67_result,
    )
    if reused is not None:
        reused[key] = context
    return context
