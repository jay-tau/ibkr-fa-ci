"""Build Robinhood Schedule FA A2/A3 review evidence from reconciled sources."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Final, NamedTuple

from ibkr_fa.compute.dec31_close import ClosingPriceError, resolve_dec31_close
from ibkr_fa.compute.exact_inr import (
    checked_addition,
    checked_product,
    exact_decimal,
    exact_fraction,
    exact_share,
    review_amount,
)
from ibkr_fa.compute.fa_a2 import (
    FaA2GrossEvidence,
    FaA2PeakBounds,
    build_fa_a2_row,
    build_gross_evidence,
    expand_fa_a2_nature_rows,
    fa_a2_manual_fill_gaps,
)
from ibkr_fa.compute.fa_a3 import (
    build_fa_a3_rows,
    fa_a3_manual_fill_gaps,
    held_on,
    split_a3_credit_by_holding,
)
from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.compute.robinhood_a2_peak import (
    RobinhoodA2Peak,
    compute_robinhood_a2_peaks,
    robinhood_a2_peak_window,
)
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeEventReference,
    RobinhoodIncomeResult,
)
from ibkr_fa.compute.robinhood_lots import (
    LOT_BLOCKER_CAUSES,
    SAME_DAY_EVIDENCE,
    RobinhoodLotBlocker,
    RobinhoodLotBlockerKind,
    RobinhoodLotHoldingInterval,
    RobinhoodLotSlice,
    RobinhoodLotSliceKind,
    RobinhoodSecurityLotResult,
    allocate_disposal_proceeds,
    combined_disposal,
    reconcile_robinhood_security_lots,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionContract,
    RobinhoodOptionFaDisposition,
    RobinhoodOptionReconstruction,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodSecurityConfig,
)
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.models.account_identity import AccountIdentity, InstitutionIdentity
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.models.net_asset_value import NetAssetValueSummary
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountReconciliation,
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import robinhood_half_ulp
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodInventoryBlocker,
    RobinhoodInventoryBlockerKind,
    RobinhoodPositionReference,
    RobinhoodPositionSnapshotBasis,
    RobinhoodQuantityMovement,
    RobinhoodQuantityMovementKind,
    RobinhoodSecurityQuantityResult,
    resolve_robinhood_security_quantities,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityResult,
    resolve_robinhood_security_identities,
)
from ibkr_fa.parser.robinhood import ROBINHOOD_EQUITY_SALE_KINDS, RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementPosition, RobinhoodStockLoanKind
from ibkr_fa.pii import is_masked_account_id
from ibkr_fa.prices.series import MassiveOptionCloseSeries, PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Isin, PeakBasis, PeakCurrency, Ticker

_ARITHMETIC_CONTEXT = Context(prec=60)
_ACCOUNT_COUNT = 2
_LOAN_FEE_PAYMENT_SESSIONS = 5
_US_MARKETS = frozenset({"ARCA", "NASDAQ", "NYSE"})


class RobinhoodFaError(IbkrFaError):
    """Reconciled Robinhood evidence contradicts the Schedule FA contract."""


class RobinhoodFaBlockerKind(StrEnum):
    """One unresolved fact preventing a complete A2/A3 result."""

    A2_CLOSING = "a2-closing"
    A3_CLOSING = "a3-closing"
    A3_CREDIT_ATTRIBUTION = "a3-credit-attribution"
    A3_DISPOSAL = "a3-disposal"
    A3_IDENTITY = "a3-identity"
    A3_INVENTORY = "a3-inventory"
    A3_LOTS = "a3-lots"
    EVIDENCE_COVERAGE = "evidence-coverage"
    OPTION_CLASSIFICATION = "option-classification"


class RobinhoodFaManualKind(StrEnum):
    """One explicit manual Schedule FA task."""

    A2_GROSS_CREDITS = "a2-gross-credits"
    A2_ZIP = "a2-zip"
    A3_GROSS_CREDITS = "a3-gross-credits"
    A3_IDENTITY = "a3-identity"
    A3_PEAK = "a3-peak"
    OPTION_OTHER_TABLE_REVIEW = "option-other-table-review"
    OPTION_TABLE_D = "option-table-d"


class RobinhoodA3IdentityGap(StrEnum):
    """Why an ``a3-identity`` task's Table A3 identity cells need work (#1483).

    ``ADDRESS``, ``ZIP_CODE`` and ``NATURE`` are blank cells. ``ETF_NAME`` is a
    series ETF whose Name of entity is the broker's description, because no
    ``[[fa.entities]]`` entry or automatic registrant name gives it one (#793).
    A whitespace-only Name of entity is read from the row instead (#1369).
    """

    ADDRESS = "address"
    ZIP_CODE = "zip-code"
    NATURE = "nature"
    ETF_NAME = "etf-name"


class RobinhoodA3IdentityGapHolding(NamedTuple):
    """One identity gap of one holding's Table A3 row, by symbol and ISIN (#1483)."""

    gap: RobinhoodA3IdentityGap
    symbol: Ticker
    isin: Isin


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodFaBlocker:
    """One privacy-safe blocker."""

    kind: RobinhoodFaBlockerKind
    account_indices: tuple[int, ...] = ()
    security_config_indices: tuple[int, ...] = ()
    lot_ids: tuple[RobinhoodSecurityEventReference, ...] = ()
    source_lot_blockers: tuple[RobinhoodLotBlocker, ...] = ()
    """For ``A3_LOTS``, the lot engine's blockers, each with its own kind (#1550)."""
    income_blocked: bool = False
    """For ``A3_CREDIT_ATTRIBUTION``, whether it stands because Schedule OS's
    source income has unresolved blockers, rather than for one credit (#1550)."""


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodFaManualWork:
    """One manual filing task without private values. An ``a3-identity``
    task carries each identity gap of its security's rows, with the row's
    symbol and ISIN (#1483)."""

    kind: RobinhoodFaManualKind
    account_indices: tuple[int, ...] = ()
    security_config_indices: tuple[int, ...] = ()
    lot_ids: tuple[RobinhoodSecurityEventReference, ...] = ()
    identity_gaps: frozenset[RobinhoodA3IdentityGapHolding] = frozenset()


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodProgramBankEvidence:
    """An account whose in-year statement periods or interest show A2
    cash-sweep inclusion, with that interest."""

    account_index: int
    interest_references: tuple[RobinhoodIncomeEventReference, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodA2Review:
    """One account-and-credit-nature A2 row with its complete source audit."""

    account_index: int
    row: FaA2Row
    # The statement's account number matched the configured one it files.
    account_id_cross_checked: bool
    peak_usd: Decimal
    peak_ttbr: Decimal
    peak_floor_usd: Decimal
    peak_floor_inr: Decimal
    peak_floor_date: date
    known_gross_components_inr: Mapping[str, Decimal]
    unresolved_gross_natures: tuple[str, ...]
    program_bank: RobinhoodProgramBankEvidence | None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodA3Review:
    """One transfer-safe economic A3 row with source allocation provenance."""

    row: FaA3Row
    lot_id: RobinhoodSecurityEventReference
    account_indices: tuple[int, ...]
    close_references: tuple[RobinhoodSecurityEventReference, ...]
    gross_credit_usd: Decimal | None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodFaResult:
    """A2 nature rows for two accounts plus A3 output and incompleteness."""

    a2: tuple[RobinhoodA2Review, ...]
    a3: tuple[RobinhoodA3Review, ...]
    blockers: tuple[RobinhoodFaBlocker, ...]
    manual_work: tuple[RobinhoodFaManualWork, ...]
    program_bank: tuple[RobinhoodProgramBankEvidence, ...]
    cbdt_json_eligible: bool

    @property
    def a2_rows(self) -> tuple[FaA2Row, ...]:
        return tuple(item.row for item in self.a2)

    @property
    def a3_rows(self) -> tuple[FaA3Row, ...]:
        return tuple(item.row for item in self.a3)


@dataclass(frozen=True, slots=True, order=True)
class _FragmentKey:
    lot_id: RobinhoodSecurityEventReference
    close_references: tuple[RobinhoodSecurityEventReference, ...] = ()


@dataclass(frozen=True, slots=True)
class _Holding:
    """One part of a fragment: the account it ends in, the day it entered that
    account, and its quantity and exact quantity (#1263, #1318)."""

    account_index: int
    entry_date: date
    quantity: Decimal
    exact_quantity: Fraction | None = None


@dataclass(frozen=True, slots=True)
class _Fragment:
    key: _FragmentKey
    security_config_index: int
    quantity: Decimal
    cost_usd: Decimal
    open_date: date
    close_date: date | None
    account_indices: tuple[int, ...]
    holdings: tuple[_Holding, ...]
    """The parts that make up ``quantity``, so a credit can find the account
    that held each part on its holding date (#1318)."""
    net_proceeds_usd: Decimal | None = None
    exact_cost_usd: Fraction | None = None
    """The exact cost where ``cost_usd`` rounds it (#1243)."""
    exact_quantity: Fraction | None = None
    """The exact quantity where ``quantity`` rounds it (#1263)."""
    exact_net_proceeds_usd: Fraction | None = None
    """The exact net proceeds where ``net_proceeds_usd`` rounds them (#1383)."""

    @property
    def share_quantity(self) -> Decimal | Fraction:
        """The quantity a split by holding reads: the exact one where ``quantity`` rounds it."""
        return self.quantity if self.exact_quantity is None else self.exact_quantity


@dataclass(frozen=True, slots=True)
class _Dec31Position:
    """One security's Dec-31 statement holding, as A3 compares and values it."""

    quantity: Decimal
    total_usd: Decimal
    tolerance: Decimal
    exact_total_usd: Fraction | None = None
    """The exact total where a pending trade scaled it (#1246). ``total_usd`` is
    then its review Decimal value, which a share keeps only where it is exact."""


@dataclass(frozen=True, slots=True)
class _Credit:
    reference: RobinhoodIncomeEventReference
    security_config_index: int
    holding_date: date
    credit_date: date
    amount_usd: Decimal
    source_quantity: Decimal | None
    distribution_rate_usd: Decimal | None
    symbol: str
    monthly_loan_fee: bool = False


@dataclass(frozen=True, slots=True)
class _LoanRequirement:
    day: date
    quantity: Decimal
    close_inclusive: bool
    new_lending: bool


def _fail(message: str) -> RobinhoodFaError:
    return RobinhoodFaError(message)


def _share_error(security_index: int, share: str) -> InvariantError:
    """The #1243 digit-cap refusal for a ``closing`` or ``credit`` share,
    naming only the configured security's 1-based index."""
    return invariant_error(
        what=(
            f"Schedule FA A3, Robinhood security {security_index + 1}: the {share} share "
            "needs more digits than exact arithmetic allows"
        )
    )


def _event_map(
    paired: RobinhoodAccountsReconciliation,
) -> dict[RobinhoodSecurityEventReference, RobinhoodEvent]:
    result: dict[RobinhoodSecurityEventReference, RobinhoodEvent] = {}
    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            reference = RobinhoodSecurityEventReference(
                account_index,
                event.source_record_index,
            )
            if reference in result:
                raise _fail("reconciled activity contains duplicate source references")
            result[reference] = event
    return result


def _config_accounts(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
) -> dict[str, RobinhoodAccountConfig]:
    if len(paired.accounts) != _ACCOUNT_COUNT or len(config.accounts) != _ACCOUNT_COUNT:
        raise _fail("Robinhood Schedule FA requires the reconciled two-account bundle")
    result = {account.number: account for account in config.accounts}
    paired_numbers = {account.activity.primary_bundle.account_number for account in paired.accounts}
    if paired_numbers != result.keys():
        raise _fail("Schedule FA config does not match reconciled account identities")
    return result


def _coverage_blockers(
    paired: RobinhoodAccountsReconciliation,
    config_accounts: dict[str, RobinhoodAccountConfig],
    calendar_year: int,
) -> tuple[RobinhoodFaBlocker, ...]:
    start = date(calendar_year, 1, 1)
    end = date(calendar_year, 12, 31)
    blockers: list[RobinhoodFaBlocker] = []
    for account_index, account in enumerate(paired.accounts):
        number = account.activity.primary_bundle.account_number
        configured = config_accounts[number]
        required_start = max(start, configured.opening_date)
        bundle = account.activity.primary_bundle
        dec31_count = sum(statement.period_end == end for statement in bundle.statements)
        if not (
            account.evidence.start_date <= required_start
            and account.evidence.end_date >= end
            and bundle.coverage_start <= required_start
            and bundle.coverage_end >= end
            and dec31_count == 1
        ):
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.EVIDENCE_COVERAGE,
                    account_indices=(account_index,),
                )
            )
    return tuple(blockers)


def _program_bank_evidence(
    paired: RobinhoodAccountsReconciliation,
    income: RobinhoodIncomeResult,
    calendar_year: int,
) -> tuple[RobinhoodProgramBankEvidence, ...]:
    result: list[RobinhoodProgramBankEvidence] = []
    for account_index, account in enumerate(paired.accounts):
        statement_indices = tuple(
            index
            for index, (statement, cash_period) in enumerate(
                zip(
                    account.activity.primary_bundle.statements,
                    account.cash.periods,
                    strict=True,
                )
            )
            if statement.period_start.year <= calendar_year <= statement.period_end.year
            and cash_period.program_bank_present
        )
        references = tuple(
            item.event_reference
            for item in income.interest_payments
            if item.event_reference.account_index == account_index
            and item.interest_credit.credit_date.year == calendar_year
            and item.program_bank is not False
        )
        if statement_indices or references:
            result.append(
                RobinhoodProgramBankEvidence(
                    account_index=account_index,
                    interest_references=references,
                )
            )
    return tuple(result)


def _interest_cy_reconciles(
    paired: RobinhoodAccountsReconciliation,
    income: RobinhoodIncomeResult,
    account_index: int,
    calendar_year: int,
) -> bool:
    payments = tuple(
        item
        for item in income.interest_payments
        if item.event_reference.account_index == account_index
        and item.interest_credit.credit_date.year == calendar_year
    )
    account = paired.accounts[account_index]
    for statement in account.activity.primary_bundle.statements:
        if statement.period_end.year != calendar_year:
            continue
        rows = {item.label: item for item in statement.income}
        interest = rows.get("Interest Earned")
        if interest is None:
            return False
        period_total = sum(
            (
                item.interest_credit.amount_gross
                for item in payments
                if statement.period_start
                <= item.interest_credit.credit_date
                <= statement.period_end
            ),
            Decimal(),
        )
        cumulative = sum(
            (
                item.interest_credit.amount_gross
                for item in payments
                if item.interest_credit.credit_date <= statement.period_end
            ),
            Decimal(),
        )
        if interest.this_period_usd != period_total or interest.year_to_date_usd != cumulative:
            return False
    return True


def _plus_a2_proceeds(total: Decimal, cash_usd: Decimal, ttbr: Decimal) -> Decimal:
    """``total`` plus ``cash_usd`` at the December 31 ``ttbr``, both exact (#1383)."""
    at = "Robinhood Schedule FA A2 proceeds, INR"
    return checked_addition(total, checked_product(cash_usd, ttbr, at=at), at=at)


def _a2_gross_evidence(  # noqa: PLR0912
    paired: RobinhoodAccountsReconciliation,
    income: RobinhoodIncomeResult,
    options: RobinhoodOptionReconstruction,
    account_index: int,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> FaA2GrossEvidence:
    require(
        not options.blockers,
        what=(
            "a Robinhood option blocker reached A2 gross evidence; "
            "compute_robinhood_a2_peaks must refuse first"
        ),
        blockers=options.blockers,
    )
    unresolved: set[FaA2NatureOfAmount] = set()
    gross_credit_ttbr: Decimal | None = None
    year_end = date(calendar_year, 12, 31)

    if not income.schedule_os_complete:
        unresolved.update(
            {
                FaA2NatureOfAmount.DIVIDEND,
                FaA2NatureOfAmount.INTEREST,
                FaA2NatureOfAmount.OTHER_INCOME,
            }
        )
    if not _interest_cy_reconciles(paired, income, account_index, calendar_year):
        unresolved.add(FaA2NatureOfAmount.INTEREST)

    interest = [
        payment
        for payment in income.interest_payments
        if payment.event_reference.account_index == account_index
        and payment.interest_credit.credit_date.year == calendar_year
    ]
    if any(payment.program_bank is None for payment in interest):
        unresolved.add(FaA2NatureOfAmount.INTEREST)
    # The account's in-year dividends and resolved interest credits take the
    # shared A2 conversion: each total added exactly, then converted at the
    # December 31 TTBR.
    shared = build_gross_evidence(
        [
            payment.dividend
            for payment in income.dividend_payments
            if payment.event_reference.account_index == account_index
            and payment.dividend.payment_date.year == calendar_year
        ],
        [payment.interest_credit for payment in interest if payment.program_bank is not None],
        (),
        interest_non_credits=(),
        calendar_year=calendar_year,
        fx_policy=fx_policy,
        dividend_activity_present=False,
    )
    known: dict[FaA2NatureOfAmount, Decimal] = defaultdict(Decimal, shared.known_components)
    unresolved.update(shared.unresolved_natures)
    for payment in income.stock_lending_payments:
        other_income = payment.loan_fee_income or payment.substitute_dividend_income
        if (
            payment.event_reference.account_index == account_index
            and other_income is not None
            and other_income.payment_date.year == calendar_year
        ):
            if gross_credit_ttbr is None:
                gross_credit_ttbr = fx_policy.rate_for(year_end)
            known[FaA2NatureOfAmount.OTHER_INCOME] += other_income.amount_gross * gross_credit_ttbr

    # Each sale's recorded cash is its full value of consideration (owner
    # decision 2026-11-14, #1383; ADR 0006 section 7), so it files as A2
    # PROCEEDS: each equity sale's Amount and each option sell-to-close's cash.
    account = paired.accounts[account_index]
    for event in account.evidence.events:
        if (
            event.activity_date.year != calendar_year
            or event.kind not in ROBINHOOD_EQUITY_SALE_KINDS
        ):
            continue
        if event.amount_usd is None:
            raise _fail("Robinhood equity sale lacks its parser-proved cash amount")
        if gross_credit_ttbr is None:
            gross_credit_ttbr = fx_policy.rate_for(year_end)
        known[FaA2NatureOfAmount.PROCEEDS] = _plus_a2_proceeds(
            known[FaA2NatureOfAmount.PROCEEDS], event.amount_usd, gross_credit_ttbr
        )
    for trade in options.trades:
        if (
            trade.contract.account_index == account_index
            and trade.activity_date.year == calendar_year
            and trade.kind is RobinhoodEventKind.OPTION_SELL_TO_CLOSE
        ):
            if gross_credit_ttbr is None:
                gross_credit_ttbr = fx_policy.rate_for(year_end)
            known[FaA2NatureOfAmount.PROCEEDS] = _plus_a2_proceeds(
                known[FaA2NatureOfAmount.PROCEEDS], trade.exact_cash_usd, gross_credit_ttbr
            )

    return FaA2GrossEvidence(
        known_components=dict(known),
        unresolved_natures=tuple(sorted(unresolved, key=lambda nature: nature.value)),
    )


def _peak_floor_candidates(
    account: RobinhoodAccountReconciliation,
    calendar_year: int,
) -> Iterator[tuple[Decimal, date]]:
    """Each statement's USD peak-floor candidates in the calendar year, in
    statement order: its closing balance on ``period_end``, then its opening
    balance on ``period_start`` where all three opening values are printed."""
    for statement in account.activity.primary_bundle.statements:
        summary = statement.account_summary
        if statement.period_end.year == calendar_year:
            yield (
                summary.closing_brokerage_cash_usd
                + summary.closing_deposit_sweep_usd
                + summary.closing_total_securities_usd,
                statement.period_end,
            )
        if (
            statement.period_start.year == calendar_year
            and summary.opening_brokerage_cash_usd is not None
            and summary.opening_deposit_sweep_usd is not None
            and summary.opening_total_securities_usd is not None
        ):
            yield (
                summary.opening_brokerage_cash_usd
                + summary.opening_deposit_sweep_usd
                + summary.opening_total_securities_usd,
                statement.period_start,
            )


def _highest_peak_floor(candidates: list[tuple[Decimal, date]]) -> tuple[Decimal, date]:
    """The highest candidate, the later date on a tie; refuses when there is none."""
    if not candidates:
        raise _fail("Schedule FA calendar year has no statement peak floor")
    return max(candidates, key=lambda item: (item[0], item[1]))


def _peak_floor(
    account: RobinhoodAccountReconciliation,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> tuple[Decimal, date]:
    return _highest_peak_floor(
        [
            (value * fx_policy.rate_for(day), day)
            for value, day in _peak_floor_candidates(account, calendar_year)
        ]
    )


def _peak_floor_usd(
    account: RobinhoodAccountReconciliation,
    calendar_year: int,
) -> tuple[Decimal, date]:
    return _highest_peak_floor(list(_peak_floor_candidates(account, calendar_year)))


def _a2_closing_blockers(
    paired: RobinhoodAccountsReconciliation, calendar_year: int
) -> tuple[RobinhoodFaBlocker, ...]:
    """``A2_CLOSING`` for each account without exactly one statement ending
    on 31 December, whose closing balance Table A2 files."""
    year_end = date(calendar_year, 12, 31)
    return tuple(
        RobinhoodFaBlocker(kind=RobinhoodFaBlockerKind.A2_CLOSING, account_indices=(account_index,))
        for account_index, account in enumerate(paired.accounts)
        if sum(item.period_end == year_end for item in account.activity.primary_bundle.statements)
        != 1
    )


def _build_a2(
    paired: RobinhoodAccountsReconciliation,
    config_accounts: dict[str, RobinhoodAccountConfig],
    income: RobinhoodIncomeResult,
    options: RobinhoodOptionReconstruction,
    peaks_by_account: Mapping[int, RobinhoodA2Peak],
    program_bank: tuple[RobinhoodProgramBankEvidence, ...],
    calendar_year: int,
    fx_policy: FxPolicy,
    peak_currency: PeakCurrency,
) -> tuple[
    tuple[RobinhoodA2Review, ...],
    tuple[RobinhoodFaBlocker, ...],
    tuple[RobinhoodFaManualWork, ...],
]:
    reviews: list[RobinhoodA2Review] = []
    blockers = _a2_closing_blockers(paired, calendar_year)
    closing_blocked = {index for item in blockers for index in item.account_indices}
    manual: list[RobinhoodFaManualWork] = []
    bank_by_account = {item.account_index: item for item in program_bank}
    year_end = date(calendar_year, 12, 31)

    for account_index, account in enumerate(paired.accounts):
        if account_index in closing_blocked:
            continue
        number = account.activity.primary_bundle.account_number
        configured = config_accounts[number]
        (statement,) = (
            statement
            for statement in account.activity.primary_bundle.statements
            if statement.period_end == year_end
        )
        summary = statement.account_summary
        closing_usd = (
            summary.closing_brokerage_cash_usd
            + summary.closing_deposit_sweep_usd
            + summary.closing_total_securities_usd
        )
        if closing_usd != summary.closing_portfolio_value_usd:
            raise _fail("A2 closing contradicts the parsed statement")

        gross = _a2_gross_evidence(
            paired,
            income,
            options,
            account_index,
            calendar_year,
            fx_policy,
        )
        peak = peaks_by_account[account_index]
        floor_usd, floor_usd_date = _peak_floor_usd(account, calendar_year)
        floor, floor_date = (
            (floor_usd * fx_policy.rate_for(floor_usd_date), floor_usd_date)
            if peak_currency == "native"
            else _peak_floor(account, calendar_year, fx_policy)
        )
        bank = bank_by_account.get(account_index)
        row = build_fa_a2_row(
            identity=AccountIdentity(
                institution=InstitutionIdentity(
                    name=configured.institution_name,
                    address=configured.address,
                    zip_code=configured.zip_code,
                    country_iso2=configured.country_iso2,
                ),
                account_ref=configured.number,
                opening_date=configured.opening_date,
                status=configured.status,
                base_currency=statement.base_currency,
                statement_masked_account_ref=is_masked_account_id(number),
            ),
            nav=NetAssetValueSummary(
                closing_total=closing_usd,
                refusal_reason=None,
                cross_checked=False,
            ),
            closing_ttbr_dec31=fx_policy.rate_for(year_end),
            gross_components_inr=gross.known_components,
            period_end_cash_base_ccy=(
                summary.closing_brokerage_cash_usd + summary.closing_deposit_sweep_usd
            ),
            # The computed daily peak, as one audited bound.
            peak=FaA2PeakBounds(
                lower_inr=peak.peak_inr,
                lower_date=peak.peak_date,
                upper_inr=peak.peak_inr,
                upper_date=peak.peak_date,
                selected="lower",
                base_ccy=peak.peak_usd,
                ttbr=peak.ttbr,
                statement_floor_base_ccy=floor_usd,
                statement_floor_date=floor_usd_date,
            ),
        )
        rows = expand_fa_a2_nature_rows(row, gross)

        if gross.unresolved_natures:
            manual.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.A2_GROSS_CREDITS,
                    account_indices=(account_index,),
                )
            )
        if fa_a2_manual_fill_gaps(rows).zip_code:
            manual.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.A2_ZIP,
                    account_indices=(account_index,),
                )
            )
        reviews.extend(
            RobinhoodA2Review(
                account_index=account_index,
                row=filing_row,
                account_id_cross_checked=configured.number == number,
                peak_usd=peak.peak_usd,
                peak_ttbr=peak.ttbr,
                peak_floor_usd=floor_usd,
                peak_floor_inr=floor,
                peak_floor_date=floor_date,
                known_gross_components_inr={
                    key.value: value for key, value in gross.known_components.items()
                },
                unresolved_gross_natures=tuple(nature.value for nature in gross.unresolved_natures),
                program_bank=bank,
            )
            for filing_row in rows
        )
    return tuple(reviews), tuple(blockers), tuple(manual)


def _identity_by_reference(
    identities: RobinhoodSecurityIdentityResult,
) -> dict[RobinhoodSecurityEventReference, int]:
    return {
        reference: item.config_index
        for item in identities.resolved
        for reference in item.event_references
    }


def _movement_by_references(
    quantities: RobinhoodSecurityQuantityResult,
) -> dict[tuple[RobinhoodSecurityEventReference, ...], RobinhoodQuantityMovement]:
    result: dict[
        tuple[RobinhoodSecurityEventReference, ...],
        RobinhoodQuantityMovement,
    ] = {}
    for movement in quantities.movements:
        if movement.event_references in result:
            raise _fail("quantity movements duplicate one source-reference group")
        result[movement.event_references] = movement
    return result


def _disposal_movement_for_references(
    references: tuple[RobinhoodSecurityEventReference, ...],
    movement_by_references: Mapping[
        tuple[RobinhoodSecurityEventReference, ...],
        RobinhoodQuantityMovement,
    ],
) -> RobinhoodQuantityMovement | None:
    if len(set(references)) != len(references):
        raise _fail("disposal group reuses a source reference")
    direct = movement_by_references.get(references)
    if direct is None:
        if len(references) <= 1:
            return None
        movements = []
        for reference in references:
            movement = movement_by_references.get((reference,))
            if movement is None:
                return None
            if movement.event_references != (reference,):
                raise _fail("singleton disposal movement has invalid source references")
            movements.append(movement)
        grouped = combined_disposal(movements)
        if grouped.event_references != references:
            raise _fail("combined disposal references disagree with lot slices")
    else:
        grouped = direct
    if grouped.kind is RobinhoodQuantityMovementKind.DISPOSAL:
        complete_references = tuple(
            sorted(
                reference
                for movement in movement_by_references.values()
                if movement.kind is RobinhoodQuantityMovementKind.DISPOSAL
                and movement.account_index == grouped.account_index
                and movement.cusip == grouped.cusip
                and movement.activity_date == grouped.activity_date
                for reference in movement.event_references
            )
        )
        if complete_references != references:
            raise _fail("disposal group omits same-day source movements")
    return grouped


def _fragment_accounts(
    lots: RobinhoodSecurityLotResult,
    lot_id: RobinhoodSecurityEventReference,
    calendar_year: int,
    close_date: date | None,
) -> tuple[int, ...]:
    start = date(calendar_year, 1, 1)
    end = min(close_date or date(calendar_year, 12, 31), date(calendar_year, 12, 31))
    return tuple(
        sorted(
            {
                item.account_index
                for item in lots.holding_intervals
                if item.lot_id == lot_id
                and item.from_date <= end
                and (item.to_date is None or item.to_date >= start)
            }
        )
    )


def _sale_events(
    event_map: Mapping[RobinhoodSecurityEventReference, RobinhoodEvent],
    references: tuple[RobinhoodSecurityEventReference, ...],
) -> tuple[RobinhoodEvent, ...]:
    events = tuple(event_map[reference] for reference in references)
    if not events or any(event.kind not in ROBINHOOD_EQUITY_SALE_KINDS for event in events):
        raise ValueError
    if any(
        event.quantity is None
        or event.quantity <= 0
        or event.amount_usd is None
        or event.amount_usd <= 0
        for event in events
    ):
        raise _fail("sale movement lacks positive quantity or cash")
    return events


def _exact_sum(parts: tuple[tuple[Decimal, Fraction | None], ...]) -> Fraction | None:
    """The exact sum of ``(amount, exact amount)`` parts, such as costs or
    quantities, or ``None`` where their Decimal sum is exact. A part's exact
    amount is ``None`` where its Decimal is exact (#1241, #1263)."""
    exact_total = sum(
        (Fraction(amount) if exact is None else exact for amount, exact in parts), Fraction()
    )
    return (
        None
        if exact_total == Fraction(sum((amount for amount, _exact in parts), Decimal()))
        else exact_total
    )


def _group_disposal_slices(
    slices: tuple[RobinhoodLotSlice, ...],
) -> tuple[RobinhoodLotSlice, ...]:
    grouped: dict[
        RobinhoodSecurityEventReference,
        list[RobinhoodLotSlice],
    ] = defaultdict(list)
    for item in slices:
        grouped[item.lot_id].append(item)
    result: list[RobinhoodLotSlice] = []
    for _lot_id, items in sorted(grouped.items()):
        first = items[0]
        if any(
            item.cusip != first.cusip
            or item.original_date != first.original_date
            or item.movement_references != first.movement_references
            or item.kind is not RobinhoodLotSliceKind.DISPOSAL
            for item in items
        ):
            raise _fail("same-lot disposal fragments contradict each other")
        result.append(
            replace(
                first,
                quantity=sum((item.quantity for item in items), Decimal()),
                cost_usd=sum((item.cost_usd for item in items), Decimal()),
                # The pieces' exact costs and quantities (#1241, #1263) add up too.
                exact_cost_usd=_exact_sum(
                    tuple((item.cost_usd, item.exact_cost_usd) for item in items)
                ),
                exact_quantity=_exact_sum(
                    tuple((item.quantity, item.exact_quantity) for item in items)
                )
                if any(item.exact_quantity is not None for item in items)
                else None,
                residual_references=tuple(
                    sorted({reference for item in items for reference in item.residual_references})
                ),
            )
        )
    return tuple(result)


def _fragments(  # noqa: PLR0912, PLR0915
    paired: RobinhoodAccountsReconciliation,
    identities: RobinhoodSecurityIdentityResult,
    quantities: RobinhoodSecurityQuantityResult,
    lots: RobinhoodSecurityLotResult,
    calendar_year: int,
) -> tuple[tuple[_Fragment, ...], tuple[RobinhoodFaBlocker, ...]]:
    event_map = _event_map(paired)
    identity_by_reference = _identity_by_reference(identities)
    movement_by_references = _movement_by_references(quantities)
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    blockers: list[RobinhoodFaBlocker] = []
    disposed: list[_Fragment] = []
    # Each open part's (quantity, exact quantity, cost, exact cost), and its holdings.
    open_parts: dict[
        RobinhoodSecurityEventReference,
        list[tuple[Decimal, Fraction | None, Decimal, Fraction | None]],
    ] = defaultdict(list)
    open_holdings: dict[RobinhoodSecurityEventReference, list[_Holding]] = defaultdict(list)

    slices_by_movement: dict[
        tuple[RobinhoodSecurityEventReference, ...],
        list[RobinhoodLotSlice],
    ] = defaultdict(list)
    for item in lots.slices:
        if item.kind is RobinhoodLotSliceKind.DISPOSAL:
            slices_by_movement[item.movement_references].append(item)
    grouped_references = [
        reference for references in slices_by_movement for reference in references
    ]
    if len(grouped_references) != len(set(grouped_references)):
        raise _fail("one sale reference entered multiple disposal groups")
    if any(reference not in event_map for reference in grouped_references):
        raise _fail("disposal group references an unavailable source event")

    for references, slices in sorted(slices_by_movement.items()):
        lot_ids = tuple(sorted({item.lot_id for item in slices}))
        security_indices = tuple(
            sorted(
                {
                    identity_by_reference[lot_id]
                    for lot_id in lot_ids
                    if lot_id in identity_by_reference
                }
            )
        )
        movement = _disposal_movement_for_references(
            references,
            movement_by_references,
        )
        if movement is None or movement.kind is not RobinhoodQuantityMovementKind.DISPOSAL:
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_DISPOSAL,
                    security_config_indices=security_indices,
                    lot_ids=lot_ids,
                )
            )
            continue
        try:
            events = _sale_events(event_map, references)
        except ValueError:
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_DISPOSAL,
                    account_indices=(movement.account_index,),
                    security_config_indices=security_indices,
                    lot_ids=lot_ids,
                )
            )
            continue
        source_quantity = sum(
            (event.quantity for event in events if event.quantity is not None),
            Decimal(),
        )
        source_cash = sum(
            (event.amount_usd for event in events if event.amount_usd is not None),
            Decimal(),
        )
        slice_quantity = sum((item.quantity for item in slices), Decimal())
        if source_quantity != slice_quantity or -movement.signed_quantity != slice_quantity:
            raise _fail("sale quantity is not conserved across lot slices")
        proceeds_by_lot: dict[RobinhoodSecurityEventReference, Decimal] = defaultdict(Decimal)
        # Each lot's (proceeds, exact proceeds) shares, so A3 files them exactly (#1383).
        proceeds_parts_by_lot: dict[
            RobinhoodSecurityEventReference, list[tuple[Decimal, Fraction | None]]
        ] = defaultdict(list)
        allocated = allocate_disposal_proceeds(
            tuple(slices),
            source_quantity=source_quantity,
            source_cash=source_cash,
        )
        for item, proceeds, exact_proceeds in allocated:
            proceeds_by_lot[item.lot_id] += proceeds
            proceeds_parts_by_lot[item.lot_id].append((proceeds, exact_proceeds))
        if sum(proceeds_by_lot.values(), Decimal()) != source_cash:
            raise _fail("sale cash was not conserved across lot slices")

        for item in _group_disposal_slices(tuple(item for item, *_proceeds in allocated)):
            proceeds = proceeds_by_lot[item.lot_id]
            security_index = identity_by_reference.get(item.lot_id)
            if security_index is None:
                blockers.append(
                    RobinhoodFaBlocker(
                        kind=RobinhoodFaBlockerKind.A3_IDENTITY,
                        account_indices=(movement.account_index,),
                        lot_ids=(item.lot_id,),
                    )
                )
                continue
            holdings = tuple(
                _Holding(
                    piece.account_index, piece.entry_date, piece.quantity, piece.exact_quantity
                )
                for piece, *_proceeds in allocated
                if piece.lot_id == item.lot_id
            )
            if movement.activity_date > cy_end and item.original_date <= cy_end:
                open_parts[item.lot_id].append(
                    (item.quantity, item.exact_quantity, item.cost_usd, item.exact_cost_usd)
                )
                open_holdings[item.lot_id].extend(holdings)
            elif cy_start <= movement.activity_date <= cy_end:
                disposed.append(
                    _Fragment(
                        key=_FragmentKey(item.lot_id, references),
                        security_config_index=security_index,
                        quantity=item.quantity,
                        cost_usd=item.cost_usd,
                        open_date=item.original_date,
                        close_date=movement.activity_date,
                        account_indices=_fragment_accounts(
                            lots,
                            item.lot_id,
                            calendar_year,
                            movement.activity_date,
                        ),
                        holdings=holdings,
                        net_proceeds_usd=proceeds,
                        exact_cost_usd=item.exact_cost_usd,
                        exact_quantity=item.exact_quantity,
                        exact_net_proceeds_usd=_exact_sum(
                            tuple(proceeds_parts_by_lot[item.lot_id])
                        ),
                    )
                )

    for item in lots.open_fragments:
        if item.original_date <= cy_end:
            open_parts[item.lot_id].append(
                (item.quantity, item.exact_quantity, item.cost_usd, item.exact_cost_usd)
            )
            open_holdings[item.lot_id].append(
                _Holding(item.account_index, item.entry_date, item.quantity, item.exact_quantity)
            )

    acquisitions = {item.event_reference: item for item in quantities.acquisitions}
    disposal_by_lot: dict[
        RobinhoodSecurityEventReference,
        list[RobinhoodLotSlice],
    ] = defaultdict(list)
    for item in lots.slices:
        if item.kind is RobinhoodLotSliceKind.DISPOSAL:
            disposal_by_lot[item.lot_id].append(item)
    open_by_lot: dict[
        RobinhoodSecurityEventReference,
        list,
    ] = defaultdict(list)
    for item in lots.open_fragments:
        open_by_lot[item.lot_id].append(item)
    for lot_id in {
        *disposal_by_lot,
        *open_by_lot,
    }:
        acquisition = acquisitions.get(lot_id)
        if acquisition is None or acquisition.cost_usd is None:
            continue
        final_quantity = sum(
            (item.quantity for item in disposal_by_lot[lot_id]),
            Decimal(),
        ) + sum((item.quantity for item in open_by_lot[lot_id]), Decimal())
        final_cost = sum(
            (item.cost_usd for item in disposal_by_lot[lot_id]),
            Decimal(),
        ) + sum((item.cost_usd for item in open_by_lot[lot_id]), Decimal())
        # A3 files the exact costs (#1243), so they must add up too.
        final_exact_cost = sum(
            (
                Fraction(item.cost_usd) if item.exact_cost_usd is None else item.exact_cost_usd
                for item in (*disposal_by_lot[lot_id], *open_by_lot[lot_id])
            ),
            Fraction(),
        )
        # Only pro-rata quantities carry an exact conservation rule (#1263).
        pieces = (*disposal_by_lot[lot_id], *open_by_lot[lot_id])
        exact_quantity_conserved = True
        if any(item.exact_quantity is not None for item in pieces):
            final_exact_quantity = sum(
                (
                    Fraction(item.quantity) if item.exact_quantity is None else item.exact_quantity
                    for item in pieces
                ),
                Fraction(),
            )
            exact_quantity_conserved = final_exact_quantity == Fraction(acquisition.quantity)
        if (
            final_quantity != acquisition.quantity
            or final_cost != acquisition.cost_usd
            or final_exact_cost != Fraction(acquisition.cost_usd)
            or not exact_quantity_conserved
        ):
            raise _fail("economic lot fragments do not conserve source quantity and cost")

    open_fragments: list[_Fragment] = []
    for lot_id, parts in sorted(open_parts.items()):
        acquisition = acquisitions.get(lot_id)
        security_index = identity_by_reference.get(lot_id)
        if acquisition is None or acquisition.cost_usd is None or security_index is None:
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_IDENTITY,
                    lot_ids=(lot_id,),
                )
            )
            continue
        open_fragments.append(
            _Fragment(
                key=_FragmentKey(lot_id),
                security_config_index=security_index,
                quantity=sum((part[0] for part in parts), Decimal()),
                cost_usd=sum((part[2] for part in parts), Decimal()),
                exact_cost_usd=_exact_sum(
                    tuple((cost, exact) for _quantity, _exact, cost, exact in parts)
                ),
                exact_quantity=_exact_sum(
                    tuple((quantity, exact) for quantity, exact, _cost, _exact in parts)
                )
                if any(exact is not None for _quantity, exact, _cost, _exact in parts)
                else None,
                open_date=acquisition.activity_date,
                close_date=None,
                account_indices=_fragment_accounts(
                    lots,
                    lot_id,
                    calendar_year,
                    None,
                ),
                holdings=tuple(open_holdings[lot_id]),
            )
        )
    return tuple(sorted((*disposed, *open_fragments), key=lambda item: item.key)), tuple(blockers)


def _dec31_positions(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    quantities: RobinhoodSecurityQuantityResult,
    calendar_year: int,
) -> tuple[
    dict[int, _Dec31Position],
    tuple[RobinhoodFaBlocker, ...],
]:
    """Each security's Dec-31 effective quantity, value and tolerance.

    A settlement-date account with statement shares contributes its proved
    trade-date quantity and exactly scaled own total; other accounts keep
    statement contributions. Quantities and tolerances combine per security
    for comparison with the trade-dated lots (#1246).
    """
    held: dict[tuple[int, int], list[RobinhoodStatementPosition]] = {}
    tolerances: dict[int, Decimal] = defaultdict(Decimal)
    trade_dated: dict[tuple[int, int], Decimal] = {}
    whole_pending: set[int] = set()
    blockers: list[RobinhoodFaBlocker] = []
    cy_end = date(calendar_year, 12, 31)
    symbol_index: dict[str, int] = {
        item.instrument.symbol: index for index, item in enumerate(securities)
    }
    cusip_index = {item.cusip: index for index, item in enumerate(securities)}
    for account_index, account in enumerate(paired.accounts):
        statement = next(
            (
                item
                for item in account.activity.primary_bundle.statements
                if item.period_end == cy_end
            ),
            None,
        )
        if statement is None:
            continue
        for position in statement.positions:
            if position.option_expiration is not None:
                continue
            candidates = {
                index
                for index in (
                    symbol_index.get(position.symbol_or_cusip),
                    cusip_index.get(position.symbol_or_cusip),
                )
                if index is not None
            }
            if len(candidates) != 1:
                blockers.append(
                    RobinhoodFaBlocker(
                        kind=RobinhoodFaBlockerKind.A3_IDENTITY,
                        account_indices=(account_index,),
                    )
                )
                continue
            (security_index,) = candidates
            held.setdefault((account_index, security_index), []).append(position)
    for anchor in quantities.anchors:
        account = paired.accounts[anchor.account_index]
        statement = account.activity.primary_bundle.statements[anchor.statement_index]
        if statement.period_end != cy_end:
            continue
        security_index = cusip_index.get(anchor.cusip)
        if security_index is None:
            continue
        tolerances[security_index] += anchor.tolerance
        if anchor.snapshot_basis is not RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE:
            continue
        if anchor.statement_quantity == 0:
            whole_pending.add(security_index)
            continue
        key = (anchor.account_index, security_index)
        if (
            key not in held
            or sum((item.quantity for item in held[key]), Decimal()) != anchor.statement_quantity
        ):
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_CLOSING,
                    security_config_indices=(security_index,),
                )
            )
            continue
        trade_dated[key] = anchor.trade_date_quantity
    # Scaling another account must not price a whole holding the statement omits.
    blockers.extend(
        RobinhoodFaBlocker(
            kind=RobinhoodFaBlockerKind.A3_CLOSING,
            security_config_indices=(security_index,),
        )
        for security_index in sorted(whole_pending & {index for _, index in trade_dated})
    )
    parts: dict[int, list[tuple[list[RobinhoodStatementPosition], Decimal | None]]] = {
        index: [] for index in tolerances
    }
    for key, positions in held.items():
        parts.setdefault(key[1], []).append((positions, trade_dated.get(key)))
    return (
        {index: _dec31_position(index, items, tolerances[index]) for index, items in parts.items()},
        tuple(blockers),
    )


def _dec31_position(
    security_index: int,
    parts: list[tuple[list[RobinhoodStatementPosition], Decimal | None]],
    tolerance: Decimal,
) -> _Dec31Position:
    """Add up one security's accounts: each its original statement rows and
    the trade-date quantity they scale to, or ``None``."""
    if all(trade is None for _, trade in parts):
        quantity = sum((item.quantity for rows, _ in parts for item in rows), Decimal())
        total = sum((item.market_value_usd for rows, _ in parts for item in rows), Decimal())
        return _Dec31Position(quantity, total, tolerance)
    sources = [[exact_fraction(item.market_value_usd) for item in rows] for rows, _ in parts]
    if any(value is None for values in sources for value in values):
        raise _share_error(security_index, "closing")
    totals = [
        sum((value for value in values if value is not None), Fraction()) for values in sources
    ]
    shares = [
        total
        if trade is None
        else exact_share(total, trade, sum((item.quantity for item in rows), Decimal()))
        for (rows, trade), total in zip(parts, totals, strict=True)
    ]
    if any(share is None for share in shares):
        raise _share_error(security_index, "closing")
    exact = sum((share for share in shares if share is not None), Fraction())
    quantity = sum(
        (
            sum((item.quantity for item in rows), Decimal()) if trade is None else trade
            for rows, trade in parts
        ),
        Decimal(),
    )
    return _Dec31Position(quantity, exact_decimal(exact), tolerance, exact)


def _unshown_open_lots(
    fragments: tuple[_Fragment, ...], positions: Mapping[int, _Dec31Position]
) -> tuple[RobinhoodFaBlocker, ...]:
    """``A3_CLOSING`` for each security whose open lots take no share of the
    statement ending on 31 December, whatever its price: the statement shows
    none of the security's shares, or a number further from the lots' than
    its tolerance."""
    open_by_security: dict[int, list[_Fragment]] = defaultdict(list)
    for item in fragments:
        if item.close_date is None:
            open_by_security[item.security_config_index].append(item)
    blockers: list[RobinhoodFaBlocker] = []
    for security_index, items in sorted(open_by_security.items()):
        position = positions.get(security_index, _Dec31Position(Decimal(), Decimal(), Decimal()))
        quantity = sum((item.quantity for item in items), Decimal())
        if not position.quantity or abs(quantity - position.quantity) > position.tolerance:
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_CLOSING,
                    security_config_indices=(security_index,),
                    lot_ids=tuple(item.key.lot_id for item in items),
                )
            )
    return tuple(blockers)


def _temporary_lots(  # noqa: PLR0915
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    fragments: tuple[_Fragment, ...],
    positions: Mapping[int, _Dec31Position],
    prices_by_ticker: Mapping[Ticker, PriceSeries] | None,
    calendar_year: int,
) -> tuple[
    dict[_FragmentKey, OpenLot | ClosedLot],
    tuple[RobinhoodFaBlocker, ...],
]:
    event_map = _event_map(paired)
    lots: dict[_FragmentKey, OpenLot | ClosedLot] = {}
    blockers: list[RobinhoodFaBlocker] = []
    open_by_security: dict[int, list[_Fragment]] = defaultdict(list)
    for item in fragments:
        if item.close_date is None:
            open_by_security[item.security_config_index].append(item)
            continue
        security = securities[item.security_config_index]
        event = event_map[item.key.lot_id]
        if event.issuer_name is None or item.net_proceeds_usd is None:
            raise _fail("closed A3 fragment lacks issuer or net proceeds")
        cost, exact_cost = review_amount(item.cost_usd, item.exact_cost_usd)
        quantity, exact_quantity = review_amount(item.quantity, item.exact_quantity)
        proceeds, exact_proceeds = review_amount(item.net_proceeds_usd, item.exact_net_proceeds_usd)
        lots[item.key] = ClosedLot(
            currency_primary="USD",
            sub_category=security.sub_category,
            symbol=security.instrument.symbol,
            description=event.issuer_name,
            isin=security.instrument.isin,
            listing_exchange=security.instrument.listing_exchange,
            issuer_country_code=security.instrument.issuer_country_code,
            quantity=quantity,
            cost_basis_money=cost,
            proceeds_money=proceeds,
            open_date_time=item.open_date,
            close_date_time=item.close_date,
            # The recorded Amount is the sale's full value of consideration
            # (owner decision 2026-11-14, #1383; ADR 0006 section 7).
            gross_proceeds_money=proceeds,
            basis_provenance=BasisProvenance.ENGINE_FIFO,
            cost_basis_money_exact=exact_cost,
            proceeds_money_exact=exact_proceeds,
            quantity_exact=exact_quantity,
            gross_proceeds_money_exact=exact_proceeds,
        )

    valuation_date = date(calendar_year, 12, 31)
    unshown = _unshown_open_lots(fragments, positions)
    blockers.extend(unshown)
    unshown_indices = {index for item in unshown for index in item.security_config_indices}
    for security_index, items in sorted(open_by_security.items()):
        if security_index in unshown_indices:
            continue
        position = positions.get(security_index, _Dec31Position(Decimal(), Decimal(), Decimal()))
        statement_quantity, statement_total = position.quantity, position.total_usd
        security = securities[security_index]
        ordered = tuple(sorted(items, key=lambda item: item.key))
        remaining_value = statement_total
        # A split total's exact shares (#1243), and every share of a total a pending
        # trade scaled, even a lone lot's (#1246). The last takes the rest, so they
        # add back exactly.
        scaled = position.exact_total_usd
        split = len(ordered) > 1 or scaled is not None
        remaining_exact = (
            (exact_fraction(statement_total) if scaled is None else scaled) if split else None
        )
        for index, item in enumerate(ordered):
            last = index == len(ordered) - 1
            allocated = (
                remaining_value if last else statement_total * item.quantity / statement_quantity
            )
            remaining_value -= allocated
            exact_allocated: Fraction | None = None
            if split:
                share = (
                    remaining_exact
                    if last
                    else exact_share(
                        statement_total if scaled is None else scaled,
                        item.share_quantity,
                        statement_quantity,
                    )
                )
                if share is None or remaining_exact is None:
                    raise _share_error(security_index, "closing")
                remaining_exact -= share
                if Fraction(allocated) != share:
                    allocated = exact_decimal(share)
                # Keep even Decimal-exact splits on the rational conversion path.
                exact_allocated = share
            quantity, exact_quantity = review_amount(item.quantity, item.exact_quantity)
            try:
                resolved = resolve_dec31_close(
                    valuation_date=valuation_date,
                    quantity=quantity,
                    statement_total_usd=(exact_allocated if scaled is not None else allocated),
                    series=(
                        prices_by_ticker.get(security.instrument.symbol)
                        if prices_by_ticker is not None
                        else None
                    ),
                )
            except ClosingPriceError:
                blockers.append(
                    RobinhoodFaBlocker(
                        kind=RobinhoodFaBlockerKind.A3_CLOSING,
                        security_config_indices=(security_index,),
                        lot_ids=(item.key.lot_id,),
                    )
                )
                continue
            event = event_map[item.key.lot_id]
            if event.issuer_name is None:
                raise _fail("open A3 fragment lacks issuer provenance")
            cost, exact_cost = review_amount(item.cost_usd, item.exact_cost_usd)
            lots[item.key] = OpenLot(
                currency_primary="USD",
                symbol=security.instrument.symbol,
                description=event.issuer_name,
                isin=security.instrument.isin,
                issuer_country_code=security.instrument.issuer_country_code,
                report_date=valuation_date,
                quantity=quantity,
                mark_price=resolved.total_usd / quantity,
                cost_basis_money=cost,
                side=Side.LONG,
                open_date_time=item.open_date,
                closing_total_usd=resolved.total_usd,
                cost_basis_money_exact=exact_cost,
                closing_total_usd_exact=exact_allocated,
                quantity_exact=exact_quantity,
            )
        if remaining_value != 0:
            raise _fail("statement value was not conserved across open A3 fragments")
    return lots, tuple(blockers)


def _source_credits(
    paired: RobinhoodAccountsReconciliation,
    income: RobinhoodIncomeResult,
    calendar_year: int,
) -> tuple[_Credit, ...]:
    event_map = _event_map(paired)
    result: list[_Credit] = []
    for item in income.dividend_payments:
        if item.dividend.payment_date.year != calendar_year:
            continue
        event = event_map[
            RobinhoodSecurityEventReference(
                item.event_reference.account_index,
                item.event_reference.event_ordinal,
            )
        ]
        if event.instrument is None:
            raise _fail("dividend source event lacks its symbol")
        if item.dividend.holding_date is None:
            raise _fail(
                "dividend record date precedes US T+1 settlement, so its A3 holding "
                "date is unproven"
            )
        result.append(
            _Credit(
                reference=item.event_reference,
                security_config_index=item.security_config_index,
                holding_date=item.dividend.holding_date,
                credit_date=item.source_payment_date,
                amount_usd=item.dividend.amount_gross,
                source_quantity=event.description_shares,
                distribution_rate_usd=event.description_rate_usd,
                symbol=event.instrument,
            )
        )
    for item in income.stock_lending_payments:
        other_income = item.loan_fee_income or item.substitute_dividend_income
        if (
            item.security_config_index is None
            or other_income is None
            or other_income.payment_date.year != calendar_year
        ):
            continue
        event = event_map[
            RobinhoodSecurityEventReference(
                item.event_reference.account_index,
                item.event_reference.event_ordinal,
            )
        ]
        if event.instrument is None:
            raise _fail("Stock Lending source event lacks its symbol")
        result.append(
            _Credit(
                reference=item.event_reference,
                security_config_index=item.security_config_index,
                holding_date=other_income.payment_date,
                credit_date=other_income.payment_date,
                amount_usd=other_income.amount_gross,
                source_quantity=None,
                distribution_rate_usd=None,
                symbol=event.instrument,
                monthly_loan_fee=item.loan_fee_income is not None,
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.security_config_index,
                item.holding_date,
                item.credit_date,
                item.distribution_rate_usd or Decimal(),
                item.symbol,
                item.monthly_loan_fee,
                item.reference,
            ),
        )
    )


def _contains_holding_day(
    start: date,
    end: date | None,
    requirement: _LoanRequirement,
) -> bool:
    return start <= requirement.day and (
        end is None
        or requirement.day < end
        or (requirement.close_inclusive and requirement.day == end)
    )


def _loan_fee_requirements(  # noqa: PLR0911
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    credit: _Credit,
    calendar_year: int,
) -> tuple[_LoanRequirement, ...]:
    if (
        not credit.monthly_loan_fee
        or credit.credit_date.year != calendar_year
        or securities[credit.security_config_index].instrument.listing_exchange not in _US_MARKETS
    ):
        return ()
    month_end = credit.credit_date.replace(day=1) - timedelta(days=1)
    month_start = month_end.replace(day=1)
    sessions = us_market_sessions(month_start, credit.credit_date)
    prior_sessions = tuple(day for day in sessions if day <= month_end)
    if not prior_sessions:
        return ()
    post_month_sessions = tuple(day for day in sessions if day > prior_sessions[-1])
    if (
        credit.credit_date not in post_month_sessions
        or post_month_sessions.index(credit.credit_date) >= _LOAN_FEE_PAYMENT_SESSIONS
    ):
        return ()

    statements = paired.accounts[credit.reference.account_index].source_bundle.statements
    matches = tuple(
        statement
        for statement in statements
        if statement.period_start == month_start and statement.period_end == month_end
    )
    previous = tuple(
        statement for statement in statements if statement.period_end == month_start - timedelta(1)
    )
    if len(matches) != 1 or len(previous) != 1:
        return ()
    statement = matches[0]
    rows = tuple(
        row
        for row in statement.stock_loan_activity
        if row.symbol == credit.symbol and month_start <= row.transaction_date <= month_end
    )
    if not rows:
        return ()

    closing = sum(
        (
            row.shares_lent
            for row in statement.stock_loaned_securities
            if row.symbol == credit.symbol
        ),
        Decimal(),
    )
    previous_closing = sum(
        (
            row.shares_lent
            for row in previous[0].stock_loaned_securities
            if row.symbol == credit.symbol
        ),
        Decimal(),
    )
    lent = sum((row.shares for row in rows if row.kind is RobinhoodStockLoanKind.LEND), Decimal())
    returned = sum(
        (row.shares for row in rows if row.kind is RobinhoodStockLoanKind.RETURN), Decimal()
    )
    opening = closing - lent + returned
    if opening < 0 or opening != previous_closing:
        return ()

    by_day: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal(), Decimal()])
    for row in rows:
        side = 0 if row.kind is RobinhoodStockLoanKind.LEND else 1
        by_day[row.transaction_date][side] += row.shares
    requirements: list[_LoanRequirement] = []
    balance = opening
    day = month_start
    while day <= month_end:
        lent_today, returned_today = by_day[day]
        peak = balance + lent_today
        if returned_today > peak:
            return ()
        if peak > 0:
            requirements.append(
                _LoanRequirement(
                    day=day,
                    quantity=peak,
                    close_inclusive=lent_today == 0,
                    new_lending=lent_today > 0,
                )
            )
        balance = peak - returned_today
        day += timedelta(days=1)
    return tuple(requirements) if balance == closing else ()


def _delayed_loan_fee_fragment(
    fragments: tuple[_Fragment, ...],
    fragment_blockers: tuple[RobinhoodFaBlocker, ...],
    lots: RobinhoodSecurityLotResult,
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    source_credits: list[_Credit],
    calendar_year: int,
) -> _Fragment | None:
    if len(source_credits) != 1 or not source_credits[0].monthly_loan_fee:
        return None
    credit = source_credits[0]
    if (
        lots.blockers
        or lots.identity_blockers
        or any(
            credit.security_config_index in blocker.security_config_indices
            or (
                blocker.kind is RobinhoodFaBlockerKind.A3_IDENTITY
                and not blocker.security_config_indices
            )
            for blocker in fragment_blockers
        )
    ):
        return None
    requirements = _loan_fee_requirements(paired, securities, credit, calendar_year)
    if not requirements:
        return None
    account_index = credit.reference.account_index
    intervals_by_lot: dict[
        RobinhoodSecurityEventReference,
        tuple[RobinhoodLotHoldingInterval, ...],
    ] = {}
    for fragment in fragments:
        if fragment.security_config_index != credit.security_config_index:
            continue
        intervals_by_lot[fragment.key.lot_id] = tuple(
            item
            for item in lots.holding_intervals
            if item.lot_id == fragment.key.lot_id and item.account_index == account_index
        )
    event_map = _event_map(paired)

    def fragment_matches(
        fragment: _Fragment,
        requirement: _LoanRequirement,
        *,
        require_quantity: bool,
    ) -> bool:
        if fragment.close_date is not None and requirement.day > fragment.close_date:
            account_intervals = intervals_by_lot.get(fragment.key.lot_id, ())
            settlement_dates = tuple(
                event_map[reference].settle_date
                for reference in fragment.key.close_references
                if event_map[reference].settle_date is not None
            )
            return (
                not requirement.new_lending
                and any(
                    item.from_date <= fragment.close_date
                    and (item.to_date is None or item.to_date >= fragment.close_date)
                    for item in account_intervals
                )
                and bool(settlement_dates)
                and requirement.day <= max(settlement_dates)
                and (not require_quantity or fragment.quantity >= requirement.quantity)
            )
        if not _contains_holding_day(fragment.open_date, fragment.close_date, requirement):
            return False
        if require_quantity and fragment.quantity < requirement.quantity:
            return False
        return any(
            _contains_holding_day(item.from_date, item.to_date, requirement)
            and (not require_quantity or item.quantity >= requirement.quantity)
            for item in intervals_by_lot.get(fragment.key.lot_id, ())
        )

    possible = tuple(
        fragment
        for fragment in fragments
        if fragment.security_config_index == credit.security_config_index
        and any(
            fragment_matches(fragment, requirement, require_quantity=False)
            for requirement in requirements
        )
    )
    if len(possible) != 1:
        return None
    candidate = possible[0]
    return (
        candidate
        if all(
            fragment_matches(candidate, requirement, require_quantity=True)
            for requirement in requirements
        )
        else None
    )


def _accounts_on(
    lot_intervals: tuple[RobinhoodLotHoldingInterval, ...],
    holding: _Holding,
    day: date,
) -> frozenset[int]:
    """Each account that may have held ``holding`` at the end of ``day`` (#1318).

    A part that entered its account after ``day`` arrived by an internal
    transfer out of the other account that day; ``lot_intervals``, the lot's
    holding intervals, give each part that transfer took and when it had
    entered that account. One transfer can take parts that entered at
    different times, so the result can name both accounts.
    """
    accounts: set[int] = set()
    visited: set[tuple[int, date]] = set()
    pending = [(holding.account_index, holding.entry_date)]
    while pending:
        account_index, entered = pending.pop()
        if (account_index, entered) in visited:
            continue
        visited.add((account_index, entered))
        if entered <= day:
            accounts.add(account_index)
            continue
        previous = tuple(
            item
            for item in lot_intervals
            if item.account_index != account_index and item.to_date == entered
        )
        if not previous or any(item.from_date >= entered for item in previous):
            raise _fail("lot holding intervals do not trace a transferred part to its account")
        pending.extend((item.account_index, item.from_date) for item in previous)
    return frozenset(accounts)


def _held_in_account(
    fragment: _Fragment,
    lot_intervals: tuple[RobinhoodLotHoldingInterval, ...],
    account_index: int,
    day: date,
) -> tuple[Decimal, Decimal | Fraction] | None:
    """The quantity and exact quantity of ``fragment`` that ``account_index``
    held at the end of ``day``, or ``None`` where a part may have been in
    either account (#1318). A fragment held wholly in the account keeps its own
    quantities, as before."""
    held: list[_Holding] = []
    for holding in fragment.holdings:
        accounts = _accounts_on(lot_intervals, holding, day)
        if account_index in accounts:
            if len(accounts) > 1:
                return None
            held.append(holding)
    if not held:
        return Decimal(), Fraction()
    if len(held) == len(fragment.holdings):
        return fragment.quantity, fragment.share_quantity
    return (
        sum((item.quantity for item in held), Decimal()),
        sum(
            (
                Fraction(item.quantity) if item.exact_quantity is None else item.exact_quantity
                for item in held
            ),
            Fraction(),
        ),
    )


def _credit_shares(
    source_credits: list[_Credit],
    quantities: tuple[Decimal | Fraction, ...],
    rate: Decimal,
    security_index: int,
) -> tuple[tuple[Fraction, Fraction], ...]:
    """Each holder's exact USD and INR share of ``source_credits``' total, by
    its ``quantities`` entry (#1243).

    A lone holder takes the whole total, as a lone fragment takes a whole
    statement total. Refuses past the digit cap, naming only the security's
    1-based index.
    """
    total = Fraction()
    for credit in source_credits:
        amount = exact_fraction(credit.amount_usd)
        if amount is None:
            raise _share_error(security_index, "credit")
        total += amount
    exact_rate = exact_fraction(rate)
    shares = (total,) if len(quantities) == 1 else split_a3_credit_by_holding(total, quantities)
    if shares is None or exact_rate is None:
        raise _share_error(security_index, "credit")
    return tuple((share, share * exact_rate) for share in shares)


def _allocate_credits(  # noqa: PLR0912
    fragments: tuple[_Fragment, ...],
    fragment_blockers: tuple[RobinhoodFaBlocker, ...],
    income: RobinhoodIncomeResult,
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    lots: RobinhoodSecurityLotResult,
    calendar_year: int,
    fx_policy: FxPolicy,
) -> tuple[
    dict[_FragmentKey, Decimal | None],
    dict[_FragmentKey, Decimal | None],
    dict[_FragmentKey, tuple[RobinhoodIncomeEventReference, ...]],
    dict[_FragmentKey, tuple[RobinhoodIncomeEventReference, ...]],
    tuple[RobinhoodFaBlocker, ...],
]:
    """Each fragment's USD and INR gross credits, computed from exact shares,
    plus source references, delayed loan fee references, and credit-attribution blockers.

    Each credit goes only to the lots held in the account that received it at
    the end of its holding date, split by the quantity each lot held there
    (#1318). A lot moved by an internal transfer counts for the account that
    held it that day.
    """
    if not income.schedule_os_complete:
        return (
            {item.key: None for item in fragments},
            {item.key: None for item in fragments},
            {item.key: () for item in fragments},
            {item.key: () for item in fragments},
            (
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION,
                    account_indices=tuple(
                        sorted(
                            {
                                index
                                for blocker in income.blockers
                                if blocker.stops_schedule_os
                                # An unbound source form blocks the whole FA account bundle.
                                for index in (
                                    blocker.account_indices or range(len(paired.accounts))
                                )
                            }
                        )
                    ),
                    security_config_indices=tuple(
                        sorted({item.security_config_index for item in fragments})
                    ),
                    income_blocked=True,
                ),
            ),
        )

    # Exact rationals until the end (#1243), so a row's credit is never a rounded share.
    usd: dict[_FragmentKey, Fraction | None] = {item.key: Fraction() for item in fragments}
    inr: dict[_FragmentKey, Fraction | None] = {item.key: Fraction() for item in fragments}
    references: dict[
        _FragmentKey,
        list[RobinhoodIncomeEventReference],
    ] = defaultdict(list)
    accrual_references: dict[
        _FragmentKey,
        list[RobinhoodIncomeEventReference],
    ] = defaultdict(list)
    blockers: list[RobinhoodFaBlocker] = []
    grouped: dict[
        tuple[int, int, date, date, Decimal | None, bool],
        list[_Credit],
    ] = defaultdict(list)
    for credit in _source_credits(paired, income, calendar_year):
        grouped[
            (
                credit.reference.account_index,
                credit.security_config_index,
                credit.holding_date,
                credit.credit_date,
                credit.distribution_rate_usd,
                credit.monthly_loan_fee,
            )
        ].append(credit)
    intervals_by_lot: dict[
        RobinhoodSecurityEventReference,
        list[RobinhoodLotHoldingInterval],
    ] = defaultdict(list)
    for interval in lots.holding_intervals:
        intervals_by_lot[interval.lot_id].append(interval)

    for (
        account_index,
        security_index,
        holding_date,
        _credit_date,
        _distribution_rate,
        monthly_loan_fee,
    ), source_credits in sorted(grouped.items(), key=lambda item: repr(item[0])):
        in_account = {
            item.key: _held_in_account(
                item, tuple(intervals_by_lot[item.key.lot_id]), account_index, holding_date
            )
            for item in fragments
            if item.security_config_index == security_index
            and held_on(item.open_date, item.close_date, holding_date)
        }
        undecided = tuple(key for key, held in in_account.items() if held is None)
        held_quantities = {
            key: held for key, held in in_account.items() if held is not None and held[0] > 0
        }
        delayed_fragment = None
        if not held_quantities and not undecided and monthly_loan_fee:
            delayed_fragment = _delayed_loan_fee_fragment(
                fragments,
                fragment_blockers,
                lots,
                paired,
                securities,
                source_credits,
                calendar_year,
            )
            if delayed_fragment is not None:
                held_quantities = {
                    delayed_fragment.key: (
                        delayed_fragment.quantity,
                        delayed_fragment.share_quantity,
                    )
                }
        total_quantity = sum((quantity for quantity, _exact in held_quantities.values()), Decimal())
        source_quantities = [
            item.source_quantity for item in source_credits if item.source_quantity is not None
        ]
        if (
            undecided
            or not held_quantities
            or total_quantity <= 0
            or (
                source_quantities
                and abs(sum(source_quantities, Decimal()) - total_quantity)
                > sum(
                    (robinhood_half_ulp(value) for value in source_quantities),
                    robinhood_half_ulp(total_quantity),
                )
            )
        ):
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION,
                    security_config_indices=(security_index,),
                    lot_ids=tuple(key.lot_id for key in sorted((*held_quantities, *undecided))),
                )
            )
            for item in fragments:
                if item.security_config_index == security_index:
                    usd[item.key] = None
                    inr[item.key] = None
            continue

        ordered = tuple(sorted(held_quantities))
        shares = _credit_shares(
            source_credits,
            tuple(held_quantities[key][1] for key in ordered),
            fx_policy.rate_for(date(calendar_year, 12, 31)),
            security_index,
        )
        for key, (allocated_usd, allocated_inr) in zip(ordered, shares, strict=True):
            current_usd = usd[key]
            current_inr = inr[key]
            if current_usd is not None:
                usd[key] = current_usd + allocated_usd
            if current_inr is not None:
                inr[key] = current_inr + allocated_inr
            references[key].extend(credit.reference for credit in source_credits)
            if delayed_fragment is not None and delayed_fragment.key == key:
                accrual_references[key].extend(credit.reference for credit in source_credits)
    return (
        {key: None if value is None else exact_decimal(value) for key, value in usd.items()},
        {key: None if value is None else exact_decimal(value) for key, value in inr.items()},
        {key: tuple(value) for key, value in references.items()},
        {key: tuple(value) for key, value in accrual_references.items()},
        tuple(blockers),
    )


def _build_a3(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    income: RobinhoodIncomeResult,
    fragments: tuple[_Fragment, ...],
    fragment_blockers: tuple[RobinhoodFaBlocker, ...],
    lots: RobinhoodSecurityLotResult,
    temporary_lots: Mapping[_FragmentKey, OpenLot | ClosedLot],
    prices_by_ticker: Mapping[Ticker, PriceSeries] | None,
    entity_overrides: Mapping[Isin, FaEntityOverride],
    nature_by_isin: Mapping[Isin, str],
    calendar_year: int,
    fx_policy: FxPolicy,
    peak_basis: PeakBasis,
    peak_currency: PeakCurrency,
) -> tuple[
    tuple[RobinhoodA3Review, ...],
    tuple[RobinhoodFaBlocker, ...],
    tuple[RobinhoodFaManualWork, ...],
]:
    (
        usd_credits,
        inr_credits,
        _credit_refs,
        _accrual_credit_refs,
        blockers,
    ) = _allocate_credits(
        fragments,
        fragment_blockers,
        income,
        paired,
        securities,
        lots,
        calendar_year,
        fx_policy,
    )
    reviews: list[RobinhoodA3Review] = []
    manual: list[RobinhoodFaManualWork] = []
    for item in fragments:
        lot = temporary_lots.get(item.key)
        if lot is None:
            continue
        credit_inr = inr_credits[item.key]
        overlap = (
            CyOverlap(open_lots=[lot], closed_lots=[])
            if isinstance(lot, OpenLot)
            else CyOverlap(open_lots=[], closed_lots=[lot])
        )
        rows = build_fa_a3_rows(
            overlap,
            calendar_year=calendar_year,
            fx_policy=fx_policy,
            dividends_by_symbol_inr=(None if credit_inr is None else {lot.symbol: credit_inr}),
            prices_by_ticker=prices_by_ticker,
            entity_overrides=entity_overrides,
            nature_by_isin=nature_by_isin,
            peak_basis=peak_basis,
            peak_currency=peak_currency,
        )
        if len(rows) != 1:
            raise _fail("one economic Robinhood fragment did not produce one A3 row")
        row = rows[0]
        if row.gross_amount_paid_credited_inr is None:
            manual.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.A3_GROSS_CREDITS,
                    security_config_indices=(item.security_config_index,),
                    lot_ids=(item.key.lot_id,),
                )
            )
        if row.peak_value_inr is None:
            manual.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.A3_PEAK,
                    security_config_indices=(item.security_config_index,),
                    lot_ids=(item.key.lot_id,),
                )
            )
        security = securities[item.security_config_index]
        override = entity_overrides.get(security.instrument.isin)
        # #793: an ETF's broker description is not its legal name.
        etf_unnamed = (
            security.sub_category == "ETF"
            and not security.is_depositary_receipt
            and (override is None or override.name is None)
        )
        # The shared rule (#1391): a whitespace-only cell is blank.
        gaps = fa_a3_manual_fill_gaps(rows, dividends_resolved=True)
        identity_gaps = frozenset(
            RobinhoodA3IdentityGapHolding(gap, symbol, isin)
            for gap, holdings in (
                (RobinhoodA3IdentityGap.ADDRESS, gaps.address_of_entity),
                (RobinhoodA3IdentityGap.ZIP_CODE, gaps.zip_code),
                (RobinhoodA3IdentityGap.NATURE, gaps.nature_of_entity),
                (
                    RobinhoodA3IdentityGap.ETF_NAME,
                    {(row.source_ticker, row.source_isin) for row in rows} if etf_unnamed else (),
                ),
            )
            for symbol, isin in holdings
        )
        if identity_gaps or gaps.name_of_entity:
            manual.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.A3_IDENTITY,
                    security_config_indices=(item.security_config_index,),
                    lot_ids=(item.key.lot_id,),
                    identity_gaps=identity_gaps,
                )
            )
        reviews.append(
            RobinhoodA3Review(
                row=row,
                lot_id=item.key.lot_id,
                account_indices=item.account_indices,
                close_references=item.key.close_references,
                gross_credit_usd=usd_credits[item.key],
            )
        )
    return tuple(reviews), blockers, tuple(manual)


@dataclass(frozen=True, slots=True)
class _A3Sources:
    """Table A3's lots, fragments and 31 December positions, and the blockers
    these steps find. None reads a price (#1574)."""

    lots: RobinhoodSecurityLotResult
    fragments: tuple[_Fragment, ...]
    fragment_blockers: tuple[RobinhoodFaBlocker, ...]
    positions: dict[int, _Dec31Position]
    blockers: tuple[RobinhoodFaBlocker, ...]
    """The identity, inventory, lot, fragment and position blockers."""


def _a3_sources(
    paired: RobinhoodAccountsReconciliation,
    securities: tuple[RobinhoodSecurityConfig, ...],
    identities: RobinhoodSecurityIdentityResult,
    quantities: RobinhoodSecurityQuantityResult,
    calendar_year: int,
) -> _A3Sources:
    lots = reconcile_robinhood_security_lots(quantities)
    source_blockers: list[RobinhoodFaBlocker] = []
    if identities.blockers:
        source_blockers.append(RobinhoodFaBlocker(kind=RobinhoodFaBlockerKind.A3_IDENTITY))
    if quantities.blockers:
        source_blockers.append(RobinhoodFaBlocker(kind=RobinhoodFaBlockerKind.A3_INVENTORY))
    if lots.blockers:
        source_blockers.append(
            RobinhoodFaBlocker(
                kind=RobinhoodFaBlockerKind.A3_LOTS, source_lot_blockers=lots.blockers
            )
        )
    fragments, fragment_blockers = _fragments(
        paired,
        identities,
        quantities,
        lots,
        calendar_year,
    )
    positions, position_blockers = _dec31_positions(
        paired,
        securities,
        quantities,
        calendar_year,
    )
    return _A3Sources(
        lots=lots,
        fragments=fragments,
        fragment_blockers=fragment_blockers,
        positions=positions,
        blockers=(*source_blockers, *fragment_blockers, *position_blockers),
    )


def _option_manual_work(
    options: RobinhoodOptionReconstruction,
    calendar_year: int,
) -> tuple[
    tuple[RobinhoodFaManualWork, ...],
    tuple[RobinhoodFaBlocker, ...],
]:
    active_accounts = {
        trade.contract.account_index
        for trade in options.trades
        if trade.activity_date.year == calendar_year
    } | {
        anchor.contract.account_index
        for anchor in options.valuation_anchors
        if anchor.statement_date.year == calendar_year
    }
    classifications = {item.account_index: item for item in options.classifications}
    result: list[RobinhoodFaManualWork] = []
    blockers: list[RobinhoodFaBlocker] = []
    for account_index in sorted(active_accounts):
        classification = classifications.get(account_index)
        if classification is None:
            blockers.append(
                RobinhoodFaBlocker(
                    kind=RobinhoodFaBlockerKind.OPTION_CLASSIFICATION,
                    account_indices=(account_index,),
                )
            )
            result.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.OPTION_OTHER_TABLE_REVIEW,
                    account_indices=(account_index,),
                )
            )
            continue
        if classification.schedule_fa_disposition is RobinhoodOptionFaDisposition.TABLE_D:
            result.append(
                RobinhoodFaManualWork(
                    kind=RobinhoodFaManualKind.OPTION_TABLE_D,
                    account_indices=(classification.account_index,),
                )
            )
    return tuple(result), tuple(blockers)


def option_classification_blockers(
    options: RobinhoodOptionReconstruction, calendar_year: int
) -> tuple[RobinhoodFaBlocker, ...]:
    """The ``OPTION_CLASSIFICATION`` blockers :func:`compute_robinhood_fa`
    finds for ``options``, for each account with no option blocker. They need
    no prices, so the pipelines refuse on them before fetching any (#1567).
    An account's option blockers stand in for its line: the A2 peak refuses
    them before the compute reads a classification, and the line's "none in
    the financial year" can be false for it (#1567, #1574)."""
    blocked = {item.account_index for item in options.blockers}
    return tuple(
        item
        for item in _option_manual_work(options, calendar_year)[1]
        if blocked.isdisjoint(item.account_indices)
    )


def _dedupe_blockers(
    blockers: tuple[RobinhoodFaBlocker, ...],
) -> tuple[RobinhoodFaBlocker, ...]:
    return tuple(
        sorted(
            set(blockers),
            key=lambda item: (
                item.kind.value,
                item.account_indices,
                item.security_config_indices,
                item.lot_ids,
            ),
        )
    )


def _dedupe_manual(
    items: tuple[RobinhoodFaManualWork, ...],
) -> tuple[RobinhoodFaManualWork, ...]:
    identity: dict[
        tuple[tuple[int, ...], tuple[int, ...]],
        tuple[set[RobinhoodSecurityEventReference], set[RobinhoodA3IdentityGapHolding]],
    ] = {}
    deduped = {item for item in items if item.kind is not RobinhoodFaManualKind.A3_IDENTITY}
    for item in items:
        if item.kind is not RobinhoodFaManualKind.A3_IDENTITY:
            continue
        lots, gaps = identity.setdefault(
            (item.account_indices, item.security_config_indices), (set(), set())
        )
        lots.update(item.lot_ids)
        gaps.update(item.identity_gaps)
    deduped.update(
        RobinhoodFaManualWork(
            kind=RobinhoodFaManualKind.A3_IDENTITY,
            account_indices=account_indices,
            security_config_indices=security_indices,
            lot_ids=tuple(sorted(lot_ids)),
            identity_gaps=frozenset(gaps),
        )
        for (account_indices, security_indices), (lot_ids, gaps) in identity.items()
    )
    return tuple(
        sorted(
            deduped,
            key=lambda item: (
                item.kind.value,
                item.account_indices,
                item.security_config_indices,
                item.lot_ids,
            ),
        )
    )


def robinhood_fa_pre_price_blockers(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    income: RobinhoodIncomeResult,
    options: RobinhoodOptionReconstruction,
    calendar_year: int,
    fx_policy: FxPolicy,
    quantities: RobinhoodSecurityQuantityResult,
) -> tuple[RobinhoodFaBlocker, ...]:
    """The blockers :func:`compute_robinhood_fa` decides without prices, so
    the pipelines refuse on them before fetching any (#1574): every kind but
    an ``A3_CLOSING`` whose 31 December close is missing or disagrees, and
    :func:`option_classification_blockers`.

    The steps are that compute's own, run in its order, so the two agree:
    with no option blocker its blockers are these plus any such closing
    ones. First the A2 peak's account window refuses what no evidence clears
    (:func:`robinhood_a2_peak_window`), as that compute does before any
    blocker. ``fx_policy`` converts the credits the attribution step splits.
    Each refusal here is one that compute raises too, unless a step that
    reads a price refuses there first.
    """
    with localcontext(_ARITHMETIC_CONTEXT):
        config_accounts = _config_accounts(paired, config)
        for configured in config_accounts.values():
            robinhood_a2_peak_window(configured, calendar_year)
        coverage_blockers = _coverage_blockers(paired, config_accounts, calendar_year)
        identities = resolve_robinhood_security_identities(paired, securities.securities)
        a2_blockers = _a2_closing_blockers(paired, calendar_year)
        sources = _a3_sources(paired, securities.securities, identities, quantities, calendar_year)
        closing_blockers = _unshown_open_lots(sources.fragments, sources.positions)
        credit_blockers = _allocate_credits(
            sources.fragments,
            sources.fragment_blockers,
            income,
            paired,
            securities.securities,
            sources.lots,
            calendar_year,
            fx_policy,
        )[4]
        return _dedupe_blockers(
            (
                *coverage_blockers,
                *a2_blockers,
                *sources.blockers,
                *closing_blockers,
                *credit_blockers,
                *option_classification_blockers(options, calendar_year),
            )
        )


def compute_robinhood_fa(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    income: RobinhoodIncomeResult,
    options: RobinhoodOptionReconstruction,
    calendar_year: int,
    fx_policy: FxPolicy,
    prices_by_ticker: Mapping[Ticker, PriceSeries] | None = None,
    option_prices_by_contract: Mapping[
        RobinhoodOptionContract,
        MassiveOptionCloseSeries,
    ]
    | None = None,
    quantities: RobinhoodSecurityQuantityResult | None = None,
    entity_overrides: Mapping[Isin, FaEntityOverride] | None = None,
    nature_by_isin: Mapping[Isin, str] | None = None,
    peak_basis: PeakBasis = "close",
    peak_currency: PeakCurrency = "native",
) -> RobinhoodFaResult:
    """Build A2 nature rows for two accounts and transfer-safe A3 fragments."""

    if fx_policy.currency != "USD":
        raise _fail("Robinhood Schedule FA requires a USD exact-date FX policy")
    with localcontext(_ARITHMETIC_CONTEXT):
        config_accounts = _config_accounts(paired, config)
        coverage_blockers = _coverage_blockers(
            paired,
            config_accounts,
            calendar_year,
        )
        program_bank = _program_bank_evidence(paired, income, calendar_year)
        identities = resolve_robinhood_security_identities(paired, securities.securities)
        if quantities is None:
            quantities = resolve_robinhood_security_quantities(
                paired,
                identities,
                config.security_receipt_resolutions,
            )
        a2_peaks = compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=securities,
            quantities=quantities,
            options=options,
            equity_prices=prices_by_ticker or {},
            option_prices=option_prices_by_contract or {},
            calendar_year=calendar_year,
            fx_policy=fx_policy,
            peak_currency=peak_currency,
        )
        a2, a2_blockers, a2_manual = _build_a2(
            paired,
            config_accounts,
            income,
            options,
            {item.account_index: item for item in a2_peaks},
            program_bank,
            calendar_year,
            fx_policy,
            peak_currency,
        )
        sources = _a3_sources(paired, securities.securities, identities, quantities, calendar_year)
        temporary_lots, closing_blockers = _temporary_lots(
            paired,
            securities.securities,
            sources.fragments,
            sources.positions,
            prices_by_ticker,
            calendar_year,
        )
        a3, credit_blockers, a3_manual = _build_a3(
            paired,
            securities.securities,
            income,
            sources.fragments,
            sources.fragment_blockers,
            sources.lots,
            temporary_lots,
            prices_by_ticker,
            entity_overrides or {},
            nature_by_isin or {},
            calendar_year,
            fx_policy,
            peak_basis,
            peak_currency,
        )
        option_manual, option_blockers = _option_manual_work(
            options,
            calendar_year,
        )
        blockers = _dedupe_blockers(
            (
                *coverage_blockers,
                *a2_blockers,
                *sources.blockers,
                *closing_blockers,
                *credit_blockers,
                *option_blockers,
            )
        )
        manual = _dedupe_manual(
            (
                *a2_manual,
                *a3_manual,
                *option_manual,
            )
        )
        cbdt_eligible = (
            not blockers
            and not manual
            and len({item.account_index for item in a2}) == _ACCOUNT_COUNT
            and len(a3) == len(sources.fragments)
        )
        return RobinhoodFaResult(
            a2=a2,
            a3=a3,
            blockers=blockers,
            manual_work=manual,
            program_bank=program_bank,
            cbdt_json_eligible=cbdt_eligible,
        )


# ---- Refusal lines (#1550) ----------------------------------------------------

_REFERENCES = "Internal references (not account numbers): "
_NOT_WRITTEN = "Robinhood Schedule FA isn't written while this stands"
_BY_HAND = f"{_NOT_WRITTEN}, so complete the whole Robinhood Schedule FA by hand"
_STATEMENT_ROUTE = (
    f"{_NOT_WRITTEN}: supply Robinhood's statements for the periods ending on 31 December "
    "and starting on 1 January in place of one that spans both and re-run, or complete the "
    "whole Robinhood Schedule FA by hand."
)
_REPORT_ROUTE = (
    f"{_NOT_WRITTEN}: if the activity report misses a trade or transfer of that security, "
    "supply the complete report and re-run; otherwise complete the whole Robinhood "
    "Schedule FA by hand."
)
# Each explained kind's cause and route. A3_IDENTITY prints the identity
# guidance instead; A3_CREDIT_ATTRIBUTION and A3_LOTS have their own texts.
# A3_INVENTORY and A3_DISPOSAL have none: fa_prices refuses every inventory
# blocker before this compute runs, and every disposal slice the lot engine
# makes is one account's same-day sales of one security.
_FA_TEXT: Final[dict[RobinhoodFaBlockerKind, tuple[str, str]]] = {
    RobinhoodFaBlockerKind.A2_CLOSING: (
        "Table A2's closing balance comes from each account's statement ending on 31 "
        "December, and no statement of these accounts ends on that day.",
        _STATEMENT_ROUTE,
    ),
    RobinhoodFaBlockerKind.A3_CLOSING: (
        "Table A3's closing balance of these lots comes from the statement ending on 31 "
        "December, and it can't be used: that statement shows none of the security's shares "
        "or a different number than the lots hold (as when every share was bought on 31 "
        "December and settles in January), or the security's closing price for 31 December "
        "is missing from its price series or disagrees with the statement's total.",
        f"{_BY_HAND}.",
    ),
    RobinhoodFaBlockerKind.EVIDENCE_COVERAGE: (
        "Schedule FA needs each account's activity report and statements to cover the "
        "calendar year from the account's opening date, with one statement ending on 31 "
        "December, and these accounts' evidence doesn't.",
        _STATEMENT_ROUTE,
    ),
    RobinhoodFaBlockerKind.OPTION_CLASSIFICATION: (
        "These accounts traded or held options in the calendar year but none in the "
        "financial year, so the run has no [[robinhood.option_classifications]] treatment "
        "saying whether they belong in Table D or only in Table A2, and no entry for this "
        "financial year gives one.",
        f"{_BY_HAND}, reporting those options under the treatment of the financial year "
        "they were traded in.",
    ),
}
# Keyed by ``income_blocked``.
_CREDIT_TEXT: Final[dict[bool, tuple[str, str]]] = {
    True: (
        "Table A3's dividend and Stock Lending credits come from Robinhood's source income "
        "for the financial year, which has unresolved Schedule OS blockers; robinhood-income "
        "names each one.",
        f"{_NOT_WRITTEN}: clear them and re-run, or complete the whole Robinhood Schedule "
        "FA by hand.",
    ),
    False: (
        "A dividend or Stock Lending credit in the calendar year can't be split among these "
        "lots: on its holding date the account that received it held none of them, which "
        "account held a moved lot isn't known, or the credit's share count differs from "
        "the shares the lots held there.",
        f"{_BY_HAND}.",
    ),
}
_INVENTORY_TEXT: Final[dict[RobinhoodInventoryBlockerKind, tuple[str, str]]] = {
    RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH: (
        "A monthly statement shows a different number of shares of a security than the "
        "activity report's trades and transfers leave in the account by the statement's "
        "last day, so the shares held can't be proved.",
        _REPORT_ROUTE,
    ),
    RobinhoodInventoryBlockerKind.NEGATIVE_INVENTORY: (
        "The activity report takes more shares of a security out of an account than its "
        "earlier trades and transfers put in, so the shares held can't be proved.",
        _REPORT_ROUTE,
    ),
    RobinhoodInventoryBlockerKind.POSITION_IDENTITY: (
        "A monthly statement shows a position whose symbol or CUSIP matches no security in "
        "the activity report, so its shares can't be tied to one security.",
        _REPORT_ROUTE,
    ),
    RobinhoodInventoryBlockerKind.SNAPSHOT_BASIS: (
        "The trade-date or settlement-date basis of a monthly statement's share count "
        "can't be proved from the activity report and its Pending Trades, so the shares "
        "held can't be proved.",
        f"{_BY_HAND}.",
    ),
}


def _rows(references: Iterable[RobinhoodSecurityEventReference]) -> str:
    return ", ".join(f"{item.account_index}:{item.event_ordinal}" for item in references)


def _statement_scope(
    account_index: int | None,
    statement_index: int,
    positions: tuple[RobinhoodPositionReference, ...],
) -> str:
    """A statement by its account and 1-based place in the account's series,
    and its position rows, as the option refusals name them (#1134)."""
    where = f"account_index {account_index} statement {statement_index + 1}"
    rows = ", ".join(f"page {item.location.page} row {item.location.row}" for item in positions)
    return f"{where} ({rows})" if rows else where


def _with_lots(scope: str, lot_ids: tuple[RobinhoodSecurityEventReference, ...]) -> str:
    if not lot_ids:
        return scope
    lots = f"lots {_rows(lot_ids)}"
    return f"{scope} ({lots})" if scope else lots


def _blocker_scopes(blockers: list[RobinhoodFaBlocker]) -> list[str]:
    """Each set of accounts and configured securities (1-based, as
    :func:`_share_error` names them) among ``blockers``, with every lot they name."""
    lots: dict[tuple[tuple[int, ...], tuple[int, ...]], set[RobinhoodSecurityEventReference]] = {}
    for blocker in blockers:
        key = (blocker.account_indices, blocker.security_config_indices)
        lots.setdefault(key, set()).update(blocker.lot_ids)
    scopes = []
    for (accounts, securities), lot_ids in lots.items():
        parts = []
        if accounts:
            parts.append(f"account_index {', '.join(map(str, accounts))}")
        if securities:
            parts.append(f"Robinhood security {', '.join(str(index + 1) for index in securities)}")
        scopes.append(_with_lots(", ".join(parts), tuple(sorted(lot_ids))))
    return scopes


def _source_scope(blocker: RobinhoodLotBlocker | RobinhoodInventoryBlocker) -> str:
    """A lot-engine or inventory blocker's activity rows, statement and lots."""
    parts = []
    if blocker.event_references:
        parts.append(f"activity rows {_rows(blocker.event_references)}")
    if blocker.statement_index is not None:
        parts.append(
            _statement_scope(
                blocker.account_index, blocker.statement_index, blocker.position_references
            )
        )
    scope = ", ".join(parts)
    return _with_lots(scope, blocker.lot_ids) if isinstance(blocker, RobinhoodLotBlocker) else scope


def _line(cause: str, scopes: Iterable[str], route: str) -> str:
    named = "; ".join(scope for scope in scopes if scope)
    return f"{cause} {_REFERENCES}{named}. {route}" if named else f"{cause} {route}"


def _lot_reasons(blockers: list[RobinhoodFaBlocker]) -> list[str]:
    """One line per lot-engine cause the ``A3_LOTS`` blockers carry, with
    #1546's cause text and Schedule FA's route."""
    lot_blockers = [item for blocker in blockers for item in blocker.source_lot_blockers]
    reasons = []
    for kind, cause in LOT_BLOCKER_CAUSES.items():
        causes = [item for item in lot_blockers if item.kind is kind]
        if causes:
            route = (
                f"{SAME_DAY_EVIDENCE} {_BY_HAND}, matching the lots first in, first out, "
                "in the order the trades and transfers ran."
                if kind is RobinhoodLotBlockerKind.SAME_DAY_AMBIGUITY
                else f"{_BY_HAND}."
            )
            reasons.append(
                _line(cause.format(moved="sales and transfers"), map(_source_scope, causes), route)
            )
    return reasons


def fa_blocker_reasons(blockers: Iterable[RobinhoodFaBlocker]) -> tuple[str, ...]:
    """The refusal lines for ``blockers``, in kind order: one per explained
    kind present, two for ``A3_CREDIT_ATTRIBUTION`` when both its causes
    stand, and one per lot-engine cause for ``A3_LOTS`` (#1550).

    Each names its accounts, configured securities, lots or source rows by
    internal reference, the cause and a route. ``A3_IDENTITY`` has none here:
    the pipeline adds its guidance line.
    """
    items = tuple(blockers)
    reasons: list[str] = []
    for kind in RobinhoodFaBlockerKind:
        of_kind = [blocker for blocker in items if blocker.kind is kind]
        if not of_kind:
            continue
        if kind is RobinhoodFaBlockerKind.A3_LOTS:
            reasons.extend(_lot_reasons(of_kind))
        elif kind is RobinhoodFaBlockerKind.A3_CREDIT_ATTRIBUTION:
            for income_blocked, (cause, route) in _CREDIT_TEXT.items():
                group = [item for item in of_kind if item.income_blocked is income_blocked]
                if group:
                    reasons.append(_line(cause, _blocker_scopes(group), route))
        elif kind in _FA_TEXT:
            cause, route = _FA_TEXT[kind]
            reasons.append(_line(cause, _blocker_scopes(of_kind), route))
    return tuple(reasons)


def inventory_blocker_reasons(
    blockers: Iterable[RobinhoodInventoryBlocker],
) -> tuple[str, ...]:
    """One refusal line per inventory blocker kind present, in kind order,
    naming its source rows or statement positions, the cause and a route
    (#1550). ``SECURITY_RECEIPT`` has none: an unresolved REC receipt refuses in
    resolve_robinhood_security_quantities, and a resolved one leaves no blocker.
    """
    items = tuple(blockers)
    reasons = []
    for kind, (cause, route) in _INVENTORY_TEXT.items():
        of_kind = [item for item in items if item.kind is kind]
        if of_kind:
            reasons.append(_line(cause, map(_source_scope, of_kind), route))
    return tuple(reasons)
