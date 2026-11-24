"""Each Robinhood credit files on the paying account's lots, through robinhood-fa (#1318).

The run reconciles a synthetic two-account bundle in which both accounts hold
the security and receive its dividend, and runs the real option,
price-coverage, source-income, Schedule FA and writer steps. Each account has a
CY 2025 statement and an empty Q1 2026 one, so its evidence covers both CY 2025
and FY 2025-26. Only the bundle load, the Schedule FA TTBR table and the Yahoo
closes ($15 every session) are stubbed, as in
``tests/test_pipeline_robinhood_exact_a3.py``.
"""

from __future__ import annotations

import csv
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
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, RobinhoodStatementFamily
from tests._robinhood_a3_load import a3_bundle, flat_ttbr, serve_closes
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_fa_inputs import FA_CUSIP as _CUSIP
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import FA_OPENING as _OPENING
from tests._robinhood_fa_inputs import FA_SYMBOL as _SYMBOL
from tests._robinhood_fa_inputs import fa_account as _account
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_dividend as _dividend
from tests._robinhood_fa_inputs import fa_statement as _statement
from tests._robinhood_security import robinhood_security

_FY_END = date(2026, 3, 31)
_A3_CREDIT = "Total gross amount paid/credited with respect to the holding during the period"
_A2_CREDIT = "Amount"


def _statements(
    number: str,
    family: RobinhoodStatementFamily,
    events: tuple[RobinhoodEvent, ...],
    *,
    quantity: Decimal,
    dividends: Decimal,
) -> tuple[RobinhoodStatement, RobinhoodStatement]:
    """``number``'s CY 2025 statement of ``events`` and an empty Q1 2026 one,
    each ending with ``quantity`` shares at $15."""
    cash = sum((event.amount_usd or Decimal() for event in events), Decimal())
    year = _statement(
        number, family, events, cash=cash, position_quantity=quantity, dividends=dividends
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
    )
    return year, quarter


def _paid_dividend(shares: str, amount: str) -> RobinhoodEvent:
    """``_dividend()`` paying ``amount`` for ``shares`` at $0.3333 a share."""
    return replace(
        _dividend(),
        amount_usd=Decimal(amount),
        description_shares=Decimal(shares),
        description_rate_usd=Decimal("0.3333"),
    )


def _load_both_holding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make every command load two reconciled accounts holding the security
    from January 10: the first 1 share paid $0.33 of one $0.3333 dividend, the second
    2 shares paid $0.67. Schedule FA's TTBR is 82.50 every day of CY 2025."""
    first = (_buy("1"), _paid_dividend("1", "0.33"))
    second = (_buy("2"), _paid_dividend("2", "0.67"))
    statements = (
        *_statements(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            first,
            quantity=Decimal(1),
            dividends=Decimal("0.33"),
        ),
        *_statements(
            "222222222",
            RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            second,
            quantity=Decimal(2),
            dividends=Decimal("0.67"),
        ),
    )
    accounts = (_account("111111111"), _account("222222222"))
    paired = reconcile_robinhood_accounts(
        validate_robinhood_statement_bundles(statements),
        tuple(
            RobinhoodActivityReportEvidence(events=items, start_date=_OPENING, end_date=_FY_END)
            for items in (first, second)
        ),
        accounts,
    )
    security = robinhood_security(
        symbol=_SYMBOL, cusip=_CUSIP, isin=_ISIN, source="synthetic issuer evidence"
    )
    config = RobinhoodConfig(accounts=accounts, securities=(security,))
    bundle = a3_bundle(paired, config, tmp_path)
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    flat_ttbr(monkeypatch, Decimal("82.50"))
    serve_closes(monkeypatch, _SYMBOL)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_robinhood_fa_files_each_accounts_dividend_on_its_own_lot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1318 example 3 through the filing CSVs: at ₹82.50 each account's lot
    files its own account's dividend, $0.33 as 27 and $0.67 as 55, the same
    figures as that account's A2 row. Main pooled $1.00 across both lots and
    filed 28 on the first account's lot."""
    _load_both_holding(monkeypatch, tmp_path)

    exit_code = _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv")

    assert capsys.readouterr().err == ""
    assert exit_code == 0
    a3 = _rows(tmp_path / "out" / "schedule-fa-a3.csv")
    a2 = _rows(tmp_path / "out" / "schedule-fa-a2.csv")
    assert [row[_A3_CREDIT] for row in a3] == ["27", "55"]
    assert [row[_A2_CREDIT] for row in a2] == ["27", "55"]
