"""Add IBKR's ``SubTotal`` and ``Total`` rows to synthetic Trades blocks (#1090).

The splitter checks every Trades block's printed totals against its rows
and refuses a block with rows but no totals, so every synthetic Activity
Statement needs them, built from its own rows:

* one ``SubTotal`` row after each run of consecutive rows of one symbol,
  holding the run's ``Asset Category``, ``Currency`` and ``Symbol`` and
  the sums of its ``Quantity``, ``Proceeds``, ``Comm/Fee``, ``Basis``,
  ``Realized P/L`` and ``MTM P/L``;
* one ``Total`` row after the last run, holding the ``Asset Category``
  and ``Currency`` and the sums of the ``SubTotal`` rows' amounts.

Every other cell is empty, and each new row is as wide as the row before
it. Any ``SubTotal`` or ``Total`` row already in a Trades block is
replaced, so a test can edit a fixture's trade row and re-total it; all
other lines are kept byte for byte. A sum counts a cell that isn't a
decimal as ``0``: its row refuses before any total is checked.

Tests build inline statements with :func:`with_trades_totals`, which
:func:`tests._income_blocks.with_totals` also applies; ``uv run python -m
tests._trades_totals FIXTURE...`` rewrites fixture files in place.
"""

from __future__ import annotations

import csv
import io
import sys
from collections.abc import Iterable
from decimal import MAX_PREC, Decimal, InvalidOperation, localcontext
from pathlib import Path

_SUBTOTAL_KEYS = ("Asset Category", "Currency", "Symbol")
_SUBTOTAL_SUMS = ("Quantity", "Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L")
_TOTAL_KEYS = ("Asset Category", "Currency")
_TOTAL_SUMS = ("Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L")


def with_trades_totals(text: str) -> str:
    """Return ``text`` with fresh ``SubTotal`` and ``Total`` rows in each Trades block.

    A Trades block with no rows gets none.
    """
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if text.endswith("\r\n") else "\n"
    # Lines to drop, and lines to insert after each 1-based line.
    dropped: set[int] = set()
    inserts: dict[int, list[str]] = {}
    columns: list[str] = []
    run: list[tuple[int, dict[str, str], int]] = []
    subtotals: list[dict[str, str]] = []
    last_line = width = 0

    def close_run() -> None:
        nonlocal run
        if run:
            cells = {key: run[0][1].get(key, "") for key in _SUBTOTAL_KEYS}
            cells |= {
                column: _sum(row.get(column, "") for _, row, _ in run) for column in _SUBTOTAL_SUMS
            }
            subtotals.append(cells)
            inserts.setdefault(run[-1][0], []).append(_row("SubTotal", columns, cells, run[-1][2]))
            run = []

    def close_block() -> None:
        nonlocal subtotals
        close_run()
        if subtotals:
            cells = {key: subtotals[0][key] for key in _TOTAL_KEYS}
            cells |= {column: _sum(sub[column] for sub in subtotals) for column in _TOTAL_SUMS}
            inserts.setdefault(last_line, []).append(_row("Total", columns, cells, width))
            subtotals = []

    reader = csv.reader(lines)
    previous = 0
    for record in reader:
        first, line = previous + 1, reader.line_num
        previous = line
        in_trades = len(record) > 1 and record[0] == "Trades"
        if not in_trades or record[1] == "Header":
            close_block()
        if not in_trades:
            continue
        if record[1] == "Header":
            columns = _strip_trailing_empty(record[2:])
        elif record[1] in {"SubTotal", "Total"}:
            dropped.update(range(first, line + 1))
            continue
        else:
            row = dict(zip(columns, record[2:], strict=False))
            if run and run[-1][1].get("Symbol") != row.get("Symbol"):
                close_run()
            run.append((line, row, len(record)))
        last_line, width = line, len(record)
    close_block()
    return _splice(lines, dropped, inserts, newline)


def _splice(
    lines: list[str], dropped: set[int], inserts: dict[int, list[str]], newline: str
) -> str:
    """``lines`` without the ``dropped`` lines, with ``inserts`` after their lines."""
    out: list[str] = []
    for number, physical in enumerate(lines, start=1):
        if number not in dropped:
            out.append(physical)
        for added in inserts.get(number, []):
            if out and not out[-1].endswith(("\n", "\r")):
                out[-1] += newline
            out.append(added + newline)
    if inserts and lines and not lines[-1].endswith(("\n", "\r")):
        out[-1] = out[-1].removesuffix(newline)
    return "".join(out)


def _row(kind: str, columns: list[str], cells: dict[str, str], width: int) -> str:
    record = ["Trades", kind, *(cells.get(column, "") for column in columns)]
    record += [""] * (width - len(record))
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(record)
    return buffer.getvalue()


def _sum(values: Iterable[str]) -> str:
    """The exact sum of ``values``, as the splitter adds them."""
    total = Decimal(0)
    with localcontext(prec=MAX_PREC):
        for value in values:
            try:
                total += Decimal(value)
            except InvalidOperation:
                continue
    return format(total, "f")


def _strip_trailing_empty(cells: list[str]) -> list[str]:
    while cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def main(paths: list[str]) -> None:
    """Rebuild the Trades totals of each fixture file in ``paths``, in place."""
    for name in paths:
        path = Path(name)
        text = path.read_text(encoding="utf-8")
        path.write_text(with_trades_totals(text), encoding="utf-8", newline="")


if __name__ == "__main__":
    main(sys.argv[1:])
