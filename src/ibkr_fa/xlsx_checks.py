"""Checks on ``.xlsx`` worksheets opened with openpyxl's read-only mode.

Read-only openpyxl reads a sheet's XML in file order. When dimensions are cleared
or absent, it sizes each row by the row's last cell in that order, which can drop
earlier higher-column cells. It also skips a row stored after a higher-numbered
row (openpyxl 3.1.5, ``ReadOnlyWorksheet._get_row`` and ``_cells_by_row``). Excel
never writes a sheet like that, but an edited or generated file can (#961).

openpyxl also reports some problems with :func:`warnings.warn` instead of
raising, and some of those messages quote workbook text. :class:`RefuseOnWarning`
keeps every such warning off stderr and refuses on non-allowed warnings unless a
crash must propagate (#1103).

This module imports nothing from :mod:`ibkr_fa`, so every layer can use it.
"""

from __future__ import annotations

import lzma
import warnings
import zlib
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Final
from xml.etree.ElementTree import ParseError as XMLParseError
from zipfile import BadZipFile

from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.formula.translate import TranslatorError
from openpyxl.utils.cell import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet._reader import WorkSheetParser

# What openpyxl's sheet parser raises, besides ValueError, for content it can't parse.
_FORMAT_FAILURES = (
    IndexError,
    KeyError,
    TypeError,
    AttributeError,
    OverflowError,
    XMLParseError,
    BadZipFile,
    InvalidFileException,
    TokenizerError,
    TranslatorError,
    zlib.error,
    lzma.LZMAError,
)

#: The one warning a workbook read may raise (owner decision, #1103). openpyxl
#: adds its own "Normal" style when the workbook declares no named cell styles,
#: then raises this warning. The text is fixed, and no cell value changes.
ALLOWED_WARNING: Final = "Workbook contains no default style, apply openpyxl's default"


class RefuseOnWarning:
    """Record every warning raised inside the block, so none prints (#1103).

    On leaving the block, raise ``refusal`` from ``None`` if the block recorded
    any warning other than a :class:`UserWarning` whose text is exactly
    :data:`ALLOWED_WARNING`, and the block either finished or raised an error
    of ``refusal``'s type. So a read with a non-allowed warning ends in the same fixed
    refusal wherever the warning came from, even when a later check refused first. Any
    other exception propagates unchanged.
    """

    def __init__(self, refusal: Exception) -> None:
        self._refusal = refusal
        self._catcher = warnings.catch_warnings(record=True)
        self._recorded: list[warnings.WarningMessage] = []

    def __enter__(self) -> None:
        self._recorded = self._catcher.__enter__()
        warnings.simplefilter("always")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._catcher.__exit__(exc_type, exc, tb)
        if (exc is None or isinstance(exc, type(self._refusal))) and any(
            record.category is not UserWarning or str(record.message) != ALLOWED_WARNING
            for record in self._recorded
        ):
            raise self._refusal from None


def require_cells_in_order(worksheet: ReadOnlyWorksheet) -> None:
    """Raise :class:`ValueError` unless ``worksheet`` stores its rows and cells in order.

    The same check as :func:`iter_cells_in_order`, run to the end.
    """
    for _ in iter_cells_in_order(worksheet):
        pass


def iter_cells_in_order(worksheet: ReadOnlyWorksheet) -> Iterator[dict[str, Any]]:
    """Yield every cell ``worksheet`` stores, in file order, as openpyxl's parser reads it.

    Each cell is the parser's ``dict`` (``row``, ``column``, ``value``,
    ``data_type``, ``style_id``). Raise :class:`ValueError` once a row or cell
    is out of order: row numbers must strictly increase, and so must the
    columns within a row, whose cells must all belong to that row; a repeated
    reference fails too. The scan uses the read-only reader's parser and
    workbook settings, so it sees the coordinates the read sees. A format
    failure in the scan (one of :data:`_FORMAT_FAILURES`, such as the
    :class:`IndexError` from a shared-string index past the end of the table)
    is raised as :class:`ValueError` too. An :class:`OSError`, or any other
    exception, propagates unchanged.
    """
    previous_row = 0
    with worksheet._get_source() as source:
        parser = WorkSheetParser(
            source,
            worksheet._shared_strings,
            data_only=worksheet.parent.data_only,
            epoch=worksheet.parent.epoch,
            date_formats=worksheet.parent._date_formats,
            timedelta_formats=worksheet.parent._timedelta_formats,
        )
        try:
            for row, cells in parser.parse():
                if row <= previous_row:
                    raise ValueError(f"row {row} is stored out of order")  # noqa: TRY301
                previous_column = 0
                for cell in cells:
                    if cell["row"] != row or cell["column"] <= previous_column:
                        reference = f"{get_column_letter(cell['column'])}{cell['row']}"
                        raise ValueError(f"cell {reference} is stored out of order")  # noqa: TRY301
                    previous_column = cell["column"]
                    yield cell
                previous_row = row
        except (OSError, ValueError):
            raise
        except _FORMAT_FAILURES as exc:
            # The full scan can reach malformed content an earlier refusal would not read.
            raise ValueError(str(exc)) from exc
