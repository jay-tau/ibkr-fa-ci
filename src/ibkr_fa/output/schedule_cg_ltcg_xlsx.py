"""XLSX writer for Schedule CG LTCG (foreign long-term capital gains).

Sister to :mod:`ibkr_fa.output.schedule_cg_ltcg_review_csv` — same
section order, same 16-column block layout (matches the STCG
writer because the LTCG row schema is identical: open / close
trade dates, holding period, separate cost-basis / proceeds USD,
single Rule 115 specified-date + TTBR pair, cost-basis / proceeds
INR, rounded realized gain). Emitted as an Excel workbook so the
user can review the values in Excel / LibreOffice / Google Sheets
BEFORE copy-pasting the rounded TOTAL into the e-filing portal.

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — USD / INR amounts and the FX
  rate are written as Excel numbers (not strings), so the user
  can verify the audit TOTAL sums to the portal TOTAL with
  ``=SUM(...)`` directly on the spreadsheet. The portal-facing
  TOTAL cell matches the CSV writer: both carry the result's filed
  total (#615).
* **Dates stay dates** — both the portal-facing open / close
  dates (``DD/MM/YYYY``) and the audit ISO-8601 Rule-115
  specified date and SBI TTBR source date are written as Excel
  ``datetime`` cells with the appropriate number format string
  applied, so spreadsheet date arithmetic works.
* **Header row is bold + frozen** — first-row freeze pane keeps
  column headers visible while scrolling long LTCG lists.
* **Signed values are native numbers** — LTCG losses (negative
  ``realized_gain_inr``, ``proceeds_inr``, ``proceeds_usd``)
  ride as numeric cells. The CSV writer needs the
  :func:`~ibkr_fa.output._writer_contract.csv_safe_signed` apostrophe-prefix to defeat the
  formula-injection guard's leading-``-`` rule for negative
  numbers rendered as strings; XLSX has no such collision
  because the writer-contract guards only fire on **string**
  cells, and numeric ``float`` cells aren't checked.

Section order (mirrors the CSV writer, single-sheet layout):

1. Header row (bold + frozen).
2. In-FY LTCG detail rows, sorted ``(close_date, symbol)``.
3. Optional ``_excluded_stcg:`` marker row + reject rows (lots
   with holding period <= 24 months, which belong on Schedule
   CG / A (STCG) via ``ibkr-fa schedule-cg``).
4. Optional ``_excluded_pre_2024_07_23:`` marker row + reject
   rows (lots disposed before the 23 July 2024 cutover — old
   regime with indexation; out of v1 scope).
5. Optional ``_excluded_indian_source_ltcg:`` marker row +
   reject rows (Indian-source LTCG; different Schedule CG
   section; out of v1 scope).
6. Optional ``_excluded_out_of_fy_ltcg:`` marker row + reject
   rows (lots whose ``close_date`` is outside the requested FY).
7. Table F block (#1361), always written: a blank row, the
   ``_table_f:`` marker row, five rows of Schedule CG Table F row 5,
   one per date band, then the fixed note row, as in the CSV
   (:func:`~ibkr_fa.output.schedule_cg_review_csv.table_f_rows`). A
   band's whole rupees are a number in the last cell; a blank band
   leaves that cell empty and says why in column B.
8. Audit-TOTAL row (full-precision realized-gain ₹).
9. Portal-TOTAL row (rounded ₹). **MUST** be the literal last
   row in the sheet — the user pastes its rounded cell into
   the portal's Schedule CG Section B foreign-LTCG cell.

Writer-contract safety (shared with the CSV sister and the OS /
CG STCG XLSX writers via :mod:`_xlsx_contract`):

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string
  cell are rejected (binary-contamination guard). Preserves the
  ORIGINAL worksheet column index in :class:`WriterContractError`.
* :func:`_assert_no_xlsx_formula_injection` — Excel auto-executes
  formula-prefixed cells on open. The same OWASP-extended trigger
  set used by the CSV writer (``= + - @`` + C0 whitespace +
  Unicode full-width homoglyphs) is rejected on every string
  cell. Numeric / datetime cells are safe by construction.
* :func:`_assert_no_illegal_xlsx_character` — last, on the row as
  appended: a string cell holding a control character a workbook
  cell can't store (openpyxl's set) is rejected, naming only the
  row and cell (#1357).

Per-AY portal labels (issue #65): mirrors the CSV writer's
constructor signature. ``assessment_year_start`` defaults to the
package-level current AY; passing a different AY routes to the
matching per-AY snapshot via
:func:`ibkr_fa.output.portal_labels.for_ay`.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._xlsx_contract import (
    _assert_no_illegal_xlsx_character,
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _decimal_to_float,
)
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.schedule_cg_ltcg_review_csv import (
    EXCLUDED_CG_LTCG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_OUT_OF_FY_MARKER,
    EXCLUDED_PRE_CUTOVER_MARKER,
    EXCLUDED_STCG_MARKER,
    IN_SOURCE_CG_LTCG_FOLLOW_UP_ISSUE,
    IN_SOURCE_CG_LTCG_REASON,
    OUT_OF_FY_LTCG_REASON,
    PRE_CUTOVER_FOLLOW_UP_ISSUE,
    PRE_CUTOVER_REASON,
    STCG_DEFERRED_FOLLOW_UP_ISSUE,
    STCG_DEFERRED_REASON,
)
from ibkr_fa.output.schedule_cg_review_csv import (
    table_f_band_labels,
    table_f_rows,
)

_SHEET_NAME_CG_LTCG: Final[str] = "Schedule CG LTCG"

_COLUMN_COUNT: Final[int] = 16

# Excel number-format strings. Match the OS / FA-A3 / CG STCG
# XLSX writers' vocabulary so a user opening multiple sheets sees
# consistent formatting.
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0;-#,##0"
_DECIMAL_USD_FORMAT: Final[str] = "#,##0.00000000;-#,##0.00000000"
_DECIMAL_INR_FULL_PRECISION_FORMAT: Final[str] = "#,##0.00000000;-#,##0.00000000"
_DECIMAL_TTBR_FORMAT: Final[str] = "#,##0.0000"
_INTEGER_FORMAT: Final[str] = "#,##0"
_DECIMAL_QUANTITY_FORMAT: Final[str] = "#,##0.########"
_PORTAL_DATE_EXCEL_FORMAT: Final[str] = "DD/MM/YYYY"
_AUDIT_DATE_EXCEL_FORMAT: Final[str] = "YYYY-MM-DD"

_HEADER_ROW: Final[tuple[str, ...]] = (
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
)

# Per-column number-format strings for in-FY LTCG detail rows.
# Index aligns with the column position in :data:`_HEADER_ROW`.
_COLUMN_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Symbol
    None,  # ISIN
    None,  # Country
    _PORTAL_DATE_EXCEL_FORMAT,  # Open date
    _PORTAL_DATE_EXCEL_FORMAT,  # Close date
    _INTEGER_FORMAT,  # Holding period (days)
    _DECIMAL_QUANTITY_FORMAT,  # Quantity
    _DECIMAL_USD_FORMAT,  # _audit: Cost basis USD
    _DECIMAL_USD_FORMAT,  # _audit: Proceeds USD
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: FX specified date
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: SBI TTBR source date
    _DECIMAL_TTBR_FORMAT,  # _audit: SBI TTBR
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Cost basis INR
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Proceeds INR
    None,  # _audit: Statutory cost basis source
    _INTEGER_RUPEE_FORMAT,  # Realized gain INR (rounded)
)

# Per-row-type number-format tuples — see the long block comment
# on the sister :mod:`schedule_cg_xlsx` for the design rationale
# (T5 codex PR #204 round-1 P2): different row shapes have
# different column semantics, so each row type must carry its
# own format tuple. Each tuple is exactly :data:`_COLUMN_COUNT`
# (16) long.

# Marker rows are text-only labels — no number format applied.
_MARKER_FORMATS: Final[tuple[str | None, ...]] = (None,) * _COLUMN_COUNT

# Blank delimiter rows — all cells ``None``; no format applied.
_BLANK_FORMATS: Final[tuple[str | None, ...]] = (None,) * _COLUMN_COUNT

# STCG-deferred reject row layout (lots that belong on Schedule CG
# Section A) — same shape as the CG STCG writer's LTCG-deferred
# block: 16 cells with holding_period_days at index 7:
#   0: symbol,    1: reason,   2: follow_up_issue,   3: symbol,   4: isin,
#   5: open_date, 6: close_date,
#   7: holding_period_days,
#   8: quantity,  9: cost_basis, 10: proceeds,
#  11: description, 12-15: None.
_STCG_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
    None,
    None,
    None,
    None,
    None,
    _PORTAL_DATE_EXCEL_FORMAT,
    _PORTAL_DATE_EXCEL_FORMAT,
    _INTEGER_FORMAT,
    _DECIMAL_QUANTITY_FORMAT,
    _DECIMAL_USD_FORMAT,
    _DECIMAL_USD_FORMAT,
    None,
    None,
    None,
    None,
    None,
)

# Pre-cutover reject row layout — identical to STCG-deferred
# layout because both carry holding_period_days.
_PRE_CUTOVER_REJECT_FORMATS: Final[tuple[str | None, ...]] = _STCG_REJECT_FORMATS

# Indian-source LTCG reject row layout (no holding_period_days):
#   0: symbol,    1: reason,   2: follow_up_issue,   3: symbol,   4: isin,
#   5: open_date, 6: close_date,
#   7: quantity,  8: cost_basis,  9: proceeds,
#  10: description, 11-15: None.
_IN_SOURCE_LTCG_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
    None,
    None,
    None,
    None,
    None,
    _PORTAL_DATE_EXCEL_FORMAT,
    _PORTAL_DATE_EXCEL_FORMAT,
    _DECIMAL_QUANTITY_FORMAT,
    _DECIMAL_USD_FORMAT,
    _DECIMAL_USD_FORMAT,
    None,
    None,
    None,
    None,
    None,
    None,
)

# Out-of-FY LTCG reject row layout (no follow_up_issue column):
#   0: symbol,    1: reason,  2: symbol,  3: isin,
#   4: open_date, 5: close_date,
#   6: quantity,  7: cost_basis,  8: proceeds,
#   9: description, 10-15: None.
_OUT_OF_FY_LTCG_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
    None,
    None,
    None,
    None,
    _PORTAL_DATE_EXCEL_FORMAT,
    _PORTAL_DATE_EXCEL_FORMAT,
    _DECIMAL_QUANTITY_FORMAT,
    _DECIMAL_USD_FORMAT,
    _DECIMAL_USD_FORMAT,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
)

# Audit-TOTAL row: full-precision realized gain in the last cell —
# uses the full-precision INR format so the displayed value
# matches the "full precision" label.
_AUDIT_TOTAL_FORMATS: Final[tuple[str | None, ...]] = (
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    _DECIMAL_INR_FULL_PRECISION_FORMAT,
)

# Portal-TOTAL row: the filed ₹ total in the last cell — the
# whole-rupee integer the user pastes into the e-filing portal. The
# Table F block's rows use it too (#1361): a band row's last cell is
# its whole rupees, and the marker and note rows' is empty, so no
# format applies there.
_PORTAL_TOTAL_FORMATS: Final[tuple[str | None, ...]] = (
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    _INTEGER_RUPEE_FORMAT,
)

_AUDIT_TOTAL_LABEL: Final[str] = "_audit: TOTAL Realized gain INR (full precision)"


def _blank_row() -> list[object]:
    """A 16-cell blank row, matching the CSV writer's blank delimiter."""
    return [None] * _COLUMN_COUNT


def _detail_row(line: ScheduleCgLtcgLine) -> list[object]:
    """A single 16-cell LTCG detail row with native Excel types.

    The last cell (``realized_gain_inr``) is rounded to whole
    rupees per the CSV writer's convention so the "Realized gain
    INR (rounded ₹)" column displays whole-rupee amounts. As a
    consequence, a naive ``=SUM(P2:Pn)`` over that column may
    diverge from the audit-TOTAL row by a few rupees (one per
    line whose rounding rounds the other way) because the audit
    TOTAL sums the **full-precision** per-line gains and rounds
    once. The audit-TOTAL row is authoritative; the
    per-line-rounded column intentionally diverges so users
    sanity-checking against the e-filing portal see the same
    rounded amount the CSV writer emits.

    The SBI TTBR source-date cell is blank (``None``) unless the
    policy carried back to an earlier SBI row (#741), as in the
    Schedule OS XLSX.
    """
    publication_date_cell: object
    if line.fx_rate_publication_date == line.fx_specified_date:
        publication_date_cell = None
    else:
        publication_date_cell = line.fx_rate_publication_date
    return [
        str(line.symbol),
        str(line.isin),
        line.issuer_country_iso2,
        line.open_date,
        line.close_date,
        line.holding_period_days,
        line.quantity,
        line.cost_basis_usd,
        line.proceeds_usd,
        line.fx_specified_date,
        publication_date_cell,
        line.fx_rate_inr_per_usd,
        line.cost_basis_inr,
        line.proceeds_inr,
        line.cost_basis_inr_source,
        to_whole_rupees(line.realized_gain_inr),
    ]


def _stcg_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
    ]


def _pre_cutover_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
    ]


def _in_source_ltcg_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
        None,
    ]


def _out_of_fy_ltcg_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
        None,
        None,
    ]


def _audit_total_row(full_precision_sum: Decimal) -> list[object]:
    """Audit-TOTAL row: full-precision realized gain in the last cell.

    Mirrors the CSV writer and the CG STCG XLSX writer: the
    TOTAL value belongs under the "Realized gain INR (rounded ₹)"
    column (last cell), NOT under the "_audit: Proceeds INR
    (full precision)" column.
    """
    return [
        _AUDIT_TOTAL_LABEL,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        full_precision_sum,
    ]


def _portal_total_row(label: str, filed_total: Decimal) -> list[object]:
    """Portal-TOTAL row: the filed ₹ total in the last cell.

    Label in column 0 (string), the rounded-portal cell (column
    15, last) carries the integer ₹ amount the user pastes into
    the e-filing portal. Mirrors the CSV writer.
    """
    return [
        label,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        filed_total,
    ]


def _iter_rows(
    result: ScheduleCgLtcgComputeResult,
    amounts: ScheduleCgPortalAmounts,
    *,
    portal_label: str = _default_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_LTCG,
    table_f_labels: tuple[str, ...],
) -> Iterator[tuple[list[object], tuple[str | None, ...]]]:
    """Yield every (row, per-row-format-tuple) of the CG LTCG sheet."""
    yield list(_HEADER_ROW), _MARKER_FORMATS

    for line in result.ltcg_lines:
        yield _detail_row(line), _COLUMN_NUMBER_FORMATS

    if result.excluded_stcg:
        yield _blank_row(), _BLANK_FORMATS
        yield _stcg_marker_row(), _MARKER_FORMATS
        for deferred in result.excluded_stcg:
            lot = deferred.closed_lot
            yield (
                [
                    str(lot.symbol),
                    STCG_DEFERRED_REASON,
                    STCG_DEFERRED_FOLLOW_UP_ISSUE,
                    str(lot.symbol),
                    str(lot.isin),
                    lot.open_date_time,
                    lot.close_date_time,
                    deferred.holding_period_days,
                    lot.quantity,
                    lot.cost_basis_money,
                    lot.proceeds_money,
                    lot.description,
                    None,
                    None,
                    None,
                    None,
                ],
                _STCG_REJECT_FORMATS,
            )

    if result.excluded_pre_2024_07_23:
        yield _blank_row(), _BLANK_FORMATS
        yield _pre_cutover_marker_row(), _MARKER_FORMATS
        for deferred in result.excluded_pre_2024_07_23:
            lot = deferred.closed_lot
            yield (
                [
                    str(lot.symbol),
                    PRE_CUTOVER_REASON,
                    PRE_CUTOVER_FOLLOW_UP_ISSUE,
                    str(lot.symbol),
                    str(lot.isin),
                    lot.open_date_time,
                    lot.close_date_time,
                    deferred.holding_period_days,
                    lot.quantity,
                    lot.cost_basis_money,
                    lot.proceeds_money,
                    lot.description,
                    None,
                    None,
                    None,
                    None,
                ],
                _PRE_CUTOVER_REJECT_FORMATS,
            )

    if result.excluded_indian_source:
        yield _blank_row(), _BLANK_FORMATS
        yield _in_source_ltcg_marker_row(), _MARKER_FORMATS
        for lot in result.excluded_indian_source:
            yield (
                [
                    str(lot.symbol),
                    IN_SOURCE_CG_LTCG_REASON,
                    IN_SOURCE_CG_LTCG_FOLLOW_UP_ISSUE,
                    str(lot.symbol),
                    str(lot.isin),
                    lot.open_date_time,
                    lot.close_date_time,
                    lot.quantity,
                    lot.cost_basis_money,
                    lot.proceeds_money,
                    lot.description,
                    None,
                    None,
                    None,
                    None,
                    None,
                ],
                _IN_SOURCE_LTCG_REJECT_FORMATS,
            )

    if result.excluded_out_of_fy:
        yield _blank_row(), _BLANK_FORMATS
        yield _out_of_fy_ltcg_marker_row(), _MARKER_FORMATS
        for lot in result.excluded_out_of_fy:
            yield (
                [
                    str(lot.symbol),
                    OUT_OF_FY_LTCG_REASON,
                    str(lot.symbol),
                    str(lot.isin),
                    lot.open_date_time,
                    lot.close_date_time,
                    lot.quantity,
                    lot.cost_basis_money,
                    lot.proceeds_money,
                    lot.description,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ],
                _OUT_OF_FY_LTCG_REJECT_FORMATS,
            )

    yield _blank_row(), _BLANK_FORMATS
    for first, second, amount in table_f_rows(result.table_f, table_f_labels):
        yield [first, second, *[None] * (_COLUMN_COUNT - 3), amount], _PORTAL_TOTAL_FORMATS

    yield _audit_total_row(amounts.gain_inr_full_precision), _AUDIT_TOTAL_FORMATS
    yield _portal_total_row(portal_label, amounts.filed_total_inr), _PORTAL_TOTAL_FORMATS


@register(OutputKey(Destination.WORKBOOK, Artifact.CG_LTCG_XLSX))
class ScheduleCgLtcgXlsxWriter:
    """XLSX writer for Schedule CG LTCG — sister to :class:`ScheduleCgLtcgReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``schedule-cg-ltcg`` subcommand.

    The output workbook contains one sheet (``"Schedule CG LTCG"``)
    with the same column layout as the review CSV (16 cells per
    row), but values are written as native Excel types so
    spreadsheet formulas (SUM, etc.) work directly on the data.
    Signed values (realized gain, proceeds) ride as numeric cells;
    Excel renders losses with a leading ``-`` via the column
    number format string.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them (constructor ``assessment_year_start`` kwarg); see
    :class:`~ibkr_fa.output.schedule_cg_ltcg_review_csv.ScheduleCgLtcgReviewCsvWriter`
    for details.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(
        self, *, assessment_year_start: int | None = None, itr_form: ItrForm = "ITR-2"
    ) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See ``schedule_cg_ltcg_review_csv.ScheduleCgLtcgReviewCsvWriter.__init__``
        for the contract.
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
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file."""
        amounts = result.portal_amounts
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_CG_LTCG

        header_font = Font(bold=True)

        for row_index, (row, formats) in enumerate(
            _iter_rows(
                result,
                amounts,
                portal_label=self._portal_label,
                table_f_labels=self._table_f_labels,
            )
        ):
            _assert_no_nul_in_xlsx_row(row, row_index=row_index)
            _assert_no_xlsx_formula_injection(row, row_index=row_index)
            cells = _decimal_to_float(row, row_index=row_index)
            _assert_no_illegal_xlsx_character(cells, row_index=row_index)
            worksheet.append(cells)
            data_row_number = row_index + 1  # 1-indexed
            if row_index == 0:
                for column_index in range(1, len(row) + 1):
                    worksheet.cell(row=data_row_number, column=column_index).font = header_font
                continue
            for col_offset, fmt in enumerate(formats):
                if fmt is None:
                    continue
                cell = worksheet.cell(row=data_row_number, column=col_offset + 1)
                if cell.value is None:
                    continue
                cell.number_format = fmt

        worksheet.freeze_panes = "A2"

        with atomic_output_path(path) as tmp_path:
            workbook.save(tmp_path)


__all__ = ["ScheduleCgLtcgXlsxWriter"]
