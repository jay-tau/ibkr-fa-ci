"""Pin every file the three CMA commands write, through real PDF discovery.

Only the ``pdftotext`` shell-out (``subprocess.run``) is stubbed, so statement
discovery, bundle validation, compute and the writers run as in production.
Every statement is synthetic. The bytes are pinned by sha256 because the
review CSVs end lines with CRLF, which ``.gitattributes`` and pre-commit would
rewrite in committed golden files.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from ibkr_fa.cli import main
from ibkr_fa.output.income_bundle import INCOME_FILING_FILENAMES
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_fidelity_cma_cg import run_fidelity_cma_cg
from ibkr_fa.pipeline_fidelity_cma_fa import run_fidelity_cma_fa
from ibkr_fa.pipeline_fidelity_cma_income import run_fidelity_cma_income
from tests._fidelity_cma import (
    CARD_ORDER_REFUSAL,
    TOTAL_HOLDINGS_REFUSAL,
    TRANSACTION_DATE_REFUSAL,
)
from tests._fidelity_cma_bundle import BUNDLE_CONFIG as _CONFIG
from tests._fidelity_cma_bundle import CARD_AMOUNT as _CARD_AMOUNT
from tests._fidelity_cma_bundle import CARD_MARKER as _CARD_MARKER
from tests._fidelity_cma_bundle import FA_SHA256 as _FA_SHA256
from tests._fidelity_cma_bundle import FINALIZED_2025 as _FINALIZED_2025
from tests._fidelity_cma_bundle import CardsByMonth as _Cards
from tests._fidelity_cma_bundle import bundle_inputs as _inputs
from tests._fidelity_cma_bundle import cma_inputs  # noqa: F401 - fixture
from tests._fidelity_cma_bundle import folder_digests as _digests
from tests._fidelity_cma_bundle import spaxx_nav as _spaxx_nav
from tests._text_edits import replace_once

_INCOME_SHA256 = {
    # #1453: the blocked FTC withholds form-67.csv; every other digest is unchanged.
    "blocked": {
        "form-67-debug.csv": "7e995b4feeedd1311110fea7953d9a6fdf82721874f1673d4361bac3a779297f",
        "schedule-fsi-debug.csv": (
            "ef8b9c36ac7ed5d6925c0585a5cb592c2938fd82e8eaef8617a75fe8ee2e63f7"
        ),
        "schedule-fsi.csv": "8f8720b862f8fa0f6ca837807077cc1aa1bf2195ceaa4981d59b52336ed55c4c",
        "schedule-os-debug.csv": "090ececa751fae81bf61c896d8895e584932604c4accb553199ec9ec175d3c0b",
        "schedule-os.csv": "c2245cdafdc48fa05f35c46f8273d112a5e35aabab6fb624d6bada42ffb84aae",
        "schedule-tr-debug.csv": "6b009fa04743964219fe6855c2edf8e024ff0706740dc61ca2cb59bfd534e976",
        "schedule-tr.csv": "426056ef4fb998a4c925777ebc802fe0bf7761c3dd6f47e1147c99e3af6e56b2",
    },
    # #601: no Indian-tax override, so the blank Form 67 row withholds
    # form-67.csv; every other digest is unchanged. #1427: schedule-fsi.csv's
    # "Manual fill guidance" cell ends with the new override remedy; nothing
    # else changes. #1446: that cell drops its lending-fee by-hand clause;
    # nothing else changes.
    "finalized": {
        "form-67-debug.csv": "50cd7b103eefbfe3ff94b5f45a5838ff4288ffa0fd764b17a56fc051499868ea",
        "schedule-fsi-debug.csv": (
            "47565f8ef7e33d4938a8637bd33860c9f8dd6f92b269610d0623bfdabfc76d8b"
        ),
        "schedule-fsi.csv": "0ab0f89377da0fcc165da0d785273e827dab1663ed746849fb81a2aebb3055ea",
        "schedule-os-debug.csv": "090ececa751fae81bf61c896d8895e584932604c4accb553199ec9ec175d3c0b",
        "schedule-os.csv": "c2245cdafdc48fa05f35c46f8273d112a5e35aabab6fb624d6bada42ffb84aae",
        "schedule-tr-debug.csv": "0d55457248ea7128e1837ae0ae854496855635eccb5d21d03c7fb5f24f3da8a5",
        "schedule-tr.csv": "a5edc717b4f40fdf730f92fb5698ce3663238d7fb5b6ff7e7ebdd0d5e366cb68",
    },
}


@pytest.mark.parametrize(
    ("fixture", "tax_config"),
    [("blocked", ""), ("finalized", _FINALIZED_2025)],
    ids=["blocked", "finalized"],
)
def test_income_outputs_match_pinned_bytes(
    tmp_path: Path,
    cma_inputs: tuple[Path, Path, Path],  # noqa: F811 - fixture
    fixture: str,
    tax_config: str,
) -> None:
    statements, tax_forms, config = cma_inputs
    config.write_text(_CONFIG + tax_config, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    report = run_fidelity_cma_income(
        input_dir=statements,
        tax_form_dir=tax_forms,
        config_path=config,
        output_dir=out,
        financial_year=FinancialYear(2025),
        debug=True,
    )
    assert (report.dividend_count, report.interest_count) == (12, 12)
    # #917: the FY's two redemptions produce four STCG lines; at USD 1.0000
    # they net to 0, so FSI gets no Capital Gains row and the digests don't change.
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (4, 0)
    assert report.ftc_complete is (fixture == "finalized")
    assert _digests(out) == _INCOME_SHA256[fixture]


# #1361: each CSV gains its Table F block, five zeros because the FY's four
# STCG lines net to 0; without it each hashes to its earlier digest.
_CG_SHA256 = {
    "schedule-cg-ltcg.csv": "626473d19bb1115c0376c0e5544db22b92d7fed8c3d919798e071766016c1824",
    "schedule-cg.csv": "a30c11a9c7ed994f682467f26b063a9b97e52a914f3a9b29851a5a4e537a21cf",
}


def test_cg_outputs_match_pinned_bytes(tmp_path: Path, cma_inputs: tuple[Path, Path, Path]) -> None:  # noqa: F811 - fixture
    statements, _tax_forms, config = cma_inputs
    out = tmp_path / "out"
    out.mkdir()
    report = run_fidelity_cma_cg(
        input_dir=statements,
        config_path=config,
        output_dir=out,
        financial_year=FinancialYear(2025),
    )
    assert (report.statement_count, report.stcg_rows, report.out_of_fy_disposals) == (16, 4, 1)
    assert _digests(out) == _CG_SHA256


# The NAV cache the FA run writes under its own price-cache directory.
_FA_PRICE_CACHE_SHA256 = {
    "nasdaq-mutual-funds/SPAXX.csv": (
        "297a2a146c5c5fe0a9b56341fd628a46db77facd3cbdba1314bf8074b3947665"
    ),
}


def test_fa_outputs_match_pinned_bytes(tmp_path: Path, cma_inputs: tuple[Path, Path, Path]) -> None:  # noqa: F811 - fixture
    statements, _tax_forms, config = cma_inputs
    out = tmp_path / "out"
    out.mkdir()
    price_cache = tmp_path / "price-cache"
    report = run_fidelity_cma_fa(
        input_dir=statements,
        config_path=config,
        output_path=out / "schedule-fa.csv",
        calendar_year=2025,
        format_id="review-csv",
        price_cache_dir=price_cache,
        price_fetcher=_spaxx_nav,
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert report.a3_rows > 0
    assert _digests(out) == _FA_SHA256
    assert _digests(price_cache) == _FA_PRICE_CACHE_SHA256


def test_cli_refuses_a_total_holdings_row_with_an_extra_amount(
    tmp_path: Path,
    cma_inputs: tuple[Path, Path, Path],  # noqa: F811 - fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1095: one statement's ``Total Holdings`` row gains an amount in front.

    The old reader compared only the row's last two amounts, so this parsed."""
    statements, tax_forms, config = cma_inputs
    # The fixture's pdftotext stub.
    render = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)

    def edited(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = render(args, **kwargs)
        if args[4] == str(statements / "2025-06.pdf"):
            result.stdout = replace_once(
                result.stdout, "\nTotal Holdings $", "\nTotal Holdings $777.00 $"
            )
        return result

    monkeypatch.setattr(subprocess, "run", edited)
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")
    code = main(
        [
            "fidelity-cma-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statements),
            "--tax-forms",
            str(tax_forms),
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    assert capsys.readouterr().err == (
        "error: ParseError in section 'Fidelity CMA monthly statement' at row 0, "
        f"column 'Total Holdings': {TOTAL_HOLDINGS_REFUSAL}\n"
    )
    assert not any(out.iterdir())


# #1251: a card purchase dated in the month before its statement, on the last
# day of that month, posting the next session. The same purchases with the
# purchase date moved to the posting date.
_CROSS_MONTH_CARDS: _Cards = {(2025, 7): ("06/30", 1), (2026, 1): ("12/31", 2)}
_SAME_DAY_CARDS: _Cards = {(2025, 7): ("07/01", 1), (2026, 1): ("01/02", 2)}


def _run_all_three(root: Path, monkeypatch: pytest.MonkeyPatch, cards: _Cards) -> dict[str, object]:
    """Run the three CMA commands on the bundle with ``cards``; every file each
    writes, by sha256, and the counts each reports."""
    root.mkdir()
    statements, tax_forms, config = _inputs(root, monkeypatch, cards)
    outs = {name: root / name for name in ("income", "cg", "fa", "price-cache")}
    for name in ("income", "cg", "fa"):
        outs[name].mkdir()
    income = run_fidelity_cma_income(
        input_dir=statements,
        tax_form_dir=tax_forms,
        config_path=config,
        output_dir=outs["income"],
        financial_year=FinancialYear(2025),
        debug=True,
    )
    cg = run_fidelity_cma_cg(
        input_dir=statements,
        config_path=config,
        output_dir=outs["cg"],
        financial_year=FinancialYear(2025),
    )
    fa = run_fidelity_cma_fa(
        input_dir=statements,
        config_path=config,
        output_path=outs["fa"] / "schedule-fa.csv",
        calendar_year=2025,
        format_id="review-csv",
        price_cache_dir=outs["price-cache"],
        price_fetcher=_spaxx_nav,
        price_now=datetime(2026, 6, 1, tzinfo=UTC),
    )
    return {
        "counts": (income.cg_stcg_rows, cg.statement_count, cg.stcg_rows, fa.a3_rows),
        **{name: _digests(path) for name, path in outs.items()},
    }


def test_cross_month_card_purchase_date_reaches_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both cross-month rows refused their statements before #1251. Every file
    the three commands write is byte-identical with the purchase dates moved
    into the posting month, so no output reads a purchase date."""
    cross_month = _run_all_three(tmp_path / "cross-month", monkeypatch, _CROSS_MONTH_CARDS)
    same_day = _run_all_three(tmp_path / "same-day", monkeypatch, _SAME_DAY_CARDS)
    assert cross_month == same_day
    # The two card-funding sales add two STCG lines to the bundle's four, and
    # the 2025 one an A3 row to its 37.
    assert cross_month["counts"] == (6, 16, 6, 38)
    assert all(cross_month[name] for name in ("income", "cg", "fa", "price-cache"))


def test_cli_refuses_a_card_purchase_after_its_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1251: one statement's card purchase is dated after its posting date.
    This passed before; now the run exits 4 and removes the stale outputs."""
    statements, tax_forms, config = _inputs(tmp_path, monkeypatch, {(2026, 1): ("01/25", 22)})
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")
    code = main(
        [
            "fidelity-cma-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statements),
            "--tax-forms",
            str(tax_forms),
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Fidelity CMA monthly statement' at row 0: "
        f"{CARD_ORDER_REFUSAL}\n"
    )
    for marker in (_CARD_MARKER, str(-_CARD_AMOUNT)):
        assert marker not in captured.out + captured.err
    assert not any(out.iterdir())


# #1274: the bundle's opening January 2025 statement credits a $13.57 interest
# row before its period: on January 10 in a January 15-31 partial first month,
# or on December 31 in the dash-beginning full month.
_EARLY_INTEREST = "13.57"
_OPENING_EDITS = {
    "partial-first-month": (
        ("January 1, 2025 - January 31, 2025", "January 15, 2025 - January 31, 2025"),
        ("01/05 PAYROLL", "01/15 PAYROLL"),
        ("01/05 CASH", "01/15 CASH"),
        ("01/10 CASH", "01/15 CASH"),
        ("01/10 CREDIT INTEREST - - $0.10", f"01/10 CREDIT INTEREST - - ${_EARLY_INTEREST}"),
    ),
    "dash-beginning-month": (
        ("01/10 CREDIT INTEREST - - $0.10", f"12/31 CREDIT INTEREST - - ${_EARLY_INTEREST}"),
    ),
}


@pytest.mark.parametrize("opening", list(_OPENING_EDITS))
def test_cli_refuses_interest_before_the_opening_statement(
    tmp_path: Path,
    cma_inputs: tuple[Path, Path, Path],  # noqa: F811 - fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opening: str,
) -> None:
    """The run exits 4 and removes the stale outputs."""
    statements, tax_forms, config = cma_inputs
    # The fixture's pdftotext stub.
    render = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)
    edits = (
        *_OPENING_EDITS[opening],
        (
            "Total Dividends, Interest & Other Income $4.10",
            f"Total Dividends, Interest & Other Income ${Decimal(_EARLY_INTEREST) + 4:.2f}",
        ),
    )

    def edited(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = render(args, **kwargs)
        if args[4] == str(statements / "2025-01.pdf"):
            for old, new in edits:
                result.stdout = replace_once(result.stdout, old, new)
        return result

    monkeypatch.setattr(subprocess, "run", edited)
    out = tmp_path / "out"
    out.mkdir()
    for filename in INCOME_FILING_FILENAMES:
        (out / filename).write_text("stale", encoding="utf-8")
    code = main(
        [
            "fidelity-cma-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(statements),
            "--tax-forms",
            str(tax_forms),
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Fidelity CMA monthly statement' at row 0: "
        f"{TRANSACTION_DATE_REFUSAL}\n"
    )
    assert _EARLY_INTEREST not in captured.out + captured.err
    assert not any(out.iterdir())
