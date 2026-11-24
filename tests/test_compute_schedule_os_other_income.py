"""Schedule OS row 1e residual other-income checks."""

from dataclasses import fields
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.schedule_os_other_income import (
    ScheduleOsOtherIncomeComputeResult,
    compute_schedule_os_other_income_lines,
    grouped_other_income_totals,
)
from ibkr_fa.models import OtherSourceIncome
from ibkr_fa.types import CountryCode
from tests._fx_stubs import AuditedFxStub


def test_other_income_uses_exact_fy_end_rate_and_groups_by_nature() -> None:
    result = compute_schedule_os_other_income_lines(
        (
            OtherSourceIncome(
                nature="Securities lending fee",
                payment_date=date(2025, 6, 1),
                amount_gross=Decimal("0.11"),
                currency="USD",
                description="Stock Lending",
                source_row_index=1,
                source_country_iso2=CountryCode("US"),
            ),
            OtherSourceIncome(
                nature="Securities lending fee",
                payment_date=date(2026, 3, 1),
                amount_gross=Decimal("0.12"),
                currency="USD",
                description="Stock Lending",
                source_row_index=2,
                source_country_iso2=CountryCode("US"),
            ),
        ),
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=AuditedFxStub(Decimal("80"), specified_date=date(2026, 3, 31)),
    )

    assert [line.fx_specified_date for line in result.lines] == [
        date(2026, 3, 31),
        date(2026, 3, 31),
    ]
    assert [line.amount_gross_inr for line in result.lines] == [
        Decimal("8.80"),
        Decimal("9.60"),
    ]
    assert {line.source_country_iso2 for line in result.lines} == {"US"}
    assert grouped_other_income_totals(result.lines) == (
        (
            "Securities lending fee",
            Decimal("18.40"),
            Decimal("18"),
        ),
    )


def _lending_fee(payment_date: date, source_row_index: int) -> OtherSourceIncome:
    return OtherSourceIncome(
        nature="Securities lending fee",
        payment_date=payment_date,
        amount_gross=Decimal("0.11"),
        currency="USD",
        description="Stock Lending",
        source_row_index=source_row_index,
        source_country_iso2=CountryCode("US"),
    )


def test_other_income_result_carries_only_its_lines() -> None:
    """Nothing read the out-of-FY count, so the result no longer has one (#1141)."""
    assert {field.name for field in fields(ScheduleOsOtherIncomeComputeResult)} == {"lines"}


def test_other_income_keeps_boundary_receipts_and_drops_out_of_fy_ones() -> None:
    # The stub has rates for the two in-FY dates only: a rate request for
    # either out-of-FY date would raise KeyError.
    rates = {date(2025, 4, 1): Decimal("80"), date(2026, 3, 31): Decimal("80")}
    result = compute_schedule_os_other_income_lines(
        (
            _lending_fee(date(2025, 3, 31), 1),
            _lending_fee(date(2025, 4, 1), 2),
            _lending_fee(date(2026, 3, 31), 3),
            _lending_fee(date(2026, 4, 1), 4),
        ),
        financial_year_start=date(2025, 4, 1),
        financial_year_end=date(2026, 3, 31),
        fx_policy=AuditedFxStub(rates, specified_date=date(2026, 3, 31)),
    )

    assert [(line.source_row_index, line.amount_gross_inr) for line in result.lines] == [
        (2, Decimal("8.80")),
        (3, Decimal("8.80")),
    ]
    assert grouped_other_income_totals(result.lines) == (
        ("Securities lending fee", Decimal("17.60"), Decimal("18")),
    )
