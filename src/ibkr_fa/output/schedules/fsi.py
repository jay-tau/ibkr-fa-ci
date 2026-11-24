"""Schedule FSI's filing worksheet rows (#1204), laid out the way the portal
form is: country code and TIN once per country/TIN pair, then a block per
head of income. Cells come from :func:`~ibkr_fa.output._views.fsi.fsi_portal_cells`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Final

from ibkr_fa.compliance import missing_indian_tax_payable_warning
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.models.schedule_fsi_row import ScheduleFsiRow
from ibkr_fa.output._views.fsi import FSI_PORTAL_FIELDS, FsiPortalCells, fsi_portal_cells
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.portal_labels import for_ay

_COLUMN_COUNT: Final[int] = 2


def _grouped_rows(result: ScheduleFsiComputeResult, *, labels: ModuleType) -> Iterator[list[str]]:
    """Yield one ``label, value`` row per field, grouped by country.

    The portal's Schedule FSI is a stacked form — country code and TIN once,
    then a block per head of income — not a wide table, so the file follows
    that order and can be read straight down while filling it in.
    """
    headers = dict(zip(FSI_PORTAL_FIELDS, labels.PORTAL_COLUMN_HEADERS_FSI, strict=True))
    by_country: dict[tuple[str, str], list[tuple[ScheduleFsiRow, FsiPortalCells]]] = {}
    for row in result.rows:
        cells = fsi_portal_cells(row)
        # The portal dropdown displays and accepts the combined CBDT code + country
        # name, not the bare numeric code.
        country = f"{cells.country_code}-{row.country_name_portal}"
        by_country.setdefault((country, cells.tin), []).append((row, cells))

    for index, ((country_code, tin), entries) in enumerate(by_country.items()):
        if index:
            yield [""] * _COLUMN_COUNT
        yield [headers["country_code"], country_code]
        yield [headers["tin"], tin]
        for row, cells in entries:
            yield [""] * _COLUMN_COUNT
            yield [headers["head"], cells.head]
            yield [headers["income"], cells.income]
            yield [headers["tax_paid"], cells.tax_paid]
            yield [headers["tax_payable"], cells.tax_payable]
            if row.needs_indian_tax_payable:
                guidance = missing_indian_tax_payable_warning(row.override_advice)
                yield ["Manual fill guidance", guidance]
            # tax_relief is read-only on the portal: it auto-populates as min((c), (d)).
            yield [headers["dtaa_article"], row.filed.dtaa_article_number or ""]


def fsi_filing_rows(
    ctx: RenderContext,
) -> Callable[[ScheduleFsiComputeResult], Iterator[list[str]]]:
    """The run's FSI rows; resolves ``ctx``'s labels now.

    The returned function checks every row's relief first, so a relief the
    portal would overwrite refuses before anything is staged. The rows it
    returns are grouped when the first one is read.
    """
    labels = for_ay(ctx.assessment_year_start)

    def rows(result: ScheduleFsiComputeResult) -> Iterator[list[str]]:
        require_fsi_relief_matches_portal(result.rows)
        return _grouped_rows(result, labels=labels)

    return rows


__all__ = ["fsi_filing_rows"]
