"""#1447: a fractional filed Form 67 Rate(%) in the upload ``form-67.csv`` is
manual work in every command that writes it, as Fidelity CMA already counts it.

The portal's CSV importer may blank such a rate, so the filer enters it by hand
after upload. ``form-67`` (``portal-csv``), ``run-all`` (default format) and
every ``*-income`` command exit 3; each ``*-all`` command shows its income leg
as manual work. The file keeps its exact rate and the warning is unchanged.
Integral rates (an unsnapped 15.00 and a 25.00 snapped to the treaty rate),
``form-67 --format review-csv``/``xlsx`` and ``run-all --format xlsx`` keep
their exit codes.
"""

from __future__ import annotations

import csv
from collections import ChainMap
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from ibkr_fa import cli
from ibkr_fa import pipeline_robinhood as robinhood
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.models.form67_entry import Form67Entry, Form67SourceOfIncome
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.output.schedules.form67 import fractional_rate_warnings
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline_stockplan_income import run_stockplan_income
from tests._fidelity_cma_bundle import BUNDLE_CONFIG, FINALIZED_2025, bundle_inputs
from tests._form67_compute_inputs import upload_entry as _entry
from tests._form67_overrides import FORM67_READY_OVERRIDE
from tests._ftc_warnings import fractional_rate_warning
from tests._ibkr_form67_runs import US_TAX_PAYABLE as _TAX_PAYABLE
from tests._ibkr_form67_runs import wht_inputs as _wht_inputs
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML as _TIN
from tests._netbenefits import patch_pdftotext
from tests._netbenefits_e2e import NB_E2E_PDF_TEXT, nb_e2e_bundle
from tests._robinhood_cli import robinhood_main
from tests._robinhood_income_inputs import income_base_events, income_packet
from tests._robinhood_pipeline_stubs import stub_relief_run
from tests._robinhood_refusals import FRACTIONAL_RATE_ACTION
from tests._stockplan_income import STOCKPLAN_INCOME_CONFIG, write_dividend_report


def _warning(rate: str) -> str:
    return fractional_rate_warning(rate, "2")


def _filed_rates(path: Path) -> list[str]:
    """The upload CSV's column 6 (``Rate(%)``), one value per row."""
    with path.open(encoding="utf-8", newline="") as handle:
        header, *rows = csv.reader(handle)
    return [row[header.index("Rate(%)")] for row in rows]


def _leg_lines(stderr: str) -> list[list[str]]:
    """The ``*-all`` summary's status lines, each split into words."""
    return [
        line.split()
        for line in stderr.splitlines()
        if line.split()[:1] in (["ok"], ["manual-fill"], ["ERROR"])
    ]


def _status(lines: list[str]) -> str:
    """The ``run-all`` summary's ``STATUS:`` line."""
    [status] = [line for line in lines if line.startswith("STATUS:")]
    return status


# ---- the shared condition -------------------------------------------------------

_FRACTIONAL: Final = _entry()  # files 15.01
_INTEGRAL: Final = _entry(
    income=Decimal("100"),
    tax=Decimal("25"),
    normal_tax=Decimal("25"),
    section_90_credit=Decimal("25"),
    total_credit=Decimal("25"),
)
_BLANK_FRACTIONAL: Final = _entry(normal_tax=None, section_90_credit=None, total_credit=None)


def _second(entry: Form67Entry) -> Form67Entry:
    return replace(entry, serial_number=2, source_of_income=Form67SourceOfIncome.INTEREST_INCOME)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param((_FRACTIONAL,), True, id="ready-fractional"),
        pytest.param((_INTEGRAL, _second(_FRACTIONAL)), True, id="ready-one-fractional"),
        pytest.param((_INTEGRAL,), False, id="ready-integral"),
        pytest.param((_BLANK_FRACTIONAL,), False, id="blank-fractional"),
        pytest.param((_FRACTIONAL, _second(_BLANK_FRACTIONAL)), False, id="blank-beside"),
        pytest.param((), False, id="no-entries"),
    ],
)
def test_ready_with_fractional_rate(entries: tuple[Form67Entry, ...], expected: bool) -> None:
    """True only for a READY result with a fractional filed rate."""
    assert Form67ComputeResult(entries=entries).ready_with_fractional_rate is expected


@pytest.mark.parametrize(
    ("entry", "rate", "fractional"),
    [
        pytest.param(_FRACTIONAL, "15.01", True, id="15.01"),
        pytest.param(_INTEGRAL, "25.00", False, id="25.00"),
        pytest.param(
            _entry(
                income=Decimal("100"),
                tax=Decimal("12.5"),
                section_90_credit=Decimal("12"),
                total_credit=Decimal("12"),
            ),
            "12.50",
            True,
            id="12.50",
        ),
        pytest.param(_BLANK_FRACTIONAL, "15.01", True, id="blank-15.01"),
    ],
)
def test_fractional_rate_warnings_warn_for_exactly_the_rows_the_predicate_marks(
    entry: Form67Entry, rate: str, fractional: bool
) -> None:
    assert str(entry.filed.tax_paid_rate_percent) == rate
    assert entry.filed.tax_paid_rate_is_fractional is fractional
    assert fractional_rate_warnings((entry,)) == ((_warning(rate),) if fractional else ())


# ---- IBKR: form-67 and run-all ------------------------------------------------

# (dividend, tax, filed rate): 12.50 is fractional; 15.00 is integral unsnapped;
# 0.37 on 1.46 is 25.34%, which snaps to the 25% treaty rate.
_IBKR_FRACTIONAL: Final = ("100.00", "12.50", "12.50")
_IBKR_CASES: Final = [
    pytest.param(*_IBKR_FRACTIONAL, 3, id="fractional-12.50"),
    pytest.param("100.00", "15.00", "15.00", 0, id="integral-15.00"),
    pytest.param("1.46", "0.37", "25.00", 0, id="snapped-25.00"),
]
_TRADES_ROWS: Final = ("Trades,Data,", "Trades,SubTotal,", "Trades,Total,")


def _ibkr_inputs(tmp_path: Path, dividend: str, tax: str) -> tuple[Path, Path]:
    """The FY 2025-26 withholding statement with its Trades dropped, one USD
    ``dividend`` taxed USD ``tax``, a US DIVIDEND override and a US TIN."""
    statement, config = _wht_inputs(tmp_path, dividend, f"-{tax}", config=_TIN + _TAX_PAYABLE)
    lines = statement.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(_TRADES_ROWS)]
    assert len(kept) < len(lines)
    statement.write_text("".join(kept), encoding="utf-8")
    return statement, config


def _form67(statement: Path, config: Path, out: Path, *extra: str) -> int:
    return main(
        [
            "form-67",
            *("--input", str(statement), "--config", str(config), "--output", str(out)),
            *("--financial-year", "2025-26", *extra),
        ]
    )


def _run_all(statement: Path, config: Path, out: Path, *extra: str) -> int:
    return main(
        [
            "run-all",
            *("--fy-input", str(statement), "--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", *extra),
        ]
    )


@pytest.mark.parametrize(("dividend", "tax", "rate", "exit_code"), _IBKR_CASES)
def test_form67_upload_with_a_fractional_rate_exits_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dividend: str,
    tax: str,
    rate: str,
    exit_code: int,
) -> None:
    statement, config = _ibkr_inputs(tmp_path, dividend, tax)
    out = tmp_path / "form-67.csv"

    assert _form67(statement, config, out) == exit_code

    captured = capsys.readouterr()
    assert _filed_rates(out) == [rate]
    assert captured.err.splitlines().count(_warning(rate)) == (exit_code == 3)
    assert _warning(rate) not in captured.out


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_form67_formats_that_write_no_upload_keep_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_id: str
) -> None:
    statement, config = _ibkr_inputs(tmp_path, *_IBKR_FRACTIONAL[:2])

    assert _form67(statement, config, tmp_path / "form-67.out", "--format", format_id) == 0

    assert capsys.readouterr().err.splitlines().count(_warning("12.50")) == 1


@pytest.mark.parametrize(("dividend", "tax", "rate", "exit_code"), _IBKR_CASES)
def test_run_all_reports_a_fractional_upload_rate_as_manual_fill(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dividend: str,
    tax: str,
    rate: str,
    exit_code: int,
) -> None:
    statement, config = _ibkr_inputs(tmp_path, dividend, tax)
    out = tmp_path / "out"

    assert _run_all(statement, config, out) == exit_code

    err = capsys.readouterr().err
    lines = err.splitlines()
    form67 = str(out / "form-67.csv")
    assert _filed_rates(out / "form-67.csv") == [rate]
    if exit_code == 3:
        assert lines.count(_warning(rate)) == 1
        assert _status(lines).startswith("STATUS: ACTION NEEDED")
        assert [line for line in _leg_lines(err) if line[0] != "ok"] == [
            ["manual-fill", "form-67", form67]
        ]
        detail = lines[lines.index(next(line for line in lines if line.endswith(form67))) + 1]
        assert detail.strip() == (
            "-> after upload, enter each fractional Rate(%) by hand; see the Form 67 warning above"
        )
    else:
        assert "fractional" not in err
        assert _status(lines).startswith("STATUS: OK")
        assert ["ok", "form-67", form67] in _leg_lines(err)


def test_run_all_xlsx_writes_no_upload_and_keeps_form67_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    statement, config = _ibkr_inputs(tmp_path, *_IBKR_FRACTIONAL[:2])
    out = tmp_path / "out"

    assert _run_all(statement, config, out, "--format", "xlsx") == 0

    err = capsys.readouterr().err
    assert err.splitlines().count(_warning("12.50")) == 1
    assert ["ok", "form-67", str(out / "form-67.xlsx")] in _leg_lines(err)
    assert not (out / "form-67.csv").exists()


# ---- Robinhood: robinhood-income and robinhood-all ------------------------------

# (withheld, Form 1042-S Box 7a, filed rate) on _stub_relief_run's USD 100.00
# dividend. Box 7a prints whole dollars; any other form blocks the FTC.
_ROBINHOOD_CASES: Final = [
    pytest.param("12.50", "13", "12.50", 3, id="fractional-12.50"),
    pytest.param("15.00", "15", "15.00", 0, id="integral-15.00"),
    pytest.param("25.00", "25", "25.00", 0, id="snapped-25.00"),
]


def _robinhood_relief_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tax: str, form_tax: str, rate: str
) -> None:
    """#1084's relief run with USD ``tax`` withheld and a Form 1042-S with Box 7a
    USD ``form_tax`` at ``rate``. Unlike tests/test_pipeline_robinhood.py, the
    income warnings print."""
    monkeypatch.setattr(robinhood, "_annual_form_warnings", lambda _result: ())
    monkeypatch.setattr(robinhood, "_compute_os", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(robinhood, "_write_os", lambda *_args, **_kwargs: ())
    events = tuple(
        replace(event, amount_usd=Decimal(f"-{tax}"))
        if event.kind is RobinhoodEventKind.NRA_WITHHOLDING
        else event
        for event in income_base_events()
    )
    stub_relief_run(
        tmp_path,
        monkeypatch,
        "US",
        events=events,
        packet=income_packet(tax=form_tax, rate=rate),
    )


@pytest.mark.parametrize(("tax", "form_tax", "rate", "exit_code"), _ROBINHOOD_CASES)
def test_robinhood_income_is_manual_for_a_fractional_upload_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tax: str,
    form_tax: str,
    rate: str,
    exit_code: int,
) -> None:
    _robinhood_relief_run(tmp_path, monkeypatch, tax, form_tax, rate)

    assert robinhood_main("robinhood-income", tmp_path) == exit_code

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    fractional = exit_code == 3
    assert _filed_rates(tmp_path / "out" / "form-67.csv") == [rate]
    assert lines[0] == f"Robinhood income-ftc: {'manual' if fractional else 'ok'}"
    assert [line for line in lines if line.startswith("  Action: ")] == (
        [f"  Action: {FRACTIONAL_RATE_ACTION}"] if fractional else []
    )
    assert lines.count(f"  Warning: {_warning(rate)}") == fractional
    assert "fractional" not in captured.err


@pytest.mark.parametrize(("tax", "form_tax", "rate", "exit_code"), _ROBINHOOD_CASES)
def test_robinhood_all_income_leg_is_manual_for_a_fractional_upload_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tax: str,
    form_tax: str,
    rate: str,
    exit_code: int,
) -> None:
    _robinhood_relief_run(tmp_path, monkeypatch, tax, form_tax, rate)

    assert robinhood_main("robinhood-all", tmp_path, "--no-fetch-prices") == exit_code

    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.splitlines()]
    fractional = exit_code == 3
    status = "manual" if fractional else "ok"
    assert _filed_rates(tmp_path / "out" / "form-67.csv") == [rate]
    assert f"STATUS: {status}" in lines
    assert [status, "income-ftc"] in [line.split() for line in lines]
    assert lines.count(f"-> {FRACTIONAL_RATE_ACTION}") == fractional
    assert lines.count(f"-> warning: {_warning(rate)}") == fractional
    assert "fractional" not in captured.err


# ---- NetBenefits: netbenefits-income and netbenefits-all ------------------------

_AGENT: Final = 'withholding_agent_name = "Example Withholding Agent LLC"\n'
# (dividend, withheld, swept, filed rate) on the Feb-Mar statement: the fixture's
# USD 0.37 on USD 1.46 is 25.34%, which snaps to the 25% treaty rate.
_NETBENEFITS_CASES: Final = [
    pytest.param("1.46", "0.18", "1.28", "12.33", True, id="fractional-12.33"),
    pytest.param("100.00", "15.00", "85.00", "15.00", False, id="integral-15.00"),
    pytest.param("1.46", "0.37", "1.09", "25.00", False, id="snapped-25.00"),
]


def _netbenefits_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dividend: str, tax: str, swept: str
) -> tuple[Path, Path]:
    """The e2e bundle with its Feb-Mar dividend, withholding and sweep edited,
    plus a withholding agent and a READY Form 67 override."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    text = NB_E2E_PDF_TEXT["statement-feb-mar.pdf"]
    for old, new in (
        ("1.46", dividend),
        ("0.37", tax),
        ("1.090", f"{swept}0"),
        ("$1.09", f"${swept}"),
    ):
        assert old in text
        text = text.replace(old, new)
    patch_pdftotext(monkeypatch, ChainMap({"statement-feb-mar.pdf": text}, NB_E2E_PDF_TEXT))
    account = "[netbenefits.account]\n"
    toml = config.read_text(encoding="utf-8")
    assert toml.count(account) == 1
    config.write_text(
        toml.replace(account, account + _AGENT) + FORM67_READY_OVERRIDE, encoding="utf-8"
    )
    return bundle, config


@pytest.mark.parametrize("command", ["netbenefits-income", "netbenefits-all"])
@pytest.mark.parametrize(("dividend", "tax", "swept", "rate", "fractional"), _NETBENEFITS_CASES)
def test_netbenefits_income_is_manual_for_a_fractional_upload_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    dividend: str,
    tax: str,
    swept: str,
    rate: str,
    fractional: bool,
) -> None:
    """``netbenefits-all`` exits 3 anyway: its FA leg is always manual-fill."""
    bundle, config = _netbenefits_inputs(tmp_path, monkeypatch, dividend, tax, swept)
    out = tmp_path / "out"
    out.mkdir()

    code = main(
        [
            command,
            *("--input", str(bundle), "--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
            *(("--no-fetch-prices",) if command == "netbenefits-all" else ()),
        ]
    )

    captured = capsys.readouterr()
    assert _filed_rates(out / "form-67.csv") == [rate]
    assert captured.err.splitlines().count(f"warning: {_warning(rate)}") == fractional
    assert "fractional" not in captured.out
    if command == "netbenefits-income":
        assert code == (3 if fractional else 0)
    else:
        assert code == 3
        assert ["manual-fill" if fractional else "ok", "netbenefits-income"] in _leg_lines(
            captured.err
        )


# ---- StockPlan: stockplan-all and the income pipeline ---------------------------

# (dividend, withheld, filed rate) on 10-Dec-2025: 0.37 on 1.46 snaps to 25.00.
_STOCKPLAN_CASES: Final = [
    pytest.param(100.00, -12.50, "12.50", True, id="fractional-12.50"),
    pytest.param(100.00, -15.00, "15.00", False, id="integral-15.00"),
    pytest.param(1.46, -0.37, "25.00", False, id="snapped-25.00"),
]


def _stockplan_inputs(tmp_path: Path, dividend: float, tax: float) -> tuple[Path, Path]:
    """The StockPlan test report with one in-FY dividend, plus a READY override."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    write_dividend_report(input_dir, pairs=(("2025-12-10", dividend, tax),))
    config = tmp_path / "user.toml"
    config.write_text(STOCKPLAN_INCOME_CONFIG + FORM67_READY_OVERRIDE, encoding="utf-8")
    return input_dir, config


@pytest.mark.parametrize(("dividend", "tax", "rate", "fractional"), _STOCKPLAN_CASES)
def test_stockplan_all_income_leg_is_manual_for_a_fractional_upload_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dividend: float,
    tax: float,
    rate: str,
    fractional: bool,
) -> None:
    """The real income leg; the FA leg and the Schedule CG check are stubbed to
    their success codes, as tests/test_cli_stockplan_all.py does."""
    input_dir, config = _stockplan_inputs(tmp_path, dividend, tax)
    out = tmp_path / "out"

    def stockplan_fa(args: SimpleNamespace) -> int:
        Path(args.output).write_text("stub\n", encoding="utf-8")
        return cli._EXIT_MANUAL_FILL_REQUIRED

    monkeypatch.setattr(cli, "_run_stockplan_fa", stockplan_fa)
    monkeypatch.setattr(cli, "_run_stockplan_cg_status", lambda _args: cli._SOURCE_NOT_REQUIRED)

    code = main(
        [
            "stockplan-all",
            *("--input", str(input_dir), "--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2", "--no-fetch-prices"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 3  # Table A2 is always manual-fill
    assert _filed_rates(out / "form-67.csv") == [rate]
    assert captured.err.splitlines().count(f"warning: {_warning(rate)}") == fractional
    assert ["manual-fill" if fractional else "ok", "stockplan-income"] in _leg_lines(captured.err)


@pytest.mark.parametrize(("dividend", "tax", "rate", "fractional"), _STOCKPLAN_CASES)
def test_stockplan_income_with_proven_sales_requires_manual_fill_for_a_fractional_rate(
    tmp_path: Path, dividend: float, tax: float, rate: str, fractional: bool
) -> None:
    input_dir, config = _stockplan_inputs(tmp_path, dividend, tax)
    out = tmp_path / "out"
    out.mkdir()

    report = run_stockplan_income(
        input_dir=input_dir,
        config_path=config,
        output_dir=out,
        financial_year=FinancialYear(2025),
        cg_step_checks_sales=True,
    )

    assert _filed_rates(out / "form-67.csv") == [rate]
    assert report.manual_fill_warnings.count(_warning(rate)) == fractional
    assert report.manual_fill_required is fractional


# ---- Fidelity CMA: unchanged ----------------------------------------------------

_CMA_READY: Final = (
    FORM67_READY_OVERRIDE
    + """
[[form67.tax_payable_overrides]]
country = "US"
source = "INTEREST_INCOME"
tax_payable_normal_inr = "100"
"""
)
# USD 0.80 of eligible tax on each withheld in-FY payment files 5.00.
_CMA_INTEGRAL: Final = (
    FINALIZED_2025.replace(
        '{ payment_date = 2025-05-30, amount_usd = "1.00" }',
        '{ payment_date = 2025-05-30, amount_usd = "0.80" }',
    )
    .replace(
        '{ payment_date = 2025-08-29, amount_usd = "1.00" }',
        '{ payment_date = 2025-08-29, amount_usd = "0.80" }',
    )
    .replace('amount_usd = "0.75"', 'amount_usd = "0.80"')
)


@pytest.mark.parametrize(
    ("tax_years", "rate", "exit_code", "status"),
    [
        pytest.param(FINALIZED_2025, "5.73", 3, "ACTION NEEDED", id="fractional-5.73"),
        pytest.param(_CMA_INTEGRAL, "5.00", 0, "complete", id="integral-5.00"),
    ],
)
def test_fidelity_cma_income_already_counts_a_fractional_upload_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tax_years: str,
    rate: str,
    exit_code: int,
    status: str,
) -> None:
    """The golden bundle with tax year 2025 finalized and exact US DIVIDEND and
    INTEREST_INCOME tax payable, so Form 67 is READY and the FTC complete."""
    statements, tax_forms, config = bundle_inputs(tmp_path, monkeypatch, {})
    assert _CMA_INTEGRAL.count('"0.80"') == 3
    config.write_text(BUNDLE_CONFIG + tax_years + _CMA_READY, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    code = main(
        [
            "fidelity-cma-income",
            *("--input", str(statements), "--tax-forms", str(tax_forms)),
            *("--config", str(config), "--output-dir", str(out)),
            *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ]
    )

    captured = capsys.readouterr()
    assert code == exit_code
    assert _filed_rates(out / "form-67.csv") == [rate]
    assert f"  FTC status: {status}." in captured.out.splitlines()
    assert captured.err.splitlines().count(f"warning: {_warning(rate)}") == (exit_code == 3)
