from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import ibkr_fa.pipeline_robinhood_option_prices as _option_prices
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.form67 import form67_override_advice
from ibkr_fa.compute.income_engine import (
    IncomeComputeResult,
    compute_income_schedules,
    compute_schedule_os_result,
)
from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsResult,
    blocker_reasons,
    compute_robinhood_capital_gains,
    resolved_filing_reasons,
)
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodFaBlocker,
    RobinhoodFaBlockerKind,
    RobinhoodFaResult,
    compute_robinhood_fa,
    fa_blocker_reasons,
    inventory_blocker_reasons,
    option_classification_blockers,
    robinhood_fa_pre_price_blockers,
)
from ibkr_fa.compute.robinhood_income import (
    SUBSTITUTE_PAYMENT_CODES,
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
    compute_robinhood_income,
)
from ibkr_fa.compute.robinhood_options import (
    ROBINHOOD_FNO_FEMA_WARNING,
    RobinhoodOptionBlocker,
    RobinhoodOptionBlockerKind,
    RobinhoodOptionClassificationError,
    RobinhoodOptionContract,
    RobinhoodOptionReconstruction,
    RobinhoodOptionsResult,
    compute_robinhood_option_business_fsi,
    compute_robinhood_options,
    option_blocker_summary,
    option_itr_form_refusal,
    reconstruct_robinhood_options,
)
from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.compute.schedule_tr import (
    ScheduleTrComputeResult,
    compute_schedule_tr_from_fsi,
)
from ibkr_fa.exceptions import IbkrFaError, raise_replacing, relayed_failure
from ibkr_fa.fx.context import CROSS_AY_FX_WARNING_STEP, TaxFxContext
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.invariants import require
from ibkr_fa.normalize.robinhood.accounts import account_name
from ibkr_fa.normalize.robinhood.bundle import RobinhoodReconciliationError
from ibkr_fa.normalize.robinhood.coverage import EVIDENCE_PERIOD_REMEDY, REPORT_DATES
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.output.schedules.form67 import portal_import_warnings
from ibkr_fa.parser.robinhood import (
    ROBINHOOD_EQUITY_SALE_KINDS,
    ROBINHOOD_INCOME_AND_FEE_KINDS,
    ROBINHOOD_OPTION_TRADE_KINDS,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood_bundle import (
    RobinhoodBundle,
    address_verification_guidance,
    identity_failure_guidance,
    load_robinhood_bundle,
    resolved_robinhood_quantities,
)
from ibkr_fa.pipeline_robinhood_equity_prices import (
    load_robinhood_equity_prices as price_series,
)
from ibkr_fa.pipeline_robinhood_models import RobinhoodEquityPriceAudit
from ibkr_fa.prices.series import MassiveOptionCloseSeries, PriceSeries
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.types import Ticker


class RobinhoodPipelineError(IbkrFaError):
    pass


class RobinhoodItrFormError(RobinhoodPipelineError):
    pass


# How a relay names an ``OSError`` from a compute step: no path the user typed
# is known there (#1044).
_FILE_FAILURE = "a Robinhood file operation failed"


class _FnoWarningOnError:
    # A class, not a contextlib.contextmanager generator: a generator
    # re-raising an exception assigns its __traceback__, which fails with
    # TypeError on this package's frozen dataclass exceptions.
    def __init__(self, has_options: bool) -> None:
        self._has_options = has_options

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if (
            not isinstance(exc, Exception)
            or not self._has_options
            or (isinstance(exc, IbkrFaError) and ROBINHOOD_FNO_FEMA_WARNING in str(exc))
        ):
            return
        failure = relayed_failure(exc, _FILE_FAILURE)
        raise_replacing(RobinhoodPipelineError(f"{ROBINHOOD_FNO_FEMA_WARNING} {failure}"), exc)


def fno_warning_on_error(has_options: bool) -> AbstractContextManager[None]:
    """Prefix the F&O warning to errors raised in the body when ``has_options``.

    The error's text is relayed only for an :class:`IbkrFaError`; any other
    error is named by fixed text (:func:`relayed_failure`). The prefixed error
    replaces it and keeps none of it
    (:func:`~ibkr_fa.exceptions.raise_replacing`).
    """
    return _FnoWarningOnError(has_options)


@dataclass(frozen=True, slots=True, repr=False)
class ComputedRobinhood:
    options: RobinhoodOptionReconstruction | None = None
    cg: RobinhoodCapitalGainsResult | None = None
    income: RobinhoodIncomeResult | None = None


@dataclass(frozen=True, slots=True)
class RobinhoodComputeHooks:
    options: Callable[..., RobinhoodOptionsResult]
    cg: Callable[..., RobinhoodCapitalGainsResult]
    income: Callable[
        ...,
        tuple[
            RobinhoodIncomeResult,
            IncomeComputeResult | None,
            ScheduleTrComputeResult | None,
        ],
    ]
    prices: Callable[
        ...,
        tuple[
            dict[Ticker, PriceSeries],
            RobinhoodEquityPriceAudit,
            _option_prices.RobinhoodOptionPriceAudit,
            RobinhoodSecurityQuantityResult,
        ],
    ]
    fa: Callable[..., RobinhoodFaResult]
    validate: Callable[[RobinhoodBundle, ComputedRobinhood], None]
    inventory: Callable[..., RobinhoodSecurityQuantityResult]


type GuidanceWriter = Callable[[str], Path]
"""Writes a run's private identity guidance file once and returns its path."""

type RobinhoodAttemptResult = tuple[
    RobinhoodOptionsResult | None,
    RobinhoodCapitalGainsResult | None,
    RobinhoodIncomeResult | None,
    IncomeComputeResult | None,
    ScheduleTrComputeResult | None,
    RobinhoodFaResult | None,
    RobinhoodEquityPriceAudit | None,
    _option_prices.RobinhoodOptionPriceAudit | None,
    dict[str, str],
]


def fail(message: str) -> RobinhoodPipelineError:
    return RobinhoodPipelineError(message)


def _identity_guidance_line(bundle: RobinhoodBundle, write_guidance: GuidanceWriter) -> str | None:
    """Where to find the suggested ``user.toml`` blocks when a security is
    unconfigured, or ``None`` when none is.

    The blocks hold statement CUSIPs and symbols, so they go only to the
    private guidance file; the line names the file, never a value (#1044).
    """
    guidance = identity_failure_guidance(bundle)
    if not guidance:
        return None
    return (
        "the suggested user.toml blocks for the unconfigured securities are in "
        f"the private file {write_guidance(guidance)}: review the source evidence, "
        "replace every placeholder, and add them to user.toml"
    )


def _with_identity_guidance(
    reason: str, bundle: RobinhoodBundle, write_guidance: GuidanceWriter
) -> str:
    """``reason``, plus :func:`_identity_guidance_line` when a security is unconfigured."""
    line = _identity_guidance_line(bundle, write_guidance)
    return reason if line is None else f"{reason}\n{line}"


def with_receipt_identity_guidance[T](
    compute: Callable[[], T], bundle: RobinhoodBundle, write_guidance: GuidanceWriter
) -> T:
    """``compute()``. Its REC receipt-block prompt also gets
    :func:`_identity_guidance_line`, after a blank line: the receipt blocks
    don't clear an unconfigured security's stop (#1522).
    """
    try:
        return compute()
    except RobinhoodReconciliationError as exc:
        if not exc.receipt_resolution:
            raise
        refusal = exc
    line = _identity_guidance_line(bundle, write_guidance)
    if line is None:
        raise refusal
    # Raised after the handler, so it chains to nothing.
    raise RobinhoodReconciliationError(f"{refusal.message}\n\n{line}", receipt_resolution=True)


def unverified_address_reason(
    bundle: RobinhoodBundle, write_guidance: GuidanceWriter
) -> str | None:
    """Why Schedule FA stops when a held security's ``[[fa.entities]]`` entry
    has no address, or ``None`` when none does (#1449).

    Writes the run's private guidance file first: the identity section, if
    any, then the address section. The reason names the file, never a value
    (#1044).
    """
    addresses = address_verification_guidance(bundle)
    if not addresses:
        return None
    identity = identity_failure_guidance(bundle)
    path = write_guidance(f"{identity}\n{addresses}" if identity else addresses)
    # #1568: a block whose entry gets no nature names that key too.
    nature = (
        "set nature_of_entity to the issuer's legal entity type if the block has none, "
        if any(item.nature_missing for item in bundle.unverified_entity_addresses)
        else ""
    )
    return (
        "Robinhood Schedule FA was not written: one or more [[fa.entities]] entries for "
        "securities held in the calendar year have no address\n"
        "the address found for each, or a note that none was found, is in the private "
        f"file {path}: verify each address against the issuer's own filings, uncomment "
        f"its address and zip lines, {nature}replace that entry in user.toml with the "
        "block, and re-run"
    )


def cg_blocker_lines(
    bundle: RobinhoodBundle,
    result: RobinhoodCapitalGainsResult,
    write_guidance: GuidanceWriter,
) -> tuple[str, ...]:
    """``robinhood-cg``'s refusal for ``result``'s blockers, one line each:
    the fixed line, then the CG compute's line for each blocker kind present
    (#1540, #1546) and the identity guidance line where it applies.
    :func:`cg_blocker_reason` joins them."""
    lines = ["Robinhood equity capital gains contain unresolved blockers"]
    lines.extend(blocker_reasons(result.blockers))
    if any(blocker.source_identity_blockers for blocker in result.blockers) and (
        identity := _identity_guidance_line(bundle, write_guidance)
    ):
        lines.append(identity)
    return tuple(lines)


def cg_blocker_reason(
    bundle: RobinhoodBundle,
    result: RobinhoodCapitalGainsResult,
    write_guidance: GuidanceWriter,
) -> str:
    """:func:`cg_blocker_lines`, then the filing workbook's stops the resolved
    sales already show (#1570), which don't block robinhood-income."""
    return "\n".join(
        (*cg_blocker_lines(bundle, result, write_guidance), *resolved_filing_reasons(result))
    )


_FA_BLOCKERS = "Robinhood Schedule FA contains unresolved evidence blockers"


def _fa_blocker_text(blockers: tuple[RobinhoodFaBlocker, ...]) -> str:
    """The fixed line, then the FA compute's refusal lines (#1550)."""
    return "\n".join((_FA_BLOCKERS, *fa_blocker_reasons(blockers)))


def _with_fa_identity_guidance(
    reason: str,
    blockers: tuple[RobinhoodFaBlocker, ...],
    bundle: RobinhoodBundle,
    write_guidance: GuidanceWriter,
) -> str:
    """``reason``, plus the identity guidance when ``blockers`` has an ``a3-identity`` one."""
    if any(blocker.kind is RobinhoodFaBlockerKind.A3_IDENTITY for blocker in blockers):
        return _with_identity_guidance(reason, bundle, write_guidance)
    return reason


def fa_blocker_reason(
    bundle: RobinhoodBundle,
    result: RobinhoodFaResult,
    write_guidance: GuidanceWriter,
) -> str:
    """The fixed line, then the FA compute's refusal lines
    (:func:`fa_blocker_reasons`, #1550). With an ``a3-identity`` blocker,
    :func:`_identity_guidance_line` follows when a security is unconfigured."""
    return _with_fa_identity_guidance(
        _fa_blocker_text(result.blockers), result.blockers, bundle, write_guidance
    )


def fa_pre_price_blockers(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    options: RobinhoodOptionReconstruction,
    quantities: RobinhoodSecurityQuantityResult,
    income: RobinhoodIncomeResult | None,
) -> tuple[RobinhoodFaBlocker, ...]:
    """The Schedule FA blockers its compute decides without prices, for the
    inventory gate's ``quantities`` (:func:`robinhood_fa_pre_price_blockers`,
    #1574).

    Without ``income``, or when one of those steps refuses, the option
    classification blockers alone (#1567): the FA compute repeats the steps
    and raises the refusal after the prices, so the run stops where it did.
    """
    if income is not None:
        with suppress(IbkrFaError):
            return robinhood_fa_pre_price_blockers(
                bundle.paired,
                config=bundle.robinhood,
                securities=bundle.securities,
                income=income,
                options=options,
                calendar_year=financial_year.start_year,
                fx_policy=FaExactDatePolicy(
                    max_carry_forward_days=bundle.config.fx.max_carry_forward_days
                ),
                quantities=quantities,
            )
    return option_classification_blockers(options, financial_year.start_year)


def fa_pre_price_stop(
    options: RobinhoodOptionReconstruction, blockers: tuple[RobinhoodFaBlocker, ...]
) -> str | None:
    """Why Schedule FA stops before any price, or ``None``: every option
    blocker, since the A2 peak refuses each one, then the FA compute's line
    for each kind of ``blockers`` (#1567, #1574)."""
    reasons = []
    if options.blockers:
        reasons.append(
            "option blockers prevent complete Schedule FA: "
            f"{option_blocker_summary(options.blockers)}"
        )
    if blockers:
        reasons.append(_fa_blocker_text(blockers))
    return "; ".join(reasons) or None


def fa_option_stop(
    options: RobinhoodOptionReconstruction | None, financial_year: FinancialYear
) -> str | None:
    """:func:`fa_pre_price_stop` for ``options`` and their classification
    blockers alone (#1567), or ``None``. The pipelines name it after a
    refusal that comes before Schedule FA's other pre-price blockers are
    known."""
    if options is None:
        return None
    return fa_pre_price_stop(
        options, option_classification_blockers(options, financial_year.start_year)
    )


def fa_pre_price_refusal(
    options: RobinhoodOptionReconstruction,
    blockers: tuple[RobinhoodFaBlocker, ...],
    bundle: RobinhoodBundle,
    write_guidance: GuidanceWriter,
) -> str | None:
    """Schedule FA's refusal for :func:`fa_pre_price_stop`, or ``None``, with
    the identity guidance line for an ``a3-identity`` blocker, as
    :func:`fa_blocker_reason` adds it."""
    stop = fa_pre_price_stop(options, blockers)
    if stop is None:
        return None
    return _with_fa_identity_guidance(stop, blockers, bundle, write_guidance)


INTEREST_PAYER_GUIDANCE = (
    "Robinhood interest has no reviewed US payer: FSI, TR and Form 67 are refused "
    "until each account with interest in the financial year has one "
    "[[robinhood.interest_payer_countries]] entry with "
    'account_number = "<account number>", '
    "financial_year = <FY start year, e.g. 2025 for 2025-26>, "
    'country_iso2 = "US", '
    'interest_kinds = [<each kind it earned: "brokerage", "program-bank">], '
    'source = "<what you reviewed>", '
    'reason = "<why every payer is a US entity>"'
)

INTEREST_PARTITION_GUIDANCE = (
    "Robinhood interest isn't split into brokerage and program-bank interest: the "
    "statements don't settle which interest credits are which, so no "
    "[[robinhood.interest_payer_countries]] entry can clear this refusal; complete "
    "Schedule OS, Schedule FSI, Schedule TR and Form 67 by hand"
)


def interest_payer_reasons(income: RobinhoodIncomeResult | None) -> tuple[str, ...]:
    """Render the recipe for payer blockers outside ``partition_only_payer_accounts``.
    Those accounts' payer blockers arise only from the unresolved partition (#1477).
    """
    if income is not None and any(
        index not in income.partition_only_payer_accounts
        for blocker in income.blockers
        if blocker.kind is RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY
        for index in blocker.account_indices
    ):
        return (INTEREST_PAYER_GUIDANCE,)
    return ()


_Cause = RobinhoodIncomeBlockerCause
_UNRESOLVED_STATEMENT_LINE = (
    "a statement's Income summary {line} line (this period or year to date) disagrees with "
    "the activity report's {income} for that period, so ibkr-fa can't tell which is right"
)
# Each cause's problem, then the remedy a synthetic rerun proves clears it,
# or None when the line directs completion by hand (#1487).
_INCOME_BLOCKER_TEXT: dict[RobinhoodIncomeBlockerCause, tuple[str, str | None]] = {
    _Cause.ANNUAL_FORM_MISSING: (
        "no Form 1042-S was supplied for the listed account and year, and its income "
        "that year needs one",
        "Put the Form 1042-S Robinhood issued for it in the input's tax-forms folder and re-run",
    ),
    _Cause.ANNUAL_FORM_SET_ASIDE: (
        "the Form 1042-S supplied for the listed account and year failed its "
        "annual-form-identity check, so no form covers that year's income",
        None,
    ),
    _Cause.ANNUAL_FORM_DUPLICATED: (
        "more than one Form 1042-S was supplied for the listed account and year, and "
        "ibkr-fa can't tell which one applies",
        None,
    ),
    _Cause.ANNUAL_FORM_ACCOUNT_UNBOUND: (
        "a supplied Form 1042-S names neither configured Robinhood account, or names "
        "different accounts on its summary and form pages, so ibkr-fa can't tell whose "
        "form it is",
        None,
    ),
    _Cause.ANNUAL_FORM_PARTIES: (
        "a Form 1042-S doesn't name an India-resident recipient and a US withholding "
        "agent, or the account's statements name no registered holder, so the form "
        "can't be matched to this filer",
        None,
    ),
    _Cause.ANNUAL_FORMS_DISAGREE: (
        "the supplied Forms 1042-S have different recipient or withholding-agent details, "
        "so ibkr-fa can't reconcile their identities",
        None,
    ),
    _Cause.ANNUAL_FORM_SUMMARY: (
        "a Form 1042-S's summary page disagrees with its form pages, so ibkr-fa can't "
        "tell which figures apply",
        None,
    ),
    _Cause.ANNUAL_FORM_SOURCE: (
        "a Form 1042-S doesn't reconcile to the dividends, interest and Stock Lending "
        "income in the activity report for its year, so ibkr-fa can't tell which is right",
        None,
    ),
    _Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR: (
        "the account's activity report or statements don't cover the whole financial year",
        "Supply " + EVIDENCE_PERIOD_REMEDY.format(end="the end of the financial year"),
    ),
    _Cause.EVIDENCE_SHORT_OF_FORM_YEAR: (
        "the account's activity report or statements stop before 31 December of the "
        "listed year, whose Form 1042-S ibkr-fa must reconcile",
        "Supply an activity report and monthly statements through 31 December of that "
        "year, " + REPORT_DATES.format(dates="end_date") + ", and re-run",
    ),
    _Cause.UNCONFIGURED_SECURITY: (
        "a dividend or Stock Lending payment is in a security that has no "
        "[[robinhood.securities]] entry and wasn't identified automatically",
        "Add a [[robinhood.securities]] entry for it with symbol, cusip, isin, "
        "listing_exchange, sub_category, asset_class, issuer_country_code, "
        "is_depositary_receipt and source, and re-run",
    ),
    _Cause.UNMATCHED_SYMBOL: (
        "a dividend or Stock Lending payment's symbol matches no security the activity "
        "report or statements identify, so ibkr-fa can't tell which security paid it",
        None,
    ),
    _Cause.ISSUER_COUNTRY: (
        "a dividend's security has an issuer country that differs from its ISIN's "
        "country, so the dividend's source country is unproven",
        None,
    ),
    _Cause.US_WITHHOLDING_ON_NON_US_DIVIDEND: (
        "US NRA tax was withheld from a dividend whose issuer isn't in the US, which "
        "ibkr-fa can't explain",
        None,
    ),
    _Cause.LENDING_FEE_SOURCE: (
        "Stock Lending income in the financial year has no reviewed source country",
        "Add one [[robinhood.stock_lending_fee_fsi_sources]] entry for the account with "
        'account_number = "<account number>", '
        "financial_year = <FY start year, e.g. 2025 for 2025-26>, "
        'country_iso2 = "<two-letter code of the fee\'s source country, not IN>", '
        'source = "<what you reviewed>", '
        'reason = "<why that is the source country>" and re-run',
    ),
    _Cause.WITHHOLDING_WITHOUT_DIVIDEND: (
        "a tax-withholding row in the activity report matches no dividend, so ibkr-fa "
        "can't tell what it was withheld from",
        None,
    ),
    _Cause.WITHHOLDING_SEVERAL_DIVIDENDS: (
        "a tax-withholding row in the activity report matches more than one dividend, "
        "so ibkr-fa can't tell which it was withheld from",
        None,
    ),
    _Cause.DIVIDEND_SEVERAL_WITHHOLDINGS: (
        "more than one tax-withholding row in the activity report matches the same "
        "dividend, so ibkr-fa can't tell which applies",
        None,
    ),
    _Cause.STATEMENT_SPANS_YEARS: (
        "a statement's period crosses 31 December, so its Income summary can't be "
        "checked against one calendar year",
        None,
    ),
    _Cause.STATEMENT_DIVIDENDS: (
        _UNRESOLVED_STATEMENT_LINE.format(line="Dividends", income="dividends"),
        None,
    ),
    _Cause.STATEMENT_INTEREST: (
        _UNRESOLVED_STATEMENT_LINE.format(line="Interest Earned", income="interest"),
        None,
    ),
    _Cause.STATEMENT_STOCK_LENDING: (
        _UNRESOLVED_STATEMENT_LINE.format(line="Stock Lending", income="Stock Lending income"),
        None,
    ),
    _Cause.SUBSTITUTE_PAYMENT: (
        "a Form 1042-S reports a substitute payment (income code "
        f"{', '.join(SUBSTITUTE_PAYMENT_CODES[:-1])} or {SUBSTITUTE_PAYMENT_CODES[-1]}), "
        "so ibkr-fa can't tell substitute dividends from loan fees in the Stock Lending "
        "income",
        None,
    ),
    _Cause.UNSUPPORTED_FORM_CONTENT: (
        "a Form 1042-S has an income code, tax rate, amount or form condition "
        "ibkr-fa doesn't support",
        None,
    ),
    _Cause.CAPITAL_GAINS_DISTRIBUTIONS: (
        "a statement's Income summary shows Capital Gains Distributions, which ibkr-fa "
        "doesn't compute",
        None,
    ),
}
# Kinds whose blockers carry no cause: each has its own text above.
_OWN_TEXT_KINDS = frozenset(
    {
        RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY,
        RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION,
    }
)
_OS_SCHEDULES = "Schedule OS, Schedule FSI, Schedule TR and Form 67"
_FTC_SCHEDULES = "Schedule FSI, Schedule TR and Form 67"


def _joined(items: list[str]) -> str:
    """``items`` as an English list: ``a``, ``a and b``, ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _blocker_scope(accounts: set[int], years: set[int], account_labels: Mapping[int, str]) -> str:
    """`` (<accounts>, <years>)`` for a reason line, or "" when it has neither.
    Accounts go by their statement family, with the index when both share one,
    as Schedule OS names them."""
    names = [
        account_name(account_labels, index)
        if account_labels.get(index)
        else f"account index {index}"
        for index in sorted(accounts)
    ]
    parts = []
    if names:
        parts.append(f"{_joined(names)} account{'s' if len(names) > 1 else ''}")
    if years:
        parts.append(_joined([str(year) for year in sorted(years)]))
    return f" ({', '.join(parts)})" if parts else ""


def income_blocker_reasons(
    income: RobinhoodIncomeResult | None,
    account_labels: Mapping[int, str],
    *,
    schedule_os: bool,
) -> tuple[str, ...]:
    """One line per kind and cause of ``income``'s blockers that stop Schedule
    OS (``schedule_os``) or only FTC (#1487): the accounts and years it
    names, what is unresolved and what clears it, or which schedules to
    complete by hand. The partition and payer kinds keep their own texts.

    The Schedule OS refusal both commands print takes the first set; the FTC
    reasons take the second (see ``ftc_blocker_reasons``)."""
    if income is None:
        return ()
    scopes: dict[
        tuple[RobinhoodIncomeBlockerKind, RobinhoodIncomeBlockerCause],
        tuple[set[int], set[int]],
    ] = {}
    for blocker in income.blockers:
        if blocker.kind in _OWN_TEXT_KINDS or blocker.stops_schedule_os is not schedule_os:
            continue
        require(bool(blocker.causes), what="a Robinhood income blocker names no cause")
        for cause in blocker.causes:
            accounts, years = scopes.setdefault((blocker.kind, cause), (set(), set()))
            accounts.update(blocker.account_indices)
            years.update(blocker.tax_years)
    schedules = _OS_SCHEDULES if schedule_os else _FTC_SCHEDULES
    order = list(RobinhoodIncomeBlockerCause)
    reasons = []
    for (kind, cause), (accounts, years) in sorted(
        scopes.items(), key=lambda item: (item[0][0].value, order.index(item[0][1]))
    ):
        problem, remedy = _INCOME_BLOCKER_TEXT[cause]
        head = f"{kind.value}{_blocker_scope(accounts, years, account_labels)}: {problem}"
        reasons.append(
            f"{head}; complete {schedules} by hand"
            if remedy is None
            else f"{head}. {remedy}, or complete {schedules} by hand"
        )
    return tuple(reasons)


def ftc_blocker_reasons(
    income: RobinhoodIncomeResult | None, account_labels: Mapping[int, str]
) -> tuple[str, ...]:
    """The payer recipe, then the FTC-only cause lines. Each
    command prints them once: ``robinhood-all`` on its ``income-ftc`` leg,
    ``robinhood-income`` in its FTC report or, when Schedule OS refuses
    first, in that refusal (#1497)."""
    return (
        *interest_payer_reasons(income),
        *income_blocker_reasons(income, account_labels, schedule_os=False),
    )


CG_FTC_CAUSE = (
    "Schedule FSI, Schedule TR and Form 67 include the Robinhood equity capital gains, "
    "which are blocked as below; while they stay blocked, complete Schedule FSI, "
    "Schedule TR and Form 67 by hand"
)


def income_ftc_reasons(
    bundle: RobinhoodBundle,
    income: RobinhoodIncomeResult,
    cg: RobinhoodCapitalGainsResult,
    write_guidance: GuidanceWriter,
) -> tuple[str, ...]:
    """What stops ``robinhood-income``'s FTC schedules: when ``cg`` has
    blockers, :data:`CG_FTC_CAUSE` and ``robinhood-cg``'s lines for them
    (#1545), then :func:`ftc_blocker_reasons`."""
    return (
        *((CG_FTC_CAUSE, *cg_blocker_lines(bundle, cg, write_guidance)) if cg.blockers else ()),
        *ftc_blocker_reasons(income, bundle.paired.account_labels),
    )


def has_options(bundle: RobinhoodBundle) -> bool:
    """Whether ``bundle`` has any option event or position evidence."""
    quantities = bundle.quantities
    return bool(quantities.option_event_references or quantities.option_position_references)


def _require_itr_form(options: RobinhoodOptionReconstruction, itr_form: ItrForm) -> None:
    if options.required_itr_form == "ITR-3" and itr_form == "ITR-2":
        raise RobinhoodItrFormError(option_itr_form_refusal(options))


def reconstruct_options(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    itr_form: ItrForm,
) -> RobinhoodOptionReconstruction:
    """Schedule FA's option evidence: the reconstruction alone, with no FY FX (#1145)."""
    with fno_warning_on_error(has_options(bundle)):
        result = reconstruct_robinhood_options(
            bundle.paired,
            config=bundle.robinhood,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
        )
    _require_itr_form(result, itr_form)
    return result


def compute_options(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    itr_form: ItrForm,
) -> RobinhoodOptionsResult:
    fx_ctx = TaxFxContext.for_fy(financial_year, bundle.config.fx)
    cg_fx, business_fx = fx_ctx.capital_gains, fx_ctx.other_sources
    with fno_warning_on_error(has_options(bundle)):
        result = compute_robinhood_options(
            bundle.paired,
            config=bundle.robinhood,
            financial_year_start=financial_year.start_date,
            financial_year_end=financial_year.end_date,
            capital_gains_fx_policy=cg_fx,
            business_fx_policy=business_fx,
        )
    _require_itr_form(result, itr_form)
    return result


def compute_cg(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
) -> RobinhoodCapitalGainsResult:
    return compute_robinhood_capital_gains(
        bundle.paired,
        config=bundle.robinhood,
        securities=bundle.securities,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=TaxFxContext.for_fy(financial_year, bundle.config.fx).capital_gains,
        quantities=resolved_robinhood_quantities(bundle),
    )


def compute_source_income(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
) -> RobinhoodIncomeResult:
    """Normalize Robinhood source income, without building any FY schedule (#920)."""
    return compute_robinhood_income(
        bundle.paired,
        bundle.packets,
        config=bundle.robinhood,
        securities=bundle.securities,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
    )


def compute_income(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    cg: RobinhoodCapitalGainsResult,
    *,
    options: RobinhoodOptionsResult | None = None,
    suppress_cross_ay_warning: bool,
) -> tuple[RobinhoodIncomeResult, IncomeComputeResult | None, ScheduleTrComputeResult | None]:
    income = compute_source_income(bundle, financial_year)
    if income.blockers or cg.blockers:
        return income, None, None
    additional_fsi_results = (
        () if options is None else (compute_robinhood_option_business_fsi(options),)
    )
    interest_credits = list(income.interest_credits)
    assembled = compute_income_schedules(
        bundle.config,
        financial_year,
        fx_ctx=TaxFxContext.for_fy(
            financial_year,
            bundle.config.fx,
            suppress_cross_ay_warning=suppress_cross_ay_warning,
        ),
        dividends=list(income.dividends),
        withholding_taxes=list(income.withholding_taxes),
        closed_lots=[],
        interest_credits=interest_credits,
        # US only with interest: with no income blocker above, a reviewed US
        # [[robinhood.interest_payer_countries]] entry covers every one of
        # these credits (compute_robinhood_income).
        interest_payer_country_iso2="US" if interest_credits else None,
        substitute_dividend_incomes=income.substitute_dividend_incomes,
        other_source_incomes=income.filing_other_source_incomes,
        cg_stcg_result=cg.stcg,
        cg_ltcg_result=cg.ltcg,
        additional_fsi_results=additional_fsi_results,
    )
    return income, assembled, compute_schedule_tr_from_fsi(assembled.fsi_result)


def compute_os(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    income: RobinhoodIncomeResult,
    *,
    ftc_reasons: Callable[[], tuple[str, ...]] | None = None,
) -> ScheduleOsComputeResult:
    """Schedule OS, or a refusal naming what blocks it. With ``ftc_reasons``
    the refusal also names what blocks only FTC, as that call returns it:
    ``robinhood-income`` stops here, while ``robinhood-all`` names those on
    its ``income-ftc`` leg (#1497)."""
    if not income.schedule_os_complete:
        # An unresolved partition always blocks Schedule OS, so this refusal
        # names it in both robinhood-income and robinhood-all (#1477), with a
        # line for each other blocker that stops Schedule OS (#1487).
        partition = any(
            blocker.kind is RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION
            for blocker in income.blockers
        )
        labels = bundle.paired.account_labels
        raise fail(
            "\n".join(
                (
                    "Robinhood Schedule OS contains unresolved source evidence blockers",
                    *((INTEREST_PARTITION_GUIDANCE,) if partition else ()),
                    *income_blocker_reasons(income, labels, schedule_os=True),
                    *(() if ftc_reasons is None else ftc_reasons()),
                )
            )
        )
    return compute_schedule_os_result(
        bundle.config,
        financial_year,
        fx_ctx=TaxFxContext.for_fy(financial_year, bundle.config.fx),
        dividends=income.schedule_os_dividends,
        interest_credits=income.schedule_os_interest_credits,
        other_source_incomes=income.other_source_incomes,
    )[0]


def fa_inventory(
    bundle: RobinhoodBundle, *, write_guidance: GuidanceWriter
) -> RobinhoodSecurityQuantityResult:
    """Resolve inventory and preserve its refusal and private guidance before prices."""
    quantities = with_receipt_identity_guidance(
        lambda: resolved_robinhood_quantities(bundle), bundle, write_guidance
    )
    if quantities.blockers:
        reason = "\n".join(
            (
                "Robinhood equity inventory evidence is incomplete",
                *inventory_blocker_reasons(quantities.blockers),
            )
        )
        raise fail(_with_identity_guidance(reason, bundle, write_guidance))
    return quantities


def fa_prices(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    options: RobinhoodOptionReconstruction,
    input_dir: Path,
    config_path: Path,
    *,
    fetch_prices: bool,
    price_cache_dir: Path | None,
    price_fetcher: DailyCloseFetcher | None,
    write_guidance: GuidanceWriter,
) -> tuple[
    dict[Ticker, PriceSeries],
    RobinhoodEquityPriceAudit,
    _option_prices.RobinhoodOptionPriceAudit,
    RobinhoodSecurityQuantityResult,
]:
    quantities = fa_inventory(bundle, write_guidance=write_guidance)
    option_audit = _option_prices.collect_robinhood_option_price_audit(
        options=options,
        config=bundle.config,
        config_path=config_path,
        input_dir=input_dir,
        calendar_year=financial_year.start_year,
        fetch=fetch_prices,
        price_cache_dir=price_cache_dir,
    )
    if option_audit.errors:
        raise fail(option_audit.errors[0])
    prices, equity_audit = price_series(
        bundle,
        financial_year,
        input_dir,
        fetch_prices=fetch_prices,
        price_cache_dir=price_cache_dir,
        price_fetcher=price_fetcher,
        quantities=quantities,
    )
    if equity_audit.errors:
        reason = equity_audit.errors[0]
        if equity_audit.unconfigured_identity:
            reason = _with_identity_guidance(reason, bundle, write_guidance)
        raise fail(reason)
    return prices, equity_audit, option_audit, quantities


def compute_fa(
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    income: RobinhoodIncomeResult,
    options: RobinhoodOptionReconstruction,
    *,
    prices: dict[Ticker, PriceSeries],
    option_prices: dict[RobinhoodOptionContract, MassiveOptionCloseSeries],
    quantities: RobinhoodSecurityQuantityResult | None = None,
) -> RobinhoodFaResult:
    return compute_robinhood_fa(
        bundle.paired,
        config=bundle.robinhood,
        securities=bundle.securities,
        income=income,
        options=options,
        calendar_year=financial_year.start_year,
        fx_policy=FaExactDatePolicy(max_carry_forward_days=bundle.config.fx.max_carry_forward_days),
        prices_by_ticker=prices,
        option_prices_by_contract=option_prices,
        quantities=quantities or resolved_robinhood_quantities(bundle),
        entity_overrides=bundle.entity_overrides,
        nature_by_isin=bundle.nature_by_isin,
        peak_basis=bundle.config.schedule_fa.peak_basis,
        peak_currency=bundle.config.schedule_fa.peak_currency,
    )


def _then_option_stop(
    earlier: str, options: RobinhoodOptionReconstruction | None, financial_year: FinancialYear
) -> str:
    """``earlier``, then :func:`fa_option_stop` for ``options`` if there is one."""
    stop = fa_option_stop(options, financial_year)
    return earlier if stop is None else f"{earlier}; {stop}"


def attempt_compute(  # noqa: PLR0912
    bundle: RobinhoodBundle,
    financial_year: FinancialYear,
    itr_form: ItrForm,
    *,
    suppress_cross_ay_warning: bool,
    input_dir: Path,
    config_path: Path,
    fetch_prices: bool,
    price_cache_dir: Path | None,
    price_fetcher: DailyCloseFetcher | None,
    write_guidance: GuidanceWriter,
    hooks: RobinhoodComputeHooks | None = None,
    fa_refusal: str | None = None,
) -> RobinhoodAttemptResult:
    """Compute every Robinhood leg, recording each failed step's reason.

    A reason relays an :class:`IbkrFaError`'s text and names any other
    failure by fixed text (:func:`relayed_failure`), never its message.
    Given ``fa_refusal``, Schedule FA is neither priced nor computed, and
    that is its reason (#1449). With a stop before any price
    (:func:`fa_pre_price_refusal`) it runs only the inventory gate and the
    steps that need no price, as ``robinhood-fa`` does: it is neither priced
    nor computed, and the stop is its reason (#1560, #1567, #1574). Each of
    these reasons names any option stop after it (:func:`fa_option_stop`).
    """
    actual = hooks or RobinhoodComputeHooks(
        compute_options,
        compute_cg,
        compute_income,
        fa_prices,
        compute_fa,
        validate_owned_events,
        fa_inventory,
    )
    errors: dict[str, str] = {}
    options = cg = income = assembled = tr = fa = None
    equity_price_audit = option_price_audit = None
    try:
        options = actual.options(bundle, financial_year, itr_form)
        actual.validate(bundle, ComputedRobinhood(options=options))
    except (RobinhoodItrFormError, RobinhoodOptionClassificationError):
        raise
    except Exception as exc:
        errors["options"] = relayed_failure(exc, _FILE_FAILURE)
    try:
        cg = with_receipt_identity_guidance(
            lambda: actual.cg(bundle, financial_year), bundle, write_guidance
        )
        actual.validate(bundle, ComputedRobinhood(cg=cg))
    except Exception as exc:
        errors["cg"] = relayed_failure(exc, _FILE_FAILURE)
    if cg is not None:
        try:
            income, assembled, tr = actual.income(
                bundle,
                financial_year,
                cg,
                options=options,
                suppress_cross_ay_warning=suppress_cross_ay_warning,
            )
            actual.validate(bundle, ComputedRobinhood(cg=cg, income=income))
        except Exception as exc:
            errors["income"] = relayed_failure(exc, _FILE_FAILURE)
    else:
        errors["income"] = "equity CG failed, so FSI cannot reconcile capital gains"
    # A refusal that comes before Schedule FA's pre-price blockers are known
    # names any option stop after it (#1567).
    if fa_refusal is not None:
        errors["fa"] = _then_option_stop(fa_refusal, options, financial_year)
    elif options is not None and income is not None:
        stop_blockers = None
        try:
            # Before the prices: they can't clear a stop decided without them,
            # and their option replay can't explain an option blocker (#1550,
            # #1560, #1567, #1574). Inventory refusals and their guidance file
            # still come first.
            gated = actual.inventory(bundle, write_guidance=write_guidance)
            blockers = fa_pre_price_blockers(bundle, financial_year, options, gated, income)
            if options.blockers or blockers:
                stop_blockers = blockers
            else:
                prices, equity_price_audit, option_price_audit, quantities = actual.prices(
                    bundle,
                    financial_year,
                    options,
                    input_dir,
                    config_path=config_path,
                    fetch_prices=fetch_prices,
                    price_cache_dir=price_cache_dir,
                    price_fetcher=price_fetcher,
                    write_guidance=write_guidance,
                )
                fa = actual.fa(
                    bundle,
                    financial_year,
                    income,
                    options,
                    prices=prices,
                    option_prices=dict(option_price_audit.series_by_contract),
                    quantities=quantities,
                )
        except Exception as exc:
            errors["fa"] = _then_option_stop(
                relayed_failure(exc, _FILE_FAILURE), options, financial_year
            )
        # Outside the handler: a failed guidance write fails the run.
        if stop_blockers is not None and (
            refusal := fa_pre_price_refusal(options, stop_blockers, bundle, write_guidance)
        ):
            errors["fa"] = refusal
    else:
        errors["fa"] = _then_option_stop(
            "options or income failed, so Schedule FA credits are incomplete",
            options,
            financial_year,
        )
    return (
        options,
        cg,
        income,
        assembled,
        tr,
        fa,
        equity_price_audit,
        option_price_audit,
        errors,
    )


def validate_owned_events(
    bundle: RobinhoodBundle,
    computed: ComputedRobinhood,
) -> None:
    if computed.options is not None:
        expected = {
            (account_index, event.source_record_index)
            for account_index, account in enumerate(bundle.paired.accounts)
            for event in account.evidence.events
            if event.kind in ROBINHOOD_OPTION_TRADE_KINDS
        }
        actual = {
            (item.event_reference.account_index, item.event_reference.event_ordinal)
            for item in computed.options.trades
        }
        if actual != expected:
            raise fail("options layer does not cover every option event")
    if computed.cg is not None:
        expected = {
            (account_index, event.source_record_index)
            for account_index, account in enumerate(bundle.paired.accounts)
            for event in account.evidence.events
            if event.kind in ROBINHOOD_EQUITY_SALE_KINDS
        }
        actual = {
            (item.event_reference.account_index, item.event_reference.event_ordinal)
            for item in computed.cg.sale_price_checks
        }
        if actual != expected:
            raise fail("capital-gains layer does not cover every equity sale")
    if computed.income is not None:
        expected = {
            (account_index, event.source_record_index)
            for account_index, account in enumerate(bundle.paired.accounts)
            for event in account.evidence.events
            if event.kind in ROBINHOOD_INCOME_AND_FEE_KINDS
        }
        income = computed.income
        actual = {
            (item.event_reference.account_index, item.event_reference.event_ordinal)
            for item in (
                *income.dividend_payments,
                *income.interest_payments,
                *income.stock_lending_payments,
                *income.fee_payments,
                *income.withholding_payments,
            )
        }
        actual.update(
            (reference.account_index, reference.event_ordinal)
            for blocker in income.blockers
            for reference in blocker.event_references
        )
        if actual != expected:
            raise fail("income layer does not cover every income/fee event")


def hard_option_blockers(
    options: RobinhoodOptionReconstruction,
) -> tuple[RobinhoodOptionBlocker, ...]:
    manual = {
        RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
    }
    return tuple(item for item in options.blockers if item.kind not in manual)


def annual_form_warnings(income: RobinhoodIncomeResult) -> tuple[str, ...]:
    warnings = []
    discrepancy_count = len(income.annual_form_discrepancies)
    if discrepancy_count:
        warnings.append(
            "Robinhood Form 1042-S code 01 annual gross did not reconcile for "
            f"{discrepancy_count} account/year bucket(s); exact statement interest was used, "
            "and no foreign tax or relief was derived from the discrepant form bucket(s)."
        )
    pending = income.pending_annual_form_evidence
    if pending:
        warnings.append(
            "Robinhood annual forms were not supplied for "
            f"{len(pending)} FY-end account/year scope(s); exact statement income and "
            "withholding were used and should be checked against any later-issued forms."
        )
        withholding_count = sum(len(item.withholding_references) for item in pending)
        if withholding_count:
            warnings.append(
                "Form 67/TR evidence for "
                f"{withholding_count} FY-end withholding event(s) is the exact Robinhood "
                "statement debit rather than Form 1042-S; revise if a later form disagrees."
            )
        unpaired_count = sum(len(item.unpaired_us_dividend_references) for item in pending)
        if unpaired_count:
            warnings.append(
                f"{unpaired_count} FY-end US dividend event(s) had no paired withholding "
                "debit and are reported with zero foreign tax; verify late or zero withholding."
            )
    return tuple(warnings)


def income_warnings(assembled: IncomeComputeResult) -> tuple[str, ...]:
    entries = assembled.form67_result.entries
    warnings = list(
        portal_import_warnings(entries, override_advice=form67_override_advice(entries))
    )
    if not assembled.fx_policy_appropriate_for_ay:
        warnings.append(
            "Robinhood income FX policy is not independently verified for this assessment "
            f"year; {CROSS_AY_FX_WARNING_STEP}"
        )
    if not assembled.wht_fx_policy_appropriate_for_ay:
        warnings.append(
            "Robinhood withholding FX policy is not independently verified for this assessment "
            f"year; {CROSS_AY_FX_WARNING_STEP}"
        )
    return tuple(dict.fromkeys(warnings))


load_bundle = load_robinhood_bundle
