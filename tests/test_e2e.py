"""End-to-end snapshot test for the v1 pipeline.

Runs the CLI's ``schedule-fa`` subcommand against a synthetic fixture
(``tests/fixtures/e2e_year_end_2025.csv``) that includes all three IBKR Flex
sections we care about — Account Information, Open Positions (LOT),
and Trades-with-LOT-detail containing a CLOSED_LOT row. Verifies:

* The CLI exits 3: Address of entity, ZIP Code, Nature of entity, peak (blank
  under the suite's offline price fetcher, ``tests/conftest.py``), gross amount
  paid/credited and gross proceeds stay manual-fill, and without
  ``[account.custodian]`` no Table A2 is written.
* Every cell in the generated review CSV matches a golden snapshot.
* The stdout summary names the row count and masked account ID, and lists
  manual-fill portal columns including peak, gross amount paid/credited (the Flex
  Query path reads no dividends) and gross proceeds (Flex closed lots don't
  prove pre-fee consideration).

The fixture's dates are pinned to 2024-2025 so they fall within the
vendored SBI TTBR archive.
If the archive is ever refreshed in a way that changes published
rates for these specific dates, this test is what catches it — the
golden snapshot is sensitive to FX rate values at acquisition date
and Dec 31.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._fa_a3_address import FLEX_YEAR_END_CONFIG as _USER_TOML
from tests._fa_a3_name_runs import IBKR_FLEX_STATEMENT as _FIXTURE


@pytest.fixture
def setup_e2e(tmp_path: Path) -> tuple[Path, Path]:
    """Write user.toml in tmp_path and return (config_path, output_path)."""
    cfg = tmp_path / "user.toml"
    cfg.write_text(_USER_TOML, encoding="utf-8")
    out = tmp_path / "review.csv"
    return cfg, out


def test_e2e_cli_against_flex_fixture(
    setup_e2e: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: CLI consumes the Flex fixture, produces a review
    CSV whose every cell matches a snapshot of the expected portal +
    audit values, exits 3, and prints the manual-fill warning block."""
    cfg, out = setup_e2e

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )

    # Exit 3: Address of entity, ZIP Code, Nature of entity, peak (blank under
    # conftest.py's offline price fetcher), gross amount paid and gross proceeds
    # stay manual-fill, and without [account.custodian] no Table A2 is written.
    assert rc == 3
    assert out.exists()

    captured = capsys.readouterr()
    # Stdout success summary.
    assert "Wrote 3 Schedule FA Table A3 row(s)" in captured.out
    assert "Account: U***5678" in captured.out
    assert "U12345678" not in captured.out
    # Flex closed lots do not prove pre-fee consideration, so gross
    # proceeds remains manual rather than reusing commission-reduced net cash.
    assert "Peak value of investment during the Period" in captured.out
    assert (
        "Total gross amount paid/credited with respect to the holding during the period"
        in captured.out
    )
    assert "Total gross proceeds from sale or redemption of investment during the period" in (
        captured.out
    )
    assert "XQBD (US99995A1060)" in captured.out
    assert "Exit status: 3" in captured.out
    # #63: BMA disclosure advisory must appear on schedule-fa stdout
    # (FA path routes the advisory through stdout via
    # _render_run_report, not via stderr like OS/CG/CG-LTCG do). T4-r1
    # caught the missing stdout-positive assertion.
    assert "Black Money Act disclosure reminder" in captured.out
    # Stderr must be empty on the success-with-warnings path.
    assert captured.err == ""

    # --- Snapshot the CSV row-by-row -------------------------------------
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    # Header: 11 portal columns + 9 audit columns = 20.
    assert len(rows[0]) == 20

    # 3 data rows: 2 open lots (XQBB, XQBE — input order) then 1
    # closed lot (XQBD).
    assert len(rows) == 4  # header + 3 data rows

    # Row 1: XQBB open lot, opened 2024-06-15.
    # initial = 852 * 83.15 = 70843.80 -> 70844 rupees
    # closing = 10 * 95.4 * 89.47 = 85354.38 -> 85354 rupees
    # The actual rates come from the vendored TTBR archive; if those
    # rates change for these dates, this assertion catches it.
    assert rows[1] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BB INC",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "15/06/2024",
        "70844",  # initial_value_inr quantized
        "",  # peak_value (v1 incomplete)
        "85354",  # closing_value_inr quantized
        "",  # gross_amount_paid (v1 incomplete)
        "0",  # gross_proceeds: open lot -> Decimal("0") -> "0" (#16)
        "US99995B1044",
        "XQBB",
        "US",
        "852",
        "2024-06-15",
        "95.4",
        "83.15",
        "89.47",
        "",  # no disposal date (open lot)
    ]

    # Row 2: XQBE open lot, opened 2025-01-05 (a Sunday: the 2025-01-04
    # rate carries forward).
    # initial = 1005.4 * 85.35 = 85810.89 -> 85811 rupees
    # closing = 4 * 263.4 * 89.47 = 94265.592 -> 94266 rupees
    assert rows[2] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BE CORP",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "05/01/2025",
        "85811",  # initial_value_inr quantized
        "",  # peak_value (v1 incomplete)
        "94266",  # closing_value_inr quantized
        "",  # gross_amount_paid (v1 incomplete)
        "0",  # gross_proceeds: open lot -> Decimal("0") -> "0" (#16)
        "US99995D1000",
        "XQBE",
        "US",
        "1005.4",
        "2025-01-05",
        "263.4",
        "85.35",
        "89.47",
        "",  # no disposal date (open lot)
    ]

    # Row 3: XQBD closed lot, opened 2024-06-15, closed 2025-06-15.
    # initial = 260 * 83.15 = 21619.00 -> 21619
    # closing = 0 (disposed during CY)
    # Flex supplies net cash but no pre-fee consideration, so gross
    # proceeds and its Dec-31 TTBR audit cell are blank.
    # audit_disposal_date is 2025-06-15.
    assert rows[3] == [
        "2-UNITED STATES OF AMERICA",
        "SAMPLE ISSUER BD INC",
        "",  # address: no [[fa.entities]] entry (#1289)
        "",
        "",
        "15/06/2024",
        "21619",
        "",
        "0",  # closing balance for disposed lot is "0", not empty
        "",
        "",  # gross proceeds: manual-fill; net cash is not gross
        "US99995A1060",
        "XQBD",
        "US",
        "260",
        "2024-06-15",
        "",  # no Dec 31 close price (disposed)
        "83.15",
        "",  # no Dec 31 TTBR used by this closed row
        "2025-06-15",  # disposal date
    ]


def test_e2e_cli_refuses_base_currency_with_trailing_line_break(
    setup_e2e: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#835: a ``"USD\\n"`` CurrencyPrimary cell is not a currency code; the
    parser refuses it (#1021)."""
    cfg, out = setup_e2e
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "ClientAccountID,CurrencyPrimary,Name,AccountType,IBEntity\n"
        'U12345678,"USD\n",Synthetic Holder,Individual,IBLLC-US\n'
        "CurrencyPrimary,FXRateToBase,AssetClass,SubCategory,Symbol,Description,CUSIP,"
        "ISIN,ListingExchange,IssuerCountryCode,ReportDate,Quantity,MarkPrice,"
        "PositionValue,CostBasisPrice,CostBasisMoney,Side,LevelOfDetail,OpenDateTime,"
        "HoldingPeriodDateTime\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "schedule-fa",
            "--input",
            str(statement),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )

    assert rc == 4
    assert "ParseError in section 'Account Information' at row 0, column 'CurrencyPrimary'" in (
        capsys.readouterr().err
    )


def test_e2e_fixture_has_all_three_required_sections() -> None:
    """Defence-in-depth: if someone accidentally trims the e2e
    fixture, this is the first test that screams."""
    text = _FIXTURE.read_text(encoding="utf-8")
    assert "ClientAccountID" in text
    assert '"OpenDateTime","HoldingPeriodDateTime"' in text
    assert "CLOSED_LOT" in text
    assert "EXECUTION" in text


def test_e2e_writer_csv_is_well_formed_csv(
    setup_e2e: tuple[Path, Path],
) -> None:
    """Sanity: the written file is parsable as a CSV without errors —
    no quoting bugs, no encoding bugs."""
    cfg, out = setup_e2e
    main(
        [
            "schedule-fa",
            "--input",
            str(_FIXTURE),
            "--config",
            str(cfg),
            "--output",
            str(out),
            "--calendar-year",
            "2025",
        ]
    )
    # Round-trip through DictReader — would raise on malformed quoting.
    with out.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert len(rows) == 3
    # Every data row has a value for every header.
    for row in rows:
        assert set(row.keys()) == set(reader.fieldnames or [])


def test_e2e_cli_refuses_a_copied_lot_of_another_kind(
    setup_e2e: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1093: a copy of the XQBB lot marked as another asset class and level
    of detail was read as a second XQBB lot, so the holding was filed twice.
    It now refuses, and the earlier run's outputs are removed. The copy's
    neighbouring cells carry markers."""
    cfg, out = setup_e2e
    lot = (
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044",'
        '"NASDAQ","US","20251231","10","95.4","954","85.2","852","Long","LOT","20240615",'
        '"20240615"\n'
    )
    copy = lot.replace(
        '"1","STK","COMMON"', '"Synthetic Marker Q","NEW_ASSET","Synthetic Holder Two"'
    ).replace('"LOT"', '"NEW_SUMMARY"')
    text = _FIXTURE.read_text(encoding="utf-8")
    assert text.count(lot) == 1
    statement = tmp_path / "statement.csv"
    statement.write_text(text.replace(lot, lot + copy), encoding="utf-8")
    a2 = out.with_name("review-a2.csv")
    out.write_text("stale A3", encoding="utf-8")
    a2.write_text("stale A2", encoding="utf-8")

    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(cfg), "--output", str(out)),
            *("--calendar-year", "2025"),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "ParseError in section 'Open Positions (LOT)' at row 1, column 'LevelOfDetail': "
        "must be 'LOT': each Open Positions row is read as one lot, so a SUMMARY or other row "
        "would be counted as another holding. Set the Flex Query's Open Positions Level of "
        "Detail to Lot.\n"
    ) in captured.err
    assert "synthetic" not in captured.err.casefold()
    assert not out.exists()
    assert not a2.exists()
