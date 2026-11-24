"""Schedule FA run helpers and texts that the whitespace-only Name of entity
tests (#1369) share with several Robinhood Schedule FA test modules (#1872).

- ``IBKR_NAV_STATEMENT`` and ``IBKR_FLEX_STATEMENT``: the synthetic IBKR Activity
  Statement with NAV and the Flex year-end statement fixtures;
- ``XQBB_NAME`` and ``XQBE_NAME``: XQBB's and XQBE's security descriptions in
  both statements;
- ``SCHEDULE_FA_SUFFIXES``: the output file suffix of each CSV or workbook
  ``schedule-fa`` format;
- ``A3_ASCII_ERROR``: the ``str(InvariantError)`` text when the A3 bulk-upload
  text is not ASCII, which every test that pins that whole text reads (#1983);
  ``A3_ASCII_REFUSAL``: its stderr line;
- ``NAME_ACTION``: the Robinhood ``a3-identity`` action for a blank Name of entity;
- ``capture_main``: runs the CLI on an argv, captured;
- ``output_table``: every CSV row, or every row of each workbook sheet;
- ``edit_statement_names``: a statement copy with XQBB's and XQBE's descriptions
  replaced;
- ``schedule_fa_with_name``: an IBKR ``schedule-fa`` run on such a copy;
- ``robinhood_all_legs``: each ``robinhood-all`` leg's status, read from its stdout.
"""

from __future__ import annotations

import csv
from functools import partial
from pathlib import Path

import openpyxl
import pytest

from ibkr_fa.cli import main
from tests._fa_cli_runs import CliRun as _Run
from tests._fa_cli_runs import capture
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG

_FIXTURES = Path(__file__).parent / "fixtures"
IBKR_NAV_STATEMENT = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
IBKR_FLEX_STATEMENT = _FIXTURES / "e2e_year_end_2025.csv"
XQBB_NAME = "SAMPLE ISSUER BB INC"
XQBE_NAME = "SAMPLE ISSUER BE CORP"
SCHEDULE_FA_SUFFIXES = {"filing-csv": ".csv", "review-csv": ".csv", "xlsx": ".xlsx"}
A3_ASCII_ERROR = "InvariantError: A3 bulk-upload text must be ASCII (context keys: field, value)"
A3_ASCII_REFUSAL = f"error: {A3_ASCII_ERROR}\n"
NAME_ACTION = (
    "a3-identity: a Table A3 Name of entity is blank; set name to the issuer's legal "
    "name in the [[fa.entities]] entry for its ISIN and re-run, or fill it by hand "
    "before filing. Copy the review file's Address of entity and ZIP Code into address "
    "and zip too (the issuer's own if they are blank): an entry without address stops "
    "the next run until you verify the address it finds, and an available automatic "
    "address and ZIP take precedence unless prefer_configured_address = true"
)


def capture_main(run_dir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]) -> _Run:
    """Run ``argv``; return its exit, its streams with ``run_dir`` masked, and
    its output folder."""
    return capture(run_dir, capsys, partial(main, argv))


def output_table(path: Path) -> list[list[object]]:
    """Every CSV row, or every row of each workbook sheet."""
    if path.suffix == ".xlsx":
        workbook = openpyxl.load_workbook(path)
        return [list(row) for sheet in workbook for row in sheet.iter_rows(values_only=True)]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [list(row) for row in csv.reader(handle)]


def edit_statement_names(
    statement: Path, run_dir: Path, xqbb: str, *, xqbe: str = XQBE_NAME
) -> Path:
    """``statement`` with XQBB's and XQBE's descriptions replaced, quoted."""
    text = statement.read_text(encoding="utf-8")
    for old, new in ((XQBB_NAME, xqbb), (XQBE_NAME, xqbe)):
        cell = f'"{old}"' if statement == IBKR_FLEX_STATEMENT else f",{old},"
        assert text.count(cell) == 1, old
        text = text.replace(cell, f'"{new}"' if statement == IBKR_FLEX_STATEMENT else f',"{new}",')
    edited = run_dir / "statement.csv"
    edited.write_text(text, encoding="utf-8")
    return edited


def schedule_fa_with_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
    xqbb: str,
    fmt: str,
    *,
    statement: Path = IBKR_NAV_STATEMENT,
    config: str = _FULL_CONFIG,
) -> _Run:
    """``schedule-fa`` for CY 2025 on ``statement`` with XQBB's description
    ``xqbb``, ``config`` and a flat $100 close every session."""
    run_dir = tmp_path / label
    (run_dir / "out").mkdir(parents=True)
    (run_dir / "user.toml").write_text(config, encoding="utf-8")
    suffix = SCHEDULE_FA_SUFFIXES.get(fmt, ".json")
    return capture_main(
        run_dir,
        capsys,
        [
            "schedule-fa",
            *("--input", str(edit_statement_names(statement, run_dir, xqbb))),
            *("--config", str(run_dir / "user.toml")),
            *("--output", str(run_dir / "out" / f"schedule-fa{suffix}")),
            *("--calendar-year", "2025", "--format", fmt, "--itr-form", "ITR-2"),
            *("--price-cache-dir", str(run_dir / "cache")),
        ],
    )


def robinhood_all_legs(stdout: str) -> dict[str, str]:
    """Each robinhood-all leg's status."""
    legs = {}
    for line in stdout.splitlines():
        parts = line.split()
        if line.startswith("  ") and len(parts) == 2 and not line.startswith("    "):
            legs[parts[1]] = parts[0]
    return legs
