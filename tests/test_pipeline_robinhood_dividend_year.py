"""A Robinhood dividend files in the Schedule FA year of its Activity Date (#1320).

The runs reconcile a synthetic two-account bundle. The first account holds
2 shares from January 10, 2025 and receives a $10 dividend recorded
December 19, 2025, posted on December 31, 2025 or January 2, 2026; the second
holds only a $5 cash deposit. Each account has a CY 2025 statement and a
Q1 2026 one, so its evidence covers both CY 2025 and FY 2025-26, and the
dividend sits in the statement of its Activity Date. The
real option, price-coverage, source-income, Schedule FA and writer steps run;
the year-comparison test also computes Schedule OS. Only the bundle load,
the TTBR tables (₹80 on every required date) and the Yahoo
closes ($15 every session) are stubbed, as in
``tests/test_pipeline_robinhood_credit_accounts.py``.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute import robinhood_fa as robinhood_fa_mod
from ibkr_fa.compute.robinhood_fa import compute_robinhood_fa
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.pipeline_robinhood_core import compute_os, compute_source_income
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_dividend_year_bundle import _bundle, _load
from tests._robinhood_fa_inputs import FlatFx as _FlatFx
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_fa_inputs import fa_options as _options
from tests._robinhood_fa_inputs import fa_price_series as _prices

_DEC31 = date(2025, 12, 31)
_JAN2 = date(2026, 1, 2)
_A3_CREDIT = "Total gross amount paid/credited with respect to the holding during the period"
_A2_ACCOUNT = "Account Number"
_A2_NATURE = "Nature of Amount"
_A2_AMOUNT = "Amount"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize(
    ("activity", "paid", "a2", "a3"),
    [
        pytest.param(_DEC31, _JAN2, ("D", "800"), "800", id="pd-later"),
        pytest.param(_JAN2, _DEC31, ("N", "0"), "0", id="pd-earlier"),
    ],
)
def test_robinhood_fa_files_a_dividend_in_its_activity_date_year(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    activity: date,
    paid: date,
    a2: tuple[str, str],
    a3: str,
) -> None:
    """#1320 through the CY 2025 filing CSVs: posted December 31 with ``P/D``
    January 2 the dividend files A2 Dividend ₹800 and A3 ₹800; posted
    January 2 with ``P/D`` December 31 it is absent (A2 No Amount 0, A3 0).
    Main chose the year by the ``P/D`` and filed the reverse."""
    _load(monkeypatch, tmp_path, activity, paid)

    exit_code = _main("robinhood-fa", tmp_path, "--no-fetch-prices", "--format", "filing-csv")

    assert capsys.readouterr().err == ""
    assert exit_code == 0
    assert [
        (row[_A2_NATURE], row[_A2_AMOUNT])
        for row in _rows(tmp_path / "out" / "schedule-fa-a2.csv")
        if row[_A2_ACCOUNT] == "111111111"
    ] == [a2]
    assert [row[_A3_CREDIT] for row in _rows(tmp_path / "out" / "schedule-fa-a3.csv")] == [a3]


@pytest.mark.parametrize(
    ("activity", "paid"),
    [
        pytest.param(_DEC31, _JAN2, id="pd-later"),
        pytest.param(_JAN2, _DEC31, id="pd-earlier"),
        pytest.param(_DEC31, _DEC31, id="both-2025"),
        pytest.param(_JAN2, _JAN2, id="both-2026"),
    ],
)
def test_schedule_os_and_schedule_fa_put_a_dividend_in_one_calendar_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activity: date, paid: date
) -> None:
    """#1320: FY 2025-26 Schedule OS computes the dividend at ₹80/$,
    dated on its Activity Date, and Schedule FA's A2 and A3 select it in that
    calendar year only, for CY 2025 and CY 2026, never both or neither.
    CY 2025's full Schedule FA files it as A2 Dividend and A3 ₹800 exactly
    when the Activity Date is in 2025."""
    bundle = _bundle(tmp_path, activity, paid)
    income = compute_source_income(bundle, FinancialYear(2025))
    rates = {date(2025, 11, 30): Decimal(80), _DEC31: Decimal(80)}
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)

    [os_dividend] = compute_os(bundle, FinancialYear(2025), income).lines
    assert (os_dividend.payment_date, os_dividend.amount_gross_inr) == (activity, Decimal(800))
    [payment] = income.dividend_payments
    assert payment.source_payment_date == paid
    a2_years = {
        year
        for year in (2025, 2026)
        if FaA2NatureOfAmount.DIVIDEND
        in robinhood_fa_mod._a2_gross_evidence(
            bundle.paired, income, _options(), 0, year, _FlatFx()
        ).known_components
    }
    a3_years = {
        year
        for year in (2025, 2026)
        if robinhood_fa_mod._source_credits(bundle.paired, income, year)
    }
    assert a2_years == a3_years == {os_dividend.payment_date.year}

    result = compute_robinhood_fa(
        bundle.paired,
        config=bundle.robinhood,
        securities=bundle.securities,
        income=income,
        options=_options(),
        calendar_year=2025,
        fx_policy=_FlatFx(),
        prices_by_ticker=_prices(),
        entity_overrides=_entity(),
    )
    filed = os_dividend.payment_date.year == 2025
    assert result.blockers == ()
    assert [
        (row.nature_of_amount, row.gross_amount_inr)
        for row in result.a2_rows
        if row.account_number == "111111111"
    ] == [
        (FaA2NatureOfAmount.DIVIDEND, Decimal(800))
        if filed
        else (FaA2NatureOfAmount.NO_AMOUNT, Decimal(0))
    ]
    assert [item.row.gross_amount_paid_credited_inr for item in result.a3] == [
        Decimal(800) if filed else Decimal(0)
    ]
