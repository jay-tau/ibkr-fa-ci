"""The exact-arithmetic refusal fragments (#1740), shared by every test that pins them.

Each refusal puts its own label (the schedule, row, column or operation) in
front of one of these fixed texts. Tests pin these literals rather than reading
them from :mod:`ibkr_fa`, so a changed wording fails them.
"""

from __future__ import annotations

from typing import Final

INEXACT_SUM: Final = "sum must be exact within Decimal precision"
INEXACT_PRODUCT: Final = "product must be exact within Decimal precision"
TOO_MANY_DIGITS: Final = "needs more digits than exact arithmetic allows"
