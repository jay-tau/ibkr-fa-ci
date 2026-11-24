"""Shared date-parsing helpers for IBKR Flex CSV section parsers.

IBKR encodes dates in two distinct compact formats across Flex Query
sections:

* **Bare**: ``"YYYYMMDD"`` (e.g., ``"20250610"``). Used in
  Open Positions LOT (``ReportDate``, ``OpenDateTime``,
  ``HoldingPeriodDateTime``) and in the simple Trades schema.
* **Date + time**: ``"YYYYMMDD;HHMMSS"`` (e.g., ``"20250610;143015"``)
  — note the ``;`` separator. Used in the LOT-detail Trades variant
  for ``OpenDateTime`` and ``HoldingPeriodDateTime`` on
  ``LevelOfDetail == "CLOSED_LOT"`` rows.

Schedule FA computations only ever need calendar dates, so both
helpers return :class:`datetime.date`. The time component (when
present) is parsed and validated to catch malformed input but is
otherwise discarded.

Why two helpers (vs one lenient helper that accepts both shapes):
the bare format is the strict contract for Open Positions LOT — a
``;HHMMSS`` suffix there would indicate a Flex Query configuration
mistake, and we want to surface it as a :class:`ParseError` rather
than silently accept it. Each call site picks the helper that
matches its column's documented shape.

:func:`refuse_shifted_holding_period` is the #599 check the Flex Open
Positions parser runs on the two parsed dates. Flex closed lots carry
the holding date instead, and the engine checks them (#640).
"""

from __future__ import annotations

import datetime as _dt
from typing import Final

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site

_YYYYMMDD_LENGTH: Final[int] = 8
_HHMMSS_LENGTH: Final[int] = 6
# Full "YYYYMMDD;HHMMSS" is 8 + 1 + 6 = 15 characters.
_YYYYMMDD_HHMMSS_LENGTH: Final[int] = _YYYYMMDD_LENGTH + 1 + _HHMMSS_LENGTH


def _to_date(raw_date: str, *, section: str, row_index: int, column: str) -> _dt.date:
    try:
        return _dt.date(
            year=int(raw_date[:4]),
            month=int(raw_date[4:6]),
            day=int(raw_date[6:8]),
        )
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    # Fixed text, after the handler, chaining only a stand-in for the ValueError:
    # on Python 3.14 it can name the year, month and day.
    raise ParseError(
        section=section,
        row_index=row_index,
        column=column,
        message="must be a real calendar date",
        raw_value="",
    ) from crash


def parse_yyyymmdd(raw: str, *, section: str, row_index: int, column: str) -> _dt.date:
    """Parse IBKR's bare ``YYYYMMDD`` date encoding into a :class:`date`.

    Args:
        raw: The cell value (e.g., ``"20250610"``).
        section: Section name for :class:`ParseError` context.
        row_index: Zero-based row index within the section.
        column: Column name for :class:`ParseError` context.

    Returns:
        The parsed calendar date.

    Raises:
        ParseError: If ``raw`` is not exactly 8 digits, or is 8 digits
            that don't form a real calendar date (e.g., ``"20260230"``).
            The error carries no cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name.
            Nor does its exception graph: the calendar error, which on
            Python 3.14 names the date's parts, is replaced by a
            content-free stand-in cause (#1226).
    """
    if len(raw) != _YYYYMMDD_LENGTH or not raw.isdigit():
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be exactly 8 digits in YYYYMMDD format",
            raw_value="",
        )
    return _to_date(raw, section=section, row_index=row_index, column=column)


def parse_yyyymmdd_with_time(raw: str, *, section: str, row_index: int, column: str) -> _dt.date:
    """Parse IBKR's ``YYYYMMDD;HHMMSS`` date+time encoding into a :class:`date`.

    The time component is validated (must be 6 digits and a real
    HH:MM:SS) but discarded — Schedule FA only needs calendar dates.

    Args:
        raw: The cell value (e.g., ``"20250610;143015"``).
        section: Section name for :class:`ParseError` context.
        row_index: Zero-based row index within the section.
        column: Column name for :class:`ParseError` context.

    Returns:
        The parsed calendar date (time component validated then dropped).

    Raises:
        ParseError: If ``raw`` is not exactly ``YYYYMMDD;HHMMSS`` shape
            (15 chars, ``;`` at index 8, both halves all digits) or if
            the date / time components don't represent a real instant
            (e.g., ``"20260230;000000"`` or ``"20250610;249999"``).
            The error carries no cell value, in its message or
            ``raw_value``: a shifted cell can hold the holder's name.
            Nor does its exception graph: the calendar or clock error,
            which on Python 3.14 names the date's or time's parts, is
            replaced by a content-free stand-in cause (#1226).
    """
    if (
        len(raw) != _YYYYMMDD_HHMMSS_LENGTH
        or raw[_YYYYMMDD_LENGTH] != ";"
        or not raw[:_YYYYMMDD_LENGTH].isdigit()
        or not raw[_YYYYMMDD_LENGTH + 1 :].isdigit()
    ):
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be in YYYYMMDD;HHMMSS format (e.g. '20250610;143015')",
            raw_value="",
        )
    date_part = raw[:_YYYYMMDD_LENGTH]
    time_part = raw[_YYYYMMDD_LENGTH + 1 :]
    parsed = _to_date(date_part, section=section, row_index=row_index, column=column)
    # Validate the time component (we discard it but want to fail
    # loudly on malformed input rather than silently accept "249999").
    crash = None
    try:
        _dt.time(
            hour=int(time_part[:2]),
            minute=int(time_part[2:4]),
            second=int(time_part[4:6]),
        )
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        # After the handler: on Python 3.14 the ValueError names the invalid clock part.
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must have a real wall-clock time in HHMMSS",
            raw_value="",
        ) from crash
    return parsed


def refuse_shifted_holding_period(
    open_date: _dt.date,
    holding_date: _dt.date,
    *,
    section: str,
    row_index: int,
    cost_column: str,
) -> None:
    """Refuse a Flex lot whose holding-period date differs from its open date.

    IBKR documents that a US wash sale moves ``HoldingPeriodDateTime``,
    and the lot's ``cost_column`` may then include the US
    disallowed-loss add-back, which the Indian return does not
    recognise. Flex can't show the actual cost, so refuse (#599).

    Raises:
        ParseError: If the two calendar dates differ, naming the
            ``HoldingPeriodDateTime`` column. The error names neither
            date nor the lot's symbol or ISIN.
    """
    if holding_date != open_date:
        raise ParseError(
            section=section,
            row_index=row_index,
            column="HoldingPeriodDateTime",
            message=(
                f"HoldingPeriodDateTime differs from OpenDateTime. IBKR documents a shifted "
                f"holding-period date for US wash sales, so {cost_column} "
                f"may include a US wash-sale adjustment that the Indian "
                f"return does not recognise. Re-run schedule-fa with an "
                f"IBKR Activity Statement that covers this lot's "
                f"acquisition; it uses the actual opening-trade cost. "
                f"LD-coded (wash-sale) Activity Statement rows are still "
                f"refused there (https://github.com/jay-tau/ibkr-fa/issues/265)."
            ),
            raw_value="",
        )
