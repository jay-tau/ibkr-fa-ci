"""Reconcile primary Fidelity CMA statements against Form 1042-S."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.normalize.fidelity_cma.year_end import (
    CmaYearEndError,
    require_year_end_income_match,
)
from ibkr_fa.normalize.form_1042s import (
    Form1042SAmountError,
    form_1042s_violations,
    whole_dollars,
)
from ibkr_fa.parser.fidelity_cma import (
    CmaIncomeKind,
    CmaStatement,
    CmaStatementBundle,
    CmaYearEndIncomeSummary,
)
from ibkr_fa.parser.fidelity_cma_1042s import (
    Form1042SPacket,
    Form1042SRecord,
)

_EXPECTED_RECORDS: Final[dict[str, tuple[str, str, Decimal]]] = {
    "01:0.00": ("01", "02", Decimal("0.00")),
    "06:0.00": ("06", "02", Decimal("0.00")),
    "06:25.00": ("06", "00", Decimal("25.00")),
}
_CHAPTER4_EXEMPTION_CODE: Final[str] = "15"


@dataclass(frozen=True, slots=True)
class Cma1042SReconciliationError(IbkrFaError):
    message: str

    def __str__(self) -> str:
        return f"Fidelity CMA Form 1042-S reconciliation refused: {self.message}"


@dataclass(frozen=True, slots=True, repr=False)
class Reconciled1042SBucket:
    record_key: str
    statement_event_count: int


@dataclass(frozen=True, slots=True, repr=False)
class Cma1042SReconciliation:
    tax_year: int
    buckets: tuple[Reconciled1042SBucket, ...]
    withholding_event_count: int
    form_record_count: int


def _fail(message: str) -> Cma1042SReconciliationError:
    return Cma1042SReconciliationError(message)


def add_statements_from(start: str) -> str:
    """The recovery for statements that carry a balance into a late start: add
    the monthly statements from ``start``. The FY and Form 1042-S coverage
    refusals word it alike (#1586)."""
    return (
        f"add every monthly statement from {start} (or from the account's first "
        "statement) up to the earliest one given"
    )


def carried_balance_gap(start: str, *, add_from: str | None = None) -> str:
    """The gap clause for statements that start after ``start`` with a
    carried balance: add the months from ``add_from`` (by default
    ``start``)."""
    return (
        f"the statements start after {start} with a carried balance, so "
        f"{add_statements_from(start if add_from is None else add_from)}"
    )


def _whole_dollars(values: list[Decimal], what: str) -> int:
    try:
        return whole_dollars(values)
    except Form1042SAmountError as exc:
        raise CmaYearEndError(f"{what} is not a bounded whole-cent USD amount") from exc


def _validate_records(packet: Form1042SPacket) -> dict[str, Form1042SRecord]:
    records = {record.record_key: record for record in packet.records}
    if set(records) != set(_EXPECTED_RECORDS):
        raise _fail("unexpected income-code/withholding-rate records")
    for key, record in records.items():
        income_code, chapter3_exemption, chapter3_rate = _EXPECTED_RECORDS[key]
        form = record.form
        if (
            form_1042s_violations(form)
            or form.income_code != income_code
            or form.chapter3_exemption_code != chapter3_exemption
            or form.chapter3_tax_rate_percent != chapter3_rate
            or form.chapter4_exemption_code != _CHAPTER4_EXEMPTION_CODE
            or form.net_income.value != 0
        ):
            raise _fail("Form 1042-S record contains unsupported tax semantics")
    if (
        records["01:0.00"].form.federal_tax_withheld.value != 0
        or records["06:0.00"].form.federal_tax_withheld.value != 0
    ):
        raise _fail("zero-rate Form 1042-S record contains withholding")
    return records


def _validate_coverage(
    statements: tuple[CmaStatement, ...],
    year_end: CmaYearEndIncomeSummary,
    packet: Form1042SPacket,
    tax_year: int,
    coverage_clauses: Sequence[str],
) -> None:
    if packet.tax_year != tax_year or year_end.tax_year != tax_year:
        raise _fail("tax years disagree")
    if (
        packet.account_id != year_end.account_id
        or packet.account_holder != year_end.account_holder
        or packet.recipient_country_code != "IN"
        or packet.withholding_agent_name != "NATIONAL FINANCIAL SERVICES LLC"
    ):
        raise _fail("tax-form, statement, or year-end identities disagree")
    if (
        any(
            statement.account_id != packet.account_id
            or statement.account_holder != packet.account_holder
            or statement.currency != "USD"
            for statement in statements
        )
        or year_end.currency != "USD"
    ):
        raise _fail("tax-form and statement account/currency identities disagree")

    january_first = date(tax_year, 1, 1)
    december_last = date(tax_year, 12, 31)
    following_january_last = date(tax_year + 1, 1, 31)
    first = statements[0]
    if first.period_start > december_last:
        raise _fail("statement coverage starts after the tax year")
    if first.period_start > january_first and first.beginning_account_value_usd is not None:
        start = f"1 January {tax_year}"
        raise _fail(
            "; ".join(
                (
                    f"statement coverage for Form 1042-S tax year {tax_year} must start by "
                    f"{start}, or at the account's first statement if it opened later with no "
                    "carried balance",
                    carried_balance_gap(start),
                    *coverage_clauses,
                )
            )
        )
    if not any(
        statement.period_start == date(tax_year + 1, 1, 1)
        and statement.period_end == following_january_last
        for statement in statements
    ):
        raise _fail("following-January statement is required for late-posted interest")


def _tax_year_events(
    statements: tuple[CmaStatement, ...],
    tax_year: int,
) -> tuple[
    list[Decimal],
    list[Decimal],
    list[Decimal],
    list[Decimal],
]:
    interest: list[Decimal] = []
    dividends_by_date: dict[date, list[Decimal]] = defaultdict(list)
    withholding_by_date: dict[date, list[Decimal]] = defaultdict(list)
    for statement in statements:
        for credit in statement.income_credits:
            if credit.date.year != tax_year:
                continue
            if credit.kind is CmaIncomeKind.CREDIT_INTEREST:
                interest.append(credit.amount_usd)
            else:
                dividends_by_date[credit.date].append(credit.amount_usd)
        for withholding in statement.withholdings:
            if withholding.date.year == tax_year:
                withholding_by_date[withholding.date].append(withholding.amount_usd.copy_abs())

    if any(len(values) != 1 for values in dividends_by_date.values()):
        raise _fail("a dividend date is duplicated or ambiguous")
    if any(len(values) != 1 for values in withholding_by_date.values()):
        raise _fail("a withholding date is duplicated or ambiguous")
    if set(withholding_by_date) - set(dividends_by_date):
        raise _fail("withholding cannot be paired to a dividend")

    no_wht: list[Decimal] = []
    with_wht: list[Decimal] = []
    for payment_date, values in dividends_by_date.items():
        (with_wht if payment_date in withholding_by_date else no_wht).append(values[0])
    withholding = [values[0] for values in withholding_by_date.values()]
    return interest, no_wht, with_wht, withholding


def _require_bucket_match(
    record: Form1042SRecord,
    events: list[Decimal],
) -> Reconciled1042SBucket:
    if _whole_dollars(events, "statement income") != record.form.gross_income.value:
        raise _fail("rounded statement income does not match Form 1042-S Box 2")
    return Reconciled1042SBucket(record.record_key, len(events))


def reconcile_1042s(
    statements: CmaStatementBundle,
    year_end: CmaYearEndIncomeSummary,
    packet: Form1042SPacket,
    *,
    tax_year: int,
    coverage_clauses: Sequence[str] = (),
) -> Cma1042SReconciliation:
    """Require exact agreement after sum-then-round-half-up to whole dollars.

    :func:`ibkr_fa.normalize.form_1042s.whole_dollars` states the convention
    and its sources. Only the 1 January coverage refusal appends
    ``coverage_clauses``: the caller's own later coverage needs, so adding
    what it names clears them too (#1515).
    """
    statement_bundle = statements.statements
    _validate_coverage(statement_bundle, year_end, packet, tax_year, coverage_clauses)
    records = _validate_records(packet)
    interest, no_wht_dividends, with_wht_dividends, withholding = _tax_year_events(
        statement_bundle, tax_year
    )
    try:
        require_year_end_income_match(statement_bundle, year_end, tax_year=tax_year)
    except CmaYearEndError as exc:
        raise _fail(str(exc)) from exc
    buckets = (
        _require_bucket_match(records["01:0.00"], interest),
        _require_bucket_match(records["06:0.00"], no_wht_dividends),
        _require_bucket_match(records["06:25.00"], with_wht_dividends),
    )
    expected_wht_dollars = _whole_dollars(withholding, "statement withholding")
    withheld = [record.form.federal_tax_withheld.value for record in packet.records]
    if (
        expected_wht_dollars != records["06:25.00"].form.federal_tax_withheld.value
        or any(value is None or value != value.to_integral_value() for value in withheld)
        or expected_wht_dollars != sum(int(value) for value in withheld if value is not None)
    ):
        raise _fail("rounded statement withholding does not match Form 1042-S")
    return Cma1042SReconciliation(
        tax_year,
        buckets,
        len(withholding),
        len(packet.records),
    )
