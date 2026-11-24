"""Review-CSV writer for Form 67 (Statement of income from a country
or specified territory outside India and Foreign Tax Credit).

CSV contract (locked by ``tests/test_output_form67_review_csv.py``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``) so Excel recognises the
  file as UTF-8 on double-click without manual import-wizard steps.
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 17``
  cells. Header and detail rows all conform. The 17-cell width is the
  union of the 13 portal-form columns (``PORTAL_COLUMN_HEADERS_FORM67``)
  followed by 4 audit columns.
* **No TOTAL row**: like Schedule FSI / TR, Form 67's Part A item 5
  main table accepts ONE row per (country, source-of-income) aggregate
  and the e-filing portal's Form 67 service computes cross-row
  aggregates server-side. The writer therefore emits ONE CSV row per
  :class:`~ibkr_fa.compute.form67.Form67Entry` and stops.
* **Fill status**: the writer emits all 13 portal columns so the
  file is a row-for-row portal mirror. Cols 7-13 are optional on the
  model; a ``None`` is left as an EMPTY cell (NOT zero), explicitly
  signalling "not computed; user must fill before pasting into the
  portal". :mod:`ibkr_fa.compute.form67` owns the fill rules:

  * ``Tax payable on such income under normal provisions in India``
    (col 7) — only from an exact ``[[form67.tax_payable_overrides]]``
    entry; EMPTY for manual calculation otherwise.
  * ``Tax payable on such income under section 115JB/115JC`` (col 8)
    — not computed: the engine always leaves it ``None``. The writer
    still renders a supplied value.
  * ``Credit claimed under section 90 / 90A - Article No. of
    Double Taxation Avoidance Agreement`` (col 9) and ``... - Rate
    of tax as per Double Taxation Avoidance Agreement`` (col 10) —
    the withholding article and treaty rate from the vendored DTAA
    table for a DTAA country; EMPTY otherwise.
  * ``Credit claimed under section 90 / 90A - Amount`` (col 11) —
    ``min(col 7, eligible foreign tax)`` (Rule 128(5)(i)) for a DTAA
    country when col 7 is set and the eligible tax is known; EMPTY
    otherwise.
  * ``Credit claimed under section 91 - Amount`` (col 12) — only when
    the DTAA lookup explicitly returns ``NO_DTAA``,
    ``[form67.section_91]`` is supplied and col 7 is set; EMPTY
    otherwise, including for every DTAA country.
  * ``Total foreign tax credit claimed`` (col 13) — equals whichever
    of cols 11 / 12 is filled (the §90/§91 mutex); EMPTY when
    neither is.

  **Distinguished from a computed zero**: an explicit
  ``Decimal("0")`` in an INR cell (cols 7/8/11/12/13) renders as the
  literal ``"0"`` to signal "computed = ₹0", NOT an empty cell, and a
  zero DTAA rate (col 10) renders as ``"0.00"``. Same convention as
  Schedule TR's ``tax_relief_inr_total`` per PR #215 T3 r1 P2 #1.

  This file is a review/mirror artifact: it shows the per-(country,
  source) foreign-income and foreign-tax-paid totals plus whichever
  of cols 7-13 the compute layer could fill.

* **Section order**:

  1. Header row (17 cells: 13 portal headers + 4 audit headers).
  2. One detail row per :class:`~ibkr_fa.compute.form67.Form67Entry`,
     in the compute layer's sort order (``(cbdt_country_code,
     source_of_income.value)`` ascending, with 1-indexed
     ``serial_number`` preserved). The writer does NOT re-sort.

* **Rounding**: none here. Portal monetary cells are the whole-rupee
  values and portal rate cells (cols 6 and 10) the two-place values
  compute rounded half-up (:attr:`Form67Entry.filed
  <ibkr_fa.models.form67_entry.Form67Entry.filed>`, #898), written as
  ``str(value)``. Audit monetary cells preserve full
  :class:`~decimal.Decimal` precision via ``str(value)``.
* **Writer-contract safety helpers**: every cell of every row
  passes through :func:`_assert_no_nul_in_row` and
  :func:`_assert_no_csv_formula_injection` before writerow.
* **Atomic write**: staged and committed through
  :func:`~ibkr_fa.output._atomic.atomic_text_output`.

Per-AY portal labels (issue #65): mirrors the sister Schedule TR / FSI
writer's constructor signature. ``assessment_year_start`` defaults to
the package-level current AY and selects the header row's labels.

PR (post-#221 ship) step 2/N of #37 introduces this writer.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Entry
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

_AUDIT_HEADERS: Final[tuple[str, ...]] = (
    "_audit: ISO alpha-2",
    "_audit: Foreign tax INR (full precision)",
    "_audit: Underlying WHT events aggregated count",
    "_audit: Indian tax payable requires manual calculation",
)

# Column count = 13 (portal) + 4 (audit) = 17. Pinned by the round-trip
# tests in tests/test_output_form67_review_csv.py so a future column addition
# fails loudly until the test is also updated. Form 67 has no separate
# audit row for income full-precision because col 4 income is computed
# upstream by Schedule OS (already audited there); the audit ISO is the
# trace-back anchor for cross-form reconciliation.
_COLUMN_COUNT: Final[int] = 17


def _header_row(labels: ModuleType) -> list[str]:
    """Build the 17-cell header row: 13 portal headers + 4 audit headers."""
    return [*labels.PORTAL_COLUMN_HEADERS_FORM67, *_AUDIT_HEADERS]


def _format_optional(value: Decimal | str | None) -> str:
    """Render an optional cell: None ("not computed; user must fill") → EMPTY.

    Amount and rate cells receive compute's filed values, so ``str`` writes
    them as they are: a computed zero amount is ``"0"`` and a zero rate
    ``"0.00"``, NOT empty. Same convention as Schedule TR's relief cell
    (:func:`~ibkr_fa.output._views.tr.tr_portal_cells`) per PR #215 T3 r1
    P2 #1 — distinguishes "computed zero" from "not computed" so the user
    can tell which cells the compute layer actually evaluated. The col 9
    DTAA article is written verbatim (the model invariant enforces non-empty
    / non-whitespace at construction; see ``Form67Entry`` post-init).
    """
    if value is None:
        return ""
    return str(value)


def _detail_row(entry: Form67Entry) -> list[str]:
    """Render one :class:`Form67Entry` as a 17-cell CSV row.

    Amount and rate cells come from :attr:`Form67Entry.filed`, which
    compute rounds (#898); this writer doesn't round.

    Portal cells (13):
      * Sl. No. — 1-indexed serial from compute layer's sort.
      * Name of the country / specified territory — combined display
        as ``"<cbdt_country_code>-<country_name_portal>"`` using the
        entry's stored fields (T3 r0 P2 of PR #226 changed from a
        ``format_country_for_portal(iso_alpha2)`` re-derivation to
        the stored fields so the writer respects directly-constructed
        entries whose CBDT code/name fields may differ from the
        ISO-lookup-derived defaults).
      * Source of income — enum value verbatim (``"Dividend"`` in v1).
      * Income from outside India (₹) — filed whole rupees.
      * Tax paid outside India - Amount (₹) — filed whole rupees.
      * Tax paid outside India - Rate (%) — filed, two decimal places.
      * Tax payable on such income under normal provisions in India
        — filed whole rupees; EMPTY when ``None``.
      * Tax payable on such income under section 115JB/JC — filed
        whole rupees; not computed by the engine, so EMPTY unless
        a value is supplied.
      * Credit claimed under section 90 / 90A - Article No. of DTAA
        — verbatim; EMPTY when ``None``.
      * Credit claimed under section 90 / 90A - Rate of tax as per
        DTAA — filed, two decimal places; EMPTY when ``None``.
      * Credit claimed under section 90 / 90A - Amount — filed
        whole rupees; EMPTY when ``None``.
      * Credit claimed under section 91 - Amount — filed whole
        rupees; EMPTY when ``None`` (always, for a DTAA country).
      * Total foreign tax credit claimed — filed whole rupees;
        EMPTY when ``None``.

      The module docstring gives when each of these is filled; a
      ``Decimal(0)`` amount renders as ``"0"``, not EMPTY.

    Audit cells (4):
      * ISO alpha-2 — verbatim from the compute entry, for
        traceability back to the source ISIN's leading two
        characters and cross-form (FSI / TR) reconciliation.
      * Foreign tax INR (full precision) — ``str(Decimal)`` of the
        unrounded foreign_tax_inr so the spreadsheet user can
        reconcile to the upstream WHT aggregator output and detect
        any rounding drift between Form 67 col 5 (filed whole rupees)
        and Schedule FSI's WHT total.
      * Underlying WHT events aggregated count — integer count of
        underlying in-FY WHT events (INCLUDING refund / reversal
        rows that net against paid rows; matches Schedule FSI's WHT
        aggregator semantic).
      * Indian tax payable requires manual calculation — ``true`` when
        col 7 is blank because no exact source-level amount was supplied.
    """
    filed = entry.filed
    return [
        str(entry.serial_number),
        f"{entry.cbdt_country_code}-{entry.country_name_portal}",
        entry.source_of_income.value,
        str(filed.income_inr),
        str(filed.foreign_tax_inr),
        str(filed.tax_paid_rate_percent),
        _format_optional(filed.tax_payable_normal_inr),
        _format_optional(filed.tax_payable_special_inr),
        _format_optional(entry.section_90_dtaa_article),
        _format_optional(filed.section_90_dtaa_rate_percent),
        _format_optional(filed.section_90_credit_inr),
        _format_optional(filed.section_91_credit_inr),
        _format_optional(filed.total_credit_inr),
        entry.iso_alpha2,
        str(entry.foreign_tax_inr),
        str(entry.event_count),
        str(entry.tax_payable_normal_inr is None).lower(),
    ]


def _iter_rows(
    result: Form67ComputeResult,
    *,
    labels: ModuleType,
) -> Iterator[list[str]]:
    """Yield the header row followed by one detail row per result entry.

    The compute layer guarantees ``result.entries`` is already sorted
    by ``(cbdt_country_code, source_of_income.value)`` with 1-indexed
    ``serial_number`` preserved; the writer preserves that order.
    """
    yield _header_row(labels)
    for entry in result.entries:
        yield _detail_row(entry)


@register(OutputKey(Destination.REVIEW, Artifact.FORM67_CSV))
class Form67ReviewCsvWriter:
    """Write a Form 67 compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an optional
    ``assessment_year_start`` constructor argument. When the pipeline
    passes ``financial_year.assessment_year_start``, the writer
    sources Form 67 portal labels from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default.
    """

    format_id: ClassVar[str] = "review-csv"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, result: Form67ComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` using an atomic temp-file rename."""
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(
                csv.writer(fh), _iter_rows(result, labels=self._labels), start_index=0
            )


__all__ = ["Form67ReviewCsvWriter"]
