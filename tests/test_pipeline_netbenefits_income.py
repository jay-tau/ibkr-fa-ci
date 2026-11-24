"""Tests for the ``netbenefits-income`` pipeline (Schedule OS/FSI/TR/Form 67 for a
Fidelity NetBenefits dividend) and its CLI subcommand.

``pdftotext_layout`` + ``parse_statement`` are monkeypatched so the pipeline runs
against synthetic :class:`StatementData` (no binary PDFs / real statement text);
each fake PDF's text encodes its bundle filename so the fake parser can return
the matching statement.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import os
import tempfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.pipeline_netbenefits_income as income_mod
from ibkr_fa.cli import main
from ibkr_fa.compute.espp_capital_gains import require_capital_gains_coverage
from ibkr_fa.compute.form67 import Form67Readiness
from ibkr_fa.exceptions import ConfigError, InvariantError, ParseError
from ibkr_fa.output.base import OutputBinding, Subcommand
from ibkr_fa.output.income_bundle import INCOME_FILENAMES, INCOME_FILING_FILENAMES
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundle, NetBenefitsBundleError
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.parser.netbenefits.statement_pdf import (
    ReportType,
    StatementData,
    StatementDividend,
    StatementWithholding,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_netbenefits_income import (
    NetBenefitsIncomeError,
    run_netbenefits_income,
)
from tests._crash_reports import assert_no_markers, assert_prints_no_marker
from tests._form67_overrides import FORM67_READY_OVERRIDE as _READY_OVERRIDE
from tests._ftc_warnings import (
    BLANK_RELIEF_TIN_WARNING,
    MANUAL_METHOD,
    same_day_gap,
    wont_clear,
)
from tests._netbenefits import CG_SHORT_REFUSAL as _CG_SHORT_REFUSAL
from tests._netbenefits import (
    CLOSED_EMPTY_CSV,
    CLOSED_RENAMED_PROCEEDS_CSV,
    CLOSED_RENAMED_PROCEEDS_REFUSAL,
    CONF_DEC,
    DATED_LINE_UNREAD,
    patch_pdftotext,
)
from tests._netbenefits_income import ACCOUNT_MARKER as _ACCOUNT_MARKER
from tests._netbenefits_income import AGENT_LINE as _AGENT_LINE
from tests._netbenefits_income import ALL_COVERAGE_GAP_REFUSAL as _ALL_COVERAGE_GAP_REFUSAL
from tests._netbenefits_income import ALL_SHORT_COVERAGE_REFUSAL as _ALL_SHORT_COVERAGE_REFUSAL
from tests._netbenefits_income import BLANK_DEDUCTOR as _BLANK_DEDUCTOR
from tests._netbenefits_income import CG_COVER_FROM_CORRECTED_DATE as _CG_COVER_FROM_CORRECTED_DATE
from tests._netbenefits_income import CG_COVERING as _CG_COVERING
from tests._netbenefits_income import CG_OPENING_REFUSAL as _CG_OPENING_REFUSAL
from tests._netbenefits_income import CONFIRMATION_STUB as _CONFIRMATION_STUB
from tests._netbenefits_income import CORE_FUND_CONFIG_REFUSAL as _CORE_FUND_CONFIG_REFUSAL
from tests._netbenefits_income import COVER_FROM_CORRECTED_DATE as _COVER_FROM_CORRECTED_DATE
from tests._netbenefits_income import COVERING as _COVERING
from tests._netbenefits_income import DIVIDENDS_TABLE as _DIVIDENDS_TABLE
from tests._netbenefits_income import FUND_DIV as _FUND_DIV
from tests._netbenefits_income import FUND_NAME as _FUND_NAME
from tests._netbenefits_income import FUND_WHT as _FUND_WHT
from tests._netbenefits_income import GAP_IN_JANUARY as _GAP_IN_JANUARY
from tests._netbenefits_income import IN_FY_REFUSAL as _IN_FY_REFUSAL
from tests._netbenefits_income import INCOME_CONFIG as CONFIG
from tests._netbenefits_income import MARCH_DIV as _DIV
from tests._netbenefits_income import MARCH_WHT as _WHT
from tests._netbenefits_income import MISMATCH_1042S_REFUSAL as _1042S_MISMATCH_REFUSAL
from tests._netbenefits_income import MISTAKEN_OPENING as _MISTAKEN_OPENING
from tests._netbenefits_income import NOV_DEC as _NOV_DEC
from tests._netbenefits_income import PARTICIPANT_NO as _PNO
from tests._netbenefits_income import QUIET as _QUIET
from tests._netbenefits_income import SHORT_OF_THE_FY_END as _SHORT_OF_THE_FY_END
from tests._netbenefits_income import TAXES_TABLE as _TAXES_TABLE
from tests._netbenefits_income import TIN_MARKER as _TIN_MARKER
from tests._netbenefits_income import YE_DIV as _YE_DIV
from tests._netbenefits_income import YE_FILE as _YE_FILE
from tests._netbenefits_income import YE_MARKERS as _YE_MARKERS
from tests._netbenefits_income import YE_NAME as _YE_NAME
from tests._netbenefits_income import YE_WHT as _YE_WHT
from tests._netbenefits_income import event_at as _event_at
from tests._netbenefits_income import income_statement as _statement
from tests._netbenefits_income import january_statements as _january_statements
from tests._netbenefits_income import marked_ready_run as _marked_ready_run
from tests._netbenefits_income import missing_event_refusal as _missing_event_refusal
from tests._netbenefits_income import opening_date_refusal as _opening_date_refusal
from tests._netbenefits_income import patch_statements as _patch
from tests._netbenefits_income import without_withholding_agent as _without_withholding_agent
from tests._netbenefits_income import year_end_refusal as _year_end_refusal
from tests._netbenefits_income import year_end_report as _year_end
from tests._writer_stubs import StubWriter, patch_writers

FY = FinancialYear(start_year=2025)

_INCOME_WINDOW = (
    "the income window from the later of the FY start and "
    "netbenefits.account.account_opening_date through the FY end"
)
_COVERAGE_GAP_REFUSAL = (
    f"the statements (year-end reports excluded) leave a gap in {_INCOME_WINDOW} "
    "(coverage is needed to prove no dividend was missed there)"
)
_SHORT_COVERAGE_REFUSAL = (
    "the statements (year-end reports excluded) end before the FY end, so they don't "
    f"cover {_INCOME_WINDOW}; supply the statement(s) covering the gap"
)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_name: dict[str, StatementData] | None = None,
    confirmations: tuple[str, ...] = (),
    config: str = CONFIG,
    reconcile_1042s_usd: Decimal | None = None,
    include_capital_gains: bool = False,
    debug: bool = False,
    csvs: dict[str, str] | None = None,
    confirmation_text: str = _CONFIRMATION_STUB,
):
    statements = _COVERING if by_name is None else by_name
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in (*statements, *confirmations):
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    for name, text in (csvs or {}).items():
        (bundle / name).write_text(text, encoding="utf-8")
    _patch(
        monkeypatch, statements, confirmations=confirmations, confirmation_text=confirmation_text
    )
    cfg = tmp_path / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    report = run_netbenefits_income(
        input_dir=bundle,
        config_path=cfg,
        output_dir=out,
        financial_year=FY,
        reconcile_1042s_usd=reconcile_1042s_usd,
        include_capital_gains=include_capital_gains,
        debug=debug,
    )
    return report, out


def test_1042s_reconciliation_not_supplied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = _run(tmp_path, monkeypatch)
    assert report.statement_withholding_usd == Decimal("0.36")  # the in-FY Non-Resident Tax
    assert report.reconcile_1042s_usd is None


def test_1042s_reconciliation_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = _run(tmp_path, monkeypatch, reconcile_1042s_usd=Decimal("0.36"))
    assert report.reconcile_1042s_usd == Decimal("0.36")
    assert report.statement_withholding_usd == Decimal("0.36")  # still the filed figure


def test_1042s_mismatch_refuses_before_compute_and_removes_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #918: two pieces of evidence for the filed withholding disagree, so nothing is filed.
    stale = tmp_path / "out" / "form-67.csv"
    stale.parent.mkdir()
    stale.write_text("STALE", encoding="utf-8")

    def no_compute(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("income was computed after a 1042-S mismatch")

    monkeypatch.setattr(income_mod, "compute_income_schedules", no_compute)
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            config=CONFIG + _READY_OVERRIDE,
            reconcile_1042s_usd=Decimal("0.50"),
        )
    assert str(caught.value) == _1042S_MISMATCH_REFUSAL
    # #1042: neither the typed total nor the statement total is printed.
    assert_no_markers(str(caught.value), "0.50", "0.36")
    assert not stale.exists()


def test_1042s_mismatch_names_neither_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1042: the statement total is a figure from the documents, so the
    refusal says the totals differ without printing either."""
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(replace(_DIV, gross_amount_usd=Decimal("4938271.56")),),
            withholdings=(replace(_WHT, amount_usd=Decimal("-1234567.89")),),
        ),
    }
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            by_name=by_name,
            reconcile_1042s_usd=Decimal("7654321.98"),
        )
    assert str(caught.value) == _1042S_MISMATCH_REFUSAL
    assert_no_markers(str(caught.value), "1234567.89", "1,234,567.89", "7654321.98", "7,654,321.98")


def _rows(out: Path, name: str) -> list[list[str]]:
    # utf-8-sig: the review/filing writers emit a BOM, and it is a no-op on
    # the BOM-less official Form 67 CSV.
    with (out / name).open(encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


# schedule-fsi.csv's Other Sources rows for _DIV and _WHT, after its country
# block of three rows. USD 1.46 and 0.36 at 90.56, the bundled SBI TTBR the run
# uses for the 5 March 2026 payment (27 February, the last rate on or before the
# preceding month-end), are INR 132.2176 and 32.6016, filed in whole rupees.
_FSI_DIVIDEND_ROWS = [
    ["Head of Income", "Other Sources"],
    ["Income from outside India (included in PART B-TI)", "132"],
    ["Tax paid outside India", "33"],
]


# --- happy path ---------------------------------------------------------------


def test_happy_path_emits_four_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, out = _run(tmp_path, monkeypatch, config=CONFIG + _READY_OVERRIDE)
    assert report.dividend_count == 1
    assert report.withholding_count == 1
    assert (report.os_foreign_rows, report.fsi_rows, report.tr_rows, report.form67_entries) == (
        1,
        1,
        1,
        1,
    )
    assert report.manual_fill_warnings == ()
    assert report.form67_readiness is Form67Readiness.READY
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (0, 0)
    for name in ("schedule-os.csv", "schedule-fsi.csv", "schedule-tr.csv", "form-67.csv"):
        assert (out / name).exists()

    fsi = dict(_rows(out, "schedule-fsi.csv"))
    assert fsi["Country Code"] == "2-UNITED STATES OF AMERICA"
    assert fsi["Taxpayer Identification number"] == "123456789"
    form67 = _rows(out, "form-67.csv")
    assert form67[1][1] == "2"
    assert form67[1][3] == "7"


def test_cent_rounded_treaty_withholding_leaves_uncomputed_relief_blank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = dict(_COVERING)
    statements["statement_2026-02.pdf"] = replace(
        statements["statement_2026-02.pdf"],
        withholdings=(StatementWithholding(date(2026, 3, 5), "EXAMPLE CORP", Decimal("-0.37")),),
    )

    report, out = _run(tmp_path, monkeypatch, by_name=statements)

    assert report.form67_readiness is Form67Readiness.BLANK
    assert not (out / "form-67.csv").exists()
    assert (out / "form-67-debug.csv").exists()
    assert (out / "schedule-fsi.csv").exists()
    assert (out / "schedule-tr.csv").exists()


_XESP = ("EXAMPLE CORP", "99999E104")


@pytest.mark.parametrize("agent", [True, False], ids=["agent", "no-agent"])
def test_above_rate_payment_beside_unwithheld_payment_refuses_at_fsi_portal_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: bool
) -> None:
    """#405 E9b (#890): USD 2.75 withheld on a USD 9.12 dividend, then a USD
    10.00 dividend with none. Per payment, the June excess above 25% isn't
    eligible: relief ₹194 against the portal's ₹234, which refuses, as the
    June payment alone (E9a) does. A missing withholding agent doesn't change
    that (#1327)."""
    statements = {
        "statement_2025-06.pdf": _statement(
            date(2025, 6, 1),
            date(2025, 10, 31),
            dividends=(StatementDividend(date(2025, 6, 12), *_XESP, Decimal("9.12")),),
            withholdings=(StatementWithholding(date(2025, 6, 12), _XESP[0], Decimal("-2.75")),),
        ),
        "statement_2025-11.pdf": _statement(
            date(2025, 11, 1),
            date(2025, 12, 31),
            dividends=(StatementDividend(date(2025, 12, 11), *_XESP, Decimal("10.00")),),
        ),
        "statement_2026-01.pdf": _statement(date(2026, 1, 1), date(2026, 3, 31)),
    }
    config = (CONFIG if agent else _without_withholding_agent(CONFIG)).replace(
        "account_opening_date = 2025-11-01", "account_opening_date = 2025-06-01"
    ) + _READY_OVERRIDE.replace('"1000"', '"100000"')

    with pytest.raises(
        InvariantError, match="diverge from the portal's read-only relief"
    ) as caught:
        _run(tmp_path, monkeypatch, by_name=statements, config=config)
    assert dict(caught.value.context) == {
        "country": None,
        "head": None,
        "computed_relief": None,
        "portal_relief": None,
    }
    assert list((tmp_path / "out").iterdir()) == []


def test_schedule_tr_filing_csv_with_quicko_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A US dividend with an exact Indian tax claims §90, so the filing CSV
    carries the TR row and then Quicko's foreign tax credit block."""
    _, out = _run(tmp_path, monkeypatch, config=CONFIG + _READY_OVERRIDE)

    blank = ["", "", "", "", ""]
    assert _rows(out, "schedule-tr.csv") == [
        [
            "Country Code",
            "Taxpayer Identification Number",
            "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each "
            "country)",
            "Total tax relief available (total of (e) of Schedule FSI in respect of each country)",
            "Tax Relief Claimed under section (specify 90, 90A or 91)",
        ],
        # USD 0.36 withheld x 90.56 (the 2026-02-27 TTBR for the dividend's
        # preceding month-end) = 32.6016 -> 33; relief
        # min(32.6016, Indian tax 1000) = 32.6016 -> 33.
        ["2-UNITED STATES OF AMERICA", "123456789", "33", "33", "90"],
        blank,
        blank,
        ["Quicko", "", "", "", ""],
        ["Tax Credits -> Add Manually -> Taxes Withheld Outside India", "", "", "", ""],
        blank,
        ["Country", "Nature Of Income", "", "", ""],
        ["United States of America", "Dividend", "", "", ""],
        blank,
        ["Name of the deductor", "National Financial Services LLC", "", "", ""],
        blank,
        ["Credit Amount", "Tax Withheld", "", "", ""],
        ["132", "33", "", "", ""],  # USD 1.46 gross x 90.56 = 132.2176 -> 132
        blank,
        ["TIN", "123456789", "", "", ""],
        blank,
        ["Section", "Article", "", "", ""],
        ["90", "10", "", "", ""],
    ]
    # Repinned in #1033; the rows above are the whole file.
    assert hashlib.sha256((out / "schedule-tr.csv").read_bytes()).hexdigest() == (
        "6163dd5654d8638c667320b684a7975b114630f3a906946617db357e5f2b5e4d"
    )


def test_quicko_deductor_is_the_withholding_agent_not_the_institution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019: the block names the configured withholding agent, and the A2
    institution name never reaches it."""
    (tmp_path / "configured").mkdir()
    (tmp_path / "renamed").mkdir()
    renamed = CONFIG.replace(
        'institution_name = "Fidelity Stock Plan Services LLC"',
        'institution_name = "Example Custodian LLC"',
    )
    _, out = _run(tmp_path / "configured", monkeypatch, config=CONFIG + _READY_OVERRIDE)
    _, renamed_out = _run(tmp_path / "renamed", monkeypatch, config=renamed + _READY_OVERRIDE)

    assert ["Name of the deductor", "National Financial Services LLC", "", "", ""] in _rows(
        out, "schedule-tr.csv"
    )
    assert (renamed_out / "schedule-tr.csv").read_bytes() == (out / "schedule-tr.csv").read_bytes()


def test_quicko_deductor_may_equal_the_institution_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = CONFIG.replace(
        _AGENT_LINE, 'withholding_agent_name = "Fidelity Stock Plan Services LLC"\n'
    )
    _, out = _run(tmp_path, monkeypatch, config=config + _READY_OVERRIDE)

    assert ["Name of the deductor", "Fidelity Stock Plan Services LLC", "", "", ""] in _rows(
        out, "schedule-tr.csv"
    )


@pytest.mark.parametrize("debug", [False, True], ids=["filing", "debug"])
def test_missing_withholding_agent_blanks_only_the_quicko_deductor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: bool
) -> None:
    """#1327: without the name, a run that writes a Quicko form still writes
    every file a run with the name writes. Only schedule-tr.csv differs, in
    the deductor value alone, and the run asks for manual fill."""
    (tmp_path / "configured").mkdir()
    (tmp_path / "missing").mkdir()
    report, out = _run(
        tmp_path / "configured", monkeypatch, config=CONFIG + _READY_OVERRIDE, debug=debug
    )
    missing_report, missing_out = _run(
        tmp_path / "missing",
        monkeypatch,
        config=_without_withholding_agent(CONFIG) + _READY_OVERRIDE,
        debug=debug,
    )

    assert report.form67_readiness is Form67Readiness.READY
    assert missing_report.form67_readiness is Form67Readiness.READY
    assert not report.manual_fill_required
    assert missing_report.manual_fill_required
    assert missing_report.manual_fill_warnings == (*report.manual_fill_warnings, _BLANK_DEDUCTOR)
    names = sorted(path.name for path in out.iterdir())
    assert sorted(path.name for path in missing_out.iterdir()) == names
    for name in names:
        if name != "schedule-tr.csv":
            assert (missing_out / name).read_bytes() == (out / name).read_bytes(), name
    configured = b"Name of the deductor,National Financial Services LLC,"
    tr_bytes = (out / "schedule-tr.csv").read_bytes()
    assert tr_bytes.count(configured) == 1
    assert (missing_out / "schedule-tr.csv").read_bytes() == tr_bytes.replace(
        configured, b"Name of the deductor,,"
    )


def test_missing_withholding_agent_changes_nothing_without_a_quicko_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no exact Indian tax, no Form 67 entry gets a Quicko form, so the
    setting isn't needed and every output matches a run that has it."""
    (tmp_path / "configured").mkdir()
    (tmp_path / "missing").mkdir()
    report, out = _run(tmp_path / "configured", monkeypatch)
    missing_report, missing_out = _run(
        tmp_path / "missing", monkeypatch, config=_without_withholding_agent(CONFIG)
    )

    assert missing_report.form67_readiness is Form67Readiness.BLANK
    assert missing_report.manual_fill_warnings == report.manual_fill_warnings
    names = sorted(path.name for path in out.iterdir())
    assert sorted(path.name for path in missing_out.iterdir()) == names
    for name in names:
        assert (missing_out / name).read_bytes() == (out / name).read_bytes()


def test_debug_sidecars_are_unified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out = _run(tmp_path, monkeypatch, config=CONFIG + _READY_OVERRIDE, debug=True)
    assert len(_rows(out, "schedule-os.csv")[0]) == 7
    fsi_rows = _rows(out, "schedule-fsi.csv")
    assert fsi_rows[3:6] == _FSI_DIVIDEND_ROWS
    assert all(len(row) == 2 for row in fsi_rows)
    assert len(_rows(out, "schedule-tr.csv")[0]) == 5
    assert len(_rows(out, "form-67.csv")[0]) == 15
    assert len(_rows(out, "schedule-os-debug.csv")[0]) == 11
    assert len(_rows(out, "schedule-fsi-debug.csv")[0]) == 13
    assert len(_rows(out, "schedule-tr-debug.csv")[0]) == 9
    assert len(_rows(out, "form-67-debug.csv")[0]) == 17

    run_netbenefits_income(
        input_dir=tmp_path / "bundle",
        config_path=tmp_path / "user.toml",
        output_dir=out,
        financial_year=FY,
    )
    for filename in (
        "schedule-os-debug.csv",
        "schedule-fsi-debug.csv",
        "schedule-tr-debug.csv",
        "form-67-debug.csv",
    ):
        assert not (out / filename).exists()


def _patch_closed_lot(monkeypatch: pytest.MonkeyPatch, proceeds_usd: str) -> None:
    """Serve a bundle with one in-FY closed lot of a whole 2-share purchase."""
    purchase = EspPurchase(
        purchase_date=date(2025, 12, 1),
        settlement_date=date(2025, 12, 1),
        offering_period_start=date(2025, 11, 3),
        offering_period_end=date(2025, 12, 1),
        symbol="XESP",
        security_description="EXAMPLE CORP",
        cusip="99999E104",
        participant_no=_PNO,
        shares=Decimal("2"),
        purchase_price_per_share_usd=Decimal("90"),
        fmv_total_usd=Decimal("200"),
        contribution_usd=Decimal("180"),
        perquisite_usd=Decimal("20"),
    )
    sale = EspClosedLot(
        acquire_date=date(2025, 12, 1),
        sold_or_transferred_date=date(2026, 3, 20),
        quantity=Decimal("2"),
        proceeds_usd=Decimal(proceeds_usd),
        contribution_usd=Decimal("180"),
        fidelity_term="Short",
    )
    monkeypatch.setattr(
        income_mod,
        "load_bundle",
        lambda _input, **_kwargs: NetBenefitsBundle(
            open_lots=(),
            closed_lots=(sale,),
            confirmations_by_date={purchase.purchase_date: purchase},
        ),
    )


def _with_stale_income_outputs(tmp_path: Path) -> Path:
    """An output folder holding an earlier run's filing and debug files."""
    out = tmp_path / "out"
    out.mkdir()
    for name in INCOME_FILENAMES:
        (out / name).write_text("STALE", encoding="utf-8")
    return out


@pytest.mark.parametrize("proceeds_usd", ["300", "0"], ids=["positive", "zero-proceeds"])
def test_all_mode_refuses_an_in_fy_sale_before_any_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proceeds_usd: str
) -> None:
    """#666: the statements cover the window, but the closed-lots CSV lists an in-FY
    sale, which ibkr-fa can't yet check against them. A zero-proceeds row meets this
    refusal ahead of the compute's own (#894)."""
    _patch_closed_lot(monkeypatch, proceeds_usd)
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, include_capital_gains=True, by_name=_CG_COVERING)
    assert str(caught.value) == _IN_FY_REFUSAL
    # The lot's dates and amounts are file content.
    assert_no_markers(str(caught.value), "2025-12-01", "2026-03-20", "180")
    assert list(out.iterdir()) == []


def test_all_mode_refuses_reports_that_stop_before_ten_days_after_the_fy_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#666: income's own window ends at the FY end, so these reports pass it, but
    a sale traded by then could settle in the ten days after."""
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            include_capital_gains=True,
            csvs={"View closed lots.csv": CLOSED_EMPTY_CSV},
        )
    assert str(caught.value) == _CG_SHORT_REFUSAL
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2025-11-01", "16000")
    assert list(out.iterdir()) == []


def test_capital_gains_coverage_excludes_year_end_reports_itself(tmp_path: Path) -> None:
    """#666: a year-end report cannot bridge the settlement window even when a
    caller passes it directly to the shared helper."""
    cfg_path = tmp_path / "user.toml"
    cfg_path.write_text(CONFIG, encoding="utf-8")
    cfg = income_mod.load_user_config(cfg_path).netbenefits
    assert cfg is not None
    statements = [
        *_COVERING.values(),
        _statement(date(2026, 1, 1), date(2026, 12, 31), report_type=ReportType.YEAR_END),
    ]
    with pytest.raises(NetBenefitsIncomeError) as caught:
        require_capital_gains_coverage(statements, cfg, FY, error=NetBenefitsIncomeError)
    assert str(caught.value) == _CG_SHORT_REFUSAL
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2025-11-01", "16000")


def test_all_mode_refuses_an_opening_after_the_fy_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#666: income's window is empty then, so its own check passes, but an empty
    capital-gains window can't prove no sale was missed, so the run refuses."""
    late = CONFIG.replace("account_opening_date = 2025-11-01", "account_opening_date = 2026-04-15")
    assert late != CONFIG
    quiet = {
        name: replace(stmt, dividends=(), withholdings=()) for name, stmt in _CG_COVERING.items()
    }
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            include_capital_gains=True,
            by_name=quiet,
            config=late,
            csvs={"View closed lots.csv": CLOSED_EMPTY_CSV},
        )
    assert str(caught.value) == _CG_OPENING_REFUSAL
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2026-04-15", "16000")
    assert list(out.iterdir()) == []


def test_all_mode_proven_zero_files_no_capital_gains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#666: reports through ten days after the FY end and no in-FY closed lot: the
    run files income, and Schedule FSI carries no capital-gains row."""
    report, out = _run(
        tmp_path,
        monkeypatch,
        include_capital_gains=True,
        by_name=_CG_COVERING,
        csvs={"View closed lots.csv": CLOSED_EMPTY_CSV},
    )
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (0, 0)
    fsi_rows = _rows(out, "schedule-fsi.csv")
    assert fsi_rows[3:6] == _FSI_DIVIDEND_ROWS
    assert not any(row[:2] == ["Head of Income", "Capital Gains"] for row in fsi_rows)


# Pins (#666): these already refuse through income's own checks before the
# capital-gains ones run, naming the capital-gains window (#1486).
@pytest.mark.parametrize(
    ("by_name", "refusal"),
    [
        pytest.param({}, _ALL_SHORT_COVERAGE_REFUSAL, id="no-reports"),
        pytest.param(
            {k: v for k, v in _CG_COVERING.items() if k != "statement_2026-01.pdf"},
            _ALL_COVERAGE_GAP_REFUSAL,
            id="gap-inside-the-fy",
        ),
    ],
)
def test_all_mode_coverage_refusals_come_from_income_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, StatementData],
    refusal: str,
) -> None:
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            include_capital_gains=True,
            by_name=by_name,
            csvs={"View closed lots.csv": CLOSED_EMPTY_CSV},
        )
    assert str(caught.value) == refusal
    assert_no_markers(str(caught.value), "XESP", "99999E104", "2025-11-01", "16000")
    assert list(out.iterdir()) == []


_FIXTURES = Path(__file__).parent / "fixtures" / "netbenefits"


def test_all_mode_refuses_a_report_holding_a_sale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (#666): a sale row inside the FY already refuses through #1149's parser,
    before the capital-gains checks; the committed fixtures are parsed for real."""
    names = ("statement-oct-dec", "statement-jan", "statement-feb-mar", "statement-apr")
    texts: dict[str, str] = {
        f"{name}.pdf": (_FIXTURES / f"{name}.txt").read_text(encoding="utf-8") for name in names
    }
    texts["statement-feb-mar.pdf"] += (
        "t 03/20 SYNTHETIC HOLDER TWO 99997Q104 You Sold -1.000 $400.00000 - $400.00\n"
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in texts:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    (bundle / "View closed lots.csv").write_text(CLOSED_EMPTY_CSV, encoding="utf-8")
    patch_pdftotext(monkeypatch, texts)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(ParseError) as caught:
        run_netbenefits_income(
            input_dir=bundle,
            config_path=cfg,
            output_dir=out,
            financial_year=FY,
            include_capital_gains=True,
        )
    error = caught.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "statement statement-feb-mar.pdf",
        0,
        None,
        "",
    )
    assert error.message == DATED_LINE_UNREAD
    assert_no_markers(str(error), "Synthetic Holder Two", "99997Q104", "400.00", "03/20")
    assert list(out.iterdir()) == []


def test_income_without_capital_gains_ignores_closed_lots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_closed_lot(monkeypatch, "0")
    report, _ = _run(tmp_path, monkeypatch)
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (0, 0)


# #911: an empty closed-lots export beside a renamed one that lists a sale.
_RENAMED_CLOSED_LOTS = {
    "View closed lots.csv": CLOSED_EMPTY_CSV,
    "View closed lots (1).csv": CLOSED_RENAMED_PROCEEDS_CSV,
}


def test_all_mode_refuses_a_renamed_closed_lots_export_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "out" / "schedule-fsi.csv"
    stale.parent.mkdir()
    stale.write_text("STALE", encoding="utf-8")
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, include_capital_gains=True, csvs=_RENAMED_CLOSED_LOTS)
    assert str(caught.value) == f"View closed lots (1).csv {CLOSED_RENAMED_PROCEEDS_REFUSAL}"
    assert list((tmp_path / "out").iterdir()) == []


def test_income_without_capital_gains_ignores_a_renamed_closed_lots_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, out = _run(tmp_path, monkeypatch, csvs=_RENAMED_CLOSED_LOTS)
    assert (report.cg_stcg_rows, report.cg_ltcg_rows) == (0, 0)
    assert (out / "schedule-fsi.csv").exists()


# #914: "$74.34-" used to parse as +74.34, which still passes the perquisite check.
_SIGNED_GAIN_CONFIRMATION = CONF_DEC.replace("$74.34\n", "$74.34-\n")


def test_all_mode_refuses_a_signed_confirmation_gain_and_clears_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _SIGNED_GAIN_CONFIRMATION != CONF_DEC
    stale = tmp_path / "out" / "schedule-fsi.csv"
    stale.parent.mkdir()
    stale.write_text("STALE", encoding="utf-8")
    with pytest.raises(ParseError, match="column 'Gain': expected exactly one Gain; found 0"):
        _run(
            tmp_path,
            monkeypatch,
            include_capital_gains=True,
            confirmations=("espp-purchase_2025-12-31.pdf",),
            confirmation_text=_SIGNED_GAIN_CONFIRMATION,
            csvs={"View closed lots.csv": CLOSED_EMPTY_CSV},
        )
    assert list((tmp_path / "out").iterdir()) == []


def test_official_form67_is_withheld_when_tax_input_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = CONFIG.replace('[account.foreign_tins]\nUS = "123456789"\n\n', "")
    report, out = _run(tmp_path, monkeypatch, config=cfg)
    assert report.form67_entries > 0
    assert report.form67_readiness is Form67Readiness.BLANK
    assert not (out / "form-67.csv").exists()
    assert _rows(out, "form-67-debug.csv")[1]
    assert any(
        "INDIAN TAX CALCULATION REQUIRED" in warning for warning in report.manual_fill_warnings
    )


def test_missing_us_tin_warns_with_passport_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = CONFIG.replace('[account.foreign_tins]\nUS = "123456789"\n\n', "")
    report, out = _run(tmp_path, monkeypatch, config=cfg)

    assert len(report.manual_fill_warnings) == 2
    assert "INDIAN TAX CALCULATION REQUIRED" in report.manual_fill_warnings[0]
    assert report.manual_fill_warnings[1] == BLANK_RELIEF_TIN_WARNING
    assert ["2-UNITED STATES OF AMERICA", "", "33", "", ""] in _rows(out, "schedule-tr.csv")


@pytest.mark.parametrize(("tin_country", "manual"), [("GB", True), ("US", False)])
def test_blank_relief_tin_alone_requires_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tin_country: str, manual: bool
) -> None:
    config, statements = _marked_ready_run(tin_country)
    report, _ = _run(tmp_path, monkeypatch, by_name=statements, config=config)
    assert report.form67_readiness is Form67Readiness.READY
    assert report.manual_fill_required is manual
    assert report.manual_fill_warnings == ((BLANK_RELIEF_TIN_WARNING,) if manual else ())


def test_confirmation_pdf_in_bundle_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = _run(tmp_path, monkeypatch, confirmations=("espp-purchase.pdf",))
    assert report.dividend_count == 1


def test_year_end_report_excluded_from_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A YEAR-END report overlaps the dividend's date but lists NO dividend rows.
    # Its silence is NOT proof of absence, so it must neither suppress the real
    # dividend nor trip an overlap conflict — it's dropped from the income pipeline.
    by_name = {
        **_COVERING,
        "statement_2026-year-end.pdf": _statement(
            date(2026, 1, 1), date(2026, 12, 31), report_type=ReportType.YEAR_END
        ),
    }
    report, _ = _run(tmp_path, monkeypatch, by_name=by_name)
    assert report.dividend_count == 1
    assert report.withholding_count == 1


def test_correcting_the_opening_date_needs_coverage_from_the_corrected_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1471: coverage is checked from the configured opening date, so October
    statements with an October dividend and December-March statements pass it
    for a December 1 opening; the dividend then refuses. Moving the opening
    date back as told moves the coverage window onto November, which no
    statement covers, so the refusal also says what the statements must then
    cover. Supplying November clears the run."""
    statements = {
        "statement_2025-10.pdf": _statement(
            date(2025, 10, 1),
            date(2025, 10, 31),
            dividends=(
                StatementDividend(date(2025, 10, 15), "EXAMPLE CORP", "99999E104", Decimal("1.46")),
            ),
        ),
        "statement_2025-12.pdf": _statement(date(2025, 12, 1), date(2026, 3, 31)),
    }
    configured = CONFIG.replace(
        "account_opening_date = 2025-11-01", "account_opening_date = 2025-12-01"
    )
    corrected = CONFIG.replace(
        "account_opening_date = 2025-11-01", "account_opening_date = 2025-10-01"
    )
    for name in ("configured", "corrected", "covered"):
        (tmp_path / name).mkdir()

    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path / "configured", monkeypatch, by_name=statements, config=configured)
    assert str(caught.value) == (
        f"the dividend at {_event_at(1, 1, _DIVIDENDS_TABLE)} predates "
        "netbenefits.account.account_opening_date; income can't precede the account, and "
        "coverage before it isn't required — fix netbenefits.account.account_opening_date, "
        f"{_COVER_FROM_CORRECTED_DATE}"
    )

    with pytest.raises(NetBenefitsIncomeError) as caught_gap:
        _run(tmp_path / "corrected", monkeypatch, by_name=statements, config=corrected)
    assert str(caught_gap.value) == _COVERAGE_GAP_REFUSAL

    november = {"statement_2025-11.pdf": _statement(date(2025, 11, 1), date(2025, 11, 30))}
    report, _ = _run(
        tmp_path / "covered", monkeypatch, by_name={**statements, **november}, config=corrected
    )
    assert report.dividend_count == 1


@pytest.mark.parametrize("kind", ["dividend", "withholding"])
@pytest.mark.parametrize(
    ("include_capital_gains", "cover"),
    [(False, _COVER_FROM_CORRECTED_DATE), (True, _CG_COVER_FROM_CORRECTED_DATE)],
    ids=["income", "capital-gains"],
)
def test_opening_date_refusal_names_the_coverage_its_run_needs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    include_capital_gains: bool,
    cover: str,
) -> None:
    """#1478: the income leg's own refusal names the coverage its run needs:
    through the FY end for income alone, as before, and through ten days after
    it with capital gains. (netbenefits-all's preflight refuses before this leg.)
    The ``withholding`` cases pin a defensive branch through the patched
    parser: no real statement holds their withholding (see ``_january_statements``)."""
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(
            tmp_path,
            monkeypatch,
            by_name=_january_statements(kind),
            config=_MISTAKEN_OPENING,
            include_capital_gains=include_capital_gains,
        )
    assert str(caught.value) == _opening_date_refusal(kind, cover)
    assert_no_markers(str(caught.value), "2026-01-15", "2026-02-01")


@pytest.mark.parametrize("kind", ["dividend", "withholding"])
def test_cli_income_opening_date_refusal_keeps_its_coverage_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """#1478: netbenefits-income alone needs coverage only through the FY end,
    so it prints the #1471 coverage advice byte for byte. The withholding
    variant also names its missing dividend (#1486); it pins a defensive branch
    through the patched parser, as no real statement holds its withholding (see
    ``_january_statements``)."""
    by_name = _january_statements(kind)
    assert _cli_income(tmp_path, monkeypatch, config=_MISTAKEN_OPENING, by_name=by_name) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_opening_date_refusal(kind, _COVER_FROM_CORRECTED_DATE)}\n"


@pytest.mark.parametrize(
    ("by_name", "refusal"),
    [
        pytest.param(_SHORT_OF_THE_FY_END, _SHORT_COVERAGE_REFUSAL, id="short-end"),
        pytest.param(_GAP_IN_JANUARY, _COVERAGE_GAP_REFUSAL, id="gap-inside-the-window"),
    ],
)
def test_cli_income_coverage_refusal_keeps_its_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    by_name: dict[str, StatementData],
    refusal: str,
) -> None:
    """#1486: netbenefits-income alone needs coverage only through the FY end, so
    on netbenefits-all's inputs it still names the income window, byte for byte."""
    assert _cli_income(tmp_path, monkeypatch, by_name=by_name) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {refusal}\n"


def test_year_end_report_cannot_satisfy_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drop the Nov-Dec SPS month and add a full-year YEAR-END report covering it.
    # The year-end is excluded, so the income coverage gap is still exposed and
    # refused (a year-end's silence can't prove no dividend fell in that month).
    by_name = {k: v for k, v in _COVERING.items() if k != "statement_2025-11.pdf"}
    by_name["statement_2025-year-end.pdf"] = _statement(
        date(2025, 1, 1), date(2025, 12, 31), report_type=ReportType.YEAR_END
    )
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert str(caught.value) == _COVERAGE_GAP_REFUSAL
    # #1042: the gap's dates (2025-11-01..2025-12-31) are not printed.
    assert_no_markers(str(caught.value), "2025-11-01", "2025-12-31")


def test_failed_write_leaves_no_stale_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the 3rd writer (schedule-tr) to fail with pre-existing stale outputs.
    # Fail-closed: the whole output set is removed.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in _COVERING:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch(monkeypatch, _COVERING)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    filenames = (
        "schedule-os.csv",
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67.csv",
        "schedule-os-debug.csv",
        "schedule-fsi-debug.csv",
        "schedule-tr-debug.csv",
        "form-67-debug.csv",
    )
    for name in filenames:
        (out / name).write_text("STALE", encoding="utf-8")

    class _Boom(StubWriter):
        def write(self, payload: object, path: Path) -> None:
            raise OSError("disk full")

    # schedule-tr, the third write
    patch_writers(monkeypatch, Subcommand.TR, _Boom)

    with pytest.raises(OSError, match="disk full"):
        run_netbenefits_income(input_dir=bundle, config_path=cfg, output_dir=out, financial_year=FY)

    assert all(not (out / name).exists() for name in filenames)


def test_config_failure_removes_stale_schedules(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "schedule-os.csv"
    stale.write_text("STALE", encoding="utf-8")
    config = tmp_path / "invalid.toml"
    config.write_text("not valid toml", encoding="utf-8")

    with pytest.raises(ConfigError):
        run_netbenefits_income(
            input_dir=tmp_path / "unused",
            config_path=config,
            output_dir=out,
            financial_year=FY,
        )

    assert not stale.exists()


# --- refusals -----------------------------------------------------------------


def test_coverage_gap_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    by_name = {k: v for k, v in _COVERING.items() if k != "statement_2025-11.pdf"}
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert str(caught.value) == _COVERAGE_GAP_REFUSAL
    # #1042: the gap (2025-11-01..2025-12-31) is named by the window, not its dates.
    assert_no_markers(str(caught.value), "2025-11-01", "2025-12-31")


def test_short_coverage_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    by_name = {k: v for k, v in _COVERING.items() if k != "statement_2026-02.pdf"}
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert str(caught.value) == _SHORT_COVERAGE_REFUSAL
    # #1042: neither the last covered day nor the window's end date is printed.
    assert_no_markers(str(caught.value), "2026-01-31", "2026-02-01", "2026-03-31")


def test_coverage_gap_refuses_before_a_duplicate_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Coverage is checked before the statements are normalized, so the gap is
    # reported, not the duplicate dividend.
    by_name = {k: v for k, v in _COVERING.items() if k != "statement_2025-11.pdf"}
    by_name["statement_2026-02.pdf"] = _statement(
        date(2026, 2, 1), date(2026, 3, 31), dividends=(_DIV, _DIV)
    )
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert str(caught.value) == _COVERAGE_GAP_REFUSAL
    assert_no_markers(str(caught.value), "2025-11-01", "2025-12-31")


def test_no_in_fy_dividend_emits_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No dividend, but statement coverage is proven -> a safe zero: emit empty
    # schedules + exit OK (like netbenefits-cg on no-sales), not a hard error (#361).
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(date(2026, 2, 1), date(2026, 3, 31)),
    }
    report, out = _run(tmp_path, monkeypatch, by_name=by_name)
    assert report.dividend_count == 0
    assert report.withholding_count == 0
    assert (out / "schedule-os.csv").exists()
    assert (out / "form-67.csv").exists()


def test_no_dividend_1042s_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #361/#918: a zero-withholding run with a supplied NONZERO 1042-S can signal
    # missed income, so it refuses.
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(date(2026, 2, 1), date(2026, 3, 31)),
    }
    with pytest.raises(NetBenefitsIncomeError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name, reconcile_1042s_usd=Decimal("0.50"))
    assert str(caught.value) == _1042S_MISMATCH_REFUSAL
    # #1042: neither the typed total nor the zero statement total is printed.
    assert_no_markers(str(caught.value), "0.50", "0.00")


def test_no_manual_fill_warnings_on_zero_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #361: a proven-zero run has no FTC claim, so the FTC TIN / Indian tax
    # calculation warnings must not fire (nothing to fill).
    cfg = CONFIG.replace('[account.foreign_tins]\nUS = "123456789"\n\n', "")
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(date(2026, 2, 1), date(2026, 3, 31)),
    }
    report, _ = _run(tmp_path, monkeypatch, by_name=by_name, config=cfg)
    assert report.withholding_count == 0
    assert report.manual_fill_warnings == ()
    assert not report.manual_fill_required


def test_other_cusip_dividend_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A THIRD security still refuses: only the ESPP stock and the core fund
    exist. #1042: the refusal names the row and the config field, not the
    CUSIPs or the date."""
    other_div = StatementDividend(
        date(2026, 3, 17), "SAMPLE ISSUER BN INC", "99993A108", Decimal("1.00")
    )
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1), date(2026, 3, 31), dividends=(_DIV, other_div)
        ),
    }
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert str(caught.value) == (
        f"the in-FY dividend at {_event_at(3, 2, _DIVIDENDS_TABLE)} is for a CUSIP that is "
        "neither netbenefits.security.cusip nor the core money-market fund"
    )
    assert_no_markers(
        str(caught.value), "99993A108", "99999E104", "31617H821", "2026-03-17", "03/17/2026"
    )


# #1042: the January statement sorts after the February one by name, and a
# year-end report starts before both; the income step skips year-end reports
# and numbers the rest by period start.
_JAN_DIV = StatementDividend(date(2026, 1, 15), "EXAMPLE CORP", "99999E104", Decimal("1.40"))
_JAN_WHT = StatementWithholding(date(2026, 1, 15), "EXAMPLE CORP", Decimal("-0.35"))


@pytest.mark.parametrize(
    ("bad_file", "number", "orphan_day"),
    [
        ("b-jan.pdf", 1, date(2026, 1, 22)),
        ("a-feb.pdf", 2, date(2026, 2, 17)),
    ],
    ids=["january-statement", "february-statement"],
)
def test_statements_are_numbered_by_period_start_not_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_file: str,
    number: int,
    orphan_day: date,
) -> None:
    orphan = StatementWithholding(orphan_day, "Synthetic Holder Two", Decimal("-0.37"))
    by_name = {
        "a-feb.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_DIV,),
            withholdings=(_WHT, *((orphan,) if bad_file == "a-feb.pdf" else ())),
        ),
        "a-year-end.pdf": _statement(
            date(2025, 1, 1), date(2025, 12, 31), report_type=ReportType.YEAR_END
        ),
        "b-jan.pdf": _statement(
            date(2026, 1, 1),
            date(2026, 1, 31),
            dividends=(_JAN_DIV,),
            withholdings=(_JAN_WHT, *((orphan,) if bad_file == "b-jan.pdf" else ())),
        ),
    }
    config = CONFIG.replace(
        "account_opening_date = 2025-11-01", "account_opening_date = 2026-01-01"
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name, config=config)
    assert str(caught.value) == (
        f"the withholding at statement {number} (by period start, year-end reports "
        "excluded), row 2 of its Taxes Withheld table matches 0 in-FY dividend(s); expected "
        "exactly one (ambiguous or orphan)"
    )
    assert_no_markers(
        str(caught.value),
        "Synthetic Holder Two",
        orphan_day.isoformat(),
        orphan_day.strftime("%m/%d/%Y"),
    )


# --- year-end income rows (#1249) -----------------------------------------------


def _nov_dec(
    *,
    dividends: tuple[StatementDividend, ...] = (_YE_DIV,),
    withholdings: tuple[StatementWithholding, ...] = (_YE_WHT,),
) -> StatementData:
    """The November-December report, printing ``dividends`` and ``withholdings``."""
    return _statement(
        date(2025, 11, 1), date(2025, 12, 31), dividends=dividends, withholdings=withholdings
    )


_BEFORE_OPENING_DIV = replace(_YE_DIV, date=date(2025, 10, 15))
_BEFORE_OPENING_WHT = replace(_YE_WHT, date=date(2025, 10, 15))
_YEAR_END_INCOME_REFUSALS = pytest.mark.parametrize(
    ("by_name", "message", "markers"),
    [
        pytest.param(
            {**_QUIET, _YE_FILE: _year_end()},
            _year_end_refusal("dividend"),
            _YE_MARKERS,
            id="no-report-lists-it",
        ),
        pytest.param(
            {
                **_QUIET,
                _NOV_DEC: _nov_dec(dividends=(replace(_YE_DIV, gross_amount_usd=Decimal("9.86")),)),
                _YE_FILE: _year_end(),
            },
            _year_end_refusal("dividend"),
            (*_YE_MARKERS, "9.86"),
            id="different-dividend-amount",
        ),
        pytest.param(
            {**_QUIET, _NOV_DEC: _nov_dec(withholdings=()), _YE_FILE: _year_end()},
            _year_end_refusal("withholding"),
            _YE_MARKERS,
            id="withholding-the-report-leaves-out",
        ),
        pytest.param(
            {
                **_QUIET,
                _YE_FILE: _year_end(
                    dividends=(_BEFORE_OPENING_DIV,), withholdings=(_BEFORE_OPENING_WHT,)
                ),
            },
            _year_end_refusal("dividend"),
            (_YE_NAME, "9.87", "2.47", "2025-10-15"),
            id="dividend-before-the-opening-date",
        ),
        pytest.param(
            {**_QUIET, _NOV_DEC: _nov_dec(), _YE_FILE: _year_end(dividends=(_YE_DIV, _YE_DIV))},
            _year_end_refusal("dividend", row=2),
            _YE_MARKERS,
            id="dividend-printed-more-times-than-the-report-lists-it",
        ),
    ],
)


@_YEAR_END_INCOME_REFUSALS
def test_year_end_income_the_reports_dont_list_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, StatementData],
    message: str,
    markers: tuple[str, ...],
) -> None:
    """#1249: a year-end report's silence proves nothing, but an in-FY income row it
    prints must be listed, with the same amount, by a Stock Plan Services report
    covering its day. The refusal writes nothing and removes stale outputs."""
    out = _with_stale_income_outputs(tmp_path)
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name, config=CONFIG + _READY_OVERRIDE)
    assert str(caught.value) == message
    assert_prints_no_marker(caught.value, markers=markers)
    assert list(out.iterdir()) == []


@_YEAR_END_INCOME_REFUSALS
def test_cli_year_end_income_the_reports_dont_list_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    by_name: dict[str, StatementData],
    message: str,
    markers: tuple[str, ...],
) -> None:
    """#1249: ``netbenefits-income`` exits 4 with the refusal and no output."""
    assert _cli_income(tmp_path, monkeypatch, config=CONFIG + _READY_OVERRIDE, by_name=by_name) == 4
    assert list(tmp_path.glob("*/out/*")) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {message}\n"
    assert_no_markers(captured.err, *markers)


def test_income_the_reports_list_is_filed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin (#1249): the year-end rows moved to the November-December report file one
    dividend and one withholding."""
    by_name = {**_QUIET, _NOV_DEC: _nov_dec(), _YE_FILE: _year_end(dividends=(), withholdings=())}
    report, _ = _run(tmp_path, monkeypatch, by_name=by_name, config=CONFIG + _READY_OVERRIDE)
    assert (report.dividend_count, report.withholding_count) == (1, 1)


def _outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by_name: dict[str, StatementData]
) -> dict[str, bytes]:
    """Every output's bytes from a debug income run over ``by_name``."""
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    _, out = _run(root, monkeypatch, by_name=by_name, config=CONFIG + _READY_OVERRIDE, debug=True)
    return {path.name: path.read_bytes() for path in sorted(out.iterdir())}


@pytest.mark.parametrize(
    ("silent", "printing"),
    [
        pytest.param(
            {**_QUIET, _NOV_DEC: _nov_dec(), _YE_FILE: _year_end(dividends=(), withholdings=())},
            {**_QUIET, _NOV_DEC: _nov_dec(), _YE_FILE: _year_end()},
            id="rows-the-report-lists",
        ),
        pytest.param(
            {**_COVERING, _YE_FILE: _year_end(dividends=(), withholdings=())},
            {
                **_COVERING,
                _YE_FILE: _year_end(
                    dividends=(replace(_YE_DIV, date=date(2025, 3, 31)),),
                    withholdings=(replace(_YE_WHT, date=date(2025, 3, 31)),),
                ),
            },
            id="rows-before-the-fy",
        ),
        pytest.param(
            {**_COVERING, _YE_FILE: _year_end(dividends=(), withholdings=(), year=2026)},
            {
                **_COVERING,
                _YE_FILE: _year_end(
                    dividends=(replace(_YE_DIV, date=date(2026, 4, 1)),),
                    withholdings=(replace(_YE_WHT, date=date(2026, 4, 1)),),
                    year=2026,
                ),
            },
            id="rows-after-the-fy",
        ),
    ],
)
def test_year_end_income_that_is_listed_or_outside_the_fy_changes_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    silent: dict[str, StatementData],
    printing: dict[str, StatementData],
) -> None:
    """#1249: year-end rows the covering report lists, or dated outside the FY, give
    the same output bytes as a silent year-end report."""
    expected = _outputs(tmp_path, monkeypatch, silent)
    assert expected
    assert _outputs(tmp_path, monkeypatch, printing) == expected


@pytest.mark.parametrize(
    ("by_name", "error", "message", "markers"),
    [
        pytest.param(
            {"statement_2026-01.pdf": _QUIET["statement_2026-01.pdf"], _YE_FILE: _year_end()},
            NetBenefitsIncomeError,
            _COVERAGE_GAP_REFUSAL,
            _YE_MARKERS,
            id="coverage-gap",
        ),
        pytest.param(
            {_NOV_DEC: _QUIET[_NOV_DEC], _YE_FILE: _year_end()},
            NetBenefitsIncomeError,
            _SHORT_COVERAGE_REFUSAL,
            _YE_MARKERS,
            id="short-coverage",
        ),
        pytest.param(
            {
                **_QUIET,
                _NOV_DEC: _nov_dec(
                    dividends=(), withholdings=(replace(_YE_WHT, date=date(2025, 12, 1)),)
                ),
                _YE_FILE: _year_end(),
            },
            NetBenefitsBundleError,
            f"the withholding at {_event_at(1, 1, _TAXES_TABLE)} matches 0 in-FY dividend(s); "
            "expected exactly one (ambiguous or orphan)",
            (*_YE_MARKERS, "2025-12-01"),
            id="orphan-withholding",
        ),
    ],
)
def test_year_end_income_is_checked_after_the_existing_income_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, StatementData],
    error: type[Exception],
    message: str,
    markers: tuple[str, ...],
) -> None:
    """#1249: an input that refuses today keeps its refusal; the year-end check runs
    after coverage and ``normalize_income``."""
    with pytest.raises(error) as caught:
        _run(tmp_path, monkeypatch, by_name=by_name)
    assert type(caught.value) is error
    assert str(caught.value) == message
    assert_prints_no_marker(caught.value, markers=markers)


# --- the account's core money-market fund (the cash sweep) --------------------


def test_core_fund_dividend_is_income_labelled_as_the_fund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core fund pays its own dividend; it is a SECOND security, not the stock.

    Mislabelling it as the configured ESPP stock would file the fund's income under
    the wrong ISIN.
    """
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_DIV, _FUND_DIV),
            withholdings=(_WHT, _FUND_WHT),
        ),
    }
    report, out = _run(tmp_path, monkeypatch, by_name=by_name, debug=True)
    assert report.dividend_count == 2
    assert report.withholding_count == 2
    # Both are US foreign dividends, so Schedule OS 1a(i) carries their SUM and
    # Schedule FSI / TR / Form 67 stay ONE row each (same country + head + source).
    assert (report.fsi_rows, report.tr_rows, report.form67_entries) == (1, 1, 1)
    # The point of the test: each row carries its OWN security identity.
    detail = _rows(out, "schedule-os-debug.csv")
    by_symbol = {row[0]: row[1] for row in detail[1:] if row and row[0] in {"XESP", "FYIXX"}}
    assert by_symbol == {"XESP": "US99999E1047", "FYIXX": "US31617H8218"}


def test_core_fund_withholding_takes_the_funds_identity_not_the_stocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withholding table prints no CUSIP, so identity comes from the matched
    dividend. Defaulting to the configured stock would mislabel the fund's tax."""
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_FUND_DIV,),
            withholdings=(_FUND_WHT,),
        ),
    }
    report, _out = _run(tmp_path, monkeypatch, by_name=by_name)
    assert (report.dividend_count, report.withholding_count) == (1, 1)


def test_same_date_dividends_from_both_securities_pair_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two securities CAN pay on the same date; each withholding must pair with its
    own dividend, not the other's. No output file lists a withholding's security,
    so test_same_date_withholdings_take_their_own_dividends_identity in
    tests/test_normalize_netbenefits_income.py pins each one's; this run files
    both pairs."""
    same_day_fund = StatementDividend(date(2026, 3, 5), _FUND_NAME, "31617H821", Decimal("1.12"))
    same_day_fund_wht = StatementWithholding(date(2026, 3, 5), _FUND_NAME, Decimal("-0.28"))
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_DIV, same_day_fund),
            withholdings=(_WHT, same_day_fund_wht),
        ),
    }
    report, _out = _run(tmp_path, monkeypatch, by_name=by_name)
    assert (report.dividend_count, report.withholding_count) == (2, 2)


def test_one_withholding_line_for_same_day_stock_and_fund_dividends_names_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1469: the stock and the core fund pay on one day, and one withholding
    line, naming the stock, holds the tax on both: more than the stock's own
    USD 2.00 dividend. That payment can't be tested on its own (#890), so no
    exact [[form67.tax_payable_overrides]] amount clears US/DIVIDEND. The
    warning and the Schedule FSI guidance name the gap and offer no override."""
    stock = replace(_DIV, gross_amount_usd=Decimal("2.00"))
    fund = replace(_FUND_DIV, date=_DIV.date, gross_amount_usd=Decimal("10.00"))
    shared = replace(_WHT, amount_usd=Decimal("-3.00"))
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1), date(2026, 3, 31), dividends=(stock, fund), withholdings=(shared,)
        ),
    }
    warning = f"{MANUAL_METHOD} " + wont_clear(same_day_gap("US/DIVIDEND"), "US/DIVIDEND")

    report, out = _run(tmp_path, monkeypatch, by_name=by_name)

    assert report.form67_readiness is Form67Readiness.BLANK
    assert report.manual_fill_warnings == (warning,)
    guidance = [
        row for row in _rows(out, "schedule-fsi.csv") if row[:1] == ["Manual fill guidance"]
    ]
    assert guidance == [["Manual fill guidance", warning]]


def test_core_fund_dividend_without_withholding_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fund can pay a dividend with no withholding row. That payment is
    income without FTC."""
    by_name = {
        **_COVERING,
        "statement_2026-02.pdf": _statement(
            date(2026, 2, 1),
            date(2026, 3, 31),
            dividends=(_DIV, _FUND_DIV),
            withholdings=(_WHT,),
        ),
    }
    report, _out = _run(tmp_path, monkeypatch, by_name=by_name)
    assert (report.dividend_count, report.withholding_count) == (2, 1)


def test_configuring_the_core_fund_as_the_security_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuring the cash sweep as the ESPP security would collapse the two
    identities and make every downstream label ambiguous."""
    assert 'cusip = "99999E104"' in CONFIG and 'isin = "US99999E1047"' in CONFIG
    cfg = CONFIG.replace('cusip = "99999E104"', 'cusip = "31617H821"').replace(
        'isin = "US99999E1047"', 'isin = "US31617H8218"'
    )
    with pytest.raises(NetBenefitsBundleError) as caught:
        _run(tmp_path, monkeypatch, config=cfg)
    assert str(caught.value) == _CORE_FUND_CONFIG_REFUSAL
    # #1042: the configured CUSIP is named by its config field.
    assert_no_markers(str(caught.value), "31617H821")


def test_participant_mismatch_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = CONFIG.replace('participant_no = "I99999999"', 'participant_no = "I88888888"')
    with pytest.raises(ParseError, match="participant mismatch"):
        _run(tmp_path, monkeypatch, config=cfg)


def test_no_netbenefits_block_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = CONFIG.split("[netbenefits]", maxsplit=1)[0]
    with pytest.raises(NetBenefitsIncomeError, match=r"no \[netbenefits\] block"):
        _run(tmp_path, monkeypatch, config=cfg)


def test_input_not_a_directory_refuses(tmp_path: Path) -> None:
    not_dir = tmp_path / "nope.txt"
    not_dir.write_text("x", encoding="utf-8")
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(NetBenefitsBundleError, match="input path is not a directory"):
        run_netbenefits_income(
            input_dir=not_dir, config_path=cfg, output_dir=out, financial_year=FY
        )


def test_output_not_a_directory_refuses(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    not_dir = tmp_path / "out.txt"
    not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(NetBenefitsIncomeError, match="output path is not a directory"):
        run_netbenefits_income(
            input_dir=bundle, config_path=cfg, output_dir=not_dir, financial_year=FY
        )


_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


@_PERMISSIONS_ENFORCED
def test_output_folder_check_raises_eacces_instead_of_calling_it_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """#1238: the output-folder check raises EACCES instead of calling the folder missing."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    loads: list[Path] = []
    for loader in ("load_bundle", "load_statements"):
        monkeypatch.setattr(income_mod, loader, lambda path, *_a, **_kw: loads.append(path))
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    blocked = tmp_path / "blocked"
    out = blocked / "out"
    out.mkdir(parents=True)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        run_netbenefits_income(input_dir=bundle, config_path=cfg, output_dir=out, financial_year=FY)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(out)!r}"
    assert loads == []
    blocked.chmod(0o700)
    assert list(out.iterdir()) == []


# --- CLI ----------------------------------------------------------------------


def test_cli_emits_and_exits_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in _COVERING:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch(monkeypatch, _COVERING)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG + _READY_OVERRIDE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    code = main(
        [
            "netbenefits-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 0
    assert "Form 67 entries: 1" in capsys.readouterr().out
    assert (out / "schedule-os.csv").exists()


def _cli_income(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    config: str = CONFIG,
    by_name: dict[str, StatementData] = _COVERING,
) -> int:
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    bundle = root / "bundle"
    bundle.mkdir()
    for name in by_name:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch(monkeypatch, by_name)
    cfg = root / "user.toml"
    cfg.write_text(config, encoding="utf-8")
    out = _with_stale_income_outputs(root)
    return main(
        [
            "netbenefits-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
            *extra,
        ]
    )


def test_cli_1042s_advisory_when_not_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli_income(tmp_path, monkeypatch, config=CONFIG + _READY_OVERRIDE) == 0
    out = capsys.readouterr().out
    assert "statement-derived" in out
    assert "--reconcile-1042s-fy-withholding-usd" in out


def test_cli_1042s_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    flag = "--reconcile-1042s-fy-withholding-usd"
    assert _cli_income(tmp_path, monkeypatch, flag, "0.36", config=CONFIG + _READY_OVERRIDE) == 0
    assert "1042-S reconciliation: the FY-allocated 1042-S total (USD 0.36) matches" in (
        capsys.readouterr().out
    )


# ``rendered`` is how the refusal printed the typed total before #1042.
@pytest.mark.parametrize(
    ("supplied", "rendered"),
    [("0.50", "0.50"), ("999999999999999.99", "999999999999999.99")],
)
def test_cli_1042s_mismatch_exits_4_with_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    supplied: str,
    rendered: str,
) -> None:
    # #918: a huge cent-exact value is compared without quantize, so it refuses
    # like any other mismatch instead of crashing. Since #1275 the flag takes at
    # most 15 whole digits (1E100 is a usage error, in
    # test_number_limits_netbenefits.py), so the largest value it takes stands in.
    flag = "--reconcile-1042s-fy-withholding-usd"
    assert _cli_income(tmp_path, monkeypatch, flag, supplied, config=CONFIG + _READY_OVERRIDE) == 4
    assert list(tmp_path.glob("*/out/*")) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_1042S_MISMATCH_REFUSAL}\n"
    # #1042: neither the typed total nor the statement total (USD 0.36) is printed.
    assert_no_markers(captured.err, supplied, rendered, "0.36")


def test_cli_1042s_flag_rejects_invalid_amount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # >2 decimals (literal, incl. trailing-but-not-numeric like 1.2300) / negative
    # are usage errors (argparse exit 2).
    for bad in ("0.444", "1.2300", "-1.00", "nan"):
        with pytest.raises(SystemExit) as exc:
            _cli_income(tmp_path, monkeypatch, "--reconcile-1042s-fy-withholding-usd", bad)
        assert exc.value.code == 2


def test_cli_refusal_exits_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    by_name = {k: v for k, v in _COVERING.items() if k != "statement_2025-11.pdf"}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in by_name:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch(monkeypatch, by_name)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    code = main(
        [
            "netbenefits-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert err == f"error: {_COVERAGE_GAP_REFUSAL}\n"
    assert_no_markers(err, "2025-11-01", "2025-12-31")


def test_cli_overlapping_statements_that_disagree_exit_4_and_remove_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #943: the March statement leaves out the 5 March income that the
    # overlapping February-March statement lists. #1042: by period start the
    # February-March statement is statement 3 and the March one statement 4.
    by_name = {
        **_COVERING,
        "statement_2026-03.pdf": _statement(date(2026, 3, 1), date(2026, 3, 31)),
    }
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in by_name:
        (bundle / name).write_bytes(b"%PDF-1.4 placeholder")
    _patch(monkeypatch, by_name)
    cfg = tmp_path / "user.toml"
    cfg.write_text(CONFIG + _READY_OVERRIDE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    for name in INCOME_FILENAMES:
        (out / name).write_text("STALE", encoding="utf-8")
    code = main(
        [
            "netbenefits-income",
            "--itr-form",
            "ITR-2",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output-dir",
            str(out),
            "--financial-year",
            "2025-26",
        ]
    )
    assert code == 4
    assert list(out.iterdir()) == []
    err = capsys.readouterr().err
    assert err == f"error: {_missing_event_refusal('dividend', 3, 1, 4)}\n"
    assert_no_markers(err, "99999E104", "2026-03-05", "2026-02-01", "2026-03-01")


# --- Form 67 readiness (#601) ---------------------------------------------------

_ZERO_OVERRIDE = """
[form67]
[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "0"
"""


def test_blank_form67_row_withholds_official_csv_and_writes_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _report, out = _run(tmp_path, monkeypatch)
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67-debug.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]


def test_cli_blank_form67_row_is_manual_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _cli_income(tmp_path, monkeypatch) == 3


@pytest.mark.parametrize(("tin_country", "manual"), [("GB", True), ("US", False)])
def test_cli_blank_relief_tin_is_manual_fill_with_files_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tin_country: str,
    manual: bool,
) -> None:
    config, statements = _marked_ready_run(tin_country)
    assert _cli_income(tmp_path, monkeypatch, config=config, by_name=statements) == (
        3 if manual else 0
    )
    (root,) = tmp_path.iterdir()
    assert sorted(path.name for path in (root / "out").iterdir()) == sorted(INCOME_FILING_FILENAMES)
    captured = capsys.readouterr()
    warning = f"warning: {BLANK_RELIEF_TIN_WARNING}"
    assert captured.err.splitlines().count(warning) == int(manual)
    assert_no_markers(captured.out + captured.err, _TIN_MARKER, _ACCOUNT_MARKER)


_INSTITUTION_MARKER = "999900002"


def test_cli_missing_withholding_agent_is_manual_fill_with_files_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1327: the run writes every filing file, prints one fixed notice and
    exits 3; neither the institution name nor the TIN reaches the console."""
    config, statements = _marked_ready_run("US")
    config = _without_withholding_agent(config).replace(
        'institution_name = "Fidelity Stock Plan Services LLC"',
        f'institution_name = "Example Custodian {_INSTITUTION_MARKER} LLC"',
    )
    assert _INSTITUTION_MARKER in config
    assert _cli_income(tmp_path, monkeypatch, config=config, by_name=statements) == 3
    (root,) = tmp_path.iterdir()
    assert sorted(path.name for path in (root / "out").iterdir()) == sorted(INCOME_FILING_FILENAMES)
    captured = capsys.readouterr()
    assert captured.err.splitlines().count(f"warning: {_BLANK_DEDUCTOR}") == 1
    assert_no_markers(
        captured.out + captured.err, _TIN_MARKER, _ACCOUNT_MARKER, _INSTITUTION_MARKER
    )


@pytest.mark.parametrize("agent", [True, False], ids=["agent", "no-agent"])
def test_zero_credit_override_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: bool
) -> None:
    def refuse(*_args: object) -> None:
        raise AssertionError("a normal output write was attempted")

    monkeypatch.setattr(OutputBinding, "writer", refuse)
    config = CONFIG if agent else _without_withholding_agent(CONFIG)
    with pytest.raises(ConfigError, match=r"US/DIVIDEND: the computed credit rounds to ₹0"):
        _run(tmp_path, monkeypatch, config=config + _ZERO_OVERRIDE)
    assert list((tmp_path / "out").iterdir()) == []
