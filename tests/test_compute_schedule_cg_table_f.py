"""Tests for one Schedule CG section's Table F row (#1360)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from ibkr_fa.compute.schedule_cg_table_f import (
    ScheduleCgTableFCase,
    ScheduleCgTableFRow,
    TableFGain,
    cg_line_gains,
    schedule_cg_table_f_row,
)
from ibkr_fa.exceptions import InvariantError
from tests._exact_refusals import INEXACT_SUM
from tests._robinhood_cg import cg_result, sale

# One date in each band, (i) to (v), of FY 2025-26.
_BAND_DATES = (
    date(2025, 6, 10),
    date(2025, 7, 1),
    date(2025, 10, 1),
    date(2026, 1, 15),
    date(2026, 3, 20),
)


def _gain(band: int, amount: str, exact: Fraction | None = None) -> TableFGain:
    return TableFGain(_BAND_DATES[band], Decimal(amount), exact)


def _computed(*bands: str) -> ScheduleCgTableFRow:
    return ScheduleCgTableFRow(
        ScheduleCgTableFCase.COMPUTED, tuple(Decimal(band) for band in bands)
    )


def _row(*gains: TableFGain, result: str | None) -> ScheduleCgTableFRow:
    return schedule_cg_table_f_row(gains, result_inr=None if result is None else Decimal(result))


# ---- rules (a) to (e) ----


def test_a_manual_section_has_no_bands() -> None:
    """Rule (a): the result is entered by hand, so the bands are too."""
    assert _row(_gain(0, "300"), result=None) == ScheduleCgTableFRow(
        ScheduleCgTableFCase.MANUAL_SECTION, None
    )


@pytest.mark.parametrize("result", ["0", "-50"])
def test_a_result_of_zero_or_less_files_five_zeros(result: str) -> None:
    """Rule (b): even beside a band netting a gain."""
    assert _row(_gain(0, "300"), _gain(2, "-350"), result=result) == _computed(
        "0", "0", "0", "0", "0"
    )


def test_bands_without_a_loss_split_the_result_by_transfer_date() -> None:
    """Rule (c): each band is its net's floor, plus one rupee for the largest
    remainders until the bands add up to the result."""
    row = _row(
        _gain(0, "100.25"),
        _gain(2, "50.5"),
        _gain(2, "10"),
        _gain(4, "0.75"),
        result="162",
    )

    assert row == _computed("100", "0", "61", "0", "1")


def test_a_band_netting_a_gain_after_its_own_loss_is_not_a_loss_band() -> None:
    """Rule (c) reads each band's net: a loss beside a larger gain in the same
    band leaves no band netting a loss."""
    assert _row(_gain(0, "300"), _gain(0, "-100"), _gain(3, "50"), result="250") == _computed(
        "200", "0", "0", "50", "0"
    )


def test_one_gain_band_beside_a_loss_band_takes_the_whole_result() -> None:
    """Rule (d): the only gain band gets the result, the loss band 0."""
    assert _row(_gain(1, "300"), _gain(3, "-100"), result="200") == _computed(
        "0", "200", "0", "0", "0"
    )


def test_a_loss_band_beside_two_gain_bands_leaves_the_row_blank() -> None:
    """Rule (e): no source says which gain band the loss lowers."""
    assert _row(_gain(0, "300"), _gain(2, "-100"), _gain(3, "50"), result="250") == (
        ScheduleCgTableFRow(ScheduleCgTableFCase.LOSS_ACROSS_BANDS, None)
    )


# ---- exact nets (#1241) ----


def test_remainders_compare_as_exact_rationals() -> None:
    """A third of a rupee in band (iii) beats band (i)'s 28-digit cut of it,
    which ties with it as a Decimal and would win as the earlier band."""
    cut = "0.3333333333333333333333333333"
    rational = _row(
        _gain(0, cut),
        _gain(2, cut, Fraction(1, 3)),
        result="1",
    )
    decimal = _row(_gain(0, cut), _gain(2, cut), result="1")

    assert rational == _computed("0", "0", "1", "0", "0")
    assert decimal == _computed("1", "0", "0", "0", "0")


def test_a_band_net_adds_its_gains_exactly() -> None:
    """Thirds that a Decimal can't hold add up to a whole rupee in band (ii),
    whose zero remainder takes no extra rupee."""
    third = "33.33333333333333333333333333"
    row = _row(
        _gain(1, third, Fraction(100, 3)),
        _gain(1, third, Fraction(100, 3)),
        _gain(1, third, Fraction(100, 3)),
        _gain(3, "0.5"),
        result="101",
    )

    assert row == _computed("0", "100", "0", "1", "0")


# ---- the split adds up to the filed result whatever the component rounding ----

_ROUNDING = {
    # Consideration ₹9,400.4 files 9,400 and cost ₹8,200.6 files 8,201: the
    # result 1,199 is one below the rounded ₹1,199.8 gain.
    "minus-one": (("41.003", "47.002"), "1199", "-1", ("600", "0", "599", "0", "0")),
    "zero": (("41", "47"), "1200", "0", ("600", "0", "600", "0", "0")),
    # Consideration ₹9,400.6 files 9,401 and cost ₹8,200.4 files 8,200: the
    # result 1,201 is one above the rounded ₹1,200.2 gain.
    "plus-one": (("41.002", "47.003"), "1201", "1", ("601", "0", "600", "0", "0")),
}


@pytest.mark.parametrize(
    ("usd", "result", "rounding", "bands"), _ROUNDING.values(), ids=_ROUNDING.keys()
)
def test_the_bands_add_up_to_the_filed_result_in_each_rounding_case(
    usd: tuple[str, str], result: str, rounding: str, bands: tuple[str, ...]
) -> None:
    """Two equal sales in bands (i) and (iii) at ₹100 to the dollar;
    the tie on the leftover rupee goes to the earlier band."""
    cost, proceeds = usd
    engines = cg_result(
        sale(cost, proceeds),
        sale(
            cost,
            proceeds,
            sale_date=_BAND_DATES[2],
            lot_ordinal=3,
            sale_ordinal=4,
        ),
    )

    amounts = engines.stcg.portal_amounts
    assert amounts.balance_inr == Decimal(result)
    assert amounts.component_rounding_difference_inr == Decimal(rounding)
    assert engines.stcg.table_f == _computed(*bands)
    assert sum(engines.stcg.table_f.bands or (), Decimal(0)) == amounts.balance_inr


# ---- transfer dates and the engines' rows ----


@pytest.mark.parametrize(
    ("transfer_date", "band"),
    [
        (date(2025, 4, 1), 0),
        (date(2025, 6, 15), 0),
        (date(2025, 6, 16), 1),
        (date(2025, 9, 15), 1),
        (date(2025, 9, 16), 2),
        (date(2025, 12, 15), 2),
        (date(2025, 12, 16), 3),
        (date(2026, 3, 15), 3),
        (date(2026, 3, 16), 4),
        (date(2026, 3, 31), 4),
    ],
)
def test_a_gain_lands_in_the_band_of_its_transfer_date(transfer_date: date, band: int) -> None:
    row = schedule_cg_table_f_row(
        [TableFGain(transfer_date, Decimal("7"), None)], result_inr=Decimal("7")
    )

    assert row.bands == tuple(Decimal(7 if index == band else 0) for index in range(5))


def test_cg_line_gains_carry_each_lines_close_date_gain_and_exact_gain() -> None:
    engines = cg_result(sale("12", "10"))
    [line] = engines.stcg.stcg_lines

    assert list(cg_line_gains(engines.stcg.stcg_lines)) == [
        TableFGain(line.close_date, line.realized_gain_inr, None)
    ]


def test_each_engine_result_builds_its_row_from_its_own_lines() -> None:
    """Each section's gain files in its own band; no engine result leaves a
    section manual any more (#1383)."""
    engines = cg_result(
        sale("10", "12"),
        sale("10", "13", long_term=True, sale_date=_BAND_DATES[2], lot_ordinal=3, sale_ordinal=4),
    )

    assert engines.stcg.table_f == _computed("200", "0", "0", "0", "0")
    assert engines.ltcg.table_f == _computed("0", "0", "300", "0", "0")


# ---- refusals ----


def test_a_positive_result_without_a_gain_band_refuses() -> None:
    with pytest.raises(InvariantError) as raised:
        _row(_gain(0, "-100"), result="5")

    assert str(raised.value) == (
        "InvariantError: Schedule CG Table F: a positive section result needs a date "
        "band with a net gain"
    )


@pytest.mark.parametrize("result", ["99", "106"])
def test_a_result_outside_the_nets_split_refuses(result: str) -> None:
    """With no loss band, the result must be the floors' sum plus at most one
    rupee per band."""
    with pytest.raises(InvariantError) as raised:
        _row(_gain(0, "100.5"), result=result)

    assert str(raised.value) == (
        "InvariantError: Schedule CG Table F: the section result must fit its five date "
        "bands (context keys: rupees_to_allocate)"
    )


def test_a_band_net_that_cant_be_added_exactly_refuses_naming_the_gain() -> None:
    with pytest.raises(InvariantError) as raised:
        _row(_gain(0, "1E+600"), _gain(0, "1E-600"), result="1")

    assert str(raised.value) == f"InvariantError: Schedule CG Table F, gain 2: {INEXACT_SUM}"


@pytest.mark.parametrize(
    ("case", "bands", "what"),
    [
        (
            "computed",
            None,
            "ScheduleCgTableFRow.case must be a ScheduleCgTableFCase",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            None,
            "ScheduleCgTableFRow.bands must be present exactly in a computed row",
        ),
        (
            ScheduleCgTableFCase.MANUAL_SECTION,
            (Decimal("0"),) * 5,
            "ScheduleCgTableFRow.bands must be present exactly in a computed row",
        ),
        (
            ScheduleCgTableFCase.LOSS_ACROSS_BANDS,
            (Decimal("0"),) * 5,
            "ScheduleCgTableFRow.bands must be present exactly in a computed row",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4,
            "ScheduleCgTableFRow.bands must be five finite Decimals",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            [Decimal("0")] * 5,
            "ScheduleCgTableFRow.bands must be five finite Decimals",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (0,),
            "ScheduleCgTableFRow.bands must be five finite Decimals",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (Decimal("NaN"),),
            "ScheduleCgTableFRow.bands must be five finite Decimals",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (Decimal("1E+14"),),
            "ScheduleCgTableFRow band must round to a whole-rupee amount within "
            "±99,999,999,999,999 (the CBDT 14-digit bound) (context keys: value)",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (Decimal("1.5"),),
            "ScheduleCgTableFRow band must be a whole-rupee amount (no fraction, exponent "
            "or -0) (context keys: value)",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (Decimal("-0"),),
            "ScheduleCgTableFRow band must be a whole-rupee amount (no fraction, exponent "
            "or -0) (context keys: value)",
        ),
        (
            ScheduleCgTableFCase.COMPUTED,
            (Decimal("0"),) * 4 + (Decimal("-1"),),
            "ScheduleCgTableFRow bands must not be negative",
        ),
    ],
)
def test_a_row_refuses_an_inconsistent_case_or_band(case: object, bands: object, what: str) -> None:
    with pytest.raises(InvariantError) as raised:
        ScheduleCgTableFRow(case, bands)  # ty: ignore[invalid-argument-type]

    assert str(raised.value) == f"InvariantError: {what}"
