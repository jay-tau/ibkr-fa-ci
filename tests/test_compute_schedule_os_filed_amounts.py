"""Schedule OS filed amounts and the CBDT 14-digit bound (#830).

Each line's INR amount isn't filed by itself, but every filed Schedule OS total
adds it up, so it is bounded when the line is built. The totals are bounded in
:func:`~ibkr_fa.compute.income_engine.compute_schedule_os_result`, before any
writer reads them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.income_engine import compute_schedule_os_result
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine, ScheduleOsDomesticDividendLine
from ibkr_fa.compute.schedule_os_interest import ScheduleOsInterestLine
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.compute.schedule_os_substitute_dividend import ScheduleOsSubstituteDividendLine
from ibkr_fa.config import load_user_config
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models import (
    DistributionKind,
    Dividend,
    InterestCredit,
    OtherSourceIncome,
    SubstituteDividendIncome,
)
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._os_dividend_inputs import make_div

_FY = FinancialYear(2025)
_PAID = date(2025, 6, 15)
_SPECIFIED = date(2025, 5, 31)
# At any rate between ₹80 and ₹95 per USD, each line converts within the bound
# and two lines add up past it.
_LARGE_USD = "650000000000"


def _dividend_line(amount: Decimal) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker("XQAA"),
        isin=Isin("US99990A3095"),
        issuer_country_iso2="US",
        payment_date=_PAID,
        fx_specified_date=_SPECIFIED,
        fx_rate_publication_date=_SPECIFIED,
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("1"),
        amount_gross_inr=amount,
        description="XQAA(US99990A3095) Cash Dividend USD 0.35 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _domestic_dividend_line(amount: Decimal) -> ScheduleOsDomesticDividendLine:
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_iso2="IN",
        payment_date=_PAID,
        fx_specified_date=_SPECIFIED,
        fx_rate_publication_date=_SPECIFIED,
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("1"),
        amount_gross_inr=amount,
        description="XQCD(IN99970W1077) Cash Dividend USD 0.10 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _interest_line(amount: Decimal) -> ScheduleOsInterestLine:
    return ScheduleOsInterestLine(
        credit_date=_PAID,
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("1"),
        amount_gross_inr=amount,
        description="USD Credit Interest for Jun-2025",
    )


def _other_income_line(amount: Decimal) -> ScheduleOsOtherIncomeLine:
    return ScheduleOsOtherIncomeLine(
        nature="Securities lending fee",
        payment_date=_PAID,
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("1"),
        amount_gross_inr=amount,
        description="Stock Lending",
        source_row_index=1,
    )


def _substitute_dividend_line(amount: Decimal) -> ScheduleOsSubstituteDividendLine:
    return ScheduleOsSubstituteDividendLine(
        symbol=Ticker("TEST"),
        isin=Isin("BM99970C1019"),
        issuer_country_iso2=CountryCode("BM"),
        source_country_iso2=CountryCode("US"),
        payment_date=_PAID,
        fx_specified_date=date(2026, 3, 31),
        fx_rate_publication_date=date(2026, 3, 31),
        fx_rate_inr_per_usd=Decimal("85"),
        amount_gross_usd=Decimal("1"),
        amount_gross_inr=amount,
        description="synthetic substitute dividend",
    )


_Line = (
    ScheduleOsDividendLine
    | ScheduleOsDomesticDividendLine
    | ScheduleOsInterestLine
    | ScheduleOsOtherIncomeLine
    | ScheduleOsSubstituteDividendLine
)
# Each line, its model, and the sign its amount may take: other income may be negative.
_LINES: list[tuple[Callable[[Decimal], _Line], type[_Line], str]] = [
    (_dividend_line, ScheduleOsDividendLine, ""),
    (_domestic_dividend_line, ScheduleOsDomesticDividendLine, ""),
    (_interest_line, ScheduleOsInterestLine, ""),
    (_other_income_line, ScheduleOsOtherIncomeLine, ""),
    (_other_income_line, ScheduleOsOtherIncomeLine, "-"),
    (_substitute_dividend_line, ScheduleOsSubstituteDividendLine, ""),
]


@pytest.mark.parametrize(("build", "model", "sign"), _LINES)
@pytest.mark.parametrize("value", ["99999999999999", "99999999999999.4"])
def test_line_amount_rounding_within_the_cbdt_bound_accepted(
    build: Callable[[Decimal], _Line], model: type[_Line], sign: str, value: str
) -> None:
    amount = Decimal(sign + value)
    line = build(amount)
    assert type(line) is model
    assert line.amount_gross_inr == amount


@pytest.mark.parametrize(("build", "model", "sign"), _LINES)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_line_amount_rounding_beyond_the_cbdt_bound_rejected(
    build: Callable[[Decimal], _Line], model: type[_Line], sign: str, value: str
) -> None:
    with pytest.raises(InvariantError) as excinfo:
        build(Decimal(sign + value))

    assert excinfo.value.what.startswith(
        f"{model.__name__}.amount_gross_inr must round to a whole-rupee amount"
    )


# ---- filed totals ------------------------------------------------------------


def _compute(
    tmp_path: Path,
    *,
    dividends: Iterable[Dividend] = (),
    interest_credits: Iterable[InterestCredit] = (),
    substitute_dividend_incomes: Iterable[SubstituteDividendIncome] = (),
    other_source_incomes: Iterable[OtherSourceIncome] = (),
) -> None:
    config_path = tmp_path / "user.toml"
    config_path.write_text('[account]\nholder_name = "Tax Payer"\n', encoding="utf-8")
    config = load_user_config(config_path)
    compute_schedule_os_result(
        config,
        _FY,
        fx_ctx=TaxFxContext.for_fy(_FY, config.fx),
        dividends=dividends,
        interest_credits=interest_credits,
        substitute_dividend_incomes=substitute_dividend_incomes,
        other_source_incomes=other_source_incomes,
    )


def _interest_credit() -> InterestCredit:
    return InterestCredit(
        credit_date=_PAID,
        amount_gross=Decimal(_LARGE_USD),
        currency="USD",
        description="USD Credit Interest for Jun-2025",
    )


def _substitute_dividend() -> SubstituteDividendIncome:
    return SubstituteDividendIncome(
        symbol=Ticker("TEST"),
        isin=Isin("BM99970C1019"),
        issuer_country_iso2=CountryCode("BM"),
        source_country_iso2=CountryCode("US"),
        payment_date=_PAID,
        amount_gross=Decimal(_LARGE_USD),
        currency="USD",
        description="synthetic substitute dividend",
    )


def _substitute_payment_as_other_income() -> OtherSourceIncome:
    return OtherSourceIncome(
        nature="Substitute payment in lieu of dividend",
        payment_date=_PAID,
        amount_gross=Decimal(_LARGE_USD),
        currency="USD",
        description="Payment in lieu of dividend",
        source_row_index=1,
        source_country_iso2=CountryCode("US"),
    )


@pytest.mark.parametrize(
    ("incomes", "total"),
    [
        pytest.param(
            {"dividends": [make_div(amount=_LARGE_USD), make_div(amount=_LARGE_USD)]},
            "Schedule OS 1a(i) dividend total",
            id="foreign-dividends",
        ),
        pytest.param(
            {
                "dividends": [
                    make_div(symbol="XQCD", isin="IN99970W1077", amount=_LARGE_USD),
                    make_div(symbol="XQCD", isin="IN99970W1077", amount=_LARGE_USD),
                ]
            },
            "Schedule OS 1a(i) dividend total",
            id="domestic-dividends",
        ),
        pytest.param(
            {"interest_credits": [_interest_credit(), _interest_credit()]},
            "Schedule OS 1b(ix) interest total",
            id="interest",
        ),
        pytest.param(
            {
                "substitute_dividend_incomes": [_substitute_dividend()],
                "other_source_incomes": [_substitute_payment_as_other_income()],
            },
            "Schedule OS 1e other-income total",
            id="other-income-nature",
        ),
    ],
)
def test_filed_total_beyond_the_cbdt_bound_refuses(
    tmp_path: Path, incomes: dict[str, Any], total: str
) -> None:
    with pytest.raises(InvariantError) as excinfo:
        _compute(tmp_path, **incomes)

    assert excinfo.value.what.startswith(f"{total} must round to a whole-rupee amount")
