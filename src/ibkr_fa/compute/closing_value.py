"""Compute Schedule FA Table A3 ``closing_value_inr`` for one lot.

Per the CBDT Schedule-FA FAQ guidance, the *closing value* of a
foreign asset is the position value on **Dec 31 of the calendar
year** converted to INR at the SBI TT-Buying rate on that same
Dec 31 (with carry-forward for non-business days, applied by the
FX policy).

For an :class:`OpenLot` (a year-end holding, by pipeline contract),
the closing value is ``quantity * mark_price`` in USD multiplied by
the FX rate. A broker statement's Dec-31 holding total
(:attr:`OpenLot.closing_total_usd`) is the USD value instead (#1242):
dividing the total by the quantity and multiplying back could round it.
Both products are exact or refuse (#1250): each is
:func:`~ibkr_fa.compute.exact_inr.checked_product`, so no digit is lost
before the filed whole-rupee rounding. A total with an exact rational
(:attr:`OpenLot.closing_total_usd_exact`), such as a Robinhood lot's
share of a statement total split by quantity, converts that rational
instead (#1243). For a :class:`ClosedLot` (disposed during the CY), the
lot has no year-end position so the closing value is
:class:`~decimal.Decimal` ``"0"`` — there is nothing to report on Dec 31.

The "0 for disposed lots" convention follows the portal-mapping
sketch in :class:`FaA3Row`. Gate G1 (portal-semantics, deferred)
will confirm whether the e-filing portal accepts a literal zero or
a blank cell for disposed lots; the writer can render either shape
from the same upstream :class:`Decimal`.

The returned :class:`Decimal` is **full precision** —
:attr:`FaA3Row.filed <ibkr_fa.models.fa_a3_row.FaA3Row.filed>` rounds
it to the whole rupees every writer renders. See
:mod:`ibkr_fa.compute.initial_value` for the same convention on
the initial-value side.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.exact_inr import checked_exact_product, checked_product
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import ClosedLot, OpenLot

_DISPOSED_LOT_CLOSING_VALUE: Final[Decimal] = Decimal("0")


def closing_value_inr(
    position_value_usd: Decimal,
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal:
    """Convert a Dec-31 position value to INR using the FX policy.

    Pure function. The position value's currency must match
    ``fx_policy.currency`` (USD for v1).

    Args:
        position_value_usd: Year-end position value in foreign
            currency: an :class:`OpenLot`'s statement total, or its
            exact ``quantity * mark_price``.
        calendar_year: The CY whose Dec 31 is used as the FX lookup
            date (e.g., ``2025`` -> ``date(2025, 12, 31)``).
        fx_policy: An :class:`FxPolicy` whose
            :attr:`~FxPolicy.currency` matches the source currency.

    Returns:
        The exact INR value, ``position_value_usd * rate``. No
        quantization here — the row's ``filed`` value rounds it.

    Raises:
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` when no rate can be sourced
            for Dec 31 of ``calendar_year`` under the policy's
            rules. The carry-forward window in
            :class:`FaExactDatePolicy` typically covers the
            year-end public-holiday gap.
        InvariantError: If ``position_value_usd`` is negative, or the
            product can't be computed exactly (``Schedule FA A3 closing
            value, INR``, as
            :func:`~ibkr_fa.compute.exact_inr.checked_product` refuses).
    """
    require(
        position_value_usd >= 0,
        what="closing_value_inr: position_value_usd must be non-negative",
        position_value_usd=position_value_usd,
    )
    rate = fx_policy.rate_for(date(calendar_year, 12, 31))
    inr = checked_product(position_value_usd, rate, at="Schedule FA A3 closing value, INR")
    require(
        inr >= 0,
        what="closing_value_inr postcondition: result must be non-negative",
        position_value_usd=position_value_usd,
        rate=rate,
        result=inr,
    )
    return inr


def closing_position_usd(lot: OpenLot) -> Decimal:
    """The USD closing total that Table A3 files for an open lot.

    Its statement total (:attr:`~OpenLot.closing_total_usd`) when it has
    one, else the exact ``quantity * mark_price``. The A3 closing value and
    the peak's closing floor both call this, so the floor compares the
    peak with the value the row files (#1243, #1260, #1263). Callers
    handle a lot's exact-rational fields before calling this.

    Raises:
        InvariantError: When ``quantity * mark_price`` can't be computed
            exactly (``Schedule FA A3 closing value, position USD``, as
            :func:`~ibkr_fa.compute.exact_inr.checked_product` refuses).
    """
    if lot.closing_total_usd is not None:
        return lot.closing_total_usd
    return checked_product(
        lot.quantity, lot.mark_price, at="Schedule FA A3 closing value, position USD"
    )


def closing_value_inr_for_lot(
    lot: OpenLot | ClosedLot,
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal:
    """Compute closing value for a parsed lot.

    For an :class:`OpenLot` (year-end holding by pipeline
    contract), delegates to :func:`closing_value_inr` with its
    statement total (:attr:`~OpenLot.closing_total_usd`) when it has
    one, else the exact ``quantity * mark_price``, refusing as
    :func:`~ibkr_fa.compute.exact_inr.checked_product` does
    (``Schedule FA A3 closing value, position USD``). A lot with
    :attr:`~OpenLot.closing_total_usd_exact` instead returns
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of that rational times
    the rate, at the caller's precision (#1243).

    For a :class:`ClosedLot` (disposed during the CY), returns
    ``Decimal("0")`` without consulting the FX policy — there is
    no year-end position to convert.

    Returning ``0`` (rather than raising) on disposed lots makes the
    function safe to map across a heterogeneous list. The pipeline
    can then sum closing values across all CY-overlap lots without
    branching.

    Args:
        lot: An :class:`OpenLot` or :class:`ClosedLot`.
        calendar_year: See :func:`closing_value_inr`.
        fx_policy: See :func:`closing_value_inr`.

    Returns:
        Same as :func:`closing_value_inr` for open lots;
        ``Decimal("0")`` for closed lots.
    """
    if isinstance(lot, ClosedLot):
        return _DISPOSED_LOT_CLOSING_VALUE
    if lot.closing_total_usd_exact is not None:
        return checked_exact_product(
            lot.closing_total_usd_exact,
            fx_policy.rate_for(date(calendar_year, 12, 31)),
            at="Schedule FA A3 closing value, INR",
        )
    return closing_value_inr(
        closing_position_usd(lot),
        calendar_year=calendar_year,
        fx_policy=fx_policy,
    )
