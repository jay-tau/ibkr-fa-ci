"""Check the printed totals of one Activity Statement Trades block (#1090).

IBKR prints a ``SubTotal`` row after each symbol's trades and one
``Total`` row at the end of the Trades block. While it reads a Trades
block, the splitter holds these rows with their file lines and passes
them to :func:`check_trades_totals` when the block ends; they never
reach the Trades parser. Within one header-led block:

* ``Data`` rows come in runs: the consecutive rows of one symbol. Each
  run ends with exactly one ``SubTotal`` row. Its ``Asset Category``,
  ``Currency`` and ``Symbol`` equal those of every row in its run, and
  its ``Quantity``, ``Proceeds``, ``Comm/Fee``, ``Basis``,
  ``Realized P/L`` and ``MTM P/L`` each hold the sum of that column over
  the run. Its other cells are empty.
* After the last run comes exactly one ``Total`` row, which ends the
  block. Its ``Asset Category`` and ``Currency`` equal those of every
  ``SubTotal`` row, and its ``Proceeds``, ``Comm/Fee``, ``Basis``,
  ``Realized P/L`` and ``MTM P/L`` each hold the sum of that column over
  the block's ``SubTotal`` rows. Its other cells are empty, except that
  its ``Code`` cell may hold one space.
* A block with no rows needs no totals.

Sums are exact (:func:`~ibkr_fa.parser.activity_statement._totals.add_exactly`).
Only a ``Basis`` total may differ from its sum, by at most
:data:`BASIS_TOLERANCE` (owner decision, #1090).

Anything else raises :class:`~ibkr_fa.exceptions.ParseError` with the
file line as ``row_index`` and the column at fault. The check can catch
lost or changed trade rows even when the ending holdings still
reconcile, as they do after a whole round trip is removed. It cannot
detect edits that leave the totals valid. As in
:mod:`~ibkr_fa.parser.activity_statement._totals`, no refusal prints a
cell, a quantity or an amount: a shifted row can put the holder's name
or account number in any cell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser.activity_statement._totals import _RE_EXPORT, add_exactly

if TYPE_CHECKING:
    from ibkr_fa.parser.activity_statement.multi_section import Section

_SECTION: Final[str] = "Trades"
SUBTOTAL: Final[str] = "SubTotal"
TOTAL: Final[str] = "Total"
BASIS_TOLERANCE: Final[Decimal] = Decimal("0.000001")
"""How far a ``Basis`` total may differ from its exact sum (owner decision, #1090)."""

COLUMNS: Final[tuple[str, ...]] = (
    "DataDiscriminator",
    "Asset Category",
    "Currency",
    "Symbol",
    "Date/Time",
    "Quantity",
    "T. Price",
    "C. Price",
    "Proceeds",
    "Comm/Fee",
    "Basis",
    "Realized P/L",
    "MTM P/L",
    "Code",
)
"""Every column a Trades block may have, in IBKR's order; each is checked."""


@dataclass(frozen=True, slots=True)
class AggregationRow:
    """A Trades ``SubTotal`` or ``Total`` row, held by the splitter for the check.

    Attributes:
        line: The 1-based file line the row starts on.
        kind: ``"SubTotal"`` or ``"Total"``.
        cells: The row's cells, keyed by its block's columns.
    """

    line: int
    kind: str
    cells: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Shape:
    """The cells one kind of aggregation row must hold.

    Attributes:
        label: How refusals name the row.
        keys: Columns that equal those of every row it covers.
        sums: Columns that hold the sum over the rows it covers.
        blank: Every other column, with the values it may hold.
    """

    label: str
    keys: tuple[str, ...]
    sums: tuple[str, ...]
    blank: Mapping[str, frozenset[str]]


_EMPTY: Final[frozenset[str]] = frozenset({""})
_SUBTOTAL_SHAPE: Final[_Shape] = _Shape(
    label="'SubTotal' row",
    keys=("Asset Category", "Currency", "Symbol"),
    sums=("Quantity", "Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L"),
    blank=dict.fromkeys(("DataDiscriminator", "Date/Time", "T. Price", "C. Price", "Code"), _EMPTY),
)
_TOTAL_SHAPE: Final[_Shape] = _Shape(
    label="'Total' row",
    keys=("Asset Category", "Currency"),
    sums=("Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L"),
    blank={
        **dict.fromkeys(
            ("DataDiscriminator", "Symbol", "Date/Time", "Quantity", "T. Price", "C. Price"),
            _EMPTY,
        ),
        # IBKR may print one space here (owner decision, #1090).
        "Code": frozenset({"", " "}),
    },
)


def check_trades_totals(block: Section, aggregates: Sequence[AggregationRow]) -> None:
    """Check a Trades block's ``SubTotal`` and ``Total`` rows against its ``Data`` rows.

    Args:
        block: The Trades block, whose ``rows`` and ``row_lines`` hold
            only its ``Data`` rows.
        aggregates: The block's ``SubTotal`` and ``Total`` rows, in file
            order.

    Raises:
        ParseError: If a row breaks the rules in the module docstring.
    """
    check = _TradesTotals(block.header_line)
    # ``None`` marks a ``Data`` row.
    rows: list[tuple[int, AggregationRow | None, Mapping[str, str]]] = [
        (line, None, row) for line, row in zip(block.row_lines, block.rows, strict=True)
    ]
    rows += [(aggregate.line, aggregate, aggregate.cells) for aggregate in aggregates]
    for line, aggregate, cells in sorted(rows, key=lambda entry: entry[0]):
        check.before_row(line)
        if aggregate is None:
            check.data(line, cells)
        elif aggregate.kind == SUBTOTAL:
            check.subtotal(aggregate)
        else:
            check.total(aggregate)
    check.finish()


class _TradesTotals:
    """Walk one Trades block's rows in file order, checking each total."""

    def __init__(self, header_line: int) -> None:
        self._header_line = header_line
        # The current run's file lines and rows, and its column sums.
        self._run: list[tuple[int, Mapping[str, str]]] = []
        self._run_sums: dict[str, Decimal] = {}
        # Each checked ``SubTotal`` row's file line and cells, and the sums
        # of their amounts, which the ``Total`` row must hold.
        self._subtotals: list[tuple[int, Mapping[str, str]]] = []
        self._subtotal_sums: dict[str, Decimal] = {}
        self._total_line: int | None = None

    def before_row(self, line: int) -> None:
        """Refuse any row after the block's ``Total`` row."""
        if self._total_line is not None:
            raise _error(
                line,
                None,
                f"line {line} comes after the Trades block's 'Total' row on line "
                f"{self._total_line}, which must be the block's last row. {_RE_EXPORT}",
            )

    def data(self, line: int, row: Mapping[str, str]) -> None:
        """Add the ``Data`` row on ``line`` to the current run."""
        if self._run and self._run[-1][1].get("Symbol") != row.get("Symbol"):
            raise self._run_without_subtotal()
        for column in _SUBTOTAL_SHAPE.sums:
            # A column the header lacks is named by the run's ``SubTotal`` row.
            if column in row:
                amount = _finite(row[column])
                if amount is None:
                    raise _error(
                        line,
                        column,
                        f"the data row on line {line} needs a finite decimal in its "
                        f"{column!r} column.",
                    )
                _require_within_limits(amount, line, column)
                self._run_sums[column] = add_exactly(
                    self._run_sums.get(column, Decimal(0)),
                    amount,
                    section=_SECTION,
                    line=line,
                    column=column,
                )
        self._run.append((line, row))

    def subtotal(self, row: AggregationRow) -> None:
        """Check a ``SubTotal`` row against the run above it, then close the run."""
        amounts = _shape(row, _SUBTOTAL_SHAPE)
        if not self._run:
            raise _error(
                row.line,
                None,
                f"the 'SubTotal' row on line {row.line} has no rows above it since "
                f"the previous 'SubTotal' row or the block's header. {_RE_EXPORT}",
            )
        for column in _SUBTOTAL_SHAPE.keys:
            if any(run_row[column] != row.cells[column] for _, run_row in self._run):
                raise _error(
                    row.line,
                    column,
                    f"the 'SubTotal' row on line {row.line} names a different symbol, "
                    f"asset category or currency from the rows above it. {_RE_EXPORT}",
                )
        rows = _span(self._run[0][0], self._run[-1][0])
        for column in _SUBTOTAL_SHAPE.sums:
            if not _matches(amounts[column], self._run_sums[column], column, row.line):
                raise _error(
                    row.line,
                    column,
                    f"the 'SubTotal' row on line {row.line} doesn't equal the sum of the "
                    f"rows on {rows} in its {column!r} column. A row may be missing or "
                    f"changed. {_RE_EXPORT}",
                )
        for column in _TOTAL_SHAPE.sums:
            self._subtotal_sums[column] = add_exactly(
                self._subtotal_sums.get(column, Decimal(0)),
                amounts[column],
                section=_SECTION,
                line=row.line,
                column=column,
            )
        self._subtotals.append((row.line, row.cells))
        self._run = []
        self._run_sums = {}

    def total(self, row: AggregationRow) -> None:
        """Check the ``Total`` row against the block's ``SubTotal`` rows."""
        amounts = _shape(row, _TOTAL_SHAPE)
        if self._run:
            raise self._run_without_subtotal()
        if not self._subtotals:
            raise _error(
                row.line,
                None,
                f"the 'Total' row on line {row.line} has no 'SubTotal' row above it "
                f"in its block. {_RE_EXPORT}",
            )
        for column in _TOTAL_SHAPE.keys:
            if any(subtotal[column] != row.cells[column] for _, subtotal in self._subtotals):
                raise _error(
                    row.line,
                    column,
                    f"the 'Total' row on line {row.line} names a different asset "
                    f"category or currency from the rows it covers. {_RE_EXPORT}",
                )
        subtotal_lines = ", ".join(str(line) for line, _ in self._subtotals)
        noun = "line" if len(self._subtotals) == 1 else "lines"
        for column in _TOTAL_SHAPE.sums:
            if not _matches(amounts[column], self._subtotal_sums[column], column, row.line):
                raise _error(
                    row.line,
                    column,
                    f"the 'Total' row on line {row.line} doesn't equal the sum of the "
                    f"'SubTotal' rows on {noun} {subtotal_lines} in its {column!r} "
                    f"column. A row may be missing or changed. {_RE_EXPORT}",
                )
        self._total_line = row.line

    def finish(self) -> None:
        """Refuse a block whose rows don't end with a ``SubTotal`` and the ``Total``."""
        if self._run:
            raise self._run_without_subtotal()
        if self._subtotals and self._total_line is None:
            raise _error(
                self._subtotals[-1][0],
                None,
                f"the Trades block whose header is on line {self._header_line} ends "
                f"without a 'Total' row. {_RE_EXPORT}",
            )

    def _run_without_subtotal(self) -> ParseError:
        first, last = self._run[0][0], self._run[-1][0]
        return _error(
            last,
            None,
            f"the rows on {_span(first, last)} end without a 'SubTotal' row. {_RE_EXPORT}",
        )


def _shape(row: AggregationRow, shape: _Shape) -> dict[str, Decimal]:
    """Check every cell of ``row``, in column order; return its amounts."""
    amounts: dict[str, Decimal] = {}
    for column in COLUMNS:
        if column in shape.blank:
            if row.cells.get(column, "") not in shape.blank[column]:
                raise _error(
                    row.line,
                    column,
                    f"the {shape.label} on line {row.line} has a value in its {column!r} "
                    f"column, which must be empty. {_RE_EXPORT}",
                )
            continue
        if column not in row.cells:
            raise _error(
                row.line,
                column,
                f"the {shape.label} on line {row.line} is missing its {column!r} cell.",
            )
        if column in shape.sums:
            amount = _finite(row.cells[column])
            if amount is None:
                raise _error(
                    row.line,
                    column,
                    f"the {shape.label} on line {row.line} needs a finite decimal in its "
                    f"{column!r} column.",
                )
            _require_within_limits(amount, row.line, column)
            amounts[column] = amount
    return amounts


def _matches(printed: Decimal, total: Decimal, column: str, line: int) -> bool:
    """Whether a printed total equals its exact sum, within the ``Basis`` tolerance."""
    if column != "Basis":
        return printed == total
    # copy_negate and copy_abs are exact; unary minus and abs() round.
    difference = add_exactly(
        printed, total.copy_negate(), section=_SECTION, line=line, column=column
    )
    return difference.copy_abs() <= BASIS_TOLERANCE


def _finite(raw: str) -> Decimal | None:
    """``raw`` as a finite decimal, or ``None``."""
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _require_within_limits(amount: Decimal, line: int, column: str) -> None:
    """Refuse ``amount`` past its column's read-time digit limits (#1276)."""
    kind = NumberKind.QUANTITY if column == "Quantity" else NumberKind.MONEY
    breach = number_limit_breach(amount, kind)
    if breach is not None:
        raise _error(line, column, breach)


def _span(first: int, last: int) -> str:
    return f"line {first}" if first == last else f"lines {first}-{last}"


def _error(line: int, column: str | None, message: str) -> ParseError:
    return ParseError(
        section=_SECTION,
        row_index=line,
        column=column,
        message=message,
        raw_value="",
    )
