"""Every Schedule FA writer renders the models' filed whole-rupee values (#1077).

The models round (:attr:`FaA3Row.filed`, :attr:`FaA2Row.filed`); the A3
review CSV, filing CSV, xlsx, Quicko workbook and CBDT JSON fragment, and the
A2 filing CSV, review CSV, xlsx and CBDT JSON fragment only render those
values. Audit columns keep the rows' full-precision fields.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute import CyOverlap, build_fa_a3_rows
from ibkr_fa.config import FaEntityOverride
from ibkr_fa.models.fa_a2_row import FaA2FiledValues, FaA2NatureOfAmount, FaA2Row, FaA2Status
from ibkr_fa.models.fa_a3_row import FaA3FiledValues, FaA3PeakBlankReason, FaA3Row
from ibkr_fa.output import Subcommand, output_binding
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.cbdt_json_fragment import (
    CbdtA2FragmentInput,
    CbdtA2Remedies,
    CbdtA3Remedies,
    CbdtJsonIncompleteError,
    CbdtScheduleFaFragmentInput,
    build_cbdt_schedule_fa_fragment,
)
from ibkr_fa.output.cbdt_json_schema import A2_DEFINITION, A3_DEFINITION
from ibkr_fa.output.netbenefits_quicko_fa_xlsx import (
    QUICKO_TEMPLATE_FILENAME,
    QuickoFaWorkbookInput,
    QuickoFaXlsxWriter,
)
from ibkr_fa.types import Isin
from tests._fa_lot_inputs import fx_for, make_open_lot
from tests._netbenefits_fa import write_quicko_template
from tests._writer_stubs import writer_class

_A3_AMOUNTS = tuple(field.name for field in fields(FaA3FiledValues))
_A2_AMOUNTS = tuple(field.name for field in fields(FaA2FiledValues))

# Each format's cells for _A3_AMOUNTS / _A2_AMOUNTS, in that order.
_A3_JSON_KEYS = (
    "InitialValOfInvstmnt",
    "PeakBalanceDuringPeriod",
    "ClosingBalance",
    "TotGrossAmtPaidCredited",
    "TotGrossProceeds",
)
_A2_JSON_KEYS = ("PeakBalanceDuringPeriod", "ClosingBalance", "GrossAmtPaidCredited")
# Quicko's Purchase, Peak and Fair Market Value, then Dividend/Interest Income
# and Sale Value (its columns list Sale Value first).
_QUICKO_CELLS = ("J2", "K2", "L2", "N2", "M2")


def _a3(**overrides: Any) -> FaA3Row:
    """A synthetic A3 row whose default amounts all end in ``.50``."""
    values: dict[str, Any] = {
        "country_name": "United States of America",
        "country_code": "2",
        "name_of_entity": "SAMPLE ISSUER AA INC",
        "address_of_entity": "100 Example Plaza, Sampleville, CA",
        "zip_code": "99990",
        "nature_of_entity": "Listed Company",
        "date_of_acquiring": date(2025, 3, 15),
        "initial_value_inr": Decimal("64217.50"),
        "peak_value_inr": Decimal("85000.50"),
        "closing_value_inr": Decimal("83000.50"),
        "gross_amount_paid_credited_inr": Decimal("500.50"),
        "gross_proceeds_from_sale_inr": Decimal("1200.50"),
        "source_isin": "US99990A3095",
        "source_ticker": "XQAA",
        "source_country_iso2": "US",
        "audit_cost_basis_usd": Decimal("884.123"),
        "audit_acquisition_date": date(2025, 3, 15),
        "audit_close_price_usd": Decimal("140.4567"),
        "audit_ttbr_acquisition": Decimal("83.07"),
        "audit_ttbr_dec31": Decimal("89.53"),
        "audit_disposal_date": None,
    }
    values.update(overrides)
    values.setdefault(
        "peak_blank_reason",
        FaA3PeakBlankReason.PRICES_NOT_FETCHED if values["peak_value_inr"] is None else None,
    )
    return FaA3Row(**values)


def _a2(**overrides: Any) -> FaA2Row:
    """An A2 row with peak ``64217.50`` and closing ``-0.4``."""
    values: dict[str, Any] = {
        "country_code": "2",
        "country_name": "UNITED STATES OF AMERICA",
        "institution_name": "Interactive Brokers LLC",
        "institution_address": "One Pickwick Plaza, Greenwich, CT",
        "institution_zip": "06830",
        "account_number": "U1234567",
        "status": FaA2Status.BENEFICIAL_OWNER,
        "account_opening_date": date(2024, 2, 1),
        "peak_balance_inr": Decimal("64217.50"),
        "closing_balance_inr": Decimal("-0.4"),
        "nature_of_amount": FaA2NatureOfAmount.DIVIDEND,
        "gross_amount_inr": Decimal("1234.50"),
        "base_currency": "USD",
        "audit_closing_balance_base_ccy": Decimal("-0.0045"),
        "audit_ttbr_dec31": Decimal("89.53"),
        "audit_peak_override_source": "test config",
        "audit_peak_override_reason": "test peak",
    }
    values.update(overrides)
    if values["peak_balance_inr"] is None:
        values["audit_peak_override_source"] = None
        values["audit_peak_override_reason"] = None
    return FaA2Row(**values)


def _fragment_input(a2: FaA2Row, a3: FaA3Row) -> CbdtScheduleFaFragmentInput:
    return CbdtScheduleFaFragmentInput(
        a2=(CbdtA2FragmentInput(row=a2, account_id_cross_checked=True),),
        a3_rows=(a3,),
        calendar_year=2025,
        assessment_year_start=2026,
        itr_form="ITR-2",
        a2_remedies=CbdtA2Remedies(
            peak="supply the peak", address="supply the address", zip_code="supply the zip"
        ),
        a3_remedies=CbdtA3Remedies(
            address=lambda _isin: "supply the entity address",
            zip_code=lambda _isin: "supply the entity zip",
            nature=lambda _isin: "supply the entity nature",
        ),
    )


def _csv_row(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        _header, row = csv.reader(handle)
    return row


def _xlsx_row(path: Path) -> list[object]:
    worksheet = load_workbook(path).active
    assert worksheet is not None
    return [cell.value for cell in worksheet[2]]


# Each A3 / A2 format, as (amount cells, audit cells) for one row.
_Cells = tuple[list[object], list[object]]


def _a3_review_csv(row: FaA3Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a3-review.csv"
    writer_class(Subcommand.FA, "review-csv")().write([row], path)
    cells = _csv_row(path)
    return list(cells[6:11]), list(cells[11:])


def _a3_filing_csv(row: FaA3Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a3-filing.csv"
    output_binding(Subcommand.FA, "filing-csv").writer(RenderContext(2026)).write([row], path)
    return list(_csv_row(path)[7:12]), []


def _a3_xlsx(row: FaA3Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a3.xlsx"
    writer_class(Subcommand.FA, "xlsx")().write([row], path)
    cells = _xlsx_row(path)
    return cells[6:11], cells[11:]


def _a3_quicko(row: FaA3Row, tmp_path: Path) -> _Cells:
    template = tmp_path / QUICKO_TEMPLATE_FILENAME
    write_quicko_template(template)
    path = tmp_path / "a3-quicko.xlsx"
    QuickoFaXlsxWriter().write(
        QuickoFaWorkbookInput(rows=(row,), account_number="I99999999", template_path=template),
        path,
    )
    worksheet = load_workbook(path)["Shares & Securities"]
    return [worksheet[cell].value for cell in _QUICKO_CELLS], []


def _a3_json(row: FaA3Row, _tmp_path: Path) -> _Cells:
    fragment = build_cbdt_schedule_fa_fragment(_fragment_input(_a2(), row))
    item = fragment["ScheduleFA"][A3_DEFINITION][0]
    return [item[key] for key in _A3_JSON_KEYS], []


def _a2_filing_csv(row: FaA2Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a2-filing.csv"
    output_binding(Subcommand.FA_A2, "filing-csv").writer(RenderContext(2026)).write([row], path)
    cells = _csv_row(path)
    return [cells[8], cells[9], cells[11]], []


def _a2_review_csv(row: FaA2Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a2-review.csv"
    writer_class(Subcommand.FA_A2, "review-csv")().write([row], path)
    cells = _csv_row(path)
    return [cells[7], cells[8], cells[10]], list(cells[11:])


def _a2_xlsx(row: FaA2Row, tmp_path: Path) -> _Cells:
    path = tmp_path / "a2.xlsx"
    writer_class(Subcommand.FA_A2, "xlsx")().write([row], path)
    cells = _xlsx_row(path)
    return [cells[7], cells[8], cells[10]], cells[11:]


def _a2_json(row: FaA2Row, _tmp_path: Path) -> _Cells:
    fragment = build_cbdt_schedule_fa_fragment(_fragment_input(row, _a3()))
    item = fragment["ScheduleFA"][A2_DEFINITION][0]
    return [item[key] for key in _A2_JSON_KEYS], []


_A3_FORMATS: dict[str, Callable[[FaA3Row, Path], _Cells]] = {
    "review-csv": _a3_review_csv,
    "filing-csv": _a3_filing_csv,
    "xlsx": _a3_xlsx,
    "quicko": _a3_quicko,
    "cbdt-json": _a3_json,
}
_A2_FORMATS: dict[str, Callable[[FaA2Row, Path], _Cells]] = {
    "filing-csv": _a2_filing_csv,
    "review-csv": _a2_review_csv,
    "xlsx": _a2_xlsx,
    "cbdt-json": _a2_json,
}
_TEXT_FORMATS = frozenset({"review-csv", "filing-csv"})


def _rendered(format_id: str, value: Decimal | None) -> object:
    """``value`` as ``format_id`` renders a filed amount cell."""
    if format_id in _TEXT_FORMATS:
        return "" if value is None else str(value)
    return None if value is None else int(value)


@pytest.mark.parametrize("format_id", _A3_FORMATS)
def test_every_a3_format_renders_the_filed_values(format_id: str, tmp_path: Path) -> None:
    row = _a3()
    amounts, _audit = _A3_FORMATS[format_id](row, tmp_path)
    filed = [getattr(row.filed, name) for name in _A3_AMOUNTS]
    assert filed == [Decimal(value) for value in ("64218", "85001", "83001", "501", "1201")]
    assert amounts == [_rendered(format_id, value) for value in filed]


@pytest.mark.parametrize("format_id", _A2_FORMATS)
def test_every_a2_format_renders_the_filed_values(format_id: str, tmp_path: Path) -> None:
    """The ``-0.4`` closing balance files as ``0`` in every format, never ``-0``."""
    row = _a2()
    amounts, _audit = _A2_FORMATS[format_id](row, tmp_path)
    filed = [getattr(row.filed, name) for name in _A2_AMOUNTS]
    assert filed == [Decimal("64218"), Decimal("0"), Decimal("1235")]
    assert amounts == [_rendered(format_id, value) for value in filed]
    assert amounts[1] == _rendered(format_id, Decimal("0"))


@pytest.mark.parametrize("format_id", _A3_FORMATS)
def test_a3_minus_zero_closing_value_writes_as_zero(format_id: str, tmp_path: Path) -> None:
    """A ``-0.00`` closing value files as ``0``; the review CSV used to refuse
    it through its formula guard."""
    amounts, _audit = _A3_FORMATS[format_id](_a3(closing_value_inr=Decimal("-0.00")), tmp_path)
    assert amounts[_A3_AMOUNTS.index("closing_value_inr")] == _rendered(format_id, Decimal("0"))


@pytest.mark.parametrize(
    ("value", "shown"),
    [("0", None), ("-0.00", None), ("0.4", None), ("0.5", 1)],
)
def test_quicko_leaves_a_zero_filed_optional_amount_blank(
    value: str, shown: int | None, tmp_path: Path
) -> None:
    """Sale Value and Dividend/Interest Income are blank when the filed value
    is zero, whatever sign or fraction the row's amount had."""
    row = _a3(
        gross_proceeds_from_sale_inr=Decimal(value),
        gross_amount_paid_credited_inr=Decimal(value),
    )
    amounts, _audit = _a3_quicko(row, tmp_path)
    assert amounts[3:] == [shown, shown]


def _patch_filed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every filed amount ``7``, so a cell that shows it came from ``filed``."""
    seven = Decimal("7")
    monkeypatch.setattr(
        FaA3Row, "filed", property(lambda _row: FaA3FiledValues(*(seven,) * len(_A3_AMOUNTS)))
    )
    monkeypatch.setattr(
        FaA2Row, "filed", property(lambda _row: FaA2FiledValues(*(seven,) * len(_A2_AMOUNTS)))
    )


@pytest.mark.parametrize("format_id", _A3_FORMATS)
def test_every_a3_format_reads_filed_and_keeps_full_precision_audit(
    format_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = _A3_FORMATS[format_id]
    _unpatched_amounts, audit = write(_a3(), tmp_path)
    _patch_filed(monkeypatch)
    amounts, patched_audit = write(_a3(), tmp_path)
    assert amounts == [_rendered(format_id, Decimal("7"))] * len(_A3_AMOUNTS)
    if format_id in {"review-csv", "xlsx"}:
        assert patched_audit == audit


@pytest.mark.parametrize("format_id", _A2_FORMATS)
def test_every_a2_format_reads_filed_and_keeps_full_precision_audit(
    format_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = _A2_FORMATS[format_id]
    _unpatched_amounts, audit = write(_a2(), tmp_path)
    _patch_filed(monkeypatch)
    amounts, patched_audit = write(_a2(), tmp_path)
    assert amounts == [_rendered(format_id, Decimal("7"))] * len(_A2_AMOUNTS)
    if format_id in {"review-csv", "xlsx"}:
        assert patched_audit == audit


def test_audit_cells_keep_the_full_precision_values(tmp_path: Path) -> None:
    """The audit columns the patched-``filed`` tests compare carry the rows'
    unrounded values, not whole rupees."""
    _amounts, a3_csv_audit = _a3_review_csv(_a3(), tmp_path)
    assert a3_csv_audit[3] == "884.123"  # cost basis (USD)
    assert a3_csv_audit[5] == "140.4567"  # Dec-31 close (USD)
    _amounts, a2_csv_audit = _a2_review_csv(_a2(), tmp_path)
    assert a2_csv_audit[1] == "'-0.0045"  # closing balance (base currency)
    _amounts, a2_xlsx_audit = _a2_xlsx(_a2(), tmp_path)
    assert a2_xlsx_audit[1] == pytest.approx(-0.0045)


@pytest.mark.parametrize("format_id", sorted(set(_A3_FORMATS) - {"cbdt-json"}))
def test_blank_a3_peak_stays_blank(format_id: str, tmp_path: Path) -> None:
    amounts, _audit = _A3_FORMATS[format_id](_a3(peak_value_inr=None), tmp_path)
    assert amounts[_A3_AMOUNTS.index("peak_value_inr")] == _rendered(format_id, None)


@pytest.mark.parametrize("format_id", sorted(set(_A2_FORMATS) - {"cbdt-json"}))
def test_blank_a2_peak_stays_blank(format_id: str, tmp_path: Path) -> None:
    amounts, _audit = _A2_FORMATS[format_id](_a2(peak_balance_inr=None), tmp_path)
    assert amounts[_A2_AMOUNTS.index("peak_balance_inr")] == _rendered(format_id, None)


def test_blank_peaks_are_json_gaps() -> None:
    a3 = _a3(peak_value_inr=None)
    a2 = _a2(peak_balance_inr=None)
    with pytest.raises(CbdtJsonIncompleteError) as exc:
        build_cbdt_schedule_fa_fragment(_fragment_input(a2, a3))
    gaps = [reason.split(":", 1)[0] for reason in exc.value.reasons]
    assert gaps == ["A2 PeakBalanceDuringPeriod", "A3 row 1 PeakBalanceDuringPeriod"]


@pytest.mark.parametrize(
    ("iso", "isin", "code", "portal", "name"),
    [
        ("NL", "NL99971G1013", "31", "31-NETHERLANDS", "Netherlands"),
        ("US", "US99990A3095", "2", "2-UNITED STATES OF AMERICA", "United States of America"),
    ],
)
def test_produced_a3_row_shows_one_country_in_every_format(
    iso: str, isin: str, code: str, portal: str, name: str, tmp_path: Path
) -> None:
    """The A3 producer takes both country cells from one lookup (#1188): the
    review CSV and xlsx show the ISO code's portal entry, the filing CSV and
    the complete ITR-2/ITR-3 fragments file its CBDT code."""
    [row] = build_fa_a3_rows(
        CyOverlap(
            open_lots=[make_open_lot(isin=isin, iso_country=iso, open_date=date(2025, 12, 31))],
            closed_lots=[],
        ),
        calendar_year=2025,
        fx_policy=fx_for(),
        nature_of_entity="Listed Company",
        dividends_by_symbol_inr={},
        # #1289: the fragment needs an address, and only an entry supplies one.
        entity_overrides={
            Isin(isin): FaEntityOverride(
                isin=Isin(isin), address="100 Example Plaza, Sampleville, CA", zip_code="00000"
            )
        },
    )
    writer_class(Subcommand.FA, "review-csv")().write([row], tmp_path / "review.csv")
    assert _csv_row(tmp_path / "review.csv")[0] == portal
    writer_class(Subcommand.FA, "xlsx")().write([row], tmp_path / "a3.xlsx")
    assert _xlsx_row(tmp_path / "a3.xlsx")[0] == portal
    for itr_form in ("ITR-2", "ITR-3"):
        filing = tmp_path / f"filing-{itr_form}.csv"
        filing_csv = output_binding(Subcommand.FA, "filing-csv")
        filing_csv.writer(RenderContext(2026, itr_form)).write([row], filing)
        assert _csv_row(filing)[1] == code
        fragment = build_cbdt_schedule_fa_fragment(
            replace(_fragment_input(_a2(), row), itr_form=itr_form)
        )
        [item] = fragment["ScheduleFA"][A3_DEFINITION]
        assert (item["CountryCodeExcludingIndia"], item["CountryName"]) == (code, name)
