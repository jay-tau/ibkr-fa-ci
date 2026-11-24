"""Check the Activity Statement's Realized & Unrealized Performance Summary (#913).

Filed gains are computed from the Trades section, so this summary's
profit and loss figures are never read. Its ``Cost Adj.`` column is the
exception: a cost adjustment means IBKR changed a holding's cost basis,
which the lots built from Trades may not reflect. So every row's
``Cost Adj.`` must be blank or zero. The splitter refuses a
``SubTotal``/``Total``-type row here, so every row reaches this check.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser.activity_statement.multi_section import KnownActivitySection, Section

_SECTION: Final[str] = KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY.value
_COST_ADJ_COL: Final[str] = "Cost Adj."


def check_cost_adjustments(blocks: Sequence[Section]) -> None:
    """Refuse a summary row whose ``Cost Adj.`` isn't blank or zero.

    Args:
        blocks: The section's blocks from
            :func:`~ibkr_fa.parser.activity_statement.multi_section.split_activity_statement_sections`
            (empty when the section is absent).

    Raises:
        ParseError: Naming the file line of a row that has no
            ``Cost Adj.`` column or whose ``Cost Adj.`` is anything but
            blank or a zero decimal. The error carries no cell value, in
            its message or ``raw_value``: a shifted cell can hold the
            holder's name or account number.
    """
    for block in blocks:
        for row, line in zip(block.rows, block.row_lines, strict=True):
            if _COST_ADJ_COL not in row:
                raise ParseError(
                    section=_SECTION,
                    row_index=line,
                    column=_COST_ADJ_COL,
                    message=(
                        f"line {line} has no {_COST_ADJ_COL!r} column, so it "
                        f"can't be checked for a cost adjustment. IBKR may have "
                        f"changed the section's schema; re-export the Activity "
                        f"Statement."
                    ),
                    raw_value="",
                )
            raw = row[_COST_ADJ_COL]
            if raw == "":
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation:
                value = None
            if value is None or not value.is_zero():
                raise ParseError(
                    section=_SECTION,
                    row_index=line,
                    column=_COST_ADJ_COL,
                    message=(
                        f"line {line} has a {_COST_ADJ_COL!r} cell that isn't blank or "
                        f"a zero decimal. A non-zero adjustment changes a holding's "
                        f"cost basis, which the lots built from the Trades section "
                        f"may not reflect, so the run refuses rather than file an unverified "
                        f"cost."
                    ),
                    raw_value="",
                )
            # A zero such as ``0E-30`` passes the check above: its digits still
            # count (#1276).
            breach = number_limit_breach(value, NumberKind.MONEY)
            if breach is not None:
                raise ParseError(
                    section=_SECTION,
                    row_index=line,
                    column=_COST_ADJ_COL,
                    message=breach,
                    raw_value="",
                )
