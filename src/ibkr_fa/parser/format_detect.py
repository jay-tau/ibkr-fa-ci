"""Detect which IBKR CSV format an input file is in.

The CLI accepts two distinct IBKR CSV serialisations:

* **Flex Query** — column-tabular, sections separated by their own
  header rows. Handled by :mod:`ibkr_fa.parser`.
* **Activity Statement** — section-tagged, every row begins with the
  section name and a ``Header``/``Data`` discriminator. Handled by
  :mod:`ibkr_fa.parser.activity_statement`.

Auto-detection is preferable to a user-supplied ``--format`` flag for
two reasons: (1) most users won't know which export they have, and
(2) misidentification fails loud at parse time anyway (column tuples
don't match), so the flag would only push the same error one step
later in the pipeline.

The sniff is **strict**: we only read the first non-blank line and
match it against unambiguous sentinels. Any ambiguity → raise
:class:`~ibkr_fa.exceptions.ParseError` with both expected sentinels
named.

Activity Statement sentinel
    First non-blank line tokenised as CSV begins with the literal
    cells ``("Statement", "Header")``. IBKR always emits the
    ``Statement`` section first in an Activity Statement; this is the
    most stable identification cue.

Flex Query sentinel
    First non-blank line's **first cell** is ``"BOF"``, the begin-of-file
    record IBKR writes first when the query's "Include header and trailer
    records" setting is on (#1313), or is in a known-good registry
    of Flex section first columns (``"CurrencyPrimary"``,
    ``"ClientAccountID"``), with which a Flex export without those
    records begins because it starts with a section header row. Schedule
    FA refuses a Flex Query without the records after parsing it
    (:func:`ibkr_fa.compute.source_coverage.require_flex_calendar_year_period`),
    so its refusal can say what to export.

Anything else → reject with both sentinels in the error message so
the user knows what their export should look like.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser._csv_records import csv_records, refuse_garbled_bom

# First-column values that unambiguously identify a Flex Query CSV:
# its begin-of-file record, or its leading section's first column.
# Kept in this module (rather than imported from
# :mod:`ibkr_fa.parser.multi_section`) because adding a new Flex
# section that doesn't start with one of these would not break
# detection — every Flex export so far in IBKR's published reference
# starts with at least one of these in its first section.
_FLEX_FIRST_COLUMN_SENTINELS: Final[frozenset[str]] = frozenset(
    {
        "BOF",
        "CurrencyPrimary",
        "ClientAccountID",
    }
)

_MIN_ACTIVITY_STATEMENT_FIRST_ROW_LEN: Final[int] = 2

# Activity Statement always starts with the ``Statement`` section's
# Header row. Two-cell match is unambiguous: no Flex section first
# column is ``"Statement"``.
_ACTIVITY_STATEMENT_SENTINEL_FIRST_TWO: Final[tuple[str, str]] = ("Statement", "Header")


class IbkrCsvFormat(StrEnum):
    """The two IBKR CSV serialisations this codebase parses.

    Values are stable strings used in :class:`ParseError` messages,
    log lines, and the :class:`~ibkr_fa.pipeline_fa_models.RunReport` echoed
    back to the CLI; they are not user-facing flags (auto-detection
    is mandatory).
    """

    FLEX_QUERY = "flex-query"
    ACTIVITY_STATEMENT = "activity-statement"


def detect_ibkr_csv_format(path: Path) -> IbkrCsvFormat:
    """Sniff ``path``'s first non-blank line to identify the format.

    Reads only enough of the file to tokenise the first non-blank
    row; never materialises more.

    Args:
        path: Path to the IBKR CSV file.

    Returns:
        The detected :class:`IbkrCsvFormat`.

    Raises:
        FileNotFoundError: If ``path`` doesn't exist.
        ParseError: If the file is empty; if the first non-blank record
            is malformed: a quote is left open to the end of the file,
            text follows a closing quote, or a cell is longer than the CSV
            reader allows (#1098); if that record starts on the file's first
            line with a garbled byte-order mark (#1195); or if it matches
            neither the Flex Query nor the Activity Statement sentinel. The sentinel
            error's message names both sentinels so the user can compare
            against their export. The malformed-record and sentinel
            refusals report, as ``row_index``, the zero-based line the
            record starts on. None prints a cell: the first cell can hold
            the holder's name or account number.
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        first = next(
            csv_records(f, section="<file>", first_line=0, reexport="re-export it from IBKR."),
            None,
        )

    if first is None:
        raise ParseError(
            section="<file>",
            row_index=0,
            column=None,
            message=(
                f"input CSV {path} is empty or contains only blank "
                f"lines; expected an IBKR Flex Query or Activity "
                f"Statement export."
            ),
            raw_value="",
        )

    first_row_index, first_row = first
    refuse_garbled_bom(first_row_index, first_row, first_line=0)
    if (
        len(first_row) >= _MIN_ACTIVITY_STATEMENT_FIRST_ROW_LEN
        and (first_row[0], first_row[1]) == _ACTIVITY_STATEMENT_SENTINEL_FIRST_TWO
    ):
        return IbkrCsvFormat.ACTIVITY_STATEMENT

    if first_row[0] in _FLEX_FIRST_COLUMN_SENTINELS:
        return IbkrCsvFormat.FLEX_QUERY

    raise ParseError(
        section="<file>",
        row_index=first_row_index,
        column=None,
        message=(
            f"input CSV {path}'s first non-blank record doesn't match a known "
            f"IBKR export format. Expected one of:\n"
            f"  * Activity Statement: first two cells "
            f"('Statement', 'Header'); or\n"
            f"  * Flex Query: first cell in "
            f"{sorted(_FLEX_FIRST_COLUMN_SENTINELS)}."
        ),
        raw_value="",
    )
