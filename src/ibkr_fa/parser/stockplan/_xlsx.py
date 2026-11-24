"""Shared helpers for the Morgan Stanley StockPlan (Shareworks) .xlsx parsers
(the dividend report and the capital-gain/loss workbook).

All helpers are fail-closed ("it's taxes — never be wrong"): a corrupt file, a
missing sheet, a formula cell, a stored spreadsheet error value, a non-allowed
openpyxl warning, a non-date/non-numeric/non-finite cell, or an unconfirmed
currency raises
:class:`~ibkr_fa.exceptions.ParseError` rather than returning partial data. A refusal
names the sheet and row, plus the column when applicable, in fixed text, and carries
an empty ``raw_value``: it never prints a cell, a row, a workbook-provided sheet name,
a date or exception text
(#1043). Its exception graph holds none either: a refusal that replaces openpyxl's
error or a title date's lookup or calendar error has only a content-free stand-in
cause (#1226). The one input left in the graph is the supplied path, in the
suppressed ``OSError`` context of a refusal that names its errno.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError as XMLParseError
from zipfile import BadZipFile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site, errno_name
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.xlsx_checks import RefuseOnWarning, iter_cells_in_order

# openpyxl signals a corrupt / non-.xlsx file with these; treat as a parse
# refusal (controlled domain error) rather than letting a raw traceback escape.
# XMLParseError covers a valid zip whose worksheet XML is corrupt. An OSError
# is refused separately, naming its errno.
WORKBOOK_OPEN_ERRORS = (
    BadZipFile,
    InvalidFileException,
    XMLParseError,
    KeyError,
    ValueError,
)

USD_STATEMENT = "All currencies are in US Dollars unless specified."
# StockPlan title dates are DD-Mmm-YYYY. Parsed via an explicit month map
# (locale-independent, like the dividend parser).
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


def title_date(token: str, *, section: str, path: Path, report: str) -> _dt.date:
    """A ``DD-Mon-YYYY`` token from a matched title row as a date, or refuse.

    A month that isn't an English three-letter name, or a day the month doesn't
    have, refuses at row 0 as ``malformed <report> date in <file name>``,
    without the date. The month lookup's error, which names the month, and the
    calendar error, which on Python 3.14 names the date's parts, are replaced by
    a content-free stand-in cause (#1226).
    """
    day, month, year = token.split("-")
    try:
        return _dt.date(int(year), _MONTHS[month.capitalize()], int(day))
    except (KeyError, ValueError) as exc:
        crash = ReplacedCrashError(crash_site(exc))
    # After the handler: the KeyError names the month, and on Python 3.14 the
    # ValueError names the date's parts.
    raise ParseError(
        section=section,
        row_index=0,
        column=None,
        message=f"malformed {report} date in {path.name}",
        raw_value="",
    ) from crash


def require_run_after_coverage(
    run_on: _dt.date, end: _dt.date, *, section: str, path: Path, missed: str
) -> None:
    """Refuse a report not run strictly after its claimed coverage ends.

    One run earlier, or on the last day, can miss ``missed`` (e.g. ``"sales"``)
    that happen after it was run, so its coverage can't prove none is missing
    (#680, #682). The refusal prints neither date.
    """
    if run_on <= end:
        raise ParseError(
            section=section,
            row_index=0,
            column=None,
            message=(
                f"{path.name} was not run strictly after its reporting period ended, so it "
                f"can miss later {missed}; re-export the report after the period ends"
            ),
            raw_value="",
        )


def load_sheet_rows(
    path: Path,
    sheet: str,
    *,
    required_sheets: Collection[str],
    optional_sheets: Collection[str] = (),
) -> list[tuple[object, ...]]:
    """Load ``sheet``'s rows from ``path``, or refuse.

    Wraps openpyxl so a corrupt / non-.xlsx file, a missing sheet, or a sheet
    that stores its rows or cells out of order yields a controlled
    :class:`ParseError` instead of a raw traceback or a partial read. A refusal
    names ``path``'s file name, the expected sheets and, for an ``OSError``, its
    errno name; never a sheet name the workbook provides, nor openpyxl's or the
    ``OSError``'s text. openpyxl's error, which can name a ZIP member or quote a
    cell, is replaced by a content-free stand-in cause (#1226).

    A sheet that stores a formula in any cell refuses too, whether or not a value
    was stored with it, naming the cell's zero-based row and ``#<n>`` column but
    never the formula or its value (#1089). A sheet with no formula that stores a
    spreadsheet error value (such as ``#N/A``) in any cell refuses at the first
    such cell in stored order, naming its row and column in the same way, but
    never the file name or the error code (#1133). Both checks run only after
    the whole in-order scan has passed, so a sheet stored out of order refuses
    as unreadable instead. So the rows hold only literal stored values.

    Every warning raised from opening the workbook to the last read is recorded,
    never printed. Any warning except openpyxl's fixed
    :data:`~ibkr_fa.xlsx_checks.ALLOWED_WARNING` refuses as an unreadable sheet,
    in place of the rows or any other refusal (#1103). For example, openpyxl
    warns with a date-styled cell's number when it is out of range for a date,
    then reads the cell as the text ``#VALUE!``.
    """
    with RefuseOnWarning(_unreadable_rows(path, sheet)):
        return _read_sheet_rows(
            path, sheet, required_sheets=required_sheets, optional_sheets=optional_sheets
        )


def _unreadable_rows(path: Path, sheet: str) -> ParseError:
    """The fixed refusal for a sheet whose rows openpyxl can't read cleanly."""
    return ParseError(
        section=sheet,
        row_index=0,
        column=None,
        message=f"could not read the {sheet!r} sheet's rows from {path.name}",
        raw_value="",
    )


def _read_sheet_rows(
    path: Path,
    sheet: str,
    *,
    required_sheets: Collection[str],
    optional_sheets: Collection[str],
) -> list[tuple[object, ...]]:
    """:func:`load_sheet_rows` without its warning check."""
    crash = None
    try:
        workbook = openpyxl.load_workbook(path, data_only=False, read_only=True)
    except OSError as exc:
        raise ParseError(
            section=sheet,
            row_index=0,
            column=None,
            message=f"could not open {path.name} ({errno_name(exc)})",
            raw_value="",
        ) from None
    except WORKBOOK_OPEN_ERRORS as exc:
        crash = ReplacedCrashError(crash_site(exc))
    if crash is not None:
        # After the handler: openpyxl's error can name a ZIP member.
        raise ParseError(
            section=sheet,
            row_index=0,
            column=None,
            message=f"could not open {path.name} as an .xlsx workbook",
            raw_value="",
        ) from crash
    try:
        if sheet not in workbook.sheetnames:
            raise ParseError(
                section=sheet,
                row_index=0,
                column=None,
                message=f"{path.name} has no {sheet!r} sheet",
                raw_value="",
            )
        actual = set(workbook.sheetnames)
        missing = set(required_sheets) - actual
        if missing:
            raise ParseError(
                section=sheet,
                row_index=0,
                column=None,
                message=f"{path.name} is missing required sheet(s): {sorted(missing)}",
                raw_value="",
            )
        expected = set(required_sheets) | set(optional_sheets)
        unexpected = actual - expected
        if unexpected:
            raise ParseError(
                section=sheet,
                row_index=0,
                column=None,
                message=(
                    f"{path.name} has {len(unexpected)} unexpected sheet(s); "
                    f"expected only {sorted(expected)}"
                ),
                raw_value="",
            )
        try:
            worksheet = workbook[sheet]
            # Read every cell, not only the range the sheet's <dimension> tag declares,
            # and refuse a sheet whose stored order would hide cells from the read.
            worksheet.reset_dimensions()
            # The first cell of each data type, in stored order, once the whole scan passed.
            first: dict[str, dict[str, Any]] = {}
            for stored in iter_cells_in_order(worksheet):
                first.setdefault(stored["data_type"], stored)
            if formula := first.get("f"):
                raise ParseError(
                    section=sheet,
                    row_index=formula["row"] - 1,
                    column=f"#{formula['column']}",
                    message=(
                        f"{path.name} stores a formula in this cell; only stored values are "
                        "supported. Re-export the report."
                    ),
                    raw_value="",
                )
            if error := first.get("e"):
                raise ParseError(
                    section=sheet,
                    row_index=error["row"] - 1,
                    column=f"#{error['column']}",
                    message="This cell stores a spreadsheet error value. Re-export the report.",
                    raw_value="",
                )
            return [tuple(row) for row in worksheet.iter_rows(values_only=True)]
        except OSError as exc:
            raise ParseError(
                section=sheet,
                row_index=0,
                column=None,
                message=(
                    f"could not read the {sheet!r} sheet's rows from {path.name} "
                    f"({errno_name(exc)})"
                ),
                raw_value="",
            ) from None
        except WORKBOOK_OPEN_ERRORS as exc:
            crash = ReplacedCrashError(crash_site(exc))
        # After the handler but before the close: openpyxl's error can quote a cell.
        raise _unreadable_rows(path, sheet) from crash
    finally:
        workbook.close()


def cell(row: tuple[object, ...], i: int) -> object:
    """The ``i``-th cell of a (possibly short) row, or ``None`` if absent."""
    return row[i] if i < len(row) else None


def is_blank_row(row: tuple[object, ...]) -> bool:
    """Whether every cell in ``row`` is empty."""
    return all(value in (None, "") for value in row)


def single_text(row: tuple[object, ...]) -> str | None:
    """Column-zero text when every later cell is empty, otherwise ``None``."""
    first = cell(row, 0)
    if isinstance(first, str) and all(value in (None, "") for value in row[1:]):
        return first
    return None


def require_exact_row(
    rows: list[tuple[object, ...]],
    row_index: int,
    expected: tuple[object, ...],
    *,
    section: str,
    path: Path,
    description: str,
) -> None:
    """Require a row at its known position, allowing only blank xlsx padding."""
    row = rows[row_index] if row_index < len(rows) else ()
    actual = tuple(cell(row, i) for i in range(len(expected)))
    if actual != expected or any(value not in (None, "") for value in row[len(expected) :]):
        raise ParseError(
            section=section,
            row_index=row_index,
            column=None,
            message=f"expected exact {description} {expected!r} in {path.name}",
            raw_value="",
        )


def require_matching_text_row(
    rows: list[tuple[object, ...]],
    row_index: int,
    pattern: re.Pattern[str],
    *,
    section: str,
    path: Path,
    description: str,
) -> str:
    """Require one text-only row matching the known report shape."""
    row = rows[row_index] if row_index < len(rows) else ()
    text = single_text(row)
    if text is None or pattern.fullmatch(text) is None:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=None,
            message=f"expected {description} in {path.name}",
            raw_value="",
        )
    return text


def require_no_trailing_values(
    row: tuple[object, ...], width: int, *, section: str, row_index: int
) -> None:
    """Refuse populated cells beyond the known schema width."""
    for index, value in enumerate(row[width:], start=width):
        if value not in (None, ""):
            raise ParseError(
                section=section,
                row_index=row_index,
                column=f"#{index + 1}",
                message=f"unexpected populated column beyond the known {width}-column schema",
                raw_value="",
            )


def refuse_unknown_row(*, section: str, row_index: int) -> None:
    """Refuse a nonblank row outside a report's explicit allowlist."""
    raise ParseError(
        section=section,
        row_index=row_index,
        column=None,
        message="unknown non-data row; the StockPlan report schema may have changed",
        raw_value="",
    )


def is_data_row(row: tuple[object, ...]) -> bool:
    """True if the row is a data row (a transaction date in col 0, or any content beyond it).

    The reports' non-data rows (title, "Unable to get quote…" lines, currency
    footer) carry only a text STRING in column 0. So a row is data if it has any
    populated column >= 1, OR column 0 holds a real date/datetime (a transaction
    date). Classifying this way — rather than by a narrow subset of "marker"
    columns — means a malformed data row (a stray date, or a value past column 0
    but missing a required field) is REFUSED by the field checks downstream, not
    silently skipped (which could under-report). (never-be-wrong)
    """
    if isinstance(cell(row, 0), _dt.date):  # datetime is a subclass of date
        return True
    return any(cell(row, i) not in (None, "") for i in range(1, len(row)))


def require_usd_currency(rows: list[tuple[object, ...]], path: Path, *, section: str) -> None:
    """Refuse unless the report affirmatively confirms it is in US Dollars."""
    matches = [i for i, row in enumerate(rows) if single_text(row) == USD_STATEMENT]
    if len(matches) != 1:
        raise ParseError(
            section=section,
            row_index=matches[0] if matches else 0,
            column=None,
            message=(
                f"could not uniquely confirm {path.name} is denominated in US Dollars "
                f"(expected exactly one {USD_STATEMENT!r} statement, found {len(matches)})"
            ),
            raw_value="",
        )


def to_decimal(
    value: object, *, section: str, row_index: int, column: str, kind: NumberKind
) -> Decimal:
    """Coerce an xlsx cell (int/float/str) to a finite ``Decimal`` within ``kind``'s
    read-time digit limits (:mod:`ibkr_fa.number_checks`), or refuse.

    A float reads as ``Decimal(str(value))``, so ``1e-300`` has 300 decimal
    places; an integer cell has no digit limit of its own.
    """
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"non-numeric {column}",
            raw_value="",
        ) from None
    if not number.is_finite():
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"non-finite {column}",
            raw_value="",
        )
    breach = number_limit_breach(number, kind)
    if breach is not None:
        raise ParseError(
            section=section, row_index=row_index, column=column, message=breach, raw_value=""
        )
    return number


def to_date(value: object, *, section: str, row_index: int, column: str) -> _dt.date:
    """The cell as a ``date`` (openpyxl yields ``datetime`` for date cells), or refuse."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    raise ParseError(
        section=section,
        row_index=row_index,
        column=column,
        message=f"data row has a missing or non-date {column}",
        raw_value="",
    )
