"""Build Activity Statement income blocks for the parser tests (#908)."""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation

from ibkr_fa.parser.activity_statement import KnownActivitySection
from ibkr_fa.parser.activity_statement.multi_section import Section
from tests._trades_totals import with_trades_totals

_INCOME_SECTIONS = frozenset({"Dividends", "Withholding Tax", "Interest"})

HEADER_LINE = 39
"""The file line of a test block's header; its rows start on the next line."""

COLUMNS = ("Currency", "Date", "Description", "Amount")


def total_row(amount: str) -> dict[str, str]:
    """The per-currency ``Total`` row IBKR prints after a run of rows."""
    return {"Currency": "Total", "Date": "", "Description": "", "Amount": amount}


def summary_row(section: str, currency: str, amount: str) -> dict[str, str]:
    """The ``Total <Section> in <CCY>`` summary row that can end a block."""
    return {
        "Currency": currency,
        "Date": "",
        "Description": f"Total {section} in {currency}",
        "Amount": amount,
    }


def block(
    kind: KnownActivitySection,
    rows: list[dict[str, str]],
    columns: tuple[str, ...] = COLUMNS,
) -> Section:
    """``rows`` as one block whose header is on :data:`HEADER_LINE`."""
    first = HEADER_LINE + 1
    return Section(
        kind=kind,
        columns=columns,
        header_line=HEADER_LINE,
        rows=rows,
        row_lines=list(range(first, first + len(rows))),
    )


def closed(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """``rows`` followed by the ``Total`` row IBKR prints after them, if any.

    The total is the rows' sum, or ``0`` when an amount is missing or
    isn't a decimal: such a row refuses before the parser reaches the
    total.
    """
    if not rows:
        return rows
    try:
        total = sum((Decimal(row["Amount"]) for row in rows), Decimal(0))
    except (KeyError, InvalidOperation):
        total = Decimal(0)
    return [*rows, total_row(str(total))]


def _sum(amounts: list[str]) -> Decimal:
    """The amounts' sum, or ``0`` when one isn't a decimal: its row refuses first."""
    try:
        return sum((Decimal(amount) for amount in amounts), Decimal(0))
    except InvalidOperation:
        return Decimal(0)


def with_totals(text: str) -> str:
    """Activity Statement ``text`` with the ``Total`` rows IBKR prints.

    Each run of Dividends, Withholding Tax or Interest ``Data`` rows of
    one currency gets a ``Total`` row after it holding their sum, padded
    to the run's width, unless one already follows it. Each Trades block
    gets its ``SubTotal`` and ``Total`` rows (:func:`with_trades_totals`).
    """
    lines = text.splitlines(keepends=True)
    reader = csv.reader(lines)
    inserts: dict[int, str] = {}
    run: tuple[str, str] | None = None
    amounts: list[str] = []
    last_line = width = 0

    def close() -> None:
        if run is not None:
            cells = [run[0], "Data", "Total", "", "", str(_sum(amounts))]
            inserts[last_line] = ",".join(cells + [""] * (width - len(cells))) + "\n"

    for cells in reader:
        income = len(cells) > 5 and cells[0] in _INCOME_SECTIONS and cells[1] == "Data"
        if income and cells[2] == "Total" and run is not None and run[0] == cells[0]:
            run = None
            continue
        key = (cells[0], cells[2]) if income else None
        if key != run:
            close()
            run, amounts = key, []
        if key is not None:
            amounts.append(cells[5])
            last_line, width = reader.line_num, len(cells)
    close()
    out: list[str] = []
    for number, line in enumerate(lines, start=1):
        out.append(line)
        if number in inserts:
            out.append(("" if line.endswith("\n") else "\n") + inserts[number])
    return with_trades_totals("".join(out))
