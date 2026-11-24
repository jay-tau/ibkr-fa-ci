"""Tests for :mod:`ibkr_fa.output.review_csv`."""

from __future__ import annotations

import csv
import os
import stat
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import FaA3Row
from ibkr_fa.output import (
    Subcommand,
    available_formats,
)
from ibkr_fa.output.portal_labels import PORTAL_COLUMN_HEADERS_FA_A3
from ibkr_fa.output.review_csv import ReviewCsvWriter
from ibkr_fa.output.schedules.fa import A3_UPLOAD_HEADERS
from tests._fa_a3_name_runs import A3_ASCII_ERROR
from tests._fa_a3_rows import fa_filing_csv_writer as _filing_writer
from tests._fa_a3_rows import make_fa_a3_upload_row as make_row
from tests._writer_stubs import writer_class


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read ``path`` and return ``(header, data_rows)``."""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    return rows[0], rows[1:]


# ---- registration ----------------------------------------------------------


def test_writer_registered_with_review_csv_format_id() -> None:
    """The @register decorator must put ReviewCsvWriter under its key,
    listed as 'review-csv'. The CLI's --format flag will use this id."""
    assert "review-csv" in available_formats(Subcommand.FA)
    assert writer_class(Subcommand.FA, "review-csv") is ReviewCsvWriter


def test_filing_csv_matches_live_uploader_shape(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa.csv"
    row = make_row(
        address="100 Main Street,Anytown,CA",
        peak=Decimal("80000"),
        gross_paid=Decimal("0"),
        gross_proceeds=Decimal("0"),
    )
    _filing_writer().write([row], out)
    header, data_rows = read_csv(out)

    assert header == list(A3_UPLOAD_HEADERS)
    assert len(header) == 12
    assert data_rows == [
        [
            "1",
            "2",
            "SAMPLE ISSUER AW INC",
            "100 Main Street Anytown CA",
            "",
            "Listed Company",
            "2024-06-15",
            "32269",
            "80000",
            "41693",
            "0",
            "0",
        ]
    ]
    lines = out.read_text(encoding="utf-8").splitlines()
    assert all(line.count(",") == 11 for line in lines)
    assert '"' not in lines[1]


def test_filing_csv_collapses_text_its_row_accepted_at_the_raw_limit(tmp_path: Path) -> None:
    """The row's limit counts raw characters; the CSV still collapses
    whitespace runs in a name of exactly 125 (#1189)."""
    out = tmp_path / "schedule-fa.csv"
    _filing_writer().write([make_row(name_of_entity="B" + " " * 123 + "C")], out)
    assert read_csv(out)[1][0][2] == "B C"


@pytest.mark.parametrize(
    ("length", "message"),
    [
        (126, "InvariantError: FaA3Row.name_of_entity must be at most 125 characters"),
        (125, A3_ASCII_ERROR),
    ],
    ids=["over-the-limit", "within-the-limit"],
)
def test_filing_csv_long_non_ascii_name_refused_by_its_row_first(
    tmp_path: Path, length: int, message: str
) -> None:
    """A non-ASCII name over the limit is refused when its row is built (#1189),
    before the writer's ASCII check, which still refuses one within it."""
    out = tmp_path / "schedule-fa.csv"
    with pytest.raises(InvariantError) as exc:
        _filing_writer().write([make_row(name_of_entity="É" * length)], out)
    assert str(exc.value) == message
    assert not out.exists()


def test_filing_csv_rejects_amount_above_cbdt_limit(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="14-digit bound"):
        _filing_writer().write(
            [make_row(initial=Decimal("100000000000000"))], tmp_path / "schedule-fa.csv"
        )


@pytest.mark.parametrize("accepting_form", [None, "ITR-2"], ids=["default", "itr-2"])
def test_filing_csv_enforces_selected_forms_country_list(
    tmp_path: Path, accepting_form: str | None
) -> None:
    """ITR-3's dropdown lacks this code; ITR-2's, the default, has it."""
    row = make_row(country_code="263", country_name="Zimbabwe", iso2="ZW")
    _filing_writer(accepting_form).write([row], tmp_path / "itr2.csv")

    with pytest.raises(InvariantError, match="ITR-3 Schedule FA country dropdown"):
        _filing_writer("ITR-3").write([row], tmp_path / "itr3.csv")


def test_format_id_is_kebab_case() -> None:
    """Sanity: matches the lowercase-kebab-case rule enforced by
    the registry (PR #88)."""
    assert ReviewCsvWriter.format_id == "review-csv"


# ---- header structure ------------------------------------------------------


def test_writes_header_with_portal_columns_first_then_audit(tmp_path: Path) -> None:
    out = tmp_path / "review.csv"
    ReviewCsvWriter().write([], out)
    header, _ = read_csv(out)
    # First 11 columns: portal labels in exact portal order.
    assert tuple(header[:11]) == PORTAL_COLUMN_HEADERS_FA_A3
    # Remaining columns: all audit-prefixed.
    assert all(h.startswith("_audit: ") for h in header[11:])
    # 11 portal + 9 audit = 20 columns total.
    assert len(header) == 20


def test_writes_header_even_when_no_rows(tmp_path: Path) -> None:
    """Empty input still produces a usable header — the user sees
    column names even if the parser found nothing."""
    out = tmp_path / "empty.csv"
    ReviewCsvWriter().write([], out)
    header, data = read_csv(out)
    assert len(header) == 20
    assert data == []


# ---- portal cell formatting ------------------------------------------------


def test_open_lot_row_portal_cells(tmp_path: Path) -> None:
    out = tmp_path / "open.csv"
    ReviewCsvWriter().write([make_row()], out)
    _, [data_row] = read_csv(out)
    # Country cell becomes "2-UNITED STATES OF AMERICA"
    assert data_row[0] == "2-UNITED STATES OF AMERICA"
    assert data_row[1] == "SAMPLE ISSUER AW INC"
    assert data_row[2] == "United States of America"
    assert data_row[3] == ""  # zip
    assert data_row[4] == "Listed Company"
    assert data_row[5] == "15/06/2024"
    # Initial value: ROUND_HALF_UP to whole rupees.
    # 32268.575 -> 32269
    assert data_row[6] == "32269"
    # Peak value: None -> empty cell (v1 incomplete)
    assert data_row[7] == ""
    # Closing balance: 41692.50 -> 41693 (half-up)
    assert data_row[8] == "41693"
    # Gross amount paid: None -> empty cell
    assert data_row[9] == ""
    # Gross proceeds: None -> empty cell
    assert data_row[10] == ""


def test_closed_lot_row_portal_cells(tmp_path: Path) -> None:
    """Closed lot has closing_value_inr = Decimal('0'); writer must
    render '0' (NOT empty)."""
    out = tmp_path / "closed.csv"
    row = make_row(
        closing=Decimal("0"),
        close_px_usd=None,
        ttbr_dec31=None,
        disposal=date(2025, 6, 15),
    )
    ReviewCsvWriter().write([row], out)
    _, [data_row] = read_csv(out)
    # Closing balance must be literal "0", not "" — empty is a
    # different cell value (means "v1 doesn't compute"). 0 means
    # "no year-end position".
    assert data_row[8] == "0"


def test_uk_lot_uses_combined_portal_country(tmp_path: Path) -> None:
    """Regression for PR #95 bug fix: GB lot must produce
    '44-UNITED KINGDOM...' (not '1-CANADA' which the old CSV had)."""
    out = tmp_path / "uk.csv"
    row = make_row(
        country_code="44",
        country_name="United Kingdom",
        iso2="GB",
        isin="GB99970J1065",
        ticker="XQCR",
    )
    ReviewCsvWriter().write([row], out)
    _, [data_row] = read_csv(out)
    assert data_row[0] == "44-UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND"


def test_v1_missing_columns_render_as_empty_cells(tmp_path: Path) -> None:
    """Portal columns whose FaA3Row value is None must render as
    empty cells — the empty cell visually screams 'fill me in'
    (user-confirmed decision). Gross proceeds is ``None`` when a
    disposed lot's source does not prove pre-fee consideration."""
    out = tmp_path / "missing.csv"
    ReviewCsvWriter().write([make_row()], out)
    _, [data_row] = read_csv(out)
    # Indexes 7 (peak), 9 (gross paid), 10 (gross proceeds).
    assert data_row[7] == ""
    assert data_row[9] == ""
    assert data_row[10] == ""


def test_dates_use_dd_mm_yyyy_in_portal_column(tmp_path: Path) -> None:
    """Single-digit day/month must zero-pad."""
    out = tmp_path / "date.csv"
    ReviewCsvWriter().write([make_row(date_acq=date(2025, 1, 5))], out)
    _, [data_row] = read_csv(out)
    assert data_row[5] == "05/01/2025"


# ---- audit cell formatting -------------------------------------------------


def test_open_lot_audit_cells(tmp_path: Path) -> None:
    out = tmp_path / "audit_open.csv"
    ReviewCsvWriter().write([make_row()], out)
    header, [data_row] = read_csv(out)
    audit = dict(zip(header[11:], data_row[11:], strict=True))
    assert audit["_audit: Source ISIN"] == "US99990E1038"
    assert audit["_audit: Source ticker"] == "XQAW"
    assert audit["_audit: Source country (ISO 3166-1 alpha-2)"] == "US"
    # Cost basis preserves full Decimal precision (NOT rounded).
    assert audit["_audit: Cost basis (USD)"] == "386.45"
    # Acquisition date is ISO-8601 (distinct from DD/MM/YYYY portal format).
    assert audit["_audit: Acquisition date"] == "2024-06-15"
    assert audit["_audit: Closing price on Dec 31 (USD)"] == "32.70"
    assert audit["_audit: TTBR on acquisition date (INR per USD)"] == "83.50"
    assert audit["_audit: TTBR on Dec 31 (INR per USD)"] == "85.00"
    # Open lot has no disposal date.
    assert audit["_audit: Disposal date"] == ""


def test_closed_lot_audit_cells(tmp_path: Path) -> None:
    out = tmp_path / "audit_closed.csv"
    row = make_row(
        # PR #122 (correctness invariants): disposed lots must have
        # closing_value_inr == 0 and both Dec-31 audit fields None,
        # else FaA3Row.__post_init__ rejects the construction.
        closing=Decimal("0"),
        close_px_usd=None,
        ttbr_dec31=None,
        disposal=date(2025, 6, 15),
    )
    ReviewCsvWriter().write([row], out)
    header, [data_row] = read_csv(out)
    audit = dict(zip(header[11:], data_row[11:], strict=True))
    # Closed lot: no close price, no year-end TTBR; has disposal date.
    assert audit["_audit: Closing price on Dec 31 (USD)"] == ""
    assert audit["_audit: TTBR on Dec 31 (INR per USD)"] == ""
    assert audit["_audit: Disposal date"] == "2025-06-15"


def test_closed_lot_income_audits_dec31_ttbr(tmp_path: Path) -> None:
    out = tmp_path / "audit_closed_income.csv"
    row = make_row(
        closing=Decimal("0"),
        gross_paid=Decimal("85"),
        close_px_usd=None,
        ttbr_dec31=Decimal("85"),
        disposal=date(2025, 6, 15),
    )
    ReviewCsvWriter().write([row], out)
    header, [data_row] = read_csv(out)
    audit = dict(zip(header[11:], data_row[11:], strict=True))
    assert audit["_audit: TTBR on Dec 31 (INR per USD)"] == "85"


def test_audit_preserves_full_decimal_precision_no_quantization(
    tmp_path: Path,
) -> None:
    """Audit columns are for verification — they MUST NOT round.
    Spot-check with an awkward Decimal that wouldn't survive
    half-up quantization."""
    out = tmp_path / "precision.csv"
    row = make_row(
        cost_usd=Decimal("12345.67891234"),
        ttbr_acq=Decimal("83.4321"),
    )
    ReviewCsvWriter().write([row], out)
    header, [data_row] = read_csv(out)
    audit = dict(zip(header[11:], data_row[11:], strict=True))
    assert audit["_audit: Cost basis (USD)"] == "12345.67891234"
    assert audit["_audit: TTBR on acquisition date (INR per USD)"] == "83.4321"


# ---- file behaviour --------------------------------------------------------


def test_overwrites_existing_file(tmp_path: Path) -> None:
    """Per the FaA3Writer Protocol contract, write() overwrites
    any pre-existing file at ``path``."""
    out = tmp_path / "out.csv"
    out.write_text("stale content that must be replaced\n", encoding="utf-8")
    if os.name == "posix":
        out.chmod(0o644)
    ReviewCsvWriter().write([make_row()], out)
    text = out.read_text(encoding="utf-8")
    assert "stale" not in text
    assert "SAMPLE ISSUER AW INC" in text
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [out]


def test_does_not_create_parent_directories(tmp_path: Path) -> None:
    """Per the FaA3Writer Protocol contract, write() does NOT
    auto-create parent dirs — fails loud on a typo'd path so the
    user notices."""
    out = tmp_path / "nonexistent_subdir" / "review.csv"
    with pytest.raises(OSError):
        ReviewCsvWriter().write([], out)


def test_accepts_iterable_input_not_just_list(tmp_path: Path) -> None:
    """Per the FaA3Writer Protocol contract, rows is Iterable —
    callers may pass a generator. The writer materialises exactly
    once internally."""
    out = tmp_path / "iter.csv"

    def gen() -> Iterator[FaA3Row]:
        yield make_row(ticker="A")
        yield make_row(ticker="B")
        yield make_row(ticker="C")

    ReviewCsvWriter().write(gen(), out)
    _, data = read_csv(out)
    audit_ticker_col_index = 11 + 1  # portal cols + Source ISIN col
    assert [r[audit_ticker_col_index] for r in data] == ["A", "B", "C"]


def test_preserves_row_order(tmp_path: Path) -> None:
    """Input order must round-trip through to the CSV."""
    out = tmp_path / "order.csv"
    rows = [make_row(ticker=t) for t in ("ZZZ", "AAA", "XQDE")]
    ReviewCsvWriter().write(rows, out)
    _, data = read_csv(out)
    # Portal column 1 (Name of entity) is the same default in every
    # make_row, so check the audit ticker column instead.
    audit_ticker_col_index = 12
    assert [r[audit_ticker_col_index] for r in data] == ["ZZZ", "AAA", "XQDE"]


def test_quotes_cells_containing_commas(tmp_path: Path) -> None:
    """Free-text portal cells (Name of entity, Address) may contain
    commas. csv.writer's default quoting handles this — but verify
    end-to-end that the resulting CSV round-trips correctly."""
    out = tmp_path / "commas.csv"
    row = make_row(name_of_entity="SAMPLE ISSUER AW INC, CLASS A")
    ReviewCsvWriter().write([row], out)
    _, [data_row] = read_csv(out)
    # Name field must come back intact, comma and all.
    assert data_row[1] == "SAMPLE ISSUER AW INC, CLASS A"


# ---- integration regression ------------------------------------------------


def test_round_trip_through_output_binding(tmp_path: Path) -> None:
    """End-to-end via the registry: CLI will look the writer up by
    format_id and instantiate it — verify that path works."""
    out = tmp_path / "registry.csv"
    writer_cls = writer_class(Subcommand.FA, "review-csv")
    writer_cls().write([make_row()], out)
    header, [data_row] = read_csv(out)
    assert tuple(header[:11]) == PORTAL_COLUMN_HEADERS_FA_A3
    assert data_row[0] == "2-UNITED STATES OF AMERICA"


# ---- per-AY portal labels threading (issue #65) ---------------------------


def test_writer_with_registered_ay_writes_snapshot_headers(tmp_path: Path) -> None:
    """Threading an explicit registered AY into the writer sources
    portal-column headers from that AY's snapshot (issue #65)."""
    from ibkr_fa.output.portal_labels import for_ay  # noqa: PLC0415

    out = tmp_path / "ay2026.csv"
    ReviewCsvWriter(assessment_year_start=2026).write([], out)
    header, _ = read_csv(out)
    expected = for_ay(2026).PORTAL_COLUMN_HEADERS_FA_A3
    assert tuple(header[:11]) == tuple(expected)


def test_writer_with_forward_compat_ay_falls_back_silently(tmp_path: Path) -> None:
    """A +1 forward-compat AY (one beyond the latest registered
    snapshot) silently falls back to the latest snapshot's labels —
    the writer does NOT raise. The CLI is responsible for surfacing
    a separate "labels not yet verified" advisory."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        for_ay,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    out = tmp_path / "forward.csv"
    ReviewCsvWriter(assessment_year_start=latest + 1).write([], out)
    header, _ = read_csv(out)
    # Fallback returns the LATEST snapshot's labels, so the headers
    # match the latest registered AY, not a stub or empty value.
    assert tuple(header[:11]) == tuple(for_ay(latest).PORTAL_COLUMN_HEADERS_FA_A3)


def test_writer_with_unknown_ay_raises_at_construction(tmp_path: Path) -> None:
    """An AY two beyond the latest registered snapshot is OUT of
    forward-compat range; the writer raises
    :class:`UnknownAssessmentYearError` at construction time so the
    failure is loud (rather than silently writing labels from the
    wrong AY)."""
    from ibkr_fa.output.portal_labels import (  # noqa: PLC0415
        _SNAPSHOTS_BY_AY_START,
        UnknownAssessmentYearError,
    )

    latest = max(_SNAPSHOTS_BY_AY_START)
    with pytest.raises(UnknownAssessmentYearError):
        ReviewCsvWriter(assessment_year_start=latest + 2)


def test_writer_without_ay_uses_default_labels(tmp_path: Path) -> None:
    """Backward-compat: callers that don't pass
    ``assessment_year_start`` get the package-level (CURRENT_AY)
    labels — preserves pre-#65 behaviour for direct callers (tests
    and downstream tooling)."""
    out = tmp_path / "default.csv"
    ReviewCsvWriter().write([], out)
    header, _ = read_csv(out)
    # PORTAL_COLUMN_HEADERS_FA_A3 imported at module top is the
    # current-AY re-export, so an unthreaded writer must match.
    assert tuple(header[:11]) == PORTAL_COLUMN_HEADERS_FA_A3


def test_formula_injection_in_user_supplied_address_rejected(tmp_path: Path) -> None:
    """A [[fa.entities]] address/name/zip that starts with a CSV-formula
    trigger must be rejected by the default A3 writer too (parity with the A3
    xlsx writer and every sibling review-csv writer)."""
    from ibkr_fa.output._writer_contract import WriterContractError  # noqa: PLC0415

    out = tmp_path / "fa.csv"
    out.write_bytes(b"existing private output")
    bad = make_row(address='=HYPERLINK("http://evil","clickme")')
    with pytest.raises(WriterContractError):
        ReviewCsvWriter().write([bad], out)
    assert out.read_bytes() == b"existing private output"
    assert list(tmp_path.iterdir()) == [out]
