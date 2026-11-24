"""Tests for :mod:`ibkr_fa.output.xlsx` (issue #42 — XLSX writer).

Mirrors the row-shape and per-AY threading tests in
``test_output_review_csv.py``, plus XLSX-specific concerns:

* numeric / datetime cells stay typed (not stringified)
* header row is bold + frozen
* writer-contract still rejects NUL + formula-injection triggers
* the file is a valid XLSX (round-trips through openpyxl on read)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output import (
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output._xlsx_contract import (
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _decimal_to_float,
    _whole_rupee_cell,
)
from ibkr_fa.output.portal_labels import PORTAL_COLUMN_HEADERS_FA_A3
from ibkr_fa.output.xlsx import XlsxWriter
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._cbdt_fragment_inputs import unchecked_row as _unchecked


def make_row(
    *,
    country_code: str = "2",
    country_name: str = "United States of America",
    iso2: str = "US",
    name_of_entity: str = "SAMPLE ISSUER AW INC",
    address: str = "United States of America",
    zip_code: str = "",
    nature: str = "Listed Company",
    date_acq: date = date(2024, 6, 15),
    initial: Decimal = Decimal("32268.575"),  # 386.45 * 83.50
    peak: Decimal | None = None,
    closing: Decimal = Decimal("41692.50"),  # 15 * 32.70 * 85.00
    gross_paid: Decimal | None = None,
    gross_proceeds: Decimal | None = None,
    isin: str = "US99990E1038",
    ticker: str = "XQAW",
    cost_usd: Decimal = Decimal("386.45"),
    close_px_usd: Decimal | None = Decimal("32.70"),
    ttbr_acq: Decimal = Decimal("83.50"),
    ttbr_dec31: Decimal | None = Decimal("85.00"),
    disposal: date | None = None,
) -> FaA3Row:
    return FaA3Row(
        country_name=country_name,
        country_code=country_code,
        name_of_entity=name_of_entity,
        address_of_entity=address,
        zip_code=zip_code,
        nature_of_entity=nature,
        date_of_acquiring=date_acq,
        initial_value_inr=initial,
        peak_value_inr=peak,
        closing_value_inr=closing,
        gross_amount_paid_credited_inr=gross_paid,
        gross_proceeds_from_sale_inr=gross_proceeds,
        source_isin=Isin(isin),
        source_ticker=Ticker(ticker),
        source_country_iso2=CountryCode(iso2),
        audit_cost_basis_usd=cost_usd,
        audit_acquisition_date=date_acq,
        audit_close_price_usd=close_px_usd,
        audit_ttbr_acquisition=ttbr_acq,
        audit_ttbr_dec31=ttbr_dec31,
        audit_disposal_date=disposal,
        peak_blank_reason=FaA3PeakBlankReason.PRICES_NOT_FETCHED if peak is None else None,
    )


def _read_xlsx(path: Path) -> tuple[list[object], list[list[object]]]:
    """Return ``(header_cells, data_rows)`` from the first sheet."""
    wb = load_workbook(path, data_only=False)
    ws = wb.active
    assert ws is not None
    rows = [[cell.value for cell in row] for row in ws.iter_rows()]
    return rows[0], rows[1:]


# ---- registration ---------------------------------------------------------


def test_writer_registered_with_xlsx_format_id() -> None:
    assert "xlsx" in available_formats(Subcommand.FA)
    assert output_binding(Subcommand.FA, "xlsx").target is XlsxWriter


def test_format_id_is_kebab_case() -> None:
    assert XlsxWriter.format_id == "xlsx"


# ---- structural contract --------------------------------------------------


def test_writes_header_with_portal_columns_first_then_audit(tmp_path: Path) -> None:
    out = tmp_path / "review.xlsx"
    XlsxWriter().write([], out)
    header, _ = _read_xlsx(out)
    assert tuple(header[:11]) == PORTAL_COLUMN_HEADERS_FA_A3
    assert all(isinstance(h, str) and h.startswith("_audit: ") for h in header[11:])
    assert len(header) == 20


def test_sheet_named_table_a3(tmp_path: Path) -> None:
    """Forward-compat with multi-table FA: each table gets its own sheet."""
    out = tmp_path / "review.xlsx"
    XlsxWriter().write([], out)
    wb = load_workbook(str(out))
    assert wb.sheetnames == ["Table A3"]


def test_header_row_is_bold_and_frozen(tmp_path: Path) -> None:
    """Bold + frozen header is what makes the XLSX better than the CSV."""
    out = tmp_path / "review.xlsx"
    XlsxWriter().write([make_row()], out)
    wb = load_workbook(str(out))
    ws = wb.active
    assert ws is not None
    assert ws.cell(row=1, column=1).font.bold is True
    assert ws.freeze_panes == "A2"


def test_writes_header_even_when_no_rows(tmp_path: Path) -> None:
    out = tmp_path / "empty.xlsx"
    XlsxWriter().write([], out)
    header, data = _read_xlsx(out)
    assert len(header) == 20
    assert data == []


# ---- numeric / datetime cells stay typed ----------------------------------


def test_portal_inr_amounts_written_as_integers(tmp_path: Path) -> None:
    """The row's filed whole-rupee values, written as ints so spreadsheet
    SUM() works directly."""
    out = tmp_path / "row.xlsx"
    XlsxWriter().write([make_row()], out)
    _, data = _read_xlsx(out)
    initial_value = data[0][6]  # column 7 (1-indexed): Initial value
    closing_value = data[0][8]  # column 9: Closing balance
    assert isinstance(initial_value, int)
    assert isinstance(closing_value, int)
    # CBDT HALF_UP: 32268.575 -> 32269
    assert initial_value == 32269
    # 41692.50 -> 41693 (HALF_UP on x.5)
    assert closing_value == 41693


def test_whole_rupee_cell_renders_a_whole_rupee_amount_as_an_int() -> None:
    cell = _whole_rupee_cell(Decimal("64218"), field="Schedule FA A3 initial value")
    assert cell == 64218
    assert type(cell) is int
    assert _whole_rupee_cell(None, field="Schedule FA A3 peak value") is None


def test_whole_rupee_cell_refuses_a_fraction_without_printing_it() -> None:
    """The helper never rounds: a fraction is refused, and neither the value
    nor its rounded digits appear in the refusal."""
    with pytest.raises(InvariantError) as exc:
        _whole_rupee_cell(Decimal("13579.25"), field="Schedule FA A2 closing balance")
    assert str(exc.value) == (
        "InvariantError: Schedule FA A2 closing balance must be a whole-rupee amount "
        "(no fraction, exponent or -0) (context keys: value)"
    )
    assert "13579" not in str(exc.value)


@pytest.mark.parametrize("value", ["5E+2", "-0", "500.00", "0.4"])
def test_whole_rupee_cell_refuses_what_require_whole_rupees_refuses(value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        _whole_rupee_cell(Decimal(value), field="Schedule FA A2 peak balance")
    assert exc.value.what == (
        "Schedule FA A2 peak balance must be a whole-rupee amount (no fraction, exponent or -0)"
    )


def test_portal_date_written_as_datetime(tmp_path: Path) -> None:
    out = tmp_path / "row.xlsx"
    XlsxWriter().write([make_row(date_acq=date(2024, 6, 15))], out)
    _, data = _read_xlsx(out)
    cell_value = data[0][5]
    # openpyxl returns datetime.datetime for date cells.
    assert isinstance(cell_value, datetime)
    assert cell_value.year == 2024
    assert cell_value.month == 6
    assert cell_value.day == 15


def test_audit_decimals_written_as_floats(tmp_path: Path) -> None:
    """Audit columns preserve numeric typing (not strings) so the
    user can subtract / divide in Excel."""
    out = tmp_path / "row.xlsx"
    XlsxWriter().write([make_row(cost_usd=Decimal("386.45"), ttbr_acq=Decimal("83.50"))], out)
    _, data = _read_xlsx(out)
    cost_usd = data[0][14]  # column 15: _audit: Cost basis (USD)
    ttbr_acq = data[0][17]  # column 18: _audit: TTBR on acquisition date
    assert isinstance(cost_usd, float)
    assert isinstance(ttbr_acq, float)
    assert cost_usd == pytest.approx(386.45)
    assert ttbr_acq == pytest.approx(83.50)


def test_unfilled_portal_amounts_are_blank(tmp_path: Path) -> None:
    """peak_value_inr / gross_amount_paid_credited_inr / gross_proceeds
    may be None; they must render as blank cells (None in openpyxl)."""
    out = tmp_path / "blank.xlsx"
    XlsxWriter().write([make_row(peak=None, gross_paid=None, gross_proceeds=None)], out)
    _, data = _read_xlsx(out)
    assert data[0][7] is None  # Peak value
    assert data[0][9] is None  # Gross amount paid
    assert data[0][10] is None  # Gross proceeds


def test_closed_lot_close_price_and_ttbr_dec31_left_blank(tmp_path: Path) -> None:
    """audit_close_price_usd / audit_ttbr_dec31 are None for
    CLOSED-lot rows without gross credits; that must render as blank cells.
    Closed lots also have closing_value_inr == 0 and disposal_date set."""
    out = tmp_path / "closedlot.xlsx"
    XlsxWriter().write(
        [
            make_row(
                close_px_usd=None,
                ttbr_dec31=None,
                disposal=date(2025, 8, 1),
                closing=Decimal("0"),
            )
        ],
        out,
    )
    _, data = _read_xlsx(out)
    assert data[0][16] is None  # _audit: Closing price on Dec 31 (USD)
    assert data[0][18] is None  # _audit: TTBR on Dec 31


def test_disposal_date_blank_for_open_lot(tmp_path: Path) -> None:
    out = tmp_path / "open.xlsx"
    XlsxWriter().write([make_row(disposal=None)], out)
    _, data = _read_xlsx(out)
    assert data[0][19] is None  # _audit: Disposal date


def test_disposal_date_present_for_closed_lot(tmp_path: Path) -> None:
    out = tmp_path / "closed.xlsx"
    # This disposed lot has no income, so both Dec-31 audit fields are None.
    XlsxWriter().write(
        [
            make_row(
                disposal=date(2025, 11, 20),
                closing=Decimal("0"),
                close_px_usd=None,
                ttbr_dec31=None,
            )
        ],
        out,
    )
    _, data = _read_xlsx(out)
    disposal = data[0][19]
    assert isinstance(disposal, datetime)
    assert disposal.year == 2025
    assert disposal.month == 11
    assert disposal.day == 20


# ---- writer-contract: NUL + formula-injection enforcement -----------------


def test_nul_byte_in_string_cell_raises(tmp_path: Path) -> None:
    """Mirrors the CSV writer's NUL-rejection contract — Excel can
    silently accept binary contamination too."""
    out = tmp_path / "nul.xlsx"
    # The row refuses a control character itself (#1367); this pins the writer.
    bad = _unchecked(make_row(), name_of_entity="EVIL\x00CO")
    with pytest.raises(WriterContractError) as excinfo:
        XlsxWriter().write([bad], out)
    assert excinfo.value.violation == "nul_byte"


def test_nul_byte_cell_index_preserved_across_typed_cells(
    tmp_path: Path,
) -> None:
    """Regression: a NUL in an audit string cell that lives AFTER
    numeric / datetime portal cells must report its ORIGINAL
    worksheet column index — not a compacted string-only position.

    ``source_ticker`` is portal column 11 (0-indexed) in the FA-A3
    layout (11 portal cells + audit ISIN). Earlier portal cells
    include 5 INR integers and 2 dates (non-string), so a naive
    ``string-cells-only`` compaction would report the NUL at
    index 6 instead of 12 and send the user to the wrong column.
    """
    out = tmp_path / "indexed.xlsx"
    bad = make_row(ticker="EVIL\x00")
    with pytest.raises(WriterContractError) as excinfo:
        XlsxWriter().write([bad], out)
    assert excinfo.value.violation == "nul_byte"
    # Portal cols 0-10: country, name, address, zip, nature,
    # date_acq, initial, peak, closing, paid, proceeds.
    # Audit col 11 = source ISIN, col 12 = source ticker.
    assert excinfo.value.cell_index == 12


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\v", "\r", "\n"])
def test_formula_trigger_in_string_cell_raises(tmp_path: Path, trigger: str) -> None:
    """Excel auto-executes formula-prefixed cells; the writer must
    reject the OWASP-extended trigger set on every string cell."""
    out = tmp_path / "formula.xlsx"
    # The row refuses the control-character triggers itself (#1367); this pins
    # the writer.
    bad = _unchecked(make_row(), name_of_entity=f"{trigger}cmd|'/c calc'!A0")
    with pytest.raises(WriterContractError) as excinfo:
        XlsxWriter().write([bad], out)
    assert excinfo.value.violation == "formula_trigger"
    assert excinfo.value.trigger == trigger


@pytest.mark.parametrize("trigger", ["\uff1d", "\uff0b", "\uff0d", "\uff20"])
def test_fullwidth_formula_homoglyphs_rejected(tmp_path: Path, trigger: str) -> None:
    """Full-width Unicode homoglyphs may execute as formulas in some
    locales — same defense as the CSV writer."""
    out = tmp_path / "homoglyph.xlsx"
    bad = make_row(name_of_entity=f'{trigger}HYPERLINK("evil")')
    with pytest.raises(WriterContractError) as excinfo:
        XlsxWriter().write([bad], out)
    assert excinfo.value.violation == "formula_trigger"


def test_leading_whitespace_then_formula_trigger_rejected(tmp_path: Path) -> None:
    """Defense-in-depth: spaces before a formula trigger don't sneak
    past the lstrip-then-check path."""
    out = tmp_path / "padded.xlsx"
    bad = make_row(name_of_entity="   =1+1")
    with pytest.raises(WriterContractError) as excinfo:
        XlsxWriter().write([bad], out)
    assert excinfo.value.violation == "formula_trigger"


# Synthetic values a cell could hold (#1048): an entity name and an account
# number. No refusal may print them.
_HOLDER = "Synthetic Holder Two"
_ACCOUNT = "U1234567"


@pytest.mark.parametrize(
    ("check", "bad_cell", "marker", "expected"),
    [
        (
            _assert_no_xlsx_formula_injection,
            f"={_HOLDER}",
            _HOLDER,
            "WriterContractError: row 4 cell at index 3 starts with formula-trigger character '='",
        ),
        (
            _assert_no_xlsx_formula_injection,
            f" +{_ACCOUNT}",
            _ACCOUNT,
            "WriterContractError: row 4 cell at index 3 starts with formula-trigger character '+'",
        ),
        (
            _assert_no_nul_in_xlsx_row,
            f"{_HOLDER}\x00",
            _HOLDER,
            "WriterContractError: row 4 cell at index 3 contains a NUL byte",
        ),
    ],
    ids=["formula-trigger", "formula-trigger-after-lstrip", "nul-byte"],
)
def test_xlsx_refusal_names_the_row_and_cell_index_not_the_cell(
    check: Callable[..., None], bad_cell: str, marker: str, expected: str
) -> None:
    """#1048: a cell can hold a name or an account number, so the refusal prints
    its position in the whole row and the rule it broke, never its value."""
    with pytest.raises(WriterContractError) as raised:
        check(["ok", date(2025, 12, 31), Decimal("1.5"), bad_cell], row_index=4)
    message = str(raised.value)
    assert message == expected
    assert marker.lower() not in message.lower()
    assert not hasattr(raised.value, "cell_value_excerpt")


def test_xlsx_formula_trigger_set_is_canonical_from_writer_contract() -> None:
    """Regression: the shared XLSX contract module must consume the
    canonical OWASP-extended trigger set from ``_writer_contract``
    rather than re-declaring its own copy. If the CSV writer's set
    is ever extended (e.g., a new Unicode homoglyph), every XLSX
    writer must pick up the new trigger automatically; otherwise an
    attacker could route a payload through an XLSX surface that the
    CSV surface rejects."""
    from ibkr_fa.output import _writer_contract as canonical  # noqa: PLC0415
    from ibkr_fa.output import _xlsx_contract as xlsx_contract  # noqa: PLC0415

    # The xlsx contract module imports the canonical name at module
    # level; assert it is the SAME object (not a copy).
    assert xlsx_contract._FORMULA_TRIGGERS is canonical._FORMULA_TRIGGERS


def test_numeric_cell_starting_with_minus_is_safe(tmp_path: Path) -> None:
    """A numeric cell whose stringified value starts with '-' is a
    NUMBER, not a string, so the formula-injection check (which only
    fires on strings) does NOT reject it. Verified directly on the
    sanitizer helper to avoid synthesising an FaA3Row that violates
    its non-negative-INR invariants."""
    from ibkr_fa.output.xlsx import (  # noqa: PLC0415
        _assert_no_xlsx_formula_injection,
    )

    # Mixed cells: a negative number, a negative float, and a benign
    # string. None of these should trigger the formula-injection
    # rejection.
    _assert_no_xlsx_formula_injection([-1000, -1.5, "OK"], row_index=1)


# ---- per-AY portal labels threading (issue #65 contract) ------------------


def test_constructor_accepts_assessment_year_start(tmp_path: Path) -> None:
    writer = XlsxWriter(assessment_year_start=2026)
    out = tmp_path / "ay.xlsx"
    writer.write([], out)
    header, _ = _read_xlsx(out)
    # Header sourced from the per-AY snapshot — must match the
    # AY-2026-27 surface.
    assert header[0] == "Country Name and Code"


def test_constructor_defaults_to_current_ay_labels(tmp_path: Path) -> None:
    XlsxWriter().write([], tmp_path / "default.xlsx")
    XlsxWriter(assessment_year_start=2026).write([], tmp_path / "ay2026.xlsx")
    default_header, _ = _read_xlsx(tmp_path / "default.xlsx")
    ay2026_header, _ = _read_xlsx(tmp_path / "ay2026.xlsx")
    # AY 2026 IS the current default per #65; the two must be
    # byte-identical at the header level.
    assert default_header == ay2026_header


def test_forward_compat_fallback_for_ay_2027(tmp_path: Path) -> None:
    """+1 forward-compat fallback: AY 2027-28 (no own snapshot)
    returns the latest registered snapshot (AY 2026-27) without
    raising — see :func:`ibkr_fa.output.portal_labels.for_ay`."""
    writer = XlsxWriter(assessment_year_start=2027)
    out = tmp_path / "ay2027.xlsx"
    writer.write([], out)
    header, _ = _read_xlsx(out)
    assert header[0] == "Country Name and Code"


def test_unregistered_ay_plus_two_raises_on_construction() -> None:
    """AY+2 falls outside the +1 forward-compat window; the snapshot
    selector must raise."""
    from ibkr_fa.output.portal_labels import UnknownAssessmentYearError  # noqa: PLC0415

    with pytest.raises(UnknownAssessmentYearError):
        XlsxWriter(assessment_year_start=2028)


# ---- file format sanity ---------------------------------------------------


def test_output_file_is_valid_xlsx(tmp_path: Path) -> None:
    """The file must be a real XLSX: openpyxl.load_workbook() must
    succeed (no manual parsing). Catches accidental text-mode writes
    or stray bytes injected into the workbook."""
    out = tmp_path / "valid.xlsx"
    XlsxWriter().write([make_row(), make_row(ticker="XQCA")], out)
    wb = load_workbook(str(out))
    assert wb.sheetnames == ["Table A3"]
    ws = wb["Table A3"]
    assert ws.max_row == 3  # header + 2 data rows
    assert ws.max_column == 20


def test_overwrite_existing_file(tmp_path: Path) -> None:
    """The writer overwrites — same contract as CSV writer."""
    out = tmp_path / "overwrite.xlsx"
    XlsxWriter().write([make_row()], out)
    first_size = out.stat().st_size
    XlsxWriter().write([], out)
    second_size = out.stat().st_size
    # Empty workbook is smaller than one-data-row workbook.
    assert second_size < first_size


# A row mixing text, a date, an int and Decimals, with ``value`` at index 3.
def _mixed_row(value: Decimal) -> list[object]:
    return ["XQAW", date(2025, 12, 31), 7, value, Decimal("1.5")]


_UNREPRESENTABLE_AT_ROW_5_CELL_3 = (
    "WriterContractError: row 5 cell at index 3 holds a number the workbook cannot store:"
    " it would read back as infinite, empty or zero"
)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1E400"),  # float() is inf
        Decimal("-1E400"),
        Decimal("1.7976931348623157E308"),  # a finite float, stored as text that reads back inf
        Decimal("1E-400"),  # float() is 0.0
        Decimal("-1E-400"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("NaN"),
        Decimal("sNaN"),
    ],
)
def test_decimal_cell_the_workbook_cannot_store_is_refused(value: Decimal) -> None:
    """#832: a Decimal that would be stored as an empty, infinite or zero cell
    refuses, naming its row and its index in the whole row."""
    with pytest.raises(WriterContractError) as raised:
        _decimal_to_float(_mixed_row(value), row_index=5)
    assert (raised.value.violation, raised.value.row_index, raised.value.cell_index) == (
        "unrepresentable_number",
        5,
        3,
    )
    # #1048: the message names the position and the rule, never the number.
    assert str(raised.value) == _UNREPRESENTABLE_AT_ROW_5_CELL_3
    assert not hasattr(raised.value, "cell_value_excerpt")


def test_decimal_cell_refusal_omits_the_number() -> None:
    """#1048: an amount the workbook cannot store is refused without printing it."""
    with pytest.raises(WriterContractError) as raised:
        _decimal_to_float(_mixed_row(Decimal("1234567E400")), row_index=5)
    message = str(raised.value)
    assert message == _UNREPRESENTABLE_AT_ROW_5_CELL_3
    assert "1234567" not in message


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Decimal("0.1"), 0.1),  # ordinary float rounding
        (Decimal("1E-320"), 1e-320),  # subnormal, but not zero
        (Decimal("1E308"), 1e308),
        (Decimal("0"), 0.0),
        (Decimal("-0"), -0.0),
        (Decimal("0E-500"), 0.0),
    ],
)
def test_decimal_cell_the_workbook_can_store_is_converted(value: Decimal, stored: float) -> None:
    assert _decimal_to_float(_mixed_row(value), row_index=5) == [
        "XQAW",
        date(2025, 12, 31),
        7,
        stored,
        1.5,
    ]
