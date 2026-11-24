"""Tests for the official Form 67 ``IncomeDetails.csv`` upload (#1203: its rows
and warnings live in ``output/schedules/form67.py``)."""

from __future__ import annotations

import csv
import re
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openpyxl import load_workbook

import ibkr_fa.output.schedules.form67 as upload
from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Entry,
    Form67SourceOfIncome,
)
from ibkr_fa.exceptions import ConfigError, InvariantError
from ibkr_fa.models import form67_entry
from ibkr_fa.output import (
    Artifact,
    Destination,
    OutputBinding,
    OutputKey,
    RenderContext,
    Subcommand,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.filing_csv import FORM67_UPLOAD, PortalCsv
from ibkr_fa.output.schedules.form67 import (
    FORM67_UPLOAD_HEADERS,
    form67_upload_rows,
    fractional_rate_warnings,
    portal_import_warnings,
)
from tests._form67_compute_inputs import upload_entry as _entry
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING, fractional_rate_warning
from tests._ftc_warnings import UNPRODUCED_WHAT as _UNPRODUCED_WHAT

_OFFICIAL_TEMPLATE = Path(__file__).parent / "fixtures" / "form67" / "IncomeDetails.csv"


def _read(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def _writer(format_id: str = "portal-csv") -> Any:
    return output_binding(Subcommand.FORM67, format_id).writer(RenderContext(2026))


def test_upload_rendered_as_portal_csv() -> None:
    assert output_binding(Subcommand.FORM67, "portal-csv") == OutputBinding(
        OutputKey(Destination.PORTAL_ENTRY, Artifact.FORM67_CSV),
        PortalCsv(form67_upload_rows, FORM67_UPLOAD),
    )


def test_header_exactly_matches_official_template(tmp_path: Path) -> None:
    with _OFFICIAL_TEMPLATE.open(newline="", encoding="ascii") as handle:
        official_header = next(csv.reader(handle))
    path = tmp_path / "IncomeDetails.csv"
    _writer().write(Form67ComputeResult(entries=()), path)
    assert tuple(official_header) == FORM67_UPLOAD_HEADERS
    assert _read(path) == [official_header]
    assert len(official_header) == 15


def test_output_has_no_bom_or_crlf(tmp_path: Path) -> None:
    path = tmp_path / "IncomeDetails.csv"
    _writer().write(Form67ComputeResult(entries=(_entry(),)), path)
    content = path.read_bytes()
    assert content.startswith(b"Sl. No.")
    assert not content.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in content


def test_us_dividend_maps_to_official_codes_and_columns(tmp_path: Path) -> None:
    path = tmp_path / "IncomeDetails.csv"
    _writer().write(Form67ComputeResult(entries=(_entry(),)), path)
    _, row = _read(path)
    assert row == [
        "1",
        "2",
        "",
        "7",
        "",
        "10001",
        "1501",
        "15.01",
        "3000",
        "",
        "10",
        "25.00",
        "1500",
        "",
        "1500",
    ]


_FILED_AMOUNTS_AND_RATES = (
    "income_inr",
    "foreign_tax_inr",
    "tax_paid_rate_percent",
    "tax_payable_normal_inr",
    "tax_payable_special_inr",
    "section_90_dtaa_rate_percent",
    "section_90_credit_inr",
    "section_91_credit_inr",
    "total_credit_inr",
)


@pytest.mark.parametrize(
    ("format_id", "section"),
    [(fmt, section) for fmt in ("portal-csv", "review-csv", "xlsx") for section in ("90", "91")]
    + [("review-csv", "blank"), ("xlsx", "blank")],
)
def test_writers_render_filed_values_without_rounding(
    tmp_path: Path, filed_without_rounding, format_id: str, section: str
) -> None:
    """#898: raw fields and another rounding pass must not supply portal cells."""
    citation = None if section == "91" else "Article 10(2)(b)"
    entry = _entry(
        article=citation,
        dtaa_rate=None if section == "91" else Decimal("25"),
        normal_tax=None if section == "blank" else Decimal("3000"),
        section_90_credit=Decimal("1500") if section == "90" else None,
        section_91_credit=Decimal("1500") if section == "91" else None,
        total_credit=None if section == "blank" else Decimal("1500"),
    )
    filed = filed_without_rounding(
        entry,
        income_inr=Decimal("10123"),
        foreign_tax_inr=Decimal("1701"),
        tax_paid_rate_percent=Decimal("17.25"),
        tax_payable_normal_inr=None if section == "blank" else Decimal("3101"),
        tax_payable_special_inr=Decimal("2101"),
        section_90_dtaa_article=None if section == "91" else "Article 99(9)",
        section_90_dtaa_rate_percent=None if section == "91" else Decimal("26.25"),
        section_90_credit_inr=Decimal("1601") if section == "90" else None,
        section_91_credit_inr=Decimal("1601") if section == "91" else None,
        total_credit_inr=None if section == "blank" else Decimal("1601"),
    )
    path = tmp_path / ("form67.xlsx" if format_id == "xlsx" else "form67.csv")
    _writer(format_id).write(Form67ComputeResult(entries=(entry,)), path)
    expected = [getattr(filed, name) for name in _FILED_AMOUNTS_AND_RATES]
    # #1079: the portal files the number of the filed citation, which differs
    # from the entry's here; the review files show the entry's full citation.
    if format_id == "portal-csv":
        expected.append(None if section == "91" else "99")
    else:
        expected.append(citation)
    if format_id == "xlsx":
        sheet = load_workbook(path).active
        assert sheet is not None
        detail = list(sheet.values)[1]
    else:
        detail = _read(path)[1]
        expected = ["" if value is None else str(value) for value in expected]
    indices = (
        (5, 6, 7, 8, 9, 11, 12, 13, 14, 10)
        if format_id == "portal-csv"
        else (3, 4, 5, 6, 7, 9, 10, 11, 12, 8)
    )
    assert [detail[index] for index in indices] == expected


def test_portal_warnings_read_filed_values(filed_without_rounding) -> None:
    entry = _entry()
    filed_without_rounding(
        entry, tax_paid_rate_percent=Decimal("17.25"), tax_payable_normal_inr=None
    )
    assert portal_import_warnings((entry,)) == (
        fractional_rate_warning("17.25", "2"),
        MISSING_INDIAN_TAX_WARNING,
    )


def test_uncomputed_claim_refuses_the_official_csv(tmp_path: Path) -> None:
    # #601: a BLANK row is left for manual work in form-67-debug.csv; the
    # portal refuses a row with no credit, so the official file never holds one.
    entry = _entry(
        normal_tax=None,
        section_90_credit=None,
        total_credit=None,
    )
    path = tmp_path / "IncomeDetails.csv"
    with pytest.raises(ConfigError, match=r"belong in form-67-debug\.csv"):
        _writer().write(Form67ComputeResult(entries=(entry,)), path)
    assert not path.exists()


def test_cap_bound_claim_refuses_blank_mandatory_indian_tax(tmp_path: Path) -> None:
    # No producer builds a credit without col 7, so it fails the readiness
    # invariant; until #1491 it was the INCOMPLETE ConfigError.
    entry = _entry(normal_tax=None)
    path = tmp_path / "IncomeDetails.csv"
    message = f"InvariantError: {_UNPRODUCED_WHAT} (context keys: country, source)"
    with pytest.raises(InvariantError, match=rf"\A{re.escape(message)}\Z"):
        _writer().write(Form67ComputeResult(entries=(entry,)), path)
    assert not path.exists()


def test_populated_optional_fields_map_to_official_columns(tmp_path: Path) -> None:
    entry = _entry(
        income=Decimal("10000"),
        tax=Decimal("1500"),
        normal_tax=Decimal("3000.40"),
        special_tax=Decimal("2000.60"),
        article="Article 10(2)(b)",
        dtaa_rate=Decimal("25"),
        section_90_credit=Decimal("1500"),
        total_credit=Decimal("1500"),
    )
    path = tmp_path / "IncomeDetails.csv"
    _writer().write(Form67ComputeResult(entries=(entry,)), path)
    _, row = _read(path)
    assert row[8:] == [
        "3000",
        "2001",
        "10",
        "25.00",
        "1500",
        "",
        "1500",
    ]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (Form67SourceOfIncome.SALARY, "1"),
        (Form67SourceOfIncome.HOUSE_PROPERTY, "2"),
        (Form67SourceOfIncome.BUSINESS_INCOME, "3"),
        (Form67SourceOfIncome.LONG_TERM_CAPITAL_GAIN, "4"),
        (Form67SourceOfIncome.SHORT_TERM_CAPITAL_GAIN, "5"),
        (Form67SourceOfIncome.INTEREST_INCOME, "6"),
        (Form67SourceOfIncome.DIVIDEND, "7"),
        (Form67SourceOfIncome.ROYALTY, "8"),
        (Form67SourceOfIncome.FEES_FOR_TECHNICAL_SERVICES, "9"),
    ],
)
def test_source_codes_match_official_annexure(
    tmp_path: Path, source: Form67SourceOfIncome, code: str
) -> None:
    path = tmp_path / "IncomeDetails.csv"
    _writer().write(Form67ComputeResult(entries=(_entry(source=source),)), path)
    assert _read(path)[1][3] == code


def test_source_others_refuses_without_required_description(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="requires a description"):
        _writer().write(
            Form67ComputeResult(entries=(_entry(source=Form67SourceOfIncome.OTHERS),)),
            tmp_path / "IncomeDetails.csv",
        )


def test_country_others_refuses_without_required_description(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="country code 9999"):
        _writer().write(
            Form67ComputeResult(entries=(_entry(country_code="9999"),)),
            tmp_path / "IncomeDetails.csv",
        )


def test_amount_over_fourteen_digits_refuses(tmp_path: Path) -> None:
    """``Form67Entry`` itself refuses this income (#830), so a stub carries it."""
    entry = _entry(
        tax=Decimal("1"),
        normal_tax=Decimal("1"),
        section_90_credit=Decimal("1"),
        total_credit=Decimal("1"),
    )
    stub = SimpleNamespace(**{field.name: getattr(entry, field.name) for field in fields(entry)})
    stub.filed = replace(entry.filed, income_inr=Decimal("999999999999999"))
    message = (
        "InvariantError: official Form 67 amount must be a non-negative integer of at most 14 "
        "digits (context keys: field, value)"
    )
    with pytest.raises(InvariantError, match=rf"\A{re.escape(message)}\Z"):
        _writer().write(
            Form67ComputeResult(entries=(cast("Form67Entry", stub),)),
            tmp_path / "IncomeDetails.csv",
        )


def test_rate_over_one_hundred_refuses(tmp_path: Path) -> None:
    with pytest.raises(InvariantError, match="between 0 and 100"):
        _writer().write(
            Form67ComputeResult(
                entries=(
                    # Only a non-DTAA row can render an INR ratio over 100%:
                    # a DTAA row without a source rate is UNPROVEN (#767).
                    _entry(
                        income=Decimal("100"),
                        tax=Decimal("101"),
                        rate=Decimal("101"),
                        article=None,
                        dtaa_rate=None,
                        normal_tax=Decimal("101"),
                        section_90_credit=None,
                        section_91_credit=Decimal("101"),
                        total_credit=Decimal("101"),
                    ),
                )
            ),
            tmp_path / "IncomeDetails.csv",
        )


def test_noncanonical_article_refuses_when_the_entry_is_built() -> None:
    with pytest.raises(InvariantError) as exc:
        _entry(article="Section 25")
    assert str(exc.value) == (
        "InvariantError: Form67Entry.section_90_dtaa_article (col 9) must be a DTAA "
        "citation shaped like 'Article 10(2)(b)'"
    )


def test_fractional_rate_gets_manual_import_warning() -> None:
    warnings = portal_import_warnings((_entry(),))
    assert len(warnings) == 1
    assert "15.01" in warnings[0]
    assert "Do not round" in warnings[0]


def test_integral_rate_needs_no_import_warning() -> None:
    assert (
        portal_import_warnings(
            (
                _entry(
                    income=Decimal("100"),
                    tax=Decimal("25"),
                    normal_tax=Decimal("25"),
                    section_90_credit=Decimal("25"),
                    total_credit=Decimal("25"),
                ),
            )
        )
        == ()
    )


def test_missing_indian_tax_warning_emits_once_for_multiple_entries() -> None:
    incomplete = _entry(
        income=Decimal("100"),
        tax=Decimal("25"),
        normal_tax=None,
        section_90_credit=None,
        total_credit=None,
    )
    warnings = portal_import_warnings((incomplete, incomplete))
    assert len(warnings) == 1
    warning = warnings[0]
    assert "INDIAN TAX CALCULATION REQUIRED" in warning
    assert "average Indian tax rate" in warning
    assert "section 2(10)" in warning
    assert "Rule 128(5)(i)" in warning
    assert "does not prescribe an average or marginal-rate formula" in warning
    assert "legal question kept open" in warning
    assert "no reported ruling was found" in warning
    assert "tax_payable_overrides" in warning


def test_fractional_rate_warnings_leave_out_the_missing_indian_tax_warning() -> None:
    incomplete = _entry(normal_tax=None, section_90_credit=None, total_credit=None)
    warnings = portal_import_warnings(iter((incomplete,)))
    assert warnings[1:] == (MISSING_INDIAN_TAX_WARNING,)
    assert fractional_rate_warnings((incomplete,)) == warnings[:1]
    assert "15.01" in warnings[0]


def test_repeated_fractional_rates_warn_once_per_entry_before_the_missing_tax_warning() -> None:
    """#1203: identical fractional-rate warnings are kept, one per entry in entry
    order, and the missing-Indian-tax warning follows them once."""
    dividend = _entry(normal_tax=None, section_90_credit=None, total_credit=None)
    integral = _entry(
        income=Decimal("100"),
        tax=Decimal("25"),
        normal_tax=Decimal("25"),
        section_90_credit=Decimal("25"),
        total_credit=Decimal("25"),
    )
    interest = replace(dividend, source_of_income=Form67SourceOfIncome.INTEREST_INCOME)
    fractional = fractional_rate_warning("15.01", "2")

    assert fractional_rate_warnings((dividend, integral, interest)) == (fractional, fractional)
    assert portal_import_warnings((dividend, integral, interest)) == (
        fractional,
        fractional,
        MISSING_INDIAN_TAX_WARNING,
    )


def test_portal_import_warnings_checks_rate_before_advancing_iterator() -> None:
    invalid_rate = _entry(
        income=Decimal("100"),
        tax=Decimal("101"),
        article=None,
        dtaa_rate=None,
        normal_tax=None,
        section_90_credit=None,
        total_credit=None,
    )

    def entries():
        yield invalid_rate
        raise AssertionError("advanced past an invalid rate")

    with pytest.raises(InvariantError, match="rate must be between 0 and 100"):
        portal_import_warnings(entries())


def test_portal_import_warnings_observes_each_value_before_iterator_reuses_entry() -> None:
    filed = SimpleNamespace(
        tax_paid_rate_percent=Decimal("15.01"),
        tax_paid_rate_is_fractional=True,
        tax_payable_normal_inr=None,
    )
    entry = SimpleNamespace(cbdt_country_code="2", filed=filed)

    def entries():
        yield entry
        filed.tax_paid_rate_percent = Decimal("15.00")
        filed.tax_paid_rate_is_fractional = False
        filed.tax_payable_normal_inr = Decimal("1")
        yield entry

    warnings = portal_import_warnings(entries())
    assert len(warnings) == 2
    assert "15.01" in warnings[0]
    assert warnings[1] == MISSING_INDIAN_TAX_WARNING


def test_formula_injection_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _entry(article="Article 10(2)(b)")
    monkeypatch.setattr(
        form67_entry, "bare_article_number", lambda _citation, *, field: "=HYPERLINK()"
    )
    with pytest.raises(WriterContractError, match="formula"):
        _writer().write(Form67ComputeResult(entries=(entry,)), tmp_path / "IncomeDetails.csv")


def test_mid_write_failure_leaves_no_destination_or_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_entry: Form67Entry, **_kwargs: object) -> list[str]:
        raise RuntimeError("simulated")

    monkeypatch.setattr(upload, "_detail_row", crash)
    path = tmp_path / "IncomeDetails.csv"
    with pytest.raises(RuntimeError, match="simulated"):
        _writer().write(Form67ComputeResult(entries=(_entry(),)), path)
    assert not path.exists()
    assert list(tmp_path.glob(".IncomeDetails.csv.*.tmp")) == []
