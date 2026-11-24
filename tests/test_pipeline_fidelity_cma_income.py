"""Fidelity CMA income/FTC pipeline and runtime refund-state tests."""

from __future__ import annotations

import csv
import errno
import hashlib
import os
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

import ibkr_fa.pipeline_fidelity_cma_income as income_mod
from ibkr_fa.cli import main
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.compute.ftc_eligibility import FtcEligibilityError
from ibkr_fa.config import (
    _KNOWN_FIDELITY_CMA_ELIGIBLE_TAX,
    _KNOWN_FIDELITY_CMA_US_TAX_YEAR,
    FIDELITY_CMA_FTC_STATUSES,
    FidelityCmaRefundStatus,
)
from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.normalize.fidelity_cma.form_1042s import (
    Cma1042SReconciliationError,
)
from ibkr_fa.output._cleanup import OutputRemovalError, RunOutputs
from ibkr_fa.output.base import OutputBinding, Subcommand
from ibkr_fa.output.income_bundle import INCOME_FILENAMES, INCOME_FILING_FILENAMES
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST
from ibkr_fa.parser.fidelity_cma import (
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaStatement,
    CmaWithholding,
    CmaYearEndIncomeSummary,
    to_source_record,
    validate_statement_bundle,
)
from ibkr_fa.parser.fidelity_cma_1042s import parse_1042s_packet
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import (
    FidelityCmaCgError,
    FidelityCmaCgRunReport,
    run_fidelity_cma_cg,
)
from ibkr_fa.pipeline_fidelity_cma_income import (
    FidelityCmaIncomeError,
    FidelityCmaIncomeRunReport,
    run_fidelity_cma_income,
)
from tests._crash_reports import assert_replaced_unchained
from tests._fidelity_cma import (
    HELD_OVER_24_MONTHS_REFUSAL,
    UNMATCHED_SALE_REFUSAL,
    core_transaction,
    income_year_end,
    monthly_history,
)
from tests._fidelity_cma_1042s import packet_text
from tests._fidelity_cma_1042s_reconcile import JANUARY_2025_REFUSAL
from tests._fidelity_cma_cg import (
    CARRIED_HOLDING,
    CG_EARLY_END,
    CG_WINDOW,
    HOLDING_HISTORY,
    LATE_START,
    LATER_HOLDING,
    PAST_CAP,
    SHARE_REFUSAL,
    thirds,
)
from tests._fidelity_cma_income_run import CMA_FINALIZED_2025_TAX_CONFIG as _FINALIZED_TAX_CONFIG
from tests._fidelity_cma_income_run import CMA_INCOME_CONFIG as _CONFIG
from tests._fidelity_cma_income_run import cma_income_bundle as _bundle
from tests._fidelity_cma_income_run import cma_income_statement as _statement
from tests._fidelity_cma_income_run import patch_cma_income_sources as _patch_sources
from tests._fidelity_cma_income_run import run_cma_income_cli as _main
from tests._fidelity_cma_income_run import write_cma_tax_forms as _tax_forms
from tests._ftc_warnings import BLANK_RELIEF_TIN_WARNING
from tests._ftc_warnings import FORM67_RELIEF_REFUSAL as _RELIEF_REFUSAL
from tests._fx_stubs import AuditedFxStub
from tests._writer_stubs import StubWriter, patch_writers

_ACCOUNT = "Z99-999999"
_HOLDER = "TAX PAYER"
_FY = FinancialYear(2025)

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tax_config: str = "",
    config_text: str = _CONFIG,
    statements: tuple[CmaStatement, ...] | None = None,
    year_ends: dict[int, CmaYearEndIncomeSummary] | None = None,
    seed_stale_outputs: bool = False,
    debug: bool = False,
    check_1042s_coverage: bool = False,
):
    _patch_sources(
        monkeypatch,
        statements=statements,
        year_ends=year_ends,
        check_1042s_coverage=check_1042s_coverage,
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(config_text + tax_config, encoding="utf-8")
    _tax_forms(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    if seed_stale_outputs:
        for filename in INCOME_FILING_FILENAMES:
            (out / filename).write_text("stale", encoding="utf-8")
    report = run_fidelity_cma_income(
        input_dir=tmp_path / "statements",
        tax_form_dir=tmp_path / "tax-forms",
        config_path=cfg,
        output_dir=out,
        financial_year=_FY,
        debug=debug,
    )
    return report, out


def test_debug_sidecars_are_unified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A blocked FTC withholds form-67.csv (#1453), so this run files a complete FTC.
    _, out = _run(
        tmp_path,
        monkeypatch,
        config_text=_OVERRIDE_CONFIG,
        tax_config=_FINALIZED_TAX_CONFIG,
        debug=True,
    )
    with (out / "schedule-os.csv").open(newline="", encoding="utf-8-sig") as handle:
        filing_rows = list(csv.reader(handle))
    assert len(filing_rows[0]) == 7
    assert [PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST, "47", "", "", "", "", ""] in filing_rows
    with (out / "schedule-fsi.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert len(next(csv.reader(handle))) == 2  # vertical label, value
    with (out / "schedule-tr.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert len(next(csv.reader(handle))) == 5
    with (out / "form-67.csv").open(newline="", encoding="utf-8") as handle:
        assert len(next(csv.reader(handle))) == 15
    for filename, width in (
        ("schedule-os-debug.csv", 11),
        ("schedule-fsi-debug.csv", 13),
        ("schedule-tr-debug.csv", 9),
        ("form-67-debug.csv", 17),
    ):
        with (out / filename).open(newline="", encoding="utf-8-sig") as handle:
            assert len(next(csv.reader(handle))) == width
    with (out / "schedule-os-debug.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert not any(
            row[0] == PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST for row in csv.reader(handle)
        )

    # Every sidecar is a target, so the preclear and a failed run remove it too.
    assert {path.name for path in out.iterdir()} <= set(INCOME_FILENAMES)


def test_debug_os_review_shows_cma_dividend_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review shows the line's kind, not text from the description
    (#606)."""
    _, out = _run(tmp_path, monkeypatch, debug=True)
    rows = _csv_rows(out / "schedule-os-debug.csv")
    assert [row["Distribution type"] for row in rows if row["Symbol"] == "SPAXX"] == [
        "Cash Dividend"
    ]


# ADR 0004 section 6: the statuses that can claim FTC, and those that block it.
_FTC_STATUSES = ("finalized", "no-refund-claim")
_BLOCKING_STATUSES = ("unknown", "not-filed-planned", "filed-pending")
_BLOCKED_TAIL = (
    "FTC is blocked for the entire FY until every in-FY withholding year is "
    "finalized or no-refund-claim and reconciled. To claim it, give the "
    "[[fidelity_cma.us_tax_years]] entry with tax_year = 2025 refund_status = "
    '"finalized" or "no-refund-claim", a non-empty provenance string and eligible_tax = '
    '[{ payment_date = YYYY-MM-DD, amount_usd = "0.00" }, ...] with one entry for every '
    "withholding date in 2025 on the statements: the whole-cent US tax owed on that "
    "payment, 0 allowed, never more than the statement withheld"
)
# The warning for a US tax year other than the 1042-S packet's (#1424).
_UNRECONCILABLE = (
    "US tax year 2026 can't be reconciled in this version: fidelity-cma-income reads only "
    "a Form 1042-S packet for US tax year 2025, so FTC is blocked for the entire FY "
    "whatever 2026's refund status; complete Schedule FSI, Schedule TR and Form 67 by hand"
)


def _tax_year(tax_year: int, status: str, eligible: dict[str, str] | None = None) -> str:
    """One ``[[fidelity_cma.us_tax_years]]`` table; ``eligible`` adds provenance and
    the per-date allocations, keyed by TOML date."""
    lines = ["", "[[fidelity_cma.us_tax_years]]", f"tax_year = {tax_year}"]
    lines.append(f'refund_status = "{status}"')
    if eligible is not None:
        lines.append(f'provenance = "{tax_year} IRS transcript and CA event allocation"')
        entries = ", ".join(
            f'{{ payment_date = {day}, amount_usd = "{amount}" }}'
            for day, amount in eligible.items()
        )
        lines.append(f"eligible_tax = [{entries}]")
    return "\n".join(lines) + "\n"


# sha256 of every file a --debug run writes, taken before #624 moved CMA
# income normalization and FTC eligibility out of the pipeline. The debug
# CSVs end lines with CRLF, which .gitattributes and pre-commit would rewrite
# in committed golden files, so the bytes are pinned by digest.
_PINNED_OUTPUT_SHA256 = {
    # #1453: the blocked FTC withholds form-67.csv; every other digest is unchanged.
    "blocked": {
        "schedule-os.csv": "aab02c15b4c17773b9e76b2e52bf7069ff7c954441da131dbf0af3f45baba06f",
        "schedule-os-debug.csv": "21a7b3f4485103e44be620851bf02b396a088c7f75a847c6582e304bfdcf7524",
        "schedule-fsi.csv": "0e187ec74dc0e8492fc203544aae86ea44b8f94cacf4ade0fac9fef18b4bbe10",
        "schedule-fsi-debug.csv": (
            "01e09223f08982af6748d0d471812a59fef4aefa8df066949e664280db8c9fe9"
        ),
        "schedule-tr.csv": "426056ef4fb998a4c925777ebc802fe0bf7761c3dd6f47e1147c99e3af6e56b2",
        "schedule-tr-debug.csv": "6b009fa04743964219fe6855c2edf8e024ff0706740dc61ca2cb59bfd534e976",
        "form-67-debug.csv": "7e995b4feeedd1311110fea7953d9a6fdf82721874f1673d4361bac3a779297f",
    },
    # #601: no Indian-tax override, so the blank Form 67 row withholds
    # form-67.csv; every other digest is unchanged. #1427: schedule-fsi.csv's
    # "Manual fill guidance" cell ends with the new override remedy; nothing
    # else changes. #1446: that cell drops its lending-fee by-hand clause;
    # nothing else changes.
    "finalized": {
        "schedule-os.csv": "aab02c15b4c17773b9e76b2e52bf7069ff7c954441da131dbf0af3f45baba06f",
        "schedule-os-debug.csv": "21a7b3f4485103e44be620851bf02b396a088c7f75a847c6582e304bfdcf7524",
        "schedule-fsi.csv": "830f74448129bb8f9f686ca4887aa0939331994b437acd957aa1e5f5bb00e8cf",
        "schedule-fsi-debug.csv": (
            "a5a585ff43993ed52e69d7528bcbea3155b1aabbb475af590be1c6a8efb13fa3"
        ),
        "schedule-tr.csv": "108e42a9c064989b3fe024ec53cc14b602cc0ee85d77936055ad2a3b44dd9f62",
        "schedule-tr-debug.csv": "77bf2205d4254e55dadb8892603b50a296ba2f46789947b08b472a4d579b8fce",
        "form-67-debug.csv": "1480d70ebae3f35b400cd476dae20935cdb02277d1be75ede0522f2699b9496b",
    },
}


@pytest.mark.parametrize(
    ("fixture", "tax_config"),
    [
        ("blocked", ""),
        ("finalized", _FINALIZED_TAX_CONFIG),
        # The same allocation under no-refund-claim files the same bytes.
        ("finalized", _FINALIZED_TAX_CONFIG.replace('"finalized"', '"no-refund-claim"')),
    ],
    ids=["blocked", "finalized", "no-refund-claim"],
)
def test_outputs_match_pinned_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    tax_config: str,
) -> None:
    _, out = _run(tmp_path, monkeypatch, tax_config=tax_config, debug=True)
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in out.iterdir()
    } == _PINNED_OUTPUT_SHA256[fixture]


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _fsi_fields(path: Path) -> dict[str, str]:
    """Read the vertical Schedule FSI filing CSV as a label -> value map."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {label: value for label, value in csv.reader(handle) if label}


@pytest.mark.parametrize(
    ("status", "tax_config"),
    [
        ("unknown", ""),
        *((status, _tax_year(2025, status)) for status in _BLOCKING_STATUSES),
    ],
    ids=["omitted-year", *_BLOCKING_STATUSES],
)
def test_blocking_status_emits_income_but_blocks_all_ftc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    tax_config: str,
) -> None:
    report, out = _run(tmp_path, monkeypatch, tax_config=tax_config)
    assert report.blocked_refund_years == ((2025, FidelityCmaRefundStatus(status)),)
    assert report.manual_fill_warnings == (
        f"US tax year 2025 refund status is {status}; {_BLOCKED_TAIL}",
    )
    assert report.dividend_count == 1
    assert report.interest_count == 1
    assert report.interest_total_inr_rounded == Decimal("47")
    assert report.actual_withholding_count == 1
    assert report.eligible_withholding_count == 0
    assert not report.ftc_complete
    assert report.form67_entries == 0
    # #1453: the header-only form-67.csv would read as nothing to claim.
    assert {path.name for path in out.glob("*.csv")} == {
        "schedule-os.csv",
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67-debug.csv",
    }


def test_blocked_warning_names_every_key_a_claiming_year_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1471: the blocked year's warning names the table, each key of a year
    entry and of its eligible_tax entries, and each status that can claim FTC,
    as the config loader reads them."""
    report, _ = _run(tmp_path, monkeypatch)
    (warning,) = report.manual_fill_warnings
    keys = _KNOWN_FIDELITY_CMA_US_TAX_YEAR | _KNOWN_FIDELITY_CMA_ELIGIBLE_TAX
    assert "[[fidelity_cma.us_tax_years]]" in warning
    assert {key for key in keys if re.search(rf"\b{key}\b", warning)} == keys
    assert all(f'"{status.value}"' in warning for status in FIDELITY_CMA_FTC_STATUSES)


# A withholding dated in 2025 before the FY: the statements list it, so a
# claiming 2025 entry needs an eligible_tax entry for it too (#1471).
_MARCH = replace(
    _statement(2025, 3),
    income_credits=(
        CmaIncomeCredit(
            date(2025, 3, 14), CmaIncomeKind.SPAXX_DIVIDEND, Decimal("0.40"), "99971W106"
        ),
    ),
    withholdings=(CmaWithholding(date(2025, 3, 14), Decimal("-0.10")),),
)


@pytest.mark.parametrize("status", _FTC_STATUSES)
@pytest.mark.parametrize("withheld", [False, True], ids=["zero", "withheld"])
@pytest.mark.parametrize("pre_fy", [False, True], ids=["in-fy-only", "with-pre-fy-date"])
def test_following_the_blocked_warning_unblocks_ftc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    withheld: bool,
    pre_fy: bool,
) -> None:
    """#1471: applying exactly what the blocked year's warning says to the
    config, with each amount either 0 or the statement's withholding, loads,
    passes the eligible-tax checks and unblocks FTC for that year."""
    statements = (_MARCH, *_bundle()) if pre_fy else _bundle()
    for name in ("blocked", "followed"):
        (tmp_path / name).mkdir()
    blocked, _ = _run(tmp_path / "blocked", monkeypatch, statements=statements)
    assert blocked.manual_fill_warnings == (
        f"US tax year 2025 refund status is unknown; {_BLOCKED_TAIL}",
    )

    eligible = {
        row.date.isoformat(): str(row.amount_usd.copy_abs()) if withheld else "0.00"
        for statement in statements
        for row in statement.withholdings
        if row.date.year == 2025
    }
    assert len(eligible) == (2 if pre_fy else 1)
    report, _ = _run(
        tmp_path / "followed",
        monkeypatch,
        statements=statements,
        tax_config=_tax_year(2025, status, eligible),
    )
    assert report.blocked_refund_years == ()
    assert report.eligible_withholding_count == (1 if withheld else 0)


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_emits_only_eligible_tax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    report, out = _run(
        tmp_path,
        monkeypatch,
        tax_config=_tax_year(2025, status, {"2025-04-15": "0.10"}),
    )
    assert report.blocked_refund_years == ()
    assert report.ftc_complete
    assert not report.filing_complete
    assert report.eligible_withholding_count == 1
    assert report.form67_entries == 1
    assert len(report.manual_fill_warnings) == 1
    assert "INDIAN TAX CALCULATION REQUIRED" in report.manual_fill_warnings[0]
    assert report.warnings == ()
    fsi = _fsi_fields(out / "schedule-fsi.csv")
    assert fsi["Tax payable on such income under normal provisions in India"] == ""
    # Read-only on the live portal: auto-populated as min((c), (d)), so the
    # filing worksheet contains no user-entry row for it.
    assert "Tax relief available in India (e)= (c) or (d) whichever is lower" not in fsi
    assert fsi["Relevant article of DTAA if relief claimed u/s 90 or 90A"] == ""
    [tr] = _csv_rows(out / "schedule-tr.csv")
    assert (
        tr["Total tax relief available (total of (e) of Schedule FSI in respect of each country)"]
        == ""
    )
    assert tr["Tax Relief Claimed under section (specify 90, 90A or 91)"] == ""


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_zero_tax_is_complete_without_form67_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    report, _ = _run(
        tmp_path,
        monkeypatch,
        tax_config=_tax_year(2025, status, {"2025-04-15": "0.00"}),
    )
    assert report.ftc_complete
    assert report.filing_complete
    assert report.actual_withholding_count == 1
    assert report.eligible_withholding_count == 0
    assert report.form67_entries == 0


_OVERRIDE_CONFIG = (
    _CONFIG
    + """\
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "30"

[[form67.tax_payable_overrides]]
country = "US"
source = "INTEREST_INCOME"
tax_payable_normal_inr = "5"
"""
)


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_per_source_overrides_complete_mixed_fsi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    report, out = _run(
        tmp_path,
        monkeypatch,
        config_text=_OVERRIDE_CONFIG,
        tax_config=_tax_year(2025, status, {"2025-04-15": "0.10"}),
    )
    assert report.filing_complete
    assert report.manual_fill_warnings == ()
    fsi = _fsi_fields(out / "schedule-fsi.csv")
    assert fsi["Tax payable on such income under normal provisions in India"] == "35"
    with (out / "schedule-tr.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert not any(row[0].startswith("Quicko") for row in csv.reader(handle))


def test_dividend_only_override_with_credit_interest_names_the_interest_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "Indian tax calculation required" warning's DIVIDEND override alone
    refuses when Credit Interest shares the US Other Sources row; the refusal
    names INTEREST_INCOME, which test_per_source_overrides_complete_mixed_fsi
    adds to complete the run (#1427)."""
    dividend_only = (
        _CONFIG
        + """\
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "30"
"""
    )
    with pytest.raises(InvariantError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            config_text=dividend_only,
            tax_config=_FINALIZED_TAX_CONFIG,
        )
    assert str(caught.value) == _RELIEF_REFUSAL
    assert "INTEREST_INCOME" in str(caught.value)
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize("status", _FTC_STATUSES)
@pytest.mark.parametrize("blocking", _BLOCKING_STATUSES)
def test_one_unresolved_tax_year_blocks_every_fy_ftc_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    blocking: str,
) -> None:
    report, _ = _run(
        tmp_path,
        monkeypatch,
        statements=_bundle(second_year_withholding=True),
        tax_config=_tax_year(2025, status, {"2025-04-15": "0.10"}) + _tax_year(2026, blocking),
    )
    assert report.blocked_refund_years == ((2026, FidelityCmaRefundStatus(blocking)),)
    assert report.dividend_count == 2
    assert report.actual_withholding_count == 2
    assert report.eligible_withholding_count == 0
    assert report.form67_entries == 0
    assert report.manual_fill_warnings == (_UNRECONCILABLE,)


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_ftc_status_but_unreconciled_year_soft_blocks_income_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    report, _ = _run(
        tmp_path,
        monkeypatch,
        statements=_bundle(second_year_withholding=True),
        tax_config=(
            _tax_year(2025, "finalized", {"2025-04-15": "0.10"})
            + _tax_year(2026, status, {"2026-01-15": "0.05"})
        ),
    )
    assert report.blocked_refund_years == ((2026, FidelityCmaRefundStatus(status)),)
    assert report.dividend_count == 2
    assert report.form67_entries == 0
    assert report.manual_fill_warnings == (_UNRECONCILABLE,)


@pytest.mark.parametrize("status", _FTC_STATUSES)
@pytest.mark.parametrize(
    ("eligible", "message"),
    [
        ({"2025-04-15": "0.26"}, "eligible tax cannot exceed actual statement withholding"),
        (
            {"2025-05-15": "0.10"},
            "eligible-tax dates must match every statement withholding date",
        ),
        (
            {"2025-04-15": "0.10", "2025-05-15": "0.00"},
            "eligible-tax dates must match every statement withholding date",
        ),
        ({}, "eligible-tax dates must match every statement withholding date"),
    ],
    ids=["above-actual", "wrong-date", "extra-date", "no-allocations"],
)
def test_ftc_status_allocation_mismatch_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    eligible: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(FtcEligibilityError) as caught:
        _run(tmp_path, monkeypatch, tax_config=_tax_year(2025, status, eligible))
    assert str(caught.value) == message
    assert not any((tmp_path / "out").glob("*.csv"))


@pytest.mark.parametrize(
    ("config_text", "tax_config", "code", "status_line"),
    [
        (_OVERRIDE_CONFIG, "", 3, "FTC status: BLOCKED"),
        (_OVERRIDE_CONFIG, _tax_year(2025, "filed-pending"), 3, "FTC status: BLOCKED"),
        (
            _CONFIG,
            _tax_year(2025, "no-refund-claim", {"2025-04-15": "0.10"}),
            3,
            "FTC status: ACTION NEEDED",
        ),
        (
            _OVERRIDE_CONFIG,
            _tax_year(2025, "no-refund-claim", {"2025-04-15": "0.10"}),
            0,
            "FTC status: complete",
        ),
        (
            _CONFIG,
            _tax_year(2025, "no-refund-claim", {"2025-04-15": "0.00"}),
            0,
            "FTC status: complete",
        ),
    ],
    ids=[
        "omitted-year",
        "filed-pending",
        "no-refund-claim-needs-indian-tax",
        "no-refund-claim-complete",
        "no-refund-claim-zero-tax",
    ],
)
def test_cli_exit_code_follows_ftc_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_text: str,
    tax_config: str,
    code: int,
    status_line: str,
) -> None:
    _patch_sources(monkeypatch)
    assert _main(tmp_path, config_text + tax_config) == code
    assert status_line in capsys.readouterr().out


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_failed_1042s_reconciliation_refuses_before_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise Cma1042SReconciliationError("synthetic mismatch")

    def unreachable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("FTC eligibility checked after a failed reconciliation")

    _patch_sources(monkeypatch)
    monkeypatch.setattr(income_mod, "reconcile_1042s", refuse)
    monkeypatch.setattr(income_mod, "eligible_us_withholding", unreachable)
    assert _main(tmp_path, _CONFIG + _tax_year(2025, status, {"2025-04-15": "0.10"})) == 4
    assert capsys.readouterr().err == (
        "error: Fidelity CMA Form 1042-S reconciliation refused: synthetic mismatch\n"
    )
    assert not any((tmp_path / "out").glob("*.csv"))


_WROTE = "Wrote Fidelity CMA Schedule OS / FSI / TR filing CSVs and {} for FY 2025-26."
_FILING_NAMES = ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv")


@pytest.mark.parametrize("debug", [False, True], ids=["plain", "debug"])
def test_blocked_ftc_withholds_the_official_form67(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    debug: bool,
) -> None:
    """A blocked FTC leaves Form 67 with no rows though there is US tax to
    claim, so the run writes form-67-debug.csv in place of form-67.csv and says
    why, as a BLANK Form 67 does; a stale form-67.csv is removed (#1453)."""
    _patch_sources(monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    (out / "form-67.csv").write_text("stale", encoding="utf-8")

    code = _main(tmp_path, _CONFIG, *(["--debug"] if debug else []))

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.splitlines()[0] == _WROTE.format(
        "Form 67 debug CSV (official Form 67 CSV withheld: FTC is blocked; see the warning above)"
    )
    assert "  FTC status: BLOCKED.\n" in captured.out
    assert f"US tax year 2025 refund status is unknown; {_BLOCKED_TAIL}" in captured.err
    expected = (
        set(INCOME_FILENAMES) - {"form-67.csv"} if debug else {*_FILING_NAMES, "form-67-debug.csv"}
    )
    assert {path.name for path in out.iterdir()} == expected
    with (out / "form-67-debug.csv").open(newline="", encoding="utf-8-sig") as handle:
        assert len(next(csv.reader(handle))) == 17


@pytest.mark.parametrize(
    ("config_text", "statements"),
    [
        (_CONFIG, tuple(replace(statement, withholdings=()) for statement in _bundle())),
        (_OVERRIDE_CONFIG + _FINALIZED_TAX_CONFIG, None),
    ],
    ids=["no-us-withholding", "finalized"],
)
def test_unblocked_ftc_writes_the_official_form67(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_text: str,
    statements: tuple[CmaStatement, ...] | None,
) -> None:
    """Nothing blocked: the official form-67.csv is written as before (#1453),
    header-only when the FY has no US withholding to claim."""
    _patch_sources(monkeypatch, statements=statements)

    code = _main(tmp_path, config_text)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines()[0] == _WROTE.format("official Form 67 CSV")
    assert "  FTC status: complete.\n" in captured.out
    out = tmp_path / "out"
    assert {path.name for path in out.iterdir()} == {*_FILING_NAMES, "form-67.csv"}
    rows = (out / "form-67.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == (1 if statements is not None else 2)


# #1510: each coverage refusal names FY 2025-26's whole window, then every
# edge the statements miss. #1515: the window starts by 1 January of the Form
# 1042-S's tax year too, and the refusal names every later coverage stop the
# added months could still hit.
_INCOME_WINDOW = (
    "statement coverage for FY 2025-26 must run from the earlier of 1 April 2025 and "
    "1 January of your Form 1042-S's tax year, or from the account's first statement if "
    "it opened later with no carried balance, through 30 April 2026, the month after FY "
    "end, for late-posted interest"
)
_INCOME_LATE_START = (
    "the statements start after 1 April 2025 with a carried balance, so add every monthly "
    "statement from the earlier of 1 April 2025 and 1 January of your Form 1042-S's tax "
    "year (or from the account's first statement) up to the earliest one given"
)
_TAX_YEAR_START = (
    "the statements start with a carried balance, so if they start after 1 January of "
    "your Form 1042-S's tax year, add every monthly statement from that 1 January (or from "
    "the account's first statement) up to the earliest one given"
)
_INCOME_EARLY_END = (
    "the statements end before 30 April 2026, so add every monthly statement after the "
    "latest one given through 30 April 2026"
)
_REDEMPTION_HOLDING = (
    "if FY 2025-26 then has a SPAXX redemption and the earliest statement begins with a "
    f"SPAXX holding, {HOLDING_HISTORY}"
)


def test_missing_year_end_evidence_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FidelityCmaIncomeError, match="year-end report"):
        _run(tmp_path, monkeypatch, year_ends={})


def test_checked_1042s_box_refuses_after_clearing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = (
        ("1234567890", "01", "02", "0.00", 2, 0),
        ("2234567890", "06", "02", "0.00", 2, 0),
        ("3234567890", "06", "00", "25.00", 8, 2),
    )
    pro_rata = "15 Check if pro-rata basis reporting"
    text = packet_text(records, account=_ACCOUNT, holder=_HOLDER).replace(
        pro_rata, f"{pro_rata}  X"
    )
    _patch_sources(monkeypatch)
    monkeypatch.setattr(income_mod, "read_1042s_packet", lambda _path: parse_1042s_packet(text))
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")

    assert _main(tmp_path, _CONFIG) == 4
    assert "Form 1042-S Box 15 area is marked or changed" in capsys.readouterr().err
    assert not any(out.iterdir())


@pytest.mark.parametrize("file_count", [0, 2])
def test_tax_form_directory_requires_exactly_one_packet(
    tmp_path: Path,
    file_count: int,
) -> None:
    tax_forms = tmp_path / "tax-forms"
    tax_forms.mkdir()
    for index in range(file_count):
        (tax_forms / f"{index}.pdf").write_bytes(b"%PDF placeholder")
    with pytest.raises(FidelityCmaIncomeError, match="exactly one"):
        income_mod._discover_1042s(tax_forms)


@_PERMISSIONS_ENFORCED
def test_unsearchable_output_folder_raises_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """#1238: the output-folder check raises EACCES instead of calling the folder missing."""
    loads: list[object] = []
    monkeypatch.setattr(
        income_mod, "load_fidelity_cma_statements", lambda *args, **_: loads.append(args)
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    blocked = tmp_path / "blocked"
    out = blocked / "out"
    out.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        run_fidelity_cma_income(
            input_dir=tmp_path / "statements",
            tax_form_dir=tmp_path / "tax-forms",
            config_path=cfg,
            output_dir=out,
            financial_year=_FY,
        )
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(out)!r}"
    blocked.chmod(0o700)
    assert list(out.iterdir()) == []
    assert loads == []


@_PERMISSIONS_ENFORCED
def test_unsearchable_tax_form_folder_raises_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """#1238: the tax-form folder check raises EACCES instead of calling the folder missing."""
    blocked = tmp_path / "blocked"
    tax_forms = blocked / "tax-forms"
    tax_forms.mkdir(parents=True)
    (tax_forms / "2025-1042-s.pdf").write_bytes(b"%PDF placeholder")
    request.addfinalizer(lambda: blocked.chmod(0o700))
    bundle = validate_statement_bundle(_bundle())

    def load_then_lock(*_args: object, spaxx_isin: str, **_kwargs: object) -> object:
        # Lock once the output collision guard has listed the folder.
        blocked.chmod(0)
        return bundle, {2025: income_year_end()}, to_source_record(bundle, spaxx_isin=spaxx_isin)

    reads: list[Path] = []
    monkeypatch.setattr(income_mod, "load_fidelity_cma_statements", load_then_lock)
    monkeypatch.setattr(income_mod, "read_1042s_packet", reads.append)
    cfg = tmp_path / "user.toml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(PermissionError) as raised:
        run_fidelity_cma_income(
            input_dir=tmp_path / "statements",
            tax_form_dir=tax_forms,
            config_path=cfg,
            output_dir=out,
            financial_year=_FY,
        )
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(tax_forms)!r}"
    blocked.chmod(0o700)
    assert list(out.iterdir()) == []
    assert reads == []
    assert [path.name for path in tax_forms.iterdir()] == ["2025-1042-s.pdf"]


def test_interrupted_write_rolls_back_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWriter(StubWriter):
        def write(self, _result: object, _path: Path) -> None:
            schedule_os = tmp_path / "out" / "schedule-os.csv"
            schedule_os.unlink()
            schedule_os.mkdir()
            raise KeyboardInterrupt

    patch_writers(monkeypatch, Subcommand.TR, FailingWriter)
    with pytest.raises(OutputRemovalError, match="may still be there") as exc_info:
        _run(tmp_path, monkeypatch)
    schedule_os = tmp_path / "out" / "schedule-os.csv"
    assert str(schedule_os) in str(exc_info.value)
    assert_replaced_unchained(exc_info.value, KeyboardInterrupt)
    assert schedule_os.is_dir()
    assert not any(path.is_file() for path in (tmp_path / "out").glob("*.csv"))


def test_cli_exit_tracks_blocked_manual_and_complete_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = FidelityCmaIncomeRunReport(
        output_dir=tmp_path,
        financial_year=_FY,
        dividend_count=1,
        interest_count=1,
        interest_total_inr_rounded=Decimal("47"),
        actual_withholding_count=1,
        eligible_withholding_count=0,
        blocked_refund_years=((2025, FidelityCmaRefundStatus.FILED_PENDING),),
        os_foreign_rows=1,
        os_interest_rows=1,
        fsi_rows=1,
        tr_rows=0,
        form67_entries=0,
        cg_stcg_rows=0,
        cg_ltcg_rows=0,
        manual_fill_warnings=("FTC blocked",),
        fx_policy_appropriate_for_ay=True,
        form67_readiness=Form67Readiness.READY,
        run_outputs=RunOutputs((), ()),
    )
    args = [
        "fidelity-cma-income",
        "--itr-form",
        "ITR-2",
        "--input",
        "statements",
        "--tax-forms",
        "tax-forms",
        "--config",
        "user.toml",
        "--output-dir",
        str(tmp_path),
        "--financial-year",
        "2025-26",
    ]
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_income", lambda **_kwargs: report)
    code = main(args)
    captured = capsys.readouterr()
    assert code == 3
    assert "FTC status: BLOCKED" in captured.out
    assert "Schedule OS 1b(ix) total: ₹47" in captured.out
    assert "capital gains" not in captured.out
    assert "warning: FTC blocked" in captured.err

    complete = replace(
        report,
        eligible_withholding_count=1,
        blocked_refund_years=(),
        tr_rows=1,
        form67_entries=1,
        manual_fill_warnings=(),
    )
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_income", lambda **_kwargs: complete)
    assert main(args) == 0
    assert "FTC status: complete" in capsys.readouterr().out

    with_gains = replace(complete, cg_stcg_rows=2)
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_income", lambda **_kwargs: with_gains)
    assert main(args) == 0
    assert (
        "  Schedule FSI includes Fidelity CMA SPAXX capital gains: 2 STCG, 0 LTCG.\n"
        "  FTC status: complete."
    ) in capsys.readouterr().out

    advisory = replace(complete, warnings=("INDIAN TAX CALCULATION REQUIRED",))
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_income", lambda **_kwargs: advisory)
    assert main(args) == 0
    captured = capsys.readouterr()
    assert "FTC status: complete" in captured.out
    assert "warning: INDIAN TAX CALCULATION REQUIRED" in captured.err

    manual = replace(complete, manual_fill_warnings=(BLANK_RELIEF_TIN_WARNING,))
    monkeypatch.setattr("ibkr_fa.cli.run_fidelity_cma_income", lambda **_kwargs: manual)
    assert main(args) == 3
    captured = capsys.readouterr()
    assert "FTC status: ACTION NEEDED" in captured.out
    assert f"warning: {BLANK_RELIEF_TIN_WARNING}\n" in captured.err


@pytest.mark.parametrize("status", _FTC_STATUSES)
def test_blank_us_tin_with_eligible_withholding_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    """test_per_source_overrides_complete_mixed_fsi's complete filing without
    [account.foreign_tins]: the pipeline itself, not a stub report, raises the
    blank-TIN warning, so the CLI exits 3 (#1416). The warning names the
    passport route, and a passport number there clears it (#1510)."""
    config = _OVERRIDE_CONFIG.replace('[account.foreign_tins]\nUS = "123456789"\n\n', "")
    assert "foreign_tins" not in config
    tax_config = _tax_year(2025, status, {"2025-04-15": "0.10"})
    _patch_sources(monkeypatch)

    code = _main(tmp_path, config + tax_config)

    captured = capsys.readouterr()
    assert code == 3
    assert f"warning: {BLANK_RELIEF_TIN_WARNING}\n" in captured.err
    assert "FTC status: ACTION NEEDED" in captured.out

    passport = _OVERRIDE_CONFIG.replace('US = "123456789"', 'US = "P1234567"')
    assert 'US = "P1234567"' in passport

    code = _main(tmp_path, passport + tax_config)

    captured = capsys.readouterr()
    assert code == 0
    assert BLANK_RELIEF_TIN_WARNING not in captured.err
    assert "FTC status: complete." in captured.out
    fsi = _fsi_fields(tmp_path / "out" / "schedule-fsi.csv")
    assert fsi["Taxpayer Identification number"] == "P1234567"


# --- Form 67 readiness (#601) ---------------------------------------------------

_ZERO_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "0"

[[form67.tax_payable_overrides]]
country = "US"
source = "INTEREST_INCOME"
tax_payable_normal_inr = "5"
"""


def test_blank_form67_row_withholds_official_csv_and_writes_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, out = _run(tmp_path, monkeypatch, tax_config=_FINALIZED_TAX_CONFIG)
    assert report.form67_entries == 1
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67-debug.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]


def test_zero_credit_override_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object) -> None:
        raise AssertionError("a normal output write was attempted")

    monkeypatch.setattr(OutputBinding, "writer", refuse)
    with pytest.raises(ConfigError, match=r"US/DIVIDEND: the computed credit rounds to ₹0"):
        _run(
            tmp_path,
            monkeypatch,
            config_text=_CONFIG + _ZERO_OVERRIDE,
            tax_config=_FINALIZED_TAX_CONFIG,
        )
    assert list((tmp_path / "out").iterdir()) == []


# --- Sweep-fund capital gains in Schedule FSI (#917) -----------------------------

_BUY = CmaCoreTransactionKind.BUY
_SELL = CmaCoreTransactionKind.SELL


def _core(
    kind: CmaCoreTransactionKind, day: date, quantity: str, amount: str
) -> CmaCoreTransaction:
    return core_transaction(kind, day, quantity, "1.0000", amount)


def _spaxx_history(
    *transactions: CmaCoreTransaction, start: tuple[int, int] = (2025, 4)
) -> tuple[CmaStatement, ...]:
    """Monthly statements from ``start`` through April 2026 with no carried
    balance and no income, each holding the SPAXX ``transactions`` leave."""
    return monthly_history(
        *transactions,
        start=date(*start, 1),
        price=Decimal("1.0000"),
        carry_position_value=True,
    )


def _run_cg(tmp_path: Path) -> FidelityCmaCgRunReport:
    """Run ``fidelity-cma-cg`` on the statements and config ``_run`` set up."""
    cg_out = tmp_path / "cg-out"
    cg_out.mkdir()
    return run_fidelity_cma_cg(
        input_dir=tmp_path / "statements",
        config_path=tmp_path / "user.toml",
        output_dir=cg_out,
        financial_year=_FY,
    )


@pytest.mark.parametrize(
    "sale_day", [date(2025, 4, 20), date(2026, 3, 31)], ids=["mid-fy", "fy-end"]
)
def test_spaxx_gain_reaches_fsi_as_fidelity_cma_cg_files_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sale_day: date
) -> None:
    """Each row is within half a cent of its units at USD 1.0000; FIFO sells
    the first lot for one cent more than it cost, USD 0.01 or ₹0.85 at ₹85.
    A sale on 31 March, the FY's last day, is still in the FY (#1416)."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)
    statements = _spaxx_history(
        _core(_BUY, date(2025, 4, 10), "10.005", "10.00"),
        _core(_BUY, date(2025, 4, 11), "9.995", "10.00"),
        _core(_SELL, sale_day, "-10.005", "-10.01"),
    )
    report, out = _run(tmp_path, monkeypatch, statements=statements)
    cg_report = _run_cg(tmp_path)

    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (1, 0)
    assert cg_report.total_stcg_inr_rounded == Decimal("1")
    fsi = _fsi_fields(out / "schedule-fsi.csv")
    assert fsi["Head of Income"] == "Capital Gains"
    assert fsi["Income from outside India (included in PART B-TI)"] == str(
        cg_report.total_stcg_inr_rounded
    )


def _first_holding(statements: tuple[CmaStatement, ...], quantity: str) -> tuple[CmaStatement, ...]:
    first, *rest = statements
    return (replace(first, position=replace(first.position, quantity=Decimal(quantity))), *rest)


@pytest.mark.parametrize(
    ("statements", "message"),
    [
        pytest.param(
            (
                replace(
                    _bundle()[0],
                    core_transactions=(_core(_SELL, date(2025, 4, 20), "-1.000", "-1.00"),),
                ),
                *_bundle()[1:],
            ),
            "SPAXX capital gains require statements starting before any carried holding",
            id="carried-holding",
        ),
        pytest.param(
            _spaxx_history(_core(_SELL, date(2025, 4, 20), "-1.000", "-1.00")),
            UNMATCHED_SALE_REFUSAL,
            id="unmatched-sale",
        ),
        pytest.param(
            _first_holding(
                _spaxx_history(
                    _core(_BUY, date(2025, 4, 10), "2.000", "2.00"),
                    _core(_SELL, date(2025, 5, 20), "-1.000", "-1.00"),
                ),
                "3.000",
            ),
            "SPAXX transaction quantities do not match the monthly holding",
            id="holding-mismatch",
        ),
        pytest.param(
            _spaxx_history(
                _core(_BUY, date(2023, 4, 3), "1.000", "1.00"),
                _core(_SELL, date(2025, 4, 20), "-1.000", "-1.00"),
                start=(2023, 4),
            ),
            f"{HELD_OVER_24_MONTHS_REFUSAL} are unsupported pending "
            "authoritative section 50AA treatment",
            id="held-over-24-months",
        ),
    ],
)
def test_unprovable_in_fy_redemption_refuses_income_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statements: tuple[CmaStatement, ...],
    message: str,
) -> None:
    _patch_sources(monkeypatch, statements=statements)
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-fsi.csv").write_text("stale", encoding="utf-8")

    assert _main(tmp_path, _CONFIG) == 4
    assert capsys.readouterr().err == f"error: {message}\n"
    assert list(out.iterdir()) == []


def test_redemption_after_fy_end_needs_no_acquisition_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The following-month statement's sale is in the next FY, so the carried
    holding needs no history."""
    *statements, april_2026 = _bundle()
    sale = _core(_SELL, date(2026, 4, 15), "-1.000", "-1.00")
    report, out = _run(
        tmp_path,
        monkeypatch,
        statements=(*statements, replace(april_2026, core_transactions=(sale,))),
    )
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (0, 0)
    assert _fsi_fields(out / "schedule-fsi.csv")["Head of Income"] == "Other Sources"


# --- one opening rule for income and CG (#921) ------------------------------------


def test_both_commands_accept_an_account_opened_during_the_fy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first statement from 1 May 2025 with a dash beginning value is the
    account's opening, so no earlier FY sale or income can be missing."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)
    statements = _spaxx_history(
        _core(_BUY, date(2025, 5, 15), "10.000", "10.00"),
        _core(_SELL, date(2025, 6, 16), "-4.000", "-4.00"),
        start=(2025, 5),
    )
    report, _ = _run(tmp_path, monkeypatch, statements=statements)
    assert report.cg_stcg_rows == _run_cg(tmp_path).stcg_rows == 1


def test_both_commands_refuse_a_late_start_with_a_carried_balance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Statements from May 2025 that carry April's balance could miss April.
    They reach each command's end, so each refusal names the start (#1510).
    April may begin with SPAXX units and, for income, hold the FY's only SPAXX
    redemption, so each also names the history before a carried holding
    (#1515)."""
    with pytest.raises(FidelityCmaIncomeError) as income_refusal:
        _run(tmp_path, monkeypatch, statements=_bundle()[1:])
    assert str(income_refusal.value) == (
        f"{_INCOME_WINDOW}; {_INCOME_LATE_START}; {_REDEMPTION_HOLDING}"
    )
    with pytest.raises(FidelityCmaCgError) as cg_refusal:
        _run_cg(tmp_path)
    assert str(cg_refusal.value) == f"{CG_WINDOW}; {LATE_START}; {LATER_HOLDING}"


def test_both_commands_name_both_missing_edges_and_one_rerun_clears_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1510's example: statements from May 2025 to February 2026, the May one
    carrying a balance. Each command's first refusal names both edges, and
    adding what it names clears every coverage stop on one re-run: the
    account's first statement is April 2025, which begins with no SPAXX
    holding and carries no balance into 1 January's check (#1515)."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)
    # April 2025 (the account's first statement) through April 2026.
    history = _spaxx_history(
        _core(_BUY, date(2025, 4, 15), "10.000", "10.00"),
        _core(_SELL, date(2025, 6, 16), "-4.000", "-4.00"),
    )
    may_to_february = history[1:11]
    assert (may_to_february[0].period_start, may_to_february[-1].period_end) == (
        date(2025, 5, 1),
        date(2026, 2, 28),
    )
    assert may_to_february[0].beginning_account_value_usd is not None

    with pytest.raises(FidelityCmaIncomeError) as income_refusal:
        _run(tmp_path, monkeypatch, statements=may_to_february, check_1042s_coverage=True)
    assert str(income_refusal.value) == (
        f"{_INCOME_WINDOW}; {_INCOME_LATE_START}; {_INCOME_EARLY_END}; {LATER_HOLDING}"
    )
    with pytest.raises(FidelityCmaCgError) as cg_refusal:
        _run_cg(tmp_path)
    assert str(cg_refusal.value) == f"{CG_WINDOW}; {LATE_START}; {CG_EARLY_END}; {LATER_HOLDING}"

    # Add April 2025, then every month after February 2026 through each end.
    rerun = tmp_path / "rerun"
    rerun.mkdir()
    report, _ = _run(rerun, monkeypatch, statements=history, check_1042s_coverage=True)
    assert history[-1].period_end == date(2026, 4, 30)
    assert report.cg_stcg_rows == 1
    _patch_sources(monkeypatch, statements=history[:12])
    assert history[11].period_end == date(2026, 3, 31)
    assert _run_cg(rerun).stcg_rows == 1


# --- every later coverage stop in the first refusal (#1515) ------------------------


def _opened_october_2024(*sales: CmaCoreTransaction) -> tuple[CmaStatement, ...]:
    """October 2024 (the account's first statement, which buys SPAXX units)
    through April 2026, with ``sales``."""
    history = _spaxx_history(
        _core(_BUY, date(2024, 10, 15), "10.000", "10.00"), *sales, start=(2024, 10)
    )
    assert [history[index].period_start for index in (0, 3, 6, 17)] == [
        date(2024, 10, 1),
        date(2025, 1, 1),
        date(2025, 4, 1),
        date(2026, 3, 1),
    ]
    assert history[6].position.beginning_market_value_usd is not None
    return history


def test_income_names_the_form_1042s_january_and_one_rerun_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1515's first example: monthly statements from April 2025 to March 2026,
    April carrying a balance, the 2025 Form 1042-S, no SPAXX redemption. The
    refusal names the end (#1510) and the form's 1 January, and clears the
    stale filing outputs. Every FY month is given and none has a SPAXX
    redemption, so no gain needs history. Adding January to March 2025 and
    April 2026 clears every coverage stop on one re-run."""
    with pytest.raises(FidelityCmaIncomeError) as refusal:
        _run(
            tmp_path,
            monkeypatch,
            statements=_bundle()[:-1],
            seed_stale_outputs=True,
            check_1042s_coverage=True,
        )
    assert str(refusal.value) == f"{_INCOME_WINDOW}; {_TAX_YEAR_START}; {_INCOME_EARLY_END}"
    assert not any((tmp_path / "out").glob("*.csv"))

    rerun = tmp_path / "rerun"
    rerun.mkdir()
    statements = (*(_statement(2025, month) for month in (1, 2, 3)), *_bundle())
    report, _ = _run(rerun, monkeypatch, statements=statements, check_1042s_coverage=True)
    assert (report.dividend_count, report.interest_count, report.cg_stcg_rows) == (1, 1, 0)


@pytest.mark.parametrize(
    ("sale_day", "income_holding"),
    [
        pytest.param(date(2025, 6, 16), LATER_HOLDING, id="redemption-given"),
        pytest.param(date(2026, 3, 16), _REDEMPTION_HOLDING, id="redemption-in-missing-march"),
    ],
)
def test_both_commands_name_the_history_before_a_carried_holding_and_one_rerun_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sale_day: date,
    income_holding: str,
) -> None:
    """#1515's second example: statements from April 2025 to February 2026,
    April carrying SPAXX units bought when the account opened in October 2024.
    CG names the history before that holding. Income, with an in-FY SPAXX
    redemption given or in the missing March, names 1 January and then the
    history before whatever the earliest statement carries. Adding exactly
    what each names clears every coverage stop on one re-run."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)
    history = _opened_october_2024(_core(_SELL, sale_day, "-4.000", "-4.00"))
    april_to_february = history[6:17]

    with pytest.raises(FidelityCmaIncomeError) as income_refusal:
        _run(tmp_path, monkeypatch, statements=april_to_february, check_1042s_coverage=True)
    assert str(income_refusal.value) == (
        f"{_INCOME_WINDOW}; {_TAX_YEAR_START}; {_INCOME_EARLY_END}; {income_holding}"
    )
    with pytest.raises(FidelityCmaCgError) as cg_refusal:
        _run_cg(tmp_path)
    assert str(cg_refusal.value) == f"{CG_WINDOW}; {CG_EARLY_END}; {CARRIED_HOLDING}"

    # Income: January 2025, whose SPAXX units date back to the account's first
    # statement, and March and April 2026. CG: back to the account's first and
    # March 2026.
    rerun = tmp_path / "rerun"
    rerun.mkdir()
    report, _ = _run(rerun, monkeypatch, statements=history, check_1042s_coverage=True)
    assert report.cg_stcg_rows == 1
    _patch_sources(monkeypatch, statements=history[:18])
    assert _run_cg(rerun).stcg_rows == 1


@pytest.mark.parametrize(
    ("sales", "added", "refusal_text"),
    [
        pytest.param((), 3, JANUARY_2025_REFUSAL, id="no-redemption"),
        pytest.param(
            (_core(_SELL, date(2025, 6, 16), "-4.000", "-4.00"),),
            0,
            f"{JANUARY_2025_REFUSAL}; {LATER_HOLDING}",
            id="fy-redemption",
        ),
    ],
)
def test_form_1042s_january_refusal_names_every_later_stop_when_it_comes_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sales: tuple[CmaCoreTransaction, ...],
    added: int,
    refusal_text: str,
) -> None:
    """Statements from April 2025 to April 2026 meet the FY window, so the
    Form 1042-S check refuses first: April carries a balance into a start after
    1 January 2025. With an in-FY SPAXX redemption the run computes gains, so
    the refusal also names the history before a carried holding; adding what
    it names clears every coverage stop on one re-run (#1515)."""
    monkeypatch.setattr("ibkr_fa.fx.context.Rule115CapitalGainsPolicy", AuditedFxStub)
    history = _opened_october_2024(*sales)

    with pytest.raises(Cma1042SReconciliationError) as refusal:
        _run(tmp_path, monkeypatch, statements=history[6:], check_1042s_coverage=True)
    assert str(refusal.value) == refusal_text

    # From 1 January 2025, and back to the account's first statement when the
    # January one begins with SPAXX units the FY's redemption draws on.
    rerun = tmp_path / "rerun"
    rerun.mkdir()
    report, _ = _run(rerun, monkeypatch, statements=history[added:], check_1042s_coverage=True)
    assert report.cg_stcg_rows == len(sales)


def test_an_account_opened_in_april_missing_only_the_end_names_only_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the account's first statement given there is no carried balance,
    so no earlier month can be missing: the refusal names only the end edge
    (#1510), even with an in-FY SPAXX redemption (#1515)."""
    history = _spaxx_history(
        _core(_BUY, date(2025, 4, 15), "10.000", "10.00"),
        _core(_SELL, date(2025, 6, 16), "-4.000", "-4.00"),
    )
    with pytest.raises(FidelityCmaIncomeError) as refusal:
        _run(tmp_path, monkeypatch, statements=history[:-1], check_1042s_coverage=True)
    assert str(refusal.value) == f"{_INCOME_WINDOW}; {_INCOME_EARLY_END}"


# --- exact SPAXX shares (#1244) ----------------------------------------------------

_MARKER = "736253"


def test_thirds_of_a_spaxx_cost_file_the_exact_fsi_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1244's repro at ₹50: FSI files the exact ₹0.50 gain as ₹1, the
    balance ``fidelity-cma-cg`` files. Rounded thirds of the cost made it ₹0."""
    monkeypatch.setattr(
        "ibkr_fa.fx.context.Rule115CapitalGainsPolicy", partial(AuditedFxStub, Decimal(50))
    )
    report, out = _run(tmp_path, monkeypatch, statements=_spaxx_history(*thirds()))
    cg_report = _run_cg(tmp_path)

    assert (report.cg_stcg_rows, cg_report.total_stcg_inr_rounded) == (3, Decimal(1))
    fsi = _fsi_fields(out / "schedule-fsi.csv")
    assert fsi["Head of Income"] == "Capital Gains"
    assert fsi["Income from outside India (included in PART B-TI)"] == "1"


def test_cli_share_past_the_digit_cap_exits_4_and_removes_stale_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sale is the third statement's only Core Fund row."""
    _patch_sources(
        monkeypatch,
        statements=_spaxx_history(
            _core(_BUY, date(2025, 4, 10), "1.000", "1.00"),
            _core(_SELL, date(2025, 6, 3), "-1.000", f"-{PAST_CAP}"),
        ),
    )
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")

    assert _main(tmp_path, _CONFIG) == 4

    err = capsys.readouterr().err
    assert err == f"error: {SHARE_REFUSAL.format(3, 1)}\n"
    assert _MARKER not in err
    assert _ACCOUNT not in err
    assert list(out.iterdir()) == []


# ---- Schedule FSI files the Schedule OS rupees (#1319) ------------------------


def _first_cells(path: Path) -> dict[str, str]:
    """A filing CSV's first two cells per row, as a label -> value map."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row[0]: row[1] for row in csv.reader(handle) if len(row) > 1 and row[0]}


def test_fsi_other_sources_income_is_the_schedule_os_rupees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USD 0.13 of dividend (USD 0.01 withheld) and USD 0.13 of interest
    file 1a(i) ₹11 and 1b(ix) ₹12. The US row's relief is claimed against
    Other Sources, so ITR-3 rule 894 refuses an upload whose FSI income is
    more than Schedule OS's: FSI files ₹23, not the ₹24 its exact income
    rounds to, and its tax paid, tax payable and relief article are unchanged."""
    statements = [replace(s, income_credits=(), withholdings=()) for s in _bundle()]
    september = next(
        index for index, s in enumerate(statements) if s.period_start == date(2025, 9, 1)
    )
    statements[september] = replace(
        statements[september],
        income_credits=(
            CmaIncomeCredit(
                date(2025, 9, 12), CmaIncomeKind.SPAXX_DIVIDEND, Decimal("0.13"), "99971W106"
            ),
            CmaIncomeCredit(
                date(2025, 9, 30), CmaIncomeKind.CREDIT_INTEREST, Decimal("0.13"), None
            ),
        ),
        withholdings=(CmaWithholding(date(2025, 9, 12), Decimal("-0.01")),),
    )
    year_end = replace(
        income_year_end(),
        dividends_usd=Decimal("0.13"),
        interest_usd=Decimal("0.13"),
        withholding_usd=Decimal("-0.01"),
    )

    report, out = _run(
        tmp_path,
        monkeypatch,
        config_text=_OVERRIDE_CONFIG,
        tax_config=_tax_year(2025, "finalized", {"2025-09-12": "0.01"}),
        statements=tuple(statements),
        year_ends={2025: year_end},
    )

    assert report.ftc_complete
    schedule_os = _first_cells(out / "schedule-os.csv")
    assert schedule_os["1ai. Dividend income [other than (ii) and (iii)]"] == "11"
    assert schedule_os[PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST] == "12"
    fsi = _fsi_fields(out / "schedule-fsi.csv")
    assert fsi["Income from outside India (included in PART B-TI)"] == "23"
    assert fsi["Tax paid outside India"] == "1"
    assert fsi["Tax payable on such income under normal provisions in India"] == "35"
    assert fsi["Relevant article of DTAA if relief claimed u/s 90 or 90A"] == "25"
