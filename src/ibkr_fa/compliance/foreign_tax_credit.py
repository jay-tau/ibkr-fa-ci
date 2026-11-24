"""Foreign tax credit guidance shared by Schedule FSI, Schedule TR and Form 67."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "BLANK_RELIEF_TAXPAYER_ID_WARNING",
    "MISSING_INDIAN_TAX_PAYABLE_WARNING",
    "missing_indian_tax_payable_warning",
]

BLANK_RELIEF_TAXPAYER_ID_WARNING: Final[str] = (
    "Schedule FSI and TR leave the foreign Taxpayer Identification Number blank "
    "on a relief-claiming row; add it under [account.foreign_tins], or use your "
    "passport number if that country did not allot you a TIN"
)

# How to work out the blank Indian tax payable by hand.
_MANUAL_METHOD: Final[str] = (
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

MISSING_INDIAN_TAX_PAYABLE_WARNING: Final[str] = (
    f"{_MANUAL_METHOD} To have the tool fill it, add an exact "
    "[[form67.tax_payable_overrides]] amount for the country's DIVIDEND row and, "
    "when its Schedule FSI Other Sources row also holds interest or other income, "
    "one for INTEREST_INCOME or OTHERS too."
)


def missing_indian_tax_payable_warning(advice: Iterable[str]) -> str:
    """The warning for blank Indian tax payable: :data:`MISSING_INDIAN_TAX_PAYABLE_WARNING`
    when ``advice`` is empty, else its method followed by ``advice``, the
    per-row sentences that replace its override sentence (#1469)."""
    sentences = tuple(advice)
    if not sentences:
        return MISSING_INDIAN_TAX_PAYABLE_WARNING
    return " ".join((_MANUAL_METHOD, *sentences))
