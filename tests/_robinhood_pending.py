"""Shared values and builders for the Dec-31 pending-trade A3 tests (#1246).

A trade on 2025-12-31 settles on 2026-01-02, so the December statement shows
it only as a Pending Trades row. Normalization proves the trade-date quantity
from that row (#493), and Schedule FA A3 compares and values the trade-dated
lots at that quantity.

:func:`load_dec31_accounts` makes every command load two accounts, the first
making a Dec-31 trade; the pending A3 tests and the Schedule FA blocker
harness in ``tests/_robinhood_fa_blockers.py`` share it (#1815).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

import ibkr_fa.compute.robinhood_fa as robinhood_fa_mod
import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.inventory import RobinhoodPositionSnapshotBasis
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementSection,
    SourceLocation,
)
from tests._robinhood_a3_load import a3_bundle, flat_ttbr, in_symbol, serve_closes
from tests._robinhood_fa_blocker_lines import (
    BY_HAND,
    CLOSING_CAUSE,
    FA_UNRESOLVED,
    REFERENCES,
)
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_security import robinhood_security

# Privacy markers: the pending row's description and the traded symbol.
# A refusal, chain or CLI output must omit whichever markers its input holds.
DESCRIPTION: Final = "SYNTHPENDDESC"
SYMBOL: Final = "ZQXP"
MARKERS: Final = (DESCRIPTION, SYMBOL)
TRADED: Final = date(2025, 12, 31)
SETTLES: Final = date(2026, 1, 2)


def dec31_trade(event: RobinhoodEvent, row: int) -> RobinhoodEvent:
    """``event`` as activity report row ``row``, traded 2025-12-31 and
    settling 2026-01-02."""
    return replace(
        event,
        source_record_index=row,
        activity_date=TRADED,
        process_date=TRADED,
        settle_date=SETTLES,
    )


def pending_row(event: RobinhoodEvent, row: int = 1) -> RobinhoodStatementActivity:
    """The December statement's Pending Trades row for ``event``, described
    ``DESCRIPTION`` and shown under ``SYMBOL``."""
    amount = event.amount_usd
    if amount is None or event.cusip is None:
        raise ValueError("a pending trade needs an amount and a CUSIP")
    return RobinhoodStatementActivity(
        location=SourceLocation(page=2, section=RobinhoodStatementSection.PENDING_TRADES, row=row),
        description=DESCRIPTION,
        symbol=SYMBOL,
        account_type="Margin",
        transaction_code=event.trans_code,
        transaction_date=event.activity_date,
        settle_date=event.settle_date,
        quantity=event.quantity,
        quantity_suffix=None,
        price_usd=event.price_usd,
        debit_usd=-amount if amount < 0 else None,
        credit_usd=amount if amount > 0 else None,
        cusips=(event.cusip,),
    )


def forge_settlement_anchors(monkeypatch: pytest.MonkeyPatch, **changes: Decimal) -> None:
    """Make Schedule FA A3's Dec-31 position step, and only it, read each
    settlement-date anchor with ``changes`` applied. A2 keeps the real anchors."""
    positions = robinhood_fa_mod._dec31_positions

    def forged(paired: Any, securities: Any, quantities: Any, calendar_year: int) -> Any:
        anchors = tuple(
            replace(anchor, **changes)
            if anchor.snapshot_basis is RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE
            else anchor
            for anchor in quantities.anchors
        )
        return positions(paired, securities, replace(quantities, anchors=anchors), calendar_year)

    monkeypatch.setattr(robinhood_fa_mod, "_dec31_positions", forged)


_FY_END = date(2026, 3, 31)
# The trades settled in CY 2025 and the Dec-31 trade of #1246's repro: 2
# shares bought for $20 in January, and 1 for $10 on Dec 31.
DEC31_REPRO = ((_buy("2"),), dec31_trade(_buy("1"), 5))


def _quantity(events: tuple[RobinhoodEvent, ...]) -> Decimal:
    """The shares ``events`` buy, less those they sell."""
    return sum(
        (
            event.quantity if event.kind is RobinhoodEventKind.EQUITY_BUY else -event.quantity
            for event in events
            if event.quantity is not None
        ),
        Decimal(),
    )


def _statements(
    number: str,
    family: RobinhoodStatementFamily,
    settled: tuple[RobinhoodEvent, ...],
    trades: tuple[RobinhoodEvent, ...],
    *,
    pending: bool,
) -> tuple[RobinhoodStatement, RobinhoodStatement]:
    """``number``'s CY 2025 statement of ``settled``, with each of ``trades``
    as a Pending Trades row if ``pending``, and its Q1 2026 statement of
    ``trades``, settled. Each shows its shares under ``SYMBOL`` at $15."""
    cash = sum((event.amount_usd or Decimal() for event in settled), Decimal())
    year = _statement(number, family, settled, cash=cash, position_quantity=_quantity(settled))
    rows = tuple(pending_row(trade, row) for row, trade in enumerate(trades, start=1))
    year = replace(year, activities=(*year.activities, *(rows if pending else ())))
    quarter = _statement(
        number,
        family,
        trades,
        cash=cash + sum((trade.amount_usd or Decimal() for trade in trades), Decimal()),
        position_quantity=_quantity((*settled, *trades)),
        start=date(2026, 1, 1),
        end=_FY_END,
        opening_cash=cash,
        opening_securities=year.account_summary.closing_total_securities_usd,
    )
    return in_symbol(year, SYMBOL), in_symbol(quarter, SYMBOL)


def load_dec31_accounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trades: tuple[tuple[RobinhoodEvent, ...], RobinhoodEvent],
    *,
    pending: bool = True,
    dec31_close: Decimal = Decimal(15),
) -> tuple[str, ...]:
    """Make every command load two accounts, reconciled as it loads them, the
    first making ``trades`` in ``SYMBOL``, with the Dec-31 trade's Pending
    Trades row if ``pending``; return the markers in that input. Schedule FA's
    TTBR is 80 every day of CY 2025, and Yahoo closes at $15 every session but
    Dec 31's, at ``dec31_close``."""
    settled, trade = trades
    marked = tuple(replace(event, instrument=SYMBOL) for event in (*settled, trade))
    deposit = (_cash(),)
    statements = (
        *_statements(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            marked[:-1],
            marked[-1:],
            pending=pending,
        ),
        *_statements(
            "222222222", RobinhoodStatementFamily.MANAGED_INDIVIDUAL, deposit, (), pending=pending
        ),
    )
    accounts = (_account("111111111"), _account("222222222"))
    security = robinhood_security(
        symbol=SYMBOL, cusip=_CUSIP, isin=_ISIN, source="synthetic issuer evidence"
    )
    config = RobinhoodConfig(accounts=accounts, securities=(security,))

    def load(**_kwargs: object) -> SimpleNamespace:
        paired = reconcile_robinhood_accounts(
            validate_robinhood_statement_bundles(statements),
            tuple(
                RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_FY_END)
                for items in (marked, deposit)
            ),
            accounts,
        )
        return a3_bundle(paired, config, tmp_path)

    monkeypatch.setattr(pipeline, "_load", load)
    flat_ttbr(monkeypatch, Decimal(80))
    serve_closes(monkeypatch, SYMBOL, changes={TRADED: dec31_close})
    rows = [row for statement in statements for row in statement.activities]
    texts = {
        *(event.instrument for event in marked),
        *(text for row in rows for text in (row.description, row.symbol)),
        *(item.symbol_or_cusip for statement in statements for item in statement.positions),
    }
    return tuple(marker for marker in MARKERS if marker in texts)


# #1550: the a3-closing line after the fixed one names the security and the
# lots the statement's total can't price; format() fills ``{lots}``.
CLOSING_BLOCKED = (
    f"{FA_UNRESOLVED}\n{CLOSING_CAUSE} {REFERENCES}Robinhood security 1 (lots {{lots}}). {BY_HAND}."
)
