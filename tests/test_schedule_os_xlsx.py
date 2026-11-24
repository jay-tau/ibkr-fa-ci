"""Tests for :mod:`ibkr_fa.output.schedule_os_xlsx` (issue #42 follow-up).

Mirrors the row-shape and per-AY threading tests in
``test_schedule_os_review_csv.py``, plus XLSX-specific concerns:

* numeric / datetime cells stay typed (not stringified)
* header row is bold + frozen
* writer-contract still rejects NUL + formula-injection triggers
* the file is a valid XLSX (round-trips through openpyxl on read)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.models import DistributionKind
from ibkr_fa.output import (
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
)
from ibkr_fa.output.schedule_os_xlsx import ScheduleOsXlsxWriter
from ibkr_fa.types import Isin, Ticker
from tests._schedule_os_totals import total_rows


def make_line(
    *,
    symbol: str = "XQCA",
    isin: str = "US99972B1017",
    issuer_country_iso2: str = "US",
    payment_date: date = date(2025, 6, 12),
    fx_specified_date: date = date(2025, 5, 31),
    fx_rate_publication_date: date | None = None,
    fx_rate: Decimal = Decimal("83.50"),
    amount_gross_usd: Decimal = Decimal("120.4567"),
    amount_gross_inr: Decimal = Decimal("10058.134450"),
    description: str | None = None,
    kind: DistributionKind = DistributionKind.CASH_DIVIDEND,
) -> ScheduleOsDividendLine:
    if description is None:
        description = f"{symbol}({isin}) Cash Dividend USD 0.8300 per Share (Ordinary Dividend)"
    if fx_rate_publication_date is None:
        fx_rate_publication_date = fx_specified_date
    return ScheduleOsDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2=issuer_country_iso2,
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_rate_publication_date,
        fx_rate_inr_per_usd=fx_rate,
        amount_gross_usd=amount_gross_usd,
        amount_gross_inr=amount_gross_inr,
        description=description,
        kind=kind,
    )


def make_domestic_line(
    *,
    symbol: str = "XQCD",
    isin: str = "IN99970W1077",
    payment_date: date = date(2025, 7, 1),
    fx_specified_date: date = date(2025, 6, 30),
    fx_rate_publication_date: date | None = None,
    fx_rate: Decimal = Decimal("83.50"),
    amount_gross_usd: Decimal = Decimal("12.34"),
    amount_gross_inr: Decimal = Decimal("1030.39"),
    description: str | None = None,
) -> ScheduleOsDomesticDividendLine:
    if description is None:
        description = f"{symbol}({isin}) Cash Dividend USD 0.3500 per Share (Ordinary Dividend)"
    if fx_rate_publication_date is None:
        fx_rate_publication_date = fx_specified_date
    return ScheduleOsDomesticDividendLine(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        issuer_country_iso2="IN",
        payment_date=payment_date,
        fx_specified_date=fx_specified_date,
        fx_rate_publication_date=fx_rate_publication_date,
        fx_rate_inr_per_usd=fx_rate,
        amount_gross_usd=amount_gross_usd,
        amount_gross_inr=amount_gross_inr,
        description=description,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def make_result(
    *,
    lines: list[ScheduleOsDividendLine] | None = None,
    domestic_lines: list[ScheduleOsDomesticDividendLine] | None = None,
    other_income_lines: tuple[ScheduleOsOtherIncomeLine, ...] = (),
    interest_total_inr_rounded: Decimal = Decimal("0"),
) -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(
        lines=[] if lines is None else lines,
        domestic_lines=[] if domestic_lines is None else domestic_lines,
        interest_total_inr_rounded=interest_total_inr_rounded,
        other_income_lines=other_income_lines,
    )


def _read_xlsx(path: Path) -> list[list[object]]:
    """Return the worksheet's rows as native Python values."""
    wb = load_workbook(path, data_only=False)
    ws = wb.active
    assert ws is not None
    return [[cell.value for cell in row] for row in ws.iter_rows()]


_LENDING_FEE = ScheduleOsOtherIncomeLine(
    nature="Securities lending fee",
    payment_date=date(2025, 6, 1),
    fx_specified_date=date(2026, 3, 31),
    fx_rate_publication_date=date(2026, 3, 31),
    fx_rate_inr_per_usd=Decimal("80"),
    amount_gross_usd=Decimal("0.11"),
    amount_gross_inr=Decimal("8.80"),
    description="Stock Lending",
    source_row_index=1,
)


def test_xlsx_emits_other_income_audit_rows(tmp_path: Path) -> None:
    result = make_result(other_income_lines=(_LENDING_FEE,))
    output = tmp_path / "schedule-os.xlsx"

    ScheduleOsXlsxWriter().write(result, output)

    rows = _read_xlsx(output)
    assert any(row[0] == "_other_income:" for row in rows)
    assert any(row[0] == "Securities lending fee" and row[5] == 0.11 for row in rows)


def _find_detail_row(data: list[list[object]], symbol: str) -> list[object]:
    """Return the first detail row whose Symbol cell equals ``symbol``."""
    for row in data:
        if row and row[0] == symbol:
            return row
    raise AssertionError(f"no detail row found with Symbol={symbol!r}")


# ---- registry ------------------------------------------------------------


def test_xlsx_format_registered_for_os() -> None:
    """The OS xlsx writer is registered alongside review-csv."""
    assert "xlsx" in available_formats(Subcommand.OS)
    assert output_binding(Subcommand.OS, "xlsx").target is ScheduleOsXlsxWriter


# ---- header row ----------------------------------------------------------


def test_header_row_matches_csv_writer_byte_for_byte(tmp_path: Path) -> None:
    """The XLSX header row labels match the CSV writer's headers
    exactly — a user opening both files side-by-side sees the same
    column captions."""
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(make_result(lines=[make_line()]), out)
    data = _read_xlsx(out)
    assert data[0] == [
        "Symbol",
        "ISIN",
        "Country (ISO 3166-1 alpha-2)",
        "Payment date (DD/MM/YYYY)",
        "Distribution type",
        "_audit: Gross USD",
        "_audit: FX specified date (YYYY-MM-DD)",
        "_audit: SBI TTBR source date (YYYY-MM-DD)",
        "_audit: SBI TTBR (INR/USD)",
        "_audit: Gross INR (full precision)",
        "Gross INR (rounded \u20b9)",
    ]


def test_header_row_is_bold_and_frozen(tmp_path: Path) -> None:
    """First row is bold; the sheet's freeze pane starts at A2."""
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(make_result(lines=[make_line()]), out)
    wb = load_workbook(out, data_only=False)
    ws = wb.active
    assert ws is not None
    for column_index in range(1, 12):  # 11 header cells
        assert ws.cell(row=1, column=column_index).font.bold is True
    assert ws.freeze_panes == "A2"


# ---- detail rows: typed cells -------------------------------------------


def test_payment_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(lines=[make_line(payment_date=date(2025, 6, 12))]),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQCA")[3]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 6
    assert cell.day == 12


def test_fx_specified_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(lines=[make_line(fx_specified_date=date(2025, 5, 31))]),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQCA")[6]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 5
    assert cell.day == 31


def test_decimal_amounts_written_as_numbers(tmp_path: Path) -> None:
    """USD / INR / TTBR cells preserve numeric typing (not strings)
    so the user can sum them with ``=SUM(...)`` in Excel."""
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(
            lines=[
                make_line(
                    fx_rate=Decimal("83.50"),
                    amount_gross_usd=Decimal("120.4567"),
                    amount_gross_inr=Decimal("10058.134450"),
                )
            ]
        ),
        out,
    )
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQCA")
    gross_usd = detail[5]
    ttbr = detail[8]
    gross_inr_full = detail[9]
    gross_inr_rounded = detail[10]
    assert isinstance(gross_usd, float)
    assert gross_usd == pytest.approx(120.4567)
    assert isinstance(ttbr, float)
    assert ttbr == pytest.approx(83.50)
    assert isinstance(gross_inr_full, float)
    assert gross_inr_full == pytest.approx(10058.13445)
    # 120.4567 * 83.50 = 10058.134450; the rounded portal cell is
    # whole-rupee int (HALF_UP)
    assert gross_inr_rounded == 10058


def test_publication_date_blank_when_equal_to_specified_date(tmp_path: Path) -> None:
    """When the publication date matches the Rule-115 specified
    date, the publication cell is blank — same elision the CSV
    writer applies."""
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(
            lines=[
                make_line(
                    fx_specified_date=date(2025, 5, 31),
                    fx_rate_publication_date=date(2025, 5, 31),
                )
            ]
        ),
        out,
    )
    data = _read_xlsx(out)
    assert _find_detail_row(data, "XQCA")[7] is None


def test_publication_date_populated_when_carry_back_resolved(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(
            lines=[
                make_line(
                    fx_specified_date=date(2025, 5, 31),
                    fx_rate_publication_date=date(2025, 5, 30),
                )
            ]
        ),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQCA")[7]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 5
    assert cell.day == 30


def test_distribution_type_renders_line_kind(tmp_path: Path) -> None:
    """The ``Distribution type`` column renders ``line.kind`` and never
    parses the description (#606): a description with no
    ``SYMBOL(ISIN)`` prefix still shows the kind's label."""
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(
            lines=[make_line(description="Special payout", kind=DistributionKind.SPECIAL_DIVIDEND)]
        ),
        out,
    )
    data = _read_xlsx(out)
    assert _find_detail_row(data, "XQCA")[4] == "Special Dividend"


# ---- section structure ---------------------------------------------------


def test_empty_result_emits_one_zero_total(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(make_result(), out)
    data = _read_xlsx(out)
    assert len(data) == 12
    assert data[0][0] == "Symbol"
    _, portal, interest = total_rows(data)
    assert data[portal][10] == 0
    assert data[interest] == [PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, *[None] * 9, 0]


def test_interest_total_row_follows_the_ordinary_portal_total(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(make_result(interest_total_inr_rounded=Decimal("4321")), out)
    data = _read_xlsx(out)
    _, _, interest = total_rows(data)
    assert interest == len(data) - 1
    assert data[interest] == [PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, *[None] * 9, 4321]
    worksheet = load_workbook(out).active
    assert worksheet is not None
    assert worksheet.cell(row=interest + 1, column=11).number_format == "#,##0"


def test_totals_come_before_the_other_income_block(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    result = make_result(
        lines=[make_line()],
        other_income_lines=(_LENDING_FEE,),
        interest_total_inr_rounded=Decimal("4321"),
    )
    ScheduleOsXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    audit, portal, interest = total_rows(data)
    assert [data[portal][10], data[interest][10]] == [10058, 4321]
    assert data[audit][9] == pytest.approx(10058.134450)
    assert data[interest + 2][0] == "_other_income:"
    assert data[interest + 3][0] == "Securities lending fee"


def test_ordinary_portal_total_combines_every_source(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    result = make_result(
        lines=[make_line()],
        domestic_lines=[make_domestic_line()],
    )
    ScheduleOsXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    _, portal, _ = total_rows(data)
    assert data[portal][10] == 11089


def test_detail_rows_are_globally_sorted(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    result = make_result(
        lines=[make_line()],
        domestic_lines=[
            make_domestic_line(payment_date=date(2025, 6, 1), fx_specified_date=date(2025, 5, 31))
        ],
    )
    ScheduleOsXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    symbols = [row[0] for row in data if row[0] in {"XQCA", "XQCD"}]
    assert symbols == ["XQCD", "XQCA"]


def test_quicko_period_rows_keep_native_numeric_values(tmp_path: Path) -> None:
    out = tmp_path / "os.xlsx"
    ScheduleOsXlsxWriter().write(
        make_result(
            lines=[
                make_line(
                    payment_date=date(2026, 3, 12),
                    amount_gross_inr=Decimal("236.6815"),
                )
            ]
        ),
        out,
    )
    data = _read_xlsx(out)
    header_index = next(
        i
        for i, row in enumerate(data)
        if row[0]
        == "Parsed-source contribution to Schedule OS section 10 / Quicko dividend periods"
    )
    assert data[header_index][1:3] == [
        "Quicko Income to add / ITR section 10 row 3a",
        "Quicko Expense to add / ITR section 57(i) aggregate (not computed)",
    ]
    period_rows = data[header_index + 1 : header_index + 6]

    # The 2026-03-12 line falls in the 16 Dec - 15 Mar period; 236.6815
    # rounds half-up to 237.
    assert [row[1] for row in period_rows] == [0, 0, 0, 237, 0]
    assert period_rows[3][9] == pytest.approx(236.6815)
    assert [row[2] for row in period_rows] == [None] * 5


def test_audit_total_full_precision(tmp_path: Path) -> None:
    """The audit-TOTAL row carries the FULL-precision INR sum;
    the portal-TOTAL row that follows carries the HALF_UP whole-
    rupee rounded value."""
    out = tmp_path / "os.xlsx"
    result = make_result(
        lines=[
            make_line(amount_gross_inr=Decimal("1250.246813")),
            make_line(symbol="XQAA", isin="US99990A3095", amount_gross_inr=Decimal("410.753187")),
        ]
    )
    ScheduleOsXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    audit, portal, _ = total_rows(data)
    assert data[audit][9] == pytest.approx(1661.0)  # 1250.246813 + 410.753187
    assert data[portal][10] == 1661


# ---- writer-contract enforcement ----------------------------------------


def test_nul_byte_in_string_cell_raises(tmp_path: Path) -> None:
    """A NUL byte in any string cell aborts the write — XLSX is
    just as vulnerable to binary contamination as CSV."""
    out = tmp_path / "os.xlsx"
    bad_line = make_line(isin="US99972B101\x00")
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleOsXlsxWriter().write(make_result(lines=[bad_line]), out)
    assert excinfo.value.violation == "nul_byte"


@pytest.mark.parametrize(
    "trigger",
    [
        # ASCII Excel formula triggers.
        "=",
        "+",
        "-",
        "@",
        # C0 whitespace triggers (Excel treats a leading C0 whitespace
        # the same as the trigger that follows it).
        "\t",
        "\v",
        "\r",
        "\n",
        # Full-width Unicode homoglyphs (CJK ASCII forms). These are
        # in the OWASP-extended set because Excel will normalise them
        # to ASCII at evaluation time, so a homoglyph-prefixed string
        # is just as dangerous as the ASCII form.
        "\uff1d",  # full-width equals sign
        "\uff0b",  # full-width plus sign
        "\uff0d",  # full-width hyphen-minus
        "\uff20",  # full-width commercial at
    ],
)
def test_formula_trigger_in_string_cell_raises(tmp_path: Path, trigger: str) -> None:
    """Excel auto-executes formula-prefixed cells; the OS writer
    must reject the OWASP-extended trigger set on every string
    cell. Injected via the Symbol cell, which is passed through
    :func:`str` without any whitespace stripping."""
    out = tmp_path / "os.xlsx"
    bad_line = make_line(symbol=f"{trigger}EVIL")
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleOsXlsxWriter().write(make_result(lines=[bad_line]), out)
    assert excinfo.value.violation == "formula_trigger"
    assert excinfo.value.trigger == trigger


# ---- per-AY portal-label threading --------------------------------------


def test_assessment_year_start_threads_per_ay_label(tmp_path: Path) -> None:
    """When ``assessment_year_start`` is passed, the writer resolves
    portal labels from the matching per-AY snapshot. We assert the
    snapshot's 1a(i) and 1b(ix) label STRINGS each start one row —
    this proves the snapshot was actually consulted."""
    from ibkr_fa.output.portal_labels import for_ay  # noqa: PLC0415

    out = tmp_path / "os.xlsx"
    ay_start = 2026
    snapshot = for_ay(ay_start)
    ScheduleOsXlsxWriter(assessment_year_start=ay_start).write(make_result(), out)
    data = _read_xlsx(out)
    _, portal, interest = total_rows(data)
    assert data[portal][0] == snapshot.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND
    assert data[interest][0] == snapshot.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST


# ---- regression: cell_index in NUL errors -------------------------------


def test_nul_byte_cell_index_preserved_across_typed_cells(tmp_path: Path) -> None:
    """Regression: a NUL in a string cell that lives AFTER numeric /
    datetime cells must report its ORIGINAL worksheet column index,
    not a compacted string-only position. Symbol is column 0, ISIN
    is column 1, both string. Payment-date column 3 is a date
    (non-string). The cell_index must reflect the worksheet column,
    not a string-only filtered position."""
    out = tmp_path / "os.xlsx"
    bad_line = make_line(symbol="XQCA\x00")
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleOsXlsxWriter().write(make_result(lines=[bad_line]), out)
    assert excinfo.value.violation == "nul_byte"
    # Symbol is column 0 (first column).
    assert excinfo.value.cell_index == 0
