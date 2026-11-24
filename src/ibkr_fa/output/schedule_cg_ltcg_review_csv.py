"""Review-CSV writer for Schedule CG LTCG (foreign long-term capital gains).

Mirror of :mod:`ibkr_fa.output.schedule_cg_review_csv` for the LTCG
pathway. Same atomic-write contract, BOM (``utf-8-sig``), uniform
row width, marker-row audit blocks, and writer-contract safety
helpers (NUL + CSV-formula-injection guards on every cell).

CSV contract (locked by ``tests/test_schedule_cg_ltcg_review_csv.py``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``).
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 16``
  cells (matches the STCG writer width since the row schema is
  identical — open/close trade dates, holding period, separate
  cost-basis/proceeds USD, single Rule 115 specified-date, SBI TTBR
  source date (blank unless carried back, #741) + TTBR,
  cost-basis/proceeds INR, rounded realized gain).
* **Section order**: header → in-FY LTCG detail rows (sorted
  ``(close_date, symbol)``) → optional ``_excluded_stcg:`` audit
  block → optional ``_excluded_pre_2024_07_23:`` audit block →
  optional ``_excluded_indian_source_ltcg:`` audit block → optional
  ``_excluded_out_of_fy_ltcg:`` audit block → ``_table_f:`` block →
  audit TOTAL row →
  :data:`~ibkr_fa.output.portal_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG`
  TOTAL row.
* **Marker rows**: prefixes ``_excluded_stcg:``,
  ``_excluded_pre_2024_07_23:``,
  ``_excluded_indian_source_ltcg:``, ``_excluded_out_of_fy_ltcg:``
  are module-level constants and re-exported via
  :mod:`ibkr_fa.output`. The LTCG-flavoured audit-block prefixes
  are DISTINCT from the STCG-flavoured ones so a tool consuming
  both CSVs in sequence does not accidentally cross-route.
* **Table F block** (#1361): the STCG writer's block
  (:func:`~ibkr_fa.output.schedule_cg_review_csv.table_f_rows`), always
  written, with Schedule CG Table F row 5's five date bands from the
  result's
  :attr:`~ibkr_fa.compute.schedule_cg_ltcg.ScheduleCgLtcgComputeResult.table_f`.
* **TOTAL-row contract**: the LITERAL last row's first cell equals
  :data:`PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG` exactly. Last cell is
  the filed ₹ total, the result's
  :attr:`~ibkr_fa.compute.schedule_cg_portal_amounts.ScheduleCgPortalAmounts.filed_total_inr`
  (rounded consideration less rounded cost, #615).
  Per the T4 review fix on the STCG writer, the audit-TOTAL row's
  value sits in the SAME last cell, not under the "Proceeds INR"
  column.
* **OWASP escapes**: signed cells (realized_gain_inr, proceeds_usd,
  proceeds_inr, audit-block lot.cost_basis_money + proceeds_money)
  routed through ``csv_safe_signed``; free-text upstream cells
  (symbol, ISIN, description) routed through ``_csv_safe_text``.
  Same defense-in-depth pattern as the STCG writer.
* **Atomic write**: same temp-file-then-replace pattern as the OS
  writer; identical guard semantics.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import (
    _FORMULA_TRIGGERS,
    csv_safe_signed,
    write_checked_rows,
)
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.schedule_cg_review_csv import (
    table_f_band_labels,
    table_f_rows,
)

_COLUMN_COUNT: Final[int] = 16

EXCLUDED_STCG_MARKER: Final[str] = "_excluded_stcg:"
EXCLUDED_PRE_CUTOVER_MARKER: Final[str] = "_excluded_pre_2024_07_23:"
EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER: Final[str] = "_excluded_indian_source_ltcg:"
EXCLUDED_LTCG_OUT_OF_FY_MARKER: Final[str] = "_excluded_out_of_fy_ltcg:"

STCG_DEFERRED_FOLLOW_UP_ISSUE: Final[str] = "#29"
STCG_DEFERRED_REASON: Final[str] = (
    "Short-term — holding <= 24 months; belongs on Schedule CG / A (STCG) "
    "via the schedule-cg subcommand"
)
PRE_CUTOVER_FOLLOW_UP_ISSUE: Final[str] = ""
PRE_CUTOVER_REASON: Final[str] = (
    "Disposed before 2024-07-23 — old regime (20% with indexation via "
    "CBDT Cost Inflation Index) — out of v1 scope; no dedicated tracker "
    "issue yet"
)
IN_SOURCE_CG_LTCG_FOLLOW_UP_ISSUE: Final[str] = ""
IN_SOURCE_CG_LTCG_REASON: Final[str] = (
    "Indian-source — domestic CG via different Schedule CG section "
    "(out of v1 scope; no dedicated tracker issue yet)"
)
OUT_OF_FY_LTCG_REASON: Final[str] = "close_date outside the requested financial year window"

_HEADER_ROW: Final[list[str]] = [
    "Symbol",
    "ISIN",
    "Country (ISO 3166-1 alpha-2)",
    "Open date (DD/MM/YYYY)",
    "Close date (DD/MM/YYYY)",
    "Holding period (days)",
    "Quantity",
    "_audit: Cost basis USD",
    "_audit: Proceeds USD",
    "_audit: Transfer preceding-month-end date (YYYY-MM-DD)",
    "_audit: SBI TTBR source date (YYYY-MM-DD)",
    "_audit: Transfer SBI TTBR (INR/USD)",
    "_audit: Statutory cost basis INR (full precision)",
    "_audit: Proceeds INR (full precision)",
    "_audit: Statutory cost basis source",
    "Realized gain INR (rounded ₹)",
]

_STCG_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_STCG_MARKER,
    "reason",
    "follow_up_issue",
    "symbol",
    "isin",
    "open_date",
    "close_date",
    "holding_period_days",
    "quantity",
    "cost_basis_usd",
    "proceeds_usd",
    "description",
    "",
    "",
    "",
    "",
]

_PRE_CUTOVER_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_PRE_CUTOVER_MARKER,
    "reason",
    "follow_up_issue",
    "symbol",
    "isin",
    "open_date",
    "close_date",
    "holding_period_days",
    "quantity",
    "cost_basis_usd",
    "proceeds_usd",
    "description",
    "",
    "",
    "",
    "",
]

_IN_SOURCE_CG_LTCG_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
    "reason",
    "follow_up_issue",
    "symbol",
    "isin",
    "open_date",
    "close_date",
    "quantity",
    "cost_basis_usd",
    "proceeds_usd",
    "description",
    "",
    "",
    "",
    "",
    "",
]

_OUT_OF_FY_LTCG_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_LTCG_OUT_OF_FY_MARKER,
    "reason",
    "symbol",
    "isin",
    "open_date",
    "close_date",
    "quantity",
    "cost_basis_usd",
    "proceeds_usd",
    "description",
    "",
    "",
    "",
    "",
    "",
    "",
]

_AUDIT_TOTAL_LABEL: Final[str] = "_audit: TOTAL Realized gain INR (full precision)"


def _csv_safe_text(value: str) -> str:
    """OWASP single-quote escape for upstream-derived text cells.

    Mirror of ``ibkr_fa.output.schedule_cg_review_csv._csv_safe_text``.
    """
    if value == "":
        return value
    if value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    stripped = value.lstrip()
    if stripped == "":
        return value
    if stripped[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def _blank_row() -> list[str]:
    return [""] * _COLUMN_COUNT


def _detail_row(line: ScheduleCgLtcgLine) -> list[str]:
    # Blank unless the policy carried back to an earlier SBI row (#741),
    # as in the Schedule OS review CSV.
    if line.fx_rate_publication_date == line.fx_specified_date:
        rate_publication_cell = ""
    else:
        rate_publication_cell = line.fx_rate_publication_date.isoformat()
    return [
        _csv_safe_text(str(line.symbol)),
        _csv_safe_text(str(line.isin)),
        line.issuer_country_iso2,
        line.open_date.strftime("%d/%m/%Y"),
        line.close_date.strftime("%d/%m/%Y"),
        str(line.holding_period_days),
        str(line.quantity),
        str(line.cost_basis_usd),
        csv_safe_signed(line.proceeds_usd),
        line.fx_specified_date.isoformat(),
        rate_publication_cell,
        str(line.fx_rate_inr_per_usd),
        str(line.cost_basis_inr),
        csv_safe_signed(line.proceeds_inr),
        _csv_safe_text(line.cost_basis_inr_source),
        csv_safe_signed(to_whole_rupees(line.realized_gain_inr)),
    ]


def _iter_rows(
    result: ScheduleCgLtcgComputeResult,
    amounts: ScheduleCgPortalAmounts,
    *,
    portal_label: str = _default_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    table_f_labels: tuple[str, ...],
) -> Iterator[list[str]]:
    yield list(_HEADER_ROW)

    for line in result.ltcg_lines:
        yield _detail_row(line)

    if result.excluded_stcg:
        yield _blank_row()
        yield list(_STCG_MARKER_ROW)
        for deferred in result.excluded_stcg:
            lot = deferred.closed_lot
            yield [
                _csv_safe_text(str(lot.symbol)),
                STCG_DEFERRED_REASON,
                STCG_DEFERRED_FOLLOW_UP_ISSUE,
                _csv_safe_text(str(lot.symbol)),
                _csv_safe_text(str(lot.isin)),
                lot.open_date_time.isoformat(),
                lot.close_date_time.isoformat(),
                str(deferred.holding_period_days),
                str(lot.quantity),
                csv_safe_signed(lot.cost_basis_money),
                csv_safe_signed(lot.proceeds_money),
                _csv_safe_text(lot.description),
                "",
                "",
                "",
                "",
            ]

    if result.excluded_pre_2024_07_23:
        yield _blank_row()
        yield list(_PRE_CUTOVER_MARKER_ROW)
        for deferred in result.excluded_pre_2024_07_23:
            lot = deferred.closed_lot
            yield [
                _csv_safe_text(str(lot.symbol)),
                PRE_CUTOVER_REASON,
                PRE_CUTOVER_FOLLOW_UP_ISSUE,
                _csv_safe_text(str(lot.symbol)),
                _csv_safe_text(str(lot.isin)),
                lot.open_date_time.isoformat(),
                lot.close_date_time.isoformat(),
                str(deferred.holding_period_days),
                str(lot.quantity),
                csv_safe_signed(lot.cost_basis_money),
                csv_safe_signed(lot.proceeds_money),
                _csv_safe_text(lot.description),
                "",
                "",
                "",
                "",
            ]

    if result.excluded_indian_source:
        yield _blank_row()
        yield list(_IN_SOURCE_CG_LTCG_MARKER_ROW)
        for lot in result.excluded_indian_source:
            yield [
                _csv_safe_text(str(lot.symbol)),
                IN_SOURCE_CG_LTCG_REASON,
                IN_SOURCE_CG_LTCG_FOLLOW_UP_ISSUE,
                _csv_safe_text(str(lot.symbol)),
                _csv_safe_text(str(lot.isin)),
                lot.open_date_time.isoformat(),
                lot.close_date_time.isoformat(),
                str(lot.quantity),
                csv_safe_signed(lot.cost_basis_money),
                csv_safe_signed(lot.proceeds_money),
                _csv_safe_text(lot.description),
                "",
                "",
                "",
                "",
                "",
            ]

    if result.excluded_out_of_fy:
        yield _blank_row()
        yield list(_OUT_OF_FY_LTCG_MARKER_ROW)
        for lot in result.excluded_out_of_fy:
            yield [
                _csv_safe_text(str(lot.symbol)),
                OUT_OF_FY_LTCG_REASON,
                _csv_safe_text(str(lot.symbol)),
                _csv_safe_text(str(lot.isin)),
                lot.open_date_time.isoformat(),
                lot.close_date_time.isoformat(),
                str(lot.quantity),
                csv_safe_signed(lot.cost_basis_money),
                csv_safe_signed(lot.proceeds_money),
                _csv_safe_text(lot.description),
                "",
                "",
                "",
                "",
                "",
                "",
            ]

    yield _blank_row()
    for first, second, amount in table_f_rows(result.table_f, table_f_labels):
        yield [
            first,
            second or "",
            *[""] * (_COLUMN_COUNT - 3),
            "" if amount is None else csv_safe_signed(amount),
        ]

    # T4 / STCG-writer parity: the audit-TOTAL value sits in the LAST
    # cell (under "Realized gain INR (rounded ₹)" header), not under
    # any of the audit columns.
    yield [
        _AUDIT_TOTAL_LABEL,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        csv_safe_signed(amounts.gain_inr_full_precision),
    ]
    yield [
        portal_label,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        csv_safe_signed(amounts.filed_total_inr),
    ]


@register(OutputKey(Destination.REVIEW, Artifact.CG_LTCG_CSV))
class ScheduleCgLtcgReviewCsvWriter:
    """Write a Schedule CG LTCG compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an
    optional ``assessment_year_start`` constructor argument. When
    the pipeline passes
    ``financial_year.assessment_year_start``, the writer sources
    Schedule CG LTCG portal labels from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default. See
    :class:`ibkr_fa.output.review_csv.ReviewCsvWriter` for the
    full rationale.
    """

    format_id: ClassVar[str] = "review-csv"

    def __init__(
        self, *, assessment_year_start: int | None = None, itr_form: ItrForm = "ITR-2"
    ) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See :class:`ibkr_fa.output.review_csv.ReviewCsvWriter.__init__`
        for the contract — the rationale is identical.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)
        self._portal_label = self._labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG_BY_ITR_FORM[itr_form]
        self._table_f_labels = table_f_band_labels(
            5,
            self._labels.PORTAL_COLUMN_HEADERS_CG_TABLE_F,
            self._labels.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM[itr_form],
        )

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY and ITR form (ITR-2 headers when ``None``)."""
        if ctx.itr_form is None:
            return cls(assessment_year_start=ctx.assessment_year_start)
        return cls(assessment_year_start=ctx.assessment_year_start, itr_form=ctx.itr_form)

    def write(self, result: ScheduleCgLtcgComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` using an atomic temp-file rename."""
        amounts = result.portal_amounts
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(
                csv.writer(fh),
                _iter_rows(
                    result,
                    amounts,
                    portal_label=self._portal_label,
                    table_f_labels=self._table_f_labels,
                ),
                start_index=0,
            )
