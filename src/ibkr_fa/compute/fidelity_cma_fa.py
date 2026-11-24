"""Reconstruct Fidelity CMA daily account-value bounds for Schedule FA A2.

The priced days run from the later of January 1 and the first statement's
start through December 31. Each uses the SPAXX NAV of the latest US market
session on or before it. A2 refuses a NAV dated on a non-session day from the
first day's seed session through December 31. It needs the NAV of each day
that starts with SPAXX or has a core transaction, and of December 31, refusing
when one is missing. Any other day holds no SPAXX from its start to its end, so
its NAV multiplies zero: it is used when the series has it and never required
(#1533). Only calendar years whose closures the US market calendar lists are
supported.

The free credit balance and SPAXX quantity ledgers, each day's value and
the Dec-31 account value are exact (#1267): their sums, differences and
products are :func:`~ibkr_fa.compute.exact_inr.checked_addition`,
:func:`~ibkr_fa.compute.exact_inr.checked_subtraction`,
:func:`~ibkr_fa.compute.exact_inr.checked_sum` and
:func:`~ibkr_fa.compute.exact_inr.checked_product`, so each bound's day and
filed rupee follow the exact amounts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
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
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.parser.fidelity_cma import (
    CmaCoreTransaction,
    CmaIncomeKind,
    CmaStatementBundle,
)
from ibkr_fa.prices.series import DEFAULT_MAX_PRICE_STALENESS_DAYS, PriceSeries
from ibkr_fa.reference.us_market_calendar import (
    is_us_market_session,
    supports_calendar_year,
    us_market_session_on_or_before,
)
from ibkr_fa.types import PeakCurrency

_ZERO = Decimal(0)
_FCB = "Fidelity CMA A2 peak, free credit balance USD"
_QUANTITY = "Fidelity CMA A2 peak, SPAXX quantity"
_ACCOUNT_VALUE = "Fidelity CMA A2 peak, account value USD"
_CLOSING_VALUE = "Fidelity CMA A2 closing, account value USD"
_BY_HAND = "enter the Fidelity CMA Schedule FA by hand"
# The NAV refusals' route (#1533, #1552): a fresh fetch replaces the cached series.
_MOVE_NAV_CACHE_ASIDE = (
    "Move SPAXX.csv, and any SPAXX.csv.lock beside it, out of the nasdaq-mutual-funds "
    "folder of the price cache directory (--price-cache-dir; by default the "
    ".ibkr-fa-price-cache folder in the --input directory), then re-run"
)


def _sum(amounts: Iterable[Decimal], *, at: str) -> Decimal:
    return checked_sum(enumerate(amounts), at=lambda _: at)


def _statements(numbers: list[int]) -> str:
    """``statement 2``, ``statements 1 and 2`` or ``statements 1, 2 and 5``."""
    *rest, last = numbers
    return f"statements {', '.join(map(str, rest))} and {last}" if rest else f"statement {last}"


def _value(fcb: Decimal, quantity: Decimal, nav: Decimal, *, at: str) -> Decimal:
    """The exact ``fcb + quantity * nav``."""
    return checked_addition(fcb, checked_product(quantity, nav, at=at), at=at)


class FidelityCmaFaError(IbkrFaError):
    """The CMA Schedule FA run cannot proceed, or its value be reconstructed, without guessing.

    :mod:`ibkr_fa.pipeline_fidelity_cma_fa` raises and re-exports this class (#1590).
    """


@dataclass(frozen=True, slots=True, repr=False)
class FidelityCmaPeakBounds:
    """Annual posting-order bounds plus exact Dec-31 account value and SPAXX NAV.

    Each bound is ``<bound>_usd`` on ``<bound>_date``, converted at that date's
    ``<bound>_ttbr`` to ``<bound>_inr``. Native mode orders the bounds in USD,
    so ``lower_inr`` can exceed ``upper_inr``.
    """

    lower_inr: Decimal
    upper_inr: Decimal
    lower_date: date
    upper_date: date
    lower_usd: Decimal
    upper_usd: Decimal
    lower_ttbr: Decimal
    upper_ttbr: Decimal
    closing_usd: Decimal
    closing_inr: Decimal
    closing_ttbr: Decimal
    closing_nav_usd: Decimal


def require_supported_calendar_year(calendar_year: int) -> None:
    """Refuse a calendar year whose US market closures aren't all listed."""
    if not supports_calendar_year(calendar_year):
        raise FidelityCmaFaError(
            "Fidelity CMA Schedule FA Table A2 daily pricing does not support "
            f"calendar year {calendar_year}"
        )


def _price(series: PriceSeries, day: date, max_staleness_days: int) -> Decimal:
    """``day``'s NAV: the close dated on its US market session, never an earlier one."""
    session = us_market_session_on_or_before(day)
    if day - session > timedelta(days=max_staleness_days):
        raise FidelityCmaFaError("SPAXX daily NAV coverage is incomplete")
    price = series.closes.get(session)
    if price is None:
        raise FidelityCmaFaError(
            "Fidelity CMA Schedule FA Table A2: the SPAXX daily NAV series has no Close "
            f"for a US market session in the priced window. {_MOVE_NAV_CACHE_ASIDE}: a fresh "
            "fetch can return closes a series cached earlier lacks. If the fresh series still "
            f"lacks a held or traded day's close, {_BY_HAND}."
        )
    return price


def compute_fidelity_cma_peak_bounds(  # noqa: PLR0912, PLR0915
    statements: CmaStatementBundle,
    *,
    calendar_year: int,
    series: PriceSeries,
    fx_policy: FxPolicy,
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    peak_currency: PeakCurrency = "native",
) -> FidelityCmaPeakBounds:
    """Compute exact EOD values and lower/upper intraday-order bounds."""
    warn_if_inr_peak_currency(peak_currency)
    bundle = statements.statements
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    if (
        bundle[0].beginning_account_value_usd is not None
        or bundle[0].position.beginning_market_value_usd is not None
    ):
        raise FidelityCmaFaError(
            "CMA peak reconstruction requires statements from before any carried balance"
        )
    if bundle[0].period_start > cy_end or bundle[-1].period_end != cy_end:
        raise FidelityCmaFaError("CMA Schedule FA requires statement coverage through December 31")
    accrued = [
        number
        for number, statement in enumerate(bundle, start=1)
        if statement.accrued_interest_usd != 0
    ]
    if accrued:
        raise FidelityCmaFaError(
            "non-zero accrued interest is unsupported for CMA peak bounds: "
            f"{_statements(accrued)} (by period start, year-end reports excluded) "
            f"{'shows' if len(accrued) == 1 else 'show'} it, so {_BY_HAND}."
        )
    require_supported_calendar_year(calendar_year)
    if series.currency != "USD" or not series.split_safe:
        raise FidelityCmaFaError("SPAXX NAV series has unsupported currency or split semantics")
    seed = us_market_session_on_or_before(max(cy_start, bundle[0].period_start))
    if any(seed <= day <= cy_end and not is_us_market_session(day) for day in series.closes):
        raise FidelityCmaFaError(
            "Fidelity CMA Schedule FA Table A2: the SPAXX daily NAV series has a Close dated "
            "on a day that is not a US market session in the priced window. "
            f"{_MOVE_NAV_CACHE_ASIDE}: a fresh fetch can return the series without that "
            f"Close. If the fresh series still has it, {_BY_HAND}."
        )

    core_by_date: dict[date, list[CmaCoreTransaction]] = defaultdict(list)
    lower_external_by_date: dict[date, list[Decimal]] = defaultdict(list)
    upper_external_by_date: dict[date, list[Decimal]] = defaultdict(list)
    statement_ends = {statement.period_end: statement for statement in bundle}
    for statement in bundle:
        for transaction in statement.core_transactions:
            core_by_date[transaction.date].append(transaction)
        for credit in statement.income_credits:
            if (
                credit.kind is CmaIncomeKind.CREDIT_INTEREST
                and credit.date < statement.period_start
            ):
                # The printed economic date is in the previous statement period,
                # but the row appears only in this statement. Bound its unknown
                # posting day across the current statement window.
                lower_external_by_date[statement.period_end].append(credit.amount_usd)
                upper_external_by_date[statement.period_start].append(credit.amount_usd)
            else:
                lower_external_by_date[credit.date].append(credit.amount_usd)
                upper_external_by_date[credit.date].append(credit.amount_usd)
        for withholding in statement.withholdings:
            lower_external_by_date[withholding.date].append(withholding.amount_usd)
            upper_external_by_date[withholding.date].append(withholding.amount_usd)
        for activity in statement.cash_activity:
            lower_external_by_date[activity.date].append(activity.amount_usd)
            upper_external_by_date[activity.date].append(activity.amount_usd)
        for card in statement.debit_card_transactions:
            lower_external_by_date[card.post_date].append(card.amount_usd)
            upper_external_by_date[card.post_date].append(card.amount_usd)

    lower_fcb = _ZERO
    upper_fcb = _ZERO
    quantity = _ZERO
    lower = PeakSelector(fx_policy=fx_policy, peak_currency=peak_currency)
    upper = PeakSelector(fx_policy=fx_policy, peak_currency=peak_currency)
    day = bundle[0].period_start
    while day <= cy_end:
        in_cy = day >= cy_start
        nav = None
        if in_cy and (quantity != 0 or day in core_by_date):
            nav = _price(series, day, max_price_staleness_days)
        elif in_cy:
            # Only a core transaction changes the quantity, so this day holds
            # no SPAXX from start to end and its NAV multiplies zero. It's
            # still used when the series has it: the product's trailing zeros
            # reach the review CSV's full-precision audit bounds (#1533).
            nav = series.closes.get(us_market_session_on_or_before(day))
        lower_start_fcb, upper_start_fcb, start_quantity = (
            lower_fcb,
            upper_fcb,
            quantity,
        )
        lower_deltas = list(lower_external_by_date.get(day, ()))
        upper_deltas = list(upper_external_by_date.get(day, ()))
        lower_fcb = checked_addition(lower_fcb, _sum(lower_deltas, at=_FCB), at=_FCB)
        upper_fcb = checked_addition(upper_fcb, _sum(upper_deltas, at=_FCB), at=_FCB)

        for transaction in core_by_date.get(day, ()):
            if nav is not None and transaction.price_per_unit_usd != nav:
                raise FidelityCmaFaError(
                    "SPAXX statement execution price does not match fetched daily NAV"
                )
            lower_fcb = checked_subtraction(lower_fcb, transaction.amount_usd, at=_FCB)
            upper_fcb = checked_subtraction(upper_fcb, transaction.amount_usd, at=_FCB)
            quantity = checked_addition(quantity, transaction.quantity, at=_QUANTITY)
            if nav is not None:
                delta = checked_subtraction(
                    checked_product(transaction.quantity, nav, at=_ACCOUNT_VALUE),
                    transaction.amount_usd,
                    at=_ACCOUNT_VALUE,
                )
                lower_deltas.append(delta)
                upper_deltas.append(delta)

        statement = statement_ends.get(day)
        if statement is not None:
            expected_fcb = statement.free_credit_balance_usd or _ZERO
            if (
                lower_fcb != expected_fcb
                or upper_fcb != expected_fcb
                or quantity != statement.position.quantity
            ):
                raise FidelityCmaFaError(
                    "reconstructed CMA month-end cash or quantity does not match the statement"
                )
            if nav is not None and statement.position.price_per_unit_usd != nav:
                raise FidelityCmaFaError(
                    "SPAXX statement month-end price does not match fetched daily NAV"
                )

        if in_cy:
            # Without a NAV the day holds zero units: the value is the cash alone.
            price = _ZERO if nav is None else nav
            lower_start_usd = _value(lower_start_fcb, start_quantity, price, at=_ACCOUNT_VALUE)
            lower_end_usd = _value(lower_fcb, quantity, price, at=_ACCOUNT_VALUE)
            upper_start_usd = _value(upper_start_fcb, start_quantity, price, at=_ACCOUNT_VALUE)
            upper_end_usd = _value(upper_fcb, quantity, price, at=_ACCOUNT_VALUE)
            if lower_end_usd != checked_addition(
                lower_start_usd, _sum(lower_deltas, at=_ACCOUNT_VALUE), at=_ACCOUNT_VALUE
            ) or upper_end_usd != checked_addition(
                upper_start_usd, _sum(upper_deltas, at=_ACCOUNT_VALUE), at=_ACCOUNT_VALUE
            ):
                raise FidelityCmaFaError("CMA daily account-value reconstruction is inconsistent")
            lower_usd = max(lower_start_usd, lower_end_usd)
            upper_usd = checked_addition(
                upper_start_usd,
                _sum((value for value in upper_deltas if value > 0), at=_ACCOUNT_VALUE),
                at=_ACCOUNT_VALUE,
            )
            lower.offer(day, lower_usd)
            upper.offer(day, upper_usd)
        day += timedelta(days=1)

    closing_statement = statement_ends[cy_end]
    closing_nav = _price(series, cy_end, max_price_staleness_days)
    lower_closing_usd = _value(lower_fcb, quantity, closing_nav, at=_CLOSING_VALUE)
    upper_closing_usd = _value(upper_fcb, quantity, closing_nav, at=_CLOSING_VALUE)
    if (
        lower_closing_usd != upper_closing_usd
        or lower_closing_usd != closing_statement.account_value_usd
    ):
        raise FidelityCmaFaError(
            "reconstructed CMA Dec-31 account value does not match the statement"
        )
    closing_ttbr = fx_policy.rate_for(cy_end)
    lower_peak = lower.result()
    upper_peak = upper.result()
    if lower_peak is None or upper_peak is None:
        raise FidelityCmaFaError("CMA Schedule FA calendar-year window is empty")
    return FidelityCmaPeakBounds(
        lower_inr=lower_peak.value_inr,
        upper_inr=upper_peak.value_inr,
        lower_date=lower_peak.on_date,
        upper_date=upper_peak.on_date,
        lower_usd=lower_peak.value_native,
        upper_usd=upper_peak.value_native,
        lower_ttbr=lower_peak.ttbr,
        upper_ttbr=upper_peak.ttbr,
        closing_usd=lower_closing_usd,
        closing_inr=lower_closing_usd * closing_ttbr,
        closing_ttbr=closing_ttbr,
        closing_nav_usd=closing_nav,
    )
