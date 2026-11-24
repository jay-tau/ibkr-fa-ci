"""The Fidelity CMA capital-gains texts and inputs that several test modules
share: the FY 2025-26 statement coverage refusal texts (``CG_WINDOW``,
``LATE_START`` and ``CG_EARLY_END``); the carried SPAXX holding texts
(``HOLDING_HISTORY``, and ``CARRIED_HOLDING`` and ``LATER_HOLDING`` built from
it); ``PAST_CAP``, a SPAXX amount past the exact-arithmetic digit cap that holds
the privacy marker ``_MARKER``; ``SHARE_REFUSAL``, the digit-cap refusal's
format text; and :func:`thirds`, #1244's three-sale repro. The texts are written
out here rather than read from ``src``.

Every value is synthetic.
"""

from __future__ import annotations

from datetime import date

from ibkr_fa.parser.fidelity_cma import CmaCoreTransaction, CmaCoreTransactionKind
from tests._exact_refusals import TOO_MANY_DIGITS
from tests._fidelity_cma import core_transaction as _tx

# #1510: each coverage refusal names FY 2025-26's whole window, then every
# edge the statements miss.
CG_WINDOW = (
    "statement coverage for FY 2025-26 must run from 1 April 2025, or from the "
    "account's first statement if it opened later in the FY with no carried balance, "
    "through 31 March 2026"
)
LATE_START = (
    "the statements start after 1 April 2025 with a carried balance, so add every "
    "monthly statement from 1 April 2025 (or from the account's first statement) up to "
    "the earliest one given"
)
CG_EARLY_END = (
    "the statements end before 31 March 2026, so add every monthly statement after the "
    "latest one given through 31 March 2026"
)
# #1515: SPAXX capital gains need statements from before any carried holding.
HOLDING_HISTORY = (
    "add every monthly statement before it back to the account's first, as SPAXX capital "
    "gains need history from before any carried holding"
)
CARRIED_HOLDING = f"the earliest statement begins with a SPAXX holding, so {HOLDING_HISTORY}"
LATER_HOLDING = f"if the earliest statement then begins with a SPAXX holding, {HOLDING_HISTORY}"

_BUY = CmaCoreTransactionKind.BUY
_SELL = CmaCoreTransactionKind.SELL
# 1,008 digits, past the exact-arithmetic digit cap, holding the privacy marker.
_MARKER = "736253"
PAST_CAP = "1." + "0" * 1000 + "9" + _MARKER
SHARE_REFUSAL = "SPAXX lot allocation, statement {} activity row {}: the share " + TOO_MANY_DIGITS


def thirds() -> tuple[CmaCoreTransaction, ...]:
    """#1244's repro: 3.015 units bought for USD 3.02 and redeemed as three
    1.005-unit sales for USD 1.01 each, so each lot costs a third of USD 3.02."""
    return (
        _tx(_BUY, date(2025, 4, 1), "3.015", "1.0000", "3.02"),
        *(_tx(_SELL, date(2025, month, 1), "-1.005", "1.0000", "-1.01") for month in (5, 6, 7)),
    )
