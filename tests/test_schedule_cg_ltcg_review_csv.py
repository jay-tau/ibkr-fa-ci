"""Unit tests for Schedule CG LTCG review-CSV writer."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

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
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM,
)
from ibkr_fa.output.schedule_cg_ltcg_review_csv import _AUDIT_TOTAL_LABEL
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._writer_stubs import writer_class


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


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def _empty_result() -> ScheduleCgLtcgComputeResult:
    return ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


def test_cg_ltcg_review_csv_registered() -> None:
    assert "review-csv" in available_formats(Subcommand.CG_LTCG)
    writer_cls = writer_class(Subcommand.CG_LTCG, "review-csv")
    assert writer_cls().format_id == "review-csv"  # type: ignore[abstract]


def test_writer_emits_header_total_for_empty_result(tmp_path: Path) -> None:
    writer = writer_class(Subcommand.CG_LTCG, "review-csv")()
    out = tmp_path / "cg-ltcg.csv"
    writer.write(_empty_result(), out)
    rows = _read_rows(out)
    # header + Table F block (blank row, marker, five bands, note; #1361)
    # + audit-total + portal-total = 11
    assert len(rows) == 11
    assert rows[0][0] == "Symbol"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    assert rows[-1][-1] == "0"


def test_writer_emits_one_detail_row_per_ltcg_line(tmp_path: Path) -> None:
    line = _make_ltcg_line()
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[line],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    assert rows[1][0] == "XQAQ"
    assert rows[1][1] == "US99992A1097"
    assert rows[1][2] == "US"
    assert rows[1][3] == "01/04/2023"
    assert rows[1][4] == "10/12/2025"
    # Holding period > 24 months
    assert int(rows[1][5]) > 720
    assert rows[1][6] == "10"
    # Column layout matches STCG: 9=specified date, 10=SBI TTBR source
    # date (#741), 11=TTBR, 12=basis INR, 13=proceeds INR, 14=basis
    # source, 15=rounded gain INR.
    assert rows[1][9] == "2025-11-30"
    # No carry-back: blank source-date cell.
    assert rows[1][10] == ""
    assert rows[1][11] == "86.20"
    # cost_basis_inr (idx 12): 1500 * 86.20 = 129300.00
    assert rows[1][12] == "129300.00"
    # proceeds_inr (idx 13): 2200 * 86.20 = 189640.00
    assert rows[1][13] == "189640.00"
    assert rows[1][14] == "test basis"
    # rounded gain (idx 15): (2200 - 1500) * 86.20 = 60340.00 → 60340
    assert rows[1][15] == "60340"


def test_header_puts_sbi_source_date_after_specified_date(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(_empty_result(), out)
    assert _read_rows(out)[0] == [
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


def test_carried_back_row_shows_sbi_source_date(tmp_path: Path) -> None:
    """2025-11-30 is a Sunday; the rate comes from SBI's 2025-11-29 row."""
    line = _make_ltcg_line(fx_rate_publication_date=date(2025, 11, 29))
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(
        ScheduleCgLtcgComputeResult([line], [], [], [], []), out
    )
    detail = _read_rows(out)[1]
    assert detail[9:12] == ["2025-11-30", "2025-11-29", "86.20"]


def test_writer_emits_stcg_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2025, 1, 15),
        close_date_time=date(2025, 6, 20),
    )
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=156)
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[deferred],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_STCG_MARKER in markers
    idx = markers.index(EXCLUDED_STCG_MARKER)
    data = rows[idx + 1]
    assert data[0] == "XQCA"
    assert data[2] == "#29"
    assert data[7] == "156"


def test_writer_emits_pre_cutover_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 7, 22),
    )
    deferred = PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1664)
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[deferred],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_PRE_CUTOVER_MARKER in markers


def test_writer_emits_indian_source_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2023, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[lot],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER in markers


def test_writer_emits_out_of_fy_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 12, 1),
    )
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[lot],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_LTCG_OUT_OF_FY_MARKER in markers


def test_writer_total_sums_realized_gain_with_half_up_rounding(tmp_path: Path) -> None:
    lines = [
        _make_ltcg_line(
            proceeds_usd=Decimal("1100.45"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("1000.00"),
            proceeds_inr=Decimal("1100.45"),
            realized_gain_inr=Decimal("100.45"),
        ),
        _make_ltcg_line(
            symbol=Ticker("XQCA"),
            isin=Isin("US99972B1017"),
            proceeds_usd=Decimal("1200.55"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("1000.00"),
            proceeds_inr=Decimal("1200.55"),
            realized_gain_inr=Decimal("200.55"),
        ),
    ]
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=lines,
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG
    # 100.45 + 200.55 = 301.00 → 301
    assert rows[-1][-1] == "301"
    # Audit-TOTAL value is in LAST cell (matches STCG writer post-T4 fix)
    audit_total_row = rows[-2]
    assert audit_total_row[0] == _AUDIT_TOTAL_LABEL
    assert audit_total_row[13] == ""
    assert audit_total_row[14] == ""
    assert audit_total_row[15] == "301.00"


def test_writer_total_handles_net_loss(tmp_path: Path) -> None:
    lines = [
        _make_ltcg_line(
            proceeds_usd=Decimal("1500.00"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("3000.00"),
            proceeds_inr=Decimal("1500.00"),
            realized_gain_inr=Decimal("-1500.00"),
        )
    ]
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=lines,
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    assert rows[-1][-1] == "'-1500"
    assert rows[1][-1] == "'-1500"


def test_writer_uses_utf8_bom(tmp_path: Path) -> None:
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(_empty_result(), out)
    assert out.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_writer_uniform_column_count(tmp_path: Path) -> None:
    line = _make_ltcg_line()
    lot_stcg = _make_closed_lot(
        open_date_time=date(2025, 1, 15),
        close_date_time=date(2025, 6, 20),
    )
    lot_pre = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 7, 22),
    )
    lot_in = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        open_date_time=date(2023, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    lot_oof = _make_closed_lot(
        open_date_time=date(2020, 1, 1),
        close_date_time=date(2024, 12, 1),
    )
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[line],
        excluded_stcg=[DeferredStcgRow(closed_lot=lot_stcg, holding_period_days=156)],
        excluded_pre_2024_07_23=[PreCutoverLtcgRow(closed_lot=lot_pre, holding_period_days=1664)],
        excluded_indian_source=[lot_in],
        excluded_out_of_fy=[lot_oof],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    for r in rows:
        assert len(r) == 16, f"row had {len(r)} cells: {r!r}"
    # Both TOTAL amounts sit under the last header (#741).
    assert rows[0][15] == "Realized gain INR (rounded ₹)"
    assert rows[-2][15] == str(result.portal_amounts.gain_inr_full_precision)
    assert rows[-1][15] == str(result.portal_amounts.filed_total_inr)


def test_writer_escapes_formula_trigger_in_description(tmp_path: Path) -> None:
    lot = _make_closed_lot(description="=cmd|'/c calc'!A0")
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1664)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    flat = " ".join(c for r in _read_rows(out) for c in r)
    assert "'=cmd|'/c calc'!A0" in flat


def test_writer_rejects_nul_in_description(tmp_path: Path) -> None:
    lot = _make_closed_lot(description="SAMPLE ISSUER CA\x00CORP")
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[PreCutoverLtcgRow(closed_lot=lot, holding_period_days=1664)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    with pytest.raises(WriterContractError):
        writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    assert not out.exists()


def test_writer_escapes_negative_proceeds_in_stcg_detail_row(tmp_path: Path) -> None:
    """T5 r5 fix mirror — LTCG writer also escapes negative proceeds.

    A second, positive line keeps the schedule's total consideration
    positive: a negative total is refused (#615 owner decision 2).
    """
    line = _make_ltcg_line(
        cost_basis_usd=Decimal("100"),
        proceeds_usd=Decimal("-50"),
        cost_basis_inr=Decimal("100") * Decimal("86.20"),
        proceeds_inr=Decimal("-50") * Decimal("86.20"),
        realized_gain_inr=(Decimal("-50") - Decimal("100")) * Decimal("86.20"),
    )
    result = ScheduleCgLtcgComputeResult(
        ltcg_lines=[line, _make_ltcg_line()],
        excluded_stcg=[],
        excluded_pre_2024_07_23=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg-ltcg.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    detail = rows[1]
    # proceeds_usd (idx 8): apostrophe-escaped
    assert detail[8] == "'-50"
    # proceeds_inr (idx 13): apostrophe-escaped
    assert detail[13].startswith("'-")


# ---- per-AY portal labels threading (issue #65) ---------------------------


def test_writer_with_registered_ay_uses_snapshot_labels(tmp_path: Path) -> None:
    """Threading a registered AY sources portal labels from that AY's
    snapshot (issue #65)."""
    from ibkr_fa.output.portal_labels import for_ay  # noqa: PLC0415

    snap = for_ay(2026)
    out = tmp_path / "ay2026.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")(assessment_year_start=2026).write(
        _empty_result(), out
    )
    rows = _read_rows(out)
    first_cells = [r[0] for r in rows if r]
    assert snap.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG in first_cells


def test_itr3_writer_uses_b9e_destination(tmp_path: Path) -> None:
    out = tmp_path / "itr3.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")(
        assessment_year_start=2026, itr_form="ITR-3"
    ).write(_empty_result(), out)
    assert _read_rows(out)[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM["ITR-3"]


def test_writer_with_forward_compat_ay_falls_back_silently(tmp_path: Path) -> None:
    """+1 forward-compat AY silently falls back to the latest snapshot."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        for_ay,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    snap = for_ay(latest)
    out = tmp_path / "forward.csv"
    writer_class(Subcommand.CG_LTCG, "review-csv")(assessment_year_start=latest + 1).write(
        _empty_result(), out
    )
    rows = _read_rows(out)
    first_cells = [r[0] for r in rows if r]
    assert snap.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG in first_cells


def test_writer_with_unknown_ay_raises_at_construction() -> None:
    """AY two beyond latest registered raises at construction."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        UnknownAssessmentYearError,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    with pytest.raises(UnknownAssessmentYearError):
        writer_class(Subcommand.CG_LTCG, "review-csv")(assessment_year_start=latest + 2)
