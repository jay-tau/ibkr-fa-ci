"""Review-CSV writer for Schedule CG STCG (foreign short-term capital gains).

Mirror of :mod:`ibkr_fa.output.schedule_os_review_csv` for the CG
pathway. Same atomic-write contract, BOM (``utf-8-sig``), uniform
row width, marker-row audit blocks, and writer-contract safety
helpers (NUL + CSV-formula-injection guards on every cell).

CSV contract (locked by ``tests/test_schedule_cg_review_csv.py``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``).
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 16``
  cells (more than the 11 of Schedule OS because STCG rows additionally
  carry both the open and close trade dates, the holding period, and
  separate cost-basis/proceeds USD columns — but only ONE Rule 115
  specified-date + TTBR pair since the capital-gains conversion is a
  single transfer-date event, NOT a double conversion).
* **SBI TTBR source date**: the SBI archive date the rate was read
  from, blank unless the policy carried back from the specified date
  (#741, the Schedule OS convention from #160).
* **Section order**: header → in-FY STCG detail rows
  (sorted ``(close_date, symbol)``) → optional
  ``_excluded_ltcg_deferred:`` audit block → optional
  ``_excluded_indian_source:`` audit block → optional
  ``_excluded_out_of_fy:`` audit block → ``_table_f:`` block → audit
  TOTAL row →
  :data:`~ibkr_fa.output.portal_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG`
  TOTAL row.
* **Marker rows**: prefixes ``_excluded_ltcg_deferred:``,
  ``_excluded_indian_source:``, ``_excluded_out_of_fy:`` are
  module-level constants and re-exported via :mod:`ibkr_fa.output`.
* **Table F block** (#1361): always written, even with no in-FY line.
  A blank row, the :data:`TABLE_F_MARKER` row, five rows of Schedule CG
  Table F row 3, one per date band, then the :data:`TABLE_F_NOTE` row.
  Each band row's first cell is its label (:func:`table_f_band_labels`)
  and its last cell the band's whole rupees from the result's
  :attr:`~ibkr_fa.compute.schedule_cg.ScheduleCgStcgComputeResult.table_f`.
  A blank band leaves that cell blank, and its second cell gives the
  filing workbook's reason
  (:func:`~ibkr_fa.output.schedules.cg_filing.table_f_blank_basis`). The
  bands are this file's alone, before any set-off.
* **TOTAL-row contract**: the LITERAL last row's first cell equals
  :data:`PORTAL_FIELD_LABEL_CG_FOREIGN_STCG` exactly. Last cell is the
  filed ₹ total, the result's
  :attr:`~ibkr_fa.compute.schedule_cg_portal_amounts.ScheduleCgPortalAmounts.filed_total_inr`
  (rounded consideration less rounded cost, #615).
* **Atomic write**: same temp-file-then-replace pattern as the OS
  writer; identical guard semantics.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.compute.schedule_cg_table_f import TABLE_F_BAND_COUNT, ScheduleCgTableFRow
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
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
from ibkr_fa.output.schedules.cg_filing import TABLE_F_COLUMNS, table_f_blank_basis

_COLUMN_COUNT: Final[int] = 16

EXCLUDED_LTCG_DEFERRED_MARKER: Final[str] = "_excluded_ltcg_deferred:"
EXCLUDED_CG_INDIAN_SOURCE_MARKER: Final[str] = "_excluded_indian_source:"
EXCLUDED_OUT_OF_FY_MARKER: Final[str] = "_excluded_out_of_fy:"

LTCG_DEFERRED_FOLLOW_UP_ISSUE: Final[str] = ""
LTCG_DEFERRED_REASON: Final[str] = (
    "Long-term — holding > 24 months; belongs on Schedule CG / Section B "
    "(LTCG) — run `ibkr-fa schedule-cg-ltcg` against the same Activity "
    "Statement to produce the LTCG review CSV"
)
IN_SOURCE_CG_FOLLOW_UP_ISSUE: Final[str] = ""
IN_SOURCE_CG_REASON: Final[str] = (
    "Indian-source — domestic CG via different Schedule CG section "
    "(out of v1 scope; no dedicated tracker issue yet)"
)
OUT_OF_FY_REASON: Final[str] = "close_date outside the requested financial year window"
#: The first cell of the row that opens the Table F block (#1361), shared by
#: the four Schedule CG review writers.
TABLE_F_MARKER: Final[str] = "_table_f:"
#: The Table F block's closing note (#1361), shared by the four Schedule CG
#: review writers.
TABLE_F_NOTE: Final[str] = (
    "These Table F date bands are this file's sales alone, net by sale date, before "
    "any set-off; the five add up to this file's portal TOTAL, or are 0 when it isn't "
    "positive. Add the same row's band amounts from every other capital-gains file, "
    "including Robinhood's and any Indian ones. Then lower the bands for any capital "
    "loss set off against them, so the row totals Schedule BFLA item 3v for row 3 or "
    "3vii for row 5 (ITR-3: 5viii or 5x). The portal checks only the total."
)

# T5 (Codex CLI gpt-5.5) PR #163 P1 fix: Rule 115 prescribes ONE
# specified date for capital gains — the transfer-event's preceding-
# month-end. The detail row therefore carries a SINGLE Rule 115
# specified-date + TTBR pair (down from the pre-fix two pairs), and
# both cost_basis_inr and proceeds_inr are derived at that same
# transfer-date rate. The acquisition-date FX columns are gone
# because the underlying acquisition-date conversion was removed
# from the compute layer.
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

_LTCG_DEFERRED_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_LTCG_DEFERRED_MARKER,
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

_IN_SOURCE_CG_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_CG_INDIAN_SOURCE_MARKER,
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

_OUT_OF_FY_MARKER_ROW: Final[list[str]] = [
    EXCLUDED_OUT_OF_FY_MARKER,
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
    """Render an upstream-derived text cell safely for CSV output.

    Free-text cells sourced from IBKR (symbol, description) or from
    user-supplied data could in principle start with one of the
    OWASP CSV-formula-trigger characters (``= + - @`` plus the C0
    whitespace triggers ``\\t \\v \\r \\n`` and four full-width Unicode
    homoglyphs equivalent to ``= + - @``). The writer-contract
    :func:`~ibkr_fa.output._writer_contract._assert_no_csv_formula_injection`
    would then raise :class:`WriterContractError` and abort the
    entire ``schedule-cg`` run — even though the upstream value is
    legitimately what IBKR sent and the user reasonably expects to
    see it in the audit CSV.

    Prepending the OWASP-canonical single-quote escape converts the
    cell into a literal-text cell that Excel / LibreOffice strip the
    apostrophe from on display, while raw CSV readers see the literal
    ``'`` prefix. Empty cells pass through unchanged.

    Symbol cells are unlikely to start with formula triggers in
    practice (IBKR tickers are uppercase ASCII alphanumeric), but
    description cells legitimately could (e.g., a hypothetical
    corporate name starting with ``@``). The cost of universal
    safe-text escaping is one extra character per affected cell;
    the benefit is that a malicious or malformed upstream payload
    can never abort the run via the writer contract.
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


def table_f_band_labels(row: int, headings: tuple[str, ...], row_label: str) -> tuple[str, ...]:
    """Table F row ``row``'s five band labels, (i) to (v) (#1361).

    ``headings`` are the AY's band headings and ``row_label`` the row's text
    for the run's ITR form, as the filing workbook's Table F rows use them.
    """
    return tuple(
        f"Schedule CG / F / {row}({column}) {heading}: {row_label}"
        for column, heading in zip(TABLE_F_COLUMNS, headings, strict=True)
    )


def table_f_rows(
    table_f: ScheduleCgTableFRow, band_labels: tuple[str, ...]
) -> Iterator[tuple[str, str | None, Decimal | None]]:
    """The Table F block after its blank row, as each row's first, second and last cell (#1361).

    The :data:`TABLE_F_MARKER` row, one row per band labelled from
    ``band_labels``, then the :data:`TABLE_F_NOTE` row. A band row's last
    cell is the band's whole rupees; a blank band's is ``None``, and its
    second cell says why. The other cells are blank.
    """
    yield TABLE_F_MARKER, None, None
    if table_f.bands is None:
        reason = table_f_blank_basis(table_f.case)
        bands: tuple[Decimal | None, ...] = (None,) * TABLE_F_BAND_COUNT
    else:
        reason, bands = None, table_f.bands
    for label, band in zip(band_labels, bands, strict=True):
        yield label, reason, band
    yield TABLE_F_NOTE, None, None


def _blank_row() -> list[str]:
    return [""] * _COLUMN_COUNT


def _detail_row(line: ScheduleCgStcgLine) -> list[str]:
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
    result: ScheduleCgStcgComputeResult,
    amounts: ScheduleCgPortalAmounts,
    *,
    portal_label: str = _default_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    table_f_labels: tuple[str, ...],
) -> Iterator[list[str]]:
    yield list(_HEADER_ROW)

    for line in result.stcg_lines:
        yield _detail_row(line)

    if result.excluded_ltcg_deferred:
        yield _blank_row()
        yield list(_LTCG_DEFERRED_MARKER_ROW)
        for deferred in result.excluded_ltcg_deferred:
            lot = deferred.closed_lot
            yield [
                _csv_safe_text(str(lot.symbol)),
                LTCG_DEFERRED_REASON,
                LTCG_DEFERRED_FOLLOW_UP_ISSUE,
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
        yield list(_IN_SOURCE_CG_MARKER_ROW)
        for lot in result.excluded_indian_source:
            yield [
                _csv_safe_text(str(lot.symbol)),
                IN_SOURCE_CG_REASON,
                IN_SOURCE_CG_FOLLOW_UP_ISSUE,
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
        yield list(_OUT_OF_FY_MARKER_ROW)
        for lot in result.excluded_out_of_fy:
            yield [
                _csv_safe_text(str(lot.symbol)),
                OUT_OF_FY_REASON,
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

    # T4 (@claude review) PR #163 P2: the audit-TOTAL value belongs
    # under the "Realized gain INR (rounded ₹)" column (last cell),
    # NOT under the "_audit: Proceeds INR (full precision)" column.
    # A spreadsheet user verifying by column sums would otherwise see
    # the realized-gain total in the proceeds column.
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


@register(OutputKey(Destination.REVIEW, Artifact.CG_STCG_CSV))
class ScheduleCgReviewCsvWriter:
    """Write a Schedule CG STCG compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an
    optional ``assessment_year_start`` constructor argument. When
    the pipeline passes
    ``financial_year.assessment_year_start``, the writer sources
    Schedule CG portal labels from
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
        self._portal_label = self._labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG_BY_ITR_FORM[itr_form]
        self._table_f_labels = table_f_band_labels(
            3,
            self._labels.PORTAL_COLUMN_HEADERS_CG_TABLE_F,
            self._labels.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM[itr_form],
        )

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY and ITR form (ITR-2 headers when ``None``)."""
        if ctx.itr_form is None:
            return cls(assessment_year_start=ctx.assessment_year_start)
        return cls(assessment_year_start=ctx.assessment_year_start, itr_form=ctx.itr_form)

    def write(self, result: ScheduleCgStcgComputeResult, path: Path) -> None:
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
