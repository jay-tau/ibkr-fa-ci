"""Whole-rupee Schedule CG portal amounts for one schedule (#615).

The ITR form computes a residual Schedule CG row from its components:
Balance is the full value of consideration less the section 48
deductions, and the gain row follows from the Balance (ITR-2 A5c/A5e
and B8c/B8e, ITR-3 A6c/A6g and B9c/B9e). The utility takes
consideration and cost only as whole rupees, and CBDT's Category A
validation rules refuse a return whose Balance or gain differs from
that arithmetic. So the filed total is the rounded consideration less
the rounded cost, never the separately rounded gain (owner decision 1
on #615). ``docs/portal/schedule-cg.md`` quotes the sources.

Where a line carries exact amounts (#1241), each total adds the exact
rationals, its whole rupees are their exact HALF_UP
(:func:`~ibkr_fa.compute.exact_inr.exact_whole_rupees`), and its unrounded
Decimal is :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of the sum.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from fractions import Fraction
from typing import Literal

from ibkr_fa.compute.exact_inr import checked_exact_sum, exact_decimal, exact_whole_rupees
from ibkr_fa.invariants import require
from ibkr_fa.models.money import require_filed_amount, to_whole_rupees
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine, exact_amount


@dataclass(frozen=True, slots=True)
class _Total:
    """One column's total: unrounded, and its exact rational where a row has one (#1241)."""

    decimal: Decimal
    exact: Fraction | None

    @property
    def amount(self) -> Decimal:
        """The review amount, projected from the rational where one is present."""
        return self.decimal if self.exact is None else exact_decimal(self.exact)

    @property
    def whole_rupees(self) -> Decimal:
        """The total HALF_UP to whole rupees: exactly, where it is a rational."""
        if self.exact is None:
            return to_whole_rupees(self.decimal)
        # Keep the Decimal sum's context refusals, including trailing zeros.
        rounded = None
        with suppress(DecimalException):
            rounded = to_whole_rupees(self.decimal)
        require(
            rounded is not None and rounded.is_finite(),
            what=(
                "Schedule CG exact total: whole-rupee rounding must succeed within Decimal context"
            ),
        )
        return exact_whole_rupees(self.exact)


def _column_sum(
    rows: tuple[ScheduleCgStcgLine | ScheduleCgLtcgLine, ...],
    column: Literal["proceeds_inr", "cost_basis_inr", "realized_gain_inr"],
    schedule: Literal["STCG", "LTCG"],
) -> _Total:
    """Sum one INR column of ``rows`` exactly, never rounding (#1166, #1241).

    Where any row has exact amounts, the total also adds the rows' exact
    rationals, and its unrounded amount is
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of that sum. A refusal
    names the 1-based row whose addition failed; see
    :func:`~ibkr_fa.compute.exact_inr.checked_exact_sum`.
    """
    total, exact = checked_exact_sum(
        (
            (position, getattr(row, column), exact_amount(row, column))
            for position, row in enumerate(rows, start=1)
        ),
        at=lambda position: f"Schedule CG {schedule}, input row {position}, column {column}",
        exact=any(row.exact_amounts is not None for row in rows),
    )
    return _Total(total, exact)


@dataclass(frozen=True, slots=True)
class ScheduleCgPortalAmounts:
    """One schedule's whole-rupee portal figures.

    Attributes:
        consideration_inr: Sum of ``proceeds_inr``, HALF_UP to whole rupees.
            Where a line has exact amounts, the sum of the exact rationals
            (#1241), as for every total here.
        cost_of_acquisition_inr: Sum of ``cost_basis_inr``, HALF_UP to whole
            rupees.
        balance_inr: ``consideration_inr - cost_of_acquisition_inr``, as the
            form computes Balance.
        gain_inr_full_precision: Sum of ``realized_gain_inr``, unrounded, or
            :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of the exact sum.
            The audit TOTAL row.
        gain_inr_rounded: ``gain_inr_full_precision``, HALF_UP to whole rupees.
        filed_total_inr: The filed gain: ``balance_inr``.
        component_rounding_difference_inr: ``balance_inr - gain_inr_rounded``:
            -1, 0 or 1.
    """

    consideration_inr: Decimal
    cost_of_acquisition_inr: Decimal
    balance_inr: Decimal
    gain_inr_full_precision: Decimal
    gain_inr_rounded: Decimal
    filed_total_inr: Decimal
    component_rounding_difference_inr: Decimal

    def __post_init__(self) -> None:
        whole_rupee_fields = {
            "consideration_inr": self.consideration_inr,
            "cost_of_acquisition_inr": self.cost_of_acquisition_inr,
            "balance_inr": self.balance_inr,
            "gain_inr_rounded": self.gain_inr_rounded,
            "filed_total_inr": self.filed_total_inr,
            "component_rounding_difference_inr": self.component_rounding_difference_inr,
        }
        for name, value in whole_rupee_fields.items():
            require(
                value.as_tuple().exponent == 0 and not (value == 0 and value.is_signed()),
                what="ScheduleCgPortalAmounts whole-rupee fields need exponent 0 and no -0",
                field=name,
                value=value,
            )
        require(
            self.balance_inr == self.consideration_inr - self.cost_of_acquisition_inr,
            what="ScheduleCgPortalAmounts.balance_inr must be consideration less cost",
            balance_inr=self.balance_inr,
        )
        require(
            self.gain_inr_rounded == to_whole_rupees(self.gain_inr_full_precision),
            what="ScheduleCgPortalAmounts.gain_inr_rounded must round the full-precision gain",
            gain_inr_rounded=self.gain_inr_rounded,
        )
        require(
            self.component_rounding_difference_inr == self.balance_inr - self.gain_inr_rounded
            and abs(self.component_rounding_difference_inr) <= 1,
            what=(
                "ScheduleCgPortalAmounts.component_rounding_difference_inr must be "
                "balance less rounded gain, at most 1 either way"
            ),
            component_rounding_difference_inr=self.component_rounding_difference_inr,
        )
        require(
            self.filed_total_inr == self.balance_inr,
            what="ScheduleCgPortalAmounts.filed_total_inr must be the component balance",
            filed_total_inr=self.filed_total_inr,
        )


def require_schedule_cg_filed_amounts(
    lines: Iterable[ScheduleCgStcgLine | ScheduleCgLtcgLine],
    *,
    schedule: Literal["STCG", "LTCG"],
) -> None:
    """Refuse a consideration, cost or balance outside the CBDT 14-digit bound (#830).

    Each source calls this once its lines are final, before any writer reads
    :func:`schedule_cg_portal_amounts`. It checks magnitude only; that
    function's eligibility check stays where it is. A line's own proceeds,
    cost and gain are review detail, so only the filed totals are bounded.

    Raises:
        InvariantError: If a consideration or cost sum can't be added
            exactly (#1166), or any of the three totals rounds outside
            ±99,999,999,999,999.
    """
    rows = tuple(lines)
    consideration = _column_sum(rows, "proceeds_inr", schedule)
    cost = _column_sum(rows, "cost_basis_inr", schedule)
    # Bound the unrounded sums first: rounding one past the decimal context's
    # precision would raise before the check could refuse it.
    require_filed_amount(consideration.amount, what=f"Schedule CG {schedule} consideration")
    require_filed_amount(cost.amount, what=f"Schedule CG {schedule} cost of acquisition")
    require_filed_amount(
        consideration.whole_rupees - cost.whole_rupees,
        what=f"Schedule CG {schedule} balance",
    )


def schedule_cg_portal_amounts(
    lines: Iterable[ScheduleCgStcgLine | ScheduleCgLtcgLine],
    *,
    schedule: Literal["STCG", "LTCG"],
) -> ScheduleCgPortalAmounts:
    """Round one schedule's consideration and cost, and file their difference.

    ``schedule`` only names the schedule in the refusals below.

    Raises:
        InvariantError: If a consideration, cost or gain sum can't be added
            exactly (#1166). Or if the rounded consideration is negative, or
            is zero while the rounded cost is not. The schema and utility
            refuse negative consideration, and ITR-2 r.104/108 and ITR-3
            r.361/365 refuse deductions against zero consideration (owner
            decision 2 on #615). This can refuse one source's figures that
            another source's consideration would make valid in the same row.
    """
    rows = tuple(lines)
    consideration = _column_sum(rows, "proceeds_inr", schedule).whole_rupees
    cost = _column_sum(rows, "cost_basis_inr", schedule).whole_rupees
    require(
        consideration > 0 or cost == consideration == 0,
        what=(
            "Schedule CG rounded consideration is negative, or zero against a "
            "claimed cost; the ITR refuses that row"
        ),
        schedule=schedule,
        consideration_inr=consideration,
        cost_of_acquisition_inr=cost,
    )
    gain = _column_sum(rows, "realized_gain_inr", schedule)
    balance = consideration - cost
    gain_rounded = gain.whole_rupees
    return ScheduleCgPortalAmounts(
        consideration_inr=consideration,
        cost_of_acquisition_inr=cost,
        balance_inr=balance,
        gain_inr_full_precision=gain.amount,
        gain_inr_rounded=gain_rounded,
        filed_total_inr=balance,
        component_rounding_difference_inr=balance - gain_rounded,
    )
