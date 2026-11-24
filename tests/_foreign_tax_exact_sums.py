"""Synthetic withholding rows and #1167's privacy markers, shared by the exact
foreign-tax sum compute and CLI tests (#1167).

- ``PAYMENT_DATE`` and ``ISIN``: the plain withholding row's payment date and
  synthetic ISIN.
- ``MARKED_SYMBOL``, ``MARKED_ISIN``, ``MARKED_DESCRIPTION``, ``MARKED_TEXT``,
  ``MARKED_PAYMENT_DATE`` and ``MARKED_AMOUNT``: the marked instrument's fields;
  ``MARKERS`` lists every marker, none of which a refusal or captured output
  may print.
- ``wht``: a USD withholding row of ``amount`` on the plain instrument.
- ``marked_wht``: a USD withholding row on the marked instrument.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Isin, Ticker

PAYMENT_DATE = date(2025, 7, 15)
ISIN = Isin("US99992A1097")

# #1167's privacy markers: a symbol, an ISIN-shaped identifier, a description,
# a source or reason, a payment date and a 29-digit amount. Withholding rows
# and dividend lines have no source or reason field, so SYNTHSOURCE2B rides in
# the description. No refusal or captured output may print one.
MARKED_SYMBOL = "SYNTHTAX2B"
MARKED_ISIN = Isin("US99999Q2001")
MARKED_DESCRIPTION = "SYNTHDESC2B"
MARKED_TEXT = f"{MARKED_DESCRIPTION} SYNTHSOURCE2B"
MARKED_PAYMENT_DATE = date(2025, 6, 17)
MARKED_AMOUNT = "1.2345678901234567890123456789"
MARKERS = (
    MARKED_SYMBOL,
    MARKED_ISIN,
    MARKED_DESCRIPTION,
    "SYNTHSOURCE2B",
    MARKED_PAYMENT_DATE.isoformat(),
    MARKED_AMOUNT,
)


def wht(amount: str | Decimal, *, isin: Isin = ISIN, on: date = PAYMENT_DATE) -> WithholdingTax:
    return WithholdingTax(
        symbol=Ticker("XQAQ"),
        isin=isin,
        payment_date=on,
        amount=Decimal(amount),
        currency="USD",
        description="XQAQ Cash Dividend - Tax",
    )


def marked_wht(
    amount: str = "-" + MARKED_AMOUNT, *, on: date = MARKED_PAYMENT_DATE
) -> WithholdingTax:
    """Withholding on the marked instrument, by default the 29-digit amount paid
    on the marked date."""
    return WithholdingTax(
        symbol=Ticker(MARKED_SYMBOL),
        isin=MARKED_ISIN,
        payment_date=on,
        amount=Decimal(amount),
        currency="USD",
        description=MARKED_TEXT,
    )
