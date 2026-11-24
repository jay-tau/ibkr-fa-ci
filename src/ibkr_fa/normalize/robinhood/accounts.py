"""Bind two Robinhood activity reports to two filed statement accounts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from itertools import permutations

from ibkr_fa.config import RobinhoodAccountConfig
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood.activity import (
    ROBINHOOD_CRYPTO_CODE,
    ROBINHOOD_MANAGED_TRADE_KINDS,
    RobinhoodActivityReconciliation,
    reconcile_robinhood_activity,
)
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
    RobinhoodStatementBundle,
)
from ibkr_fa.normalize.robinhood.cash import (
    RobinhoodCashReconciliation,
    reconcile_robinhood_cash,
)
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    RobinhoodCusipTransition,
    apply_cusip_transitions,
)
from ibkr_fa.normalize.robinhood.transfers import (
    RobinhoodTransferReconciliation,
    reconcile_robinhood_transfers,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementFamily

_ACCOUNT_COUNT = 2


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodActivityReportEvidence:
    """One parsed report plus the dates selected when it was generated."""

    events: tuple[RobinhoodEvent, ...]
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAccountReconciliation:
    """One assigned report and its bounded account proof."""

    report_index: int
    evidence: RobinhoodActivityReportEvidence
    activity: RobinhoodActivityReconciliation
    cash: RobinhoodCashReconciliation
    source_bundle: RobinhoodStatementBundle


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAccountsReconciliation:
    """The complete two-account evidence proof from ADR 0006."""

    accounts: tuple[RobinhoodAccountReconciliation, RobinhoodAccountReconciliation]
    transfers: RobinhoodTransferReconciliation

    @property
    def account_labels(self) -> dict[int, str]:
        """Privacy-safe account types in reconciliation-index order."""
        return {
            index: _family_label(account.source_bundle.family)
            for index, account in enumerate(self.accounts)
        }

    @property
    def cusip_transition_references(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    f"{account_index}:{event_ordinal}"
                    for account_index, account in enumerate(self.accounts)
                    for use in account.activity.cusip_transition_uses
                    for event_ordinal in use.event_ordinals
                }
            )
        )


@dataclass(frozen=True, slots=True)
class _RuledOutPairingError(RobinhoodReconciliationError):
    """A pair failure that rules its pairing out whatever dates the evidence
    covers (#1508)."""


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def _family_label(family: RobinhoodStatementFamily) -> str:
    return family.value.replace("-", " ").title()


def account_name(account_labels: Mapping[int, str], index: int) -> str:
    """Account ``index`` by its statement family label, with the index when
    both accounts share that label, as refusals and blocker lines name it."""
    label = account_labels[index]
    return label if Counter(account_labels.values())[label] == 1 else f"{label} (index {index})"


def _evidence_period_failure(
    message: str, bundle: RobinhoodStatementBundle
) -> RobinhoodReconciliationError:
    """``message``, naming ``bundle``'s account by statement family, as a
    refusal that evidence for the run's period clears."""
    return RobinhoodReconciliationError(
        message.format(account=f"the {_family_label(bundle.family)} account"),
        evidence_period=True,
    )


def _validate_inputs(
    bundles: tuple[RobinhoodStatementBundle, ...],
    reports: tuple[RobinhoodActivityReportEvidence, ...],
    accounts: tuple[RobinhoodAccountConfig, ...],
) -> None:
    if len(bundles) != _ACCOUNT_COUNT:
        raise _fail("paired reconciliation requires exactly two statement bundles")
    if len(reports) != _ACCOUNT_COUNT:
        raise _fail("paired reconciliation requires exactly two activity reports")
    if len(accounts) != _ACCOUNT_COUNT:
        raise _fail("paired reconciliation requires exactly two configured accounts")
    if len({bundle.account_number for bundle in bundles}) != _ACCOUNT_COUNT:
        raise _fail("paired reconciliation statement accounts are duplicated")
    if len({account.number for account in accounts}) != _ACCOUNT_COUNT:
        raise _fail("paired reconciliation configured accounts are duplicated")
    if {bundle.account_number for bundle in bundles} != {account.number for account in accounts}:
        raise _fail("configured and statement account identities disagree")
    if not all(account.same_beneficial_owner for account in accounts):
        raise _fail("same beneficial ownership is required for paired reconciliation")
    for report in reports:
        if not report.events:
            raise _fail("activity report contains no events")
        if report.start_date > report.end_date:
            raise _fail("activity report attested range is invalid")
        ordinals = [event.source_record_index for event in report.events]
        if len(ordinals) != len(set(ordinals)):
            raise _fail("activity report source ordinals are duplicated")


def _validate_bundle_account(
    bundle: RobinhoodStatementBundle,
    account: RobinhoodAccountConfig,
) -> None:
    first = bundle.statements[0]
    if not (first.period_start <= account.opening_date <= first.period_end):
        raise _fail("configured account opening date is outside the first statement period")
    if not bundle.opening_components_unavailable:
        raise _fail("first statement does not carry the required opening layout")


def _assign_reports(
    reports: tuple[RobinhoodActivityReportEvidence, ...],
    bundles: tuple[RobinhoodStatementBundle, ...],
    accounts: tuple[RobinhoodAccountConfig, ...],
    transitions: tuple[RobinhoodCusipTransition, ...],
    *,
    financial_year: FinancialYear | None = None,
    schedule_fa: bool = True,
) -> tuple[tuple[int, RobinhoodActivityReconciliation], ...]:
    account_by_number = {account.number: account for account in accounts}
    failures: dict[tuple[int, int], str] = {}
    evidence_period_failures: set[tuple[int, int]] = set()
    # Pairs giving managed activity to another family's account, or activity
    # before the account's opening date: never the right pairing, whatever
    # period their evidence covers.
    ruled_out: set[tuple[int, int]] = set()
    candidates: list[tuple[tuple[int, RobinhoodActivityReconciliation], ...]] = []
    for report_indices in permutations(range(_ACCOUNT_COUNT)):
        report_index_by_bundle = dict(enumerate(report_indices))
        bounded: list[RobinhoodStatementBundle | None] = [None] * _ACCOUNT_COUNT
        for bundle_index, bundle in enumerate(bundles):
            report_index = report_index_by_bundle[bundle_index]
            report = reports[report_index]
            if _managed_activity_on_other_family(report, bundle):
                failures[(report_index, bundle_index)] = (
                    "managed activity is assigned to a non-managed statement account"
                )
                ruled_out.add((report_index, bundle_index))
                continue
            try:
                bounded[bundle_index] = _bounded_bundle(
                    bundle,
                    report,
                    account_by_number[bundle.account_number],
                )
            except RobinhoodReconciliationError as exc:
                failures[(report_index, bundle_index)] = exc.message
                if exc.evidence_period:
                    evidence_period_failures.add((report_index, bundle_index))
                if isinstance(exc, _RuledOutPairingError):
                    ruled_out.add((report_index, bundle_index))
        if any(bundle is None for bundle in bounded):
            continue
        bounded_pair = (bounded[0], bounded[1])
        activities: list[RobinhoodActivityReconciliation | None] = [None] * _ACCOUNT_COUNT
        for bundle_index, bounded_bundle in enumerate(bounded_pair):
            assert bounded_bundle is not None
            report_index = report_index_by_bundle[bundle_index]
            report = reports[report_index]
            counterpart = bounded_pair[1 - bundle_index]
            assert counterpart is not None
            try:
                activities[bundle_index] = apply_cusip_transitions(
                    reconcile_robinhood_activity(
                        bounded_bundle,
                        (counterpart,),
                        report.events,
                    ),
                    report.events,
                    account_number=bounded_bundle.account_number,
                    transitions=transitions,
                )
            except RobinhoodReconciliationError as exc:
                failures[(report_index, bundle_index)] = exc.message
        if any(activity is None for activity in activities):
            continue
        candidates.append(
            tuple(
                (report_index_by_bundle[bundle_index], activity)
                for bundle_index, activity in enumerate(activities)
                if activity is not None
            )
        )

    if len(candidates) > 1:
        raise _fail("activity report assignment is ambiguous")
    if not candidates:
        details = "; ".join(
            f"report {report_index}/bundle {bundle_index}: {failures[(report_index, bundle_index)]}"
            for report_index in range(_ACCOUNT_COUNT)
            for bundle_index in range(_ACCOUNT_COUNT)
            if (report_index, bundle_index) in failures
        )
        # Each assignment's pairs. Marked for the loader's
        # evidence-period remedy only when every assignment that could be the
        # right one fails nothing but checks that remedy clears (#1503).
        assignments = [
            {
                (report_index, bundle_index)
                for bundle_index, report_index in enumerate(report_indices)
            }
            for report_indices in permutations(range(_ACCOUNT_COUNT))
        ]
        possible = [pairs for pairs in assignments if not pairs & ruled_out]
        named_bundles = {bundle_index for _, bundle_index in evidence_period_failures}
        # Evidence cannot move an opening into the period the run needs (the
        # FA year for a run that builds Schedule FA, whose compute refuses an
        # account opened after it; else the FY, #1508) or complete a first
        # statement whose end is beyond the remedy's guaranteed end.
        remedy_fits = financial_year is None or (
            all(
                account.opening_date.year <= financial_year.start_year
                if schedule_fa
                else account.opening_date <= financial_year.end_date
                for account in accounts
            )
            and all(
                bundles[bundle_index].statements[0].period_end <= financial_year.end_date
                for pairs in possible
                for _report_index, bundle_index in pairs
                if bundle_index in named_bundles
            )
            # A crossed failure can name an account whose correct pair passed.
            # Following the remedy must not empty any named report: activity
            # from the opening date to the FY end that the account's statements
            # show (#1516), or an event by then in the report each remaining
            # assignment gives it, keeps the report it asks for non-empty.
            and all(
                _statements_show_activity(
                    bundles[bundle_index],
                    account_by_number[bundles[bundle_index].account_number].opening_date,
                    financial_year.end_date,
                )
                or any(
                    event.activity_date <= financial_year.end_date
                    for event in reports[index].events
                )
                for pairs in possible
                for index, bundle_index in pairs
                if bundle_index in named_bundles
            )
        )
        raise RobinhoodReconciliationError(
            f"no activity report assignment reconciles ({details})",
            evidence_period=bool(possible)
            and remedy_fits
            and all(pairs & failures.keys() <= evidence_period_failures for pairs in possible),
        )
    return candidates[0]


def _statements_show_activity(
    bundle: RobinhoodStatementBundle, opening_date: date, end: date
) -> bool:
    """Whether ``bundle``'s statements print a row dated from ``opening_date``
    to ``end`` that reconciliation pairs with a report event, so a report
    for that range holds at least one event."""
    return any(
        row.transaction_code != ROBINHOOD_CRYPTO_CODE
        and opening_date <= row.transaction_date <= end
        for statement in bundle.statements
        for row in statement.activities
    )


def _managed_activity_on_other_family(
    report: RobinhoodActivityReportEvidence,
    bundle: RobinhoodStatementBundle,
) -> bool:
    return any(event.kind in ROBINHOOD_MANAGED_TRADE_KINDS for event in report.events) and (
        bundle.family is not RobinhoodStatementFamily.MANAGED_INDIVIDUAL
    )


def _bounded_bundle(
    source: RobinhoodStatementBundle,
    report: RobinhoodActivityReportEvidence,
    account: RobinhoodAccountConfig,
) -> RobinhoodStatementBundle:
    if any(
        event.activity_date < report.start_date or event.activity_date > report.end_date
        for event in report.events
    ):
        raise _fail("activity event falls outside the attested report range")
    if report.start_date > account.opening_date:
        raise _evidence_period_failure(
            "activity report starts after {account}'s opening date", source
        )
    if any(event.activity_date < account.opening_date for event in report.events):
        # An account has no activity before its opening date, so this pair is
        # never the right one, whatever dates the evidence covers (#1508).
        raise _RuledOutPairingError("activity event predates the configured account opening date")
    count = sum(statement.period_end <= report.end_date for statement in source.statements)
    if count == 0:
        raise _evidence_period_failure(
            "activity report range contains no complete statement period of {account}", source
        )
    if count < len(source.statements):
        trailing = source.statements[count]
        if trailing.period_start <= report.end_date < trailing.period_end:
            raise _evidence_period_failure(
                "activity report ends partway through one of {account}'s statement periods",
                source,
            )
    statements = source.statements[:count]
    if statements[-1].period_end != report.end_date:
        raise _evidence_period_failure(
            "activity report ends after {account}'s last statement", source
        )
    return replace(
        source,
        coverage_end=statements[-1].period_end,
        statements=statements,
    )


def reconcile_robinhood_accounts(
    bundles: Iterable[RobinhoodStatementBundle],
    reports: Iterable[RobinhoodActivityReportEvidence],
    configured_accounts: Iterable[RobinhoodAccountConfig],
    cusip_transitions: Iterable[RobinhoodCusipTransition] = (),
    *,
    financial_year: FinancialYear | None = None,
    schedule_fa: bool = True,
) -> RobinhoodAccountsReconciliation:
    """Require one unique, complete two-report/two-account reconciliation.

    When supplied, ``financial_year`` bounds the loader's remedy mark only,
    as ``schedule_fa`` (whether the run builds Schedule FA) does.
    """

    bundle_tuple = tuple(bundles)
    report_tuple = tuple(reports)
    account_tuple = tuple(configured_accounts)
    transition_tuple = tuple(cusip_transitions)
    _validate_inputs(bundle_tuple, report_tuple, account_tuple)
    account_by_number = {account.number: account for account in account_tuple}
    for bundle in bundle_tuple:
        _validate_bundle_account(bundle, account_by_number[bundle.account_number])
    assignment = _assign_reports(
        report_tuple,
        bundle_tuple,
        account_tuple,
        transition_tuple,
        financial_year=financial_year,
        schedule_fa=schedule_fa,
    )

    results: list[RobinhoodAccountReconciliation] = []
    for bundle_index, (report_index, activity) in enumerate(assignment):
        evidence = report_tuple[report_index]
        cash = reconcile_robinhood_cash(activity, evidence.events)
        results.append(
            RobinhoodAccountReconciliation(
                report_index=report_index,
                evidence=evidence,
                activity=activity,
                cash=cash,
                source_bundle=bundle_tuple[bundle_index],
            )
        )

    account_results = (results[0], results[1])
    transfers = reconcile_robinhood_transfers(
        account_results[0].cash,
        account_results[1].activity.primary_bundle,
        same_beneficial_owner=True,
    )
    if (
        transfers.cash_reconciliation.activity_reconciliation.primary_bundle
        is not account_results[0].activity.primary_bundle
        or transfers.counterpart_bundle is not account_results[1].activity.primary_bundle
    ):
        raise _fail("paired transfer result does not retain bounded account identity")
    return RobinhoodAccountsReconciliation(
        accounts=account_results,
        transfers=transfers,
    )
