"""The shared portal-entry CSV renderer (#1201).

A catalog binding's :class:`PortalCsv` pairs a schedule's row function with the
artifact's :class:`CsvDialect`. Building the run's writer passes the row
function the run's context, so a context refusal comes before any file exists.
Writing calls the row function with the value before it stages the file, so a
refusal the function makes when called comes first too. It then pulls, checks
and writes the rows one at a time, in order, and publishes the file after the
last one.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import RenderContext

type Rows = Callable[[Any], Iterable[Sequence[str]]]


@dataclass(frozen=True)
class CsvDialect:
    """A file's encoding (``utf-8-sig`` writes a BOM), line ending, and how many
    leading cells of a row the formula check covers (``None``: all). The NUL
    check, which runs first, covers every cell."""

    encoding: str
    line_ending: str
    formula_columns: int | None


# The FA uploader needs signed amounts as plain numbers, such as -6, so only the
# first six cells, the text ones, are formula-checked.
FA_UPLOAD: Final = CsvDialect(encoding="utf-8", line_ending="\r\n", formula_columns=6)
# A manual-entry worksheet, read in a spreadsheet: every cell is formula-checked.
FILING_WORKSHEET: Final = CsvDialect(encoding="utf-8-sig", line_ending="\n", formula_columns=None)
# Form 67's upload: LF line endings, and every cell is formula-checked.
FORM67_UPLOAD: Final = CsvDialect(encoding="utf-8", line_ending="\n", formula_columns=None)


@dataclass(frozen=True)
class PortalCsvWriter:
    """A run's writer, built by :meth:`PortalCsv.from_context`."""

    rows: Rows
    dialect: CsvDialect

    def write(self, value: object, path: Path) -> None:
        rows = self.rows(value)
        with atomic_text_output(path, encoding=self.dialect.encoding, newline="") as fh:
            write_checked_rows(
                csv.writer(fh, lineterminator=self.dialect.line_ending),
                rows,
                start_index=0,
                formula_columns=self.dialect.formula_columns,
            )


@dataclass(frozen=True)
class PortalCsv:
    """A catalog target; ``rows`` takes a run's context and returns its rows."""

    rows: Callable[[RenderContext], Rows]
    dialect: CsvDialect

    def from_context(self, ctx: RenderContext) -> PortalCsvWriter:
        return PortalCsvWriter(self.rows(ctx), self.dialect)
