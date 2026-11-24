"""Compute Schedule FA Table A3 ``initial_value_inr`` for one lot.

Per the CBDT Schedule-FA FAQ guidance and practitioner consensus
(ClearTax, Quicko, Anshul Dixit, Dinesh Aarjav), the *initial value*
of a foreign asset is converted to INR at the SBI TT-Buying rate on
the **acquisition date**. The foreign-currency amount is the lot's
acquisition value: the cash paid for a purchase, and the FMV at
acquisition for an ESPP purchase (ADR-0003 decision 2) or an RSU vest.

For both :class:`OpenLot` and :class:`ClosedLot` the acquisition date
is :attr:`open_date_time` (NOT ``HoldingPeriodDateTime``, which
wash-sale rules can roll backwards). PR #86's parser-level validation
ensures ``open_date_time`` is the right field; this module just
trusts that contract. Any Flex lot whose ``HoldingPeriodDateTime``
date differs from ``OpenDateTime`` is refused, because its cost basis
may include a US wash-sale add-back (#599): open lots by the Flex
parser, closed lots by the engine (#640).

A lot whose cost has an exact rational
(:attr:`~ibkr_fa.models.OpenLot.cost_basis_money_exact`), such as a
Robinhood lot's share of a third of its cost, converts that rational
instead, so its filed rupees are the exact HALF_UP (#1243).

The returned :class:`Decimal` is **full precision** — no rounding,
no quantization. :attr:`FaA3Row.filed <ibkr_fa.models.fa_a3_row.FaA3Row.filed>`
rounds it to the whole rupees every writer renders. Keeping precision
inside compute means downstream stages can sum / cross-check exact
figures without accumulated rounding error.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import checked_exact_product, checked_product
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import ClosedLot, OpenLot


def initial_value_inr(
    cost_basis_money: Decimal,
    acquired_on: date,
    *,
    fx_policy: FxPolicy,
) -> Decimal:
    """Convert a cost basis in foreign currency to INR using the FX policy.

    Pure function: zero side effects beyond delegating to
    :meth:`FxPolicy.rate_for`. Caller is responsible for ensuring
    ``cost_basis_money`` and ``acquired_on`` reference the same
    economic event (the lot's acquisition).

    Args:
        cost_basis_money: The foreign-currency cost basis (e.g.,
            USD for v1). Must be a :class:`~decimal.Decimal` to
            preserve IBKR's published precision through the
            multiplication.
        acquired_on: Calendar date of the acquisition. Schedule FA
            uses calendar dates only — time of day is not relevant
            to any Indian-tax FX rule.
        fx_policy: An :class:`FxPolicy` whose
            :attr:`~FxPolicy.currency` matches the source currency
            of ``cost_basis_money``. The pipeline picks the policy;
            this function trusts the choice.

    Returns:
        The exact INR value, ``cost_basis_money * rate`` (#1250). No
        quantization — the row's ``filed`` value rounds it to whole
        rupees.

    Raises:
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` when no rate can be sourced
            for ``acquired_on`` under the policy's rules.
        InvariantError: If ``cost_basis_money`` is negative
            (zero-cost lots are accepted; see PR #122 Codex P2), or the
            product can't be computed exactly (``Schedule FA A3 initial
            value, INR``, as
            :func:`~ibkr_fa.compute.exact_inr.checked_product` refuses).
    """
    require(
        cost_basis_money >= 0,
        what="initial_value_inr: cost_basis_money must be non-negative",
        cost_basis_money=cost_basis_money,
    )
    rate = fx_policy.rate_for(acquired_on)
    inr = checked_product(cost_basis_money, rate, at="Schedule FA A3 initial value, INR")
    # Post: result is non-negative. Free given both factors >= 0,
    # but the assertion documents the invariant for future callers.
    require(
        inr >= 0,
        what="initial_value_inr postcondition: result must be non-negative",
        cost_basis_money=cost_basis_money,
        rate=rate,
        result=inr,
    )
    return inr


def initial_value_inr_for_lot(
    lot: OpenLot | ClosedLot,
    *,
    fx_policy: FxPolicy,
) -> Decimal:
    """Compute :func:`initial_value_inr` for a parsed lot.

    Convenience wrapper that pulls ``cost_basis_money`` and
    ``open_date_time`` out of the lot dataclass. Defined as a
    separate function (rather than overloading
    :func:`initial_value_inr`) so the per-lot call site is one line
    and the primitive remains decoupled from the lot models for
    standalone testing / future non-lot uses.

    A lot with :attr:`~ibkr_fa.models.OpenLot.cost_basis_money_exact`
    then returns :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of that
    rational times the rate, at the caller's precision (#1243). It refuses
    as :func:`~ibkr_fa.compute.exact_inr.checked_product` does where the
    rate has no exact rational.

    Args:
        lot: An :class:`OpenLot` or :class:`ClosedLot`. Both expose
            ``cost_basis_money: Decimal`` and ``open_date_time: date``
            with identical semantics, so the function is generic
            over the union.
        fx_policy: See :func:`initial_value_inr`.

    Returns:
        Same as :func:`initial_value_inr`.
    """
    inr = initial_value_inr(
        lot.cost_basis_money,
        lot.open_date_time,
        fx_policy=fx_policy,
    )
    if lot.cost_basis_money_exact is None:
        return inr
    return checked_exact_product(
        lot.cost_basis_money_exact,
        fx_policy.rate_for(lot.open_date_time),
        at="Schedule FA A3 initial value, INR",
    )
