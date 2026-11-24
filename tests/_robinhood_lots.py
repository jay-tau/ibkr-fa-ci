"""Synthetic Robinhood quantity inputs for source-lot matching, shared by the
Robinhood lot-matching and Schedule FA compute tests (#1819).

:func:`lot_acquisition` and :func:`lot_movement` build one acquisition record or
quantity movement for :data:`LOT_CUSIP`; :func:`lot_transfer` builds a transfer's
outgoing and incoming movements with their quantity evidence; and
:func:`lot_quantity_result` builds a whole security quantity result from them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountSecurityQuantity,
    RobinhoodAcquisitionRecord,
    RobinhoodInventoryBlocker,
    RobinhoodPositionAnchor,
    RobinhoodQuantityMovement,
    RobinhoodQuantityMovementKind,
    RobinhoodQuantityResidual,
    RobinhoodSecurityEventReference,
    RobinhoodSecurityQuantityResult,
    RobinhoodTransferQuantityEvidence,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind

LOT_CUSIP = "99971T103"


def lot_event_reference(account_index: int, ordinal: int) -> RobinhoodSecurityEventReference:
    return RobinhoodSecurityEventReference(account_index, ordinal)


def lot_acquisition(
    account_index: int,
    ordinal: int,
    activity_date: date,
    quantity: Decimal,
    cost_usd: Decimal | None,
) -> RobinhoodAcquisitionRecord:
    return RobinhoodAcquisitionRecord(
        account_index=account_index,
        cusip=LOT_CUSIP,
        activity_date=activity_date,
        quantity=quantity,
        cost_usd=cost_usd,
        event_reference=lot_event_reference(account_index, ordinal),
        event_kind=(
            RobinhoodEventKind.SECURITY_RECEIPT
            if cost_usd is None
            else RobinhoodEventKind.EQUITY_BUY
        ),
    )


def lot_movement(
    account_index: int,
    ordinal: int,
    activity_date: date,
    quantity: Decimal,
    kind: RobinhoodQuantityMovementKind,
    *,
    transfer_index: int | None = None,
) -> RobinhoodQuantityMovement:
    return RobinhoodQuantityMovement(
        account_index=account_index,
        cusip=LOT_CUSIP,
        activity_date=activity_date,
        settle_date=activity_date,
        signed_quantity=quantity,
        kind=kind,
        event_references=(lot_event_reference(account_index, ordinal),),
        transfer_index=transfer_index,
    )


def lot_quantity_result(
    acquisitions: tuple[RobinhoodAcquisitionRecord, ...],
    movements: tuple[RobinhoodQuantityMovement, ...],
    final_quantities: dict[int, Decimal],
    *,
    transfers: tuple[RobinhoodTransferQuantityEvidence, ...] = (),
    residuals: tuple[RobinhoodQuantityResidual, ...] = (),
    anchors: tuple[RobinhoodPositionAnchor, ...] = (),
    blockers: tuple[RobinhoodInventoryBlocker, ...] = (),
) -> RobinhoodSecurityQuantityResult:
    acquisition_movements = tuple(
        RobinhoodQuantityMovement(
            account_index=acquisition.account_index,
            cusip=acquisition.cusip,
            activity_date=acquisition.activity_date,
            settle_date=acquisition.activity_date,
            signed_quantity=acquisition.quantity,
            kind=RobinhoodQuantityMovementKind.ACQUISITION,
            event_references=(acquisition.event_reference,),
        )
        for acquisition in acquisitions
    )
    return RobinhoodSecurityQuantityResult(
        acquisitions=acquisitions,
        movements=tuple(
            sorted(
                (*acquisition_movements, *movements),
                key=lambda movement: (
                    movement.activity_date,
                    movement.account_index,
                    movement.kind.value,
                ),
            )
        ),
        transfers=transfers,
        residuals=residuals,
        anchors=anchors,
        blockers=blockers,
        identity_blockers=(),
        quantities=tuple(
            RobinhoodAccountSecurityQuantity(
                account_index=account_index,
                cusip=LOT_CUSIP,
                quantity=quantity,
            )
            for account_index, quantity in sorted(final_quantities.items())
        ),
        option_event_references=(),
        option_position_references=(),
        neutral_event_references=(),
    )


def lot_transfer(
    source_account_index: int,
    activity_date: date,
    quantity: Decimal,
    *,
    transfer_index: int = 0,
    source_ordinal: int = 100,
    destination_ordinal: int = 101,
    tolerance: Decimal = Decimal(),
) -> tuple[
    tuple[RobinhoodQuantityMovement, RobinhoodQuantityMovement],
    RobinhoodTransferQuantityEvidence,
]:
    destination_account_index = 1 - source_account_index
    outgoing = lot_movement(
        source_account_index,
        source_ordinal,
        activity_date,
        -quantity,
        RobinhoodQuantityMovementKind.TRANSFER_OUT,
        transfer_index=transfer_index,
    )
    incoming = lot_movement(
        destination_account_index,
        destination_ordinal,
        activity_date,
        quantity,
        RobinhoodQuantityMovementKind.TRANSFER_IN,
        transfer_index=transfer_index,
    )
    return (
        (outgoing, incoming),
        RobinhoodTransferQuantityEvidence(
            source_account_index=source_account_index,
            destination_account_index=destination_account_index,
            cusip=LOT_CUSIP,
            activity_date=activity_date,
            ledger_quantity=quantity,
            destination_display_quantity=quantity,
            tolerance=tolerance,
            source_event_references=outgoing.event_references,
            destination_event_references=incoming.event_references,
        ),
    )
