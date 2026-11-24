"""XLSX writer for Schedule CG STCG (foreign short-term capital gains).

Sister to :mod:`ibkr_fa.output.schedule_cg_review_csv`: same
section order, same 16-column block layout (more columns than
Schedule OS because STCG rows additionally carry open/close
trade dates, holding period, and separate cost-basis/proceeds
USD columns — but only ONE Rule 115 specified-date + TTBR pair
since the capital-gains conversion is a single transfer-date
event per Rule 115 for ordinary purchased shares. The special
non-resident/Indian-company path is the first proviso to section 48
plus Rule 115A and is not applied here),
emitted as an Excel workbook so the user can review the values
in Excel / LibreOffice / Google Sheets BEFORE copy-pasting the
rounded TOTAL into the e-filing portal.

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
  column headers visible while scrolling long STCG lists.
* **Signed values are native numbers** — STCG losses (negative
  ``realized_gain_inr``, ``proceeds_inr``, ``proceeds_usd``)
  ride as numeric cells. The CSV writer needs the
  :func:`~ibkr_fa.output._writer_contract.csv_safe_signed` apostrophe-prefix to defeat the
  formula-injection guard's leading-``-`` rule for negative
  numbers rendered as strings; XLSX has no such collision
  because the writer-contract guards only fire on **string**
  cells, and numeric ``float`` cells aren't checked.

Section order (mirrors the CSV writer, single-sheet layout):

1. Header row (bold + frozen).
2. In-FY STCG detail rows, sorted ``(close_date, symbol)``.
3. Optional ``_excluded_ltcg_deferred:`` marker row + reject
   rows (lots with holding period > 24 months, which belong on
   Schedule CG Section B via ``ibkr-fa schedule-cg-ltcg``).
4. Optional ``_excluded_indian_source:`` marker row + reject
   rows (Indian-source CG; out of v1 scope).
5. Optional ``_excluded_out_of_fy:`` marker row + reject rows
   (lots whose ``close_date`` is outside the requested FY).
6. Table F block (#1361), always written: a blank row, the
   ``_table_f:`` marker row, five rows of Schedule CG Table F row 3,
   one per date band, then the fixed note row, as in the CSV
   (:func:`~ibkr_fa.output.schedule_cg_review_csv.table_f_rows`). A
   band's whole rupees are a number in the last cell; a blank band
   leaves that cell empty and says why in column B.
7. Audit-TOTAL row (full-precision realized-gain ₹).
8. Portal-TOTAL row (rounded ₹). **MUST** be the literal last
   row in the sheet — the user pastes its rounded cell into
   the portal's Schedule CG Section A foreign-STCG cell.

Writer-contract safety (shared with the CSV sister and the OS
XLSX writer via :mod:`_xlsx_contract`):

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
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_portal_amounts import ScheduleCgPortalAmounts
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
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
from ibkr_fa.output.schedule_cg_review_csv import (
    EXCLUDED_CG_INDIAN_SOURCE_MARKER,
    EXCLUDED_LTCG_DEFERRED_MARKER,
    EXCLUDED_OUT_OF_FY_MARKER,
    IN_SOURCE_CG_FOLLOW_UP_ISSUE,
    IN_SOURCE_CG_REASON,
    LTCG_DEFERRED_FOLLOW_UP_ISSUE,
    LTCG_DEFERRED_REASON,
    OUT_OF_FY_REASON,
    table_f_band_labels,
    table_f_rows,
)

_SHEET_NAME_CG_STCG: Final[str] = "Schedule CG STCG"

_COLUMN_COUNT: Final[int] = 16

# Excel number-format strings. Match the OS / FA-A3 XLSX writers'
# vocabulary so a user opening multiple sheets sees consistent
# formatting.
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

# Per-column number-format strings. ``None`` = default cell format
# (text / no format applied). Index aligns with the column position
# in :data:`_HEADER_ROW`.
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

# Per-row-type number-format tuples. The XLSX sheet mixes 16-cell
# rows with **different column semantics**: detail rows align 1:1
# with :data:`_HEADER_ROW`, but the audit-TOTAL row puts a
# full-precision Decimal in column 16 (which would display as
# rounded under :data:`_INTEGER_RUPEE_FORMAT` despite the "full
# precision" label), and the exclusion-block reject rows shift the
# date / USD / quantity columns to different positions because the
# leading marker columns push everything right. Applying
# :data:`_COLUMN_NUMBER_FORMATS` unconditionally would format
# unrelated cells with mismatched types (T5 codex round-1 P2).
# Each row-type tuple below is exactly :data:`_COLUMN_COUNT` long.

# Marker rows are text-only labels — no number format applied.
_MARKER_FORMATS: Final[tuple[str | None, ...]] = (None,) * _COLUMN_COUNT

# Blank delimiter rows — all cells ``None``; no format applied.
_BLANK_FORMATS: Final[tuple[str | None, ...]] = (None,) * _COLUMN_COUNT

# LTCG-deferred reject row layout (see ``_iter_rows``):
#   0: symbol (str),         1: reason (str),    2: follow_up_issue (str),
#   3: symbol (str),         4: isin (str),
#   5: open_date (date),     6: close_date (date),
#   7: holding_period_days (int),
#   8: quantity (Decimal),   9: cost_basis (Decimal),  10: proceeds (Decimal),
#  11: description (str),   12-15: None.
_LTCG_DEFERRED_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
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

# In-source reject row layout (no holding_period_days column):
#   0: symbol,    1: reason,   2: follow_up_issue,   3: symbol,   4: isin,
#   5: open_date, 6: close_date,
#   7: quantity,  8: cost_basis,  9: proceeds,
#  10: description, 11-15: None.
_IN_SOURCE_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
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

# Out-of-FY reject row layout (no follow_up_issue column):
#   0: symbol,    1: reason,  2: symbol,  3: isin,
#   4: open_date, 5: close_date,
#   6: quantity,  7: cost_basis,  8: proceeds,
#   9: description, 10-15: None.
_OUT_OF_FY_REJECT_FORMATS: Final[tuple[str | None, ...]] = (
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

# Audit-TOTAL row: full-precision realized gain in the last cell.
# The label in column 0 explicitly says "full precision", so the
# cell number format must NOT round to whole rupees — use the same
# full-precision INR format as the audit cost-basis / proceeds
# detail columns so the displayed value matches the label.
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

# Portal-TOTAL row: the filed ₹ total in the last cell. The value
# is the figure the user pastes into the e-filing portal, so the
# whole-rupee integer format matches both the label and the CSV
# writer's cell. The Table F block's rows use it too (#1361): a band
# row's last cell is its whole rupees, and the marker and note rows'
# is empty, so no format applies there.
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


def _detail_row(line: ScheduleCgStcgLine) -> list[object]:
    """A single 16-cell STCG detail row with native Excel types.

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


def _ltcg_deferred_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
    ]


def _in_source_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
        None,
    ]


def _out_of_fy_marker_row() -> list[object]:
    return [
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
        None,
        None,
        None,
        None,
        None,
        None,
    ]


def _audit_total_row(full_precision_sum: Decimal) -> list[object]:
    """Audit-TOTAL row: full-precision realized gain in the last cell.

    Mirrors the CSV writer: the TOTAL value belongs under the
    "Realized gain INR (rounded ₹)" column (last cell), NOT under
    the "_audit: Proceeds INR (full precision)" column — a
    spreadsheet user verifying by column sums would otherwise see
    the realized-gain total in the proceeds column (T4 (@claude
    review) PR #163 P2 fix, mirrored from the CSV writer).
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
    result: ScheduleCgStcgComputeResult,
    amounts: ScheduleCgPortalAmounts,
    *,
    portal_label: str = _default_labels.PORTAL_FIELD_LABEL_CG_FOREIGN_STCG,
    table_f_labels: tuple[str, ...],
) -> Iterator[tuple[list[object], tuple[str | None, ...]]]:
    """Yield every (row, number-format tuple) of the CG STCG sheet.

    The second element of each tuple is the per-column number-format
    strings to apply to **that** row. Detail rows align with the
    header, but audit-TOTAL / exclusion-block / portal-TOTAL rows
    have **different column semantics** (T5 codex PR #204 P2): each
    yields the matching format tuple so a TOTAL cell never receives
    a detail-row's column format and vice versa.
    """
    yield list(_HEADER_ROW), _MARKER_FORMATS

    for line in result.stcg_lines:
        yield _detail_row(line), _COLUMN_NUMBER_FORMATS

    if result.excluded_ltcg_deferred:
        yield _blank_row(), _BLANK_FORMATS
        yield _ltcg_deferred_marker_row(), _MARKER_FORMATS
        for deferred in result.excluded_ltcg_deferred:
            lot = deferred.closed_lot
            yield (
                [
                    str(lot.symbol),
                    LTCG_DEFERRED_REASON,
                    LTCG_DEFERRED_FOLLOW_UP_ISSUE,
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
                _LTCG_DEFERRED_REJECT_FORMATS,
            )

    if result.excluded_indian_source:
        yield _blank_row(), _BLANK_FORMATS
        yield _in_source_marker_row(), _MARKER_FORMATS
        for lot in result.excluded_indian_source:
            yield (
                [
                    str(lot.symbol),
                    IN_SOURCE_CG_REASON,
                    IN_SOURCE_CG_FOLLOW_UP_ISSUE,
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
                _IN_SOURCE_REJECT_FORMATS,
            )

    if result.excluded_out_of_fy:
        yield _blank_row(), _BLANK_FORMATS
        yield _out_of_fy_marker_row(), _MARKER_FORMATS
        for lot in result.excluded_out_of_fy:
            yield (
                [
                    str(lot.symbol),
                    OUT_OF_FY_REASON,
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
                _OUT_OF_FY_REJECT_FORMATS,
            )

    yield _blank_row(), _BLANK_FORMATS
    for first, second, amount in table_f_rows(result.table_f, table_f_labels):
        yield [first, second, *[None] * (_COLUMN_COUNT - 3), amount], _PORTAL_TOTAL_FORMATS

    yield _audit_total_row(amounts.gain_inr_full_precision), _AUDIT_TOTAL_FORMATS
    yield _portal_total_row(portal_label, amounts.filed_total_inr), _PORTAL_TOTAL_FORMATS


@register(OutputKey(Destination.WORKBOOK, Artifact.CG_STCG_XLSX))
class ScheduleCgXlsxWriter:
    """XLSX writer for Schedule CG STCG — sister to :class:`ScheduleCgReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``schedule-cg`` subcommand.

    The output workbook contains one sheet (``"Schedule CG STCG"``)
    with the same column layout as the review CSV (16 cells per
    row), but values are written as native Excel types so
    spreadsheet formulas (SUM, etc.) work directly on the data.
    Signed values (realized gain, proceeds) ride as numeric cells;
    Excel renders losses with a leading ``-`` via the column
    number format string.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them (constructor ``assessment_year_start`` kwarg); see
    :class:`~ibkr_fa.output.schedule_cg_review_csv.ScheduleCgReviewCsvWriter`
    for details.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(
        self, *, assessment_year_start: int | None = None, itr_form: ItrForm = "ITR-2"
    ) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See :class:`ibkr_fa.output.schedule_cg_review_csv.ScheduleCgReviewCsvWriter.__init__`
        for the contract.
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
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file."""
        amounts = result.portal_amounts
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_CG_STCG

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


__all__ = ["ScheduleCgXlsxWriter"]
