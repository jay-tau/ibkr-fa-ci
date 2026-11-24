"""Parse Fidelity NetBenefits open/closed-lots CSV exports into raw lot models.

The two exports (``View open lots.csv`` / ``View closed lots.csv``) are the raw
record of the ESPP shares. They carry **no** security identity (symbol / CUSIP /
ISIN) — that comes from ``[netbenefits.security]`` config and is joined
downstream, never duplicated here. The models below capture only what the CSV
states, verbatim. Fidelity's "Holding period" / "Term" is kept **raw** and is
NEVER used for Indian capital-gains classification (that is computed from the
dates downstream — ADR-0003 decision 11).

Three Fidelity export quirks are handled:

* a UTF-8 BOM (read with ``utf-8-sig``);
* a trailing blank line and a ``"The values are displayed in USD"`` footer —
  the only currency signal, so it is asserted (the non-USD guard);
* the closed-lots "Date sold or transferred" header cell wrapped in
  ``<span style="S">…</span>``, whose inline CSS commas (``rgb(0, 0, 51)``)
  would otherwise make ``csv.reader`` split one header cell into extra columns. ``S`` is
  one of the two documented styles in ``_DECORATED_DATE_SOLD``. Exactly one such
  decoration is removed from the file's first non-blank line (a line of only
  commas and whitespace is blank) before parsing; nothing else is ever removed.

Any ``<`` or ``>`` left after that is markup, which could hide a cell, a row or a
whole header (#1097). The readers refuse it, naming the header or the cell, and
:func:`detect_fidelity_lots_csv` reports the file as a :class:`MarkupNearMiss`,
never as unrelated.

A run that ignores open lots may ignore a markup file only when at least one
non-blank record is a lots header or a near miss, and every such candidate is
the exact open-lots header or an open-lots-only near miss.

The parse functions are pure (text in, models out) for fixture-based testing;
thin ``read_*`` wrappers and ``detect_fidelity_lots_csv`` do the file I/O.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ParseError, errno_name
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._csv_records import csv_records

_SECTION_OPEN: Final[str] = "Fidelity open lots"
_SECTION_CLOSED: Final[str] = "Fidelity closed lots"

# The footer is the only currency signal (there is no per-row currency column).
_USD_FOOTER: Final[str] = "The values are displayed in USD"
# ESPP shares are "Share source" == "SP"; anything else (RS, etc.) is a
# different plan with different tax treatment and is rejected here.
_ESPP_SHARE_SOURCE: Final[str] = "SP"

# Locale-proof month parsing (``%b`` is locale-sensitive; Fidelity emits English).
_MONTHS: Final[dict[str, int]] = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Z][a-z]{2})-(\d{1,2})-(\d{4})$")
_DATE_SOLD: Final[str] = "Date sold or transferred"
# The closed-lots header's only documented decoration (#324, #1097): one of
# these exact cells on the first non-blank line loses its <span> tags, once.
_DECORATED_DATE_SOLD: Final[tuple[str, ...]] = tuple(
    f'<span style="{style}">{_DATE_SOLD}</span>'
    for style in (
        "color: rgb(0, 0, 51); background-color: rgb(255, 255, 255);",
        "color: rgb(0, 0, 51);",
    )
)
_HEADER_MARKUP: Final[str] = (
    f"the header carries markup other than the export's documented <span> around {_DATE_SOLD!r}"
)
_CELL_MARKUP: Final[str] = (
    "the cell holds markup ('<' or '>'); only the closed-lots "
    f"{_DATE_SOLD!r} header may carry the export's documented <span> decoration"
)
_REEXPORT: Final[str] = "re-export it from NetBenefits."
_CENT: Final[Decimal] = Decimal("0.01")

_OPEN_HEADER: Final[tuple[str, ...]] = (
    "Date acquired",
    "Quantity",
    "Cost basis",
    "Cost basis/share",
    "Value",
    "Gain/loss",
    "Sale availability date",
    "Transfer availability date",
    "Grant date",
    "Share source",
    "Holding period",
)
_CLOSED_HEADER: Final[tuple[str, ...]] = (
    "Date acquired",
    "Quantity",
    _DATE_SOLD,
    "Proceeds",
    "Cost basis",
    "Gain/loss",
    "Term",
)


def _column_key(cell: str) -> str:
    """A header cell compared ignoring case and spacing, for near-miss detection."""
    return " ".join(cell.split()).casefold()


_OPEN_KEYS: Final[frozenset[str]] = frozenset(map(_column_key, _OPEN_HEADER))
_CLOSED_KEYS: Final[frozenset[str]] = frozenset(map(_column_key, _CLOSED_HEADER))
_OPEN_ONLY_KEYS: Final[frozenset[str]] = _OPEN_KEYS - _CLOSED_KEYS
# Columns only a lots export has: every lots column except the money and
# quantity ones (Quantity, Cost basis, Value, Gain/loss, Proceeds, Term), whose
# names other CSVs use too.
_LOTS_ONLY_KEYS: Final[frozenset[str]] = (_OPEN_KEYS | _CLOSED_KEYS) - frozenset(
    map(_column_key, ("Quantity", "Cost basis", "Value", "Gain/loss", "Proceeds", "Term"))
)
_DATE_ACQUIRED_KEY: Final[str] = _column_key("Date acquired")


class FidelityLotsKind(StrEnum):
    """Which Fidelity lots export a CSV is (by its header)."""

    OPEN = "open-lots"
    CLOSED = "closed-lots"


@dataclass(frozen=True, slots=True)
class NearMissLotsHeader:
    """A first CSV row that looks like a Fidelity lots header but matches neither
    exact header (#911): it starts with "Date acquired" or has a column only a
    lots export has. A bundle refuses such a file rather than ignore it, since a
    changed export could hold the lots the run needs.

    Attributes:
        columns: The row's stripped cells.
        open_lots_only: Every column is an open-lots column, ignoring case and
            spacing, and one of them is in the open-lots export only. A run that
            ignores open-lots exports may ignore this file too; any other near
            miss may be a closed-lots export.
    """

    columns: tuple[str, ...]
    open_lots_only: bool

    def difference(self) -> str:
        """How the row differs from the nearer lots header (closed-lots on a tie).

        Names only the expected columns, which are code constants, and counts the
        unexpected ones: a header cell is file content and is never printed.
        """
        have = Counter(self.columns)
        diffs = []
        for kind, expected in (
            (FidelityLotsKind.CLOSED, _CLOSED_HEADER),
            (FidelityLotsKind.OPEN, _OPEN_HEADER),
        ):
            missing = tuple((Counter(expected) - have).elements())
            unexpected = tuple((have - Counter(expected)).elements())
            diffs.append((kind, expected, missing, unexpected))
        kind, expected, missing, unexpected = min(diffs, key=lambda d: len(d[2]) + len(d[3]))
        if not missing and not unexpected:
            return f"it has the {kind} columns, but not in the expected order {expected}"
        return (
            f"compared with the {kind} header {expected}, missing: "
            f"{', '.join(map(repr, missing)) or 'none'}; unexpected: {len(unexpected)} column(s)"
        )


@dataclass(frozen=True, slots=True)
class MarkupNearMiss:
    """A CSV holding markup (``<`` or ``>``) other than the closed-lots header's
    documented decoration (#1097). Markup can hide a lots header or rows, so the
    file is never treated as unrelated or as a lots export.

    Attributes:
        open_lots_only: The file proves it is an open-lots export only: at least
            one non-blank record is a lots header or a near miss (a candidate),
            and every candidate is the exact open-lots header or an open-lots-only
            :class:`NearMissLotsHeader`. A run that ignores open-lots exports may
            ignore this file too; any other markup file may hide closed lots.
    """

    open_lots_only: bool


@dataclass(frozen=True, slots=True)
class EspLot:
    """One held ESPP lot from ``View open lots.csv``.

    Attributes:
        acquire_date: "Date acquired" (the purchase date).
        quantity: "Quantity" of shares (> 0).
        contribution_usd: "Cost basis" — the **discounted price the employee
            paid** (the US basis). NOT the Schedule FA / capital-gains cost
            basis (that is the FMV at purchase; ADR-0003 decision 2).
        cost_per_share_paid: "Cost basis/share" — ``contribution_usd`` per share.
        fidelity_term: "Holding period" ("Short" / "Long"), kept RAW — never
            used for Indian CG classification.
    """

    acquire_date: _dt.date
    quantity: Decimal
    contribution_usd: Decimal
    cost_per_share_paid: Decimal
    fidelity_term: str


@dataclass(frozen=True, slots=True)
class EspClosedLot:
    """One disposed ESPP lot from ``View closed lots.csv``.

    Attributes:
        acquire_date: "Date acquired".
        sold_or_transferred_date: "Date sold or transferred" — a disposal that
            may be a sale OR a transfer (named so downstream never assumes a
            sale).
        quantity: "Quantity" of shares (> 0).
        proceeds_usd: "Proceeds".
        contribution_usd: "Cost basis" — the discounted price paid (US basis),
            as for :class:`EspLot`.
        fidelity_term: "Term" ("Short" / "Long"), kept RAW.
    """

    acquire_date: _dt.date
    sold_or_transferred_date: _dt.date
    quantity: Decimal
    proceeds_usd: Decimal
    contribution_usd: Decimal
    fidelity_term: str


def _date(raw: str, *, section: str, row_index: int, column: str) -> _dt.date:
    match = _DATE_RE.match(raw.strip())
    if match is None or match.group(1) not in _MONTHS:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="not a Fidelity date (expected e.g. 'Mar-31-2026')",
            raw_value="",
        )
    with contextlib.suppress(ValueError):
        return _dt.date(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))
    # Refused outside the handler, so no chained error names the date's parts.
    raise ParseError(
        section=section,
        row_index=row_index,
        column=column,
        message="not a valid calendar date",
        raw_value="",
    )


def _decimal(raw: str, *, section: str, row_index: int, column: str, kind: NumberKind) -> Decimal:
    # Amounts are parsed as plain decimals, with no thousands separators: a
    # "1,234.50" figure fails loud here (safe — no wrong number flows
    # downstream) rather than being silently mis-stripped. Revisit only against
    # a confirmed large-figure export (the locale is pinned USD by the footer).
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="not a valid number",
            raw_value="",
        ) from exc
    if not value.is_finite():
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message="must be a finite number",
            raw_value="",
        )
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise ParseError(
            section=section, row_index=row_index, column=column, message=breach, raw_value=""
        )
    return value


def _without_header_decoration(text: str) -> str:
    """``text`` with the documented decoration (see ``_DECORATED_DATE_SOLD``)
    removed once from its first non-blank line, if that line carries it. A line
    of only commas and whitespace is blank. Nothing else is removed."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.replace(",", "").strip():
            for decorated in _DECORATED_DATE_SOLD:
                if decorated in line:
                    lines[index] = line.replace(decorated, _DATE_SOLD, 1)
                    return "\n".join(lines)
            break
    return text


def _has_markup(text: str) -> bool:
    return "<" in text or ">" in text


def _data_rows(
    text: str, expected_header: tuple[str, ...], *, section: str
) -> list[tuple[int, list[str]]]:
    """Remove the documented header decoration, CSV-parse, refuse markup,
    validate the header, assert the USD footer, and return the data rows as
    ``(physical_row_index, cells)`` pairs.

    The physical index (the zero-based line the row starts on, counting the
    header, any blank lines and every line a quoted cell spans) is returned so
    cell-level errors in the callers report the same line number as the
    structural errors raised here. Every refusal here that names a row reports
    that line too.

    Only one exact ``<span style="S">Date sold or transferred</span>`` on the
    first non-blank line is removed, ``S`` being a documented style (see the
    module docstring). Any ``<`` or ``>`` left in the header refuses as header
    markup. In every later row, a ``<`` or ``>`` in any cell refuses before the
    footer and column-count checks, naming the cell's header column (``None``
    past the header's width).

    Raises :class:`ParseError` on a malformed CSV record (a quote left open to
    the end of the file, text after a closing quote, or a cell longer than the
    CSV reader allows, #1098), markup, a wrong header, a missing/garbled USD
    footer, a stray row after the footer, or a data row with the wrong column
    count.
    """
    non_blank = list(
        csv_records(
            io.StringIO(_without_header_decoration(text.lstrip("\ufeff"))),
            section=section,
            first_line=0,
            reexport=_REEXPORT,
        )
    )
    if not non_blank:
        raise ParseError(
            section=section,
            row_index=0,
            column=None,
            message="file is empty or contains only blank lines",
            raw_value="",
        )

    header_index, header = non_blank[0]
    if any(map(_has_markup, header)):
        raise ParseError(
            section=section,
            row_index=header_index,
            column=None,
            message=_HEADER_MARKUP,
            raw_value="",
        )
    if tuple(c.strip() for c in header) != expected_header:
        raise ParseError(
            section=section,
            row_index=header_index,
            column=None,
            message=f"unexpected header; expected columns {expected_header}",
            raw_value="",
        )

    data: list[tuple[int, list[str]]] = []
    footer_seen = False
    for i, row in non_blank[1:]:
        for column_index, cell in enumerate(row):
            if _has_markup(cell):
                raise ParseError(
                    section=section,
                    row_index=i,
                    column=(
                        expected_header[column_index]
                        if column_index < len(expected_header)
                        else None
                    ),
                    message=_CELL_MARKUP,
                    raw_value="",
                )
        cells = [c.strip() for c in row]
        # Fidelity may pad the footer to the full CSV width with empty cells.
        if cells[0] == _USD_FOOTER and all(not cell for cell in cells[1:]):
            footer_seen = True
            continue
        if footer_seen:
            raise ParseError(
                section=section,
                row_index=i,
                column=None,
                message=f"unexpected row after the '{_USD_FOOTER}' footer",
                raw_value="",
            )
        if len(row) != len(expected_header):
            raise ParseError(
                section=section,
                row_index=i,
                column=None,
                message=(
                    f"row has {len(row)} columns, expected {len(expected_header)} "
                    "(possible column shift / corruption)"
                ),
                raw_value="",
            )
        data.append((i, row))

    if not footer_seen:
        raise ParseError(
            section=section,
            row_index=header_index,
            column=None,
            message=(
                f"missing the '{_USD_FOOTER}' footer — the only signal that the "
                "figures are USD; refusing rather than assume the currency"
            ),
            raw_value="",
        )
    return data


def parse_open_lots(text: str, *, source: str = _SECTION_OPEN) -> tuple[EspLot, ...]:
    """Parse ``View open lots.csv`` text into :class:`EspLot` records (pure).

    ``source`` labels the file in error messages.

    Raises:
        ParseError: A malformed header, footer, row or cell, or a row whose
            columns disagree. Cell refusals name ``source``, the CSV row and the
            column; structural refusals name the affected row or file. Neither
            prints a cell's or row's contents.
    """
    section = source
    lots: list[EspLot] = []
    for i, row in _data_rows(text, _OPEN_HEADER, section=section):
        acquire_date = _date(row[0], section=section, row_index=i, column="Date acquired")
        quantity = _decimal(
            row[1], section=section, row_index=i, column="Quantity", kind=NumberKind.QUANTITY
        )
        contribution = _decimal(
            row[2], section=section, row_index=i, column="Cost basis", kind=NumberKind.MONEY
        )
        cost_per_share = _decimal(
            row[3], section=section, row_index=i, column="Cost basis/share", kind=NumberKind.PRICE
        )
        value = _decimal(
            row[4], section=section, row_index=i, column="Value", kind=NumberKind.MONEY
        )
        gain_loss = _decimal(
            row[5], section=section, row_index=i, column="Gain/loss", kind=NumberKind.MONEY
        )
        share_source = row[9].strip()
        if share_source != _ESPP_SHARE_SOURCE:
            raise ParseError(
                section=section,
                row_index=i,
                column="Share source",
                message=(
                    f"Share source is not {_ESPP_SHARE_SOURCE!r} (ESPP); "
                    "only ESPP lots are supported"
                ),
                raw_value="",
            )
        # A held lot has a positive quantity and positive money paid; the current
        # value is non-negative. Only Gain/loss may be negative.
        _require_positive(quantity, section=section, row_index=i, column="Quantity")
        _require_positive(contribution, section=section, row_index=i, column="Cost basis")
        _require_positive(cost_per_share, section=section, row_index=i, column="Cost basis/share")
        _require_non_negative(value, section=section, row_index=i, column="Value")
        # Cross-check the redundant columns to catch a column shift / corruption.
        _require_close(
            value - contribution,
            gain_loss,
            _CENT,
            section=section,
            row_index=i,
            column="Gain/loss",
            what="Value - Cost basis should equal Gain/loss",
        )
        # Catches an independently-corrupted "Cost basis/share" cell. It does NOT
        # catch a Quantity<->Cost basis/share value transposition (that preserves
        # both the product and the quotient, since Fidelity emits
        # cost-per-share = cost-basis / quantity) — the header guards the column
        # order, and the #310 Yahoo price cross-check validates the magnitude.
        _require_close(
            cost_per_share * quantity,
            contribution,
            quantity * Decimal("0.005") + _CENT,
            section=section,
            row_index=i,
            column="Cost basis/share",
            what="Cost basis/share x Quantity should equal Cost basis",
        )
        lots.append(
            EspLot(
                acquire_date=acquire_date,
                quantity=quantity,
                contribution_usd=contribution,
                cost_per_share_paid=cost_per_share,
                fidelity_term=row[10].strip(),
            )
        )
    return tuple(lots)


def parse_closed_lots(text: str, *, source: str = _SECTION_CLOSED) -> tuple[EspClosedLot, ...]:
    """Parse ``View closed lots.csv`` text into :class:`EspClosedLot` records.

    Pure. An export with no disposals (header + footer only) yields ``()``.
    ``source`` labels the file in error messages.

    Note: the closed-lots export has **no "Share source" column**, so — unlike
    ``parse_open_lots`` — it cannot filter to ESPP (``SP``) rows. The caller must
    supply the ESPP closed-lots view; the downstream identity join attributes
    every parsed lot to the single configured ``[netbenefits.security]``, so a
    multi-plan closed export would misattribute. (A non-empty closed export is the
    consumer's concern in #318.)

    Raises:
        ParseError: As for :func:`parse_open_lots`; refusals carry no value.
    """
    section = source
    lots: list[EspClosedLot] = []
    for i, row in _data_rows(text, _CLOSED_HEADER, section=section):
        acquire_date = _date(row[0], section=section, row_index=i, column="Date acquired")
        quantity = _decimal(
            row[1], section=section, row_index=i, column="Quantity", kind=NumberKind.QUANTITY
        )
        sold_date = _date(row[2], section=section, row_index=i, column="Date sold or transferred")
        proceeds = _decimal(
            row[3], section=section, row_index=i, column="Proceeds", kind=NumberKind.MONEY
        )
        contribution = _decimal(
            row[4], section=section, row_index=i, column="Cost basis", kind=NumberKind.MONEY
        )
        gain_loss = _decimal(
            row[5], section=section, row_index=i, column="Gain/loss", kind=NumberKind.MONEY
        )
        _require_positive(quantity, section=section, row_index=i, column="Quantity")
        _require_positive(contribution, section=section, row_index=i, column="Cost basis")
        _require_non_negative(proceeds, section=section, row_index=i, column="Proceeds")
        if sold_date < acquire_date:
            raise ParseError(
                section=section,
                row_index=i,
                column="Date sold or transferred",
                message="the disposal date precedes the acquisition date",
                raw_value="",
            )
        _require_close(
            proceeds - contribution,
            gain_loss,
            _CENT,
            section=section,
            row_index=i,
            column="Gain/loss",
            what="Proceeds - Cost basis should equal Gain/loss",
        )
        lots.append(
            EspClosedLot(
                acquire_date=acquire_date,
                sold_or_transferred_date=sold_date,
                quantity=quantity,
                proceeds_usd=proceeds,
                contribution_usd=contribution,
                fidelity_term=row[6].strip(),
            )
        )
    return tuple(lots)


def _require_positive(value: Decimal, *, section: str, row_index: int, column: str) -> None:
    if value <= 0:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"{column} must be > 0",
            raw_value="",
        )


def _require_non_negative(value: Decimal, *, section: str, row_index: int, column: str) -> None:
    if value < 0:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"{column} must be >= 0",
            raw_value="",
        )


def _require_close(
    got: Decimal,
    expected: Decimal,
    tolerance: Decimal,
    *,
    section: str,
    row_index: int,
    column: str,
    what: str,
) -> None:
    if abs(got - expected) > tolerance:
        raise ParseError(
            section=section,
            row_index=row_index,
            column=column,
            message=f"{what}, but they differ by more than the rounding tolerance",
            raw_value="",
        )


def detect_fidelity_lots_kind(
    first_row: list[str],
) -> FidelityLotsKind | NearMissLotsHeader | None:
    """Classify a CSV record (with the documented header decoration removed) as
    the open / closed lots header, a :class:`NearMissLotsHeader`, or ``None`` if
    it is not a lots header at all (e.g. an IBKR export)."""
    header = tuple(c.strip() for c in first_row)
    if header == _OPEN_HEADER:
        return FidelityLotsKind.OPEN
    if header == _CLOSED_HEADER:
        return FidelityLotsKind.CLOSED
    keys = frozenset(map(_column_key, header))
    starts_like_lots = bool(header) and _column_key(header[0]).startswith(_DATE_ACQUIRED_KEY)
    if not starts_like_lots and keys.isdisjoint(_LOTS_ONLY_KEYS):
        return None
    return NearMissLotsHeader(
        columns=header,
        open_lots_only=keys <= _OPEN_KEYS and not keys.isdisjoint(_OPEN_ONLY_KEYS),
    )


def detect_fidelity_lots_csv(
    path: Path, *, source: str = "Fidelity lots CSV"
) -> FidelityLotsKind | NearMissLotsHeader | MarkupNearMiss | None:
    """Classify ``path`` as a Fidelity lots export, a near miss, or ``None`` if
    it is not one and does not look like one. ``source`` labels the file if it
    can't be read (see :func:`_read_csv_text`).

    The documented header decoration is removed first (see the module
    docstring). If the text then holds no ``<`` or ``>``, the first non-blank
    record is classified by :func:`detect_fidelity_lots_kind`. Otherwise the file
    is a :class:`MarkupNearMiss`, never ``None``: every non-blank record is
    classified, and the file is open-lots only when at least one record is a
    lots header or a near miss and every such record is the exact open-lots
    header or an open-lots-only near miss.

    A record it reads that is malformed (a quote left open to the end of the
    file, text after a closing quote, or a cell longer than the CSV reader
    allows) raises :class:`ParseError` under ``source``, at the zero-based line
    the record starts on (#1098): a file it can't read can't be ruled out as a
    lots export.
    """
    text = _without_header_decoration(_read_csv_text(path, section=source))
    records = (
        row
        for _, row in csv_records(
            io.StringIO(text), section=source, first_line=0, reexport=_REEXPORT
        )
    )
    if not _has_markup(text):
        first = next(records, None)
        return None if first is None else detect_fidelity_lots_kind(first)
    candidates = [kind for row in records if (kind := detect_fidelity_lots_kind(row)) is not None]
    return MarkupNearMiss(
        open_lots_only=bool(candidates)
        and all(
            kind is FidelityLotsKind.OPEN
            or (isinstance(kind, NearMissLotsHeader) and kind.open_lots_only)
            for kind in candidates
        )
    )


def read_open_lots(path: Path, *, source: str) -> tuple[EspLot, ...]:
    """Read + parse ``View open lots.csv`` from disk; ``source`` labels it."""
    return parse_open_lots(_read_csv_text(path, section=source), source=source)


def read_closed_lots(path: Path, *, source: str) -> tuple[EspClosedLot, ...]:
    """Read + parse ``View closed lots.csv`` from disk; ``source`` labels it."""
    return parse_closed_lots(_read_csv_text(path, section=source), source=source)


def _read_csv_text(path: Path, *, section: str) -> str:
    """Read ``path`` as UTF-8(-sig) text, mapping an unreadable / wrong-encoding
    file to a :class:`ParseError` (a controlled refusal) rather than letting a
    raw ``UnicodeDecodeError`` / ``OSError`` escape — mirrors the PDF reader's
    contract so a corrupt export refuses cleanly instead of crashing the CLI.

    The refusal names ``section`` and, for an ``OSError``, its errno name;
    never ``path``, the exception's text or its file name, and it chains no
    exception.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        message = "the file is not UTF-8 text"
    except OSError as exc:
        message = f"could not read the file ({errno_name(exc)})"
    # Refused outside the handler: a decode error holds the file's bytes.
    raise ParseError(section=section, row_index=0, column=None, message=message, raw_value="")
