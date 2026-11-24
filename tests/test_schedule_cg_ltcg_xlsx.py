"""Tests for :mod:`ibkr_fa.output.schedule_cg_ltcg_xlsx`.

Sister to :mod:`tests.test_schedule_cg_xlsx`. Validates the XLSX
writer for Schedule CG LTCG against the same shape contracts the
CSV sister enforces (section order, marker rows, typed cells,
writer-contract guards), plus XLSX-specific properties (native
date / numeric cells, per-row column number formats, freeze pane,
bold header, sheet name).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from ibkr_fa.compute.schedule_cg_ltcg import (
    DeferredStcgRow,
    PreCutoverLtcgRow,
    ScheduleCgLtcgComputeResult,
)
from ibkr_fa.models import BasisProvenance, ClosedLot
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.output import (
    EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_OUT_OF_FY_MARKER,
    EXCLUDED_PRE_CUTOVER_MARKER,
    EXCLUDED_STCG_MARKER,
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG, for_ay
from ibkr_fa.output.schedule_cg_ltcg_xlsx import ScheduleCgLtcgXlsxWriter, _iter_rows
from ibkr_fa.types import CountryCode, Isin, Ticker


def _make_ltcg_line(**overrides: object) -> ScheduleCgLtcgLine:
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAQ"),
        "isin": Isin("US99992A1097"),
        "issuer_country_iso2": "US",
        "description": "SAMPLE ISSUER AQ INC",
        "open_date": date(2023, 4, 1),
        "close_date": date(2025, 12, 10),
        "holding_period_days": (date(2025, 12, 10) - date(2023, 4, 1)).days,
        "quantity": Decimal("10"),
        "cost_basis_usd": Decimal("1500"),
        "proceeds_usd": Decimal("2200"),
        "fx_specified_date": date(2025, 11, 30),
        "fx_rate_inr_per_usd": Decimal("86.20"),
        "cost_basis_inr": Decimal("1500") * Decimal("86.20"),
        "cost_basis_inr_source": "test basis",
        "proceeds_inr": Decimal("2200") * Decimal("86.20"),
        "realized_gain_inr": (Decimal("2200") - Decimal("1500")) * Decimal("86.20"),
    }
    defaults.update(overrides)
    defaults.setdefault("fx_rate_publication_date", defaults["fx_specified_date"])
    return ScheduleCgLtcgLine(**defaults)  # ty: ignore[invalid-argument-type]


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
        "open_date_time": date(2021, 1, 1),
        "close_date_time": date(2025, 12, 1),
        "basis_provenance": BasisProvenance.ENGINE_FIFO,
    }
    defaults.update(overrides)
    return ClosedLot(**defaults)  # ty: ignore[invalid-argument-type]


def _empty_result() -> ScheduleCgLtcgComputeResult:
    return ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


def _result_with(
    *,
    ltcg_lines: list[ScheduleCgLtcgLine] | None = None,
    excluded_stcg: list[DeferredStcgRow] | None = None,
    excluded_pre_2024_07_23: list[PreCutoverLtcgRow] | None = None,
    excluded_indian_source: list[ClosedLot] | None = None,
    excluded_out_of_fy: list[ClosedLot] | None = None,
) -> ScheduleCgLtcgComputeResult:
    return ScheduleCgLtcgComputeResult(
        ltcg_lines=ltcg_lines or [],
        excluded_stcg=excluded_stcg or [],
        excluded_pre_2024_07_23=excluded_pre_2024_07_23 or [],
        excluded_indian_source=excluded_indian_source or [],
        excluded_out_of_fy=excluded_out_of_fy or [],
    )


def _read_xlsx(path: Path) -> list[list[object]]:
    workbook = load_workbook(path)
    worksheet = workbook.active
    assert worksheet is not None
    return [list(row) for row in worksheet.iter_rows(values_only=True)]


def _find_detail_row(data: list[list[object]], symbol: str) -> list[object]:
    for row in data:
        if row and row[0] == symbol:
            return row
    raise AssertionError(f"No detail row found for symbol {symbol!r}")


def _find_row_by_first_cell(worksheet: Worksheet, needle: str) -> int:
    """Return the 1-indexed row number whose column-1 cell contains ``needle``."""
    for row_index in range(1, worksheet.max_row + 1):
        cell_value = worksheet.cell(row=row_index, column=1).value
        if cell_value is not None and needle in str(cell_value):
            return row_index
    raise AssertionError(f"No row whose column-1 starts with {needle!r}")


# ---- registry / format-id ----------------------------------------------------


def test_xlsx_writer_registered_under_xlsx_format() -> None:
    assert "xlsx" in available_formats(Subcommand.CG_LTCG)
    writer_cls = output_binding(Subcommand.CG_LTCG, "xlsx").target
    assert writer_cls is ScheduleCgLtcgXlsxWriter
    assert writer_cls().format_id == "xlsx"


# ---- structural -------------------------------------------------------------


def test_writer_emits_header_audit_total_portal_total_for_empty_result(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_empty_result(), out)
    data = _read_xlsx(out)
    # header, the Table F block (blank row, marker, five bands, note; #1361),
    # audit TOTAL, portal TOTAL
    assert len(data) == 11
    assert data[0][0] == "Symbol"
    assert "_audit: TOTAL" in str(data[-2][0])
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    assert data[-1][-1] == 0


def test_writer_emits_one_detail_row_per_ltcg_line(tmp_path: Path) -> None:
    line = _make_ltcg_line()
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[line]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    assert detail[1] == "US99992A1097"
    assert detail[2] == "US"


def test_header_puts_sbi_source_date_after_specified_date(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_empty_result(), out)
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


def test_writer_emits_stcg_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(open_date_time=date(2024, 1, 1), close_date_time=date(2025, 6, 1))
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=517)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_stcg=[deferred]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_STCG_MARKER in markers
    idx = markers.index(EXCLUDED_STCG_MARKER)
    reject_row = data[idx + 1]
    assert reject_row[0] == "XQCA"
    assert reject_row[7] == 517  # holding_period_days


def test_writer_emits_pre_cutover_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 7, 1),  # before 2024-07-23 cutover
    )
    deferred = PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1643)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_pre_2024_07_23=[deferred]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_PRE_CUTOVER_MARKER in markers


def test_writer_emits_indian_source_ltcg_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2021, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_indian_source=[lot]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER in markers


def test_writer_emits_out_of_fy_ltcg_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(close_date_time=date(2024, 12, 15))  # outside FY 2025-26
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_out_of_fy=[lot]), out)
    data = _read_xlsx(out)
    markers = [str(r[0]) if r and r[0] is not None else "" for r in data]
    assert EXCLUDED_LTCG_OUT_OF_FY_MARKER in markers


def test_portal_total_is_literal_last_row(tmp_path: Path) -> None:
    """Critical writer-contract assertion: the portal-TOTAL row must
    be the LITERAL last row of the sheet, even after multiple
    exclusion blocks. The user copy-pastes the rounded-₹ cell directly
    from the bottom of the sheet, which always holds the filed total (#1383)."""
    out = tmp_path / "cg-ltcg.xlsx"
    result = _result_with(
        ltcg_lines=[_make_ltcg_line()],
        excluded_stcg=[DeferredStcgRow(closed_lot=_make_closed_lot(), holding_period_days=400)],
        excluded_pre_2024_07_23=[
            PreCutoverLtcgRow(
                closed_lot=_make_closed_lot(close_date_time=date(2024, 7, 1)),
                holding_period_days=1643,
            )
        ],
        excluded_indian_source=[_make_closed_lot(symbol=Ticker("XQCD"))],
        excluded_out_of_fy=[_make_closed_lot(close_date_time=date(2024, 12, 15))],
    )
    ScheduleCgLtcgXlsxWriter().write(result, out)
    data = _read_xlsx(out)
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    assert data[-1][-1] == result.portal_amounts.filed_total_inr


def test_audit_total_row_precedes_portal_total_row(tmp_path: Path) -> None:
    """Audit TOTAL must come immediately before portal TOTAL — same
    ordering as the CSV writer so an Excel user comparing both
    artefacts sees identical block order."""
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    data = _read_xlsx(out)
    assert "_audit:" in str(data[-2][0])
    assert data[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG


# ---- typed cell preservation -------------------------------------------------


def test_open_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    cell = worksheet.cell(row=2, column=4)
    assert cell.value is not None
    assert hasattr(cell.value, "year")
    assert cell.value.year == 2023  # type: ignore[union-attr]


def test_close_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    cell = worksheet.cell(row=2, column=5)
    assert cell.value is not None
    assert hasattr(cell.value, "year")
    assert cell.value.year == 2025  # type: ignore[union-attr]


def test_fx_specified_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    cell = worksheet.cell(row=2, column=10)
    assert cell.value is not None
    assert hasattr(cell.value, "year")
    assert cell.value.year == 2025  # type: ignore[union-attr]


def test_decimal_amounts_written_as_numbers(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    # Quantity (col 7) is 10 → whole-number float reads back as int
    assert isinstance(detail[6], (int, float))
    assert detail[6] == 10
    # Cost basis USD (col 8) is 1500 → whole-number float reads as int
    assert isinstance(detail[7], (int, float))
    assert detail[7] == 1500
    # SBI TTBR source date (col 11): blank without carry-back (#741)
    assert detail[10] is None
    # FX rate (col 12) is 86.20 → fractional float
    assert isinstance(detail[11], float)
    assert detail[11] == pytest.approx(86.20)


def test_holding_period_written_as_integer(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    expected_days = (date(2025, 12, 10) - date(2023, 4, 1)).days
    assert detail[5] == expected_days
    assert isinstance(detail[5], int)


# ---- signed loss values ------------------------------------------------------


def test_net_loss_row_renders_as_native_negative_number(tmp_path: Path) -> None:
    loss_line = _make_ltcg_line(
        cost_basis_usd=Decimal("2000"),
        proceeds_usd=Decimal("1000"),
        cost_basis_inr=Decimal("2000") * Decimal("86.20"),
        proceeds_inr=Decimal("1000") * Decimal("86.20"),
        realized_gain_inr=(Decimal("1000") - Decimal("2000")) * Decimal("86.20"),
    )
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[loss_line]), out)
    data = _read_xlsx(out)
    detail = _find_detail_row(data, "XQAQ")
    # realized_gain_inr column (col 16, index 15) — net loss
    realized_gain = detail[15]
    assert realized_gain is not None
    assert isinstance(realized_gain, (int, float))
    assert realized_gain < 0


def test_audit_total_handles_net_loss(tmp_path: Path) -> None:
    """Multi-line case where gains and losses sum to a net loss."""
    gain = _make_ltcg_line(
        symbol=Ticker("XQAQ"),
        proceeds_usd=Decimal("2200"),
        proceeds_inr=Decimal("2200") * Decimal("86.20"),
        realized_gain_inr=(Decimal("2200") - Decimal("1500")) * Decimal("86.20"),
    )
    loss = _make_ltcg_line(
        symbol=Ticker("XQCA"),
        isin=Isin("US99972B1017"),
        cost_basis_usd=Decimal("2000"),
        proceeds_usd=Decimal("500"),
        cost_basis_inr=Decimal("2000") * Decimal("86.20"),
        proceeds_inr=Decimal("500") * Decimal("86.20"),
        realized_gain_inr=(Decimal("500") - Decimal("2000")) * Decimal("86.20"),
    )
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[gain, loss]), out)
    data = _read_xlsx(out)
    # Net realised: 60340 + (-129300) = -68960
    assert data[-1][-1] == -68960


# ---- header + format ---------------------------------------------------------


def test_header_row_is_bold(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    for col in range(1, 15):
        assert worksheet.cell(row=1, column=col).font.bold is True


def test_sheet_is_frozen_below_header(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    assert worksheet.freeze_panes == "A2"


def test_sheet_name_is_schedule_cg_ltcg(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_empty_result(), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    assert worksheet.title == "Schedule CG LTCG"


# ---- per-row column-format routing -------------------------------------------


def test_audit_total_cell_uses_full_precision_format(tmp_path: Path) -> None:
    """The audit-TOTAL row's column-16 cell carries the FULL-precision
    realized-gain ₹ sum, so it MUST use the full-precision number
    format (matching the CG STCG XLSX writer)."""
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    audit_row = _find_row_by_first_cell(worksheet, "_audit: TOTAL")
    cell = worksheet.cell(row=audit_row, column=16)
    assert cell.number_format == "#,##0.00000000;-#,##0.00000000"


def test_portal_total_cell_uses_whole_rupee_format(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[_make_ltcg_line()]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    portal_row = _find_row_by_first_cell(worksheet, PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG)
    cell = worksheet.cell(row=portal_row, column=16)
    assert cell.number_format == "#,##0;-#,##0"


def test_sbi_source_date_cell_is_a_native_date_only_on_carry_back(tmp_path: Path) -> None:
    """2025-11-30 is a Sunday; the rate comes from SBI's 2025-11-29 row."""
    carried = _make_ltcg_line(fx_rate_publication_date=date(2025, 11, 29))
    same_day = _make_ltcg_line(symbol=Ticker("XQCA"), isin=Isin("US99972B1017"))
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[carried, same_day]), out)
    worksheet = load_workbook(out).active
    assert worksheet is not None
    carried_row = _find_row_by_first_cell(worksheet, "XQAQ")
    source = worksheet.cell(row=carried_row, column=11)
    assert source.value == datetime(2025, 11, 29)
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
        ltcg_lines=[_make_ltcg_line()],
        excluded_stcg=[
            DeferredStcgRow(
                closed_lot=_make_closed_lot(
                    open_date_time=date(2025, 1, 15), close_date_time=date(2025, 6, 20)
                ),
                holding_period_days=156,
            )
        ],
        excluded_pre_2024_07_23=[
            PreCutoverLtcgRow(
                closed_lot=_make_closed_lot(
                    open_date_time=date(2020, 1, 1), close_date_time=date(2024, 7, 22)
                ),
                holding_period_days=1664,
            )
        ],
        excluded_indian_source=[
            _make_closed_lot(
                symbol=Ticker("XQCD"),
                isin=Isin("IN99970W1077"),
                issuer_country_code=CountryCode("IN"),
                open_date_time=date(2023, 1, 5),
                close_date_time=date(2025, 6, 10),
            )
        ],
        excluded_out_of_fy=[
            _make_closed_lot(open_date_time=date(2020, 1, 1), close_date_time=date(2024, 12, 1))
        ],
    )
    rows = list(_iter_rows(result, result.portal_amounts, table_f_labels=("label",) * 5))
    assert {(len(row), len(formats)) for row, formats in rows} == {(16, 16)}
    (audit_row, audit_formats), (portal_row, portal_formats) = rows[-2:]
    assert audit_row[15] == result.portal_amounts.gain_inr_full_precision
    assert audit_formats[15] == "#,##0.00000000;-#,##0.00000000"
    assert portal_row[15] == result.portal_amounts.filed_total_inr
    assert portal_formats[15] == "#,##0;-#,##0"


def test_stcg_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """STCG-deferred reject rows have a shifted column layout (date
    columns at 1-indexed 6/7 like the CG STCG writer's LTCG-deferred
    block). Pin the DD/MM/YYYY format at the actual columns."""
    lot = _make_closed_lot(
        open_date_time=date(2024, 1, 1),
        close_date_time=date(2025, 6, 1),
    )
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=517)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_stcg=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_STCG_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=7).number_format == "DD/MM/YYYY"


def test_stcg_reject_row_holding_days_get_integer_format(tmp_path: Path) -> None:
    """holding_period_days lives at column 8 (1-indexed) on the
    STCG-deferred reject row layout — must get integer format."""
    lot = _make_closed_lot()
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=517)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_stcg=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_STCG_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=8).number_format == "#,##0"


def test_pre_cutover_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 7, 1),
    )
    deferred = PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1643)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_pre_2024_07_23=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_PRE_CUTOVER_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=7).number_format == "DD/MM/YYYY"


def test_in_source_ltcg_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """In-source LTCG reject rows have no holding_period_days column;
    dates are at 1-indexed columns 6/7."""
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2021, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_indian_source=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=7).number_format == "DD/MM/YYYY"


def test_out_of_fy_ltcg_reject_row_dates_get_date_format(tmp_path: Path) -> None:
    """Out-of-FY LTCG reject rows have no follow_up_issue column;
    dates are at 1-indexed columns 5/6."""
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 12, 15),
    )
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_out_of_fy=[lot]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_LTCG_OUT_OF_FY_MARKER)
    reject_row = marker_row + 1
    assert worksheet.cell(row=reject_row, column=5).number_format == "DD/MM/YYYY"
    assert worksheet.cell(row=reject_row, column=6).number_format == "DD/MM/YYYY"


def test_stcg_marker_row_cells_have_no_number_format_applied(tmp_path: Path) -> None:
    lot = _make_closed_lot()
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=400)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_stcg=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_STCG_MARKER)
    for col in range(1, 15):
        cell = worksheet.cell(row=marker_row, column=col)
        assert cell.number_format == "General"


def test_pre_cutover_marker_row_cells_have_no_number_format_applied(
    tmp_path: Path,
) -> None:
    lot = _make_closed_lot(close_date_time=date(2024, 7, 1))
    deferred = PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1643)
    out = tmp_path / "cg-ltcg.xlsx"
    ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_pre_2024_07_23=[deferred]), out)
    workbook = load_workbook(out)
    worksheet = workbook.active
    assert worksheet is not None
    marker_row = _find_row_by_first_cell(worksheet, EXCLUDED_PRE_CUTOVER_MARKER)
    for col in range(1, 15):
        cell = worksheet.cell(row=marker_row, column=col)
        assert cell.number_format == "General"


# ---- writer-contract enforcement ---------------------------------------------


def test_writer_rejects_nul_byte_in_string_cell(tmp_path: Path) -> None:
    bad_line = _make_ltcg_line(symbol=Ticker("XO\x00RC"))
    out = tmp_path / "cg-ltcg.xlsx"
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[bad_line]), out)
    assert excinfo.value.violation == "nul_byte"
    assert excinfo.value.cell_index == 0
    assert not out.exists()


@pytest.mark.parametrize(
    "trigger",
    [
        "=",
        "+",
        "-",
        "@",
        "\t",
        "\r",
        "\n",
        "\v",
        "\uff1d",  # full-width equals sign
        "\uff0b",  # full-width plus sign
        "\uff0d",  # full-width hyphen-minus
        "\uff20",  # full-width commercial at
    ],
)
def test_formula_trigger_in_string_cell_raises(tmp_path: Path, trigger: str) -> None:
    bad_line = _make_ltcg_line(symbol=Ticker(f"{trigger}EVIL()"))
    out = tmp_path / "cg-ltcg.xlsx"
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgLtcgXlsxWriter().write(_result_with(ltcg_lines=[bad_line]), out)
    assert excinfo.value.violation == "formula_trigger"
    assert not out.exists()


def test_nul_byte_cell_index_preserved_across_typed_cells(tmp_path: Path) -> None:
    """Regression: NUL planting in the STCG-deferred description
    column (column-index 11, 0-indexed) must surface as
    ``cell_index == 11`` despite the leading 5 string cells, 2
    datetime cells, and 4 numeric cells in the same row."""
    lot = _make_closed_lot(description="SAMPLE ISSUER CA \x00 CORP")
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=400)
    out = tmp_path / "cg-ltcg.xlsx"
    with pytest.raises(WriterContractError) as excinfo:
        ScheduleCgLtcgXlsxWriter().write(_result_with(excluded_stcg=[deferred]), out)
    assert excinfo.value.violation == "nul_byte"
    assert excinfo.value.cell_index == 11


# ---- per-AY portal-label threading -------------------------------------------


def test_per_ay_writer_routes_portal_label_via_for_ay(tmp_path: Path) -> None:
    """Issue #65: ``assessment_year_start`` constructor kwarg must
    route the portal-cell label through
    :func:`ibkr_fa.output.portal_labels.for_ay`."""
    snapshot = for_ay(2026)
    out = tmp_path / "cg-ltcg.xlsx"
    writer = ScheduleCgLtcgXlsxWriter(assessment_year_start=2026)
    writer.write(_empty_result(), out)
    data = _read_xlsx(out)
    assert data[-1][0] == snapshot.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG


def test_itr3_writer_uses_b9e_destination(tmp_path: Path) -> None:
    snapshot = for_ay(2026)
    out = tmp_path / "itr3.xlsx"
    ScheduleCgLtcgXlsxWriter(assessment_year_start=2026, itr_form="ITR-3").write(
        _empty_result(), out
    )
    assert (
        _read_xlsx(out)[-1][0] == snapshot.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM["ITR-3"]
    )
