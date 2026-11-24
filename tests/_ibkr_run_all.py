"""The FY 2025-26 end-to-end IBKR statement, its user.toml writer and the
``run-all`` launchers the IBKR command tests share (#609).

The ``run-all`` output tests' runner is also shared by the Schedule FA blank
address and portal parity tests.

- ``RUN_ALL_ACCOUNT_TOML``: that runner's ``[account]`` and ``[fx]`` tables;
  unlike ``tests._ibkr_user_toml.US_PERSON_USER_TOML``, it sets
  ``us_person = false``.
- ``RUN_ALL_TAX_PAYABLE_TOML``: the ``[form67]`` block it appends by default,
  after ``tests._ibkr_user_toml.US_FOREIGN_TIN_TOML`` and
  ``tests._ibkr_user_toml.CUSTODIAN_TOML``.
- ``GB_DIVIDEND_LINES``: a UK holding's instrument, dividend and tax lines, each
  keyed by the end of the US line it goes after; ``GB_DIVIDEND_TOTALS``: the
  Dividends and Withholding Tax Totals edits once they join.
- ``run_all_from_blocks``: ``run-all`` for FY 2025-26, offline, on copies of both
  statements and a user.toml joined from those blocks; its keyword options pick
  the statement edits, the blocks and the flags. Returns the exit code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from tests._ibkr_user_toml import CUSTODIAN_TOML, US_FOREIGN_TIN_TOML, US_PERSON_USER_TOML
from tests._run_all_fakes import RUN_ALL_SCHEDULE_NAMES
from tests._text_edits import replace_once

_FIXTURES = Path(__file__).parent / "fixtures"
# Period is exactly FY 2025-26, with Trades, FII, Dividends and Withholding Tax.
FY_2025_26_INPUT = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
# Calendar year 2025, which does not contain FY 2025-26.
CY_2025_INPUT = _FIXTURES / "e2e_activity_statement_2025.csv"

RUN_ALL_ACCOUNT_TOML = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = false

[fx]
max_carry_forward_days = 4
"""
RUN_ALL_TAX_PAYABLE_TOML = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "100.00"
"""

# A UK dividend and its tax, after each US line of the same section. With no
# col 7 override for GB, its Form 67 entry is BLANK.
GB_DIVIDEND_LINES = {
    "NASDAQ,1,COMMON,,,,,,\n": (
        "Financial Instrument Information,Data,Stocks,XQCR,SAMPLE ISSUER CR PLC,12345,"
        "GB99970J1065,GB99970J1065,LSE,1,COMMON,,,,,,\n"
    ),
    "(Ordinary Dividend),2.60,,,,,,,,,,,\n": (
        "Dividends,Data,USD,12-09-2025,XQCR(GB99970J1065) Cash Dividend USD 0.50 per "
        "Share (Ordinary Dividend),5.00,,,,,,,,,,,\n"
    ),
    "US Tax,-0.39,,,,,,,,,,,\n": (
        "Withholding Tax,Data,USD,12-09-2025,XQCR(GB99970J1065) Cash Dividend USD 0.50 "
        "per Share - GB Tax,-0.75,,,,,,,,,,,\n"
    ),
}
# The Dividends and Withholding Tax Totals once the GB rows join their runs.
GB_DIVIDEND_TOTALS = (("Total,,,2.60,", "Total,,,7.60,"), ("Total,,,-0.39,", "Total,,,-1.14,"))


def write_e2e_config(tmp_path: Path, name: str = "user.toml") -> Path:
    """Write :data:`US_PERSON_USER_TOML` as ``tmp_path / name``."""
    config = tmp_path / name
    config.write_text(US_PERSON_USER_TOML, encoding="utf-8")
    return config


def run_all_argv(tmp_path: Path, input_path: Path) -> list[str]:
    """``run-all`` on ``input_path`` for FY 2025-26, with :data:`US_PERSON_USER_TOML`
    written in ``tmp_path`` and the outputs in ``tmp_path / "out"``."""
    config = write_e2e_config(tmp_path)
    return [
        "run-all",
        "--fy-input",
        str(input_path),
        "--financial-year",
        "2025-26",
        "--format",
        "review-csv",
        "--itr-form",
        "ITR-2",
        "--config",
        str(config),
        "--output-dir",
        str(tmp_path / "out"),
    ]


def run_all_fy(tmp_path: Path, input_path: Path) -> int:
    return main(run_all_argv(tmp_path, input_path))


def run_all_both(tmp_path: Path, fy_input: Path, cy_input: Path) -> int:
    return main([*run_all_argv(tmp_path, fy_input), "--cy-input", str(cy_input)])


# FY_2025_26_INPUT holds one XQBA round trip, one dividend and its WHT.
def run_all_from_blocks(
    tmp_path: Path,
    *,
    masked: bool = False,
    trades: bool = False,
    fractional_rate: bool = False,
    tins: bool = True,
    custodian: bool = True,
    tax_payable: bool = True,
    quiet: bool = False,
    blank_gb: bool = False,
    cy: bool = True,
) -> int:
    fy_text = FY_2025_26_INPUT.read_text(encoding="utf-8")
    cy_text = CY_2025_INPUT.read_text(encoding="utf-8")
    if not trades:
        fy_text = "".join(
            line
            for line in fy_text.splitlines(keepends=True)
            if not line.startswith(("Trades,Data,", "Trades,SubTotal,", "Trades,Total,"))
        )
    if fractional_rate:
        fy_text = replace_once(fy_text, "US Tax,-0.39,", "US Tax,-0.40,")
        fy_text = replace_once(fy_text, "Total,,,-0.39,", "Total,,,-0.40,")
    if blank_gb:
        for us_line_end, gb_line in GB_DIVIDEND_LINES.items():
            fy_text = replace_once(fy_text, us_line_end, us_line_end + gb_line)
        for old, new in GB_DIVIDEND_TOTALS:
            fy_text = replace_once(fy_text, old, new)
    if masked:
        fy_text = replace_once(fy_text, "Account,U12345678,", "Account,U********,")
        cy_text = replace_once(cy_text, "Account,U12345678,", "Account,U********,")
    fy_input = tmp_path / "fy.csv"
    fy_input.write_text(fy_text, encoding="utf-8")
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(cy_text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(
        RUN_ALL_ACCOUNT_TOML
        + (US_FOREIGN_TIN_TOML if tins else "")
        + (CUSTODIAN_TOML if custodian else "")
        + (RUN_ALL_TAX_PAYABLE_TOML if tax_payable else ""),
        encoding="utf-8",
    )
    argv = [
        "run-all",
        "--fy-input",
        str(fy_input),
        *(("--cy-input", str(cy_input)) if cy else ()),
        "--financial-year",
        "2025-26",
        "--format",
        "review-csv",
        "--itr-form",
        "ITR-2",
        "--config",
        str(config),
        "--output-dir",
        str(tmp_path / "out"),
        "--no-fetch-prices",
    ]
    if masked:
        argv.append("--allow-masked-account")
    if quiet:
        argv.append("--quiet")
    return main(argv)


def spy_on(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> Mock:
    """Replace ``module.name`` with a mock that wraps it."""
    mock = Mock(wraps=getattr(module, name))
    monkeypatch.setattr(module, name, mock)
    return mock


def seed_every_target(out: Path) -> list[str]:
    """Write an earlier run's file at every ``run-all`` target; their names."""
    out.mkdir()
    for name in RUN_ALL_SCHEDULE_NAMES:
        for target in cli._run_all_targets(out, name):
            target.write_text("an earlier run", encoding="utf-8")
    return sorted(path.name for path in out.iterdir())
