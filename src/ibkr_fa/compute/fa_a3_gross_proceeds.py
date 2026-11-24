"""Compute Schedule FA Table A3 ``gross_proceeds_from_sale_inr`` for one lot.

Implements the FA-A3 column "Total gross proceeds from sale or
redemption of investment during the period" — issue #16.

For closed lots, the gross proceeds are the foreign-currency sale
proceeds converted to INR at the SBI TT-Buying rate on December 31
of the reporting calendar year. This keeps every Schedule FA gross
credit on one calendar-year closing rate; Schedule CG continues to
apply its separate Rule 115 capital-gains policy.

For open lots (still held at year-end), there's been no disposal, so
the gross proceeds are :class:`~decimal.Decimal` 0 by definition.
Returning 0 (not None) lets every FA-A3 row carry a concrete
non-negative value — the portal's ``whole min 0`` validation rejects
blank cells, so leaving open-lot rows blank would force the writer
to emit a non-zero exit and force the user into manual-fill mode for
otherwise-clean output (issue #137 Tier-1 P1.1 / P1.2 pattern: same
rationale as the dividend-attribution fix).

To file the *gross* sale consideration the FA-A3 column actually asks
for, a source that proves it records
:attr:`ClosedLot.gross_proceeds_money` = ``quantity * execution price``
(the raw pre-fee execution value, #348; that attribute lists each
source's basis). Robinhood records its sale ``Amount`` there, which it
files as the full value of consideration (owner decision 2026-11-14,
#1383). When present it is used directly and is non-negative by
construction; its exact rational, where the Decimal rounds it, is
converted instead. Sources that cannot prove the raw pre-fee
consideration leave it ``None`` for manual completion; their net cash
is never presented as gross proceeds.

Computed values preserve full :class:`Decimal` precision.
:attr:`FaA3Row.filed <ibkr_fa.models.fa_a3_row.FaA3Row.filed>` rounds
them half-up to the whole rupees every writer renders (CBDT FAQ HALF_UP).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import checked_exact_product, checked_product
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models import ClosedLot, OpenLot


def gross_proceeds_inr(
    proceeds_money: Decimal,
    calendar_year: int,
    *,
    fx_policy: FxPolicy,
) -> Decimal:
    """Convert sale proceeds in foreign currency to INR using the FX policy.

    Pure function: zero side effects beyond delegating to
    :meth:`FxPolicy.rate_for`. Sign is NOT constrained at this layer
    — :class:`ClosedLot` legitimately allows negative
    ``proceeds_money`` for deep-loss / commission-heavy disposals.

    Args:
        proceeds_money: The foreign-currency sale proceeds (e.g.,
            USD for v1). Must be a :class:`~decimal.Decimal` to
            preserve IBKR's published precision through the
            multiplication. May be negative (deep-loss case);
            this primitive does NOT clamp.
        calendar_year: Schedule FA reporting calendar year. Gross
            proceeds use its December 31 TTBR.
        fx_policy: An :class:`FxPolicy` whose
            :attr:`~FxPolicy.currency` matches the source currency
            of ``proceeds_money``. The pipeline picks the policy;
            this function trusts the choice.

    Returns:
        The exact INR value, ``proceeds_money * rate`` (#1250). Sign
        matches the sign of ``proceeds_money`` (the rate is always
        positive). No quantization — the row's ``filed`` value rounds
        it to whole rupees.

    Raises:
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` when no rate can be sourced
            for December 31 of ``calendar_year`` under the policy's
            rules, even when ``proceeds_money`` is 0 (#735).
        InvariantError: The product can't be computed exactly
            (``Schedule FA A3 gross proceeds, INR``, as
            :func:`~ibkr_fa.compute.exact_inr.checked_product` refuses).
    """
    # ponytail: one year-end rate for every FA gross credit; split only
    # if ITD publishes a proceeds-specific conversion date.
    rate = fx_policy.rate_for(date(calendar_year, 12, 31))
    return checked_product(proceeds_money, rate, at="Schedule FA A3 gross proceeds, INR")


def gross_proceeds_inr_for_lot(
    lot: OpenLot | ClosedLot,
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> Decimal | None:
    """Compute the gross-proceeds-INR for a parsed lot, FA-A3 ready.

    Closed lots with a source-proven ``gross_proceeds_money`` convert
    that raw pre-fee consideration at the reporting calendar year's
    December 31 TTBR. Robinhood files its recorded Amount as consideration
    (#1383). Where the Decimal rounds ``gross_proceeds_money_exact``, that
    rational is converted instead, as A3's initial value converts an exact
    cost (#1243). Closed lots without gross proceeds return ``None`` for
    manual fill. Open lots return :class:`Decimal` 0 because there was no disposal.

    Args:
        lot: An :class:`OpenLot` or :class:`ClosedLot`. Open lots
            short-circuit to 0; closed lots delegate.
        calendar_year: Schedule FA reporting calendar year.
        fx_policy: See :func:`gross_proceeds_inr`. Only consulted
            for the closed-lot path.

    Returns:
        Full-precision non-negative :class:`~decimal.Decimal` for an
        open lot or a closed lot with proven gross consideration;
        ``None`` for a closed lot whose gross consideration is absent.

    Raises:
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` when December 31 lacks a rate
            in the policy's window, even for proven gross proceeds of
            0 (#735). Never raised for open lots or a closed lot
            without proven gross proceeds.
    """
    if isinstance(lot, OpenLot):
        return Decimal("0")
    if lot.gross_proceeds_money is None:
        return None
    gross = gross_proceeds_inr(
        lot.gross_proceeds_money,
        calendar_year,
        fx_policy=fx_policy,
    )
    if lot.gross_proceeds_money_exact is not None:
        gross = checked_exact_product(
            lot.gross_proceeds_money_exact,
            fx_policy.rate_for(date(calendar_year, 12, 31)),
            at="Schedule FA A3 gross proceeds, INR",
        )
    require(
        gross >= 0,
        what="gross_proceeds_inr_for_lot: gross_proceeds_money path must be non-negative",
        gross_proceeds_money=lot.gross_proceeds_money,
        computed_inr=gross,
    )
    return gross
