"""Unit tests for Schedule CG review-CSV writer."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

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
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM,
)
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._writer_stubs import writer_class


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


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def _empty_result() -> ScheduleCgStcgComputeResult:
    return ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )


def test_cg_review_csv_registered() -> None:
    assert "review-csv" in available_formats(Subcommand.CG)
    writer_cls = writer_class(Subcommand.CG, "review-csv")
    assert writer_cls().format_id == "review-csv"  # type: ignore[abstract]


def test_writer_emits_header_total_for_empty_result(tmp_path: Path) -> None:
    writer = writer_class(Subcommand.CG, "review-csv")()
    out = tmp_path / "cg.csv"
    writer.write(_empty_result(), out)
    rows = _read_rows(out)
    # header + Table F block (blank row, marker, five bands, note; #1361)
    # + audit-total + portal-total = 11
    assert len(rows) == 11
    assert rows[0][0] == "Symbol"
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    assert rows[-1][-1] == "0"


def test_writer_emits_one_detail_row_per_stcg_line(tmp_path: Path) -> None:
    line = _make_stcg_line()
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[line],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    assert rows[1][0] == "XQAQ"
    assert rows[1][1] == "US99992A1097"
    assert rows[1][2] == "US"
    assert rows[1][3] == "15/01/2025"
    assert rows[1][4] == "20/06/2025"
    assert rows[1][5] == "156"
    assert rows[1][6] == "10"
    # T5 Codex P1 fix: single Rule 115 specified-date + TTBR pair
    # (transfer-event only). Column layout: 9=specified date,
    # 10=SBI TTBR source date (#741), 11=TTBR, 12=basis INR,
    # 13=proceeds INR, 14=basis source, 15=rounded gain INR.
    assert rows[1][9] == "2025-05-31"
    # SBI published on the specified date: no carry-back, blank cell.
    assert rows[1][10] == ""
    assert rows[1][11] == "86.20"
    # cost_basis_inr (idx 12): 1500 * 86.20 = 129300.00
    assert rows[1][12] == "129300.00"
    # proceeds_inr (idx 13): 1800 * 86.20 = 155160.00
    assert rows[1][13] == "155160.00"
    assert rows[1][14] == "test basis"
    # realized gain rounded (idx 15): (1800 - 1500) * 86.20 = 25860 INR
    assert rows[1][15] == "25860"


def test_header_puts_sbi_source_date_after_specified_date(tmp_path: Path) -> None:
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(_empty_result(), out)
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
    """A specified date SBI didn't publish on shows the SBI row the rate came from."""
    line = _make_stcg_line(fx_rate_publication_date=date(2025, 5, 30))
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(
        ScheduleCgStcgComputeResult([line], [], [], []), out
    )
    detail = _read_rows(out)[1]
    assert detail[9:12] == ["2025-05-31", "2025-05-30", "86.20"]


def test_writer_emits_ltcg_deferred_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot()
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[deferred],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_LTCG_DEFERRED_MARKER in markers
    # Find the deferred data row
    idx = markers.index(EXCLUDED_LTCG_DEFERRED_MARKER)
    data = rows[idx + 1]
    assert data[0] == "XQCA"
    # T2 PR #164 review: post-#30 (schedule-cg-ltcg shipped), the
    # follow-up-issue column on the LTCG audit block is empty
    # (was "#30" before LTCG shipped) and the audit reason points
    # users at the schedule-cg-ltcg subcommand.
    assert data[2] == ""
    assert data[7] == "1430"


def test_writer_emits_indian_source_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        description="SAMPLE ISSUER CD LTD",
        open_date_time=date(2025, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[lot],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_CG_INDIAN_SOURCE_MARKER in markers


def test_writer_emits_out_of_fy_audit_block(tmp_path: Path) -> None:
    lot = _make_closed_lot(
        open_date_time=date(2024, 1, 1),
        close_date_time=date(2024, 6, 1),
    )
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[lot],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    markers = [r[0] for r in rows]
    assert EXCLUDED_OUT_OF_FY_MARKER in markers


def test_writer_total_sums_realized_gain_with_half_up_rounding(tmp_path: Path) -> None:
    """TOTAL must sum realized_gain_inr (NOT proceeds), rounded HALF_UP."""
    lines = [
        _make_stcg_line(
            proceeds_usd=Decimal("1100.45"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("1000.00"),
            proceeds_inr=Decimal("1100.45"),
            realized_gain_inr=Decimal("100.45"),
        ),
        _make_stcg_line(
            symbol=Ticker("XQCA"),
            isin=Isin("US99972B1017"),
            proceeds_usd=Decimal("1200.55"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("1000.00"),
            proceeds_inr=Decimal("1200.55"),
            realized_gain_inr=Decimal("200.55"),
        ),
    ]
    result = ScheduleCgStcgComputeResult(
        stcg_lines=lines,
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    # Last row first cell = portal label, last cell = rounded TOTAL
    assert rows[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG
    # 100.45 + 200.55 = 301.00 → 301
    assert rows[-1][-1] == "301"
    # T4 @claude review P2 fix (locked again post-T5 P1 fix): the
    # audit TOTAL row's full-precision value lives in column 15 (last,
    # under "Realized gain INR (rounded ₹)" header), NOT column 13
    # (under "_audit: Proceeds INR"). #741 inserted the SBI TTBR
    # source-date column at idx 10, so the value-bearing column moved
    # from idx 14 to idx 15.
    audit_total_row = rows[-2]
    assert audit_total_row[0] == "_audit: TOTAL Realized gain INR (full precision)"
    assert audit_total_row[13] == ""  # NOT the value-bearing column
    assert audit_total_row[14] == ""
    assert audit_total_row[15] == "301.00"


def test_writer_total_handles_net_loss(tmp_path: Path) -> None:
    """Net losses are valid (carry-forward 8 AYs); negative TOTAL must be
    written with an OWASP single-quote escape to defeat the formula-injection
    guard while remaining human-readable."""
    lines = [
        _make_stcg_line(
            proceeds_usd=Decimal("1500.00"),
            fx_rate_inr_per_usd=Decimal("1"),
            cost_basis_inr=Decimal("3000.00"),
            proceeds_inr=Decimal("1500.00"),
            realized_gain_inr=Decimal("-1500.00"),
        )
    ]
    result = ScheduleCgStcgComputeResult(
        stcg_lines=lines,
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    # Leading apostrophe is the canonical OWASP escape; Excel strips it
    # on display while raw csv.reader returns it literally.
    assert rows[-1][-1] == "'-1500"
    # The per-line detail row also escapes
    assert rows[1][-1] == "'-1500"


def test_writer_uses_utf8_bom(tmp_path: Path) -> None:
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(_empty_result(), out)
    assert out.read_bytes()[:3] == b"\xef\xbb\xbf"


def test_writer_uniform_column_count(tmp_path: Path) -> None:
    line = _make_stcg_line()
    lot_ltcg = _make_closed_lot()
    lot_in = _make_closed_lot(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_code=CountryCode("IN"),
        open_date_time=date(2025, 1, 5),
        close_date_time=date(2025, 6, 10),
    )
    lot_oof = _make_closed_lot(open_date_time=date(2024, 1, 1), close_date_time=date(2024, 6, 1))
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[line],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot_ltcg, holding_period_days=1430)],
        excluded_indian_source=[lot_in],
        excluded_out_of_fy=[lot_oof],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    for r in rows:
        assert len(r) == 16, f"row had {len(r)} cells: {r!r}"
    # Both TOTAL amounts sit under the last header (#741).
    assert rows[0][15] == "Realized gain INR (rounded ₹)"
    assert rows[-2][15] == str(result.portal_amounts.gain_inr_full_precision)
    assert rows[-1][15] == str(result.portal_amounts.filed_total_inr)


def test_writer_rejects_nul_in_description(tmp_path: Path) -> None:
    lot = _make_closed_lot(description="SAMPLE ISSUER CA\x00CORP")
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    with pytest.raises(WriterContractError):
        writer_class(Subcommand.CG, "review-csv")().write(result, out)
    assert not out.exists()


def test_writer_escapes_formula_trigger_in_description(tmp_path: Path) -> None:
    """T1-quality P2 fix: free-text upstream cells (symbol, description)
    that start with a formula-trigger character are escaped with the
    OWASP-canonical single-quote prefix rather than aborting the run.
    The writer-contract guard still fires on bypass attempts (raw NUL
    bytes, etc.) — only formula-trigger escapes are pre-applied at the
    row-builder layer."""
    lot = _make_closed_lot(description="=cmd|'/c calc'!A0")
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    assert out.exists()
    rows = _read_rows(out)
    # The audit row contains the escaped description.
    flat = " ".join(c for r in rows for c in r)
    assert "'=cmd|'/c calc'!A0" in flat


def test_writer_escapes_formula_trigger_in_symbol(tmp_path: Path) -> None:
    """Symbols starting with a formula-trigger character are also escaped.

    IBKR ticker symbols are normally uppercase-alphanumeric, but the
    writer can't trust upstream — the apostrophe escape protects
    against parser bugs or malicious test fixtures.
    """
    lot = _make_closed_lot(symbol=Ticker("-WEIRD"))
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    flat_cells = [c for r in rows for c in r]
    assert any(c == "'-WEIRD" for c in flat_cells), flat_cells


def test_writer_escapes_negative_proceeds_in_audit_block(tmp_path: Path) -> None:
    """T5 r4 (Codex bot) P2 fix: an audit-block lot whose
    ``ClosedLot.proceeds_money`` is negative (the model allows it:
    net proceeds can be negative for a deep loss) must NOT trip the writer-contract's
    formula-injection guard. The audit row builders now route the
    proceeds/basis cells through :func:`~ibkr_fa.output._writer_contract.csv_safe_signed`, same as
    the detail-row realized-gain cell.

    Without the escape, a negative proceeds_money cell starts with
    ``-`` and aborts the entire schedule-cg run even though the
    upstream payload is legitimate.
    """
    lot = _make_closed_lot(
        cost_basis_money=Decimal("100"),
        proceeds_money=Decimal("-50"),
    )
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    flat = [c for r in rows for c in r]
    # The audit-block row's proceeds cell must contain the apostrophe-
    # escaped value, not a raw '-50' that would have triggered the
    # formula-injection guard.
    assert "'-50" in flat


def test_writer_escapes_negative_proceeds_in_stcg_detail_row(tmp_path: Path) -> None:
    """T5 r5 (Codex bot) P2 follow-up: the same negative-proceeds
    escape must apply to the MAIN STCG detail row, not just the
    audit blocks. The pre-fix model invariant ``proceeds_usd >= 0``
    rejected legitimate negative-proceeds STCG lots in the
    compute layer entirely; once that invariant is dropped, the
    writer must escape the negative values to avoid the formula
    guard tripping at write-time. A second, positive line keeps the
    schedule's total consideration positive: a negative total is
    refused (#615 owner decision 2).
    """
    line = _make_stcg_line(
        cost_basis_usd=Decimal("100"),
        proceeds_usd=Decimal("-50"),
        cost_basis_inr=Decimal("100") * Decimal("86.20"),
        proceeds_inr=Decimal("-50") * Decimal("86.20"),
        realized_gain_inr=(Decimal("-50") - Decimal("100")) * Decimal("86.20"),
    )
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[line, _make_stcg_line()],
        excluded_ltcg_deferred=[],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    writer_class(Subcommand.CG, "review-csv")().write(result, out)
    rows = _read_rows(out)
    detail = rows[1]
    # proceeds_usd cell (idx 8): apostrophe-escaped
    assert detail[8] == "'-50"
    # proceeds_inr cell (idx 13): apostrophe-escaped negative
    assert detail[13].startswith("'-")


def test_writer_atomic_no_staging_file_left_on_nul_failure(tmp_path: Path) -> None:
    """NUL-byte guard still fires (not pre-escaped) — atomic-write must
    leave no staging file behind on failure."""
    lot = _make_closed_lot(description="SAMPLE ISSUER CA\x00CORP")
    result = ScheduleCgStcgComputeResult(
        stcg_lines=[],
        excluded_ltcg_deferred=[DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)],
        excluded_indian_source=[],
        excluded_out_of_fy=[],
    )
    out = tmp_path / "cg.csv"
    with pytest.raises(WriterContractError):
        writer_class(Subcommand.CG, "review-csv")().write(result, out)
    # No .tmp staging file should be left behind
    leftovers = list(tmp_path.glob(".cg.csv.*.tmp"))
    assert leftovers == []


# ---- per-AY portal labels threading (issue #65) ---------------------------


def test_writer_with_registered_ay_uses_snapshot_labels(tmp_path: Path) -> None:
    """Threading a registered AY sources portal labels from that AY's
    snapshot (issue #65)."""
    from ibkr_fa.output.portal_labels import for_ay  # noqa: PLC0415

    snap = for_ay(2026)
    out = tmp_path / "ay2026.csv"
    writer_class(Subcommand.CG, "review-csv")(assessment_year_start=2026).write(
        _empty_result(), out
    )
    rows = _read_rows(out)
    first_cells = [r[0] for r in rows if r]
    assert snap.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG in first_cells


def test_itr3_writer_uses_a6g_destination(tmp_path: Path) -> None:
    out = tmp_path / "itr3.csv"
    writer_class(Subcommand.CG, "review-csv")(assessment_year_start=2026, itr_form="ITR-3").write(
        _empty_result(), out
    )
    assert _read_rows(out)[-1][0] == PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM["ITR-3"]


def test_writer_with_forward_compat_ay_falls_back_silently(tmp_path: Path) -> None:
    """+1 forward-compat AY silently falls back to the latest snapshot."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        for_ay,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    snap = for_ay(latest)
    out = tmp_path / "forward.csv"
    writer_class(Subcommand.CG, "review-csv")(assessment_year_start=latest + 1).write(
        _empty_result(), out
    )
    rows = _read_rows(out)
    first_cells = [r[0] for r in rows if r]
    assert snap.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG in first_cells


def test_writer_with_unknown_ay_raises_at_construction() -> None:
    """AY two beyond latest registered raises at construction."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        UnknownAssessmentYearError,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    with pytest.raises(UnknownAssessmentYearError):
        writer_class(Subcommand.CG, "review-csv")(assessment_year_start=latest + 2)
