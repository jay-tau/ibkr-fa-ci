"""Split an IBKR Flex Query CSV into named sections.

The Flex Query CSV interleaves multiple sections. Its only section
markers are optional header and trailer records (see below), so the
cue this module reads is that each section starts
with its own header row. We identify sections by matching the header
row's column tuple against a registry of known IBKR sections.

Why a registry of full column tuples (vs heuristics like "first column
is X"): IBKR's section headers all start with ``"CurrencyPrimary"``,
so first-column matching is ambiguous. The full column set, however,
is unique per section and stable across IBKR's published Flex Query
Reference. A new section type just gets a new :class:`KnownSection`
entry.

A header that matches no known section is skipped with its data rows:
a Flex Query may carry sections ibkr-fa doesn't read. Two kinds of
header are refused with :class:`~ibkr_fa.exceptions.ParseError`
instead, because dropping them could silently change a filed figure:

* **Near miss.** The header isn't a registered signature but holds
  more than half of the columns of a Trades or Open Positions
  signature, at least one of them a column only that section's
  signatures have (e.g. ``Proceeds`` for Trades, ``MarkPrice`` for
  Open Positions). That is how an added, removed or renamed column
  looks, in any column order. The error names the extra and missing
  columns against the nearest signature.
* **Repeated column.** The header repeats a column name and, with the
  repeats removed, matches a signature, is a near miss, or starts with
  ``ClientAccountID``.

Both errors name only columns from this module's signature tables and
count any other header cell, because a shifted or edited header can hold
the holder's name or account number.

The rule reads the whole column set, so a data row whose single cell
happens to equal a column name stays a data row.

Header and trailer records (#1313)
    With "Include header and trailer records" turned on, IBKR adds the
    records its Reporting Reference ("Delivery Configuration and General
    Configuration") lays out: a ``BOF`` row first (``BOF``, account ID,
    section count, report from date, report to date, generation date and
    time, two build numbers), an ``EOF`` row last (``EOF``, account ID),
    and around each section a ``BOS`` row (section code, ``BOS``,
    description) and an ``EOS`` row (section code, ``EOS``, two
    checksums). The page also names ``BOA`` and ``EOA`` rows without a
    layout. :func:`split_flex_query` reads the ``BOF`` row's from and to
    dates and nothing else from these records. A ``BOS`` or ``EOS`` row
    is one whose first cell is a section code the page lists and whose
    second cell is ``BOS`` or ``EOS``; the other records are known by
    their first cell. With a ``BOF`` record, descriptor-first ``BOS``/``EOS``
    rows and unread-section rows with either descriptor in their second
    cell refuse. Section headers are classified before the second-cell check.
    Each record ends the section before it. The
    records are optional, but a file that has them must meet these layout rules:
    exactly one ``BOF`` row, first, with 8 fields, ``YYYYMMDD`` dates
    and a from date no later than its to date; exactly one ``EOF`` row,
    last, with 2 fields; ``BOA`` and ``EOA`` rows only between them; and, outside a
    section, only a section header or one of these records. Anything
    else raises :class:`~ibkr_fa.exceptions.ParseError` with fixed text.

Usage::

    from pathlib import Path
    from ibkr_fa.parser import split_sections, KnownSection

    sections = split_sections(Path("flex.csv"))
    open_lots_rows = sections[KnownSection.OPEN_POSITIONS_LOT].rows
    # → list[dict[str, str]] — one dict per data row, keyed by column name.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ParseError, PathRefusalError
from ibkr_fa.invariants import require
from ibkr_fa.models.source import FlexPeriod
from ibkr_fa.parser._csv_records import csv_records, refuse_garbled_bom
from ibkr_fa.path_checks import exists


class KnownSection(StrEnum):
    """The Flex Query sections v1 cares about.

    Values are human-readable section names used in error messages and
    diagnostics (NOT IBKR-defined identifiers — IBKR's CSVs have no
    in-band section names; we name them ourselves for ergonomics).
    """

    OPEN_POSITIONS_LOT = "Open Positions (LOT)"
    TRADES = "Trades"
    ACCOUNT_INFORMATION = "Account Information"
    CASH_TRANSACTIONS = "Cash Transactions"


# Full column tuples for each known section, sourced from IBKR Flex
# Reference and verified against private real exports. Stored as
# frozensets so column ORDER doesn't have to match for identification
# (IBKR may reorder; data rows are still mapped by name, each zipped
# with the header row's columns).
#
# A single :class:`KnownSection` may have MULTIPLE signatures
# registered: IBKR's Flex Query configurator lets users add or remove
# columns within a section, producing different schemas for the same
# section type. Each variant we want to recognise gets its own
# frozenset entry pointing at the same enum value.
_SECTION_SIGNATURES: dict[frozenset[str], KnownSection] = {
    # Open Positions (LevelOfDetail = LOT) — verified against
    # private Flex Query exports with and without Closed Lots.
    frozenset(
        {
            "CurrencyPrimary",
            "FXRateToBase",
            "AssetClass",
            "SubCategory",
            "Symbol",
            "Description",
            "CUSIP",
            "ISIN",
            "ListingExchange",
            "IssuerCountryCode",
            "ReportDate",
            "Quantity",
            "MarkPrice",
            "PositionValue",
            "CostBasisPrice",
            "CostBasisMoney",
            "Side",
            "LevelOfDetail",
            "OpenDateTime",
            "HoldingPeriodDateTime",
        }
    ): KnownSection.OPEN_POSITIONS_LOT,
    # Trades, simple variant — 27 columns, no LOT-detail fields.
    # Emitted when the Flex Query has Closed Lots disabled. Verified
    # against a private Flex Query export.
    frozenset(
        {
            "CurrencyPrimary",
            "FXRateToBase",
            "AssetClass",
            "SubCategory",
            "Symbol",
            "Description",
            "CUSIP",
            "ISIN",
            "ListingExchange",
            "IssuerCountryCode",
            "ReportDate",
            "TradeDate",
            "SettleDateTarget",
            "TransactionType",
            "Quantity",
            "TradePrice",
            "TradeMoney",
            "Proceeds",
            "Taxes",
            "IBCommission",
            "IBCommissionCurrency",
            "NetCash",
            "ClosePrice",
            "Open/CloseIndicator",
            "CostBasis",
            "FifoPnlRealized",
            "Buy/Sell",
        }
    ): KnownSection.TRADES,
    # Trades, LOT-detail variant — 30 columns. Emitted when the Flex
    # Query has Closed Lots enabled: IBKR adds LevelOfDetail,
    # OpenDateTime, HoldingPeriodDateTime to the Trades schema and
    # interleaves CLOSED_LOT rows (one per FIFO-matched lot) with
    # EXECUTION rows (one per trade execution). See
    # :mod:`ibkr_fa.parser.closed_lots` for the row-level filter.
    # Verified against a private Flex Query export with Closed Lots.
    frozenset(
        {
            "CurrencyPrimary",
            "FXRateToBase",
            "AssetClass",
            "SubCategory",
            "Symbol",
            "Description",
            "CUSIP",
            "ISIN",
            "ListingExchange",
            "IssuerCountryCode",
            "ReportDate",
            "TradeDate",
            "SettleDateTarget",
            "TransactionType",
            "Quantity",
            "TradePrice",
            "TradeMoney",
            "Proceeds",
            "Taxes",
            "IBCommission",
            "IBCommissionCurrency",
            "NetCash",
            "ClosePrice",
            "Open/CloseIndicator",
            "CostBasis",
            "FifoPnlRealized",
            "Buy/Sell",
            "LevelOfDetail",
            "OpenDateTime",
            "HoldingPeriodDateTime",
        }
    ): KnownSection.TRADES,
}


# Columns only one section's signatures have, per section.
_SECTION_ONLY_COLUMNS: dict[KnownSection, frozenset[str]] = {
    kind: frozenset[str]().union(*(sig for sig, k in _SECTION_SIGNATURES.items() if k is kind))
    - frozenset[str]().union(*(sig for sig, k in _SECTION_SIGNATURES.items() if k is not kind))
    for kind in set(_SECTION_SIGNATURES.values())
}


# Some IBKR Flex sections (notably Account Information) have variable
# column composition — the user can enable/disable individual fields
# in their Flex Query config — so a single ``frozenset`` signature
# isn't usable. For those sections we identify by first-column hint.
# Each entry must be a column that ONLY appears as the first column of
# exactly one section type and is always emitted by IBKR (regardless
# of which optional fields the user has enabled). Keep this registry
# tiny and add an entry only when no signature-based identification is
# practical.
_KNOWN_SECTION_FIRST_COLUMNS: dict[str, KnownSection] = {
    "ClientAccountID": KnownSection.ACCOUNT_INFORMATION,
}

# Every column name the tables above hold: the only header cells a refusal names.
_KNOWN_COLUMNS: frozenset[str] = frozenset[str]().union(
    *_SECTION_SIGNATURES, _KNOWN_SECTION_FIRST_COLUMNS
)


SectionRows = list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class Section:
    """One identified section from a Flex Query CSV.

    Attributes:
        kind: The :class:`KnownSection` matched. An unrecognised
            header never becomes a :class:`Section`:
            :func:`split_sections` drops it and its rows.
        columns: The column tuple from the header row, in file order.
        rows: One dict per data row, keyed by column name (the row's
            cells zipped with :attr:`columns`).
    """

    kind: KnownSection
    columns: tuple[str, ...]
    rows: SectionRows


@dataclass(frozen=True, slots=True)
class FlexQuery:
    """A Flex Query CSV split by :func:`split_flex_query`.

    Attributes:
        sections: One :class:`Section` per recognised section, as
            :func:`split_sections` returns them.
        period: The ``BOF`` record's period, or ``None`` when the file
            has no header and trailer records.
    """

    sections: dict[KnownSection, Section]
    period: FlexPeriod | None


# The header and trailer record types IBKR's page names.
_BOF: Final = "BOF"
_EOF: Final = "EOF"
_BOA: Final = "BOA"
_EOA: Final = "EOA"
_BOS: Final = "BOS"
_EOS: Final = "EOS"
_BOF_FIELDS: Final = 8
_EOF_FIELDS: Final = 2
_BOF_FROM_FIELD: Final = 3
_BOF_TO_FIELD: Final = 4
_YYYYMMDD_LENGTH: Final = 8
# The section codes IBKR's page lists, the first cell of a BOS or EOS record.
_SECTION_CODES: Final = frozenset(
    {
        "ACCT",
        "EQUT",
        "EQUS",
        "FIFO",
        "MTMP",
        "PLUI",
        "CRTT",
        "CRTS",
        "CPOV",
        "POST",
        "POSS",
        "TRNT",
        "TRNS",
        "USTR",
        "UBTR",
        "ADJU",
        "PPPO",
        "CORP",
        "TRFR",
        "RATE",
        "CDIV",
        "ODIV",
        "CFCD",
        "TIER",
        "HTOB",
        "DVPT",
        "UNBC",
        "IBGN",
        "CTRN",
    }
)

# The fixed rules a header or trailer record can break.
_BOF_NOT_FIRST: Final = "the BOF record isn't the first record"
_BOF_REPEATED: Final = "there's more than one BOF record"
_EOF_NOT_LAST: Final = "the EOF record is missing or isn't the last record"
_BOF_FIELD_COUNT: Final = f"the BOF record doesn't have {_BOF_FIELDS} fields"
_BOF_DATE_FORMAT: Final = "a BOF date isn't YYYYMMDD"
_BOF_DATES_REVERSED: Final = "the BOF from date is after its to date"
_ACCOUNT_RECORD_OUTSIDE: Final = "a BOA or EOA record is outside its documented place"
_UNNAMED_RECORD: Final = "a record type IBKR's page doesn't name"


def _layout_error(line: int, rule: str) -> ParseError:
    """The refusal for a header or trailer record at ``line`` that breaks ``rule``."""
    return ParseError(
        section="<file>",
        row_index=line,
        column=None,
        message=(
            f"the Flex Query's header and trailer records don't match IBKR's "
            f"documented layout: {rule}. Re-export the Flex Query."
        ),
        raw_value="",
    )


def _record_type(row: list[str]) -> str | None:
    """The header or trailer record type ``row`` is, or ``None`` for any other row."""
    if row[0] in {_BOF, _EOF, _BOA, _EOA}:
        return row[0]
    if len(row) > 1 and row[0] in _SECTION_CODES and row[1] in {_BOS, _EOS}:
        return row[1]
    return None


def _yyyymmdd(cell: str) -> date | None:
    """``cell`` as a date when it is a real date written as 8 ASCII digits, else ``None``."""
    if len(cell) != _YYYYMMDD_LENGTH or not (cell.isascii() and cell.isdigit()):
        return None
    try:
        return date(int(cell[:4]), int(cell[4:6]), int(cell[6:]))
    except ValueError:
        # Returned, not raised: the refusal then chains nothing, and on
        # Python 3.14 this error names the date's parts.
        return None


def _bof_period(row: list[str], *, line: int) -> FlexPeriod:
    """Read the from and to dates of the ``BOF`` record ``row`` at ``line``."""
    if len(row) != _BOF_FIELDS:
        raise _layout_error(line, _BOF_FIELD_COUNT)
    start = _yyyymmdd(row[_BOF_FROM_FIELD])
    end = _yyyymmdd(row[_BOF_TO_FIELD])
    if start is None or end is None:
        raise _layout_error(line, _BOF_DATE_FORMAT)
    if start > end:
        raise _layout_error(line, _BOF_DATES_REVERSED)
    return FlexPeriod(start_date=start, end_date=end, line=line)


@dataclass(slots=True)
class _Records:
    """The header and trailer records :func:`split_flex_query` has read so far.

    Attributes:
        period: The ``BOF`` record's period, once it is read.
        eof_line: The line of the ``EOF`` record, once it is read.
        first: No record has been read yet.
    """

    period: FlexPeriod | None = None
    eof_line: int | None = None
    first: bool = True

    def read(self, row: list[str], *, line: int) -> bool:
        """Check the record ``row`` at ``line``; return whether it is a header or trailer record.

        Raises:
            ParseError: If ``row`` breaks a rule in the module docstring.
        """
        record = _record_type(row)
        first, self.first = self.first, False
        if record in {_BOA, _EOA}:
            if self.period is None or self.eof_line is not None:
                raise _layout_error(line, _ACCOUNT_RECORD_OUTSIDE)
        elif self.eof_line is not None:
            raise _layout_error(line, _EOF_NOT_LAST)
        elif record == _BOF:
            if self.period is not None:
                raise _layout_error(line, _BOF_REPEATED)
            if not first:
                raise _layout_error(line, _BOF_NOT_FIRST)
            self.period = _bof_period(row, line=line)
        elif record is not None and self.period is None:
            raise _layout_error(line, _BOF_NOT_FIRST)
        elif record == _EOF:
            if len(row) != _EOF_FIELDS:
                # Without the documented two-field row, a valid EOF is missing.
                raise _layout_error(line, _EOF_NOT_LAST)
            self.eof_line = line
        elif self.period is not None and row[0] in {_BOS, _EOS}:
            # A misplaced descriptor must not become data in an unread section.
            raise _layout_error(line, _UNNAMED_RECORD)
        return record is not None

    def finish(self, *, line: int) -> None:
        """Refuse a file with a ``BOF`` record and no ``EOF`` record, at its last ``line``."""
        if self.period is not None and self.eof_line is None:
            raise _layout_error(line, _EOF_NOT_LAST)


def _flex_csv_records(source: Iterable[str], records: _Records) -> Iterator[tuple[int, list[str]]]:
    """Stream CSV records, refusing undecodable text after BOF without its bytes."""
    try:
        yield from csv_records(
            source, section="<file>", first_line=0, reexport="re-export the Flex Query."
        )
    except UnicodeDecodeError:
        if records.period is None:
            raise
    else:
        return
    # Outside the handler: a decode error keeps the input bytes.
    raise ParseError(
        section="<file>",
        row_index=0,
        column=None,
        message="the file is not UTF-8 text",
        raw_value="",
    )


# Every IBKR Flex section's header row begins with this column. We use
# it as a fallback heuristic to identify candidate header rows for
# UNKNOWN section types (whose column-set we haven't catalogued yet).
# For KNOWN section types we identify by column-set match instead, so
# header recognition is order-independent for the sections that
# matter — see _classify_row.
_HEADER_FIRST_COLUMN_HINT = "CurrencyPrimary"


def _first_column_hint(row: list[str]) -> KnownSection | None:
    """Return the :class:`KnownSection` matched by ``row[0]``, if any.

    Used as a fallback for sections with variable column composition
    (e.g., Account Information) where signature-based identification
    isn't practical. Returns :data:`None` if ``row[0]`` doesn't
    identify any known section. ``row`` must be non-empty (callers in
    this module guard against empty rows before invoking).
    """
    return _KNOWN_SECTION_FIRST_COLUMNS.get(row[0])


def _describe_columns(columns: list[str]) -> str:
    """Describe header ``columns`` for a refusal without echoing an unknown cell.

    Names the columns in :data:`_KNOWN_COLUMNS` and counts the rest, e.g.
    ``"column(s) ['Symbol'] and 1 other column(s) not in any known section"``.
    """
    known = [column for column in columns if column in _KNOWN_COLUMNS]
    others = len(columns) - len(known)
    if not others:
        return f"column(s) {known!r}"
    if not known:
        return f"{others} column(s) not in any known section"
    return f"column(s) {known!r} and {others} other column(s) not in any known section"


def _resembled_signature(columns: frozenset[str]) -> frozenset[str] | None:
    """Return the registered signature ``columns`` matches or nearly matches.

    That is ``columns`` itself when it is a signature. Otherwise it is the
    nearest signature (fewest extra plus missing columns, the first
    registered on a tie) of which ``columns`` is a near miss: ``columns``
    holds more than half of the signature's columns, at least one of them
    in the signature's section's :data:`_SECTION_ONLY_COLUMNS`. Returns
    :data:`None` when no signature qualifies.
    """
    if columns in _SECTION_SIGNATURES:
        return columns
    near_misses = [
        signature
        for signature, kind in _SECTION_SIGNATURES.items()
        if 2 * len(columns & signature) > len(signature) and columns & _SECTION_ONLY_COLUMNS[kind]
    ]
    return min(near_misses, key=lambda signature: len(columns ^ signature), default=None)


def _classify_row(row: list[str], *, file_row_index: int) -> tuple[bool, KnownSection | None]:
    """Decide if ``row`` is a section header, and which (if any) known section.

    Returns ``(is_header, kind)``:
      - If the row's column set matches a known section signature AND
        every column name in the row is unique, returns
        ``(True, KnownSection.<X>)``. Order-independent.
      - Else if the row's column set is a near miss of a signature (see
        the module docstring), raises :class:`ParseError` naming the
        section it resembles and its extra and missing columns against
        the nearest signature (an extra column not in
        :data:`_KNOWN_COLUMNS` is counted, not named).
      - Else if ``row[0]`` matches an entry in
        :data:`_KNOWN_SECTION_FIRST_COLUMNS`, returns ``(True, that_kind)``.
        Used for sections with variable column composition.
      - Else if the row's first cell is the well-known IBKR header
        marker ``"CurrencyPrimary"``, treats it as an unknown-section
        header: ``(True, None)``. The splitter drops the section's data
        rows.
      - Otherwise, ``(False, None)`` — treat as a data row.

    A row that repeats a column name raises :class:`ParseError` naming
    the repeat (counting it, if it isn't in :data:`_KNOWN_COLUMNS`) when,
    with the repeats removed, it matches a signature or is a near miss of
    one, or when its first column is in
    :data:`_KNOWN_SECTION_FIRST_COLUMNS`: ``dict(zip(...))`` in the
    data-row path would silently overwrite the earlier column's value.
    Any other row with a repeat follows the last two rules above.

    ``file_row_index`` is the zero-based line the row starts on, which a
    refusal reports under the ``"<file>"`` section.
    """
    if not row:
        return False, None
    columns = frozenset(row)
    resembled = _resembled_signature(columns)
    kind = _SECTION_SIGNATURES[resembled] if resembled is not None else _first_column_hint(row)
    if kind is not None and len(columns) != len(row):
        repeated = [column for column, count in Counter(row).items() if count > 1]
        raise ParseError(
            section="<file>",
            row_index=file_row_index,
            column=None,
            message=(
                f"header row looks like the {kind.value!r} section but repeats "
                f"{_describe_columns(repeated)}; re-export the Flex Query with each column once"
            ),
            raw_value="",
        )
    if resembled is not None and resembled != columns:
        extra = [column for column in row if column not in resembled]
        missing = sorted(resembled - columns)
        raise ParseError(
            section="<file>",
            row_index=file_row_index,
            column=None,
            message=(
                f"header row looks like a changed {_SECTION_SIGNATURES[resembled].value!r} "
                f"section header but matches none of its supported column sets; against "
                f"the nearest one it has extra {_describe_columns(extra)} and lacks "
                f"column(s) {missing!r}. Fix those columns in the Flex Query, or, if this is a "
                "different section, remove it from the query or report it"
            ),
            raw_value="",
        )
    if kind is not None:
        return True, kind
    return (True, None) if row[0] == _HEADER_FIRST_COLUMN_HINT else (False, None)


def split_sections(path: Path) -> dict[KnownSection, Section]:
    """Read ``path`` and return one :class:`Section` per recognised section.

    The sections of :func:`split_flex_query`, without the period; it
    raises the same errors.
    """
    return split_flex_query(path).sections


def split_flex_query(path: Path) -> FlexQuery:
    """Read ``path`` into its recognised sections and its ``BOF`` period.

    The sections are keyed by :class:`KnownSection`
    so callers can look up sections by the typed identifier rather than a string.
    A section whose header matches no known section is skipped with its
    data rows — a Flex Query can carry sections ibkr-fa doesn't read —
    unless the header is a near miss of a known signature or repeats a
    column (see the module docstring), which raises
    :class:`~ibkr_fa.exceptions.ParseError`.

    A section that appears MORE THAN ONCE in the same file (rare but
    possible if IBKR splits a long section by date range) raises
    :class:`~ibkr_fa.exceptions.ParseError` — we don't have a defined
    semantic for merging.

    The header and trailer records are optional here (see the module
    docstring): a file without them has no period.

    Args:
        path: Filesystem path to the Flex Query CSV.

    Returns:
        A :class:`FlexQuery` with ``{KnownSection: Section}`` for every
        recognised section in the file, and the ``BOF`` record's period
        or ``None``. Missing-section detection (e.g., the caller wanted
        ``Open Positions`` and it isn't here) is the caller's
        responsibility — v1 callers already know which sections they
        require.

    Raises:
        PathRefusalError: If ``path`` does not exist.
        ParseError: If a recognised section appears more than once; if
            a header is a near miss of a known signature or repeats a
            column (see the module docstring); if a header or trailer
            record breaks a rule in the module docstring; if text read after
            ``BOF`` is not UTF-8; or if a CSV record is
            malformed: a quote is left open to the end of the file, text
            follows a closing quote, or a cell is longer than the CSV
            reader allows (#1098); or if the record on the file's first
            line starts with a garbled byte-order mark (#1195). No refusal
            carries a cell value, and a
            header refusal names only known column names. A refusal under
            the ``"<file>"`` section, such as a malformed record, reports
            the zero-based line the record starts on; one under a section
            name reports a row index within that section.
    """
    if not exists(path):
        raise PathRefusalError("Flex Query CSV not found", path)

    found: dict[KnownSection, Section] = {}
    current_kind: KnownSection | None = None
    current_columns: tuple[str, ...] = ()
    current_rows: SectionRows = []
    section_index = -1  # 0-based index of the section currently being read
    file_row_index = 0  # 0-based line the current record starts on
    section_row_index = -1  # 0-based index within the current section's data rows
    records = _Records()
    in_section = False  # a header row has been read since the last record

    def _flush() -> None:
        nonlocal current_kind, current_columns, current_rows, section_row_index
        if current_kind is not None and current_columns:
            if current_kind in found:
                # Duplicate-section diagnostic: the offending position is
                # the SECOND header occurrence, which sits one past the
                # last data row of this section. Clamp the negative
                # 'no rows yet' sentinel to 0 so the row_index >= 0
                # contract holds.
                raise ParseError(
                    section=current_kind.value,
                    row_index=max(section_row_index + 1, 0),
                    column=None,
                    message=(
                        f"section {current_kind.value!r} appears more than "
                        f"once in the file; merging is not defined"
                    ),
                    raw_value="",
                )
            found[current_kind] = Section(
                kind=current_kind,
                columns=current_columns,
                rows=current_rows,
            )
        # Reset for the next section.
        current_kind = None
        current_columns = ()
        current_rows = []
        section_row_index = -1

    # ``utf-8-sig`` transparently strips a BOM if present (no-op otherwise);
    # IBKR Flex CSVs are mostly emitted as UTF-8 without BOM, but desktop
    # export/edit flows often add one.
    with path.open(newline="", encoding="utf-8-sig") as f:
        for file_row_index, row in _flex_csv_records(f, records):
            refuse_garbled_bom(file_row_index, row, first_line=0)
            if records.read(row, line=file_row_index):
                # Ends the section: a data row now refuses. A header flushes it.
                in_section = False
                continue

            is_header, header_kind = _classify_row(row, file_row_index=file_row_index)
            if is_header:
                _flush()
                section_index += 1
                current_columns = tuple(row)
                current_kind = header_kind
                in_section = True
                # Unknown sections (header_kind is None): fall through;
                # rows are dropped because current_kind stays None and
                # _flush() is a no-op.
                continue

            if records.period is not None and (
                not in_section or (current_kind is None and len(row) > 1 and row[1] in {_BOS, _EOS})
            ):
                # Neither an outside row nor an unlisted section descriptor is data.
                raise _layout_error(file_row_index, _UNNAMED_RECORD)

            if current_kind is None:
                # Either we're before any header (malformed file) or we're
                # inside an unknown section. Either way, drop the row.
                # If we're before any header at all, that's a hard error.
                if section_index < 0:
                    raise ParseError(
                        section="<file>",
                        row_index=file_row_index,
                        column=None,
                        message=(
                            "first non-blank row is not a known section "
                            f"header (expected first column "
                            f"{_HEADER_FIRST_COLUMN_HINT!r} or a row "
                            "matching a known section signature)"
                        ),
                        raw_value="",
                    )
                continue

            section_row_index += 1
            if len(row) != len(current_columns):
                raise ParseError(
                    section=current_kind.value,
                    row_index=section_row_index,
                    column=None,
                    message=(
                        f"row has {len(row)} field(s); section header "
                        f"declared {len(current_columns)}"
                    ),
                    raw_value="",
                )
            current_rows.append(dict(zip(current_columns, row, strict=True)))

        _flush()

    records.finish(line=file_row_index)

    # Postconditions (PR #124): every kept section is well-formed.
    # Free given the construction above and _classify_row's
    # duplicate-column rejection, but the assertions document the
    # contract and catch any future refactor that weakens those
    # checks (e.g., by short-circuiting the duplicate-check path).
    for kind, section in found.items():
        require(
            len(section.columns) > 0,
            what="split_sections post: every section's columns tuple must be non-empty",
            kind=kind.value,
        )
        require(
            len(set(section.columns)) == len(section.columns),
            what="split_sections post: every section's column names must be unique",
            kind=kind.value,
            columns=section.columns,
        )

    return FlexQuery(sections=found, period=records.period)
