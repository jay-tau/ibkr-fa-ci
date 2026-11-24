"""The ``stockplan-income`` tests' config, dividend report writer and run
helpers that several test modules share.

- :data:`STOCKPLAN_INCOME_CONFIG`: a user.toml with ``[account]``,
  ``[account.foreign_tins]`` and ``[stockplan.security]`` tables; the security's
  ISIN is the one the StockPlan issuer gate accepts.
- :func:`write_dividend_report`: writes a synthetic ``IN - Dividend Report``
  .xlsx (openpyxl).
- :func:`setup_income_run`: writes that report and a config under one tmp path,
  makes an empty output folder and returns the three paths.
- :func:`run_income_cli`: runs ``stockplan-income`` (ITR-2, FY 2025-26) through
  ``ibkr_fa.cli.main`` and returns its exit code.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import openpyxl

from ibkr_fa.cli import main
from tests._stockplan_report_writers import DIVIDEND_REPORT_HEADER, STOCKPLAN_USD_FOOTER

STOCKPLAN_INCOME_CONFIG = """\
[account]
number = "MS-STOCKPLAN-1"
holder_name = "Sample Holder"
opening_date = 2025-07-14
status = "BeneficialOwner"
us_person = false

[account.foreign_tins]
US = "123456789"

[stockplan.security]
symbol = "GOOG"
isin = "US02079K1079"
"""


# (date, dividend, withholding): 2 in-FY dividends + paired NRA withholdings,
# plus one OUT-of-FY pair (2024-12-10) that the FY filter must exclude from the
# filed schedules and the summary counts.
_PAIRS: tuple[tuple[str, float, float], ...] = (
    ("2024-12-10", 2.34, -0.59),
    ("2025-12-10", 2.60, -0.65),
    ("2026-03-11", 4.12, -1.03),
)


def write_dividend_report(
    input_dir: Path,
    *,
    run_on: str = "22-Jun-2026",
    period: str = "reporting period 01-Jan-1900 to 21-Jun-2026",
    name: str = "IN - Dividend Report.xlsx",
    zero_activity: bool = False,
    symbol: str = "GOOG",
    pairs: tuple[tuple[str, float, float], ...] = _PAIRS,
) -> Path:
    """A synthetic dividend report of ``pairs`` (by default, see :data:`_PAIRS`)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IN - Dividend Report"
    ws.append([f"Alphabet, Inc. - IN - Dividend Report (report run on {run_on} for {period})"])
    ws.append(DIVIDEND_REPORT_HEADER)
    if zero_activity:
        ws.append(["No records found"])
    else:
        for date, div, wht in pairs:
            dt = _dt.datetime.fromisoformat(date)
            ws.append([dt, "Dividend Paid", div, symbol, None, None, 0.0])
            ws.append([dt, "IRS Nonresident Alien Withholding", wht, symbol, None, None, 0.0])
    ws.append([STOCKPLAN_USD_FOOTER])
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / name
    wb.save(path)
    return path


def setup_income_run(
    tmp_path: Path, *, config: str = STOCKPLAN_INCOME_CONFIG
) -> tuple[Path, Path, Path]:
    write_dividend_report(tmp_path / "in")
    cfg = tmp_path / "user.toml"
    cfg.write_text(config)
    out = tmp_path / "out"
    out.mkdir()
    return tmp_path / "in", cfg, out


def run_income_cli(input_dir: Path, cfg: Path, out: Path) -> int:
    return main(
        [
            "stockplan-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(input_dir),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
