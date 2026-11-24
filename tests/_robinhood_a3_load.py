"""The shared bundle load for the Robinhood Schedule FA A3 pipeline tests.

The Dec-31 pending-trade load in ``tests/_robinhood_pending.py`` and the
Schedule FA blocker load in ``tests/_robinhood_fa_blockers.py`` reconcile their
own synthetic statements and activity reports, then stub only the bundle load,
Schedule FA's TTBR table and the Yahoo closes with these helpers.
``tests/_robinhood_option_texts.py`` and
``tests/test_robinhood_cg_resolved_filing_stops.py`` build their bundles with
``a3_bundle`` too. Each loader keeps its own differences at its call.
``load_two_accounts`` is #1243's two-account load, which
``tests/test_pipeline_robinhood_exact_a3.py`` and the Schedule FA A3 CLI,
entity address, manual-action and country tests drive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    resolve_robinhood_security_identities,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, RobinhoodStatementFamily
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import Ticker
from tests._robinhood_a3_exact import DESCRIPTION, SYMBOL
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_price_series as _prices
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_pipeline_stubs import stand_in_bundle as _pipeline_bundle
from tests._robinhood_security import configured_securities, robinhood_security

_FY_END = date(2026, 3, 31)
# Every Dec-31 statement here shows 2 shares, at $15 ($30) unless a test sets the price.
_HELD = Decimal(2)


def in_symbol(statement: RobinhoodStatement, symbol: str) -> RobinhoodStatement:
    """``statement`` with each position shown under ``symbol``."""
    positions = tuple(replace(item, symbol_or_cusip=symbol) for item in statement.positions)
    return replace(statement, positions=positions)


def a3_bundle(
    paired: RobinhoodAccountsReconciliation, config: RobinhoodConfig, run_dir: Path
) -> SimpleNamespace:
    """The pipeline bundle of ``paired`` under ``config``: no tax-form
    packets, the shared income config in ``run_dir``, the synthetic
    security's reviewed entity and an empty ``nature_by_isin`` map."""
    bundle = _pipeline_bundle()
    bundle.paired = paired
    bundle.packets = ()
    bundle.robinhood = config
    bundle.securities = configured_securities(config)
    bundle.identities = resolve_robinhood_security_identities(paired, config.securities)
    bundle.config = _shared_config(run_dir)
    bundle.entity_overrides = _entity()
    bundle.nature_by_isin = {}
    return bundle


def flat_ttbr(monkeypatch: pytest.MonkeyPatch, rate: Decimal) -> None:
    """Make Schedule FA's TTBR ``rate`` every day of CY 2025."""
    rates = {date(2025, 1, 1) + timedelta(days=day): rate for day in range(365)}
    monkeypatch.setattr("ibkr_fa.fx.policy_fa.load_ttbr", lambda _currency: rates)


def serve_closes(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    *,
    price: str = "15",
    changes: Mapping[date, Decimal] = MappingProxyType({}),
    requested_only: bool = False,
) -> None:
    """Make Yahoo close ``symbol`` at ``price`` every session, but each day
    in ``changes`` at its close there. Each load returns that series, or with
    ``requested_only`` only the requested windows' series."""
    prices: dict[Ticker, PriceSeries] = {
        Ticker(symbol): replace(series, ticker=Ticker(symbol), closes={**series.closes, **changes})
        for series in _prices(price).values()
    }
    if requested_only:
        monkeypatch.setattr(
            "ibkr_fa.pipeline_robinhood_equity_prices.load_price_series",
            lambda windows, **_kwargs: {ticker: prices[ticker] for ticker in windows},
        )
    else:
        monkeypatch.setattr(
            "ibkr_fa.pipeline_robinhood_equity_prices.load_price_series",
            lambda *_args, **_kwargs: prices,
        )


def year_and_quarter_statements(
    number: str,
    family: RobinhoodStatementFamily,
    events: tuple[RobinhoodEvent, ...],
    *,
    cash: Decimal,
    quantity: Decimal,
    dividends: Decimal = Decimal(),
    price: Decimal = Decimal(15),
) -> tuple[RobinhoodStatement, RobinhoodStatement]:
    """``number``'s CY 2025 statement of ``events`` and an empty Q1 2026 one,
    each ending with ``quantity`` shares of ``SYMBOL`` at ``price``."""
    year = _statement(
        number,
        family,
        events,
        cash=cash,
        position_quantity=quantity,
        dividends=dividends,
        price=price,
    )
    quarter = _statement(
        number,
        family,
        (),
        cash=cash,
        position_quantity=quantity,
        start=date(2026, 1, 1),
        end=_FY_END,
        opening_cash=cash,
        opening_securities=year.account_summary.closing_total_securities_usd,
        price=price,
    )
    return in_symbol(year, SYMBOL), in_symbol(quarter, SYMBOL)


# ``load_two_accounts`` reads ``DESCRIPTION`` and ``_account`` from this module when it
# runs, so the blank A3 name and comma-only text CLI tests patch them here.
def load_two_accounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *events: RobinhoodEvent,
    dividends: Decimal = Decimal(),
    price: str = "15",
    rate: str = "84.15",
    country: str = "US",
) -> SimpleNamespace:
    """Make every command load two reconciled accounts, the first making
    ``events`` in ``SYMBOL``, each described ``DESCRIPTION``, and holding 2
    shares on Dec 31 at ``price``; return the bundle. The security's issuer
    country is ``country``. Schedule FA's TTBR is ``rate`` every day of CY
    2025, and Yahoo closes at ``price`` every session."""
    marked = tuple(replace(event, instrument=SYMBOL, description=DESCRIPTION) for event in events)
    deposit = (_cash(),)
    cash = sum((event.amount_usd or Decimal() for event in marked), Decimal())
    statements = (
        *year_and_quarter_statements(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            marked,
            cash=cash,
            quantity=_HELD,
            dividends=dividends,
            price=Decimal(price),
        ),
        *year_and_quarter_statements(
            "222222222",
            RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            deposit,
            cash=Decimal(5),
            quantity=Decimal(),
        ),
    )
    accounts = (_account("111111111"), _account("222222222"))
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(statements),
        tuple(
            RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_FY_END)
            for items in (marked, deposit)
        ),
        accounts,
    )
    security = robinhood_security(
        symbol=SYMBOL,
        cusip=_CUSIP,
        isin=_ISIN,
        issuer_country_code=country,
        source="synthetic issuer evidence",
    )
    config = RobinhoodConfig(accounts=accounts, securities=(security,))
    bundle = a3_bundle(paired, config, tmp_path)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    flat_ttbr(monkeypatch, Decimal(rate))
    serve_closes(monkeypatch, SYMBOL, price=price)
    return bundle
