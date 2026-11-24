"""Match Robinhood source lots by original-acquisition-date FIFO.

Each split of a lot's cost or a sale's cash by quantity also carries its exact
rational share (#1241) beside the Decimal one, which is rounded at the
allocation precision. A Decimal amount here keeps that rounding: Schedule FA
A3 still reads it. The exact share is set only where the Decimal rounds it,
and it reaches Schedule CG and FSI through
:class:`~ibkr_fa.models.closed_lot.ClosedLot`.

A sale split pro rata across same-day lots also gives each lot an exact
quantity part (#1263). Each lot then carries the exact quantity it gave up and
the exact quantity it keeps beside the Decimal ones, which are rounded at the
allocation precision; a later take, the sale's cash split and Schedule FA
A3's closing, credit and peak shares read the exact quantities. They are set
only where the Decimal rounds them.

The same-day unit-cost test compares exact costs and quantities too (#1326),
so a later partial sale of a same-day group that an earlier pro-rata sale
split can split it pro rata again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Final

from ibkr_fa.compute.exact_inr import exact_fraction, exact_share
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
)
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodAcquisitionRecord,
    RobinhoodInventoryBlocker,
    RobinhoodInventoryUnprovedFact,
    RobinhoodPositionAnchor,
    RobinhoodPositionReference,
    RobinhoodQuantityMovement,
    RobinhoodQuantityMovementKind,
    RobinhoodQuantityResidual,
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityBlocker,
)

_ALLOCATION_CONTEXT = Context(prec=20)
_ARITHMETIC_CONTEXT = Context(prec=60)
_TRANSFER_LEG_COUNT = 2


class RobinhoodLotSliceKind(StrEnum):
    """The source movement that consumed a lot fragment."""

    DISPOSAL = "disposal"
    TRANSFER = "transfer"


class RobinhoodLotBlockerKind(StrEnum):
    """Evidence gaps that prevent resolved lot attribution."""

    ANCHOR_DRIFT = "anchor-drift"
    INHERITED_BASIS = "inherited-basis"
    INHERITED_QUANTITY = "inherited-quantity"
    QUANTITY_SHORTFALL = "quantity-shortfall"
    SAME_DAY_AMBIGUITY = "same-day-ambiguity"


# What each cause means, ``{moved}`` naming the movements a refusal lists:
# "sales" in Schedule CG's, "sales and transfers" in Schedule FA's (#1546,
# #1550). Each refusal adds its references and route. INHERITED_BASIS has
# none: its one source, a REC receipt with no
# [[robinhood.security_receipt_resolutions]] entry, refuses in
# resolve_robinhood_security_quantities before the lot engine runs.
LOT_BLOCKER_CAUSES: Final[dict[RobinhoodLotBlockerKind, str]] = {
    RobinhoodLotBlockerKind.ANCHOR_DRIFT: (
        "A monthly statement shows a quantity of a security that differs from the one "
        "replayed from the activity report, and the difference isn't explained by a proved "
        "transfer between the accounts, so the lots its {moved} took can't be matched exactly."
    ),
    RobinhoodLotBlockerKind.INHERITED_QUANTITY: (
        "The activity report doesn't prove how many shares of a security an account held: a "
        "monthly statement shows a position that doesn't match it or can't be tied to one "
        "security, the statement's trade-date or settlement-date quantity basis can't be "
        "proved, or a sale or transfer takes more shares than the report shows held."
    ),
    RobinhoodLotBlockerKind.QUANTITY_SHORTFALL: (
        "The lots from the activity report don't cover the shares of a security it sold or "
        "moved, or don't add up to the shares it still held."
    ),
    RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY: (
        "Robinhood's activity report gives each trade and transfer a date but no time, and "
        "the tool doesn't resolve which lots these {moved} took when a sale or transfer took "
        "part of the lots acquired on one day at different costs per share, or shares moved "
        "between the two accounts on a day one of them also sold or moved shares of that "
        "security."
    ),
}
# Where the trade times a same-day cause needs are, before its route.
SAME_DAY_EVIDENCE: Final = "Robinhood's trade confirmations give each trade's execution time."


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodLotSlice:
    """One resolved source-lot allocation to a movement."""

    lot_id: RobinhoodSecurityEventReference
    account_index: int
    cusip: str
    movement_references: tuple[RobinhoodSecurityEventReference, ...]
    quantity: Decimal
    cost_usd: Decimal
    original_date: date
    entry_date: date
    kind: RobinhoodLotSliceKind
    residual_references: tuple[RobinhoodSecurityEventReference, ...] = ()
    exact_cost_usd: Fraction | None = None
    """The exact cost where ``cost_usd`` rounds it (#1241); ``None`` where ``cost_usd`` is exact."""
    exact_quantity: Fraction | None = None
    """The exact quantity where ``quantity`` rounds it (#1263), else ``None``."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOpenLotFragment:
    """One resolved source-lot fragment still held in an account."""

    lot_id: RobinhoodSecurityEventReference
    account_index: int
    cusip: str
    quantity: Decimal
    cost_usd: Decimal
    original_date: date
    entry_date: date
    exact_cost_usd: Fraction | None = None
    """The exact cost where ``cost_usd`` rounds it (#1241); ``None`` where ``cost_usd`` is exact."""
    exact_quantity: Fraction | None = None
    """The exact quantity where ``quantity`` rounds it (#1263), else ``None``."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodLotHoldingInterval:
    """One quantity-preserving account interval for an economic lot."""

    lot_id: RobinhoodSecurityEventReference
    account_index: int
    cusip: str
    from_date: date
    to_date: date | None
    quantity: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodLotBlocker:
    """One lot blocker carrying source handles but no quantities or money."""

    account_index: int | None
    cusip: str | None
    kind: RobinhoodLotBlockerKind
    statement_index: int | None = None
    event_references: tuple[RobinhoodSecurityEventReference, ...] = ()
    position_references: tuple[RobinhoodPositionReference, ...] = ()
    lot_ids: tuple[RobinhoodSecurityEventReference, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityLotResult:
    """Original-acquisition lots plus explicit evidence blockers."""

    slices: tuple[RobinhoodLotSlice, ...]
    open_fragments: tuple[RobinhoodOpenLotFragment, ...]
    holding_intervals: tuple[RobinhoodLotHoldingInterval, ...]
    blockers: tuple[RobinhoodLotBlocker, ...]
    identity_blockers: tuple[RobinhoodSecurityIdentityBlocker, ...]


@dataclass(slots=True)
class _Fragment:
    lot_id: RobinhoodSecurityEventReference
    quantity: Decimal
    cost_usd: Decimal
    original_date: date
    entry_date: date
    batch_id: int
    batch_rank: int
    ordered_batch: bool
    exact_cost_usd: Fraction | None = None
    exact_quantity: Fraction | None = None


@dataclass(frozen=True, slots=True)
class _TakenFragment:
    lot_id: RobinhoodSecurityEventReference
    quantity: Decimal
    cost_usd: Decimal
    original_date: date
    exact_cost_usd: Fraction | None
    exact_quantity: Fraction | None
    source_rank: int = 0


@dataclass(frozen=True, slots=True)
class _Consumption:
    slices: tuple[RobinhoodLotSlice, ...]
    intervals: tuple[RobinhoodLotHoldingInterval, ...]
    taken: tuple[_TakenFragment, ...]


@dataclass(frozen=True, slots=True)
class _LotResult:
    slices: tuple[RobinhoodLotSlice, ...]
    open_fragments: tuple[RobinhoodOpenLotFragment, ...]
    holding_intervals: tuple[RobinhoodLotHoldingInterval, ...]


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def _slice_key(item: RobinhoodLotSlice) -> tuple[object, ...]:
    return (
        item.cusip,
        item.account_index,
        item.kind.value,
        item.movement_references,
        item.lot_id,
        item.original_date,
        item.entry_date,
        item.quantity,
        item.cost_usd,
        item.residual_references,
    )


def _open_key(item: RobinhoodOpenLotFragment) -> tuple[object, ...]:
    return (
        item.cusip,
        item.account_index,
        item.original_date,
        item.entry_date,
        item.lot_id,
        item.quantity,
        item.cost_usd,
    )


def _interval_key(item: RobinhoodLotHoldingInterval) -> tuple[object, ...]:
    return (
        item.cusip,
        item.account_index,
        item.from_date,
        item.to_date is None,
        item.to_date or date.max,
        item.lot_id,
        item.quantity,
    )


def _same_unit_cost(fragments: list[_Fragment], movement: RobinhoodQuantityMovement) -> bool:
    """Whether every fragment has the first's unit cost, compared exactly (#1326).

    Each side is a fragment's exact cost and quantity where it carries them,
    else its Decimals, so the rounding of an earlier pro-rata sale is no
    difference in cost.
    """
    exact: list[tuple[Fraction, Fraction]] = []
    for fragment in fragments:
        cost = (
            exact_fraction(fragment.cost_usd)
            if fragment.exact_cost_usd is None
            else fragment.exact_cost_usd
        )
        quantity = (
            exact_fraction(fragment.quantity)
            if fragment.exact_quantity is None
            else fragment.exact_quantity
        )
        if cost is None or quantity is None:
            raise _share_error(movement.event_references)
        exact.append((cost, quantity))
    (first_cost, first_quantity), *rest = exact
    return all(first_cost * quantity == cost * first_quantity for cost, quantity in rest)


def _share_error(references: tuple[RobinhoodSecurityEventReference, ...]) -> InvariantError:
    """The #1241 digit-cap refusal, naming the allocated movement's first source row."""
    return invariant_error(
        what=(
            f"Robinhood lot allocation, activity row {references[0].event_ordinal}, "
            "column Amount: the share needs more digits than exact arithmetic allows"
        )
    )


def _unless_exact(value: Decimal, exact: Fraction) -> Fraction | None:
    """``exact``, or ``None`` where ``value`` already equals it."""
    return None if value.is_finite() and Fraction(value) == exact else exact


def _exact_quantity_taken(
    fragment: _Fragment,
    quantity: Decimal,
    exact_quantity: Fraction | None,
    movement: RobinhoodQuantityMovement,
) -> tuple[Fraction, Fraction] | None:
    """The exact quantity taken and the fragment's exact quantity (#1263).

    ``None`` where both are their Decimals. ``exact_quantity`` is a pro-rata
    part's exact quantity; otherwise a whole take takes the fragment's exact
    quantity and a partial take takes exactly ``quantity``.
    """
    if exact_quantity is None and fragment.exact_quantity is None:
        return None
    whole = (
        exact_fraction(fragment.quantity)
        if fragment.exact_quantity is None
        else fragment.exact_quantity
    )
    taken = exact_quantity
    if taken is None:
        taken = whole if quantity == fragment.quantity else exact_fraction(quantity)
    if whole is None or taken is None:
        raise _share_error(movement.event_references)
    return taken, whole


def _take(
    queue: list[_Fragment],
    fragment: _Fragment,
    quantity: Decimal,
    movement: RobinhoodQuantityMovement,
    kind: RobinhoodLotSliceKind,
    exact_ratio: tuple[Fraction, Fraction] | None = None,
    exact_quantity: Fraction | None = None,
) -> tuple[RobinhoodLotSlice, RobinhoodLotHoldingInterval, _TakenFragment]:
    """Take ``quantity`` of ``fragment`` for ``movement``.

    A partial take's cost is the fragment's cost times ``quantity`` over its
    quantity, rounded at the allocation precision. Its exact cost is the
    fragment's exact cost times ``exact_ratio``, by default the exact
    quantity taken over the fragment's exact quantity, and the fragment
    keeps the exact rest. ``exact_quantity`` is the exact quantity taken
    where ``quantity`` rounds it, a pro-rata part (#1263); the fragment then
    keeps the exact quantity rest too.
    """
    original_quantity = fragment.quantity
    original_cost = fragment.cost_usd
    exact_cost = fragment.exact_cost_usd
    exact_rest: Fraction | None = None
    exact_quantities = _exact_quantity_taken(fragment, quantity, exact_quantity, movement)
    if quantity == original_quantity:
        cost = original_cost
    else:
        with localcontext(_ALLOCATION_CONTEXT):
            cost = original_cost * quantity / original_quantity
    if quantity != original_quantity or exact_ratio is not None:
        source = original_cost if exact_cost is None else exact_cost
        exact_cost = exact_share(
            source, *(exact_ratio or exact_quantities or (quantity, original_quantity))
        )
        if exact_cost is None:
            raise _share_error(movement.event_references)
        exact_rest = Fraction(source) - exact_cost
        require(
            quantity != original_quantity or exact_rest == 0,
            what=(
                f"Robinhood lot allocation, activity row "
                f"{movement.event_references[0].event_ordinal}, "
                "column Amount: exact cost remainder has no remaining quantity"
            ),
        )
    exact_taken: Fraction | None = None
    exact_quantity_rest: Fraction | None = None
    if exact_quantities is not None:
        exact_taken, whole = exact_quantities
        exact_quantity_rest = whole - exact_taken
        require(
            (quantity == original_quantity) == (exact_quantity_rest == 0),
            what=(
                f"Robinhood lot allocation, activity row "
                f"{movement.event_references[0].event_ordinal}, "
                "column Quantity: exact quantity remainder must match the remaining quantity"
            ),
        )
    fragment.quantity -= quantity
    fragment.cost_usd -= cost
    if exact_rest is not None:
        assert exact_cost is not None
        exact_cost = _unless_exact(cost, exact_cost)
        fragment.exact_cost_usd = _unless_exact(fragment.cost_usd, exact_rest)
    if exact_taken is not None and exact_quantity_rest is not None:
        exact_taken = _unless_exact(quantity, exact_taken)
        fragment.exact_quantity = _unless_exact(fragment.quantity, exact_quantity_rest)
    if fragment.quantity == 0:
        queue.remove(fragment)
    if (
        fragment.quantity < 0
        or fragment.cost_usd < 0
        or (exact_quantity_rest is not None and exact_quantity_rest < 0)
    ):
        raise _fail("lot fragment consumption exceeded its source balance")
    if cost + fragment.cost_usd != original_cost:
        raise _fail("lot fragment cost allocation did not conserve source cost")

    return (
        RobinhoodLotSlice(
            lot_id=fragment.lot_id,
            account_index=movement.account_index,
            cusip=movement.cusip,
            movement_references=movement.event_references,
            quantity=quantity,
            cost_usd=cost,
            original_date=fragment.original_date,
            entry_date=fragment.entry_date,
            kind=kind,
            exact_cost_usd=exact_cost,
            exact_quantity=exact_taken,
        ),
        RobinhoodLotHoldingInterval(
            lot_id=fragment.lot_id,
            account_index=movement.account_index,
            cusip=movement.cusip,
            from_date=fragment.entry_date,
            to_date=movement.activity_date,
            quantity=quantity,
        ),
        _TakenFragment(
            lot_id=fragment.lot_id,
            quantity=quantity,
            cost_usd=cost,
            original_date=fragment.original_date,
            exact_cost_usd=exact_cost,
            exact_quantity=exact_taken,
        ),
    )


def _adjusted_exact_quantity(
    item: _TakenFragment,
    adjustment: Decimal,
    movement: RobinhoodQuantityMovement,
) -> Fraction | None:
    """The exact quantity of ``item`` plus a residual ``adjustment`` (#1263)."""
    if item.exact_quantity is None:
        return None
    exact_adjustment = exact_fraction(adjustment)
    if exact_adjustment is None:
        raise _share_error(movement.event_references)
    return _unless_exact(item.quantity + adjustment, item.exact_quantity + exact_adjustment)


def _residual_for_shortfall(
    residuals: tuple[RobinhoodQuantityResidual, ...],
    remaining: dict[int, Decimal],
    movement: RobinhoodQuantityMovement,
    shortfall: Decimal,
) -> tuple[int, RobinhoodQuantityResidual] | None:
    movement_references = frozenset(movement.event_references)
    for residual_index, residual in enumerate(residuals):
        residual_references = frozenset(residual.event_references)
        if (
            residual.account_index == movement.account_index
            and residual.cusip == movement.cusip
            and shortfall <= remaining[residual_index]
            and (
                movement_references <= residual_references
                or residual_references <= movement_references
            )
        ):
            return residual_index, residual
    return None


def _quantity_shortfall(
    movement: RobinhoodQuantityMovement,
) -> RobinhoodLotBlocker:
    return RobinhoodLotBlocker(
        account_index=movement.account_index,
        cusip=movement.cusip,
        kind=RobinhoodLotBlockerKind.QUANTITY_SHORTFALL,
        event_references=movement.event_references,
    )


def _same_day_blocker(
    movement: RobinhoodQuantityMovement,
    fragments: list[_Fragment],
) -> RobinhoodLotBlocker:
    return RobinhoodLotBlocker(
        account_index=movement.account_index,
        cusip=movement.cusip,
        kind=RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY,
        event_references=movement.event_references,
        lot_ids=tuple(sorted(fragment.lot_id for fragment in fragments)),
    )


def _consume(  # noqa: PLR0912, PLR0915
    queue: list[_Fragment],
    requested_quantity: Decimal,
    movement: RobinhoodQuantityMovement,
    kind: RobinhoodLotSliceKind,
    residuals: tuple[RobinhoodQuantityResidual, ...],
    residual_remaining: dict[int, Decimal],
    quantity_adjustments: dict[RobinhoodSecurityEventReference, Decimal],
    residual_limit: Decimal | None = None,
) -> _Consumption | RobinhoodLotBlocker:
    available = sum((fragment.quantity for fragment in queue), Decimal())
    residual: RobinhoodQuantityResidual | None = None
    adjustment = Decimal()
    quantity = requested_quantity
    if requested_quantity > available:
        adjustment = requested_quantity - available
        matched = _residual_for_shortfall(
            residuals,
            residual_remaining,
            movement,
            adjustment,
        )
        if matched is None or available == 0:
            return _quantity_shortfall(movement)
        residual_index, residual = matched
        if kind is RobinhoodLotSliceKind.TRANSFER and (
            residual_limit is None or adjustment > residual_limit
        ):
            return _quantity_shortfall(movement)
        residual_remaining[residual_index] -= adjustment
        quantity = available

    slices: list[RobinhoodLotSlice] = []
    intervals: list[RobinhoodLotHoldingInterval] = []
    taken: list[_TakenFragment] = []
    while quantity:
        if not queue:
            return _quantity_shortfall(movement)
        first_date = min(fragment.original_date for fragment in queue)
        candidates = [fragment for fragment in queue if fragment.original_date == first_date]
        ordered_batch = len({fragment.batch_id for fragment in candidates}) == 1 and all(
            fragment.ordered_batch for fragment in candidates
        )
        if ordered_batch:
            first_rank = min(fragment.batch_rank for fragment in candidates)
            group = sorted(
                (fragment for fragment in candidates if fragment.batch_rank == first_rank),
                key=lambda fragment: fragment.lot_id,
            )
        else:
            group = sorted(candidates, key=lambda fragment: fragment.lot_id)

        group_quantity = sum((fragment.quantity for fragment in group), Decimal())
        # Each allocation's exact cost ratio, None to take its quantity's
        # share, and its exact quantity where the Decimal part rounds it.
        allocations: list[
            tuple[_Fragment, Decimal, tuple[Fraction, Fraction] | None, Fraction | None]
        ]
        if quantity >= group_quantity:
            allocations = [(fragment, fragment.quantity, None, None) for fragment in group]
        elif len(group) == 1:
            allocations = [(group[0], quantity, None, None)]
        elif len({fragment.original_date for fragment in group}) == 1 and _same_unit_cost(
            group, movement
        ):
            group_ratio = Fraction()
            for fragment in group:
                share = exact_share(Decimal(1), fragment.quantity, group_quantity)
                if share is None:
                    raise _share_error(movement.event_references)
                group_ratio += share
            require(
                group_ratio == 1,
                what=(
                    f"Robinhood lot allocation, activity row "
                    f"{movement.event_references[0].event_ordinal}, "
                    "column Quantity: group quantity must equal its exact fragment sum"
                ),
            )
            # Each fragment gives up the exact quantity over the group's exact
            # quantity of its exact quantity and of its exact cost, whatever
            # its rounded quantity part (#1241, #1263). The last part takes
            # the exact rest, so the parts add back to quantity exactly.
            exact_sold = exact_fraction(quantity)
            if exact_sold is None:
                raise _share_error(movement.event_references)
            exact_fragments: list[Fraction] = []
            for fragment in group:
                exact_fragment = (
                    exact_fraction(fragment.quantity)
                    if fragment.exact_quantity is None
                    else fragment.exact_quantity
                )
                if exact_fragment is None:
                    raise _share_error(movement.event_references)
                exact_fragments.append(exact_fragment)
            exact_group = sum(exact_fragments, Fraction())
            ratio = (exact_sold, exact_group)
            allocations = []
            unallocated = quantity
            exact_unallocated = exact_sold
            for fragment, exact_fragment in zip(group[:-1], exact_fragments, strict=False):
                with localcontext(_ALLOCATION_CONTEXT):
                    part = quantity * fragment.quantity / group_quantity
                exact_part = exact_sold * exact_fragment / exact_group
                allocations.append((fragment, part, ratio, exact_part))
                unallocated -= part
                exact_unallocated -= exact_part
            allocations.append((group[-1], unallocated, ratio, exact_unallocated))
        else:
            return _same_day_blocker(movement, group)

        consumed = Decimal()
        source_rank = len({item.source_rank for item in taken})
        for fragment, fragment_quantity, exact_ratio, exact_part in allocations:
            item_slice, interval, item_taken = _take(
                queue,
                fragment,
                fragment_quantity,
                movement,
                kind,
                exact_ratio,
                exact_part,
            )
            slices.append(item_slice)
            intervals.append(interval)
            taken.append(replace(item_taken, source_rank=source_rank))
            consumed += fragment_quantity
        quantity -= consumed

    if adjustment:
        assert residual is not None
        last_slice = slices[-1]
        last_interval = intervals[-1]
        last_taken = taken[-1]
        residual_references = tuple(sorted(residual.event_references))
        slices[-1] = replace(
            last_slice,
            residual_references=residual_references,
        )
        if kind is RobinhoodLotSliceKind.DISPOSAL:
            intervals[-1] = replace(
                last_interval,
                quantity=last_interval.quantity + adjustment,
            )
            exact_quantity = _adjusted_exact_quantity(last_taken, adjustment, movement)
            taken[-1] = replace(
                last_taken,
                quantity=last_taken.quantity + adjustment,
                exact_quantity=exact_quantity,
            )
            slices[-1] = replace(
                slices[-1],
                quantity=slices[-1].quantity + adjustment,
                exact_quantity=exact_quantity,
            )
            quantity_adjustments[last_taken.lot_id] += adjustment

    return _Consumption(
        slices=tuple(slices),
        intervals=tuple(intervals),
        taken=tuple(taken),
    )


def combined_disposal(
    movements: list[RobinhoodQuantityMovement],
) -> RobinhoodQuantityMovement:
    if not movements:
        raise _fail("combined disposal requires source movements")
    first = movements[0]
    if any(
        movement.kind is not RobinhoodQuantityMovementKind.DISPOSAL
        or movement.transfer_index is not None
        or movement.signed_quantity >= 0
        or movement.account_index != first.account_index
        or movement.cusip != first.cusip
        or movement.activity_date != first.activity_date
        for movement in movements
    ):
        raise _fail("same-day disposal movements contradict their grouping")
    return RobinhoodQuantityMovement(
        account_index=first.account_index,
        cusip=first.cusip,
        activity_date=first.activity_date,
        settle_date=max(movement.settle_date for movement in movements),
        signed_quantity=sum(
            (movement.signed_quantity for movement in movements),
            Decimal(),
        ),
        kind=RobinhoodQuantityMovementKind.DISPOSAL,
        event_references=tuple(
            sorted(reference for movement in movements for reference in movement.event_references)
        ),
    )


def allocate_disposal_proceeds(
    slices: tuple[RobinhoodLotSlice, ...],
    *,
    source_quantity: Decimal,
    source_cash: Decimal,
) -> tuple[tuple[RobinhoodLotSlice, Decimal, Fraction | None], ...]:
    """Split one sale's cash across its disposal slices by quantity.

    Each slice gets ``source_cash`` times its quantity over
    ``source_quantity`` at the caller's precision, and the last the rest. Its
    exact share (#1241) comes third, ``None`` where the Decimal is exact; it
    is taken by the slice's exact quantity (#1263). A sale with one slice
    keeps its cash whole, so it has no share to take.
    """
    if (
        not slices
        or source_quantity <= 0
        or source_cash <= 0
        or any(item.kind is not RobinhoodLotSliceKind.DISPOSAL for item in slices)
        or sum((item.quantity for item in slices), Decimal()) != source_quantity
    ):
        raise _fail("disposal proceeds inputs do not conserve source quantity and cash")
    ordered = tuple(
        sorted(
            slices,
            key=lambda item: (
                item.lot_id,
                item.original_date,
                item.entry_date,
                item.quantity,
                item.cost_usd,
            ),
        )
    )
    allocated = Decimal()
    exact_allocated = Fraction()
    result = []
    for index, item in enumerate(ordered):
        proceeds = (
            source_cash - allocated
            if index == len(ordered) - 1
            else source_cash * item.quantity / source_quantity
        )
        exact: Fraction | None = None
        if len(ordered) > 1:
            # The sidecar must not bypass the Decimal operand's existing digit cap.
            if item.exact_quantity is not None and exact_fraction(item.quantity) is None:
                raise _share_error(item.movement_references)
            exact = exact_share(
                source_cash,
                item.quantity if item.exact_quantity is None else item.exact_quantity,
                source_quantity,
            )
            if exact is None:
                raise _share_error(item.movement_references)
            exact_allocated += exact
            exact = _unless_exact(proceeds, exact)
        result.append((item, proceeds, exact))
        allocated += proceeds
    if allocated != source_cash:
        raise _fail("sale proceeds were not conserved across disposal slices")
    if len(ordered) > 1:
        require(
            exact_allocated == Fraction(source_cash),
            what=(
                f"Robinhood lot allocation, activity row "
                f"{ordered[0].movement_references[0].event_ordinal}, "
                "column Amount: exact proceeds shares must conserve source cash"
            ),
        )
    return tuple(result)


def _movement_map(
    movements: tuple[RobinhoodQuantityMovement, ...],
) -> dict[RobinhoodQuantityMovementKind, list[RobinhoodQuantityMovement]]:
    result: dict[RobinhoodQuantityMovementKind, list[RobinhoodQuantityMovement]] = defaultdict(list)
    for movement in movements:
        result[movement.kind].append(movement)
    return result


def _run_original_date_fifo(  # noqa: PLR0912, PLR0915
    quantity_result: RobinhoodSecurityQuantityResult,
    cusip: str,
    acquisitions: dict[RobinhoodSecurityEventReference, RobinhoodAcquisitionRecord],
) -> _LotResult | RobinhoodLotBlocker:
    movements = tuple(movement for movement in quantity_result.movements if movement.cusip == cusip)
    by_date: dict[date, list[RobinhoodQuantityMovement]] = defaultdict(list)
    for movement in movements:
        by_date[movement.activity_date].append(movement)

    queues: dict[int, list[_Fragment]] = defaultdict(list)
    slices: list[RobinhoodLotSlice] = []
    intervals: list[RobinhoodLotHoldingInterval] = []
    residuals = tuple(residual for residual in quantity_result.residuals if residual.cusip == cusip)
    residual_remaining = {index: residual.quantity for index, residual in enumerate(residuals)}
    quantity_adjustments: dict[RobinhoodSecurityEventReference, Decimal] = defaultdict(Decimal)
    next_batch_id = 0

    for activity_date in sorted(by_date):
        daily = _movement_map(tuple(by_date[activity_date]))
        local_batch_ids: dict[int, int] = {}
        for movement in daily[RobinhoodQuantityMovementKind.ACQUISITION]:
            acquisition = acquisitions[movement.event_references[0]]
            if acquisition.cost_usd is None:
                raise _fail("resolved lot acquisition lacks source basis")
            batch_id = local_batch_ids.setdefault(acquisition.account_index, next_batch_id)
            if batch_id == next_batch_id:
                next_batch_id += 1
            queues[acquisition.account_index].append(
                _Fragment(
                    lot_id=acquisition.event_reference,
                    quantity=acquisition.quantity,
                    cost_usd=acquisition.cost_usd,
                    original_date=acquisition.activity_date,
                    entry_date=acquisition.activity_date,
                    batch_id=batch_id,
                    batch_rank=0,
                    ordered_batch=False,
                )
            )

        transfer_outs = daily[RobinhoodQuantityMovementKind.TRANSFER_OUT]
        disposals_by_account: dict[int, list[RobinhoodQuantityMovement]] = defaultdict(list)
        for movement in daily[RobinhoodQuantityMovementKind.DISPOSAL]:
            disposals_by_account[movement.account_index].append(movement)
        transfer_sources = [movement.account_index for movement in transfer_outs]
        transfer_destinations = {
            quantity_result.transfers[movement.transfer_index].destination_account_index
            for movement in transfer_outs
            if movement.transfer_index is not None
        }
        if (
            len(transfer_sources) != len(set(transfer_sources))
            or set(transfer_sources) & transfer_destinations
            or set(disposals_by_account) & (set(transfer_sources) | transfer_destinations)
        ):
            ambiguous = sorted(
                (
                    *transfer_outs,
                    *daily[RobinhoodQuantityMovementKind.TRANSFER_IN],
                    *daily[RobinhoodQuantityMovementKind.DISPOSAL],
                ),
                key=lambda movement: (
                    movement.account_index,
                    movement.event_references,
                ),
            )
            first = ambiguous[0]
            return RobinhoodLotBlocker(
                account_index=first.account_index,
                cusip=cusip,
                kind=RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY,
                event_references=tuple(
                    sorted(
                        reference
                        for movement in ambiguous
                        for reference in movement.event_references
                    )
                ),
            )

        inbound: list[tuple[int, int, _TakenFragment]] = []
        for movement in sorted(
            transfer_outs,
            key=lambda item: (
                item.account_index,
                item.transfer_index if item.transfer_index is not None else -1,
            ),
        ):
            if movement.transfer_index is None:
                raise _fail("transfer movement lacks transfer evidence index")
            transfer = quantity_result.transfers[movement.transfer_index]
            consumed = _consume(
                queues[movement.account_index],
                -movement.signed_quantity,
                movement,
                RobinhoodLotSliceKind.TRANSFER,
                residuals,
                residual_remaining,
                quantity_adjustments,
                residual_limit=transfer.tolerance,
            )
            if isinstance(consumed, RobinhoodLotBlocker):
                return consumed
            slices.extend(consumed.slices)
            intervals.extend(consumed.intervals)
            inbound.extend(
                (transfer.destination_account_index, movement.transfer_index, item)
                for item in consumed.taken
            )

        transfer_batches: dict[tuple[int, int], int] = {}
        for destination_account_index, transfer_index, taken in inbound:
            batch_key = (destination_account_index, transfer_index)
            batch_id = transfer_batches.setdefault(batch_key, next_batch_id)
            if batch_id == next_batch_id:
                next_batch_id += 1
            queues[destination_account_index].append(
                _Fragment(
                    lot_id=taken.lot_id,
                    quantity=taken.quantity,
                    cost_usd=taken.cost_usd,
                    original_date=taken.original_date,
                    entry_date=activity_date,
                    batch_id=batch_id,
                    batch_rank=taken.source_rank,
                    ordered_batch=True,
                    exact_cost_usd=taken.exact_cost_usd,
                    exact_quantity=taken.exact_quantity,
                )
            )

        for account_index, account_disposals in sorted(disposals_by_account.items()):
            movement = combined_disposal(account_disposals)
            consumed = _consume(
                queues[account_index],
                -movement.signed_quantity,
                movement,
                RobinhoodLotSliceKind.DISPOSAL,
                residuals,
                residual_remaining,
                quantity_adjustments,
            )
            if isinstance(consumed, RobinhoodLotBlocker):
                return consumed
            slices.extend(consumed.slices)
            intervals.extend(consumed.intervals)

    open_fragments: list[RobinhoodOpenLotFragment] = []
    for account_index, queue in queues.items():
        for fragment in queue:
            open_fragments.append(
                RobinhoodOpenLotFragment(
                    lot_id=fragment.lot_id,
                    account_index=account_index,
                    cusip=cusip,
                    quantity=fragment.quantity,
                    cost_usd=fragment.cost_usd,
                    original_date=fragment.original_date,
                    entry_date=fragment.entry_date,
                    exact_cost_usd=fragment.exact_cost_usd,
                    exact_quantity=fragment.exact_quantity,
                )
            )
            intervals.append(
                RobinhoodLotHoldingInterval(
                    lot_id=fragment.lot_id,
                    account_index=account_index,
                    cusip=cusip,
                    from_date=fragment.entry_date,
                    to_date=None,
                    quantity=fragment.quantity,
                )
            )

    terminal_costs: dict[RobinhoodSecurityEventReference, Decimal] = defaultdict(Decimal)
    terminal_quantities: dict[RobinhoodSecurityEventReference, Decimal] = defaultdict(Decimal)
    terminal_pieces: dict[RobinhoodSecurityEventReference, list[tuple[Decimal, Fraction | None]]]
    terminal_pieces = defaultdict(list)
    for item in (
        *(item for item in slices if item.kind is RobinhoodLotSliceKind.DISPOSAL),
        *open_fragments,
    ):
        terminal_costs[item.lot_id] += item.cost_usd
        terminal_quantities[item.lot_id] += item.quantity
        terminal_pieces[item.lot_id].append((item.quantity, item.exact_quantity))
    for lot_id, acquisition in acquisitions.items():
        if acquisition.cusip != cusip:
            continue
        if (
            terminal_costs[lot_id] != acquisition.cost_usd
            or terminal_quantities[lot_id] != acquisition.quantity + quantity_adjustments[lot_id]
            or not _exact_quantities_conserved(
                terminal_pieces[lot_id], acquisition.quantity + quantity_adjustments[lot_id]
            )
        ):
            raise _fail("resolved lot inventory did not conserve source acquisition")

    expected_quantities = {
        quantity.account_index: quantity.quantity
        for quantity in quantity_result.quantities
        if quantity.cusip == cusip
    }
    account_indices = {movement.account_index for movement in movements} | set(expected_quantities)
    for account_index in sorted(account_indices):
        actual = sum(
            (
                fragment.quantity
                for fragment in open_fragments
                if fragment.account_index == account_index
            ),
            Decimal(),
        )
        if actual != expected_quantities.get(account_index, Decimal()):
            return RobinhoodLotBlocker(
                account_index=account_index,
                cusip=cusip,
                kind=RobinhoodLotBlockerKind.QUANTITY_SHORTFALL,
                event_references=tuple(
                    sorted(
                        reference
                        for movement in movements
                        if movement.account_index == account_index
                        for reference in movement.event_references
                    )
                ),
            )

    return _LotResult(
        slices=tuple(sorted(slices, key=_slice_key)),
        open_fragments=tuple(sorted(open_fragments, key=_open_key)),
        holding_intervals=tuple(sorted(intervals, key=_interval_key)),
    )


def _exact_quantities_conserved(
    pieces: list[tuple[Decimal, Fraction | None]], total: Decimal
) -> bool:
    """Whether a lot's exact quantities add up to ``total`` (#1263).

    Only a lot with an exact quantity is checked; the others' Decimal
    quantities are their exact quantities.
    """
    if all(exact is None for _quantity, exact in pieces):
        return True
    exact_sum = Fraction()
    for quantity, exact in pieces:
        piece = exact_fraction(quantity) if exact is None else exact
        if piece is None:
            return False
        exact_sum += piece
    return exact_sum == exact_fraction(total)


def _validate_inputs(  # noqa: PLR0912
    result: RobinhoodSecurityQuantityResult,
) -> dict[RobinhoodSecurityEventReference, RobinhoodAcquisitionRecord]:
    acquisitions = {item.event_reference: item for item in result.acquisitions}
    if len(acquisitions) != len(result.acquisitions):
        raise _fail("acquisition source handles are duplicated")

    acquisition_movements = tuple(
        movement
        for movement in result.movements
        if movement.kind is RobinhoodQuantityMovementKind.ACQUISITION
    )
    if any(len(movement.event_references) != 1 for movement in acquisition_movements):
        raise _fail("acquisition movement has invalid source shape")
    movement_acquisition_references = {
        reference for movement in acquisition_movements for reference in movement.event_references
    }
    if movement_acquisition_references != set(acquisitions):
        raise _fail("acquisition records and movements disagree")
    acquisition_quantities: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    movement_quantities: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for acquisition in result.acquisitions:
        acquisition_quantities[(acquisition.account_index, acquisition.cusip)] += (
            acquisition.quantity
        )
    for movement in acquisition_movements:
        movement_quantities[(movement.account_index, movement.cusip)] += movement.signed_quantity
        acquisition = acquisitions[movement.event_references[0]]
        if (
            movement.account_index != acquisition.account_index
            or movement.cusip != acquisition.cusip
            or movement.activity_date != acquisition.activity_date
            or movement.signed_quantity != acquisition.quantity
        ):
            raise _fail("acquisition record and source movement disagree")
    if acquisition_quantities != movement_quantities:
        raise _fail("acquisition records and movement quantities disagree")

    movement_references = [
        reference for movement in result.movements for reference in movement.event_references
    ]
    if len(movement_references) != len(set(movement_references)):
        raise _fail("security movement handles are duplicated")

    transfer_movements: dict[int, list[RobinhoodQuantityMovement]] = defaultdict(list)
    for movement in result.movements:
        if not movement.event_references or movement.signed_quantity == 0:
            raise _fail("security movement has an invalid source shape")
        if movement.kind is RobinhoodQuantityMovementKind.ACQUISITION:
            if len(movement.event_references) != 1:
                raise _fail("acquisition movement has invalid source shape")
        elif movement.signed_quantity >= 0:
            if movement.kind is not RobinhoodQuantityMovementKind.TRANSFER_IN:
                raise _fail("outgoing security movement has invalid quantity sign")
        elif movement.kind is RobinhoodQuantityMovementKind.TRANSFER_IN:
            raise _fail("incoming security transfer has invalid quantity sign")
        if movement.kind in {
            RobinhoodQuantityMovementKind.TRANSFER_IN,
            RobinhoodQuantityMovementKind.TRANSFER_OUT,
        }:
            if movement.transfer_index is None:
                raise _fail("transfer movement lacks transfer evidence index")
            if not 0 <= movement.transfer_index < len(result.transfers):
                raise _fail("transfer movement evidence index is invalid")
            transfer_movements[movement.transfer_index].append(movement)

    quantity_keys = [(quantity.account_index, quantity.cusip) for quantity in result.quantities]
    if len(quantity_keys) != len(set(quantity_keys)):
        raise _fail("final security quantities are duplicated")

    if set(transfer_movements) != set(range(len(result.transfers))):
        raise _fail("transfer evidence is not consumed exactly once")
    for transfer_index, transfer in enumerate(result.transfers):
        movements = transfer_movements[transfer_index]
        if len(movements) != _TRANSFER_LEG_COUNT:
            raise _fail("transfer evidence does not have exactly two movements")
        outgoing = next(
            (
                movement
                for movement in movements
                if movement.kind is RobinhoodQuantityMovementKind.TRANSFER_OUT
            ),
            None,
        )
        incoming = next(
            (
                movement
                for movement in movements
                if movement.kind is RobinhoodQuantityMovementKind.TRANSFER_IN
            ),
            None,
        )
        if (
            outgoing is None
            or incoming is None
            or outgoing.account_index != transfer.source_account_index
            or incoming.account_index != transfer.destination_account_index
            or outgoing.cusip != transfer.cusip
            or incoming.cusip != transfer.cusip
            or outgoing.activity_date != transfer.activity_date
            or incoming.activity_date != transfer.activity_date
            or -outgoing.signed_quantity != transfer.effective_quantity
            or incoming.signed_quantity != transfer.effective_quantity
            or outgoing.event_references != transfer.source_event_references
            or incoming.event_references != transfer.destination_event_references
        ):
            raise _fail("transfer evidence and movement pair disagree")
    return acquisitions


def _inherited_blocker(
    blocker: RobinhoodInventoryBlocker,
) -> RobinhoodLotBlocker:
    kind = (
        RobinhoodLotBlockerKind.INHERITED_BASIS
        if blocker.unproved_fact is RobinhoodInventoryUnprovedFact.BASIS
        else RobinhoodLotBlockerKind.INHERITED_QUANTITY
    )
    return RobinhoodLotBlocker(
        account_index=blocker.account_index,
        cusip=blocker.cusip,
        kind=kind,
        statement_index=blocker.statement_index,
        event_references=blocker.event_references,
        position_references=blocker.position_references,
    )


def _anchor_drift_explained(
    anchor: RobinhoodPositionAnchor,
    result: RobinhoodSecurityQuantityResult,
) -> bool:
    drift = anchor.ledger_quantity - anchor.statement_quantity
    if drift <= 0:
        return False
    return any(
        residual.cusip == anchor.cusip
        and residual.quantity == drift
        and residual.account_index == transfer.source_account_index
        and anchor.account_index == transfer.destination_account_index
        and residual.event_references == transfer.source_event_references
        and transfer.cusip == anchor.cusip
        and transfer.activity_date <= anchor.period_end
        and residual.quantity <= transfer.tolerance
        for residual in result.residuals
        for transfer in result.transfers
    )


def _reconcile(
    quantity_result: RobinhoodSecurityQuantityResult,
) -> RobinhoodSecurityLotResult:
    acquisitions = _validate_inputs(quantity_result)
    blockers = [_inherited_blocker(blocker) for blocker in quantity_result.blockers]
    blocked_cusips = {
        blocker.cusip
        for blocker in quantity_result.blockers
        if blocker.cusip is not None
        and blocker.unproved_fact
        in {
            RobinhoodInventoryUnprovedFact.BASIS,
            RobinhoodInventoryUnprovedFact.QUANTITY,
        }
    }
    for anchor in quantity_result.anchors:
        if anchor.ledger_quantity != anchor.statement_quantity and not _anchor_drift_explained(
            anchor, quantity_result
        ):
            blocked_cusips.add(anchor.cusip)
            blockers.append(
                RobinhoodLotBlocker(
                    account_index=anchor.account_index,
                    cusip=anchor.cusip,
                    kind=RobinhoodLotBlockerKind.ANCHOR_DRIFT,
                    statement_index=anchor.statement_index,
                    position_references=anchor.position_references,
                )
            )

    cusips = sorted(
        {movement.cusip for movement in quantity_result.movements}
        | {acquisition.cusip for acquisition in quantity_result.acquisitions}
    )
    slices: list[RobinhoodLotSlice] = []
    open_fragments: list[RobinhoodOpenLotFragment] = []
    intervals: list[RobinhoodLotHoldingInterval] = []
    for cusip in cusips:
        if cusip in blocked_cusips:
            continue
        if any(
            acquisition.cusip == cusip and acquisition.cost_usd is None
            for acquisition in quantity_result.acquisitions
        ):
            raise _fail("resolved security contains an acquisition without basis")
        resolved = _run_original_date_fifo(
            quantity_result,
            cusip,
            acquisitions,
        )
        if isinstance(resolved, RobinhoodLotBlocker):
            blockers.append(resolved)
            continue
        slices.extend(resolved.slices)
        open_fragments.extend(resolved.open_fragments)
        intervals.extend(resolved.holding_intervals)

    unique_blockers = tuple(dict.fromkeys(blockers))
    output_cusips = {item.cusip for item in (*slices, *open_fragments, *intervals)}
    blocked_output_cusips = {
        blocker.cusip for blocker in unique_blockers if blocker.cusip is not None
    }
    if output_cusips & blocked_output_cusips:
        raise _fail("blocked security reached resolved lot output")
    return RobinhoodSecurityLotResult(
        slices=tuple(sorted(slices, key=_slice_key)),
        open_fragments=tuple(sorted(open_fragments, key=_open_key)),
        holding_intervals=tuple(sorted(intervals, key=_interval_key)),
        blockers=unique_blockers,
        identity_blockers=quantity_result.identity_blockers,
    )


def reconcile_robinhood_security_lots(
    quantity_result: RobinhoodSecurityQuantityResult,
) -> RobinhoodSecurityLotResult:
    """Resolve same-owner transfers with original acquisition date and basis."""

    with localcontext(_ARITHMETIC_CONTEXT):
        return _reconcile(quantity_result)
