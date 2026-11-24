"""Exact foreign-tax, Form 67 and Schedule FSI relief sums (#1167).

Owner decision (2026-11-10, made on #1166 and applied here): these sums never
round. Each addition widens to at least ``EXACT_SUM_DIGITS`` and refuses only
a nonfinite amount, a decimal arithmetic failure or an addition that would
still round at that precision.

All values are synthetic. ``_SIX`` (``_LONG`` then five ``_TINY``) totals
exactly 1.50, so ₹2; the 28-digit context keeps ``_LONG`` and files ₹1.
``_HUGE`` plus ``_MINUTE`` needs more than ``EXACT_SUM_DIGITS`` digits. A
reversal listed before its original returns a country or ISIN net to zero
while a narrower ISIN or payment total still holds the large amount, so each
accumulator is reached with every earlier one exact.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from ibkr_fa.compute.foreign_tax import (
    ForeignTaxNetting,
    PaidWithholding,
    WithholdingPaymentKey,
    net_foreign_tax_by_country,
)
from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    _decide_treaty_rate,
    _gross_by_payment,
    _PaymentGross,
    _TreatyRateDecision,
    compute_form67_entries_from_wht,
)
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    ScheduleFsiWhtContribution,
    _merge_results,
    compute_schedule_fsi_from_withholding_tax,
)
from ibkr_fa.compute.schedule_fsi_relief import enrich_fsi_with_form67_relief
from ibkr_fa.compute.schedule_os import ScheduleOsDividendLine
from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import (
    Form67Config,
    Form67TaxPayableOverride,
    ScheduleOsIndianAdrRoutingMode,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome, Form67TreatyRateStatus
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import render_without_frames
from tests._exact_refusals import INEXACT_PRODUCT as _PRODUCT
from tests._exact_refusals import INEXACT_SUM as _EXACT
from tests._foreign_tax_exact_sums import ISIN as _ISIN
from tests._foreign_tax_exact_sums import MARKED_AMOUNT as _MARKED_AMOUNT
from tests._foreign_tax_exact_sums import MARKED_ISIN as _MARKED_ISIN
from tests._foreign_tax_exact_sums import MARKED_PAYMENT_DATE as _MARKED_ON
from tests._foreign_tax_exact_sums import MARKED_SYMBOL as _MARKED_SYMBOL
from tests._foreign_tax_exact_sums import MARKED_TEXT as _MARKED_TEXT
from tests._foreign_tax_exact_sums import MARKERS as _MARKERS
from tests._foreign_tax_exact_sums import PAYMENT_DATE as _ON
from tests._foreign_tax_exact_sums import marked_wht as _marked_wht
from tests._foreign_tax_exact_sums import wht as _wht
from tests._fx_stubs import AuditedFxStub

_LONG = "1.499999999999999999999999998"
_TINY = "4E-28"
_SIX = (_LONG, *(_TINY,) * 5)
_EXACT_SIX = Decimal("1.5000000000000000000000000000")
_HUGE = "1E+10"
_MINUTE = "1E-995"

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)
_LATER = date(2025, 8, 14)
_OTHER_ISIN = Isin("US99972B1017")
_GB_ISIN = Isin("GB99970J1065")
_INTEREST = Form67SourceOfIncome.INTEREST_INCOME
_OTHERS = Form67SourceOfIncome.OTHERS


def _refusal(
    raised: pytest.ExceptionInfo[InvariantError],
    expected: str,
    *,
    markers: tuple[str, ...] = (),
    exact: str = _EXACT,
) -> None:
    """The full message, no context values, no chained arithmetic error and no marker."""
    error = raised.value
    assert str(error) == f"InvariantError: {expected}: {exact}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    for marker in markers:
        assert marker not in rendered, marker


def _paid(*magnitudes: str) -> list[WithholdingTax]:
    """One payment's paid rows, each magnitude negated without rounding."""
    return [_wht(Decimal(magnitude).copy_negate()) for magnitude in magnitudes]


def _fx(rate: str | Mapping[date, str] = "1") -> AuditedFxStub:
    if isinstance(rate, str):
        return AuditedFxStub(Decimal(rate))
    return AuditedFxStub({on: Decimal(value) for on, value in rate.items()})


def _net(rows: list[WithholdingTax], fx: AuditedFxStub | None = None) -> ForeignTaxNetting:
    return net_foreign_tax_by_country(
        rows,
        fx_policy=_fx() if fx is None else fx,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        adr_routing_mode=ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
    )


def _dividend(
    usd: str = "10", inr: str = "10", *, on: date = _ON, issuer: str = "US"
) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker("XQAQ"),
        isin=_ISIN,
        issuer_country_iso2=issuer,
        payment_date=on,
        fx_specified_date=on,
        fx_rate_publication_date=on,
        fx_rate_inr_per_usd=Decimal("1"),
        amount_gross_usd=Decimal(usd),
        amount_gross_inr=Decimal(inr),
        description="XQAQ Cash Dividend (Ordinary Dividend)",
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _marked_dividend() -> ScheduleOsDividendLine:
    """A dividend carrying every marker, the 29-digit amount in USD and INR.

    Built at 60 digits so its own checks keep all 29 digits.
    """
    with localcontext(Context(prec=60)):
        return ScheduleOsDividendLine(
            symbol=Ticker(_MARKED_SYMBOL),
            isin=_MARKED_ISIN,
            issuer_country_iso2="US",
            payment_date=_MARKED_ON,
            fx_specified_date=_MARKED_ON,
            fx_rate_publication_date=_MARKED_ON,
            fx_rate_inr_per_usd=Decimal("1"),
            amount_gross_usd=Decimal(_MARKED_AMOUNT),
            amount_gross_inr=Decimal(_MARKED_AMOUNT),
            description=_MARKED_TEXT,
            kind=DistributionKind.CASH_DIVIDEND,
        )


def _form67(
    rows: list[WithholdingTax],
    lines: list[ScheduleOsDividendLine],
    fx: AuditedFxStub | None = None,
) -> Form67ComputeResult:
    return compute_form67_entries_from_wht(
        rows,
        os_dividend_lines=lines,
        wht_fx_policy=_fx() if fx is None else fx,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


def _fragments(
    rows: list[WithholdingTax], fx: AuditedFxStub | None = None
) -> list[ScheduleFsiWhtContribution]:
    return compute_schedule_fsi_from_withholding_tax(
        rows,
        fx_policy=_fx() if fx is None else fx,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    ).contributions


def _fsi_row(iso: str = "US", *, income: str = "10", tax: str = "0") -> ScheduleFsiRow:
    country = lookup_country(iso)
    return ScheduleFsiRow(
        cbdt_country_code=country.cbdt_code,
        country_name_portal=country.country_name_portal,
        iso_alpha2=iso,
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal(income),
        foreign_tax_inr=Decimal(tax),
        event_count=1,
    )


def _fragment(tax: str, iso: str = "US") -> ScheduleFsiWhtContribution:
    country = lookup_country(iso)
    return ScheduleFsiWhtContribution(
        cbdt_country_code=country.cbdt_code,
        country_name_portal=country.country_name_portal,
        iso_alpha2=iso,
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        foreign_tax_inr=Decimal(tax),
        event_count=1,
    )


def _merge(rows: list[ScheduleFsiRow], fragments: list[ScheduleFsiWhtContribution]) -> list:
    return _merge_results(ScheduleFsiComputeResult(rows=rows), wht_contributions=fragments).rows


def _decide(
    paid: Mapping[WithholdingPaymentKey, PaidWithholding],
    gross: Mapping[WithholdingPaymentKey, _PaymentGross],
) -> _TreatyRateDecision:
    """The US treaty-rate test at 25% over hand-built payment totals."""
    return _decide_treaty_rate(
        iso="US",
        country_rate_percent=Decimal("10"),
        treaty_rate_percent=Decimal("25"),
        foreign_tax_inr=Decimal("1"),
        netting=ForeignTaxNetting(
            countries={},
            signed_inr_by_isin={},
            paid_by_payment=paid,
            included_rows=(),
            cross_fy_reversal_report=WhtCrossFyReversalReport(),
        ),
        gross_by_payment=gross,
    )


def _entry(
    *,
    income: str = "68.4372",
    tax_payable: str = "20.53116",
    credit: str = "17.1093",
) -> Form67Entry:
    """A US dividend entry with a section 90 credit, as the relief tests build it."""
    tax = Decimal("17.3833")
    return Form67Entry(
        serial_number=1,
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        source_of_income=Form67SourceOfIncome.DIVIDEND,
        income_inr=Decimal(income),
        foreign_tax_inr=tax,
        foreign_tax_rate_percent=(tax / Decimal(income)) * Decimal("100"),
        event_count=1,
        tax_payable_normal_inr=Decimal(tax_payable),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25.00"),
        tax_paid_rate_percent=Decimal("25"),
        highest_payment_rate_percent=Decimal("25"),
        treaty_rate_status=Form67TreatyRateStatus.WITHIN,
        eligible_foreign_tax_inr=tax,
        section_90_credit_inr=Decimal(credit),
        total_credit_inr=Decimal(credit),
    )


def _enrich(
    row: ScheduleFsiRow,
    entry: Form67Entry,
    *,
    interest: str | None = None,
    other: str | None = None,
    overrides: Mapping[Form67SourceOfIncome, str] | None = None,
) -> list[ScheduleFsiRow]:
    config = Form67Config(
        tax_payable_overrides=tuple(
            Form67TaxPayableOverride(
                iso_alpha2="US", source_of_income=source, tax_payable_normal_inr=Decimal(amount)
            )
            for source, amount in (overrides or {}).items()
        )
    )
    return enrich_fsi_with_form67_relief(
        ScheduleFsiComputeResult(rows=[row]),
        Form67ComputeResult(entries=(entry,)),
        interest_income_inr_by_iso=None if interest is None else {"US": Decimal(interest)},
        other_income_inr_by_iso=None if other is None else {"US": Decimal(other)},
        form67_config=config,
    ).rows


# ---- exact totals ----------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_six_paid_rows_net_to_the_exact_tax(reverse: bool) -> None:
    """Reversed, the last addition drops only a trailing zero in 28 digits
    (``Rounded`` without ``Inexact``); the exact totals keep it."""
    netting = _net(_paid(*(reversed(_SIX) if reverse else _SIX)))

    country = netting.countries["US"]
    [paid] = netting.paid_by_payment.values()
    for net in (country.signed_inr, country.signed_native, netting.signed_inr_by_isin[_ISIN]):
        assert net.compare_total(_EXACT_SIX.copy_negate()) == 0
    for total in (paid.native, paid.inr):
        assert total.compare_total(_EXACT_SIX) == 0


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_six_paid_rows_file_two_rupees_on_fsi_and_form_67(reverse: bool) -> None:
    """In the original order, the 28-digit context filed ₹1 on both. A ₹10
    dividend on the same payment gives FSI and Form 67 their income."""
    rows = _paid(*(reversed(_SIX) if reverse else _SIX))

    [row] = _merge([_fsi_row()], _fragments(rows))
    [entry] = _form67(rows, [_dividend()]).entries

    assert row.foreign_tax_inr.compare_total(_EXACT_SIX) == 0
    assert entry.foreign_tax_inr.compare_total(_EXACT_SIX) == 0
    assert row.filed.foreign_tax_inr == entry.filed.foreign_tax_inr == 2


def test_exact_tax_just_below_one_and_a_half_files_one_rupee() -> None:
    """The exact net, -₹1.4999999999999999999999999999, needs 29 digits. Unary
    minus would round its negation to 1.5 and file ₹2; ``copy_negate`` doesn't."""
    rows = [_wht("-1.499999999999999999999999999"), _wht("-9E-28")]
    exact = Decimal("1.4999999999999999999999999999")

    [fragment] = _fragments(rows)
    [entry] = _form67(rows, [_dividend()]).entries

    assert fragment.foreign_tax_inr.compare_total(exact) == 0
    assert entry.foreign_tax_inr.compare_total(exact) == 0
    assert entry.filed.foreign_tax_inr == 1


def test_paid_native_subtraction_keeps_every_digit() -> None:
    """Unary minus would round the 29-digit amount before the addition saw it."""
    [paid] = _net([_wht("-" + _MARKED_AMOUNT)]).paid_by_payment.values()
    assert -Decimal("-" + _MARKED_AMOUNT) != Decimal(_MARKED_AMOUNT)
    assert paid.native.compare_total(Decimal(_MARKED_AMOUNT)) == 0


def test_paid_inr_subtraction_keeps_digits_the_native_total_does_not_need() -> None:
    """At a 28-digit rate, $1 and $1E-27 add exactly in 28 digits; their INR
    amounts, 28 digits each, need 55 together."""
    [paid] = _net(_paid("1", "1E-27"), _fx(_LONG)).paid_by_payment.values()
    with localcontext(Context(prec=120)):
        exact_inr = Decimal(_LONG) + Decimal(_LONG) * Decimal("1E-27")
    assert paid.native.compare_total(Decimal("1.000000000000000000000000001")) == 0
    assert paid.inr.compare_total(exact_inr) == 0
    assert len(paid.inr.as_tuple().digits) == 55


def test_form_67_country_income_and_payment_gross_keep_every_digit() -> None:
    """Six dividends of one payment, ``_SIX`` in USD and INR, reversed so the
    28-digit context would drop only the trailing zero."""
    lines = [_dividend(amount, amount) for amount in reversed(_SIX)]

    [entry] = _form67(_paid("0.1"), lines).entries
    [payment] = _gross_by_payment(lines).values()

    assert entry.income_inr.compare_total(_EXACT_SIX) == 0
    assert entry.filed.income_inr == 2
    assert payment.usd.compare_total(_EXACT_SIX) == 0
    assert payment.inr.compare_total(_EXACT_SIX) == 0


def test_eligible_foreign_tax_keeps_every_digit() -> None:
    """One payment above the 25% treaty rate makes the country ``ABOVE``; its
    eligible tax then adds ``_LONG`` and five within-rate ``_TINY`` amounts."""
    keys = [WithholdingPaymentKey(None, _ISIN, date(2025, 7, day)) for day in range(1, 7)]
    paid = {
        keys[0]: PaidWithholding(native=Decimal("10"), inr=Decimal(_LONG), row_count=1),
        **{key: PaidWithholding(Decimal("1"), Decimal(_TINY), 1) for key in keys[1:]},
    }
    gross = {
        key: _PaymentGross(usd=Decimal("10"), inr=Decimal("100"), line_count=1) for key in keys
    }

    decision = _decide(paid, gross)

    assert decision.status is Form67TreatyRateStatus.ABOVE
    assert decision.eligible_foreign_tax_inr is not None
    assert decision.eligible_foreign_tax_inr.compare_total(_EXACT_SIX) == 0


def test_wht_merge_keeps_every_digit() -> None:
    [row] = _merge([_fsi_row()], [_fragment(amount) for amount in _SIX])
    assert row.foreign_tax_inr.compare_total(_EXACT_SIX) == 0
    assert row.filed.foreign_tax_inr == 2


def test_relief_reconciles_an_income_that_needs_a_29th_digit() -> None:
    """In 28 digits the dividend income plus ₹1E-27 interest rounded back to
    the dividend income, so the row didn't reconcile and stayed unenriched."""
    [row] = _enrich(
        _fsi_row(income="68.437200000000000000000000001", tax="17.3833"),
        _entry(),
        interest="1E-27",
        overrides={_INTEREST: "7"},
    )
    assert (row.tax_relief_inr, row.tax_payable_inr) == (Decimal("17.1093"), Decimal("27.53116"))


def test_relief_tax_payable_keeps_every_digit() -> None:
    [row] = _enrich(
        _fsi_row(income="120", tax="17.3833"),
        _entry(tax_payable=_LONG, credit="1"),
        interest="51.5628",
        overrides={_INTEREST: _TINY},
    )
    assert row.tax_payable_inr is not None
    assert row.tax_payable_inr.compare_total(Decimal("1.4999999999999999999999999984")) == 0


@pytest.mark.parametrize(
    "amounts",
    [("-0.39", "-1.25", "-0.01"), ("-12.34", "12.34"), ("-0.10", "-0.20")],
    ids=["cents", "reversal", "trailing-zeros"],
)
def test_ordinary_amounts_keep_their_value_and_exponent(amounts: tuple[str, ...]) -> None:
    """Cent amounts at a TTBR rate: each total equals the context's own sum,
    exponent included. A reversal is dated after its original."""
    rows = [_wht(amount, on=_ON if amount.startswith("-") else _LATER) for amount in amounts]
    netting = _net(rows, _fx("85.23"))
    inr = [row.amount * Decimal("85.23") for row in rows]
    signed_inr = sum(inr, Decimal())
    paid_inr = sum((amount.copy_negate() for amount in inr if amount < 0), Decimal())

    assert netting.countries["US"].signed_inr.compare_total(signed_inr) == 0
    assert netting.signed_inr_by_isin[_ISIN].compare_total(signed_inr) == 0
    [paid] = netting.paid_by_payment.values()
    assert paid.inr.compare_total(paid_inr) == 0


def test_withholding_row_converts_to_the_exact_inr() -> None:
    """#1312: USD 912201.23717217787913340935 at the Rule 128 rate of 87.70 is
    exactly ₹80000048.4999…95, so FSI's and Form 67's tax paid file ₹80000048.
    Main's 28-digit product, ₹80000048.50000000000000000000, filed ₹80000049."""
    rows = [_wht("-912201.23717217787913340935")]
    fx = _fx("87.70")
    exact = Decimal("80000048.4999999999999999999950")

    netting = _net(rows, fx)
    [paid] = netting.paid_by_payment.values()
    [row] = _merge([_fsi_row(income="800000484.849")], _fragments(rows, fx))
    [entry] = _form67(rows, [_dividend("9122012.37", "800000484.849")], fx).entries

    for net in (netting.countries["US"].signed_inr, netting.signed_inr_by_isin[_ISIN]):
        assert net.compare_total(exact.copy_negate()) == 0
    for total in (paid.inr, row.foreign_tax_inr, entry.foreign_tax_inr):
        assert total.compare_total(exact) == 0
    assert row.filed.foreign_tax_inr == entry.filed.foreign_tax_inr == Decimal("80000048")


def test_no_withholding_has_no_totals() -> None:
    netting = _net([])
    assert (netting.countries, netting.signed_inr_by_isin, netting.paid_by_payment) == ({}, {}, {})


# ---- refusals past the cap -------------------------------------------------------


def test_country_inr_net_past_the_cap_refuses_at_its_row() -> None:
    with pytest.raises(InvariantError) as raised:
        _net([_marked_wht(), _wht("-" + _HUGE), _wht("-" + _MINUTE)])
    _refusal(raised, "Foreign tax country net, included row 3, column amount_inr", markers=_MARKERS)


def test_country_native_net_past_the_cap_refuses_at_its_row() -> None:
    """A rate of 1E+20 on the last row's date keeps the INR net within the cap."""
    rows = [_marked_wht(), _wht("-" + _HUGE), _wht("-" + _MINUTE, on=_LATER)]
    with pytest.raises(InvariantError) as raised:
        _net(rows, _fx({_MARKED_ON: "1", _ON: "1", _LATER: "1E+20"}))
    _refusal(raised, "Foreign tax country net, included row 3, column amount", markers=_MARKERS)


def test_isin_net_past_the_cap_refuses_at_its_row() -> None:
    """The other ISIN's reversal, listed before its original, returns the
    country net to the marked amount before the tiny row is added."""
    rows = [
        _marked_wht(),
        _wht(_HUGE, isin=_OTHER_ISIN, on=_LATER),
        _wht("-" + _HUGE),
        _wht("-" + _MINUTE),
        _wht("-" + _HUGE, isin=_OTHER_ISIN),
    ]
    with pytest.raises(InvariantError) as raised:
        _net(rows)
    _refusal(raised, "Foreign tax ISIN net, included row 4, column amount_inr", markers=_MARKERS)


def test_paid_native_total_past_the_cap_refuses_at_its_row() -> None:
    """The payment's own reversal, listed first, returns its ISIN and country
    nets to zero; its paid total, before refunds, keeps the 28-digit amount.

    This pair uses a 28-digit amount rather than the 29-digit marker, which
    pairing, negating with unary minus, couldn't pair before #1312.
    """
    paired, paid_on = "1.234567890123456789012345678", date(2025, 6, 2)
    rows = [
        _marked_wht(paired),
        _marked_wht("-" + paired, on=paid_on),
        _marked_wht("-1E-1000", on=paid_on),
    ]
    with pytest.raises(InvariantError) as raised:
        _net(rows)
    _refusal(
        raised,
        "Foreign tax payment paid total, included row 3, column amount",
        markers=(*_MARKERS[:-1], paired),
    )


def test_paid_inr_total_past_the_cap_refuses_at_its_row() -> None:
    """At a 28-digit rate the native paid total needs 996 digits and the INR
    one 1,023."""
    rows = [_wht(_HUGE, on=_LATER), _wht("-" + _HUGE), _wht("-1E-985")]
    with pytest.raises(InvariantError) as raised:
        _net(rows, _fx("1.234567890123456789012345678"))
    _refusal(raised, "Foreign tax payment paid total, included row 3, column amount_inr")


def test_withholding_inr_past_the_cap_refuses_at_its_row() -> None:
    """#1312: at a rate of 1,000 significant digits the first row's INR is
    exact, but the marked 29-digit row's needs 1,029 digits. Main rounded it."""
    rows = [_wht("-1"), _marked_wht()]
    with pytest.raises(InvariantError) as raised:
        _net(rows, _fx("8." + "3" * 999))
    _refusal(
        raised,
        "Foreign tax INR, included row 2, column amount",
        markers=_MARKERS,
        exact=_PRODUCT,
    )


@pytest.mark.parametrize(
    ("usd", "inr", "column"),
    [
        ((_HUGE, _MINUTE), ("1", "1"), "amount_gross_usd"),
        (("1", "1"), (_HUGE, _MINUTE), "amount_gross_inr"),
    ],
    ids=["usd", "inr"],
)
def test_form_67_country_income_past_the_cap_refuses_at_its_row(
    usd: tuple[str, str], inr: tuple[str, str], column: str
) -> None:
    """The lines are on different payments, so no payment gross is long."""
    lines = [
        _marked_dividend(),
        _dividend(usd[0], inr[0]),
        _dividend(usd[1], inr[1], on=_LATER),
    ]
    with pytest.raises(InvariantError) as raised:
        _form67([], lines)
    _refusal(raised, f"Form 67 country income, input row 3, column {column}", markers=_MARKERS)


@pytest.mark.parametrize(
    ("usd", "inr", "column"),
    [
        ((_HUGE, _MINUTE), ("1", "1"), "amount_gross_usd"),
        (("1", "1"), (_HUGE, _MINUTE), "amount_gross_inr"),
    ],
    ids=["usd", "inr"],
)
def test_form_67_payment_gross_past_the_cap_refuses_at_its_row(
    usd: tuple[str, str], inr: tuple[str, str], column: str
) -> None:
    """One payment's two lines name different issuer countries (as an ADR's
    can), so neither country's income is long."""
    lines = [
        _marked_dividend(),
        _dividend(usd[0], inr[0]),
        _dividend(usd[1], inr[1], issuer="GB"),
    ]
    with pytest.raises(InvariantError) as raised:
        _form67([], lines)
    _refusal(raised, f"Form 67 payment gross, input row 3, column {column}", markers=_MARKERS)


@pytest.mark.parametrize("native", ["1", "10"], ids=["within-rate", "above-rate"])
def test_eligible_foreign_tax_past_the_cap_refuses_at_its_payment(native: str) -> None:
    """Payments count from the first, the skipped GB one included."""
    gb, first, second = (
        WithholdingPaymentKey(None, _GB_ISIN, _ON),
        WithholdingPaymentKey(None, _ISIN, _ON),
        WithholdingPaymentKey(None, _ISIN, _LATER),
    )
    paid = {
        gb: PaidWithholding(Decimal("1"), Decimal("1"), 1),
        first: PaidWithholding(Decimal("1"), Decimal(_HUGE), 1),
        second: PaidWithholding(Decimal(native), Decimal(_MINUTE), 1),
    }
    gross = {key: _PaymentGross(Decimal("10"), Decimal("100"), 1) for key in (first, second)}
    with pytest.raises(InvariantError) as raised:
        _decide(paid, gross)
    _refusal(raised, "Form 67 eligible foreign tax, payment 3, field eligible_foreign_tax_inr")


# #1312: 999-digit amounts. A quarter of _FOUR_AND_A_BIT needs 1,001 digits;
# a quarter of _JUST_BELOW_FOUR fits in 1,000, but 2 minus it needs 1,001; 100
# times _ONE_AND_A_BIT or _LONG_RATE needs 1,001.
_FOUR_AND_A_BIT = "4." + "0" * 997 + "1"
_JUST_BELOW_FOUR = "3." + "9" * 998
_ONE_AND_A_BIT = "1." + "0" * 997 + "1"
_LONG_RATE = "8." + "1" * 998

# Each Form 67 field past the cap: (withholding rows, dividend lines, FX rates,
# refused field, refusal, input markers). A GB-issuer dividend on a US payment, as an ADR's
# can be, gives the payment a gross that isn't its country's income.
_FORM_67_PAST_THE_CAP = {
    "country-inr-rate": (
        [_wht("-1")],
        [_dividend()],
        _LONG_RATE,
        "country 1, field foreign_tax_rate_percent",
        _PRODUCT,
        (_LONG_RATE[-40:],),
    ),
    "country-source-rate": (
        # USD 1E-998 at ₹100/$ keeps the INR tax to 997 digits; the USD tax has 999.
        [_wht("-1"), _wht("-1E-998", on=_LATER)],
        [_dividend(), _dividend(on=_LATER)],
        {_ON: "1", _LATER: "1E+2"},
        "country 1, field tax_paid_rate_percent",
        _PRODUCT,
        ("1E-998",),
    ),
    "country-treaty-tax": (
        [_wht("-1")],
        [_dividend(_FOUR_AND_A_BIT)],
        "1",
        "country 1, field expected_tax_currency",
        _PRODUCT,
        (_FOUR_AND_A_BIT[-40:],),
    ),
    "country-treaty-difference": (
        [_wht("-2")],
        [_dividend(_JUST_BELOW_FOUR)],
        "1",
        "country 1, field expected_tax_difference",
        _EXACT,
        (_JUST_BELOW_FOUR[-40:],),
    ),
    "payment-treaty-tax": (
        [_wht("-1")],
        [_dividend(_FOUR_AND_A_BIT, issuer="GB"), _dividend(on=_LATER)],
        "1",
        "treaty-rate test, payment 1, field treaty_tax_usd",
        _PRODUCT,
        (_FOUR_AND_A_BIT[-40:],),
    ),
    "payment-excess": (
        [_wht("-2")],
        [_dividend(_JUST_BELOW_FOUR, issuer="GB"), _dividend(on=_LATER)],
        "1",
        "treaty-rate test, payment 1, field excess_usd",
        _EXACT,
        (_JUST_BELOW_FOUR[-40:],),
    ),
    "payment-rate": (
        # The payment's reversal and a later USD 1E-998 leave the country only
        # that, while the payment's paid total keeps all 999 digits.
        [_wht("-" + _ONE_AND_A_BIT), _wht(_ONE_AND_A_BIT, on=_LATER), _wht("-1E-998", on=_LATER)],
        [_dividend(), _dividend(on=_LATER)],
        "1",
        "treaty-rate test, payment 1, field rate_percent",
        _PRODUCT,
        (_ONE_AND_A_BIT[-40:], "1E-998"),
    ),
    "payment-cap": (
        [_wht("-4")],
        [_dividend("10", _FOUR_AND_A_BIT)],
        "1",
        "eligible foreign tax, payment 1, field cap_inr",
        _PRODUCT,
        (_FOUR_AND_A_BIT[-40:],),
    ),
}


@pytest.mark.parametrize("case", list(_FORM_67_PAST_THE_CAP))
def test_form_67_treaty_arithmetic_past_the_cap_refuses_at_its_field(case: str) -> None:
    """#1312: column 6's rate numerators and treaty snap, and the treaty-rate
    test's products, excess, rate numerator and cap, are exact up to the cap
    and refuse past it, naming the country or payment and the field, never a
    value. The corresponding arithmetic on main ran at 28 digits (and left
    the payment-rate case's reversal an orphan)."""
    rows, lines, rate, field, refusal, markers = _FORM_67_PAST_THE_CAP[case]
    with pytest.raises(InvariantError) as raised:
        _form67(rows, lines, _fx(rate))
    _refusal(raised, f"Form 67 {field}", markers=markers, exact=refusal)


def test_wht_merge_past_the_cap_refuses_at_its_contribution() -> None:
    fragments = [_fragment("1", "GB"), _fragment(_HUGE), _fragment(_MINUTE)]
    with pytest.raises(InvariantError) as raised:
        _merge([_fsi_row(), _fsi_row("GB")], fragments)
    _refusal(raised, "Schedule FSI WHT merge, contribution 3, column foreign_tax_inr")


def test_relief_source_slices_past_the_cap_refuse() -> None:
    with pytest.raises(InvariantError) as raised:
        _enrich(_fsi_row(income="100", tax="17.3833"), _entry(), interest=_HUGE, other=_MINUTE)
    _refusal(
        raised,
        "Schedule FSI relief reconciliation, fields interest_income_inr and other_income_inr",
    )


def test_relief_income_past_the_cap_refuses() -> None:
    with pytest.raises(InvariantError) as raised:
        _enrich(_fsi_row(income="1E+11", tax="17.3833"), _entry(income=_HUGE), interest=_MINUTE)
    _refusal(
        raised,
        "Schedule FSI relief reconciliation, fields entry.income_inr and source_slice_total_inr",
    )


@pytest.mark.parametrize(
    ("slice_kwargs", "source", "key"),
    [
        ({"interest": "51.5628"}, _INTEREST, "INTEREST_INCOME"),
        ({"other": "51.5628"}, _OTHERS, "OTHERS"),
    ],
    ids=["interest", "others"],
)
def test_relief_tax_payable_past_the_cap_refuses(
    slice_kwargs: dict[str, str], source: Form67SourceOfIncome, key: str
) -> None:
    with pytest.raises(InvariantError) as raised:
        _enrich(
            _fsi_row(income="120", tax="17.3833"),
            _entry(tax_payable=_HUGE),
            overrides={source: _MINUTE},
            **slice_kwargs,
        )
    _refusal(raised, f"Schedule FSI relief tax payable, key {key}, field tax_payable_normal_inr")
