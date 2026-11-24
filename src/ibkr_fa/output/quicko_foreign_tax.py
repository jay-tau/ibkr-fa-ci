"""Quicko's "Taxes Withheld Outside India" block for the Schedule TR filing CSV.

Quicko asks for one foreign tax credit form per Form 67 entry, that is per
``(country, source of income)``. Schedule TR rows are per country only and carry
no per-source income, so the block is built from the Form 67 result. Only the
display mapping happens here: the amounts are the entry's filed whole-rupee
values (#898), the article is its filed article number (#1079), and the section
is whichever credit the engine set.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.models.form67_entry import Form67Entry
from ibkr_fa.reference.country_codes import lookup_country

_COLUMN_COUNT: Final[int] = 5


@dataclass(frozen=True, slots=True)
class QuickoForeignTaxInput:
    """What a run gives the Schedule TR filing CSV to add the Quicko block.

    Attributes:
        form67: The run's Form 67 result.
        deductor_name: Filed as "Name of the deductor": the withholding agent
            the filer configured from their Form 1042-S (ADR-0019), or
            ``None`` to leave that value blank for the filer.
        foreign_tins: ``[account.foreign_tins]``, ISO alpha-2 to identifier.
    """

    form67: Form67ComputeResult
    deductor_name: str | None
    foreign_tins: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class QuickoForeignTaxEntry:
    """One Quicko "Add Taxes Withheld Outside India" form."""

    country_name: str
    nature_of_income: str
    deductor_name: str
    credit_amount_inr: Decimal
    tax_withheld_inr: Decimal
    taxpayer_id_number: str
    section: str
    article: str


def _section(entry: Form67Entry) -> str | None:
    """Quicko's section for ``entry``, or ``None`` when the entry gets no Quicko
    form."""

    if entry.tax_payable_normal_inr is None:
        # No exact Indian tax for this row, so it stays out of the Quicko
        # entries; filing needs the mandatory Indian-tax amount that
        # supports the claimed credit.
        return None
    if entry.section_90_credit_inr is not None:
        return "90"
    if entry.section_91_credit_inr is not None:
        return "91"
    # Same ownership rule as TIN: Form 67 reports the incomplete FTC
    # configuration. Quicko cannot add a tax-relief row without it.
    return None


def has_quicko_foreign_tax_entries(form67: Form67ComputeResult) -> bool:
    """Whether :func:`quicko_foreign_tax_entries` keeps at least one entry."""

    return any(_section(entry) is not None for entry in form67.entries)


def quicko_foreign_tax_entries(
    form67: Form67ComputeResult,
    *,
    deductor_name: str | None,
    foreign_tins: Mapping[str, str],
) -> tuple[QuickoForeignTaxEntry, ...]:
    """Translate the Form 67 source-of-income rows to Quicko's tax-credit form.

    A ``None`` ``deductor_name`` leaves each form's "Name of the deductor"
    blank, as a missing TIN leaves its TIN blank.
    """

    entries: list[QuickoForeignTaxEntry] = []
    for entry in form67.entries:
        section = _section(entry)
        if section is None:
            continue
        filed = entry.filed
        entries.append(
            QuickoForeignTaxEntry(
                country_name=lookup_country(entry.iso_alpha2).country_name,
                nature_of_income=str(entry.source_of_income),
                deductor_name="" if deductor_name is None else deductor_name,
                credit_amount_inr=filed.income_inr,
                tax_withheld_inr=filed.foreign_tax_inr,
                taxpayer_id_number=foreign_tins.get(entry.iso_alpha2, ""),
                section=section,
                # A section 90 entry always has a citation, a section 91 entry never.
                article=filed.section_90_dtaa_article_number or "",
            )
        )
    return tuple(entries)


def _field_row(label: str, value: str) -> list[str]:
    return [label, value, *([""] * (_COLUMN_COUNT - 2))]


def _quicko_pair(
    left_heading: str,
    left_value: str,
    right_heading: str,
    right_value: str,
) -> Iterator[list[str]]:
    """Yield Quicko's two side-by-side fields as headings over values."""

    yield [left_heading, right_heading, "", "", ""]
    yield [left_value, right_value, "", "", ""]


def quicko_foreign_tax_rows(entries: Iterable[QuickoForeignTaxEntry]) -> Iterator[list[str]]:
    """Yield the five-column block rows that follow the Schedule TR rows."""

    for entry in entries:
        yield [""] * _COLUMN_COUNT
        yield [""] * _COLUMN_COUNT
        yield _field_row("Quicko", "")
        yield _field_row("Tax Credits -> Add Manually -> Taxes Withheld Outside India", "")
        yield [""] * _COLUMN_COUNT
        yield from _quicko_pair(
            "Country",
            entry.country_name,
            "Nature Of Income",
            entry.nature_of_income,
        )
        yield [""] * _COLUMN_COUNT
        yield _field_row("Name of the deductor", entry.deductor_name)
        yield [""] * _COLUMN_COUNT
        yield from _quicko_pair(
            "Credit Amount",
            str(entry.credit_amount_inr),
            "Tax Withheld",
            str(entry.tax_withheld_inr),
        )
        yield [""] * _COLUMN_COUNT
        yield _field_row("TIN", entry.taxpayer_id_number)
        yield [""] * _COLUMN_COUNT
        yield from _quicko_pair(
            "Section",
            entry.section,
            "Article",
            entry.article,
        )


__all__ = [
    "QuickoForeignTaxEntry",
    "QuickoForeignTaxInput",
    "has_quicko_foreign_tax_entries",
    "quicko_foreign_tax_entries",
    "quicko_foreign_tax_rows",
]
