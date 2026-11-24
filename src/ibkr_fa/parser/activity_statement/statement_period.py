"""Parse the ``Statement`` section's reporting period.

Activity Statement exports begin with a ``Statement`` section that
includes a ``Period`` field in the form
``"<Month> <Day>, <Year> - <Month> <Day>, <Year>"`` (e.g.,
``"January 1, 2025 - December 31, 2025"``).

This module exposes the shared :func:`parse_statement_period` parser.
Which window a report accepts is the caller's policy: the validators
live in :mod:`ibkr_fa.compute.source_coverage` (#625).

The Activity Statement also lacks a per-row report date on Open
Positions (unlike Flex's LOT-detail rows). The end date returned
by :func:`parse_statement_period` is what downstream code (Trades
→ OpenLot reconstruction and the Open Positions cross-check) uses
as ``report_date``.

:func:`parse_statement_broker_names` reads the same section's
``BrokerName`` rows, which name the IBKR company that carries the
account (e.g., ``"Interactive Brokers LLC"``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Final

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site
from ibkr_fa.models.source import StatementPeriod

_SECTION: Final[str] = "Statement"
_PERIOD_FIELD: Final[str] = "Period"
_BROKER_NAME_FIELD: Final[str] = "BrokerName"

# ``"January 1, 2025 - December 31, 2025"`` with arbitrary
# whitespace around the separator. The month-name and ordinal-day
# parsing is delegated to a name-to-number map below so we get
# locale-independent behaviour (``datetime.strptime`` with ``%B``
# depends on the active locale).
_PERIOD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*
    (?P<sm>[A-Za-z]+)\s+(?P<sd>\d{1,2}),\s*(?P<sy>\d{4})
    \s*-\s*
    (?P<em>[A-Za-z]+)\s+(?P<ed>\d{1,2}),\s*(?P<ey>\d{4})
    \s*$
    """,
    re.VERBOSE,
)

# Spelled with the leading capital IBKR uses; matched
# case-insensitively in the parser so a stray locale variation
# doesn't trip the comparison. ``January``..``December`` only —
# IBKR doesn't abbreviate months in this field.
_MONTHS: Final[dict[str, int]] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _fields(rows: Iterable[dict[str, str]]) -> Iterator[tuple[str, str]]:
    """Yield each ``Statement`` row as ``(Field Name, Field Value)``.

    Raises:
        ParseError: If a row lacks the ``Field Name`` or
            ``Field Value`` column.
    """
    for row_index, row in enumerate(rows):
        try:
            yield row["Field Name"], row["Field Value"]
        except KeyError as exc:
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column=str(exc.args[0]),
                message=(
                    f"Statement section row is missing the "
                    f"{exc.args[0]!r} column; expected columns "
                    f"('Field Name', 'Field Value')."
                ),
                raw_value="",
            ) from exc


def _find_period_value(rows: Iterable[dict[str, str]]) -> tuple[int, str]:
    """Return the only ``Period`` row's index in the ``Statement`` rows and its value.

    Reads every row, so each is checked for its two columns.

    Raises:
        ParseError: If a row lacks the ``Field Name`` or ``Field Value``
            column. At the second ``Period`` row, if there is more than
            one, whether or not they agree. If no row in the ``Statement``
            section has ``Field Name == "Period"``: without the period we
            cannot validate the CY constraint, so refuse to continue.
    """
    periods = [
        (row_index, value)
        for row_index, (name, value) in enumerate(_fields(rows))
        if name == _PERIOD_FIELD
    ]
    if len(periods) > 1:
        raise ParseError(
            section=_SECTION,
            row_index=periods[1][0],
            column=_PERIOD_FIELD,
            message=(
                "the Statement section has more than one Period row; refusing to "
                "choose one, because the period decides which events the report "
                "covers. Re-export the Activity Statement as one statement for one "
                "period."
            ),
            raw_value="",
        )
    if periods:
        return periods[0]

    raise ParseError(
        section=_SECTION,
        row_index=0,
        column=_PERIOD_FIELD,
        message=(
            f"Statement section is missing the {_PERIOD_FIELD!r} "
            f"field. Cannot validate the reporting period without "
            f"it. Re-export the Activity Statement; IBKR always "
            f"emits this field."
        ),
        raw_value="",
    )


def _parse_period_string(period: str, *, row_index: int) -> StatementPeriod:
    """Parse ``"January 1, 2025 - December 31, 2025"`` into dates.

    Args:
        period: The ``Period`` row's ``Field Value``.
        row_index: The ``Period`` row's index in the ``Statement`` rows.

    Raises:
        ParseError: If the string doesn't match the expected shape,
            uses an unknown month name, names a calendar-invalid
            date (e.g., ``February 30``) or a start after its end.
            The error names the ``Period`` row and carries no cell
            value, in its message or ``raw_value``: a shifted cell can
            hold the holder's name or account number. Nor does its
            exception graph: the month lookup's error, which names the
            month, and the calendar error, which on Python 3.14 names the
            date's parts, are replaced by a content-free stand-in cause
            (#1226).
    """
    match = _PERIOD_PATTERN.match(period)
    if match is None:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=_PERIOD_FIELD,
            message=(
                "the Period doesn't match the expected shape "
                "'<Month> <Day>, <Year> - <Month> <Day>, <Year>' "
                "(e.g., 'January 1, 2025 - December 31, 2025')."
            ),
            raw_value="",
        )

    crash = None
    try:
        start_month = _MONTHS[match.group("sm").lower()]
        end_month = _MONTHS[match.group("em").lower()]
    except KeyError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        # After the handler: the KeyError names the month.
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=_PERIOD_FIELD,
            message=(
                f"the Period contains an unrecognised month name; expected "
                f"one of {sorted(_MONTHS)}."
            ),
            raw_value="",
        ) from crash

    try:
        start = date(int(match.group("sy")), start_month, int(match.group("sd")))
        end = date(int(match.group("ey")), end_month, int(match.group("ed")))
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        # Fixed text, after the handler: on Python 3.14 the ValueError can name
        # the date's parts.
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=_PERIOD_FIELD,
            message="the Period contains a date that isn't a real calendar date.",
            raw_value="",
        ) from crash

    if start > end:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=_PERIOD_FIELD,
            message=(
                "the Period's start date is after its end date; refusing to "
                "interpret a reverse range."
            ),
            raw_value="",
        )

    return StatementPeriod(start_date=start, end_date=end)


def parse_statement_period(rows: Iterable[dict[str, str]]) -> StatementPeriod:
    """Locate and parse the ``Period`` field in the Statement section.

    Args:
        rows: Data rows from a ``Statement`` section returned by
            :func:`split_activity_statement_sections`. Row format:
            ``{"Field Name": ..., "Field Value": ...}``.

    Returns:
        The parsed :class:`StatementPeriod`.

    Raises:
        ParseError: If a row lacks the ``Field Name`` or ``Field Value``
            column, or the ``Period`` field is missing, appears in more than
            one row (named at the second, whether or not the rows agree),
            is malformed, uses an unknown month name, names an invalid
            date, or starts after it ends. For a malformed ``Period``, the
            error names its row; no error carries a cell value. Period-vs-calendar-year
            validation is performed separately by
            :func:`~ibkr_fa.compute.source_coverage.require_calendar_year_period`.
    """
    row_index, period_str = _find_period_value(rows)
    return _parse_period_string(period_str, row_index=row_index)


def parse_statement_broker_names(rows: Iterable[dict[str, str]]) -> tuple[str, ...]:
    """Return every ``BrokerName`` value in the ``Statement`` rows, in file order.

    Values are returned as written, blank ones included, and how many
    there are is not checked: which broker names a report accepts is
    the caller's policy (#665).

    Raises:
        ParseError: If a row lacks the ``Field Name`` or
            ``Field Value`` column.
    """
    return tuple(value for name, value in _fields(rows) if name == _BROKER_NAME_FIELD)
