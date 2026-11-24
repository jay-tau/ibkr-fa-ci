"""Tests for :mod:`ibkr_fa.models.execution_event` invariants (#640)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import ExecutionEvent, ExecutionKind, Side
from ibkr_fa.types import CountryCode, Isin, Ticker

# Synthetic XQAW round trip: 25 shares bought at 38.75 for 969.75 with a
# 1.00 commission, sold at 44.20 for 1104.00 net of a 1.00 commission.
_OPEN = ExecutionEvent(
    currency_primary="USD",
    symbol=Ticker("XQAW"),
    description="SAMPLE ISSUER AW INC",
    isin=Isin("US99990E1038"),
    listing_exchange="NASDAQ",
    issuer_country_code=CountryCode("US"),
    kind=ExecutionKind.OPEN,
    side=Side.LONG,
    trade_date_time=datetime(2026, 6, 16, 10, 15),
    row_index=0,
    quantity=Decimal("25"),
    acquisition_cost=Decimal("969.75"),
    net_proceeds=None,
    trade_price=None,
)
_CLOSE = replace(
    _OPEN,
    kind=ExecutionKind.CLOSE,
    row_index=1,
    acquisition_cost=None,
    net_proceeds=Decimal("1104.00"),
    trade_price=Decimal("44.20"),
)


def test_valid_events_construct() -> None:
    """A SHORT open is valid, as is a SHORT close (the matcher refuses
    it, #109) and a close with negative net proceeds at a zero price (a
    worthless removal after commission)."""
    replace(_OPEN, side=Side.SHORT)
    replace(_CLOSE, side=Side.SHORT)
    replace(_CLOSE, net_proceeds=Decimal("-1"), trade_price=Decimal("0"))


@pytest.mark.parametrize(
    ("event", "changes", "what"),
    [
        (_OPEN, {"symbol": Ticker("")}, "symbol must be non-empty"),
        (_OPEN, {"row_index": -1}, "row_index must be non-negative"),
        (_OPEN, {"quantity": Decimal("0")}, "quantity must be positive"),
        (_OPEN, {"quantity": Decimal("NaN")}, "quantity must be positive"),
        (_CLOSE, {"kind": "O"}, "kind must be an ExecutionKind enum member"),
        (_OPEN, {"side": None}, "side must be a Side enum member"),
        (_OPEN, {"acquisition_cost": None}, "needs a non-negative finite acquisition_cost"),
        (_OPEN, {"acquisition_cost": Decimal("-1")}, "non-negative finite acquisition_cost"),
        (_OPEN, {"trade_price": Decimal("1")}, "carries no net_proceeds or trade_price"),
        (_CLOSE, {"acquisition_cost": Decimal("1")}, "carries no acquisition_cost"),
        (_CLOSE, {"net_proceeds": None}, "needs a finite net_proceeds"),
        (_CLOSE, {"net_proceeds": Decimal("Infinity")}, "needs a finite net_proceeds"),
        (_CLOSE, {"trade_price": None}, "non-negative finite trade_price"),
        (_CLOSE, {"trade_price": Decimal("-0.01")}, "non-negative finite trade_price"),
    ],
)
def test_invariants_refuse_inconsistent_events(
    event: ExecutionEvent, changes: dict[str, object], what: str
) -> None:
    with pytest.raises(InvariantError, match=what):
        replace(event, **changes)
