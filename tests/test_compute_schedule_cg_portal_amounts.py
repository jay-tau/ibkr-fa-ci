"""Tests for :mod:`ibkr_fa.compute.schedule_cg_portal_amounts` (#615, #830, #1241)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from typing import Literal

import pytest

from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import (
    ScheduleCgPortalAmounts,
    require_schedule_cg_filed_amounts,
    schedule_cg_portal_amounts,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from tests._cg_exact_lines import exact_ltcg_line, exact_stcg_line
from tests._cg_lines import ltcg_line, stcg_line


def _whole_fields(amounts: ScheduleCgPortalAmounts) -> tuple[Decimal, ...]:
    return (
        amounts.consideration_inr,
        amounts.cost_of_acquisition_inr,
        amounts.balance_inr,
        amounts.gain_inr_rounded,
        amounts.filed_total_inr,
        amounts.component_rounding_difference_inr,
    )


def test_empty_lines_give_all_zeros() -> None:
    amounts = schedule_cg_portal_amounts([], schedule="STCG")

    assert amounts.gain_inr_full_precision == 0
    for value in _whole_fields(amounts):
        assert value == 0
        assert not value.is_signed()
        assert value.as_tuple().exponent == 0


def test_policies_agree() -> None:
    amounts = schedule_cg_portal_amounts(
        [stcg_line("1000.2", "400.1"), stcg_line("500.1", "100.1")],
        schedule="STCG",
    )

    assert amounts.consideration_inr == Decimal("1500")
    assert amounts.cost_of_acquisition_inr == Decimal("500")
    assert amounts.balance_inr == Decimal("1000")
    assert amounts.gain_inr_full_precision == Decimal("1000.1")
    assert amounts.gain_inr_rounded == Decimal("1000")
    assert amounts.filed_total_inr == Decimal("1000")
    assert amounts.component_rounding_difference_inr == 0


@pytest.mark.parametrize(
    ("proceeds", "cost", "balance", "gain_rounded", "difference"),
    [
        ("1000.4", "1200.6", "-201", "-200", "-1"),
        ("1000.6", "1200.4", "-199", "-200", "1"),
    ],
)
def test_policies_differ_by_one_rupee(
    proceeds: str, cost: str, balance: str, gain_rounded: str, difference: str
) -> None:
    amounts = schedule_cg_portal_amounts([ltcg_line(proceeds, cost)], schedule="LTCG")

    assert amounts.balance_inr == Decimal(balance)
    assert amounts.gain_inr_rounded == Decimal(gain_rounded)
    assert amounts.filed_total_inr == Decimal(balance)
    assert amounts.component_rounding_difference_inr == Decimal(difference)


def test_sign_flip_files_the_component_balance() -> None:
    amounts = schedule_cg_portal_amounts([stcg_line("100.4", "100.6")], schedule="STCG")

    assert amounts.filed_total_inr == Decimal("-1")
    assert amounts.gain_inr_rounded == 0
    assert not amounts.gain_inr_rounded.is_signed()


def test_rounds_sums_not_lines() -> None:
    amounts = schedule_cg_portal_amounts(
        [stcg_line("0.4", "0"), stcg_line("0.4", "0")],
        schedule="STCG",
    )

    assert amounts.consideration_inr == Decimal("1")


@pytest.mark.parametrize(("make_line", "schedule"), [(stcg_line, "STCG"), (ltcg_line, "LTCG")])
@pytest.mark.parametrize(
    ("proceeds", "cost"),
    [
        ("0.40", "0.60"),
        ("-0.60", "0"),
    ],
)
def test_refuses_figures_the_portal_rejects(
    make_line: Callable[[str, str], ScheduleCgStcgLine | ScheduleCgLtcgLine],
    schedule: Literal["STCG", "LTCG"],
    proceeds: str,
    cost: str,
) -> None:
    line = make_line(proceeds, cost)

    with pytest.raises(InvariantError) as excinfo:
        schedule_cg_portal_amounts([line], schedule=schedule)

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }


def test_zero_consideration_without_cost_is_accepted() -> None:
    amounts = schedule_cg_portal_amounts([stcg_line("0.40", "0.40")], schedule="STCG")

    for value in _whole_fields(amounts):
        assert value == 0
        assert not value.is_signed()


@pytest.mark.parametrize(
    "overrides",
    [
        {"consideration_inr": Decimal("1000.0")},
        {"balance_inr": Decimal("-200")},
        {"component_rounding_difference_inr": Decimal("0")},
        {"filed_total_inr": Decimal("-200")},
        {"gain_inr_rounded": Decimal("-202"), "component_rounding_difference_inr": Decimal("1")},
        {
            "gain_inr_full_precision": Decimal("-203"),
            "gain_inr_rounded": Decimal("-203"),
            "component_rounding_difference_inr": Decimal("2"),
        },
        {"gain_inr_rounded": Decimal("-0"), "balance_inr": Decimal("-0")},
    ],
)
def test_value_rejects_inconsistent_fields(overrides: dict[str, Decimal]) -> None:
    fields = {
        "consideration_inr": Decimal("1000"),
        "cost_of_acquisition_inr": Decimal("1201"),
        "balance_inr": Decimal("-201"),
        "gain_inr_full_precision": Decimal("-200.2"),
        "gain_inr_rounded": Decimal("-200"),
        "filed_total_inr": Decimal("-201"),
        "component_rounding_difference_inr": Decimal("-1"),
    }
    ScheduleCgPortalAmounts(**fields)
    fields.update(overrides)

    with pytest.raises(InvariantError):
        ScheduleCgPortalAmounts(**fields)


def test_results_expose_portal_amounts_on_access() -> None:
    stcg = ScheduleCgStcgComputeResult(
        stcg_lines=[stcg_line("0.40", "0.60")],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    ltcg = ScheduleCgLtcgComputeResult(
        ltcg_lines=[ltcg_line("0.40", "0.60")],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )

    for result in (stcg, ltcg):
        with pytest.raises(InvariantError):
            _ = result.portal_amounts

    stcg.stcg_lines[:] = [stcg_line("1000.4", "1200.6")]
    ltcg.ltcg_lines[:] = [ltcg_line("1000.4", "1200.6")]
    for result in (stcg, ltcg):
        assert result.portal_amounts.filed_total_inr == Decimal("-201")

    stcg.stcg_lines.clear()
    ltcg.ltcg_lines.clear()
    for result in (stcg, ltcg):
        assert result.portal_amounts.filed_total_inr == 0


# ---- filed totals and the CBDT 14-digit bound (#830) ------------------------

_SCHEDULES = [(stcg_line, "STCG"), (ltcg_line, "LTCG")]


@pytest.mark.parametrize(("make_line", "schedule"), _SCHEDULES)
@pytest.mark.parametrize(
    ("amounts", "total"),
    [
        ([("60000000000000", "0"), ("60000000000000", "0")], "consideration"),
        ([("1", "60000000000000"), ("1", "60000000000000")], "cost of acquisition"),
        ([("-60000000000000", "60000000000000")], "balance"),
    ],
)
def test_filed_total_beyond_the_cbdt_bound_refuses(
    make_line: Callable[[str, str], ScheduleCgStcgLine | ScheduleCgLtcgLine],
    schedule: Literal["STCG", "LTCG"],
    amounts: list[tuple[str, str]],
    total: str,
) -> None:
    lines = [make_line(proceeds, cost) for proceeds, cost in amounts]

    with pytest.raises(InvariantError) as excinfo:
        require_schedule_cg_filed_amounts(lines, schedule=schedule)

    assert excinfo.value.what.startswith(
        f"Schedule CG {schedule} {total} must round to a whole-rupee amount"
    )


@pytest.mark.parametrize(("make_line", "schedule"), _SCHEDULES)
def test_oversized_line_gain_is_review_detail(
    make_line: Callable[[str, str], ScheduleCgStcgLine | ScheduleCgLtcgLine],
    schedule: Literal["STCG", "LTCG"],
) -> None:
    lines = [make_line("-60000000000000", "60000000000000"), make_line("90000000000000", "0")]

    require_schedule_cg_filed_amounts(lines, schedule=schedule)

    amounts = schedule_cg_portal_amounts(lines, schedule=schedule)
    assert lines[0].realized_gain_inr == Decimal("-120000000000000")
    assert amounts.consideration_inr == Decimal("30000000000000")
    assert amounts.cost_of_acquisition_inr == Decimal("60000000000000")
    assert amounts.balance_inr == Decimal("-30000000000000")


@pytest.mark.parametrize(("make_line", "schedule"), _SCHEDULES)
@pytest.mark.parametrize(("proceeds", "cost"), [("0.40", "0.60"), ("-0.60", "0")])
def test_in_range_totals_leave_portal_eligibility_to_access(
    make_line: Callable[[str, str], ScheduleCgStcgLine | ScheduleCgLtcgLine],
    schedule: Literal["STCG", "LTCG"],
    proceeds: str,
    cost: str,
) -> None:
    lines = [make_line(proceeds, cost)]

    require_schedule_cg_filed_amounts(lines, schedule=schedule)

    with pytest.raises(InvariantError, match="rounded consideration is negative, or zero"):
        schedule_cg_portal_amounts(lines, schedule=schedule)


# ---- exact amounts (#1241) ---------------------------------------------------

_EXACT_LINES: dict[str, Callable[..., ScheduleCgStcgLine | ScheduleCgLtcgLine]] = {
    "STCG": exact_stcg_line,
    "LTCG": exact_ltcg_line,
}


def _two_rate_lines(schedule: str) -> list[ScheduleCgStcgLine | ScheduleCgLtcgLine]:
    """#1241's one-FY, two-rate repro: 3 shares bought for $10.00, 1 sold on 2025-02-15 for
    $3.00 at ₹84.25 and 2 on 2025-03-20 for $10.00 at ₹84.10. The exact costs, ₹842.50/3
    and ₹1682/3, add to ₹841.50."""
    line = _EXACT_LINES[schedule]
    return [
        line(cost_usd=Fraction(10, 3), proceeds_usd=3, rate="84.25", sold=date(2025, 2, 15)),
        line(
            cost_usd=Fraction(20, 3),
            proceeds_usd=10,
            rate="84.10",
            sold=date(2025, 3, 20),
            quantity="2",
        ),
    ]


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_exact_cost_on_a_half_rupee_files_its_half_up(schedule: Literal["STCG", "LTCG"]) -> None:
    """#1241: the exact cost ₹841.50 files ₹842, though the lines' Decimal costs add to
    ₹841.4999…; the audit TOTAL is the exact gain ₹252.25."""
    amounts = schedule_cg_portal_amounts(_two_rate_lines(schedule), schedule=schedule)

    assert [str(value) for value in _whole_fields(amounts)] == [
        "1094",
        "842",
        "252",
        "252",
        "252",
        "0",
    ]
    assert str(amounts.gain_inr_full_precision) == "252.25"


@pytest.mark.parametrize(("make_line", "schedule"), _SCHEDULES)
def test_exact_and_plain_rows_add_exactly(
    make_line: Callable[[str, str], ScheduleCgStcgLine | ScheduleCgLtcgLine],
    schedule: Literal["STCG", "LTCG"],
) -> None:
    """#1241: the first repro row's exact cost ₹842.50/3 plus a plain row's 31-digit cost is
    ₹841.50 and a fraction, so ₹842; their Decimals add to ₹841.4999…, so ₹841."""
    lines = [
        _two_rate_lines(schedule)[0],
        make_line("841", "560.6666666666666666666666666667"),
    ]

    amounts = schedule_cg_portal_amounts(lines, schedule=schedule)

    assert [str(value) for value in _whole_fields(amounts)] == [
        "1094",
        "842",
        "252",
        "252",
        "252",
        "0",
    ]
    # ₹252.25 less a third of 1E-28, cut at the 28-digit context.
    assert str(amounts.gain_inr_full_precision) == "252.2499999999999999999999999"


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
@pytest.mark.parametrize(
    "context",
    [
        Context(prec=3),
        Context(prec=3, traps=[]),
        Context(traps=[Inexact]),
        Context(traps=[Rounded]),
    ],
)
def test_exact_totals_keep_context_rounding_refusals(
    schedule: Literal["STCG", "LTCG"], context: Context
) -> None:
    line = _EXACT_LINES[schedule](
        cost_usd=Fraction(37033, 3),
        proceeds_usd=Fraction(37036, 3),
        rate="1",
        sold=date(2025, 2, 15),
    )
    with localcontext(context), pytest.raises(InvariantError) as raised:
        schedule_cg_portal_amounts([line], schedule=schedule)
    assert str(raised.value) == (
        "InvariantError: Schedule CG exact total: whole-rupee rounding must succeed "
        "within Decimal context"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is raised.value.__context__ is None


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_exact_total_checks_trailing_zeros_before_projection(
    schedule: Literal["STCG", "LTCG"],
) -> None:
    line = _EXACT_LINES[schedule](
        cost_usd=Fraction(37033, 3),
        proceeds_usd=Fraction(37036, 3),
        rate="3",
        sold=date(2025, 2, 15),
    )
    line = replace(line, proceeds_inr=Decimal("37036.0"))
    with localcontext(Context(traps=[Rounded])), pytest.raises(InvariantError) as raised:
        schedule_cg_portal_amounts([line], schedule=schedule)
    assert str(raised.value) == (
        "InvariantError: Schedule CG exact total: whole-rupee rounding must succeed "
        "within Decimal context"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is raised.value.__context__ is None


def test_rows_without_exact_amounts_keep_their_decimal_totals() -> None:
    """#1241: without exact rows each total is the Decimal sum, as before, trailing zero
    included; the exact path's division would give ``1000.1``."""
    amounts = schedule_cg_portal_amounts(
        [stcg_line("1000.20", "400.10"), stcg_line("500.10", "100.10")], schedule="STCG"
    )

    assert [str(value) for value in _whole_fields(amounts)] == [
        "1500",
        "500",
        "1000",
        "1000",
        "1000",
        "0",
    ]
    assert str(amounts.gain_inr_full_precision) == "1000.10"


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_bound_reads_the_exact_total(schedule: Literal["STCG", "LTCG"]) -> None:
    """#1241: three exact thirds of ₹99,999,999,999,999.50 total the bound and refuse, though
    their Decimal cuts add to ₹99,999,999,999,999.4999…; 1E-20 less files the largest amount."""
    third = Fraction(199999999999999, 6)

    def lines(proceeds: Fraction) -> list[ScheduleCgStcgLine | ScheduleCgLtcgLine]:
        line = _EXACT_LINES[schedule]
        return [
            line(cost_usd=0, proceeds_usd=proceeds, rate="1", sold=date(2025, 2, 15))
            for _ in range(3)
        ]

    with pytest.raises(InvariantError) as excinfo:
        require_schedule_cg_filed_amounts(lines(third), schedule=schedule)
    assert str(excinfo.value) == (
        f"InvariantError: Schedule CG {schedule} consideration must round to a whole-rupee "
        "amount within ±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)"
    )

    below = lines(third - Fraction(1, 3 * 10**20))
    require_schedule_cg_filed_amounts(below, schedule=schedule)
    assert schedule_cg_portal_amounts(below, schedule=schedule).consideration_inr == Decimal(
        "99999999999999"
    )
