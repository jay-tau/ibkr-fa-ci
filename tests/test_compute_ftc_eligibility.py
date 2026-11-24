"""Final eligible US tax: refund-status gate and per-date allocation rules."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.ftc_eligibility import (
    FtcEligibilityError,
    UsWithholdingEligibility,
    eligible_us_withholding,
)
from ibkr_fa.config import (
    FidelityCmaConfig,
    FidelityCmaEligibleTax,
    FidelityCmaRefundStatus,
    FidelityCmaUsTaxYearConfig,
)
from ibkr_fa.models import WithholdingTax
from ibkr_fa.types import Isin, Ticker

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)


def _actual(day: date, amount: str = "-0.25", *, currency: str = "USD") -> WithholdingTax:
    return WithholdingTax(
        symbol=Ticker("XQCN"),
        isin=Isin("US99971W1062"),
        payment_date=day,
        amount=Decimal(amount),
        currency=currency,
        description="SAMPLE ISSUER CN Non-Resident Tax",
    )


# ADR 0004 section 6: the statuses that can claim FTC, and those that block it.
_FTC_STATUSES = [FidelityCmaRefundStatus.FINALIZED, FidelityCmaRefundStatus.NO_REFUND_CLAIM]
_BLOCKING_STATUSES = [
    FidelityCmaRefundStatus.UNKNOWN,
    FidelityCmaRefundStatus.NOT_FILED_PLANNED,
    FidelityCmaRefundStatus.FILED_PENDING,
]


def _allocated(
    tax_year: int,
    eligible: dict[date, str],
    status: FidelityCmaRefundStatus = FidelityCmaRefundStatus.FINALIZED,
) -> FidelityCmaUsTaxYearConfig:
    return FidelityCmaUsTaxYearConfig(
        tax_year=tax_year,
        refund_status=status,
        eligible_tax=tuple(
            FidelityCmaEligibleTax(payment_date=day, amount_usd=Decimal(amount))
            for day, amount in eligible.items()
        ),
        provenance="final IRS transcript",
    )


def _eligibility(
    actual: list[WithholdingTax],
    *years: FidelityCmaUsTaxYearConfig,
    reconciled: frozenset[int] = frozenset({2025}),
) -> UsWithholdingEligibility:
    config = FidelityCmaConfig(
        account_id="Z99-999999",
        spaxx_isin=Isin("US99971W1062"),
        us_tax_years=years,
    )
    return eligible_us_withholding(
        actual,
        window_start=_FY_START,
        window_end=_FY_END,
        us_tax_year=config.us_tax_year,
        reconciled_tax_years=reconciled,
    )


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_year_emits_only_positive_eligible_tax(
    status: FidelityCmaRefundStatus,
) -> None:
    result = _eligibility(
        [_actual(date(2025, 4, 15)), _actual(date(2025, 6, 15), "-0.30")],
        _allocated(2025, {date(2025, 4, 15): "0.10", date(2025, 6, 15): "0.00"}, status),
    )
    assert result.blocked_tax_years == ()
    assert result.eligible == (
        WithholdingTax(
            symbol=Ticker("XQCN"),
            isin=Isin("US99971W1062"),
            payment_date=date(2025, 4, 15),
            amount=Decimal("-0.10"),
            currency="USD",
            description="XQCN(US99971W1062) Final eligible US tax",
        ),
    )


def test_year_missing_from_config_blocks_as_unknown() -> None:
    result = _eligibility([_actual(date(2025, 4, 15))])
    assert result == UsWithholdingEligibility(
        eligible=(),
        blocked_tax_years=((2025, FidelityCmaRefundStatus.UNKNOWN),),
    )


def test_every_status_either_claims_or_blocks() -> None:
    assert {*_FTC_STATUSES, *_BLOCKING_STATUSES} == set(FidelityCmaRefundStatus)


@pytest.mark.parametrize("status", _BLOCKING_STATUSES)
def test_blocking_status_blocks(status: FidelityCmaRefundStatus) -> None:
    result = _eligibility(
        [_actual(date(2025, 4, 15))],
        FidelityCmaUsTaxYearConfig(tax_year=2025, refund_status=status),
    )
    assert result.eligible == ()
    assert result.blocked_tax_years == ((2025, status),)


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_but_unreconciled_year_blocks(status: FidelityCmaRefundStatus) -> None:
    result = _eligibility(
        [_actual(date(2025, 4, 15))],
        _allocated(2025, {date(2025, 4, 15): "0.10"}, status),
        reconciled=frozenset(),
    )
    assert result.eligible == ()
    assert result.blocked_tax_years == ((2025, status),)


def test_window_spanning_two_ftc_status_years_claims_both() -> None:
    result = _eligibility(
        [_actual(date(2025, 4, 15)), _actual(date(2026, 1, 15), "-0.05")],
        _allocated(2025, {date(2025, 4, 15): "0.10"}),
        _allocated(2026, {date(2026, 1, 15): "0.05"}, FidelityCmaRefundStatus.NO_REFUND_CLAIM),
        reconciled=frozenset({2025, 2026}),
    )
    assert result.blocked_tax_years == ()
    assert [(row.payment_date, row.amount) for row in result.eligible] == [
        (date(2025, 4, 15), Decimal("-0.10")),
        (date(2026, 1, 15), Decimal("-0.05")),
    ]


@pytest.mark.parametrize("status", _FTC_STATUSES)
@pytest.mark.parametrize("blocking", _BLOCKING_STATUSES)
def test_one_blocked_us_year_blocks_every_row_of_the_window(
    status: FidelityCmaRefundStatus,
    blocking: FidelityCmaRefundStatus,
) -> None:
    result = _eligibility(
        [_actual(date(2025, 4, 15)), _actual(date(2026, 1, 15), "-0.05")],
        _allocated(2025, {date(2025, 4, 15): "0.10"}, status),
        FidelityCmaUsTaxYearConfig(tax_year=2026, refund_status=blocking),
        reconciled=frozenset({2025, 2026}),
    )
    assert result.eligible == ()
    assert result.blocked_tax_years == ((2026, blocking),)


def test_blocked_window_returns_before_allocation_checks() -> None:
    # The duplicate date would refuse if the window were not blocked.
    result = _eligibility([_actual(date(2025, 4, 15)), _actual(date(2025, 4, 15))])
    assert result.eligible == ()
    assert result.blocked_tax_years == ((2025, FidelityCmaRefundStatus.UNKNOWN),)


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_duplicate_date_anywhere_in_bundle_refuses(status: FidelityCmaRefundStatus) -> None:
    with pytest.raises(FtcEligibilityError, match="duplicate withholding date"):
        _eligibility(
            [_actual(date(2024, 12, 15)), _actual(date(2024, 12, 15)), _actual(date(2025, 4, 15))],
            _allocated(2025, {date(2025, 4, 15): "0.10"}, status),
        )


@pytest.mark.parametrize(
    ("actual", "eligible"),
    [
        ([date(2025, 4, 15)], {date(2025, 4, 15): "0.10", date(2025, 5, 15): "0.00"}),
        ([date(2025, 4, 15), date(2025, 5, 15)], {date(2025, 4, 15): "0.10"}),
        # A same-year date before the window still needs an allocation.
        ([date(2025, 2, 15), date(2025, 4, 15)], {date(2025, 4, 15): "0.10"}),
    ],
    ids=["extra-configured-date", "missing-configured-date", "pre-window-date"],
)
@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_configured_dates_must_match_the_years_actual_dates(
    actual: list[date],
    eligible: dict[date, str],
    status: FidelityCmaRefundStatus,
) -> None:
    with pytest.raises(
        FtcEligibilityError,
        match=r"^eligible-tax dates must match every statement withholding date$",
    ):
        _eligibility([_actual(day) for day in actual], _allocated(2025, eligible, status))


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_eligible_tax_above_actual_refuses(status: FidelityCmaRefundStatus) -> None:
    with pytest.raises(
        FtcEligibilityError,
        match=r"^eligible tax cannot exceed actual statement withholding$",
    ):
        _eligibility(
            [_actual(date(2025, 4, 15))],
            _allocated(2025, {date(2025, 4, 15): "0.26"}, status),
        )


@pytest.mark.parametrize(
    "row",
    [_actual(date(2025, 4, 15), "0.25"), _actual(date(2025, 4, 15), currency="EUR")],
    ids=["refund", "non-usd"],
)
def test_non_negative_or_non_usd_actual_refuses(row: WithholdingTax) -> None:
    with pytest.raises(FtcEligibilityError, match="negative USD"):
        _eligibility([row], _allocated(2025, {date(2025, 4, 15): "0.10"}))


def test_eligible_tax_past_28_digits_keeps_every_digit() -> None:
    """#1312: the eligible row is the configured tax negated with every digit.
    Main negated it with unary minus, rounding it to -1000000000 at 28 digits."""
    day = date(2025, 6, 16)
    result = _eligibility(
        [_actual(day, "-1000000000.00")],
        _allocated(2025, {day: "999999999.99999999999999999999"}),
    )
    assert result.blocked_tax_years == ()
    [row] = result.eligible
    assert row.payment_date == day
    assert row.amount.compare_total(Decimal("-999999999.99999999999999999999")) == 0
