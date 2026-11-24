"""Broker-agnostic Schedule OS / FSI / Form 67 compute.

Every source family (IBKR, NetBenefits, StockPlan, Fidelity CMA, Robinhood)
feeds its parsed income into :func:`compute_income_schedules`, with the
:class:`~ibkr_fa.fx.context.TaxFxContext` that supplies each head's FX policy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal

from ibkr_fa.compute.exact_inr import exact_signed_inr_sum
from ibkr_fa.compute.foreign_tax import net_foreign_tax_by_country, require_no_orphan_wht
from ibkr_fa.compute.form67 import Form67ComputeResult, compute_form67_entries_from_wht
from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.compute.schedule_fsi import (
    InterestPayerCountryError,
    ScheduleFsiComputeResult,
    compute_schedule_fsi,
)
from ibkr_fa.compute.schedule_fsi_relief import (
    enrich_fsi_with_form67_relief,
    require_form67_credit_reconciles,
    stamp_taxpayer_ids,
)
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    compute_schedule_os_dividend_lines,
)
from ibkr_fa.compute.schedule_os_interest import (
    ScheduleOsInterestComputeResult,
    compute_schedule_os_interest_lines,
)
from ibkr_fa.compute.schedule_os_other_income import (
    ScheduleOsOtherIncomeComputeResult,
    ScheduleOsOtherIncomeLine,
    compute_schedule_os_other_income_lines,
)
from ibkr_fa.compute.schedule_os_substitute_dividend import (
    ScheduleOsSubstituteDividendComputeResult,
    ScheduleOsSubstituteDividendLine,
    compute_schedule_os_substitute_dividend_lines,
)
from ibkr_fa.config import (
    ScheduleOsIndianAdrRoutingMode,
    UserConfig,
)
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.invariants import require
from ibkr_fa.models import (
    ClosedLot,
    Dividend,
    InterestCredit,
    OtherSourceIncome,
    SubstituteDividendIncome,
)
from ibkr_fa.models.money import require_filed_amount
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.models.withholding_tax import WithholdingTax

__all__ = [
    "IncomeComputeResult",
    "compute_income_schedules",
    "compute_schedule_os_result",
]


@dataclass(frozen=True, slots=True)
class IncomeComputeResult:
    """Shared Schedule FSI / OS / Form 67 compute output."""

    fsi_result: ScheduleFsiComputeResult
    os_result: ScheduleOsComputeResult
    form67_result: Form67ComputeResult
    os_dividend_rows_consumed: int
    os_interest_rows_consumed: int
    os_substitute_dividend_rows_consumed: int
    cg_stcg_rows_consumed: int
    cg_ltcg_rows_consumed: int
    wht_rows_in_fy: int
    excluded_cg_indian_source_count: int
    excluded_cg_out_of_fy_count: int
    excluded_ltcg_pre_cutover_count: int
    excluded_ltcg_indian_source_count: int
    excluded_ltcg_out_of_fy_count: int
    interest_credits_out_of_fy_count: int
    substitute_dividends_out_of_fy_count: int
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode
    fx_policy_appropriate_for_ay: bool
    wht_fx_policy_appropriate_for_ay: bool
    os_substitute_dividend_lines: tuple[ScheduleOsSubstituteDividendLine, ...]
    os_other_income_lines: tuple[ScheduleOsOtherIncomeLine, ...]


def compute_schedule_os_result(
    config: UserConfig,
    financial_year: FinancialYear,
    *,
    fx_ctx: TaxFxContext,
    dividends: Iterable[Dividend],
    interest_credits: Iterable[InterestCredit],
    substitute_dividend_incomes: Iterable[SubstituteDividendIncome] = (),
    other_source_incomes: Iterable[OtherSourceIncome] = (),
) -> tuple[
    ScheduleOsComputeResult,
    ScheduleOsInterestComputeResult,
    ScheduleOsSubstituteDividendComputeResult,
    ScheduleOsOtherIncomeComputeResult,
]:
    """Build Schedule OS independently from FSI/Form 67 completeness."""

    _require_fx_ctx_for(fx_ctx, financial_year)
    os_result = compute_schedule_os_dividend_lines(
        dividends,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.dividend,
        adr_routing_mode=config.os.schedule_os_indian_adr_routing,
    )
    interest_result = compute_schedule_os_interest_lines(
        interest_credits,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.other_sources,
    )
    substitute_result = compute_schedule_os_substitute_dividend_lines(
        substitute_dividend_incomes,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.other_sources,
    )
    other_result = compute_schedule_os_other_income_lines(
        other_source_incomes,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.other_sources,
    )
    substitute_as_other = tuple(
        ScheduleOsOtherIncomeLine(
            nature="Substitute payment in lieu of dividend",
            payment_date=line.payment_date,
            fx_specified_date=line.fx_specified_date,
            fx_rate_publication_date=line.fx_rate_publication_date,
            fx_rate_inr_per_usd=line.fx_rate_inr_per_usd,
            amount_gross_usd=line.amount_gross_usd,
            amount_gross_inr=line.amount_gross_inr,
            description=line.description,
            source_row_index=None,
            source_country_iso2=line.source_country_iso2,
        )
        for line in substitute_result.lines
    )
    os_result = replace(
        os_result,
        interest_total_inr_rounded=interest_result.total_inr_rounded,
        other_income_lines=(*substitute_as_other, *other_result.lines),
    )
    _require_os_filed_totals(os_result, interest_result.total_inr_rounded)
    return os_result, interest_result, substitute_result, other_result


def _require_os_filed_totals(result: ScheduleOsComputeResult, interest_total: Decimal) -> None:
    """Refuse a filed Schedule OS total past the CBDT bound before any writer runs (#830).

    The 1a(i) total, its section 10 periods and the 1e totals are otherwise
    first computed when a writer reads them.
    """
    require_filed_amount(
        result.ordinary_dividend_total_inr_rounded, what="Schedule OS 1a(i) dividend total"
    )
    for period, amount in enumerate(result.dividend_periods, start=1):
        require_filed_amount(
            amount.whole_rupees, what="Schedule OS section 10 dividend period", period=period
        )
    require_filed_amount(interest_total, what="Schedule OS 1b(ix) interest total")
    for nature, _exact, whole_rupees in result.other_income_totals:
        require_filed_amount(whole_rupees, what="Schedule OS 1e other-income total", nature=nature)


def _fsi_relief_other_income_by_country(
    substitute_lines: Iterable[ScheduleOsSubstituteDividendLine],
    other_lines: Iterable[ScheduleOsOtherIncomeLine],
) -> dict[str, Decimal]:
    """Return the exact 1e income per source country for FSI relief.

    Each country's income is an exact signed sum of its substitute-dividend
    and other-income lines (#1136).
    """
    amounts_by_iso: dict[str, list[Decimal]] = {}
    for line in substitute_lines:
        amounts_by_iso.setdefault(line.source_country_iso2, []).append(line.amount_gross_inr)
    for line in other_lines:
        require(
            line.source_country_iso2 is not None,
            what="FSI other income requires a reviewed source country",
        )
        source_country = line.source_country_iso2
        assert source_country is not None
        amounts_by_iso.setdefault(source_country, []).append(line.amount_gross_inr)
    return {
        iso: exact_signed_inr_sum(amounts, label="Schedule FSI relief other income by country")
        for iso, amounts in amounts_by_iso.items()
    }


def _require_fx_ctx_for(fx_ctx: TaxFxContext, financial_year: FinancialYear) -> None:
    require(
        fx_ctx.financial_year == financial_year,
        what="income engine: fx_ctx must be built for the financial year being computed",
        fx_ctx_financial_year=fx_ctx.financial_year,
        financial_year=financial_year,
    )


def _require_us_interest_payer(
    interest_payer_country_iso2: object,
    interest_credits: list[InterestCredit],
) -> None:
    """Refuse interest whose source did not state a US payer (#698)."""
    if interest_payer_country_iso2 is None:
        if interest_credits:
            raise InterestPayerCountryError(
                "interest credits were supplied, but the source did not state their payer country"
            )
        return
    if not (isinstance(interest_payer_country_iso2, str) and interest_payer_country_iso2 == "US"):
        raise InterestPayerCountryError(
            f"interest payer country {interest_payer_country_iso2!r} is not supported; "
            "only US interest payers are supported"
        )


def compute_income_schedules(
    config: UserConfig,
    financial_year: FinancialYear,
    *,
    fx_ctx: TaxFxContext,
    dividends: list[Dividend],
    withholding_taxes: list[WithholdingTax],
    closed_lots: list[ClosedLot],
    interest_credits: list[InterestCredit],
    interest_payer_country_iso2: str | None,
    substitute_dividend_incomes: Iterable[SubstituteDividendIncome] = (),
    other_source_incomes: Iterable[OtherSourceIncome] = (),
    cg_stcg_result: ScheduleCgStcgComputeResult | None = None,
    cg_ltcg_result: ScheduleCgLtcgComputeResult | None = None,
    additional_fsi_results: Iterable[ScheduleFsiComputeResult] = (),
) -> IncomeComputeResult:
    """Run the broker-agnostic Schedule OS / FSI / Form 67 compute.

    Takes already-parsed ``dividends`` / ``withholding_taxes`` / ``closed_lots``
    / ``interest_credits`` from any source adapter and runs the shared compute:
    per-aggregator OS / interest / CG runs, the per-ISIN orphan-WHT guard,
    ``compute_schedule_fsi``, the Form 67 FTC, and relief-enrichment + TIN-stamp.
    Source adapters with statutory basis rules may provide precomputed CG
    results instead of ``closed_lots``.
    Returns an :class:`IncomeComputeResult` with no broker-specific metadata;
    each caller wraps it in its own report/context type.

    ``interest_payer_country_iso2`` is the caller's statement of who paid every
    credit in ``interest_credits`` (#698). ``"US"`` declares that every payer is
    a US entity, and Schedule FSI files the interest under US. ``None`` says the
    source states no payer and is allowed only when ``interest_credits`` is
    empty. Every other value, of any type, is refused, with or without
    interest: only US interest payers are supported. The check runs on the
    whole ``interest_credits`` list, out-of-FY credits included, before any
    compute, and raises :class:`InterestPayerCountryError`.
    """
    _require_us_interest_payer(interest_payer_country_iso2, interest_credits)
    _require_fx_ctx_for(fx_ctx, financial_year)
    fx_policy_appropriate_for_ay = fx_ctx.fx_policy_appropriate_for_ay
    # Income and WHT policies currently share the AY-verification boundary.
    wht_fx_policy_appropriate_for_ay = fx_policy_appropriate_for_ay

    # Multi-year Activity Statements (needed for FIFO / LTCG matching)
    # commonly contain out-of-FY WHT rows; the run report counts only
    # the in-FY ones.
    in_fy_withholding_taxes = [
        w
        for w in withholding_taxes
        if financial_year.start_date <= w.payment_date <= financial_year.end_date
    ]

    wht_fx_policy = fx_ctx.wht

    (
        os_result,
        interest_result,
        substitute_dividend_result,
        other_income_result,
    ) = compute_schedule_os_result(
        config,
        financial_year,
        fx_ctx=fx_ctx,
        dividends=dividends,
        interest_credits=interest_credits,
        substitute_dividend_incomes=substitute_dividend_incomes,
        other_source_incomes=other_source_incomes,
    )
    if (cg_stcg_result is None) != (cg_ltcg_result is None):
        raise ValueError("precomputed STCG and LTCG results must be supplied together")
    if cg_stcg_result is not None:
        if closed_lots:
            raise ValueError("closed_lots must be empty when precomputed CG results are supplied")
    else:
        cg_stcg_result = compute_schedule_cg_stcg_lines(
            closed_lots,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            fx_policy=fx_ctx.capital_gains,
        )
        cg_ltcg_result = compute_schedule_cg_ltcg_lines(
            closed_lots,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            fx_policy=fx_ctx.capital_gains,
        )
    assert cg_ltcg_result is not None

    # Per-ISIN orphan-WHT guard: the FSI WHT aggregator layers tax onto
    # (country, OTHER_SOURCES), so paid WHT without its dividend must refuse.
    require_no_orphan_wht(
        net_foreign_tax_by_country(
            withholding_taxes,
            fx_policy=wht_fx_policy,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            adr_routing_mode=config.os.schedule_os_indian_adr_routing,
        ),
        os_result.lines,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
    )

    fsi_result_raw = compute_schedule_fsi(
        os_result=os_result,
        cg_stcg_result=cg_stcg_result,
        cg_ltcg_result=cg_ltcg_result,
        interest_result=interest_result,
        substitute_dividend_result=substitute_dividend_result,
        other_income_result=other_income_result,
        broker_domicile_iso2=interest_payer_country_iso2,
        withholding_tax=withholding_taxes,
        wht_fx_policy=wht_fx_policy,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        adr_routing_mode=config.os.schedule_os_indian_adr_routing,
        additional_income_results=additional_fsi_results,
    )
    # Issue #36/#37: compute the Form 67 FTC ONCE here and reuse it as the
    # single source of truth for the §90/§91 relief shown across FSI, TR, and
    # Form 67 (so the three schedules never disagree). Form 67 remains the
    # producer; FSI rows inherit the matching dividend credit
    # (enrich_fsi_with_form67_relief) and Schedule TR aggregates it per country.
    form67_result = compute_form67_entries_from_wht(
        withholding_tax=tuple(withholding_taxes),
        os_dividend_lines=tuple(os_result.lines),
        wht_fx_policy=wht_fx_policy,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        form67_config=config.form67,
        adr_routing_mode=config.os.schedule_os_indian_adr_routing,
    )
    other_income_inr_by_iso = _fsi_relief_other_income_by_country(
        substitute_dividend_result.lines, other_income_result.lines
    )
    fsi_result = stamp_taxpayer_ids(
        enrich_fsi_with_form67_relief(
            fsi_result_raw,
            form67_result,
            interest_income_inr_by_iso=(
                {interest_payer_country_iso2: interest_result.total_inr_full}
                if interest_payer_country_iso2 is not None and interest_result.lines
                else {}
            ),
            other_income_inr_by_iso=other_income_inr_by_iso,
            form67_config=config.form67,
        ),
        config.account.foreign_tins,
    )
    require_form67_credit_reconciles(fsi_result, form67_result)

    return IncomeComputeResult(
        fsi_result=fsi_result,
        os_result=os_result,
        form67_result=form67_result,
        os_dividend_rows_consumed=len(os_result.lines),
        os_interest_rows_consumed=len(interest_result.lines),
        os_substitute_dividend_rows_consumed=len(substitute_dividend_result.lines),
        cg_stcg_rows_consumed=len(cg_stcg_result.stcg_lines),
        cg_ltcg_rows_consumed=len(cg_ltcg_result.ltcg_lines),
        wht_rows_in_fy=len(in_fy_withholding_taxes),
        excluded_cg_indian_source_count=len(cg_stcg_result.excluded_indian_source),
        excluded_cg_out_of_fy_count=len(cg_stcg_result.excluded_out_of_fy),
        excluded_ltcg_pre_cutover_count=len(cg_ltcg_result.excluded_pre_2024_07_23),
        excluded_ltcg_indian_source_count=len(cg_ltcg_result.excluded_indian_source),
        excluded_ltcg_out_of_fy_count=len(cg_ltcg_result.excluded_out_of_fy),
        interest_credits_out_of_fy_count=interest_result.excluded_out_of_fy_count,
        substitute_dividends_out_of_fy_count=(substitute_dividend_result.excluded_out_of_fy_count),
        adr_routing_mode=config.os.schedule_os_indian_adr_routing,
        fx_policy_appropriate_for_ay=fx_policy_appropriate_for_ay,
        wht_fx_policy_appropriate_for_ay=wht_fx_policy_appropriate_for_ay,
        os_substitute_dividend_lines=substitute_dividend_result.lines,
        os_other_income_lines=other_income_result.lines,
    )
