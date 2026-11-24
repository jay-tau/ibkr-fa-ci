"""One recipient copy of IRS Form 1042-S, shared by every issuer's parser.

Issuer parsers (Fidelity CMA, Robinhood) read different layouts into the
same box types. A field that a parser does not read is ``None``, never a
default ``False`` or ``0``, so form-level checks skip it rather than pass it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.invariants import require


class Form1042SAmountKind(StrEnum):
    """Distinguish blank, IRS dash-zero, and a printed numeric amount."""

    BLANK = "blank"
    DASH_ZERO = "dash-zero"
    AMOUNT = "amount"


@dataclass(frozen=True, slots=True, repr=False)
class Form1042SAmount:
    """One monetary box without blank-to-zero coercion.

    ``value`` is the box's amount, or ``None`` when the box is blank.
    """

    kind: Form1042SAmountKind
    value: Decimal | None

    def __post_init__(self) -> None:
        if self.kind is Form1042SAmountKind.BLANK:
            require(self.value is None, what="blank Form 1042-S amount cannot carry a value")
        else:
            require(
                self.value is not None and self.value.is_finite() and self.value >= 0,
                what="non-blank Form 1042-S amount must be finite and non-negative",
            )


class Form1042SCopyType(StrEnum):
    """Recipient copy designations."""

    B = "B"
    C = "C"
    D = "D"


@dataclass(frozen=True, slots=True, repr=False)
class Form1042SCopy:
    """Boxes 1-11 and 15 of one physical recipient copy."""

    copy_type: Form1042SCopyType
    unique_form_identifier: str | None
    tax_year: int
    amended: bool
    amendment_number: str | None
    income_code: str
    gross_income: Form1042SAmount
    chapter_indicator: str | None
    chapter3_exemption_code: str
    chapter3_tax_rate_percent: Decimal
    chapter4_exemption_code: str
    chapter4_tax_rate_percent: Decimal
    withholding_allowance: Form1042SAmount
    net_income: Form1042SAmount
    federal_tax_withheld: Form1042SAmount
    federal_tax_not_deposited: bool | None
    subsequent_year_withholding: bool | None
    qualified_intermediary: bool | None
    tax_withheld_other_agents: Form1042SAmount
    overwithheld_tax_repaid: Form1042SAmount
    total_withholding_credit: Form1042SAmount
    tax_paid_by_agent: Form1042SAmount
    pro_rata_reporting: bool | None
