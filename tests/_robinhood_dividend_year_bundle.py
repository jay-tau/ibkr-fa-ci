"""The synthetic two-account Robinhood bundle the #1320 dividend-year runs load.

``_bundle`` reconciles a synthetic two-account bundle. The first account holds
2 shares from January 10, 2025 and receives a $10 dividend recorded
December 19, 2025, with the given Activity Date and ``P/D``; the second holds
only a $5 cash deposit. Each account has a CY 2025 statement and a Q1 2026
one, so its evidence covers both CY 2025 and FY 2025-26, and the dividend sits
in the statement of its Activity Date. ``_load`` makes every command load that
bundle, with Schedule FA's TTBR at ₹80 every day of CY 2025 and Yahoo closing
the security at $15 every session. ``_bundle`` reads ``_account``, ``_buy`` and
``_entity`` from this module's globals when it runs, so a test can patch them
here, as ``tests/test_fa_a2_zip_manual_fill.py`` does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementFamily
from tests._robinhood_a3_load import a3_bundle, flat_ttbr, serve_closes
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import FA_SYMBOL as _SYMBOL
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_cash as _cash
from tests._robinhood_fa_inputs import fa_dividend as _dividend
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_security import robinhood_security

_FY_END = date(2026, 3, 31)


def _dated_dividend(activity: date, paid: date) -> RobinhoodEvent:
    """``_dividend()``'s $10 for 2 shares, recorded December 19, 2025, with
    Activity Date ``activity`` and the description's ``P/D`` ``paid``."""
    return replace(
        _dividend(),
        activity_date=activity,
        process_date=activity,
        settle_date=activity,
        record_date=date(2025, 12, 19),
        payment_date=paid,
    )


def _bundle(tmp_path: Path, activity: date, paid: date):
    """The reconciled two-account bundle; the dividend sits in the first
    account's CY 2025 statement when ``activity`` is in 2025, else in its
    Q1 2026 one."""
    dividend = _dated_dividend(activity, paid)
    in_2025 = activity.year == 2025
    year_events = (_buy(), dividend) if in_2025 else (_buy(),)
    quarter_events = () if in_2025 else (dividend,)
    year_cash = sum((event.amount_usd or Decimal() for event in year_events), Decimal())
    quarter_cash = sum((event.amount_usd or Decimal() for event in quarter_events), year_cash)
    year = _statement(
        "111111111",
        RobinhoodStatementFamily.INDIVIDUAL,
        year_events,
        cash=year_cash,
        position_quantity=Decimal(2),
        dividends=Decimal(10) if in_2025 else Decimal(),
    )
    quarter = _statement(
        "111111111",
        RobinhoodStatementFamily.INDIVIDUAL,
        quarter_events,
        cash=quarter_cash,
        position_quantity=Decimal(2),
        dividends=Decimal() if in_2025 else Decimal(10),
        start=date(2026, 1, 1),
        end=_FY_END,
        opening_cash=year_cash,
        opening_securities=year.account_summary.closing_total_securities_usd,
    )
    deposit = (_cash(),)
    other_year = _statement(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        deposit,
        cash=Decimal(5),
        position_quantity=Decimal(),
    )
    other_quarter = _statement(
        "222222222",
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        (),
        cash=Decimal(5),
        position_quantity=Decimal(),
        start=date(2026, 1, 1),
        end=_FY_END,
        opening_cash=Decimal(5),
        opening_securities=Decimal(),
    )
    accounts = (_account("111111111"), _account("222222222"))
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles((year, quarter, other_year, other_quarter)),
        tuple(
            RobinhoodActivityReportEvidence(events=events, start_date=_OPENING, end_date=_FY_END)
            for events in ((*year_events, *quarter_events), deposit)
        ),
        accounts,
    )
    security = robinhood_security(
        symbol=_SYMBOL, cusip=_CUSIP, isin=_ISIN, source="synthetic issuer evidence"
    )
    config = RobinhoodConfig(accounts=accounts, securities=(security,))
    bundle = a3_bundle(paired, config, tmp_path)
    # tests/test_fa_a2_zip_manual_fill.py patches this module's ``_entity``.
    bundle.entity_overrides = _entity()
    return bundle


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, activity: date, paid: date) -> None:
    """Make every command load ``_bundle``, with Schedule FA's TTBR at ₹80."""
    bundle = _bundle(tmp_path, activity, paid)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    flat_ttbr(monkeypatch, Decimal(80))
    serve_closes(monkeypatch, _SYMBOL)
