"""Tests for the Schedule FA Table A2 writers."""

from __future__ import annotations

import csv as csvmod
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.fa_a2_row import FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.output import (
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.filing_csv import PortalCsvWriter
from ibkr_fa.output.portal_labels import PORTAL_COLUMN_HEADERS_FA_A2
from ibkr_fa.output.schedules.fa import A2_UPLOAD_HEADERS
from tests._writer_stubs import writer_class


def _row(**overrides: object) -> FaA2Row:
    kwargs: dict[str, Any] = {
        "country_code": "2",
        "country_name": "UNITED STATES OF AMERICA",
        "institution_name": "Interactive Brokers LLC",
        "institution_address": "One Pickwick Plaza, Greenwich, CT",
        "institution_zip": "06830",
        "account_number": "U12345678",
        "status": FaA2Status.BENEFICIAL_OWNER,
        "account_opening_date": date(2024, 1, 1),
        "peak_balance_inr": None,
        "closing_balance_inr": Decimal("11069.08041662"),  # 123.718346 x 89.47
        "nature_of_amount": FaA2NatureOfAmount.NO_AMOUNT,
        "gross_amount_inr": Decimal("0"),
        "base_currency": "USD",
        "audit_closing_balance_base_ccy": Decimal("123.718346"),
        "audit_ttbr_dec31": Decimal("89.47"),
        "audit_account_number_from_config": True,
        "audit_nav_cross_checked": True,
        "audit_gross_components": {},
        "audit_period_end_cash_base_ccy": None,
    }
    kwargs.update(overrides)
    return FaA2Row(**kwargs)


def _filing_writer(itr_form: Any = None) -> PortalCsvWriter:
    writer = output_binding(Subcommand.FA_A2, "filing-csv").writer(RenderContext(2026, itr_form))
    assert isinstance(writer, PortalCsvWriter)
    return writer


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csvmod.reader(fh))


# ---- registry -------------------------------------------------------------


def test_both_formats_registered() -> None:
    assert available_formats(Subcommand.FA_A2) == ["filing-csv", "review-csv", "xlsx"]


# ---- filing-csv -----------------------------------------------------------


def test_filing_csv_exactly_matches_official_template(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    _filing_writer().write([_row(institution_address="One Pickwick Plaza,Greenwich,CT")], out)
    header, data = _read_csv(out)

    assert header == list(A2_UPLOAD_HEADERS)
    assert len(header) == len(data) == 12
    assert data == [
        "UNITED STATES OF AMERICA",
        "2",
        "Interactive Brokers LLC",
        "One Pickwick Plaza Greenwich CT",
        "06830",
        "U12345678",
        "BENEFICIAL_OWNER",
        "2024-01-01",
        "",
        "11069",
        "N",
        "0",
    ]
    assert out.read_text(encoding="utf-8").splitlines()[0].count(",") == 11
    assert out.read_text(encoding="utf-8").splitlines()[1].count(",") == 11
    assert "_audit:" not in out.read_text(encoding="utf-8")
    assert '"' not in out.read_text(encoding="utf-8").splitlines()[1]


def test_filing_csv_sanitizes_country_name_commas(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    _filing_writer().write(
        [_row(country_code="255", country_name="TANZANIA, UNITED REPUBLIC OF")], out
    )
    _, data = _read_csv(out)

    assert data[0] == "TANZANIA UNITED REPUBLIC OF"
    assert out.read_text(encoding="utf-8").splitlines()[1].count(",") == 11


@pytest.mark.parametrize("accepting_form", [None, "ITR-2"], ids=["default", "itr-2"])
def test_filing_csv_enforces_selected_forms_country_list(
    tmp_path: Path, accepting_form: str | None
) -> None:
    """ITR-3's dropdown lacks this code; ITR-2's, the default, has it."""
    row = _row(country_code="967", country_name="YEMEN")
    _filing_writer(accepting_form).write([row], tmp_path / "itr2.csv")

    with pytest.raises(InvariantError, match="ITR-3 Schedule FA country dropdown"):
        _filing_writer("ITR-3").write([row], tmp_path / "itr3.csv")


def test_filing_csv_allows_negative_margin_balance(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    _filing_writer().write(
        [
            _row(
                closing_balance_inr=Decimal("-500.4"),
                audit_closing_balance_base_ccy=Decimal("-5.59"),
            )
        ],
        out,
    )
    _, data = _read_csv(out)

    assert data[9] == "-500"


def test_filing_csv_rejects_amount_above_cbdt_limit(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="14-digit bound"):
        _filing_writer().write(
            [_row(closing_balance_inr=Decimal("100000000000000"))], tmp_path / "a2.csv"
        )


# ---- review-csv -----------------------------------------------------------


def test_csv_header_and_row(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    writer_class(Subcommand.FA_A2, "review-csv")().write([_row()], out)
    header, data = _read_csv(out)
    assert header[: len(PORTAL_COLUMN_HEADERS_FA_A2)] == list(PORTAL_COLUMN_HEADERS_FA_A2)
    assert len(header) == 11 + 17  # 11 portal + 17 audit
    # Shared by every A2 producer; each bound is converted at its own date's
    # TTBR, so the INR pair need not keep the native order (#888).
    assert header[17] == "_audit: Peak lower bound INR (converted at its own date; full precision)"
    assert header[19] == "_audit: Peak upper bound INR (converted at its own date; full precision)"
    assert data[0] == "2-UNITED STATES OF AMERICA"
    assert data[4] == "U12345678"
    assert data[5] == "BENEFICIAL_OWNER"
    assert data[6] == "01/01/2024"  # DD/MM/YYYY
    assert data[7] == ""  # peak = manual-fill
    assert data[8] == "11069"  # closing, HALF_UP whole rupees
    assert data[9] == "No Amount paid/credited"
    assert data[10] == "0"


def test_csv_renders_peak_override_provenance(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    row = _row(
        peak_balance_inr=Decimal("9000"),
        audit_peak_override_source="reviewed workbook",
        audit_peak_override_reason="daily account peak",
    )
    writer_class(Subcommand.FA_A2, "review-csv")().write([row], out)
    header, data = _read_csv(out)
    assert data[header.index("_audit: Peak override source")] == "reviewed workbook"
    assert data[header.index("_audit: Peak override reason")] == "daily account peak"


def test_csv_renders_computed_peak_and_statement_floor_provenance(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.csv"
    row = _row(
        peak_balance_inr=Decimal("9000"),
        audit_peak_balance_lower_inr=Decimal("9000"),
        audit_peak_balance_upper_inr=Decimal("9000"),
        audit_peak_balance_lower_date=date(2025, 6, 1),
        audit_peak_balance_upper_date=date(2025, 6, 1),
        audit_peak_balance_bound="lower",
        audit_peak_balance_base_ccy=Decimal("100"),
        audit_peak_ttbr=Decimal("90"),
        audit_statement_peak_floor_base_ccy=Decimal("95"),
        audit_statement_peak_floor_date=date(2025, 5, 31),
    )
    writer_class(Subcommand.FA_A2, "review-csv")().write([row], out)
    header, data = _read_csv(out)
    peak_usd = "_audit: Selected peak balance (base currency, full precision)"
    assert data[header.index(peak_usd)] == "100"
    assert data[header.index("_audit: TTBR on selected peak date")] == "90"
    assert data[header.index("_audit: Maximum statement account value (base currency)")] == "95"
    assert data[header.index("_audit: Maximum statement account value date")] == "2025-05-31"


def test_csv_negative_closing_renders(tmp_path: Path) -> None:
    """A realistic margined account: both the INR closing and its
    base-currency audit value are negative — both must be `'`-escaped so the
    formula-injection guard doesn't abort the write."""
    out = tmp_path / "a2.csv"
    row = _row(
        closing_balance_inr=Decimal("-500.4"),
        audit_closing_balance_base_ccy=Decimal("-5.59"),
    )
    writer_class(Subcommand.FA_A2, "review-csv")().write([row], out)
    _, data = _read_csv(out)
    assert data[8] == "'-500"  # portal closing, OWASP apostrophe escape (CG convention)
    assert data[12] == "'-5.59"  # audit base-ccy closing, also escaped


def test_csv_manual_fill_gross_blank(tmp_path: Path) -> None:
    out = tmp_path / "a2.csv"
    row = _row(
        nature_of_amount=None,
        gross_amount_inr=None,
        audit_gross_components={"Dividend": Decimal("3.2"), "Interest": Decimal("0.95")},
    )
    writer_class(Subcommand.FA_A2, "review-csv")().write([row], out)
    _, data = _read_csv(out)
    assert data[9] == ""  # nature blank
    assert data[10] == ""  # gross blank
    assert data[16] == "Dividend=3.2; Interest=0.95"  # audit breakdown preserved


def test_csv_formula_injection_rejected(tmp_path: Path) -> None:
    out = tmp_path / "a2.csv"
    with pytest.raises(WriterContractError):
        writer_class(Subcommand.FA_A2, "review-csv")().write(
            [_row(institution_name="=cmd|'/c calc'")], out
        )


# ---- xlsx -----------------------------------------------------------------


def test_xlsx_sheet_and_numeric_cells(tmp_path: Path) -> None:
    out = tmp_path / "schedule-fa-a2.xlsx"
    writer_class(Subcommand.FA_A2, "xlsx")().write([_row()], out)
    wb = load_workbook(filename=out)
    ws = wb.active
    assert ws is not None
    assert ws.title == "Table A2"
    assert ws.freeze_panes == "A2"
    # closing balance (col 9) is a numeric cell, whole rupees.
    assert ws.cell(row=2, column=9).value == 11069
    # peak (col 8) is empty (manual-fill).
    assert ws.cell(row=2, column=8).value is None
    assert ws.cell(row=2, column=6).value == "BENEFICIAL_OWNER"


def test_xlsx_negative_closing(tmp_path: Path) -> None:
    out = tmp_path / "a2.xlsx"
    writer_class(Subcommand.FA_A2, "xlsx")().write(
        [_row(closing_balance_inr=Decimal("-500.4"))], out
    )
    ws = load_workbook(filename=out).active
    assert ws is not None
    assert ws.cell(row=2, column=9).value == -500


def test_xlsx_selected_peak_and_bounds_are_numeric(tmp_path: Path) -> None:
    out = tmp_path / "a2.xlsx"
    row = _row(
        peak_balance_inr=Decimal("100.4"),
        audit_peak_balance_lower_inr=Decimal("100.4"),
        audit_peak_balance_upper_inr=Decimal("120.6"),
        audit_peak_balance_lower_date=date(2024, 5, 1),
        audit_peak_balance_upper_date=date(2024, 6, 1),
        audit_peak_balance_bound="lower",
    )
    writer_class(Subcommand.FA_A2, "xlsx")().write([row], out)
    ws = load_workbook(filename=out).active
    assert ws is not None
    assert ws.cell(row=1, column=18).value == (
        "_audit: Peak lower bound INR (converted at its own date; full precision)"
    )
    assert ws.cell(row=1, column=20).value == (
        "_audit: Peak upper bound INR (converted at its own date; full precision)"
    )
    assert ws.cell(row=2, column=8).value == 100
    assert ws.cell(row=2, column=18).value == 100.4
    assert ws.cell(row=2, column=19).value == datetime(2024, 5, 1)
    assert ws.cell(row=2, column=20).value == 120.6
    assert ws.cell(row=2, column=22).value == "lower"


def test_xlsx_formula_injection_rejected(tmp_path: Path) -> None:
    out = tmp_path / "a2.xlsx"
    with pytest.raises(WriterContractError):
        writer_class(Subcommand.FA_A2, "xlsx")().write(
            [_row(institution_address="=HYPERLINK(1)")], out
        )
