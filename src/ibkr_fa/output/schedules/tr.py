"""Schedule TR's filing worksheet rows (#1204): the five portal columns under
the form's headers, one row per country, then Quicko's foreign tax credit
block when the run supplies its inputs. Cells come from
:func:`~ibkr_fa.output._views.tr.tr_portal_cells`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_fsi import require_fsi_relief_matches_portal
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.output._views.tr import tr_portal_cells
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.quicko_foreign_tax import (
    QuickoForeignTaxInput,
    quicko_foreign_tax_entries,
    quicko_foreign_tax_rows,
)


def _iter_rows(
    result: ScheduleTrComputeResult,
    *,
    portal_headers: tuple[str, ...],
    quicko_foreign_tax: QuickoForeignTaxInput | None,
) -> Iterator[list[str]]:
    yield list(portal_headers)
    for row in result.rows:
        cells = tr_portal_cells(row)
        yield replace(
            cells, country_code=f"{row.cbdt_country_code}-{row.country_name_portal}"
        ).as_row()

    if quicko_foreign_tax is not None:
        yield from quicko_foreign_tax_rows(
            quicko_foreign_tax_entries(
                quicko_foreign_tax.form67,
                deductor_name=quicko_foreign_tax.deductor_name,
                foreign_tins=quicko_foreign_tax.foreign_tins,
            )
        )


def tr_filing_rows(ctx: RenderContext) -> Callable[[ScheduleTrComputeResult], Iterator[list[str]]]:
    """The run's TR rows; resolves ``ctx``'s labels and headers now (ITR-2
    headers when ``ctx.itr_form`` is ``None``).

    The returned function checks the relief of the FSI rows the TR result
    carries first, so a relief the portal would overwrite refuses before
    anything is staged. With ``ctx.quicko_foreign_tax``, Quicko's block is
    built from the Form 67 result after the last TR row is read; without it,
    only the TR rows are written.
    """
    labels = for_ay(ctx.assessment_year_start)
    itr_form: ItrForm = "ITR-2" if ctx.itr_form is None else ctx.itr_form
    portal_headers = labels.PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM[itr_form]
    quicko_foreign_tax = ctx.quicko_foreign_tax

    def rows(result: ScheduleTrComputeResult) -> Iterator[list[str]]:
        require_fsi_relief_matches_portal(result.fsi_rows_with_overwritten_relief)
        return _iter_rows(
            result, portal_headers=portal_headers, quicko_foreign_tax=quicko_foreign_tax
        )

    return rows


__all__ = ["tr_filing_rows"]
