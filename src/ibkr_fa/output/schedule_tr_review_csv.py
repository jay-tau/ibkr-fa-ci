"""Review-CSV writer for Schedule TR (Summary of Tax Relief Claimed).

CSV contract (locked by ``tests/test_schedule_tr_review_csv.py`` +
``tests/test_writer_contract.py``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``) so Excel recognises the
  file as UTF-8 on double-click without manual import-wizard steps.
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 9``
  cells. Header and detail rows all conform. The 9-cell width is the
  union of the 5 portal-form columns (``PORTAL_COLUMN_HEADERS_TR``)
  followed by 4 audit columns (``_audit: ISO alpha-2``,
  ``_audit: Country name (portal)``,
  ``_audit: Total tax paid INR (full precision)``,
  ``_audit: FSI rows aggregated count``).
* **No TOTAL row**: like Schedule FSI, the e-filing portal's Schedule
  TR accepts one row per source country and computes its own
  cross-country aggregates server-side (the DTAA-vs-non-DTAA
  sub-totals are not per-row constructs). The writer therefore emits
  ONE CSV row per :class:`~ibkr_fa.models.ScheduleTrRow` and stops.
* **Fill status**: the writer emits all 5 portal columns so the
  file is a row-for-row portal mirror. Some columns are filled
  only when their inputs exist and are otherwise left as EMPTY
  cells (NOT zero), explicitly signalling "not computed; user must
  fill before pasting into the portal". Affected portal columns:

  * ``Taxpayer Identification Number`` — from
    ``[account.foreign_tins]``; EMPTY when the country has no TIN.
  * ``Total tax relief available in India`` — the country's
    computed FSI relief, which needs exact Indian tax from
    ``[[form67.tax_payable_overrides]]``; EMPTY without it or
    when another gate in
    :func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`
    leaves it uncomputed. **Distinguished from a computed-zero
    relief**: a computed ``Decimal("0")`` renders as the literal
    ``"0"`` to signal "computed = ₹0", NOT an empty cell. See
    PR #215 T3 r1 P2 #1.
  * ``Section under which relief claimed`` — set together with
    the relief; EMPTY whenever the relief is EMPTY.

  This file is therefore a review/mirror artifact: it shows the
  country-level foreign-tax-paid totals plus whichever of these
  columns the compute layer could fill.

* **Section order**:

  1. Header row (9 cells: 5 portal headers + 4 audit headers).
  2. One detail row per :class:`~ibkr_fa.models.ScheduleTrRow`, in
     the compute layer's sort order (``cbdt_country_code``
     ascending; lexicographic). The writer does NOT re-sort.

* **Rounding**: none here. Portal monetary cells are the TR row's
  whole-rupee totals, which compute sums from the FSI rows' filed
  values (#900), written as ``str(value)``. Audit monetary cells
  preserve full :class:`~decimal.Decimal` precision via ``str(value)``.
* **Relief the portal would overwrite**: a result carrying FSI rows
  whose relief the portal's read-only FSI cell would overwrite is
  refused before anything is written.
* **Writer-contract safety helpers**: every cell of every row
  passes through :func:`_assert_no_nul_in_row` and
  :func:`_assert_no_csv_formula_injection` before writerow.
* **Atomic write**: staged and committed through
  :func:`~ibkr_fa.output._atomic.atomic_text_output`.

Per-AY portal labels (issue #65): mirrors the sister Schedule FSI
writer's constructor signature. ``assessment_year_start`` defaults
to the package-level current AY and selects the header row's labels.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_fsi import require_fsi_relief_matches_portal
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._views.tr import tr_portal_cells
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

_AUDIT_HEADERS: Final[tuple[str, ...]] = (
    "_audit: ISO alpha-2",
    "_audit: Country name (portal)",
    "_audit: Total tax paid INR (full precision)",
    "_audit: FSI rows aggregated count",
)

# Column count = 5 (portal) + 4 (audit) = 9. Pinned by the round-trip
# tests in tests/test_schedule_tr_review_csv.py.
_COLUMN_COUNT: Final[int] = 9


def _header_row(headers: tuple[str, ...]) -> list[str]:
    """Build the 9-cell header row: 5 portal headers + 4 audit headers."""
    return [*headers, *_AUDIT_HEADERS]


def _detail_row(row: ScheduleTrRow) -> list[str]:
    """Render one :class:`ScheduleTrRow` as a 9-cell review row.

    Portal cells (5), rendered by
    :func:`~ibkr_fa.output._views.tr.tr_portal_cells`:
      * Country Code — CBDT 3-digit numeric.
      * TIN — the per-country TIN carried from Schedule FSI; EMPTY
        when the country has none.
      * Total tax paid outside India (₹) — whole rupees, as they are.
      * Total tax relief available in India (₹) — whole rupees, as
        they are; EMPTY when ``None`` (left for manual fill, see
        :func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`).
        ``Decimal(0)`` renders as literal "0" to distinguish
        "computed zero" from "not computed".
      * Section under which relief claimed — set together with the
        relief; EMPTY when ``None``.

    Audit cells (4):
      * ISO alpha-2 — verbatim from the compute row.
      * Country name (portal) — ALL-CAPS display name.
      * Total tax paid INR (full precision) — ``str(Decimal)`` of
        the full-precision FSI sum when populated by compute (PR
        #216 T2/T3 r2 fix); falls back to the portal-displayed
        ``foreign_tax_inr_total`` when ``foreign_tax_inr_total_full_precision``
        is ``None`` (legacy direct constructors that don't track
        full precision separately).
      * FSI rows aggregated count — integer count of upstream
        FSI rows that fed this TR row.
    """
    full_precision_total = (
        row.foreign_tax_inr_total_full_precision
        if row.foreign_tax_inr_total_full_precision is not None
        else row.foreign_tax_inr_total
    )
    return [
        *tr_portal_cells(row).as_row(),
        row.iso_alpha2,
        row.country_name_portal,
        str(full_precision_total),
        str(row.event_count),
    ]


def _iter_rows(
    result: ScheduleTrComputeResult,
    *,
    portal_headers: tuple[str, ...],
) -> Iterator[list[str]]:
    """Yield the header row followed by one detail row per result row.

    The compute layer guarantees ``result.rows`` is already sorted
    by ``cbdt_country_code``; the writer preserves that order.
    """
    yield _header_row(portal_headers)
    for row in result.rows:
        yield _detail_row(row)


@register(OutputKey(Destination.REVIEW, Artifact.TR_CSV))
class ScheduleTrReviewCsvWriter:
    """Write a Schedule TR compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an
    optional ``assessment_year_start`` constructor argument. When
    the pipeline passes ``financial_year.assessment_year_start``,
    the writer sources Schedule TR portal labels from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default.
    """

    format_id: ClassVar[str] = "review-csv"

    def __init__(
        self, *, assessment_year_start: int | None = None, itr_form: ItrForm = "ITR-2"
    ) -> None:
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)
        self._portal_headers = self._labels.PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM[itr_form]

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY and ITR form (ITR-2 headers when ``None``)."""
        if ctx.itr_form is None:
            return cls(assessment_year_start=ctx.assessment_year_start)
        return cls(assessment_year_start=ctx.assessment_year_start, itr_form=ctx.itr_form)

    def write(self, result: ScheduleTrComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` using an atomic temp-file rename."""
        require_fsi_relief_matches_portal(result.fsi_rows_with_overwritten_relief)
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(
                csv.writer(fh),
                _iter_rows(result, portal_headers=self._portal_headers),
                start_index=0,
            )


__all__ = ["ScheduleTrReviewCsvWriter"]
