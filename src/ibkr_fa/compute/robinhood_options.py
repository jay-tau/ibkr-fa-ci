"""Reconstruct reconciled Robinhood long-option activity and tax evidence.

Option quantities are whole contracts, exact by definition: an activity trade
or statement position whose quantity isn't a whole number gets an
``UNSUPPORTED_CONTRACT`` blocker of its own (#1134). Only a displayed price and
a cash amount carry display rounding, which the premium band allows for.

A draw's share of its purchase or sale cash is the exact rational
``cash * drawn / quantity`` (#1245). The FY totals and INR conversions keep
those rationals until filing. Gross profit and unfavourable differences file
their exact HALF_UP; the no-books filing derives its other fields from those
whole rupees and the rounded reviewed config amounts. FSI uses that BP income
when a filing is available, otherwise the exact net P&L. This replaces #1148's
cumulative rounding of each share to a millionth of a dollar. A Decimal amount
shows its rational (:func:`_shown`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import date, timedelta
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum
from fractions import Fraction
from functools import partial
from itertools import accumulate
from operator import add, mul, sub
from types import TracebackType
from typing import Final, Literal

from ibkr_fa.compute.exact_inr import (
    exact_decimal,
    exact_fraction,
    exact_share,
    exact_whole_rupees,
)
from ibkr_fa.compute.lot_matching import FifoPool
from ibkr_fa.compute.robinhood_inputs import (
    holds_two_accounts,
    is_one_financial_year,
    same_distinct_account_numbers,
)
from ibkr_fa.compute.schedule_cg_table_f import (
    ScheduleCgTableFRow,
    TableFGain,
    schedule_cg_table_f_row,
)
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    compute_schedule_fsi_from_business_income,
)
from ibkr_fa.config import (
    RobinhoodConfig,
    RobinhoodOptionTreatment,
    RobinhoodOptionTurnoverBasis,
)
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models._row_checks import require_filed_amounts
from ibkr_fa.models.money import within_filed_bound
from ibkr_fa.models.schedule_cg_ltcg_row import NEW_REGIME_CUTOVER_DATE
from ibkr_fa.models.schedule_cg_row import is_short_term_holding
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import robinhood_half_ulp
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodPositionReference,
)
from ibkr_fa.normalize.robinhood.security import (
    RobinhoodSecurityEventReference,
)
from ibkr_fa.parser.robinhood import (
    ROBINHOOD_OPTION_TRADE_KINDS,
    RobinhoodEvent,
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementPosition
from ibkr_fa.types import STANDARD_OPTION_MULTIPLIER

_ARITHMETIC_CONTEXT = Context(prec=60)
# The same 60 digits with Inexact trapped (#1148): see _Exact.
_EXACT_CONTEXT = Context(prec=60, traps=[DivisionByZero, InvalidOperation, Overflow, Inexact])
# The same 60 digits with nothing trapped: see _shown.
_SHOWN_CONTEXT = Context(prec=60, traps=[])
_WHOLE_RUPEE = Decimal("1")
_SUPPORTED_OPTION_BUSINESS_FINANCIAL_YEAR = 2025
ROBINHOOD_FNO_FEMA_WARNING: Final[str] = (
    "FEMA/LRS F&O warning: RBI's LRS Master Direction says remittances in the "
    "nature of margins or margin calls to overseas exchanges or counterparties "
    "are not allowed under LRS. ibkr-fa does not determine whether these trades "
    "were LRS-funded or otherwise FEMA-compliant; fema_risk_acknowledged = true "
    "enables tax reporting only after independent review."
)
_SUPPORTED_TREATMENT_SCOPE: Final[str] = (
    "This warning covers only supported long-option BTO-to-STC contracts closed "
    "without delivery; it makes no determination for puts, shorts, expiry, exercise, "
    "assignment, hedges, adjusted contracts, or delivery-settled lifecycles."
)


class RobinhoodOptionsError(IbkrFaError):
    """Reconciled Robinhood inputs violate the option-adapter contract."""


class RobinhoodOptionClassificationError(RobinhoodOptionsError):
    """Option activity has no explicit account/FY tax treatment."""


class RobinhoodOptionBlockerKind(StrEnum):
    """One unresolved fact preventing complete option tax output."""

    NEGATIVE_INVENTORY = "negative-inventory"
    OPEN_BUSINESS_POSITION = "open-business-position"
    ORDERING_AMBIGUITY = "ordering-ambiguity"
    POSITION_RECONCILIATION = "position-reconciliation"
    PRE_CUTOVER_HOLDING_RULE = "pre-cutover-holding-rule"
    PREMIUM_RECONCILIATION = "premium-reconciliation"
    UNSUPPORTED_CONTRACT = "unsupported-contract"
    UNSUPPORTED_LIFECYCLE = "unsupported-lifecycle"


_BLOCKER_MESSAGES: Final[dict[RobinhoodOptionBlockerKind, str]] = {
    RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY: (
        "an option contract is sold to close more than the activity report shows bought "
        "to open, so check that the report starts before the opening trades"
    ),
    RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION: (
        "an option position treated as business income is still open at the end of the "
        "financial year, and its closing stock isn't valued, so complete it by hand"
    ),
    RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY: (
        "an option contract has same-day trades whose order isn't known (opens and closes "
        "on one day, or fills at different prices), so match its lots by hand"
    ),
    RobinhoodOptionBlockerKind.POSITION_RECONCILIATION: (
        "an option position on a monthly statement doesn't match the quantity the activity "
        "report's trades give, so check that the report and the statements agree"
    ),
    RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE: (
        "an option position treated as capital gains was closed before "
        f"{NEW_REGIME_CUTOVER_DATE.isoformat()}, and gains under the earlier holding-period "
        "rules aren't computed, so compute them by hand"
    ),
    RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION: (
        "an option trade's amount doesn't match its quantity times price times 100, so "
        "check that trade in the activity report"
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT: (
        "an option contract or statement position isn't a long call with a positive whole "
        "number of contracts and a market value of zero or more, so check its right, "
        "quantity and market value, and report an unsupported contract by hand"
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE: (
        "an option contract was still held at expiry or has activity after it, and "
        "expiry, exercise and assignment aren't computed, so report it by hand"
    ),
}


def option_blocker_message(kind: RobinhoodOptionBlockerKind) -> str:
    """What one blocker kind stops, and what the user must do or check (#802)."""
    require(
        isinstance(kind, RobinhoodOptionBlockerKind) and kind in _BLOCKER_MESSAGES,
        what="Robinhood option blocker kind has no message",
        kind=kind,
    )
    return _BLOCKER_MESSAGES[kind]


# The kind with no single row prints a fixed account-wide scope (#1134).
_ACCOUNT_SCOPES: Final[dict[RobinhoodOptionBlockerKind, str]] = {
    RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION: (
        "option positions open at the financial year's end"
    ),
}


def _column_labels(columns: Sequence[str]) -> str:
    if len(columns) == 1:
        return f"column {columns[0]}"
    return f"columns {', '.join(columns[:-1])} and {columns[-1]}"


def _blocker_location(blocker: RobinhoodOptionBlocker) -> str:
    """Where ``blocker`` is: its row, section and fixed columns, or its kind's
    account-wide scope. Names no identifier, symbol, amount or input date."""
    account = f"first at account {blocker.account_index + 1}, "
    located = blocker.located_at
    scope = _ACCOUNT_SCOPES.get(blocker.kind)
    if scope is not None:
        require(
            located is None
            and not blocker.columns
            and blocker.missing_from_statement_index is None,
            what="account-wide Robinhood option blocker names a row",
            kind=blocker.kind,
        )
        return account + scope
    require(
        located is not None
        and bool(blocker.columns)
        and located.account_index == blocker.account_index,
        what="Robinhood option blocker has no row in its account",
        kind=blocker.kind,
    )
    assert located is not None
    if isinstance(located, RobinhoodPositionReference):
        where = (
            f"{located.location.section.value}: statement {located.statement_index + 1} "
            f"page {located.location.page} row {located.location.row}"
        )
    else:
        where = f"activity report row {located.event_ordinal}"
    missing = blocker.missing_from_statement_index
    return f"{account}{where}; {_column_labels(blocker.columns)}" + (
        "" if missing is None else f" (no matching position on statement {missing + 1})"
    )


def option_blocker_summary(blockers: Iterable[RobinhoodOptionBlocker]) -> str:
    """Each present kind's message with its blocker count and where its first
    blocker is, in enum order (#802, #1134)."""
    items = tuple(blockers)
    messages = {item.kind: option_blocker_message(item.kind) for item in items}
    counts = Counter(item.kind for item in items)
    first: dict[RobinhoodOptionBlockerKind, str] = {}
    for item in items:
        first.setdefault(item.kind, _blocker_location(item))
    return "; ".join(
        f"{messages[kind]} ({counts[kind]} blocker{'' if counts[kind] == 1 else 's'}; "
        f"{first[kind]})"
        for kind in RobinhoodOptionBlockerKind
        if kind in counts
    )


class RobinhoodOptionCapitalGainBucket(StrEnum):
    """Semantic term bucket for a reviewed option capital election."""

    STCG_OTHER_ASSETS = "stcg-other-assets"
    LTCG_OTHER_ASSETS = "ltcg-other-assets"

    @property
    def table_f_row(self) -> int:
        """The Schedule CG Table F row the bucket's gains go in (#1362): row 3,
        short-term gains at applicable rates, or row 5, long-term gains at 12.5%."""
        return 3 if self is RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS else 5


class RobinhoodOptionLossBucket(StrEnum):
    """Manual Schedule CFL bucket selected by the reviewed treatment."""

    SHORT_TERM_CAPITAL = "short-term-capital-loss"
    LONG_TERM_CAPITAL = "long-term-capital-loss"
    SPECULATIVE_BUSINESS = "speculative-business-loss"
    NON_SPECULATIVE_BUSINESS = "non-speculative-business-loss"


class RobinhoodOptionFaDisposition(StrEnum):
    """Schedule FA destination determined by the reviewed treatment."""

    TABLE_D = "table-d-other-capital-asset"
    A2_ONLY = "a2-custodial-account-only"


@dataclass(frozen=True, slots=True, repr=False, order=True)
class RobinhoodOptionContract:
    """Account-local option identity carried by both activity and statements."""

    account_index: int
    symbol: str
    expiration: date
    right: str
    strike_usd: Decimal
    multiplier: Decimal = STANDARD_OPTION_MULTIPLIER


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionTradeEvidence:
    """One exact activity event plus its displayed-premium uncertainty."""

    contract: RobinhoodOptionContract
    event_reference: RobinhoodSecurityEventReference
    kind: RobinhoodEventKind
    activity_date: date
    process_date: date
    settle_date: date
    quantity: Decimal
    displayed_price_usd: Decimal
    exact_cash_usd: Decimal
    displayed_gross_mid_usd: Decimal
    displayed_gross_low_usd: Decimal
    displayed_gross_high_usd: Decimal
    cash_minus_displayed_mid_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionValuationAnchor:
    """One statement position already included in the account's reported NAV."""

    contract: RobinhoodOptionContract
    position_reference: RobinhoodPositionReference
    statement_date: date
    quantity: Decimal
    displayed_price_usd: Decimal
    market_value_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionLotSlice:
    """One FIFO-matched acquisition/disposal slice using exact source cash.

    ``exact_cost_usd`` and ``exact_net_proceeds_usd`` are its exact shares of
    its purchase and sale cash (#1245); the Decimal amounts show them.
    """

    contract: RobinhoodOptionContract
    open_references: tuple[RobinhoodSecurityEventReference, ...]
    close_references: tuple[RobinhoodSecurityEventReference, ...]
    open_date: date
    close_date: date
    quantity: Decimal
    cost_usd: Decimal
    net_proceeds_usd: Decimal
    net_pnl_usd: Decimal
    in_financial_year: bool
    exact_cost_usd: Fraction
    exact_net_proceeds_usd: Fraction

    @property
    def exact_net_pnl_usd(self) -> Fraction:
        return self.exact_net_proceeds_usd - self.exact_cost_usd


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionOpenLot:
    """One acquisition pool still open at the end of supplied activity."""

    contract: RobinhoodOptionContract
    open_references: tuple[RobinhoodSecurityEventReference, ...]
    open_date: date
    remaining_quantity: Decimal
    remaining_cost_usd: Decimal
    exact_remaining_cost_usd: Fraction


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionClassificationEvidence:
    """Runtime-visible reviewed decision and its filing consequences."""

    account_index: int
    financial_year: int
    treatment: RobinhoodOptionTreatment
    source: str
    reason: str
    schedule_fa_disposition: RobinhoodOptionFaDisposition
    books_maintained: bool | None = None
    other_speculative_business: bool | None = None
    nature_of_business_code: str | None = None
    turnover_basis: RobinhoodOptionTurnoverBasis | None = None
    external_expenses_inr: Decimal | None = None
    bp_additions_inr: Decimal | None = None
    bp_deductions_inr: Decimal | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionCapitalGainLine:
    """Exact manual Schedule CG evidence without a fabricated option ISIN.

    ``exact_net_gain_inr`` is the slice's exact net P&L at ``fx_rate``, which
    ``net_gain_inr`` shows (#1245) and Table F splits (#1362).
    """

    source_slice: RobinhoodOptionLotSlice
    bucket: RobinhoodOptionCapitalGainBucket
    loss_bucket: RobinhoodOptionLossBucket | None
    fx_rate: Decimal
    fx_specified_date: date
    fx_rate_date: date
    cost_inr: Decimal
    net_proceeds_inr: Decimal
    net_gain_inr: Decimal
    exact_net_gain_inr: Fraction


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionBusinessTotals:
    """One business-treated account's exact FY totals in USD, before any FX (#1145).

    ``slices`` are the in-FY slices the totals add up, without unsupported
    contracts. The Decimal totals show the exact ones (#1245).
    """

    account_index: int
    treatment: RobinhoodOptionTreatment
    slices: tuple[RobinhoodOptionLotSlice, ...]
    matched_premium_paid_usd: Decimal
    matched_premium_received_usd: Decimal
    net_pnl_usd: Decimal
    filing_complete: bool

    @property
    def exact_matched_premium_paid_usd(self) -> Fraction:
        return sum((item.exact_cost_usd for item in self.slices), Fraction())

    @property
    def exact_matched_premium_received_usd(self) -> Fraction:
        return sum((item.exact_net_proceeds_usd for item in self.slices), Fraction())


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionBusinessSummary:
    """Exact realized option P&L feeding reviewed ITR-3 business schedules.

    ``exact_net_pnl_inr`` is the exact net P&L in INR; the Decimal amounts
    show their exact values (#1245).
    """

    account_index: int
    treatment: RobinhoodOptionTreatment
    matched_premium_paid_usd: Decimal
    matched_premium_received_usd: Decimal
    net_pnl_usd: Decimal
    fx_rate: Decimal
    fx_specified_date: date
    fx_rate_date: date
    matched_premium_paid_inr: Decimal
    matched_premium_received_inr: Decimal
    net_pnl_inr: Decimal
    exact_net_pnl_inr: Fraction
    loss_bucket: RobinhoodOptionLossBucket | None
    schedule_bp_bucket: Literal[
        "speculative-business",
        "business-other-than-speculative",
    ]
    filing_complete: bool = True


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionBusinessFiling:
    """Complete no-books speculative-business fields for one financial year."""

    financial_year: int
    turnover_basis: RobinhoodOptionTurnoverBasis
    turnover_inr_full_precision: Decimal
    gross_profit_inr_full_precision: Decimal
    expenditure_inr_full_precision: Decimal
    net_income_inr_full_precision: Decimal
    turnover_inr: Decimal
    gross_profit_inr: Decimal
    expenditure_inr: Decimal
    net_income_inr: Decimal
    bp_additions_inr: Decimal
    bp_deductions_inr: Decimal
    bp_income_inr: Decimal
    cfl_loss_inr: Decimal
    event_count: int
    nature_of_business_code: str
    books_maintained: Literal[False] = False

    def __post_init__(self) -> None:
        # The eight whole-rupee amounts the options CSV files as ITR-3 fields (#830).
        require_filed_amounts(
            self,
            "turnover_inr",
            "gross_profit_inr",
            "expenditure_inr",
            "net_income_inr",
            "bp_additions_inr",
            "bp_deductions_inr",
            "bp_income_inr",
            "cfl_loss_inr",
        )


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionBlocker:
    """One privacy-safe blocker with resolvable source handles.

    ``located_at`` is the row its summary names: the first row of the first
    failing group or row. ``columns`` are the fixed source column labels its
    check compares. A position missing from a statement is located at its
    first contributing trade, and ``missing_from_statement_index`` names that
    statement. The account-wide kinds have none of these (#1134).
    """

    kind: RobinhoodOptionBlockerKind
    account_index: int
    contracts: tuple[RobinhoodOptionContract, ...] = ()
    event_references: tuple[RobinhoodSecurityEventReference, ...] = ()
    position_references: tuple[RobinhoodPositionReference, ...] = ()
    located_at: RobinhoodSecurityEventReference | RobinhoodPositionReference | None = None
    columns: tuple[str, ...] = ()
    missing_from_statement_index: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionReconstruction:
    """Reconstructed option activity, every blocker and the FY tax evidence in
    USD: everything Schedule FA reads, with no FX (#1145).

    ``capital_gain_slices`` are the in-FY capital-gains slices the INR step
    files; ``business_totals`` has one entry per business-treated account.
    """

    financial_year_end: date
    trades: tuple[RobinhoodOptionTradeEvidence, ...]
    lot_slices: tuple[RobinhoodOptionLotSlice, ...]
    open_lots: tuple[RobinhoodOptionOpenLot, ...]
    valuation_anchors: tuple[RobinhoodOptionValuationAnchor, ...]
    classifications: tuple[RobinhoodOptionClassificationEvidence, ...]
    capital_gain_slices: tuple[RobinhoodOptionLotSlice, ...]
    business_totals: tuple[RobinhoodOptionBusinessTotals, ...]
    blockers: tuple[RobinhoodOptionBlocker, ...]
    excluded_out_of_fy_close_references: tuple[
        tuple[RobinhoodSecurityEventReference, ...],
        ...,
    ]
    a2_gross_credit_references: tuple[RobinhoodSecurityEventReference, ...]

    @property
    def required_itr_form(self) -> Literal["ITR-3"] | None:
        """ITR-3 when a reviewed classification treats options as business income."""
        return (
            "ITR-3"
            if any(
                item.treatment is not RobinhoodOptionTreatment.CAPITAL_GAINS
                for item in self.classifications
            )
            else None
        )

    @property
    def validated_valuation_anchors(self) -> tuple[RobinhoodOptionValuationAnchor, ...]:
        invalid = {
            RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
            RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
            RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
            RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION,
            RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
            RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
        }
        if any(blocker.kind in invalid for blocker in self.blockers):
            return ()
        return self.valuation_anchors


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodOptionsResult(RobinhoodOptionReconstruction):
    """The reconstruction plus its reviewed filing outputs in INR."""

    capital_gain_lines: tuple[RobinhoodOptionCapitalGainLine, ...]
    business_summaries: tuple[RobinhoodOptionBusinessSummary, ...]

    @property
    def manual_work_required(self) -> bool:
        return bool(
            self.blockers
            or self.capital_gain_lines
            or (self.business_summaries and compute_robinhood_option_business_filing(self) is None)
        )

    @property
    def business_fsi_manual_work_required(self) -> bool:
        business_buckets = {summary.schedule_bp_bucket for summary in self.business_summaries}
        return bool(self.business_summaries) and (
            len(business_buckets) != 1
            or any(not summary.filing_complete for summary in self.business_summaries)
        )

    @property
    def fsi_manual_work_required(self) -> bool:
        return bool(
            self.blockers or self.capital_gain_lines or self.business_fsi_manual_work_required
        )

    @property
    def table_f(self) -> dict[RobinhoodOptionCapitalGainBucket, ScheduleCgTableFRow]:
        """The Schedule CG Table F row of each bucket with capital-gain lines, in
        bucket order, computed on access (#1362).

        Each row splits the bucket's exact gains by close date
        (:func:`~ibkr_fa.compute.schedule_cg_table_f.schedule_cg_table_f_row`)
        to their exact total's HALF_UP whole rupees, the total the lines' gains
        add up to. A bucket whose total rounds past the CBDT 14-digit bound,
        which no return can file, has a blank ``MANUAL_SECTION`` row.
        """
        rows: dict[RobinhoodOptionCapitalGainBucket, ScheduleCgTableFRow] = {}
        for bucket in RobinhoodOptionCapitalGainBucket:
            lines = [line for line in self.capital_gain_lines if line.bucket is bucket]
            if not lines:
                continue
            total = exact_whole_rupees(sum((line.exact_net_gain_inr for line in lines), Fraction()))
            rows[bucket] = schedule_cg_table_f_row(
                (
                    TableFGain(
                        line.source_slice.close_date, line.net_gain_inr, line.exact_net_gain_inr
                    )
                    for line in lines
                ),
                result_inr=total if within_filed_bound(total) else None,
            )
        return rows


def option_treatment_warning(
    treatment: RobinhoodOptionTreatment,
    *,
    account_index: int,
) -> str | None:
    """Return the account-scoped Indian option-treatment advisory."""
    prefix = f"Robinhood option account index {account_index}: "
    if treatment is RobinhoodOptionTreatment.SPECULATIVE_BUSINESS:
        return (
            prefix + "the reviewed section 43(5) speculative-business treatment means any "
            "loss is ring-fenced under section 73, and carrying it forward requires "
            "a timely loss return under sections 80 and 139(3). " + _SUPPORTED_TREATMENT_SCOPE
        )
    if treatment is RobinhoodOptionTreatment.NON_SPECULATIVE_BUSINESS:
        return (
            prefix + "UNRESOLVED section 43(5)(d) basis: non-speculative-business treatment "
            "requires an eligible derivative transaction on a prescribed/notified "
            "recognised stock exchange. ibkr-fa has not established that a US exchange "
            "qualifies; retain independent statutory evidence or use speculative-business. "
            "If section 72 ordinary-business loss treatment is claimed, carrying the loss "
            "forward requires a timely return under sections 80 and 139(3). "
            + _SUPPORTED_TREATMENT_SCOPE
        )
    return None


def option_treatment_warnings(options: RobinhoodOptionReconstruction) -> tuple[str, ...]:
    """Return distinct account-scoped advisories for reviewed business treatments."""
    return tuple(
        dict.fromkeys(
            warning
            for item in options.classifications
            if (
                warning := option_treatment_warning(
                    item.treatment,
                    account_index=item.account_index,
                )
            )
            is not None
        )
    )


def option_classification_audit_notes(
    options: RobinhoodOptionsResult,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return safely labelled user inputs and treatment warnings for the workpaper."""
    notes: list[tuple[str, str, str, str]] = []
    for item in options.classifications:
        account_index = str(item.account_index)
        treatment = item.treatment.value
        notes.extend(
            (
                (
                    "classification-source",
                    account_index,
                    treatment,
                    "User-stated classification source; not a section 43(5)(d) "
                    f"determination: {item.source}",
                ),
                (
                    "classification-reason",
                    account_index,
                    treatment,
                    "User-stated classification reason; not a section 43(5)(d) "
                    f"determination: {item.reason}",
                ),
            )
        )
        warning = option_treatment_warning(item.treatment, account_index=item.account_index)
        if warning is not None:
            notes.append(("classification-warning", account_index, treatment, warning))
    return tuple(notes)


def compute_robinhood_option_business_fsi(
    options: RobinhoodOptionsResult,
) -> ScheduleFsiComputeResult:
    """Return one complete positive single-bucket option-business FSI contribution."""

    summaries = options.business_summaries
    if not summaries or options.business_fsi_manual_work_required:
        return ScheduleFsiComputeResult(rows=[])
    account_indices = {summary.account_index for summary in summaries}
    fy_slices = tuple(
        item
        for item in options.lot_slices
        if item.in_financial_year and item.contract.account_index in account_indices
    )
    filing = compute_robinhood_option_business_filing(options)
    if filing is not None:
        income_inr = filing.bp_income_inr
    else:
        income_inr = _shown(
            sum((summary.exact_net_pnl_inr for summary in summaries), Fraction()),
            _decimal_sum,
            *(summary.net_pnl_inr for summary in summaries),
        )
    return compute_schedule_fsi_from_business_income(
        income_inr,
        source_country_iso2="US",
        event_count=len(fy_slices),
    )


def compute_robinhood_option_business_filing(
    options: RobinhoodOptionsResult,
) -> RobinhoodOptionBusinessFiling | None:
    """Build exact no-books P&L/BP/CFL fields from reviewed speculative differences."""

    if len(options.business_summaries) != 1:
        return None
    summary = options.business_summaries[0]
    classification = next(
        (item for item in options.classifications if item.account_index == summary.account_index),
        None,
    )
    if (
        classification is None
        or summary.treatment is not RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
        or not summary.filing_complete
        or classification.financial_year != _SUPPORTED_OPTION_BUSINESS_FINANCIAL_YEAR
        or classification.books_maintained is not False
        or classification.other_speculative_business is not False
        or classification.nature_of_business_code is None
        or classification.turnover_basis is not RobinhoodOptionTurnoverBasis.ICAI_5_11A_DIFFERENCES
        or classification.external_expenses_inr is None
        or classification.bp_additions_inr is None
        or classification.bp_deductions_inr is None
    ):
        return None
    slices = tuple(
        item
        for item in options.lot_slices
        if item.in_financial_year and item.contract.account_index == summary.account_index
    )
    if not slices:
        return None
    refusal = partial(_slices_total_refusal, slices)
    rate = summary.fx_rate
    exact_rate = _rational(rate, refusal)
    exact_expenses = _rational(classification.external_expenses_inr, refusal)
    exact_favourable_usd = sum(
        (max(item.exact_net_pnl_usd, Fraction()) for item in slices), Fraction()
    )
    exact_unfavourable_usd = sum(
        (max(-item.exact_net_pnl_usd, Fraction()) for item in slices), Fraction()
    )
    exact_gross_profit = exact_favourable_usd * exact_rate
    exact_unfavourable = exact_unfavourable_usd * exact_rate
    exact_net_income = exact_gross_profit - exact_unfavourable - exact_expenses
    require(
        exact_net_income == summary.exact_net_pnl_inr - exact_expenses,
        what="option no-books P&L must reconcile to the exact business summary",
    )
    # The audit rows show the exact amounts.
    favourable_usd = _shown(
        exact_favourable_usd,
        _decimal_sum,
        *(max(item.net_pnl_usd, Decimal()) for item in slices),
    )
    unfavourable_usd = _shown(
        exact_unfavourable_usd,
        _decimal_sum,
        *(min(item.net_pnl_usd, Decimal()).copy_negate() for item in slices),
    )
    turnover_full = _shown(
        exact_gross_profit + exact_unfavourable,
        lambda: (favourable_usd + unfavourable_usd) * rate,
    )
    gross_profit_full = _shown(exact_gross_profit, mul, favourable_usd, rate)
    unfavourable_full = _shown(exact_unfavourable, mul, unfavourable_usd, rate)
    expenditure_full = _shown(
        exact_unfavourable + exact_expenses,
        add,
        unfavourable_full,
        classification.external_expenses_inr,
    )
    net_income_full = _shown(exact_net_income, sub, gross_profit_full, expenditure_full)
    gross_profit_inr = exact_whole_rupees(exact_gross_profit)
    unfavourable_inr = exact_whole_rupees(exact_unfavourable)
    with _Exact(refusal):
        external_expenses_inr = _whole_rupees(classification.external_expenses_inr)
        turnover_inr = gross_profit_inr + unfavourable_inr
        expenditure_inr = unfavourable_inr + external_expenses_inr
        net_income_inr = gross_profit_inr - expenditure_inr
        bp_additions_inr = _whole_rupees(classification.bp_additions_inr)
        bp_deductions_inr = _whole_rupees(classification.bp_deductions_inr)
        bp_income_inr = net_income_inr + bp_additions_inr - bp_deductions_inr
        return RobinhoodOptionBusinessFiling(
            financial_year=classification.financial_year,
            turnover_basis=classification.turnover_basis,
            turnover_inr_full_precision=turnover_full,
            gross_profit_inr_full_precision=gross_profit_full,
            expenditure_inr_full_precision=expenditure_full,
            net_income_inr_full_precision=net_income_full,
            turnover_inr=turnover_inr,
            gross_profit_inr=gross_profit_inr,
            expenditure_inr=expenditure_inr,
            net_income_inr=net_income_inr,
            bp_additions_inr=bp_additions_inr,
            bp_deductions_inr=bp_deductions_inr,
            bp_income_inr=bp_income_inr,
            cfl_loss_inr=max(Decimal(), bp_income_inr.copy_negate()),
            event_count=len(slices),
            nature_of_business_code=classification.nature_of_business_code,
        )


@dataclass(frozen=True, slots=True)
class _OpenPool:
    contract: RobinhoodOptionContract
    references: tuple[RobinhoodSecurityEventReference, ...]
    activity_date: date
    quantity: Decimal
    cash_usd: Decimal


@dataclass(frozen=True, slots=True)
class _TradeGroup:
    contract: RobinhoodOptionContract
    activity_date: date
    kind: RobinhoodEventKind
    trades: tuple[RobinhoodOptionTradeEvidence, ...]

    @property
    def quantity(self) -> Decimal:
        with _Exact(partial(_quantity_refusal, self.references)):
            return sum((trade.quantity for trade in self.trades), Decimal())

    @property
    def cash_abs(self) -> Decimal:
        with _Exact(partial(_amount_refusal, self.references)):
            return sum((trade.exact_cash_usd.copy_abs() for trade in self.trades), Decimal())

    @property
    def references(self) -> tuple[RobinhoodSecurityEventReference, ...]:
        return tuple(trade.event_reference for trade in self.trades)


def _fail(message: str) -> RobinhoodOptionsError:
    return RobinhoodOptionsError(message)


class _Exact:
    """Run a block's Decimal arithmetic exactly, or refuse (#1148).

    The block runs at the module's 60 digits with ``Inexact`` trapped. An
    operation that would round raises :class:`RobinhoodOptionsError` with the
    fixed text ``refusal()`` builds; one that only drops trailing zeros is
    exact and passes.
    """

    __slots__ = ("_context", "_refusal")

    def __init__(self, refusal: Callable[[], str]) -> None:
        self._refusal = refusal
        self._context = localcontext(_EXACT_CONTEXT)

    def __enter__(self) -> None:
        self._context.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._context.__exit__(exc_type, exc, traceback)
        if exc_type is not None and issubclass(exc_type, Inexact):
            raise _fail(self._refusal()) from None


def _activity_refusal(
    references: Sequence[RobinhoodSecurityEventReference], column: str, subject: str
) -> str:
    return (
        f"Robinhood option activity row {references[0].event_ordinal}, column {column}: "
        f"{subject} needs more digits than exact option arithmetic allows"
    )


def _amount_refusal(references: Sequence[RobinhoodSecurityEventReference]) -> str:
    """Reading or grouping a trade's cash, comparing same-day prices, or the premium band."""
    return _activity_refusal(references, "Amount", "the trade amount")


def _total_refusal(references: Sequence[RobinhoodSecurityEventReference]) -> str:
    """A cost, proceeds or gain share, total or conversion that exact
    arithmetic can't hold (#1148, #1245)."""
    return _activity_refusal(references, "Amount", "an option cost, proceeds or gain total")


def _slices_total_refusal(slices: Sequence[RobinhoodOptionLotSlice]) -> str:
    """A total over ``slices``, at its first slice's first close. A total over
    no slices is 0 and never refuses."""
    return _total_refusal(slices[0].close_references)


def _quantity_refusal(references: Sequence[RobinhoodSecurityEventReference]) -> str:
    """Any activity contract-quantity sum or difference."""
    return _activity_refusal(references, "Quantity", "the contract quantity")


def _position_refusal(reference: RobinhoodPositionReference) -> str:
    """The statement position quantity sum, which may have no activity trade."""
    location = reference.location
    return (
        f"Robinhood option account {reference.account_index + 1}, {location.section.value}: "
        f"statement {reference.statement_index + 1} page {location.page} row {location.row}, "
        "column Qty: the position quantity needs more digits than exact option arithmetic allows"
    )


def _whole_rupees(amount: Decimal) -> Decimal:
    """A reviewed INR config amount's whole-rupee rounding. An exact option
    total files :func:`~ibkr_fa.compute.exact_inr.exact_whole_rupees` (#1245)."""
    return amount.quantize(_WHOLE_RUPEE, rounding=ROUND_HALF_UP, context=_ARITHMETIC_CONTEXT)


def _rational(value: Decimal, refusal: Callable[[], str]) -> Fraction:
    """``value`` as an exact rational, refusing with ``refusal()`` a nonfinite
    value or one past :func:`~ibkr_fa.compute.exact_inr.exact_fraction`'s digit cap."""
    exact = exact_fraction(value)
    if exact is None:
        raise _fail(refusal())
    return exact


def _allocated(
    cash: Decimal,
    quantity: Decimal,
    drawn: Decimal,
    references: Sequence[RobinhoodSecurityEventReference],
) -> Fraction:
    """``cash * drawn / quantity`` as an exact rational (#1245).

    This replaces #1148's ``R``, which rounded the share half-even to the finer
    of 1E-6 and ``cash``'s exponent. It is ``cash`` when ``drawn`` is the
    whole ``quantity``, and it never falls as ``drawn`` grows. An operand past
    :func:`~ibkr_fa.compute.exact_inr.exact_fraction`'s digit cap refuses at
    the first row of ``references``.
    """
    share = exact_share(cash, drawn, quantity)
    if share is None:
        raise _fail(_total_refusal(references))
    return share


def _drawn_share(
    cash: Decimal,
    quantity: Decimal,
    before: Decimal,
    after: Decimal,
    references: Sequence[RobinhoodSecurityEventReference],
) -> tuple[Decimal, Fraction]:
    """The part of ``cash`` that taking its group from ``before`` to ``after``
    drawn contracts gets, as shown and exactly. The exact draws of a whole
    group add back to ``cash``, and none is negative."""
    exact = _allocated(cash, quantity, after, references) - _allocated(
        cash, quantity, before, references
    )
    return _shown(exact, _decimal_drawn_share, cash, quantity, before, after), exact


def _decimal_drawn_share(
    cash: Decimal, quantity: Decimal, before: Decimal, after: Decimal
) -> Decimal:
    """#1148's ``R(after) - R(before)`` without its rounding: each cumulative
    share keeps trailing zeros down to ``cash``'s exponent, whatever the
    quantities' exponents."""
    cash_exponent = cash.as_tuple().exponent
    assert isinstance(cash_exponent, int)  # finite: _allocated refused other cash
    cash_unit = Decimal(1).scaleb(cash_exponent)

    def at_cash(drawn: Decimal) -> Decimal:
        exact = Fraction(cash) * Fraction(drawn) / Fraction(quantity)
        share = (Decimal(exact.numerator) / Decimal(exact.denominator)).normalize()
        exponent = share.as_tuple().exponent
        coarser = share.is_zero() or (isinstance(exponent, int) and exponent > cash_exponent)
        return share.quantize(cash_unit) if coarser else share

    return at_cash(after) - at_cash(before)


def _decimal_sum(*amounts: Decimal) -> Decimal:
    return sum(amounts, Decimal())


def _shown(exact: Fraction, operation: Callable[..., Decimal], *operands: Decimal) -> Decimal:
    """``exact`` as a Decimal amount (#1245).

    It is ``operation(*operands)``, the Decimal arithmetic #1148 ran on the
    shown operands, where that gives ``exact`` within 60 digits, so an amount
    that divides exactly keeps its digits. Elsewhere it is one division of
    ``exact`` with at least 60 digits, widened if needed to retain a tenth,
    cut toward zero
    (:func:`~ibkr_fa.compute.exact_inr.exact_decimal`), whose HALF_UP whole
    rupees are ``exact``'s.
    """
    with localcontext(_SHOWN_CONTEXT) as context:
        context.clear_flags()
        shown = operation(*operands)
        if not context.flags[Inexact] and shown == exact:
            return shown
        return exact_decimal(exact)


def _validate_inputs(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    financial_year_start: date,
    financial_year_end: date,
) -> None:
    if not is_one_financial_year(financial_year_start, financial_year_end):
        raise _fail("Robinhood options require one complete April-to-March financial year")
    if not holds_two_accounts(paired, config):
        raise _fail("Robinhood options require the reconciled two-account bundle")
    if not same_distinct_account_numbers(paired, config):
        raise _fail("option config does not match the reconciled account identities")


def _contract_from_event(
    account_index: int,
    event: RobinhoodEvent,
) -> RobinhoodOptionContract:
    if (
        event.option_symbol is None
        or event.option_expiration is None
        or event.option_right is None
        or event.option_strike_usd is None
        or event.quantity is None
        or event.price_usd is None
        or event.amount_usd is None
    ):
        raise _fail("option event lacks its parser-required contract or cash fields")
    return RobinhoodOptionContract(
        account_index=account_index,
        symbol=event.option_symbol,
        expiration=event.option_expiration,
        right=event.option_right,
        strike_usd=event.option_strike_usd,
    )


def _whole_contracts(quantity: Decimal) -> bool:
    return quantity == quantity.to_integral_value()


def _premium_band(event: RobinhoodEvent) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The displayed gross premium band of one trade, or ``ValueError``.

    The quantity is a whole number of contracts, exact by definition, so it
    gets no allowance (#1134). Only the displayed price and the cash carry
    display rounding: half a unit in the last digit each shows.
    A separate preflight preserves the previous exact-arithmetic refusals;
    its bounds never enter the acceptance check.
    """
    assert event.quantity is not None
    assert event.price_usd is not None
    assert event.amount_usd is not None
    price_band = robinhood_half_ulp(event.price_usd)
    cash_band = robinhood_half_ulp(event.amount_usd.copy_abs())
    # Preserve prior exactness refusals without widening the acceptance band.
    quantity_step = robinhood_half_ulp(event.quantity)
    _previous_low = (
        max(Decimal(), event.quantity - quantity_step)
        * max(Decimal(), event.price_usd - price_band)
        * STANDARD_OPTION_MULTIPLIER
    )
    _previous_high = (
        (event.quantity + quantity_step)
        * (event.price_usd + price_band)
        * STANDARD_OPTION_MULTIPLIER
    )
    low = event.quantity * max(Decimal(), event.price_usd - price_band) * STANDARD_OPTION_MULTIPLIER
    high = event.quantity * (event.price_usd + price_band) * STANDARD_OPTION_MULTIPLIER
    mid = event.quantity * event.price_usd * STANDARD_OPTION_MULTIPLIER
    cash = event.amount_usd.copy_abs()
    if not (low <= cash + cash_band and cash - cash_band <= high):
        raise ValueError
    return mid, low, high, cash - mid


def _source_trades(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[
    tuple[RobinhoodOptionTradeEvidence, ...],
    tuple[RobinhoodOptionBlocker, ...],
]:
    trades: list[RobinhoodOptionTradeEvidence] = []
    blockers: list[RobinhoodOptionBlocker] = []
    for account_index, account in enumerate(paired.accounts):
        for event in account.evidence.events:
            if event.kind not in ROBINHOOD_OPTION_TRADE_KINDS:
                continue
            contract = _contract_from_event(account_index, event)
            reference = RobinhoodSecurityEventReference(
                account_index,
                event.source_record_index,
            )
            assert event.quantity is not None
            unsupported = tuple(
                column
                for column, failed in (
                    ("Description", contract.right != "Call"),
                    ("Quantity", not _whole_contracts(event.quantity)),
                )
                if failed
            )
            if unsupported:
                blockers.append(
                    RobinhoodOptionBlocker(
                        kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
                        account_index=account_index,
                        contracts=(contract,),
                        event_references=(reference,),
                        located_at=reference,
                        columns=unsupported,
                    )
                )
            if event.activity_date > contract.expiration:
                blockers.append(
                    RobinhoodOptionBlocker(
                        kind=RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
                        account_index=account_index,
                        contracts=(contract,),
                        event_references=(reference,),
                        located_at=reference,
                        columns=("Activity Date", "Description"),
                    )
                )
            with _Exact(partial(_amount_refusal, (reference,))):
                try:
                    mid, low, high, residual = _premium_band(event)
                except ValueError:
                    assert event.quantity is not None
                    assert event.price_usd is not None
                    assert event.amount_usd is not None
                    mid = event.quantity * event.price_usd * STANDARD_OPTION_MULTIPLIER
                    low = high = mid
                    residual = event.amount_usd.copy_abs() - mid
                    blockers.append(
                        RobinhoodOptionBlocker(
                            kind=RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION,
                            account_index=account_index,
                            contracts=(contract,),
                            event_references=(reference,),
                            located_at=reference,
                            columns=("Quantity", "Price", "Amount"),
                        )
                    )
            assert event.price_usd is not None
            assert event.amount_usd is not None
            trades.append(
                RobinhoodOptionTradeEvidence(
                    contract=contract,
                    event_reference=reference,
                    kind=event.kind,
                    activity_date=event.activity_date,
                    process_date=event.process_date,
                    settle_date=event.settle_date,
                    quantity=event.quantity,
                    displayed_price_usd=event.price_usd,
                    exact_cash_usd=event.amount_usd,
                    displayed_gross_mid_usd=mid,
                    displayed_gross_low_usd=low,
                    displayed_gross_high_usd=high,
                    cash_minus_displayed_mid_usd=residual,
                )
            )
    return (
        tuple(
            sorted(
                trades,
                key=lambda trade: (
                    trade.contract,
                    trade.activity_date,
                    trade.event_reference.event_ordinal,
                ),
            )
        ),
        tuple(blockers),
    )


def _contract_from_position(
    account_index: int,
    position: RobinhoodStatementPosition,
) -> RobinhoodOptionContract | None:
    fields = (
        position.option_expiration,
        position.option_right,
        position.option_strike_usd,
    )
    if all(value is None for value in fields):
        return None
    if any(value is None for value in fields):
        raise _fail("statement option position has partial contract identity")
    assert position.option_expiration is not None
    assert position.option_right is not None
    assert position.option_strike_usd is not None
    return RobinhoodOptionContract(
        account_index=account_index,
        symbol=position.symbol_or_cusip,
        expiration=position.option_expiration,
        right=position.option_right,
        strike_usd=position.option_strike_usd,
    )


def _valuation_anchors(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[
    tuple[RobinhoodOptionValuationAnchor, ...],
    tuple[RobinhoodOptionBlocker, ...],
]:
    anchors: list[RobinhoodOptionValuationAnchor] = []
    blockers: list[RobinhoodOptionBlocker] = []
    for account_index, account in enumerate(paired.accounts):
        for statement_index, statement in enumerate(account.activity.primary_bundle.statements):
            for position in statement.positions:
                contract = _contract_from_position(account_index, position)
                if contract is None:
                    continue
                reference = RobinhoodPositionReference(
                    account_index,
                    statement_index,
                    position.location,
                )
                unsupported = tuple(
                    column
                    for column, failed in (
                        ("Description", contract.right != "Call"),
                        (
                            "Qty",
                            position.quantity <= 0 or not _whole_contracts(position.quantity),
                        ),
                        ("Mkt Value", position.market_value_usd < 0),
                    )
                    if failed
                )
                if unsupported:
                    blockers.append(
                        RobinhoodOptionBlocker(
                            kind=RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT,
                            account_index=account_index,
                            contracts=(contract,),
                            position_references=(reference,),
                            located_at=reference,
                            columns=unsupported,
                        )
                    )
                anchors.append(
                    RobinhoodOptionValuationAnchor(
                        contract=contract,
                        position_reference=reference,
                        statement_date=statement.period_end,
                        quantity=position.quantity,
                        displayed_price_usd=position.price_usd,
                        market_value_usd=position.market_value_usd,
                    )
                )
    return (
        tuple(
            sorted(
                anchors,
                key=lambda anchor: (
                    anchor.contract,
                    anchor.statement_date,
                    anchor.position_reference.statement_index,
                    anchor.position_reference.location.page,
                    anchor.position_reference.location.row,
                ),
            )
        ),
        tuple(blockers),
    )


def signed_option_quantity(trade: RobinhoodOptionTradeEvidence) -> Decimal:
    """Return the contract quantity a BTO adds or an STC removes."""
    return (
        trade.quantity
        if trade.kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN
        else trade.quantity.copy_negate()
    )


def _net_quantity(trades: Sequence[RobinhoodOptionTradeEvidence]) -> Decimal:
    """The trades' net contract quantity, exactly, or refuse at the first trade's row."""
    with _Exact(partial(_quantity_refusal, tuple(trade.event_reference for trade in trades))):
        return sum((signed_option_quantity(trade) for trade in trades), Decimal())


def _first_oversold(
    trades: Sequence[RobinhoodOptionTradeEvidence],
) -> RobinhoodSecurityEventReference:
    """The first of ``trades`` after which fewer than no contracts are held.
    The caller has found that all of them net below zero, by the same exact
    running sum."""
    with _Exact(partial(_quantity_refusal, tuple(trade.event_reference for trade in trades))):
        held = accumulate(signed_option_quantity(trade) for trade in trades)
        return next(
            trade.event_reference for trade, net in zip(trades, held, strict=True) if net < 0
        )


def _position_blockers(
    paired: RobinhoodAccountsReconciliation,
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    anchors: tuple[RobinhoodOptionValuationAnchor, ...],
) -> tuple[RobinhoodOptionBlocker, ...]:
    blockers: list[RobinhoodOptionBlocker] = []
    trades_by_account: dict[int, list[RobinhoodOptionTradeEvidence]] = defaultdict(list)
    anchors_by_statement: dict[
        tuple[int, int],
        list[RobinhoodOptionValuationAnchor],
    ] = defaultdict(list)
    contracts_by_account: dict[int, set[RobinhoodOptionContract]] = defaultdict(set)
    for trade in trades:
        trades_by_account[trade.contract.account_index].append(trade)
        contracts_by_account[trade.contract.account_index].add(trade.contract)
    for anchor in anchors:
        account_index = anchor.contract.account_index
        anchors_by_statement[(account_index, anchor.position_reference.statement_index)].append(
            anchor
        )
        contracts_by_account[account_index].add(anchor.contract)

    for account_index, account in enumerate(paired.accounts):
        for statement_index, statement in enumerate(account.activity.primary_bundle.statements):
            observed: dict[RobinhoodOptionContract, Decimal] = defaultdict(Decimal)
            references: dict[
                RobinhoodOptionContract,
                list[RobinhoodPositionReference],
            ] = defaultdict(list)
            for anchor in anchors_by_statement[(account_index, statement_index)]:
                with _Exact(partial(_position_refusal, anchor.position_reference)):
                    observed[anchor.contract] += anchor.quantity
                references[anchor.contract].append(anchor.position_reference)
            for contract in contracts_by_account[account_index]:
                relevant = [
                    trade
                    for trade in trades_by_account[account_index]
                    if trade.contract == contract and trade.settle_date <= statement.period_end
                ]
                expected = _net_quantity(relevant)
                if expected < 0:
                    blockers.append(
                        RobinhoodOptionBlocker(
                            kind=RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
                            account_index=account_index,
                            contracts=(contract,),
                            event_references=tuple(trade.event_reference for trade in relevant),
                            located_at=_first_oversold(relevant),
                            columns=("Trans Code", "Quantity"),
                        )
                    )
                    continue
                if observed[contract] != expected:
                    printed = tuple(references[contract])
                    blockers.append(
                        RobinhoodOptionBlocker(
                            kind=RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
                            account_index=account_index,
                            contracts=(contract,),
                            event_references=tuple(trade.event_reference for trade in relevant),
                            position_references=printed,
                            # Nothing printed: the activity holds a nonzero quantity.
                            located_at=printed[0] if printed else relevant[0].event_reference,
                            columns=("Qty",) if printed else ("Quantity",),
                            missing_from_statement_index=None if printed else statement_index,
                        )
                    )
    return tuple(blockers)


def _trade_groups(
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
) -> dict[RobinhoodOptionContract, tuple[_TradeGroup, ...]]:
    grouped: dict[
        tuple[RobinhoodOptionContract, date, RobinhoodEventKind],
        list[RobinhoodOptionTradeEvidence],
    ] = defaultdict(list)
    for trade in trades:
        grouped[(trade.contract, trade.activity_date, trade.kind)].append(trade)
    result: dict[RobinhoodOptionContract, list[_TradeGroup]] = defaultdict(list)
    for (contract, activity_date, kind), items in grouped.items():
        result[contract].append(
            _TradeGroup(
                contract=contract,
                activity_date=activity_date,
                kind=kind,
                trades=tuple(
                    sorted(
                        items,
                        key=lambda trade: trade.event_reference.event_ordinal,
                    )
                ),
            )
        )
    return {
        contract: tuple(
            sorted(
                groups,
                key=lambda group: (
                    group.activity_date,
                    group.kind.value,
                ),
            )
        )
        for contract, groups in result.items()
    }


def _match_lots(
    paired: RobinhoodAccountsReconciliation,
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    *,
    financial_year_start: date,
    financial_year_end: date,
) -> tuple[
    tuple[RobinhoodOptionLotSlice, ...],
    tuple[RobinhoodOptionOpenLot, ...],
    tuple[RobinhoodOptionBlocker, ...],
]:
    slices: list[RobinhoodOptionLotSlice] = []
    open_lots: list[RobinhoodOptionOpenLot] = []
    blockers: list[RobinhoodOptionBlocker] = []
    report_end_by_account = {
        index: account.evidence.end_date for index, account in enumerate(paired.accounts)
    }
    for contract, groups in sorted(_trade_groups(trades).items()):
        by_date: dict[date, set[RobinhoodEventKind]] = defaultdict(set)
        for group in groups:
            by_date[group.activity_date].add(group.kind)
        mixed_date = next((day for day, kinds in by_date.items() if len(kinds) > 1), None)
        if mixed_date is not None:
            first_row = min(
                (group.references[0] for group in groups if group.activity_date == mixed_date),
                key=lambda reference: reference.event_ordinal,
            )
            blockers.append(
                _ordering_blocker(contract, groups, first_row, ("Activity Date", "Trans Code"))
            )
            continue
        mixed_prices = next((group for group in groups if not _same_fill_price(group)), None)
        if mixed_prices is not None:
            blockers.append(
                _ordering_blocker(
                    contract,
                    groups,
                    mixed_prices.references[0],
                    ("Activity Date", "Quantity", "Amount"),
                )
            )
            continue

        opens: FifoPool[_OpenPool] = FifoPool()
        drawn: dict[_OpenPool, Decimal] = {}
        local_slices: list[RobinhoodOptionLotSlice] = []
        failed = False
        for group in groups:
            if group.kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN:
                pool = _OpenPool(
                    contract=contract,
                    references=group.references,
                    activity_date=group.activity_date,
                    quantity=group.quantity,
                    cash_usd=group.cash_abs,
                )
                opens.add(pool, pool.quantity)
                continue

            consumption = opens.consume(
                group.quantity,
                at=(
                    f"Robinhood option activity row {group.references[0].event_ordinal}, "
                    "column Quantity"
                ),
            )
            if consumption.shortfall > 0:
                blockers.append(
                    RobinhoodOptionBlocker(
                        kind=RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY,
                        account_index=contract.account_index,
                        contracts=(contract,),
                        event_references=group.references,
                        located_at=group.references[0],
                        columns=("Trans Code", "Quantity"),
                    )
                )
                failed = True
                break

            sold_quantity = group.quantity
            proceeds_usd = group.cash_abs
            sold = Decimal()
            for pool, quantity in consumption.allocations:
                with _Exact(partial(_quantity_refusal, group.references)):
                    pool_before = drawn.get(pool, Decimal())
                    drawn[pool] = pool_before + quantity
                    sold_before, sold = sold, sold + quantity
                cost, exact_cost = _drawn_share(
                    pool.cash_usd, pool.quantity, pool_before, drawn[pool], group.references
                )
                proceeds, exact_proceeds = _drawn_share(
                    proceeds_usd, sold_quantity, sold_before, sold, group.references
                )
                local_slices.append(
                    RobinhoodOptionLotSlice(
                        contract=contract,
                        open_references=pool.references,
                        close_references=group.references,
                        open_date=pool.activity_date,
                        close_date=group.activity_date,
                        quantity=quantity,
                        cost_usd=cost,
                        net_proceeds_usd=proceeds,
                        net_pnl_usd=_shown(exact_proceeds - exact_cost, sub, proceeds, cost),
                        in_financial_year=(
                            financial_year_start <= group.activity_date <= financial_year_end
                        ),
                        exact_cost_usd=exact_cost,
                        exact_net_proceeds_usd=exact_proceeds,
                    )
                )
        if failed:
            continue
        remaining = opens.remaining()
        if remaining and contract.expiration <= report_end_by_account[contract.account_index]:
            blockers.append(
                RobinhoodOptionBlocker(
                    kind=RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE,
                    account_index=contract.account_index,
                    contracts=(contract,),
                    event_references=tuple(
                        reference for pool, _ in remaining for reference in pool.references
                    ),
                    located_at=remaining[0][0].references[0],
                    columns=("Description", "Quantity"),
                )
            )
        slices.extend(local_slices)
        for pool, quantity in remaining:
            remaining_cost, exact_remaining_cost = _drawn_share(
                pool.cash_usd,
                pool.quantity,
                drawn.get(pool, Decimal()),
                pool.quantity,
                pool.references,
            )
            open_lots.append(
                RobinhoodOptionOpenLot(
                    contract=contract,
                    open_references=pool.references,
                    open_date=pool.activity_date,
                    remaining_quantity=quantity,
                    remaining_cost_usd=remaining_cost,
                    exact_remaining_cost_usd=exact_remaining_cost,
                )
            )
    return tuple(slices), tuple(open_lots), tuple(blockers)


def _ordering_blocker(
    contract: RobinhoodOptionContract,
    groups: Sequence[_TradeGroup],
    located_at: RobinhoodSecurityEventReference,
    columns: tuple[str, ...],
) -> RobinhoodOptionBlocker:
    """An ``ORDERING_AMBIGUITY`` blocker naming every trade of ``contract``."""
    return RobinhoodOptionBlocker(
        kind=RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,
        account_index=contract.account_index,
        contracts=(contract,),
        event_references=tuple(trade.event_reference for group in groups for trade in group.trades),
        located_at=located_at,
        columns=columns,
    )


def _same_fill_price(group: _TradeGroup) -> bool:
    """Whether every trade in ``group`` fills at its first trade's price, compared
    exactly as ``|cash_a| * qty_b == |cash_b| * qty_a`` rather than by dividing."""
    first = group.trades[0]
    with _Exact(partial(_amount_refusal, group.references)):
        return all(
            trade.exact_cash_usd.copy_abs() * first.quantity
            == first.exact_cash_usd.copy_abs() * trade.quantity
            for trade in group.trades[1:]
        )


def _quantity_on(
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    account_index: int,
    on_date: date,
) -> Decimal:
    return _net_quantity(
        tuple(
            trade
            for trade in trades
            if trade.contract.account_index == account_index and trade.activity_date <= on_date
        )
    )


def _account_has_fy_exposure(
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    account_index: int,
    financial_year_start: date,
    financial_year_end: date,
) -> bool:
    account_trades = tuple(
        trade for trade in trades if trade.contract.account_index == account_index
    )
    return bool(account_trades) and (
        any(
            financial_year_start <= trade.activity_date <= financial_year_end
            for trade in account_trades
        )
        or _quantity_on(account_trades, account_index, financial_year_start - timedelta(days=1))
        != 0
    )


def _also_found(remedy: str, blockers: tuple[RobinhoodOptionBlocker, ...]) -> str:
    """The clause saying ``remedy`` won't clear ``blockers``, the option
    blockers the run also found (#1522), or nothing without any."""
    if not blockers:
        return ""
    return (
        f"; {remedy} won't clear the unresolved option blockers the run also found: "
        f"{option_blocker_summary(blockers)}"
    )


def option_itr_form_refusal(reconstruction: RobinhoodOptionReconstruction) -> str:
    """Why a reviewed business treatment refuses ``--itr-form ITR-2``, and
    every option blocker ``reconstruction`` has: ITR-3 clears none (#1569)."""
    return (
        f"{ROBINHOOD_FNO_FEMA_WARNING} reviewed Robinhood option treatment requires "
        "--itr-form ITR-3" + _also_found("the flag", reconstruction.blockers)
    )


def _classification_refusal(
    missing: tuple[str, ...],
    financial_year: int,
    blockers: tuple[RobinhoodOptionBlocker, ...],
) -> RobinhoodOptionClassificationError:
    """The refusal for the ``missing`` accounts' entries, naming each as
    ``account N`` and the financial year (#1554), then ``blockers``: no entry
    clears them (#1522)."""
    return RobinhoodOptionClassificationError(
        f"{ROBINHOOD_FNO_FEMA_WARNING} Robinhood F&O requires an explicit account/FY "
        "[[robinhood.option_classifications]] entry for "
        + (
            f"account {missing[0]}"
            if len(missing) == 1
            else f"each of accounts {' and '.join(missing)}"
        )
        + ' with account_number = "<account number>", '
        f"financial_year = {financial_year}, "
        'treatment = "<capital-gains, speculative-business or '
        'non-speculative-business>", '
        "fema_risk_acknowledged = true, "
        'source = "<what you reviewed>", '
        'reason = "<why that treatment applies>"' + _also_found("the entry", blockers)
    )


def _classification_evidence(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    financial_year_start: date,
    financial_year_end: date,
) -> tuple[tuple[RobinhoodOptionClassificationEvidence, ...], tuple[str, ...]]:
    """Each exposed account's reviewed treatment, and the 1-based numbers of
    the exposed accounts with none."""
    classifications: list[RobinhoodOptionClassificationEvidence] = []
    missing: list[str] = []
    for account_index, account in enumerate(paired.accounts):
        if not _account_has_fy_exposure(
            trades,
            account_index,
            financial_year_start,
            financial_year_end,
        ):
            continue
        number = account.activity.primary_bundle.account_number
        configured = config.option_classification_for(number, financial_year_start.year)
        if configured is None:
            missing.append(str(account_index + 1))
            continue
        classifications.append(
            RobinhoodOptionClassificationEvidence(
                account_index=account_index,
                financial_year=configured.financial_year,
                treatment=configured.treatment,
                source=configured.source,
                reason=configured.reason,
                schedule_fa_disposition=(
                    RobinhoodOptionFaDisposition.TABLE_D
                    if configured.treatment is RobinhoodOptionTreatment.CAPITAL_GAINS
                    else RobinhoodOptionFaDisposition.A2_ONLY
                ),
                books_maintained=configured.books_maintained,
                other_speculative_business=configured.other_speculative_business,
                nature_of_business_code=configured.nature_of_business_code,
                turnover_basis=configured.turnover_basis,
                external_expenses_inr=configured.external_expenses_inr,
                bp_additions_inr=configured.bp_additions_inr,
                bp_deductions_inr=configured.bp_deductions_inr,
            )
        )
    return tuple(classifications), tuple(missing)


def _blocked(
    blockers: tuple[RobinhoodOptionBlocker, ...],
    account_index: int,
    contract: RobinhoodOptionContract | None = None,
) -> bool:
    return any(
        blocker.account_index == account_index
        and (contract is None or not blocker.contracts or contract in blocker.contracts)
        for blocker in blockers
    )


def _unsupported_contract(
    blockers: tuple[RobinhoodOptionBlocker, ...],
    account_index: int,
    contract: RobinhoodOptionContract,
) -> bool:
    if contract.right == "Call":
        return False
    return any(
        blocker.kind is RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT
        and blocker.account_index == account_index
        and contract in blocker.contracts
        for blocker in blockers
    )


def _tax_evidence(
    trades: tuple[RobinhoodOptionTradeEvidence, ...],
    slices: tuple[RobinhoodOptionLotSlice, ...],
    classifications: tuple[RobinhoodOptionClassificationEvidence, ...],
    blockers: tuple[RobinhoodOptionBlocker, ...],
    *,
    financial_year_end: date,
) -> tuple[
    tuple[RobinhoodOptionLotSlice, ...],
    tuple[RobinhoodOptionBusinessTotals, ...],
    tuple[RobinhoodOptionBlocker, ...],
]:
    """The capital-gains slices to file, each business account's exact USD
    totals, and the blockers they add, all before any FX (#1145)."""
    capital_slices: list[RobinhoodOptionLotSlice] = []
    business_totals: list[RobinhoodOptionBusinessTotals] = []
    new_blockers: list[RobinhoodOptionBlocker] = []
    slices_by_account: dict[int, list[RobinhoodOptionLotSlice]] = defaultdict(list)
    for item in slices:
        if item.in_financial_year:
            slices_by_account[item.contract.account_index].append(item)

    for classification in classifications:
        account_index = classification.account_index
        current_slices = tuple(slices_by_account[account_index])
        if classification.treatment is RobinhoodOptionTreatment.CAPITAL_GAINS:
            for item in current_slices:
                if _blocked(blockers, account_index, item.contract):
                    continue
                if item.close_date < NEW_REGIME_CUTOVER_DATE:
                    new_blockers.append(
                        RobinhoodOptionBlocker(
                            kind=RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,
                            account_index=account_index,
                            contracts=(item.contract,),
                            event_references=item.close_references,
                            located_at=item.close_references[0],
                            columns=("Activity Date",),
                        )
                    )
                    continue
                capital_slices.append(item)
            continue

        business_slices = tuple(
            item
            for item in current_slices
            if not _unsupported_contract(blockers, account_index, item.contract)
        )
        business_trades = tuple(
            trade
            for trade in trades
            if trade.contract.account_index == account_index
            and not _unsupported_contract(blockers, account_index, trade.contract)
        )
        open_at_end = _quantity_on(business_trades, account_index, financial_year_end)
        if open_at_end != 0:
            new_blockers.append(
                RobinhoodOptionBlocker(
                    kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
                    account_index=account_index,
                    contracts=tuple(
                        sorted(
                            {
                                trade.contract
                                for trade in business_trades
                                if trade.activity_date <= financial_year_end
                            }
                        )
                    ),
                )
            )
        exact_cost = sum((item.exact_cost_usd for item in business_slices), Fraction())
        exact_proceeds = sum((item.exact_net_proceeds_usd for item in business_slices), Fraction())
        cost = _shown(exact_cost, _decimal_sum, *(item.cost_usd for item in business_slices))
        proceeds = _shown(
            exact_proceeds,
            _decimal_sum,
            *(item.net_proceeds_usd for item in business_slices),
        )
        business_totals.append(
            RobinhoodOptionBusinessTotals(
                account_index=account_index,
                treatment=classification.treatment,
                slices=business_slices,
                matched_premium_paid_usd=cost,
                matched_premium_received_usd=proceeds,
                net_pnl_usd=_shown(exact_proceeds - exact_cost, sub, proceeds, cost),
                filing_complete=not _blocked(blockers, account_index) and open_at_end == 0,
            )
        )
    return tuple(capital_slices), tuple(business_totals), tuple(new_blockers)


def _capital_gain_line(
    item: RobinhoodOptionLotSlice, fx_policy: AuditedFxPolicy
) -> RobinhoodOptionCapitalGainLine:
    resolution = fx_policy.rate_for_audited(item.close_date)
    rate = resolution.rate
    exact_rate = _rational(rate, partial(_total_refusal, item.close_references))
    short_term = is_short_term_holding(item.open_date, item.close_date)
    exact_gain = item.exact_net_pnl_usd * exact_rate
    return RobinhoodOptionCapitalGainLine(
        source_slice=item,
        bucket=(
            RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS
            if short_term
            else RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS
        ),
        loss_bucket=(
            None
            if exact_gain >= 0
            else (
                RobinhoodOptionLossBucket.SHORT_TERM_CAPITAL
                if short_term
                else RobinhoodOptionLossBucket.LONG_TERM_CAPITAL
            )
        ),
        fx_rate=rate,
        fx_specified_date=resolution.specified_date,
        fx_rate_date=resolution.rate_date,
        cost_inr=_shown(item.exact_cost_usd * exact_rate, mul, item.cost_usd, rate),
        net_proceeds_inr=_shown(
            item.exact_net_proceeds_usd * exact_rate, mul, item.net_proceeds_usd, rate
        ),
        net_gain_inr=_shown(exact_gain, mul, item.net_pnl_usd, rate),
        exact_net_gain_inr=exact_gain,
    )


def _business_summary(
    totals: RobinhoodOptionBusinessTotals,
    fx_policy: AuditedFxPolicy,
    financial_year_end: date,
) -> RobinhoodOptionBusinessSummary:
    resolution = fx_policy.rate_for_audited(financial_year_end)
    rate = resolution.rate
    cost, proceeds, pnl = (
        totals.matched_premium_paid_usd,
        totals.matched_premium_received_usd,
        totals.net_pnl_usd,
    )
    exact_cost, exact_proceeds = (
        totals.exact_matched_premium_paid_usd,
        totals.exact_matched_premium_received_usd,
    )
    # No slices: every total is 0 at any rate.
    exact_rate = (
        _rational(rate, partial(_slices_total_refusal, totals.slices))
        if totals.slices
        else Fraction()
    )
    exact_pnl_inr = (exact_proceeds - exact_cost) * exact_rate
    speculative = totals.treatment is RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
    return RobinhoodOptionBusinessSummary(
        account_index=totals.account_index,
        treatment=totals.treatment,
        matched_premium_paid_usd=cost,
        matched_premium_received_usd=proceeds,
        net_pnl_usd=pnl,
        fx_rate=rate,
        fx_specified_date=resolution.specified_date,
        fx_rate_date=resolution.rate_date,
        matched_premium_paid_inr=_shown(exact_cost * exact_rate, mul, cost, rate),
        matched_premium_received_inr=_shown(exact_proceeds * exact_rate, mul, proceeds, rate),
        net_pnl_inr=_shown(exact_pnl_inr, mul, pnl, rate),
        exact_net_pnl_inr=exact_pnl_inr,
        loss_bucket=(
            None
            if exact_proceeds >= exact_cost
            else (
                RobinhoodOptionLossBucket.SPECULATIVE_BUSINESS
                if speculative
                else RobinhoodOptionLossBucket.NON_SPECULATIVE_BUSINESS
            )
        ),
        schedule_bp_bucket=(
            "speculative-business" if speculative else "business-other-than-speculative"
        ),
        filing_complete=totals.filing_complete,
    )


def _dedupe_blockers(
    blockers: tuple[RobinhoodOptionBlocker, ...],
) -> tuple[RobinhoodOptionBlocker, ...]:
    unique: dict[tuple[object, ...], RobinhoodOptionBlocker] = {}
    for blocker in blockers:
        key = (
            blocker.kind,
            blocker.account_index,
            blocker.contracts,
            blocker.event_references,
            blocker.position_references,
        )
        # The first blocker of a key keeps its location (#1134).
        unique.setdefault(key, blocker)
    return tuple(
        sorted(
            unique.values(),
            key=lambda blocker: (
                blocker.account_index,
                blocker.kind.value,
                blocker.contracts,
                tuple(
                    (reference.account_index, reference.event_ordinal)
                    for reference in blocker.event_references
                ),
                tuple(
                    (
                        reference.account_index,
                        reference.statement_index,
                        reference.location.page,
                        reference.location.section.value,
                        reference.location.row,
                    )
                    for reference in blocker.position_references
                ),
            ),
        )
    )


def reconstruct_robinhood_options(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    financial_year_start: date,
    financial_year_end: date,
) -> RobinhoodOptionReconstruction:
    """Reconstruct supported long Calls, every blocker and the FY tax evidence
    in USD. Needs no FX: Schedule FA runs this step alone (#1145)."""

    with localcontext(_ARITHMETIC_CONTEXT):
        _validate_inputs(
            paired,
            config,
            financial_year_start,
            financial_year_end,
        )
        trades, trade_blockers = _source_trades(paired)
        tax_trades = tuple(
            trade
            for trade in trades
            if not _unsupported_contract(
                trade_blockers,
                trade.contract.account_index,
                trade.contract,
            )
        )
        anchors, anchor_blockers = _valuation_anchors(paired)
        position_blockers = _position_blockers(paired, trades, anchors)
        slices, open_lots, lot_blockers = _match_lots(
            paired,
            trades,
            financial_year_start=financial_year_start,
            financial_year_end=financial_year_end,
        )
        found = (
            *trade_blockers,
            *anchor_blockers,
            *position_blockers,
            *lot_blockers,
        )
        classifications, missing = _classification_evidence(
            paired,
            config,
            tax_trades,
            financial_year_start,
            financial_year_end,
        )
        blockers = _dedupe_blockers(found)
        capital_slices, business_totals, tax_blockers = _tax_evidence(
            trades,
            slices,
            classifications,
            blockers,
            financial_year_end=financial_year_end,
        )
        blockers = _dedupe_blockers((*blockers, *tax_blockers))
        if missing:
            # The classified accounts' treatment blockers are known by now (#1569).
            raise _classification_refusal(missing, financial_year_start.year, blockers)
        return RobinhoodOptionReconstruction(
            financial_year_end=financial_year_end,
            trades=trades,
            lot_slices=slices,
            open_lots=open_lots,
            valuation_anchors=anchors,
            classifications=classifications,
            capital_gain_slices=capital_slices,
            business_totals=business_totals,
            blockers=blockers,
            excluded_out_of_fy_close_references=tuple(
                item.close_references for item in slices if not item.in_financial_year
            ),
            a2_gross_credit_references=tuple(
                trade.event_reference
                for trade in trades
                if trade.kind is RobinhoodEventKind.OPTION_SELL_TO_CLOSE
            ),
        )


def convert_robinhood_options(
    reconstruction: RobinhoodOptionReconstruction,
    *,
    capital_gains_fx_policy: AuditedFxPolicy,
    business_fx_policy: AuditedFxPolicy,
) -> RobinhoodOptionsResult:
    """Convert the reconstruction's capital-gains slices and business totals to
    INR at the FY rates: the one option step that needs FX (#1145)."""

    if capital_gains_fx_policy.currency != "USD" or business_fx_policy.currency != "USD":
        raise _fail("Robinhood option FX policies must convert USD")
    with localcontext(_ARITHMETIC_CONTEXT):
        return RobinhoodOptionsResult(
            **{
                field.name: getattr(reconstruction, field.name)
                for field in fields(RobinhoodOptionReconstruction)
            },
            capital_gain_lines=tuple(
                _capital_gain_line(item, capital_gains_fx_policy)
                for item in reconstruction.capital_gain_slices
            ),
            business_summaries=tuple(
                _business_summary(item, business_fx_policy, reconstruction.financial_year_end)
                for item in reconstruction.business_totals
            ),
        )


def compute_robinhood_options(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    financial_year_start: date,
    financial_year_end: date,
    capital_gains_fx_policy: AuditedFxPolicy,
    business_fx_policy: AuditedFxPolicy,
) -> RobinhoodOptionsResult:
    """Reconstruct supported long Calls, then convert their FY tax evidence to
    INR: the reconstruction needs no FX, the conversion needs both policies."""

    return convert_robinhood_options(
        reconstruct_robinhood_options(
            paired,
            config=config,
            financial_year_start=financial_year_start,
            financial_year_end=financial_year_end,
        ),
        capital_gains_fx_policy=capital_gains_fx_policy,
        business_fx_policy=business_fx_policy,
    )
