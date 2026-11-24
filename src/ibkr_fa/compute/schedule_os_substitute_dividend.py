"""Compute residual-other-source treatment for evidenced substitute dividends."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import exact_inr_conversion
from ibkr_fa.compute.schedule_os_other_income import OTHER_INCOME_LABEL
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import SubstituteDividendIncome
from ibkr_fa.models.money import require_filed_amount
from ibkr_fa.types import CountryCode, Isin, Ticker


@dataclass(frozen=True, slots=True)
class ScheduleOsSubstituteDividendLine:
    """One in-FY substitute dividend converted under Rule 115(1), Explanation (2)(c)."""

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: CountryCode
    source_country_iso2: CountryCode
    payment_date: date
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    amount_gross_usd: Decimal
    amount_gross_inr: Decimal
    description: str

    def __post_init__(self) -> None:
        require(
            self.fx_rate_inr_per_usd > 0 and self.amount_gross_inr > 0,
            what="substitute-dividend FX values must be positive",
        )
        require_filed_amount(
            self.amount_gross_inr, what="ScheduleOsSubstituteDividendLine.amount_gross_inr"
        )
        require(
            self.fx_rate_publication_date <= self.fx_specified_date,
            what="substitute-dividend FX publication must not follow its specified date",
        )


@dataclass(frozen=True, slots=True)
class ScheduleOsSubstituteDividendComputeResult:
    """Schedule OS 1e substitute-dividend contribution."""

    lines: tuple[ScheduleOsSubstituteDividendLine, ...] = ()
    excluded_out_of_fy_count: int = 0


def compute_schedule_os_substitute_dividend_lines(
    incomes: Iterable[SubstituteDividendIncome],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> ScheduleOsSubstituteDividendComputeResult:
    """Convert in-FY substitute dividends at the residual-other-source FY-end rate."""

    require(
        financial_year_start <= financial_year_end,
        what="substitute-dividend financial year start must not follow its end",
    )
    materialised = tuple(incomes)
    require(
        all(item.currency == "USD" for item in materialised),
        what="substitute-dividend v1 supports USD only",
    )
    in_fy = sorted(
        (
            item
            for item in materialised
            if financial_year_start <= item.payment_date <= financial_year_end
        ),
        key=lambda item: (item.payment_date, item.symbol, item.source_row_index or -1),
    )
    lines: list[ScheduleOsSubstituteDividendLine] = []
    for income in in_fy:
        resolution = fx_policy.rate_for_audited(income.payment_date)
        rate = resolution.rate
        lines.append(
            ScheduleOsSubstituteDividendLine(
                symbol=income.symbol,
                isin=income.isin,
                issuer_country_iso2=income.issuer_country_iso2,
                source_country_iso2=income.source_country_iso2,
                payment_date=income.payment_date,
                fx_specified_date=resolution.specified_date,
                fx_rate_publication_date=resolution.rate_date,
                fx_rate_inr_per_usd=rate,
                amount_gross_usd=income.amount_gross,
                amount_gross_inr=exact_inr_conversion(
                    income.amount_gross,
                    rate,
                    label=OTHER_INCOME_LABEL,
                    source_row_index=income.source_row_index,
                ),
                description=income.description,
            )
        )
    return ScheduleOsSubstituteDividendComputeResult(
        lines=tuple(lines),
        excluded_out_of_fy_count=len(materialised) - len(in_fy),
    )
