"""The Form 67 compute tests' inputs: withholding rows, Schedule OS dividend
lines, the default FX rate, the unproven-rate and refund cases, and an upload
entry."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.form67_entry import (
    Form67Entry,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Isin, Ticker

FY_START = date(2025, 4, 1)
FY_END = date(2026, 3, 31)
DEFAULT_ISIN_US: Isin = Isin("US99993A1088")
DEFAULT_TICKER_XQBN: Ticker = Ticker("XQBN")
DEFAULT_PAYMENT_DATE = date(2025, 12, 15)
# The default dividend line's INR-per-USD rate; tests that need no other rate
# convert the withholding at it too.
FORM67_FX_RATE = Decimal("83.3333")


def withholding_row(
    *,
    isin: Isin = DEFAULT_ISIN_US,
    payment_date: date = DEFAULT_PAYMENT_DATE,
    amount: Decimal = Decimal("-1.80"),
    currency: str = "USD",
    description: str = "XQBN(US99993A1088) Cash Dividend - US Tax",
    symbol: Ticker = DEFAULT_TICKER_XQBN,
) -> WithholdingTax:
    return WithholdingTax(
        symbol=symbol,
        isin=isin,
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        description=description,
    )


def os_dividend_line(
    *,
    isin: Isin = DEFAULT_ISIN_US,
    payment_date: date = DEFAULT_PAYMENT_DATE,
    amount_gross_usd: Decimal = Decimal("12.00"),
    amount_gross_inr: Decimal = Decimal("1000.00"),
    issuer_country_iso2: str = "US",
    fx_rate_inr_per_usd: Decimal = FORM67_FX_RATE,
    description: str = "XQBN(US99993A1088) Cash Dividend (Ordinary Dividend)",
    symbol: Ticker = DEFAULT_TICKER_XQBN,
) -> ScheduleOsDividendLine:
    first_of_month = date(payment_date.year, payment_date.month, 1)
    preceding_month_end = first_of_month - timedelta(days=1)
    return ScheduleOsDividendLine(
        symbol=symbol,
        isin=isin,
        issuer_country_iso2=issuer_country_iso2,
        payment_date=payment_date,
        fx_specified_date=preceding_month_end,
        fx_rate_publication_date=preceding_month_end,
        fx_rate_inr_per_usd=fx_rate_inr_per_usd,
        amount_gross_usd=amount_gross_usd,
        amount_gross_inr=amount_gross_inr,
        description=description,
        kind=DistributionKind.CASH_DIVIDEND,
    )


FEB_12 = date(2026, 2, 12)
MAR_12 = date(2026, 3, 12)

# The two ways a DTAA entry has positive INR tax and income but no
# source-currency rate (#767): (WHT rows, OS line, WHT FX rates).
UNPROVEN_SOURCE_RATE_CASES = {
    # USD 25 withheld at ₹90 and refunded at ₹80 nets to USD 0 but ₹250.
    "same-year-refund": (
        [
            withholding_row(payment_date=FEB_12, amount=Decimal("-25.00")),
            withholding_row(payment_date=MAR_12, amount=Decimal("25.00")),
        ],
        os_dividend_line(
            payment_date=FEB_12,
            amount_gross_usd=Decimal("100.00"),
            amount_gross_inr=Decimal("9000.00"),
            fx_rate_inr_per_usd=Decimal("90"),
        ),
        {FEB_12: Decimal("90"), MAR_12: Decimal("80")},
    ),
    # USD 10.05 tax on USD 10.00 income is 100.5%, but ₹804 on ₹900 is 89.3%.
    "source-tax-above-income": (
        [withholding_row(payment_date=MAR_12, amount=Decimal("-10.05"))],
        os_dividend_line(
            payment_date=MAR_12,
            amount_gross_usd=Decimal("10.00"),
            amount_gross_inr=Decimal("900.00"),
            fx_rate_inr_per_usd=Decimal("90"),
        ),
        {MAR_12: Decimal("80")},
    ),
}

# ---- the per-payment treaty-rate test (#890) -----------------------------

JUN_12 = date(2025, 6, 12)
JUN_RATE = Decimal("85.10")  # the 2025-05-31 TTBR, for both Rule 128 and Rule 115
E9_TAX = Decimal("2.75")
E9_GROSS = Decimal("9.12")
# #405 E9a: USD 2.75 withheld on a USD 9.12 dividend, about 30.15% of it.
E9_WHT = withholding_row(payment_date=JUN_12, amount=-E9_TAX)
E9A_LINE = os_dividend_line(
    payment_date=JUN_12,
    amount_gross_usd=E9_GROSS,
    amount_gross_inr=E9_GROSS * JUN_RATE,
    fx_rate_inr_per_usd=JUN_RATE,
)

_SEP_11 = date(2025, 9, 11)
REFUND_BESIDE_ABOVE_RATE_PAYMENTS = {
    # A later-month refund of one of the payment's two rows takes net tax below
    # 25%; paid tax is still about 30.15%.
    "refund-of-one-row-of-above-rate-payment": (
        [
            withholding_row(payment_date=JUN_12, amount=Decimal("-2.25")),
            withholding_row(payment_date=JUN_12, amount=Decimal("-0.50")),
            withholding_row(payment_date=date(2025, 8, 14), amount=Decimal("0.50")),
        ],
        [E9A_LINE],
        {JUN_12: JUN_RATE, date(2025, 8, 14): Decimal("86.00")},
    ),
    # One refund that either of two above-rate payments could be the source of.
    "refund-with-two-above-rate-payments": (
        [
            E9_WHT,
            withholding_row(payment_date=_SEP_11, amount=-E9_TAX),
            withholding_row(payment_date=date(2025, 10, 15), amount=E9_TAX),
        ],
        [E9A_LINE, replace(E9A_LINE, payment_date=_SEP_11)],
        {JUN_12: JUN_RATE, _SEP_11: Decimal("87.00"), date(2025, 10, 15): Decimal("88.00")},
    ),
}


# ---- the Form 67 upload (#1203) -------------------------------------------


def upload_entry(
    *,
    source: Form67SourceOfIncome = Form67SourceOfIncome.DIVIDEND,
    country_code: str = "2",
    income: Decimal = Decimal("10000.50"),
    tax: Decimal = Decimal("1500.75"),
    rate: Decimal | None = None,
    article: str | None = "Article 10(2)(b)",
    dtaa_rate: Decimal | None = Decimal("25"),
    normal_tax: Decimal | None = Decimal("3000"),
    special_tax: Decimal | None = None,
    section_90_credit: Decimal | None = Decimal("1500"),
    section_91_credit: Decimal | None = None,
    total_credit: Decimal | None = Decimal("1500"),
) -> Form67Entry:
    inr_ratio = rate if rate is not None else tax / income * 100
    # A DTAA entry has a source rate; here it is the INR ratio.
    treaty_rate_status = None
    if dtaa_rate is not None:
        treaty_rate_status = (
            Form67TreatyRateStatus.WITHIN
            if inr_ratio <= dtaa_rate
            else Form67TreatyRateStatus.ABOVE
        )
    return Form67Entry(
        serial_number=1,
        cbdt_country_code=country_code,
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=source,
        income_inr=income,
        foreign_tax_inr=tax,
        foreign_tax_rate_percent=inr_ratio,
        event_count=1,
        tax_payable_normal_inr=normal_tax,
        tax_payable_special_inr=special_tax,
        section_90_dtaa_article=article,
        section_90_dtaa_rate_percent=dtaa_rate,
        tax_paid_rate_percent=None if dtaa_rate is None else inr_ratio,
        highest_payment_rate_percent=None if dtaa_rate is None else inr_ratio,
        treaty_rate_status=treaty_rate_status,
        eligible_foreign_tax_inr=tax if dtaa_rate is not None else None,
        section_90_credit_inr=section_90_credit,
        section_91_credit_inr=section_91_credit,
        total_credit_inr=total_credit,
    )
