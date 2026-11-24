"""Shared Form 1042-S form-level gate and whole-dollar rounding (#637)."""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal, Inexact, Rounded, localcontext

import pytest

from ibkr_fa.models.form_1042s import (
    Form1042SAmount,
    Form1042SAmountKind,
    Form1042SCopy,
    Form1042SCopyType,
)
from ibkr_fa.normalize.form_1042s import (
    Form1042SAmountError,
    Form1042SViolation,
    form_1042s_violations,
    whole_dollars,
)

_BLANK = Form1042SAmount(Form1042SAmountKind.BLANK, None)
_DASH_ZERO = Form1042SAmount(Form1042SAmountKind.DASH_ZERO, Decimal(0))


def _amount(value: str) -> Form1042SAmount:
    return Form1042SAmount(Form1042SAmountKind.AMOUNT, Decimal(value))


def _copy(**changes: object) -> Form1042SCopy:
    copy = Form1042SCopy(
        copy_type=Form1042SCopyType.B,
        unique_form_identifier="1234567890",
        tax_year=2025,
        amended=False,
        amendment_number=None,
        income_code="06",
        gross_income=_amount("100.00"),
        chapter_indicator="3",
        chapter3_exemption_code="00",
        chapter3_tax_rate_percent=Decimal("25.00"),
        chapter4_exemption_code="15",
        chapter4_tax_rate_percent=Decimal("0.00"),
        withholding_allowance=_amount("0.00"),
        net_income=_amount("100.00"),
        federal_tax_withheld=_amount("25.00"),
        federal_tax_not_deposited=False,
        subsequent_year_withholding=False,
        qualified_intermediary=False,
        tax_withheld_other_agents=_amount("0.00"),
        overwithheld_tax_repaid=_amount("0.00"),
        total_withholding_credit=_amount("25.00"),
        tax_paid_by_agent=_amount("0.00"),
        pro_rata_reporting=False,
    )
    return replace(copy, **changes)


def test_clean_copy_has_no_violations() -> None:
    assert form_1042s_violations(_copy()) == ()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"gross_income": _BLANK}, (Form1042SViolation.GROSS_INCOME,)),
        ({"gross_income": _amount("100.50")}, (Form1042SViolation.GROSS_INCOME,)),
        (
            {"federal_tax_withheld": _BLANK},
            (
                Form1042SViolation.FEDERAL_TAX_WITHHELD,
                Form1042SViolation.CREDIT_DIFFERS_FROM_WITHHELD,
            ),
        ),
        (
            {
                "federal_tax_withheld": _amount("25.50"),
                "total_withholding_credit": _amount("25.50"),
            },
            (
                Form1042SViolation.FEDERAL_TAX_WITHHELD,
                Form1042SViolation.TOTAL_WITHHOLDING_CREDIT,
            ),
        ),
        (
            {"total_withholding_credit": _BLANK},
            (
                Form1042SViolation.TOTAL_WITHHOLDING_CREDIT,
                Form1042SViolation.CREDIT_DIFFERS_FROM_WITHHELD,
            ),
        ),
        (
            {"total_withholding_credit": _amount("26.00")},
            (Form1042SViolation.CREDIT_DIFFERS_FROM_WITHHELD,),
        ),
        ({"withholding_allowance": _amount("1.00")}, (Form1042SViolation.WITHHOLDING_ALLOWANCE,)),
        (
            {"tax_withheld_other_agents": _amount("1.00")},
            (Form1042SViolation.TAX_WITHHELD_OTHER_AGENTS,),
        ),
        (
            {"overwithheld_tax_repaid": _amount("1.00")},
            (Form1042SViolation.OVERWITHHELD_TAX_REPAID,),
        ),
        ({"tax_paid_by_agent": _amount("1.00")}, (Form1042SViolation.TAX_PAID_BY_AGENT,)),
        ({"chapter4_tax_rate_percent": Decimal("1.00")}, (Form1042SViolation.CHAPTER4_TAX_RATE,)),
        ({"chapter4_tax_rate_percent": Decimal("sNaN")}, (Form1042SViolation.CHAPTER4_TAX_RATE,)),
        ({"amended": True}, (Form1042SViolation.AMENDED,)),
        ({"amendment_number": "1"}, (Form1042SViolation.AMENDED,)),
        ({"chapter_indicator": "4"}, (Form1042SViolation.CHAPTER_INDICATOR,)),
        ({"federal_tax_not_deposited": True}, (Form1042SViolation.FEDERAL_TAX_NOT_DEPOSITED,)),
        ({"subsequent_year_withholding": True}, (Form1042SViolation.SUBSEQUENT_YEAR_WITHHOLDING,)),
        ({"qualified_intermediary": True}, (Form1042SViolation.QUALIFIED_INTERMEDIARY,)),
        ({"pro_rata_reporting": True}, (Form1042SViolation.PRO_RATA_REPORTING,)),
    ],
)
def test_each_gate_check_reports_its_violation(
    changes: dict[str, object],
    expected: tuple[Form1042SViolation, ...],
) -> None:
    assert form_1042s_violations(_copy(**changes)) == expected


@pytest.mark.parametrize("amount", [_BLANK, _DASH_ZERO])
def test_blank_or_dash_zero_counts_as_zero_for_boxes_5_8_9_11(amount: Form1042SAmount) -> None:
    copy = _copy(
        withholding_allowance=amount,
        tax_withheld_other_agents=amount,
        overwithheld_tax_repaid=amount,
        tax_paid_by_agent=amount,
    )

    assert form_1042s_violations(copy) == ()


def test_fields_the_parser_did_not_read_are_skipped() -> None:
    copy = _copy(
        chapter_indicator=None,
        federal_tax_not_deposited=None,
        subsequent_year_withholding=None,
        qualified_intermediary=None,
        pro_rata_reporting=None,
    )

    assert form_1042s_violations(copy) == ()


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        ((), 0),
        (("0.00",), 0),
        (("1.49",), 1),
        (("1.50",), 2),
        (("2.50",), 3),
        (("0.24", "0.25"), 0),
        (("0.25", "0.25"), 1),
        (("0.01",) * 49, 0),
        (("0.01",) * 50, 1),
        (("0.07",) * 21, 1),
        (("1", "1.5", "1E+2"), 103),
        (("99999999999999.99",), 100_000_000_000_000),
    ],
)
def test_whole_dollars_sums_exact_cents_then_rounds_half_up_once(
    amounts: tuple[str, ...],
    expected: int,
) -> None:
    assert whole_dollars(Decimal(amount) for amount in amounts) == expected


@pytest.mark.parametrize(
    "amount",
    ["1.000", "0.001", "-0.01", "-0", "NaN", "sNaN", "Infinity", "100000000000000.00"],
)
def test_whole_dollars_refuses_over_precise_negative_or_unbounded_amounts(amount: str) -> None:
    with pytest.raises(Form1042SAmountError):
        whole_dollars((Decimal("1.00"), Decimal(amount)))


def test_whole_dollars_ignores_the_ambient_decimal_context() -> None:
    amounts = (Decimal("0.25"), Decimal("0.25"), Decimal("99999999999999.49"))
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_FLOOR
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert whole_dollars(amounts) == 100_000_000_000_000
