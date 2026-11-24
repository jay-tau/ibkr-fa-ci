"""How NetBenefits refusals name a statement among every report, year-end reports
included, numbered by period start.

The income normalizer and the Schedule FA proofs share this label, so a refusal
names a statement the same way wherever it fires.
"""

from __future__ import annotations

from typing import Final

ALL_REPORTS_ORDER: Final = "by period start, year-end reports included"


def statement_among_all_reports(number: int) -> str:
    """Name the 1-based ``number``-th report, such as ``statement 2 (by period
    start, year-end reports included)``."""
    return f"statement {number} ({ALL_REPORTS_ORDER})"
