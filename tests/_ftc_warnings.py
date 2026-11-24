"""Foreign tax credit warnings, Form 67 override advice, and the Form 67 and
Schedule FSI refusals that test modules pin, for any broker (#1689, #1745,
#1751, #1758, #1762, #1860).

Each is written out here rather than read from ``src/``, so a wording change
fails the tests that check it. ``test_compliance_foreign_tax_credit.py`` checks
the missing Indian tax warning against ``src/``.
"""

from __future__ import annotations

from typing import Final

# A relief-claiming Schedule FSI/TR row with no foreign TIN (#1084).
BLANK_RELIEF_TIN_WARNING: Final = (
    "Schedule FSI and TR leave the foreign Taxpayer Identification Number blank "
    "on a relief-claiming row; add it under [account.foreign_tins], or use your "
    "passport number if that country did not allot you a TIN"
)

# How to work out a blank Indian tax payable by hand: the missing Indian tax
# warning's text before its override sentence or per-row advice (#1469).
MANUAL_METHOD: Final = (
    "⚠️  INDIAN TAX CALCULATION REQUIRED: Schedule FSI column (d) and Form 67 "
    "column 7 are left blank where Indian tax attributable to foreign income "
    "cannot be derived from broker data. Rule 128(5)(i) limits FTC to the lower "
    "of Indian tax payable on that income and eligible foreign tax, but does "
    "not prescribe an average or marginal-rate formula. For ordinary income, "
    "Promac Engineering v ACIT (ITA 501/Bang/2018, paras 12-13) applied "
    "`foreign income x average Indian tax rate`; section 2(10) defines that "
    "average as `Indian income-tax on total income / total income`. Promac was "
    "for pre-Rule-128 AY 2014-15, and the Karnataka High Court later disposed "
    "the Revenue's appeal on monetary limits with the legal question kept open; "
    "no reported ruling was found settling average versus marginal rate under "
    "Rule 128(5)(i). Do not substitute foreign tax withheld. Confirm the method "
    "under the relevant DTAA/current law and any special-rate treatment, then "
    "enter the result manually."
)

# The missing Indian tax warning with its generic override sentence.
MISSING_INDIAN_TAX_WARNING: Final = (
    f"{MANUAL_METHOD} To have the tool fill it, add an exact "
    "[[form67.tax_payable_overrides]] amount for the country's DIVIDEND row and, "
    "when its Schedule FSI Other Sources row also holds interest or other income, "
    "one for INTEREST_INCOME or OTHERS too."
)


def blank_row_warning(*advice: str) -> str:
    """The missing Indian tax warning with each blank row's ``advice`` in place
    of its generic override sentence (#1469)."""
    return " ".join((MANUAL_METHOD, *advice))


def fractional_rate_warning(rate: str, country_code: str) -> str:
    """The warning for a fractional Form 67 Rate(%) the CSV importer may blank."""
    return (
        f"Form 67 CSV importer may blank fractional Rate(%) {rate} for country code "
        f"{country_code}; after upload, enter {rate} manually and verify it before "
        "saving. Do not round it to an integer."
    )


# ---- #1461/#1469: a BLANK Form 67 row's override advice ----
# Each gap keeps a BLANK row's credit uncomputed even with an exact
# [[form67.tax_payable_overrides]] amount. These two causes follow the row's
# "<ISO>/<SOURCE>: " prefix.

# Same-day dividends, or a withholding with no dividend or above it (#890).
SAME_DAY_GAP_CAUSE: Final = (
    "the withholding can't be matched to a single dividend payment (same-day dividends of "
    "one security in one account can't be told apart, or a payment's withholding has no "
    "dividend or exceeds it), so the treaty-eligible foreign tax, which is capped per "
    "payment (#890), and the section 90 credit can't be computed."
)

# No source-currency rate, for example after a same-year refund (#593).
SOURCE_RATE_GAP_CAUSE: Final = (
    "the source-currency tax rate can't be established (for example, a same-year refund "
    "netted the source-currency tax to zero, #593), so neither the treaty-eligible foreign "
    "tax nor the section 90 credit can be computed."
)

# The section 91 inputs a KY (no DTAA with India) row needs beside its override.
KY_SECTION_91_NEED: Final = (
    "KY/DIVIDEND: KY has no DTAA with India, so its section 91 credit also needs "
    "[form67.section_91] total_indian_tax_before_91_relief_inr and total_income_inr."
)


def same_day_gap(row: str) -> str:
    """The same-day allocation gap for ``row``, such as ``US/DIVIDEND``."""
    return f"{row}: {SAME_DAY_GAP_CAUSE}"


def source_rate_gap(row: str) -> str:
    """The unproven source-currency rate gap for ``row``."""
    return f"{row}: {SOURCE_RATE_GAP_CAUSE}"


def unvendored_dtaa_gap(iso: str) -> str:
    """The gap for a dividend row of a country with no vendored DTAA row (#249)."""
    return (
        f"{iso}/DIVIDEND: {iso} has no row in the vendored DTAA dividend table, so neither "
        "a section 90 nor a section 91 credit can be computed (#249)."
    )


CH_GAP: Final = unvendored_dtaa_gap("CH")


def override_sentence(iso: str) -> str:
    """The advice for a dividend row an exact override clears."""
    return (
        f"To have the tool fill {iso}/DIVIDEND, add an exact [[form67.tax_payable_overrides]] "
        f"amount for it and, when the {iso} Schedule FSI Other Sources row also holds interest "
        "or other income, one for INTEREST_INCOME or OTHERS too."
    )


def wont_clear(gap: str, row: str) -> str:
    """``gap``, then that no exact override clears ``row``."""
    return f"{gap} An exact [[form67.tax_payable_overrides]] amount won't clear {row}."


# ---- #1758: a REFUSED Form 67 row's refusal, and the report's no-credit line ----


def without_override(row: str) -> str:
    """How to produce the other outputs without ``row``'s override: the end of
    its refusal, with no closing period."""
    return (
        "To produce the other outputs, remove the [[form67.tax_payable_overrides]] "
        f"entry for {row}; the row is then left blank for manual Form 67 work"
    )


def refused_override(gap: str, row: str) -> str:
    """The refusal of ``row``'s override, which can't clear ``gap``."""
    return f"Form 67 cannot be produced: {gap} {without_override(row)}."


# A KY (no DTAA with India) override without the section 91 inputs.
KY_SECTION_91_REFUSAL: Final = (
    "Form 67 cannot be produced: KY/DIVIDEND: KY has no DTAA with India, and a section 91 "
    "credit needs [form67.section_91] total_indian_tax_before_91_relief_inr and "
    "total_income_inr."
)


def zero_credit_text(row: str) -> str:
    """The #601 refusal of a credit that files as ₹0, after ``row``'s
    "<ISO>/<SOURCE>: " prefix."""
    return (
        "the computed credit rounds to ₹0 in whole rupees, the amount Form 67 files; "
        "zero-credit rows are refused under the owner's #601 policy. "
        f"{without_override(row)}, and Schedule FSI tax and relief are completed by hand, "
        "entering 0 only where the whole FSI row's tax really is 0"
    )


US_ZERO_CREDIT_REFUSAL: Final = (
    f"Form 67 cannot be produced: US/DIVIDEND: {zero_credit_text('US/DIVIDEND')}."
)


def no_credit_line(row: str, reason: str) -> str:
    """The Form 67 report's line for ``row`` with Indian tax payable but no credit."""
    return f"{row}: Indian tax payable supplied, but no credit computed ({reason})"


# ---- invariant refusals ----

# The ``what`` of Form 67 readiness's invariant for a row whose col 7, claimed
# credit and col 13 are neither all computed nor all blank (src/ibkr_fa/compute/form67.py).
UNPRODUCED_WHAT: Final = (
    "Form 67 row: col 7, the claimed credit and col 13 are neither all computed "
    "nor all blank, and no named gap keeps the credit uncomputed"
)

# Schedule FSI's refusal when its relief would differ from the portal's. The
# refusal names the row by its country and head of income; the relief figures
# keep only their context keys (#1047, #1252).
PORTAL_RELIEF_REFUSAL: Final = (
    "InvariantError: Schedule FSI filing would diverge from the portal's read-only "
    "relief (min of tax paid and Indian tax payable) on the US Other Sources row; "
    "resolve the filing amounts manually before filing "
    "(context keys: country, head, computed_relief, portal_relief)"
)

# require_form67_credit_reconciles's refusal, as str(InvariantError) (#1427, #1446).
FORM67_RELIEF_REFUSAL: Final = (
    "InvariantError: Form 67 credit must equal the matching Schedule FSI relief, and that "
    "country's Schedule FSI Other Sources row also holds other income: add an exact "
    "[[form67.tax_payable_overrides]] amount for each other source in it (INTEREST_INCOME "
    "for interest; OTHERS for substitute payments and reviewed Robinhood stock-lending "
    "fees together) (context keys: country, form67_credit, fsi_relief)"
)
