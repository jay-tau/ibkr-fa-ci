"""Form 67's upload rows and live-importer warnings (#1203): CBDT's official
``IncomeDetails.csv`` columns, not the form's visible labels. Amounts, rates
and the article number come from ``.filed``; nothing here rounds them."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from decimal import Decimal
from typing import Final, Protocol

from ibkr_fa.compliance import missing_indian_tax_payable_warning
from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Entry,
    Form67Readiness,
    Form67SourceOfIncome,
    require_no_refused_form67_rows,
)
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.invariants import require
from ibkr_fa.models.form67_entry import Form67FiledValues
from ibkr_fa.models.money import CBDT_AMOUNT_BOUND
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.reference.country_codes import lookup_country

FORM67_UPLOAD_HEADERS: Final[tuple[str, ...]] = (
    "Sl. No.",
    "Name of the country/ specified territory",
    "Please specify",
    "Source of income",
    "Please specify ",
    "Income from outside India",
    "Amount",
    "Rate(%)",
    "Tax payable on such income under normal provisions in India",
    "Tax payable on such income under section 115JB/ JC",
    "Article No. of Double Taxation Avoidance Agreements",
    "Rate of tax as per Double Taxation Avoidance Agreements(%)",
    "Amount ",
    "Credit claimed under section 91",
    "Total foreign tax credit claimed",
)

_SOURCE_CODES: Final[dict[Form67SourceOfIncome, str]] = {
    Form67SourceOfIncome.SALARY: "1",
    Form67SourceOfIncome.HOUSE_PROPERTY: "2",
    Form67SourceOfIncome.BUSINESS_INCOME: "3",
    Form67SourceOfIncome.LONG_TERM_CAPITAL_GAIN: "4",
    Form67SourceOfIncome.SHORT_TERM_CAPITAL_GAIN: "5",
    Form67SourceOfIncome.INTEREST_INCOME: "6",
    Form67SourceOfIncome.DIVIDEND: "7",
    Form67SourceOfIncome.ROYALTY: "8",
    Form67SourceOfIncome.FEES_FOR_TECHNICAL_SERVICES: "9",
    Form67SourceOfIncome.OTHERS: "10",
}
_OTHERS_COUNTRY_CODE = "9999"
_MAX_AMOUNT_DIGITS = len(str(CBDT_AMOUNT_BOUND))
_HUNDRED = Decimal("100")


class _PortalImportEntry(Protocol):
    cbdt_country_code: str

    @property
    def filed(self) -> Form67FiledValues: ...


def _amount(value: Decimal | None, *, field: str) -> str:
    """Render a filed whole-rupee amount, which compute has already rounded."""
    if value is None:
        return ""
    rendered = str(value)
    require(
        rendered.isascii() and rendered.isdecimal() and len(rendered) <= _MAX_AMOUNT_DIGITS,
        what="official Form 67 amount must be a non-negative integer of at most 14 digits",
        field=field,
        value=rendered,
    )
    return rendered


def _rate(value: Decimal | None, *, field: str) -> str:
    """Render a filed two-place rate, which compute has already rounded."""
    if value is None:
        return ""
    require(
        Decimal("0") <= value <= _HUNDRED,
        what="official Form 67 rate must be between 0 and 100 percent",
        field=field,
        value=value,
    )
    return str(value)


def fractional_rate_warnings(entries: Iterable[_PortalImportEntry]) -> tuple[str, ...]:
    """One warning per entry whose Rate(%) the live importer may blank."""
    warnings: list[str] = []
    for entry in entries:
        filed = entry.filed
        rendered = _rate(filed.tax_paid_rate_percent, field="Foreign tax rate")
        if filed.tax_paid_rate_is_fractional:
            warnings.append(
                f"Form 67 CSV importer may blank fractional Rate(%) {rendered} for "
                f"country code {entry.cbdt_country_code}; after upload, enter {rendered} "
                "manually and verify it before saving. Do not round it to an integer."
            )
    return tuple(warnings)


def portal_import_warnings(
    entries: Iterable[_PortalImportEntry], *, override_advice: Sequence[str] = ()
) -> tuple[str, ...]:
    """Warnings for live-importer limitations and manual filing work.

    ``override_advice`` is :func:`~ibkr_fa.compute.form67.form67_override_advice`
    of the run's entries, which the missing Indian tax warning gives in place
    of its generic override sentence (#1469).
    """
    warnings: list[str] = []
    missing_indian_tax = False
    for entry in entries:
        missing_indian_tax |= entry.filed.tax_payable_normal_inr is None
        warnings.extend(fractional_rate_warnings((entry,)))
    if missing_indian_tax:
        warnings.append(missing_indian_tax_payable_warning(override_advice))
    return tuple(warnings)


def _detail_row(entry: Form67Entry) -> list[str]:
    require(
        entry.cbdt_country_code != _OTHERS_COUNTRY_CODE,
        what=(
            "official Form 67 country code 9999 (Others) requires a description "
            "that Form67Entry does not carry"
        ),
        iso_alpha2=entry.iso_alpha2,
    )
    country = lookup_country(entry.iso_alpha2)
    require(
        country.cbdt_code == entry.cbdt_country_code,
        what="official Form 67 country code must match the vendored country reference",
        iso_alpha2=entry.iso_alpha2,
        expected=country.cbdt_code,
        actual=entry.cbdt_country_code,
    )
    require(
        entry.source_of_income is not Form67SourceOfIncome.OTHERS,
        what=(
            "official Form 67 source code 10 (Others) requires a description "
            "that Form67Entry does not carry"
        ),
        source=entry.source_of_income,
    )
    filed = entry.filed
    return [
        str(entry.serial_number),
        entry.cbdt_country_code,
        "",
        _SOURCE_CODES[entry.source_of_income],
        "",
        _amount(filed.income_inr, field="Income from outside India"),
        _amount(filed.foreign_tax_inr, field="Tax paid outside India"),
        _rate(filed.tax_paid_rate_percent, field="Foreign tax rate"),
        _amount(filed.tax_payable_normal_inr, field="Tax payable under normal provisions"),
        _amount(filed.tax_payable_special_inr, field="Tax payable under section 115JB/JC"),
        filed.section_90_dtaa_article_number or "",
        _rate(filed.section_90_dtaa_rate_percent, field="DTAA rate"),
        _amount(filed.section_90_credit_inr, field="Section 90/90A credit"),
        _amount(filed.section_91_credit_inr, field="Section 91 credit"),
        _amount(filed.total_credit_inr, field="Total foreign tax credit"),
    ]


def _rows(result: Form67ComputeResult) -> Iterator[Sequence[str]]:
    # Callers withhold this file for BLANK results and refuse REFUSED ones
    # before writing anything (#601); this is the upload's own check, run once
    # the file is staged and before the header.
    require_no_refused_form67_rows(result)
    if result.readiness is Form67Readiness.BLANK:
        raise ConfigError(
            field="form67",
            message=(
                "the official Form 67 CSV needs every row's Indian tax payable and "
                "credit; rows left blank for manual work belong in form-67-debug.csv"
            ),
        )
    yield FORM67_UPLOAD_HEADERS
    for entry in result.entries:
        yield _detail_row(entry)


def form67_upload_rows(
    ctx: RenderContext,
) -> Callable[[Form67ComputeResult], Iterator[Sequence[str]]]:
    """The run's lazy upload rows, header first. The file has fixed headers and
    compute-rounded values, so it needs no labels, but unsupported AYs still
    refuse when the writer is built."""
    for_ay(ctx.assessment_year_start)
    return _rows


__all__ = [
    "FORM67_UPLOAD_HEADERS",
    "form67_upload_rows",
    "fractional_rate_warnings",
    "portal_import_warnings",
]
