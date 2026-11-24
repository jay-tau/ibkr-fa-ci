"""Shared writer-contract helpers for the XLSX output family.

Sister to :mod:`ibkr_fa.output._writer_contract` (the CSV-side
contract module). All XLSX writers — FA-A3 (issue #42), Schedule
OS, Schedule CG / CG-LTCG, Schedule FSI — route their per-row safety checks
through this module so:

* The OWASP-extended formula-trigger set
  (:data:`ibkr_fa.output._writer_contract._FORMULA_TRIGGERS`) is
  the SAME object the CSV writers consume. If the canonical set
  is ever extended (e.g., a new Unicode homoglyph), every XLSX
  writer picks it up automatically — no risk of one writer
  silently diverging and accepting payloads a sister writer
  rejects.
* The NUL-byte check preserves the **original worksheet column
  index** in :class:`WriterContractError.cell_index`. XLSX rows
  are heterogeneous (Decimals, datetimes, ``None`` mixed with
  strings). A naive compact-then-check pattern that filters to
  string-only cells would report a position relative to the
  filtered subsequence, sending users to the wrong column despite
  the contract promising a deterministic location (PR #202 T5
  P3 + T4 Low — locked in by regression test
  ``test_nul_byte_cell_index_preserved_across_typed_cells``).
* The control-character check runs on the row about to be appended,
  after every other check. It refuses a string cell holding a
  character openpyxl can't store (U+0000 to U+0008, U+000B, U+000C,
  U+000E to U+001F; its ``ILLEGAL_CHARACTERS_RE``) and names only the
  row and cell, because openpyxl's own error holds the whole cell
  text (#1357). Tab, line feed, carriage return and DEL pass this check.
* Numeric Decimals are coerced to ``float`` exactly once at write
  time. openpyxl writes :class:`Decimal` cells as strings, which
  loses numeric typing in the resulting file; coercion to
  ``float`` keeps the cell numeric and formulas-capable. The
  precision loss is acceptable because XLSX is a review /
  copy-source artifact, not the source of truth (the audit
  :class:`Decimal` remains in the IBKR statement). A value the
  workbook cannot store at all (it would read back as infinite,
  empty or zero) refuses instead (#832).
* FA portal amount cells hold the model's filed whole-rupee values
  (:func:`_whole_rupee_cell`), the same values the CSV writers
  render, so a user comparing the two artifacts cell-by-cell sees
  identical amounts. No FA writer rounds.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.compat.strings import safe_string

from ibkr_fa.models.money import require_whole_rupees
from ibkr_fa.output._writer_contract import (
    # Unused here since #1622; tests/test_output_xlsx.py checks it is the canonical set.
    _FORMULA_TRIGGERS,  # noqa: F401
    _NUL,
    _XLSX_CELL_TEXT_LIMIT,
    WriterContractError,
    _formula_trigger,
)


def _assert_no_nul_in_xlsx_row(cells: Sequence[object], *, row_index: int) -> None:
    """XLSX-aware NUL check that preserves the original column index.

    The CSV-side :func:`_assert_no_nul_in_row` only accepts a
    homogeneous ``Sequence[str]``. XLSX rows are heterogeneous
    (Decimals, datetimes, ``None`` mixed with strings) and we can
    only carry NUL bytes in *string* cells. Naively filtering to a
    string-only sequence and reusing the CSV helper would compact
    the row, so ``WriterContractError.cell_index`` would point to
    the string-only position rather than the actual worksheet
    column, sending users to the wrong cell. This wrapper iterates
    the original sequence with :func:`enumerate` so the reported
    ``cell_index`` always matches the worksheet column the user
    can locate.
    """
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, str):
            continue
        if _NUL in cell:
            raise WriterContractError(
                violation="nul_byte",
                row_index=row_index,
                cell_index=cell_index,
                trigger="",
            )


def _assert_no_xlsx_formula_injection(cells: Sequence[object], *, row_index: int) -> None:
    """Reject any string cell whose first char is a formula trigger.

    XLSX equivalent of
    :func:`ibkr_fa.output._writer_contract._assert_no_csv_formula_injection`.
    Each cell is decided by the same
    :func:`ibkr_fa.output._writer_contract._formula_trigger` the CSV check
    uses, so XLSX writers cannot silently diverge from the CSV writers if the
    trigger set is ever extended (e.g., a new Unicode homoglyph). XLSX is
    *more* vulnerable than CSV here — Excel auto-executes formula-prefixed
    cells with NO opt-in — but the same defense set still applies.

    Only string cells are checked — Excel interprets numeric /
    datetime cell values as values, not formulas, so they are safe
    by construction.

    Args:
        cells: The cells about to be written to the worksheet.
            String cells are checked twice (raw + post-lstrip),
            as the CSV check does.
        row_index: Zero-based row index for the error message.

    Raises:
        WriterContractError: With ``violation="formula_trigger"`` on
            the first offending string cell.
    """
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, str) or cell == "":
            continue
        trigger = _formula_trigger(cell)
        if trigger is not None:
            raise WriterContractError(
                violation="formula_trigger",
                row_index=row_index,
                cell_index=cell_index,
                trigger=trigger,
            )


def _assert_xlsx_text_fits(cells: Sequence[object], *, row_index: int) -> None:
    """Reject any string cell longer than a workbook cell can hold (#1323).

    openpyxl silently cuts strings over 32,767 Python code points. Excel's
    :data:`~ibkr_fa.output._writer_contract._XLSX_CELL_TEXT_LIMIT` counts UTF-16
    code units, so a character outside the Basic Multilingual Plane (an emoji,
    say) counts as two. No value is cut, wrapped or moved.

    Raises:
        WriterContractError: With ``violation="text_too_long"`` on the first
            such cell.
    """
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, str):
            continue
        if len(cell.encode("utf-16-le", "surrogatepass")) // 2 > _XLSX_CELL_TEXT_LIMIT:
            raise WriterContractError(
                violation="text_too_long",
                row_index=row_index,
                cell_index=cell_index,
                trigger="",
            )


def _assert_no_illegal_xlsx_character(cells: Sequence[object], *, row_index: int) -> None:
    """Reject any string cell holding a character a workbook cell can't store (#1357).

    The set is openpyxl's own :data:`~openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE`.
    The whole cell is checked, including characters past the first 32,767
    characters openpyxl keeps.

    Raises:
        WriterContractError: With ``violation="illegal_character"`` on the
            first such cell.
    """
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, str):
            continue
        if ILLEGAL_CHARACTERS_RE.search(cell) is not None:
            raise WriterContractError(
                violation="illegal_character",
                row_index=row_index,
                cell_index=cell_index,
                trigger="",
            )


def _decimal_to_float(cells: Sequence[object], *, row_index: int) -> list[object]:
    """Convert :class:`Decimal` cells to ``float`` for openpyxl.

    openpyxl writes :class:`Decimal` values as strings (loses
    numeric typing in the resulting cell). Converting to ``float``
    at write time keeps the cell numeric and formulas-capable; the
    precision loss is acceptable for audit columns (the source
    :class:`Decimal` remains in the IBKR statement; the XLSX is a
    review artifact, not the source of truth).

    Losing the value is not (#832). openpyxl stores a ``float`` as the text
    :func:`openpyxl.compat.strings.safe_string` gives it, and an infinite or
    NaN one as an empty cell. So a ``Decimal`` that is not finite, or whose
    ``float`` or stored text reads back as infinite (``1E400``,
    ``1.7976931348623157E308``) or as zero while it is not zero
    (``1E-400``), is refused. Ordinary float rounding is allowed.

    Raises:
        WriterContractError: With ``violation="unrepresentable_number"``
            and the cell's ``row_index`` and ``cell_index``, on the first
            such cell.
    """
    converted: list[object] = []
    for cell_index, cell in enumerate(cells):
        if not isinstance(cell, Decimal):
            converted.append(cell)
            continue
        number = float(cell) if cell.is_finite() else math.nan
        stored = float(safe_string(number)) if math.isfinite(number) else number
        if not math.isfinite(stored) or (stored == 0 and not cell.is_zero()):
            raise WriterContractError(
                violation="unrepresentable_number",
                row_index=row_index,
                cell_index=cell_index,
                trigger="",
            )
        converted.append(number)
    return converted


def _whole_rupee_cell(value: Decimal | None, *, field: str) -> int | None:
    """A filed whole-rupee amount as an ``int`` cell, so Excel treats it as a number.

    Mirrors :func:`ibkr_fa.output.portal_labels.format_amount_for_portal`:
    it never rounds, and refuses (naming ``field``) a value that isn't a
    whole-rupee amount. :data:`None` stays :data:`None`.
    """
    if value is None:
        return None
    require_whole_rupees(value, what=field)
    return int(value)


__all__ = [
    "_assert_no_illegal_xlsx_character",
    "_assert_no_nul_in_xlsx_row",
    "_assert_no_xlsx_formula_injection",
    "_assert_xlsx_text_fits",
    "_decimal_to_float",
    "_whole_rupee_cell",
]
