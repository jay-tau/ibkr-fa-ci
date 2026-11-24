"""One-cell refusals shared by the IBKR parsers (#913, #1093).

No refusal here carries a cell value, in its message or ``raw_value``: a
column-shifted row can put the holder's name or account number in any
cell. The section, column and row locate the cell.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach

# Every Activity Statement and Flex row a parser reads as a lot or an
# instrument must be a stock (#913, #1093).
STOCKS_ONLY: Final[str] = "v1 reads only stock rows, and would read any other instrument as a stock"


def require_cell(
    row: dict[str, str],
    column: str,
    expected: str,
    *,
    section: str,
    row_index: int,
    why: str,
    expected_name: str = "",
) -> None:
    """Refuse a row whose ``column`` is missing or isn't exactly ``expected`` (#913).

    The refusal never prints the cell. It prints ``expected`` only when no
    ``expected_name`` is given; pass one, such as ``"the row's Symbol"``,
    when ``expected`` is itself a cell value.
    """
    if column not in row:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"required column {column!r} missing from the row; {why}.",
            raw_value="",
        )
    if row[column] != expected:
        rule = f"must match {expected_name}" if expected_name else f"must be {expected!r}"
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"{rule}: {why}.",
            raw_value="",
        )


def require_columns(
    row: dict[str, str],
    columns: tuple[str, ...],
    *,
    section: str,
    row_index: int,
    message: Callable[[list[str]], str],
) -> None:
    """Refuse a row missing any of ``columns``.

    Without this, a missing required column can raise a raw ``KeyError``
    when a reader subscripts it, past the CLI's documented-error catch.
    The refusal names the first missing column in
    ``columns``' order; ``message`` builds its text from every missing
    column, in that order.
    """
    missing = [c for c in columns if c not in row]
    if missing:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=missing[0],
            message=message(missing),
            raw_value="",
        )


def parse_decimal_cell(
    raw: str, *, section: str, row_index: int, column: str, kind: NumberKind
) -> Decimal:
    """Parse a decimal cell, refusing a non-number, a non-finite value (PR #136)
    and a value past ``kind``'s read-time digit limits (#1276).

    It runs in the caller's decimal context.
    """
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be a decimal number",
            raw_value="",
        ) from exc
    if not value.is_finite():
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be a finite decimal",
            raw_value="",
        )
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise ParseError(
            section=section, row_index=row_index, column=column, message=breach, raw_value=""
        )
    return value
