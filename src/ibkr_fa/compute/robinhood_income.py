"""Convert reconciled Robinhood income evidence into shared FTC models."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from enum import StrEnum

from ibkr_fa.compute.robinhood_inputs import (
    holds_two_accounts,
    is_one_financial_year,
    same_distinct_account_numbers,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import RobinhoodConfig, RobinhoodInterestKind, RobinhoodSecurityConfig
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.invariants import invariant_error
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.form_1042s import Form1042SAmount, Form1042SAmountKind, Form1042SCopyType
from ibkr_fa.models.interest_credit import InterestCredit
from ibkr_fa.models.other_source_income import OtherSourceIncome
from ibkr_fa.models.substitute_dividend_income import SubstituteDividendIncome
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.normalize.form_1042s import (
    Form1042SAmountError,
    form_1042s_violations,
    whole_dollars,
)
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountReconciliation,
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.security import (
    resolve_robinhood_security_identities,
)
from ibkr_fa.parser.robinhood import (
    ROBINHOOD_FEE_KINDS,
    ROBINHOOD_INCOME_AND_FEE_KINDS,
    ROBINHOOD_INCOME_KINDS,
    RobinhoodEvent,
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_1042s import (
    Robinhood1042SCopy,
    Robinhood1042SPacket,
    Robinhood1042SSummaryRow,
)
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementLayout
from ibkr_fa.types import CountryCode

_ARITHMETIC_CONTEXT = Context(prec=60)
_CENT = Decimal("0.01")
_WHOLE_DOLLAR = Decimal("1")
_COPY_COUNT = 2
_DIVIDEND_CODE = "06"
_BROKERAGE_INTEREST_CODE = "01"
_SUBSTITUTE_DIVIDEND_CODE = "34"
# Form 1042-S income codes for substitute payments, in the order the
# SUBSTITUTE_PAYMENT cause text lists them.
SUBSTITUTE_PAYMENT_CODES = ("33", "34", "35", "53", "54")
_SUPPORTED_CODES = frozenset({_DIVIDEND_CODE, _BROKERAGE_INTEREST_CODE, _SUBSTITUTE_DIVIDEND_CODE})
_INCOME_LABELS = frozenset(
    {
        "Dividends",
        "Capital Gains Distributions",
        "Interest Earned",
        "Stock Lending",
    }
)
_SWEEP_DESCRIPTIONS = frozenset(
    {
        "CLOSING SWEEP BALANCE",
        "FDIC SWEEP",
        "INTEREST PAYMENT",
        "OPENING SWEEP BALANCE",
    }
)
_SWEEP_INTEREST_DESCRIPTION = "INTEREST PAYMENT"
# SEC Release No. 34-96930 (88 FR 13872, 13917) made May 28, 2024, the day
# after Memorial Day, the compliance date for T+1 settlement under amended
# Exchange Act Rule 15c6-1(a); its trades settled on May 29. ADR 0006's "held
# at the end of the day before the record date" rule assumes T+1. It fails for
# earlier record dates: a May 24, 2024 trade still settled T+2, on May 29, so
# it missed record date May 28 although it was held at the end of May 27.
_FIRST_US_T1_SETTLEMENT_DATE = date(2024, 5, 29)


class RobinhoodIncomeError(IbkrFaError):
    """Reconciled Robinhood evidence contradicts the income adapter contract."""


class RobinhoodIncomeBlockerKind(StrEnum):
    """A source fact that prevents filing-complete Robinhood income."""

    ANNUAL_FORM_COVERAGE = "annual-form-coverage"
    ANNUAL_FORM_IDENTITY = "annual-form-identity"
    ANNUAL_FORM_RECONCILIATION = "annual-form-reconciliation"
    EVIDENCE_COVERAGE = "evidence-coverage"
    IDENTITY = "identity"
    INTEREST_PAYER_COUNTRY = "interest-payer-country"
    JURISDICTION = "jurisdiction"
    PAYMENT_PAIRING = "payment-pairing"
    PROGRAM_BANK_PARTITION = "program-bank-partition"
    STATEMENT_RECONCILIATION = "statement-reconciliation"
    STOCK_LENDING_CLASSIFICATION = "stock-lending-classification"
    UNSUPPORTED_FORM_SEMANTICS = "unsupported-form-semantics"
    UNSUPPORTED_INCOME = "unsupported-income"


class RobinhoodIncomeBlockerCause(StrEnum):
    """Why a blocker was raised, grouped by its kind (#1487).

    ``PROGRAM_BANK_PARTITION`` and ``INTEREST_PAYER_COUNTRY`` blockers carry
    none: each has its own text (#1477, #698)."""

    # ANNUAL_FORM_COVERAGE: zero or several forms bound to an account/year that
    # needs one. ``SET_ASIDE``: a supplied form naming the account failed an
    # identity check.
    ANNUAL_FORM_MISSING = "annual-form-missing"
    ANNUAL_FORM_SET_ASIDE = "annual-form-set-aside"
    ANNUAL_FORM_DUPLICATED = "annual-form-duplicated"
    # ANNUAL_FORM_IDENTITY
    ANNUAL_FORM_ACCOUNT_UNBOUND = "annual-form-account-unbound"
    ANNUAL_FORM_PARTIES = "annual-form-parties"
    ANNUAL_FORMS_DISAGREE = "annual-forms-disagree"
    # ANNUAL_FORM_RECONCILIATION
    ANNUAL_FORM_SUMMARY = "annual-form-summary"
    ANNUAL_FORM_SOURCE = "annual-form-source"
    # EVIDENCE_COVERAGE: the activity report or statements stop short of the
    # financial year, or of a year a supplied Form 1042-S covers.
    EVIDENCE_SHORT_OF_FINANCIAL_YEAR = "evidence-short-of-financial-year"
    EVIDENCE_SHORT_OF_FORM_YEAR = "evidence-short-of-form-year"
    # IDENTITY: the payment's symbol belongs to a CUSIP with no identity, or to
    # no CUSIP the activity report or statements show.
    UNCONFIGURED_SECURITY = "unconfigured-security"
    UNMATCHED_SYMBOL = "unmatched-symbol"
    # JURISDICTION
    ISSUER_COUNTRY = "issuer-country"
    US_WITHHOLDING_ON_NON_US_DIVIDEND = "us-withholding-on-non-us-dividend"
    LENDING_FEE_SOURCE = "lending-fee-source"
    # PAYMENT_PAIRING
    WITHHOLDING_WITHOUT_DIVIDEND = "withholding-without-dividend"
    WITHHOLDING_SEVERAL_DIVIDENDS = "withholding-several-dividends"
    DIVIDEND_SEVERAL_WITHHOLDINGS = "dividend-several-withholdings"
    # STATEMENT_RECONCILIATION: a statement spans two calendar years, or its
    # Income summary line disagrees with the activity report.
    STATEMENT_SPANS_YEARS = "statement-spans-years"
    STATEMENT_DIVIDENDS = "statement-dividends"
    STATEMENT_INTEREST = "statement-interest"
    STATEMENT_STOCK_LENDING = "statement-stock-lending"
    # STOCK_LENDING_CLASSIFICATION
    SUBSTITUTE_PAYMENT = "substitute-payment"
    # UNSUPPORTED_FORM_SEMANTICS
    UNSUPPORTED_FORM_CONTENT = "unsupported-form-content"
    # UNSUPPORTED_INCOME
    CAPITAL_GAINS_DISTRIBUTIONS = "capital-gains-distributions"


# Kinds that leave exact source evidence sufficient for Schedule OS.
_FTC_ONLY_KINDS = frozenset(
    {
        RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE,
        RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
        RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
        RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY,
        RobinhoodIncomeBlockerKind.JURISDICTION,
        RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
    }
)


class RobinhoodAnnualFormDiscrepancyDirection(StrEnum):
    """Direction of a bounded annual-form gross mismatch."""

    FORM_ABOVE_SOURCE = "form-above-source"
    FORM_BELOW_SOURCE = "form-below-source"


class RobinhoodStockLendingSubtype(StrEnum):
    """Tax subtype proven by the annual source-evidence pool."""

    SECURITIES_LENDING_FEE = "securities-lending-fee"
    SUBSTITUTE_DIVIDEND = "substitute-dividend"


class RobinhoodFeeTreatment(StrEnum):
    """Current filing disposition for an already-reconciled cash fee."""

    RECONCILIATION_ONLY_NO_DEDUCTION = "reconciliation-only-no-deduction"


@dataclass(frozen=True, slots=True, repr=False, order=True)
class RobinhoodIncomeEventReference:
    """Privacy-safe identity of one source event inside a paired result."""

    account_index: int
    event_ordinal: int


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodIncomeBlocker:
    """One blocker without private source values."""

    kind: RobinhoodIncomeBlockerKind
    account_indices: tuple[int, ...] = ()
    event_references: tuple[RobinhoodIncomeEventReference, ...] = ()
    tax_years: tuple[int, ...] = ()
    causes: tuple[RobinhoodIncomeBlockerCause, ...] = ()
    """Why it was raised, in enum order (#1487)."""

    @property
    def stops_schedule_os(self) -> bool:
        """Whether exact source evidence falls short of Schedule OS, not just
        FTC. A coverage blocker for an annual-form year stops only FTC."""
        return self.kind not in _FTC_ONLY_KINDS and not (
            self.kind is RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE and self.tax_years
        )


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAnnualFormDiscrepancy:
    """One zero-tax annual bucket superseded by exact source interest."""

    packet_index: int
    account_index: int
    tax_year: int
    income_code: str
    direction: RobinhoodAnnualFormDiscrepancyDirection
    event_references: tuple[RobinhoodIncomeEventReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAnnualFormPendingEvidence:
    """FY-end-year source evidence retained without an annual form."""

    account_index: int
    tax_year: int
    income_references: tuple[RobinhoodIncomeEventReference, ...]
    withholding_references: tuple[RobinhoodIncomeEventReference, ...]
    unpaired_us_dividend_references: tuple[RobinhoodIncomeEventReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodDividendPayment:
    """One source dividend plus facts the generic model cannot retain."""

    event_reference: RobinhoodIncomeEventReference
    security_config_index: int
    source_record_date: date
    source_payment_date: date
    """The description's ``P/D``: audit evidence that groups one dividend's A3
    payments. It never selects a year; ``dividend.payment_date`` (the Activity
    Date, ADR 0005) does."""
    jurisdiction: CountryCode
    withholding_references: tuple[RobinhoodIncomeEventReference, ...]
    in_financial_year: bool
    dividend: Dividend


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodInterestPayment:
    """One source interest credit and its brokerage/bank classification."""

    event_reference: RobinhoodIncomeEventReference
    program_bank: bool | None
    in_financial_year: bool
    interest_credit: InterestCredit


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStockLendingPayment:
    """One SLIP row plus its aggregate annual-form classification."""

    event_reference: RobinhoodIncomeEventReference
    security_config_index: int | None
    issuer_country_iso2: CountryCode | None
    source_country_iso2: CountryCode | None
    withholding_agent_country_iso2: CountryCode | None
    in_financial_year: bool
    subtype: RobinhoodStockLendingSubtype | None
    annual_bucket_index: int | None
    loan_fee_income: OtherSourceIncome | None
    substitute_dividend_income: SubstituteDividendIncome | None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodFeePayment:
    """One ADR or management fee retained without claiming a deduction."""

    event_reference: RobinhoodIncomeEventReference
    source_kind: RobinhoodEventKind
    security_config_index: int | None
    in_financial_year: bool
    treatment: RobinhoodFeeTreatment = RobinhoodFeeTreatment.RECONCILIATION_ONLY_NO_DEDUCTION


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodWithholdingPayment:
    """One source withholding row paired to one dividend."""

    event_reference: RobinhoodIncomeEventReference
    dividend_reference: RobinhoodIncomeEventReference
    source_kind: RobinhoodEventKind
    jurisdiction: CountryCode
    in_financial_year: bool
    withholding_tax: WithholdingTax


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAnnualIncomeBucket:
    """One deduplicated 1042-S rate pool reconciled to source events."""

    packet_index: int
    account_index: int
    tax_year: int
    income_code: str
    chapter3_exemption_code: str
    chapter3_tax_rate_percent: Decimal
    chapter4_exemption_code: str
    chapter4_tax_rate_percent: Decimal
    gross_income_usd: Decimal
    source_gross_income_usd: Decimal
    gross_income_kind: Form1042SAmountKind
    withholding_usd: Decimal
    source_withholding_usd: Decimal
    withholding_kind: Form1042SAmountKind
    withholding_agent_country_iso2: CountryCode
    payment_references: tuple[RobinhoodIncomeEventReference, ...]
    withholding_references: tuple[RobinhoodIncomeEventReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodIncomeResult:
    """Review provenance and all-or-nothing assembler-ready inputs."""

    dividend_payments: tuple[RobinhoodDividendPayment, ...]
    interest_payments: tuple[RobinhoodInterestPayment, ...]
    stock_lending_payments: tuple[RobinhoodStockLendingPayment, ...]
    fee_payments: tuple[RobinhoodFeePayment, ...]
    withholding_payments: tuple[RobinhoodWithholdingPayment, ...]
    annual_buckets: tuple[RobinhoodAnnualIncomeBucket, ...]
    blockers: tuple[RobinhoodIncomeBlocker, ...]
    excluded_out_of_fy_event_references: tuple[RobinhoodIncomeEventReference, ...]
    excluded_packet_indices: tuple[int, ...]
    zero_round_unreported_interest_references: tuple[RobinhoodIncomeEventReference, ...]
    unreported_lending_fee_references: tuple[RobinhoodIncomeEventReference, ...]
    annual_form_discrepancies: tuple[RobinhoodAnnualFormDiscrepancy, ...] = ()
    pending_annual_form_evidence: tuple[RobinhoodAnnualFormPendingEvidence, ...] = ()
    partition_only_payer_accounts: tuple[int, ...] = ()
    """Accounts whose ``INTEREST_PAYER_COUNTRY`` blocker only their unresolved
    ``PROGRAM_BANK_PARTITION`` causes: their reviewed US account/FY entry
    declares every interest kind, so no entry can clear it (#1477)."""

    @property
    def filing_complete(self) -> bool:
        return not self.blockers

    @property
    def dividends(self) -> tuple[Dividend, ...]:
        if not self.filing_complete:
            return ()
        return tuple(item.dividend for item in self.dividend_payments if item.in_financial_year)

    @property
    def interest_credits(self) -> tuple[InterestCredit, ...]:
        if not self.filing_complete:
            return ()
        return tuple(
            item.interest_credit for item in self.interest_payments if item.in_financial_year
        )

    @property
    def withholding_taxes(self) -> tuple[WithholdingTax, ...]:
        if not self.filing_complete:
            return ()
        return tuple(
            item.withholding_tax for item in self.withholding_payments if item.in_financial_year
        )

    @property
    def substitute_dividend_incomes(self) -> tuple[SubstituteDividendIncome, ...]:
        if not self.filing_complete:
            return ()
        return tuple(
            item.substitute_dividend_income
            for item in self.stock_lending_payments
            if item.in_financial_year and item.substitute_dividend_income is not None
        )

    @property
    def schedule_os_complete(self) -> bool:
        """Whether exact source evidence is sufficient for Schedule OS."""

        return not any(blocker.stops_schedule_os for blocker in self.blockers)

    @property
    def schedule_os_dividends(self) -> tuple[Dividend, ...]:
        if not self.schedule_os_complete:
            return ()
        return tuple(item.dividend for item in self.dividend_payments if item.in_financial_year)

    @property
    def schedule_os_interest_credits(self) -> tuple[InterestCredit, ...]:
        if not self.schedule_os_complete:
            return ()
        return tuple(
            item.interest_credit for item in self.interest_payments if item.in_financial_year
        )

    @property
    def other_source_incomes(self) -> tuple[OtherSourceIncome, ...]:
        if not self.schedule_os_complete:
            return ()
        return tuple(
            item.loan_fee_income
            for item in self.stock_lending_payments
            if item.in_financial_year and item.loan_fee_income is not None
        )

    @property
    def filing_other_source_incomes(self) -> tuple[OtherSourceIncome, ...]:
        if not self.filing_complete:
            return ()
        return tuple(
            item.loan_fee_income
            for item in self.stock_lending_payments
            if item.in_financial_year and item.loan_fee_income is not None
        )


@dataclass(frozen=True, slots=True, repr=False)
class _DividendSource:
    reference: RobinhoodIncomeEventReference
    event: RobinhoodEvent
    security_config_index: int
    jurisdiction: CountryCode
    in_financial_year: bool


@dataclass(frozen=True, slots=True, repr=False)
class _TaxSource:
    reference: RobinhoodIncomeEventReference
    event: RobinhoodEvent
    in_financial_year: bool


@dataclass(frozen=True, slots=True, repr=False)
class _StockLendingSource:
    reference: RobinhoodIncomeEventReference
    event: RobinhoodEvent
    security_config_index: int | None
    issuer_country_iso2: CountryCode | None
    in_financial_year: bool


@dataclass(frozen=True, slots=True, repr=False)
class _FormBucket:
    packet_index: int
    account_index: int
    copy: Robinhood1042SCopy
    gross_income_usd: Decimal
    withholding_usd: Decimal


def _fail(message: str) -> RobinhoodIncomeError:
    return RobinhoodIncomeError(message)


def _holding_date(record_date: date) -> date | None:
    """Return the A3 holding date, or ``None`` before T+1 settlement began."""
    if record_date < _FIRST_US_T1_SETTLEMENT_DATE:
        return None
    return record_date - timedelta(days=1)


def _blocker(
    kind: RobinhoodIncomeBlockerKind,
    *causes: RobinhoodIncomeBlockerCause,
    account_indices: Iterable[int] = (),
    event_references: Iterable[RobinhoodIncomeEventReference] = (),
    tax_years: Iterable[int] = (),
) -> RobinhoodIncomeBlocker:
    return RobinhoodIncomeBlocker(
        kind=kind,
        account_indices=tuple(sorted(set(account_indices))),
        event_references=tuple(sorted(set(event_references))),
        tax_years=tuple(sorted(set(tax_years))),
        causes=_ordered_causes(causes),
    )


def _ordered_causes(
    causes: Iterable[RobinhoodIncomeBlockerCause],
) -> tuple[RobinhoodIncomeBlockerCause, ...]:
    members = tuple(RobinhoodIncomeBlockerCause)
    return tuple(sorted(set(causes), key=members.index))


def _sorted_blockers(
    blockers: Iterable[RobinhoodIncomeBlocker],
) -> tuple[RobinhoodIncomeBlocker, ...]:
    """One blocker per kind, accounts, years and events, in that order; a
    blocker raised again for another cause gains that cause (#1487)."""
    causes: dict[
        tuple[
            RobinhoodIncomeBlockerKind,
            tuple[int, ...],
            tuple[int, ...],
            tuple[RobinhoodIncomeEventReference, ...],
        ],
        list[RobinhoodIncomeBlockerCause],
    ] = defaultdict(list)
    for item in blockers:
        key = (item.kind, item.account_indices, item.tax_years, item.event_references)
        causes[key].extend(item.causes)
    return tuple(
        RobinhoodIncomeBlocker(
            kind=kind,
            account_indices=account_indices,
            event_references=event_references,
            tax_years=tax_years,
            causes=_ordered_causes(key_causes),
        )
        for (kind, account_indices, tax_years, event_references), key_causes in sorted(
            causes.items(), key=lambda item: (item[0][0].value, *item[0][1:])
        )
    )


def _validate_inputs(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    financial_year_start: date,
    financial_year_end: date,
) -> dict[str, int]:
    if not is_one_financial_year(financial_year_start, financial_year_end):
        raise _fail("income requires one complete April-to-March financial year")
    if not holds_two_accounts(paired, config):
        raise _fail("income requires the reconciled two-account Robinhood bundle")
    if not same_distinct_account_numbers(paired, config):
        raise _fail("income config does not match the reconciled account identities")
    if {account.country_iso2 for account in config.accounts} != {"US"}:
        raise _fail("income requires US-domiciled Robinhood account identities")
    return {account.number: index for index, account in enumerate(config.accounts)}


def _source_events(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...]:
    rows: list[tuple[RobinhoodIncomeEventReference, RobinhoodEvent]] = []
    seen: set[RobinhoodIncomeEventReference] = set()
    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            reference = RobinhoodIncomeEventReference(account_index, event.source_record_index)
            if reference in seen:
                raise _fail("reconciled activity contains a duplicate event reference")
            seen.add(reference)
            if event.kind in ROBINHOOD_INCOME_AND_FEE_KINDS:
                rows.append((reference, event))
    return tuple(sorted(rows))


def _identity_by_symbol(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
) -> tuple[dict[str, int], frozenset[str]]:
    """Each resolved symbol's security index, and the symbols a configured
    entry would resolve for each CUSIP with no security identity: its report
    and statement symbols (#1487, #1497)."""
    result = resolve_robinhood_security_identities(paired, securities)
    by_symbol: dict[str, int] = {}
    for resolved in result.resolved:
        for symbol in resolved.observed_symbols:
            previous = by_symbol.setdefault(symbol, resolved.config_index)
            if previous != resolved.config_index:
                raise _fail("one source symbol resolves to multiple configured securities")
    unconfigured = frozenset(
        symbol for blocker in result.blockers for symbol in blocker.observed_symbols
    )
    return by_symbol, unconfigured


def _identity_blocker(
    reference: RobinhoodIncomeEventReference,
    event: RobinhoodEvent,
    unconfigured_symbols: frozenset[str],
) -> RobinhoodIncomeBlocker:
    """The blocker for a payment whose symbol resolves to no security."""
    return _blocker(
        RobinhoodIncomeBlockerKind.IDENTITY,
        (
            RobinhoodIncomeBlockerCause.UNCONFIGURED_SECURITY
            if event.instrument in unconfigured_symbols
            else RobinhoodIncomeBlockerCause.UNMATCHED_SYMBOL
        ),
        account_indices=(reference.account_index,),
        event_references=(reference,),
        tax_years=(event.activity_date.year,),
    )


def _distribution_key(event: RobinhoodEvent) -> tuple[object, ...]:
    if (
        event.instrument is None
        or event.record_date is None
        or event.payment_date is None
        or event.description_shares is None
        or event.description_rate_usd is None
    ):
        raise _fail("distribution event lacks parser-proved payment identity")
    return (
        event.instrument,
        event.activity_date,
        event.record_date,
        event.payment_date,
        event.description_shares,
        event.description_rate_usd,
    )


def _fee_payment(
    reference: RobinhoodIncomeEventReference,
    event: RobinhoodEvent,
    identity_by_symbol: dict[str, int],
    in_financial_year: bool,
) -> RobinhoodFeePayment:
    return RobinhoodFeePayment(
        event_reference=reference,
        source_kind=event.kind,
        security_config_index=(
            None if event.instrument is None else identity_by_symbol.get(event.instrument)
        ),
        in_financial_year=in_financial_year,
    )


def _stock_lending_source(
    reference: RobinhoodIncomeEventReference,
    event: RobinhoodEvent,
    securities: tuple[RobinhoodSecurityConfig, ...],
    identity_by_symbol: dict[str, int],
    unconfigured_symbols: frozenset[str],
    in_financial_year: bool,
) -> tuple[_StockLendingSource, tuple[RobinhoodIncomeBlocker, ...]]:
    if event.instrument is None or event.amount_usd is None:
        raise _fail("Stock Lending event lacks its source identity or amount")
    config_index = identity_by_symbol.get(event.instrument)
    issuer_country: CountryCode | None = None
    blockers: list[RobinhoodIncomeBlocker] = []
    if config_index is None:
        blockers.append(_identity_blocker(reference, event, unconfigured_symbols))
    else:
        security = securities[config_index]
        issuer_country = security.instrument.issuer_country_code
    return (
        _StockLendingSource(
            reference=reference,
            event=event,
            security_config_index=config_index,
            issuer_country_iso2=issuer_country,
            in_financial_year=in_financial_year,
        ),
        tuple(blockers),
    )


def _income_sources(
    rows: tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
    securities: tuple[RobinhoodSecurityConfig, ...],
    identity_by_symbol: dict[str, int],
    unconfigured_symbols: frozenset[str],
    financial_year_start: date,
    financial_year_end: date,
) -> tuple[
    tuple[_DividendSource, ...],
    tuple[RobinhoodInterestPayment, ...],
    tuple[_TaxSource, ...],
    tuple[_StockLendingSource, ...],
    tuple[RobinhoodFeePayment, ...],
    list[RobinhoodIncomeBlocker],
    tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
]:
    """Sort the source rows by kind. The last item holds the dividends whose
    symbol resolves to no security: each gets an identity blocker instead of
    a payment."""
    dividends: list[_DividendSource] = []
    interests: list[RobinhoodInterestPayment] = []
    taxes: list[_TaxSource] = []
    stock_lending: list[_StockLendingSource] = []
    fees: list[RobinhoodFeePayment] = []
    blockers: list[RobinhoodIncomeBlocker] = []
    unidentified: list[tuple[RobinhoodIncomeEventReference, RobinhoodEvent]] = []
    for reference, event in rows:
        in_fy = financial_year_start <= event.activity_date <= financial_year_end
        if event.kind in ROBINHOOD_FEE_KINDS:
            fees.append(_fee_payment(reference, event, identity_by_symbol, in_fy))
            continue
        if event.kind is RobinhoodEventKind.STOCK_LENDING_INCOME:
            source, source_blockers = _stock_lending_source(
                reference,
                event,
                securities,
                identity_by_symbol,
                unconfigured_symbols,
                in_fy,
            )
            stock_lending.append(source)
            blockers.extend(source_blockers)
            continue
        if event.kind is RobinhoodEventKind.CASH_INTEREST:
            if event.amount_usd is None:
                raise _fail("interest event lacks its positive source amount")
            interests.append(
                RobinhoodInterestPayment(
                    event_reference=reference,
                    program_bank=None,
                    in_financial_year=in_fy,
                    interest_credit=InterestCredit(
                        credit_date=event.activity_date,
                        amount_gross=event.amount_usd,
                        currency="USD",
                        description=event.description,
                        source_row_index=event.source_record_index,
                    ),
                )
            )
            continue
        if event.kind in {
            RobinhoodEventKind.NRA_WITHHOLDING,
            RobinhoodEventKind.FOREIGN_TAX_WITHHOLDING,
        }:
            taxes.append(_TaxSource(reference, event, in_fy))
            continue
        if event.kind is not RobinhoodEventKind.CASH_DIVIDEND:
            continue
        if event.instrument is None or event.amount_usd is None:
            raise _fail("dividend event lacks its source identity or amount")
        config_index = identity_by_symbol.get(event.instrument)
        if config_index is None:
            blockers.append(_identity_blocker(reference, event, unconfigured_symbols))
            unidentified.append((reference, event))
            continue
        security = securities[config_index]
        jurisdiction = security.instrument.issuer_country_code
        if jurisdiction != str(security.instrument.isin)[:2]:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.JURISDICTION,
                    RobinhoodIncomeBlockerCause.ISSUER_COUNTRY,
                    account_indices=(reference.account_index,),
                    event_references=(reference,),
                    tax_years=(event.activity_date.year,),
                )
            )
        dividends.append(_DividendSource(reference, event, config_index, jurisdiction, in_fy))
    return (
        tuple(dividends),
        tuple(interests),
        tuple(taxes),
        tuple(stock_lending),
        tuple(fees),
        blockers,
        tuple(unidentified),
    )


def _sweep_description(value: str) -> str:
    return " ".join(value.upper().split())


def _sweep_interest_counts(
    account: RobinhoodAccountReconciliation,
    interests: tuple[RobinhoodInterestPayment, ...],
) -> tuple[Counter[tuple[date, Decimal]], bool]:
    sweep_counts: Counter[tuple[date, Decimal]] = Counter()
    unsafe = False
    statements = account.activity.primary_bundle.statements
    if len(account.cash.periods) != len(statements):
        raise _fail("cash periods do not align with bounded statements")
    for statement_index, statement in enumerate(statements):
        period_events = tuple(
            item
            for item in interests
            if statement.period_start <= item.interest_credit.credit_date <= statement.period_end
        )
        rows = tuple(getattr(statement, "sweep_activity", ()))
        banks = tuple(getattr(statement, "sweep_banks", ()))
        layout = getattr(
            statement,
            "layout",
            RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
        )
        period = account.cash.periods[statement_index]
        if period_events and period.program_bank_present and not rows:
            unsafe = True
        if layout is not RobinhoodStatementLayout.DEPOSIT_SWEEP:
            if rows or banks:
                unsafe = True
            continue
        if banks and not rows:
            unsafe = True
        for row in rows:
            description = _sweep_description(row.description)
            if description not in _SWEEP_DESCRIPTIONS:
                unsafe = True
                continue
            if description != _SWEEP_INTEREST_DESCRIPTION:
                continue
            if row.credit_usd is None or row.credit_usd <= 0 or row.debit_usd is not None:
                unsafe = True
                continue
            sweep_counts[(row.transaction_date, row.credit_usd)] += 1
    return sweep_counts, unsafe


def _partition_interest_payments(
    paired: RobinhoodAccountsReconciliation,
    interests: tuple[RobinhoodInterestPayment, ...],
) -> tuple[
    tuple[RobinhoodInterestPayment, ...],
    tuple[RobinhoodIncomeBlocker, ...],
]:
    decisions: dict[RobinhoodIncomeEventReference, bool] = {}
    blockers: list[RobinhoodIncomeBlocker] = []
    for account_index, account in enumerate(paired.accounts):
        account_interests = tuple(
            item for item in interests if item.event_reference.account_index == account_index
        )
        if not account_interests:
            continue
        events_by_key: dict[
            tuple[date, Decimal],
            list[RobinhoodInterestPayment],
        ] = defaultdict(list)
        for item in account_interests:
            events_by_key[
                (
                    item.interest_credit.credit_date,
                    item.interest_credit.amount_gross,
                )
            ].append(item)

        sweep_counts, unsafe = _sweep_interest_counts(
            account,
            account_interests,
        )

        covering_counts = Counter(
            (
                item.interest_credit.credit_date,
                item.interest_credit.amount_gross,
            )
            for item in account_interests
        )
        if any(
            not any(
                statement.period_start <= item.interest_credit.credit_date <= statement.period_end
                for statement in account.activity.primary_bundle.statements
            )
            for item in account_interests
        ):
            unsafe = True
        if any(
            sweep_count not in {0, covering_counts.get(key, 0)}
            for key, sweep_count in sweep_counts.items()
        ):
            unsafe = True

        if unsafe:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION,
                    account_indices=(account_index,),
                    event_references=(item.event_reference for item in account_interests),
                    tax_years=(item.interest_credit.credit_date.year for item in account_interests),
                )
            )
            continue
        for key, items in events_by_key.items():
            is_bank = sweep_counts[key] == len(items)
            decisions.update((item.event_reference, is_bank) for item in items)

    return (
        tuple(
            replace(
                item,
                program_bank=decisions.get(item.event_reference),
            )
            for item in interests
        ),
        tuple(blockers),
    )


def _interest_kind(item: RobinhoodInterestPayment) -> RobinhoodInterestKind | None:
    """The payment's partition class, or ``None`` while it is unresolved."""
    if item.program_bank is None:
        return None
    if item.program_bank:
        return RobinhoodInterestKind.PROGRAM_BANK
    return RobinhoodInterestKind.BROKERAGE


def _interest_payer_blockers(
    paired: RobinhoodAccountsReconciliation,
    interests: tuple[RobinhoodInterestPayment, ...],
    config: RobinhoodConfig,
    financial_year: int,
) -> tuple[tuple[RobinhoodIncomeBlocker, ...], tuple[int, ...]]:
    """Block FTC for in-FY interest that no reviewed US payer entry covers (#698).

    Only in-FY interest reaches the shared engine, so an account with none
    needs no entry. An account with some needs exactly one complete
    ``[[robinhood.interest_payer_countries]]`` entry for its own account/FY,
    with country US and an interest kind for each payment. There is no
    fallback to the account country or to another account/FY's entry.

    Also returns blocked accounts whose reviewed US account/FY entry declares
    every kind (#1477). Only their unresolved partition leaves a payment without
    a kind, so no entry can clear their blocker.
    """
    blockers: list[RobinhoodIncomeBlocker] = []
    partition_only: list[int] = []
    for account_index, account in enumerate(paired.accounts):
        account_number = account.activity.primary_bundle.account_number
        entries = [
            entry
            for entry in config.interest_payer_countries
            if entry.account_number == account_number and entry.financial_year == financial_year
        ]
        declared: frozenset[RobinhoodInterestKind] = frozenset()
        if len(entries) == 1 and entries[0].country_iso2 == "US":
            entry = entries[0]
            if (
                isinstance(entry.financial_year, int)
                and not isinstance(entry.financial_year, bool)
                and isinstance(entry.interest_kinds, frozenset)
                and entry.interest_kinds <= frozenset(RobinhoodInterestKind)
                and all(
                    isinstance(text, str) and text.strip() for text in (entry.source, entry.reason)
                )
            ):
                declared = entry.interest_kinds
        uncovered = tuple(
            item
            for item in interests
            if item.event_reference.account_index == account_index
            and item.in_financial_year
            and _interest_kind(item) not in declared
        )
        if uncovered:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY,
                    account_indices=(account_index,),
                    event_references=(item.event_reference for item in uncovered),
                    tax_years=(item.interest_credit.credit_date.year for item in uncovered),
                )
            )
            if declared == frozenset(RobinhoodInterestKind):
                partition_only.append(account_index)
    return tuple(blockers), tuple(partition_only)


def _pair_payments(
    dividends: tuple[_DividendSource, ...],
    unidentified: tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
    taxes: tuple[_TaxSource, ...],
    securities: tuple[RobinhoodSecurityConfig, ...],
    unconfigured_symbols: frozenset[str],
) -> tuple[
    tuple[RobinhoodDividendPayment, ...],
    tuple[RobinhoodWithholdingPayment, ...],
    list[RobinhoodIncomeBlocker],
]:
    """Pair each withholding with the one dividend it matches. The match needs
    no security, so ``unidentified`` dividends pair too: a withholding paired
    with one gets the identity blocker its dividend has, and only an
    identified dividend's issuer country is checked against NRA tax (#1507)."""
    sources = {item.reference: item for item in dividends}
    exact: dict[tuple[int, tuple[object, ...]], list[RobinhoodIncomeEventReference]]
    exact = defaultdict(list)
    loose: dict[tuple[int, str, date], list[RobinhoodIncomeEventReference]] = defaultdict(list)
    for reference, event in (*((item.reference, item.event) for item in dividends), *unidentified):
        if event.instrument is None:
            raise _fail("dividend event lacks its source symbol")
        exact[(reference.account_index, _distribution_key(event))].append(reference)
        loose[(reference.account_index, event.instrument, event.activity_date)].append(reference)

    paired: dict[RobinhoodIncomeEventReference, _TaxSource] = {}
    invalid_dividends: set[RobinhoodIncomeEventReference] = set()
    blockers: list[RobinhoodIncomeBlocker] = []
    for tax in taxes:
        event = tax.event
        if event.instrument is None or event.amount_usd is None:
            raise _fail("withholding event lacks its source identity or amount")
        candidates = (
            exact.get((tax.reference.account_index, _distribution_key(event)), [])
            if event.kind is RobinhoodEventKind.NRA_WITHHOLDING
            else loose.get(
                (tax.reference.account_index, event.instrument, event.activity_date),
                [],
            )
        )
        if len(candidates) != 1:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.PAYMENT_PAIRING,
                    (
                        RobinhoodIncomeBlockerCause.WITHHOLDING_SEVERAL_DIVIDENDS
                        if candidates
                        else RobinhoodIncomeBlockerCause.WITHHOLDING_WITHOUT_DIVIDEND
                    ),
                    account_indices=(tax.reference.account_index,),
                    event_references=(tax.reference, *candidates),
                    tax_years=(event.activity_date.year,),
                )
            )
            invalid_dividends.update(candidates)
            continue
        (dividend,) = candidates
        previous = paired.get(dividend)
        if previous is not None:
            invalid_dividends.add(dividend)
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.PAYMENT_PAIRING,
                    RobinhoodIncomeBlockerCause.DIVIDEND_SEVERAL_WITHHOLDINGS,
                    account_indices=(tax.reference.account_index,),
                    event_references=(
                        dividend,
                        previous.reference,
                        tax.reference,
                    ),
                    tax_years=(event.activity_date.year,),
                )
            )
            continue
        source = sources.get(dividend)
        if source is None:
            # The keys share the dividend's symbol and Activity Date, so this
            # is its dividend's blocker for the withholding's own reference.
            blockers.append(_identity_blocker(tax.reference, event, unconfigured_symbols))
        elif event.kind is RobinhoodEventKind.NRA_WITHHOLDING and source.jurisdiction != "US":
            invalid_dividends.add(dividend)
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.JURISDICTION,
                    RobinhoodIncomeBlockerCause.US_WITHHOLDING_ON_NON_US_DIVIDEND,
                    account_indices=(tax.reference.account_index,),
                    event_references=(dividend, tax.reference),
                    tax_years=(event.activity_date.year,),
                )
            )
            continue
        paired[dividend] = tax

    dividend_payments: list[RobinhoodDividendPayment] = []
    withholding_payments: list[RobinhoodWithholdingPayment] = []
    for item in dividends:
        event = item.event
        if event.record_date is None or event.payment_date is None or event.amount_usd is None:
            raise _fail("dividend event lacks parser-proved source facts")
        security = securities[item.security_config_index]
        tax = paired.get(item.reference)
        valid_tax = tax is not None and item.reference not in invalid_dividends
        dividend_payments.append(
            RobinhoodDividendPayment(
                event_reference=item.reference,
                security_config_index=item.security_config_index,
                source_record_date=event.record_date,
                source_payment_date=event.payment_date,
                jurisdiction=item.jurisdiction,
                withholding_references=((tax.reference,) if valid_tax and tax is not None else ()),
                in_financial_year=item.in_financial_year,
                dividend=Dividend(
                    symbol=security.instrument.symbol,
                    isin=security.instrument.isin,
                    payment_date=event.activity_date,
                    amount_gross=event.amount_usd,
                    currency="USD",
                    description=event.description,
                    source_row_index=event.source_record_index,
                    kind=DistributionKind.CASH_DIVIDEND,
                    type_slice="Cash Dividend USD",
                    source_account_index=item.reference.account_index,
                    holding_date=_holding_date(event.record_date),
                ),
            )
        )
        if not valid_tax or tax is None:
            continue
        tax_event = tax.event
        if tax_event.amount_usd is None:
            raise _fail("paired withholding event lacks its source amount")
        withholding_payments.append(
            RobinhoodWithholdingPayment(
                event_reference=tax.reference,
                dividend_reference=item.reference,
                source_kind=tax_event.kind,
                jurisdiction=item.jurisdiction,
                in_financial_year=tax.in_financial_year,
                withholding_tax=WithholdingTax(
                    symbol=security.instrument.symbol,
                    isin=security.instrument.isin,
                    payment_date=event.activity_date,
                    amount=tax_event.amount_usd,
                    currency="USD",
                    description=tax_event.description,
                    source_account_index=item.reference.account_index,
                ),
            )
        )
    return (
        tuple(sorted(dividend_payments, key=lambda item: item.event_reference)),
        tuple(sorted(withholding_payments, key=lambda item: item.event_reference)),
        blockers,
    )


def _source_years(
    dividends: tuple[RobinhoodDividendPayment, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    fa_calendar_year: int,
) -> set[tuple[int, int]]:
    return {
        (item.event_reference.account_index, item.dividend.payment_date.year)
        for item in dividends
        if item.in_financial_year or item.dividend.payment_date.year == fa_calendar_year
    } | {
        (item.event_reference.account_index, item.interest_credit.credit_date.year)
        for item in interests
        if item.in_financial_year
    }


def _unidentified_dividend_years(
    unidentified: tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
    financial_year_start: date,
    financial_year_end: date,
) -> set[tuple[int, int]]:
    """The account/years ``_source_years`` gives these dividends once they are
    identified. The account and Activity Date need no security, so an annual
    form they need is named on the first run, beside the identity blocker
    (#1504), and the statements the re-run reads are read on the first run too
    (#1507)."""
    return {
        (reference.account_index, event.activity_date.year)
        for reference, event in unidentified
        if financial_year_start <= event.activity_date <= financial_year_end
        or event.activity_date.year == financial_year_start.year
    }


def _account_index_by_number(paired: RobinhoodAccountsReconciliation) -> dict[str, int]:
    return {
        account.activity.primary_bundle.account_number: index
        for index, account in enumerate(paired.accounts)
    }


def _packet_binding(
    packet: Robinhood1042SPacket, account_by_number: dict[str, int]
) -> tuple[set[str], int | None]:
    """The account references ``packet``'s summary and copies name, and the
    account it binds to: the one reference's account, or None when they name
    more than one or an unknown account."""
    references = {
        packet.summary.account_reference,
        *(copy.recipient.account_reference for copy in packet.copies),
    }
    if len(references) != 1:
        return references, None
    return references, account_by_number.get(next(iter(references)))


def _supplied_form_years(
    paired: RobinhoodAccountsReconciliation,
    packets: tuple[Robinhood1042SPacket, ...],
    relevant_years: set[int],
) -> set[tuple[int, int]]:
    account_by_number = _account_index_by_number(paired)
    result: set[tuple[int, int]] = set()
    for packet in packets:
        if packet.tax_year not in relevant_years:
            continue
        _references, account_index = _packet_binding(packet, account_by_number)
        if account_index is not None:
            result.add((account_index, packet.tax_year))
    return result


def _annual_form_years(
    source_years: set[tuple[int, int]],
    supplied_form_years: set[tuple[int, int]],
    financial_year_end: date,
) -> set[tuple[int, int]]:
    return {
        item for item in source_years if date(item[1], 12, 31) <= financial_year_end
    } | supplied_form_years


def _stock_lending_amount(item: _StockLendingSource) -> Decimal:
    if item.event.amount_usd is None:
        raise _fail("Stock Lending event lacks its positive source amount")
    return item.event.amount_usd


def _coverage_blockers(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    config_index_by_number: dict[str, int],
    annual_form_years: set[tuple[int, int]],
    financial_year_start: date,
    financial_year_end: date,
    rows: tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
) -> list[RobinhoodIncomeBlocker]:
    blockers: list[RobinhoodIncomeBlocker] = []
    for account_index, account in enumerate(paired.accounts):
        evidence = account.evidence
        bundle = account.activity.primary_bundle
        in_fy_refs = tuple(
            reference
            for reference, event in rows
            if reference.account_index == account_index
            and financial_year_start <= event.activity_date <= financial_year_end
            and event.kind in ROBINHOOD_INCOME_KINDS
        )
        number = bundle.account_number
        configured = config.accounts[config_index_by_number[number]]
        required_fy_start = max(financial_year_start, configured.opening_date)
        if not (
            evidence.start_date <= required_fy_start
            and evidence.end_date >= financial_year_end
            and bundle.coverage_start <= required_fy_start
            and bundle.coverage_end >= financial_year_end
        ):
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE,
                    RobinhoodIncomeBlockerCause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR,
                    account_indices=(account_index,),
                    event_references=in_fy_refs,
                )
            )
        for _required_account, year in sorted(
            item for item in annual_form_years if item[0] == account_index
        ):
            required_start = max(date(year, 1, 1), configured.opening_date)
            required_end = date(year, 12, 31)
            if not (
                evidence.start_date <= required_start
                and evidence.end_date >= required_end
                and bundle.coverage_start <= required_start
                and bundle.coverage_end >= required_end
            ):
                blockers.append(
                    _blocker(
                        RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE,
                        RobinhoodIncomeBlockerCause.EVIDENCE_SHORT_OF_FORM_YEAR,
                        account_indices=(account_index,),
                        event_references=in_fy_refs,
                        tax_years=(year,),
                    )
                )
    return blockers


def _statement_blockers(
    paired: RobinhoodAccountsReconciliation,
    statement_years: set[tuple[int, int]],
    financial_year_start: date,
    financial_year_end: date,
    dividends: tuple[RobinhoodDividendPayment, ...],
    unidentified_dividends: tuple[tuple[RobinhoodIncomeEventReference, RobinhoodEvent], ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    stock_lending: tuple[_StockLendingSource, ...],
) -> list[RobinhoodIncomeBlocker]:
    """Check every account's statement Income summaries in the FY's calendar
    years, and each of ``statement_years``, against the recognized income.

    An account-year in ``statement_years`` (recognized income, an unidentified
    dividend, Stock Lending or a bound Form 1042-S) reads every statement of
    its calendar year. Any other account-year reads only statements that start
    on or before the FY end (ADR 0006 §8).

    The Dividends line's totals count unidentified dividends too: they need
    only the account, Activity Date and amount (#1507).
    """
    fy_years = {
        (account_index, year)
        for account_index in range(len(paired.accounts))
        for year in (financial_year_start.year, financial_year_end.year)
    }
    dividend_amounts = [
        (item.event_reference, item.dividend.payment_date, item.dividend.amount_gross)
        for item in dividends
    ]
    for reference, event in unidentified_dividends:
        if event.amount_usd is None:
            raise _fail("dividend event lacks its source identity or amount")
        dividend_amounts.append((reference, event.activity_date, event.amount_usd))
    blockers: list[RobinhoodIncomeBlocker] = []
    for account_index, year in sorted(statement_years | fy_years):
        whole_year = (account_index, year) in statement_years
        account = paired.accounts[account_index]
        statements = account.activity.primary_bundle.statements
        if len(account.cash.periods) != len(statements):
            raise _fail("cash periods do not align with bounded statements")
        year_dividends = tuple(
            (reference, day, amount)
            for reference, day, amount in dividend_amounts
            if reference.account_index == account_index and day.year == year
        )
        year_interests = tuple(
            item
            for item in interests
            if item.event_reference.account_index == account_index
            and item.interest_credit.credit_date.year == year
        )
        year_stock_lending = tuple(
            item
            for item in stock_lending
            if item.reference.account_index == account_index
            and item.event.activity_date.year == year
        )
        for statement in statements:
            if year not in {statement.period_start.year, statement.period_end.year}:
                continue
            if not whole_year and statement.period_start > financial_year_end:
                continue
            if statement.period_start.year != year or statement.period_end.year != year:
                blockers.append(
                    _blocker(
                        RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
                        RobinhoodIncomeBlockerCause.STATEMENT_SPANS_YEARS,
                        account_indices=(account_index,),
                        tax_years=(year,),
                    )
                )
                continue
            income = {item.label: item for item in statement.income}
            if set(income) != _INCOME_LABELS:
                raise _fail("statement income summary contradicts its parser contract")
            period_dividends = tuple(
                (reference, day, amount)
                for reference, day, amount in year_dividends
                if statement.period_start <= day <= statement.period_end
            )
            period_interests = tuple(
                item
                for item in year_interests
                if statement.period_start
                <= item.interest_credit.credit_date
                <= statement.period_end
            )
            period_stock_lending = tuple(
                item
                for item in year_stock_lending
                if statement.period_start <= item.event.activity_date <= statement.period_end
            )
            expected_dividends = sum((amount for _ref, _day, amount in period_dividends), Decimal())
            expected_interests = sum(
                (item.interest_credit.amount_gross for item in period_interests),
                Decimal(),
            )
            expected_stock_lending = sum(
                (_stock_lending_amount(item) for item in period_stock_lending),
                Decimal(),
            )
            cumulative_dividends = sum(
                (amount for _ref, day, amount in year_dividends if day <= statement.period_end),
                Decimal(),
            )
            cumulative_interests = sum(
                (
                    item.interest_credit.amount_gross
                    for item in year_interests
                    if item.interest_credit.credit_date <= statement.period_end
                ),
                Decimal(),
            )
            cumulative_stock_lending = sum(
                (
                    _stock_lending_amount(item)
                    for item in year_stock_lending
                    if item.event.activity_date <= statement.period_end
                ),
                Decimal(),
            )
            mismatched = tuple(
                cause
                for cause, label, this_period, year_to_date in (
                    (
                        RobinhoodIncomeBlockerCause.STATEMENT_DIVIDENDS,
                        "Dividends",
                        expected_dividends,
                        cumulative_dividends,
                    ),
                    (
                        RobinhoodIncomeBlockerCause.STATEMENT_INTEREST,
                        "Interest Earned",
                        expected_interests,
                        cumulative_interests,
                    ),
                    (
                        RobinhoodIncomeBlockerCause.STATEMENT_STOCK_LENDING,
                        "Stock Lending",
                        expected_stock_lending,
                        cumulative_stock_lending,
                    ),
                )
                if income[label].this_period_usd != this_period
                or income[label].year_to_date_usd != year_to_date
            )
            if mismatched:
                blockers.append(
                    _blocker(
                        RobinhoodIncomeBlockerKind.STATEMENT_RECONCILIATION,
                        *mismatched,
                        account_indices=(account_index,),
                        event_references=(
                            *(reference for reference, _day, _amount in period_dividends),
                            *(item.event_reference for item in period_interests),
                            *(item.reference for item in period_stock_lending),
                        ),
                        tax_years=(year,),
                    )
                )
            distributions = income["Capital Gains Distributions"]
            if distributions.this_period_usd != 0 or distributions.year_to_date_usd != 0:
                blockers.append(
                    _blocker(
                        RobinhoodIncomeBlockerKind.UNSUPPORTED_INCOME,
                        RobinhoodIncomeBlockerCause.CAPITAL_GAINS_DISTRIBUTIONS,
                        account_indices=(account_index,),
                        tax_years=(year,),
                    )
                )
    return blockers


def _bucket_key(item: Robinhood1042SCopy | Robinhood1042SSummaryRow) -> tuple[object, ...]:
    """The 1042-S bucket a copy or summary row reports: income code and both
    chapters' exemption codes and tax rates."""
    return (
        item.income_code,
        item.chapter3_exemption_code,
        item.chapter3_tax_rate_percent,
        item.chapter4_exemption_code,
        item.chapter4_tax_rate_percent,
    )


def _summary_matches_copy(
    row: Robinhood1042SSummaryRow,
    copy: Robinhood1042SCopy,
) -> bool:
    def agrees(summary: Form1042SAmount, form: Form1042SAmount) -> bool:
        if summary.kind is Form1042SAmountKind.BLANK:
            return form.value in {None, Decimal()}
        return summary.value == form.value

    return (
        agrees(row.gross_income, copy.gross_income)
        and agrees(row.federal_tax_withheld, copy.federal_tax_withheld)
        and agrees(row.tax_withheld_other_agents, copy.tax_withheld_other_agents)
        and agrees(row.tax_paid_by_agent, copy.tax_paid_by_agent)
        and agrees(row.total_withholding_credit, copy.total_withholding_credit)
        and agrees(row.amount_repaid_to_recipient, copy.overwithheld_tax_repaid)
    )


def _deduplicated_copies(packet: Robinhood1042SPacket) -> tuple[Robinhood1042SCopy, ...]:
    grouped: dict[str | None, list[Robinhood1042SCopy]] = defaultdict(list)
    for copy in packet.copies:
        grouped[copy.unique_form_identifier].append(copy)
    result: list[Robinhood1042SCopy] = []
    for copies in grouped.values():
        by_type = {copy.copy_type: copy for copy in copies}
        if (
            len(copies) != _COPY_COUNT
            or set(by_type) != {Form1042SCopyType.B, Form1042SCopyType.C}
            or by_type[Form1042SCopyType.B].economic_fingerprint()
            != by_type[Form1042SCopyType.C].economic_fingerprint()
        ):
            raise _fail("Form 1042-S B/C copies are not one equal economic record")
        result.append(by_type[Form1042SCopyType.B])
    return tuple(sorted(result, key=lambda item: item.page_ordinal))


def _packet_buckets(
    packet_index: int,
    account_index: int,
    packet: Robinhood1042SPacket,
    dividends: tuple[RobinhoodDividendPayment, ...],
    withholdings: tuple[RobinhoodWithholdingPayment, ...],
) -> tuple[tuple[_FormBucket, ...], RobinhoodIncomeBlocker | None]:
    copies = _deduplicated_copies(packet)
    copy_by_key = {_bucket_key(copy): copy for copy in copies}
    summary_by_key = {_bucket_key(row): row for row in packet.summary.rows}
    if (
        not copies
        or len(copy_by_key) != len(copies)
        or len(summary_by_key) != len(packet.summary.rows)
        or set(copy_by_key) != set(summary_by_key)
        or any(
            not _summary_matches_copy(summary_by_key[key], copy)
            for key, copy in copy_by_key.items()
        )
    ):
        return (), _blocker(
            RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
            RobinhoodIncomeBlockerCause.ANNUAL_FORM_SUMMARY,
            account_indices=(account_index,),
            tax_years=(packet.tax_year,),
        )

    unsupported = _blocker(
        RobinhoodIncomeBlockerKind.UNSUPPORTED_FORM_SEMANTICS,
        RobinhoodIncomeBlockerCause.UNSUPPORTED_FORM_CONTENT,
        account_indices=(account_index,),
        tax_years=(packet.tax_year,),
    )
    withholding_by_dividend = {item.dividend_reference: item for item in withholdings}
    us_dividends = tuple(
        item
        for item in dividends
        if item.event_reference.account_index == account_index
        and item.dividend.payment_date.year == packet.tax_year
        and item.jurisdiction == "US"
    )
    buckets: list[_FormBucket] = []
    for copy in copies:
        gross = copy.gross_income.value
        federal = copy.federal_tax_withheld.value
        net_income = copy.net_income.value
        if (
            copy.tax_year != packet.tax_year
            or copy.income_code not in _SUPPORTED_CODES
            or form_1042s_violations(copy)
            or gross is None
            or gross < 0
            or (gross == 0 and (copy.income_code != _DIVIDEND_CODE or federal != 0))
            or federal is None
            or net_income not in {None, gross}
            or (net_income is not None and net_income != net_income.to_integral_value())
            or copy.chapter3_tax_rate_percent < 0
            or not copy.chapter3_tax_rate_percent.is_finite()
            or (
                copy.income_code == _BROKERAGE_INTEREST_CODE
                and (copy.chapter3_tax_rate_percent != 0 or federal != 0)
            )
            or (
                copy.income_code == _DIVIDEND_CODE
                and copy.chapter3_tax_rate_percent == 0
                and federal != 0
            )
            or (
                copy.income_code == _SUBSTITUTE_DIVIDEND_CODE
                and (copy.chapter3_tax_rate_percent != 0 or federal != 0)
            )
        ):
            return (), unsupported
        bucket = _FormBucket(packet_index, account_index, copy, gross, federal)
        # Box 2 prints whole dollars, so a code-06 $0 (with $0 tax) can cover
        # dividends under $0.50; it stands only when its rate proves one (#1247).
        if gross == 0 and not any(
            _dividend_matches_bucket(
                bucket, item, withholding_by_dividend.get(item.event_reference)
            )
            for item in us_dividends
        ):
            return (), unsupported
        buckets.append(bucket)
    return tuple(buckets), None


def _rounded_withholding(gross: Decimal, rate_percent: Decimal) -> Decimal:
    return (gross * rate_percent / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _whole_dollars(values: Iterable[Decimal], label: str) -> int:
    try:
        return whole_dollars(values)
    except Form1042SAmountError as exc:
        raise _fail(f"{label} is not a non-negative whole-cent USD amount") from exc


def _zero_round_interest_references(
    buckets: tuple[_FormBucket, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
) -> tuple[RobinhoodIncomeEventReference, ...]:
    if (
        not interests
        or any(bucket.copy.income_code == _BROKERAGE_INTEREST_CODE for bucket in buckets)
        or _whole_dollars(
            (item.interest_credit.amount_gross for item in interests),
            "annual interest",
        )
        != 0
    ):
        return ()
    return tuple(sorted(item.event_reference for item in interests))


def _dividend_matches_bucket(
    bucket: _FormBucket,
    dividend: RobinhoodDividendPayment,
    withholding: RobinhoodWithholdingPayment | None,
) -> bool:
    _whole_dollars((dividend.dividend.amount_gross,), "annual gross income")
    rate = bucket.copy.chapter3_tax_rate_percent
    if rate == 0:
        return withholding is None
    expected = _rounded_withholding(dividend.dividend.amount_gross, rate)
    return (expected == 0 and withholding is None) or (
        withholding is not None and expected == withholding.withholding_tax.amount.copy_abs()
    )


def _reconcile_bucket_events(
    buckets: tuple[_FormBucket, ...],
    dividends: tuple[RobinhoodDividendPayment, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    stock_lending: tuple[_StockLendingSource, ...],
    withholdings: tuple[RobinhoodWithholdingPayment, ...],
) -> tuple[
    tuple[RobinhoodAnnualIncomeBucket, ...],
    tuple[RobinhoodIncomeEventReference, ...],
    bool,
]:
    """Match one bound Form 1042-S's buckets to the account-year's source income.

    Every supplied US dividend, interest credit and Stock Lending row for the
    account and tax year must be matched or exempt. Form 1042-S is reconciled against US
    dividends only: non-US dividends are proven by the statement Income
    summaries and the ``DTAX`` pairing instead. A form whose dividend totals do
    not reconcile to its matched US dividends stays unreconciled.
    """
    account_index = buckets[0].account_index
    tax_year = buckets[0].copy.tax_year
    year_dividends = tuple(
        item
        for item in dividends
        if item.event_reference.account_index == account_index
        and item.dividend.payment_date.year == tax_year
        and item.jurisdiction == "US"
    )
    year_interests = tuple(
        item
        for item in interests
        if item.event_reference.account_index == account_index
        and item.interest_credit.credit_date.year == tax_year
    )
    year_stock_lending = tuple(
        item
        for item in stock_lending
        if item.reference.account_index == account_index
        and item.event.activity_date.year == tax_year
    )
    first = next((item for item in year_stock_lending if item.security_config_index is None), None)
    if first is not None:
        raise invariant_error(
            what=(
                "Robinhood activity report, stock-lending row without a security identity"
                " (column Instrument) reached annual-form reconciliation"
            ),
            source_row_index=first.event.source_record_index,
        )
    withholding_by_dividend = {item.dividend_reference: item for item in withholdings}
    matches: dict[RobinhoodIncomeEventReference, list[int]] = defaultdict(list)
    zero_round_interest_references = _zero_round_interest_references(
        buckets,
        year_interests,
    )
    for index, bucket in enumerate(buckets):
        copy = bucket.copy
        if copy.income_code == _BROKERAGE_INTEREST_CODE:
            for interest in year_interests:
                matches[interest.event_reference].append(index)
            continue
        for dividend in year_dividends:
            withholding = withholding_by_dividend.get(dividend.event_reference)
            if _dividend_matches_bucket(bucket, dividend, withholding):
                matches[dividend.event_reference].append(index)

    all_payments = {
        *(item.event_reference for item in year_dividends),
        *(item.event_reference for item in year_interests),
        *(item.reference for item in year_stock_lending),
    }
    exempt = {
        *zero_round_interest_references,
        *(item.reference for item in year_stock_lending),
    }
    if (
        set(matches) & exempt
        or set(matches) | exempt != all_payments
        or any(len(indices) != 1 for indices in matches.values())
    ):
        return (), (), False

    result: list[RobinhoodAnnualIncomeBucket] = []
    for index, bucket in enumerate(buckets):
        refs = tuple(sorted(reference for reference, match in matches.items() if match == [index]))
        dividends_in_bucket = tuple(item for item in year_dividends if item.event_reference in refs)
        interests_in_bucket = tuple(item for item in year_interests if item.event_reference in refs)
        stock_lending_in_bucket = tuple(
            item for item in year_stock_lending if item.reference in refs
        )
        withholding_rows = tuple(
            withholding_by_dividend[item.event_reference]
            for item in dividends_in_bucket
            if item.event_reference in withholding_by_dividend
        )
        gross_amounts = (
            *(item.dividend.amount_gross for item in dividends_in_bucket),
            *(item.interest_credit.amount_gross for item in interests_in_bucket),
            *(_stock_lending_amount(item) for item in stock_lending_in_bucket),
        )
        tax_amounts = tuple(item.withholding_tax.amount.copy_abs() for item in withholding_rows)
        gross = sum(gross_amounts, Decimal())
        tax = sum(tax_amounts, Decimal())
        if (
            _whole_dollars(gross_amounts, "annual gross income") != bucket.gross_income_usd
            or _whole_dollars(tax_amounts, "annual withholding") != bucket.withholding_usd
        ):
            return (), (), False
        result.append(
            RobinhoodAnnualIncomeBucket(
                packet_index=bucket.packet_index,
                account_index=bucket.account_index,
                tax_year=tax_year,
                income_code=bucket.copy.income_code,
                chapter3_exemption_code=bucket.copy.chapter3_exemption_code,
                chapter3_tax_rate_percent=bucket.copy.chapter3_tax_rate_percent,
                chapter4_exemption_code=bucket.copy.chapter4_exemption_code,
                chapter4_tax_rate_percent=bucket.copy.chapter4_tax_rate_percent,
                gross_income_usd=bucket.gross_income_usd,
                source_gross_income_usd=gross,
                gross_income_kind=bucket.copy.gross_income.kind,
                withholding_usd=bucket.withholding_usd,
                source_withholding_usd=tax,
                withholding_kind=bucket.copy.total_withholding_credit.kind,
                withholding_agent_country_iso2=CountryCode("US"),
                payment_references=refs,
                withholding_references=tuple(
                    sorted(item.event_reference for item in withholding_rows)
                ),
            )
        )
    return tuple(result), zero_round_interest_references, True


def _recipient_identity(copy: Robinhood1042SCopy) -> tuple[object, ...]:
    recipient = copy.recipient
    return (
        recipient.name,
        recipient.country_code,
        recipient.address,
        recipient.city_state_postal,
        recipient.us_tin,
        recipient.chapter3_status_code,
        recipient.chapter4_status_code,
        recipient.foreign_tin,
        recipient.date_of_birth,
    )


def _blocker_affects_account_year(
    blocker: RobinhoodIncomeBlocker,
    account_index: int,
    tax_year: int,
) -> bool:
    return (not blocker.account_indices or account_index in blocker.account_indices) and (
        not blocker.tax_years or tax_year in blocker.tax_years
    )


def _zero_tax_interest_discrepancy_fallback(
    buckets: tuple[_FormBucket, ...],
    *,
    account_index: int,
    tax_year: int,
    dividends: tuple[RobinhoodDividendPayment, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    stock_lending: tuple[_StockLendingSource, ...],
    withholdings: tuple[RobinhoodWithholdingPayment, ...],
) -> tuple[tuple[RobinhoodAnnualIncomeBucket, ...], RobinhoodAnnualFormDiscrepancy] | None:
    interest_buckets = tuple(
        bucket for bucket in buckets if bucket.copy.income_code == _BROKERAGE_INTEREST_CODE
    )
    residual_buckets = tuple(
        bucket for bucket in buckets if bucket.copy.income_code != _BROKERAGE_INTEREST_CODE
    )
    if len(interest_buckets) != 1 or not residual_buckets:
        return None

    bucket = interest_buckets[0]
    source_interests = tuple(
        item
        for item in interests
        if item.event_reference.account_index == account_index
        and item.interest_credit.credit_date.year == tax_year
    )
    if (
        not source_interests
        or bucket.copy.chapter3_tax_rate_percent != 0
        or bucket.withholding_usd != 0
    ):
        return None
    source_amounts = tuple(item.interest_credit.amount_gross for item in source_interests)
    source_gross = sum(source_amounts, Decimal())
    difference = bucket.gross_income_usd - source_gross
    if (
        _whole_dollars(source_amounts, "annual interest") == bucket.gross_income_usd
        or difference.copy_abs() >= _WHOLE_DOLLAR
    ):
        return None

    annual, zero_round, complete = _reconcile_bucket_events(
        residual_buckets,
        dividends,
        (),
        stock_lending,
        withholdings,
    )
    if not complete or zero_round:
        return None
    source_references = tuple(sorted(item.event_reference for item in source_interests))
    if any(
        reference in source_references
        for annual_bucket in annual
        for reference in annual_bucket.payment_references
    ):
        raise _fail("interest discrepancy reference also entered an annual bucket")
    return (
        annual,
        RobinhoodAnnualFormDiscrepancy(
            packet_index=bucket.packet_index,
            account_index=account_index,
            tax_year=tax_year,
            income_code=_BROKERAGE_INTEREST_CODE,
            direction=(
                RobinhoodAnnualFormDiscrepancyDirection.FORM_ABOVE_SOURCE
                if difference > 0
                else RobinhoodAnnualFormDiscrepancyDirection.FORM_BELOW_SOURCE
            ),
            event_references=source_references,
        ),
    )


def _form_reconciliation(  # noqa: PLR0912, PLR0915
    paired: RobinhoodAccountsReconciliation,
    packets: tuple[Robinhood1042SPacket, ...],
    annual_form_years: set[tuple[int, int]],
    dividends: tuple[RobinhoodDividendPayment, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    stock_lending: tuple[_StockLendingSource, ...],
    withholdings: tuple[RobinhoodWithholdingPayment, ...],
    financial_year_start: date,
    financial_year_end: date,
    pre_form_blockers: tuple[RobinhoodIncomeBlocker, ...],
) -> tuple[
    tuple[RobinhoodAnnualIncomeBucket, ...],
    list[RobinhoodIncomeBlocker],
    tuple[int, ...],
    tuple[RobinhoodIncomeEventReference, ...],
    tuple[RobinhoodAnnualFormDiscrepancy, ...],
]:
    blockers: list[RobinhoodIncomeBlocker] = []
    relevant_years = {financial_year_start.year, financial_year_end.year}
    account_by_number = _account_index_by_number(paired)
    bound: dict[tuple[int, int], list[tuple[int, Robinhood1042SPacket]]] = defaultdict(list)
    excluded: list[int] = []
    # Account/years whose supplied form an identity check set aside (#1487).
    set_aside: set[tuple[int, int]] = set()
    recipient_identities: set[tuple[object, ...]] = set()
    agents: set[object] = set()

    for packet_index, packet in enumerate(packets):
        if packet.tax_year not in relevant_years:
            excluded.append(packet_index)
            continue
        references, account_index = _packet_binding(packet, account_by_number)
        if any(copy.income_code in SUBSTITUTE_PAYMENT_CODES for copy in packet.copies):
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION,
                    RobinhoodIncomeBlockerCause.SUBSTITUTE_PAYMENT,
                    account_indices=(() if account_index is None else (account_index,)),
                    event_references=(
                        item.reference
                        for item in stock_lending
                        if account_index is not None
                        and item.reference.account_index == account_index
                        and item.event.activity_date.year == packet.tax_year
                    ),
                    tax_years=(packet.tax_year,),
                )
            )
        if account_index is None:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
                    RobinhoodIncomeBlockerCause.ANNUAL_FORM_ACCOUNT_UNBOUND,
                    tax_years=(packet.tax_year,),
                )
            )
            set_aside.update(
                (account_by_number[reference], packet.tax_year)
                for reference in references
                if reference in account_by_number
            )
            continue
        if (account_index, packet.tax_year) not in annual_form_years:
            raise _fail("a bound financial-year Form 1042-S packet is outside annual-form scope")
        copies = _deduplicated_copies(packet)
        bundle = paired.accounts[account_index].activity.primary_bundle
        if not copies or any(
            copy.recipient.country_code != "IN"
            or copy.agent.country_code != "US"
            or not bundle.registration_lines
            for copy in copies
        ):
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
                    RobinhoodIncomeBlockerCause.ANNUAL_FORM_PARTIES,
                    account_indices=(account_index,),
                    tax_years=(packet.tax_year,),
                )
            )
            set_aside.add((account_index, packet.tax_year))
            continue
        recipient_identities.update(_recipient_identity(copy) for copy in copies)
        agents.update(copy.agent for copy in copies)
        bound[(account_index, packet.tax_year)].append((packet_index, packet))

    if len(recipient_identities) > 1 or len(agents) > 1:
        # Scoped to the bound forms whose identities were compared (#1497).
        blockers.append(
            _blocker(
                RobinhoodIncomeBlockerKind.ANNUAL_FORM_IDENTITY,
                RobinhoodIncomeBlockerCause.ANNUAL_FORMS_DISAGREE,
                account_indices=(account_index for account_index, _year in bound),
                tax_years=(tax_year for _account, tax_year in bound),
            )
        )

    reconciled: list[RobinhoodAnnualIncomeBucket] = []
    zero_round_unreported: list[RobinhoodIncomeEventReference] = []
    discrepancies: list[RobinhoodAnnualFormDiscrepancy] = []
    for account_index, tax_year in sorted(annual_form_years):
        matches = bound.get((account_index, tax_year), [])
        if len(matches) != 1:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.ANNUAL_FORM_COVERAGE,
                    (
                        RobinhoodIncomeBlockerCause.ANNUAL_FORM_DUPLICATED
                        if matches
                        else RobinhoodIncomeBlockerCause.ANNUAL_FORM_SET_ASIDE
                        if (account_index, tax_year) in set_aside
                        else RobinhoodIncomeBlockerCause.ANNUAL_FORM_MISSING
                    ),
                    account_indices=(account_index,),
                    tax_years=(tax_year,),
                )
            )
            continue
        packet_index, packet = matches[0]
        buckets, semantic_blocker = _packet_buckets(
            packet_index,
            account_index,
            packet,
            dividends,
            withholdings,
        )
        if semantic_blocker is not None:
            blockers.append(semantic_blocker)
            continue
        if any(copy.income_code in SUBSTITUTE_PAYMENT_CODES for copy in packet.copies):
            continue
        if any(
            _blocker_affects_account_year(blocker, account_index, tax_year)
            for blocker in pre_form_blockers
        ):
            continue
        annual, zero_round, complete = _reconcile_bucket_events(
            buckets,
            dividends,
            interests,
            stock_lending,
            withholdings,
        )
        if not complete:
            fallback = _zero_tax_interest_discrepancy_fallback(
                buckets,
                account_index=account_index,
                tax_year=tax_year,
                dividends=dividends,
                interests=interests,
                stock_lending=stock_lending,
                withholdings=withholdings,
            )
            if fallback is not None:
                fallback_annual, discrepancy = fallback
                reconciled.extend(fallback_annual)
                discrepancies.append(discrepancy)
                continue
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.ANNUAL_FORM_RECONCILIATION,
                    RobinhoodIncomeBlockerCause.ANNUAL_FORM_SOURCE,
                    account_indices=(account_index,),
                    event_references=(
                        *(
                            item.event_reference
                            for item in dividends
                            if item.event_reference.account_index == account_index
                            and item.dividend.payment_date.year == tax_year
                        ),
                        *(
                            item.event_reference
                            for item in interests
                            if item.event_reference.account_index == account_index
                            and item.interest_credit.credit_date.year == tax_year
                        ),
                        *(
                            item.reference
                            for item in stock_lending
                            if item.reference.account_index == account_index
                            and item.event.activity_date.year == tax_year
                        ),
                    ),
                    tax_years=(tax_year,),
                )
            )
            continue
        reconciled.extend(annual)
        zero_round_unreported.extend(zero_round)
    annual_references = {
        reference for bucket in reconciled for reference in bucket.payment_references
    }
    zero_round_references = set(zero_round_unreported)
    discrepancy_reference_list = [
        reference for item in discrepancies for reference in item.event_references
    ]
    discrepancy_references = set(discrepancy_reference_list)
    if (
        annual_references & zero_round_references
        or annual_references & discrepancy_references
        or zero_round_references & discrepancy_references
        or len(discrepancy_references) != len(discrepancy_reference_list)
    ):
        raise _fail("annual-form interest dispositions overlap")
    return (
        tuple(reconciled),
        blockers,
        tuple(sorted(excluded)),
        tuple(sorted(zero_round_unreported)),
        tuple(discrepancies),
    )


def _pending_annual_form_evidence(
    source_years: set[tuple[int, int]],
    annual_form_years: set[tuple[int, int]],
    dividends: tuple[RobinhoodDividendPayment, ...],
    interests: tuple[RobinhoodInterestPayment, ...],
    withholdings: tuple[RobinhoodWithholdingPayment, ...],
) -> tuple[RobinhoodAnnualFormPendingEvidence, ...]:
    result = []
    for account_index, tax_year in sorted(source_years - annual_form_years):
        year_dividends = tuple(
            item
            for item in dividends
            if item.in_financial_year
            and item.event_reference.account_index == account_index
            and item.dividend.payment_date.year == tax_year
        )
        income_references = tuple(
            sorted(
                (
                    *(item.event_reference for item in year_dividends),
                    *(
                        item.event_reference
                        for item in interests
                        if item.in_financial_year
                        and item.event_reference.account_index == account_index
                        and item.interest_credit.credit_date.year == tax_year
                    ),
                )
            )
        )
        withholding_references = tuple(
            sorted(
                item.event_reference
                for item in withholdings
                if item.in_financial_year
                and item.event_reference.account_index == account_index
                and item.withholding_tax.payment_date.year == tax_year
            )
        )
        if not income_references:
            continue
        result.append(
            RobinhoodAnnualFormPendingEvidence(
                account_index=account_index,
                tax_year=tax_year,
                income_references=income_references,
                withholding_references=withholding_references,
                unpaired_us_dividend_references=tuple(
                    sorted(
                        item.event_reference
                        for item in year_dividends
                        if item.jurisdiction == "US" and not item.withholding_references
                    )
                ),
            )
        )
    return tuple(result)


def _validate_pending_annual_form_evidence(
    pending: tuple[RobinhoodAnnualFormPendingEvidence, ...],
    annual: tuple[RobinhoodAnnualIncomeBucket, ...],
    zero_round: tuple[RobinhoodIncomeEventReference, ...],
    discrepancies: tuple[RobinhoodAnnualFormDiscrepancy, ...],
) -> None:
    pending_income_list = [reference for item in pending for reference in item.income_references]
    pending_income = set(pending_income_list)
    pending_withholding_list = [
        reference for item in pending for reference in item.withholding_references
    ]
    pending_withholding = set(pending_withholding_list)
    annual_income = {reference for item in annual for reference in item.payment_references}
    annual_withholding = {reference for item in annual for reference in item.withholding_references}
    historical_interest = {
        *zero_round,
        *(reference for item in discrepancies for reference in item.event_references),
    }
    if (
        len(pending_income) != len(pending_income_list)
        or len(pending_withholding) != len(pending_withholding_list)
        or pending_income & annual_income
        or pending_income & historical_interest
        or pending_withholding & annual_withholding
    ):
        raise _fail("pending annual-form evidence overlaps another disposition")


def _stock_lending_payments(
    paired: RobinhoodAccountsReconciliation,
    sources: tuple[_StockLendingSource, ...],
    config: RobinhoodConfig,
    financial_year: int,
) -> tuple[tuple[RobinhoodStockLendingPayment, ...], tuple[RobinhoodIncomeBlocker, ...]]:
    payments: list[RobinhoodStockLendingPayment] = []
    blockers: list[RobinhoodIncomeBlocker] = []
    for source in sources:
        event = source.event
        if event.instrument is None:
            raise _fail("classified Stock Lending event lacks its source symbol")
        account_number = paired.accounts[
            source.reference.account_index
        ].activity.primary_bundle.account_number
        reviewed_source = (
            config.stock_lending_fee_fsi_source_for(account_number, financial_year)
            if source.in_financial_year
            else None
        )
        if source.in_financial_year and reviewed_source is None:
            blockers.append(
                _blocker(
                    RobinhoodIncomeBlockerKind.JURISDICTION,
                    RobinhoodIncomeBlockerCause.LENDING_FEE_SOURCE,
                    account_indices=(source.reference.account_index,),
                    event_references=(source.reference,),
                    tax_years=(event.activity_date.year,),
                )
            )
        payment_source_country = (
            None if reviewed_source is None else CountryCode(reviewed_source.country_iso2)
        )
        payments.append(
            RobinhoodStockLendingPayment(
                event_reference=source.reference,
                security_config_index=source.security_config_index,
                issuer_country_iso2=source.issuer_country_iso2,
                source_country_iso2=payment_source_country,
                withholding_agent_country_iso2=None,
                in_financial_year=source.in_financial_year,
                subtype=RobinhoodStockLendingSubtype.SECURITIES_LENDING_FEE,
                annual_bucket_index=None,
                loan_fee_income=OtherSourceIncome(
                    nature="Securities lending fee - Robinhood Stock Lending",
                    payment_date=event.activity_date,
                    amount_gross=_stock_lending_amount(source),
                    currency="USD",
                    description=event.description,
                    source_row_index=event.source_record_index,
                    source_country_iso2=payment_source_country,
                ),
                substitute_dividend_income=None,
            )
        )
    return (
        tuple(sorted(payments, key=lambda item: item.event_reference)),
        _sorted_blockers(blockers),
    )


def compute_robinhood_income(
    paired: RobinhoodAccountsReconciliation,
    packets: Iterable[Robinhood1042SPacket],
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    financial_year_start: date,
    financial_year_end: date,
) -> RobinhoodIncomeResult:
    """Return complete shared income inputs or only privacy-safe review evidence.

    Every account's statement Income summaries in the FY's calendar years are
    checked against the recognized income, whether or not the account has any
    (ADR 0006 §8).
    """

    with localcontext(_ARITHMETIC_CONTEXT):
        config_index_by_number = _validate_inputs(
            paired, config, financial_year_start, financial_year_end
        )
        packet_tuple = tuple(packets)
        rows = _source_events(paired)
        identities, unconfigured_symbols = _identity_by_symbol(paired, securities.securities)
        (
            dividend_sources,
            interests,
            tax_sources,
            stock_lending_sources,
            fees,
            blockers,
            unidentified_dividends,
        ) = _income_sources(
            rows,
            securities.securities,
            identities,
            unconfigured_symbols,
            financial_year_start,
            financial_year_end,
        )
        interests, partition_blockers = _partition_interest_payments(
            paired,
            interests,
        )
        blockers.extend(partition_blockers)
        dividends, withholdings, pairing_blockers = _pair_payments(
            dividend_sources,
            unidentified_dividends,
            tax_sources,
            securities.securities,
            unconfigured_symbols,
        )
        blockers.extend(pairing_blockers)
        source_years = _source_years(
            dividends,
            interests,
            financial_year_start.year,
        )
        stock_lending_years = {
            (item.reference.account_index, item.event.activity_date.year)
            for item in stock_lending_sources
        }
        supplied_form_years = _supplied_form_years(
            paired,
            packet_tuple,
            {financial_year_start.year, financial_year_end.year},
        )
        # Unidentified dividends count where they will once identified: toward
        # the forms, their years' evidence coverage (#1504) and the statement
        # checks (#1507).
        unidentified_years = _unidentified_dividend_years(
            unidentified_dividends, financial_year_start, financial_year_end
        )
        statement_years = (
            source_years | unidentified_years | stock_lending_years | supplied_form_years
        )
        required_annual_form_years = _annual_form_years(
            source_years | unidentified_years,
            supplied_form_years,
            financial_year_end,
        )
        blockers.extend(
            _coverage_blockers(
                paired,
                config,
                config_index_by_number,
                required_annual_form_years,
                financial_year_start,
                financial_year_end,
                rows,
            )
        )
        statement_blockers = _statement_blockers(
            paired,
            statement_years,
            financial_year_start,
            financial_year_end,
            dividends,
            unidentified_dividends,
            interests,
            stock_lending_sources,
        )
        blockers.extend(statement_blockers)
        (
            annual,
            form_blockers,
            excluded_packets,
            zero_round_unreported,
            annual_form_discrepancies,
        ) = _form_reconciliation(
            paired,
            packet_tuple,
            required_annual_form_years,
            dividends,
            interests,
            stock_lending_sources,
            withholdings,
            financial_year_start,
            financial_year_end,
            tuple(blockers),
        )
        blockers.extend(form_blockers)
        pending_annual_form_evidence = _pending_annual_form_evidence(
            source_years,
            required_annual_form_years,
            dividends,
            interests,
            withholdings,
        )
        _validate_pending_annual_form_evidence(
            pending_annual_form_evidence,
            annual,
            zero_round_unreported,
            annual_form_discrepancies,
        )
        stock_lending, stock_lending_blockers = _stock_lending_payments(
            paired,
            stock_lending_sources,
            config,
            financial_year_start.year,
        )
        blockers.extend(stock_lending_blockers)
        # Last, so earlier checks (annual-form reconciliation skips account/years
        # that already have a blocker) see the same blockers as before #698.
        payer_blockers, partition_only_payer_accounts = _interest_payer_blockers(
            paired, interests, config, financial_year_start.year
        )
        blockers.extend(payer_blockers)
        excluded_events = tuple(
            reference
            for reference, event in rows
            if not (financial_year_start <= event.activity_date <= financial_year_end)
        )
        return RobinhoodIncomeResult(
            dividend_payments=dividends,
            interest_payments=interests,
            stock_lending_payments=stock_lending,
            fee_payments=fees,
            withholding_payments=withholdings,
            annual_buckets=annual,
            blockers=_sorted_blockers(blockers),
            excluded_out_of_fy_event_references=excluded_events,
            excluded_packet_indices=excluded_packets,
            zero_round_unreported_interest_references=zero_round_unreported,
            unreported_lending_fee_references=(
                ()
                if any(
                    blocker.kind is RobinhoodIncomeBlockerKind.STOCK_LENDING_CLASSIFICATION
                    for blocker in blockers
                )
                else tuple(sorted(item.reference for item in stock_lending_sources))
            ),
            annual_form_discrepancies=annual_form_discrepancies,
            pending_annual_form_evidence=pending_annual_form_evidence,
            partition_only_payer_accounts=partition_only_payer_accounts,
        )
