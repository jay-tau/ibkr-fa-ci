"""Relief the portal's read-only Schedule FSI cell would overwrite (#900).

The issue's example: a $10 US dividend with $3 withheld, at a constant ₹100/$,
and ₹400 of Indian tax payable. Form 67 caps the credit at the treaty rate,
₹250, but the portal fills the FSI relief itself as min(₹300, ₹400) = ₹300. So
every Schedule FSI and TR output refuses, in every format, while standalone
Form 67 still files the ₹250 credit.
"""

from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest

from ibkr_fa.cli import main
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiFiledValues
from ibkr_fa.output import Subcommand
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fsi_compute import _compute_schedule_fsi_for_pipeline
from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL as _REFUSAL
from tests._writer_stubs import writer_type

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_USER_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true

[account.foreign_tins]
US = "123456789"

[fx]
max_carry_forward_days = 4
"""
_TAX_PAYABLE_400 = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "400"
"""
_FORMATS = ("filing-csv", "review-csv", "xlsx")
# The example's computed (₹250) and portal (₹300) relief, as whole tokens. The
# refusal keeps only their context keys, which stderr names (#1046, #1252).
_RELIEF_FIGURES = re.compile(r"\b(250|300)\b")


@pytest.fixture
def example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The example's statement and config, priced at a constant ₹100/$."""
    rates = {date(2024, 1, 1) + timedelta(days=day): Decimal("100") for day in range(1100)}
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)
    text = _FIXTURE.read_text(encoding="utf-8")
    # Each amount is in its row and in that section's Total row.
    for old, new in ((",2.60,", ",10.00,"), (",-0.39,", ",-3.00,")):
        assert text.count(old) == 2
        text = text.replace(old, new)
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML + _TAX_PAYABLE_400, encoding="utf-8")
    return statement, config


def _schedule(command: str, statement: Path, config: Path, output: Path, *extra: str) -> int:
    return main(
        [
            command,
            "--input",
            str(statement),
            "--config",
            str(config),
            "--output",
            str(output),
            "--financial-year",
            "2025-26",
            *(("--itr-form", "ITR-2") if command == "schedule-tr" else ()),
            *extra,
        ]
    )


def test_the_example_flags_the_dividend_row(example: tuple[Path, Path]) -> None:
    statement, config = example

    context = _compute_schedule_fsi_for_pipeline(
        input_path=statement, config_path=config, financial_year=FinancialYear(2025)
    )

    (dividends,) = (
        row for row in context.fsi_result.rows if row.head_of_income is HeadOfIncome.OTHER_SOURCES
    )
    assert dividends.filed == ScheduleFsiFiledValues(
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("300"),
        tax_payable_inr=Decimal("400"),
        tax_relief_inr=Decimal("250"),
        dtaa_article="Article 25(2)(a)",
    )
    assert dividends.filed.dtaa_article_number == "25"
    assert dividends.filed.portal_relief_inr == Decimal("300")
    assert dividends.filed.portal_overwrites_relief
    assert [row for row in context.fsi_result.rows if row.filed.portal_overwrites_relief] == [
        dividends
    ]


@pytest.mark.parametrize("command", ["schedule-fsi", "schedule-tr"])
@pytest.mark.parametrize("format_id", _FORMATS)
def test_every_fsi_and_tr_format_refuses_and_removes_the_previous_output(
    example: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    format_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement, config = example
    output = tmp_path / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    output.write_text("STALE", encoding="utf-8")
    writer = writer_type(Subcommand.FSI if command == "schedule-fsi" else Subcommand.TR, format_id)
    write = create_autospec(writer.write, side_effect=writer.write)
    monkeypatch.setattr(writer, "write", write)

    rc = _schedule(command, statement, config, output, "--format", format_id)

    assert rc == 4
    write.assert_not_called()
    assert not output.exists()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"error: {_REFUSAL}\n")
    assert _RELIEF_FIGURES.search(captured.err) is None


def test_run_all_refuses_before_any_schedule_writes(
    example: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ibkr_fa import cli  # noqa: PLC0415

    statement, config = example
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-os.csv").write_text("STALE", encoding="utf-8")
    invoke = Mock(wraps=cli._invoke_run_all_handler)
    monkeypatch.setattr(cli, "_invoke_run_all_handler", invoke)

    rc = main(
        [
            "run-all",
            "--fy-input",
            str(statement),
            "--financial-year",
            "2025-26",
            "--format",
            "review-csv",
            "--itr-form",
            "ITR-2",
            "--config",
            str(config),
            "--output-dir",
            str(out),
        ]
    )

    assert rc == 4
    invoke.assert_not_called()
    assert list(out.iterdir()) == []
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"error: run-all: {_REFUSAL}\n")
    assert _RELIEF_FIGURES.search(captured.err) is None


def test_standalone_form67_still_files_the_treaty_capped_credit(
    example: tuple[Path, Path], tmp_path: Path
) -> None:
    statement, config = example
    output = tmp_path / "form-67.csv"

    assert _schedule("form-67", statement, config, output) == 0

    with output.open(encoding="utf-8-sig", newline="") as fh:
        header, row = list(csv.reader(fh))
    filed = dict(zip(header, row, strict=True))
    assert filed["Income from outside India"] == "1000"
    assert filed["Amount"] == "300"  # foreign tax paid
    assert filed["Total foreign tax credit claimed"] == "250"


@pytest.mark.parametrize("command", ["schedule-fsi", "schedule-tr"])
@pytest.mark.parametrize("format_id", _FORMATS)
@pytest.mark.parametrize("override", ["matching", "blank"])
def test_relief_the_portal_keeps_still_writes_in_every_format(
    tmp_path: Path, command: str, format_id: str, override: str
) -> None:
    """The real-rate fixture's 15% withholding gives fractional rupees whose
    relief equals the tax paid, so the portal keeps it; without a tax-payable
    override the relief is blank, nothing is compared, and the blank Indian tax
    payable makes the run exit 3 (#1328)."""
    config = tmp_path / "user.toml"
    config.write_text(
        _USER_TOML + (_TAX_PAYABLE_400 if override == "matching" else ""), encoding="utf-8"
    )
    output = tmp_path / f"{command}.{'xlsx' if format_id == 'xlsx' else 'csv'}"
    context = _compute_schedule_fsi_for_pipeline(
        input_path=_FIXTURE, config_path=config, financial_year=FinancialYear(2025)
    )
    compared = [row.filed.portal_relief_inr is not None for row in context.fsi_result.rows]
    assert any(compared) is (override == "matching")

    rc = _schedule(command, _FIXTURE, config, output, "--format", format_id)

    assert rc == (0 if override == "matching" else 3)
    assert output.is_file()
