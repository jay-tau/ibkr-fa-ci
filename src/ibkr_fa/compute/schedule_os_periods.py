"""The five accrual periods of Schedule OS section 10 and Schedule CG Table F.

Both tables ask for amounts in the same five date bands: up to 15 June, 16 June
to 15 September, 16 September to 15 December, 16 December to 15 March, and 16
March to 31 March. Schedule OS section 10 row 3a and Quicko's dividend screens
ask for the 1a(i) dividends by payment date, and Schedule CG Table F asks for
capital gains by transfer date (#1360). :func:`accrual_period_index` maps a
date to its band for both.

Each dividend period is floored to whole rupees and the leftover rupees of the
HALF_UP total go to the largest remainders, the lower period first on a tie,
so the periods always add up to the filed 1a(i) value.
:func:`split_whole_rupees` is that rule; Schedule FSI splits each filed
Schedule OS field across countries with it too (#1319), and Table F splits a
section's result across its date bands with it
(:mod:`ibkr_fa.compute.schedule_cg_table_f`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Final, Protocol

from ibkr_fa.compute.exact_inr import checked_subtraction, exact_inr_sum
from ibkr_fa.invariants import require

_PERIOD_COUNT: Final[int] = 5
_ONE_RUPEE: Final[Decimal] = Decimal("1")
_ZERO: Final[Decimal] = Decimal("0")
#: The fixed label of every Schedule OS dividend exactness refusal (#1136).
DIVIDEND_LABEL: Final[str] = "Schedule OS dividend"


class DividendAmount(Protocol):
    """The two fields of a Schedule OS dividend line the period split reads."""

    @property
    def payment_date(self) -> date: ...

    @property
    def amount_gross_inr(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class ScheduleOsPeriodAmount:
    """One section 10 period: its exact INR and its whole-rupee share."""

    exact_inr: Decimal
    whole_rupees: Decimal


def accrual_period_index(accrual_date: date) -> int:
    """Return the accrual period (0-4) of ``accrual_date``.

    The year is ignored: compute has already kept only in-FY lines.
    """
    month_day = (accrual_date.month, accrual_date.day)
    if month_day <= (3, 15) or month_day >= (12, 16):
        return 3
    if month_day <= (3, 31):
        return 4
    if month_day <= (6, 15):
        return 0
    if month_day <= (9, 15):
        return 1
    require(
        month_day <= (12, 15),
        what="An accrual date must fall in one of the five accrual periods",
        accrual_date=accrual_date,
    )
    return 2


def split_dividend_periods(
    lines: Iterable[DividendAmount],
) -> tuple[ScheduleOsPeriodAmount, ...]:
    """Return the five periods, whole rupees summing to the HALF_UP total."""
    amounts_by_period: list[list[Decimal]] = [[] for _ in range(_PERIOD_COUNT)]
    for line in lines:
        amounts_by_period[accrual_period_index(line.payment_date)].append(line.amount_gross_inr)
    exact = [exact_inr_sum(amounts, label=DIVIDEND_LABEL) for amounts in amounts_by_period]
    rounded_total = exact_inr_sum(exact, label=DIVIDEND_LABEL).quantize(
        _ONE_RUPEE, rounding=ROUND_HALF_UP
    )
    floors = split_whole_rupees(
        exact,
        target=rounded_total,
        what="Schedule OS period rounding allocation must fit the five periods",
    )
    require(
        sum(floors, _ZERO) == rounded_total,
        what="Schedule OS period whole rupees must sum to the rounded 1a(i) total",
        period_whole_rupees=floors,
        rounded_total=rounded_total,
    )
    return tuple(
        ScheduleOsPeriodAmount(exact_inr=exact_inr, whole_rupees=whole_rupees)
        for exact_inr, whole_rupees in zip(exact, floors, strict=True)
    )


def allocate_period_rupees(
    groups: Sequence[Iterable[DividendAmount]],
    *,
    period_targets: Sequence[Decimal],
) -> tuple[tuple[int, ...], ...]:
    """Split each period's whole rupees across ``groups``, keeping every period total.

    The caller chooses the grouping (by security, or by account and country).
    Each group gets its floored period amount, then the largest remainders
    take the rupees still needed to reach that period's target, the earlier
    group first on a tie.
    """
    require(
        len(period_targets) == _PERIOD_COUNT
        and all(
            target.is_finite() and target == target.to_integral_value() for target in period_targets
        ),
        what="Schedule OS period allocation needs five whole-rupee period targets",
        period_targets=period_targets,
    )
    exact_by_group = [
        [period.exact_inr for period in split_dividend_periods(lines)] for lines in groups
    ]
    whole_by_period = [
        split_whole_rupees(
            [periods[period_index] for periods in exact_by_group],
            target=target,
            what="Quicko period rounding allocation must fit the emitted dividend groups",
        )
        for period_index, target in enumerate(period_targets)
    ]
    return tuple(
        tuple(int(period[group_index]) for period in whole_by_period)
        for group_index in range(len(groups))
    )


def split_whole_rupees(
    amounts: Sequence[Decimal | Fraction], *, target: Decimal, what: str
) -> tuple[Decimal, ...]:
    """Split the whole-rupee ``target`` across the non-negative exact ``amounts``.

    Each amount is floored to whole rupees, then the rupees still needed to
    reach ``target`` go one each to the largest remainders, the earlier
    amount first on a tie. Remainders are exact
    (:func:`~ibkr_fa.compute.exact_inr.checked_subtraction`, #1312, or a
    :class:`~fractions.Fraction`'s own arithmetic, #1360), so remainders past
    the context's precision tie only when equal. The shares add up to
    ``target``, and each is its amount's floor or one rupee more. ``what`` is
    the refusal when ``target`` is below the floors' sum or more than one
    rupee per amount above it.
    """
    floors = [
        Decimal(math.floor(amount))
        if isinstance(amount, Fraction)
        else amount.quantize(_ONE_RUPEE, rounding=ROUND_FLOOR)
        for amount in amounts
    ]
    rupees_to_allocate = int(target - sum(floors, _ZERO))
    require(
        0 <= rupees_to_allocate <= len(amounts),
        what=what,
        rupees_to_allocate=rupees_to_allocate,
    )
    remainders = [
        amount - Fraction(floor)
        if isinstance(amount, Fraction)
        else checked_subtraction(
            amount, floor, at=f"Whole-rupee split, amount {position}, field remainder"
        )
        for position, (amount, floor) in enumerate(zip(amounts, floors, strict=True), start=1)
    ]
    largest_remainders = sorted(
        range(len(amounts)), key=lambda index: (remainders[index], -index), reverse=True
    )
    for index in largest_remainders[:rupees_to_allocate]:
        floors[index] += _ONE_RUPEE
    return tuple(floors)
