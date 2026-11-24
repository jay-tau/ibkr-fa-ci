"""Schedule FSI portal cells, shared by the review and filing CSVs.

What gets reviewed must match what gets filed, so both writers render
the portal columns through :func:`fsi_portal_cells`.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass, fields
from decimal import Decimal
from typing import Final

from ibkr_fa.models.schedule_fsi_row import ScheduleFsiRow


@dataclass(frozen=True, slots=True)
class FsiPortalCells:
    """The eight portal cells, in ``PORTAL_COLUMN_HEADERS_FSI`` order."""

    country_code: str
    tin: str
    head: str
    income: str
    tax_paid: str
    tax_payable: str
    tax_relief: str
    dtaa_article: str

    def as_row(self) -> list[str]:
        return list(astuple(self))


FSI_PORTAL_FIELDS: Final[tuple[str, ...]] = tuple(field.name for field in fields(FsiPortalCells))


def _optional_amount(amount: Decimal | None) -> str:
    """``""`` for ``None`` (manual fill), else the filed whole-rupee amount."""
    return "" if amount is None else str(amount)


def fsi_portal_cells(row: ScheduleFsiRow) -> FsiPortalCells:
    """Render ``row``'s filed whole-rupee values (#900) as they are."""
    filed = row.filed
    return FsiPortalCells(
        country_code=row.cbdt_country_code,
        tin=row.taxpayer_id_number or "",
        head=row.head_of_income.value,
        income=str(filed.income_inr),
        tax_paid=str(filed.foreign_tax_inr),
        tax_payable=_optional_amount(filed.tax_payable_inr),
        tax_relief=_optional_amount(filed.tax_relief_inr),
        dtaa_article=row.dtaa_article or "",
    )


__all__ = [
    "FSI_PORTAL_FIELDS",
    "FsiPortalCells",
    "fsi_portal_cells",
]
