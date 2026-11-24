"""Schedule FA A3 Address of entity and ZIP Code without evidence (#1289).

A holding without an ``[[fa.entities]]`` entry used to file its country name as
the issuer's address. That cell now stays blank, and the manual-fill warning
names Address of entity and ZIP Code with the holdings, as it does for Nature of
entity.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.cli import main
from tests._fa_a3_address import ADDRESS_MISSING_HEADING as _ADDRESS_BLOCK
from tests._fa_a3_address import FLEX_YEAR_END_CONFIG as _USER_TOML
from tests._fa_a3_address import ZIP_MISSING_HEADING as _ZIP_BLOCK
from tests._fa_a3_address import listed_holdings as _named
from tests._fa_a3_name_runs import IBKR_FLEX_STATEMENT as _FIXTURE
from tests._ibkr_run_all import run_all_from_blocks as _run_all

_ADDRESS = "100 Example Plaza, Sampleville, CA"
# The Flex fixture's holdings: XQBB and XQBE open, XQBD closed.
_XQBB = "XQBB (US99995B1044)"
_HOLDINGS = [_XQBB, "XQBD (US99995A1060)", "XQBE (US99995D1000)"]


def _schedule_fa(tmp_path: Path, fmt: str, config_extra: str = "") -> Path:
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML + config_extra, encoding="utf-8")
    out = tmp_path / ("fa.xlsx" if fmt == "xlsx" else "fa.csv")
    argv = ["schedule-fa", "--input", str(_FIXTURE), "--config", str(config)]
    argv += ["--output", str(out), "--calendar-year", "2025", "--format", fmt]
    assert main([*argv, "--no-fetch-prices"]) == 3
    return out


def _identity_cells(path: Path, fmt: str) -> list[tuple[object, object]]:
    """Each data row's (Address of entity, ZIP Code) cells."""
    if fmt == "xlsx":
        sheet = load_workbook(path).active
        assert sheet is not None
        return [(row[2], row[3]) for row in sheet.iter_rows(min_row=2, values_only=True)]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        _header, *rows = csv.reader(handle)
    column = 3 if fmt == "filing-csv" else 2  # the filing CSV starts with a serial number
    return [(row[column], row[column + 1]) for row in rows]


@pytest.mark.parametrize("fmt", ["review-csv", "filing-csv", "xlsx"])
def test_holding_without_an_entry_files_a_blank_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    out = _schedule_fa(tmp_path, fmt)

    blank = None if fmt == "xlsx" else ""
    assert _identity_cells(out, fmt) == [(blank, blank)] * 3
    stdout = capsys.readouterr().out
    assert "  - Address of entity\n  - ZIP Code\n  - Nature of entity\n" in stdout
    assert _named(stdout, _ADDRESS_BLOCK) == _HOLDINGS
    assert _named(stdout, _ZIP_BLOCK) == _HOLDINGS


def test_run_all_fa_leg_files_a_blank_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_all(tmp_path) == 3

    assert _identity_cells(tmp_path / "out" / "schedule-fa.csv", "review-csv") == [("", "")] * 3
    stdout = capsys.readouterr().out
    held = ["XQBB (US99995B1044)", "XQBE (US99995D1000)"]
    assert _named(stdout, _ADDRESS_BLOCK) == held
    assert _named(stdout, _ZIP_BLOCK) == held


def test_entry_without_zip_names_only_its_zip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every holding has an entry, so no address is blank; XQBB's has no zip."""
    entries = "".join(
        f'\n[[fa.entities]]\nisin = "{isin}"\naddress = "{_ADDRESS}"\n{zip_line}'
        for isin, zip_line in (
            ("US99995B1044", ""),
            ("US99995D1000", 'zip = "00001"\n'),
            ("US99995A1060", 'zip = "00001"\n'),
        )
    )
    out = _schedule_fa(tmp_path, "review-csv", entries)

    # Rows: XQBB and XQBE open, then XQBD closed.
    assert _identity_cells(out, "review-csv") == [
        (_ADDRESS, ""),
        (_ADDRESS, "00001"),
        (_ADDRESS, "00001"),
    ]
    stdout = capsys.readouterr().out
    assert "  - ZIP Code\n" in stdout
    assert "  - Address of entity\n" not in stdout
    assert _ADDRESS_BLOCK not in stdout
    assert _named(stdout, _ZIP_BLOCK) == [_XQBB]


def test_non_ascii_country_without_an_entry_files_a_blank_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Main refused this filing CSV (exit 4) only because the fallback put the
    non-ASCII country name Türkiye in the address cell. That value is gone, so
    the row files a blank address for manual fill (exit 3), as any other row
    without an entry does."""
    isin = "TR99990E1032"
    source = _FIXTURE.read_text(encoding="utf-8").replace("US99995B1044", isin)
    source = source.replace('"NASDAQ","US","20251231"', '"NASDAQ","TR","20251231"', 1)
    flex = tmp_path / "flex.csv"
    flex.write_text(source, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "fa.csv"
    argv = ["schedule-fa", "--input", str(flex), "--config", str(config), "--output", str(out)]
    argv += ["--calendar-year", "2025", "--format", "filing-csv", "--no-fetch-prices"]

    assert main(argv) == 3
    assert _identity_cells(out, "filing-csv") == [("", "")] * 3
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"XQBB ({isin})" in _named(captured.out, _ADDRESS_BLOCK)
