"""Tests for :mod:`ibkr_fa.models.schedule_fsi_row`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from ibkr_fa.invariants import InvariantError
from ibkr_fa.models import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_fsi_row import ScheduleFsiFiledValues


def _row(
    *,
    cbdt_country_code: str = "2",
    country_name_portal: str = "UNITED STATES OF AMERICA",
    iso_alpha2: str = "US",
    head_of_income: HeadOfIncome = HeadOfIncome.OTHER_SOURCES,
    income_inr: Decimal = Decimal("8400.50"),
    foreign_tax_inr: Decimal = Decimal("1260.00"),
    event_count: int = 4,
) -> ScheduleFsiRow:
    """Construct a valid :class:`ScheduleFsiRow` for tests. All
    arguments default to a US "Other Sources" dividend
    aggregate; individual fields can be overridden per call."""
    return ScheduleFsiRow(
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country_name_portal,
        iso_alpha2=iso_alpha2,
        head_of_income=head_of_income,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        event_count=event_count,
    )


# ---- HeadOfIncome enum --------------------------------------------------


def test_head_of_income_members_pinned() -> None:
    """Pin the heads reachable from supported broker evidence.

    Salary and house-property income remain outside ibkr-fa's scope.
    """
    assert set(HeadOfIncome) == {
        HeadOfIncome.OTHER_SOURCES,
        HeadOfIncome.CAPITAL_GAINS,
        HeadOfIncome.BUSINESS_PROFESSION,
    }


def test_head_of_income_values_are_verbatim_portal_labels() -> None:
    """The string values are the EXACT portal labels — the writer
    renders them verbatim with no separate lookup. A typo here
    becomes a portal-form-validation error at upload time, so the
    pinning is critical.
    """
    assert HeadOfIncome.OTHER_SOURCES.value == "Other Sources"
    assert HeadOfIncome.CAPITAL_GAINS.value == "Capital Gains"
    assert HeadOfIncome.BUSINESS_PROFESSION.value == "Business or Profession"


# ---- ScheduleFsiRow construction ----------------------------------------


def test_valid_row_constructs() -> None:
    row = _row()
    assert row.cbdt_country_code == "2"
    assert row.country_name_portal == "UNITED STATES OF AMERICA"
    assert row.iso_alpha2 == "US"
    assert row.head_of_income is HeadOfIncome.OTHER_SOURCES
    assert row.income_inr == Decimal("8400.50")
    assert row.foreign_tax_inr == Decimal("1260.00")
    assert row.event_count == 4


def test_frozen_dataclass() -> None:
    """Mutating a ScheduleFsiRow must fail — frozen dataclass."""
    row = _row()
    with pytest.raises(FrozenInstanceError):
        row.income_inr = Decimal("9999.99")  # ty: ignore[invalid-assignment]


# ---- cbdt_country_code invariants ---------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "1",  # 1-digit (CBDT minimum)
        "44",  # 2-digit (United Kingdom)
        "2",  # 1-digit (United States)
        "1268",  # 4-digit (CBDT maximum, Antigua and Barbuda)
    ],
)
def test_valid_cbdt_country_codes_accepted(code: str) -> None:
    row = _row(cbdt_country_code=code)
    assert row.cbdt_country_code == code


@pytest.mark.parametrize(
    "bad_code",
    [
        "",  # empty
        "12345",  # 5 digits (too long)
        "US",  # alpha (must be digits)
        "1a",  # mixed
        "-1",  # negative-sign
        " 1 ",  # whitespace
        "002.0",  # decimal
        "\u00b2",  # Unicode superscript 2 — accepted by isdigit but not isdecimal
        "\u00b3",  # Unicode superscript 3 — accepted by isdigit but not isdecimal
        "\uff19\uff11",  # fullwidth digits 91 — accepted by isdecimal but not isascii
        "\u0669\u0661",  # Arabic-Indic digits 91 — accepted by isdecimal but not isascii
        "\u0967",  # Devanagari digit 1 — accepted by isdecimal but not isascii
    ],
)
def test_invalid_cbdt_country_codes_rejected(bad_code: str) -> None:
    with pytest.raises(InvariantError):
        _row(cbdt_country_code=bad_code)


def test_unicode_digit_lookalikes_cannot_bypass_india_sentinel() -> None:
    """Defence-in-depth: the India sentinel compares against ASCII
    '91' only. Without the isascii() guard, non-ASCII decimal
    digits (e.g., fullwidth U+FF19 U+FF11 or Arabic-Indic U+0669
    U+0661) would pass the cbdt_country_code validation AND skip
    the India sentinel — creating an India-shaped FSI row that
    bypasses the structural rejection. The ASCII guard catches
    these BEFORE the sentinel runs."""
    india_lookalikes = [
        "\uff19\uff11",  # FULLWIDTH DIGIT NINE + ONE
        "\u0669\u0661",  # ARABIC-INDIC DIGIT NINE + ONE
    ]
    for india_lookalike in india_lookalikes:
        with pytest.raises(InvariantError):
            _row(cbdt_country_code=india_lookalike, iso_alpha2="US")


# ---- country_name_portal invariants -------------------------------------


def test_empty_country_name_portal_rejected() -> None:
    with pytest.raises(InvariantError):
        _row(country_name_portal="")


def test_whitespace_only_country_name_portal_rejected() -> None:
    """Whitespace-only would silently pass a naive ``!= ""`` check
    but produces a blank cell on the portal upload — reject at
    construction time."""
    with pytest.raises(InvariantError):
        _row(country_name_portal="   ")


def test_mixed_case_country_name_portal_rejected() -> None:
    """Portal labels are ALL-CAPS. A mixed-case name indicates a
    corrupted upstream country-codes vendor row — reject at
    construction time rather than letting the portal validation
    fail at upload time."""
    with pytest.raises(InvariantError):
        _row(country_name_portal="United States of America")


# ---- India structural rejection (Schedule FSI = "outside India") --------


def test_iso_alpha2_india_rejected() -> None:
    """Schedule FSI is by definition 'income from outside India'
    (CBDT portal). India must be structurally impossible at the
    row layer — direct construction with ``iso_alpha2="IN"``
    must raise even if the CBDT code is something other than
    91 (defensive guard)."""
    with pytest.raises(InvariantError):
        _row(cbdt_country_code="2", iso_alpha2="IN")


def test_cbdt_code_91_india_rejected() -> None:
    """CBDT code 91 = India. Must be structurally rejected even
    if iso_alpha2 is something other than IN (defensive guard;
    the two fields are independent in the dataclass)."""
    with pytest.raises(InvariantError):
        _row(cbdt_country_code="91", iso_alpha2="US")


def test_india_full_combination_rejected() -> None:
    """The fully-Indian combination — CBDT 91 + IN + 'INDIA' —
    must be rejected with the India guard's error message."""
    with pytest.raises(InvariantError):
        _row(
            cbdt_country_code="91",
            country_name_portal="INDIA",
            iso_alpha2="IN",
        )


# ---- iso_alpha2 invariants ----------------------------------------------


@pytest.mark.parametrize(
    "bad_iso",
    [
        "",
        "U",  # 1 char
        "USA",  # 3 char
        "us",  # lowercase
        "Us",  # mixed case
        "U1",  # alphanumeric
        "12",  # digits
        " U",  # whitespace
    ],
)
def test_invalid_iso_alpha2_rejected(bad_iso: str) -> None:
    with pytest.raises(InvariantError):
        _row(iso_alpha2=bad_iso)


def test_non_ascii_capital_in_iso_alpha2_rejected() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``."""
    with pytest.raises(InvariantError) as exc:
        _row(iso_alpha2="ÜS")
    assert exc.value.what == (
        "ScheduleFsiRow.iso_alpha2 must be a 2-letter uppercase ASCII string (ISO 3166-1 alpha-2)"
    )
    assert exc.value.context == (("iso_alpha2", None),)


# ---- income_inr invariants ----------------------------------------------


def test_zero_income_rejected() -> None:
    """Schedule FSI only reports POSITIVE foreign income. A zero-
    income (country, head) bucket has nothing to report — the
    compute layer must not materialise such rows."""
    with pytest.raises(InvariantError):
        _row(income_inr=Decimal("0"))


def test_negative_income_rejected() -> None:
    """Loss-making capital gains aggregate to a NET POSITIVE figure
    across offsetting wins/losses before reaching FSI; per-row
    losses are tracked in Schedule CG but never independently
    surface to FSI as a negative row."""
    with pytest.raises(InvariantError):
        _row(income_inr=Decimal("-1.00"))


# ---- foreign_tax_inr invariants -----------------------------------------


def test_zero_foreign_tax_accepted() -> None:
    """A Capital Gains row commonly has zero foreign tax (US
    accounts have no foreign-source WHT on CG events); the
    aggregate must allow zero without rejecting the row."""
    row = _row(foreign_tax_inr=Decimal("0"))
    assert row.foreign_tax_inr == Decimal("0")


def test_negative_foreign_tax_rejected() -> None:
    """WHT reversals are netted at compute time; the aggregate
    reaching this model is always non-negative."""
    with pytest.raises(InvariantError):
        _row(foreign_tax_inr=Decimal("-1.00"))


# ---- event_count invariants ---------------------------------------------


@pytest.mark.parametrize("bad_count", [0, -1, -100])
def test_non_positive_event_count_rejected(bad_count: int) -> None:
    with pytest.raises(InvariantError):
        _row(event_count=bad_count)


def test_minimum_event_count_accepted() -> None:
    row = _row(event_count=1)
    assert row.event_count == 1


# ---- Capital Gains head ---------------------------------------------------


def test_capital_gains_head_with_zero_foreign_tax() -> None:
    """Cross-head smoke: a CG row with zero foreign tax (common US
    case) constructs cleanly."""
    row = _row(
        head_of_income=HeadOfIncome.CAPITAL_GAINS,
        income_inr=Decimal("125000.00"),
        foreign_tax_inr=Decimal("0"),
        event_count=2,
    )
    assert row.head_of_income is HeadOfIncome.CAPITAL_GAINS
    assert row.foreign_tax_inr == Decimal("0")


# ---- head_of_income membership (#938) ---------------------------------------


_NOT_A_HEAD = [None, *(head.value for head in HeadOfIncome)]


@pytest.mark.parametrize("value", _NOT_A_HEAD, ids=repr)
def test_head_of_income_must_be_a_head_of_income_member(value: object) -> None:
    """A raw ``"Other Sources"`` equals its member but isn't one."""
    with pytest.raises(InvariantError) as exc:
        _row(head_of_income=value)  # ty: ignore[invalid-argument-type]
    assert exc.value.what == "ScheduleFsiRow.head_of_income must be a HeadOfIncome enum member"
    assert exc.value.context == (("head_of_income", None),)


@pytest.mark.parametrize("value", _NOT_A_HEAD, ids=repr)
def test_head_of_income_must_be_a_member_through_replace(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), head_of_income=value)
    assert exc.value.what == "ScheduleFsiRow.head_of_income must be a HeadOfIncome enum member"


@pytest.mark.parametrize("head", list(HeadOfIncome))
def test_every_head_of_income_constructs(head: HeadOfIncome) -> None:
    assert _row(head_of_income=head).head_of_income is head


# ---- filed amounts and the CBDT 14-digit bound (#830) -----------------------


def _filing(field: str, amount: Decimal) -> dict[str, Decimal]:
    """Overrides filing ``amount`` in ``field``; relief can't exceed the tax paid."""
    if field == "tax_relief_inr":
        return {"foreign_tax_inr": amount, "tax_relief_inr": amount}
    return {field: amount}


_FILED_AMOUNTS = ["income_inr", "foreign_tax_inr", "tax_payable_inr", "tax_relief_inr"]


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999", "99999999999999.4"])
def test_filed_amount_rounding_within_the_cbdt_bound_accepted(field: str, value: str) -> None:
    amount = Decimal(value)
    assert getattr(replace(_row(), **_filing(field, amount)), field) == amount


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_filed_amount_rounding_beyond_the_cbdt_bound_rejected(field: str, value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), **_filing(field, Decimal(value)))
    assert exc.value.what.startswith(f"ScheduleFsiRow.{field} must round to a whole-rupee amount")


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
def test_filed_amount_must_be_a_finite_decimal(field: str) -> None:
    with pytest.raises(InvariantError, match=rf"ScheduleFsiRow\.{field} must be a finite Decimal"):
        replace(_row(), **{field: Decimal("NaN")})


# ---- filed whole-rupee values and the portal-relief flag (#900) ------------


def _ftc_row(
    *,
    foreign_tax_inr: Decimal = Decimal("300"),
    tax_payable_inr: Decimal | None = Decimal("400"),
    tax_relief_inr: Decimal | None = Decimal("250"),
) -> ScheduleFsiRow:
    """The issue's example by default: $10 of dividend with $3 withheld at
    ₹100/$, ₹400 of Indian tax, and a treaty-capped relief of ₹250."""
    return replace(
        _row(income_inr=Decimal("1000"), foreign_tax_inr=foreign_tax_inr),
        tax_payable_inr=tax_payable_inr,
        tax_relief_inr=tax_relief_inr,
        dtaa_article="Article 25(2)(a)",
    )


def test_filed_rounds_every_amount_half_up_and_keeps_blanks() -> None:
    row = replace(
        _row(income_inr=Decimal("8400.50"), foreign_tax_inr=Decimal("1260.49")),
        tax_payable_inr=Decimal("1999.5"),
        tax_relief_inr=Decimal("1260.49"),
        dtaa_article="Article 25(2)(a)",
    )

    assert row.filed == ScheduleFsiFiledValues(
        income_inr=Decimal("8401"),
        foreign_tax_inr=Decimal("1260"),
        tax_payable_inr=Decimal("2000"),
        tax_relief_inr=Decimal("1260"),
        dtaa_article="Article 25(2)(a)",
    )
    assert row.filed.dtaa_article_number == "25"
    assert _row().filed.tax_payable_inr is None
    assert _row().filed.tax_relief_inr is None
    assert _row().filed.dtaa_article is None
    assert _row().filed.dtaa_article_number is None


def test_filed_follows_replace() -> None:
    row = _ftc_row()
    assert row.filed.portal_overwrites_relief

    fixed = replace(row, tax_relief_inr=Decimal("300"))

    assert fixed.filed.tax_relief_inr == Decimal("300")
    assert not fixed.filed.portal_overwrites_relief


def test_portal_overwrites_a_treaty_capped_relief() -> None:
    filed = _ftc_row().filed

    assert filed.portal_relief_inr == Decimal("300")
    assert filed.portal_overwrites_relief


@pytest.mark.parametrize(
    ("foreign_tax", "tax_payable", "tax_relief", "portal_relief"),
    [
        # Relief equals tax paid; tax payable is higher.
        (Decimal("300"), Decimal("400"), Decimal("300"), Decimal("300")),
        # Relief equals tax payable; tax paid is higher.
        (Decimal("300"), Decimal("200"), Decimal("200"), Decimal("200")),
        # Unequal raw amounts that file the same whole rupees.
        (Decimal("641.2283"), Decimal("862.4664984"), Decimal("640.74"), Decimal("641")),
        # A computed zero: nothing payable, nothing relieved.
        (Decimal("300"), Decimal("0"), Decimal("0"), Decimal("0")),
    ],
    ids=["tax-paid", "tax-payable", "after-rounding", "zero"],
)
def test_portal_keeps_a_relief_that_matches_in_whole_rupees(
    foreign_tax: Decimal, tax_payable: Decimal, tax_relief: Decimal, portal_relief: Decimal
) -> None:
    filed = _ftc_row(
        foreign_tax_inr=foreign_tax, tax_payable_inr=tax_payable, tax_relief_inr=tax_relief
    ).filed

    assert filed.portal_relief_inr == portal_relief
    assert not filed.portal_overwrites_relief


def test_zero_relief_against_a_positive_portal_relief_is_overwritten() -> None:
    """A computed ``0`` is a filed value, not a blank, so it is compared."""
    filed = _ftc_row(tax_relief_inr=Decimal("0")).filed

    assert filed.portal_relief_inr == Decimal("300")
    assert filed.portal_overwrites_relief


@pytest.mark.parametrize(
    ("tax_payable", "tax_relief"),
    [(Decimal("400"), None), (None, Decimal("250")), (None, None)],
    ids=["blank-relief", "blank-payable", "both-blank"],
)
def test_a_blank_relief_or_tax_payable_is_not_compared(
    tax_payable: Decimal | None, tax_relief: Decimal | None
) -> None:
    filed = _ftc_row(tax_payable_inr=tax_payable, tax_relief_inr=tax_relief).filed

    assert filed.portal_relief_inr is None
    assert not filed.portal_overwrites_relief


# ---- DTAA citation shape (#1079) --------------------------------------------

_SHAPE = "must be a DTAA citation shaped like 'Article 10(2)(b)'"
_MALFORMED_CITATION = "Article 7(Synthetic Holder Two)x"


@pytest.mark.parametrize(
    "citation", [_MALFORMED_CITATION, "Article 8 U1234567", 10, {}, [], b"Article 10"], ids=repr
)
def test_dtaa_article_of_another_shape_refused(citation: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), dtaa_article=citation)
    # The fixed text proves no part of the citation reaches the message.
    assert str(exc.value) == f"InvariantError: ScheduleFsiRow.dtaa_article {_SHAPE}"


@pytest.mark.parametrize("citation", ["", "   "], ids=repr)
def test_blank_dtaa_article_refused(citation: str) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), dtaa_article=citation)
    assert exc.value.what == "ScheduleFsiRow.dtaa_article must be a non-empty string when populated"


def test_hand_built_filed_values_refuse_a_malformed_citation_when_read() -> None:
    filed = replace(_row().filed, dtaa_article=_MALFORMED_CITATION)
    with pytest.raises(InvariantError) as exc:
        filed.dtaa_article_number  # noqa: B018
    assert str(exc.value) == f"InvariantError: ScheduleFsiFiledValues.dtaa_article {_SHAPE}"


# ---- Other Sources income filed from Schedule OS (#1319) ------------------


def test_filed_income_is_the_schedule_os_rupees_when_set() -> None:
    """An Other Sources row files its Schedule OS rupees, not the HALF_UP of
    its exact income; its exact income is kept for relief and the audit."""
    row = replace(_row(income_inr=Decimal("23.5")), filed_income_inr=Decimal("23"))

    assert row.income_inr == Decimal("23.5")
    assert row.filed.income_inr == Decimal("23")
    assert _row(income_inr=Decimal("23.5")).filed.income_inr == Decimal("24")


def test_filed_income_may_be_zero() -> None:
    """A country whose shares of its Schedule OS fields round to nothing
    files ₹0, as the HALF_UP of an income below half a rupee does."""
    row = replace(_row(income_inr=Decimal("0.6")), filed_income_inr=Decimal("0"))

    assert row.filed.income_inr == Decimal("0")


@pytest.mark.parametrize(
    "head", [HeadOfIncome.CAPITAL_GAINS, HeadOfIncome.BUSINESS_PROFESSION], ids=lambda h: h.name
)
def test_filed_income_is_refused_on_other_heads(head: HeadOfIncome) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(head_of_income=head), filed_income_inr=Decimal("8400"))
    assert exc.value.what == (
        "ScheduleFsiRow.filed_income_inr must be non-negative on an Other Sources row"
    )


def test_negative_filed_income_is_refused() -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), filed_income_inr=Decimal("-1"))
    assert exc.value.what == (
        "ScheduleFsiRow.filed_income_inr must be non-negative on an Other Sources row"
    )


@pytest.mark.parametrize("value", ["8400.5", "8400.0", "8.4E+3", "-0"])
def test_filed_income_must_be_whole_rupees(value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), filed_income_inr=Decimal(value))
    assert exc.value.what == (
        "ScheduleFsiRow.filed_income_inr must be a whole-rupee amount (no fraction, exponent or -0)"
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "100000000000000",
            "ScheduleFsiRow.filed_income_inr must round to a whole-rupee amount within "
            "±99,999,999,999,999 (the CBDT 14-digit bound)",
        ),
        ("NaN", "ScheduleFsiRow.filed_income_inr must be a finite Decimal or None"),
    ],
)
def test_filed_income_must_be_a_filed_amount(value: str, message: str) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_row(), filed_income_inr=Decimal(value))
    assert exc.value.what == message
