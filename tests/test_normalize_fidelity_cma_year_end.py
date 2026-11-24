"""Fidelity CMA monthly statements against the year-end report."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

import pytest

from ibkr_fa.normalize.fidelity_cma.form_1042s import (
    Cma1042SReconciliationError,
    reconcile_1042s,
)
from ibkr_fa.normalize.fidelity_cma.year_end import (
    CmaYearEndError,
    require_year_end_income_match,
    require_year_end_match,
    to_cents,
)
from ibkr_fa.parser.fidelity_cma import (
    CmaIncomeKind,
    CmaStatement,
    CmaYearEndIncomeSummary,
    CmaYearEndPosition,
    validate_statement_bundle,
)
from tests._fidelity_cma import (
    WITHHOLDING_CENTS_REFUSAL,
    YEAR_END_HOLDINGS_REFUSAL,
    YEAR_END_TOTALS_REFUSAL,
    YEAR_END_WITHHOLDING_SIGN_REFUSAL,
)
from tests._fidelity_cma_1042s_reconcile import _packet, _statements, _year_end


def _with_march(**changes: object) -> list[CmaStatement]:
    """The 1042-S fixture with its March statement (8 withheld dividends) changed."""
    statements = list(_statements())
    statements[2] = replace(statements[2], **changes)
    return statements


def test_matching_totals_pass_with_late_posted_interest() -> None:
    # 0.10 of the 1.50 interest is dated Dec 31 but sits in the January statement.
    require_year_end_income_match(_statements(), _year_end(), tax_year=2025)


@pytest.mark.parametrize("value", ["100000000000000.00", "0E+14"])
def test_to_cents_keeps_fidelity_cent_digit_bound(value: str) -> None:
    assert to_cents(Decimal("99999999999999.99"), "statement income") == 9_999_999_999_999_999
    with pytest.raises(CmaYearEndError) as caught:
        to_cents(Decimal(value), "statement income")
    assert str(caught.value) == "statement income is not a bounded whole-cent USD amount"


@pytest.mark.parametrize(
    ("field", "value"),
    [("dividends_usd", "10.49"), ("interest_usd", "1.49"), ("withholding_usd", "-2.01")],
)
def test_one_cent_difference_refuses(field: str, value: str) -> None:
    year_end = replace(_year_end(), **{field: Decimal(value)})
    with pytest.raises(CmaYearEndError, match=YEAR_END_TOTALS_REFUSAL):
        require_year_end_income_match(_statements(), year_end, tax_year=2025)


@pytest.mark.parametrize(
    ("first", "second"),
    [("2.25", "-0.25"), ("1.005", "0.995")],
    ids=["negative-row", "sub-cent-rows"],
)
def test_each_row_must_be_whole_cents_even_when_the_total_matches(first: str, second: str) -> None:
    income = list(_statements()[2].income_credits)
    income[0] = replace(income[0], amount_usd=Decimal(first))
    income[1] = replace(income[1], amount_usd=Decimal(second))
    with pytest.raises(CmaYearEndError, match="statement dividend is not a bounded whole-cent"):
        require_year_end_income_match(
            _with_march(income_credits=tuple(income)), _year_end(), tax_year=2025
        )


def test_positive_statement_withholding_refuses_even_when_magnitudes_match() -> None:
    withholdings = list(_statements()[2].withholdings)
    withholdings[0] = replace(withholdings[0], amount_usd=Decimal("0.25"))
    with pytest.raises(CmaYearEndError, match="statement withholding must be negative"):
        require_year_end_income_match(
            _with_march(withholdings=tuple(withholdings)), _year_end(), tax_year=2025
        )


def test_positive_year_end_withholding_refuses() -> None:
    year_end = replace(_year_end(), withholding_usd=Decimal("2.00"))
    with pytest.raises(CmaYearEndError, match=YEAR_END_WITHHOLDING_SIGN_REFUSAL):
        require_year_end_income_match(_statements(), year_end, tax_year=2025)


@pytest.mark.parametrize("target", ["statement", "year-end"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_withholding_keeps_typed_errors(target: str, value: str) -> None:
    statements = list(_statements())
    year_end = _year_end()
    if target == "statement":
        rows = list(statements[2].withholdings)
        rows[0] = replace(rows[0], amount_usd=Decimal(value))
        statements[2] = replace(statements[2], withholdings=tuple(rows))
    else:
        year_end = replace(year_end, withholding_usd=Decimal(value))

    with pytest.raises(CmaYearEndError, match=WITHHOLDING_CENTS_REFUSAL):
        require_year_end_income_match(statements, year_end, tax_year=2025)
    with pytest.raises(
        Cma1042SReconciliationError,
        match=r"^Fidelity CMA Form 1042-S reconciliation refused: .*withholding is not a bounded",
    ):
        reconcile_1042s(validate_statement_bundle(statements), year_end, _packet(), tax_year=2025)


def test_unknown_income_kind_refuses() -> None:
    income = list(_statements()[2].income_credits)
    income[0] = replace(income[0], kind=cast("CmaIncomeKind", "Return of Capital"))
    with pytest.raises(CmaYearEndError, match="unsupported CMA income classification"):
        require_year_end_income_match(
            _with_march(income_credits=tuple(income)), _year_end(), tax_year=2025
        )


def test_report_for_another_tax_year_refuses() -> None:
    with pytest.raises(CmaYearEndError, match="different tax year"):
        require_year_end_income_match(_statements(), _year_end(), tax_year=2024)


_POSITIONED = replace(
    _year_end(),
    position=CmaYearEndPosition(
        quantity=Decimal("100.000"),
        price_per_unit_usd=Decimal("1.0000"),
        market_value_usd=Decimal("100.00"),
        income_earned_usd=Decimal(0),
    ),
)


def test_year_end_match_checks_holdings_then_income() -> None:
    statements = _statements()
    require_year_end_match(statements, statements[11], _POSITIONED, calendar_year=2025)
    with pytest.raises(CmaYearEndError, match="totals do not match"):
        require_year_end_match(
            statements,
            statements[11],
            replace(_POSITIONED, interest_usd=Decimal("1.49")),
            calendar_year=2025,
        )


@pytest.mark.parametrize(
    ("field", "value", "interest"),
    [
        ("quantity", "100.001", None),
        ("price_per_unit_usd", "1.0001", None),
        ("market_value_usd", "100.01", None),
        ("quantity", "100.001", "1.49"),
    ],
    ids=["quantity", "price-per-unit", "market-value", "holdings-and-income"],
)
def test_year_end_match_refuses_holdings_that_differ_before_income(
    field: str, value: str, interest: str | None
) -> None:
    position = _POSITIONED.position
    assert position is not None
    year_end = replace(_POSITIONED, position=replace(position, **{field: Decimal(value)}))
    if interest is not None:
        year_end = replace(year_end, interest_usd=Decimal(interest))
    statements = _statements()
    with pytest.raises(CmaYearEndError, match=YEAR_END_HOLDINGS_REFUSAL):
        require_year_end_match(statements, statements[11], year_end, calendar_year=2025)


@pytest.mark.parametrize(
    ("year_end", "calendar_year"),
    [(None, 2025), (_year_end(), 2025), (_POSITIONED, 2024)],
    ids=["no-report", "no-holdings", "other-year"],
)
def test_year_end_match_refuses_missing_holdings_evidence(
    year_end: CmaYearEndIncomeSummary | None, calendar_year: int
) -> None:
    statements = _statements()
    with pytest.raises(CmaYearEndError, match="holdings evidence is missing"):
        require_year_end_match(
            statements,
            statements[11],
            year_end,
            calendar_year=calendar_year,
        )
