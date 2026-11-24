"""Indian tax-year primitives: the financial year, the CY → AY mapping and
the first AY under the Income-tax Act, 2025 FX framework.

A leaf module: it imports only the stdlib and :mod:`ibkr_fa.invariants`,
so the parser, compute and output layers can use these rules without
importing :mod:`ibkr_fa.pipeline` (#603).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from ibkr_fa.invariants import require

# The earliest year that fits in :class:`datetime.date` and matches
# real-world Indian FY usage. Used as the lower bound for
# :class:`FinancialYear.start_year`.
_MIN_FINANCIAL_YEAR_START: Final[int] = 1900
_MAX_FINANCIAL_YEAR_START: Final[int] = 9998


def starting_ay_for_calendar_year(calendar_year: int) -> int:
    """Return the starting year of the AY in which a CY-N return is filed.

    Schedule FA is calendar-year based. The AY for which a CY-N return
    is filed begins April 1 of N+1 and ends March 31 of N+2, and is
    represented by its **starting** year (e.g., AY "2026-27" => 2026;
    CY 2025 returns are filed in AY 2026-27). The mapping is therefore
    ``starting_ay = calendar_year + 1``.
    """
    return calendar_year + 1


@dataclass(frozen=True, slots=True)
class FinancialYear:
    """Indian financial year (Apr 1 → Mar 31).

    Stored as the FY's **start year** only (e.g., FY 2025-26 → start_year=2025);
    the end year, AY mapping, and date boundaries derive from it
    deterministically. This matches how Indian tax law describes FYs
    ("Financial Year 2025-26", "AY 2026-27") rather than how datetimes
    represent date ranges.

    Attributes:
        start_year: 4-digit year of the FY's April 1. Must be in
            ``[1900, 9998]`` so the derived end date
            (``date(start_year + 1, 3, 31)``) fits in
            :class:`datetime.date` (whose max year is 9999).
    """

    start_year: int

    def __post_init__(self) -> None:
        require(
            self.start_year >= _MIN_FINANCIAL_YEAR_START,
            what="FinancialYear: start_year must be >= 1900 so derived dates fit datetime.date",
            start_year=self.start_year,
        )
        require(
            self.start_year <= _MAX_FINANCIAL_YEAR_START,
            what=(
                "FinancialYear: start_year must be <= 9998 so the derived "
                "end_date (date(start_year + 1, 3, 31)) fits in datetime.date"
            ),
            start_year=self.start_year,
        )

    @property
    def end_year(self) -> int:
        """4-digit year of the FY's March 31."""
        return self.start_year + 1

    @property
    def end_year_2digit(self) -> int:
        """2-digit year used in FY labels (e.g., 2025-26 → 26)."""
        return self.end_year % 100

    @property
    def start_date(self) -> date:
        """First day of the FY (April 1)."""
        return date(self.start_year, 4, 1)

    @property
    def end_date(self) -> date:
        """Last day of the FY (March 31)."""
        return date(self.end_year, 3, 31)

    @property
    def assessment_year_start(self) -> int:
        """Starting year of the AY for this FY (FY 2025-26 → 2026)."""
        return self.start_year + 1

    def __str__(self) -> str:
        """Render as 'YYYY-YY' (e.g., '2025-26')."""
        return f"{self.start_year}-{self.end_year_2digit:02d}"


# AYs are keyed by their starting year (AY 2026-27 => 2026); see
# :func:`starting_ay_for_calendar_year`.
#
# The Income-tax Act, 2025 + Income-tax Rules, 2026 framework applies
# from AY 2027-28 onwards (return for FY 2026-27 = CY 2026). For
# AY 2026-27 (CY 2025) the 1961 Act remains in force via Section 536's
# transitional saving clause (Section 536(2)(c) keeps the 1961 Act
# applicable to "proceedings … in respect of any tax year beginning
# before the 1st April, 2026"; FY 2025-26 / AY 2026-27 qualifies).
#
# Per **Rule 206(1)** of the Income-tax Rules, 2026 (the resident
# general FX-conversion rule, new equivalent of the 1962 Rules'
# Rule 115), foreign-currency income is converted to INR using the
# **telegraphic transfer buying rate** on a "specified date"
# tabulated per income head. Per **Rule 207(3)(b)** (the TTBR
# definition cross-referenced by Rule 206(2)(a)), that rate is the
# SBI's TT buying rate. **The carry-forward fallback** ("last
# published rate may be taken" when no rate is published on the
# specified date) appears only in **Rule 207(1)**, which governs
# **TDS** on income payable to non-residents/IFSC units — not in
# Rule 206. For Schedule FA reporting by a resident, the FA-specific
# date-selection semantics and carry-forward continue to rest on
# **CBDT FAQ / practice**, just as they did under the 1961-Act
# framework. ibkr-fa's implementation (SBI TTBR with carry-forward,
# exact event date per CBDT FAQ) therefore remains the most
# defensible reading under the new framework, but the
# FA-specific date rule isn't reissued in the Rules text — pending
# verification (tracked at #126 / #127), CY 2026+ runs print a
# warning but are NOT blocked.
#
# Verified via the gazetted Notification No. 22/2026 (G.S.R. 198(E))
# dated 20 March 2026 reproduced at
# https://taxguru.in/income-tax/income-tax-rules-2026.html
# and the Income-tax Act, 2025 Section 536 text at
# https://indiankanoon.org/doc/31102361/.
FIRST_AY_REQUIRING_2025_ACT_FRAMEWORK: Final[int] = 2027
