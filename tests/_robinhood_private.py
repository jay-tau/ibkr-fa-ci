"""Private Robinhood paired-reconciliation audit loader."""

from __future__ import annotations

import csv
import os
import subprocess
import tomllib
from functools import cache
from pathlib import Path
from typing import Any, Final

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodReconciliationError,
    reconcile_robinhood_accounts,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import read_robinhood_activity
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
    read_robinhood_statement,
)
from tests._private_data import private_dataset, private_pdfs
from tests._robinhood_statements import robinhood_account_config

_DATASET: Final[tuple[str, ...]] = (
    "rh/statements",
    "rh/report/individual.csv",
    "rh/report/managed-individual.csv",
    "rh/report/ranges.toml",
)
_MALFORMED_ATTESTATION: Final[str] = "private report attestation is malformed"
_PDFINFO_TIMEOUT_SECONDS: Final[int] = 30


def private_robinhood_dataset() -> tuple[Path, Path]:
    """Return the statements folder and the ranges file. Skips when the
    whole paired dataset is absent; fails when only part of it is present."""
    statements, _individual, _managed, ranges = private_dataset(*_DATASET)
    return statements, ranges


def _attestation(ranges: Path) -> tuple[list[Any], list[Any]]:
    raw = tomllib.loads(ranges.read_text(encoding="utf-8"))
    return raw["reports"], raw["accounts"]


def _ordered_report_specs(report_specs: list[Any]) -> tuple[Any, ...]:
    """The report specs in the order the reconciliation receives them, so
    ``RobinhoodAccountReconciliation.report_index`` indexes this tuple."""
    return tuple(reversed(report_specs))


def _require_paired_attestation(report_specs: list[Any], account_specs: list[Any]) -> None:
    """Each report spec names the activity file of exactly one account spec's
    family (``<family>.csv``), and each family has exactly one report."""
    families = [spec["family"] for spec in account_specs]
    report_families = [Path(spec["file"]).stem for spec in report_specs]
    if len(set(families)) != len(families) or sorted(report_families) != sorted(families):
        pytest.fail(_MALFORMED_ATTESTATION, pytrace=False)


def _reconcile_private_accounts(
    statements_root: Path, ranges: Path
) -> RobinhoodAccountsReconciliation:
    report_specs, account_specs = _attestation(ranges)
    _require_paired_attestation(report_specs, account_specs)

    statements = []
    for path in private_pdfs(statements_root):
        if "crypto" in path.relative_to(statements_root).parts:
            continue
        statements.append(read_robinhood_statement(path))
    bundles = validate_robinhood_statement_bundles(statements)

    ordered_specs = _ordered_report_specs(report_specs)
    reports = tuple(
        RobinhoodActivityReportEvidence(
            events=read_robinhood_activity(ranges.parent / spec["file"]),
            start_date=spec["start_date"],
            end_date=spec["end_date"],
        )
        for spec in ordered_specs
    )
    opening_by_family = {
        RobinhoodStatementFamily(spec["family"]): spec["opening_date"] for spec in account_specs
    }
    accounts = tuple(
        robinhood_account_config(
            bundle.account_number,
            opening_date=opening_by_family[bundle.family],
            address="private statement",
            zip_code="",
            source="private statement",
        )
        for bundle in bundles
    )
    paired = reconcile_robinhood_accounts(bundles, reports, accounts)
    # The reconciliation binds each report by its evidence; it must land on
    # the account whose family the report's file names.
    if any(
        Path(ordered_specs[account.report_index]["file"]).stem != account.source_bundle.family.value
        for account in paired.accounts
    ):
        pytest.fail(_MALFORMED_ATTESTATION, pytrace=False)
    return paired


@cache
def load_private_accounts_reconciliation() -> RobinhoodAccountsReconciliation:
    """Load the gitignored bundle and return one cached, privacy-safe proof.

    Failures are reported after the ``except`` block, so pytest prints no
    chained source exception."""

    statements_root, ranges = private_robinhood_dataset()
    try:
        paired = _reconcile_private_accounts(statements_root, ranges)
    except (
        KeyError,
        OSError,
        ParseError,
        RobinhoodReconciliationError,
        TypeError,
        ValueError,
    ):
        paired = None
    if paired is None:
        pytest.fail("private paired Robinhood reconciliation failed", pytrace=False)
    return paired


def _report_records(path: Path) -> tuple[tuple[str, ...], ...]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        records = [tuple(row) for row in csv.reader(source, strict=True)]
    return tuple(records[1 : records.index(("",))])


@cache
def load_private_report_records() -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Each activity report's rows read with ``csv`` alone, without the
    parser, indexed like ``RobinhoodAccountReconciliation.report_index``.
    Row ``n`` (from 0) is the parser's source record ``n + 1``."""

    _statements_root, ranges = private_robinhood_dataset()
    try:
        report_specs, _account_specs = _attestation(ranges)
        records = tuple(
            _report_records(ranges.parent / spec["file"])
            for spec in _ordered_report_specs(report_specs)
        )
    except (KeyError, OSError, TypeError, ValueError, csv.Error):
        records = None
    if records is None:
        pytest.fail("private Robinhood report rows can't be read", pytrace=False)
    return records


def report_cusip(description: str) -> str | None:
    """The CUSIP a report Description cell prints on its own ``CUSIP:`` line."""
    cusips = [
        line.removeprefix("CUSIP: ")
        for line in description.splitlines()
        if line.startswith("CUSIP: ")
    ]
    return cusips[0] if len(cusips) == 1 else None


def private_robinhood_pdfs(role: str) -> tuple[Path, list[Path]]:
    """Return ``rh`` and every PDF, sorted, in its folders whose path names
    ``role`` (letter case ignored). Skips when ``rh`` or every such folder is
    absent, as ``private_dataset`` does for a missing part, and when those
    folders hold no PDF, so a check that reads no PDF never passes."""

    [root] = private_dataset("rh")

    def names_role(relative: Path) -> bool:
        return role in " ".join(part.lower() for part in relative.parts)

    errors: list[OSError] = []
    folders = [Path(folder) for folder, _subfolders, _names in os.walk(root, onerror=errors.append)]
    if errors:
        pytest.fail("a private dataset folder can't be read", pytrace=False)
    if not any(names_role(folder.relative_to(root)) for folder in folders):
        pytest.skip(f"private dataset rh/<{role} folder> is unavailable")
    pdfs = [path for path in private_pdfs(root) if names_role(path.parent.relative_to(root))]
    if not pdfs:
        pytest.skip(f"private dataset rh/<{role} folder> holds no PDF")
    return root, pdfs


def read_private_statement(path: Path) -> RobinhoodStatement | str:
    """The parsed statement, or the parser's refusal message. Failing after
    the ``except`` block keeps pytest from printing the source exception."""
    try:
        return read_robinhood_statement(path)
    except ParseError as exc:
        return exc.message


def private_pdf_page_count(path: Path) -> int:
    """The page count ``pdfinfo`` reads from the PDF's page tree: a reader
    independent of the ``pdftotext`` text the parsers split into pages."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=_PDFINFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    counts = (
        []
        if result is None
        else [
            line.removeprefix("Pages:").strip()
            for line in result.stdout.splitlines()
            if line.startswith("Pages:")
        ]
    )
    if len(counts) != 1 or not counts[0].isdecimal():
        pytest.fail("a private PDF's page count can't be read", pytrace=False)
    return int(counts[0])
