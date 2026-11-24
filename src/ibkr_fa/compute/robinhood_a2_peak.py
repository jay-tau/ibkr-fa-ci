"""Compute synchronized daily Robinhood Schedule FA A2 account peaks.

Every daily cash, equity and option ledger, the statement checks against
them and each day's account value are exact (#1267): their sums, differences
and products are :func:`~ibkr_fa.compute.exact_inr.checked_addition`,
:func:`~ibkr_fa.compute.exact_inr.checked_subtraction`,
:func:`~ibkr_fa.compute.exact_inr.checked_sum` and
:func:`~ibkr_fa.compute.exact_inr.checked_product`, so the peak day and its
filed rupee follow the exact amounts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ibkr_fa.compute.exact_inr import (
    checked_addition,
    checked_product,
    checked_subtraction,
    checked_sum,
)
from ibkr_fa.compute.fa_peak import PeakSelector, warn_if_inr_peak_currency
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionContract,
    RobinhoodOptionReconstruction,
    RobinhoodOptionValuationAnchor,
    option_blocker_summary,
    signed_option_quantity,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import RobinhoodAccountConfig, RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountReconciliation,
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import (
    robinhood_statement_signed_amount,
)
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.prices.series import MassiveOptionCloseSeries, PriceSeries
from ibkr_fa.reference.us_market_calendar import (
    supports_calendar_year,
    us_market_session_on_or_before,
)
from ibkr_fa.types import PeakCurrency, Ticker

_CASH = "Robinhood A2 peak, daily cash USD"
_STATEMENT_CASH = "Robinhood A2 peak, statement cash USD"
_EQUITY_QUANTITY = "Robinhood A2 peak, equity quantity"
_OPTION_QUANTITY = "Robinhood A2 peak, option quantity"
_ACCOUNT_VALUE = "Robinhood A2 peak, account value USD"
_NO_IDENTITY = "Robinhood equity inventory has no configured identity"


def _option_quantity_sum(quantities: Iterable[Decimal]) -> Decimal:
    return checked_sum(enumerate(quantities), at=lambda _: _OPTION_QUANTITY)


class RobinhoodA2PeakError(IbkrFaError):
    """The daily brokerage-account peak cannot be proved."""


class RobinhoodA2PeakIdentityError(RobinhoodA2PeakError):
    """A held security has no configured identity, so its closes can't be
    looked up; the pipeline adds the identity guidance (#1567)."""


@dataclass(frozen=True, slots=True)
class RobinhoodA2Peak:
    """One account's selected peak and TTBR conversion."""

    account_index: int
    peak_date: date
    peak_usd: Decimal
    ttbr: Decimal
    peak_inr: Decimal


@dataclass(frozen=True, slots=True)
class RobinhoodAccountEquityReplay:
    """Trade-date equity quantities and exact required market sessions."""

    starting_quantities: Mapping[str, Decimal]
    daily_quantities: Mapping[date, Mapping[str, Decimal]]
    required_sessions: Mapping[str, frozenset[date]]


@dataclass(frozen=True, slots=True)
class RobinhoodAccountOptionReplay:
    """Trade-date option quantities held each day and exact required market sessions."""

    daily_quantities: Mapping[date, Mapping[RobinhoodOptionContract, Decimal]]
    required_sessions: Mapping[RobinhoodOptionContract, frozenset[date]]


def _configured_accounts(
    paired: RobinhoodAccountsReconciliation,
    config: RobinhoodConfig,
) -> Iterator[tuple[int, RobinhoodAccountReconciliation, RobinhoodAccountConfig]]:
    """Each reconciled account with its index and configured account, in
    order; refuses when it reaches an account no configured number matches."""
    config_by_number = {account.number: account for account in config.accounts}
    for account_index, account in enumerate(paired.accounts):
        configured = config_by_number.get(account.activity.primary_bundle.account_number)
        if configured is None:
            raise RobinhoodA2PeakError("Robinhood account config does not match statements")
        yield account_index, account, configured


def _require_supported_calendar_year(calendar_year: int) -> None:
    if not supports_calendar_year(calendar_year):
        raise RobinhoodA2PeakError(
            f"Robinhood daily A2 pricing does not support calendar year {calendar_year}"
        )


def _equity_price(series: PriceSeries, ticker: Ticker, day: date) -> Decimal:
    if series.ticker != ticker or series.currency != "USD" or not series.split_safe:
        raise RobinhoodA2PeakError("Robinhood equity price identity, currency, or basis is invalid")
    session = us_market_session_on_or_before(day)
    price = series.closes.get(session)
    if price is None:
        raise RobinhoodA2PeakError("Robinhood equity close coverage is incomplete")
    return price


def _option_price(
    series: MassiveOptionCloseSeries,
    contract: RobinhoodOptionContract,
    day: date,
) -> Decimal:
    if (
        series.underlying != contract.symbol
        or series.expiration != contract.expiration
        or series.strike_usd != contract.strike_usd
        or series.multiplier != contract.multiplier
    ):
        raise RobinhoodA2PeakError("Robinhood option price identity is invalid")
    session = us_market_session_on_or_before(day)
    bar = series.bars.get(session)
    if bar is None:
        raise RobinhoodA2PeakError("Robinhood option close coverage is incomplete")
    return bar.close_usd


def _option_anchor_trade_quantity(
    options: RobinhoodOptionReconstruction,
    anchor: RobinhoodOptionValuationAnchor,
) -> Decimal:
    """Validate one option statement snapshot against trade/settlement ledgers."""

    relevant = tuple(trade for trade in options.trades if trade.contract == anchor.contract)
    trade_quantity = _option_quantity_sum(
        signed_option_quantity(trade)
        for trade in relevant
        if trade.activity_date <= anchor.statement_date
    )
    settlement_quantity = _option_quantity_sum(
        signed_option_quantity(trade)
        for trade in relevant
        if trade.settle_date <= anchor.statement_date
    )
    if trade_quantity == settlement_quantity:
        if anchor.quantity != trade_quantity:
            raise RobinhoodA2PeakError("Robinhood daily option inventory misses a statement")
        return trade_quantity
    pending_delta = _option_quantity_sum(
        signed_option_quantity(trade)
        for trade in relevant
        if trade.activity_date <= anchor.statement_date < trade.settle_date
    )
    if checked_subtraction(
        trade_quantity, settlement_quantity, at=_OPTION_QUANTITY
    ) != pending_delta or (anchor.quantity == trade_quantity) == (
        anchor.quantity == settlement_quantity
    ):
        raise RobinhoodA2PeakError("Robinhood option statement snapshot basis is ambiguous")
    return trade_quantity


def _statement_only_crypto_cash(
    account: RobinhoodAccountReconciliation,
) -> dict[date, Decimal]:
    """Brokerage-cash movements printed only as statement ``COIN`` rows."""

    result: dict[date, Decimal] = defaultdict(Decimal)
    for item in account.activity.crypto_rows:
        statement = account.activity.primary_bundle.statements[
            item.statement_reference.statement_index
        ]
        row = next(
            (
                candidate
                for candidate in statement.activities
                if candidate.location == item.statement_reference.location
            ),
            None,
        )
        if row is None:
            raise RobinhoodA2PeakError("statement-only Crypto cash row does not resolve")
        amount = robinhood_statement_signed_amount(row)
        if amount is None:
            raise RobinhoodA2PeakError("statement-only Crypto cash row lacks an amount")
        day = row.settle_date or row.transaction_date
        result[day] = checked_addition(result[day], amount, at=_CASH)
    return result


def replay_robinhood_account_equities(
    *,
    account_index: int,
    account: RobinhoodAccountReconciliation,
    quantities: RobinhoodSecurityQuantityResult,
    start: date,
    end: date,
) -> RobinhoodAccountEquityReplay:
    """Replay one account's trade-date equity ledger once for pricing and audit."""

    deltas: dict[date, list[tuple[str, Decimal]]] = defaultdict(list)
    for movement in quantities.movements:
        if movement.account_index == account_index:
            deltas[movement.activity_date].append((movement.cusip, movement.signed_quantity))
    for residual in quantities.residuals:
        if residual.account_index == account_index:
            deltas[residual.activity_date].append((residual.cusip, residual.quantity))

    anchors: dict[date, list[tuple[str, Decimal, Decimal]]] = defaultdict(list)
    for anchor in quantities.anchors:
        if anchor.account_index != account_index:
            continue
        statement = account.activity.primary_bundle.statements[anchor.statement_index]
        anchors[statement.period_end].append(
            (anchor.cusip, anchor.trade_date_quantity, anchor.tolerance)
        )

    ledger: dict[str, Decimal] = defaultdict(Decimal)

    def apply(day: date) -> None:
        net: dict[str, Decimal] = defaultdict(Decimal)
        for cusip, delta in deltas.get(day, ()):
            net[cusip] = checked_addition(net[cusip], delta, at=_EQUITY_QUANTITY)
        for cusip, delta in net.items():
            ledger[cusip] = checked_addition(ledger[cusip], delta, at=_EQUITY_QUANTITY)
            if ledger[cusip] < 0:
                raise RobinhoodA2PeakError("Robinhood daily equity inventory became negative")
        for cusip, trade_date_quantity, tolerance in anchors.get(day, ()):
            difference = checked_subtraction(
                ledger[cusip], trade_date_quantity, at=_EQUITY_QUANTITY
            )
            if difference.copy_abs() > tolerance:
                raise RobinhoodA2PeakError("Robinhood daily equity inventory misses a statement")
            ledger[cusip] = trade_date_quantity

    for day in sorted({*deltas, *anchors}):
        if day < start:
            apply(day)
    starting = dict(ledger)
    daily: dict[date, Mapping[str, Decimal]] = {}
    required: dict[str, set[date]] = defaultdict(set)
    day = start
    while day <= end:
        before = dict(ledger)
        apply(day)
        for cusip in set(before) | set(ledger):
            if before.get(cusip, Decimal()) != 0 or ledger.get(cusip, Decimal()) != 0:
                required[cusip].add(us_market_session_on_or_before(day))
        daily[day] = {cusip: value for cusip, value in ledger.items() if value != 0}
        day += timedelta(days=1)
    return RobinhoodAccountEquityReplay(
        starting_quantities=starting,
        daily_quantities=daily,
        required_sessions={cusip: frozenset(days) for cusip, days in required.items()},
    )


def robinhood_required_equity_sessions(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    quantities: RobinhoodSecurityQuantityResult,
    calendar_year: int,
) -> dict[Ticker, frozenset[date]]:
    """Return exact price sessions required by every account's A2 equity replay."""

    _require_supported_calendar_year(calendar_year)
    security_by_cusip = {security.cusip: security for security in securities.securities}
    if len(security_by_cusip) != len(securities.securities):
        raise RobinhoodA2PeakError("Robinhood security CUSIPs are duplicated")
    required: dict[Ticker, set[date]] = defaultdict(set)
    for account_index, account, configured in _configured_accounts(paired, config):
        replay = replay_robinhood_account_equities(
            account_index=account_index,
            account=account,
            quantities=quantities,
            start=max(date(calendar_year, 1, 1), configured.opening_date),
            end=date(calendar_year, 12, 31),
        )
        for cusip, sessions in replay.required_sessions.items():
            security = security_by_cusip.get(cusip)
            if security is None:
                raise RobinhoodA2PeakIdentityError(_NO_IDENTITY)
            required[security.instrument.symbol].update(sessions)
    return {ticker: frozenset(sessions) for ticker, sessions in required.items()}


def replay_robinhood_account_options(
    *,
    account_index: int,
    options: RobinhoodOptionReconstruction,
    start: date,
    end: date,
) -> RobinhoodAccountOptionReplay:
    """Replay one account's trade-date option ledger once for pricing and audit.

    Each day holds only unexpired contracts with a non-zero end-of-day quantity.
    """

    deltas: dict[date, list[tuple[RobinhoodOptionContract, Decimal]]] = defaultdict(list)
    for trade in options.trades:
        if trade.contract.account_index == account_index:
            deltas[trade.activity_date].append((trade.contract, signed_option_quantity(trade)))

    anchors: dict[date, list[tuple[RobinhoodOptionContract, Decimal]]] = defaultdict(list)
    for anchor in options.validated_valuation_anchors:
        if anchor.contract.account_index == account_index:
            anchors[anchor.statement_date].append(
                (anchor.contract, _option_anchor_trade_quantity(options, anchor))
            )

    ledger: dict[RobinhoodOptionContract, Decimal] = defaultdict(Decimal)

    def apply(day: date) -> None:
        net: dict[RobinhoodOptionContract, Decimal] = defaultdict(Decimal)
        for contract, delta in deltas.get(day, ()):
            net[contract] = checked_addition(net[contract], delta, at=_OPTION_QUANTITY)
        for contract, delta in net.items():
            ledger[contract] = checked_addition(ledger[contract], delta, at=_OPTION_QUANTITY)
            if ledger[contract] < 0:
                raise RobinhoodA2PeakError("Robinhood daily option inventory became negative")
        for contract, statement_quantity in anchors.get(day, ()):
            if ledger[contract] != statement_quantity:
                raise RobinhoodA2PeakError("Robinhood daily option inventory misses a statement")
            ledger[contract] = statement_quantity

    for day in sorted({*deltas, *anchors}):
        if day < start:
            apply(day)
    daily: dict[date, Mapping[RobinhoodOptionContract, Decimal]] = {}
    required: dict[RobinhoodOptionContract, set[date]] = defaultdict(set)
    day = start
    while day <= end:
        apply(day)
        held = {
            contract: quantity
            for contract, quantity in ledger.items()
            if quantity != 0 and day <= contract.expiration
        }
        for contract in held:
            required[contract].add(us_market_session_on_or_before(day))
        daily[day] = held
        day += timedelta(days=1)
    return RobinhoodAccountOptionReplay(
        daily_quantities=daily,
        required_sessions={contract: frozenset(days) for contract, days in required.items()},
    )


def robinhood_required_option_sessions(
    options: RobinhoodOptionReconstruction,
    *,
    calendar_year: int,
) -> dict[RobinhoodOptionContract, frozenset[date]]:
    """Return exact close sessions required by every account's A2 option replay.

    The window starts on Jan 1, not the account opening date: no option is held
    before an account opens, so the sessions are the ones the A2 pricing loop reads.
    """

    _require_supported_calendar_year(calendar_year)
    account_indexes = {
        *(trade.contract.account_index for trade in options.trades),
        *(anchor.contract.account_index for anchor in options.validated_valuation_anchors),
    }
    required: dict[RobinhoodOptionContract, frozenset[date]] = {}
    for account_index in sorted(account_indexes):
        replay = replay_robinhood_account_options(
            account_index=account_index,
            options=options,
            start=date(calendar_year, 1, 1),
            end=date(calendar_year, 12, 31),
        )
        required.update(replay.required_sessions)
    return required


def robinhood_a2_peak_window(
    configured: RobinhoodAccountConfig, calendar_year: int
) -> tuple[date, date]:
    """The first and last day of an account's A2 peak: 1 January, or the
    account's opening date, to 31 December. Refuses a calendar year the
    pricing calendar doesn't cover and an account that opens after the year:
    no evidence clears either, so Schedule FA checks them before naming any
    blocker it decides without prices (#1574)."""
    _require_supported_calendar_year(calendar_year)
    start = max(date(calendar_year, 1, 1), configured.opening_date)
    end = date(calendar_year, 12, 31)
    if start > end:
        raise RobinhoodA2PeakError("Robinhood account opens after the Schedule FA year")
    return start, end


def _compute_account_peak(  # noqa: PLR0912, PLR0915
    *,
    account_index: int,
    account: RobinhoodAccountReconciliation,
    configured: RobinhoodAccountConfig,
    securities: tuple[RobinhoodSecurityConfig, ...],
    quantities: RobinhoodSecurityQuantityResult,
    options: RobinhoodOptionReconstruction,
    equity_prices: Mapping[Ticker, PriceSeries],
    option_prices: Mapping[RobinhoodOptionContract, MassiveOptionCloseSeries],
    calendar_year: int,
    fx_policy: FxPolicy,
    peak_currency: PeakCurrency,
) -> RobinhoodA2Peak:
    start, end = robinhood_a2_peak_window(configured, calendar_year)

    cash_deltas: dict[date, Decimal] = defaultdict(Decimal)
    for event in account.evidence.events:
        if event.amount_usd is not None:
            cash_deltas[event.activity_date] = checked_addition(
                cash_deltas[event.activity_date], event.amount_usd, at=_CASH
            )
    for day, amount in _statement_only_crypto_cash(account).items():
        cash_deltas[day] = checked_addition(cash_deltas[day], amount, at=_CASH)
    option_replay = replay_robinhood_account_options(
        account_index=account_index,
        options=options,
        start=start,
        end=end,
    )

    cash_anchors: dict[date, Decimal] = {}
    for statement, period in zip(
        account.activity.primary_bundle.statements,
        account.cash.periods,
        strict=True,
    ):
        cash_anchors[statement.period_end] = checked_sum(
            enumerate(
                (
                    statement.account_summary.closing_brokerage_cash_usd,
                    statement.account_summary.closing_deposit_sweep_usd,
                    period.closing_unsettled_usd,
                )
            ),
            at=lambda _: _STATEMENT_CASH,
        )

    security_by_cusip = {security.cusip: security for security in securities}
    if len(security_by_cusip) != len(securities):
        raise RobinhoodA2PeakError("Robinhood security CUSIPs are duplicated")

    cash = Decimal()
    equity_replay = replay_robinhood_account_equities(
        account_index=account_index,
        account=account,
        quantities=quantities,
        start=start,
        end=end,
    )

    def apply(day: date) -> None:
        nonlocal cash
        cash = checked_addition(cash, cash_deltas.get(day, Decimal()), at=_CASH)
        expected_cash = cash_anchors.get(day)
        if expected_cash is not None and cash != expected_cash:
            raise RobinhoodA2PeakError("Robinhood daily cash misses a statement")

    for day in sorted({*cash_deltas, *cash_anchors}):
        if day < start:
            apply(day)

    selector = PeakSelector(fx_policy=fx_policy, peak_currency=peak_currency)
    previous_equity = dict(equity_replay.starting_quantities)
    day = start
    while day <= end:
        apply(day)
        equity = equity_replay.daily_quantities[day]
        for cusip, previous_quantity in previous_equity.items():
            if previous_quantity == 0:
                continue
            security = security_by_cusip.get(cusip)
            if security is None:
                raise RobinhoodA2PeakError(_NO_IDENTITY)
            series = equity_prices.get(security.instrument.symbol)
            if series is not None and day in series.split_ratios:
                raise RobinhoodA2PeakError(
                    "Robinhood in-year split quantity effects are unsupported"
                )
        total = cash
        for cusip, quantity in equity.items():
            if quantity == 0:
                continue
            security = security_by_cusip.get(cusip)
            if security is None:
                raise RobinhoodA2PeakError(_NO_IDENTITY)
            ticker = security.instrument.symbol
            series = equity_prices.get(ticker)
            if series is None:
                raise RobinhoodA2PeakError("Robinhood equity price cache is incomplete")
            value = checked_product(quantity, _equity_price(series, ticker, day), at=_ACCOUNT_VALUE)
            total = checked_addition(total, value, at=_ACCOUNT_VALUE)
        for contract, quantity in option_replay.daily_quantities[day].items():
            series = option_prices.get(contract)
            if series is None:
                raise RobinhoodA2PeakError("Robinhood option price cache is incomplete")
            value = checked_product(
                checked_product(quantity, contract.multiplier, at=_ACCOUNT_VALUE),
                _option_price(series, contract, day),
                at=_ACCOUNT_VALUE,
            )
            total = checked_addition(total, value, at=_ACCOUNT_VALUE)
        selector.offer(day, total)
        previous_equity = dict(equity)
        day += timedelta(days=1)

    peak = selector.result()
    assert peak is not None
    return RobinhoodA2Peak(
        account_index=account_index,
        peak_date=peak.on_date,
        peak_usd=peak.value_native,
        ttbr=peak.ttbr,
        peak_inr=peak.value_inr,
    )


def compute_robinhood_a2_peaks(
    paired: RobinhoodAccountsReconciliation,
    *,
    config: RobinhoodConfig,
    securities: RobinhoodSecurityIdentities,
    quantities: RobinhoodSecurityQuantityResult,
    options: RobinhoodOptionReconstruction,
    equity_prices: Mapping[Ticker, PriceSeries],
    option_prices: Mapping[RobinhoodOptionContract, MassiveOptionCloseSeries],
    calendar_year: int,
    fx_policy: FxPolicy,
    peak_currency: PeakCurrency = "native",
) -> tuple[RobinhoodA2Peak, ...]:
    """Compute every account peak or refuse the entire Schedule FA leg."""

    warn_if_inr_peak_currency(peak_currency)
    _require_supported_calendar_year(calendar_year)
    if quantities.blockers:
        raise RobinhoodA2PeakError("Robinhood equity inventory evidence is incomplete")
    if options.blockers:
        raise RobinhoodA2PeakError(
            "Robinhood option blockers prevent the A2 peak: "
            f"{option_blocker_summary(options.blockers)}"
        )
    result: list[RobinhoodA2Peak] = []
    for account_index, account, configured in _configured_accounts(paired, config):
        result.append(
            _compute_account_peak(
                account_index=account_index,
                account=account,
                configured=configured,
                securities=securities.securities,
                quantities=quantities,
                options=options,
                equity_prices=equity_prices,
                option_prices=option_prices,
                calendar_year=calendar_year,
                fx_policy=fx_policy,
                peak_currency=peak_currency,
            )
        )
    return tuple(result)
