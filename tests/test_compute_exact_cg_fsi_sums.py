"""Exact Schedule CG portal totals and FSI capital-gains nets (#1166), with
exact rational shares (#1241).

Owner decision (2026-11-10): these sums never round. A sale split across lots
by quantity can carry 28 to 60 significant digits per lot, so the 28-digit
context dropped digits from real totals; the additions now widen to
at least ``EXACT_SUM_DIGITS`` and refuse only a nonfinite amount, a decimal
arithmetic failure or an addition that would still round at that precision.

All values are synthetic. ``_SIX`` (``_LONG`` then five ``_TINY``) totals
exactly 1.50, so ₹2; the 28-digit context keeps ``_LONG`` and files ₹1.
``_HUGE`` plus ``_MINUTE`` needs more than ``EXACT_SUM_DIGITS`` digits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, getcontext, localcontext
from fractions import Fraction
from typing import Any, Literal

import pytest

from ibkr_fa.compute.exact_inr import (
    EXACT_SUM_DIGITS,
    checked_addition,
    checked_exact_sum,
    checked_subtraction,
    checked_sum,
)
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import (
    require_schedule_cg_filed_amounts,
    schedule_cg_portal_amounts,
)
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    _merge_results,
    compute_schedule_fsi_from_capital_gains,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from ibkr_fa.types import Isin
from tests._cg_exact_lines import exact_ltcg_line, exact_stcg_line
from tests._cg_lines import MARKED_AMOUNT, MARKERS, ltcg_line, marked, stcg_line
from tests._crash_reports import render_without_frames
from tests._exact_refusals import INEXACT_SUM as _EXACT

_LONG = "1.499999999999999999999999998"
_TINY = "4E-28"
_SIX = (_LONG, *(_TINY,) * 5)
_HUGE = "1E+10"
_MINUTE = "1E-995"

type Schedule = Literal["STCG", "LTCG"]


def _refusal(
    raised: pytest.ExceptionInfo[InvariantError], expected: str, *, markers: tuple[str, ...] = ()
) -> None:
    """The full message, no context values, no chained arithmetic error and no marker."""
    error = raised.value
    assert str(error) == f"InvariantError: {expected}: {_EXACT}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    for marker in markers:
        assert marker not in rendered, marker


def _stcg(*pairs: tuple[str, str]) -> list[ScheduleCgStcgLine]:
    return [stcg_line(proceeds, cost) for proceeds, cost in pairs]


def _ltcg(*pairs: tuple[str, str]) -> list[ScheduleCgLtcgLine]:
    return [ltcg_line(proceeds, cost) for proceeds, cost in pairs]


def _in[LineT: (ScheduleCgStcgLine, ScheduleCgLtcgLine)](line: LineT, country: str) -> LineT:
    """``line`` issued in ``country``, under a private-use ISIN there."""
    return replace(line, issuer_country_iso2=country, isin=Isin(f"{country}99970J1065"))


def _lines(schedule: Schedule, *pairs: tuple[str, str]) -> list[Any]:
    """STCG or LTCG lines; loosely typed, so one test covers both schedules."""
    factory = stcg_line if schedule == "STCG" else ltcg_line
    return [factory(proceeds, cost) for proceeds, cost in pairs]


def _fsi(stcg: list[Any] | None = None, ltcg: list[Any] | None = None) -> ScheduleFsiComputeResult:
    return compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=ScheduleCgStcgComputeResult(stcg or [], [], [], []),
        cg_ltcg_result=ScheduleCgLtcgComputeResult(ltcg or [], [], [], [], []),
    )


def _positions(*amounts: str) -> list[tuple[int, Decimal]]:
    return [(position, Decimal(amount)) for position, amount in enumerate(amounts, start=1)]


def _at(position: int) -> str:
    return f"Synthetic total, input row {position}, column amount"


# ---- shared arithmetic -------------------------------------------------------


def test_six_amounts_keep_every_digit() -> None:
    assert sum(map(Decimal, _SIX), Decimal()) == Decimal(_LONG)  # the 28-digit context
    total = checked_sum(_positions(*_SIX), at=_at)
    assert total.compare_total(Decimal("1.5000000000000000000000000000")) == 0
    assert to_whole_rupees(total) == 2


def test_discarded_zero_is_kept() -> None:
    """Reversed, the last addition only drops a trailing zero in 28 digits
    (``Rounded`` without ``Inexact``); the exact total keeps it."""
    reversed_six = _positions(*reversed(_SIX))
    with localcontext() as context:
        context.clear_flags()
        sum((amount for _position, amount in reversed_six), Decimal())
        assert context.flags[Rounded]
        assert not context.flags[Inexact]
    total = checked_sum(reversed_six, at=_at)
    assert total.compare_total(Decimal("1.5000000000000000000000000000")) == 0


def test_subtraction_changes_sign_without_rounding() -> None:
    """Unary minus would round the 29-digit operand to 28 digits first."""
    operand = Decimal("-" + MARKED_AMOUNT)
    assert -operand != Decimal(MARKED_AMOUNT)
    result = checked_subtraction(Decimal("0"), operand, at=_at(1))
    assert result.compare_total(Decimal(MARKED_AMOUNT)) == 0


def test_hostile_context_still_adds_exactly_and_is_left_unchanged() -> None:
    with localcontext(Context(prec=9, traps=[Inexact, Rounded])):
        total = checked_sum(_positions(*_SIX), at=_at)
        assert getcontext().prec == 9
    assert total == Decimal("1.5")


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        ((), "0"),
        (("0", "0.00"), "0.00"),
        (("5.00", "-1.25"), "3.75"),
        (("-2.50", "1.25", "-0.75"), "-2.00"),
        (("8574.25", "4287.125"), "12861.375"),
    ],
)
def test_ordinary_amounts_keep_their_value_and_exponent(
    amounts: tuple[str, ...], expected: str
) -> None:
    total = checked_sum(_positions(*amounts), at=_at)
    assert total.compare_total(sum(map(Decimal, amounts), Decimal())) == 0
    assert total.compare_total(Decimal(expected)) == 0


def test_cap_is_inclusive() -> None:
    longest = Decimal(f"1E+{EXACT_SUM_DIGITS - 1}")
    total = checked_addition(longest, Decimal("1"), at=_at(2))
    assert len(total.as_tuple().digits) == EXACT_SUM_DIGITS
    with pytest.raises(InvariantError) as raised:
        checked_addition(longest * 10, Decimal("1"), at=_at(2))
    _refusal(raised, _at(2))


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_amount_refuses(amount: str) -> None:
    with pytest.raises(InvariantError) as raised:
        checked_sum(_positions("1.25", amount), at=_at)
    _refusal(raised, _at(2))


def test_overflow_refuses() -> None:
    """Past the context's largest exponent, whatever the precision."""
    with pytest.raises(InvariantError) as raised:
        checked_addition(Decimal("9E+999999"), Decimal("9E+999999"), at=_at(2))
    _refusal(raised, _at(2))


def test_over_long_sum_refuses_at_its_row() -> None:
    with pytest.raises(InvariantError) as raised:
        checked_sum(_positions(MARKED_AMOUNT, _HUGE, _MINUTE), at=_at)
    _refusal(raised, _at(3), markers=(MARKED_AMOUNT,))


def _terms(*terms: tuple[str, Fraction | None]) -> list[tuple[int, Decimal, Fraction | None]]:
    return [
        (position, Decimal(amount), rational)
        for position, (amount, rational) in enumerate(terms, start=1)
    ]


def test_inexact_sum_is_the_checked_sum() -> None:
    """#1241: without ``exact``, the total is :func:`checked_sum`'s, every digit kept."""
    total, rational = checked_exact_sum(
        _terms(*((amount, None) for amount in _SIX)), at=_at, exact=False
    )
    assert rational is None
    assert total.compare_total(Decimal("1.5000000000000000000000000000")) == 0


def test_inexact_sum_refuses_to_drop_a_rational() -> None:
    """#1241: a sum that isn't exact may not silently lose a term's rational."""
    with pytest.raises(InvariantError) as raised:
        checked_exact_sum(_terms(("1.25", None), ("3.333", Fraction(10, 3))), at=_at, exact=False)
    assert str(raised.value) == (
        "InvariantError: checked_exact_sum: a term's exact rational would be dropped"
    )


def test_exact_sum_adds_each_rational_or_its_amount() -> None:
    """#1241: a term's rational where it has one, else its amount's; the Decimal total is
    unchanged."""
    total, rational = checked_exact_sum(
        _terms(("3.333333333333333333333333333", Fraction(10, 3)), ("1.25", None)),
        at=_at,
        exact=True,
    )
    assert rational == Fraction(55, 12)
    assert total.compare_total(Decimal("4.583333333333333333333333333")) == 0


def test_exact_sum_refuses_an_amount_without_a_rational_at_its_row() -> None:
    """#1241: 1E-1001 adds exactly as a Decimal but has no exact rational: the exact sum
    refuses at its row, as an inexact sum does."""
    terms = _terms(("0", None), (f"1E-{EXACT_SUM_DIGITS + 1}", None))
    total, _ = checked_exact_sum(terms, at=_at, exact=False)
    assert total.compare_total(Decimal(f"1E-{EXACT_SUM_DIGITS + 1}")) == 0
    with pytest.raises(InvariantError) as raised:
        checked_exact_sum(terms, at=_at, exact=True)
    _refusal(raised, _at(2))


@pytest.mark.parametrize(
    "terms",
    [
        pytest.param((("1.25", None), ("NaN", Fraction(1, 3))), id="nonfinite"),
        pytest.param(
            ((_HUGE, Fraction(10**10)), (_MINUTE, Fraction(1, 10**995))), id="past-the-cap"
        ),
    ],
)
def test_exact_sum_keeps_the_checked_sum_refusals(
    terms: tuple[tuple[str, Fraction | None], ...],
) -> None:
    """#1241: the Decimal amounts still add as :func:`checked_sum` does, rationals or not."""
    with pytest.raises(InvariantError) as raised:
        checked_exact_sum(_terms(*terms), at=_at, exact=True)
    _refusal(raised, _at(2))


# ---- Schedule CG portal amounts ----------------------------------------------


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_six_amount_consideration_files_the_exact_total(schedule: Schedule, reverse: bool) -> None:
    amounts = tuple(reversed(_SIX)) if reverse else _SIX
    lines = _lines(schedule, *((amount, "0") for amount in amounts))

    require_schedule_cg_filed_amounts(lines, schedule=schedule)
    amounts_filed = schedule_cg_portal_amounts(lines, schedule=schedule)

    assert amounts_filed.consideration_inr == 2
    assert amounts_filed.filed_total_inr == 2
    # Every digit, the trailing zero the reversed order drops in 28 digits too.
    exact = Decimal("1.5000000000000000000000000000")
    assert amounts_filed.gain_inr_full_precision.compare_total(exact) == 0
    assert amounts_filed.gain_inr_rounded == 2


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_cost_and_gain_are_exact(schedule: Schedule) -> None:
    """Cost: six costs against ₹2 proceeds each. Gain: ``_LONG`` gained on one
    line and ``_TINY`` lost on five, against exact proceeds and costs."""
    cost_lines = _lines(schedule, *(("2", amount) for amount in _SIX))
    assert schedule_cg_portal_amounts(cost_lines, schedule=schedule).cost_of_acquisition_inr == 2

    gain_lines = _lines(schedule, (_LONG, "0"), *(("0", _TINY),) * 5)
    gain = schedule_cg_portal_amounts(gain_lines, schedule=schedule).gain_inr_full_precision
    assert gain.compare_total(Decimal("1.4999999999999999999999999960")) == 0


def test_bound_reads_the_exact_consideration() -> None:
    """The 28-digit context keeps the first proceeds and passes the bound; the
    exact total reaches ₹99,999,999,999,999.5 and is refused."""
    lines = _stcg(("99999999999999.49999999999999", "0"), *(("2E-15", "0"),) * 5)
    with pytest.raises(InvariantError) as raised:
        require_schedule_cg_filed_amounts(lines, schedule="STCG")
    assert str(raised.value) == (
        "InvariantError: Schedule CG STCG consideration must round to a whole-rupee amount "
        "within ±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)"
    )


def test_eligibility_reads_the_exact_consideration() -> None:
    """Owner decision 2026-11-10 (exact, never round): the 28-digit context
    keeps only the first proceeds, ₹0, and refuses the ₹1 cost; the exact
    consideration is ₹0.50, so ₹1, and the row is valid."""
    lines = _stcg(("0.4999999999999999999999999998", "0.6"), *(("4E-29", "0"),) * 5)
    filed = schedule_cg_portal_amounts(lines, schedule="STCG")
    assert (filed.consideration_inr, filed.cost_of_acquisition_inr, filed.filed_total_inr) == (
        1,
        1,
        0,
    )


_COLUMN_CASES = {
    "proceeds_inr": ((_HUGE, "0"), (_MINUTE, "0")),
    "cost_basis_inr": (("1", _HUGE), ("1", _MINUTE)),
    "realized_gain_inr": ((_HUGE, "0"), ("0", _MINUTE)),
}


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
@pytest.mark.parametrize(
    ("compute", "column"),
    [
        (require_schedule_cg_filed_amounts, "proceeds_inr"),
        (require_schedule_cg_filed_amounts, "cost_basis_inr"),
        (schedule_cg_portal_amounts, "proceeds_inr"),
        (schedule_cg_portal_amounts, "cost_basis_inr"),
        (schedule_cg_portal_amounts, "realized_gain_inr"),
    ],
)
def test_portal_sum_past_the_cap_refuses_at_its_row(
    schedule: Schedule, compute: Callable[..., object], column: str
) -> None:
    lines = [marked(_lines(schedule, ("1", "0"))[0]), *_lines(schedule, *_COLUMN_CASES[column])]
    with pytest.raises(InvariantError) as raised:
        compute(lines, schedule=schedule)
    _refusal(raised, f"Schedule CG {schedule}, input row 3, column {column}", markers=MARKERS)


# ---- Schedule FSI capital gains ----------------------------------------------


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_fsi_net_files_the_exact_total(schedule: Schedule) -> None:
    lines = _lines(schedule, *((amount, "0") for amount in _SIX))
    [row] = _fsi(**{schedule.lower(): lines}).rows
    assert (row.income_inr, row.filed.income_inr, row.event_count) == (
        Decimal("1.5"),
        Decimal("2"),
        6,
    )


@pytest.mark.parametrize(
    ("stcg", "ltcg", "income"),
    [
        ((_LONG, "0"), (_TINY, "0"), "1.4999999999999999999999999984"),
        (("0", _TINY), (_LONG, "0"), "1.4999999999999999999999999976"),
    ],
    ids=["both-gains", "section-70-2"],
)
def test_fsi_combination_is_exact(
    stcg: tuple[str, str], ltcg: tuple[str, str], income: str
) -> None:
    [row] = _fsi(stcg=_stcg(stcg), ltcg=_ltcg(ltcg)).rows
    assert row.income_inr.compare_total(Decimal(income)) == 0


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_fsi_net_past_the_cap_refuses_at_its_row_before_country_filtering(
    schedule: Schedule,
) -> None:
    """The US net fails at its line's position among all lines, GB's included."""
    lines = [
        _in(_lines(schedule, ("1", "0"))[0], "GB"),
        marked(_lines(schedule, ("1", "0"))[0]),
        _in(_lines(schedule, ("2", "0"))[0], "GB"),
        *_lines(schedule, (_HUGE, "0"), (_MINUTE, "0")),
    ]
    with pytest.raises(InvariantError) as raised:
        _fsi(**{schedule.lower(): lines})
    _refusal(
        raised,
        f"Schedule FSI capital gains {schedule} net, input row 5, column realized_gain_inr",
        markers=MARKERS,
    )


def test_fsi_refuses_the_first_country_in_code_order() -> None:
    """Both countries' nets fail; the refusal always names GB's row."""
    lines = [
        *_stcg((_HUGE, "0"), (_MINUTE, "0")),
        *(_in(line, "GB") for line in _stcg((_HUGE, "0"), (_MINUTE, "0"))),
    ]
    with pytest.raises(InvariantError) as raised:
        _fsi(stcg=lines)
    _refusal(raised, "Schedule FSI capital gains STCG net, input row 4, column realized_gain_inr")


def test_fsi_combination_past_the_cap_refuses_in_each_branch_that_adds() -> None:
    """Each net is exact; their sum is too long. Both losses refuse too, though
    that row would be omitted."""
    branches = {
        "both-gains": ([marked(stcg_line("1", "0")), *_stcg((_HUGE, "0"))], _ltcg((_MINUTE, "0"))),
        "section-70-2": (
            _stcg(("0", _MINUTE)),
            [marked(ltcg_line("1", "0")), *_ltcg((_HUGE, "0"))],
        ),
        "both-losses": ([marked(stcg_line("1", "0")), *_stcg(("0", _HUGE))], _ltcg(("0", _MINUTE))),
    }
    for stcg, ltcg in branches.values():
        with pytest.raises(InvariantError) as raised:
            _fsi(stcg=stcg, ltcg=ltcg)
        _refusal(
            raised, "Schedule FSI capital gains, fields stcg_net and ltcg_net", markers=MARKERS
        )


def test_section_70_3_files_the_stcg_net_alone() -> None:
    """An LTCG net loss leaves the STCG net as the income, so no addition runs
    even where one would exceed the cap."""
    [row] = _fsi(stcg=_stcg((_HUGE, "0")), ltcg=_ltcg(("0", _MINUTE))).rows
    assert (row.income_inr, row.event_count) == (Decimal(_HUGE), 1)


def test_long_fsi_income_keeps_every_digit_in_a_merge() -> None:
    """A capital-gains income longer than the context reaches #1136's merge
    only from a second capital-gains source, which no command has. Since #1312
    the merge keeps every digit, ₹2.4999999999999999999999999984 filed as ₹2;
    main refused it rather than round."""
    longer = _fsi(stcg=_stcg((_LONG, "0"), (_TINY, "0")))
    assert len(longer.rows[0].income_inr.as_tuple().digits) == 29
    [row] = _merge_results(longer, _fsi(stcg=_stcg(("1", "0")))).rows
    assert row.income_inr.compare_total(Decimal("2.4999999999999999999999999984")) == 0
    assert (row.filed.income_inr, row.event_count) == (Decimal("2"), 3)


def test_long_fsi_income_past_the_digit_cap_refuses_a_merge() -> None:
    """#1312: a merged capital-gains income that needs 1,001 digits still refuses."""
    tiny = _fsi(stcg=_stcg(("1E-1000", "0")))
    with pytest.raises(InvariantError) as raised:
        _merge_results(tiny, _fsi(stcg=_stcg(("1", "0"))))
    assert str(raised.value) == (
        f"InvariantError: Schedule FSI income by country and head INR {_EXACT}"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


_SOLD = date(2025, 5, 15)


def _issue_may_slice() -> tuple[list[ScheduleCgStcgLine], list[ScheduleCgLtcgLine]]:
    """#1241's FSI repro, FY 2025-26: 2 of 3 shares bought for $10.00 sold for $10.00 at
    ₹84.15, at the exact cost $20/3: exact gain ₹280.50."""
    line = exact_stcg_line(
        cost_usd=Fraction(20, 3), proceeds_usd=10, rate="84.15", sold=_SOLD, quantity="2"
    )
    return [line], []


def _split_tie() -> tuple[list[ScheduleCgStcgLine], list[ScheduleCgLtcgLine]]:
    """Exact gains ₹1/3 (STCG) and ₹280.50 - ₹1/3 (LTCG), at ₹1/$; their Decimal cuts add
    to ₹280.4999…, which files ₹280."""
    stcg = exact_stcg_line(cost_usd=0, proceeds_usd=Fraction(1, 3), rate="1", sold=_SOLD)
    ltcg = exact_ltcg_line(cost_usd=0, proceeds_usd=Fraction(1681, 6), rate="1", sold=_SOLD)
    return [stcg], [ltcg]


@pytest.mark.parametrize(
    ("build", "events"),
    [
        pytest.param(_issue_may_slice, 1, id="issue-may-slice"),
        pytest.param(_split_tie, 2, id="split"),
    ],
)
def test_fsi_files_the_exact_income(
    build: Callable[[], tuple[list[ScheduleCgStcgLine], list[ScheduleCgLtcgLine]]], events: int
) -> None:
    """#1241: the exact income ₹280.50 files ₹281."""
    stcg, ltcg = build()
    [row] = _fsi(stcg=stcg, ltcg=ltcg).rows
    assert (row.filed.income_inr, row.event_count) == (Decimal("281"), events)
    assert row.income_inr.compare_total(Decimal("280.5")) == 0


def test_fsi_exact_nets_decide_the_set_off() -> None:
    """#1241: three exact LTCG gains of ₹1/3 and a ₹1 LTCG loss net exactly ₹0, so section
    70(3) doesn't apply and all five events count. Their Decimal cuts net -1E-28, which would
    leave the four LTCG events out."""
    thirds = [
        exact_ltcg_line(cost_usd=0, proceeds_usd=Fraction(1, 3), rate="1", sold=_SOLD)
        for _ in range(3)
    ]
    ltcg = [*thirds, ltcg_line("0", "1")]

    [row] = _fsi(stcg=_stcg(("5", "0")), ltcg=ltcg).rows

    assert (str(row.income_inr), row.event_count) == ("5", 5)
    decimal_net = checked_sum(enumerate((line.realized_gain_inr for line in ltcg), start=1), at=_at)
    assert decimal_net == Decimal("-1E-28")


def test_fsi_country_without_exact_lines_keeps_its_decimal_net() -> None:
    """#1241: GB's plain line adds as before, trailing zero included, beside a US line with
    exact amounts."""
    us = exact_stcg_line(cost_usd=0, proceeds_usd=Fraction(1, 3), rate="1", sold=_SOLD)
    rows = {row.iso_alpha2: row for row in _fsi(stcg=[_in(stcg_line("1.50", "0"), "GB"), us]).rows}
    assert (str(rows["GB"].income_inr), rows["GB"].event_count) == ("1.50", 1)
    assert str(rows["US"].income_inr) == "0.3333333333333333333333333333"
