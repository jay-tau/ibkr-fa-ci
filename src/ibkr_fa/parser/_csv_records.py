"""Strict CSV record reading shared by the broker CSV readers (#1098).

Python's default ``csv.reader`` repairs bad quoting instead of refusing it: an
unclosed quote runs to the end of the file, and text after a closing quote is
glued onto the cell. A cut-short or edited file would then parse as complete.
:func:`csv_records` reads with ``strict=True`` and turns every ``csv.Error``,
including the field-size error, into a :class:`ParseError`.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator

from ibkr_fa.exceptions import ParseError


def csv_records(
    lines: Iterable[str], *, section: str, first_line: int, reexport: str
) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(line, record)`` for every non-blank CSV record in ``lines``.

    ``line`` is the line the record starts on, the first line being
    ``first_line`` (0 or 1). A quoted cell can hold line breaks, so a record
    can span several lines and a record count would fall short. A record is
    blank when it has no cell, or only empty or whitespace cells.

    Raises:
        ParseError: A record is malformed: a quote is left open to the end of
            the file, text follows a closing quote, or a cell is longer than
            the CSV reader allows. The refusal reports under ``section`` at the
            line the record starts on, counted from ``first_line``, with column
            ``None``. Its message names that line counted from 1 and ends with
            ``reexport``. It carries no cell and none of the ``csv.Error`` text.
    """
    reader = csv.reader(lines, strict=True)
    # Taken before each record is read: after a failure, ``line_num`` can
    # point past the record's start, even at the end of the file.
    start = 0
    while True:
        try:
            record = next(reader)
        except StopIteration:
            return
        except csv.Error:
            raise ParseError(
                section=section,
                row_index=start + first_line,
                column=None,
                message=(
                    f"the CSV record starting on line {start + 1} is malformed: a quote is "
                    "left open to the end of the file, text follows a closing quote, or a "
                    "cell is longer than the CSV reader allows. The file may have been "
                    f"edited or cut short; {reexport}"
                ),
                raw_value="",
            ) from None
        if any(cell.strip() for cell in record):
            yield start + first_line, record
        start = reader.line_num


# A UTF-8 byte-order mark read as Latin-1 and saved again as UTF-8, which
# reads back as the text ``ï»¿`` (#1195).
_GARBLED_BOM = "\u00ef\u00bb\u00bf"


def refuse_garbled_bom(line: int, record: list[str], *, first_line: int) -> None:
    """Refuse an IBKR CSV whose first line starts with a garbled byte-order mark (#1195).

    ``line`` and ``record`` are one of :func:`csv_records`' records, read with
    ``first_line``. Only a record starting on the file's first line is checked,
    and only the start of its first cell. The mark isn't stripped: a file
    re-encoded once may be damaged elsewhere too.

    Raises:
        ParseError: Under the ``"<file>"`` section at ``first_line``, with column
            ``None``. It carries no cell.
    """
    if line == first_line and record[0].startswith(_GARBLED_BOM):
        raise ParseError(
            section="<file>",
            row_index=first_line,
            column=None,
            message=(
                "the file starts with a garbled byte-order mark, so it was re-encoded "
                "after download and may be damaged elsewhere. Download the CSV from "
                "IBKR again and use it unedited."
            ),
            raw_value="",
        )
