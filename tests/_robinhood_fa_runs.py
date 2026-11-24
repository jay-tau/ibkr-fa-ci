"""Schedule FA runs over the synthetic Robinhood inputs of
``tests._robinhood_fa_inputs``, the price series that make the Dec-31 close
refuse its required session, and the A3 file's peak and closing cells, shared
by the Robinhood Schedule FA compute and CLI tests (#1772)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import override

import pytest

import ibkr_fa.compute.robinhood_fa as robinhood_fa_mod
from ibkr_fa.compute.robinhood_fa import compute_robinhood_fa
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference import us_market_calendar
from ibkr_fa.types import Ticker
from tests._robinhood_fa_inputs import (
    FA_END,
    FlatFx,
    fa_buy,
    fa_config,
    fa_entity,
    fa_income,
    fa_options,
    fa_paired,
    fa_paired_events,
    fa_price_series,
    fa_summary,
)
from tests._robinhood_security import configured_securities

DEC30 = date(2025, 12, 30)


def compute_fa_open(prices: dict[Ticker, PriceSeries]) -> robinhood_fa_mod.RobinhoodFaResult:
    """Schedule FA for one open 2-share lot whose statement values it at $30."""
    paired, accounts = fa_paired(sold=False, dividend=False)
    return compute_robinhood_fa(
        paired,
        config=fa_config(accounts),
        securities=configured_securities(fa_config(accounts)),
        income=fa_income(dividend=False),
        options=fa_options(),
        calendar_year=2025,
        fx_policy=FlatFx(),
        prices_by_ticker=prices,
        entity_overrides=fa_entity(),
    )


def resolver_session_dec30(monkeypatch: pytest.MonkeyPatch) -> dict[Ticker, PriceSeries]:
    """Only the Dec-31 resolver takes Tue 2025-12-30 as the required session.
    A2 and the A3 peak keep the real calendar, under which every bar of
    ``fa_price_series()`` is valid, so its 2025-12-31 bar, dated after that
    session, is the only failure."""
    monkeypatch.setattr(
        "ibkr_fa.compute.dec31_close.us_market_session_on_or_before", lambda _day: DEC30
    )
    return fa_price_series()


def dec31_closure_bar(monkeypatch: pytest.MonkeyPatch) -> dict[Ticker, PriceSeries]:
    """A controlled calendar lists 2025-12-31 as a closure, so the required
    session is Tue 2025-12-30, but the price source still has a 2025-12-31 bar
    dated after it."""
    prices = fa_price_series()
    monkeypatch.setattr(us_market_calendar, "_CLOSURES", us_market_calendar._CLOSURES | {FA_END})
    return prices


# Each makes the resolver refuse the series with its required-session message.
DEC31_SESSION_REFUSALS = pytest.mark.parametrize(
    "session_refusal",
    [resolver_session_dec30, dec31_closure_bar],
    ids=["resolver-session", "dec31-closure-bar"],
)


class Fx8250(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("82.50")


def compute_fa_statement_total(
    events: tuple[RobinhoodEvent, ...],
    *,
    total: str,
    price: str,
    yahoo: str | dict[Ticker, PriceSeries],
) -> robinhood_fa_mod.RobinhoodFaResult:
    """Schedule FA at ₹82.50/$ for ``events``, buys and sales, whose Dec-31
    statement shows one position of the shares still held at a displayed
    ``price``, worth ``total``, against a Yahoo close of ``yahoo`` every
    session, or the series ``yahoo``."""
    held = sum(
        (
            -(event.quantity or Decimal())
            if event.kind is RobinhoodEventKind.EQUITY_SELL
            else event.quantity or Decimal()
            for event in events
        ),
        Decimal(),
    )
    paired, accounts = fa_paired_events(events, position_quantity=held)
    account = paired.accounts[0]
    statement = account.activity.primary_bundle.statements[0]
    (position,) = statement.positions
    valued = replace(
        statement,
        positions=(replace(position, price_usd=Decimal(price), market_value_usd=Decimal(total)),),
        account_summary=fa_summary(
            statement.account_summary.closing_brokerage_cash_usd, Decimal(total)
        ),
    )
    bundle = replace(account.activity.primary_bundle, statements=(valued,))
    activity = replace(account.activity, primary_bundle=bundle)
    forged = replace(
        paired,
        accounts=(
            replace(
                account,
                activity=activity,
                cash=replace(account.cash, activity_reconciliation=activity),
            ),
            paired.accounts[1],
        ),
    )
    return compute_robinhood_fa(
        forged,
        config=fa_config(accounts),
        securities=configured_securities(fa_config(accounts)),
        income=fa_income(dividend=False),
        options=fa_options(),
        calendar_year=2025,
        fx_policy=Fx8250(),
        prices_by_ticker=fa_price_series(yahoo) if isinstance(yahoo, str) else yahoo,
        entity_overrides=fa_entity(),
    )


def compute_fa_one_dollar_three_shares() -> robinhood_fa_mod.RobinhoodFaResult:
    """One open 3-share lot whose Dec-31 statement shows $1.00 at a displayed
    $0.33, against a Yahoo close of $0.3333, inside the cross-check tolerance."""
    return compute_fa_statement_total((fa_buy("3"),), total="1.00", price="0.33", yahoo="0.3333")


def a3_peak_and_closing_cells(path: Path) -> list[str]:
    """The one A3 row's Peak and Closing balance cells."""
    header, row = path.read_text(encoding="utf-8").splitlines()
    names = header.split(",")
    cells = dict(zip(names, row.split(","), strict=True))
    return [cells["Peak value of investment during the Period"], cells["Closing balance"]]
