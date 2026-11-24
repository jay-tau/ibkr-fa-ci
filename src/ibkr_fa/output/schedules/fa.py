"""Schedule FA's A2 and A3 bulk-upload rows (#1201): the uploader's shapes, not
the manual-entry labels. Per record: the form's country dropdown, then
``.filed``, then text and amounts in column order. Money comes only from
``.filed``; nothing here rounds or derives an amount.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from decimal import Decimal
from functools import partial
from types import ModuleType
from typing import Final

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.invariants import require
from ibkr_fa.models._row_checks import upload_text
from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.models.fa_a3_row import FaA3Row
from ibkr_fa.models.money import CBDT_AMOUNT_BOUND
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.cbdt_json_schema import A2_NATURE_LETTERS
from ibkr_fa.output.portal_labels import for_ay

A2_UPLOAD_HEADERS: Final[tuple[str, ...]] = (
    "Country/Region name",
    "Country Name and Code",
    "Name of financial institution",
    "Address of financial institution",
    "ZIP Code",
    "Account Number",
    "Status",
    "Account opening date",
    "Peak Balance During the Period",
    "Closing balance",
    "Nature of Amount",
    "Amount",
)

A3_UPLOAD_HEADERS: Final[tuple[str, ...]] = (
    "Country/Region name",
    "Country Name and Code",
    "Name of entity",
    "Address of entity",
    "ZIP Code",
    "Nature of entity",
    "Date of acquiring the interest",
    "Initial value of the investment",
    "Peak value of investment during the Period",
    "Closing balance",
    "Total gross amount paid/credited with respect to the holding during the period",
    "Total gross proceeds from sale or redemption of investment during the period",
)


def _portal_text(value: str, *, table: str, field: str, max_length: int) -> str:
    """Remove delimiters the portal's non-RFC CSV splitter cannot handle."""
    rendered = upload_text(value)
    require(
        len(rendered) <= max_length,
        what=f"official Schedule FA {table} {field} must be at most {max_length} characters",
        length=len(rendered),
    )
    return rendered


def _a3_text(value: str, *, field: str, max_length: int) -> str:
    require(value.isascii(), what="A3 bulk-upload text must be ASCII", field=field, value=value)
    return _portal_text(value, table="A3", field=field, max_length=max_length)


def _portal_amount(value: Decimal | None, *, table: str, labels: ModuleType, field: str) -> str:
    rendered = labels.format_amount_for_portal(value, field=f"Schedule FA {table} {field}")
    require(
        rendered == "" or abs(int(rendered)) <= CBDT_AMOUNT_BOUND,
        what=f"official Schedule FA {table} amount must fit the CBDT 14-digit bound",
        field=field,
        value=rendered,
    )
    return rendered


_a2_text = partial(_portal_text, table="A2")
_a2_amount = partial(_portal_amount, table="A2")
_a3_amount = partial(_portal_amount, table="A3")


def _a2_cells(row: FaA2Row, *, labels: ModuleType) -> list[str]:
    """Render one row in the exact official bulk-upload template shape."""
    filed = row.filed
    return [
        _a2_text(row.country_name, field="country name", max_length=55),
        row.country_code,
        _a2_text(row.institution_name, field="institution name", max_length=125),
        _a2_text(row.institution_address, field="institution address", max_length=200),
        _a2_text(row.institution_zip, field="ZIP code", max_length=8),
        _a2_text(row.account_number, field="account number", max_length=34),
        row.status.value,
        row.account_opening_date.isoformat(),
        _a2_amount(filed.peak_balance_inr, labels=labels, field="peak balance"),
        _a2_amount(filed.closing_balance_inr, labels=labels, field="closing balance"),
        "" if row.nature_of_amount is None else A2_NATURE_LETTERS[row.nature_of_amount],
        _a2_amount(filed.gross_amount_inr, labels=labels, field="gross amount"),
    ]


def _a3_cells(row: FaA3Row, *, serial_number: int, labels: ModuleType) -> list[str]:
    filed = row.filed
    return [
        str(serial_number),
        row.country_code,
        _a3_text(row.name_of_entity, field="Name of entity", max_length=125),
        _a3_text(row.address_of_entity, field="Address of entity", max_length=200),
        _a3_text(row.zip_code, field="ZIP Code", max_length=8),
        _a3_text(row.nature_of_entity, field="Nature of entity", max_length=34),
        row.date_of_acquiring.isoformat(),
        _a3_amount(filed.initial_value_inr, labels=labels, field="initial value"),
        _a3_amount(filed.peak_value_inr, labels=labels, field="peak value"),
        _a3_amount(filed.closing_value_inr, labels=labels, field="closing balance"),
        _a3_amount(filed.gross_amount_paid_credited_inr, labels=labels, field="gross amount"),
        _a3_amount(filed.gross_proceeds_from_sale_inr, labels=labels, field="gross proceeds"),
    ]


def a2_upload_rows(ctx: RenderContext) -> Callable[[Iterable[FaA2Row]], Iterator[Sequence[str]]]:
    """The run's lazy A2 rows, header first; resolves ``ctx``'s labels now."""
    labels = for_ay(ctx.assessment_year_start)
    itr_form: ItrForm = "ITR-2" if ctx.itr_form is None else ctx.itr_form

    def rows(records: Iterable[FaA2Row]) -> Iterator[Sequence[str]]:
        yield A2_UPLOAD_HEADERS
        for row in records:
            labels.require_fa_country_code_supported(row.country_code, itr_form=itr_form)
            yield _a2_cells(row, labels=labels)

    return rows


def a3_upload_rows(ctx: RenderContext) -> Callable[[Iterable[FaA3Row]], Iterator[Sequence[str]]]:
    """The run's lazy A3 rows, header first; resolves ``ctx``'s labels now."""
    labels = for_ay(ctx.assessment_year_start)
    itr_form: ItrForm = "ITR-2" if ctx.itr_form is None else ctx.itr_form

    def rows(records: Iterable[FaA3Row]) -> Iterator[Sequence[str]]:
        yield A3_UPLOAD_HEADERS
        for serial_number, row in enumerate(records, start=1):
            labels.require_fa_country_code_supported(row.country_code, itr_form=itr_form)
            yield _a3_cells(row, serial_number=serial_number, labels=labels)

    return rows


__all__ = ["A2_UPLOAD_HEADERS", "A3_UPLOAD_HEADERS", "a2_upload_rows", "a3_upload_rows"]
