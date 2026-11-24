"""The Robinhood CG compute's own result for one-share sales of a security,
and a review file's rows as text, shared by the Robinhood outputs and pipeline
tests (#1845)."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ibkr_fa.compute.robinhood_lots import RobinhoodLotSlice
from ibkr_fa.config import RobinhoodSecurityConfig
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from tests._robinhood_cg_inputs import cg_config, cg_from_lots, cg_lot_result, cg_slice, cg_trade
from tests._robinhood_exact_bundle import SECURITY


def review_rows(path: Path) -> list[list[str]]:
    """``path``'s rows as text, without the CSV apostrophe escape; a blank XLSX cell is ``""``."""
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [[cell.removeprefix("'") for cell in row] for row in csv.reader(handle)]
    workbook = load_workbook(path)
    worksheet = workbook.active
    assert worksheet is not None
    rows = [
        ["" if cell is None else str(cell) for cell in row]
        for row in worksheet.iter_rows(values_only=True)
    ]
    workbook.close()
    return rows


def _one_share_trade(
    kind: RobinhoodEventKind,
    ordinal: int,
    day: date,
    amount: str,
    security: RobinhoodSecurityConfig,
) -> RobinhoodEvent:
    """One share of ``security`` traded for ``amount``, negative for a buy."""
    symbol = security.instrument.symbol
    return replace(
        cg_trade(kind, ordinal, day, "1", amount.removeprefix("-"), amount),
        instrument=symbol,
        description=f"Synthetic {symbol}",
    )


def one_share_sales_cg(
    *sales: tuple[date, date, str, str],
    security: RobinhoodSecurityConfig = SECURITY,
) -> Any:
    """The real Robinhood CG compute's result for one-share sales of
    ``security``, each ``(buy date, sale date, USD cost, USD Amount)`` from
    its own lot, at the compute tests' flat ₹85 TTBR (#1383)."""
    events: list[RobinhoodEvent] = []
    slices: list[RobinhoodLotSlice] = []
    for index, (bought, sold, cost, amount) in enumerate(sales):
        buy, sell = 2 * index + 1, 2 * index + 2
        events += (
            _one_share_trade(RobinhoodEventKind.EQUITY_BUY, buy, bought, f"-{cost}", security),
            _one_share_trade(RobinhoodEventKind.EQUITY_SELL, sell, sold, amount, security),
        )
        slices.append(cg_slice(buy, (sell,), quantity="1", cost=cost, original_date=bought))
    return cg_from_lots(
        tuple(events), cg_lot_result(tuple(slices)), config=cg_config(securities=(security,))
    )
