"""Strict CSV reading shared by the vendored reference loaders (#945).

``str.splitlines`` joins the two parts of a quoted cell that spans a line
break, and the default ``csv`` dialect reads ``"2"5`` as ``25``. Either
can turn a corrupted cell into a different, valid value, so the loaders
read through :func:`read_rows` and check each row with
:func:`require_plain_cells` before checking its cells' grammar.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from ibkr_fa.invariants import invariant_error, require

if TYPE_CHECKING:
    from collections.abc import Sequence


def read_rows(text: str, *, csv_name: str, header: tuple[str, ...]) -> list[list[str]]:
    """Parse ``text`` strictly, check its header and return the rows after it.

    An empty line is returned as an empty row; the caller decides what it
    means.

    Raises:
        InvariantError: On malformed CSV, naming the line (e.g. text after
            a closing quote or a quote left open at the end of the file),
            or on a first row other than ``header``.
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise invariant_error(
            what=(
                f"{csv_name}: malformed CSV (text after a closing quote, a quote left open "
                "or a cell over the csv field size limit)"
            ),
            line=reader.line_num,
        ) from exc
    got = tuple(rows[0]) if rows else ()
    require(
        got == header,
        what=(
            f"unexpected header in {csv_name}; expected {header!r}. "
            "Update both the CSV and this loader together."
        ),
        header=got,
    )
    return rows[1:]


def require_plain_cells(row: Sequence[str], header: tuple[str, ...], *, csv_name: str) -> None:
    """Require one printable cell per ``header`` column, none padded with whitespace.

    So a line break, tab, other control or format character, or line
    separator refuses, and a cell that spans lines is never joined. A
    cell refusal names its column. The error keeps only the context keys:
    the row of a width refusal and the key cell (the first) of a cell
    refusal, never their values.

    Raises:
        InvariantError: On a row of the wrong width or a cell that isn't plain.
    """
    require(
        len(row) == len(header),
        what=f"{csv_name}: every row must have exactly {len(header)} columns matching the header",
        row=row,
    )
    for column, cell in zip(header, row, strict=True):
        require(
            cell.isprintable() and cell == cell.strip(),
            what=f"{csv_name}: {column} must be printable with no leading or trailing whitespace",
            **{header[0]: repr(row[0])},
        )
