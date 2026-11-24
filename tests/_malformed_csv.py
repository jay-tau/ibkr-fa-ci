"""Shared inputs and checks for the strict CSV readers' malformed-record refusal (#1098).

Each bad record holds the privacy markers checked for it; no refusal prints a
marker or any ``csv.Error`` text.
"""

from __future__ import annotations

import csv

from ibkr_fa.exceptions import ParseError
from tests._crash_reports import assert_no_markers

HOLDER = "Synthetic Holder Two"
MARKER = "Synthetic Marker Q"
# A cell whose quote is left open to the end of the file.
UNCLOSED = f'"{HOLDER} {MARKER}'
# A quoted cell with text after its closing quote.
AFTER_QUOTE = f'"{HOLDER}"{MARKER}'
# What ``csv.Error`` says for each malformed record.
_CSV_ERROR_TEXTS = ("unexpected end of data", "expected after", "field larger", "field limit")


def oversized_cell() -> str:
    """An unquoted cell one character longer than the CSV reader allows."""
    return f"{HOLDER} {MARKER} ".ljust(csv.field_size_limit() + 1, "x")


def malformed_message(line: int, reexport: str) -> str:
    """The fixed refusal for a malformed record starting on 1-based ``line``."""
    return (
        f"the CSV record starting on line {line} is malformed: a quote is left open to the "
        "end of the file, text follows a closing quote, or a cell is longer than the CSV "
        f"reader allows. The file may have been edited or cut short; {reexport}"
    )


def assert_malformed(
    error: ParseError,
    *,
    section: str,
    row_index: int,
    line: int,
    reexport: str,
    markers: tuple[str, ...] = (HOLDER, MARKER),
) -> None:
    """``error`` is the fixed malformed-record refusal with no explicit cause
    and suppressed context; it prints none of ``markers`` or the ``csv.Error`` text."""
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        section,
        row_index,
        None,
        malformed_message(line, reexport),
        "",
    )
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert_no_markers(str(error), *markers, *_CSV_ERROR_TEXTS)
