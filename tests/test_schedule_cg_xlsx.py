"""Tests for :mod:`ibkr_fa.output.schedule_cg_xlsx` (issue #42 follow-up).

Mirrors the row-shape and per-AY threading tests in
``test_schedule_cg_review_csv.py``, plus XLSX-specific concerns:

* numeric / datetime cells stay typed (not stringified)
* signed values (net STCG losses) ride as native numbers
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
from openpyxl.worksheet.worksheet import Worksheet

from ibkr_fa.compute.schedule_cg import (
    DeferredLtcgRow,
    ScheduleCgStcgComputeResult,
)
from ibkr_fa.models import BasisProvenance, ClosedLot
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from ibkr_fa.output import (
    EXCLUDED_CG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_DEFERRED_MARKER,
    EXCLUDED_OUT_OF_FY_MARKER,
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_STCG, for_ay
from ibkr_fa.output.schedule_cg_xlsx import ScheduleCgXlsxWriter, _iter_rows
from ibkr_fa.types import CountryCode, Isin, Ticker


def _make_stcg_line(**overrides: object) -> ScheduleCgStcgLine:
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAQ"),
        "isin": Isin("US99992A1097"),
        "issuer_country_iso2": "US",
        "description": "SAMPLE ISSUER AQ INC",
        "open_date": date(2025, 1, 15),
        "close_date": date(2025, 6, 20),
        "holding_period_days": 156,
        "quantity": Decimal("10"),
        "cost_basis_usd": Decimal("1500"),
        "proceeds_usd": Decimal("1800"),
        "fx_specified_date": date(2025, 5, 31),
        "fx_rate_inr_per_usd": Decimal("86.20"),
        "cost_basis_inr": Decimal("1500") * Decimal("86.20"),
        "cost_basis_inr_source": "test basis",
        "proceeds_inr": Decimal("1800") * Decimal("86.20"),
        "realized_gain_inr": (Decimal("1800") - Decimal("1500")) * Decimal("86.20"),
    }
    defaults.update(overrides)
    defaults.setdefault("fx_rate_publication_date", defaults["fx_specified_date"])
    return ScheduleCgStcgLine(**defaults)  # ty: ignore[invalid-argument-type]


def _make_closed_lot(**overrides: object) -> ClosedLot:
    defaults: dict[str, object] = {
        "currency_primary": "USD",
        "sub_category": "COMMON",
        "symbol": Ticker("XQCA"),
        "description": "SAMPLE ISSUER CA CORP",
        "isin": Isin("US99972B1017"),
        "listing_exchange": "NASDAQ",
        "issuer_country_code": CountryCode("US"),
        "quantity": Decimal("5"),
        "cost_basis_money": Decimal("500"),
        "proceeds_money": Decimal("600"),
        "open_date_time": date(2022, 1, 1),
        "close_date_time": date(2025, 12, 1),
        "basis_provenance": BasisProvenance.ENGINE_FIFO,
    }
    defaults.update(overrides)
    return ClosedLot(**defaults)  # ty: ignore[invalid-argument-type]


def _empty_result() -> ScheduleCgStcgComputeResult:
    return ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


def _result_with(
    *,
    stcg_lines: list[ScheduleCgStcgLine] | None = None,
    excluded_ltcg_deferred: list[DeferredLtcgRow] | None = None,
    excluded_indian_source: list[ClosedLot] | None = None,
    excluded_out_of_fy: list[ClosedLot] | None = None,
) -> ScheduleCgStcgComputeResult:
    return ScheduleCgStcgComputeResult(
        stcg_lines=stcg_lines or [],
        excluded_ltcg_deferred=excluded_ltcg_deferred or [],
        excluded_indian_source=excluded_indian_source or [],
        excluded_out_of_fy=excluded_out_of_fy or [],
    )


def _read_xlsx(path: Path) -> list[list[object]]:
    workbook = load_workbook(path)
    worksheet = workbook.active
    assert worksheet is not None
    rows: list[list[object]] = []
    for row in worksheet.iter_rows(values_only=True):
        rows.append(list(row))
    return rows


def _find_detail_row(data: list[list[object]], symbol: str) -> list[object]:
    for row in data[1:]:
        if row and row[0] == symbol:
            return row
    raise AssertionError(f"no detail row for symbol {symbol!r}")


# ---- registry --------------------------------------------------------------


def test_cg_xlsx_registered() -> None:
    assert "xlsx" in available_formats(Subcommand.CG)
    writer_cls = output_binding(Subcommand.CG, "xlsx").target
    assert writer_cls is ScheduleCgXlsxWriter
    assert writer_cls().format_id == "xlsx"  # type: ignore[abstract]


# ---- structural --------------------------------------------------------------


def test_writer_emits_header_audit_total_portal_total_for_empty_result(tmp_path: Path) -> None:
    """No STCG lines, no exclusions → 11 rows: header, the Table F block (blank
    row, marker, five bands, note; #1361), audit TOTAL, portal TOTAL."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_empty_result(), out)
    data = _read_xlsx(out)
    assert len(data) == 11
    assert data[0][0] == "Symbol"
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    # rounded ₹ portal cell: 0 with no STCG lines
    assert data[-1][-1] == 0


def test_writer_emits_one_detail_row_per_stcg_line(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[_make_stcg_line()]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    assert detail[1] == "US99992A1097"
    assert detail[2] == "US"
    assert detail[14] == "test basis"
    # realized gain rounded (idx 15): (1800 - 1500) * 86.20 = 25860 INR
    assert detail[15] == 25860


def test_header_puts_sbi_source_date_after_specified_date(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_empty_result(), out)
    assert _read_xlsx(out)[0] == [
        "Symbol",
        "ISIN",
        "Country (ISO 3166-1 alpha-2)",
        "Open date (DD/MM/YYYY)",
        "Close date (DD/MM/YYYY)",
        "Holding period (days)",
        "Quantity",
        "_audit: Cost basis USD",
        "_audit: Proceeds USD",
        "_audit: Transfer preceding-month-end date (YYYY-MM-DD)",
        "_audit: SBI TTBR source date (YYYY-MM-DD)",
        "_audit: Transfer SBI TTBR (INR/USD)",
        "_audit: Statutory cost basis INR (full precision)",
        "_audit: Proceeds INR (full precision)",
        "_audit: Statutory cost basis source",
        "Realized gain INR (rounded ₹)",
    ]


def test_writer_emits_ltcg_deferred_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot()
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(excluded_ltcg_deferred=[deferred]),
        out,
    )
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_LTCG_DEFERRED_MARKER in markers
    idx = markers.index(EXCLUDED_LTCG_DEFERRED_MARKER)
    deferred_row = data[idx + 1]
    assert deferred_row[0] == "XQCA"
    # follow_up_issue is empty (post-#30 cleanup); openpyxl reads
    # back empty-string cells as None.
    assert deferred_row[2] in ("", None)
    assert deferred_row[7] == 1430  # holding_period_days (numeric)


def test_writer_emits_indian_source_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2025, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_indian_source=[lot]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_CG_INDIAN_SOURCE_MARKER in markers


def test_writer_emits_out_of_fy_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        close_date_time=date(2024, 12, 15),  # outside FY 2025-26
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_out_of_fy=[lot]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_OUT_OF_FY_MARKER in markers


def test_portal_total_is_literal_last_row(tmp_path: Path) -> None:
    """Critical writer-contract assertion: the portal-TOTAL row must
    be the LITERAL last row of the sheet, even after multiple
    exclusion blocks. The user copy-pastes the rounded-₹ cell directly
    from the bottom of the sheet, which always holds the filed total (#1383)."""
    out = tmp_path / "cg.xlsx"
    result = _result_with(
        stcg_lines=[_make_stcg_line()],
        excluded_ltcg_deferred=[
            DeferredLtcgRow(closed_lot=_make_closed_lot(), holding_period_days=1430)
        ],
        excluded_indian_source=[_make_closed_lot(symbol=Ticker("XQCD"))],
        excluded_out_of_fy=[_make_closed_lot(close_date_time=date(2024, 12, 15))],
    )
    ScheduleCgXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    assert data[-1][-1] == result.portal_amounts.filed_total_inr


def test_audit_total_row_precedes_portal_total_row(tmp_path: Path) -> None:
    """Audit TOTAL must come immediately before portal TOTAL — same
    ordering as the CSV writer so an Excel user comparing both
    artefacts sees identical block order."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[_make_stcg_line()]), out)
    data = _read_xlsx(out)
    assert "_audit:" in str(data[-2][0])
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG


# ---- typed cell preservation -------------------------------------------------


def test_open_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line(open_date=date(2025, 1, 15))]),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQAQ")[3]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 1
    assert cell.day == 15


def test_close_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line(close_date=date(2025, 6, 20))]),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQAQ")[4]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 6
    assert cell.day == 20


def test_fx_specified_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line(fx_specified_date=date(2025, 5, 31))]),
        out,
    )
    data = _read_xlsx(out)
    cell = _find_detail_row(data, "XQAQ")[9]
    assert isinstance(cell, datetime)
    assert cell.year == 2025
    assert cell.month == 5
    assert cell.day == 31


def test_decimal_amounts_written_as_numbers(tmp_path: Path) -> None:
    """USD / INR / TTBR cells stay numeric so the user can SUM them
    in Excel directly. openpyxl reads back whole-number floats as
    Python ``int`` (Excel doesn't store the int/float distinction),
    so isinstance checks accept either."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line()]),
        out,
    )
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    # quantity (idx 6)
    assert isinstance(detail[6], (int, float))
    assert detail[6] == pytest.approx(10.0)
    # cost basis USD (idx 7)
    assert isinstance(detail[7], (int, float))
    assert detail[7] == pytest.approx(1500.0)
    # proceeds USD (idx 8)
    assert isinstance(detail[8], (int, float))
    assert detail[8] == pytest.approx(1800.0)
    # SBI TTBR source date (idx 10): blank when SBI published on the
    # specified date (#741)
    assert detail[10] is None
    # TTBR (idx 11) — fractional, always read back as float
    assert isinstance(detail[11], float)
    assert detail[11] == pytest.approx(86.20)
    # cost basis INR (idx 12)
    assert isinstance(detail[12], (int, float))
    assert detail[12] == pytest.approx(129300.0)
    # proceeds INR (idx 13)
    assert isinstance(detail[13], (int, float))
    assert detail[13] == pytest.approx(155160.0)
    assert detail[14] == "test basis"
    # realized gain rounded (idx 15): int via to_whole_rupees+coerce
    assert detail[15] == 25860


def test_holding_period_written_as_integer(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line(holding_period_days=156)]),
        out,
    )
    data = _read_xlsx(out)
    assert _find_detail_row(data, "XQAQ")[5] == 156


# ---- signed values (loss) ----------------------------------------------------


def test_net_loss_row_renders_as_native_negative_number(tmp_path: Path) -> None:
    """Critical XLSX-vs-CSV divergence: the CSV writer needs the
    apostrophe-prefix on negative cells to defeat the
    formula-injection guard's leading-``-`` rule for STRINGS, but
    XLSX numeric cells aren't subject to the guard (only string
    cells are checked). So a STCG loss rides as a native negative
    ``float`` in the XLSX — and Excel users can SUM the column
    without any conversion."""
    loss_line = _make_stcg_line(
        proceeds_usd=Decimal("1200"),  # loss: cost=1500
        proceeds_inr=Decimal("1200") * Decimal("86.20"),
        realized_gain_inr=(Decimal("1200") - Decimal("1500")) * Decimal("86.20"),
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[loss_line]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    # realized gain rounded (idx 15) — loss: -300 * 86.20 = -25860
    assert detail[15] == -25860
    # proceeds INR (idx 13) — also negative? no, proceeds is positive 103440
    assert detail[13] == pytest.approx(103440.0)
    # ALSO verify the portal-TOTAL cell carries the signed integer
    assert data[-1][-1] == -25860


def test_audit_total_handles_net_loss(tmp_path: Path) -> None:
    """Multi-line case where gains and losses sum to a net loss."""
    gain = _make_stcg_line(
        symbol=Ticker("XQAQ"),
        proceeds_usd=Decimal("1800"),
        proceeds_inr=Decimal("1800") * Decimal("86.20"),
        realized_gain_inr=(Decimal("1800") - Decimal("1500")) * Decimal("86.20"),
    )
    loss = _make_stcg_line(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        cost_basis_usd=Decimal("2000"),
        proceeds_usd=Decimal("1000"),
        cost_basis_inr=Decimal("2000") * Decimal("86.20"),
        proceeds_inr=Decimal("1000") * Decimal("86.20"),
        realized_gain_inr=(Decimal("1000") - Decimal("2000")) * Decimal("86.20"),
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[gain, loss]), out)
    data = _read_xlsx(out)
    # Net realised: 25860 + (-86200) = -60340
    assert data[-1][-1] == -60340


# ---- header + format ---------------------------------------------------------


def test_header_row_is_bold(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    for col in range(1, 15):
        assert worksheet.cell(row=1, column=col).font.bold is True


def test_sheet_is_frozen_below_header(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    assert worksheet.freeze_panes == "A2"


def test_sheet_name_is_schedule_cg_stcg(tmp_path: Path) -> None:
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    assert worksheet.title == "Schedule CG STCG"


# ---- per-row column-format routing (T5 codex PR #204 P2) ---------------------


def _find_row_by_first_cell(worksheet: Worksheet, needle: str) -> int:
    """Return the 1-indexed row number whose column-1 cell contains ``needle``."""
    for row_index in range(1, worksheet.max_row + 1):
        cell_value = worksheet.cell(row=row_index, column=1).value
        if cell_value is not None and needle in str(cell_value):
            return row_index
    raise AssertionError(f"No row whose column-1 starts with {needle!r}")


def test_audit_total_cell_uses_full_precision_format(tmp_path: Path) -> None:
    """Regression for T5 codex PR #204 P2 — the audit-TOTAL row's
    column-15 cell carries the FULL-precision realized-gain ₹ sum,
    so it MUST use the full-precision number format
    (``#,##0.00000000;-#,##0.00000000``) and NOT the rounded
    whole-rupee detail format (``#,##0;-#,##0``). Without the fix
    a user opening the workbook saw the audit-TOTAL value rendered
    as a rounded integer, contradicting the row label that
    explicitly promises "full precision"."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line()]),
        out,
    )
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    audit_row = _find_row_by_first_cell(worksheet, "_audit: TOTAL")
    cell = worksheet.cell(row=audit_row, column=16)
    assert cell.number_format == "#,##0.00000000;-#,##0.00000000"


def test_portal_total_cell_uses_whole_rupee_format(tmp_path: Path) -> None:
    """The portal-TOTAL row's column-15 cell is the whole-rupee
    integer the user pastes into the e-filing portal, so it MUST
    use the rounded integer format (``#,##0;-#,##0``)."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(stcg_lines=[_make_stcg_line()]),
        out,
    )
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    portal_row = _find_row_by_first_cell(worksheet, PORTAL_FIELD_LABEL_CG_FOREIGN_STCG)
    cell = worksheet.cell(row=portal_row, column=16)
    assert cell.number_format == "#,##0;-#,##0"


def test_sbi_source_date_cell_is_a_native_date_only_on_carry_back(tmp_path: Path) -> None:
    carried = _make_stcg_line(fx_rate_publication_date=date(2025, 5, 30))
    same_day = _make_stcg_line(symbol=Ticker("XQCA"), isin=Isin("US99972B1017"))
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[carried, same_day]), out)
    worksheet = load_workbook(out).active
    assert worksheet is not None
    carried_row = _find_row_by_first_cell(worksheet, "XQAQ")
    source = worksheet.cell(row=carried_row, column=11)
    assert source.value == datetime(2025, 5, 30)
    assert source.number_format == "YYYY-MM-DD"
    # The shifted TTBR, INR and rounded-gain cells keep their formats.
    assert [worksheet.cell(row=carried_row, column=c).number_format for c in (12, 13, 14, 16)] == [
        "#,##0.0000",
        "#,##0.00000000;-#,##0.00000000",
        "#,##0.00000000;-#,##0.00000000",
        "#,##0;-#,##0",
    ]
    same_day_row = _find_row_by_first_cell(worksheet, "XQCA")
    assert worksheet.cell(row=same_day_row, column=11).value is None


def test_every_generated_row_and_format_tuple_has_16_cells() -> None:
    """Checks the generated rows: the read-back sheet pads short rows."""
    result = _result_with(
        stcg_lines=[_make_stcg_line()],
        excluded_ltcg_deferred=[
            DeferredLtcgRow(closed_lot=_make_closed_lot(), holding_period_days=1430)
        ],
        excluded_indian_source=[
            _make_closed_lot(
                symbol=Ticker("XQCD"),
                isin=Isin("IN99970W1077"),
                issuer_country_code=CountryCode("IN"),
                open_date_time=date(2025, 1, 5),
                close_date_time=date(2025, 6, 10),
            )
        ],
        excluded_out_of_fy=[
            _make_closed_lot(open_date_time=date(2024, 1, 1), close_date_time=date(2024, 6, 1))
        ],
    )
    rows = list(_iter_rows(result, result.portal_amounts, table_f_labels=("label",) * 5))
    assert {(len(row), len(formats)) for row, formats in rows} == {(16, 16)}
    (audit_row, audit_formats), (portal_row, portal_formats) = rows[-2:]
    assert audit_row[15] == result.portal_amounts.gain_inr_full_precision
    assert audit_formats[15] == "#,##0.00000000;-#,##0.00000000"
    assert portal_row[15] == result.portal_amounts.filed_total_inr
    assert portal_formats[15] == "#,##0;-#,##0"


def test_ltcg_deferred_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """Regression for T5 codex PR #204 P2 — exclusion-block reject
    rows have a SHIFTED column layout (3 leading marker columns
    push the date columns to indices 5/6 instead of 3/4). Without
    per-row format tuples, the date columns would not get the
    ``DD/MM/YYYY`` format applied, and the detail-row date format
    would fall on unrelated cells. Pin the LTCG-deferred date
    formatting at the actual columns."""
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 15),
        close_date_time=date(2025, 6, 20),
    )
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1983)
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_ltcg_deferred=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_LTCG_DEFERRED_MARKER)
    reject_row = marker_row + 1
    # Columns 6 / 7 = open_date / close_date (1-indexed).
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=7).number_format == "DD/MM/YYYY"


def test_ltcg_deferred_reject_row_holding_days_get_integer_format(tmp_path: Path) -> None:
    """holding_period_days lives at column-8 (1-indexed) in the
    LTCG-deferred reject row layout. It MUST get the integer
    format applied so 1983 renders as ``1,983`` rather than the
    detail row's column-8 USD format which would render it as
    ``1,983.00000000``."""
    lot = _make_closed_lot()
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1983)
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_ltcg_deferred=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_LTCG_DEFERRED_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=8).number_format == "#,##0"


def test_marker_row_cells_have_no_number_format_applied(tmp_path: Path) -> None:
    """Exclusion marker rows are text-only labels (column names
    embedded as strings). They MUST NOT receive a number format
    that would either format a string cell oddly or cascade into
    rendering text as a numeric value."""
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(
        _result_with(
            excluded_ltcg_deferred=[
                DeferredLtcgRow(closed_lot=_make_closed_lot(), holding_period_days=1430)
            ],
        ),
        out,
    )
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_LTCG_DEFERRED_MARKER)
    # The default openpyxl cell number_format is "General" — any
    # marker-row cell that *had* received a date/USD/rupee format
    # would diverge from this default.
    for col in range(1, 15):
        cell = worksheet.cell(row=marker_row, column=col)
        assert cell.number_format == "General"


def test_in_source_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """T4 claude PR #204 round-2 follow-up — pin the in-source
    reject row's date columns at their shifted positions (6/7,
    1-indexed) so a future column-order edit can't silently break
    date formatting on this block. Layout: 0=symbol, 1=reason,
    2=follow_up_issue, 3=symbol, 4=isin, 5=open_date, 6=close_date,
    7=quantity, 8=cost_basis, 9=proceeds."""
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2025, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_indian_source=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_CG_INDIAN_SOURCE_MARKER)
    reject_row = marker_row + 1
    # Columns 6 / 7 = open_date / close_date (1-indexed).
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=7).number_format == "DD/MM/YYYY"


def test_in_source_marker_row_cells_have_no_number_format_applied(tmp_path: Path) -> None:
    """In-source marker row counterpart of the LTCG-deferred
    marker-format pin — ensure the text-only label row gets no
    cascade from detail-row column formats."""
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_indian_source=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_CG_INDIAN_SOURCE_MARKER)
    for col in range(1, 15):
        cell = worksheet.cell(row=marker_row, column=col)
        assert cell.number_format == "General"


def test_out_of_fy_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """T4 claude PR #204 round-2 follow-up — pin the out-of-FY
    reject row's date columns at their shifted positions (5/6,
    1-indexed). Layout omits follow_up_issue: 0=symbol, 1=reason,
    2=symbol, 3=isin, 4=open_date, 5=close_date, 6=quantity,
    7=cost_basis, 8=proceeds."""
    lot = _make_closed_lot(
        open_date_time=date(2024, 1, 15),
        close_date_time=date(2024, 12, 15),  # outside FY 2025-26
    )
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_out_of_fy=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_OUT_OF_FY_MARKER)
    reject_row = marker_row + 1
    # Columns 5 / 6 = open_date / close_date (1-indexed).
    assert worksheet.cell(row=reject_row, column=5).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"


def test_out_of_fy_marker_row_cells_have_no_number_format_applied(tmp_path: Path) -> None:
    """Out-of-FY marker row counterpart of the marker-format pins."""
    lot = _make_closed_lot(close_date_time=date(2024, 12, 15))
    out = tmp_path / "cg.xlsx"
    ScheduleCgXlsxWriter().write(_result_with(excluded_out_of_fy=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_OUT_OF_FY_MARKER)
    for col in range(1, 15):
        cell = worksheet.cell(row=marker_row, column=col)
        assert cell.number_format == "General"


# ---- writer-contract enforcement ---------------------------------------------


def test_writer_rejects_nul_byte_in_string_cell(tmp_path: Path) -> None:
    """NUL bytes are not valid XML payload — must be rejected at
    write time, not silently corrupt the file. Planted via the
    Symbol cell (``NewType("Ticker", str)`` with no length / shape
    validation in the model layer)."""
    out = tmp_path / "cg.xlsx"
    bad_line = _make_stcg_line(symbol=Ticker("XQAQ\x00X"))
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[bad_line]), out)
    assert excinfo.value.violation == "nul_byte"
    assert excinfo.value.cell_index == 0


@pytest.mark.parametrize(
    "trigger",
    [
        # ASCII Excel formula triggers.
        "=",
        "+",
        "-",
        "@",
        # C0 whitespace triggers.
        "\t",
        "\v",
        "\r",
        "\n",
        # Full-width Unicode homoglyphs (CJK ASCII forms).
        "\uff1d",  # full-width equals sign
        "\uff0b",  # full-width plus sign
        "\uff0d",  # full-width hyphen-minus
        "\uff20",  # full-width commercial at
    ],
)
def test_formula_trigger_in_string_cell_raises(tmp_path: Path, trigger: str) -> None:
    """Excel auto-executes formula-prefixed cells; the CG writer
    must reject the OWASP-extended trigger set on every string cell.
    Injected via Symbol (str-coerced un-stripped, unlike description
    which the upstream STCG line normalises)."""
    out = tmp_path / "cg.xlsx"
    bad_line = _make_stcg_line(symbol=Ticker(f"{trigger}EVIL"))
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgXlsxWriter().write(_result_with(stcg_lines=[bad_line]), out)
    assert excinfo.value.violation == "formula_trigger"
    assert excinfo.value.trigger == trigger


def test_nul_byte_cell_index_preserved_across_typed_cells(tmp_path: Path) -> None:
    """PR #202 T5 round-1 P3 regression: planting a NUL in the
    description (string cell at column 11 in the LTCG-deferred
    block) must report ``cell_index == 11`` despite the
    intervening numeric / datetime cells. A naive
    string-filter-then-CSV-helper compaction would report a
    smaller index."""
    lot = _make_closed_lot(description="SAMPLE ISSUER CA\x00CORP")
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)
    out = tmp_path / "cg.xlsx"
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgXlsxWriter().write(
            _result_with(excluded_ltcg_deferred=[deferred]),
            out,
        )
    assert excinfo.value.violation == "nul_byte"
    # description sits at column 11 in the LTCG-deferred reject row
    assert excinfo.value.cell_index == 11


# ---- per-AY portal label threading -------------------------------------------


def test_per_ay_writer_routes_portal_label_via_for_ay(tmp_path: Path) -> None:
    """Issue #65: ``assessment_year_start`` constructor kwarg must
    route the portal-cell label through
    :func:`ibkr_fa.output.portal_labels.for_ay`. The current AY
    has the canonical label; a different AY must produce a
    matching snapshot's label (verified by identity of the literal
    last-row first cell with the snapshot label module's exported
    constant)."""
    snapshot = for_ay(2026)
    out = tmp_path / "cg.xlsx"
    writer = ScheduleCgXlsxWriter(assessment_year_start=2026)
    writer.write(_empty_result(), out)
    data = _read_xlsx(out)
    assert data[-1][0] == snapshot.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG


def test_itr3_writer_uses_a6g_destination(tmp_path: Path) -> None:
    snapshot = for_ay(2026)
    out = tmp_path / "itr3.xlsx"
    ScheduleCgXlsxWriter(assessment_year_start=2026, itr_form="ITR-3").write(_empty_result(), out)
    assert (
        _read_xlsx(out)[-1][0] == snapshot.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM["ITR-3"]
    )
