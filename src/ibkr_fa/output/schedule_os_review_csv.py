"""Review-CSV writer for Schedule OS ordinary dividends.

CSV contract (locked by ``tests/test_schedule_os_review_csv.py`` +
``tests/test_writer_contract.py`` + ``scripts/smoke_test_schedule_os.sh``):

* **Encoding**: UTF-8 with BOM (``utf-8-sig``) so Excel recognises the
  file as UTF-8 on double-click without manual import-wizard steps.
* **Uniform width**: every emitted row is exactly ``_COLUMN_COUNT = 11``
  cells. Header, detail, marker, audit, and TOTAL rows all conform; the
  blank-row delimiter between sections is ``[""] * 11`` (not a
  truly-empty row), so ``csv.reader`` parses it as an 11-cell row of
  empty strings.
* **Section order**: header → all allow-listed ordinary-dividend detail
  rows, globally sorted by ``(payment_date, symbol)`` while retaining
  source country → five Schedule OS section 10 / Quicko period totals → one full-precision
  audit total → one Schedule OS 1a(i) portal total → one Schedule OS
  1b(ix) interest total → optional ``_other_income:`` 1e block (emitted
  only when ``result.other_income_lines`` is non-empty).
* **TOTAL-row contract**: the 1a(i) portal total is followed by the
  1b(ix) interest total, which is the literal last row unless the
  ``_other_income:`` 1e block follows it. The 1b(ix) row carries
  ``result.interest_total_inr_rounded`` as stored, printed at ``0`` too;
  the file has no per-credit interest detail block. A result without that
  total is refused before the target is opened. India/foreign source
  grouping remains audit data and is not rounded into separate portal
  cells.
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
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.invariants import invariant_error
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

_COLUMN_COUNT: Final[int] = 11

_HEADER_ROW: Final[list[str]] = [
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
]

_AUDIT_TOTAL_LABEL: Final[str] = "_audit: 1a(i) TOTAL Gross INR (full precision)"
_DIVIDEND_PERIOD_SECTION_LABEL: Final[str] = (
    "Parsed-source contribution to Schedule OS section 10 / Quicko dividend periods"
)
_DIVIDEND_PERIOD_LABELS: Final[tuple[str, ...]] = (
    "Quicko Period 1 / ITR: Up to 15/06",
    "Quicko Period 2 / ITR: 16/06 to 15/09",
    "Quicko Period 3 / ITR: 16/09 to 15/12",
    "Quicko Period 4 / ITR: 16/12 to 15/03",
    "Quicko Period 5 / ITR: 16/03 to 31/03",
)
_ONE_RUPEE: Final[Decimal] = Decimal("1")


def _require_interest_total(result: ScheduleOsComputeResult) -> Decimal:
    """Return the result's 1b(ix) total, or refuse a result without one."""
    interest_total = result.interest_total_inr_rounded
    if interest_total is None:
        raise invariant_error(
            what="Schedule OS review output requires the separately computed interest total"
        )
    return interest_total


def _round_inr(value: Decimal) -> Decimal:
    """Round a rupee amount to whole INR using CBDT FAQ HALF_UP convention."""
    return value.quantize(_ONE_RUPEE, rounding=ROUND_HALF_UP)


def _blank_row() -> list[str]:
    return [""] * _COLUMN_COUNT


def _detail_row(
    line: ScheduleOsDividendLine | ScheduleOsDomesticDividendLine,
) -> list[str]:
    if line.fx_rate_publication_date == line.fx_specified_date:
        rate_publication_cell = ""
    else:
        rate_publication_cell = line.fx_rate_publication_date.isoformat()
    return [
        str(line.symbol),
        str(line.isin),
        line.issuer_country_iso2,
        line.payment_date.strftime("%d/%m/%Y"),
        line.kind.value,
        str(line.amount_gross_usd),
        line.fx_specified_date.isoformat(),
        rate_publication_cell,
        str(line.fx_rate_inr_per_usd),
        str(line.amount_gross_inr),
        str(_round_inr(line.amount_gross_inr)),
    ]


def _audit_total_row(label: str, full_precision_sum: Decimal) -> list[str]:
    return [
        label,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        str(full_precision_sum),
        "",
    ]


def _portal_total_row(label: str, rounded_total: Decimal) -> list[str]:
    return [
        label,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        str(rounded_total),
    ]


def _iter_rows(
    result: ScheduleOsComputeResult,
    *,
    interest_total: Decimal,
    labels: ModuleType = _default_labels,
) -> Iterator[list[str]]:
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
        "",
        "",
        "",
        "",
        "",
        "",
        "_audit: Income (full precision INR)",
        "",
    ]
    for period_label, period in zip(_DIVIDEND_PERIOD_LABELS, result.dividend_periods, strict=True):
        yield [
            period_label,
            str(period.whole_rupees),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            str(period.exact_inr),
            "",
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
            "",
            "Payment date",
            "",
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
                "",
                line.payment_date.strftime("%d/%m/%Y"),
                line.description,
                str(line.amount_gross_usd),
                line.fx_specified_date.isoformat(),
                (
                    ""
                    if line.fx_rate_publication_date == line.fx_specified_date
                    else line.fx_rate_publication_date.isoformat()
                ),
                str(line.fx_rate_inr_per_usd),
                str(line.amount_gross_inr),
                str(_round_inr(line.amount_gross_inr)),
            ]
        for nature, exact, whole_rupees in result.other_income_totals:
            yield [
                f"_audit: 1e TOTAL / {nature}",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                str(exact),
                str(whole_rupees),
            ]


@register(OutputKey(Destination.REVIEW, Artifact.OS_CSV))
class ScheduleOsReviewCsvWriter:
    """Write a Schedule OS compute result as a review CSV.

    Per-AY portal labels (issue #65): The writer accepts an
    optional ``assessment_year_start`` constructor argument. When
    the pipeline passes
    ``financial_year.assessment_year_start``, the writer sources
    Schedule OS portal labels from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default. See
    :class:`ibkr_fa.output.review_csv.ReviewCsvWriter` for the
    full rationale.
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

    def write(self, result: ScheduleOsComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` using an atomic temp-file rename.

        Raises:
            InvariantError: ``result.interest_total_inr_rounded`` is
                ``None``; raised before ``path`` is opened.
        """
        interest_total = _require_interest_total(result)
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(
                csv.writer(fh),
                _iter_rows(result, interest_total=interest_total, labels=self._labels),
                start_index=0,
            )
