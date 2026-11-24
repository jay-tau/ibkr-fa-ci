"""The date and number grammar the two Fidelity statement readers share.

The NetBenefits statement reader (:mod:`ibkr_fa.parser.netbenefits.statement_pdf`)
and the Fidelity CMA monthly statement reader (:mod:`ibkr_fa.parser.fidelity_cma`)
both read English ``Month D, YYYY`` dates, yearless ``MM/DD`` transaction dates and
the same number tokens. The helpers here never raise: each reader keeps its own
patterns, candidate years, window and refusals.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import re
from collections.abc import Iterable
from enum import Enum
from typing import Final

# Locale-proof full-month parsing (``%B`` is locale-sensitive; Fidelity emits
# English month names). The CMA reader joins the names, in this order, into regex
# alternations.
FULL_MONTHS: Final[dict[str, int]] = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}  # fmt: skip

# The whole-number part of every amount and price: grouped thousands or plain digits.
# Grouped thousands are matched strictly so a malformed value (``$1,23,4.00``)
# refuses rather than being silently re-grouped. Numeric classes are ASCII ``[0-9]``
# (not ``\d``, which also matches Unicode digits that ``Decimal`` would silently
# normalize — a "never normalize a filed figure" violation).
WHOLE_NUMBER: Final[str] = r"(?:[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)"
# A money amount with the ``$`` required: optional minus, ``$``, 2 decimals.
MONEY_DOLLAR_REQUIRED: Final[str] = rf"-?\${WHOLE_NUMBER}\.[0-9]{{2}}"
# A money amount with the ``$`` optional: optional minus, optional ``$``, 2 decimals.
MONEY_DOLLAR_OPTIONAL: Final[str] = rf"-?\$?{WHOLE_NUMBER}\.[0-9]{{2}}"
# A per-unit price with the ``$`` optional: no sign, optional ``$``, 4 decimals.
PRICE_DOLLAR_OPTIONAL: Final[str] = rf"\$?{WHOLE_NUMBER}\.[0-9]{{4}}"
# A yearless ``MM/DD`` transaction date: two ASCII digits, ``/``, two ASCII digits.
YEARLESS_DATE: Final[str] = r"[0-9]{2}/[0-9]{2}"

_FULL_DATE_RE: Final = re.compile(rf"({'|'.join(FULL_MONTHS)}) ([0-9]{{1,2}}), ([0-9]{{4}})")


class FullDateFailure(Enum):
    """Why :func:`full_date` returned no date."""

    MALFORMED = "malformed"
    NOT_A_CALENDAR_DATE = "not a calendar date"


def full_date(raw: str) -> _dt.date | FullDateFailure:
    """``raw`` as a ``Month D, YYYY`` date with a full English month name, or why not.

    It never raises, so a reader's refusal chains no error that names the date's parts.
    """
    match = _FULL_DATE_RE.fullmatch(raw)
    if match is None:
        return FullDateFailure.MALFORMED
    month, day, year = match.groups()
    with contextlib.suppress(ValueError):
        return _dt.date(int(year), FULL_MONTHS[month], int(day))
    return FullDateFailure.NOT_A_CALENDAR_DATE


def yearless_dates(
    month: int, day: int, years: Iterable[int], earliest: _dt.date, latest: _dt.date
) -> list[_dt.date]:
    """The dates ``month``/``day`` names in ``years`` that fall within ``earliest`` and
    ``latest``, both included. A year with no such date (``02/29`` outside a leap year,
    ``13/01``) is skipped."""
    dates: list[_dt.date] = []
    for year in years:
        try:
            candidate = _dt.date(year, month, day)
        except ValueError:
            continue
        if earliest <= candidate <= latest:
            dates.append(candidate)
    return dates
