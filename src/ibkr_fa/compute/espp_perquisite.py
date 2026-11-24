"""ESPP discount perquisite Form-16 advisory (#I12 / #317).

The Microsoft ESPP discount (FMV at purchase - the discounted contribution paid)
is a **salary perquisite** under §17(2), taxable in the purchase year and normally
already included by the employer in the employee's **Form 16**. ``ibkr-fa`` does
not compute salary, so this is a *reminder* to verify it — plus the warning that
the future capital-gains cost of acquisition is the **FMV** (§49(2AA)), not the
discounted price actually paid.

This is a pure compute helper: it takes the already-parsed, already-cross-checked
:class:`~ibkr_fa.parser.netbenefits.confirmation_pdf.EspPurchase` (whose
``perquisite_usd == fmv_total_usd - contribution_usd`` identity the #309 parser
enforces) plus the SBI TTBR for the purchase date, and returns the structured
advisory. It produces **no filed figure** — the INR is indicative (the employer's
Form-16 FX governs); rendering lives in the CLI.

The perquisite INR uses the salary FX rule, Rule 115(1), Explanation
**(2)(a)** — TTBR of the last day of the month immediately preceding the
perquisite month — which the caller supplies (the specified date coincides
with the Explanation (2)(e) dividend rule already implemented by
:class:`~ibkr_fa.fx.policy_rule115.Rule115Policy`). When no rate is available
the INR is left ``None`` and the CLI degrades to a USD-only reminder rather
than failing the run for a non-filed advisory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase

_INDIAN_FY_START_MONTH = 4  # April


def financial_year_label(day: date) -> str:
    """Indian financial year (April-March) of ``day``, e.g. ``"2025-26"``."""
    start_year = day.year if day.month >= _INDIAN_FY_START_MONTH else day.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


@dataclass(frozen=True, slots=True)
class EsppPerquisiteAdvisory:
    """One ESPP purchase's perquisite verification advisory (advisory only)."""

    purchase_date: date
    financial_year: str
    perquisite_usd: Decimal
    perquisite_inr: int | None
    fmv_total_usd: Decimal
    contribution_usd: Decimal


def build_espp_perquisite_advisory(
    purchase: EspPurchase, *, perquisite_inr_rate: Decimal | None
) -> EsppPerquisiteAdvisory:
    """Build the advisory for one ESPP purchase.

    ``perquisite_inr_rate`` is the salary-perquisite SBI TTBR (Rule 115(1), Explanation (2)(a),
    preceding-month-end) for ``purchase.purchase_date``, or ``None`` if it could
    not be resolved (the INR is then left ``None`` — the CLI shows USD only).
    """
    perquisite_inr = (
        None
        if perquisite_inr_rate is None
        else int((purchase.perquisite_usd * perquisite_inr_rate).quantize(0, ROUND_HALF_UP))
    )
    return EsppPerquisiteAdvisory(
        purchase_date=purchase.purchase_date,
        financial_year=financial_year_label(purchase.purchase_date),
        perquisite_usd=purchase.perquisite_usd,
        perquisite_inr=perquisite_inr,
        fmv_total_usd=purchase.fmv_total_usd,
        contribution_usd=purchase.contribution_usd,
    )
