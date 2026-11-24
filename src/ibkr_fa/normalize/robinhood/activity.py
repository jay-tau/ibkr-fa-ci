"""Reconcile Robinhood activity events against issued statement evidence."""

from __future__ import annotations

import operator
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import partial

from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
    RobinhoodStatementBundle,
)
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementActivity,
    RobinhoodStatementSection,
    SourceLocation,
)


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementReference:
    """Resolvable row identity inside one sorted statement bundle."""

    statement_index: int
    location: SourceLocation


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCusipTransitionUse:
    """One selected manual exception preserving the activity CUSIP."""

    symbol: str
    activity_cusip: str
    statement_cusip: str
    event_ordinals: tuple[int, ...]
    statement_references: tuple[RobinhoodStatementReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodActivityGroup:
    """One exact/banded multiset reconciliation group."""

    transaction_code: str
    event_kinds: tuple[RobinhoodEventKind, ...]
    event_ordinals: tuple[int, ...]
    statement_references: tuple[RobinhoodStatementReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodInKindTransferGroup:
    """One aggregate in-kind ITRF comparison without invented row pairing."""

    event_ordinals: tuple[int, ...]
    statement_references: tuple[RobinhoodStatementReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodPendingStatementEvent:
    """A CSV event whose settlement lies after issued coverage."""

    event_ordinal: int


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodPendingTradeGroup:
    """Pending-settlement statement evidence for one CSV trade multiset."""

    event_ordinals: tuple[int, ...]
    statement_references: tuple[RobinhoodStatementReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCryptoStatementRow:
    """Statement-only Crypto cash evidence excluded from the brokerage CSV."""

    statement_reference: RobinhoodStatementReference


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodActivityReconciliation:
    """Complete #459 evidence without cash/transfer/position processing."""

    primary_bundle: RobinhoodStatementBundle
    activity_groups: tuple[RobinhoodActivityGroup, ...]
    in_kind_transfer_groups: tuple[RobinhoodInKindTransferGroup, ...]
    pending_statement_events: tuple[RobinhoodPendingStatementEvent, ...]
    pending_trade_groups: tuple[RobinhoodPendingTradeGroup, ...]
    crypto_rows: tuple[RobinhoodCryptoStatementRow, ...]
    cusip_transition_uses: tuple[RobinhoodCusipTransitionUse, ...] = ()


@dataclass(frozen=True, slots=True)
class _StatementEvidence:
    reference: RobinhoodStatementReference
    row: RobinhoodStatementActivity


class _AssignmentStatus(StrEnum):
    NONE = "none"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"


type _DiscreteKey = tuple[
    int,
    str,
    date,
    Decimal | None,
    bool,
    bool,
]
type _PendingKey = tuple[
    str,
    date,
    date,
    Decimal | None,
    bool,
    bool,
]
type _OperandClass = tuple[object, ...]

ROBINHOOD_TRADE_KINDS = frozenset(
    {
        RobinhoodEventKind.EQUITY_BUY,
        RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
        RobinhoodEventKind.PRIMARY_ISSUE_BUY,
        RobinhoodEventKind.EQUITY_SELL,
        RobinhoodEventKind.MANAGED_EQUITY_BUY,
        RobinhoodEventKind.MANAGED_EQUITY_SELL,
        RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
    }
)
ROBINHOOD_TRADE_CODES = frozenset({"Buy", "Sell", "BTO", "STC"})
# Statement-only Crypto cash rows' code: reconciliation pairs every other
# statement activity row with a report event of the row's date.
ROBINHOOD_CRYPTO_CODE = "COIN"
ROBINHOOD_MANAGED_TRADE_KINDS = frozenset(
    {
        RobinhoodEventKind.MANAGED_EQUITY_BUY,
        RobinhoodEventKind.MANAGED_EQUITY_SELL,
    }
)
# Both CUSIP-pairing checks refuse with this text: the activity pairing here
# and the configured-transition check in identity_exceptions.
ROBINHOOD_CUSIP_CONTRADICTION = "statement CUSIP contradicts activity CUSIP"
_MAX_ASSIGNMENT_SEARCH_NODES = 10_000
_CANONICAL_ZERO = Decimal("0")
_PRICE_BAND = "activity price band"
_IN_KIND_BAND = "in-kind aggregate quantity"


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def robinhood_half_ulp(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise _fail("activity comparison contains a non-finite Decimal")
    return Decimal(1).scaleb(exponent) / 2


def robinhood_exact(
    operation: Callable[..., Decimal],
    *operands: Decimal,
    label: str,
) -> Decimal:
    """Return ``operation(*operands)`` with every digit kept (#1308).

    It runs through :func:`ibkr_fa.parser._exact.exactly`. Where every digit
    can't be kept, it refuses as ``"<label> exceeds the supported precision"``;
    ``label`` is fixed text naming the check, never a value.
    """
    return exactly(operation, *operands, refusal=partial(_precision_refusal, label=label))


def robinhood_exact_sum(
    values: Iterable[Decimal],
    *,
    label: str,
    start: Decimal = _CANONICAL_ZERO,
) -> Decimal:
    """``sum(values, start)`` with every digit kept, refusing as :func:`robinhood_exact`."""
    return robinhood_exact(partial(sum, values, start), label=label)


def _precision_refusal(_out_of_memory: bool, *, label: str) -> RobinhoodReconciliationError:
    return _fail(f"{label} exceeds the supported precision")


def robinhood_display_band(values: Iterable[Decimal]) -> Decimal:
    """The largest displayed half-unit of ``values``, refusing as :func:`robinhood_half_ulp`."""
    return max(robinhood_half_ulp(value) for value in values)


def robinhood_beyond_band(left: Decimal, right: Decimal, tolerance: Decimal, *, label: str) -> bool:
    """Whether the magnitude of the exact ``left - right`` exceeds ``tolerance``.

    It refuses as :func:`robinhood_exact`.
    """
    return robinhood_exact(operator.sub, left, right, label=label).copy_abs() > tolerance


def _within_display_band(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= robinhood_half_ulp(left) + robinhood_half_ulp(right)


def _event_price_within_band(
    event: RobinhoodEvent,
    evidence: _StatementEvidence,
) -> bool:
    event_price = event.price_usd
    row_price = evidence.row.price_usd
    if event_price is None or row_price is None:
        return event_price is None and row_price is None
    tolerance = robinhood_exact(
        operator.add,
        robinhood_half_ulp(event_price),
        robinhood_half_ulp(row_price),
        label=_PRICE_BAND,
    )
    if event.kind in ROBINHOOD_MANAGED_TRADE_KINDS:
        if event.amount_usd is None or event.quantity is None:
            return False
        if robinhood_statement_signed_amount(evidence.row) != event.amount_usd:
            return False
        # The amount's half unit spread over the quantity, without dividing:
        # |difference| x |quantity| <= tolerance x |quantity| + half unit.
        quantity = event.quantity.copy_abs()
        difference = robinhood_exact(operator.sub, event_price, row_price, label=_PRICE_BAND)
        return robinhood_exact(
            operator.mul, difference.copy_abs(), quantity, label=_PRICE_BAND
        ) <= robinhood_exact(
            operator.add,
            robinhood_exact(operator.mul, tolerance, quantity, label=_PRICE_BAND),
            robinhood_half_ulp(event.amount_usd),
            label=_PRICE_BAND,
        )
    return abs(event_price - row_price) <= tolerance


def robinhood_signed_cash(
    debit: Decimal | None,
    credit: Decimal | None,
    *,
    row_kind: str,
) -> Decimal | None:
    """Sign one statement row's debit/credit cells exactly.

    A negative cell, then a row populating both cells, refuses. A nonzero
    credit is returned unchanged and a nonzero debit negated with
    ``copy_negate``, so no digit is rounded and the active decimal context is
    neither used nor flagged. A lone zero of any sign or scale is canonical
    ``Decimal("0")``; pending-lifecycle keys compare these tuples. A row with
    neither cell has no signed amount.
    """
    if (debit is not None and debit < 0) or (credit is not None and credit < 0):
        raise _fail(f"statement {row_kind} debit/credit must be non-negative")
    if debit is not None and credit is not None:
        raise _fail(f"statement {row_kind} row populates both debit and credit")
    if credit is not None:
        return credit if credit else _CANONICAL_ZERO
    if debit is not None:
        return debit.copy_negate() if debit else _CANONICAL_ZERO
    return None


def robinhood_statement_signed_amount(
    row: RobinhoodStatementActivity,
) -> Decimal | None:
    return robinhood_signed_cash(row.debit_usd, row.credit_usd, row_kind="activity")


def _statement_reference(
    statement_index: int,
    row: RobinhoodStatementActivity,
) -> RobinhoodStatementReference:
    return RobinhoodStatementReference(statement_index, row.location)


def _account_activity_rows(
    bundle: RobinhoodStatementBundle,
) -> tuple[_StatementEvidence, ...]:
    return tuple(
        _StatementEvidence(_statement_reference(index, row), row)
        for index, statement in enumerate(bundle.statements)
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
    )


def _pending_trade_rows(
    bundle: RobinhoodStatementBundle,
) -> tuple[_StatementEvidence, ...]:
    return tuple(
        _StatementEvidence(_statement_reference(index, row), row)
        for index, statement in enumerate(bundle.statements)
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
    )


def _statement_index_for_settlement(
    bundle: RobinhoodStatementBundle,
    settlement_date: date,
) -> int | None:
    if settlement_date < bundle.coverage_start:
        raise _fail("activity event settles before statement coverage")
    if settlement_date > bundle.coverage_end:
        return None
    matches = [
        index
        for index, statement in enumerate(bundle.statements)
        if statement.period_start <= settlement_date <= statement.period_end
    ]
    if len(matches) != 1:
        raise _fail("activity settlement date does not resolve to one statement")
    return matches[0]


def _event_discrete_key(event: RobinhoodEvent, statement_index: int) -> _DiscreteKey:
    return (
        statement_index,
        event.trans_code,
        event.activity_date,
        event.amount_usd,
        event.quantity is not None,
        event.price_usd is not None,
    )


def _row_discrete_key(evidence: _StatementEvidence) -> _DiscreteKey:
    row = evidence.row
    return (
        evidence.reference.statement_index,
        row.transaction_code,
        row.transaction_date,
        robinhood_statement_signed_amount(row),
        row.quantity is not None,
        row.price_usd is not None,
    )


def _event_operand_class(event: RobinhoodEvent) -> _OperandClass:
    return (
        event.kind,
        event.instrument,
        event.cusip,
        event.quantity,
        event.price_usd,
    )


def _row_operand_class(evidence: _StatementEvidence) -> _OperandClass:
    row = evidence.row
    return (row.symbol, row.cusips, row.quantity, row.price_usd)


def _row_candidate_class(evidence: _StatementEvidence) -> _OperandClass:
    """Exponent-aware class: band candidacy depends on displayed precision."""

    row = evidence.row
    return (
        row.symbol,
        row.cusips,
        row.quantity.as_tuple() if row.quantity is not None else None,
        row.price_usd.as_tuple() if row.price_usd is not None else None,
    )


def _candidate(  # noqa: PLR0911
    event: RobinhoodEvent,
    evidence: _StatementEvidence,
    *,
    banded: bool,
) -> bool:
    row = evidence.row
    if (
        row.symbol is not None
        and row.symbol != event.instrument
        and not (
            event.kind in ROBINHOOD_MANAGED_TRADE_KINDS
            and event.instrument is not None
            and row.symbol.startswith(event.instrument)
            and event.cusip is not None
            and bool(row.cusips)
            and row.cusips[0] == event.cusip
        )
    ):
        return False
    if (event.quantity is None) != (row.quantity is None):
        return False
    if event.quantity is not None and row.quantity is not None:
        if event.kind is RobinhoodEventKind.SECURITY_RECEIPT and banded:
            if not _within_display_band(event.quantity, row.quantity):
                return False
        elif event.quantity != row.quantity:
            return False
    if (event.price_usd is None) != (row.price_usd is None):
        return False
    if event.price_usd is not None and row.price_usd is not None:
        if banded:
            if not _event_price_within_band(event, evidence):
                return False
        elif event.price_usd != row.price_usd:
            return False
    return True


def _assignment_status(
    events: Sequence[RobinhoodEvent],
    rows: Sequence[_StatementEvidence],
    candidate: Callable[[RobinhoodEvent, _StatementEvidence], bool],
    prune_class: Callable[[_StatementEvidence], _OperandClass],
) -> _AssignmentStatus:
    if len(events) != len(rows):
        return _AssignmentStatus.NONE
    candidates = [
        tuple(index for index, row in enumerate(rows) if candidate(event, row)) for event in events
    ]
    if any(not indexes for indexes in candidates):
        return _AssignmentStatus.NONE

    signatures: set[frozenset[tuple[tuple[_OperandClass, _OperandClass], int]]] = set()
    visited_nodes = 0

    def visit(
        remaining: tuple[int, ...],
        used_rows: frozenset[int],
        pairs: tuple[tuple[int, int], ...],
    ) -> None:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > _MAX_ASSIGNMENT_SEARCH_NODES:
            raise _fail("activity matching exceeds the safe search budget")
        if len(signatures) > 1:
            return
        if not remaining:
            pair_classes = Counter(
                (
                    _event_operand_class(events[event_index]),
                    _row_operand_class(rows[row_index]),
                )
                for event_index, row_index in pairs
            )
            signatures.add(frozenset(pair_classes.items()))
            return
        event_index = min(
            remaining,
            key=lambda index: sum(row_index not in used_rows for row_index in candidates[index]),
        )
        available_rows: list[int] = []
        seen_classes: set[_OperandClass] = set()
        for row_index in candidates[event_index]:
            if row_index in used_rows:
                continue
            row_class = prune_class(rows[row_index])
            if row_class in seen_classes:
                continue
            seen_classes.add(row_class)
            available_rows.append(row_index)
        available = tuple(available_rows)
        if not available:
            return
        next_remaining = tuple(index for index in remaining if index != event_index)
        for row_index in available:
            visit(
                next_remaining,
                used_rows | {row_index},
                (*pairs, (event_index, row_index)),
            )

    visit(tuple(range(len(events))), frozenset(), ())
    if not signatures:
        return _AssignmentStatus.NONE
    if len(signatures) > 1:
        return _AssignmentStatus.AMBIGUOUS
    return _AssignmentStatus.UNIQUE


def _require_cusip_pairing(
    events: Sequence[RobinhoodEvent],
    rows: Sequence[_StatementEvidence],
    candidate: Callable[[RobinhoodEvent, _StatementEvidence], bool],
    prune_class: Callable[[_StatementEvidence], _OperandClass],
    compared: Callable[[_StatementEvidence], bool],
) -> None:
    """Refuse unless the accepted pass allows a pairing with no CUSIP contradiction.

    A compared row contradicts its event when the row prints a CUSIP, the event
    has one, and the row's first printed CUSIP differs from it; later ones can
    spill over from the next row at a page break.
    """

    def agrees(event: RobinhoodEvent, evidence: _StatementEvidence) -> bool:
        cusips = evidence.row.cusips
        return (
            not compared(evidence) or not cusips or event.cusip is None or cusips[0] == event.cusip
        )

    status = _assignment_status(
        events,
        rows,
        lambda event, row: candidate(event, row) and agrees(event, row),
        prune_class,
    )
    if status is _AssignmentStatus.NONE:
        raise _fail(ROBINHOOD_CUSIP_CONTRADICTION)


def _event_contract(event: RobinhoodEvent) -> _OperandClass:
    return (
        event.option_symbol,
        event.option_expiration,
        event.option_right,
        event.option_strike_usd,
    )


def _row_contract(evidence: _StatementEvidence) -> _OperandClass:
    row = evidence.row
    return (row.option_symbol, row.option_expiration, row.option_right, row.option_strike_usd)


def _require_contract_pairing(
    events: Sequence[RobinhoodEvent],
    rows: Sequence[_StatementEvidence],
    candidate: Callable[[RobinhoodEvent, _StatementEvidence], bool],
    prune_class: Callable[[_StatementEvidence], _OperandClass],
) -> None:
    """Refuse unless the accepted pass allows a pairing with no contract contradiction.

    Every event's option contract (symbol, expiry, right and strike) must be
    its row's; events and rows outside option trades carry none. The refusal
    names every row of the group, and no contract, date or amount.
    """

    status = _assignment_status(
        events,
        rows,
        lambda event, row: candidate(event, row) and _event_contract(event) == _row_contract(row),
        prune_class,
    )
    if status is _AssignmentStatus.NONE:
        references = sorted(
            (row.reference for row in rows),
            key=lambda ref: (ref.statement_index, ref.location.page, ref.location.row),
        )
        named = ", ".join(
            f"statement {ref.statement_index + 1} page {ref.location.page} row {ref.location.row}"
            for ref in references
        )
        raise _fail(
            "statement option contract contradicts activity option contract "
            f"({references[0].location.section.value}: {named}; column Description)"
        )


def _require_exact_or_banded(
    events: Sequence[RobinhoodEvent],
    rows: Sequence[_StatementEvidence],
    candidate: Callable[..., bool],
    *,
    subject: str,
) -> bool:
    """Return whether the group pairs only within displayed precision.

    An exact pairing wins; otherwise one banded pairing is required. The three
    matching-decision refusals start with ``subject``.
    """

    exact = _assignment_status(
        events,
        rows,
        lambda event, row: candidate(event, row, banded=False),
        _row_operand_class,
    )
    if exact is _AssignmentStatus.AMBIGUOUS:
        raise _fail(f"{subject} exact matching is ambiguous")
    banded = exact is _AssignmentStatus.NONE
    if banded:
        status = _assignment_status(
            events,
            rows,
            lambda event, row: candidate(event, row, banded=True),
            _row_candidate_class,
        )
        if status is _AssignmentStatus.NONE:
            raise _fail(f"{subject} quantity or price evidence disagrees")
        if status is _AssignmentStatus.AMBIGUOUS:
            raise _fail(f"{subject} display-precision matching is ambiguous")
    return banded


def _require_unique_group_match(
    events: Sequence[RobinhoodEvent],
    rows: Sequence[_StatementEvidence],
) -> None:
    """Require one exact or banded pairing of an issued activity group.

    Rows without a symbol are then checked pair by pair: the accepted pass
    must allow a pairing in which each such row's first printed CUSIP is its
    event's. Rows with a symbol keep the per-symbol check in
    ``apply_cusip_transitions``. The accepted pass must also allow a pairing
    in which each option trade's contract is its row's; a contract never
    picks the pass or settles a tie.
    """

    if len(events) != len(rows):
        raise _fail("activity group multiplicities disagree")
    if (
        any(row.row.symbol is None for row in rows)
        and len({event.instrument for event in events}) > 1
    ):
        raise _fail("blank-symbol activity group spans multiple instruments")
    banded = _require_exact_or_banded(events, rows, _candidate, subject="activity")
    candidate = partial(_candidate, banded=banded)
    prune_class = _row_candidate_class if banded else _row_operand_class
    _require_cusip_pairing(
        events,
        rows,
        candidate,
        prune_class,
        lambda row: row.row.symbol is None,
    )
    _require_contract_pairing(events, rows, candidate, prune_class)


def _in_kind_groups(
    events: Sequence[tuple[int, RobinhoodEvent]],
    rows: Sequence[_StatementEvidence],
) -> tuple[RobinhoodInKindTransferGroup, ...]:
    event_groups: dict[tuple[int, date, str], list[tuple[int, RobinhoodEvent]]] = defaultdict(list)
    row_groups: dict[tuple[int, date, str], list[_StatementEvidence]] = defaultdict(list)
    for statement_index, event in events:
        if event.instrument is None or event.quantity is None:
            raise _fail("in-kind event lacks instrument or quantity")
        event_groups[(statement_index, event.activity_date, event.instrument)].append(
            (statement_index, event)
        )
    for evidence in rows:
        row = evidence.row
        if row.symbol is None or row.quantity is None:
            raise _fail("in-kind statement row lacks symbol or quantity")
        if row.price_usd is not None or robinhood_statement_signed_amount(row) is not None:
            raise _fail("in-kind statement row unexpectedly carries price or cash")
        row_groups[(evidence.reference.statement_index, row.transaction_date, row.symbol)].append(
            evidence
        )
    if set(event_groups) != set(row_groups):
        raise _fail("in-kind transfer group keys disagree")

    result: list[RobinhoodInKindTransferGroup] = []
    for key in sorted(event_groups, key=repr):
        event_group = event_groups[key]
        row_group = row_groups[key]
        if len(event_group) != len(row_group):
            raise _fail("in-kind transfer row counts disagree")
        event_quantities = [event.quantity for _, event in event_group]
        row_quantities = [evidence.row.quantity for evidence in row_group]
        if any(value is None for value in (*event_quantities, *row_quantities)):
            raise _fail("in-kind transfer quantity is missing")
        all_quantities = [
            value for value in (*event_quantities, *row_quantities) if value is not None
        ]
        tolerance = robinhood_display_band(all_quantities)
        if robinhood_beyond_band(
            robinhood_exact_sum(
                (value for value in event_quantities if value is not None), label=_IN_KIND_BAND
            ),
            robinhood_exact_sum(
                (value for value in row_quantities if value is not None), label=_IN_KIND_BAND
            ),
            tolerance,
            label=_IN_KIND_BAND,
        ):
            raise _fail("in-kind aggregate quantity evidence disagrees")
        result.append(
            RobinhoodInKindTransferGroup(
                event_ordinals=tuple(sorted(event.source_record_index for _, event in event_group)),
                statement_references=tuple(
                    sorted(
                        (evidence.reference for evidence in row_group),
                        key=lambda ref: (
                            ref.statement_index,
                            ref.location.page,
                            ref.location.row,
                        ),
                    )
                ),
            )
        )
    return tuple(result)


def _pending_key_event(event: RobinhoodEvent) -> _PendingKey:
    return (
        event.trans_code,
        event.activity_date,
        event.settle_date,
        event.amount_usd,
        event.quantity is not None,
        event.price_usd is not None,
    )


def _pending_key_row(evidence: _StatementEvidence) -> _PendingKey:
    row = evidence.row
    if row.settle_date is None:
        raise _fail("pending statement row lacks settle date")
    return (
        row.transaction_code,
        row.transaction_date,
        row.settle_date,
        robinhood_statement_signed_amount(row),
        row.quantity is not None,
        row.price_usd is not None,
    )


def _pending_candidate(
    event: RobinhoodEvent,
    evidence: _StatementEvidence,
    *,
    banded: bool,
) -> bool:
    row = evidence.row
    if event.quantity != row.quantity:
        return False
    if event.price_usd is None or row.price_usd is None:
        return event.price_usd is None and row.price_usd is None
    return _event_price_within_band(event, evidence) if banded else event.price_usd == row.price_usd


def _pending_trade_groups(
    bundle: RobinhoodStatementBundle,
    events: Sequence[RobinhoodEvent],
) -> tuple[tuple[RobinhoodPendingTradeGroup, ...], frozenset[int]]:
    """Match pending-settlement rows to CSV trades by quantity and price.

    The pending table prints no symbol, so every row is then checked pair by
    pair: the accepted pass must allow a pairing in which each row's first
    printed CUSIP is its trade's, and one in which each option row's contract
    (symbol, expiry, right and strike) is its trade's. A contract never picks
    the pass or settles a tie. No CUSIP transition applies to these rows.
    """

    rows = _pending_trade_rows(bundle)
    trade_events = [event for event in events if event.kind in ROBINHOOD_TRADE_KINDS]
    events_by_key: dict[_PendingKey, list[RobinhoodEvent]] = defaultdict(list)
    rows_by_key: dict[_PendingKey, list[_StatementEvidence]] = defaultdict(list)
    for event in trade_events:
        events_by_key[_pending_key_event(event)].append(event)
    for row in rows:
        rows_by_key[_pending_key_row(row)].append(row)

    result: list[RobinhoodPendingTradeGroup] = []
    matched_ordinals: set[int] = set()
    for key in sorted(rows_by_key, key=repr):
        row_group = rows_by_key[key]
        event_group = events_by_key.get(key, [])
        if len(event_group) != len(row_group):
            raise _fail("pending trade multiplicities disagree")
        banded = _require_exact_or_banded(
            event_group, row_group, _pending_candidate, subject="pending trade"
        )
        candidate = partial(_pending_candidate, banded=banded)
        prune_class = _row_candidate_class if banded else _row_operand_class
        _require_cusip_pairing(
            event_group,
            row_group,
            candidate,
            prune_class,
            lambda _row: True,
        )
        _require_contract_pairing(event_group, row_group, candidate, prune_class)
        ordinals = tuple(sorted(event.source_record_index for event in event_group))
        matched_ordinals.update(ordinals)
        result.append(
            RobinhoodPendingTradeGroup(
                event_ordinals=ordinals,
                statement_references=tuple(
                    sorted(
                        (row.reference for row in row_group),
                        key=lambda ref: (
                            ref.statement_index,
                            ref.location.page,
                            ref.location.row,
                        ),
                    )
                ),
            )
        )
    return tuple(result), frozenset(matched_ordinals)


def reconcile_robinhood_activity(  # noqa: PLR0912
    primary_bundle: RobinhoodStatementBundle,
    counterpart_bundles: Iterable[RobinhoodStatementBundle],
    events: Iterable[RobinhoodEvent],
) -> RobinhoodActivityReconciliation:
    """Require exhaustive agreement for the primary issued activity window."""

    counterparts = tuple(counterpart_bundles)
    counterpart_accounts = [bundle.account_number for bundle in counterparts]
    if primary_bundle.account_number in counterpart_accounts or len(
        set(counterpart_accounts)
    ) != len(counterpart_accounts):
        raise _fail("counterpart statement bundles are duplicated or self-referential")

    ordered_events = tuple(sorted(events, key=lambda event: event.source_record_index))
    if len({event.source_record_index for event in ordered_events}) != len(ordered_events):
        raise _fail("activity event source ordinals are duplicated")

    issued: list[tuple[int, RobinhoodEvent]] = []
    pending: list[RobinhoodEvent] = []
    for event in ordered_events:
        statement_index = _statement_index_for_settlement(
            primary_bundle,
            event.settle_date,
        )
        if statement_index is None:
            pending.append(event)
        else:
            issued.append((statement_index, event))

    all_statement_rows = _account_activity_rows(primary_bundle)
    crypto_rows = tuple(
        RobinhoodCryptoStatementRow(evidence.reference)
        for evidence in all_statement_rows
        if evidence.row.transaction_code == ROBINHOOD_CRYPTO_CODE
    )
    comparable_rows = tuple(
        evidence
        for evidence in all_statement_rows
        if evidence.row.transaction_code != ROBINHOOD_CRYPTO_CODE
    )

    in_kind_events = [
        (index, event)
        for index, event in issued
        if event.kind is RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER
    ]
    in_kind_rows = [
        evidence
        for evidence in comparable_rows
        if evidence.row.transaction_code == "ITRF"
        and evidence.row.quantity is not None
        and robinhood_statement_signed_amount(evidence.row) is None
    ]
    in_kind_groups = _in_kind_groups(in_kind_events, in_kind_rows)
    in_kind_ordinals = {event.source_record_index for _, event in in_kind_events}
    in_kind_references = {evidence.reference for evidence in in_kind_rows}

    normal_events: dict[_DiscreteKey, list[RobinhoodEvent]] = defaultdict(list)
    normal_rows: dict[_DiscreteKey, list[_StatementEvidence]] = defaultdict(list)
    for statement_index, event in issued:
        if event.source_record_index not in in_kind_ordinals:
            normal_events[_event_discrete_key(event, statement_index)].append(event)
    for evidence in comparable_rows:
        if evidence.reference not in in_kind_references:
            normal_rows[_row_discrete_key(evidence)].append(evidence)
    if set(normal_events) != set(normal_rows):
        raise _fail("issued activity signature sets disagree")

    activity_groups: list[RobinhoodActivityGroup] = []
    for key in sorted(normal_events, key=repr):
        event_group = normal_events[key]
        row_group = normal_rows[key]
        _require_unique_group_match(event_group, row_group)
        activity_groups.append(
            RobinhoodActivityGroup(
                transaction_code=key[1],
                event_kinds=tuple(
                    sorted(
                        {event.kind for event in event_group},
                        key=lambda kind: kind.value,
                    )
                ),
                event_ordinals=tuple(sorted(event.source_record_index for event in event_group)),
                statement_references=tuple(
                    sorted(
                        (row.reference for row in row_group),
                        key=lambda ref: (
                            ref.statement_index,
                            ref.location.page,
                            ref.location.row,
                        ),
                    )
                ),
            )
        )

    pending_groups, pending_matched = _pending_trade_groups(
        primary_bundle,
        ordered_events,
    )
    for event in pending:
        if event.activity_date <= primary_bundle.coverage_end:
            if event.kind not in ROBINHOOD_TRADE_KINDS:
                raise _fail("a non-trade event straddles the statement coverage boundary")
            if event.source_record_index not in pending_matched:
                raise _fail("a straddling trade lacks pending-settlement evidence")

    return RobinhoodActivityReconciliation(
        primary_bundle=primary_bundle,
        activity_groups=tuple(activity_groups),
        in_kind_transfer_groups=in_kind_groups,
        pending_statement_events=tuple(
            RobinhoodPendingStatementEvent(event.source_record_index) for event in pending
        ),
        pending_trade_groups=pending_groups,
        crypto_rows=crypto_rows,
    )
