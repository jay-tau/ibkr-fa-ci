"""Schedule FA Table A3 dividend decisions for the IBKR FA pipeline (#626).

* :func:`resolve_fa_a3_dividends` — the per-symbol INR totals for the A3
  gross-amount column, or ``None`` for manual fill (PR #137 "zero-when-sure").
* :func:`classify_orphan_dividends` — aggregated dividend symbols that no
  A3 row carries, classified for the CLI's Schedule OS / CG advisory (#142).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.dividends import aggregate_dividends_by_symbol_inr
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.models import ClosedLot, Dividend, DividendEvidence, OpenLot, Side
from ibkr_fa.models.fa_orphan_dividend import OrphanDividendInfo
from ibkr_fa.types import Ticker


def resolve_fa_a3_dividends(
    dividends: Sequence[Dividend],
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
    dividend_evidence: DividendEvidence,
) -> tuple[dict[Ticker, Decimal] | None, bool]:
    """Aggregate per-symbol INR CY dividends for the FA gross-amount column.

    An empty dividend list proves zero only when ``dividend_evidence`` is
    ``ABSENT`` (a full-CY Activity Statement with no dividend or accrual
    section), and only once the dispatcher's ``Change in NAV`` check has
    passed (#1324): a statement whose ``Change in NAV`` shows dividends
    without a ``Dividends`` section refuses before this runs. Flex Queries
    and incomplete dividend evidence return ``None`` for manual fill, as
    does an aggregate with no in-CY payment.

    Returns:
        ``(by_symbol_inr, zeroed_no_cy_dividends)``: the per-symbol totals
        (``{}`` when zero is proven, ``None`` for manual fill), and whether
        zero was proven.

    Raises:
        MissingFxRateError: When an in-CY dividend needs a missing December 31
            TTBR.
    """
    if not dividends:
        if dividend_evidence is DividendEvidence.ABSENT:
            return {}, True
        return None, False
    aggregate = aggregate_dividends_by_symbol_inr(
        dividends, calendar_year=calendar_year, fx_policy=fx_policy
    )
    return (aggregate if aggregate else None), False


def classify_orphan_dividends(
    dividends_by_symbol_inr: Mapping[Ticker, Decimal] | None,
    *,
    a3_symbols: Set[Ticker],
    dividends: Sequence[Dividend],
    open_lots: Sequence[OpenLot],
    closed_lots: Sequence[ClosedLot],
    calendar_year: int,
) -> tuple[OrphanDividendInfo, ...]:
    """Classify each aggregated dividend symbol not in ``a3_symbols``.

    A symbol is ``previously_held`` when it appears as a LONG open lot or
    any closed lot, else ``never_held``. SHORT opens don't count (T3 PR
    #170 P2): a short is a borrowed-and-resold liability, so a SHORT-only
    payment in lieu must not get the "previously owned and sold; sale
    belongs on Schedule CG" advisory. Closed lots are LONG-only by
    construction.

    ``latest_payment_date`` is the symbol's latest payment inside the
    calendar year, matching the window
    :func:`~ibkr_fa.compute.dividends.aggregate_dividends_by_symbol_inr`
    sums (T1-correctness PR #170 P3). ``last_sold_date`` is the symbol's
    latest close, set only for ``previously_held``.

    Returns:
        One record per orphan symbol, sorted by symbol; ``()`` when the
        dividends are unresolved (``dividends_by_symbol_inr`` is ``None``).
    """
    if dividends_by_symbol_inr is None:
        return ()
    traded_symbols: set[Ticker] = {lot.symbol for lot in open_lots if lot.side == Side.LONG} | {
        lot.symbol for lot in closed_lots
    }
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    latest_payment_date_by_symbol: dict[Ticker, date] = {}
    for div in dividends:
        if not (cy_start <= div.payment_date <= cy_end):
            continue
        current = latest_payment_date_by_symbol.get(div.symbol)
        if current is None or div.payment_date > current:
            latest_payment_date_by_symbol[div.symbol] = div.payment_date
    latest_close_date_by_symbol: dict[Ticker, date] = {}
    for lot in closed_lots:
        current_close = latest_close_date_by_symbol.get(lot.symbol)
        if current_close is None or lot.close_date_time > current_close:
            latest_close_date_by_symbol[lot.symbol] = lot.close_date_time
    return tuple(
        OrphanDividendInfo(
            symbol=symbol,
            classification=("previously_held" if symbol in traded_symbols else "never_held"),
            total_amount_inr=total_inr,
            latest_payment_date=latest_payment_date_by_symbol[symbol],
            last_sold_date=latest_close_date_by_symbol.get(symbol)
            if symbol in traded_symbols
            else None,
        )
        for symbol, total_inr in sorted(dividends_by_symbol_inr.items(), key=lambda kv: str(kv[0]))
        if symbol not in a3_symbols
    )
