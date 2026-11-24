"""Tests for :class:`ibkr_fa.models.form67_entry.Form67Entry`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

import pytest

from ibkr_fa.compute.form67 import Form67Readiness, Form67RowReadiness, form67_row_readiness
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.form67_entry import (
    Form67Entry,
    Form67FiledValues,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
from ibkr_fa.pipeline_form67_models import Form67PerEntryRow


def _valid_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "serial_number": 1,
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "source_of_income": Form67SourceOfIncome.DIVIDEND,
        "income_inr": Decimal("10000.00"),
        "foreign_tax_inr": Decimal("2500.00"),
        # 2500 / 10000 * 100 = 25 — US treaty rate.
        "foreign_tax_rate_percent": Decimal("25.0000"),
        "event_count": 4,
    }
    base.update(overrides)
    return base


# ---- positive construction -------------------------------------------------


def test_row_accepts_unset_optional_defaults() -> None:
    """All seven optional tax-computation fields default to None
    ('not computed; user must fill' contract per PR #212 / #215)."""
    row = Form67Entry(**_valid_kwargs())
    assert row.tax_payable_normal_inr is None
    assert row.tax_payable_special_inr is None
    assert row.section_90_dtaa_article is None
    assert row.section_90_dtaa_rate_percent is None
    assert row.section_90_credit_inr is None
    assert row.section_91_credit_inr is None
    assert row.total_credit_inr is None


def test_row_accepts_populated_section_90_credit() -> None:
    row = Form67Entry(
        **_valid_kwargs(
            tax_payable_normal_inr=Decimal("3000"),
            tax_payable_special_inr=Decimal("0"),
            section_90_dtaa_article="Article 10(2)(b)",
            section_90_dtaa_rate_percent=Decimal("25"),
            tax_paid_rate_percent=Decimal("25"),
            highest_payment_rate_percent=Decimal("25"),
            treaty_rate_status=Form67TreatyRateStatus.WITHIN,
            eligible_foreign_tax_inr=Decimal("2500"),
            section_90_credit_inr=Decimal("2500"),
            total_credit_inr=Decimal("2500"),
        )
    )
    assert row.section_90_credit_inr == Decimal("2500")
    assert row.section_91_credit_inr is None
    assert row.total_credit_inr == Decimal("2500")


def test_row_accepts_populated_section_91_credit_for_non_dtaa_country() -> None:
    """For a country with no DTAA, the credit lives in column (12)
    not (11)."""
    row = Form67Entry(
        **_valid_kwargs(
            cbdt_country_code="999",  # hypothetical non-DTAA jurisdiction
            country_name_portal="HYPOTHETICAL NON-DTAA TERRITORY",
            iso_alpha2="HX",
            section_91_credit_inr=Decimal("2500"),
            total_credit_inr=Decimal("2500"),
        )
    )
    assert row.section_90_credit_inr is None
    assert row.section_91_credit_inr == Decimal("2500")


# ---- serial_number invariants ---------------------------------------------


def test_row_rejects_zero_serial_number() -> None:
    with pytest.raises(InvariantError, match="serial_number"):
        Form67Entry(**_valid_kwargs(serial_number=0))


def test_row_rejects_negative_serial_number() -> None:
    with pytest.raises(InvariantError, match="serial_number"):
        Form67Entry(**_valid_kwargs(serial_number=-1))


# ---- cbdt_country_code invariants ------------------------------------------


def test_row_rejects_non_decimal_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="cbdt_country_code"):
        Form67Entry(**_valid_kwargs(cbdt_country_code="US"))


def test_row_rejects_empty_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="cbdt_country_code"):
        Form67Entry(**_valid_kwargs(cbdt_country_code=""))


def test_row_rejects_too_long_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="cbdt_country_code"):
        Form67Entry(**_valid_kwargs(cbdt_country_code="12345"))


def test_row_rejects_fullwidth_digits_in_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="cbdt_country_code"):
        Form67Entry(**_valid_kwargs(cbdt_country_code="\uff19\uff11"))  # fullwidth "91"


# ---- country_name_portal invariants ----------------------------------------


def test_row_rejects_empty_country_name() -> None:
    with pytest.raises(InvariantError, match="country_name_portal"):
        Form67Entry(**_valid_kwargs(country_name_portal=""))


def test_row_rejects_mixed_case_country_name() -> None:
    with pytest.raises(InvariantError, match="country_name_portal"):
        Form67Entry(**_valid_kwargs(country_name_portal="United States of America"))


# ---- iso_alpha2 invariants -------------------------------------------------


def test_row_rejects_lowercase_iso_alpha2() -> None:
    with pytest.raises(InvariantError, match="iso_alpha2"):
        Form67Entry(**_valid_kwargs(iso_alpha2="us"))


def test_row_rejects_three_char_iso_alpha2() -> None:
    with pytest.raises(InvariantError, match="iso_alpha2"):
        Form67Entry(**_valid_kwargs(iso_alpha2="USA"))


def test_row_rejects_iso_alpha2_with_digits() -> None:
    """isalpha() check rejects digit-bearing strings like 'U1'
    that would pass isupper() but indicate a bug (per PR #217 T4 r4)."""
    with pytest.raises(InvariantError, match="iso_alpha2"):
        Form67Entry(**_valid_kwargs(iso_alpha2="U1"))


# ---- India sentinel --------------------------------------------------------


def test_row_rejects_india_iso_alpha2() -> None:
    with pytest.raises(InvariantError, match="India"):
        Form67Entry(**_valid_kwargs(iso_alpha2="IN", cbdt_country_code="2"))


def test_row_rejects_india_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="India"):
        Form67Entry(**_valid_kwargs(iso_alpha2="US", cbdt_country_code="91"))


# ---- source_of_income invariants -------------------------------------------


def test_row_rejects_non_enum_source() -> None:
    with pytest.raises(InvariantError, match="source_of_income"):
        Form67Entry(**_valid_kwargs(source_of_income="Dividend"))


def test_row_accepts_dividend_source() -> None:
    row = Form67Entry(**_valid_kwargs(source_of_income=Form67SourceOfIncome.DIVIDEND))
    assert row.source_of_income is Form67SourceOfIncome.DIVIDEND


def test_row_accepts_all_form67_source_enum_members() -> None:
    """Model is future-ready for CG / interest / royalty / FTS WHT
    once source-kind tagging on the WHT model ships (#37 follow-up)."""
    for source in Form67SourceOfIncome:
        row = Form67Entry(**_valid_kwargs(source_of_income=source))
        assert row.source_of_income is source


# ---- numeric invariants ----------------------------------------------------


def test_row_rejects_zero_income_inr() -> None:
    with pytest.raises(InvariantError, match="income_inr"):
        # Set tax to 0 too else the percent-derivation will fire first.
        Form67Entry(
            **_valid_kwargs(
                income_inr=Decimal("0"),
                foreign_tax_inr=Decimal("100"),
                foreign_tax_rate_percent=Decimal("0.01"),
            )
        )


def test_row_rejects_zero_foreign_tax_inr() -> None:
    with pytest.raises(InvariantError, match="foreign_tax_inr"):
        Form67Entry(
            **_valid_kwargs(
                foreign_tax_inr=Decimal("0"),
                foreign_tax_rate_percent=Decimal("0.0001"),
            )
        )


def test_row_rejects_zero_foreign_tax_rate_percent() -> None:
    with pytest.raises(InvariantError, match="foreign_tax_rate_percent"):
        Form67Entry(**_valid_kwargs(foreign_tax_rate_percent=Decimal("0")))


def test_row_rejects_mismatched_foreign_tax_rate_percent() -> None:
    """Percentage must equal tax/income*100 — surface upstream
    aggregation bugs."""
    with pytest.raises(InvariantError, match="foreign_tax_rate_percent"):
        Form67Entry(
            **_valid_kwargs(
                income_inr=Decimal("10000"),
                foreign_tax_inr=Decimal("2500"),
                foreign_tax_rate_percent=Decimal("30"),  # should be 25
            )
        )


def test_row_accepts_percent_within_tolerance() -> None:
    """Tolerance is 1e-4 to absorb per-event aggregation
    rounding artefacts."""
    row = Form67Entry(
        **_valid_kwargs(
            income_inr=Decimal("10000"),
            foreign_tax_inr=Decimal("2500"),
            # Within 1e-4 of 25 — within tolerance.
            foreign_tax_rate_percent=Decimal("25.00005"),
        )
    )
    assert row.foreign_tax_rate_percent == Decimal("25.00005")


def test_row_rejects_zero_event_count() -> None:
    with pytest.raises(InvariantError, match="event_count"):
        Form67Entry(**_valid_kwargs(event_count=0))


# ---- §90/§90A credit invariants -------------------------------------------


def test_row_rejects_negative_section_90_credit() -> None:
    with pytest.raises(InvariantError, match="section_90_credit_inr"):
        Form67Entry(**_valid_kwargs(section_90_credit_inr=Decimal("-1")))


def test_row_accepts_zero_section_90_credit_when_computed() -> None:
    """Zero credit is a legitimate computed outcome — same semantic
    as Schedule TR per PR #215 T3 r1 P2. DTAA article/rate are
    still required (T3 r5 P3 #1 of PR #221) — even a zero §90
    relief claim must cite which treaty article was applied."""
    row = Form67Entry(
        **_valid_kwargs(
            section_90_dtaa_article="Article 10(2)(b)",
            section_90_dtaa_rate_percent=Decimal("25"),
            tax_paid_rate_percent=Decimal("25"),
            highest_payment_rate_percent=Decimal("25"),
            treaty_rate_status=Form67TreatyRateStatus.WITHIN,
            eligible_foreign_tax_inr=Decimal("2500"),
            section_90_credit_inr=Decimal("0"),
            total_credit_inr=Decimal("0"),
        )
    )
    assert row.section_90_credit_inr == Decimal("0")


def test_row_rejects_section_90_credit_exceeding_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="section_90_credit_inr"):
        Form67Entry(
            **_valid_kwargs(
                foreign_tax_inr=Decimal("100"),
                foreign_tax_rate_percent=Decimal("1"),
                income_inr=Decimal("10000"),
                section_90_credit_inr=Decimal("150"),
            )
        )


# ---- eligible foreign tax invariants --------------------------------------


def test_row_rejects_negative_eligible_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="eligible_foreign_tax_inr must be in"):
        Form67Entry(**_valid_kwargs(eligible_foreign_tax_inr=Decimal("-1")))


def test_row_rejects_eligible_foreign_tax_exceeding_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="eligible_foreign_tax_inr must be in"):
        Form67Entry(**_valid_kwargs(eligible_foreign_tax_inr=Decimal("2500.01")))


def test_row_rejects_section_90_credit_without_eligible_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="WITHIN needs eligible_foreign_tax_inr"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article="Article 10(2)(b)",
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("25"),
                highest_payment_rate_percent=Decimal("25"),
                treaty_rate_status=Form67TreatyRateStatus.WITHIN,
                section_90_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )


def test_row_rejects_section_90_credit_exceeding_eligible_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="requires eligible_foreign_tax_inr"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article="Article 10(2)(b)",
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("30"),
                highest_payment_rate_percent=Decimal("30"),
                treaty_rate_status=Form67TreatyRateStatus.ABOVE,
                eligible_foreign_tax_inr=Decimal("2000"),
                section_90_credit_inr=Decimal("2000.01"),
                total_credit_inr=Decimal("2000.01"),
            )
        )


# ---- §90 credit vs Indian tax payable (#935) -------------------------------


def _section_90_kwargs(**overrides: Any) -> dict[str, Any]:
    """The valid section 90 entry: income ₹10,000, foreign and eligible
    foreign tax ₹2,500, Indian tax payable ₹3,000, credit ₹2,500."""
    section_90: dict[str, Any] = {
        "tax_payable_normal_inr": Decimal("3000"),
        "tax_payable_special_inr": Decimal("0"),
        "section_90_dtaa_article": "Article 10(2)(b)",
        "section_90_dtaa_rate_percent": Decimal("25"),
        "tax_paid_rate_percent": Decimal("25"),
        "highest_payment_rate_percent": Decimal("25"),
        "treaty_rate_status": Form67TreatyRateStatus.WITHIN,
        "eligible_foreign_tax_inr": Decimal("2500"),
        "section_90_credit_inr": Decimal("2500"),
        "total_credit_inr": Decimal("2500"),
    }
    return _valid_kwargs(**(section_90 | overrides))


_CREDIT_ABOVE_TAX_PAYABLE = "section_90_credit_inr \\(col 11\\) must be <= tax_payable_normal_inr"


def _assert_names_credit_and_tax_payable(exc: InvariantError) -> None:
    assert exc.context == (
        ("section_90_credit_inr", None),
        ("tax_payable_normal_inr", None),
    )


def test_row_rejects_section_90_credit_above_tax_payable() -> None:
    with pytest.raises(InvariantError, match=_CREDIT_ABOVE_TAX_PAYABLE) as exc:
        Form67Entry(**_section_90_kwargs(tax_payable_normal_inr=Decimal("1")))
    _assert_names_credit_and_tax_payable(exc.value)


def test_replace_rejects_section_90_credit_above_tax_payable() -> None:
    entry = Form67Entry(**_section_90_kwargs())
    with pytest.raises(InvariantError, match=_CREDIT_ABOVE_TAX_PAYABLE) as exc:
        replace(entry, tax_payable_normal_inr=Decimal("1"))
    _assert_names_credit_and_tax_payable(exc.value)


@pytest.mark.parametrize(
    ("tax_payable", "credit"),
    [("2000", "2000"), ("3000", "2500")],
    ids=["equal", "below"],
)
def test_row_accepts_section_90_credit_at_or_below_tax_payable(
    tax_payable: str, credit: str
) -> None:
    row = Form67Entry(
        **_section_90_kwargs(
            tax_payable_normal_inr=Decimal(tax_payable),
            section_90_credit_inr=Decimal(credit),
            total_credit_inr=Decimal(credit),
        )
    )
    assert row.section_90_credit_inr == Decimal(credit)


# ---- treaty-rate status invariants (#767) ---------------------------------

_WITHIN = Form67TreatyRateStatus.WITHIN
_ABOVE = Form67TreatyRateStatus.ABOVE
_UNPROVEN = Form67TreatyRateStatus.UNPROVEN


def _treaty_kwargs(**overrides: Any) -> dict[str, Any]:
    """A WITHIN US entry, with every field the run-report row requires."""
    base = _valid_kwargs(
        tax_payable_normal_inr=None,
        tax_payable_special_inr=None,
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25"),
        tax_paid_rate_percent=Decimal("25"),
        highest_payment_rate_percent=Decimal("25"),
        eligible_foreign_tax_inr=Decimal("2500.00"),
        treaty_rate_status=_WITHIN,
        section_90_credit_inr=None,
        section_91_credit_inr=None,
        total_credit_inr=None,
    )
    base.update(overrides)
    return base


_NO_SOURCE_RATE: dict[str, Any] = {
    "treaty_rate_status": _UNPROVEN,
    "tax_paid_rate_percent": None,
    "highest_payment_rate_percent": None,
    "eligible_foreign_tax_inr": None,
}
_ABOVE_RATE: dict[str, Any] = {
    "treaty_rate_status": _ABOVE,
    "tax_paid_rate_percent": Decimal("30"),
    "highest_payment_rate_percent": Decimal("30"),
}
# #890: an above-rate payment beside a refund that can't be attributed to it.
_ALLOCATION_UNPROVEN: dict[str, Any] = {
    "treaty_rate_status": _UNPROVEN,
    "payment_allocation_unproven": True,
    "tax_paid_rate_percent": Decimal("26"),
    "highest_payment_rate_percent": Decimal("31"),
    "eligible_foreign_tax_inr": None,
}

_VALID_TREATY_RATE_STATUSES: dict[str, dict[str, Any]] = {
    "within": {},
    "above": _ABOVE_RATE,
    # #890: one payment above the rate, averaged by others to a lower country rate.
    "above-with-country-rate-below-treaty-rate": {
        **_ABOVE_RATE,
        "tax_paid_rate_percent": Decimal("16"),
    },
    "unproven-with-col-7": {**_NO_SOURCE_RATE, "tax_payable_normal_inr": Decimal("3000")},
    "payment-allocation-unproven-refund": {
        **_ALLOCATION_UNPROVEN,
        "tax_payable_normal_inr": Decimal("3000"),
    },
    "payment-allocation-unproven-same-day": {
        **_ALLOCATION_UNPROVEN,
        "tax_paid_rate_percent": Decimal("15"),
        "highest_payment_rate_percent": None,
    },
    "no-treaty-rate": {
        "treaty_rate_status": None,
        "section_90_dtaa_article": None,
        "section_90_dtaa_rate_percent": None,
        "highest_payment_rate_percent": None,
        "eligible_foreign_tax_inr": None,
    },
}

_BROKEN_TREATY_RATE_STATUSES: dict[str, dict[str, Any]] = {
    "treaty-rate-without-status": {"treaty_rate_status": None},
    "status-without-treaty-rate": {
        "section_90_dtaa_article": None,
        "section_90_dtaa_rate_percent": None,
    },
    "status-not-the-enum": {**_ABOVE_RATE, "treaty_rate_status": "above"},
    "within-without-source-rate": {"tax_paid_rate_percent": None},
    "above-without-source-rate": {**_ABOVE_RATE, "tax_paid_rate_percent": None},
    "within-without-payment-rate": {"highest_payment_rate_percent": None},
    "above-without-payment-rate": {**_ABOVE_RATE, "highest_payment_rate_percent": None},
    "within-above-treaty-rate": {"highest_payment_rate_percent": Decimal("30")},
    "above-at-treaty-rate": {**_ABOVE_RATE, "highest_payment_rate_percent": Decimal("25")},
    "above-below-treaty-rate": {**_ABOVE_RATE, "highest_payment_rate_percent": Decimal("20")},
    "within-eligible-below-foreign-tax": {"eligible_foreign_tax_inr": Decimal("2000")},
    "above-without-eligible": {**_ABOVE_RATE, "eligible_foreign_tax_inr": None},
    "no-treaty-rate-with-payment-rate": {
        "treaty_rate_status": None,
        "section_90_dtaa_article": None,
        "section_90_dtaa_rate_percent": None,
        "eligible_foreign_tax_inr": None,
    },
    "unproven-with-source-rate": {**_NO_SOURCE_RATE, "tax_paid_rate_percent": Decimal("25")},
    "unproven-with-payment-rate": {
        **_NO_SOURCE_RATE,
        "highest_payment_rate_percent": Decimal("30"),
    },
    "payment-allocation-unproven-with-within": {"payment_allocation_unproven": True},
    "payment-allocation-unproven-with-above": {**_ABOVE_RATE, "payment_allocation_unproven": True},
    "payment-allocation-unproven-not-a-bool": {
        **_ALLOCATION_UNPROVEN,
        "payment_allocation_unproven": 1,
    },
    "payment-allocation-unproven-without-source-rate": {
        **_ALLOCATION_UNPROVEN,
        "tax_paid_rate_percent": None,
    },
    "payment-allocation-unproven-at-treaty-rate": {
        **_ALLOCATION_UNPROVEN,
        "highest_payment_rate_percent": Decimal("25"),
    },
    "payment-allocation-unproven-with-eligible": {
        **_ALLOCATION_UNPROVEN,
        "eligible_foreign_tax_inr": Decimal("2500"),
    },
    "payment-allocation-unproven-with-section-90-credit": {
        **_ALLOCATION_UNPROVEN,
        "tax_payable_normal_inr": Decimal("3000"),
        "section_90_credit_inr": Decimal("2500"),
        "total_credit_inr": Decimal("2500"),
    },
    "payment-allocation-unproven-with-zero-col-13": {
        **_ALLOCATION_UNPROVEN,
        "total_credit_inr": Decimal("0"),
    },
    "unproven-with-eligible": {**_NO_SOURCE_RATE, "eligible_foreign_tax_inr": Decimal("2500")},
    "unproven-with-section-90-credit": {
        **_NO_SOURCE_RATE,
        "tax_payable_normal_inr": Decimal("3000"),
        "section_90_credit_inr": Decimal("2500"),
        "total_credit_inr": Decimal("2500"),
    },
    "unproven-with-zero-col-13": {**_NO_SOURCE_RATE, "total_credit_inr": Decimal("0")},
    "unproven-with-positive-col-13": {**_NO_SOURCE_RATE, "total_credit_inr": Decimal("100")},
}

# The run-report row repeats the entry's invariant, so both are held to it.
_STATUS_CARRIERS = pytest.mark.parametrize(
    "cls", [Form67Entry, Form67PerEntryRow], ids=lambda cls: cls.__name__
)
# A report row's readiness where its treaty status is refused first.
_BLANK = Form67RowReadiness(Form67Readiness.BLANK)


@_STATUS_CARRIERS
@pytest.mark.parametrize("case", sorted(_VALID_TREATY_RATE_STATUSES))
def test_treaty_rate_status_accepts_consistent_fields(cls: type, case: str) -> None:
    kwargs = _treaty_kwargs(**_VALID_TREATY_RATE_STATUSES[case])
    if cls is Form67PerEntryRow:
        kwargs["readiness"] = form67_row_readiness(Form67Entry(**kwargs))
    assert cls(**kwargs).treaty_rate_status is kwargs["treaty_rate_status"]


@_STATUS_CARRIERS
@pytest.mark.parametrize("case", sorted(_BROKEN_TREATY_RATE_STATUSES))
def test_treaty_rate_status_rejects_inconsistent_fields(cls: type, case: str) -> None:
    kwargs = _treaty_kwargs(**_BROKEN_TREATY_RATE_STATUSES[case])
    if cls is Form67PerEntryRow:
        kwargs["readiness"] = _BLANK
    with pytest.raises(InvariantError, match=rf"{cls.__name__}\.treaty_rate_status"):
        cls(**kwargs)


@pytest.mark.parametrize(
    "overrides",
    [
        {"section_90_dtaa_rate_percent": Decimal("NaN")},
        {"section_90_dtaa_rate_percent": Decimal("sNaN")},
        {"section_90_dtaa_rate_percent": "25"},
        {"eligible_foreign_tax_inr": Decimal("sNaN")},
    ],
    ids=[
        "nan-treaty-rate",
        "signaling-nan-treaty-rate",
        "string-treaty-rate",
        "signaling-nan-eligible",
    ],
)
def test_report_row_rejects_undecidable_treaty_comparisons(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvariantError, match=r"Form67PerEntryRow\.treaty_rate_status"):
        Form67PerEntryRow(**_treaty_kwargs(**overrides), readiness=_BLANK)


@pytest.mark.parametrize("status", [{}, _ABOVE_RATE], ids=["within", "above"])
@pytest.mark.parametrize("rate", ["NaN", "sNaN"])
def test_report_row_rejects_unordered_treaty_rate_with_trapping_disabled(
    status: dict[str, Any], rate: str
) -> None:
    with localcontext() as context:
        context.traps[InvalidOperation] = False
        with pytest.raises(InvariantError, match=r"Form67PerEntryRow\.treaty_rate_status"):
            Form67PerEntryRow(
                **_treaty_kwargs(**status, section_90_dtaa_rate_percent=Decimal(rate)),
                readiness=_BLANK,
            )


@_STATUS_CARRIERS
@pytest.mark.parametrize("rate", ["0", "-5", "100.01", "NaN", "sNaN"])
def test_highest_payment_rate_must_be_a_percentage(cls: type, rate: str) -> None:
    kwargs = _treaty_kwargs(highest_payment_rate_percent=Decimal(rate))
    if cls is Form67PerEntryRow:
        kwargs["readiness"] = _BLANK
    with pytest.raises(InvariantError, match=rf"{cls.__name__}\.highest_payment_rate_percent"):
        cls(**kwargs)


@_STATUS_CARRIERS
@pytest.mark.parametrize("rate", [25, 25.0, True])
def test_highest_payment_rate_must_be_a_decimal(cls: type, rate: object) -> None:
    kwargs = _treaty_kwargs(highest_payment_rate_percent=rate)
    if cls is Form67PerEntryRow:
        kwargs["readiness"] = _BLANK
    with pytest.raises(InvariantError, match=rf"{cls.__name__}\.highest_payment_rate_percent"):
        cls(**kwargs)


# ---- §91 credit invariants ------------------------------------------------


def test_row_rejects_section_91_credit_exceeding_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="section_91_credit_inr"):
        Form67Entry(
            **_valid_kwargs(
                foreign_tax_inr=Decimal("100"),
                foreign_tax_rate_percent=Decimal("1"),
                income_inr=Decimal("10000"),
                section_91_credit_inr=Decimal("150"),
            )
        )


# ---- §90/§90A vs §91 mutual exclusion -------------------------------------


def test_row_rejects_both_section_90_and_section_91_credit() -> None:
    """A (country, source) is covered by EITHER §90/§90A (DTAA) OR §91
    (non-DTAA), not both."""
    with pytest.raises(InvariantError, match="mutually exclusive"):
        Form67Entry(
            **_valid_kwargs(
                eligible_foreign_tax_inr=Decimal("2500"),
                section_90_credit_inr=Decimal("1000"),
                section_91_credit_inr=Decimal("1500"),
                total_credit_inr=Decimal("2500"),
            )
        )


# ---- total_credit_inr invariants ------------------------------------------


def test_row_rejects_total_credit_exceeding_foreign_tax() -> None:
    with pytest.raises(InvariantError, match="total_credit_inr"):
        Form67Entry(
            **_valid_kwargs(
                foreign_tax_inr=Decimal("100"),
                foreign_tax_rate_percent=Decimal("1"),
                income_inr=Decimal("10000"),
                total_credit_inr=Decimal("150"),
            )
        )


def test_row_rejects_total_credit_mismatched_with_populated_branch() -> None:
    """If §90 credit is populated, total MUST equal the §90 amount —
    surface upstream relief-computation bugs."""
    with pytest.raises(InvariantError, match="total_credit_inr"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article="Article 10(2)(b)",
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("25"),
                highest_payment_rate_percent=Decimal("25"),
                treaty_rate_status=Form67TreatyRateStatus.WITHIN,
                eligible_foreign_tax_inr=Decimal("2500"),
                section_90_credit_inr=Decimal("1000"),
                total_credit_inr=Decimal("1500"),  # doesn't match §90
            )
        )


# ---- §90 DTAA sub-column invariants ---------------------------------------


def test_row_rejects_whitespace_only_dtaa_article() -> None:
    with pytest.raises(InvariantError) as exc:
        Form67Entry(**_valid_kwargs(section_90_dtaa_article="   "))
    assert exc.value.what == (
        "Form67Entry.section_90_dtaa_article must be a non-empty string when provided "
        "(None for v1 skeleton)"
    )


def test_row_rejects_negative_dtaa_rate_percent() -> None:
    with pytest.raises(InvariantError, match="section_90_dtaa_rate_percent"):
        Form67Entry(**_valid_kwargs(section_90_dtaa_rate_percent=Decimal("-1")))


# ---- dataclass shape ------------------------------------------------------


def test_row_is_frozen() -> None:
    row = Form67Entry(**_valid_kwargs())
    with pytest.raises(AttributeError):
        row.serial_number = 99  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_row_is_hashable() -> None:
    row1 = Form67Entry(**_valid_kwargs())
    row2 = Form67Entry(**_valid_kwargs())
    assert hash(row1) == hash(row2)
    assert {row1, row2} == {row1}


# ---- enum value coverage (verbatim from Form 67 PDF) ----------------------


def test_source_of_income_enum_values_match_form_67_pdf_verbatim() -> None:
    """Verbatim from the official Form 67 PDF retrieved 2026-06-04
    from archive.org Wayback Machine snapshot 20230629195202 of
    incometaxindia.gov.in/forms/income-tax%20rules/itr62form67.pdf
    (Part A item 5, column 3 row labels)."""
    assert Form67SourceOfIncome.SALARY.value == "Salary"
    assert Form67SourceOfIncome.HOUSE_PROPERTY.value == "House property"
    assert Form67SourceOfIncome.BUSINESS_INCOME.value == "Business/professional income"
    assert Form67SourceOfIncome.LONG_TERM_CAPITAL_GAIN.value == "Long term capital gain"
    assert Form67SourceOfIncome.SHORT_TERM_CAPITAL_GAIN.value == "Short term capital gain"
    assert Form67SourceOfIncome.INTEREST_INCOME.value == "Interest income"
    assert Form67SourceOfIncome.DIVIDEND.value == "Dividend"
    assert Form67SourceOfIncome.ROYALTY.value == "Royalty not being part of business income"
    assert (
        Form67SourceOfIncome.FEES_FOR_TECHNICAL_SERVICES.value
        == "Fees for technical services not being part of business income"
    )
    assert Form67SourceOfIncome.OTHERS.value == "Others (specify)"


# ---- regression: T3 r1 P2 total_credit_inr without source (PR #221) ----


def test_row_rejects_non_zero_total_credit_when_both_branches_none() -> None:
    """T3 r1 P2 of PR #221: total_credit_inr cannot be non-zero
    when both §90/§90A and §91 credits are None — a total without
    a populated source column (11)/(12) would claim FTC with no
    statutory basis."""
    with pytest.raises(InvariantError, match="without a populated source column"):
        Form67Entry(
            **_valid_kwargs(
                section_90_credit_inr=None,
                section_91_credit_inr=None,
                total_credit_inr=Decimal("2500"),
            )
        )


def test_row_accepts_zero_total_credit_when_both_branches_none() -> None:
    """Zero total with both branches None is permitted (e.g., a
    placeholder for 'computed total = 0' cases)."""
    row = Form67Entry(
        **_valid_kwargs(
            section_90_credit_inr=None,
            section_91_credit_inr=None,
            total_credit_inr=Decimal("0"),
        )
    )
    assert row.total_credit_inr == Decimal("0")


# ---- regression: T4 r3 P2 symmetric total_credit_inr / source-column (PR #221) ----


def test_row_rejects_section_90_credit_populated_without_total_credit() -> None:
    """T4 r3 P2 of PR #221: symmetric companion to the (non-zero
    total, both branches None) guard. A caller that populates
    section_90_credit_inr but leaves total_credit_inr=None must
    fail construction — the portal writes col 13 (Total FTC)
    directly from total_credit_inr, so silently passing this state
    would drop the FTC claim from the portal submission."""
    with pytest.raises(InvariantError, match="total_credit_inr must be provided"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article="Article 10(2)(b)",
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("25"),
                highest_payment_rate_percent=Decimal("25"),
                treaty_rate_status=Form67TreatyRateStatus.WITHIN,
                eligible_foreign_tax_inr=Decimal("2500"),
                section_90_credit_inr=Decimal("2500"),
                section_91_credit_inr=None,
                total_credit_inr=None,
            )
        )


def test_row_rejects_section_91_credit_populated_without_total_credit() -> None:
    """T4 r3 P2 of PR #221: same symmetric guard for the §91
    (non-DTAA) branch."""
    with pytest.raises(InvariantError, match="total_credit_inr must be provided"):
        Form67Entry(
            **_valid_kwargs(
                section_90_credit_inr=None,
                section_91_credit_inr=Decimal("2500"),
                total_credit_inr=None,
            )
        )


# ---- regression: T3 r5 P3 #1 §90/§91 ↔ DTAA metadata coupling (PR #221) ----


def test_row_rejects_section_90_credit_without_dtaa_article() -> None:
    """T3 r5 P3 #1 of PR #221: a populated §90/§90A credit (col 11)
    requires the DTAA article (col 9). Claiming treaty relief
    without citing the article would leave col 9 blank on the
    portal submission."""
    with pytest.raises(InvariantError, match="section_90_dtaa_article"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article=None,
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("25"),
                highest_payment_rate_percent=Decimal("25"),
                treaty_rate_status=Form67TreatyRateStatus.WITHIN,
                eligible_foreign_tax_inr=Decimal("2500"),
                section_90_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )


def test_row_rejects_section_90_credit_without_dtaa_rate() -> None:
    """T3 r5 P3 #1 of PR #221: a populated §90/§90A credit (col 11)
    requires the DTAA rate (col 10) — the portal expects col 10
    populated alongside col 11."""
    with pytest.raises(InvariantError, match="section_90_dtaa_rate_percent"):
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article="Article 10(2)(b)",
                section_90_dtaa_rate_percent=None,
                eligible_foreign_tax_inr=Decimal("2500"),
                section_90_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )


def test_row_rejects_section_91_credit_with_dtaa_article() -> None:
    """T3 r5 P3 #1 of PR #221: §91 unilateral relief applies only
    to NON-DTAA countries, so col 9 (DTAA article) must be None
    whenever the §91 credit branch is populated."""
    with pytest.raises(InvariantError, match="section_90_dtaa_article"):
        Form67Entry(
            **_valid_kwargs(
                cbdt_country_code="999",
                country_name_portal="HYPOTHETICAL NON-DTAA TERRITORY",
                iso_alpha2="HX",
                section_90_dtaa_article="Article 10(2)(b)",
                section_91_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )


def test_row_rejects_section_91_credit_with_dtaa_rate() -> None:
    """T3 r5 P3 #1 of PR #221: §91 unilateral relief applies only
    to NON-DTAA countries, so col 10 (DTAA rate) must be None
    whenever the §91 credit branch is populated."""
    with pytest.raises(InvariantError, match="section_90_dtaa_rate_percent"):
        Form67Entry(
            **_valid_kwargs(
                cbdt_country_code="999",
                country_name_portal="HYPOTHETICAL NON-DTAA TERRITORY",
                iso_alpha2="HX",
                section_90_dtaa_rate_percent=Decimal("25"),
                tax_paid_rate_percent=Decimal("25"),
                highest_payment_rate_percent=Decimal("25"),
                treaty_rate_status=Form67TreatyRateStatus.WITHIN,
                eligible_foreign_tax_inr=Decimal("2500"),
                section_91_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )


# ---- filed amounts and the CBDT 14-digit bound (#830) -----------------------


def _filing(field: str, amount: Decimal) -> dict[str, Any]:
    """Valid kwargs filing ``amount`` in ``field``.

    A credit can't exceed the foreign tax, so a credit row files ``amount`` as
    its income and foreign tax too, and as both its section and total credit.
    """
    if field == "income_inr":
        return _valid_kwargs(
            income_inr=amount, foreign_tax_rate_percent=Decimal("2500.00") / amount * 100
        )
    if field == "foreign_tax_inr":
        return _valid_kwargs(
            foreign_tax_inr=amount, foreign_tax_rate_percent=amount / Decimal("10000.00") * 100
        )
    if field.startswith("tax_payable_"):
        return _valid_kwargs(**{field: amount})
    credit = {
        "income_inr": amount,
        "foreign_tax_inr": amount,
        "foreign_tax_rate_percent": Decimal("100"),
        "total_credit_inr": amount,
    }
    if field == "section_91_credit_inr":
        return _valid_kwargs(
            **credit,
            cbdt_country_code="999",
            country_name_portal="HYPOTHETICAL NON-DTAA TERRITORY",
            iso_alpha2="HX",
            section_91_credit_inr=amount,
        )
    return _valid_kwargs(
        **credit,
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("25"),
        tax_paid_rate_percent=Decimal("100"),
        highest_payment_rate_percent=Decimal("100"),
        treaty_rate_status=Form67TreatyRateStatus.ABOVE,
        eligible_foreign_tax_inr=amount,
        section_90_credit_inr=amount,
    )


# Each filed amount, and the field its refusal names: the total credit always
# equals its section credit, which is checked first.
_FILED_AMOUNTS = [
    ("income_inr", "income_inr"),
    ("foreign_tax_inr", "foreign_tax_inr"),
    ("tax_payable_normal_inr", "tax_payable_normal_inr"),
    ("tax_payable_special_inr", "tax_payable_special_inr"),
    ("section_90_credit_inr", "section_90_credit_inr"),
    ("section_91_credit_inr", "section_91_credit_inr"),
    ("total_credit_inr", "section_90_credit_inr"),
]


@pytest.mark.parametrize(("field", "named"), _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999", "99999999999999.4"])
def test_filed_amount_rounding_within_the_cbdt_bound_accepted(
    field: str, named: str, value: str
) -> None:
    amount = Decimal(value)
    row = Form67Entry(**_filing(field, amount))
    assert getattr(row, field) == getattr(row, named) == amount


@pytest.mark.parametrize(("field", "named"), _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_filed_amount_rounding_beyond_the_cbdt_bound_rejected(
    field: str, named: str, value: str
) -> None:
    with pytest.raises(InvariantError) as exc:
        Form67Entry(**_filing(field, Decimal(value)))
    assert exc.value.what.startswith(f"Form67Entry.{named} must round to a whole-rupee amount")


def test_amount_over_fourteen_digits_refuses_before_the_portal_writer() -> None:
    """Moved from the official Form 67 CSV writer's tests: the model now
    refuses the 15-digit income that writer's own guard refused."""
    with pytest.raises(InvariantError, match=r"Form67Entry\.income_inr must round"):
        Form67Entry(**_filing("income_inr", Decimal("999999999999999")))


# ---- filed values (#898) ----------------------------------------------------


def _filed_kwargs(**overrides: Any) -> dict[str, Any]:
    """A READY section 90 entry whose every filed value needs rounding."""
    filed = {
        "income_inr": Decimal("10000.50"),
        "foreign_tax_inr": Decimal("1225.49"),
        "foreign_tax_rate_percent": Decimal("1225.49") / Decimal("10000.50") * 100,
        "tax_paid_rate_percent": Decimal("12.245"),
        "highest_payment_rate_percent": Decimal("12.245"),
        "section_90_dtaa_rate_percent": Decimal("15.005"),
        "eligible_foreign_tax_inr": Decimal("1225.49"),
        "tax_payable_normal_inr": Decimal("2000.5"),
        "tax_payable_special_inr": Decimal("0.4"),
        "section_90_credit_inr": Decimal("1225.49"),
        "total_credit_inr": Decimal("1225.49"),
    }
    return _treaty_kwargs(**(filed | overrides))


def test_filed_values_round_amounts_to_whole_rupees_and_rates_to_two_places() -> None:
    filed = Form67Entry(**_filed_kwargs()).filed
    assert filed == Form67FiledValues(
        income_inr=Decimal("10001"),
        foreign_tax_inr=Decimal("1225"),
        tax_paid_rate_percent=Decimal("12.25"),
        tax_payable_normal_inr=Decimal("2001"),
        tax_payable_special_inr=Decimal("0"),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("15.01"),
        section_90_credit_inr=Decimal("1225"),
        section_91_credit_inr=None,
        total_credit_inr=Decimal("1225"),
    )
    assert filed.section_90_dtaa_article_number == "10"
    assert filed.claimed_credit_inr == Decimal("1225")
    assert [str(value) for value in (filed.income_inr, filed.tax_paid_rate_percent)] == [
        "10001",
        "12.25",
    ]


def test_filed_rate_falls_back_to_the_inr_ratio_without_a_source_rate() -> None:
    entry = Form67Entry(
        **_filed_kwargs(**_NO_SOURCE_RATE, section_90_credit_inr=None, total_credit_inr=None)
    )
    assert entry.filed.tax_paid_rate_percent == Decimal("12.25")  # 12.2542...
    assert entry.filed.claimed_credit_inr is None
    assert entry.filed.total_credit_inr is None


def test_filed_values_follow_a_replaced_entry() -> None:
    entry = Form67Entry(**_filed_kwargs())
    replaced = replace(
        entry, section_90_credit_inr=Decimal("0.49"), total_credit_inr=Decimal("0.49")
    )
    assert replaced.filed.total_credit_inr == Decimal("0")
    assert entry.filed.total_credit_inr == Decimal("1225")


def test_filed_zero_rate_is_never_negative_zero() -> None:
    filed = Form67Entry(
        **_valid_kwargs(
            section_90_dtaa_article="Article 10(2)(b)",
            section_90_dtaa_rate_percent=Decimal("-0"),
            treaty_rate_status=_ABOVE,
            tax_paid_rate_percent=Decimal("25"),
            highest_payment_rate_percent=Decimal("25"),
            eligible_foreign_tax_inr=Decimal("0"),
        )
    ).filed
    assert str(filed.section_90_dtaa_rate_percent) == "0.00"


def test_report_row_files_the_same_values_as_its_entry() -> None:
    kwargs = _filed_kwargs()
    entry = Form67Entry(**kwargs)
    row = Form67PerEntryRow(**kwargs, readiness=form67_row_readiness(entry))
    assert row.filed == entry.filed


def test_filed_rate_too_large_to_round_raises_a_decimal_error() -> None:
    """A tiny income makes an INR ratio too large to round to two places. The
    decimal error is what the CLI refuses as too large to compute (#832)."""
    entry = Form67Entry(
        **_valid_kwargs(
            income_inr=Decimal("1E-30"),
            foreign_tax_inr=Decimal("1"),
            foreign_tax_rate_percent=Decimal("1E+32"),
        )
    )
    with pytest.raises(InvalidOperation):
        entry.filed  # noqa: B018


@pytest.mark.parametrize("cls", [Form67Entry, Form67PerEntryRow], ids=lambda cls: cls.__name__)
def test_filed_values_refuse_an_infinite_rate(cls: type) -> None:
    """The model allows any rate >= 0 as the DTAA rate, so ``filed`` refuses an
    infinite one by name; the report row checks its readiness through ``filed``
    when it is built."""
    kwargs = _treaty_kwargs(section_90_dtaa_rate_percent=Decimal("Infinity"))
    extra = {"readiness": _BLANK} if cls is Form67PerEntryRow else {}
    with pytest.raises(InvariantError) as exc:
        cls(**kwargs, **extra).filed  # noqa: B018
    assert exc.value.what == (
        f"{cls.__name__}.section_90_dtaa_rate_percent must be a finite Decimal or None"
    )


# ---- DTAA citation shape (#1079) --------------------------------------------

_SHAPE = "must be a DTAA citation shaped like 'Article 10(2)(b)'"
_MALFORMED_CITATION = "Article 7(Synthetic Holder Two)x"
_ROW_CLASSES = pytest.mark.parametrize(
    "cls", [Form67Entry, Form67PerEntryRow], ids=lambda cls: cls.__name__
)


def _row(cls: type, citation: object) -> Form67Entry | Form67PerEntryRow:
    """A row without a credit, so no coupling check reads the citation."""
    extra = {"readiness": _BLANK} if cls is Form67PerEntryRow else {}
    return cls(**_treaty_kwargs(section_90_dtaa_article=citation), **extra)


@_ROW_CLASSES
@pytest.mark.parametrize(("citation", "number"), [("Article 10(2)(b)", "10"), (None, None)])
def test_filed_values_carry_the_citation_and_its_number(
    cls: type, citation: str | None, number: str | None
) -> None:
    filed = _row(cls, citation).filed
    assert filed.section_90_dtaa_article == citation
    assert filed.section_90_dtaa_article_number == number


@_ROW_CLASSES
@pytest.mark.parametrize("citation", [_MALFORMED_CITATION, "Article 8 U1234567", 10, b"Article 10"])
def test_row_refuses_a_citation_of_another_shape(cls: type, citation: object) -> None:
    with pytest.raises(InvariantError) as exc:
        _row(cls, citation)
    # The fixed text proves no part of the citation reaches the message.
    assert str(exc.value) == (
        f"InvariantError: {cls.__name__}.section_90_dtaa_article (col 9) {_SHAPE}"
    )


def test_dtaa_rate_refusal_wins_over_the_citation_shape() -> None:
    with pytest.raises(InvariantError) as exc:
        Form67Entry(
            **_valid_kwargs(
                section_90_dtaa_article=_MALFORMED_CITATION,
                section_90_dtaa_rate_percent=Decimal("-1"),
            )
        )
    assert exc.value.what == "Form67Entry.section_90_dtaa_rate_percent must be >= 0 when provided"


def test_section_91_coupling_refusal_wins_over_the_citation_shape() -> None:
    with pytest.raises(InvariantError) as exc:
        Form67Entry(
            **_valid_kwargs(
                cbdt_country_code="999",
                country_name_portal="HYPOTHETICAL NON-DTAA TERRITORY",
                iso_alpha2="HX",
                section_90_dtaa_article=_MALFORMED_CITATION,
                section_91_credit_inr=Decimal("2500"),
                total_credit_inr=Decimal("2500"),
            )
        )
    assert exc.value.what.startswith(
        "Form67Entry.section_90_dtaa_article (col 9) must be None when section_91_credit_inr"
    )


def test_hand_built_filed_values_refuse_a_malformed_citation_when_read() -> None:
    filed = replace(
        Form67Entry(**_filed_kwargs()).filed, section_90_dtaa_article=_MALFORMED_CITATION
    )
    with pytest.raises(InvariantError) as exc:
        filed.section_90_dtaa_article_number  # noqa: B018
    assert str(exc.value) == (
        f"InvariantError: Form67FiledValues.section_90_dtaa_article (col 9) {_SHAPE}"
    )
