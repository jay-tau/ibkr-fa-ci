"""Indian-tax compliance helpers (BMA, ITR-form selection, ...)."""

from __future__ import annotations

from ibkr_fa.compliance.black_money_act import BMA_DISCLOSURE_ADVISORY
from ibkr_fa.compliance.foreign_tax_credit import (
    MISSING_INDIAN_TAX_PAYABLE_WARNING,
    missing_indian_tax_payable_warning,
)
from ibkr_fa.compliance.itr_form_selection import ITR_FORM_SELECTION_ADVISORY

__all__ = [
    "BMA_DISCLOSURE_ADVISORY",
    "ITR_FORM_SELECTION_ADVISORY",
    "MISSING_INDIAN_TAX_PAYABLE_WARNING",
    "missing_indian_tax_payable_warning",
]
