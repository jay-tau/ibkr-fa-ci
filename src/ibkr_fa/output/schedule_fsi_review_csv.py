"""Review-CSV writer for Schedule FSI (Foreign Source Income).

CSV contract (locked by ``tests/test_schedule_fsi_review_csv.py`` +
``tests/test_writer_contract.py``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``) so Excel recognises the
  file as UTF-8 on double-click without manual import-wizard steps.
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 13``
  cells. Header and detail rows all conform. The 13-cell width is the
  union of the 8 portal-form columns (``PORTAL_COLUMN_HEADERS_FSI``)
  followed by 5 audit columns (``_audit: ISO alpha-2``,
  ``_audit: Country name (portal)``,
  ``_audit: Income INR (full precision)``,
  ``_audit: Tax INR (full precision)``, ``_audit: Event count``).
  Note: Schedule FSI has NO separate "Country Name" portal column —
  only "Country Code" (CBDT 3-digit numeric); the vendored ALL-CAPS
  ``country_name_portal`` lookup is preserved in the audit block for
  human-readability and trace-back.
* **No TOTAL row**: unlike Schedule OS / CG, the e-filing portal's
  Schedule FSI accepts one row per ``(country_code, head_of_income)``
  and computes its own totals server-side. The writer therefore emits
  ONE CSV row per :class:`~ibkr_fa.models.ScheduleFsiRow` and stops —
  no audit-TOTAL or portal-TOTAL row is appended. Users who want a
  running grand-total can drop ``=SUM(...)`` over the
  ``_audit: Income INR (full precision)`` column themselves.
* **Fill status**: the writer emits all 8 portal columns so the
  file is a row-for-row portal mirror. Country Code, Head of Income,
  Income and Tax paid are always filled. The other four columns
  carry what the compute layer filled on the row (rules in
  :mod:`ibkr_fa.compute.schedule_fsi_relief`):

  * ``Taxpayer Identification Number`` — stamped from
    ``[account.foreign_tins]`` in ``user.toml``; EMPTY when no TIN is
    configured for the country.
  * ``Tax relief available in India`` — a computed ``0`` for a row
    with no foreign tax, on any head. For an ``Other Sources`` row
    with a Form 67 dividend credit, that credit.
  * ``Tax payable on such income under normal provisions in India`` —
    for that ``Other Sources`` row, the Form 67 entry's column 7,
    plus the resolved Indian tax on any other income (interest,
    other sources) the row combines.
  * ``Relevant article of DTAA`` — for that row, the treaty's relief
    article.

  An EMPTY cell (NOT zero) means "not computed; fill by hand before
  pasting into the portal". That covers the tax payable on a row
  with no foreign tax, and all three FTC cells on any other row
  without a Form 67 credit to copy (for example Capital Gains with
  foreign tax). One blank is correct as it stands: the DTAA article
  beside a §91 credit, which has no treaty article to cite. A
  computed Form 67 credit that can't be reconciled with its FSI row
  (for example a row that also includes interest whose Indian tax
  isn't resolved) is never left blank; the run refuses with
  :class:`~ibkr_fa.exceptions.InvariantError` before writing.

* **Section order**:

  1. Header row (13 cells: 8 portal headers + 5 audit headers).
  2. One detail row per :class:`~ibkr_fa.models.ScheduleFsiRow`, in
     the compute layer's sort order (``(cbdt_country_code,
     head_of_income.value)`` ascending; lexicographic on the CBDT
     code per
     :class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`).
     The writer does NOT re-sort; it preserves the upstream order.

* **Rounding**: none here. Portal monetary cells are the whole-rupee
  values compute rounded half-up (:attr:`ScheduleFsiRow.filed
  <ibkr_fa.models.schedule_fsi_row.ScheduleFsiRow.filed>`, #900),
  written as ``str(value)``. Audit monetary cells preserve full
  :class:`~decimal.Decimal` precision via ``str(value)``.
* **Relief the portal would overwrite** is refused before anything is
  written (:func:`~ibkr_fa.compute.schedule_fsi.require_fsi_relief_matches_portal`).
* **Writer-contract safety helpers** (see
  :mod:`ibkr_fa.output._writer_contract`): every cell of every row
  passes through :func:`_assert_no_nul_in_row` (binary-contamination
  guard) and :func:`_assert_no_csv_formula_injection` (ASCII + C0
  whitespace + Unicode full-width formula-trigger rejection) BEFORE
  it reaches :func:`csv.writer.writerow`. Violations raise
  :class:`~ibkr_fa.output._writer_contract.WriterContractError`
  carrying the row + cell index.
* **Atomic write**: staged and committed through
  :func:`~ibkr_fa.output._atomic.atomic_text_output`.

Per-AY portal labels (issue #65): mirrors the Schedule OS / CG
writers' constructor signature. ``assessment_year_start`` defaults
to the package-level current AY; passing a different AY routes to
the matching per-AY snapshot via
:func:`ibkr_fa.output.portal_labels.for_ay`, which selects the header
row's labels. v1's Schedule FSI column headers do not change across
the AYs the package currently supports, but the per-AY threading is
kept for future-proof consistency with the sister writers.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.models.schedule_fsi_row import ScheduleFsiRow
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._views.fsi import fsi_portal_cells
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

_AUDIT_HEADERS: Final[tuple[str, ...]] = (
    "_audit: ISO alpha-2",
    "_audit: Country name (portal)",
    "_audit: Income INR (full precision)",
    "_audit: Tax INR (full precision)",
    "_audit: Event count",
)

# Column count = 8 (portal) + 5 (audit) = 13. Pinned by the round-trip
# tests in tests/test_schedule_fsi_review_csv.py so a future column
# addition fails loudly until the test is also updated. Note: Schedule
# FSI does NOT have a separate "Country Name" portal column — only
# "Country Code"; the vendored country_name_portal is preserved as an
# AUDIT column for human-readability and trace-back to the source ISIN.
_COLUMN_COUNT: Final[int] = 13


def _header_row(labels: ModuleType) -> list[str]:
    """Build the 13-cell header row: 8 portal headers + 5 audit headers."""
    return [*labels.PORTAL_COLUMN_HEADERS_FSI, *_AUDIT_HEADERS]


def _detail_row(row: ScheduleFsiRow) -> list[str]:
    """Render one :class:`ScheduleFsiRow` as a 13-cell review row.

    Portal cells (8):
      * Country Code, Head of Income, Income (₹), Tax (₹)
        — populated from the compute layer.
      * TIN — the stamped foreign TIN, or EMPTY when none is configured.
      * Tax payable, Tax relief, DTAA article — the row's FTC values
        (#36/#37, reused from Form 67), or EMPTY where the row leaves
        them unset; the module docstring says what an EMPTY cell means.

    Audit cells (5):
      * ISO alpha-2 — verbatim from the compute row, for traceability
        back to the source ISIN's leading two characters.
      * Country name (portal) — the ALL-CAPS display name from the
        vendored country-codes mapping (preserved here, not in the
        portal columns, because Schedule FSI does not have a "Country
        Name" column on the form).
      * Income / Tax INR (full precision) — ``str(Decimal)`` of the
        unrounded amounts so the spreadsheet user can reconcile to
        the upstream OS / CG / interest aggregator outputs.
      * Event count — integer count of underlying source events,
        joining FSI back to the per-event lines in those aggregators.
    """
    return [
        *fsi_portal_cells(row).as_row(),
        row.iso_alpha2,
        row.country_name_portal,
        str(row.income_inr),
        str(row.foreign_tax_inr),
        str(row.event_count),
    ]


def _iter_rows(
    result: ScheduleFsiComputeResult,
    *,
    labels: ModuleType,
) -> Iterator[list[str]]:
    """Yield the header row followed by one detail row per result row.

    The compute layer guarantees ``result.rows`` is already sorted
    by ``(cbdt_country_code, head_of_income.value)``; the writer
    preserves that order (no re-sort).
    """
    yield _header_row(labels)
    for row in result.rows:
        yield _detail_row(row)


@register(OutputKey(Destination.REVIEW, Artifact.FSI_CSV))
class ScheduleFsiReviewCsvWriter:
    """Write a Schedule FSI compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an optional
    ``assessment_year_start`` constructor argument. When the pipeline
    passes ``financial_year.assessment_year_start``, the writer
    sources Schedule FSI portal labels from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default. See
    :class:`ibkr_fa.output.review_csv.ReviewCsvWriter` for the full
    rationale.
    """

    format_id: ClassVar[str] = "review-csv"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See :class:`ibkr_fa.output.review_csv.ReviewCsvWriter.__init__`
        for the contract — the rationale is identical.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, result: ScheduleFsiComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` using an atomic temp-file rename."""
        require_fsi_relief_matches_portal(result.rows)
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(
                csv.writer(fh), _iter_rows(result, labels=self._labels), start_index=0
            )


__all__ = ["ScheduleFsiReviewCsvWriter"]
