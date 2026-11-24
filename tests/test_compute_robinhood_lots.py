"""Original-date FIFO Robinhood source-lot matching."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ibkr_fa.compute import robinhood_lots
from ibkr_fa.compute.exact_inr import exact_whole_rupees
from ibkr_fa.compute.robinhood_lots import (
    RobinhoodLotBlockerKind,
    RobinhoodLotSlice,
    RobinhoodLotSliceKind,
    RobinhoodOpenLotFragment,
    RobinhoodSecurityLotResult,
    _Fragment,
    _take,
    _TakenFragment,
    allocate_disposal_proceeds,
    reconcile_robinhood_security_lots,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountSecurityQuantity,
    RobinhoodAcquisitionRecord,
    RobinhoodInventoryBlocker,
    RobinhoodInventoryBlockerKind,
    RobinhoodInventoryUnprovedFact,
    RobinhoodPositionAnchor,
    RobinhoodPositionSnapshotBasis,
    RobinhoodQuantityMovement,
    RobinhoodQuantityMovementKind,
    RobinhoodQuantityResidual,
    RobinhoodReconciliationError,
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityBlocker,
    RobinhoodSecurityIdentityBlockerKind,
    RobinhoodSecurityQuantityResult,
    reconcile_robinhood_security_quantities,
    resolve_robinhood_security_identities,
)
from ibkr_fa.normalize.robinhood.inventory import (
    _derive_transfer_quantity_corrections,
    _transfer_corrections_hold,
)
from tests._exact_refusals import TOO_MANY_DIGITS
from tests._private_checks import private_test
from tests._robinhood_lots import LOT_CUSIP as _CUSIP
from tests._robinhood_lots import lot_acquisition as _acquisition
from tests._robinhood_lots import lot_event_reference as _reference
from tests._robinhood_lots import lot_movement as _movement
from tests._robinhood_lots import lot_quantity_result as _result
from tests._robinhood_lots import lot_transfer as _transfer
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)

_START = date(2025, 1, 1)


def _exact_cost(item: RobinhoodLotSlice | RobinhoodOpenLotFragment) -> Fraction:
    """The exact cost of a slice or open fragment: its rational, else its Decimal (#1241)."""
    return Fraction(item.cost_usd) if item.exact_cost_usd is None else item.exact_cost_usd


def _exact_quantity(item: RobinhoodLotSlice | RobinhoodOpenLotFragment) -> Fraction:
    """The exact quantity of a slice or open fragment: its rational, else its Decimal (#1263)."""
    return Fraction(item.quantity) if item.exact_quantity is None else item.exact_quantity


def _underdisplayed_transfer_result() -> RobinhoodSecurityQuantityResult:
    transfer_date = _START + timedelta(days=10)
    acquisitions = (
        # Synthetic: 0.018204 + 0.400433 = 0.418637 moves out but displays as 0.4186.
        _acquisition(0, 0, _START, Decimal("0.018204"), Decimal("12")),
        _acquisition(0, 1, _START, Decimal("0.400433"), Decimal("180")),
        _acquisition(0, 2, transfer_date, Decimal("0.371254"), Decimal("160")),
    )
    transfer_movements, transfer = _transfer(
        0,
        transfer_date,
        Decimal("0.4186"),
        tolerance=Decimal("0.00005"),
    )
    sale = _movement(
        1,
        3,
        transfer_date + timedelta(days=1),
        Decimal("-0.418637"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )
    residual = RobinhoodQuantityResidual(
        account_index=1,
        cusip=_CUSIP,
        activity_date=sale.activity_date,
        quantity=Decimal("0.000037"),
        event_references=sale.event_references,
    )
    anchors = (
        RobinhoodPositionAnchor(
            account_index=0,
            cusip=_CUSIP,
            statement_index=0,
            period_end=transfer_date - timedelta(days=1),
            ledger_quantity=Decimal("0.418637"),
            statement_quantity=Decimal("0.418637"),
            trade_date_quantity=Decimal("0.418637"),
            settlement_date_quantity=Decimal("0.418637"),
            snapshot_basis=RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
            tolerance=Decimal("0.0000005"),
            position_references=(),
        ),
        RobinhoodPositionAnchor(
            account_index=0,
            cusip=_CUSIP,
            statement_index=1,
            period_end=transfer_date + timedelta(days=2),
            # 0.418637 - 0.4186 + 0.371254 = 0.371291 (drift 0.000037).
            ledger_quantity=Decimal("0.371291"),
            statement_quantity=Decimal("0.371254"),
            trade_date_quantity=Decimal("0.371254"),
            settlement_date_quantity=Decimal("0.371254"),
            snapshot_basis=RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
            tolerance=Decimal("0.00005"),
            position_references=(),
        ),
    )
    return _result(
        acquisitions,
        (*transfer_movements, sale),
        {0: Decimal("0.371254"), 1: Decimal()},
        transfers=(transfer,),
        residuals=(residual,),
        anchors=anchors,
    )


def test_two_account_ledgers_prove_underdisplayed_transfer_quantity() -> None:
    (correction,) = _derive_transfer_quantity_corrections(_underdisplayed_transfer_result())

    assert correction.transfer_index == 0
    assert correction.quantity == Decimal("0.418637")


def test_underdisplayed_transfer_requires_exclusive_statement_precision() -> None:
    result = _underdisplayed_transfer_result()
    transfer = result.transfers[0]
    drift_anchor = result.anchors[1]
    # Same 0.000037 drift over a 4-decimal statement (half-ulp 0.00005).
    ambiguous_anchor = replace(
        drift_anchor,
        ledger_quantity=Decimal("0.371337"),
        statement_quantity=Decimal("0.3713"),
        trade_date_quantity=Decimal("0.3713"),
        settlement_date_quantity=Decimal("0.3713"),
    )

    assert not _derive_transfer_quantity_corrections(
        replace(result, anchors=(result.anchors[0], ambiguous_anchor))
    )
    assert not _derive_transfer_quantity_corrections(replace(result, residuals=()))
    assert not _derive_transfer_quantity_corrections(
        replace(
            result,
            transfers=(replace(transfer, tolerance=Decimal("0.00001")),),
        )
    )
    assert not _derive_transfer_quantity_corrections(
        replace(
            result,
            transfers=(
                replace(
                    transfer,
                    source_event_references=(
                        *transfer.source_event_references,
                        _reference(0, 999),
                    ),
                ),
            ),
        )
    )
    prior_destination = _movement(
        1,
        998,
        transfer.activity_date - timedelta(days=1),
        Decimal("1"),
        RobinhoodQuantityMovementKind.ACQUISITION,
    )
    assert not _derive_transfer_quantity_corrections(
        replace(result, movements=(*result.movements, prior_destination))
    )
    intervening_anchor = replace(
        result.anchors[0],
        statement_index=2,
        period_end=transfer.activity_date,
    )
    assert not _derive_transfer_quantity_corrections(
        replace(result, anchors=(result.anchors[0], intervening_anchor, drift_anchor))
    )
    coarse_movement = _movement(
        0,
        997,
        transfer.activity_date + timedelta(days=1),
        Decimal("0.1000"),
        RobinhoodQuantityMovementKind.ACQUISITION,
    )
    fine_movement = replace(coarse_movement, signed_quantity=Decimal("0.100000"))
    assert not _derive_transfer_quantity_corrections(
        replace(result, movements=(*result.movements, coarse_movement))
    )
    assert _derive_transfer_quantity_corrections(
        replace(result, movements=(*result.movements, fine_movement))
    )


@pytest.mark.parametrize(
    ("destination_display", "derived"),
    [
        pytest.param(Decimal("0.418637"), True, id="same-scale"),
        pytest.param(Decimal("0.4186370"), True, id="padded"),
        pytest.param(Decimal("0.418638"), False, id="other-number"),
        pytest.param(Decimal("0.4186380"), False, id="other-padded-number"),
    ],
)
def test_underdisplayed_transfer_correction_reads_the_destination_display_numerically(
    destination_display: Decimal, derived: bool
) -> None:
    """The derived 0.418637 must reproduce the destination display. Before
    #1186 a display with one more trailing zero, the same number, refused;
    the neighboring 0.418638 and 0.4186380 still do."""
    result = _underdisplayed_transfer_result()
    (transfer,) = result.transfers
    corrections = _derive_transfer_quantity_corrections(
        replace(
            result,
            transfers=(replace(transfer, destination_display_quantity=destination_display),),
        )
    )

    assert [item.quantity for item in corrections] == ([Decimal("0.418637")] if derived else [])


def test_transfer_quantity_correction_rejects_any_unrelated_change() -> None:
    first = _underdisplayed_transfer_result()
    (correction,) = _derive_transfer_quantity_corrections(first)
    transfer = first.transfers[0]
    corrected_movements = tuple(
        (
            replace(movement, signed_quantity=-correction.quantity)
            if movement.event_references == transfer.source_event_references
            else (
                replace(movement, signed_quantity=correction.quantity)
                if movement.event_references == transfer.destination_event_references
                else movement
            )
        )
        for movement in first.movements
    )
    corrected_anchor = replace(
        correction.source_anchor,
        ledger_quantity=correction.source_anchor.statement_quantity,
        tolerance=Decimal("0.0000005"),
    )
    second = replace(
        first,
        movements=corrected_movements,
        transfers=(replace(transfer, resolved_quantity=correction.quantity),),
        residuals=(),
        anchors=(first.anchors[0], corrected_anchor),
    )

    assert _transfer_corrections_hold(first, second, (correction,))
    assert not _transfer_corrections_hold(
        first,
        replace(
            second,
            quantities=(
                RobinhoodAccountSecurityQuantity(0, _CUSIP, Decimal("0.371253")),
                second.quantities[1],
            ),
        ),
        (correction,),
    )


def test_partial_disposal_conserves_exact_source_cost() -> None:
    """Selling 1 of 3 shares bought for $10 costs exactly $10/3, which files ₹281 at
    ₹84.15; the 20-digit share filed ₹280 (#1241)."""
    acquisition = _acquisition(0, 0, _START, Decimal("3"), Decimal("10"))
    sale = _movement(
        0,
        1,
        _START + timedelta(days=1),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(_result((acquisition,), (sale,), {0: Decimal("2")}))

    disposal = next(item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL)
    (fragment,) = result.open_fragments
    assert disposal.quantity == 1
    assert fragment.quantity == 2
    assert disposal.cost_usd + fragment.cost_usd == 10
    assert not result.blockers
    assert to_whole_rupees(disposal.cost_usd * Decimal("84.15")) == 280
    exact = disposal.exact_cost_usd
    assert exact is not None
    assert exact_whole_rupees(exact * Fraction("84.15")) == 281
    assert (exact, fragment.exact_cost_usd) == (Fraction(10, 3), Fraction(20, 3))
    assert _exact_cost(disposal) + _exact_cost(fragment) == 10
    # A lot sold on its own is no pro-rata split: no quantity rounds or carries a rational (#1263).
    assert (
        getattr(disposal, "exact_quantity", None),
        getattr(fragment, "exact_quantity", None),
    ) == (None, None)


def test_same_day_split_takes_each_fragments_exact_cost_share() -> None:
    """#1241: selling 1 of a same-day pair of 1 and 2 shares at $10 each takes a
    third of each lot's cost exactly, whatever the 20-digit quantity parts. The
    sale's cost is exactly $10, which files 10 x 84.15 = 841.5 -> ₹842; the
    20-digit costs added to 9.9999999999999999998 and filed ₹841."""
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10.00")),
        _acquisition(0, 1, _START, Decimal("2"), Decimal("20.00")),
    )
    sale = _movement(
        0, 2, _START + timedelta(days=1), Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL
    )

    result = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal("2")}))

    assert not result.blockers
    slices = sorted(result.slices, key=lambda item: item.lot_id)
    fragments = sorted(result.open_fragments, key=lambda item: item.lot_id)
    decimal_cost = sum((item.cost_usd for item in slices), Decimal())
    assert to_whole_rupees(decimal_cost * Decimal("84.15")) == 841
    exact_cost = sum(map(_exact_cost, slices), Fraction())
    assert exact_whole_rupees(exact_cost * Fraction("84.15")) == 842
    assert exact_cost == 10
    assert [item.exact_cost_usd for item in slices] == [Fraction(10, 3), Fraction(20, 3)]
    # Each lot's sold and held exact costs add back to its cost.
    assert [
        _exact_cost(item) + _exact_cost(fragment)
        for item, fragment in zip(slices, fragments, strict=True)
    ] == [10, 20]
    # The Decimal quantities and costs are the 20-digit parts they always were.
    assert sum((item.quantity for item in slices), Decimal()) == 1
    assert [(item.quantity, item.cost_usd) for item in slices] == [
        (Decimal("0.33333333333333333333"), Decimal("3.3333333333333333333")),
        (Decimal("0.66666666666666666667"), Decimal("6.6666666666666666665")),
    ]
    assert [(item.quantity, item.cost_usd) for item in fragments] == [
        (Decimal("0.66666666666666666667"), Decimal("6.6666666666666666667")),
        (Decimal("1.33333333333333333333"), Decimal("13.3333333333333333335")),
    ]


@pytest.mark.parametrize("transferred", [False, True])
def test_same_day_split_refuses_to_drop_an_exact_cost_remainder(transferred: bool) -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("2"), Decimal("20")),
    )
    quantity = Decimal("2.999999999999999999999")
    day = _START + timedelta(days=1)
    if transferred:
        movements, transfer = _transfer(0, day, quantity, source_ordinal=7)
        quantities = _result(
            acquisitions,
            movements,
            {0: Decimal("1E-21"), 1: quantity},
            transfers=(transfer,),
        )
    else:
        sale = _movement(0, 7, day, quantity.copy_negate(), RobinhoodQuantityMovementKind.DISPOSAL)
        quantities = _result(acquisitions, (sale,), {0: Decimal("1E-21")})

    with pytest.raises(InvariantError) as raised:
        reconcile_robinhood_security_lots(quantities)

    assert str(raised.value) == (
        "InvariantError: Robinhood lot allocation, activity row 7, column Amount: "
        "exact cost remainder has no remaining quantity"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is raised.value.__context__ is None


def test_same_day_split_refuses_a_rounded_group_quantity() -> None:
    """Both lots cost exactly $10 a share (#1326), so the sale splits pro rata,
    and the group's 71-digit quantity rounds at 60 digits."""
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("2." + "0" * 69 + "1"), Decimal("20." + "0" * 68 + "1")),
    )
    sale = _movement(
        0, 7, _START + timedelta(days=1), Decimal("-.5"), RobinhoodQuantityMovementKind.DISPOSAL
    )

    with pytest.raises(InvariantError) as raised:
        reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal("2.5")}))

    assert str(raised.value) == (
        "InvariantError: Robinhood lot allocation, activity row 7, column Quantity: "
        "group quantity must equal its exact fragment sum"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is raised.value.__context__ is None


@pytest.mark.parametrize("source_account_index", [0, 1])
def test_partial_transfer_preserves_lot_cost_date_and_intervals(
    source_account_index: int,
) -> None:
    acquisition = _acquisition(
        source_account_index,
        0,
        _START,
        Decimal("2"),
        Decimal("10"),
    )
    transfer_date = _START + timedelta(days=1)
    transfer_movements, transfer = _transfer(
        source_account_index,
        transfer_date,
        Decimal("1"),
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (acquisition,),
            transfer_movements,
            {source_account_index: Decimal("1"), 1 - source_account_index: Decimal("1")},
            transfers=(transfer,),
        )
    )

    transfer_slice = next(
        item for item in result.slices if item.kind is RobinhoodLotSliceKind.TRANSFER
    )
    assert transfer_slice.lot_id == acquisition.event_reference
    assert transfer_slice.cost_usd == 5
    assert transfer_slice.original_date == _START
    assert {
        (fragment.account_index, fragment.quantity, fragment.cost_usd)
        for fragment in result.open_fragments
    } == {
        (source_account_index, Decimal("1"), Decimal("5")),
        (1 - source_account_index, Decimal("1"), Decimal("5")),
    }
    assert len(result.holding_intervals) == 3


def test_partial_transfer_carries_the_exact_cost_to_the_destination_sale() -> None:
    """#1241: moving 1 of 3 shares bought for $10 to the other account and selling
    it there costs exactly $10/3; the source keeps exactly $20/3."""
    acquisition = _acquisition(0, 0, _START, Decimal("3"), Decimal("10"))
    transfer_movements, transfer = _transfer(0, _START + timedelta(days=1), Decimal("1"))
    sale = _movement(
        1, 2, _START + timedelta(days=2), Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (acquisition,),
            (*transfer_movements, sale),
            {0: Decimal("2"), 1: Decimal()},
            transfers=(transfer,),
        )
    )

    assert not result.blockers
    disposal = next(item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL)
    (fragment,) = result.open_fragments
    assert (disposal.account_index, disposal.cost_usd) == (1, Decimal("3.3333333333333333333"))
    assert (disposal.exact_cost_usd, fragment.account_index, fragment.exact_cost_usd) == (
        Fraction(10, 3),
        0,
        Fraction(20, 3),
    )


_SHARE_REFUSAL = (
    "InvariantError: Robinhood lot allocation, activity row {row}, column Amount: the share "
    + TOO_MANY_DIGITS
)
# Past EXACT_SUM_DIGITS (1000), with the #1241 privacy digit run 736251.
_PAST_CAP = Decimal("736251." + "1" * 995)


def _refused_bare(raised: pytest.ExceptionInfo[InvariantError], row: int) -> None:
    """The #1241 digit-cap refusal at ``row``: fixed text, no context, no chain."""
    assert str(raised.value) == _SHARE_REFUSAL.format(row=row)
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("quantity", "cost", "transferred"),
    [
        pytest.param(Decimal("3"), _PAST_CAP, False, id="cost"),
        pytest.param(Decimal("3." + "0" * 1000), Decimal("10"), False, id="quantity"),
        pytest.param(Decimal("3"), _PAST_CAP, True, id="transfer"),
    ],
)
def test_lot_share_past_the_digit_cap_refuses_at_the_allocated_row(
    quantity: Decimal, cost: Decimal, transferred: bool
) -> None:
    """#1241: taking 1 share of a lot whose exact share would need an operand past
    1000 digits refuses, naming the sale's or transfer's activity row (7),
    rather than round it."""
    acquisition = _acquisition(0, 0, _START, quantity, cost)
    taken = _START + timedelta(days=1)
    if transferred:
        movements, transfer = _transfer(0, taken, Decimal("1"), source_ordinal=7)
        quantities = _result(
            (acquisition,), movements, {0: Decimal("2"), 1: Decimal("1")}, transfers=(transfer,)
        )
    else:
        sale = _movement(0, 7, taken, Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL)
        quantities = _result((acquisition,), (sale,), {0: Decimal("2")})

    with pytest.raises(InvariantError) as raised:
        reconcile_robinhood_security_lots(quantities)

    _refused_bare(raised, 7)


def _proceeds_slices(*quantities: str) -> tuple[RobinhoodLotSlice, ...]:
    """Disposal slices of one sale (activity row 9), one lot each."""
    return tuple(
        RobinhoodLotSlice(
            lot_id=_reference(0, ordinal),
            account_index=0,
            cusip=_CUSIP,
            movement_references=(_reference(0, 9),),
            quantity=Decimal(quantity),
            cost_usd=Decimal("1"),
            original_date=_START,
            entry_date=_START,
            kind=RobinhoodLotSliceKind.DISPOSAL,
        )
        for ordinal, quantity in enumerate(quantities)
    )


@pytest.mark.parametrize(
    ("quantities", "expected"),
    [
        pytest.param(
            ("1", "1", "1"),
            [
                (Decimal("3.333333333333333333333333333"), Fraction(10, 3)),
                (Decimal("3.333333333333333333333333333"), Fraction(10, 3)),
                (Decimal("3.333333333333333333333333334"), Fraction(10, 3)),
            ],
            id="thirds",
        ),
        pytest.param(
            ("1", "1"),
            [(Decimal("5.00"), None), (Decimal("5.00"), None)],
            id="halves",
        ),
    ],
)
def test_disposal_proceeds_carry_exact_shares_of_the_cash(
    quantities: tuple[str, ...], expected: list[tuple[Decimal, Fraction | None]]
) -> None:
    """#1241: each slice's proceeds is the Decimal share at the caller's precision
    as before, with its exact share of the cash where the Decimal rounds it."""
    slices = _proceeds_slices(*quantities)
    source_quantity = Decimal(len(quantities))

    with localcontext(Context(prec=28)):
        allocated = allocate_disposal_proceeds(
            slices, source_quantity=source_quantity, source_cash=Decimal("10.00")
        )

    assert [item for item, *_ in allocated] == list(slices)
    assert [(proceeds, exact) for _, proceeds, exact in allocated] == expected
    shares = [Fraction(proceeds) if exact is None else exact for _, proceeds, exact in allocated]
    assert sum(shares, Fraction()) == 10


def test_disposal_proceeds_past_the_digit_cap_refuse_at_the_sale_row() -> None:
    """#1241: a sale's cash past 1000 digits refuses rather than round its shares."""
    with pytest.raises(InvariantError) as raised:
        allocate_disposal_proceeds(
            _proceeds_slices("1", "1", "1"), source_quantity=Decimal("3"), source_cash=_PAST_CAP
        )

    _refused_bare(raised, 9)


def test_disposal_proceeds_refuse_nonconserving_exact_shares() -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("1E-70"), Decimal("1E-69")),
    )
    sale = _movement(
        0, 7, _START + timedelta(days=1), Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL
    )
    lots = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal()}))
    assert not lots.blockers
    assert sum((Fraction(item.quantity) for item in lots.slices), Fraction()) > 1

    with localcontext(Context(prec=60)), pytest.raises(InvariantError) as raised:
        allocate_disposal_proceeds(
            lots.slices, source_quantity=Decimal("1"), source_cash=Decimal("10")
        )

    assert str(raised.value) == (
        "InvariantError: Robinhood lot allocation, activity row 7, column Amount: "
        "exact proceeds shares must conserve source cash"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is raised.value.__context__ is None


def test_nonfinite_decimal_proceeds_keep_the_conservation_refusal() -> None:
    with (
        localcontext(Context(Emax=1, Emin=-1, traps=[])),
        pytest.raises(RobinhoodReconciliationError) as raised,
    ):
        allocate_disposal_proceeds(
            _proceeds_slices("1", "1"), source_quantity=Decimal("2"), source_cash=Decimal("1000")
        )
    assert str(raised.value) == (
        "Robinhood reconciliation refused: sale proceeds were not conserved across disposal slices"
    )
    assert raised.value.__cause__ is raised.value.__context__ is None


# ---- Exact pro-rata quantities (#1263) ----------------------------------------

# Three same-day 1-share lots bought at $10 each.
_THIRDS_LOTS = tuple(
    _acquisition(0, ordinal, _START, Decimal("1"), Decimal("10")) for ordinal in range(3)
)
_THIRDS_IDS = tuple(item.event_reference for item in _THIRDS_LOTS)
# Sell 1 share of them at activity row 3; they split it pro rata.
_THIRDS_SALE = _movement(
    0, 3, _START + timedelta(days=1), Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL
)


def _sale_slices(
    result: RobinhoodSecurityLotResult, movement: RobinhoodQuantityMovement
) -> tuple[RobinhoodLotSlice, ...]:
    """The slices ``movement`` took, in lot order."""
    return tuple(
        sorted(
            (
                item
                for item in result.slices
                if item.movement_references == movement.event_references
            ),
            key=lambda item: item.lot_id,
        )
    )


def _exact_by_lot(
    result: RobinhoodSecurityLotResult,
) -> tuple[
    dict[RobinhoodSecurityEventReference, Fraction],
    dict[RobinhoodSecurityEventReference, Fraction],
]:
    """Per lot, the exact quantities and costs its disposal slices and open fragments hold."""
    quantities: defaultdict[RobinhoodSecurityEventReference, Fraction] = defaultdict(Fraction)
    costs: defaultdict[RobinhoodSecurityEventReference, Fraction] = defaultdict(Fraction)
    for item in (
        *(item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL),
        *result.open_fragments,
    ):
        quantities[item.lot_id] += _exact_quantity(item)
        costs[item.lot_id] += _exact_cost(item)
    return dict(quantities), dict(costs)


# Each of the three lots holds exactly its 1 share and $10, sold or not.
_THIRDS_CONSERVED = (
    dict.fromkeys(_THIRDS_IDS, Fraction(1)),
    dict.fromkeys(_THIRDS_IDS, Fraction(10)),
)


def test_same_day_pro_rata_sale_keeps_each_lots_exact_quantity() -> None:
    """#1263's lot repro: selling 1 of three same-day 1-share lots bought at $10
    each gives up exactly 1/3 of each lot and keeps exactly 2/3, beside main's
    20-digit Decimal parts; per lot they add back to exactly 1 share."""
    result = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE,), {0: Decimal("2")})
    )

    assert not result.blockers
    slices = _sale_slices(result, _THIRDS_SALE)
    fragments = sorted(result.open_fragments, key=lambda item: item.lot_id)
    # The Decimal quantities and costs are the 20-digit parts main has.
    assert [(item.quantity, item.cost_usd) for item in slices] == [
        (Decimal("0.33333333333333333333"), Decimal("3.3333333333333333333")),
        (Decimal("0.33333333333333333333"), Decimal("3.3333333333333333333")),
        (Decimal("0.33333333333333333334"), Decimal("3.3333333333333333334")),
    ]
    assert [(item.quantity, item.cost_usd) for item in fragments] == [
        (Decimal("0.66666666666666666667"), Decimal("6.6666666666666666667")),
        (Decimal("0.66666666666666666667"), Decimal("6.6666666666666666667")),
        (Decimal("0.66666666666666666666"), Decimal("6.6666666666666666666")),
    ]
    assert [item.exact_quantity for item in slices] == [Fraction(1, 3)] * 3
    assert [item.exact_quantity for item in fragments] == [Fraction(2, 3)] * 3
    assert _exact_by_lot(result) == _THIRDS_CONSERVED


def test_later_sale_of_a_pro_rata_remainder_splits_its_cash_by_exact_quantity() -> None:
    """#1263: selling the group's remaining 2 shares for $10.00 takes each lot's
    exact 2/3, so each slice's proceeds is exactly $10/3. Main split the cash
    by the rounded 0.66666666666666666667/...66 quantities, whose shares were
    exact Decimals and so carried no rational."""
    later = _movement(
        0, 4, _START + timedelta(days=2), Decimal("-2"), RobinhoodQuantityMovementKind.DISPOSAL
    )
    result = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE, later), {0: Decimal()})
    )
    assert not result.blockers
    assert not result.open_fragments
    slices = _sale_slices(result, later)

    with localcontext(Context(prec=28)):
        allocated = allocate_disposal_proceeds(
            slices, source_quantity=Decimal("2"), source_cash=Decimal("10.00")
        )

    assert [(proceeds, exact) for _, proceeds, exact in allocated] == [
        (Decimal("3.33333333333333333335"), Fraction(10, 3)),
        (Decimal("3.33333333333333333335"), Fraction(10, 3)),
        (Decimal("3.3333333333333333333"), Fraction(10, 3)),
    ]
    assert [item.quantity for item in slices] == [
        Decimal("0.66666666666666666667"),
        Decimal("0.66666666666666666667"),
        Decimal("0.66666666666666666666"),
    ]
    assert [item.exact_quantity for item in slices] == [Fraction(2, 3)] * 3
    assert [item.exact_cost_usd for item in slices] == [Fraction(20, 3)] * 3
    assert _exact_by_lot(result) == _THIRDS_CONSERVED


def test_later_pro_rata_sale_splits_each_lots_exact_remainder() -> None:
    """#1263: a later sale of 0.5 of the group's remaining 2 shares takes exactly
    1/6 of a share and $5/3 from each lot, which keeps exactly 1/2 share and
    $5. A kept 0.50000000000000000000 is exact, so it carries no rational."""
    later = _movement(
        0, 4, _START + timedelta(days=2), Decimal("-0.5"), RobinhoodQuantityMovementKind.DISPOSAL
    )
    result = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE, later), {0: Decimal("1.5")})
    )

    assert not result.blockers
    slices = _sale_slices(result, later)
    fragments = sorted(result.open_fragments, key=lambda item: item.lot_id)
    assert [(item.quantity, item.cost_usd, item.exact_cost_usd) for item in slices] == [
        (Decimal("0.16666666666666666667"), Decimal("1.6666666666666666666"), Fraction(5, 3)),
        (Decimal("0.16666666666666666667"), Decimal("1.6666666666666666666"), Fraction(5, 3)),
        (Decimal("0.16666666666666666666"), Decimal("1.6666666666666666667"), Fraction(5, 3)),
    ]
    assert [(item.quantity, _exact_cost(item)) for item in fragments] == [
        (Decimal("0.5"), Fraction(5)),
        (Decimal("0.5"), Fraction(5)),
        (Decimal("0.5"), Fraction(5)),
    ]
    assert [item.exact_quantity for item in slices] == [Fraction(1, 6)] * 3
    assert [item.exact_quantity for item in fragments] == [None] * 3
    assert _exact_by_lot(result) == _THIRDS_CONSERVED


def test_pro_rata_transfer_carries_exact_quantities_to_the_destination_sale() -> None:
    """#1263: moving 1 of the group's remaining 2 shares to the other account
    takes exactly 1/3 of a share from each lot. Selling 0.5 there for $3.00
    splits those exact thirds, 1/6 from each lot, and gives each exactly $1.00;
    main split the cash by the rounded 0.16666666666666666667/...66."""
    transfer_movements, transfer = _transfer(
        0, _START + timedelta(days=2), Decimal("1"), source_ordinal=5, destination_ordinal=6
    )
    sale = _movement(
        1, 7, _START + timedelta(days=3), Decimal("-0.5"), RobinhoodQuantityMovementKind.DISPOSAL
    )
    result = reconcile_robinhood_security_lots(
        _result(
            _THIRDS_LOTS,
            (_THIRDS_SALE, *transfer_movements, sale),
            {0: Decimal("1"), 1: Decimal("0.5")},
            transfers=(transfer,),
        )
    )
    assert not result.blockers
    moved = _sale_slices(result, transfer_movements[0])
    sold = _sale_slices(result, sale)

    with localcontext(Context(prec=28)):
        allocated = allocate_disposal_proceeds(
            sold, source_quantity=Decimal("0.5"), source_cash=Decimal("3.00")
        )

    assert [exact for *_, exact in allocated] == [Fraction(1)] * 3
    assert [item.quantity for item in moved] == [
        Decimal("0.33333333333333333334"),
        Decimal("0.33333333333333333334"),
        Decimal("0.33333333333333333332"),
    ]
    assert [item.exact_quantity for item in moved] == [Fraction(1, 3)] * 3
    assert [(item.account_index, item.quantity) for item in sold] == [
        (1, Decimal("0.16666666666666666667")),
        (1, Decimal("0.16666666666666666667")),
        (1, Decimal("0.16666666666666666666")),
    ]
    assert [(item.exact_quantity, item.exact_cost_usd) for item in sold] == [
        (Fraction(1, 6), Fraction(5, 3))
    ] * 3
    assert sorted(
        (item.account_index, item.lot_id, _exact_quantity(item)) for item in result.open_fragments
    ) == sorted(
        (account_index, lot_id, quantity)
        for lot_id in _THIRDS_IDS
        for account_index, quantity in ((0, Fraction(1, 3)), (1, Fraction(1, 6)))
    )
    assert _exact_by_lot(result) == _THIRDS_CONSERVED


@pytest.mark.parametrize(
    ("acquisitions", "sold", "held"),
    [
        pytest.param(
            _THIRDS_LOTS[:2],
            [(Decimal("0.5"), Decimal("5")), (Decimal("0.5"), Decimal("5"))],
            [(Decimal("0.5"), Decimal("5")), (Decimal("0.5"), Decimal("5"))],
            id="same-day-halves",
        ),
        pytest.param(
            (_acquisition(0, 0, _START, Decimal("2"), Decimal("10.00")),),
            [(Decimal("1"), Decimal("5.00"))],
            [(Decimal("1"), Decimal("5.00"))],
            id="one-lot",
        ),
    ],
)
def test_sale_that_divides_exactly_carries_no_exact_quantity(
    acquisitions: tuple[RobinhoodAcquisitionRecord, ...],
    sold: list[tuple[Decimal, Decimal]],
    held: list[tuple[Decimal, Decimal]],
) -> None:
    """Control (#1263): selling 1 share of two same-day 1-share lots bought at $10
    each, or of one 2-share lot, takes exact Decimal quantities, so no slice or
    fragment carries a rational and the Decimals and proceeds are main's."""
    sale = _movement(
        0, 3, _START + timedelta(days=1), Decimal("-1"), RobinhoodQuantityMovementKind.DISPOSAL
    )
    result = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal("1")}))

    assert not result.blockers
    slices = _sale_slices(result, sale)
    fragments = sorted(result.open_fragments, key=lambda item: item.lot_id)
    allocated = allocate_disposal_proceeds(
        slices, source_quantity=Decimal("1"), source_cash=Decimal("3.00")
    )
    assert [(proceeds, exact) for _, proceeds, exact in allocated] == (
        [(Decimal("1.50"), None), (Decimal("1.50"), None)]
        if len(slices) == 2
        else [(Decimal("3.00"), None)]
    )
    assert [(item.quantity, item.cost_usd) for item in slices] == sold
    assert [(item.quantity, item.cost_usd) for item in fragments] == held
    assert [
        (getattr(item, "exact_quantity", None), item.exact_cost_usd)
        for item in (*slices, *fragments)
    ] == [(None, None)] * (len(slices) + len(fragments))


def _exact_fragment(
    *,
    cost: Decimal = Decimal("6.6666666666666666667"),
    exact_cost: Fraction | None = Fraction(20, 3),
) -> _Fragment:
    """A lot's open 2/3 share after #1263's pro-rata repro: 20-digit Decimals beside rationals."""
    return _Fragment(
        lot_id=_reference(0, 0),
        quantity=Decimal("0.66666666666666666667"),
        cost_usd=cost,
        original_date=_START,
        entry_date=_START,
        batch_id=0,
        batch_rank=0,
        ordered_batch=False,
        exact_cost_usd=exact_cost,
        exact_quantity=Fraction(2, 3),
    )


# The sale (activity row 7) that _take's unit cases allocate.
_TAKE_SALE = _movement(
    0, 7, _START + timedelta(days=1), Decimal("-0.5"), RobinhoodQuantityMovementKind.DISPOSAL
)
_QUANTITY_REMAINDER_REFUSAL = (
    "InvariantError: Robinhood lot allocation, activity row 7, column Quantity: "
    "exact quantity remainder must match the remaining quantity"
)


def test_partial_take_of_an_exact_fragment_takes_cost_by_its_exact_quantity() -> None:
    """#1263: taking 0.5 share of a fragment holding exactly 2/3 share for $20/3
    costs exactly $5, and the fragment keeps exactly 1/6 share and $5/3. Main
    took the cost share by 0.5 over the rounded 0.66666666666666666667."""
    fragment = _exact_fragment()
    queue = [fragment]

    item, _interval, taken = _take(
        queue, fragment, Decimal("0.5"), _TAKE_SALE, RobinhoodLotSliceKind.DISPOSAL
    )

    assert (item.quantity, item.cost_usd, item.exact_cost_usd) == (
        Decimal("0.5"),
        Decimal("5.0000000000000000001"),
        Fraction(5),
    )
    assert (taken.exact_cost_usd, taken.exact_quantity, item.exact_quantity) == (
        Fraction(5),
        None,
        None,
    )
    assert queue == [fragment]
    assert (fragment.quantity, fragment.cost_usd) == (
        Decimal("0.16666666666666666667"),
        Decimal("1.6666666666666666666"),
    )
    assert (fragment.exact_quantity, fragment.exact_cost_usd) == (Fraction(1, 6), Fraction(5, 3))


@pytest.mark.parametrize(
    ("quantity", "exact_ratio", "exact_quantity"),
    [
        pytest.param(
            Decimal("0.66666666666666666667"),
            (Fraction(1), Fraction(2)),
            Fraction(1, 3),
            id="whole-decimal-partial-exact",
        ),
        pytest.param(Decimal("0.5"), None, Fraction(2, 3), id="partial-decimal-whole-exact"),
    ],
)
def test_take_refuses_an_exact_quantity_remainder_the_decimal_contradicts(
    quantity: Decimal,
    exact_ratio: tuple[Fraction, Fraction] | None,
    exact_quantity: Fraction,
) -> None:
    """#1263: a forged pro-rata part whose exact quantity leaves a remainder where
    the Decimal take leaves none, or none where it leaves one, refuses with
    fixed text before the fragment changes. Zero cost keeps #1241's exact
    cost remainder check quiet."""
    fragment = _exact_fragment(cost=Decimal(0), exact_cost=None)
    queue = [fragment]

    with pytest.raises(InvariantError) as raised:
        _take(
            queue,
            fragment,
            quantity,
            _TAKE_SALE,
            RobinhoodLotSliceKind.DISPOSAL,
            exact_ratio,
            exact_quantity,
        )

    assert str(raised.value) == _QUANTITY_REMAINDER_REFUSAL
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert queue == [fragment]
    assert (fragment.quantity, fragment.exact_quantity) == (
        Decimal("0.66666666666666666667"),
        Fraction(2, 3),
    )


def test_take_checks_the_exact_cost_remainder_before_the_exact_quantity_one() -> None:
    """#1263: with a positive cost, the forged whole-Decimal part with a partial
    exact quantity refuses on #1241's exact cost remainder first."""
    fragment = _exact_fragment()

    with pytest.raises(InvariantError) as raised:
        _take(
            [fragment],
            fragment,
            Decimal("0.66666666666666666667"),
            _TAKE_SALE,
            RobinhoodLotSliceKind.DISPOSAL,
            (Fraction(1), Fraction(2)),
            Fraction(1, 3),
        )

    assert str(raised.value) == (
        "InvariantError: Robinhood lot allocation, activity row 7, column Amount: "
        "exact cost remainder has no remaining quantity"
    )
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_take_refuses_an_exact_part_past_the_fragments_exact_quantity() -> None:
    """#1263: a forged 3/4-share part of a fragment holding exactly 2/3 leaves a
    negative exact rest, which refuses as an overdrawn fragment."""
    fragment = _exact_fragment()

    with pytest.raises(RobinhoodReconciliationError) as raised:
        _take(
            [fragment],
            fragment,
            Decimal("0.5"),
            _TAKE_SALE,
            RobinhoodLotSliceKind.DISPOSAL,
            None,
            Fraction(3, 4),
        )

    assert str(raised.value) == (
        "Robinhood reconciliation refused: lot fragment consumption exceeded its source balance"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("exact", "expected"),
    [
        pytest.param(Fraction(2, 3), Fraction(2, 3) + Fraction(37, 10**6), id="exact"),
        pytest.param(None, None, id="decimal"),
    ],
)
def test_residual_adjustment_adds_to_the_exact_quantity(
    exact: Fraction | None, expected: Fraction | None
) -> None:
    """#1263: a sale's 0.000037 residual adjustment adds to its last slice's exact
    quantity; a slice whose Decimal is exact keeps ``None``."""
    taken = _TakenFragment(
        lot_id=_reference(0, 2),
        quantity=Decimal("0.66666666666666666666"),
        cost_usd=Decimal("6.6666666666666666666"),
        original_date=_START,
        exact_cost_usd=Fraction(20, 3),
        exact_quantity=exact,
    )

    # Read at call time, so the module still imports on a tree without it.
    adjusted = robinhood_lots._adjusted_exact_quantity
    assert adjusted(taken, Decimal("0.000037"), _TAKE_SALE) == expected


def test_residual_on_a_later_sale_keeps_exact_quantities_conserved() -> None:
    """#1263: selling the group's remaining 2 shares plus a 0.000037 display
    residual adds the residual to the last lot's exact 2/3, and each lot's
    exact quantities still add back to its purchase plus its adjustment."""
    later = _movement(
        0,
        4,
        _START + timedelta(days=2),
        Decimal("-2.000037"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )
    residual = RobinhoodQuantityResidual(
        account_index=0,
        cusip=_CUSIP,
        activity_date=later.activity_date,
        quantity=Decimal("0.000037"),
        event_references=later.event_references,
    )

    result = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE, later), {0: Decimal()}, residuals=(residual,))
    )

    assert not result.blockers
    slices = _sale_slices(result, later)
    assert [(item.quantity, item.exact_quantity) for item in slices] == [
        (Decimal("0.66666666666666666667"), Fraction(2, 3)),
        (Decimal("0.66666666666666666667"), Fraction(2, 3)),
        (Decimal("0.66670366666666666666"), Fraction(2, 3) + Fraction(37, 10**6)),
    ]
    assert slices[-1].residual_references == residual.event_references
    quantities, costs = _exact_by_lot(result)
    assert quantities == {
        **dict.fromkeys(_THIRDS_IDS, Fraction(1)),
        _THIRDS_IDS[-1]: 1 + Fraction(37, 10**6),
    }
    assert costs == dict.fromkeys(_THIRDS_IDS, Fraction(10))


@pytest.mark.parametrize("forgery", ["dropped", "nudged"])
def test_conservation_refuses_exact_quantities_that_do_not_add_back(
    monkeypatch: pytest.MonkeyPatch, forgery: str
) -> None:
    """#1263: the end-of-run check refuses a lot whose exact quantities don't add
    back to its purchase though its Decimals do: here each open fragment's
    exact 2/3 is forged away, or nudged by 1E-30."""
    real = RobinhoodOpenLotFragment

    def forged(**fields: Any) -> RobinhoodOpenLotFragment:
        exact = fields["exact_quantity"]
        if exact is not None:
            fields["exact_quantity"] = None if forgery == "dropped" else exact + Fraction(1, 10**30)
        return real(**fields)

    monkeypatch.setattr(robinhood_lots, "RobinhoodOpenLotFragment", forged)

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_security_lots(_result(_THIRDS_LOTS, (_THIRDS_SALE,), {0: Decimal("2")}))

    assert str(raised.value) == (
        "Robinhood reconciliation refused: resolved lot inventory did not conserve "
        "source acquisition"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_pro_rata_sale_past_the_digit_cap_refuses_at_the_sale_row() -> None:
    """#1263: a same-day pro-rata sale of 1E-1001 shares, 1001 places past the
    units digit, refuses with #1241's fixed text at the sale's activity row
    (7), as on main. (A long coefficient can't get there: the sale's quantity
    is negated at 60 digits.)"""
    sale = _movement(
        0,
        7,
        _START + timedelta(days=1),
        Decimal("-1E-1001"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    with pytest.raises(InvariantError) as raised:
        # It refuses before it compares the held quantity.
        reconcile_robinhood_security_lots(_result(_THIRDS_LOTS, (sale,), {0: Decimal("3")}))

    _refused_bare(raised, 7)


# ---- Exact same-day unit costs (#1326) ------------------------------------------


def _later_sales(*quantities: str) -> tuple[RobinhoodQuantityMovement, ...]:
    """Sales after _THIRDS_SALE, a day apart, at activity rows 4, 5 and on."""
    return tuple(
        _movement(
            0,
            4 + index,
            _START + timedelta(days=2 + index),
            Decimal(quantity),
            RobinhoodQuantityMovementKind.DISPOSAL,
        )
        for index, quantity in enumerate(quantities)
    )


def test_third_partial_sale_of_a_twice_split_group_splits_pro_rata() -> None:
    """#1326's lot repro: after selling 1 share and then 0.5 of the three
    same-day 1-share lots bought at $10 each, every lot holds exactly 1/2
    share and $5, but the Decimal costs differ in the 20th digit. Main
    compared those and gave a third sale of 0.5 a same-day blocker. The exact
    unit costs match, so it splits pro rata: exactly 1/6 share and $5/3 from
    each lot, which keeps exactly 1/3 share and $10/3."""
    second, third = _later_sales("-0.5", "-0.5")
    split = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE, second), {0: Decimal("1.5")})
    )
    assert [
        (item.cost_usd, _exact_cost(item))
        for item in sorted(split.open_fragments, key=lambda item: item.lot_id)
    ] == [
        (Decimal("5.0000000000000000001"), Fraction(5)),
        (Decimal("5.0000000000000000001"), Fraction(5)),
        (Decimal("4.9999999999999999999"), Fraction(5)),
    ]

    result = reconcile_robinhood_security_lots(
        _result(_THIRDS_LOTS, (_THIRDS_SALE, second, third), {0: Decimal("1")})
    )

    assert not result.blockers
    slices = _sale_slices(result, third)
    assert [(item.exact_quantity, item.exact_cost_usd) for item in slices] == [
        (Fraction(1, 6), Fraction(5, 3))
    ] * 3
    assert sum((item.quantity for item in slices), Decimal()) == Decimal("0.5")
    assert [(_exact_quantity(item), _exact_cost(item)) for item in result.open_fragments] == [
        (Fraction(1, 3), Fraction(10, 3))
    ] * 3
    assert _exact_by_lot(result) == _THIRDS_CONSERVED


# Two same-day 1-share lots bought for $10.00 and $10.01, at activity rows 3 and 4.
_CENT_APART_LOTS = tuple(
    _acquisition(0, ordinal, _START + timedelta(days=1), Decimal("1"), Decimal(cost))
    for ordinal, cost in ((3, "10.00"), (4, "10.01"))
)


@pytest.mark.parametrize(
    ("acquisitions", "earlier_sales", "held"),
    [
        pytest.param(_CENT_APART_LOTS, (), Decimal("1.5"), id="first-sale"),
        pytest.param(
            (*_THIRDS_LOTS, *_CENT_APART_LOTS),
            (
                _movement(
                    0,
                    5,
                    _START + timedelta(days=2),
                    Decimal("-3"),
                    RobinhoodQuantityMovementKind.DISPOSAL,
                ),
            ),
            Decimal("1.5"),
            id="after-a-whole-group-sale",
        ),
    ],
)
def test_same_day_lots_at_different_unit_costs_still_block_a_partial_sale(
    acquisitions: tuple[RobinhoodAcquisitionRecord, ...],
    earlier_sales: tuple[RobinhoodQuantityMovement, ...],
    held: Decimal,
) -> None:
    """Control (#1326): same-day 1-share lots bought for $10.00 and $10.01 have
    different exact unit costs, so selling 0.5 of them (activity row 6)
    blocks, as on main, whether it is the first sale or follows a sale of the
    whole earlier group of three $10 lots."""
    sale = _movement(
        0, 6, _START + timedelta(days=3), Decimal("-0.5"), RobinhoodQuantityMovementKind.DISPOSAL
    )

    result = reconcile_robinhood_security_lots(
        _result(acquisitions, (*earlier_sales, sale), {0: held})
    )

    assert [(item.kind, item.event_references, item.lot_ids) for item in result.blockers] == [
        (
            RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY,
            sale.event_references,
            tuple(item.event_reference for item in _CENT_APART_LOTS),
        )
    ]


# Within the readers' digit limits: with n = 5E30 + 5, lots of n and n - 1
# times 1E-20 shares costing n + 1 and n times $1E-20. Their unit costs differ,
# but their 62-digit exact cross products round identically at main's 60-digit precision.
_SIXTY_DIGIT_LOTS = (
    _acquisition(
        0,
        0,
        _START,
        Decimal("50000000000.00000000000000000005"),
        Decimal("50000000000.00000000000000000006"),
    ),
    _acquisition(
        0,
        1,
        _START,
        Decimal("50000000000.00000000000000000004"),
        Decimal("50000000000.00000000000000000005"),
    ),
)
# The old rounded-group-quantity case's two lots have differing unit costs,
# but their Decimal cross products agree at 60 digits.
_SEVENTY_DIGIT_LOTS = (
    _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
    _acquisition(0, 1, _START, Decimal("2." + "0" * 69 + "1"), Decimal("20")),
)


@pytest.mark.parametrize(
    ("acquisitions", "sold", "held"),
    [
        pytest.param(
            _SIXTY_DIGIT_LOTS,
            "-1",
            Decimal("99999999999.00000000000000000009"),
            id="within-limits",
        ),
        pytest.param(
            _SIXTY_DIGIT_LOTS,
            "-100000000000.00000000000000000004",
            Decimal("0.00000000000000000005"),
            id="within-limits-cost-remainder",
        ),
        pytest.param(
            _SIXTY_DIGIT_LOTS,
            "-100000000000.00000000000000000008",
            Decimal("0.00000000000000000001"),
            id="within-limits-overdrawn",
        ),
        pytest.param(_SEVENTY_DIGIT_LOTS, "-1", Decimal("2." + "0" * 69 + "1"), id="past-limits"),
    ],
)
def test_same_day_lots_whose_unit_costs_differ_past_60_digits_block_a_partial_sale(
    acquisitions: tuple[RobinhoodAcquisitionRecord, ...], sold: str, held: Decimal
) -> None:
    """#1326: main multiplied the Decimals at 60 digits, so it took these lots'
    different unit costs as one and split them pro rata. Selling 1 share of
    the first pair passed; selling all but 0.00000000000000000005 or
    0.00000000000000000001 of it refused in the split, on the exact cost
    remainder or an overdrawn fragment; the second pair refused on the
    group-quantity check. The exact comparison sees the difference, so each
    sale (activity row 7) gets the same-day blocker."""
    sale = _movement(
        0, 7, _START + timedelta(days=1), Decimal(sold), RobinhoodQuantityMovementKind.DISPOSAL
    )

    result = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: held}))

    assert [(item.kind, item.event_references) for item in result.blockers] == [
        (RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY, sale.event_references)
    ]


@pytest.mark.parametrize(
    ("quantity", "cost"),
    [
        pytest.param(Decimal("3"), _PAST_CAP, id="cost"),
        pytest.param(Decimal("3." + "0" * 1000), Decimal("30"), id="quantity"),
    ],
)
def test_same_day_unit_costs_past_the_digit_cap_refuse_at_the_sale_row(
    quantity: Decimal, cost: Decimal
) -> None:
    """#1326: a same-day group whose unit costs can't be compared exactly, one
    lot's cost or quantity past 1000 digits, refuses with #1241's fixed text at
    the sale's activity row (7). Main blocked the cost case as a different
    unit cost, and refused the quantity case the same way when it split."""
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, quantity, cost),
    )
    sale = _movement(
        0, 7, _START + timedelta(days=1), Decimal("-0.5"), RobinhoodQuantityMovementKind.DISPOSAL
    )

    with pytest.raises(InvariantError) as raised:
        reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal("3.5")}))

    _refused_bare(raised, 7)


def test_internal_transfer_uses_original_date_fifo() -> None:
    transferred = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    local = _acquisition(1, 1, _START + timedelta(days=1), Decimal("1"), Decimal("20"))
    transfer_movements, transfer = _transfer(
        0,
        _START + timedelta(days=2),
        Decimal("1"),
    )
    sale = _movement(
        1,
        2,
        _START + timedelta(days=3),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (transferred, local),
            (*transfer_movements, sale),
            {0: Decimal(), 1: Decimal("1")},
            transfers=(transfer,),
        )
    )

    assert not result.blockers
    disposal = next(item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL)
    assert disposal.lot_id == transferred.event_reference
    assert disposal.original_date == _START
    assert disposal.cost_usd == 10


def test_same_day_partial_group_requires_invariant_basis() -> None:
    """Selling part of a same-day group bought at different costs per share is
    blocked: the sold share's basis depends on which lot it took. The
    equal-cost control is
    ``test_sale_that_divides_exactly_carries_no_exact_quantity[same-day-halves]``."""
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("1"), Decimal("20")),
    )
    sale = _movement(
        0,
        2,
        _START + timedelta(days=1),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal("1")}))

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
    }
    assert not result.slices


def test_same_day_transfer_destination_disposal_is_ambiguous() -> None:
    transferred = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    local_date = _START + timedelta(days=2)
    local = _acquisition(1, 1, local_date, Decimal("1"), Decimal("10"))
    transfer_movements, transfer = _transfer(0, local_date, Decimal("1"))
    sale = _movement(
        1,
        2,
        local_date,
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (transferred, local),
            (*transfer_movements, sale),
            {0: Decimal(), 1: Decimal("1")},
            transfers=(transfer,),
        )
    )

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
    }
    assert not result.slices


def test_same_day_full_group_consumption_needs_no_member_order() -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("1"), Decimal("20")),
    )
    sale = _movement(
        0,
        2,
        _START + timedelta(days=1),
        Decimal("-2"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(_result(acquisitions, (sale,), {0: Decimal()}))

    assert not result.blockers
    assert {
        item.lot_id for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL
    } == {item.event_reference for item in acquisitions}


def test_transfer_batch_preserves_source_fifo_order() -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START + timedelta(days=1), Decimal("1"), Decimal("20")),
    )
    transfer_movements, transfer = _transfer(
        0,
        _START + timedelta(days=2),
        Decimal("2"),
    )
    sale = _movement(
        1,
        2,
        _START + timedelta(days=3),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(
        _result(
            acquisitions,
            (*transfer_movements, sale),
            {0: Decimal(), 1: Decimal("1")},
            transfers=(transfer,),
        )
    )

    assert not result.blockers
    disposal_slices = [
        item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL
    ]
    assert len(disposal_slices) == 1
    assert disposal_slices[0].lot_id == acquisitions[0].event_reference


def test_transfer_preserves_an_unordered_same_day_source_group() -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(0, 1, _START, Decimal("1"), Decimal("20")),
    )
    transfer_movements, transfer = _transfer(
        0,
        _START + timedelta(days=1),
        Decimal("2"),
    )
    sale = _movement(
        1,
        2,
        _START + timedelta(days=2),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(
        _result(
            acquisitions,
            (*transfer_movements, sale),
            {0: Decimal(), 1: Decimal("1")},
            transfers=(transfer,),
        )
    )

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
    }
    assert not result.slices


def test_same_day_crossing_transfers_do_not_invent_an_outgoing_order() -> None:
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("1"), Decimal("10")),
        _acquisition(1, 1, _START + timedelta(days=1), Decimal("1"), Decimal("50")),
    )
    transfer_date = _START + timedelta(days=2)
    forward_movements, forward = _transfer(
        0,
        transfer_date,
        Decimal("1"),
        source_ordinal=100,
        destination_ordinal=101,
    )
    reverse_movements, reverse = _transfer(
        1,
        transfer_date,
        Decimal("1"),
        transfer_index=1,
        source_ordinal=102,
        destination_ordinal=103,
    )

    result = reconcile_robinhood_security_lots(
        _result(
            acquisitions,
            (*forward_movements, *reverse_movements),
            {0: Decimal("1"), 1: Decimal("1")},
            transfers=(forward, reverse),
        )
    )

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
    }
    assert len(result.blockers[0].event_references) == 4
    assert not result.slices
    assert not result.open_fragments
    assert not result.holding_intervals


def test_anchor_drift_excludes_the_security_globally() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    anchor = RobinhoodPositionAnchor(
        account_index=0,
        cusip=_CUSIP,
        statement_index=0,
        period_end=_START,
        ledger_quantity=Decimal("1"),
        statement_quantity=Decimal("1.001"),
        trade_date_quantity=Decimal("1.001"),
        settlement_date_quantity=Decimal("1.001"),
        snapshot_basis=RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
        tolerance=Decimal("0.01"),
        position_references=(),
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (acquisition,),
            (),
            {0: Decimal("1.001")},
            anchors=(anchor,),
        )
    )

    assert {blocker.kind for blocker in result.blockers} == {RobinhoodLotBlockerKind.ANCHOR_DRIFT}
    assert not result.open_fragments


def test_inherited_basis_blocker_excludes_the_security_globally() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), None)
    blocker = RobinhoodInventoryBlocker(
        account_index=0,
        cusip=_CUSIP,
        unproved_fact=RobinhoodInventoryUnprovedFact.BASIS,
        kind=RobinhoodInventoryBlockerKind.SECURITY_RECEIPT,
        event_references=(acquisition.event_reference,),
    )

    result = reconcile_robinhood_security_lots(
        _result((acquisition,), (), {0: Decimal("1")}, blockers=(blocker,))
    )

    assert {item.kind for item in result.blockers} == {RobinhoodLotBlockerKind.INHERITED_BASIS}
    assert not result.open_fragments


def test_identity_and_unscoped_inventory_blockers_do_not_exclude_lots() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    identity_blocker = RobinhoodSecurityIdentityBlocker(
        kind=RobinhoodSecurityIdentityBlockerKind.MISSING_CONFIG,
        event_references=(acquisition.event_reference,),
        observed_symbols=(),
    )
    inventory_blocker = RobinhoodInventoryBlocker(
        account_index=0,
        cusip=None,
        unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
        kind=RobinhoodInventoryBlockerKind.POSITION_IDENTITY,
    )
    quantity_result = replace(
        _result(
            (acquisition,),
            (),
            {0: Decimal("1")},
            blockers=(inventory_blocker,),
        ),
        identity_blockers=(identity_blocker,),
    )

    result = reconcile_robinhood_security_lots(quantity_result)

    assert result.identity_blockers == (identity_blocker,)
    assert result.blockers[0].cusip is None
    assert result.open_fragments


def test_unproved_shortfall_becomes_a_blocker_not_an_error() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    sale = _movement(
        0,
        1,
        _START + timedelta(days=1),
        Decimal("-2"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(_result((acquisition,), (sale,), {0: Decimal()}))

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.QUANTITY_SHORTFALL
    }
    assert not result.slices


def test_unrelated_residual_does_not_authorize_a_shortfall() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    sale = _movement(
        0,
        1,
        _START + timedelta(days=1),
        Decimal("-2"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )
    unrelated = RobinhoodQuantityResidual(
        account_index=0,
        cusip=_CUSIP,
        activity_date=sale.activity_date,
        quantity=Decimal("1"),
        event_references=(_reference(0, 99),),
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (acquisition,),
            (sale,),
            {0: Decimal()},
            residuals=(unrelated,),
        )
    )

    assert {blocker.kind for blocker in result.blockers} == {
        RobinhoodLotBlockerKind.QUANTITY_SHORTFALL
    }


@pytest.mark.parametrize(
    ("statement_quantity", "transfer_tolerance", "expected_blocker"),
    [
        (Decimal("0.999"), Decimal("0.001"), None),
        (Decimal("0.998"), Decimal("0.01"), RobinhoodLotBlockerKind.ANCHOR_DRIFT),
        (Decimal("0.999"), Decimal("0.0005"), RobinhoodLotBlockerKind.ANCHOR_DRIFT),
    ],
)
def test_transfer_residual_anchor_requires_exact_bounded_explanation(
    statement_quantity: Decimal,
    transfer_tolerance: Decimal,
    expected_blocker: RobinhoodLotBlockerKind | None,
) -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("0.999"), Decimal("10"))
    transfer_date = _START + timedelta(days=1)
    transfer_movements, transfer = _transfer(
        0,
        transfer_date,
        Decimal("1"),
        tolerance=transfer_tolerance,
    )
    residual = RobinhoodQuantityResidual(
        account_index=0,
        cusip=_CUSIP,
        activity_date=transfer.activity_date,
        quantity=Decimal("0.001"),
        event_references=transfer.source_event_references,
    )
    anchor = RobinhoodPositionAnchor(
        account_index=1,
        cusip=_CUSIP,
        statement_index=0,
        period_end=transfer_date,
        ledger_quantity=Decimal("1"),
        statement_quantity=statement_quantity,
        trade_date_quantity=statement_quantity,
        settlement_date_quantity=statement_quantity,
        snapshot_basis=RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
        tolerance=Decimal("0.01"),
        position_references=(),
    )

    result = reconcile_robinhood_security_lots(
        _result(
            (acquisition,),
            transfer_movements,
            {0: Decimal(), 1: statement_quantity},
            transfers=(transfer,),
            residuals=(residual,),
            anchors=(anchor,),
        )
    )

    if expected_blocker is not None:
        assert {blocker.kind for blocker in result.blockers} == {expected_blocker}
        assert not result.open_fragments
        return
    assert not result.blockers
    assert result.slices[0].quantity == Decimal("0.999")
    assert result.slices[0].cost_usd == 10
    assert result.slices[0].residual_references == residual.event_references
    assert result.open_fragments[0].quantity == Decimal("0.999")
    assert result.open_fragments[0].cost_usd == 10


def test_transfer_display_residual_never_enters_destination_tax_lots() -> None:
    # Synthetic: 4 + 0.003681 = 4.003681 held, displayed as 4.0037 when moved;
    # 4.0037 - 4.003681 = 0.000019 residual; cost 1150 + 1.40 = 1151.40.
    acquisitions = (
        _acquisition(0, 0, _START, Decimal("4"), Decimal("1150")),
        _acquisition(0, 1, _START + timedelta(days=1), Decimal("0.003681"), Decimal("1.40")),
    )
    transfer_date = _START + timedelta(days=2)
    transfer_movements, transfer = _transfer(
        0,
        transfer_date,
        Decimal("4.0037"),
        tolerance=Decimal("0.5"),
    )
    sale = _movement(
        1,
        2,
        transfer_date + timedelta(days=1),
        Decimal("-4.003681"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )
    residual = RobinhoodQuantityResidual(
        account_index=0,
        cusip=_CUSIP,
        activity_date=transfer_date,
        quantity=Decimal("0.000019"),
        event_references=transfer.source_event_references,
    )
    anchor = RobinhoodPositionAnchor(
        account_index=1,
        cusip=_CUSIP,
        statement_index=0,
        period_end=sale.activity_date,
        ledger_quantity=Decimal("0.000019"),
        statement_quantity=Decimal(),
        trade_date_quantity=Decimal(),
        settlement_date_quantity=Decimal(),
        snapshot_basis=RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
        tolerance=Decimal("0.50005"),
        position_references=(),
    )

    result = reconcile_robinhood_security_lots(
        _result(
            acquisitions,
            (*transfer_movements, sale),
            {0: Decimal(), 1: Decimal()},
            transfers=(transfer,),
            residuals=(residual,),
            anchors=(anchor,),
        )
    )

    transfer_slices = [
        item for item in result.slices if item.kind is RobinhoodLotSliceKind.TRANSFER
    ]
    disposal_slices = [
        item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL
    ]
    assert not result.blockers
    assert not result.open_fragments
    assert sum((item.quantity for item in transfer_slices), Decimal()) == Decimal("4.003681")
    assert sum((item.cost_usd for item in transfer_slices), Decimal()) == Decimal("1151.40")
    assert transfer_slices[-1].residual_references == residual.event_references
    assert sum((item.quantity for item in disposal_slices), Decimal()) == Decimal("4.003681")
    assert sum((item.cost_usd for item in disposal_slices), Decimal()) == Decimal("1151.40")


def test_structural_acquisition_disagreement_refuses_statically() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("10"))
    forged = _result((acquisition,), (), {0: Decimal("1")})
    forged = replace(
        forged,
        movements=(
            replace(
                forged.movements[0],
                signed_quantity=Decimal("2"),
            ),
        ),
    )

    with pytest.raises(RobinhoodReconciliationError, match="acquisition record"):
        reconcile_robinhood_security_lots(forged)


def test_allocation_is_independent_of_the_ambient_decimal_context() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("3"), Decimal("10"))
    sale = _movement(
        0,
        1,
        _START + timedelta(days=1),
        Decimal("-1"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )
    quantity_result = _result((acquisition,), (sale,), {0: Decimal("2")})

    expected = reconcile_robinhood_security_lots(quantity_result)
    with localcontext(Context(prec=6)):
        actual = reconcile_robinhood_security_lots(quantity_result)

    assert actual == expected


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=20),
            st.integers(min_value=1, max_value=200),
        ),
        min_size=1,
        max_size=8,
    ),
    st.data(),
)
def test_fifo_property_conserves_quantity_and_cost(
    raw_acquisitions: list[tuple[int, int]],
    data: st.DataObject,
) -> None:
    acquisitions = tuple(
        _acquisition(
            0,
            ordinal,
            _START + timedelta(days=ordinal),
            Decimal(quantity),
            Decimal(cost),
        )
        for ordinal, (quantity, cost) in enumerate(raw_acquisitions)
    )
    total_quantity = sum((item.quantity for item in acquisitions), Decimal())
    sold = Decimal(
        data.draw(
            st.integers(min_value=0, max_value=int(total_quantity)),
            label="sold quantity",
        )
    )
    movements = (
        (
            _movement(
                0,
                len(acquisitions),
                _START + timedelta(days=len(acquisitions) + 1),
                -sold,
                RobinhoodQuantityMovementKind.DISPOSAL,
            ),
        )
        if sold
        else ()
    )

    result = reconcile_robinhood_security_lots(
        _result(
            acquisitions,
            movements,
            {0: total_quantity - sold},
        )
    )

    assert not result.blockers
    disposal_slices = [
        item for item in result.slices if item.kind is RobinhoodLotSliceKind.DISPOSAL
    ]
    assert (
        sum((item.quantity for item in disposal_slices), Decimal())
        + sum((item.quantity for item in result.open_fragments), Decimal())
        == total_quantity
    )
    assert sum((item.cost_usd for item in disposal_slices), Decimal()) + sum(
        (item.cost_usd for item in result.open_fragments), Decimal()
    ) == sum((item.cost_usd for item in acquisitions if item.cost_usd is not None), Decimal())
    # Per lot, the exact costs sold and held add back to its cost exactly (#1241).
    exact_by_lot: defaultdict[RobinhoodSecurityEventReference, Fraction] = defaultdict(Fraction)
    for item in (*disposal_slices, *result.open_fragments):
        exact_by_lot[item.lot_id] += _exact_cost(item)
    assert exact_by_lot == {
        item.event_reference: Fraction(item.cost_usd)
        for item in acquisitions
        if item.cost_usd is not None
    }
    # Lots on distinct days are never split pro rata, so no quantity is
    # rounded and none carries a rational (#1263).
    assert all(
        getattr(item, "exact_quantity", None) is None
        for item in (*disposal_slices, *result.open_fragments)
    )


@given(
    st.lists(st.integers(min_value=1, max_value=2000), min_size=2, max_size=5),
    st.data(),
)
def test_same_day_pro_rata_property_keeps_exact_quantities(
    raw_quantities: list[int],
    data: st.DataObject,
) -> None:
    """#1263: same-day lots of 0.01 to 20.00 shares bought at $10 a share, a
    partial sale split pro rata, then maybe a sale of the rest. Per lot the
    exact quantities sold and held add back to its purchase exactly; each lot
    gives up and keeps exactly its purchase's share of each; every exact cost
    is $10 times its exact quantity; and each sale's cash splits exactly by
    its slices' exact quantities."""
    acquisitions = tuple(
        _acquisition(0, ordinal, _START, Decimal(f"{quantity}E-2"), Decimal(f"{quantity}E-1"))
        for ordinal, quantity in enumerate(raw_quantities)
    )
    purchased = {item.event_reference: Fraction(item.quantity) for item in acquisitions}
    total = sum(purchased.values(), Fraction())
    sold = data.draw(st.integers(min_value=1, max_value=sum(raw_quantities) - 1), label="sold")
    sales = [
        _movement(
            0,
            len(acquisitions),
            _START + timedelta(days=1),
            Decimal(f"-{sold}E-2"),
            RobinhoodQuantityMovementKind.DISPOSAL,
        )
    ]
    if data.draw(st.booleans(), label="sell the rest"):
        sales.append(
            _movement(
                0,
                len(acquisitions) + 1,
                _START + timedelta(days=2),
                Decimal(f"-{sum(raw_quantities) - sold}E-2"),
                RobinhoodQuantityMovementKind.DISPOSAL,
            )
        )
    held = Decimal() if len(sales) == 2 else Decimal(f"{sum(raw_quantities) - sold}E-2")

    result = reconcile_robinhood_security_lots(_result(acquisitions, tuple(sales), {0: held}))

    assert not result.blockers
    quantities, costs = _exact_by_lot(result)
    assert quantities == purchased
    assert costs == {lot_id: 10 * quantity for lot_id, quantity in purchased.items()}
    for item in (*result.slices, *result.open_fragments):
        assert _exact_cost(item) == 10 * _exact_quantity(item)
    first_part = Fraction(sold, 100) / total
    for item in _sale_slices(result, sales[0]):
        assert _exact_quantity(item) == purchased[item.lot_id] * first_part
    rest = _sale_slices(result, sales[1]) if len(sales) == 2 else result.open_fragments
    for item in rest:
        assert _exact_quantity(item) == purchased[item.lot_id] * (1 - first_part)
    cash = Decimal(f"{data.draw(st.integers(min_value=1, max_value=10**6), label='cash')}E-2")
    for sale in sales:
        slices = _sale_slices(result, sale)
        sale_quantity = sum(map(_exact_quantity, slices), Fraction())
        assert sale_quantity == -Fraction(sale.signed_quantity)
        with localcontext(Context(prec=60)):
            allocated = allocate_disposal_proceeds(
                slices, source_quantity=-sale.signed_quantity, source_cash=cash
            )
        assert [
            Fraction(proceeds) if exact is None else exact for _, proceeds, exact in allocated
        ] == [Fraction(cash) * _exact_quantity(item) / sale_quantity for item in slices]


def test_lot_models_and_failures_do_not_repr_private_values() -> None:
    acquisition = _acquisition(0, 0, _START, Decimal("1"), Decimal("9876.54"))
    sale = _movement(
        0,
        1,
        _START + timedelta(days=1),
        Decimal("-2"),
        RobinhoodQuantityMovementKind.DISPOSAL,
    )

    result = reconcile_robinhood_security_lots(_result((acquisition,), (sale,), {0: Decimal()}))

    assert _CUSIP not in repr(result)
    assert "9876.54" not in repr(result)
    assert _CUSIP not in repr(result.blockers[0])


def _lots_reconcile_with_their_sources(
    quantities: RobinhoodSecurityQuantityResult,
    result: RobinhoodSecurityLotResult,
) -> bool:
    """For each resolved security: every slice and open fragment traces to
    one of its acquisitions and keeps that acquisition's date; each
    acquisition's cost is fully shared out between its disposal slices and
    its open fragments; each account's open fragments hold the ledger's
    final quantity; and each slice draws on movements of its own side
    (sales for a disposal, outgoing transfers for a transfer)."""
    resolved = {
        item.cusip for item in (*result.slices, *result.open_fragments, *result.holding_intervals)
    }
    acquisitions = {
        item.event_reference: item for item in quantities.acquisitions if item.cusip in resolved
    }
    side = {
        RobinhoodLotSliceKind.DISPOSAL: RobinhoodQuantityMovementKind.DISPOSAL,
        RobinhoodLotSliceKind.TRANSFER: RobinhoodQuantityMovementKind.TRANSFER_OUT,
    }
    movement_by_reference = {
        reference: movement
        for movement in quantities.movements
        for reference in movement.event_references
    }
    final = {(item.account_index, item.cusip): item.quantity for item in quantities.quantities}
    lots = (*result.slices, *result.open_fragments)
    if not all(
        (acquisition := acquisitions.get(item.lot_id)) is not None
        and acquisition.cusip == item.cusip
        and item.original_date == acquisition.activity_date
        and item.quantity > 0
        for item in lots
    ):
        return False
    if not all(
        (movement := movement_by_reference.get(reference)) is not None
        and movement.kind is side[item.kind]
        and movement.account_index == item.account_index
        and movement.cusip == item.cusip
        and movement.signed_quantity < 0
        for item in result.slices
        for reference in item.movement_references
    ):
        return False
    # Cost shares are divided at 20 digits; added exactly, they rebuild the
    # source cost.
    with localcontext(Context(prec=120)):
        for reference, acquisition in acquisitions.items():
            shared = sum(
                (
                    item.cost_usd
                    for item in lots
                    if item.lot_id == reference
                    and not (
                        isinstance(item, RobinhoodLotSlice)
                        and item.kind is RobinhoodLotSliceKind.TRANSFER
                    )
                ),
                Decimal(),
            )
            if shared != acquisition.cost_usd:
                return False
        held: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
        for fragment in result.open_fragments:
            held[(fragment.account_index, fragment.cusip)] += fragment.quantity
        keys = {
            (movement.account_index, movement.cusip)
            for movement in quantities.movements
            if movement.cusip in resolved
        } | {key for key in final if key[1] in resolved}
        return all(held[key] == final.get(key, Decimal()) for key in keys)


@private_test
def test_private_security_lots_are_resolved_or_explicitly_blocked() -> None:
    paired = load_private_accounts_reconciliation()
    identities = resolve_robinhood_security_identities(paired, ())
    quantities = reconcile_robinhood_security_quantities(paired, identities)

    result = reconcile_robinhood_security_lots(quantities)

    resolved_cusips = {
        item.cusip for item in (*result.slices, *result.open_fragments, *result.holding_intervals)
    }
    blocked_cusips = {blocker.cusip for blocker in result.blockers if blocker.cusip is not None}
    if not (
        result.identity_blockers == quantities.identity_blockers
        and resolved_cusips.isdisjoint(blocked_cusips)
        and {movement.cusip for movement in quantities.movements}
        <= (resolved_cusips | blocked_cusips)
        and _lots_reconcile_with_their_sources(quantities, result)
    ):
        pytest.fail("private security lots disagree with their sources", pytrace=False)
