"""FIFO matching of closes against earlier opens.

:class:`FifoPool` is quantity-only: it decides how much quantity each
open entry gives up to a close, oldest entry first, and subtracts
exactly (#1273). Its callers keep their own ordering and evidence
checks, their own money arithmetic and their own response to a
shortfall.

:func:`match_fifo` is the Activity Statement caller (#104, #640). It
turns the Trades parser's :class:`~ibkr_fa.models.ExecutionEvent`
records into :class:`~ibkr_fa.models.ClosedLot` slices and residual
:class:`~ibkr_fa.models.OpenLot` records. Their quantities and their
cost and proceeds shares are exact up to the filed rounding (#1273).

Each slice's cost basis is the matched open's actual acquisition cost,
FIFO within the account. The broker's close-side ``Basis`` never reaches
the matcher: it can fold in US lot accounting the Indian computation
does not use, most commonly a US wash-sale disallowed-loss add-back and
in principle a non-FIFO lot election. This is the decision recorded in
#107 and #265. ADR 0006
(``docs/decisions/0006-robinhood-multi-account-evidence.md``) records
the authorities and treats account-wise FIFO for a foreign account as
policy by analogy, not settled law. The ``LD`` rows where IBKR rewrites
the open ``Basis`` itself stay refused by the parser (#265).

:func:`require_indian_basis` applies the same rule to lots the broker
matched itself (Flex ``CLOSED_LOT`` rows): a shifted holding-period date
means the broker's cost may carry a US wash-sale adjustment, so the lot
is refused (#599).
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from ibkr_fa.compute.exact_inr import (
    checked_product,
    checked_subtraction,
    exact_share,
    review_amount,
)
from ibkr_fa.exceptions import IbkrFaError, LotMatchingError
from ibkr_fa.invariants import require
from ibkr_fa.models import (
    BasisProvenance,
    ClosedLot,
    ExecutionEvent,
    ExecutionKind,
    OpenLot,
    Side,
)
from ibkr_fa.types import Ticker


class BrokerBasisError(IbkrFaError):
    """A broker-matched lot's cost may not be the actual acquisition cost (#599)."""


@dataclass(frozen=True, slots=True)
class FifoConsumption[T]:
    """The ``(item, quantity)`` slices one close took, or its shortfall.

    Attributes:
        allocations: Slices in FIFO order; empty when ``shortfall`` is
            positive.
        shortfall: Quantity the pool could not cover; 0 on success.
    """

    allocations: tuple[tuple[T, Decimal], ...]
    shortfall: Decimal


class FifoPool[T]:
    """Open entries with their remaining quantity, in insertion order.

    Quantities are subtracted exactly (#1273), never at the caller's
    precision, so what a pool hands out and keeps adds back to what it was
    given.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: deque[tuple[T, Decimal]] = deque()

    def add(self, item: T, quantity: Decimal) -> None:
        """Append an open entry holding ``quantity``."""
        _require_positive(quantity)
        self._entries.append((item, quantity))

    def consume(
        self, quantity: Decimal, *, at: str, prefer_open_on_tie: bool = False
    ) -> FifoConsumption[T]:
        """Take ``quantity`` from the oldest entries, all or nothing.

        A request larger than the pool holds returns its shortfall and
        leaves the pool unchanged. An entry leaves the pool exactly when
        its remaining quantity reaches 0. On a numeric tie,
        ``prefer_open_on_tie`` keeps the open quantity's Decimal exponent.

        Every subtraction is exact (#1273), so the slices add up to
        ``quantity`` and each entry's slices and remainder to what it held.
        A difference past the exact-arithmetic digit cap refuses as
        :func:`~ibkr_fa.compute.exact_inr.checked_subtraction` does, before
        the pool changes; ``at`` is a fixed label naming the caller's row.
        """
        _require_positive(quantity)
        allocations: list[tuple[T, Decimal, Decimal]] = []
        remaining = quantity
        for item, available in self._entries:
            taken = min(available, remaining) if prefer_open_on_tie else min(remaining, available)
            allocations.append((item, taken, checked_subtraction(available, taken, at=at)))
            remaining = checked_subtraction(remaining, taken, at=at)
            if remaining == 0:
                break
        if remaining > 0:
            return FifoConsumption(allocations=(), shortfall=remaining)
        for item, _taken, left in allocations:
            if left == 0:
                self._entries.popleft()
            else:
                self._entries[0] = (item, left)
        return FifoConsumption(
            allocations=tuple((item, taken) for item, taken, _left in allocations),
            shortfall=Decimal(0),
        )

    def remaining(self) -> tuple[tuple[T, Decimal], ...]:
        """Return each open entry with its remaining quantity, oldest first."""
        return tuple(self._entries)


def _require_positive(quantity: Decimal) -> None:
    require(
        quantity.is_finite() and quantity > 0,
        what="FIFO quantity must be positive and finite",
        quantity=quantity,
    )


def match_fifo(
    events: Sequence[ExecutionEvent],
    *,
    dec31_close_prices: Mapping[Ticker, Decimal],
    report_date: date,
) -> tuple[list[OpenLot], list[ClosedLot]]:
    """FIFO-match each close against the same symbol's earlier opens.

    Per symbol, events are ordered by ``(trade_date_time, row_index)``.
    A close consumes only opens that come before it in that order, so it
    never matches a later open, even a same-day or same-second one. Each
    slice emits one :class:`ClosedLot`:

    * cost ``acquisition_cost * slice_qty / open_qty`` (the module
      docstring explains why this ignores the broker's close basis);
    * proceeds ``slice_qty * (net_proceeds / close_qty)``, because the
      close is one execution at a uniform price;
    * gross proceeds ``slice_qty * trade_price`` (FA-A3, #348).

    Opens with quantity left after matching become :class:`OpenLot`
    records with cost ``acquisition_cost * remaining / open_qty``,
    marked at the symbol's Dec-31 close (#133). The price is looked up
    only for these survivors: a symbol closed out during the year has no
    Open Positions row.

    These quantities and amounts are exact up to the filed rounding
    (#1273). The pool subtracts quantities exactly, and gross proceeds
    are an exact product. A cost or proceeds share is the Decimal above
    where that is the exact share, else
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of it, with the
    exact rational in ``cost_basis_money_exact`` or
    ``proceeds_money_exact`` where that Decimal can't hold it. So every
    lot's quantities, costs and proceeds add back exactly to the trade
    they came from.

    Args:
        events: The parser's events, in Trades row order.
        dec31_close_prices: Symbol -> period-end close price, from the
            Open Positions Summary rows.
        report_date: Statement period end, stamped on every
            :class:`OpenLot`.

    Returns:
        ``(open_lots, closed_lots)``. Open lots come out in input order.
        Closed lots come out grouped by symbol, in the order each
        symbol's first close appears in the input; then by close, in
        ``(trade_date_time, row_index)`` order; then by matched open, in
        FIFO order.

    Raises:
        LotMatchingError: A close is a closing BUY, refused before any
            matching, or reaches an open SHORT lot (#109); the earlier
            opens cannot cover a close; a surviving open's symbol has
            no Dec-31 close price (#133); or a share needs more digits
            than exact arithmetic allows, at the close for a slice and
            at the open for a surviving lot (#1273).
        InvariantError: A quantity difference or gross proceeds product
            needs more digits than exact arithmetic allows (#1273).
    """
    require(
        len({event.row_index for event in events}) == len(events),
        what="match_fifo needs one event per Trades row index",
        event_count=len(events),
    )
    opens = [event for event in events if event.kind is ExecutionKind.OPEN]
    opens_by_symbol: dict[Ticker, list[ExecutionEvent]] = defaultdict(list)
    for opened in opens:
        opens_by_symbol[opened.symbol].append(opened)
    closes_by_symbol: dict[Ticker, list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        if event.kind is ExecutionKind.CLOSE:
            _refuse_short_cover(event)
            closes_by_symbol[event.symbol].append(event)

    remaining = {opened: opened.quantity for opened in opens}
    closed_lots: list[ClosedLot] = []
    for symbol, closes in closes_by_symbol.items():
        upcoming = deque(sorted(opens_by_symbol[symbol], key=_fifo_order))
        pool: FifoPool[ExecutionEvent] = FifoPool()
        for closed in sorted(closes, key=_fifo_order):
            while upcoming and _fifo_order(upcoming[0]) < _fifo_order(closed):
                opened = upcoming.popleft()
                pool.add(opened, opened.quantity)
                # Restored below if the pool still holds part of it.
                remaining[opened] = Decimal(0)
            consumption = pool.consume(
                closed.quantity, at=_at(closed, "quantity"), prefer_open_on_tie=True
            )
            _refuse_short_open(closed, consumption.allocations or pool.remaining())
            if consumption.shortfall > 0:
                raise LotMatchingError(
                    row_index=closed.row_index,
                    message=(
                        "the closing trade closes more shares than the earlier "
                        "opening trades of its symbol hold, so part of it can't "
                        "be FIFO-matched. The FII section may be truncated, or the "
                        "opening trades pre-date this Activity Statement's "
                        "period — check Open Positions reconciliation upstream."
                    ),
                )
            closed_lots.extend(
                _closed_lot(opened, closed, quantity)
                for opened, quantity in consumption.allocations
            )
        remaining.update(pool.remaining())

    open_lots = [
        _open_lot(
            opened,
            quantity,
            dec31_close_prices=dec31_close_prices,
            report_date=report_date,
        )
        for opened, quantity in remaining.items()
        if quantity != 0
    ]
    fully_consumed = sum(1 for quantity in remaining.values() if quantity == 0)
    require(
        len(open_lots) + fully_consumed == len(opens),
        what=(
            "match_fifo post: each open is either fully consumed by closes "
            "or emitted as a surviving OpenLot; no open may be silently dropped"
        ),
        open_count=len(opens),
        surviving_open_count=len(open_lots),
        fully_consumed_count=fully_consumed,
    )
    return open_lots, closed_lots


def _fifo_order(event: ExecutionEvent) -> tuple[datetime, int]:
    return event.trade_date_time, event.row_index


def _refuse_short_cover(closed: ExecutionEvent) -> None:
    """Refuse a closing BUY, a buy-to-cover of a SHORT position (#109)."""
    if closed.side is not Side.LONG:
        raise LotMatchingError(
            row_index=closed.row_index,
            message=(
                "the closing trade has a positive Quantity: a closing BUY "
                "(buy-to-cover of a SHORT position). v1 does not support "
                "closing SHORT positions on Schedule FA; see "
                "https://github.com/jay-tau/ibkr-fa/issues/109."
            ),
        )


def _refuse_short_open(
    closed: ExecutionEvent, reached: tuple[tuple[ExecutionEvent, Decimal], ...]
) -> None:
    """Refuse a close whose FIFO walk reaches an open SHORT lot (#109)."""
    for opened, _quantity in reached:
        if opened.side is not Side.LONG:
            raise LotMatchingError(
                row_index=closed.row_index,
                message=(
                    f"the closing trade at row {closed.row_index} matched "
                    f"FIFO-first against an opening SHORT lot at row "
                    f"{opened.row_index}. v1 does not support closing SHORT "
                    "positions on Schedule FA; see "
                    "https://github.com/jay-tau/ibkr-fa/issues/109."
                ),
            )


def _acquisition_cost(opened: ExecutionEvent) -> Decimal:
    cost = opened.acquisition_cost
    assert cost is not None  # narrow for ty; ExecutionEvent requires it on an OPEN
    return cost


def _at(event: ExecutionEvent, column: str) -> str:
    """The fixed refusal label for ``column`` of ``event``'s Trades row."""
    return f"FIFO lot matching, Trades row {event.row_index}, column {column}"


def _share(
    shown: Callable[[], Decimal],
    amount: Decimal,
    part: Decimal,
    whole: Decimal,
    *,
    row_index: int,
) -> tuple[Decimal, Fraction | None]:
    """The share of ``amount`` that ``part`` of ``whole`` carries, exactly (#1273).

    Returns ``shown()``, the share at the caller's precision, where that is
    the exact share, else :func:`~ibkr_fa.compute.exact_inr.exact_decimal`
    of it, plus the exact rational where that Decimal can't hold it.
    Refuses at ``row_index``, before ``shown()`` runs, a share past the
    exact-arithmetic digit cap.
    """
    exact = exact_share(amount, part, whole)
    if exact is None:
        raise LotMatchingError(
            row_index=row_index,
            message="a lot's cost or proceeds share needs more digits than exact arithmetic allows",
        )
    decimal = shown()
    return review_amount(decimal, None if Fraction(decimal) == exact else exact)


def _closed_lot(opened: ExecutionEvent, closed: ExecutionEvent, quantity: Decimal) -> ClosedLot:
    net_proceeds, trade_price = closed.net_proceeds, closed.trade_price
    # Narrow for ty; ExecutionEvent requires both on a CLOSE.
    assert net_proceeds is not None and trade_price is not None
    acquisition_cost = _acquisition_cost(opened)
    cost, cost_exact = _share(
        lambda: acquisition_cost * quantity / opened.quantity,
        acquisition_cost,
        quantity,
        opened.quantity,
        row_index=closed.row_index,
    )
    proceeds, proceeds_exact = _share(
        lambda: quantity * (net_proceeds / closed.quantity),
        net_proceeds,
        quantity,
        closed.quantity,
        row_index=closed.row_index,
    )
    return ClosedLot(
        currency_primary=opened.currency_primary,
        sub_category=None,
        symbol=opened.symbol,
        description=opened.description,
        isin=opened.isin,
        listing_exchange=opened.listing_exchange,
        issuer_country_code=opened.issuer_country_code,
        quantity=quantity,
        cost_basis_money=cost,
        proceeds_money=proceeds,
        open_date_time=opened.trade_date_time.date(),
        close_date_time=closed.trade_date_time.date(),
        gross_proceeds_money=checked_product(
            quantity, trade_price, at=_at(closed, "gross_proceeds_money")
        ),
        basis_provenance=BasisProvenance.ENGINE_FIFO,
        cost_basis_money_exact=cost_exact,
        proceeds_money_exact=proceeds_exact,
    )


def _open_lot(
    opened: ExecutionEvent,
    quantity: Decimal,
    *,
    dec31_close_prices: Mapping[Ticker, Decimal],
    report_date: date,
) -> OpenLot:
    if opened.symbol not in dec31_close_prices:
        raise LotMatchingError(
            row_index=opened.row_index,
            message=(
                "the opening trade leaves a surviving open lot after FIFO "
                "matching, but its symbol does not appear in the Open "
                "Positions section, so the Dec 31 close price can't be "
                "determined. See https://github.com/jay-tau/ibkr-fa/issues/133."
            ),
        )
    acquisition_cost = _acquisition_cost(opened)
    cost, cost_exact = _share(
        lambda: acquisition_cost * quantity / opened.quantity,
        acquisition_cost,
        quantity,
        opened.quantity,
        row_index=opened.row_index,
    )
    return OpenLot(
        currency_primary=opened.currency_primary,
        symbol=opened.symbol,
        description=opened.description,
        isin=opened.isin,
        issuer_country_code=opened.issuer_country_code,
        report_date=report_date,
        quantity=quantity,
        mark_price=dec31_close_prices[opened.symbol],
        cost_basis_money=cost,
        side=opened.side,
        open_date_time=opened.trade_date_time.date(),
        cost_basis_money_exact=cost_exact,
    )


def require_indian_basis(lots: Sequence[ClosedLot]) -> None:
    """Refuse a broker-matched lot whose holding-period date moved (#599).

    IBKR documents that a US wash sale moves ``HoldingPeriodDateTime``,
    and the lot's ``CostBasis`` may then include the US disallowed-loss
    add-back, which the Indian return does not recognise. A Flex lot
    can't show the actual cost, so it is refused. Callers pass the full
    lot list, before any CY/FY filtering, so a lot is refused whatever
    year it falls in. Lots ibkr-fa matched itself pass unchecked.

    Raises:
        BrokerBasisError: If a ``BROKER_MATCHED`` lot's holding-period
            calendar date differs from its open date.
    """
    for lot in lots:
        if (
            lot.basis_provenance is BasisProvenance.BROKER_MATCHED
            and lot.holding_period_date_time != lot.open_date_time
        ):
            raise BrokerBasisError(
                "a broker-matched Flex Query closed lot has a HoldingPeriodDateTime "
                "on a different day from its OpenDateTime. IBKR documents a shifted "
                "holding-period date for US wash sales, so CostBasis may "
                "include a US wash-sale adjustment that the Indian return "
                "does not recognise. Re-run schedule-fa with an IBKR "
                "Activity Statement that covers this lot's acquisition; it "
                "uses the actual "
                "opening-trade cost. LD-coded (wash-sale) Activity Statement "
                "rows are still refused there "
                "(https://github.com/jay-tau/ibkr-fa/issues/265)."
            )
