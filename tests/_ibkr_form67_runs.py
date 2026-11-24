"""The IBKR Form 67 command tests' statements, configs, launchers and readers,
all on the FY 2025-26 end-to-end statement (#1312, #1474).

The override and section 91 config pieces are shared with the other brokers'
advice tests too (#1469).
"""

from __future__ import annotations

import csv
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Final

import pytest

from ibkr_fa.cli import main
from tests._ibkr_run_all import FY_2025_26_INPUT
from tests._ibkr_user_toml import US_PERSON_USER_TOML
from tests._text_edits import replace_once

# ---- #1474: the statement's security moved to another country ------------------

# The statement's one security, in its Financial Instrument Information row
# (twice), its dividend and its withholding.
FIXTURE_ISIN = "US99995C1027"
KY_ISIN = "KY99995C1000"  # vendored as no_dtaa: a section 91 credit
CH_ISIN = "CH99995C1002"  # CH has no vendored DTAA row (#249)


def statement_with_isin(tmp_path: Path, isin: str) -> Path:
    """The FY 2025-26 statement with its security's ISIN set to ``isin``."""
    text = FY_2025_26_INPUT.read_text(encoding="utf-8")
    assert text.count(FIXTURE_ISIN) == 4
    assert text.count(" - US Tax,") == 1
    path = tmp_path / "statement.csv"
    path.write_text(
        text.replace(FIXTURE_ISIN, isin).replace(" - US Tax,", f" - {isin[:2]} Tax,"),
        encoding="utf-8",
    )
    return path


def config_with(tmp_path: Path, form67: str = "") -> Path:
    """Write :data:`US_PERSON_USER_TOML` and then ``form67`` as ``tmp_path / "user.toml"``."""
    path = tmp_path / "user.toml"
    path.write_text(US_PERSON_USER_TOML + form67, encoding="utf-8")
    return path


def ibkr_output_dir(tmp_path: Path) -> Path:
    """The run's empty ``tmp_path / "out"``, made with mode 0o700."""
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    return out


def missing_tax_warnings(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Every printed line holding the missing Indian tax warning."""
    captured = capsys.readouterr()
    return [
        line
        for line in (captured.out + captured.err).splitlines()
        if "INDIAN TAX CALCULATION REQUIRED" in line
    ]


SECTION_91_INPUTS = """
[form67.section_91]
total_indian_tax_before_91_relief_inr = "30000"
total_income_inr = "100000"
"""
KY_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "KY"
source = "DIVIDEND"
tax_payable_normal_inr = "3000"
"""
CH_OVERRIDE = KY_OVERRIDE.replace('"KY"', '"CH"')

# ---- #1312: the statement's dividend and withholding edited ---------------------

US_TAX_PAYABLE: Final = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "99999999"
"""
_WHT_DESCRIPTION: Final = "XQBA(US99995C1027) Cash Dividend USD 0.26 per Share - US Tax"

FSI_TAX: Final = "Tax paid outside India"
TR_RELIEF: Final = (
    "Total tax relief available (total of (e) of Schedule FSI in respect of each country)"
)
F67_RATE: Final = "Tax paid outside India - Rate"  # column 6


def wht_inputs(
    tmp_path: Path, dividend: str, tax: str, *, reversal: str | None = None, config: str = ""
) -> tuple[Path, Path]:
    """The FY 2025-26 withholding fixture with its XQBA dividend set to
    ``dividend``, its 12-09-2025 US tax to ``tax``, an optional 20-10-2025
    ``reversal`` row, and both Total rows the exact sums; and the fixture's
    config plus ``config``."""
    text = FY_2025_26_INPUT.read_text(encoding="utf-8")
    text = replace_once(text, "(Ordinary Dividend),2.60,", f"(Ordinary Dividend),{dividend},")
    text = replace_once(text, "Dividends,Data,Total,,,2.60,", f"Dividends,Data,Total,,,{dividend},")
    rows = (
        [("12-09-2025", tax)]
        if reversal is None
        else [("12-09-2025", tax), ("20-10-2025", reversal)]
    )
    with localcontext(Context(prec=1200)):
        total = sum((Decimal(amount) for _, amount in rows), Decimal(0))
    text = replace_once(
        text,
        f"Withholding Tax,Data,USD,12-09-2025,{_WHT_DESCRIPTION},-0.39,,,,,,,,,,,\n"
        "Withholding Tax,Data,Total,,,-0.39,",
        "".join(
            f"Withholding Tax,Data,USD,{day},{_WHT_DESCRIPTION},{amount},,,,,,,,,,,\n"
            for day, amount in rows
        )
        + f"Withholding Tax,Data,Total,,,{total},",
    )
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    config_path = tmp_path / "user.toml"
    config_path.write_text(US_PERSON_USER_TOML + config, encoding="utf-8")
    return statement, config_path


def run_command(
    command: str, statement: Path, config: Path, output: Path, format_id: str = "filing-csv"
) -> int:
    """Run ``command`` on ``statement`` for FY 2025-26, writing ``output``."""
    return main(
        [
            command,
            *("--input", str(statement), "--config", str(config), "--output", str(output)),
            *("--format", format_id, "--financial-year", "2025-26"),
            *(("--itr-form", "ITR-2") if command == "schedule-tr" else ()),
        ]
    )


def fsi_other_sources(path: Path) -> dict[str, str]:
    """The Other Sources block of a Schedule FSI filing CSV, field to value."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    block: dict[str, str] = {}
    for row in rows[rows.index(["Head of Income", "Other Sources"]) + 1 :]:
        if not any(row):
            break
        block[row[0]] = row[1]
    return block


def one_row(path: Path) -> dict[str, str]:
    """The CSV file's one data row, header to value."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        [row] = list(csv.DictReader(handle))
    return row
