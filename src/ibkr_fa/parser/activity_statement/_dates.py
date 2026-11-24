"""Shared date parser for IBKR Activity Statement section parsers.

IBKR's per-account date-format preference varies: some accounts emit
``DD-MM-YYYY`` (IBKR India flavor, e.g. ``"18-09-2025"``) while others
emit ``YYYY-MM-DD`` (IBKR LLC / US flavor, e.g. ``"2025-12-31"``). Both
shapes are unambiguous because the 4-digit group always identifies the
year, so a single helper can accept either.

The Dividends, Withholding Tax, and Interest section parsers all share
this helper. The Interest parser (#139) was the first to accept both
formats and its docstring anticipated this lift: *"A future PR will lift
this dual-format helper to a shared module and back-port Dividends +
Withholding Tax to it."* This module is that shared helper.

The Trades section uses a distinct ``"YYYY-MM-DD, HH:MM:SS"`` *datetime*
format (with the comma) and intentionally does **not** use this helper;
see :func:`ibkr_fa.parser.activity_statement.trades._parse_full_date_time`.
Both readers refuse a date outside the statement's own Period through
:func:`require_within_period` (#1143).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Final

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site
from ibkr_fa.models import StatementPeriod

_OUTSIDE_PERIOD: Final[str] = (
    "this row is dated outside the statement's Period; a statement whose rows "
    "contradict its own period can't prove its totals. Re-export the Activity "
    "Statement for one period."
)

# DD-MM-YYYY (day-first) OR YYYY-MM-DD (ISO). The 4-digit group always
# identifies the year, so the two shapes are unambiguous. ``fullmatch``
# is used at the call site to make the exact-shape intent explicit and
# avoid any trailing-newline permissiveness of ``$``.
_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:(?P<d1>\d{2})-(?P<m1>\d{2})-(?P<y1>\d{4})"
    r"|(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2}))"
)


def require_within_period(
    day: _dt.date,
    period: StatementPeriod,
    *,
    section: str,
    row_index: int,
    column: str,
) -> None:
    """Refuse a row dated outside the statement's own Period (#1143).

    A statement whose rows fall outside its stated period contradicts
    itself. The bounds are inclusive.

    Raises:
        ParseError: If ``day`` is before ``period.start_date`` or after
            ``period.end_date``. The error names no date, period or
            cell value.
    """
    if not period.start_date <= day <= period.end_date:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=_OUTSIDE_PERIOD,
            raw_value="",
        )


def parse_statement_date(
    raw: str,
    *,
    section: str,
    row_index: int,
    period: StatementPeriod,
    column: str = "Date",
) -> _dt.date:
    """Parse an IBKR Activity Statement date in either ``DD-MM-YYYY`` or
    ``YYYY-MM-DD`` form, inside the statement's Period.

    Args:
        raw: The raw date cell value.
        section: Statement section name, used to tag the
            :class:`~ibkr_fa.exceptions.ParseError` (e.g. ``"Dividends"``).
        row_index: Zero-based index of the offending row, for diagnostics.
        period: The statement's parsed Period; the date must fall in it.
        column: Column name to tag the error with (defaults to ``"Date"``).

    Returns:
        The parsed :class:`datetime.date`.

    Raises:
        ParseError: If ``raw`` matches neither shape, matches a shape
            but is not a real calendar date such as ``"2026-02-30"``, or
            falls outside ``period`` (:func:`require_within_period`).
            The error carries no cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name.
            Nor does its exception graph: the calendar error, which on
            Python 3.14 names the date's parts, is replaced by a
            content-free stand-in cause (#1226).
    """
    match = _DATE_PATTERN.fullmatch(raw)
    if not match:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must match DD-MM-YYYY or YYYY-MM-DD",
            raw_value="",
        )
    groups = match.groupdict()
    if groups["y1"] is not None:
        year, month, day = int(groups["y1"]), int(groups["m1"]), int(groups["d1"])
    else:
        year, month, day = int(groups["y2"]), int(groups["m2"]), int(groups["d2"])
    crash = None
    try:
        parsed = _dt.date(year, month, day)
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        # Fixed text, after the handler: on Python 3.14 the ValueError can name
        # the date's parts.
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be a real calendar date",
            raw_value="",
        ) from crash
    require_within_period(parsed, period, section=section, row_index=row_index, column=column)
    return parsed
