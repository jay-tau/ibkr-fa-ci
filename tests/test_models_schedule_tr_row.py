"""Tests for :mod:`ibkr_fa.models.schedule_tr_row`."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection


def _valid_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cbdt_country_code": "2",
        "country_name_portal": "UNITED STATES OF AMERICA",
        "iso_alpha2": "US",
        "foreign_tax_inr_total": Decimal("1000"),
        "event_count": 2,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ---- TaxReliefSection enum
# ---------------------------------------------------------------------------


def test_tax_relief_section_enum_values_match_itr2_form() -> None:
    """Portal labels are EXACT: writers render them verbatim."""
    assert TaxReliefSection.SECTION_90.value == "90"
    assert TaxReliefSection.SECTION_90A.value == "90A"
    assert TaxReliefSection.SECTION_91.value == "91"


def test_tax_relief_section_is_str_enum() -> None:
    """StrEnum: members are also str instances, so writers can use
    them directly as portal-cell values."""
    assert isinstance(TaxReliefSection.SECTION_90, str)
    assert TaxReliefSection.SECTION_90 == "90"


def test_tax_relief_section_members_are_three() -> None:
    """Pin the enum surface so a future change (e.g., add §91A) is
    a deliberate edit, not an accidental addition."""
    assert {s.value for s in TaxReliefSection} == {"90", "90A", "91"}


# ---------------------------------------------------------------------------
# ---- ScheduleTrRow CBDT country code invariants
# ---------------------------------------------------------------------------


def test_row_rejects_non_decimal_cbdt_code() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(cbdt_country_code="US"))
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(cbdt_country_code="2A"))


def test_row_rejects_empty_cbdt_code() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(cbdt_country_code=""))


def test_row_rejects_too_long_cbdt_code() -> None:
    """1-4 character range — 5-char string rejected."""
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(cbdt_country_code="12345"))


def test_row_rejects_fullwidth_or_arabic_indic_digits_in_cbdt() -> None:
    """isdecimal() alone would accept fullwidth digits; we require
    isascii() too so the India-sentinel ASCII '91' check below works.
    """
    # Arabic-Indic digit for "9" (U+0669)
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(cbdt_country_code="\u0669"))


def test_row_accepts_1_through_4_digit_cbdt_codes() -> None:
    """CBDT vendored codes span 1-4 digits."""
    for code in ["2", "44", "111", "1268"]:
        row = ScheduleTrRow(
            **_valid_kwargs(
                cbdt_country_code=code,
                # The 1268 country is Antigua and Barbuda; use a
                # different iso_alpha2 to satisfy the India guard.
                iso_alpha2="US",  # arbitrary non-IN
            )
        )
        assert row.cbdt_country_code == code


# ---------------------------------------------------------------------------
# ---- ScheduleTrRow country name + ISO invariants
# ---------------------------------------------------------------------------


def test_row_rejects_empty_country_name() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(country_name_portal=""))


def test_row_rejects_whitespace_only_country_name() -> None:
    """T4 r1 nit: pin the `strip() != ""` short-circuit explicitly.
    `"   ".isupper()` is False (no cased characters) so the
    `isupper()` check would catch it too, but the strip-then-empty
    guard fires earlier — verify both code paths reject."""
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(country_name_portal="   "))


def test_row_rejects_mixed_case_country_name() -> None:
    """ALL-CAPS is the portal-form convention; mixed-case indicates
    a corrupted upstream country-codes vendor row."""
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(country_name_portal="United States Of America"))


def test_row_rejects_lowercase_iso() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(iso_alpha2="us"))


def test_row_rejects_short_iso() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(iso_alpha2="U"))


def test_row_rejects_long_iso() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(iso_alpha2="USA"))


def test_row_rejects_numeric_iso() -> None:
    """ISO 3166-1 alpha-2 is exactly 2 LETTERS."""
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(iso_alpha2="12"))


# ---------------------------------------------------------------------------
# ---- India guard (Schedule TR is taxes paid OUTSIDE India only)
# ---------------------------------------------------------------------------


def test_row_rejects_india_iso() -> None:
    with pytest.raises(InvariantError, match="OUTSIDE India"):
        ScheduleTrRow(
            **_valid_kwargs(
                iso_alpha2="IN",
                cbdt_country_code="2",  # decoy non-India CBDT
                country_name_portal="INDIA",
            )
        )


def test_row_rejects_india_cbdt_code() -> None:
    with pytest.raises(InvariantError, match="OUTSIDE India"):
        ScheduleTrRow(
            **_valid_kwargs(
                iso_alpha2="US",  # decoy non-India ISO
                cbdt_country_code="91",
            )
        )


# ---------------------------------------------------------------------------
# ---- foreign_tax_inr_total + event_count invariants
# ---------------------------------------------------------------------------


def test_row_rejects_zero_foreign_tax() -> None:
    """Schedule TR only reports countries where positive foreign tax
    was paid (the entire relief framework is meaningless at zero)."""
    with pytest.raises(InvariantError, match="positive"):
        ScheduleTrRow(**_valid_kwargs(foreign_tax_inr_total=Decimal("0")))


def test_row_rejects_negative_foreign_tax() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(foreign_tax_inr_total=Decimal("-100")))


def test_row_rejects_zero_event_count() -> None:
    with pytest.raises(InvariantError, match=">= 1"):
        ScheduleTrRow(**_valid_kwargs(event_count=0))


def test_row_rejects_negative_event_count() -> None:
    with pytest.raises(InvariantError):
        ScheduleTrRow(**_valid_kwargs(event_count=-1))


# ---------------------------------------------------------------------------
# ---- optional tax_relief_inr_total + section_under_which_claimed
# ---------------------------------------------------------------------------


def test_row_accepts_none_for_optional_fields() -> None:
    """Optional fields default to None for the 'not computed; user
    must fill' contract from PR #212."""
    row = ScheduleTrRow(**_valid_kwargs())
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


def test_row_accepts_optional_fields_when_populated() -> None:
    row = ScheduleTrRow(
        **_valid_kwargs(
            tax_relief_inr_total=Decimal("500"),
            section_under_which_claimed=TaxReliefSection.SECTION_90,
        )
    )
    assert row.tax_relief_inr_total == Decimal("500")
    assert row.section_under_which_claimed is TaxReliefSection.SECTION_90


def test_row_accepts_zero_tax_relief_when_computed() -> None:
    """An explicit zero relief with its section is accepted and stays
    distinct from None. See compute/schedule_tr.py for the fill rules."""
    # PR #222: zero relief still requires section_under_which_claimed
    # populated per the symmetric coupling invariant.
    row = ScheduleTrRow(
        **_valid_kwargs(
            tax_relief_inr_total=Decimal("0"),
            section_under_which_claimed=TaxReliefSection.SECTION_90,
        )
    )
    assert row.tax_relief_inr_total == Decimal("0")
    assert row.tax_relief_inr_total is not None  # NOT None — explicitly computed zero


def test_row_rejects_negative_tax_relief() -> None:
    with pytest.raises(InvariantError, match="non-negative"):
        ScheduleTrRow(
            **_valid_kwargs(
                tax_relief_inr_total=Decimal("-1"),
                section_under_which_claimed=TaxReliefSection.SECTION_90,
            )
        )


def test_row_rejects_non_taxrelief_section_string() -> None:
    """T3 r1 P3 fix: section_under_which_claimed must be a
    TaxReliefSection enum member, not a raw string. Static typing
    catches this at type-check time but a runtime caller could
    pass a string like "91A" (non-existent section); StrEnum's
    value-equality would silently accept it without enum membership."""
    # PR #222: section requires accompanying tax_relief per the
    # symmetric coupling invariant; supply a value so the original
    # isinstance check is the one that fires.
    with pytest.raises(InvariantError, match="TaxReliefSection enum member"):
        ScheduleTrRow(
            **_valid_kwargs(
                tax_relief_inr_total=Decimal("100"),
                section_under_which_claimed="91A",  # type: ignore[arg-type]
            ),
        )


def test_row_rejects_tax_relief_exceeding_foreign_tax_paid() -> None:
    """A supplied relief above foreign_tax_inr_total is rejected."""
    with pytest.raises(InvariantError, match="<= foreign_tax_inr_total"):
        ScheduleTrRow(
            **_valid_kwargs(
                foreign_tax_inr_total=Decimal("500"),
                tax_relief_inr_total=Decimal("501"),
                section_under_which_claimed=TaxReliefSection.SECTION_90,
            )
        )


def test_row_accepts_tax_relief_equal_to_foreign_tax_paid() -> None:
    """A supplied relief equal to foreign_tax_inr_total is accepted."""
    row = ScheduleTrRow(
        **_valid_kwargs(
            foreign_tax_inr_total=Decimal("500"),
            tax_relief_inr_total=Decimal("500"),
            section_under_which_claimed=TaxReliefSection.SECTION_90,
        )
    )
    assert row.tax_relief_inr_total == row.foreign_tax_inr_total


# ---------------------------------------------------------------------------
# ---- PR #222: symmetric tax_relief_inr_total ↔ section_under_which_claimed coupling
# ---------------------------------------------------------------------------


def test_row_rejects_tax_relief_without_section() -> None:
    """PR #222 forward direction: a populated relief amount without a
    statutory section reference is structurally inconsistent. The
    portal cannot accept a relief claim without naming §90 / §90A /
    §91. Parallel to Form67Entry's §90 credit ↔ DTAA article
    coupling at form67_entry.py:433-457 (PR #221 r6)."""
    with pytest.raises(InvariantError, match="both be set or both be None"):
        ScheduleTrRow(
            **_valid_kwargs(
                tax_relief_inr_total=Decimal("500"),
                section_under_which_claimed=None,
            )
        )


def test_row_rejects_section_without_tax_relief() -> None:
    """PR #222 converse direction: a section reference without an
    accompanying relief amount is structurally inconsistent (no
    claim is being made). Symmetric to the forward direction
    above."""
    with pytest.raises(InvariantError, match="both be set or both be None"):
        ScheduleTrRow(
            **_valid_kwargs(
                tax_relief_inr_total=None,
                section_under_which_claimed=TaxReliefSection.SECTION_90,
            )
        )


def test_row_accepts_both_relief_and_section_populated() -> None:
    """The legitimate populated case: both fields supplied together."""
    row = ScheduleTrRow(
        **_valid_kwargs(
            tax_relief_inr_total=Decimal("500"),
            section_under_which_claimed=TaxReliefSection.SECTION_91,
        )
    )
    assert row.tax_relief_inr_total == Decimal("500")
    assert row.section_under_which_claimed is TaxReliefSection.SECTION_91


def test_row_accepts_both_relief_and_section_none() -> None:
    """Both coupled fields None is valid: it is how the compute layer
    leaves a country's relief for manual fill ('not computed; user
    must fill')."""
    row = ScheduleTrRow(**_valid_kwargs())
    assert row.tax_relief_inr_total is None
    assert row.section_under_which_claimed is None


# ---------------------------------------------------------------------------
# ---- foreign_tax_inr_total_full_precision (T2/T3 r2 audit-cell fix #216)
# ---------------------------------------------------------------------------


def test_row_accepts_none_full_precision_for_legacy_back_compat() -> None:
    """Legacy direct constructors (pre-PR-#216 test helpers, etc.)
    omit foreign_tax_inr_total_full_precision; the field defaults
    to None and writers fall back to foreign_tax_inr_total for
    the audit cell."""
    row = ScheduleTrRow(**_valid_kwargs())
    assert row.foreign_tax_inr_total_full_precision is None


def test_row_accepts_populated_full_precision_independent_of_portal_total() -> None:
    """Compute-layer construction populates both fields. Their
    values can diverge by construction (per-row rounding); there
    is NO ordering invariant between them."""
    row = ScheduleTrRow(
        **_valid_kwargs(
            foreign_tax_inr_total=Decimal("4"),
            foreign_tax_inr_total_full_precision=Decimal("3.00"),
        )
    )
    assert row.foreign_tax_inr_total == Decimal("4")
    assert row.foreign_tax_inr_total_full_precision == Decimal("3.00")


def test_row_accepts_zero_full_precision() -> None:
    """Zero full-precision is permitted (e.g., test fixture that
    rounds many tiny non-zero FSI rows up to whole rupees while
    full-precision sum stays at zero — unrealistic but the
    invariant is purely `>= 0`)."""
    row = ScheduleTrRow(**_valid_kwargs(foreign_tax_inr_total_full_precision=Decimal("0")))
    assert row.foreign_tax_inr_total_full_precision == Decimal("0")


def test_row_rejects_negative_full_precision() -> None:
    with pytest.raises(InvariantError, match="full_precision"):
        ScheduleTrRow(**_valid_kwargs(foreign_tax_inr_total_full_precision=Decimal("-0.01")))


def test_legacy_positional_tax_relief_binds_to_tax_relief_not_full_precision() -> None:
    """T2 r3 P3 regression for PR #216: when ``foreign_tax_inr_total_full_precision``
    was originally added between ``event_count`` and
    ``tax_relief_inr_total``, a positional caller of the form
    ``ScheduleTrRow(cbdt, country, iso, fta, ec, Decimal(50))``
    silently re-bound ``Decimal(50)`` from
    ``tax_relief_inr_total`` to the new full-precision field.
    Reordering moved ``foreign_tax_inr_total_full_precision`` to
    the END of the field list so legacy positional binding
    continues to work. This test pins that contract.

    Mirrors the PR #189 T5 r2 regression test pattern.

    PR #222: the symmetric coupling invariant requires
    section_under_which_claimed to accompany tax_relief_inr_total,
    so the test now passes both as positionals (fields 6 and 7).
    """
    row = ScheduleTrRow(
        "2",
        "UNITED STATES OF AMERICA",
        "US",
        Decimal("1000"),
        2,
        Decimal("50"),  # 6th positional → MUST bind to tax_relief_inr_total
        TaxReliefSection.SECTION_90,  # 7th positional → section_under_which_claimed
    )
    assert row.tax_relief_inr_total == Decimal("50")
    assert row.section_under_which_claimed is TaxReliefSection.SECTION_90
    assert row.foreign_tax_inr_total_full_precision is None  # NOT the 6th positional


# ---------------------------------------------------------------------------
# ---- dataclass shape
# ---------------------------------------------------------------------------


def test_row_is_frozen() -> None:
    row = ScheduleTrRow(**_valid_kwargs())
    with pytest.raises(AttributeError):
        row.cbdt_country_code = "44"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_row_is_hashable() -> None:
    """Frozen dataclasses with slots are hashable by default — useful
    for set deduplication during aggregation tests."""
    row1 = ScheduleTrRow(**_valid_kwargs())
    row2 = ScheduleTrRow(**_valid_kwargs())
    assert hash(row1) == hash(row2)
    assert {row1, row2} == {row1}


# ---------------------------------------------------------------------------
# ---- filed amounts and the CBDT 14-digit bound (#830)
# ---------------------------------------------------------------------------


def _filing(field: str, amount: Decimal) -> dict[str, Any]:
    """Overrides filing ``amount`` in ``field``; relief can't exceed the tax paid."""
    if field == "tax_relief_inr_total":
        return _valid_kwargs(
            foreign_tax_inr_total=amount,
            tax_relief_inr_total=amount,
            section_under_which_claimed=TaxReliefSection.SECTION_90,
        )
    return _valid_kwargs(**{field: amount})


_FILED_AMOUNTS = ["foreign_tax_inr_total", "tax_relief_inr_total"]


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
def test_filed_amount_at_the_cbdt_bound_accepted(field: str) -> None:
    amount = Decimal("99999999999999")
    assert getattr(ScheduleTrRow(**_filing(field, amount)), field) == amount


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["1.500", "1.499", "500.00", "5E+2", "-0", "99999999999999.4"])
def test_filed_amount_that_is_not_whole_rupees_rejected(field: str, value: str) -> None:
    """#900: writers render the totals as they are, so a fraction, an exponent
    form or ``-0`` is refused rather than rounded (or printed) by a writer."""
    with pytest.raises(InvariantError) as exc:
        ScheduleTrRow(**_filing(field, Decimal(value)))
    assert exc.value.what == (
        f"ScheduleTrRow.{field} must be a whole-rupee amount (no fraction, exponent or -0)"
    )


@pytest.mark.parametrize("field", _FILED_AMOUNTS)
@pytest.mark.parametrize("value", ["99999999999999.5", "100000000000000", "1E100"])
def test_filed_amount_rounding_beyond_the_cbdt_bound_rejected(field: str, value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        ScheduleTrRow(**_filing(field, Decimal(value)))
    assert exc.value.what.startswith(f"ScheduleTrRow.{field} must round to a whole-rupee amount")


def test_audit_full_precision_total_beyond_the_cbdt_bound_accepted() -> None:
    row = ScheduleTrRow(**_valid_kwargs(foreign_tax_inr_total_full_precision=Decimal("1E100")))
    assert row.foreign_tax_inr_total_full_precision == Decimal("1E100")
