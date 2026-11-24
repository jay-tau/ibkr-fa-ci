"""Decide which actual US withholding is eligible foreign tax.

The eligibility rules are those of
``docs/decisions/0004-fidelity-cma-classification.md`` section 6,
"Income classification and FTC eligibility".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.config import (
    FIDELITY_CMA_FTC_STATUSES,
    FidelityCmaRefundStatus,
    FidelityCmaUsTaxYearConfig,
)
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models import WithholdingTax


class FtcEligibilityError(IbkrFaError):
    """Configured final US tax cannot be matched to the actual withholding."""


@dataclass(frozen=True, slots=True, repr=False)
class UsWithholdingEligibility:
    """Final eligible tax rows, or the US tax years that block the window."""

    eligible: tuple[WithholdingTax, ...]
    blocked_tax_years: tuple[tuple[int, FidelityCmaRefundStatus], ...]


def eligible_us_withholding(
    actual: Iterable[WithholdingTax],
    *,
    window_start: date,
    window_end: date,
    us_tax_year: Callable[[int], FidelityCmaUsTaxYearConfig],
    reconciled_tax_years: frozenset[int],
) -> UsWithholdingEligibility:
    """Return the eligible US tax for the withholding dated in the window.

    ``actual`` is every statement's withholding, not only the window's. FTC
    is all-or-nothing for the window: unless every US tax year with
    in-window withholding is finalized or no-refund-claim, and reconciled,
    nothing is eligible and the blocking years are returned before the
    allocations are checked.
    """
    rows = tuple(actual)
    if any(row.currency != "USD" or row.amount >= 0 for row in rows):
        raise FtcEligibilityError("actual US withholding must be negative USD amounts")
    in_window = [row for row in rows if window_start <= row.payment_date <= window_end]
    statuses = {
        year: us_tax_year(year) for year in sorted({row.payment_date.year for row in in_window})
    }
    blocked = tuple(
        (year, entry.refund_status)
        for year, entry in statuses.items()
        if entry.refund_status not in FIDELITY_CMA_FTC_STATUSES or year not in reconciled_tax_years
    )
    if blocked:
        return UsWithholdingEligibility(eligible=(), blocked_tax_years=blocked)

    actual_by_year: dict[int, dict[date, Decimal]] = {}
    for row in rows:
        year_rows = actual_by_year.setdefault(row.payment_date.year, {})
        if row.payment_date in year_rows:
            raise FtcEligibilityError("duplicate withholding date in statement bundle")
        year_rows[row.payment_date] = row.amount.copy_abs()

    eligible_by_date: dict[date, Decimal] = {}
    for year, entry in statuses.items():
        configured = {item.payment_date: item.amount_usd for item in entry.eligible_tax}
        year_actual = actual_by_year[year]
        if set(configured) != set(year_actual):
            raise FtcEligibilityError(
                "eligible-tax dates must match every statement withholding date"
            )
        if any(configured[day] > year_actual[day] for day in year_actual):
            raise FtcEligibilityError("eligible tax cannot exceed actual statement withholding")
        eligible_by_date.update(configured)

    return UsWithholdingEligibility(
        eligible=tuple(
            WithholdingTax(
                symbol=row.symbol,
                isin=row.isin,
                payment_date=row.payment_date,
                amount=eligible_by_date[row.payment_date].copy_negate(),
                currency=row.currency,
                description=f"{row.symbol}({row.isin}) Final eligible US tax",
            )
            for row in in_window
            if eligible_by_date[row.payment_date] > 0
        ),
        blocked_tax_years=(),
    )
