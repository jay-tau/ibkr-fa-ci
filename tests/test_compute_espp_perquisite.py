"""Tests for the ESPP perquisite Form-16 advisory compute (#I12 / #317)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.compute.espp_perquisite import (
    build_espp_perquisite_advisory,
    financial_year_label,
)
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase


def _purchase(
    day: date,
    *,
    fmv: str = "743.30",
    contrib: str = "668.96",
    perquisite: str = "74.34",
    shares: str = "3.482",
    price: str = "192.12",
) -> EspPurchase:
    return EspPurchase(
        purchase_date=day,
        settlement_date=day,
        offering_period_start=date(day.year, 1, 1),
        offering_period_end=day,
        symbol="XESP",
        security_description="EXAMPLE CORP",
        cusip="99999E104",
        participant_no="I99999999",
        shares=Decimal(shares),
        purchase_price_per_share_usd=Decimal(price),
        fmv_total_usd=Decimal(fmv),
        contribution_usd=Decimal(contrib),
        perquisite_usd=Decimal(perquisite),
    )


def test_financial_year_label_april_boundary() -> None:
    assert financial_year_label(date(2025, 12, 31)) == "2025-26"
    assert financial_year_label(date(2026, 3, 31)) == "2025-26"
    assert financial_year_label(date(2026, 4, 1)) == "2026-27"
    assert financial_year_label(date(2025, 3, 31)) == "2024-25"


def test_build_advisory_converts_inr_half_up() -> None:
    adv = build_espp_perquisite_advisory(
        _purchase(date(2025, 12, 31)), perquisite_inr_rate=Decimal("88.95")
    )
    # 74.34 * 88.95 = 6612.5430 -> 6613
    assert adv.perquisite_inr == 6613
    assert adv.perquisite_usd == Decimal("74.34")
    assert adv.fmv_total_usd == Decimal("743.30")
    assert adv.contribution_usd == Decimal("668.96")
    assert adv.financial_year == "2025-26"
    assert adv.purchase_date == date(2025, 12, 31)


def test_build_advisory_rounds_half_up_not_bankers() -> None:
    # 1.00 * 10.5 = 10.5 -> 11 under ROUND_HALF_UP (banker's rounding would give 10).
    adv = build_espp_perquisite_advisory(
        _purchase(
            date(2025, 12, 31),
            fmv="10.00",
            contrib="9.00",
            perquisite="1.00",
            shares="1.000",
            price="9.0000",
        ),
        perquisite_inr_rate=Decimal("10.5"),
    )
    assert adv.perquisite_inr == 11


def test_build_advisory_no_rate_leaves_inr_none() -> None:
    adv = build_espp_perquisite_advisory(
        _purchase(
            date(2026, 3, 31),
            fmv="839.23",
            contrib="755.31",
            perquisite="83.92",
            shares="4.6315",
            price="163.08",
        ),
        perquisite_inr_rate=None,
    )
    assert adv.perquisite_inr is None
    assert adv.perquisite_usd == Decimal("83.92")
    assert adv.financial_year == "2025-26"
