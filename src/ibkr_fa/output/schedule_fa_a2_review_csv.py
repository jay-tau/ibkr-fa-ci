"""Review-CSV writer for Schedule FA Table A2 (the IBKR custodial account, #18).

Sister to :mod:`ibkr_fa.output.review_csv` (Table A3), but emitted as a SEPARATE
sibling file (e.g. ``schedule-fa-a2.csv`` alongside the A3 ``schedule-fa.csv``)
so the working A3 output is untouched. One row — the IBKR account itself.

Two column blocks: the 11 portal-facing cells (labels match the e-filing
portal verbatim, ``None`` → empty cell with a CLI manual-fill warning) and the
audit cells (the base-currency NAV + TTBR provenance + the per-nature gross
breakdown), prefixed ``"_audit: "``. Every cell passes the NUL +
formula-injection contract before write, and the file is written atomically.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from ibkr_fa.models.fa_a2_row import FaA2Row
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

_AUDIT_HEADERS: Final[tuple[str, ...]] = (
    "_audit: Base currency",
    "_audit: Closing balance (base currency, full precision)",
    "_audit: TTBR on Dec 31 (INR per base-currency unit)",
    "_audit: Account number sourced from config (statement masked)",
    "_audit: NAV cross-checked vs Change in NAV",
    "_audit: Gross credited components (per nature)",
    "_audit: Peak lower bound INR (converted at its own date; full precision)",
    "_audit: Peak lower bound date",
    "_audit: Peak upper bound INR (converted at its own date; full precision)",
    "_audit: Peak upper bound date",
    "_audit: Selected peak bound",
    "_audit: Selected peak balance (base currency, full precision)",
    "_audit: TTBR on selected peak date",
    "_audit: Maximum statement account value (base currency)",
    "_audit: Maximum statement account value date",
    "_audit: Peak override source",
    "_audit: Peak override reason",
)


def _format_optional_decimal(value: Decimal | None) -> str:
    """Render a full-precision audit Decimal, escaped for CSV safety.

    A negative value (e.g. a margined account's base-currency NAV) starts with
    ``-`` which would trip the formula-injection guard, so it gets the OWASP
    ``'`` prefix (Excel strips it on open) — mirrors Schedule CG's audit sums.
    ``None`` → empty cell.
    """
    if value is None:
        return ""
    rendered = str(value)
    return f"'{rendered}" if rendered.startswith("-") else rendered


def _format_amount_csv_safe(value: Decimal | None, *, labels: ModuleType, field: str) -> str:
    """Filed whole-rupee portal amount, escaped for CSV safety.

    A negative closing balance (a margined account's NAV) renders with a
    leading ``-`` which would trip the formula-injection guard, so it gets the
    OWASP-canonical ``'`` prefix (Excel strips it on open) — mirrors
    :func:`~ibkr_fa.output._writer_contract.csv_safe_signed`. ``None`` → empty cell.
    """
    rendered = labels.format_amount_for_portal(value, field=f"Schedule FA A2 {field}")
    return f"'{rendered}" if rendered.startswith("-") else rendered


def _format_components(components: Mapping[str, Decimal]) -> str:
    """Render the per-nature gross breakdown, e.g. ``"Dividend=1.5; Interest=0.73"``."""
    return "; ".join(f"{name}={amount}" for name, amount in sorted(components.items()))


def _header_row(labels: ModuleType) -> list[str]:
    return [*labels.PORTAL_COLUMN_HEADERS_FA_A2, *_AUDIT_HEADERS]


def _portal_cells(row: FaA2Row, *, labels: ModuleType) -> list[str]:
    """The 11 portal-facing cells, in left-to-right portal order."""
    filed = row.filed
    return [
        f"{row.country_code}-{row.country_name}",
        row.institution_name,
        row.institution_address,
        row.institution_zip,
        row.account_number,
        row.status.value,
        labels.format_date_for_portal(row.account_opening_date),
        _format_amount_csv_safe(filed.peak_balance_inr, labels=labels, field="peak balance"),
        _format_amount_csv_safe(filed.closing_balance_inr, labels=labels, field="closing balance"),
        "" if row.nature_of_amount is None else row.nature_of_amount.value,
        _format_amount_csv_safe(filed.gross_amount_inr, labels=labels, field="gross amount"),
    ]


def _audit_cells(row: FaA2Row) -> list[str]:
    """The 6 audit cells, in :data:`_AUDIT_HEADERS` order."""
    return [
        row.base_currency,
        _format_optional_decimal(row.audit_closing_balance_base_ccy),
        _format_optional_decimal(row.audit_ttbr_dec31),
        "yes" if row.audit_account_number_from_config else "no",
        "yes" if row.audit_nav_cross_checked else "no",
        _format_components(row.audit_gross_components),
        _format_optional_decimal(row.audit_peak_balance_lower_inr),
        (
            ""
            if row.audit_peak_balance_lower_date is None
            else row.audit_peak_balance_lower_date.isoformat()
        ),
        _format_optional_decimal(row.audit_peak_balance_upper_inr),
        (
            ""
            if row.audit_peak_balance_upper_date is None
            else row.audit_peak_balance_upper_date.isoformat()
        ),
        row.audit_peak_balance_bound or "",
        _format_optional_decimal(row.audit_peak_balance_base_ccy),
        _format_optional_decimal(row.audit_peak_ttbr),
        _format_optional_decimal(row.audit_statement_peak_floor_base_ccy),
        (
            ""
            if row.audit_statement_peak_floor_date is None
            else row.audit_statement_peak_floor_date.isoformat()
        ),
        row.audit_peak_override_source or "",
        row.audit_peak_override_reason or "",
    ]


def _iter_rows(rows: Iterable[FaA2Row], *, labels: ModuleType) -> Iterator[list[str]]:
    yield _header_row(labels)
    for row in rows:
        yield _portal_cells(row, labels=labels) + _audit_cells(row)


@register(OutputKey(Destination.REVIEW, Artifact.FA_A2_CSV))
class ScheduleFaA2ReviewCsvWriter:
    """Write the Schedule FA Table A2 row as a review CSV.

    Per-AY portal labels (issue #65): accepts an optional
    ``assessment_year_start``; when the pipeline passes it, labels come from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the process-wide
    current-AY default.
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

    def write(self, rows: Iterable[FaA2Row], path: Path) -> None:
        """Serialise ``rows`` to ``path`` using an atomic temp-file rename."""
        with atomic_text_output(path, encoding="utf-8-sig", newline="") as fh:
            write_checked_rows(csv.writer(fh), _iter_rows(rows, labels=self._labels), start_index=0)
