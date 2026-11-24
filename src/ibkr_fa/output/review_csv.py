"""Review-CSV writer for Schedule FA Table A3.

The "review CSV" is v1's default output format: a CSV the user opens
in Excel/Sheets, scans for plausibility, fills in the
:data:`None`-rendered placeholder cells (per the CLI warnings), and
then copies each row into the matching Schedule FA Table A3 row on
the e-filing portal.

Why a CSV (and not, say, a direct portal-format JSON):

* The portal's API isn't open to third-party integrations.
* The user MUST review and approve before filing — a tax return is
  not something you submit auto-generated.
* CSV is universally inspectable and editable, and copy-paste from a
  spreadsheet into the portal's table form works cleanly.

The CSV has TWO column blocks separated by a sentinel divider column:

1. **Portal-facing columns** (11) — labels match the e-filing portal
   verbatim. Order matches the portal form. Values are formatted via
   :mod:`ibkr_fa.output.portal_labels` so that what's in the cell is
   exactly what the user types into the portal. ``None`` values
   render as empty cells (the CLI warns).

2. **Audit columns** (8) — the USD source values + FX rates + dates
   that produced each INR figure, plus the source ISIN/ticker. These
   are NOT entered into the portal; their job is to let the user
   verify any portal cell independently (recompute the INR by hand,
   spot-check against the IBKR statement, etc.). Headers are
   prefixed ``"_audit: "`` so they sort to the right of the portal
   columns and visually separate from "type me into the portal".
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Self

from ibkr_fa.models import FaA3Row
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_text_output
from ibkr_fa.output._writer_contract import write_checked_rows
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay

# Header for the audit-only columns. The "_audit: " prefix is by
# design — it sorts trailing-side in alphabetic spreadsheet sorts
# (the "_" prefix beats most letters in ASCII) AND visually flags
# the column as "ignore when filing". The labels themselves are
# free text; downstream consumers should not parse them.
_AUDIT_COLUMN_HEADERS: tuple[str, ...] = (
    "_audit: Source ISIN",
    "_audit: Source ticker",
    "_audit: Source country (ISO 3166-1 alpha-2)",
    "_audit: Cost basis (USD)",
    "_audit: Acquisition date",
    "_audit: Closing price on Dec 31 (USD)",
    "_audit: TTBR on acquisition date (INR per USD)",
    "_audit: TTBR on Dec 31 (INR per USD)",
    "_audit: Disposal date",
)


def _format_optional_decimal_audit(value: object) -> str:
    """Render an audit-column Decimal value (or :data:`None`) as a
    CSV cell.

    Audit columns are NOT quantized — they preserve IBKR's full
    Decimal precision so the user can independently recompute the
    portal INR figure. ``None`` becomes an empty cell; a closed lot
    can still carry ``audit_ttbr_dec31`` when that rate converted its
    Schedule FA income.
    """
    if value is None:
        return ""
    return str(value)


def _format_optional_date_audit(value: date | None) -> str:
    """Render an audit-column date (or :data:`None`) as ISO-8601.

    Audit columns use ISO-8601 (``YYYY-MM-DD``) on purpose: it's
    the unambiguous machine-readable format, distinct from the
    DD/MM/YYYY portal-facing convention so the user can't confuse
    the two columns. ``None`` becomes an empty cell.
    """
    if value is None:
        return ""
    return value.isoformat()


def _portal_cells(row: FaA3Row, *, labels: ModuleType = _default_labels) -> list[str]:
    """The 11 portal-facing cells, in left-to-right portal order.

    Args:
        row: The :class:`FaA3Row` to render.
        labels: Snapshot module to source label/format helpers from.
            Defaults to the package-level (current-AY) re-exports
            for backward compatibility with direct callers (tests,
            etc.); the production writer threads the per-run AY's
            snapshot via :meth:`ReviewCsvWriter.write`.
    """
    filed = row.filed
    amount = labels.format_amount_for_portal
    return [
        labels.format_country_for_portal(row.source_country_iso2),
        row.name_of_entity,
        row.address_of_entity,
        row.zip_code,
        row.nature_of_entity,
        labels.format_date_for_portal(row.date_of_acquiring),
        amount(filed.initial_value_inr, field="Schedule FA A3 initial value"),
        amount(filed.peak_value_inr, field="Schedule FA A3 peak value"),
        amount(filed.closing_value_inr, field="Schedule FA A3 closing balance"),
        amount(filed.gross_amount_paid_credited_inr, field="Schedule FA A3 gross amount"),
        amount(filed.gross_proceeds_from_sale_inr, field="Schedule FA A3 gross proceeds"),
    ]


def _audit_cells(row: FaA3Row) -> list[str]:
    """The 9 audit cells, in :data:`_AUDIT_COLUMN_HEADERS` order."""
    return [
        row.source_isin,
        row.source_ticker,
        row.source_country_iso2,
        str(row.audit_cost_basis_usd),
        row.audit_acquisition_date.isoformat(),
        _format_optional_decimal_audit(row.audit_close_price_usd),
        str(row.audit_ttbr_acquisition),
        _format_optional_decimal_audit(row.audit_ttbr_dec31),
        _format_optional_date_audit(row.audit_disposal_date),
    ]


def _iter_rows(rows: Iterable[FaA3Row], *, labels: ModuleType) -> Iterator[list[str]]:
    yield list(labels.PORTAL_COLUMN_HEADERS_FA_A3) + list(_AUDIT_COLUMN_HEADERS)
    for row in rows:
        yield _portal_cells(row, labels=labels) + _audit_cells(row)


@register(OutputKey(Destination.REVIEW, Artifact.FA_A3_CSV))
class ReviewCsvWriter:
    """v1 default writer: produces a copy-paste-friendly CSV.

    Implements :class:`~ibkr_fa.output.base.FaA3Writer`.
    Registered under :attr:`format_id` ``"review-csv"`` — that's the
    value the CLI's ``--format`` flag will accept.

    Per-AY portal labels (issue #65): The writer accepts an optional
    ``assessment_year_start`` constructor argument. When the
    pipeline passes it (e.g., the FA-A3 pipeline supplies
    ``calendar_year + 1`` per Section 139 + Section 6 of the
    Income-tax Act, 1961), the writer sources labels +
    format-helpers from
    :func:`ibkr_fa.output.portal_labels.for_ay` instead of the
    process-wide ``CURRENT_AY_START`` default. This lets a single
    run target a non-default AY (e.g., a CY 2026 run filing in
    AY 2027-28) while the registered snapshot for that AY is the
    forward-compat fallback. Backward compat: callers that don't
    pass ``assessment_year_start`` still get the current-AY
    snapshot (no behaviour change).
    """

    format_id: ClassVar[str] = "review-csv"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        Args:
            assessment_year_start: 4-digit AY start year
                (e.g., ``2026`` for AY 2026-27). When ``None``
                (the default), the writer uses the package-level
                current-AY re-exports — preserves pre-#65
                behaviour for callers that don't yet thread the
                AY.

        Raises:
            UnknownAssessmentYearError: If ``assessment_year_start``
                is not registered and is more than one year past
                the latest registered snapshot. The +1 forward-
                compat fallback returns the latest snapshot
                silently; the pipeline / CLI is responsible for
                emitting a "labels not yet verified for this AY"
                warning when
                :func:`ibkr_fa.output.portal_labels.is_forward_compat_fallback`
                returns ``True``.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
        """Serialise ``rows`` to ``path``.

        Per the :class:`FaA3Writer` Protocol contract:

        * Overwrites ``path`` if it exists.
        * Replaces a symlink at ``path`` rather than writing through it,
          matching the safer atomic sibling writers.
        * Does NOT create parent directories — fails loud on a typo'd
          path so the user notices before assuming the file was
          written. Wrap in ``mkdir -p`` outside if you want directory
          auto-creation.
        * ``rows`` is materialised exactly once.

        The CSV uses the default ``csv.writer`` quoting policy: only
        cells containing commas, quotes, or newlines are quoted.
        That's enough for the portal-facing cells (free-text "Name of
        entity" may contain commas) and keeps the audit columns
        unquoted for diffability.

        Args:
            rows: The :class:`FaA3Row` records to serialise. Order is
                preserved (the row builder already orders open lots
                before closed lots).
            path: Destination file path. Must reside in a directory
                that already exists.

        Raises:
            OSError: If ``path``'s parent directory doesn't exist or
                isn't writable.
        """
        materialised = list(rows)
        with atomic_text_output(path, encoding="utf-8", newline="") as fh:
            write_checked_rows(
                csv.writer(fh), _iter_rows(materialised, labels=self._labels), start_index=0
            )
