"""Exact Schedule CG line conversion and gain subtraction (#1184).

Owner decision (2026-11-11): exact, never round. Each STCG and LTCG line's INR
proceeds and non-overridden cost are the lot's USD amounts times the rate;
statutory INR costs are preserved as supplied. Its gain is proceeds minus
cost, all at #1166's widened precision (at least
``EXACT_SUM_DIGITS`` digits) instead of the 28-digit context. A conversion or
gain that can't be exact refuses, naming the schedule, the lot's 1-based
position among all input lots (filtered ones included) and the column.

All values are synthetic. Expected values are computed at 2,000 digits.
"""

from __future__ import annotations

from datetime import date
from decimal import Clamped, Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from typing import Any, Literal

import pytest

from ibkr_fa.compute.exact_inr import (
    EXACT_SUM_DIGITS,
    checked_product,
    exact_decimal,
    exact_difference,
    exact_product,
)
from ibkr_fa.compute.schedule_cg import compute_schedule_cg_stcg_lines
from ibkr_fa.compute.schedule_cg_ltcg import compute_schedule_cg_ltcg_lines
from ibkr_fa.compute.schedule_fsi import compute_schedule_fsi_from_capital_gains
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.models.schedule_cg_row import ScheduleCgExactAmounts
from ibkr_fa.types import Isin, Ticker
from tests._compute_builders import ltcg_closed_lot as ltcg_lot
from tests._compute_builders import stcg_closed_lot as stcg_lot
from tests._crash_reports import render_without_frames
from tests._exact_refusals import INEXACT_PRODUCT as _PRODUCT_REFUSAL
from tests._exact_refusals import INEXACT_SUM as _SUM_REFUSAL
from tests._fx_stubs import AuditedFxStub
from tests._lot_matching_inputs import MatchInstrument as _Instrument
from tests._lot_matching_inputs import close_execution as _close
from tests._lot_matching_inputs import match_lots as _match
from tests._lot_matching_inputs import open_execution as _open

type Schedule = Literal["STCG", "LTCG"]

_SCHEDULES: tuple[Schedule, ...] = ("STCG", "LTCG")
_RATE = "86.20"
_LONG_USD = "1.2345678901234567890123456789"  # 29 digits
_JUST_BELOW_HALF = "1.4999999999999999999999999999"  # 29 digits; the 28-digit context makes 1.5
_TINY_COST_GAIN = "1.49999999999999999999999999999"  # 1.5 - 1E-29
_HUGE = "9E+999999"  # finite, but times the rate it passes the largest exponent
_PAST_CAP = "1E+1001"  # minus a zero cost it needs 1,002 digits
_HALF_CAP = "1" * 501  # squared it has 1,001 digits
_AT = "Synthetic product, input row 2, column amount"
_HOSTILE = Context(prec=3, traps=[Inexact, Rounded])
_WIDE = Context(prec=2000)

# Privacy markers (#1184): none may reach a refusal or captured output.
_SOURCE_MARKER = "SYNTHCGSOURCE"
_MARKERS = ("ZQXA", "SYNTHCGDESC", "US99981C1015", "SYNTHCGID", "SYNTHCGREASON")
_MARKED: dict[str, object] = {
    "symbol": Ticker("ZQXA"),
    "description": "SYNTHCGDESC",
    "isin": Isin("US99981C1015"),
    "listing_exchange": "SYNTHCGID",
    "sub_category": "SYNTHCGREASON",
    "cost_basis_money": Decimal("1234.56"),
    "proceeds_money": Decimal("2345.67"),
}


def _times(multiplicand: Decimal, multiplier: Decimal) -> Decimal:
    with localcontext(_WIDE):
        return multiplicand * multiplier


def _minus(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    with localcontext(_WIDE):
        return minuend - subtrahend


def _lot(schedule: Schedule, **fields: object) -> ClosedLot:
    """An in-FY lot of ``schedule``'s term, from the shared compute builders."""
    return (stcg_lot if schedule == "STCG" else ltcg_lot)(**fields)


def _other(schedule: Schedule) -> Schedule:
    return "LTCG" if schedule == "STCG" else "STCG"


def _compute(schedule: Schedule, lots: list[ClosedLot], rate: str = _RATE) -> Any:
    """The schedule's compute result at a flat ``rate``; loosely typed, so one
    test covers both schedules."""
    compute = (
        compute_schedule_cg_stcg_lines if schedule == "STCG" else compute_schedule_cg_ltcg_lines
    )
    return compute(
        lots,
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=AuditedFxStub(Decimal(rate)),
    )


def _lines(schedule: Schedule, lots: list[ClosedLot], rate: str = _RATE) -> list[Any]:
    result = _compute(schedule, lots, rate)
    return result.stcg_lines if schedule == "STCG" else result.ltcg_lines


def _refusal(raised: pytest.ExceptionInfo[InvariantError], expected: str) -> str:
    """Check the complete message, no context and no chain; return the rendered chain."""
    error = raised.value
    assert str(error) == f"InvariantError: {expected}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    return render_without_frames(error)


# ---- shared arithmetic -------------------------------------------------------


def test_product_and_difference_keep_every_digit() -> None:
    """The 28-digit context rounds both; the exact helpers keep every digit."""
    product = exact_product(Decimal(_LONG_USD), Decimal(_RATE))
    difference = exact_difference(Decimal("1.5"), Decimal("1E-29"))
    assert product is not None
    assert difference is not None
    assert product.compare_total(Decimal("106.419752128641975212864197521180")) == 0
    assert difference.compare_total(Decimal(_TINY_COST_GAIN)) == 0
    assert checked_product(Decimal(_LONG_USD), Decimal(_RATE), at=_AT).compare_total(product) == 0
    assert Decimal(_LONG_USD) * Decimal(_RATE) != product
    assert Decimal("1.5") - Decimal("1E-29") != difference


_CALLERS = {"trapped": Context(), "untrapped": Context(traps=[])}
_INEXACT_PRODUCTS = {
    "nan": ("NaN", _RATE),
    "snan": (_RATE, "sNaN"),
    "infinity": ("Infinity", _RATE),
    "minus-infinity": (_RATE, "-Infinity"),
    "infinity-times-zero": ("Infinity", "0"),
    "overflow": (_HUGE, _RATE),
    "past-the-cap": (_HALF_CAP, _HALF_CAP),
}
_INEXACT_DIFFERENCES = {
    "nan": ("NaN", "1"),
    "snan": ("1", "sNaN"),
    "infinity": ("Infinity", "1"),
    "minus-infinity": ("1", "-Infinity"),
    "infinity-minus-infinity": ("Infinity", "Infinity"),
    "overflow": (_HUGE, "-" + _HUGE),
    "past-the-cap": (_PAST_CAP, "1"),
}


@pytest.mark.parametrize("caller", _CALLERS.values(), ids=_CALLERS)
@pytest.mark.parametrize(
    ("multiplicand", "multiplier"), _INEXACT_PRODUCTS.values(), ids=_INEXACT_PRODUCTS
)
def test_product_that_cannot_be_exact_is_none_and_refuses(
    caller: Context, multiplicand: str, multiplier: str
) -> None:
    """Nonfinite operands, an invalid operation, an overflow and a product past
    the digit cap, whether or not the caller traps the signal."""
    operands = (Decimal(multiplicand), Decimal(multiplier))
    with localcontext(caller):
        assert exact_product(*operands) is None
        with pytest.raises(InvariantError) as raised:
            checked_product(*operands, at=_AT)
    _refusal(raised, f"{_AT}: {_PRODUCT_REFUSAL}")


@pytest.mark.parametrize("caller", _CALLERS.values(), ids=_CALLERS)
@pytest.mark.parametrize(
    ("minuend", "subtrahend"), _INEXACT_DIFFERENCES.values(), ids=_INEXACT_DIFFERENCES
)
def test_difference_that_cannot_be_exact_is_none(
    caller: Context, minuend: str, subtrahend: str
) -> None:
    with localcontext(caller):
        assert exact_difference(Decimal(minuend), Decimal(subtrahend)) is None


def test_inherited_exponent_limit_applies() -> None:
    """The widened copy keeps the caller's ``Emax``."""
    product = (Decimal("9E+99"), Decimal(_RATE))
    difference = (Decimal("9E+100"), Decimal("-9E+100"))
    with localcontext(Context(Emax=100)):
        assert exact_product(*product) is None
        assert exact_difference(*difference) is None
    assert exact_product(*product) == Decimal("7.758E+101")
    assert exact_difference(*difference) == Decimal("1.8E+101")


def test_higher_active_precision_is_kept() -> None:
    """``EXACT_SUM_DIGITS`` is a floor: at 1,200 digits the same operands pass."""
    with localcontext(Context(prec=1200)):
        product = exact_product(Decimal(_HALF_CAP), Decimal(_HALF_CAP))
        difference = exact_difference(Decimal(_PAST_CAP), Decimal("1"))
    assert product is not None
    assert difference is not None
    assert len(product.as_tuple().digits) > EXACT_SUM_DIGITS
    assert product.compare_total(_times(Decimal(_HALF_CAP), Decimal(_HALF_CAP))) == 0
    assert difference.compare_total(Decimal("9" * 1001)) == 0


@pytest.mark.parametrize("caller", [Context(), _HOSTILE], ids=["default", "hostile"])
def test_caller_context_is_left_unchanged(caller: Context) -> None:
    """Exact and failed calls keep the caller's precision, traps and flags,
    including one already set; a trapped ``Rounded`` past the cap is None."""
    with localcontext(caller) as context:
        context.flags[Clamped] = True
        before = (context.prec, dict(context.traps), dict(context.flags))
        product = exact_product(Decimal(_LONG_USD), Decimal(_RATE))
        difference = exact_difference(Decimal("1.5"), Decimal("1E-29"))
        failed = (
            exact_product(Decimal(_HALF_CAP), Decimal(_HALF_CAP)),
            exact_product(Decimal(_HUGE), Decimal(_RATE)),
            exact_difference(Decimal("Infinity"), Decimal("Infinity")),
        )
        with pytest.raises(InvariantError):
            checked_product(Decimal("sNaN"), Decimal(_RATE), at=_AT)
        after = (context.prec, dict(context.traps), dict(context.flags))
    assert after == before
    assert failed == (None, None, None)
    assert product is not None
    assert difference is not None
    assert product.compare_total(_times(Decimal(_LONG_USD), Decimal(_RATE))) == 0
    assert difference.compare_total(Decimal(_TINY_COST_GAIN)) == 0


# ---- regressions -------------------------------------------------------------


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_just_below_half_proceeds_file_one_rupee(schedule: Schedule) -> None:
    """The 28-digit context rounded the proceeds to 1.5 and filed ₹2."""
    result = _compute(
        schedule,
        [_lot(schedule, proceeds_money=Decimal(_JUST_BELOW_HALF), cost_basis_money=Decimal(0))],
        "1",
    )
    filed = result.portal_amounts
    assert (filed.consideration_inr, filed.filed_total_inr) == (1, 1)
    [line] = result.stcg_lines if schedule == "STCG" else result.ltcg_lines
    assert line.proceeds_inr.compare_total(Decimal(_JUST_BELOW_HALF)) == 0
    assert line.realized_gain_inr.compare_total(Decimal(_JUST_BELOW_HALF)) == 0


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_tiny_cost_keeps_the_gain_below_half_and_fsi_files_one_rupee(schedule: Schedule) -> None:
    """The 28-digit context made 1.5 - 1E-29 equal 1.5, so FSI filed ₹2."""
    lot = _lot(schedule, proceeds_money=Decimal("1.5"), cost_basis_money=Decimal("1E-29"))
    fsi = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=_compute("STCG", [lot], "1"),
        cg_ltcg_result=_compute("LTCG", [lot], "1"),
    )
    [row] = fsi.rows
    assert row.filed.income_inr == 1
    assert row.income_inr.compare_total(Decimal(_TINY_COST_GAIN)) == 0
    [line] = _lines(schedule, [lot], "1")
    assert line.realized_gain_inr.compare_total(Decimal(_TINY_COST_GAIN)) == 0


_FIFO = _Instrument("XQEX", "US99984D1011", "SAMPLE ISSUER EX INC")
_BUYS = {
    "STCG": ("2025-05-12 10:00:00", "2025-06-12 10:00:00"),
    "LTCG": ("2023-05-12 10:00:00", "2023-06-12 10:00:00"),
}


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_fifo_split_lines_equal_exact_arithmetic_on_the_matched_lots(schedule: Schedule) -> None:
    """A $10.00 sale of 3 shares against 1- and 2-share buys. Since #1273
    ``match_fifo`` keeps the slices' proceeds exact, 10/3 and 20/3, adding to
    exactly $10.00; on 0d3547b they were 28-digit Decimals adding to
    $9.999…9, and this test pinned them. At ₹86.20 each line's exact amounts
    are the exact conversion and gain of the lot's amounts, its proceeds and
    gain are their ``exact_decimal``, and its cost, which divides evenly, is
    the exact product of the lot's Decimal."""
    first, second = _BUYS[schedule]
    _open_lots, lots = _match(
        [
            _open(0, first, "1", "2.15", instrument=_FIFO),
            _open(1, second, "2", "4.31", instrument=_FIFO),
            _close(2, "2025-11-10 10:00:00", "3", "10.00", trade_price="3.34", instrument=_FIFO),
        ],
        {},
    )
    assert [(lot.cost_basis_money_exact, lot.proceeds_money_exact) for lot in lots] == [
        (None, Fraction(10, 3)),
        (None, Fraction(20, 3)),
    ]
    lines = _lines(schedule, lots)
    rate = Decimal(_RATE)
    for lot, line in zip(lots, lines, strict=True):
        assert lot.proceeds_money_exact is not None
        cost = Fraction(lot.cost_basis_money) * Fraction(rate)
        proceeds = lot.proceeds_money_exact * Fraction(rate)
        assert line.exact_amounts == ScheduleCgExactAmounts(
            cost_basis_usd=Fraction(lot.cost_basis_money),
            proceeds_usd=lot.proceeds_money_exact,
            cost_basis_inr=cost,
            proceeds_inr=proceeds,
            realized_gain_inr=proceeds - cost,
        )
        assert line.cost_basis_inr.compare_total(_times(lot.cost_basis_money, rate)) == 0
        assert line.proceeds_inr.compare_total(exact_decimal(proceeds)) == 0
        assert line.realized_gain_inr.compare_total(exact_decimal(proceeds - cost)) == 0


# ---- compute refusals --------------------------------------------------------

_STATUTORY: dict[str, object] = {
    "basis_provenance": BasisProvenance.S49_2AA,
    "cost_basis_inr_override": StatutoryInrCostBasis(
        amount_inr=Decimal("3456.78"), source=_SOURCE_MARKER
    ),
}
_FAILING: dict[str, tuple[dict[str, object], str, str]] = {
    "cost-overflow": ({"cost_basis_money": Decimal(_HUGE)}, _RATE, "cost_basis_inr"),
    "proceeds-overflow": ({"proceeds_money": Decimal(_HUGE)}, _RATE, "proceeds_inr"),
    "gain-past-the-cap": (
        {"proceeds_money": Decimal(_PAST_CAP), "cost_basis_money": Decimal(0)},
        "1",
        "realized_gain_inr",
    ),
    "both-overflow-names-cost": (
        {"cost_basis_money": Decimal(_HUGE), "proceeds_money": Decimal(_HUGE)},
        _RATE,
        "cost_basis_inr",
    ),
    "infinite-rate-names-cost": ({}, "Infinity", "cost_basis_inr"),
    "infinite-rate-statutory-cost": (_STATUTORY, "Infinity", "proceeds_inr"),
}


@pytest.mark.parametrize("schedule", _SCHEDULES)
@pytest.mark.parametrize(("fields", "rate", "column"), _FAILING.values(), ids=_FAILING)
def test_line_that_cannot_be_exact_refuses_at_its_input_position(
    schedule: Schedule,
    fields: dict[str, object],
    rate: str,
    column: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An out-of-FY lot and an other-term lot come first, so the failing lot is
    input lot 3. Cost is checked before proceeds; a statutory cost has no
    product. The marked lot's text and amounts appear nowhere."""
    lot = _lot(schedule, **{**_MARKED, **fields})
    lots = [
        _lot(schedule, open_date_time=date(2024, 6, 15), close_date_time=date(2025, 3, 15)),
        _lot(_other(schedule)),
        lot,
    ]
    assert len(_lines(schedule, [*lots[:2], _lot(schedule)])) == 1  # the first two get no line
    with pytest.raises(InvariantError) as raised:
        _compute(schedule, lots, rate)
    suffix = _SUM_REFUSAL if column == "realized_gain_inr" else _PRODUCT_REFUSAL
    rendered = _refusal(raised, f"Schedule CG {schedule}, input lot 3, column {column}: {suffix}")
    override = lot.cost_basis_inr_override
    private = [*_MARKERS, str(lot.cost_basis_money), str(lot.proceeds_money)]
    if override is not None:
        private += [override.source, str(override.amount_inr)]
    assert capsys.readouterr() == ("", "")
    for text in private:
        if text != "0":
            assert text not in rendered, text


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_gain_past_the_cap_refuses_before_the_portal_total(schedule: Schedule) -> None:
    """Main computed this gain at 28 digits and only refused at the portal
    total (input row 1, column proceeds_inr); the gain subtraction is first."""
    lot = _lot(schedule, proceeds_money=Decimal(_PAST_CAP), cost_basis_money=Decimal(0))
    with pytest.raises(InvariantError) as raised:
        _ = _compute(schedule, [lot], "1").portal_amounts
    _refusal(
        raised, f"Schedule CG {schedule}, input lot 1, column realized_gain_inr: {_SUM_REFUSAL}"
    )


# ---- preserved behaviour -----------------------------------------------------


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_statutory_cost_is_kept_exactly_without_a_cost_conversion(schedule: Schedule) -> None:
    """The override is the cost as given, with its source; the USD cost, which
    would overflow at the rate, is never converted; the gain is exact."""
    override = Decimal("1234.567890123456789012345678901")
    lot = _lot(
        schedule,
        cost_basis_money=Decimal(_HUGE),
        proceeds_money=Decimal(_LONG_USD),
        basis_provenance=BasisProvenance.S49_2AA,
        cost_basis_inr_override=StatutoryInrCostBasis(amount_inr=override, source=_SOURCE_MARKER),
    )
    [line] = _lines(schedule, [lot])
    proceeds = _times(Decimal(_LONG_USD), Decimal(_RATE))
    assert line.realized_gain_inr.compare_total(_minus(proceeds, override)) == 0
    assert line.proceeds_inr.compare_total(proceeds) == 0
    assert line.cost_basis_inr.compare_total(override) == 0
    assert line.cost_basis_inr_source == _SOURCE_MARKER
    assert line.cost_basis_usd == Decimal(_HUGE)


@pytest.mark.parametrize("schedule", _SCHEDULES)
@pytest.mark.parametrize(
    ("proceeds", "cost"),
    [("-" + _LONG_USD, "0.01"), (_LONG_USD, "2.15")],
    ids=["negative-proceeds", "loss"],
)
def test_negative_proceeds_and_losses_compute_exactly(
    schedule: Schedule, proceeds: str, cost: str
) -> None:
    [line] = _lines(
        schedule, [_lot(schedule, proceeds_money=Decimal(proceeds), cost_basis_money=Decimal(cost))]
    )
    proceeds_inr = _times(Decimal(proceeds), Decimal(_RATE))
    cost_inr = _times(Decimal(cost), Decimal(_RATE))
    assert line.realized_gain_inr.compare_total(_minus(proceeds_inr, cost_inr)) == 0
    assert line.realized_gain_inr < 0
    assert line.proceeds_inr.compare_total(proceeds_inr) == 0
    assert line.cost_basis_inr.compare_total(cost_inr) == 0


@pytest.mark.parametrize("schedule", _SCHEDULES)
def test_hostile_caller_context_gives_exact_lines_and_is_left_unchanged(
    schedule: Schedule,
) -> None:
    """At 3 digits with ``Inexact`` and ``Rounded`` trapped, the line is still
    exact and the caller's precision, traps and flags are untouched."""
    lot = _lot(schedule, proceeds_money=Decimal(_LONG_USD), cost_basis_money=Decimal("2.15"))
    with localcontext(_HOSTILE) as context:
        before = (context.prec, dict(context.traps), dict(context.flags))
        [line] = _lines(schedule, [lot])
        after = (context.prec, dict(context.traps), dict(context.flags))
    proceeds = _times(Decimal(_LONG_USD), Decimal(_RATE))
    cost = _times(Decimal("2.15"), Decimal(_RATE))
    assert line.realized_gain_inr.compare_total(_minus(proceeds, cost)) == 0
    assert line.proceeds_inr.compare_total(proceeds) == 0
    assert line.cost_basis_inr.compare_total(cost) == 0
    assert after == before
