"""Reconcile policy-free Robinhood security quantities and position anchors."""

from __future__ import annotations

import operator
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import (
    MAX_PREC,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum

from ibkr_fa.config import (
    RobinhoodSecurityReceiptResolution,
    RobinhoodSecurityReceiptTreatment,
)
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountReconciliation,
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import (
    RobinhoodStatementReference,
    robinhood_beyond_band,
    robinhood_display_band,
    robinhood_exact,
    robinhood_exact_sum,
    robinhood_half_ulp,
)
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityBlocker,
    RobinhoodSecurityIdentityResult,
    canonical_statement_symbols,
)
from ibkr_fa.normalize.robinhood.transfers import (
    RobinhoodTransferDirection,
)
from ibkr_fa.parser.robinhood import (
    ROBINHOOD_EQUITY_SALE_KINDS,
    ROBINHOOD_OPTION_TRADE_KINDS,
    RobinhoodEvent,
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    RobinhoodStockLoanedSecurity,
    SourceLocation,
)

_COST_ACQUISITION_KINDS = frozenset(
    {
        RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
        RobinhoodEventKind.EQUITY_BUY,
        RobinhoodEventKind.MANAGED_EQUITY_BUY,
        RobinhoodEventKind.PRIMARY_ISSUE_BUY,
    }
)
_INVENTORY_NEUTRAL_KINDS = frozenset(
    {
        RobinhoodEventKind.ACH_CANCELLATION,
        RobinhoodEventKind.ACH_DEPOSIT,
        RobinhoodEventKind.ACH_WITHDRAWAL,
        RobinhoodEventKind.ADR_FEE,
        RobinhoodEventKind.CASH_DIVIDEND,
        RobinhoodEventKind.CASH_INTEREST,
        RobinhoodEventKind.EXTERNAL_DEBIT_CARD_TRANSFER,
        RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
        RobinhoodEventKind.INSTANT_BANK_TRANSFER,
        RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,
        RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,
        RobinhoodEventKind.MANAGEMENT_FEE,
        RobinhoodEventKind.NRA_WITHHOLDING,
        RobinhoodEventKind.STOCK_LENDING_INCOME,
    }
)
_HANDLED_KINDS = (
    _COST_ACQUISITION_KINDS
    | ROBINHOOD_EQUITY_SALE_KINDS
    | ROBINHOOD_OPTION_TRADE_KINDS
    | _INVENTORY_NEUTRAL_KINDS
    | {
        RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
        RobinhoodEventKind.SECURITY_RECEIPT,
    }
)
_INDIAN_FINANCIAL_YEAR_START_MONTH = 4
_IN_KIND_QUANTITY = "in-kind transfer quantity"
_RECIPROCAL_BAND = "reciprocal transfer quantity band"
_POSITION_QUANTITY = "statement position quantity"
_SNAPSHOT = "position snapshot"
_REPLAY = "quantity replay"
_ANCHOR_TOLERANCE = "position anchor tolerance"
_CORRECTION = "transfer quantity correction"


class RobinhoodQuantityMovementKind(StrEnum):
    """Policy-free quantity movement."""

    ACQUISITION = "acquisition"
    DISPOSAL = "disposal"
    TRANSFER_IN = "transfer-in"
    TRANSFER_OUT = "transfer-out"


class RobinhoodInventoryUnprovedFact(StrEnum):
    """The exact fact a blocker withholds."""

    BASIS = "basis"
    QUANTITY = "quantity"


class RobinhoodInventoryBlockerKind(StrEnum):
    """ADR-authorized inventory blockers."""

    ANCHOR_MISMATCH = "anchor-mismatch"
    NEGATIVE_INVENTORY = "negative-inventory"
    POSITION_IDENTITY = "position-identity"
    SECURITY_RECEIPT = "security-receipt"
    SNAPSHOT_BASIS = "snapshot-basis"


class RobinhoodPositionSnapshotBasis(StrEnum):
    """Which quantity ledger one statement position snapshot represents."""

    TRADE_DATE = "trade-date"
    SETTLEMENT_DATE = "settlement-date"
    TRADE_AND_SETTLEMENT = "trade-and-settlement"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodPositionReference:
    """Resolvable position row identity in one bounded bundle."""

    account_index: int
    statement_index: int
    location: SourceLocation


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAcquisitionRecord:
    """One source acquisition without policy-dependent lot matching."""

    account_index: int
    cusip: str
    activity_date: date
    quantity: Decimal
    cost_usd: Decimal | None
    event_reference: RobinhoodSecurityEventReference
    event_kind: RobinhoodEventKind


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodQuantityMovement:
    """One signed policy-free account quantity movement."""

    account_index: int
    cusip: str
    activity_date: date
    settle_date: date
    signed_quantity: Decimal
    kind: RobinhoodQuantityMovementKind
    event_references: tuple[RobinhoodSecurityEventReference, ...]
    transfer_index: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodTransferQuantityEvidence:
    """Raw displays plus any strictly derived economic transfer quantity."""

    source_account_index: int
    destination_account_index: int
    cusip: str
    activity_date: date
    ledger_quantity: Decimal
    destination_display_quantity: Decimal
    tolerance: Decimal
    source_event_references: tuple[RobinhoodSecurityEventReference, ...]
    destination_event_references: tuple[RobinhoodSecurityEventReference, ...]
    resolved_quantity: Decimal | None = None

    @property
    def effective_quantity(self) -> Decimal:
        """Return derived economic quantity or the raw source display."""

        return self.ledger_quantity if self.resolved_quantity is None else self.resolved_quantity


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodQuantityResidual:
    """A source-display residual absorbed under a proved band."""

    account_index: int
    cusip: str
    activity_date: date
    quantity: Decimal
    event_references: tuple[RobinhoodSecurityEventReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodPositionAnchor:
    """One statement-period quantity agreement."""

    account_index: int
    cusip: str
    statement_index: int
    period_end: date
    ledger_quantity: Decimal
    statement_quantity: Decimal
    trade_date_quantity: Decimal
    settlement_date_quantity: Decimal
    snapshot_basis: RobinhoodPositionSnapshotBasis
    tolerance: Decimal
    position_references: tuple[RobinhoodPositionReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodInventoryBlocker:
    """One fact-scoped blocker represented by source handles."""

    account_index: int
    cusip: str | None
    unproved_fact: RobinhoodInventoryUnprovedFact
    kind: RobinhoodInventoryBlockerKind
    statement_index: int | None = None
    event_references: tuple[RobinhoodSecurityEventReference, ...] = ()
    position_references: tuple[RobinhoodPositionReference, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAccountSecurityQuantity:
    """One unblocked final account/security quantity."""

    account_index: int
    cusip: str
    quantity: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityQuantityResult:
    """Complete policy-free quantity evidence for #477."""

    acquisitions: tuple[RobinhoodAcquisitionRecord, ...]
    movements: tuple[RobinhoodQuantityMovement, ...]
    transfers: tuple[RobinhoodTransferQuantityEvidence, ...]
    residuals: tuple[RobinhoodQuantityResidual, ...]
    anchors: tuple[RobinhoodPositionAnchor, ...]
    blockers: tuple[RobinhoodInventoryBlocker, ...]
    identity_blockers: tuple[RobinhoodSecurityIdentityBlocker, ...]
    quantities: tuple[RobinhoodAccountSecurityQuantity, ...]
    option_event_references: tuple[RobinhoodSecurityEventReference, ...]
    option_position_references: tuple[RobinhoodPositionReference, ...]
    neutral_event_references: tuple[RobinhoodSecurityEventReference, ...]


@dataclass(frozen=True, slots=True)
class _TransferQuantityCorrection:
    transfer_index: int
    quantity: Decimal
    source_anchor: RobinhoodPositionAnchor
    destination_residual: RobinhoodQuantityResidual


type _PositionSnapshotDecision = (
    tuple[
        Decimal,
        Decimal,
        Decimal,
        RobinhoodPositionSnapshotBasis,
    ]
    | RobinhoodInventoryBlockerKind
)


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def _event_map(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[dict[int, RobinhoodEvent], dict[int, RobinhoodEvent]]:
    return (
        {event.source_record_index: event for event in paired.accounts[0].evidence.events},
        {event.source_record_index: event for event in paired.accounts[1].evidence.events},
    )


def _group_events(
    event_map: dict[int, RobinhoodEvent],
    ordinals: tuple[int, ...],
) -> tuple[RobinhoodEvent, ...]:
    try:
        events = tuple(event_map[ordinal] for ordinal in ordinals)
    except KeyError:
        raise _fail("in-kind event reference does not resolve") from None
    if len({event.cusip for event in events}) != 1 or None in {event.cusip for event in events}:
        raise _fail("in-kind event group spans multiple security identities")
    if len({event.activity_date for event in events}) != 1:
        raise _fail("in-kind event group spans multiple activity dates")
    if any(event.kind is not RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER for event in events):
        raise _fail("in-kind event group contains a non-transfer event")
    if any(event.quantity is None for event in events):
        raise _fail("in-kind event group lacks quantity")
    return events


def _sum_quantities(events: tuple[RobinhoodEvent, ...]) -> Decimal:
    return robinhood_exact_sum(
        (event.quantity for event in events if event.quantity is not None),
        label=_IN_KIND_QUANTITY,
    )


def _event_references(
    account_index: int,
    events: tuple[RobinhoodEvent, ...],
) -> tuple[RobinhoodSecurityEventReference, ...]:
    return tuple(
        RobinhoodSecurityEventReference(account_index, event.source_record_index)
        for event in events
    )


def _join_transfers(  # noqa: PLR0912, PLR0915
    paired: RobinhoodAccountsReconciliation,
    transfer_quantities: Mapping[int, Decimal],
) -> tuple[
    tuple[RobinhoodTransferQuantityEvidence, ...],
    tuple[RobinhoodQuantityMovement, ...],
    tuple[frozenset[int], frozenset[int]],
]:
    event_maps = _event_map(paired)
    counterpart_groups = {
        frozenset(group.statement_references): group
        for group in paired.accounts[1].activity.in_kind_transfer_groups
    }
    if len(counterpart_groups) != len(paired.accounts[1].activity.in_kind_transfer_groups):
        raise _fail("counterpart in-kind reference groups are duplicated")

    consumed_counterpart: set[frozenset[RobinhoodStatementReference]] = set()
    consumed_ordinals = (set(), set())
    transfer_evidence: list[RobinhoodTransferQuantityEvidence] = []
    movements: list[RobinhoodQuantityMovement] = []
    used_transfer_quantities: set[int] = set()

    for transfer_index, transfer in enumerate(paired.transfers.security_groups):
        counterpart_key = frozenset(transfer.counterpart_references)
        counterpart_group = counterpart_groups.get(counterpart_key)
        if counterpart_group is None:
            if any(counterpart_key < group_key for group_key in counterpart_groups):
                raise _fail("counterpart in-kind group spans multiple transfer directions")
            raise _fail("counterpart in-kind transfer group is absent")
        if counterpart_key in consumed_counterpart:
            raise _fail("counterpart in-kind transfer group is consumed twice")
        consumed_counterpart.add(counterpart_key)

        primary_events = _group_events(event_maps[0], transfer.event_ordinals)
        counterpart_events = _group_events(event_maps[1], counterpart_group.event_ordinals)
        if primary_events[0].cusip != counterpart_events[0].cusip:
            raise _fail("reciprocal in-kind event identities disagree")
        if primary_events[0].activity_date != counterpart_events[0].activity_date:
            raise _fail("reciprocal in-kind event dates disagree")

        if transfer.direction is RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART:
            source_index, destination_index = 0, 1
            source_events, destination_events = primary_events, counterpart_events
        else:
            source_index, destination_index = 1, 0
            source_events, destination_events = counterpart_events, primary_events

        source_quantity = _sum_quantities(source_events)
        destination_quantity = _sum_quantities(destination_events)
        tolerance = robinhood_display_band(
            event.quantity
            for event in (*source_events, *destination_events)
            if event.quantity is not None
        )
        if robinhood_beyond_band(
            source_quantity, destination_quantity, tolerance, label=_RECIPROCAL_BAND
        ):
            raise _fail("reciprocal in-kind report quantities disagree")
        resolved_quantity = transfer_quantities.get(transfer_index)
        if resolved_quantity is not None:
            source_display = source_events[0].quantity
            destination_display = destination_events[0].quantity
            if (
                len(source_events) != 1
                or len(destination_events) != 1
                or source_display is None
                or destination_display is None
                or not resolved_quantity.is_finite()
                or resolved_quantity <= 0
                or robinhood_beyond_band(
                    resolved_quantity, source_quantity, tolerance, label=_RECIPROCAL_BAND
                )
                or not _quantity_round_trips(resolved_quantity, source_display)
                or not _quantity_round_trips(resolved_quantity, destination_display)
            ):
                raise _fail("resolved transfer quantity contradicts its raw displays")
            used_transfer_quantities.add(transfer_index)
        effective_quantity = source_quantity if resolved_quantity is None else resolved_quantity

        source_references = _event_references(source_index, source_events)
        destination_references = _event_references(destination_index, destination_events)
        activity_date = source_events[0].activity_date
        cusip = source_events[0].cusip
        assert cusip is not None
        transfer_evidence.append(
            RobinhoodTransferQuantityEvidence(
                source_account_index=source_index,
                destination_account_index=destination_index,
                cusip=cusip,
                activity_date=activity_date,
                ledger_quantity=source_quantity,
                destination_display_quantity=destination_quantity,
                tolerance=tolerance,
                source_event_references=source_references,
                destination_event_references=destination_references,
                resolved_quantity=resolved_quantity,
            )
        )
        movements.extend(
            (
                RobinhoodQuantityMovement(
                    account_index=source_index,
                    cusip=cusip,
                    activity_date=activity_date,
                    settle_date=source_events[0].settle_date,
                    signed_quantity=effective_quantity.copy_negate(),
                    kind=RobinhoodQuantityMovementKind.TRANSFER_OUT,
                    event_references=source_references,
                    transfer_index=transfer_index,
                ),
                RobinhoodQuantityMovement(
                    account_index=destination_index,
                    cusip=cusip,
                    activity_date=activity_date,
                    settle_date=destination_events[0].settle_date,
                    signed_quantity=effective_quantity,
                    kind=RobinhoodQuantityMovementKind.TRANSFER_IN,
                    event_references=destination_references,
                    transfer_index=transfer_index,
                ),
            )
        )
        consumed_ordinals[source_index].update(
            reference.event_ordinal for reference in source_references
        )
        consumed_ordinals[destination_index].update(
            reference.event_ordinal for reference in destination_references
        )

    if consumed_counterpart != set(counterpart_groups):
        raise _fail("counterpart in-kind transfer groups are not consumed exactly once")
    if used_transfer_quantities != set(transfer_quantities):
        raise _fail("resolved transfer quantities do not bind exact transfer groups")
    return (
        tuple(transfer_evidence),
        tuple(movements),
        (frozenset(consumed_ordinals[0]), frozenset(consumed_ordinals[1])),
    )


def _route_events(  # noqa: PLR0912
    paired: RobinhoodAccountsReconciliation,
    consumed_transfer_ordinals: tuple[frozenset[int], frozenset[int]],
    receipt_quantities: Mapping[RobinhoodSecurityEventReference, Decimal],
) -> tuple[
    tuple[RobinhoodAcquisitionRecord, ...],
    tuple[RobinhoodQuantityMovement, ...],
    tuple[RobinhoodInventoryBlocker, ...],
    tuple[RobinhoodSecurityEventReference, ...],
    tuple[RobinhoodSecurityEventReference, ...],
]:
    if frozenset(RobinhoodEventKind) != _HANDLED_KINDS:
        raise _fail("Robinhood event taxonomy is not exhaustively routed")

    acquisitions: list[RobinhoodAcquisitionRecord] = []
    movements: list[RobinhoodQuantityMovement] = []
    blockers: list[RobinhoodInventoryBlocker] = []
    option_references: list[RobinhoodSecurityEventReference] = []
    neutral_references: list[RobinhoodSecurityEventReference] = []
    used_receipt_quantities: set[RobinhoodSecurityEventReference] = set()
    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            reference = RobinhoodSecurityEventReference(account_index, event.source_record_index)
            if event.kind is RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER:
                if event.source_record_index not in consumed_transfer_ordinals[account_index]:
                    raise _fail("in-kind event is not consumed by transfer proof")
                continue
            if event.kind in _COST_ACQUISITION_KINDS:
                if event.cusip is None or event.quantity is None or event.amount_usd is None:
                    raise _fail("acquisition event lacks quantity, cost, or identity")
                acquisitions.append(
                    RobinhoodAcquisitionRecord(
                        account_index=account_index,
                        cusip=event.cusip,
                        activity_date=event.activity_date,
                        quantity=event.quantity,
                        cost_usd=abs(event.amount_usd),
                        event_reference=reference,
                        event_kind=event.kind,
                    )
                )
                movements.append(
                    RobinhoodQuantityMovement(
                        account_index=account_index,
                        cusip=event.cusip,
                        activity_date=event.activity_date,
                        settle_date=event.settle_date,
                        signed_quantity=event.quantity,
                        kind=RobinhoodQuantityMovementKind.ACQUISITION,
                        event_references=(reference,),
                    )
                )
                continue
            if event.kind is RobinhoodEventKind.SECURITY_RECEIPT:
                if event.cusip is None or event.quantity is None:
                    raise _fail("security receipt lacks quantity or identity")
                quantity = receipt_quantities.get(reference, event.quantity)
                if not quantity.is_finite() or quantity <= 0:
                    raise _fail("security receipt exact quantity is invalid")
                if reference in receipt_quantities:
                    used_receipt_quantities.add(reference)
                acquisitions.append(
                    RobinhoodAcquisitionRecord(
                        account_index=account_index,
                        cusip=event.cusip,
                        activity_date=event.activity_date,
                        quantity=quantity,
                        cost_usd=None,
                        event_reference=reference,
                        event_kind=event.kind,
                    )
                )
                movements.append(
                    RobinhoodQuantityMovement(
                        account_index=account_index,
                        cusip=event.cusip,
                        activity_date=event.activity_date,
                        settle_date=event.settle_date,
                        signed_quantity=quantity,
                        kind=RobinhoodQuantityMovementKind.ACQUISITION,
                        event_references=(reference,),
                    )
                )
                blockers.append(
                    RobinhoodInventoryBlocker(
                        account_index=account_index,
                        cusip=event.cusip,
                        unproved_fact=RobinhoodInventoryUnprovedFact.BASIS,
                        kind=RobinhoodInventoryBlockerKind.SECURITY_RECEIPT,
                        event_references=(reference,),
                    )
                )
                continue
            if event.kind in ROBINHOOD_EQUITY_SALE_KINDS:
                if event.cusip is None or event.quantity is None:
                    raise _fail("sale event lacks quantity or identity")
                movements.append(
                    RobinhoodQuantityMovement(
                        account_index=account_index,
                        cusip=event.cusip,
                        activity_date=event.activity_date,
                        settle_date=event.settle_date,
                        signed_quantity=event.quantity.copy_negate(),
                        kind=RobinhoodQuantityMovementKind.DISPOSAL,
                        event_references=(reference,),
                    )
                )
                continue
            if event.kind in ROBINHOOD_OPTION_TRADE_KINDS:
                option_references.append(reference)
                continue
            if event.kind in _INVENTORY_NEUTRAL_KINDS:
                neutral_references.append(reference)
                continue
            raise _fail("Robinhood event kind has no inventory disposition")
    if used_receipt_quantities != set(receipt_quantities):
        raise _fail("security receipt quantity overrides do not bind exact REC rows")
    return (
        tuple(acquisitions),
        tuple(movements),
        tuple(blockers),
        tuple(option_references),
        tuple(neutral_references),
    )


def _position_reference(
    account_index: int,
    statement_index: int,
    position: RobinhoodStatementPosition,
) -> RobinhoodPositionReference:
    return RobinhoodPositionReference(
        account_index,
        statement_index,
        position.location,
    )


def _security_symbol_map(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[set[str], dict[str, str]]:
    cusips: set[str] = set()
    symbol_cusips: dict[str, set[str]] = defaultdict(set)
    for account in paired.accounts:
        for event in account.evidence.events:
            if event.cusip is None:
                continue
            cusips.add(event.cusip)
            if event.instrument is not None:
                symbol_cusips[event.instrument].add(event.cusip)
    for cusip, symbols in canonical_statement_symbols(paired).items():
        cusips.add(cusip)
        for symbol in symbols:
            symbol_cusips[symbol].add(cusip)
    if any(len(values) > 1 for values in symbol_cusips.values()):
        raise _fail("one position symbol resolves to multiple CUSIPs")
    return cusips, {symbol: next(iter(values)) for symbol, values in symbol_cusips.items()}


def _position_cusip(
    position: RobinhoodStatementPosition,
    cusips: set[str],
    symbol_map: dict[str, str],
) -> str | None:
    if position.symbol_or_cusip in cusips:
        return position.symbol_or_cusip
    return symbol_map.get(position.symbol_or_cusip)


def _option_position(position: RobinhoodStatementPosition) -> bool:
    values = (
        position.option_expiration,
        position.option_right,
        position.option_strike_usd,
    )
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise _fail("statement position has partial option identity")
    return all(value is not None for value in values)


def _refuse_negative_equity_rows(
    statement: RobinhoodStatement,
    account_index: int,
    statement_index: int,
) -> None:
    """Refuse a negative equity position or Stock Lending row before any sum.

    A sum would net it against a positive row of the same security. A row
    with any option field keeps its option routing and refusals.
    """
    rows = (
        *(
            (position.location, "Qty", position.quantity)
            for position in statement.positions
            if position.option_expiration is None
            and position.option_right is None
            and position.option_strike_usd is None
        ),
        *(
            (loan.location, "Shares Lent", loan.shares_lent)
            for loan in statement.stock_loaned_securities
        ),
    )
    for location, column, quantity in rows:
        if quantity < 0:
            raise _fail(
                f"account_index={account_index}, statement_index={statement_index}, "
                f"section '{location.section.value}', page {location.page}, "
                f"row {location.row}, column '{column}': "
                "negative equity positions are unsupported"
            )


def _loaned_sum(values: Iterable[Decimal]) -> Decimal:
    return robinhood_exact_sum(values, label="loaned securities partition")


def _prove_loaned_partition(statement: RobinhoodStatement) -> None:
    loan_positions = [
        position
        for position in statement.positions
        if position.location.section is RobinhoodStatementSection.LOANED_SECURITIES
    ]
    parsed_loans = list(statement.stock_loaned_securities)
    by_symbol_position: dict[str, list[RobinhoodStatementPosition]] = defaultdict(list)
    by_symbol_loan: dict[str, list[RobinhoodStockLoanedSecurity]] = defaultdict(list)
    for position in loan_positions:
        by_symbol_position[position.symbol_or_cusip].append(position)
    for loan in parsed_loans:
        by_symbol_loan[loan.symbol].append(loan)
    if set(by_symbol_position) != set(by_symbol_loan):
        raise _fail("loaned security source sections disagree")
    for symbol, positions in by_symbol_position.items():
        loans = by_symbol_loan[symbol]
        if _loaned_sum(position.quantity for position in positions) != _loaned_sum(
            loan.shares_lent for loan in loans
        ) or _loaned_sum(position.market_value_usd for position in positions) != _loaned_sum(
            loan.market_value_usd for loan in loans
        ):
            raise _fail("loaned security quantities or values disagree")

    position_value = robinhood_exact_sum(
        (position.market_value_usd for position in statement.positions),
        label="statement securities total",
    )
    if position_value != statement.account_summary.closing_total_securities_usd:
        raise _fail("statement position sections do not reconcile to securities total")


def _statement_positions(
    statement: RobinhoodStatement,
    account_index: int,
    statement_index: int,
    cusips: set[str],
    symbol_map: dict[str, str],
) -> tuple[
    dict[str, Decimal],
    dict[str, tuple[RobinhoodPositionReference, ...]],
    tuple[RobinhoodPositionReference, ...],
    tuple[RobinhoodInventoryBlocker, ...],
]:
    _refuse_negative_equity_rows(statement, account_index, statement_index)
    _prove_loaned_partition(statement)
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    references: dict[str, list[RobinhoodPositionReference]] = defaultdict(list)
    options: list[RobinhoodPositionReference] = []
    blockers: list[RobinhoodInventoryBlocker] = []
    for position in statement.positions:
        reference = _position_reference(account_index, statement_index, position)
        if _option_position(position):
            options.append(reference)
            continue
        cusip = _position_cusip(position, cusips, symbol_map)
        if cusip is None:
            blockers.append(
                RobinhoodInventoryBlocker(
                    account_index=account_index,
                    cusip=None,
                    unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
                    kind=RobinhoodInventoryBlockerKind.POSITION_IDENTITY,
                    statement_index=statement_index,
                    position_references=(reference,),
                )
            )
            continue
        quantities[cusip] = robinhood_exact(
            operator.add, quantities[cusip], position.quantity, label=_POSITION_QUANTITY
        )
        references[cusip].append(reference)
    return (
        quantities,
        {key: tuple(value) for key, value in references.items()},
        tuple(options),
        tuple(blockers),
    )


def _daily_movements(
    movements: tuple[RobinhoodQuantityMovement, ...],
    *,
    settled: bool,
) -> dict[date, list[RobinhoodQuantityMovement]]:
    result: dict[date, list[RobinhoodQuantityMovement]] = defaultdict(list)
    for movement in movements:
        result[movement.settle_date if settled else movement.activity_date].append(movement)
    return result


def _pending_trade_ordinals(
    account: RobinhoodAccountReconciliation,
    statement_index: int,
) -> frozenset[int]:
    return frozenset(
        event_ordinal
        for group in account.activity.pending_trade_groups
        if any(
            reference.statement_index == statement_index for reference in group.statement_references
        )
        for event_ordinal in group.event_ordinals
    )


def _pending_quantity_delta(
    movements: tuple[RobinhoodQuantityMovement, ...],
    *,
    cusip: str,
    cutoff: date,
    pending_ordinals: frozenset[int],
) -> Decimal | None:
    pending = tuple(
        movement
        for movement in movements
        if movement.cusip == cusip and movement.activity_date <= cutoff < movement.settle_date
    )
    if not pending or any(
        movement.kind
        not in {
            RobinhoodQuantityMovementKind.ACQUISITION,
            RobinhoodQuantityMovementKind.DISPOSAL,
        }
        or not movement.event_references
        or any(
            reference.event_ordinal not in pending_ordinals
            for reference in movement.event_references
        )
        for movement in pending
    ):
        return None
    return robinhood_exact_sum(
        (movement.signed_quantity for movement in pending), label="pending quantity delta"
    )


def _resolve_position_snapshot(
    *,
    trade_quantity: Decimal,
    settlement_quantity: Decimal,
    statement_quantity: Decimal,
    tolerance: Decimal,
    pending_delta: Decimal | None,
) -> _PositionSnapshotDecision:
    trade_matches = (
        robinhood_exact(
            operator.sub, trade_quantity, statement_quantity, label=_SNAPSHOT
        ).copy_abs()
        <= tolerance
    )
    settlement_matches = (
        robinhood_exact(
            operator.sub, settlement_quantity, statement_quantity, label=_SNAPSHOT
        ).copy_abs()
        <= tolerance
    )
    if trade_quantity == settlement_quantity:
        if not trade_matches:
            return RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH
        return (
            trade_quantity,
            statement_quantity,
            statement_quantity,
            RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT,
        )
    if (
        pending_delta is None
        or robinhood_exact(operator.sub, trade_quantity, settlement_quantity, label=_SNAPSHOT)
        != pending_delta
        or trade_matches == settlement_matches
    ):
        return RobinhoodInventoryBlockerKind.SNAPSHOT_BASIS
    if trade_matches:
        return (
            trade_quantity,
            statement_quantity,
            robinhood_exact(operator.sub, statement_quantity, pending_delta, label=_SNAPSHOT),
            RobinhoodPositionSnapshotBasis.TRADE_DATE,
        )
    return (
        settlement_quantity,
        robinhood_exact(operator.add, statement_quantity, pending_delta, label=_SNAPSHOT),
        statement_quantity,
        RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE,
    )


def _apply_through(
    daily: dict[date, list[RobinhoodQuantityMovement]],
    cutoff: date,
    applied: set[date],
    quantities: dict[str, Decimal],
    residual_budget: dict[str, Decimal],
    transfers: tuple[RobinhoodTransferQuantityEvidence, ...],
    account_index: int,
    residuals: list[RobinhoodQuantityResidual],
    blockers: list[RobinhoodInventoryBlocker],
) -> None:
    for movement_date in sorted(day for day in daily if day <= cutoff and day not in applied):
        by_cusip: dict[str, list[RobinhoodQuantityMovement]] = defaultdict(list)
        for movement in daily[movement_date]:
            by_cusip[movement.cusip].append(movement)
        for cusip, dated_movements in by_cusip.items():
            delta = robinhood_exact_sum(
                (movement.signed_quantity for movement in dated_movements),
                label=_REPLAY,
            )
            allowance = robinhood_exact(
                operator.add,
                residual_budget[cusip],
                robinhood_exact_sum(
                    (
                        transfers[movement.transfer_index].tolerance
                        for movement in dated_movements
                        if movement.kind is RobinhoodQuantityMovementKind.TRANSFER_OUT
                        and movement.transfer_index is not None
                    ),
                    label=_REPLAY,
                ),
                label=_REPLAY,
            )
            updated = robinhood_exact(operator.add, quantities[cusip], delta, label=_REPLAY)
            if updated < 0:
                deficit = updated.copy_negate()
                if deficit <= allowance:
                    event_references = tuple(
                        sorted(
                            {
                                reference
                                for movement in dated_movements
                                for reference in movement.event_references
                            }
                        )
                    )
                    residuals.append(
                        RobinhoodQuantityResidual(
                            account_index=account_index,
                            cusip=cusip,
                            activity_date=max(
                                movement.activity_date for movement in dated_movements
                            ),
                            quantity=deficit,
                            event_references=event_references,
                        )
                    )
                    residual_budget[cusip] = max(
                        Decimal(),
                        robinhood_exact(
                            operator.sub, residual_budget[cusip], deficit, label=_REPLAY
                        ),
                    )
                    updated = Decimal()
                else:
                    blockers.append(
                        RobinhoodInventoryBlocker(
                            account_index=account_index,
                            cusip=cusip,
                            unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
                            kind=RobinhoodInventoryBlockerKind.NEGATIVE_INVENTORY,
                            event_references=tuple(
                                reference
                                for movement in dated_movements
                                for reference in movement.event_references
                            ),
                        )
                    )
                    updated = Decimal()
            quantities[cusip] = updated
            for movement in dated_movements:
                if (
                    movement.kind is RobinhoodQuantityMovementKind.TRANSFER_IN
                    and movement.transfer_index is not None
                ):
                    transfer = transfers[movement.transfer_index]
                    residual_budget[cusip] = robinhood_exact(
                        operator.add, residual_budget[cusip], transfer.tolerance, label=_REPLAY
                    )
        applied.add(movement_date)


def reconcile_robinhood_security_quantities(
    paired: RobinhoodAccountsReconciliation,
    identities: RobinhoodSecurityIdentityResult,
    *,
    receipt_quantities: Mapping[RobinhoodSecurityEventReference, Decimal] | None = None,
    transfer_quantities: Mapping[int, Decimal] | None = None,
) -> RobinhoodSecurityQuantityResult:
    """Build exact policy-free account quantities and statement anchors."""

    transfers, transfer_movements, consumed_transfers = _join_transfers(
        paired,
        transfer_quantities or {},
    )
    (
        acquisitions,
        event_movements,
        initial_blockers,
        option_event_references,
        neutral_event_references,
    ) = _route_events(paired, consumed_transfers, receipt_quantities or {})
    movements = tuple(
        sorted(
            (*event_movements, *transfer_movements),
            key=lambda movement: (
                movement.activity_date,
                movement.account_index,
                movement.cusip,
                movement.kind.value,
            ),
        )
    )
    cusips, symbol_map = _security_symbol_map(paired)
    blockers = list(initial_blockers)
    anchors: list[RobinhoodPositionAnchor] = []
    residuals: list[RobinhoodQuantityResidual] = []
    option_positions: list[RobinhoodPositionReference] = []
    final_quantities: list[RobinhoodAccountSecurityQuantity] = []

    for account_index, account in enumerate(paired.accounts):
        account_movements = tuple(
            movement for movement in movements if movement.account_index == account_index
        )
        trade_daily = _daily_movements(account_movements, settled=False)
        settled_daily = _daily_movements(account_movements, settled=True)
        trade_quantities: dict[str, Decimal] = defaultdict(Decimal)
        settled_quantities: dict[str, Decimal] = defaultdict(Decimal)
        trade_budget: dict[str, Decimal] = defaultdict(Decimal)
        settled_budget: dict[str, Decimal] = defaultdict(Decimal)
        trade_applied: set[date] = set()
        settled_applied: set[date] = set()

        for statement_index, statement in enumerate(account.activity.primary_bundle.statements):
            _apply_through(
                trade_daily,
                statement.period_end,
                trade_applied,
                trade_quantities,
                trade_budget,
                transfers,
                account_index,
                residuals,
                blockers,
            )
            _apply_through(
                settled_daily,
                statement.period_end,
                settled_applied,
                settled_quantities,
                settled_budget,
                transfers,
                account_index,
                residuals,
                blockers,
            )
            (
                statement_quantities,
                position_references,
                statement_options,
                position_blockers,
            ) = _statement_positions(
                statement,
                account_index,
                statement_index,
                cusips,
                symbol_map,
            )
            option_positions.extend(statement_options)
            blockers.extend(position_blockers)

            keys = set(trade_quantities) | set(settled_quantities) | set(statement_quantities)
            for cusip in sorted(keys):
                statement_quantity = statement_quantities.get(cusip, Decimal())
                movement_tolerances = [
                    robinhood_half_ulp(movement.signed_quantity.copy_abs())
                    for movement in account_movements
                    if movement.cusip == cusip and movement.activity_date <= statement.period_end
                ]
                display_tolerances = list(movement_tolerances)
                if statement_quantity != 0:
                    display_tolerances.append(robinhood_half_ulp(statement_quantity))
                tolerance = robinhood_exact(
                    operator.add,
                    max(display_tolerances, default=Decimal()),
                    max(trade_budget[cusip], settled_budget[cusip]),
                    label=_ANCHOR_TOLERANCE,
                )
                trade_quantity = trade_quantities[cusip]
                settlement_quantity = settled_quantities[cusip]
                decision = _resolve_position_snapshot(
                    trade_quantity=trade_quantity,
                    settlement_quantity=settlement_quantity,
                    statement_quantity=statement_quantity,
                    tolerance=tolerance,
                    pending_delta=(
                        _pending_quantity_delta(
                            account_movements,
                            cusip=cusip,
                            cutoff=statement.period_end,
                            pending_ordinals=_pending_trade_ordinals(
                                account,
                                statement_index,
                            ),
                        )
                        if trade_quantity != settlement_quantity
                        else None
                    ),
                )
                if isinstance(decision, RobinhoodInventoryBlockerKind):
                    blockers.append(
                        RobinhoodInventoryBlocker(
                            account_index=account_index,
                            cusip=cusip,
                            unproved_fact=RobinhoodInventoryUnprovedFact.QUANTITY,
                            kind=decision,
                            statement_index=statement_index,
                            position_references=position_references.get(cusip, ()),
                        )
                    )
                    continue
                (
                    ledger_quantity,
                    anchored_trade,
                    anchored_settlement,
                    basis,
                ) = decision
                anchors.append(
                    RobinhoodPositionAnchor(
                        account_index=account_index,
                        cusip=cusip,
                        statement_index=statement_index,
                        period_end=statement.period_end,
                        ledger_quantity=ledger_quantity,
                        statement_quantity=statement_quantity,
                        trade_date_quantity=anchored_trade,
                        settlement_date_quantity=anchored_settlement,
                        snapshot_basis=basis,
                        tolerance=tolerance,
                        position_references=position_references.get(cusip, ()),
                    )
                )
                trade_quantities[cusip] = anchored_trade
                settled_quantities[cusip] = anchored_settlement
                trade_budget[cusip] = Decimal()
                settled_budget[cusip] = Decimal()

        quantity_blocked = {
            blocker.cusip
            for blocker in blockers
            if blocker.account_index == account_index
            and blocker.cusip is not None
            and blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        }
        for cusip, quantity in trade_quantities.items():
            if cusip not in quantity_blocked:
                final_quantities.append(
                    RobinhoodAccountSecurityQuantity(
                        account_index=account_index,
                        cusip=cusip,
                        quantity=quantity,
                    )
                )

    unique_residuals = {
        (
            residual.account_index,
            residual.cusip,
            residual.event_references,
            residual.quantity,
        ): residual
        for residual in residuals
    }
    unique_blockers = tuple(dict.fromkeys(blockers))
    return RobinhoodSecurityQuantityResult(
        acquisitions=tuple(acquisitions),
        movements=movements,
        transfers=transfers,
        residuals=tuple(unique_residuals[key] for key in sorted(unique_residuals)),
        anchors=tuple(anchors),
        blockers=unique_blockers,
        identity_blockers=identities.blockers,
        quantities=tuple(final_quantities),
        option_event_references=option_event_references,
        option_position_references=tuple(option_positions),
        neutral_event_references=neutral_event_references,
    )


def _receipt_resolution_prompt(blockers: tuple[RobinhoodInventoryBlocker, ...]) -> str:
    """The config blocks to fill in for ``blockers``, naming each REC row by
    its internal references only: never its quantity or date (#1044)."""
    blocks: list[str] = []
    for blocker in blockers:
        (reference,) = blocker.event_references
        blocks.append(
            "\n".join(
                (
                    (
                        f"# REC account_index={reference.account_index}, "
                        f"source_record_index={reference.event_ordinal}"
                    ),
                    "[[robinhood.security_receipt_resolutions]]",
                    'account_number = "REPLACE_WITH_ACCOUNT_NUMBER"',
                    f"source_record_index = {reference.event_ordinal}",
                    'cusip = "REPLACE_WITH_REC_CUSIP"',
                    'treatment = "promotional-reward"',
                    'exact_quantity = "REPLACE_WITH_DOCUMENTED_QUANTITY"',
                    'reward_fair_market_value_usd = "REPLACE_WITH_DOCUMENTED_VALUE"',
                    (
                        'income_reported_financial_year = "REPLACE_WITH_FINANCIAL_YEAR_START_YEAR"'
                        "  # replace with the bare FY start year, without quotes"
                    ),
                    "income_reported = true",
                    'source = "Robinhood reward confirmation and filed Indian return/workpaper"',
                    'reason = "Promotional stock reward reported as income at documented FMV"',
                )
            )
        )
    return (
        "Robinhood REC quantity and cost basis are unresolved. Add one completed config block "
        "for every listed source row; do not set income_reported=true until the "
        "reward FMV is included in the applicable Indian return/workpaper:\n\n"
        + "\n\n".join(blocks)
    )


def _quantity_round_trips(exact: Decimal, displayed: Decimal) -> bool:
    exact_exponent = exact.as_tuple().exponent
    displayed_exponent = displayed.as_tuple().exponent
    # A coarser spelling must be equal even if scaleb underflows the display quantum.
    if (
        not isinstance(exact_exponent, int)
        or not isinstance(displayed_exponent, int)
        or (exact_exponent > displayed_exponent and exact != displayed)
    ):
        return False
    try:
        quantum = Decimal(1).scaleb(displayed_exponent)
        # Rounding to the display quantum is the check, so only the context's
        # precision is lifted; the quantum keeps its exponent limits (#1308).
        with localcontext(prec=MAX_PREC):
            half_even = exact.quantize(quantum, rounding=ROUND_HALF_EVEN)
            half_up = exact.quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, Overflow):
        return False
    return half_even == displayed and half_up == displayed


def _derive_transfer_quantity_corrections(
    result: RobinhoodSecurityQuantityResult,
) -> tuple[_TransferQuantityCorrection, ...]:
    movement_by_reference = {
        reference: movement
        for movement in result.movements
        for reference in movement.event_references
    }
    candidates: list[_TransferQuantityCorrection] = []
    for transfer_index, transfer in enumerate(result.transfers):
        if (
            transfer.resolved_quantity is not None
            or len(transfer.source_event_references) != 1
            or len(transfer.destination_event_references) != 1
        ):
            continue
        source_anchors = sorted(
            (
                anchor
                for anchor in result.anchors
                if anchor.account_index == transfer.source_account_index
                and anchor.cusip == transfer.cusip
            ),
            key=lambda anchor: anchor.period_end,
        )
        following = [
            anchor for anchor in source_anchors if transfer.activity_date <= anchor.period_end
        ]
        if not following:
            continue
        anchor = following[0]
        previous_end = max(
            (
                item.period_end
                for item in source_anchors
                if item.period_end < transfer.activity_date
            ),
            default=date.min,
        )
        drift = robinhood_exact(
            operator.sub, anchor.ledger_quantity, anchor.statement_quantity, label=_CORRECTION
        )
        exact_quantity = robinhood_exact(
            operator.add, transfer.ledger_quantity, drift, label=_CORRECTION
        )
        if (
            drift <= 0
            or drift > transfer.tolerance
            or drift <= robinhood_half_ulp(anchor.statement_quantity)
            or not _quantity_round_trips(exact_quantity, transfer.ledger_quantity)
            or not _quantity_round_trips(
                exact_quantity,
                transfer.destination_display_quantity,
            )
        ):
            continue
        transfer_references = frozenset(
            (*transfer.source_event_references, *transfer.destination_event_references)
        )
        other_movements = tuple(
            movement
            for movement in result.movements
            if movement.cusip == transfer.cusip
            and movement.account_index
            in {transfer.source_account_index, transfer.destination_account_index}
            and previous_end < movement.activity_date <= anchor.period_end
            and transfer_references.isdisjoint(movement.event_references)
        )
        if any(
            drift <= robinhood_half_ulp(movement.signed_quantity.copy_abs())
            for movement in other_movements
        ):
            continue
        if any(
            movement.account_index == transfer.destination_account_index
            and movement.cusip == transfer.cusip
            and movement.activity_date < transfer.activity_date
            for movement in result.movements
        ) or any(
            item.account_index == transfer.destination_account_index
            and item.cusip == transfer.cusip
            and item.period_end < transfer.activity_date
            and item.statement_quantity != 0
            for item in result.anchors
        ):
            continue
        residuals = tuple(
            residual
            for residual in result.residuals
            if residual.account_index == transfer.destination_account_index
            and residual.cusip == transfer.cusip
            and residual.quantity == drift
            and transfer.activity_date <= residual.activity_date <= anchor.period_end
        )
        if len(residuals) != 1:
            continue
        residual = residuals[0]
        residual_movements = {
            movement_by_reference.get(reference) for reference in residual.event_references
        }
        if None in residual_movements or len(residual_movements) != 1:
            continue
        (disposal,) = residual_movements
        assert disposal is not None
        if (
            disposal.kind is not RobinhoodQuantityMovementKind.DISPOSAL
            or disposal.account_index != transfer.destination_account_index
            or disposal.cusip != transfer.cusip
            or disposal.activity_date != residual.activity_date
            or disposal.event_references != residual.event_references
            or disposal.signed_quantity.copy_abs() != exact_quantity
        ):
            continue
        allowed_destination_references = frozenset(
            (*transfer.destination_event_references, *residual.event_references)
        )
        if any(
            movement.account_index == transfer.destination_account_index
            and movement.cusip == transfer.cusip
            and transfer.activity_date <= movement.activity_date <= residual.activity_date
            and not frozenset(movement.event_references) <= allowed_destination_references
            for movement in result.movements
        ):
            continue
        candidates.append(
            _TransferQuantityCorrection(
                transfer_index,
                exact_quantity,
                anchor,
                residual,
            )
        )

    if (
        len({item.transfer_index for item in candidates}) != len(candidates)
        or len({item.source_anchor for item in candidates}) != len(candidates)
        or len({item.destination_residual for item in candidates}) != len(candidates)
    ):
        return ()
    return tuple(candidates)


def _transfer_corrections_hold(
    first: RobinhoodSecurityQuantityResult,
    second: RobinhoodSecurityQuantityResult,
    corrections: tuple[_TransferQuantityCorrection, ...],
) -> bool:
    correction_by_index = {item.transfer_index: item for item in corrections}
    expected_transfers = tuple(
        (
            replace(transfer, resolved_quantity=correction_by_index[index].quantity)
            if index in correction_by_index
            else transfer
        )
        for index, transfer in enumerate(first.transfers)
    )
    expected_movements = list(first.movements)
    for correction in corrections:
        transfer = first.transfers[correction.transfer_index]
        for index, movement in enumerate(expected_movements):
            if movement.event_references == transfer.source_event_references:
                expected_movements[index] = replace(
                    movement,
                    signed_quantity=correction.quantity.copy_negate(),
                )
            elif movement.event_references == transfer.destination_event_references:
                expected_movements[index] = replace(
                    movement,
                    signed_quantity=correction.quantity,
                )
    expected_residuals = tuple(
        residual
        for residual in first.residuals
        if residual not in {item.destination_residual for item in corrections}
    )
    first_anchor_by_key = {
        (anchor.account_index, anchor.cusip, anchor.statement_index): anchor
        for anchor in first.anchors
    }
    second_anchor_by_key = {
        (anchor.account_index, anchor.cusip, anchor.statement_index): anchor
        for anchor in second.anchors
    }
    corrected_anchor_keys = {
        (
            item.source_anchor.account_index,
            item.source_anchor.cusip,
            item.source_anchor.statement_index,
        )
        for item in corrections
    }
    tolerance_only_scopes = tuple(
        (
            first.transfers[item.transfer_index].source_account_index,
            first.transfers[item.transfer_index].destination_account_index,
            first.transfers[item.transfer_index].cusip,
            first.transfers[item.transfer_index].activity_date,
        )
        for item in corrections
    )
    if set(first_anchor_by_key) != set(second_anchor_by_key):
        return False
    for key, before in first_anchor_by_key.items():
        after = second_anchor_by_key[key]
        if key not in corrected_anchor_keys:
            tolerance_only = any(
                after.account_index in {source_index, destination_index}
                and after.cusip == cusip
                and transfer_date <= after.period_end
                for source_index, destination_index, cusip, transfer_date in tolerance_only_scopes
            )
            if after != before and (
                not tolerance_only
                or after.tolerance > before.tolerance
                or replace(after, tolerance=before.tolerance) != before
            ):
                return False
            continue
        if (
            after.ledger_quantity != after.statement_quantity
            or replace(
                after,
                ledger_quantity=before.ledger_quantity,
                tolerance=before.tolerance,
            )
            != before
            or after.tolerance > before.tolerance
        ):
            return False
    if (
        second.transfers != expected_transfers
        or second.movements != tuple(expected_movements)
        or second.residuals != expected_residuals
        or second.quantities != first.quantities
        or not set(second.blockers) <= set(first.blockers)
    ):
        return False
    return (
        replace(
            second,
            transfers=first.transfers,
            movements=first.movements,
            residuals=first.residuals,
            anchors=first.anchors,
            blockers=first.blockers,
        )
        == first
    )


def resolve_robinhood_security_quantities(
    paired: RobinhoodAccountsReconciliation,
    identities: RobinhoodSecurityIdentityResult,
    resolutions: tuple[RobinhoodSecurityReceiptResolution, ...],
) -> RobinhoodSecurityQuantityResult:
    """Reconcile exact promotional-reward quantity and basis evidence."""

    events = _event_map(paired)
    account_indices = {
        account.activity.primary_bundle.account_number: index
        for index, account in enumerate(paired.accounts)
    }
    by_reference: dict[
        RobinhoodSecurityEventReference,
        RobinhoodSecurityReceiptResolution,
    ] = {}
    for index, resolution in enumerate(resolutions):
        account_index = account_indices.get(resolution.account_number)
        if account_index is None:
            raise _fail(f"security receipt resolution {index} names an unknown account")
        event = events[account_index].get(resolution.source_record_index)
        if (
            event is None
            or event.kind is not RobinhoodEventKind.SECURITY_RECEIPT
            or event.cusip != resolution.cusip
            or event.quantity is None
        ):
            raise _fail(
                f"security receipt resolution {index} does not match one exact REC source row"
            )
        expected_financial_year = (
            event.activity_date.year
            if event.activity_date.month >= _INDIAN_FINANCIAL_YEAR_START_MONTH
            else event.activity_date.year - 1
        )
        if (
            resolution.treatment is not RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD
            or not resolution.income_reported
            or resolution.income_reported_financial_year != expected_financial_year
            or not resolution.exact_quantity.is_finite()
            or resolution.exact_quantity <= 0
            or not _quantity_round_trips(resolution.exact_quantity, event.quantity)
            or not resolution.reward_fair_market_value_usd.is_finite()
            or resolution.reward_fair_market_value_usd <= 0
        ):
            raise _fail(f"security receipt resolution {index} contradicts the REC income treatment")
        reference = RobinhoodSecurityEventReference(account_index, event.source_record_index)
        if reference in by_reference:
            raise _fail("security receipt resolutions do not bind unique REC rows")
        by_reference[reference] = resolution

    receipt_quantities = {
        reference: resolution.exact_quantity for reference, resolution in by_reference.items()
    }
    quantities = reconcile_robinhood_security_quantities(
        paired,
        identities,
        receipt_quantities=receipt_quantities,
    )
    corrections = _derive_transfer_quantity_corrections(quantities)
    if corrections:
        corrected = reconcile_robinhood_security_quantities(
            paired,
            identities,
            receipt_quantities=receipt_quantities,
            transfer_quantities={item.transfer_index: item.quantity for item in corrections},
        )
        if _transfer_corrections_hold(quantities, corrected, corrections):
            quantities = corrected
    acquisitions = tuple(
        (
            replace(
                acquisition,
                cost_usd=by_reference[acquisition.event_reference].reward_fair_market_value_usd,
            )
            if acquisition.event_kind is RobinhoodEventKind.SECURITY_RECEIPT
            and acquisition.event_reference in by_reference
            else acquisition
        )
        for acquisition in quantities.acquisitions
    )
    blockers = tuple(
        blocker
        for blocker in quantities.blockers
        if not (
            blocker.kind is RobinhoodInventoryBlockerKind.SECURITY_RECEIPT
            and blocker.event_references[0] in by_reference
        )
    )
    unresolved = tuple(
        blocker
        for blocker in blockers
        if blocker.kind is RobinhoodInventoryBlockerKind.SECURITY_RECEIPT
    )
    if unresolved:
        raise RobinhoodReconciliationError(
            _receipt_resolution_prompt(unresolved), receipt_resolution=True
        )
    return replace(quantities, acquisitions=acquisitions, blockers=blockers)
