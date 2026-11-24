"""Reviewed Schedule CG filing positions, per source (#642).

The shared filing workbook reports a foreign exchange-listed security's
consideration in ITR-2 A5(a)(ii) / ITR-3 A6(a)(ii) and B8/B9(a)(ii),
"assets other than unquoted shares", and computes no Rule 11UA /
section 50CA FMV. No CBDT guidance or binding ruling directly resolves
that classification, so each source states the listing venues and
security classes whose evidence and position were reviewed (owner
decision 1 on #642). A source without a reviewed position here can't
build a filing result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ROBINHOOD_SCHEDULE_CG_FILING_POSITION",
    "SCHEDULE_CG_CLASSIFICATION_ADVISORY",
    "ScheduleCgFilingPosition",
]


@dataclass(frozen=True, slots=True)
class ScheduleCgFilingPosition:
    """The listing venues and security classes one source's filing position covers."""

    reviewed_listing_venues: frozenset[str]
    reviewed_security_classes: frozenset[str]


# ponytail: add a venue or class only after its listing evidence and filing
# position are reviewed with a primary citation.
ROBINHOOD_SCHEDULE_CG_FILING_POSITION: Final[ScheduleCgFilingPosition] = ScheduleCgFilingPosition(
    reviewed_listing_venues=frozenset({"ARCA", "NASDAQ", "NYSE"}),
    reviewed_security_classes=frozenset({"COMMON", "ETF"}),
)

SCHEDULE_CG_CLASSIFICATION_ADVISORY: Final[str] = (
    "Tax position: {security_count} foreign exchange-listed securities on "
    "{listing_venues} are reported as assets other than unquoted shares. Section "
    "50CA prescribed FMV is not computed. No CBDT guidance or binding ruling "
    "directly resolves this foreign-exchange classification."
)
