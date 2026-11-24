"""Tests for :mod:`ibkr_fa.compute.fa_a2`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Literal

import pytest

from ibkr_fa.compute.fa_a2 import (
    FaA2AttestedPeak,
    FaA2GrossEvidence,
    FaA2ManualFillGaps,
    FaA2PeakBounds,
    FaA2StatusError,
    build_fa_a2_row,
    build_gross_evidence,
    expand_fa_a2_nature_rows,
    fa_a2_closing_ttbr,
    fa_a2_manual_fill_gaps,
    gross_interest_inr,
)
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import (
    ClosedLot,
    Dividend,
    InterestCredit,
    InterestNonCredit,
    InterestRowGroup,
)
from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity, TaxpayerStatus
from ibkr_fa.models.fa_a2_row import FaA2ClosingBlankReason, FaA2NatureOfAmount, FaA2Status
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.net_asset_value import NavRefusalReason, NetAssetValueSummary
from ibkr_fa.types import Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fa_a2_peak import without_peak
from tests._fa_lot_inputs import fa_closed_lot as _closed_lot
from tests._fx_stubs import AuditedFxStub

_IDENTITY = AccountIdentity(
    institution=InstitutionIdentity(
        name="Interactive Brokers LLC",
        address="One Pickwick Plaza, Greenwich, CT",
        zip_code="06830",
        country_iso2="US",
    ),
    account_ref="U12345678",
    opening_date=date(2024, 1, 1),
    status=TaxpayerStatus.BENEFICIAL_OWNER,
    base_currency="USD",
    statement_masked_account_ref=True,
)
_NAV_OK = NetAssetValueSummary(
    closing_total=Decimal("123.718346"),
    refusal_reason=None,
    cross_checked=True,
)


def _build(**overrides: object):
    kwargs: dict[str, object] = {
        "identity": _IDENTITY,
        "nav": _NAV_OK,
        "closing_ttbr_dec31": Decimal("89.47"),
        "gross_components_inr": {},
    }
    kwargs.update(overrides)
    return build_fa_a2_row(**kwargs)  # ty: ignore[invalid-argument-type]


def test_cross_checked_nav_happy_path() -> None:
    row = _build()
    assert row.country_code == "2"
    assert row.institution_name == "Interactive Brokers LLC"
    assert row.institution_address == "One Pickwick Plaza, Greenwich, CT"
    assert row.institution_zip == "06830"
    assert row.account_number == "U12345678"
    assert row.account_opening_date == date(2024, 1, 1)
    assert row.base_currency == "USD"
    assert row.audit_account_number_from_config is True
    assert row.status is FaA2Status.BENEFICIAL_OWNER
    assert row.closing_balance_inr == Decimal("123.718346") * Decimal("89.47")  # 11069.08041662
    assert row.peak_balance_inr is None
    assert row.nature_of_amount is FaA2NatureOfAmount.NO_AMOUNT
    assert row.gross_amount_inr == Decimal("0")
    assert row.audit_nav_cross_checked is True


def test_status_joint_maps_to_owner() -> None:
    identity = replace(_IDENTITY, status=TaxpayerStatus.JOINT)
    assert _build(identity=identity).status is FaA2Status.OWNER


def test_status_signing_authority_refused() -> None:
    identity = replace(_IDENTITY, status=TaxpayerStatus.SIGNING_AUTHORITY)
    with pytest.raises(FaA2StatusError) as excinfo:
        _build(identity=identity)
    assert excinfo.value.status is TaxpayerStatus.SIGNING_AUTHORITY
    assert str(excinfo.value) == (
        "status 'SigningAuthority': signing-authority accounts aren't supported; "
        "the supported statuses are BeneficialOwner and Joint"
    )


def test_unmasked_statement_uses_config_number_not_from_config_flag() -> None:
    row = _build(identity=replace(_IDENTITY, statement_masked_account_ref=False))
    assert row.account_number == "U12345678"
    assert row.audit_account_number_from_config is False


@pytest.mark.parametrize("cash", [None, Decimal("0"), Decimal("42.518346")])
def test_period_end_cash_is_passed_through(cash: Decimal | None) -> None:
    assert _build(period_end_cash_base_ccy=cash).audit_period_end_cash_base_ccy == cash


_BOUNDS = FaA2PeakBounds(
    lower_inr=Decimal("8000.10"),
    lower_date=date(2025, 3, 14),
    upper_inr=Decimal("8100.20"),
    upper_date=date(2025, 3, 17),
    selected=None,
)


def test_no_peak_input_leaves_peak_and_provenance_blank() -> None:
    row = _build(peak=None)
    assert row == _build()
    assert row == without_peak(row)


def test_unsupported_peak_input_is_refused() -> None:
    with pytest.raises(InvariantError, match="peak must be audited bounds"):
        _build(peak=object())


@pytest.mark.parametrize(
    ("selected", "filed"),
    [("lower", Decimal("8000.10")), ("upper", Decimal("8100.20")), (None, None)],
)
def test_peak_bounds_file_the_selected_bound_and_keep_both(
    selected: Literal["lower", "upper"] | None, filed: Decimal | None
) -> None:
    row = _build(peak=replace(_BOUNDS, selected=selected))
    assert row == replace(
        _build(),
        peak_balance_inr=filed,
        audit_peak_balance_lower_inr=Decimal("8000.10"),
        audit_peak_balance_upper_inr=Decimal("8100.20"),
        audit_peak_balance_lower_date=date(2025, 3, 14),
        audit_peak_balance_upper_date=date(2025, 3, 17),
        audit_peak_balance_bound=selected,
    )


def test_peak_bounds_carry_base_currency_peak_and_statement_floor() -> None:
    peak = replace(
        _BOUNDS,
        selected="lower",
        base_ccy=Decimal("90.5"),
        ttbr=Decimal("88.4"),
        statement_floor_base_ccy=Decimal("89"),
        statement_floor_date=date(2025, 3, 31),
    )
    row = _build(peak=peak)
    assert row.peak_balance_inr == Decimal("8000.10")
    assert (row.audit_peak_balance_base_ccy, row.audit_peak_ttbr) == (
        Decimal("90.5"),
        Decimal("88.4"),
    )
    assert (row.audit_statement_peak_floor_base_ccy, row.audit_statement_peak_floor_date) == (
        Decimal("89"),
        date(2025, 3, 31),
    )


def test_attested_peak_files_the_value_with_its_source_and_reason() -> None:
    row = _build(
        peak=FaA2AttestedPeak(
            peak_inr=Decimal("250000"),
            source="[[schedule_fa.manual_values]] calendar_year = 2025",
            reason="user-supplied",
        )
    )
    assert row == replace(
        _build(),
        peak_balance_inr=Decimal("250000"),
        audit_peak_override_source="[[schedule_fa.manual_values]] calendar_year = 2025",
        audit_peak_override_reason="user-supplied",
    )


@pytest.mark.parametrize(("source", "reason"), [(" ", "user-supplied"), ("config", "")])
def test_attested_peak_refuses_a_blank_source_or_reason(source: str, reason: str) -> None:
    with pytest.raises(InvariantError, match="source and reason must be non-empty"):
        _build(peak=FaA2AttestedPeak(peak_inr=Decimal("250000"), source=source, reason=reason))


def test_nav_refused_leaves_closing_manual_fill() -> None:
    refused = NetAssetValueSummary(
        closing_total=None, refusal_reason=NavRefusalReason.NO_ASSET_CLASS_BLOCK
    )
    row = _build(nav=refused, closing_ttbr_dec31=Decimal("89.47"))
    assert row.closing_balance_inr is None
    assert row.audit_ttbr_dec31 is None
    assert row.closing_blank_reason is FaA2ClosingBlankReason.NO_ASSET_CLASS_BLOCK


@pytest.mark.parametrize("base_currency", ["USD", "EUR"])
@pytest.mark.parametrize("reason", list(NavRefusalReason))
def test_nav_refusal_is_copied_to_the_row(reason: NavRefusalReason, base_currency: str) -> None:
    """#1013: the NAV's reason wins, for a non-USD base too."""
    row = _build(
        identity=replace(_IDENTITY, base_currency=base_currency),
        nav=NetAssetValueSummary(closing_total=None, refusal_reason=reason),
        closing_ttbr_dec31=None,
    )
    assert row.closing_balance_inr is None
    assert row.closing_blank_reason == FaA2ClosingBlankReason(reason.value)


def test_confident_usd_nav_files_the_closing_balance_with_no_reason() -> None:
    row = _build()
    assert row.closing_balance_inr == Decimal("123.718346") * Decimal("89.47")  # 11069.08041662
    assert row.closing_blank_reason is None


# #1250: a 33-digit NAV at ₹89.47/$ is ₹11069.4999… to every digit, which files
# ₹11069. A 28-digit product rounds it to ₹11069.50, which files ₹11070.
_LONG_NAV = Decimal("123.723035654409299206437912149323")
_LONG_NAV_INR = Decimal("11069.49999999999999999999999999992881")


def test_closing_balance_is_the_exact_nav_times_ttbr() -> None:
    """#1250: the closing balance keeps all 37 digits of NAV x TTBR and files
    ₹11069. Main multiplied at 28 digits and filed ₹11070."""
    row = _build(
        nav=NetAssetValueSummary(closing_total=_LONG_NAV, refusal_reason=None, cross_checked=True)
    )
    assert row.closing_balance_inr is not None
    assert row.closing_balance_inr.compare_total(_LONG_NAV_INR) == 0
    assert row.filed.closing_balance_inr == Decimal("11069")
    assert row.audit_closing_balance_base_ccy == _LONG_NAV


@pytest.mark.parametrize(
    "nav",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)
def test_closing_balance_refuses_when_it_cannot_be_exact(nav: str) -> None:
    """#1250: past the exponent or the 1000-digit cap NAV x TTBR refuses,
    naming no value. Main raised a raw Overflow, or rounded the product and
    refused it as past the CBDT bound."""
    summary = NetAssetValueSummary(
        closing_total=Decimal(nav), refusal_reason=None, cross_checked=True
    )
    with pytest.raises(InvariantError) as exc:
        _build(nav=summary)
    assert str(exc.value) == (
        f"InvariantError: Schedule FA A2 closing balance, INR: {INEXACT_PRODUCT}"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_confident_usd_nav_without_its_rate_refuses() -> None:
    """#1013: a USD NAV always needs its rate; the cell isn't left blank silently."""
    with pytest.raises(InvariantError, match="USD base's confident NAV requires"):
        _build(closing_ttbr_dec31=None)


def test_non_usd_base_with_a_rate_refuses() -> None:
    """#1013: a EUR figure is never converted at a USD rate."""
    with pytest.raises(InvariantError, match="non-USD base takes no December 31 TTBR"):
        _build(identity=replace(_IDENTITY, base_currency="EUR"))


@pytest.mark.parametrize(
    "nature",
    [
        FaA2NatureOfAmount.DIVIDEND,
        FaA2NatureOfAmount.INTEREST,
        FaA2NatureOfAmount.PROCEEDS,
        FaA2NatureOfAmount.OTHER_INCOME,
    ],
)
def test_gross_credit_keeps_dec31_ttbr_when_closing_is_manual_fill(
    nature: FaA2NatureOfAmount,
) -> None:
    refused = NetAssetValueSummary(
        closing_total=None, refusal_reason=NavRefusalReason.NO_ASSET_CLASS_BLOCK
    )
    row = _build(
        nav=refused,
        gross_components_inr={nature: Decimal("75")},
    )
    assert row.closing_balance_inr is None
    assert row.audit_ttbr_dec31 == Decimal("89.47")


def test_non_usd_no_ttbr_leaves_closing_manual_fill() -> None:
    """Caller passes ttbr=None for a non-USD base currency → closing manual-fill."""
    row = _build(identity=replace(_IDENTITY, base_currency="EUR"), closing_ttbr_dec31=None)
    assert row.closing_balance_inr is None
    assert row.closing_blank_reason is FaA2ClosingBlankReason.NON_USD_BASE


def test_single_nature_dividend() -> None:
    row = _build(gross_components_inr={FaA2NatureOfAmount.DIVIDEND: Decimal("75")})
    assert row.nature_of_amount is FaA2NatureOfAmount.DIVIDEND
    assert row.gross_amount_inr == Decimal("75")


def test_single_nature_interest() -> None:
    row = _build(gross_components_inr={FaA2NatureOfAmount.INTEREST: Decimal("0.95")})
    assert row.nature_of_amount is FaA2NatureOfAmount.INTEREST
    assert row.gross_amount_inr == Decimal("0.95")


def test_multiple_natures_manual_fill_with_audit_breakdown() -> None:
    """Multi-nature account: dividends + interest → no single nature → manual-fill,
    but the per-nature breakdown is preserved in the audit trail."""
    row = _build(
        gross_components_inr={
            FaA2NatureOfAmount.DIVIDEND: Decimal("3.2"),
            FaA2NatureOfAmount.INTEREST: Decimal("0.95"),
        }
    )
    assert row.nature_of_amount is None
    assert row.gross_amount_inr is None
    assert row.audit_gross_components["Dividend"] == Decimal("3.2")
    assert row.audit_gross_components["Interest"] == Decimal("0.95")


def test_expand_multiple_known_natures_to_separate_rows() -> None:
    evidence = FaA2GrossEvidence(
        {
            FaA2NatureOfAmount.DIVIDEND: Decimal("3.2"),
            FaA2NatureOfAmount.INTEREST: Decimal("0.95"),
        }
    )
    rows = expand_fa_a2_nature_rows(
        _build(gross_components_inr=evidence.known_components),
        evidence,
    )
    assert [(row.nature_of_amount, row.gross_amount_inr) for row in rows] == [
        (FaA2NatureOfAmount.INTEREST, Decimal("0.95")),
        (FaA2NatureOfAmount.DIVIDEND, Decimal("3.2")),
    ]


def test_expand_unresolved_nature_keeps_blank_placeholder() -> None:
    evidence = FaA2GrossEvidence(
        {
            FaA2NatureOfAmount.DIVIDEND: Decimal("3.2"),
            FaA2NatureOfAmount.PROCEEDS: Decimal("10"),
        },
        (FaA2NatureOfAmount.PROCEEDS,),
    )
    rows = expand_fa_a2_nature_rows(
        _build(gross_components_inr=evidence.known_components),
        evidence,
    )
    assert [(row.nature_of_amount, row.gross_amount_inr) for row in rows] == [
        (FaA2NatureOfAmount.DIVIDEND, Decimal("3.2")),
        (None, None),
    ]


def test_zero_components_is_no_amount_paid() -> None:
    row = _build(gross_components_inr={FaA2NatureOfAmount.DIVIDEND: Decimal("0")})
    assert row.nature_of_amount is FaA2NatureOfAmount.NO_AMOUNT
    assert row.gross_amount_inr == Decimal("0")


def test_not_determinable_gross_is_manual_fill() -> None:
    row = _build(gross_components_inr=None)
    assert row.nature_of_amount is None
    assert row.gross_amount_inr is None


# ---- build_gross_evidence (the per-nature CY aggregation) ------------------

_FX = FaExactDatePolicy(max_carry_forward_days=14)
_NAV_REFUSED = NetAssetValueSummary(
    closing_total=None, refusal_reason=NavRefusalReason.NO_ASSET_CLASS_BLOCK
)


def _dividend(year: int) -> Dividend:
    return Dividend(
        symbol=Ticker("XQAW"),
        isin=Isin("US99990E1038"),
        payment_date=date(year, 6, 2),
        amount_gross=Decimal("5"),
        currency="USD",
        description="XQAW(US99990E1038) Cash Dividend USD 0.50 per Share",
    )


def _interest(year: int, currency: str = "USD") -> InterestCredit:
    return InterestCredit(
        credit_date=date(year, 6, 2),
        amount_gross=Decimal("10"),
        currency=currency,
        description=f"{currency} Credit Interest",
    )


def _evidence(
    dividends: list[Dividend],
    interest_credits: list[InterestCredit],
    closed_lots: list[ClosedLot],
    *,
    calendar_year: int = 2025,
    dividend_activity_present: bool = False,
    interest_non_credits: tuple[InterestNonCredit, ...] = (),
) -> FaA2GrossEvidence:
    return build_gross_evidence(
        dividends,
        interest_credits,
        closed_lots,
        interest_non_credits=interest_non_credits,
        calendar_year=calendar_year,
        fx_policy=_FX,
        dividend_activity_present=dividend_activity_present,
    )


def test_gross_evidence_all_empty_no_dividend_activity_is_empty_map() -> None:
    # No credits + no dividend section → confidently nothing → {} (→ "No Amount").
    out = _evidence([], [], [])
    assert out.known_components == {}
    assert out.unresolved_natures == ()


def test_gross_evidence_empty_does_not_require_future_dec31_rate() -> None:
    out = _evidence([], [], [], calendar_year=2099)
    assert out.known_components == {}
    assert out.unresolved_natures == ()


def test_gross_evidence_dividend_activity_present_but_empty_refuses() -> None:
    # A dividend section exists but parsed to nothing → cannot assert ₹0 → None
    # (mirrors A3's hardened zero-inference; manual-fill).
    out = _evidence([], [], [], dividend_activity_present=True)
    assert out.known_components == {}
    assert out.unresolved_natures == (FaA2NatureOfAmount.DIVIDEND,)


def test_gross_evidence_non_usd_interest_refuses() -> None:
    out = _evidence([], [_interest(2025, "EUR")], [])
    assert out.known_components == {}
    assert out.unresolved_natures == (FaA2NatureOfAmount.INTEREST,)


def _non_credit(group: InterestRowGroup, amount: str, year: int = 2025) -> InterestNonCredit:
    return InterestNonCredit(
        group=group,
        amount=Decimal(amount),
        currency="USD",
        row_date=date(year, 6, 30),
        description="USD Debit Interest" if group is InterestRowGroup.DEBIT_INTEREST else "x",
        file_line=12,
    )


@pytest.mark.parametrize(
    "row",
    [
        _non_credit(InterestRowGroup.UNPROVEN_NON_CREDIT, "-4"),
        _non_credit(InterestRowGroup.UNPROVEN_ACCRUAL, "1"),
        _non_credit(InterestRowGroup.UNPROVEN_POSITIVE, "3.17"),
        # Undated rule: a correction outside the year can still correct a credit in it.
        _non_credit(InterestRowGroup.UNPROVEN_NON_CREDIT, "-4", year=2024),
    ],
    ids=["negative-correction", "accrual", "positive-other-wording", "outside-the-year"],
)
def test_gross_evidence_unproven_interest_row_leaves_interest_unresolved(
    row: InterestNonCredit,
) -> None:
    """#905, #1280: an unproven Interest row marks the Interest nature
    unresolved, as a credit A2 can't convert does, keeping the known partial
    total; a positive unproven row adds nothing to it."""
    out = _evidence([], [_interest(2025)], [], interest_non_credits=(row,))
    assert out.known_components == {
        FaA2NatureOfAmount.INTEREST: Decimal("10") * _FX.rate_for(date(2025, 12, 31)),
    }
    assert out.unresolved_natures == (FaA2NatureOfAmount.INTEREST,)


def test_gross_evidence_leaves_debit_interest_out() -> None:
    """#905: debit interest isn't income, so it neither reduces A2's
    interest credited nor leaves it unresolved."""
    debit = _non_credit(InterestRowGroup.DEBIT_INTEREST, "-3")
    assert _evidence([], [_interest(2025)], [], interest_non_credits=(debit,)) == _evidence(
        [], [_interest(2025)], []
    )


def test_gross_evidence_converts_every_nature_at_dec31() -> None:
    out = _evidence(
        [_dividend(2025)],
        [_interest(2025)],
        [_closed_lot(2025, Decimal("321"))],
    )
    rate = _FX.rate_for(date(2025, 12, 31))
    assert out.known_components == {
        FaA2NatureOfAmount.DIVIDEND: Decimal("5") * rate,
        FaA2NatureOfAmount.INTEREST: Decimal("10") * rate,
        FaA2NatureOfAmount.PROCEEDS: Decimal("321") * rate,
    }
    assert out.unresolved_natures == ()


def test_gross_evidence_missing_pre_fee_proceeds_is_manual_fill() -> None:
    # 2099 has no Dec-31 rate, so a lookup would raise.
    out = _evidence([], [], [_closed_lot(2099)], calendar_year=2099)
    assert out.known_components == {}
    assert out.unresolved_natures == (FaA2NatureOfAmount.PROCEEDS,)


def test_gross_evidence_keeps_partial_proceeds_when_also_unresolved() -> None:
    out = _evidence([], [], [_closed_lot(2025, Decimal("321")), _closed_lot(2025)])
    assert out.known_components == {
        FaA2NatureOfAmount.PROCEEDS: Decimal("321") * _FX.rate_for(date(2025, 12, 31)),
    }
    assert out.unresolved_natures == (FaA2NatureOfAmount.PROCEEDS,)


def test_gross_evidence_ignores_closed_lot_outside_calendar_year() -> None:
    out = _evidence([], [], [_closed_lot(2024, Decimal("321")), _closed_lot(2024)])
    assert out.known_components == {}
    assert out.unresolved_natures == ()


_MISSING_RATE_INPUTS = {
    FaA2NatureOfAmount.DIVIDEND: ([_dividend(2099)], [], []),
    FaA2NatureOfAmount.INTEREST: ([], [_interest(2099)], []),
    FaA2NatureOfAmount.PROCEEDS: ([], [], [_closed_lot(2099, Decimal("321"))]),
}


@pytest.mark.parametrize("nature", list(_MISSING_RATE_INPUTS))
def test_gross_evidence_raises_on_missing_dec31_rate(
    nature: FaA2NatureOfAmount,
) -> None:
    dividends, interest_credits, closed_lots = _MISSING_RATE_INPUTS[nature]
    with pytest.raises(MissingFxRateError):
        _evidence(dividends, interest_credits, closed_lots, calendar_year=2099)


def test_gross_evidence_zero_proceeds_still_need_dec31_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#735: proven zero proceeds need the Dec-31 rate like any other proceeds."""
    lot = replace(
        _closed_lot(2025, Decimal("0")),
        open_date_time=date(2025, 1, 15),
        close_date_time=date(2025, 6, 16),
        proceeds_money=Decimal("0"),
    )
    monkeypatch.setattr(
        "ibkr_fa.fx.policy_fa.load_ttbr",
        lambda _currency: {lot.open_date_time: Decimal("85")},
    )
    fx = FaExactDatePolicy(max_carry_forward_days=0)
    assert fx.rate_for(lot.open_date_time) == Decimal("85")
    requested_dates: list[date] = []
    rate_for = fx.rate_for

    def recording_rate_for(event_date: date) -> Decimal:
        requested_dates.append(event_date)
        return rate_for(event_date)

    monkeypatch.setattr(fx, "rate_for", recording_rate_for)
    with pytest.raises(MissingFxRateError):
        build_gross_evidence(
            [],
            [],
            [lot],
            interest_non_credits=(),
            calendar_year=2025,
            fx_policy=fx,
            dividend_activity_present=False,
        )
    assert requested_dates[-1] == date(2025, 12, 31)


# ---- gross_interest_inr ----------------------------------------------------


def test_gross_interest_empty_makes_no_fx_lookup() -> None:
    assert gross_interest_inr([], calendar_year=2099, fx_policy=_FX) == Decimal(0)


def test_gross_interest_ignores_out_of_year_credit() -> None:
    # 2099 has no Dec-31 rate, so a lookup would raise.
    assert gross_interest_inr([_interest(2025)], calendar_year=2099, fx_policy=_FX) == Decimal(0)


def test_gross_interest_refuses_other_currency() -> None:
    with pytest.raises(InvariantError):
        gross_interest_inr([_interest(2025, "EUR")], calendar_year=2025, fx_policy=_FX)


def _long_credit(amount: str) -> InterestCredit:
    return InterestCredit(
        credit_date=date(2025, 6, 16),
        amount_gross=Decimal(amount),
        currency="USD",
        description="USD Credit Interest",
    )


def test_gross_interest_is_the_exact_product_at_the_dec31_rate() -> None:
    """#1312: a 27-digit credit at ₹89.47/$ is exactly ₹89472841.4999…, which
    files ₹89472841. Main's 28-digit product, ₹89472841.50000000000000000000,
    filed ₹89472842."""
    inr = gross_interest_inr(
        [_long_credit("1000031.75924891024924555717")],
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("89.47")),
    )
    assert inr.compare_total(Decimal("89472841.4999999999999999999999")) == 0
    assert to_whole_rupees(inr) == Decimal("89472841")


def test_gross_interest_just_above_the_half_files_the_rupee_above() -> None:
    """Control: one more at the last digit files ₹89472842, as main does."""
    inr = gross_interest_inr(
        [_long_credit("1000031.75924891024924555718")],
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("89.47")),
    )
    assert to_whole_rupees(inr) == Decimal("89472842")


def test_gross_evidence_dividend_and_interest_rows_file_the_exact_product() -> None:
    """#1312: at the vendored Dec-31 TTBR (₹89.47/$) the A2 Dividend and
    Interest rows each file ₹89472841. Main filed ₹89472842 on both."""
    long_amount = "1000031.75924891024924555717"
    dividend = replace(_dividend(2025), amount_gross=Decimal(long_amount))
    out = _evidence([dividend], [_long_credit(long_amount)], [])
    exact = Decimal("89472841.4999999999999999999999")
    assert set(out.known_components) == {FaA2NatureOfAmount.DIVIDEND, FaA2NatureOfAmount.INTEREST}
    for nature in (FaA2NatureOfAmount.DIVIDEND, FaA2NatureOfAmount.INTEREST):
        assert out.known_components[nature].compare_total(exact) == 0
    rows = expand_fa_a2_nature_rows(_build(gross_components_inr=out.known_components), out)
    assert [(row.nature_of_amount, row.filed.gross_amount_inr) for row in rows] == [
        (FaA2NatureOfAmount.INTEREST, Decimal("89472841")),
        (FaA2NatureOfAmount.DIVIDEND, Decimal("89472841")),
    ]


def test_gross_interest_past_1000_digits_refuses() -> None:
    """#1312: a rate of 1,000 significant digits makes the product need 1,002,
    so it refuses with a fixed label and no value. Main rounded it."""
    with pytest.raises(InvariantError) as exc:
        gross_interest_inr(
            [_long_credit("1.5")],
            calendar_year=2025,
            fx_policy=AuditedFxStub(Decimal("8." + "3" * 999)),
        )
    assert str(exc.value) == (
        f"InvariantError: Schedule FA A2 gross interest, INR: {INEXACT_PRODUCT}"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


# ---- fa_a2_closing_ttbr ----------------------------------------------------


def _closing_ttbr(
    nav: NetAssetValueSummary,
    evidence: FaA2GrossEvidence,
    *,
    base_currency: str = "USD",
    calendar_year: int = 2025,
) -> Decimal | None:
    return fa_a2_closing_ttbr(
        nav,
        base_currency=base_currency,
        gross_evidence=evidence,
        calendar_year=calendar_year,
        fx_policy=_FX,
    )


def test_closing_ttbr_usd_base_with_nav_uses_dec31_rate() -> None:
    assert _closing_ttbr(_NAV_OK, FaA2GrossEvidence({})) == _FX.rate_for(date(2025, 12, 31))


def test_closing_ttbr_missing_rate_raises() -> None:
    with pytest.raises(MissingFxRateError):
        _closing_ttbr(_NAV_OK, FaA2GrossEvidence({}), calendar_year=2099)


@pytest.mark.parametrize(
    ("nav", "base_currency"),
    [(_NAV_OK, "EUR"), (_NAV_REFUSED, "USD")],
)
def test_closing_ttbr_without_usd_nav_or_credit_makes_no_lookup(
    nav: NetAssetValueSummary,
    base_currency: str,
) -> None:
    # 2099 has no Dec-31 rate, so a lookup would raise.
    assert (
        _closing_ttbr(nav, FaA2GrossEvidence({}), base_currency=base_currency, calendar_year=2099)
        is None
    )


def test_closing_ttbr_without_nav_still_converts_known_credit() -> None:
    evidence = FaA2GrossEvidence({FaA2NatureOfAmount.INTEREST: Decimal("10")})
    assert _closing_ttbr(_NAV_REFUSED, evidence) == _FX.rate_for(date(2025, 12, 31))


def test_manual_fill_gaps_flag_each_column_any_row_leaves_blank() -> None:
    peak = FaA2AttestedPeak(peak_inr=Decimal("250000"), source="config", reason="supplied")
    complete = _build(peak=peak)
    assert fa_a2_manual_fill_gaps((complete,)) == FaA2ManualFillGaps(
        zip_code=False, peak=False, closing=False, gross=False
    )
    no_closing = _build(peak=peak, nav=_NAV_REFUSED, closing_ttbr_dec31=None)
    unresolved = expand_fa_a2_nature_rows(
        _build(), FaA2GrossEvidence({}, (FaA2NatureOfAmount.DIVIDEND,))
    )
    assert fa_a2_manual_fill_gaps((complete, no_closing, *unresolved)) == FaA2ManualFillGaps(
        zip_code=False, peak=True, closing=True, gross=True
    )


@pytest.mark.parametrize("zip_code", ["", "   "], ids=["empty", "whitespace-only"])
def test_manual_fill_gaps_flag_a_blank_zip(zip_code: str) -> None:
    """#1358: a row whose ZIP is empty or whitespace-only leaves the ZIP Code
    blank, as A3 (#1289) and the CBDT JSON fragment treat it."""
    peak = FaA2AttestedPeak(peak_inr=Decimal("250000"), source="config", reason="supplied")
    complete = _build(peak=peak)
    blank = _build(
        peak=peak,
        identity=replace(_IDENTITY, institution=replace(_IDENTITY.institution, zip_code=zip_code)),
    )
    assert blank.institution_zip == zip_code
    assert fa_a2_manual_fill_gaps((complete, blank)) == FaA2ManualFillGaps(
        zip_code=True, peak=False, closing=False, gross=False
    )
