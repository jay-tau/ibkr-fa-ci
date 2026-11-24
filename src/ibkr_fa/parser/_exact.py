"""Exact arithmetic for the parsers' own checks and derived values (#908, #1295, #1311).

A parser that checks a printed total against the rows it covers, or a printed
amount against a product of other cells, must keep every digit: a sum or
product rounded to the active decimal context's precision could hide a lost
row, or match an amount the cells don't give. So must a value a parser derives
from its cells. :func:`exactly` keeps every digit or refuses with the caller's
own :class:`~ibkr_fa.exceptions.IbkrFaError`. The IBKR Activity Statement totals
and the Robinhood activity CSV's and brokerage statement's checks use it, and
so do the Robinhood normalizer's sums, identities and quantity bands (#1308),
the StockPlan Holdings Market Value check (#1290) and the Activity Statement's
Change in NAV income check (#1279).
The IBKR Activity Statement Trades net proceeds and Open Positions
reconciliation, the Flex closed-lot proceeds and, through
:func:`distance_from_product`, the Flex Open Positions ``PositionValue`` check
use it too (#1311), as does the Activity Statement Open Positions ``Value``
check (#1315). Parsers may not import :mod:`ibkr_fa.compute`, so this is
the parser layer's counterpart of :mod:`ibkr_fa.compute.exact_inr`.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import MAX_PREC, Decimal, DecimalException, Inexact, localcontext

from ibkr_fa.exceptions import IbkrFaError


def exactly(
    operation: Callable[..., Decimal],
    *operands: Decimal,
    refusal: Callable[[bool], IbkrFaError],
) -> Decimal:
    """Return ``operation(*operands)`` with every digit kept.

    It runs in a copy of the active context at :data:`~decimal.MAX_PREC`
    digits with ``Inexact`` trapped, so it can't round. A decimal
    arithmetic failure, such as an overflow past the context's exponent
    limit, or running out of memory raises ``refusal(out_of_memory)``.
    That error is raised outside the handler, so it chains no exception.
    """
    out_of_memory = False
    with localcontext(prec=MAX_PREC) as context:
        context.traps[Inexact] = True
        try:
            return operation(*operands)
        except MemoryError:
            out_of_memory = True
        except DecimalException:
            pass
    raise refusal(out_of_memory)


def distance_from_product(
    value: Decimal,
    quantity: Decimal,
    price: Decimal,
    *,
    refusal: Callable[[bool], IbkrFaError],
) -> Decimal:
    """Return ``|value - quantity * price|`` with every digit kept.

    The product and the difference run in :func:`exactly`, which raises
    ``refusal(out_of_memory)`` where they can't be kept, and ``copy_abs()``
    takes the magnitude without rounding. The caller compares the result with
    its own tolerance and raises its own refusal.
    """
    return exactly(_less_product, value, quantity, price, refusal=refusal).copy_abs()


def _less_product(value: Decimal, quantity: Decimal, price: Decimal) -> Decimal:
    return value - quantity * price
