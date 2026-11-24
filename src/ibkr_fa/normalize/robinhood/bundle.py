"""Validate Robinhood statement identity, coverage, and adjacent anchors."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
)


@dataclass(frozen=True, slots=True)
class RobinhoodReconciliationError(IbkrFaError):
    """A cross-document Robinhood invariant failed.

    ``evidence_period`` marks a refusal that an activity report and statements
    for the run's period clear (#1498): the loader then adds that remedy.
    ``receipt_resolution`` marks the REC receipt-block prompt: the pipeline
    then adds the security-identity guidance, if any (#1522).
    """

    message: str
    evidence_period: bool = False
    receipt_resolution: bool = False

    def __str__(self) -> str:
        return f"Robinhood reconciliation refused: {self.message}"


class RobinhoodBundleError(IbkrFaError):
    """The configured Robinhood source bundle is incomplete or contradictory."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementBundle:
    """One exact account's sorted, cross-document-validated statements."""

    family: RobinhoodStatementFamily
    coverage_start: date
    coverage_end: date
    opening_components_unavailable: bool
    statements: tuple[RobinhoodStatement, ...]

    @property
    def account_number(self) -> str:
        """The exact source account identity, hidden by ``repr=False``."""

        return self.statements[0].account_number

    @property
    def registration_lines(self) -> tuple[str, ...]:
        """The exact common source registration block."""

        return self.statements[0].registration_lines


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def opening_components(
    statement: RobinhoodStatement,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """The statement's four opening values, each ``None`` where unavailable."""

    summary = statement.account_summary
    return (
        summary.opening_brokerage_cash_usd,
        summary.opening_deposit_sweep_usd,
        summary.opening_total_securities_usd,
        summary.opening_portfolio_value_usd,
    )


def _closing_components(
    statement: RobinhoodStatement,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    summary = statement.account_summary
    return (
        summary.closing_brokerage_cash_usd,
        summary.closing_deposit_sweep_usd,
        summary.closing_total_securities_usd,
        summary.closing_portfolio_value_usd,
    )


def _resolved_family(
    statements: tuple[RobinhoodStatement, ...],
) -> RobinhoodStatementFamily:
    explicit = {
        statement.family
        for statement in statements
        if statement.family is not RobinhoodStatementFamily.UNSPECIFIED
    }
    if not explicit:
        raise _fail("statement family is never explicitly identified")
    if len(explicit) != 1:
        raise _fail("statement series contains conflicting account families")
    (family,) = explicit
    first_explicit = next(
        index
        for index, statement in enumerate(statements)
        if statement.family is not RobinhoodStatementFamily.UNSPECIFIED
    )
    if any(
        statement.family is RobinhoodStatementFamily.UNSPECIFIED
        for statement in statements[first_explicit + 1 :]
    ):
        raise _fail("an unspecified account family appears after explicit family evidence")
    return family


def _validate_opening_states(
    statements: tuple[RobinhoodStatement, ...],
) -> bool:
    unavailable_indexes: list[int] = []
    for index, statement in enumerate(statements):
        components = opening_components(statement)
        present = tuple(component is not None for component in components)
        if all(present):
            continue
        if any(present):
            raise _fail("statement opening components mix unavailable and numeric values")
        unavailable_indexes.append(index)
    if unavailable_indexes not in ([], [0]):
        raise _fail("only the earliest statement may have unavailable opening components")
    return bool(unavailable_indexes)


def _validate_adjacent(
    previous: RobinhoodStatement,
    current: RobinhoodStatement,
) -> None:
    if current.period_start <= previous.period_end:
        raise _fail("statement periods duplicate or overlap")
    if current.period_start != previous.period_end + timedelta(days=1):
        raise _fail("statement series has a coverage gap")
    opening = opening_components(current)
    if any(component is None for component in opening):
        raise _fail("a non-earliest statement has unavailable opening components")
    if opening != _closing_components(previous):
        raise _fail("adjacent statement opening and closing components disagree")


def _validate_one_account(
    statements: Iterable[RobinhoodStatement],
) -> RobinhoodStatementBundle:
    ordered = tuple(
        sorted(
            statements,
            key=lambda statement: (
                statement.period_start,
                statement.period_end,
            ),
        )
    )
    if not ordered:
        raise _fail("statement account group is empty")
    if len({statement.account_number for statement in ordered}) != 1:
        raise _fail("statement account group contains multiple account identities")
    if len({statement.registration_lines for statement in ordered}) != 1:
        raise _fail("statement registration changes within one account series")
    if {statement.base_currency for statement in ordered} != {"USD"}:
        raise _fail("statement account series is not consistently USD")

    family = _resolved_family(ordered)
    opening_unavailable = _validate_opening_states(ordered)
    for previous, current in pairwise(ordered):
        _validate_adjacent(previous, current)
    return RobinhoodStatementBundle(
        family=family,
        coverage_start=ordered[0].period_start,
        coverage_end=ordered[-1].period_end,
        opening_components_unavailable=opening_unavailable,
        statements=ordered,
    )


def validate_robinhood_statement_bundles(
    statements: Iterable[RobinhoodStatement],
) -> tuple[RobinhoodStatementBundle, ...]:
    """Group statements by exact account and validate every issued period.

    This function does not infer an account opening date or opening zero. An
    earliest all-``N/A`` opening is retained as unavailable source evidence.
    """

    grouped: dict[str, list[RobinhoodStatement]] = defaultdict(list)
    for statement in statements:
        grouped[statement.account_number].append(statement)
    if not grouped:
        raise _fail("statement bundle is empty")
    return tuple(
        _validate_one_account(grouped[account_number]) for account_number in sorted(grouped)
    )
