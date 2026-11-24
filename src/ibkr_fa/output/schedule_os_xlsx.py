"""XLSX writer for Schedule OS ordinary dividends.

Sister to :mod:`ibkr_fa.output.schedule_os_review_csv`: same
section order, same column block layout (11 cells per row, mixing
portal cells with full-precision audit cells), but emitted as an
Excel workbook so the user can review the values in Excel /
LibreOffice / Google Sheets BEFORE copy-pasting the rounded TOTAL
amount into the e-filing portal's Schedule OS row 1a(i).

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — USD / INR amounts and the FX
  rate are written as Excel numbers (not strings), so the user
  can verify the audit TOTALs sum to the portal TOTAL with
  ``=SUM(...)`` directly on the spreadsheet. Whole-rupee
  quantization for the portal-facing TOTAL cells matches the CSV
  writer (HALF_UP per CBDT convention).
* **Dates stay dates** — both the portal-facing payment date
  (``DD/MM/YYYY``) and the audit ISO-8601 Rule-115 specified
  date / publication date are written as Excel ``datetime`` cells
  with the appropriate number format string applied, so
  spreadsheet date arithmetic works.
* **Header row is bold + frozen** — the first row's freeze pane
  keeps the column headers visible while scrolling long
  dividend lists; bold formatting visually separates the header
  from data.

Section order mirrors the CSV writer: header, globally sorted ordinary
dividend details, five Schedule OS section 10 / Quicko period totals, one full-precision audit
total, one rounded 1a(i) portal total, one rounded 1b(ix) interest
total, and the optional ``_other_income:`` 1e block. As in the CSV
writer, a result without the 1b(ix) total is refused before the target
is opened.

Writer-contract safety (shared with the CSV sister):

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string
  cell are rejected (binary-contamination guard). Preserves the
  ORIGINAL worksheet column index in :class:`WriterContractError`
  so the user lands on the right cell.
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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
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
from ibkr_fa.output.schedule_os_review_csv import (
    _DIVIDEND_PERIOD_LABELS,
    _DIVIDEND_PERIOD_SECTION_LABEL,
    _require_interest_total,
)

_SHEET_NAME_OS: Final[str] = "Schedule OS"

_COLUMN_COUNT: Final[int] = 11

# Excel number-format strings. Mirror the FA-A3 XLSX writer's
# vocabulary so a user opening both files sees consistent
# formatting.
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_DECIMAL_USD_FORMAT: Final[str] = "#,##0.00000000"
_DECIMAL_INR_FULL_PRECISION_FORMAT: Final[str] = "#,##0.00000000"
_DECIMAL_TTBR_FORMAT: Final[str] = "#,##0.0000"
_PORTAL_DATE_EXCEL_FORMAT: Final[str] = "DD/MM/YYYY"
_AUDIT_DATE_EXCEL_FORMAT: Final[str] = "YYYY-MM-DD"

_HEADER_ROW: Final[tuple[str, ...]] = (
    "Symbol",
    "ISIN",
    "Country (ISO 3166-1 alpha-2)",
    "Payment date (DD/MM/YYYY)",
    "Distribution type",
    "_audit: Gross USD",
    "_audit: FX specified date (YYYY-MM-DD)",
    "_audit: SBI TTBR source date (YYYY-MM-DD)",
    "_audit: SBI TTBR (INR/USD)",
    "_audit: Gross INR (full precision)",
    "Gross INR (rounded ₹)",
)

# Per-column number-format strings. ``None`` = default cell format
# (text / no format applied). Index aligns with the column position
# in :data:`_HEADER_ROW`.
_COLUMN_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Symbol
    None,  # ISIN
    None,  # Country
    _PORTAL_DATE_EXCEL_FORMAT,  # Payment date
    None,  # Distribution type
    _DECIMAL_USD_FORMAT,  # _audit: Gross USD
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: FX specified date
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: SBI TTBR source date
    _DECIMAL_TTBR_FORMAT,  # _audit: SBI TTBR
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Gross INR (full precision)
    _INTEGER_RUPEE_FORMAT,  # Gross INR (rounded)
)

_AUDIT_TOTAL_LABEL: Final[str] = "_audit: 1a(i) TOTAL Gross INR (full precision)"
_ONE_RUPEE: Final[Decimal] = Decimal("1")


def _round_inr_decimal(value: Decimal) -> Decimal:
    """Round to whole INR (HALF_UP) preserving :class:`Decimal` type.

    Returns :class:`Decimal` rather than ``int`` so the per-line
    rounded cells match the result's TOTAL cells in that column (which is
    formatted as a whole-rupee integer via
    :data:`_INTEGER_RUPEE_FORMAT`). openpyxl renders :class:`Decimal`
    via :func:`_decimal_to_float`; the float round-trip is safe for
    whole-rupee values up to ~2^53 ₹ (well beyond any plausible
    portfolio).
    """
    return value.quantize(_ONE_RUPEE, rounding=ROUND_HALF_UP)


def _blank_row() -> list[object]:
    """An 11-cell blank row, matching the CSV writer's blank delimiter.

    Uses :data:`None` rather than ``""`` so openpyxl writes truly
    empty cells (no value type, no format). The user sees a visual
    gap between sections in Excel.
    """
    return [None] * _COLUMN_COUNT


def _detail_row(
    line: ScheduleOsDividendLine | ScheduleOsDomesticDividendLine,
) -> list[object]:
    """A single 11-cell detail row with native Excel types.

    Strings stay strings, dates become :class:`~datetime.date`, USD
    / INR amounts and the TTBR rate become :class:`Decimal` (which
    :func:`_decimal_to_float` coerces to ``float`` for openpyxl).
    The publication-date cell is blank (``None``) when the
    publication date equals the Rule-115 specified date — same
    elision the CSV writer applies, so the XLSX reads the same way.
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
        line.payment_date,
        line.kind.value,
        line.amount_gross_usd,
        line.fx_specified_date,
        publication_date_cell,
        line.fx_rate_inr_per_usd,
        line.amount_gross_inr,
        _round_inr_decimal(line.amount_gross_inr),
    ]


def _audit_total_row(label: str, full_precision_sum: Decimal) -> list[object]:
    """An 11-cell audit-TOTAL row.

    Label sits in column 0 (string), the full-precision sum sits in
    column 9, and the rounded-portal-cell (column 10) is blank —
    audit rows don't fill the portal cell. Mirrors the CSV writer.
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
        full_precision_sum,
        None,
    ]


def _portal_total_row(label: str, rounded_total: Decimal) -> list[object]:
    """An 11-cell portal-TOTAL row.

    Label sits in column 0 (string), audit cell (column 9) is blank,
    and the rounded-portal cell (column 10) carries the integer ₹
    amount the user pastes into the e-filing portal. Mirrors the
    CSV writer.
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
        rounded_total,
    ]


def _iter_rows(
    result: ScheduleOsComputeResult,
    *,
    interest_total: Decimal,
    labels: ModuleType = _default_labels,
) -> Iterator[list[object]]:
    """Yield every row of the OS sheet, in section order.

    Section order matches
    :func:`ibkr_fa.output.schedule_os_review_csv._iter_rows`
    cell-for-cell (modulo native typing of date / Decimal cells)
    so the XLSX and CSV outputs round-trip the same compute result
    to the same set of values.
    """
    yield list(_HEADER_ROW)

    ordinary_lines = sorted(
        [*result.lines, *result.domestic_lines],
        key=lambda line: (line.payment_date, str(line.symbol)),
    )
    for line in ordinary_lines:
        yield _detail_row(line)

    yield _blank_row()
    yield [
        _DIVIDEND_PERIOD_SECTION_LABEL,
        "Quicko Income to add / ITR section 10 row 3a",
        "Quicko Expense to add / ITR section 57(i) aggregate (not computed)",
        None,
        None,
        None,
        None,
        None,
        None,
        "_audit: Income (full precision INR)",
        None,
    ]
    for period_label, period in zip(_DIVIDEND_PERIOD_LABELS, result.dividend_periods, strict=True):
        yield [
            period_label,
            period.whole_rupees,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            period.exact_inr,
            None,
        ]
    yield _blank_row()
    yield _audit_total_row(_AUDIT_TOTAL_LABEL, result.ordinary_dividend_total_inr_full)
    yield _portal_total_row(
        labels.PORTAL_FIELD_LABEL_OS_ORDINARY_DIVIDEND,
        result.ordinary_dividend_total_inr_rounded,
    )
    yield _portal_total_row(labels.PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, interest_total)
    if result.other_income_lines:
        yield _blank_row()
        yield [
            "_other_income:",
            "Schedule OS 1e nature",
            None,
            "Payment date",
            None,
            "_audit: Gross USD",
            "_audit: FX specified date",
            "_audit: SBI TTBR source date",
            "_audit: SBI TTBR",
            "_audit: Gross INR",
            "Gross INR (rounded ₹)",
        ]
        for line in result.other_income_lines:
            yield [
                line.nature,
                line.nature,
                None,
                line.payment_date,
                line.description,
                line.amount_gross_usd,
                line.fx_specified_date,
                (
                    None
                    if line.fx_rate_publication_date == line.fx_specified_date
                    else line.fx_rate_publication_date
                ),
                line.fx_rate_inr_per_usd,
                line.amount_gross_inr,
                _round_inr_decimal(line.amount_gross_inr),
            ]
        for nature, exact, whole_rupees in result.other_income_totals:
            yield [
                f"_audit: 1e TOTAL / {nature}",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                exact,
                whole_rupees,
            ]


@register(OutputKey(Destination.WORKBOOK, Artifact.OS_XLSX))
class ScheduleOsXlsxWriter:
    """XLSX writer for Schedule OS — sister to :class:`ScheduleOsReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``schedule-os`` subcommand.

    The output workbook contains one sheet (``"Schedule OS"``) with
    the same column layout as the review CSV (11 cells per row), but
    values are written as native Excel types so spreadsheet formulas
    (SUM, etc.) work directly on the data.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them (constructor ``assessment_year_start`` kwarg); see
    :class:`~ibkr_fa.output.schedule_os_review_csv.ScheduleOsReviewCsvWriter`
    for details.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See :class:`ibkr_fa.output.schedule_os_review_csv.ScheduleOsReviewCsvWriter.__init__`
        for the contract.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, result: ScheduleOsComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file.

        Raises:
            InvariantError: ``result.interest_total_inr_rounded`` is
                ``None``; raised before ``path`` is opened.
        """
        interest_total = _require_interest_total(result)
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_OS

        header_font = Font(bold=True)

        rows = _iter_rows(result, interest_total=interest_total, labels=self._labels)
        for row_index, row in enumerate(rows):
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
            # Apply per-column number formats for any row that has
            # the typed cell present at that column.
            for col_offset, fmt in enumerate(_COLUMN_NUMBER_FORMATS):
                if fmt is None:
                    continue
                cell = worksheet.cell(row=data_row_number, column=col_offset + 1)
                if cell.value is None:
                    continue
                cell.number_format = fmt

        worksheet.freeze_panes = "A2"

        with atomic_output_path(path) as tmp_path:
            workbook.save(tmp_path)


__all__ = ["ScheduleOsXlsxWriter"]
