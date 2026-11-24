"""Compute Schedule OS row 1e residual other-income lines."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from ibkr_fa.compute.exact_inr import exact_inr_conversion, exact_signed_inr_sum
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models.money import require_filed_amount
from ibkr_fa.models.other_source_income import OtherSourceIncome
from ibkr_fa.types import CountryCode

#: The fixed label of every 1e other-income exactness refusal, substitute
#: dividends included (#1136).
OTHER_INCOME_LABEL: Final[str] = "Schedule OS 1e other income"


@dataclass(frozen=True, slots=True)
class ScheduleOsOtherIncomeLine:
    """One in-FY residual other-income receipt converted under Rule 115."""

    nature: str
    payment_date: date
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    amount_gross_usd: Decimal
    amount_gross_inr: Decimal
    description: str
    source_row_index: int | None
    source_country_iso2: CountryCode | None = None

    def __post_init__(self) -> None:
        # Not a filed cell by itself, but every filed 1e total adds it up (#830).
        require_filed_amount(
            self.amount_gross_inr, what="ScheduleOsOtherIncomeLine.amount_gross_inr"
        )


@dataclass(frozen=True, slots=True)
class ScheduleOsOtherIncomeComputeResult:
    """Schedule OS 1e contribution: one exact audit line per in-FY receipt."""

    lines: tuple[ScheduleOsOtherIncomeLine, ...] = ()


def compute_schedule_os_other_income_lines(
    incomes: Iterable[OtherSourceIncome],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> ScheduleOsOtherIncomeComputeResult:
    """Convert in-FY row-1e income using the FY-end Rule 115 rate."""

    materialised = tuple(incomes)
    require(
        all(item.currency == "USD" for item in materialised),
        what="Schedule OS other-income v1 supports USD only",
    )
    in_fy = sorted(
        (
            item
            for item in materialised
            if financial_year_start <= item.payment_date <= financial_year_end
        ),
        key=lambda item: (item.payment_date, item.source_row_index or -1),
    )
    lines: list[ScheduleOsOtherIncomeLine] = []
    for income in in_fy:
        resolution = fx_policy.rate_for_audited(income.payment_date)
        rate = resolution.rate
        lines.append(
            ScheduleOsOtherIncomeLine(
                nature=income.nature,
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
                source_row_index=income.source_row_index,
                source_country_iso2=income.source_country_iso2,
            )
        )
    return ScheduleOsOtherIncomeComputeResult(lines=tuple(lines))


def grouped_other_income_totals(
    lines: Iterable[ScheduleOsOtherIncomeLine],
) -> tuple[tuple[str, Decimal, Decimal], ...]:
    """Return nature, exact INR, and whole-rupee totals for filing.

    Each nature's total is an exact signed sum: 1e lines may be negative.
    """

    amounts_by_nature: dict[str, list[Decimal]] = {}
    for line in lines:
        amounts_by_nature.setdefault(line.nature, []).append(line.amount_gross_inr)
    totals = {
        nature: exact_signed_inr_sum(amounts, label=OTHER_INCOME_LABEL)
        for nature, amounts in amounts_by_nature.items()
    }
    return tuple(
        (
            nature,
            exact,
            exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        )
        for nature, exact in sorted(totals.items())
    )
