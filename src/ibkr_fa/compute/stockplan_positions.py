"""The Dec-31 StockPlan (RSU) holding for Schedule FA Table A3 (#922).

The ``Releases Report`` lists every vest and the ``Vested Share Holdings``
report one aggregate as of its Selected Date, so the Dec-31 position is rebuilt
from the vests and proven complete against that aggregate. Two steps, so every
reconciliation and year-end freshness refusal fires before the caller loads
prices:

* :func:`reconstruct_stockplan_dec31_vests` picks the vests held on Dec-31 and
  reconciles all pre-Selected-Date vests to the reported aggregate.
* :func:`stockplan_holding_lots` maps each held vest to a :class:`HoldingLot`
  whose cost basis is the exact ``Price`` x ``Net Share Proceeds`` (#1314).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import checked_product, checked_sum
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models.holding_lot import HoldingLot
from ibkr_fa.models.instrument_identity import InstrumentIdentity
from ibkr_fa.models.open_lot import Side
from ibkr_fa.parser.stockplan.capital_gain_loss import StockplanVest

# The fixed label of an exactness refusal in the vests' acquisition values.
_ACQUISITION_VALUE = "StockPlan vest acquisition value"


class StockplanFaError(IbkrFaError):
    """A StockPlan Schedule FA run cannot proceed with certainty.

    Its text names the report and field by fixed name and the rule broken; it
    never prints an amount or a date (#1043).
    """


@dataclass(frozen=True, slots=True)
class StockplanDec31Vests:
    """The vests held on Dec-31, proven complete by the Holdings anchor.

    Attributes:
        held: The vests on or before Dec-31, in (vest date, order number) order.
        held_shares: Their total shares deposited.
        reconciled_as_of: The latest vest date before the Holdings Report's
            Selected Date: the last vest the Holdings aggregate reconciles.
    """

    held: tuple[StockplanVest, ...]
    held_shares: Decimal
    reconciled_as_of: date


def reconstruct_stockplan_dec31_vests(
    vests: Sequence[StockplanVest],
    holdings_shares: Decimal,
    holdings_acq_value: Decimal,
    cy_end: date,
    *,
    selected_date: date,
    releases_run_on: date,
) -> StockplanDec31Vests:
    """Return the vests held on ``cy_end`` (no sales), proven complete by the Holdings anchor.

    The Holdings aggregate is a snapshot as of ``selected_date``, the Holdings
    Report's Selected Date (#1290). Every vest dated before it, together, must
    equal that aggregate's ``(shares, Σ FMV x shares)`` exactly. The products
    and sums keep every digit (:func:`~ibkr_fa.compute.exact_inr.checked_sum`),
    so no vest rounds away. Offsetting transfers can't be seen. No vest may be
    dated on it, since the snapshot may or may not include that vest, and the
    Releases Report must be run on or after it, so it lists every vest the
    snapshot could hold. The Selected Date must be strictly after ``cy_end``,
    so each vest on or before ``cy_end`` is in the reconciled aggregate. That
    also makes the Releases Report run strictly after ``cy_end``. Returns the
    held vests (vest_date <= cy_end) and the latest vest date the aggregate
    reconciles.

    Raises:
        StockplanFaError: No vest on/before ``cy_end``; the Releases Report was
            run before ``selected_date``; a vest is dated on it; the vests
            before it don't equal the Holdings aggregate; or it is on or before
            ``cy_end``, in that order. Each refusal is fixed text.
        InvariantError: A product or sum is nonfinite, fails Decimal arithmetic,
            or needs more than :data:`~ibkr_fa.compute.exact_inr.EXACT_SUM_DIGITS`
            digits; in-limit StockPlan cells can't trigger these refusals.
    """
    ordered = sorted(vests, key=lambda v: (v.vest_date, v.order_number))
    held_count = sum(1 for v in ordered if v.vest_date <= cy_end)
    if held_count == 0:
        raise StockplanFaError(
            "the Releases Report has no Vest Date on or before Dec-31 of the calendar year; "
            "nothing to report"
        )
    if releases_run_on < selected_date:
        raise StockplanFaError(
            "the Releases Report was run before the Holdings Report's Selected Date, so a "
            "vest the Holdings Report counts can be missing from it; export the Releases "
            "Report on or after the Selected Date, for example both reports on the same day"
        )
    if any(v.vest_date == selected_date for v in ordered):
        raise StockplanFaError(
            "a Releases Report vest is dated on the Holdings Report's Selected Date, so the "
            "Holdings Report may or may not include it; re-export the Vested Share Holdings "
            "report with a later Selected Date, and the Releases Report on or after that new "
            "Selected Date, for example both on the same day"
        )
    counted = [v for v in ordered if v.vest_date < selected_date]
    cum_shares = _vest_shares(counted)
    cum_acq = checked_sum(
        enumerate(
            checked_product(v.fmv_per_share, v.shares_deposited, at=_ACQUISITION_VALUE)
            for v in counted
        ),
        at=lambda _: _ACQUISITION_VALUE,
    )
    if cum_shares != holdings_shares or cum_acq != holdings_acq_value:
        raise StockplanFaError(
            "cumulative vests do not reconcile to the Holdings Report's Number of Shares and "
            "Acquisition Value; a vest lot may be missing, or there are unaccounted "
            "sales/transfers — refusing to file an unverified Schedule FA holding"
        )
    if selected_date <= cy_end:
        raise StockplanFaError(
            "StockPlan year-end freshness: the Holdings Report's Selected Date (the 'Quote "
            "History' sheet's Quote Date) is not strictly after Dec-31 of the calendar year, "
            "so it can't show which vests were still held then; re-export the Vested Share "
            "Holdings report with a Selected Date after year-end, and the Releases Report on "
            "or after that Selected Date, for example both on the same day"
        )
    held = tuple(ordered[:held_count])
    return StockplanDec31Vests(
        held=held,
        held_shares=_vest_shares(held),
        reconciled_as_of=counted[-1].vest_date,
    )


def _vest_shares(vests: Sequence[StockplanVest]) -> Decimal:
    """The vests' total shares deposited, exactly."""
    return checked_sum(
        enumerate(v.shares_deposited for v in vests), at=lambda _: "StockPlan vest shares"
    )


def stockplan_holding_lots(
    held: Sequence[StockplanVest], instrument: InstrumentIdentity, description: str
) -> list[HoldingLot]:
    """Map each held vest to its :class:`HoldingLot`.

    Each lot's cost basis is the vest's exact FMV x shares deposited
    (:func:`~ibkr_fa.compute.exact_inr.checked_product`, #1314), the product
    the Holdings reconciliation already checked, so no digit is lost before the
    filed whole-rupee rounding.
    """
    return [
        HoldingLot(
            currency="USD",
            symbol=instrument.symbol,
            description=description,
            isin=instrument.isin,
            issuer_country_code=instrument.issuer_country_code,
            quantity=vest.shares_deposited,
            cost_basis_money=checked_product(
                vest.fmv_per_share, vest.shares_deposited, at=_ACQUISITION_VALUE
            ),
            acquisition_date=vest.vest_date,
            side=Side.LONG,
        )
        for vest in held
    ]
