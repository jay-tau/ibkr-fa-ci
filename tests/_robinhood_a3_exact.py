"""Shared values for the exact Robinhood Schedule FA A3 share tests (#1243).

Owner decision (2026-11-10, on #1166): these shares never round. Carried cost
rationals, split closing shares and credit shares stay exact rationals until
the row's filed rounding, and refuse only past the digit cap. Costs without a
separate rational keep the existing Decimal initial-value product (#1250).
"""

from __future__ import annotations

from typing import Final

from tests._exact_refusals import TOO_MANY_DIGITS

# Privacy markers: each sits on the refusing run's input, and none may reach a
# refusal, its chain or the CLI output.
DESCRIPTION: Final = "SYNTHA3DESC"
SYMBOL: Final = "ZQXT"
DIGIT_RUN: Final = "736252"
MARKERS: Final = (DESCRIPTION, SYMBOL, DIGIT_RUN)
# A quantity one digit past the cap's 1,000 places after the units digit.
TINY_QUANTITY: Final = "0." + "0" * 1000 + "1"


def share_refusal(share: str) -> str:
    """The complete rendered digit-cap refusal for a ``closing`` or ``credit`` share."""
    return (
        f"InvariantError: Schedule FA A3, Robinhood security 1: the {share} share {TOO_MANY_DIGITS}"
    )
