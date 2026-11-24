"""The exact Form 67 section 91 credit through the IBKR pipelines (#1177).

``run_form67``, ``run_schedule_fsi`` and ``run_schedule_tr`` parse a real
fixture statement, whose dividend and withholding rows are replaced, at the
income engine's input, by the compute tests' synthetic Bermuda (no-treaty)
rows; the FX policies are stubbed at one flat rate. Everything else is real:
config loading, the income engine, Form 67 readiness, the Schedule FSI portal
check and the writers.

Each scenario is one dividend and its withholding on 2025-12-15:

* ordinary: 0.20 USD with 0.03 withheld at ₹70, total tax 300000 on income
  2800000. The exact credit 1.5 files ₹2 (1f885b5's 1.4999… filed ₹1).
* product: 5.999999999999999999999999999 USD with 3 withheld at ₹1, total
  tax 0.25 on income 1. The exact credit files ₹1 (1f885b5 filed ₹2).
* readiness: 6 USD with 1 withheld at ₹1, total tax 833333333.33…3 (20
  places) on income 10000000000. The average rate 0.0833…3 has 29
  significant digits, so the former formula rounds it to 28, and 6 times that
  rounds up to 0.5. The exact credit 0.4999…98 files ₹0 (1f885b5 filed ₹1).
* control: the ordinary row with total tax 100000. The exact 0.5 files ₹1;
  the former credit filed ₹0.
* zero-tax: the ordinary row with zero total tax, a zero credit either way.
* repeating: 10 USD with 9 withheld at ₹1, total tax 1 on income 3. Both
  formulas file ₹3.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_form67 import run_form67
from ibkr_fa.pipeline_fsi import run_schedule_fsi
from ibkr_fa.pipeline_tr import run_schedule_tr
from tests._crash_reports import assert_unchained_refusal
from tests._number_limits import MONEY
from tests._section_91_inputs import (
    _EXACT_ZERO,
    _MARKERS,
    _PREFLIGHT_ZERO,
    _READINESS,
    _assert_refusal,
    _Country,
    _exact_credit,
    _net_zero_us,
    _oracle,
)
from tests._section_91_scenarios import (
    _FIXTURE,
    _FORM67,
    _PORTAL,
    _READINESS_INCOME,
    _READINESS_TAX,
    _SCENARIOS,
    _arrange,
    _form67_filed_credits,
    _rows,
    _Scenario,
)

_FY = FinancialYear(2025)
# The scenario's former total tax on income 1: 29 places, past those limits (#1275).
_TWELFTH = "0.08333333333333333333333333333"


def _suffix(format_id: str) -> str:
    return ".xlsx" if format_id == "xlsx" else ".csv"


def _form67(config: Path, output: Path, format_id: str, *, all_mode: bool = False) -> Any:
    return run_form67(
        input_path=_FIXTURE,
        config_path=config,
        output_path=output,
        financial_year=_FY,
        format_id=format_id,
        all_mode_readiness=all_mode,
    )


def _schedule(command: str, config: Path, output: Path, format_id: str) -> Any:
    run = run_schedule_fsi if command == "schedule-fsi" else run_schedule_tr
    return run(
        input_path=_FIXTURE,
        config_path=config,
        output_path=output,
        financial_year=_FY,
        format_id=format_id,
    )


# --- Form 67 -----------------------------------------------------------------------

# (format, all-mode): readiness is enforced for the portal CSV, and for run-all's
# Form 67 step in every format; standalone review CSV and XLSX write every row.
_FORM67_MODES = [
    ("portal-csv", False),
    ("review-csv", False),
    ("xlsx", False),
    ("portal-csv", True),
    ("review-csv", True),
    ("xlsx", True),
]


@pytest.mark.parametrize(
    ("format_id", "all_mode"),
    _FORM67_MODES,
    ids=[f"{format_id}{'-all-mode' if all_mode else ''}" for format_id, all_mode in _FORM67_MODES],
)
@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_form67_files_the_exact_credit_or_refuses_where_readiness_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    format_id: str,
    all_mode: bool,
) -> None:
    """Every format files cols 12 and 13 as the exact credit rounded HALF_UP,
    or, where readiness is enforced, refuses with the complete message and
    leaves no output. Standalone review CSV and XLSX write a REFUSED row as a
    status, never a compute error.

    Fails on 1f885b5 for the ordinary (₹1 filed), product (₹2 filed),
    readiness (₹1 filed, READY) and control (₹0 REFUSED, or its ConfigError)
    scenarios, and the zero-tax refusal (a ConfigError there).
    """
    scenario = _SCENARIOS[name]
    credit, filed, readiness, refusal = _FORM67[name]
    config = _arrange(monkeypatch, tmp_path, scenario)
    output = tmp_path / f"form-67{_suffix(format_id)}"
    output.write_text("stale", encoding="utf-8")
    exact = _exact_credit(
        scenario.countries[0], tax=scenario.tax, income=scenario.income, fx=scenario.fx
    )
    assert _oracle(exact) == filed

    if refusal is not None and (all_mode or format_id == "portal-csv"):
        with pytest.raises(InvariantError) as raised:
            _form67(config, output, format_id, all_mode=all_mode)
        _assert_refusal(raised, 1, refusal, _MARKERS)
        assert list(tmp_path.iterdir()) == [config]
        return

    report = _form67(config, output, format_id, all_mode=all_mode)

    assert report.output_path == output
    assert _form67_filed_credits(output, format_id) == (filed, filed)
    (row,) = report.per_entry_rows
    assert row.section_91_credit_inr == row.total_credit_inr == Decimal(credit)
    assert row.readiness == readiness


def test_the_former_readiness_total_tax_refuses_at_config_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness scenario's former total tax on income 1 had 29 places,
    past user.toml's money limit of 20, so that run now refuses as it loads
    the config: the field's path and the fixed text, no value, no chain and
    no output (#1275). The marker replaces the twelfth's last digits.

    Fails on 0d3547b, which loaded it."""
    marker = "613171"
    tax = _TWELFTH[: -len(marker)] + marker
    assert Decimal(tax).as_tuple().exponent == Decimal(_TWELFTH).as_tuple().exponent == -29
    config = _arrange(monkeypatch, tmp_path, _Scenario((_READINESS,), tax, "1", "1"))

    with pytest.raises(ConfigError) as raised:
        _form67(config, tmp_path / "form-67.csv", "portal-csv")

    field = "form67.section_91.total_indian_tax_before_91_relief_inr"
    assert_unchained_refusal(
        raised.value,
        ConfigError(field=field, message=MONEY),
        f"ConfigError at field '{field}': {MONEY}",
        markers=(marker,),
    )
    assert list(tmp_path.iterdir()) == [config]


# --- Schedule FSI and TR ------------------------------------------------------------

_SCHEDULE_FORMATS = [
    (command, format_id)
    for command in ("schedule-fsi", "schedule-tr")
    for format_id in ("filing-csv", "review-csv", "xlsx")
]


@pytest.mark.parametrize(("command", "format_id"), _SCHEDULE_FORMATS)
@pytest.mark.parametrize("name", list(_PORTAL))
def test_schedule_fsi_and_tr_refuse_a_relief_the_portal_would_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    command: str,
    format_id: str,
) -> None:
    """The portal fills relief as min(filed foreign tax, filed Indian tax). A
    former credit that broke that rule still refuses with the preflight
    message, though the exact credit may not (ordinary: corrected ₹2 =
    min(2, 3)); a corrected credit that newly breaks it refuses with the
    exact-relief message (product: ₹1 against min(3, 2)). Form 67 readiness
    runs first, as in run-all (#1480), so the readiness, control and zero-tax
    scenarios refuse with their readiness message instead. Nothing is left at
    the output path.

    Fails on 1f885b5, which passed the product and readiness scenarios and
    refused the others with the generic portal-relief message, and on
    6f81822, which refused the readiness, control and zero-tax scenarios with
    their portal-rule message.
    """
    config = _arrange(monkeypatch, tmp_path, _SCENARIOS[name])
    output = tmp_path / f"{command}{_suffix(format_id)}"
    output.write_text("stale", encoding="utf-8")

    with pytest.raises(InvariantError) as raised:
        _schedule(command, config, output, format_id)

    _assert_refusal(raised, 1, _FORM67[name][3] or _PORTAL[name], _MARKERS)
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(("command", "format_id"), _SCHEDULE_FORMATS)
def test_schedule_fsi_and_tr_write_a_repeating_rate_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, format_id: str
) -> None:
    """A repeating 1/3 average rate is ordinary: ₹3 = min(9, 3) passes the
    portal rule, and every format writes."""
    config = _arrange(monkeypatch, tmp_path, _SCENARIOS["repeating"])
    output = tmp_path / f"{command}{_suffix(format_id)}"

    report = _schedule(command, config, output, format_id)

    assert report.output_path == output
    assert output.is_file()


def test_schedule_tr_relief_is_the_sum_of_the_filed_fsi_relief_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two countries' repeating 10/3 credits each file ₹3, the exact credit
    rounded HALF_UP, in their FSI relief cell, and each country's TR relief
    total is its filed FSI relief cells added."""
    countries = (_Country(gross="10", wht="-9"), _Country(iso="KY", gross="10", wht="-9"))
    scenario = _Scenario(countries, "1", "3", "1")
    config = _arrange(monkeypatch, tmp_path, scenario)
    fsi_path = tmp_path / "schedule-fsi.csv"
    tr_path = tmp_path / "schedule-tr.csv"

    run_schedule_fsi(
        input_path=_FIXTURE, config_path=config, output_path=fsi_path, financial_year=_FY
    )
    tr = run_schedule_tr(
        input_path=_FIXTURE, config_path=config, output_path=tr_path, financial_year=_FY
    )

    exact = {
        country.iso: _exact_credit(country, tax="1", income="3", fx="1") for country in countries
    }
    assert exact == {"BM": Fraction(10, 3), "KY": Fraction(10, 3)}
    _, *fsi_rows = _rows(fsi_path)
    # Cols 1 and 7: the portal country code and the filed relief.
    fsi_relief: dict[str, Fraction] = {}
    for row in fsi_rows:
        if row[6]:
            code = str(row[0])
            fsi_relief[code] = fsi_relief.get(code, Fraction(0)) + Fraction(str(row[6]))
    _, *tr_rows = _rows(tr_path)
    tr_relief = {str(row[0]): Fraction(str(row[3])) for row in tr_rows}
    by_code = {row.cbdt_country_code: row.iso_alpha2 for row in tr.per_country_rows}
    assert {by_code[code]: relief for code, relief in tr_relief.items()} == {
        iso: Fraction(_oracle(credit)) for iso, credit in exact.items()
    }
    # The fixture's own US capital gain has no foreign tax, so no TR row.
    assert fsi_relief.pop("2") == 0
    assert tr_relief == fsi_relief
    assert [row.tax_relief_inr_total for row in tr.per_country_rows] == [3, 3]


# --- Country input rows -------------------------------------------------------------

_REFUSED_KY = _Country(iso="KY", gross="6", wht="-1", payable="1")
_OK_BM = _Country(gross="60", wht="-10", payable="5")


@pytest.mark.parametrize(
    ("scenario", "extra_wht", "position", "refusal"),
    [
        (_SCENARIOS["control"], _net_zero_us(), 2, _PREFLIGHT_ZERO),
        (
            _Scenario((_READINESS, _REFUSED_KY), _READINESS_TAX, _READINESS_INCOME, "1"),
            (),
            1,
            _EXACT_ZERO,
        ),
        (
            _Scenario((_OK_BM, _REFUSED_KY), _READINESS_TAX, _READINESS_INCOME, "1"),
            (),
            2,
            _EXACT_ZERO,
        ),
    ],
    ids=["skipped-us-then-bm", "bm-then-ky-both-refused", "only-ky-refused"],
)
def test_refusals_name_the_first_country_input_row_not_the_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    extra_wht: Sequence[WithholdingTax],
    position: int,
    refusal: str,
) -> None:
    """Positions count the countries the netting returns, in input order and
    before a net-zero US pair is skipped. Several refusals name the first.
    When both are present without the US pair, Cayman (1345) files before
    Bermuda (1441), so BM's input row 1 is serial 2, and KY's input row 2 is
    serial 1. Schedule FSI and TR give Form 67's readiness refusal, as run-all
    does (#1480)."""
    config = _arrange(monkeypatch, tmp_path, scenario, extra_wht=extra_wht)
    review = _form67(config, tmp_path / "form-67-review.csv", "review-csv")
    isos = {country.iso for country in scenario.countries}
    assert [row.iso_alpha2 for row in review.per_entry_rows] == [
        iso for iso in ("KY", "BM") if iso in isos
    ]

    with pytest.raises(InvariantError) as raised:
        _form67(config, tmp_path / "form-67.csv", "portal-csv")
    _assert_refusal(raised, position, refusal, _MARKERS)
    for command in ("schedule-fsi", "schedule-tr"):
        with pytest.raises(InvariantError) as raised:
            _schedule(command, config, tmp_path / f"{command}.csv", "review-csv")
        _assert_refusal(raised, position, refusal, _MARKERS)
