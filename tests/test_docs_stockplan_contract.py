"""Keep docs/stockplan-contract.md in step with what the StockPlan code accepts (#385).

The listed sheet, header, value and export-prefix constants must appear on
the contract page, and the StockPlan help and docs must point at it. Title
and allowed-text regex shapes are reviewed by hand.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa import pipeline_stockplan_fa as fa
from ibkr_fa import pipeline_stockplan_income as income
from ibkr_fa.compliance.foreign_tax_credit import BLANK_RELIEF_TAXPAYER_ID_WARNING
from ibkr_fa.parser.stockplan import _xlsx, holdings
from ibkr_fa.parser.stockplan import capital_gain_loss as cgl
from ibkr_fa.parser.stockplan import dividend_report as dividend

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = "docs/stockplan-contract.md"
_TEXT = (_ROOT / _CONTRACT).read_text(encoding="utf-8")

_ACCEPTED: tuple[str, ...] = (
    dividend._SHEET,
    *dividend._HEADER,
    *dividend._KNOWN_ACTIVITIES,
    dividend._NO_RECORDS,
    *dividend._OPTIONAL_SHEETS,
    cgl._RELEASES_SHEET,
    cgl._CG_SHEET,
    *cgl._RELEASES_HEADER,
    *cgl._CG_HEADER,
    *(str(value) for value in cgl._CG_SECTION_ROW if value is not None),
    cgl._RELEASE_TYPE,
    cgl._COMPLETE_STATUS,
    cgl._TAX_PAYMENT_METHOD,
    cgl._NO_RECORDS,
    *cgl._OPTIONAL_SHEETS,
    holdings._SHEET,
    *holdings._HEADER,
    holdings._CASH_FUND,
    *holdings._OPTIONAL_SHEETS,
    *holdings._QUOTES_HEADER,
    _xlsx.USD_STATEMENT,
    income._DIVIDEND_REPORT_PREFIX,
    fa._CG_PREFIX,
    fa._HOLDINGS_PREFIX,
    fa._DIVIDEND_PREFIX,
)


@pytest.mark.parametrize("value", sorted(set(_ACCEPTED)))
def test_contract_lists_every_accepted_value(value: str) -> None:
    assert f"`{value}`" in _TEXT


@pytest.mark.parametrize(
    "warning", [fa._A2_WARNING, income._ZERO_SALES_UNPROVEN, BLANK_RELIEF_TAXPAYER_ID_WARNING]
)
def test_contract_quotes_the_warning_verbatim(warning: str) -> None:
    assert f"`{warning}`" in _TEXT


@pytest.mark.parametrize("command", ["stockplan-income", "stockplan-fa", "stockplan-all"])
def test_stockplan_help_points_at_the_contract(command: str) -> None:
    subparsers = next(
        action
        for action in cli._build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert _CONTRACT in (subparsers.choices[command].description or "")


@pytest.mark.parametrize(
    ("doc", "link"),
    [
        ("README.md", "docs/stockplan-contract.md"),
        ("docs/architecture.md", "stockplan-contract.md"),
    ],
)
def test_docs_link_the_contract(doc: str, link: str) -> None:
    assert f"]({link})" in (_ROOT / doc).read_text(encoding="utf-8")
