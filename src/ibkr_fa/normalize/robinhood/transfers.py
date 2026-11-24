"""Prove reciprocal Robinhood-to-Robinhood brokerage transfers."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.normalize.robinhood.activity import (
    RobinhoodActivityGroup,
    RobinhoodInKindTransferGroup,
    RobinhoodStatementReference,
    robinhood_beyond_band,
    robinhood_display_band,
    robinhood_exact_sum,
    robinhood_statement_signed_amount,
)
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
    RobinhoodStatementBundle,
)
from ibkr_fa.normalize.robinhood.cash import (
    RobinhoodCashReconciliation,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementActivity,
    RobinhoodStatementSection,
)

_SECURITY_TRANSFER_BAND = "security transfer aggregate quantity"


class RobinhoodTransferDirection(StrEnum):
    """Direction relative to the primary account."""

    PRIMARY_TO_COUNTERPART = "primary-to-counterpart"
    COUNTERPART_TO_PRIMARY = "counterpart-to-primary"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCashTransferPair:
    """One unique exact-opposite cash ITRF pair."""

    event_ordinal: int
    primary_reference: RobinhoodStatementReference
    counterpart_reference: RobinhoodStatementReference
    direction: RobinhoodTransferDirection


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecurityTransferGroup:
    """One reciprocal aggregate security ITRF group."""

    event_ordinals: tuple[int, ...]
    primary_references: tuple[RobinhoodStatementReference, ...]
    counterpart_references: tuple[RobinhoodStatementReference, ...]
    direction: RobinhoodTransferDirection


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodTransferReconciliation:
    """Complete #461 evidence without lot/basis or tax processing."""

    cash_reconciliation: RobinhoodCashReconciliation
    counterpart_bundle: RobinhoodStatementBundle
    cash_pairs: tuple[RobinhoodCashTransferPair, ...]
    security_groups: tuple[RobinhoodSecurityTransferGroup, ...]


@dataclass(frozen=True, slots=True)
class _TransferEvidence:
    reference: RobinhoodStatementReference
    row: RobinhoodStatementActivity


type _CashKey = tuple[date, Decimal]
type _SecurityKey = tuple[date, str, str, str]

_CASH_EVENT_KINDS = frozenset(
    {
        RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,
        RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,
    }
)


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def _transfer_rows(
    bundle: RobinhoodStatementBundle,
) -> tuple[_TransferEvidence, ...]:
    result: list[_TransferEvidence] = []
    seen: set[RobinhoodStatementReference] = set()
    for statement_index, statement in enumerate(bundle.statements):
        for row in statement.activities:
            if (
                row.location.section is not RobinhoodStatementSection.ACCOUNT_ACTIVITY
                or row.transaction_code != "ITRF"
            ):
                continue
            reference = RobinhoodStatementReference(statement_index, row.location)
            if reference in seen:
                raise _fail("transfer statement reference is duplicated")
            seen.add(reference)
            result.append(_TransferEvidence(reference, row))
    return tuple(result)


def _classify_transfer(
    evidence: _TransferEvidence,
) -> tuple[str, Decimal | None]:
    row = evidence.row
    if row.settle_date is not None:
        raise _fail("ITRF statement row unexpectedly carries settle date")
    amount = robinhood_statement_signed_amount(row)
    if amount is not None:
        if (
            amount == 0
            or row.quantity is not None
            or row.price_usd is not None
            or row.source_account_reference is not None
            or row.destination_account_reference is not None
        ):
            raise _fail("cash ITRF row has an unsupported shape")
        return "cash", amount
    if (
        row.quantity is None
        or row.quantity <= 0
        or row.price_usd is not None
        or row.symbol is None
        or row.source_account_reference is None
        or row.destination_account_reference is None
    ):
        raise _fail("security ITRF row has an unsupported shape")
    return "security", None


def _coverage_contains(
    bundle: RobinhoodStatementBundle,
    transfer_date: date,
) -> bool:
    return bundle.coverage_start <= transfer_date <= bundle.coverage_end


def _cash_group_map(
    cash: RobinhoodCashReconciliation,
) -> dict[RobinhoodStatementReference, RobinhoodActivityGroup]:
    result: dict[RobinhoodStatementReference, RobinhoodActivityGroup] = {}
    for group in cash.activity_reconciliation.activity_groups:
        if group.transaction_code != "ITRF":
            continue
        if not group.event_kinds or not set(group.event_kinds) <= _CASH_EVENT_KINDS:
            raise _fail("primary cash ITRF group has unsupported event kinds")
        if len(group.event_ordinals) != 1 or len(group.statement_references) != 1:
            raise _fail("primary cash ITRF group is not uniquely paired")
        reference = group.statement_references[0]
        if reference in result:
            raise _fail("primary cash ITRF reference is duplicated")
        result[reference] = group
    return result


def _in_kind_group_map(
    cash: RobinhoodCashReconciliation,
) -> dict[frozenset[RobinhoodStatementReference], tuple[int, RobinhoodInKindTransferGroup]]:
    result: dict[
        frozenset[RobinhoodStatementReference],
        tuple[int, RobinhoodInKindTransferGroup],
    ] = {}
    for index, group in enumerate(cash.activity_reconciliation.in_kind_transfer_groups):
        key = frozenset(group.statement_references)
        if not key or key in result:
            raise _fail("primary in-kind group reference set is empty or duplicated")
        result[key] = (index, group)
    return result


def _cash_pairs(
    cash: RobinhoodCashReconciliation,
    counterpart_bundle: RobinhoodStatementBundle,
    primary_rows: tuple[_TransferEvidence, ...],
    counterpart_rows: tuple[_TransferEvidence, ...],
) -> tuple[
    tuple[RobinhoodCashTransferPair, ...],
    Counter[RobinhoodStatementReference],
    Counter[RobinhoodStatementReference],
]:
    primary_cash = [
        (evidence, amount)
        for evidence in primary_rows
        if (kind_amount := _classify_transfer(evidence))[0] == "cash"
        for amount in (kind_amount[1],)
        if amount is not None
    ]
    counterpart_cash = [
        (evidence, amount)
        for evidence in counterpart_rows
        if (kind_amount := _classify_transfer(evidence))[0] == "cash"
        for amount in (kind_amount[1],)
        if amount is not None
    ]
    primary_by_key: dict[_CashKey, list[_TransferEvidence]] = defaultdict(list)
    counterpart_by_key: dict[_CashKey, list[_TransferEvidence]] = defaultdict(list)
    for evidence, amount in primary_cash:
        primary_by_key[(evidence.row.transaction_date, amount)].append(evidence)
    for evidence, amount in counterpart_cash:
        counterpart_by_key[(evidence.row.transaction_date, amount)].append(evidence)

    source_groups = _cash_group_map(cash)
    consumed_source_refs: set[RobinhoodStatementReference] = set()
    consumed_primary: Counter[RobinhoodStatementReference] = Counter()
    consumed_counterpart: Counter[RobinhoodStatementReference] = Counter()
    pairs: list[RobinhoodCashTransferPair] = []
    for key in sorted(primary_by_key, key=repr):
        transfer_date, amount = key
        if not _coverage_contains(
            cash.activity_reconciliation.primary_bundle,
            transfer_date,
        ) or not _coverage_contains(counterpart_bundle, transfer_date):
            raise _fail("cash transfer falls outside shared statement coverage")
        primary_group = primary_by_key[key]
        counterpart_group = counterpart_by_key.get(
            (transfer_date, amount.copy_negate()),
            [],
        )
        if len(primary_group) != 1 or len(counterpart_group) != 1:
            raise _fail("cash transfer counterpart is absent or non-unique")
        primary_evidence = primary_group[0]
        counterpart_evidence = counterpart_group[0]
        source_group = source_groups.get(primary_evidence.reference)
        if source_group is None:
            raise _fail("primary cash transfer lacks #459 source provenance")
        if primary_evidence.reference in consumed_source_refs:
            raise _fail("primary cash transfer source group is consumed twice")
        consumed_source_refs.add(primary_evidence.reference)
        consumed_primary[primary_evidence.reference] += 1
        consumed_counterpart[counterpart_evidence.reference] += 1
        direction = (
            RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY
            if amount > 0
            else RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART
        )
        pairs.append(
            RobinhoodCashTransferPair(
                event_ordinal=source_group.event_ordinals[0],
                primary_reference=primary_evidence.reference,
                counterpart_reference=counterpart_evidence.reference,
                direction=direction,
            )
        )
    if consumed_source_refs != set(source_groups):
        raise _fail("primary cash ITRF source groups are not fully consumed")
    return tuple(pairs), consumed_primary, consumed_counterpart


def _security_key(
    evidence: _TransferEvidence,
    *,
    primary_account: str,
    counterpart_account: str,
) -> tuple[_SecurityKey, RobinhoodTransferDirection]:
    row = evidence.row
    source = row.source_account_reference
    destination = row.destination_account_reference
    if (
        source is None
        or destination is None
        or source == destination
        or {source, destination} != {primary_account, counterpart_account}
        or row.symbol is None
    ):
        raise _fail("security ITRF account references are unsupported")
    direction = (
        RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART
        if source == primary_account
        else RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY
    )
    return (
        (row.transaction_date, row.symbol, source, destination),
        direction,
    )


def _security_groups(
    cash: RobinhoodCashReconciliation,
    counterpart_bundle: RobinhoodStatementBundle,
    primary_rows: tuple[_TransferEvidence, ...],
    counterpart_rows: tuple[_TransferEvidence, ...],
) -> tuple[
    tuple[RobinhoodSecurityTransferGroup, ...],
    Counter[RobinhoodStatementReference],
    Counter[RobinhoodStatementReference],
]:
    primary_account = cash.activity_reconciliation.primary_bundle.account_number
    counterpart_account = counterpart_bundle.account_number
    primary_groups: dict[_SecurityKey, list[_TransferEvidence]] = defaultdict(list)
    counterpart_groups: dict[_SecurityKey, list[_TransferEvidence]] = defaultdict(list)
    directions: dict[_SecurityKey, RobinhoodTransferDirection] = {}
    for evidence in primary_rows:
        if _classify_transfer(evidence)[0] != "security":
            continue
        key, direction = _security_key(
            evidence,
            primary_account=primary_account,
            counterpart_account=counterpart_account,
        )
        primary_groups[key].append(evidence)
        directions[key] = direction
    for evidence in counterpart_rows:
        if _classify_transfer(evidence)[0] != "security":
            continue
        key, direction = _security_key(
            evidence,
            primary_account=primary_account,
            counterpart_account=counterpart_account,
        )
        counterpart_groups[key].append(evidence)
        directions[key] = direction

    source_groups = _in_kind_group_map(cash)
    consumed_source_indexes: Counter[int] = Counter()
    consumed_primary: Counter[RobinhoodStatementReference] = Counter()
    consumed_counterpart: Counter[RobinhoodStatementReference] = Counter()
    result: list[RobinhoodSecurityTransferGroup] = []
    for key in sorted(primary_groups, key=repr):
        transfer_date, _, _, _ = key
        if not _coverage_contains(
            cash.activity_reconciliation.primary_bundle,
            transfer_date,
        ) or not _coverage_contains(counterpart_bundle, transfer_date):
            raise _fail("security transfer falls outside shared statement coverage")
        primary_group = primary_groups[key]
        counterpart_group = counterpart_groups.get(key, [])
        if len(primary_group) != len(counterpart_group):
            raise _fail("security transfer row counts disagree")
        primary_quantities = [evidence.row.quantity for evidence in primary_group]
        counterpart_quantities = [evidence.row.quantity for evidence in counterpart_group]
        if any(
            quantity is None or quantity <= 0
            for quantity in (*primary_quantities, *counterpart_quantities)
        ):
            raise _fail("security transfer quantity is missing or non-positive")
        quantities = [
            quantity
            for quantity in (*primary_quantities, *counterpart_quantities)
            if quantity is not None
        ]
        tolerance = robinhood_display_band(quantities)
        if robinhood_beyond_band(
            robinhood_exact_sum(
                (quantity for quantity in primary_quantities if quantity is not None),
                label=_SECURITY_TRANSFER_BAND,
            ),
            robinhood_exact_sum(
                (quantity for quantity in counterpart_quantities if quantity is not None),
                label=_SECURITY_TRANSFER_BAND,
            ),
            tolerance,
            label=_SECURITY_TRANSFER_BAND,
        ):
            raise _fail("security transfer aggregate quantities disagree")
        primary_refs = frozenset(evidence.reference for evidence in primary_group)
        source_match = source_groups.get(primary_refs)
        if source_match is None:
            raise _fail("security transfer does not map to one #459 in-kind group")
        source_index, source_group = source_match
        consumed_source_indexes[source_index] += 1
        primary_ref_tuple = tuple(
            sorted(
                primary_refs,
                key=lambda ref: (
                    ref.statement_index,
                    ref.location.page,
                    ref.location.row,
                ),
            )
        )
        counterpart_ref_tuple = tuple(
            sorted(
                (evidence.reference for evidence in counterpart_group),
                key=lambda ref: (
                    ref.statement_index,
                    ref.location.page,
                    ref.location.row,
                ),
            )
        )
        consumed_primary.update(primary_ref_tuple)
        consumed_counterpart.update(counterpart_ref_tuple)
        result.append(
            RobinhoodSecurityTransferGroup(
                event_ordinals=source_group.event_ordinals,
                primary_references=primary_ref_tuple,
                counterpart_references=counterpart_ref_tuple,
                direction=directions[key],
            )
        )
    if consumed_source_indexes != Counter(range(len(source_groups))):
        raise _fail("#459 in-kind groups are not consumed bijectively")
    return tuple(result), consumed_primary, consumed_counterpart


def reconcile_robinhood_transfers(
    cash: RobinhoodCashReconciliation,
    counterpart_bundle: RobinhoodStatementBundle,
    *,
    same_beneficial_owner: bool,
) -> RobinhoodTransferReconciliation:
    """Require complete two-sided Robinhood brokerage transfer evidence.

    Quantity suffixes are intentionally not compared because the two statement
    series render them differently. Counterpart-bundle provenance is a caller
    obligation.
    """

    if not same_beneficial_owner:
        raise _fail("same beneficial ownership is required for internal transfers")
    primary_bundle = cash.activity_reconciliation.primary_bundle
    if primary_bundle.account_number == counterpart_bundle.account_number:
        raise _fail("primary and counterpart account identities must differ")

    primary_rows = _transfer_rows(primary_bundle)
    counterpart_rows = _transfer_rows(counterpart_bundle)
    cash_pairs, cash_primary, cash_counterpart = _cash_pairs(
        cash,
        counterpart_bundle,
        primary_rows,
        counterpart_rows,
    )
    security_groups, security_primary, security_counterpart = _security_groups(
        cash,
        counterpart_bundle,
        primary_rows,
        counterpart_rows,
    )
    if cash_primary + security_primary != Counter(evidence.reference for evidence in primary_rows):
        raise _fail("primary ITRF rows are not consumed exactly once")
    if cash_counterpart + security_counterpart != Counter(
        evidence.reference for evidence in counterpart_rows
    ):
        raise _fail("counterpart ITRF rows are not consumed exactly once")
    return RobinhoodTransferReconciliation(
        cash_reconciliation=cash,
        counterpart_bundle=counterpart_bundle,
        cash_pairs=cash_pairs,
        security_groups=security_groups,
    )
