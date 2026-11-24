"""The shared "Indian tax calculation required" warning's remedy (#1427, #1446)."""

from __future__ import annotations

from ibkr_fa.compliance import MISSING_INDIAN_TAX_PAYABLE_WARNING
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING


def test_missing_indian_tax_warning_names_every_override_its_fsi_row_needs() -> None:
    """A DIVIDEND override alone refuses when the country's Schedule FSI Other
    Sources row also holds interest or other income, so the remedy names those
    overrides too. It also equals the copy in ``tests/_ftc_warnings.py`` that
    other tests pin: this is the one test that reads it from ``src/`` (#1745)."""
    assert MISSING_INDIAN_TAX_PAYABLE_WARNING == MISSING_INDIAN_TAX_WARNING
    last_sentence = (
        " To have the tool fill it, add an exact [[form67.tax_payable_overrides]] amount "
        "for the country's DIVIDEND row and, when its Schedule FSI Other Sources row also "
        "holds interest or other income, one for INTEREST_INCOME or OTHERS too."
    )
    assert MISSING_INDIAN_TAX_PAYABLE_WARNING.endswith(
        "then enter the result manually." + last_sentence
    )
    for source in ("DIVIDEND", "INTEREST_INCOME", "OTHERS"):
        assert source in last_sentence
