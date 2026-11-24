"""Schedule TR portal cells, shared by the review and filing CSVs."""

from __future__ import annotations

from dataclasses import astuple, dataclass

from ibkr_fa.models.schedule_tr_row import ScheduleTrRow


@dataclass(frozen=True, slots=True)
class TrPortalCells:
    """The five portal cells, in ``PORTAL_COLUMN_HEADERS_TR`` order."""

    country_code: str
    tin: str
    tax_paid_total: str
    tax_relief_total: str
    section: str

    def as_row(self) -> list[str]:
        return list(astuple(self))


def tr_portal_cells(row: ScheduleTrRow) -> TrPortalCells:
    """Render ``row``'s whole-rupee totals (#900) as they are.

    A ``None`` relief or section renders ``""`` (manual fill); a computed
    zero relief renders ``"0"``.
    """
    relief = row.tax_relief_inr_total
    section = row.section_under_which_claimed
    return TrPortalCells(
        country_code=row.cbdt_country_code,
        tin=row.taxpayer_id_number or "",
        tax_paid_total=str(row.foreign_tax_inr_total),
        tax_relief_total="" if relief is None else str(relief),
        section="" if section is None else str(section),
    )


__all__ = ["TrPortalCells", "tr_portal_cells"]
