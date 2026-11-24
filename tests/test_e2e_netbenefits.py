"""End-to-end regression snapshot for the Fidelity NetBenefits ESPP flow.

Unlike the per-stage netbenefits pipeline tests (which monkeypatch
``parse_statement`` with synthetic ``StatementData``), this exercises the REAL
CSV + PDF parsers on committed synthetic fixtures (``tests/fixtures/netbenefits/``)
and the real SBI TTBR FX archive — only the ``pdftotext`` shell-out is stubbed.
It covers the calendar-year FA (A3 + A2) and the financial-year OS + FTC outputs
for the AY 2026-27 fact pattern, asserting the CY/FY split:

* CY2025 Schedule FA = ONLY the Dec-31-2025 lot; no dividend.
* FY2025-26 Schedule OS = the $1.46 dividend; Form 67 FTC preserves both the
  $0.36 variant and the fixture's cent-rounded $0.37 treaty-rate withholding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.compute.netbenefits_fa_a2 as a2_mod
import ibkr_fa.pipeline_netbenefits_fa as pipeline_mod
from ibkr_fa.cli import main
from ibkr_fa.compute.netbenefits_fa_a2 import NetBenefitsA2Result
from ibkr_fa.output.cbdt_json_fragment import CbdtJsonUnreadableError
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import QUICKO_TEMPLATE_FILENAME
from ibkr_fa.pipeline_netbenefits_fa import _preclear_cbdt_json_fragment, run_netbenefits_fa
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from tests import _netbenefits_statement_pdf as statements
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING
from tests._netbenefits import DATED_LINE_UNREAD
from tests._netbenefits_e2e import (
    NB_E2E_CBDT_FRAGMENT,
    NB_E2E_PDF_TEXT,
    NB_E2E_TOML,
    NB_E2E_TOML_WITH_PEAK,
    SESSION_REFUSALS,
    csv_file_rows,
    nb_e2e_bundle,
    nb_e2e_toml_with_peak,
    session_refusal_bundle,
)
from tests._netbenefits_fa import A3_ZIP_LINE, QUICKO_ZIP_LINE, write_quicko_template

_FIXTURES = Path(__file__).parent / "fixtures" / "netbenefits"


def _fsi_fields(path: Path) -> dict[str, str]:
    """Read the vertical Schedule FSI filing CSV as a label -> value map."""
    return {label: value for label, value in csv_file_rows(path) if label}


def test_e2e_netbenefits_fa_cy2025(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    a3 = tmp_path / "fa.csv"
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(a3),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    assert rc == 3  # manual-fill: statements do not prove the A2 account peak
    a2 = a3.with_name(f"{a3.stem}-a2{a3.suffix}")
    a3_rows = csv_file_rows(a3)
    a2_rows = csv_file_rows(a2)
    assert len(a3_rows) == 2  # header + EXACTLY the Dec-31 lot (the Mar-2026 lot is CY2026)
    assert len(a2_rows) == 2  # header + the single custodial-account row
    # CY2025 Schedule FA A3 = ONLY the Dec-31-2025 lot (the Mar-2026 lot is CY2026);
    # both income columns 0; closing = securities (no cash). FMV $743.30 is the
    # §49(2AA) cost basis; Dec-31 close from the config price override ($213.47).
    # Initial: 743.30 x 89.47 (Dec-31 TTBR) = 66503.0510 -> 66503; closing and peak:
    # 3.482 x 213.47 = 743.30254 x 89.47 = 66503.2782538 -> 66503.
    assert a3_rows[1] == [
        "1",
        "2",
        "EXAMPLE CORP",
        "1 Sample Issuer Street Sample City XX",
        "00000",
        "Listed company shares",
        "2025-12-31",
        "66503",
        "66503",
        "66503",
        "0",
        "0",
    ]
    # A2 closing == A3 closing (securities + proven-$0 cash); peak remains manual.
    # The downloaded template has a stray trailing comma, but the live uploader
    # accepts exactly 12 fields / 11 commas.
    assert a2_rows[1] == [
        "UNITED STATES OF AMERICA",
        "2",
        "Fidelity Stock Plan Services LLC",
        "245 Summer Street Boston MA",
        "02210",
        "I99999999",
        "BENEFICIAL_OWNER",
        "2025-11-03",
        "",
        "66503",
        "N",
        "0",
    ]


def test_e2e_netbenefits_csv_ignores_configured_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    out = tmp_path / "fa.csv"
    a2 = tmp_path / "fa-a2.csv"
    args = [
        "netbenefits-fa",
        "--input",
        str(bundle),
        "--config",
        str(cfg),
        "--output",
        str(out),
        "--calendar-year",
        "2025",
        "--no-fetch-prices",
    ]
    assert main(args) == 3
    before = (out.read_bytes(), a2.read_bytes())
    summary = capsys.readouterr()
    assert "Peak Balance During the Period" in summary.out

    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    assert main(args) == 3
    assert (out.read_bytes(), a2.read_bytes()) == before
    assert csv_file_rows(a2)[1][8] == ""
    assert capsys.readouterr() == summary


def test_e2e_netbenefits_fa_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The complete CLI text when the A2 peak is manual-fill, with the tmp path
    replaced by ``<tmp>`` as in ``tests/test_cli_run_all_output.py`` (#759)."""
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / "fa.csv"),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 3
    expected = (_FIXTURES / "fa-cy2025.stdout.txt").read_text(encoding="utf-8")
    assert captured.out.replace(str(tmp_path), "<tmp>") == expected
    assert captured.err == ""


_A3_END = "NOT the USD 668.96 you paid.\n"
_QUICKO_END = "schedule-fa-quicko.xlsx.\n"
_NETBENEFITS_FA = ("--output", "out/schedule-fa-a3.csv", "--calendar-year", "2025")


@pytest.mark.parametrize(
    ("argv", "quicko"),
    [
        pytest.param(("netbenefits-fa", *_NETBENEFITS_FA), False, id="filing-csv"),
        pytest.param(
            ("netbenefits-fa", *_NETBENEFITS_FA, "--format", "review-csv"), False, id="review-csv"
        ),
        pytest.param(("netbenefits-fa", *_NETBENEFITS_FA, "--quicko-workbook"), True, id="quicko"),
        pytest.param(
            (
                "netbenefits-all",
                *("--output-dir", "out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
            ),
            False,
            id="all",
        ),
    ],
)
def test_e2e_netbenefits_fa_names_a_blank_a3_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    *,
    quicko: bool,
) -> None:
    """#1330: without entity_zip the run still exits 3 and its stdout is the run
    with entity_zip plus the A3 ZIP line, and the Quicko line when the workbook
    is written; nothing else changes on stdout or stderr."""
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    if quicko:
        write_quicko_template(bundle / QUICKO_TEMPLATE_FILENAME)
    out = tmp_path / "out"
    out.mkdir()
    args = [str(tmp_path / arg) if arg.startswith("out") else arg for arg in argv]
    command = [*args[:1], "--input", str(bundle), "--config", str(cfg), *args[1:]]
    command.append("--no-fetch-prices")
    assert main(command) == 3
    with_zip = capsys.readouterr()
    assert with_zip.out.count(_A3_END) == 1
    assert "ZIP Code" not in with_zip.out

    assert 'entity_zip = "00000"\n' in NB_E2E_TOML
    cfg.write_text(NB_E2E_TOML.replace('entity_zip = "00000"\n', ""), encoding="utf-8")
    assert main(command) == 3
    captured = capsys.readouterr()

    expected = with_zip.out.replace(_A3_END, f"{_A3_END}{A3_ZIP_LINE}\n")
    if quicko:
        assert expected.count(_QUICKO_END) == 1
        expected = expected.replace(_QUICKO_END, f"{_QUICKO_END}{QUICKO_ZIP_LINE}\n")
    assert captured.out == expected
    assert captured.err == with_zip.err
    header, row = csv_file_rows(out / "schedule-fa-a3.csv")
    assert header.count("ZIP Code") == 1
    assert row[header.index("ZIP Code")] == ""


def test_e2e_netbenefits_income_fy2025_26(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch, withholding_usd="0.36")
    out = tmp_path / "out"
    out.mkdir()
    rc = main(
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
    # #601: the blank Form 67 row withholds the official CSV (manual fill, exit 3).
    assert rc == 3
    assert sorted(path.name for path in out.iterdir()) == [
        "form-67-debug.csv",
        "schedule-fsi.csv",
        "schedule-os.csv",
        "schedule-tr.csv",
    ]

    # The whole Schedule OS worksheet, pinned at 07a20f7 before #614 moved its
    # period split and 1e totals into compute, and repinned in #1033 after a
    # cell-by-cell check of the new dividend figures.
    assert hashlib.sha256((out / "schedule-os.csv").read_bytes()).hexdigest() == (
        "914089ab50f1f39c7614fb90eed9a76126e87c418e606288679eec0c4fef8e76"
    )
    # FY2025-26 Schedule OS filing worksheet: the $1.46 dividend (5 March) is
    # 1.46 x 90.56 (the 2026-02-27 TTBR for its preceding month-end) = ₹132 in
    # Quicko period 4 / ITR section 10's 16-Dec-to-15-Mar column.
    os_rows = csv_file_rows(out / "schedule-os.csv")
    period_row = next(row for row in os_rows if row[0] == "3a.")
    assert period_row == [
        "3a.",
        "Dividend Income referred in Sl.no. 1a(i)",
        "0",
        "0",
        "0",
        "132",
        "0",
    ]
    # Foreign dividends belong on Quicko's foreign screen, which carries the
    # country that drives Schedule FSI; the domestic screen must stay absent.
    assert next(row for row in os_rows if row[0] == "Country") == [
        "Country",
        "United States of America",
        "",
        "",
        "",
        "",
        "",
    ]
    assert next(row for row in os_rows if row[0] == "Description") == [
        "Description",
        "XESP - Fidelity NetBenefits stock plan",
        "",
        "",
        "",
        "",
        "",
    ]
    assert next(row for row in os_rows if row[0].startswith("Period 4:")) == [
        "Period 4: 16/12/2025 to 15/03/2026",
        "132",
        "",
        "",
        "",
        "",
        "",
    ]
    assert not any(row[0] == "Quicko / Add Dividend Income (Indian-source only)" for row in os_rows)
    assert os_rows[2][1] == str(sum(int(amount) for amount in period_row[2:])) == "132"
    assert all("_audit" not in cell for row in os_rows for cell in row)
    # Schedule FSI: broker-derived income/tax populate; Indian tax and relief stay blank.
    assert _fsi_fields(out / "schedule-fsi.csv") == {
        "Country Code": "2-UNITED STATES OF AMERICA",
        "Taxpayer Identification number": "123456789",
        "Head of Income": "Other Sources",
        "Income from outside India (included in PART B-TI)": "132",
        # USD 0.36 withholding x 90.56 = 32.6016 -> 33.
        "Tax paid outside India": "33",
        "Tax payable on such income under normal provisions in India": "",
        "Manual fill guidance": MISSING_INDIAN_TAX_WARNING,
        "Relevant article of DTAA if relief claimed u/s 90 or 90A": "",
    }
    # Schedule TR: foreign tax is known, but relief and section need Indian-tax input.
    assert csv_file_rows(out / "schedule-tr.csv")[1] == [
        "2-UNITED STATES OF AMERICA",
        "123456789",
        "33",
        "",
        "",
    ]
    tr_rows = csv_file_rows(out / "schedule-tr.csv")
    assert len(tr_rows) == 2
    # The Form 67 review CSV retains DTAA metadata but leaves Indian tax and FTC blank.
    header, form67 = csv_file_rows(out / "form-67-debug.csv")[:2]
    review = dict(zip(header, form67, strict=True))
    assert review["Tax payable on such income under normal provisions in India"] == ""
    assert review["Total foreign tax credit claimed"] == ""


def test_e2e_netbenefits_cent_rounded_treaty_withholding_reconciles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
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

    assert rc == 3
    assert {
        "schedule-os.csv",
        "schedule-fsi.csv",
        "schedule-tr.csv",
        "form-67-debug.csv",
    } == {path.name for path in out.iterdir()}
    header, form67 = csv_file_rows(out / "form-67-debug.csv")[:2]
    review = dict(zip(header, form67, strict=True))
    assert review["Tax payable on such income under normal provisions in India"] == ""
    assert review["Total foreign tax credit claimed"] == ""


# Every command that parses the statements, with its output arguments and outputs.
_EVERY_COMMAND = pytest.mark.parametrize(
    ("command", "output_args", "stale"),
    [
        pytest.param(
            "netbenefits-income",
            ("--output-dir", "out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
            ("out/schedule-os.csv", "out/schedule-tr.csv"),
            id="income",
        ),
        pytest.param(
            "netbenefits-fa",
            ("--output", "out/fa.csv", "--calendar-year", "2025", "--no-fetch-prices"),
            ("out/fa.csv", "out/fa-a2.csv"),
            id="fa",
        ),
        pytest.param(
            "netbenefits-all",
            (
                "--output-dir",
                "out",
                "--financial-year",
                "2025-26",
                "--itr-form",
                "ITR-2",
                "--no-fetch-prices",
            ),
            ("out/schedule-fa-a3.csv", "out/schedule-os.csv", "out/schedule-cg.csv"),
            id="all",
        ),
        # #666: netbenefits-cg parses the statements too, after the bundle.
        pytest.param(
            "netbenefits-cg",
            ("--output-dir", "out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
            ("out/schedule-cg.csv", "out/schedule-cg-ltcg.csv"),
            id="cg",
        ),
    ],
)


@_EVERY_COMMAND
def test_e2e_netbenefits_refuses_summary_income_without_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    output_args: tuple[str, ...],
    stale: tuple[str, ...],
) -> None:
    """#903: a statement whose summaries show income its detail sections don't list
    refuses every caller with exit 4, and the existing preclear leaves no output.
    #1041: the refusal names the summary line, not its amount."""
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch, drop_income_detail=True)
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        (tmp_path / name).write_text("stale\n", encoding="utf-8")
    args = [str(tmp_path / arg) if arg.startswith("out") else arg for arg in output_args]
    rc = main([command, "--input", str(bundle), "--config", str(cfg), *args])
    assert rc == 4
    err = capsys.readouterr().err
    prefix = "netbenefits-all: " if command == "netbenefits-all" else ""
    assert err == (
        f"error: {prefix}ParseError in section 'statement statement-feb-mar.pdf' at row 0, "
        "column 'Income Summary: Total': the 'Income Summary' Total shows a nonzero amount "
        "this period, but the statement has no 'Dividends, Interest & Other Income' section; "
        "the whole document was checked\n"
    )
    assert "1.46" not in err
    assert list(out.iterdir()) == []


# #1249: a year-end report printing income rows. Its 2026 rows are the Feb-Mar
# report's: in the income FY and after the FA year. Its 2025 rows are no Stock Plan
# Services report's; their name, day and amounts are privacy markers.
_YEAR_END = "statement-year-end.pdf"
_UNLISTED_2025_INCOME = (
    statements.INCOME_ACTIVITY.replace("03/05", "12/19")
    .replace("EXAMPLE CORP", "SYNTHQYEDIV")
    .replace("1.46", "9.87")
    .replace("0.37", "2.47")
    .replace("1.090", "7.400")
    .replace("1.09", "7.40")
)


def _year_end(year: int, income: str = "") -> str:
    """The unpriced ``year`` year-end report, printing ``income``."""
    return statements.statement_text(
        f"{year} YEAR-END INVESTMENT REPORT",
        f"January 1, {year} - December 31, {year}",
        body=statements.UNPRICED_BODY + income,
    )


def _run_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    texts: tuple[str, str],
) -> list[tuple[int, str, str, dict[str, bytes]]]:
    """Run ``argv`` (its ``out`` arguments under ``tmp_path``) over the e2e bundle
    with each of ``texts`` as the year-end report; return each run's exit code,
    stdout, stderr and output bytes."""
    monkeypatch.setitem(NB_E2E_PDF_TEXT, _YEAR_END, texts[0])
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    args = [str(tmp_path / arg) if arg.startswith("out") else arg for arg in argv]
    runs = []
    for text in texts:
        monkeypatch.setitem(NB_E2E_PDF_TEXT, _YEAR_END, text)
        rc = main([*args[:1], "--input", str(bundle), "--config", str(cfg), *args[1:]])
        captured = capsys.readouterr()
        outputs = {path.name: path.read_bytes() for path in sorted(out.iterdir())}
        runs.append((rc, captured.out, captured.err, outputs))
    return runs


@_EVERY_COMMAND
def test_e2e_netbenefits_year_end_income_the_reports_list_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    output_args: tuple[str, ...],
    stale: tuple[str, ...],
) -> None:
    """#1249: a year-end report whose income rows the Feb-Mar report lists gives the
    exit code, console output and output bytes of a silent year-end report."""
    silent, listed = _run_twice(
        tmp_path,
        monkeypatch,
        capsys,
        (command, *output_args),
        (_year_end(2026), _year_end(2026, statements.INCOME_ACTIVITY)),
    )
    assert silent[3]
    assert listed == silent


def test_e2e_netbenefits_cg_ignores_year_end_income(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1249: income no Stock Plan Services report lists, on a year-end report,
    changes nothing in ``netbenefits-cg``."""
    silent, unlisted = _run_twice(
        tmp_path,
        monkeypatch,
        capsys,
        (
            "netbenefits-cg",
            *("--output-dir", "out", "--financial-year", "2025-26", "--itr-form", "ITR-2"),
        ),
        (_year_end(2025), _year_end(2025, _UNLISTED_2025_INCOME)),
    )
    assert silent[0] == 0
    assert silent[3]
    assert unlisted == silent


# A sale behind a one-letter footnote marker; the name and identifier are privacy
# markers.
_MARKED_SALE = "t 03/20 SYNTHETIC HOLDER TWO 99997Q104 You Sold -1.000 $400.00000 - $400.00\n"


@_EVERY_COMMAND
def test_e2e_netbenefits_refuses_a_dated_line_behind_a_footnote_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    output_args: tuple[str, ...],
    stale: tuple[str, ...],
) -> None:
    """#1149: a dated line the parser doesn't read refuses whatever precedes its date,
    so every caller exits 4 and the existing preclear leaves no output."""
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch, feb_mar_extra=_MARKED_SALE)
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        (tmp_path / name).write_text("stale\n", encoding="utf-8")
    args = [str(tmp_path / arg) if arg.startswith("out") else arg for arg in output_args]
    rc = main([command, "--input", str(bundle), "--config", str(cfg), *args])
    assert rc == 4
    err = capsys.readouterr().err
    prefix = "netbenefits-all: " if command == "netbenefits-all" else ""
    assert err == (
        f"error: {prefix}ParseError in section 'statement statement-feb-mar.pdf' at row 0: "
        f"{DATED_LINE_UNREAD}\n"
    )
    for marker in ("Synthetic Holder Two", "99997Q104", "03/20", "400.00", "1.000"):
        assert marker.casefold() in _MARKED_SALE.casefold()
        assert marker.casefold() not in err.casefold()
    assert list(out.iterdir()) == []


def test_e2e_netbenefits_rejects_removed_threshold_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML + '\n[form67]\ntax_regime = "new"\n', encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    assert (
        main(
            [
                "netbenefits-income",
                "--itr-form",
                "ITR-3",
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
        == 4
    )
    error = capsys.readouterr().err
    assert error == (
        "error: ConfigError at field 'form67.tax_regime': is no longer supported; "
        "delete it from [form67]. Form 67 no longer derives Indian tax payable from a rate, "
        "regime or threshold, so rows without an exact [[form67.tax_payable_overrides]] "
        "entry leave the Indian-tax and FTC columns blank. Put exact source-level amounts "
        "in [[form67.tax_payable_overrides]]; non-DTAA (section 91) claims also need the "
        "[form67.section_91] inputs\n"
    )


def test_e2e_netbenefits_fa_cbdt_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")  # add the A2 peak override
    out = tmp_path / "fa.json"
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-3",
        ]
    )
    assert rc == 0
    fragment = json.loads(out.read_text(encoding="utf-8"))
    assert fragment["_ibkrFa"]["format"] == "cbdt-json-fragment"
    assert fragment["_ibkrFa"]["itrForm"] == "ITR-3"
    a2 = fragment["ScheduleFA"]["DtlsForeignCustodialAcc"][0]
    # The config peak fills A2 PeakBalanceDuringPeriod; closing == A3 closing.
    assert (a2["PeakBalanceDuringPeriod"], a2["ClosingBalance"]) == (66503, 66503)
    assert a2["AccountNumber"] == "I99999999"
    a3 = fragment["ScheduleFA"]["DtlsForeignEquityDebtInterest"]
    assert len(a3) == 1  # only the Dec-31-2025 lot
    assert a3[0]["ClosingBalance"] == 66503
    # The whole document, byte for byte (#619 characterization).
    assert out.read_text(encoding="utf-8") == (
        json.dumps(NB_E2E_CBDT_FRAGMENT, ensure_ascii=False, indent=2) + "\n"
    )
    # No A2/A3 sibling CSVs in JSON mode.
    assert not (tmp_path / "fa-a2.json").exists()


def test_netbenefits_report_peak_matches_the_attested_filed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    original = a2_mod.build_netbenefits_a2_row

    def distinct_row_peak(**kwargs):
        result = original(**kwargs)
        assert result.row.peak_balance_inr == Decimal("66503")
        assert result.row.audit_peak_override_source == (
            "[[netbenefits.a2_peak_overrides]] calendar_year = 2025"
        )
        assert result.row.audit_peak_override_reason == (
            "user-supplied: the monthly statements have no daily account value "
            "to compute the peak from"
        )
        return NetBenefitsA2Result(row=replace(result.row, peak_balance_inr=Decimal("12344.50")))

    monkeypatch.setattr(a2_mod, "build_netbenefits_a2_row", distinct_row_peak)
    out = tmp_path / "fa.json"
    report = run_netbenefits_fa(
        input_dir=bundle,
        config_path=cfg,
        output_path=out,
        calendar_year=2025,
        fetch_prices=False,
        format_id="cbdt-json-fragment",
        itr_form="ITR-2",
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert (
        report.cbdt_json_a2_peak_balance_inr
        == (doc["ScheduleFA"]["DtlsForeignCustodialAcc"][0]["PeakBalanceDuringPeriod"])
        == 12345
    )


def test_netbenefits_json_requires_itr_form(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(tmp_path / "unused"),
            "--config",
            str(tmp_path / "unused.toml"),
            "--output",
            str(tmp_path / "unused.json"),
            "--calendar-year",
            "2025",
            "--format",
            "cbdt-json-fragment",
        ]
    )
    assert rc == 2
    assert "--itr-form" in capsys.readouterr().err


def test_e2e_netbenefits_fa_cbdt_json_refuses_mismatched_a2_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    original = a2_mod.build_netbenefits_a2_row

    def mismatched(**kwargs):
        result = original(**kwargs)
        return NetBenefitsA2Result(row=replace(result.row, account_number="WRONG-ID"))

    monkeypatch.setattr(a2_mod, "build_netbenefits_a2_row", mismatched)
    out = tmp_path / "fa.json"
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
        ]
    )
    # #773: an unverified account binding is a structural refusal, not manual fill.
    assert rc == 4
    assert not out.exists()


@pytest.mark.parametrize(
    "output_args",
    [
        ("fa.csv",),
        ("fa.json", "--format", "cbdt-json-fragment", "--itr-form", "ITR-2"),
    ],
    ids=["csv", "cbdt-json"],
)
def test_e2e_netbenefits_fa_refuses_a3_closing_that_differs_from_a2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_args: tuple[str, ...],
) -> None:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    original = pipeline_mod.build_fa_a3_rows

    def one_closing_off(*args, **kwargs):
        first, *rest = original(*args, **kwargs)
        return [
            replace(first, closing_value_inr=first.closing_value_inr - Decimal("0.0001")),
            *rest,
        ]

    monkeypatch.setattr(pipeline_mod, "build_fa_a3_rows", one_closing_off)
    output, *format_args = output_args
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(tmp_path / output),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            *format_args,
        ]
    )
    assert rc == 4
    assert "Σ A3 closing INR differs from securities USD x Dec-31 TTBR" in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle", "user.toml"]


def test_e2e_netbenefits_fa_cbdt_json_refuses_without_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Default config has no [[netbenefits.a2_peak_overrides]] → the JSON can't be
    # completed; refuse (exit 3) with a NetBenefits-correct remedy, leave no file.
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    out = tmp_path / "fa.json"
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == 3
    assert "[[netbenefits.a2_peak_overrides]]" in capsys.readouterr().err
    assert not out.exists()


def _run_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str) -> tuple[int, Path]:
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(toml, encoding="utf-8")
    out = tmp_path / "fa.json"
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
        ]
    )
    return rc, out


def test_e2e_netbenefits_cbdt_json_refuses_missing_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ZIPs are optional in [netbenefits] (the review CSV allows them blank) but
    # CBDT-required for the JSON — refuse with NetBenefits-correct remedy text.
    assert 'entity_zip = "00000"\n' in NB_E2E_TOML_WITH_PEAK
    toml = NB_E2E_TOML_WITH_PEAK.replace('zip = "02210"\n', "").replace(
        'entity_zip = "00000"\n', ""
    )
    rc, out = _run_json(tmp_path, monkeypatch, toml)
    assert rc == 3
    err = capsys.readouterr().err
    assert "[netbenefits.account].zip" in err
    assert "[netbenefits.security].entity_zip" in err
    assert not out.exists()


def test_e2e_netbenefits_cbdt_json_does_not_compare_peak_and_closing_inr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Peak and closing use their own date's TTBR, so their INR values need not order.
    rc, out = _run_json(tmp_path, monkeypatch, nb_e2e_toml_with_peak(peak_balance_inr=1))
    assert rc == 0
    assert out.exists()


@pytest.mark.parametrize(
    ("existing", "message", "link_target"),
    [
        ('{"ScheduleFA": {"hand-merged": true}}', "refusing to overwrite", None),
        ("not an ibkr-fa fragment", "could not inspect", None),
        ("", "could not check", "fa.json"),
        ("", "could not inspect", "missing.json"),
    ],
    ids=["foreign", "unreadable", "looping-link", "dangling-link"],
)
def test_e2e_netbenefits_cbdt_json_refuses_foreign_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing: str,
    message: str,
    link_target: str | None,
) -> None:
    # --output points at a file ibkr-fa didn't author, or one it can't inspect →
    # refuse (exit 4), preserve it.
    bundle, cfg = nb_e2e_bundle(tmp_path, monkeypatch)
    cfg.write_text(NB_E2E_TOML_WITH_PEAK, encoding="utf-8")
    out = tmp_path / "fa.json"
    if link_target is None:
        out.write_text(existing, encoding="utf-8")
    else:
        out.symlink_to(link_target)
    rc = main(
        [
            "netbenefits-fa",
            "--input",
            str(bundle),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
            "--no-fetch-prices",
            "--format",
            "cbdt-json-fragment",
            "--itr-form",
            "ITR-2",
        ]
    )
    assert rc == 4
    assert message in capsys.readouterr().err
    if link_target is None:
        assert out.read_text(encoding="utf-8") == existing
    else:
        assert out.readlink() == Path(link_target)


@pytest.mark.parametrize("link_target", ["fa.json", "missing.json"])
def test_netbenefits_json_preclear_inspects_symlinks(tmp_path: Path, link_target: str) -> None:
    path = tmp_path / "fa.json"
    path.symlink_to(link_target)
    with pytest.raises(CbdtJsonUnreadableError, match="could not inspect"):
        _preclear_cbdt_json_fragment(path)
    assert path.readlink() == Path(link_target)


# --- the Dec-31 close's required US market session (#1165) ---------------------


@pytest.mark.parametrize(
    ("output", "format_args", "stale"),
    [
        pytest.param(
            "schedule-fa-a3.csv",
            ("--quicko-workbook",),
            (
                "schedule-fa-a3.csv",
                "schedule-fa-a2.csv",
                "schedule-fa-a3-a2.csv",
                "schedule-fa-quicko.xlsx",
            ),
            id="filing-csv",
        ),
        pytest.param(
            "schedule-fa.json",
            ("--format", "cbdt-json-fragment", "--itr-form", "ITR-2"),
            ("schedule-fa.json",),
            id="cbdt-json",
        ),
    ],
)
@SESSION_REFUSALS
def test_e2e_netbenefits_fa_refuses_a_dec31_close_off_the_required_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
    fetcher: DailyCloseFetcher | None,
    refusal: str,
    markers: tuple[str, ...],
    output: str,
    format_args: tuple[str, ...],
    stale: tuple[str, ...],
) -> None:
    """#1165: exit 4 with the refusal alone on stderr and no report, and an
    earlier run's filing output is gone."""
    bundle, cfg, price_args = session_refusal_bundle(tmp_path, monkeypatch, override, fetcher)
    out = tmp_path / "out"
    out.mkdir()
    for name in stale:
        prior = json.dumps(NB_E2E_CBDT_FRAGMENT) if name.endswith(".json") else "stale\n"
        (out / name).write_text(prior, encoding="utf-8")
    rc = main(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(cfg), "--output", str(out / output)),
            *("--calendar-year", "2025", *price_args, *format_args),
        ]
    )
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {refusal}\n"
    assert captured.out == ""
    for marker in markers:
        assert marker not in captured.err
    assert list(out.iterdir()) == []
