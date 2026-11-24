"""Value a :class:`HoldingLot` at its resolved Dec-31 close (#627).

A source states what it holds; the engine prices it. This module turns
a :class:`~ibkr_fa.models.holding_lot.HoldingLot` and the
:class:`~ibkr_fa.compute.dec31_close.ResolvedClose` the caller resolved
for it into the :class:`~ibkr_fa.models.OpenLot` the Schedule FA A3
compute reads.

The caller resolves the close, so it can check it against other
evidence or turn a :class:`~ibkr_fa.compute.dec31_close.ClosingPriceError`
into a blocker first. ``ResolvedClose`` records neither its date nor its
quantity: the caller must resolve it for ``report_date`` and
``lot.quantity``. A per-share close whose total is not the exact
``lot.quantity * per_share_usd`` (#1250) was resolved for another
quantity and is refused, as is one whose product can't be computed
exactly; a statement total for another quantity, or a close for another
date, cannot be detected here.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from ibkr_fa.compute.dec31_close import ResolvedClose
from ibkr_fa.compute.exact_inr import exact_product
from ibkr_fa.invariants import require
from ibkr_fa.models import OpenLot
from ibkr_fa.models.holding_lot import HoldingLot

_USD: Final[str] = "USD"


def value_holding_lot(lot: HoldingLot, *, report_date: date, close: ResolvedClose) -> OpenLot:
    """Return ``lot`` valued at ``close`` on ``report_date``.

    The mark is the per-share close, or ``close.total_usd / lot.quantity``
    when only a statement total is known. A statement total is also kept
    as :attr:`OpenLot.closing_total_usd`, so the closing value uses it
    directly rather than multiplying that quotient back (#1242).

    Raises:
        InvariantError: ``lot`` is not USD (``ResolvedClose`` holds only
            USD amounts), a per-share ``close`` was resolved for another
            quantity, or the result breaks an ``OpenLot`` invariant, e.g.
            ``report_date`` is before ``lot.acquisition_date``.
    """
    require(
        lot.currency == _USD,
        what="value_holding_lot needs a USD lot: ResolvedClose holds only USD amounts",
        currency=lot.currency,
        symbol=lot.symbol,
    )
    per_share_total = (
        exact_product(lot.quantity, close.per_share_usd)
        if close.per_share_usd is not None
        else None
    )
    require(
        close.per_share_usd is None
        or (per_share_total is not None and close.total_usd == per_share_total),
        what="value_holding_lot needs a per-share close resolved for lot.quantity",
        quantity=lot.quantity,
        per_share_usd=close.per_share_usd,
        total_usd=close.total_usd,
    )
    statement_total = close.total_usd if close.per_share_usd is None else None
    mark_price = (
        close.per_share_usd if close.per_share_usd is not None else close.total_usd / lot.quantity
    )
    return OpenLot(
        currency_primary=lot.currency,
        symbol=lot.symbol,
        description=lot.description,
        isin=lot.isin,
        issuer_country_code=lot.issuer_country_code,
        report_date=report_date,
        quantity=lot.quantity,
        mark_price=mark_price,
        cost_basis_money=lot.cost_basis_money,
        side=lot.side,
        open_date_time=lot.acquisition_date,
        closing_total_usd=statement_total,
    )
