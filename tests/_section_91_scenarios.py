"""The section 91 pipeline scenarios (#1177), shared by the section 91 pipeline
and CLI tests (#1887). ``tests/test_pipeline_section_91_exact.py`` describes
each scenario.

- ``_FIXTURE``: the Activity Statement fixture the runs parse;
  ``_ACCOUNT_TOML``: the head of every scenario's ``user.toml``;
- ``_READINESS_TAX`` and ``_READINESS_INCOME``: the readiness scenario's
  section 91 inputs;
- ``_Scenario``: synthetic countries and a run's section 91 inputs;
  ``_SCENARIOS``: the named scenarios;
- ``_READY`` and ``_ZERO_CREDIT``: Form 67 row readiness values; ``_FORM67``:
  each scenario's credits, readiness and refusal; ``_PORTAL``: each refusing
  scenario's Schedule FSI and TR portal-rule refusal;
- ``_dividend``: a country's dividend row; ``_config_text``: a scenario's
  ``user.toml`` text; ``_arrange``: feeds a scenario's rows and FX rate to the
  IBKR income engine with ``tests._form67_broker_runs.inject_income`` and
  writes its ``user.toml``;
- ``_rows``: every row of a CSV or XLSX output; ``_form67_filed_credits``:
  each Form 67 row's filed credits.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa import pipeline_fsi_compute
from ibkr_fa.compute.form67 import Form67Readiness, Form67RefusalReason, Form67RowReadiness
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Ticker
from tests._form67_broker_runs import inject_income
from tests._section_91_inputs import (
    _DESCRIPTION,
    _EXACT_PORTAL,
    _EXACT_ZERO,
    _ISIN_BY_ISO,
    _ON,
    _ORDINARY,
    _PREFLIGHT_PORTAL,
    _PREFLIGHT_ZERO,
    _PRODUCT,
    _READINESS,
    _SYMBOL,
    _Country,
    _wht,
)

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_ACCOUNT_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true

[account.foreign_tins]
BM = "123456789"
KY = "123456789"
VG = "123456789"
US = "123456789"

[fx]
max_carry_forward_days = 4

[form67]
"""
# The readiness scenario's section 91 inputs, within user.toml's money limits.
# Their rate 0.0833…3 and the exact credit 0.4999…98 each have 30 places;
# rounded to 28 digits, the credit would be 0.5 and file ₹1.
_READINESS_TAX = "833333333.33333333333333333333"
_READINESS_INCOME = "10000000000"


@dataclass(frozen=True)
class _Scenario:
    """Synthetic countries, in input order, and the run's section 91 inputs."""

    countries: tuple[_Country, ...]
    tax: str
    income: str
    fx: str


_SCENARIOS = {
    "ordinary": _Scenario((_ORDINARY,), "300000", "2800000", "70"),
    "product": _Scenario((_PRODUCT,), "0.25", "1", "1"),
    "readiness": _Scenario((_READINESS,), _READINESS_TAX, _READINESS_INCOME, "1"),
    "control": _Scenario((_ORDINARY,), "100000", "2800000", "70"),
    "zero-tax": _Scenario((_ORDINARY,), "0", "2800000", "70"),
    "repeating": _Scenario((_Country(gross="10", wht="-9"),), "1", "3", "1"),
}

_READY = Form67RowReadiness(Form67Readiness.READY)
_ZERO_CREDIT = Form67RowReadiness(Form67Readiness.REFUSED, Form67RefusalReason.ZERO_CREDIT)
# Each scenario's unrounded and filed section 91 credit, its row's readiness,
# and the refusal wherever readiness is enforced (portal CSV, run-all's Form 67
# step in every format, and Schedule FSI and TR in every format, #1480); None
# where the run writes.
_FORM67: dict[str, tuple[str, int, Form67RowReadiness, str | None]] = {
    "ordinary": ("1.5", 2, _READY, None),
    "product": ("1.499999999999999999999999999", 1, _READY, None),
    "readiness": ("0.4999999999999999999999999999", 0, _ZERO_CREDIT, _EXACT_ZERO),
    "control": ("0.5", 1, _READY, _PREFLIGHT_ZERO),
    "zero-tax": ("0", 0, _ZERO_CREDIT, _PREFLIGHT_ZERO),
    "repeating": ("3.333333333333333333333333333", 3, _READY, None),
}
# Each refusing scenario's Schedule FSI and TR portal-rule refusal, in every
# format; the repeating scenario writes.
_PORTAL = {
    "ordinary": _PREFLIGHT_PORTAL,
    "product": _EXACT_PORTAL,
    "readiness": _EXACT_PORTAL,
    "control": _PREFLIGHT_PORTAL,
    "zero-tax": _PREFLIGHT_PORTAL,
}


def _dividend(country: _Country) -> Dividend:
    return Dividend(
        symbol=Ticker(_SYMBOL),
        isin=_ISIN_BY_ISO[country.iso],
        payment_date=_ON,
        amount_gross=Decimal(country.gross),
        currency="USD",
        description=_DESCRIPTION,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _config_text(scenario: _Scenario) -> str:
    overrides = "".join(
        f'\n[[form67.tax_payable_overrides]]\ncountry = "{country.iso}"\n'
        f'source = "DIVIDEND"\ntax_payable_normal_inr = "{country.payable}"\n'
        for country in scenario.countries
        if country.payable is not None
    )
    return (
        _ACCOUNT_TOML
        + overrides
        + "\n[form67.section_91]\n"
        + f'total_indian_tax_before_91_relief_inr = "{scenario.tax}"\n'
        + f'total_income_inr = "{scenario.income}"\n'
    )


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    scenario: _Scenario,
    *,
    extra_wht: Sequence[WithholdingTax] = (),
) -> Path:
    """Feed ``scenario``'s rows, ``extra_wht`` first, to the IBKR income engine
    at one flat FX rate with ``tests._form67_broker_runs.inject_income``, and
    write its user.toml; returns the config path. Shared with the CLI tests."""
    inject_income(
        monkeypatch,
        pipeline_fsi_compute,
        dividends=[_dividend(country) for country in scenario.countries],
        withholding_taxes=[*extra_wht, *(_wht(country) for country in scenario.countries)],
        rate=Decimal(scenario.fx),
    )
    config = directory / "user.toml"
    config.write_text(_config_text(scenario), encoding="utf-8")
    return config


def _rows(path: Path) -> list[list[object]]:
    """Every row of a CSV or of an XLSX file's only sheet."""
    if path.suffix == ".xlsx":
        sheet = load_workbook(path).active
        assert sheet is not None
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [list(row) for row in csv.reader(fh)]


def _form67_filed_credits(path: Path, format_id: str) -> tuple[Decimal, ...]:
    """Each row's filed (section 91, total) credits: portal CSV by header,
    the review CSV and XLSX by their cols 12 and 13. Shared with the CLI tests."""
    header, *rows = _rows(path)
    if format_id == "portal-csv":
        section_91 = header.index("Credit claimed under section 91")
        total = header.index("Total foreign tax credit claimed")
    else:
        section_91, total = 11, 12
    return tuple(Decimal(str(cell)) for row in rows for cell in (row[section_91], row[total]))
