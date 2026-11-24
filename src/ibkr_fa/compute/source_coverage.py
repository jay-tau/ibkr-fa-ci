"""Check that a source file covers the window a report needs (#625).

The parsers record what a file states; these checks decide whether that
is enough for a report:

* :func:`require_calendar_year_period` — Schedule FA Table A3
  requires an export that ends on Dec 31 of ``--calendar-year`` and
  starts on or before Jan 1, so it may hold earlier years' opening
  trades (#1288). Partial-year and FY-aligned exports are rejected.
  It's the Activity Statement analogue of
  :func:`require_open_lots_at_calendar_year_end`.
* :func:`require_financial_year_contained_in_period` — Schedule OS,
  Schedule CG and FSI need an export that contains the Indian Financial
  Year (Apr 1, YYYY → Mar 31, YYYY+1) and may extend past it (#1284).
  Partial-FY and CY-aligned exports are rejected.
* :func:`require_open_lots_at_calendar_year_end` — Schedule FA needs a
  Flex Query whose lots are all reported at Dec 31 of the calendar
  year; a mid-year snapshot would report the wrong closing value.
* :func:`require_flex_calendar_year_period` — Schedule FA needs a Flex
  Query whose ``BOF`` header record shows a period ending on Dec 31 of
  the calendar year and starting on or before Jan 1 (#1313), the same
  rule as :func:`require_calendar_year_period`.

Each raises :class:`~ibkr_fa.exceptions.ParseError` pointing at the
source cell that falls short.

:func:`first_coverage_gap` finds the first stretch of a window that a set
of statement periods leaves uncovered (#646); each caller raises its own
error for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.open_lot import OpenLot
from ibkr_fa.models.source import FlexPeriod, StatementPeriod
from ibkr_fa.models.tax_year import FinancialYear

# The settlement bound (owner decision, 2026-11-10: 10 days): a NetBenefits statement
# prints a trade on its settlement date. The checks assume this falls at most this
# many days after the trade, so a trade made by a window's end shows in reports
# covering that end plus this bound. NetBenefits capital gains and Schedule FA share it.
SETTLEMENT_BOUND: Final = timedelta(days=10)

_SECTION: Final[str] = "Statement"
_PERIOD_FIELD: Final[str] = "Period"
_FLEX_LOT_SECTION: Final[str] = "Open Positions (LOT)"


def require_calendar_year_period(period: StatementPeriod, *, calendar_year: int) -> None:
    """Hard-require ``period`` to end on Dec 31 of ``calendar_year`` and
    start on or before Jan 1.

    Schedule FA Table A3 is reported on a calendar-year basis, and the
    statement's Open Positions is its period-end snapshot, so the period
    must end exactly on ``Dec 31, <calendar_year>``. It may start before
    ``Jan 1, <calendar_year>`` (#1288): the pipeline rebuilds each holding
    from its opening trades, so a holding bought in an earlier year needs a
    statement that includes its opening trade. The pipeline then files
    only the calendar year's lots and income rows.

    Partial-year statements (starting after Jan 1), FY-aligned (Apr-Mar)
    statements and statements ending on any other day all fail.

    Args:
        period: The parsed :class:`StatementPeriod`.
        calendar_year: The CY being reported (e.g., ``2025`` for
            AY 2026-27).

    Raises:
        ParseError: If the period starts after Jan 1 or doesn't end on
            Dec 31 of ``calendar_year``. The message tells the user what
            period to export, and prints none of the statement's dates.
    """
    if period.start_date > date(calendar_year, 1, 1) or period.end_date != date(
        calendar_year, 12, 31
    ):
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column=_PERIOD_FIELD,
            message=(
                f"the Activity Statement period must end on Dec 31, {calendar_year} "
                f"and start on or before Jan 1, {calendar_year}. Schedule FA Table "
                f"A3 is calendar-year based and rebuilds each holding from its "
                f"opening trades; re-export the Activity Statement from Jan 1, "
                f"{calendar_year}, or from your account's first trade if that is "
                f"earlier, through Dec 31, {calendar_year}."
            ),
            raw_value="",
        )


def require_flex_calendar_year_period(period: FlexPeriod | None, *, calendar_year: int) -> None:
    """Hard-require a Flex Query ``BOF`` period that ends on Dec 31 of
    ``calendar_year`` and starts on or before Jan 1 (#1313).

    A Flex Query run over part of the year can leave out a sale, and
    Schedule FA would then leave out a holding held during the year. Only
    the ``BOF`` header record states the dates a Flex Query covers, so a
    file without header and trailer records refuses too.

    Args:
        period: The ``BOF`` record's period, or ``None`` when the file
            has no header and trailer records.
        calendar_year: The CY this Schedule FA is being filed for.

    Raises:
        ParseError: If ``period`` is ``None``, at line 0; or if it starts
            after Jan 1 or doesn't end on Dec 31 of ``calendar_year``, at
            the ``BOF`` record's line. Both under the ``"<file>"`` section
            with fixed text, which prints none of the file's dates.
    """
    if period is None:
        raise ParseError(
            section="<file>",
            row_index=0,
            column=None,
            message=(
                "this Flex Query has no header and trailer records, so the dates it "
                "covers can't be checked, and Schedule FA could leave out a holding "
                "sold outside them. Turn on 'Include header and trailer records' in "
                "the Flex Query's delivery settings and run it from January 1 to "
                f"December 31, {calendar_year}, or use an Activity Statement for the "
                "year instead."
            ),
            raw_value="",
        )
    if period.start_date > date(calendar_year, 1, 1) or period.end_date != date(
        calendar_year, 12, 31
    ):
        raise ParseError(
            section="<file>",
            row_index=period.line,
            column=None,
            message=(
                f"the Flex Query's header record must show a period starting on or "
                f"before Jan 1, {calendar_year} and ending on Dec 31, {calendar_year}. "
                f"Schedule FA lists every holding held during the calendar year; "
                f"re-run the Flex Query over that period."
            ),
            raw_value="",
        )


def require_financial_year_contained_in_period(
    period: StatementPeriod,
    *,
    financial_year: FinancialYear,
    report_name: str = (
        "Indian financial-year-based reports that need historical trade context (Schedule CG)"
    ),
    needs_lot_history: bool = True,
) -> None:
    """Hard-require ``period`` to FULLY CONTAIN the Indian FY window.

    The Schedule CG STCG / LTCG pipelines need a wider Activity
    Statement than the exact FY window: FIFO lot matching against
    closing trades requires the corresponding opening trades, which
    are typically acquired before the reporting FY. A user filing
    Schedule CG for FY 2025-26 may legitimately need a statement
    that starts at the account's first trade (often years before
    Apr 1, 2025): a lot sold in the FY needs its opening trade too.

    The export must contain the FY, but extending earlier and / or
    later is acceptable. CY-aligned (Jan-Dec) exports are rejected by
    the same logic — they miss the Jan-Mar quarter of the FY. Schedule
    OS accepts the same statements and files only its in-FY rows (#1284).

    Args:
        period: The parsed :class:`StatementPeriod`.
        financial_year: The Indian FY being reported.
        report_name: Human-readable label for the calling subcommand
            so the rejection message attributes correctly.
        needs_lot_history: ``True`` (the default) ends the message with
            the advice to re-export from the account's first trade, or
            from Apr 1 of the FY if that is earlier, for reports that
            FIFO-match trades. Schedule OS passes ``False``:
            it reads no trades, so the message asks only for a date range
            that contains the FY.

    Raises:
        ParseError: If ``period.start_date > financial_year.start_date``
            (the export starts inside the FY, missing earlier events) or
            ``period.end_date < financial_year.end_date`` (the export
            ends before the FY closes, missing late-FY events).
    """
    expected_start = financial_year.start_date
    expected_end = financial_year.end_date
    fy_start_year = expected_start.year
    fy_end_year_short = expected_end.year % 100
    fy_label = f"{fy_start_year}-{fy_end_year_short:02d}"
    if period.start_date > expected_start or period.end_date < expected_end:
        advice = (
            " and typically a much wider window — re-export the Activity "
            "Statement from your account's first trade, or from Apr 1, "
            f"{fy_start_year} if that is earlier (a multi-year export is "
            "normal for Schedule CG since the FIFO match needs the opening "
            "trade of every lot held during the FY, including lots sold in it)."
            if needs_lot_history
            else f"; re-export the Activity Statement with date range Apr 1, "
            f"{fy_start_year} - Mar 31, {expected_end.year}, or a wider one."
        )
        raise ParseError(
            section=_SECTION,
            row_index=0,
            column=_PERIOD_FIELD,
            message=(
                f"the Activity Statement period does not fully contain the Indian "
                f"financial year {fy_label} ({expected_start} to "
                f"{expected_end}). {report_name} requires the export "
                f"to cover at least Apr 1, {fy_start_year} through "
                f"Mar 31, {expected_end.year}{advice}"
            ),
            raw_value="",
        )


def require_open_lots_at_calendar_year_end(
    open_lots: Sequence[OpenLot], *, calendar_year: int
) -> None:
    """Hard-require every Flex Query lot to be reported at Dec 31 of ``calendar_year``.

    Schedule FA needs end-of-CY values; a mid-year snapshot would
    silently report the wrong closing value.

    Args:
        open_lots: The lots :func:`~ibkr_fa.parser.parse_open_positions`
            returned, one per LOT row in input order.
        calendar_year: The CY this Schedule FA is being filed for.

    Raises:
        ParseError: At the first lot whose ``report_date`` is not
            Dec 31 of ``calendar_year``. ``row_index`` is the lot's
            index, which is its LOT row; the message doesn't print the
            date it found, and ``raw_value`` is empty (#1047).
    """
    expected = date(calendar_year, 12, 31)
    for row_index, lot in enumerate(open_lots):
        if lot.report_date != expected:
            raise ParseError(
                section=_FLEX_LOT_SECTION,
                row_index=row_index,
                column="ReportDate",
                message=(
                    f"v1 requires the Flex Query ReportDate to equal Dec 31 of "
                    f"the target calendar year ({expected.isoformat()}); this "
                    f"lot's ReportDate is another date. Re-run your Flex Query with "
                    f"ReportDate set to {expected.isoformat()}."
                ),
                raw_value="",
            )


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """The first stretch of a window that no period covers.

    ``short_end`` is True when no later period starts after the gap: the
    periods stop before the window's end, which is ``end``. Otherwise the
    gap runs up to the day before the next period starts.
    """

    start: date
    end: date
    short_end: bool


def first_coverage_gap(
    periods: Iterable[tuple[date, date]], *, window_start: date, window_end: date
) -> CoverageGap | None:
    """The first gap the ``(start, end)`` periods leave in ``[window_start,
    window_end]``, or ``None`` once they cover it through ``window_end``.

    Periods are walked by start date and may overlap; any period starting
    after the walk has reached ``window_end`` is not looked at.
    """
    covered = window_start - timedelta(days=1)
    for start, end in sorted(periods, key=lambda period: period[0]):
        if start > covered + timedelta(days=1):
            return CoverageGap(
                covered + timedelta(days=1), start - timedelta(days=1), short_end=False
            )
        covered = max(covered, end)
        if covered >= window_end:
            return None
    return CoverageGap(covered + timedelta(days=1), window_end, short_end=True)
