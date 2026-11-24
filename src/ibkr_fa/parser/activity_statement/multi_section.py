"""IBKR Activity Statement section splitter.

The Activity Statement CSV format is structurally different from the
Flex Query format that :mod:`ibkr_fa.parser.multi_section` handles:

* **Section-tagged, not column-tabular.** Every row's first cell is
  the section name (``"Trades"``, ``"Open Positions"``, etc.) and
  the second cell is a **discriminator** (``"Header"``, ``"Data"``,
  ``"SubTotal"``, ``"Total"``).
* **Header repeats inside a section.** Some sections (notably
  ``"Net Asset Value"``) emit a new header row mid-section to start
  a different sub-table. We model each header-led group as its own
  :class:`Section` block so the downstream consumer always sees a
  consistent ``(columns, rows)`` pair.
* **Subtotal/Total rows mixed with Data rows.** ``Trades`` for
  example has one ``SubTotal`` row per symbol and one final
  ``Total`` row. ``SubTotal``/``Total`` rows have different column
  populations from ``Data`` rows and would corrupt any aggregation if
  blindly included, so they never enter a block's rows. The splitter
  checks Trades' against the block's ``Data`` rows when the block ends
  and refuses a Trades block without them
  (:mod:`~ibkr_fa.parser.activity_statement._trades_totals`, #1090). It
  drops them only in the sections :data:`_AGGREGATION_DROPPED_SECTIONS`
  lists (Open Positions), with the reason. Every other parsed section
  refuses such a row (#1012). The Dividends, Withholding Tax, Interest
  and Realized & Unrealized Performance Summary sections print their
  totals as ``Data`` rows, which their parsers check (#908). The other
  sections define no such rows, so a dropped one could hide a row
  their parsers would refuse, such as a second account number or NAV
  total.

Every section name is classified in one place, here (#913):

* **parsed**: the sections in :data:`_SECTION_SHAPES`, each with
  the header shapes it accepts. A header column that its section
  neither reads nor declares ignored, with the reason it can't change
  a filed figure, refuses. So does a row type other than ``Header`` or
  ``Data``, except a ``SubTotal`` or ``Total`` row in Trades, which is
  checked, or in a section :data:`_AGGREGATION_DROPPED_SECTIONS`
  lists, which is dropped.
* **ignored**: :data:`_IGNORED_SECTIONS`, each with the reason it can't
  change a filed figure. Their rows aren't read.
* **refused**: :data:`_REFUSE_SECTIONS`, which refuse any non-header row.

A section on none of these lists refuses, since nothing says whether it
changes a filed figure.

This module is the equivalent of
:mod:`ibkr_fa.parser.multi_section` for the Activity Statement
format.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ParseError, PathRefusalError, ReplacedCrashError, crash_site
from ibkr_fa.invariants import require
from ibkr_fa.models.source import DividendEvidence
from ibkr_fa.parser._csv_records import csv_records, refuse_garbled_bom
from ibkr_fa.parser.activity_statement._trades_totals import AggregationRow, check_trades_totals
from ibkr_fa.path_checks import exists


class KnownActivitySection(StrEnum):
    """The Activity Statement sections the splitter returns blocks for.

    Values are the **literal section name** as IBKR emits it in
    column 1 of every row belonging to that section — these must
    match the upstream format exactly (no normalisation).

    Every member but one is parsed, with the header shapes in
    :data:`_SECTION_SHAPES`. ``Change in Dividend Accruals`` is
    ignored (:data:`_IGNORED_SECTIONS`) and kept here only so
    :func:`dividend_evidence` can see it: when present it maps to an
    empty block list, because its rows aren't read. Sections IBKR emits
    that aren't members are classified in :data:`_IGNORED_SECTIONS` or
    :data:`_REFUSE_SECTIONS`; any other section refuses (#913). To parse
    a new section, add a member, its shapes and its parser.
    """

    STATEMENT = "Statement"
    ACCOUNT_INFORMATION = "Account Information"
    NET_ASSET_VALUE = "Net Asset Value"
    CHANGE_IN_NAV = "Change in NAV"
    OPEN_POSITIONS = "Open Positions"
    TRADES = "Trades"
    DIVIDENDS = "Dividends"
    WITHHOLDING_TAX = "Withholding Tax"
    INTEREST = "Interest"
    CHANGE_IN_DIVIDEND_ACCRUALS = "Change in Dividend Accruals"
    FINANCIAL_INSTRUMENT_INFORMATION = "Financial Instrument Information"
    REALIZED_UNREALIZED_PERFORMANCE_SUMMARY = "Realized & Unrealized Performance Summary"


_HEADER_DISCRIMINATOR: Final[str] = "Header"
_DATA_DISCRIMINATOR: Final[str] = "Data"
# IBKR emits aggregation rows interleaved with data rows in some
# sections (notably Trades). They share the section name but have a
# different column population, so they are never kept as data rows.
# Trades' are checked against its data rows when the block ends (#1090).
_AGGREGATION_DISCRIMINATORS: Final[frozenset[str]] = frozenset({"SubTotal", "Total"})
# The only sections whose aggregation rows are dropped, each with the
# reason dropping them can't change a filed figure. Trades checks its
# own (#1090); every other parsed section refuses them (#1012), so a
# section parsed later refuses them until it is listed here.
_AGGREGATION_DROPPED_SECTIONS: Final[Mapping[KnownActivitySection, str]] = {
    KnownActivitySection.OPEN_POSITIONS: (
        "IBKR's totals of the positions; every position is read from its Data row"
    ),
}
# Of the sections that refuse aggregation rows, these print their
# totals as ``Data`` rows, which their parsers check (#908, #913), and
# their refusal says so.
_DATA_TOTAL_SECTIONS: Final[frozenset[KnownActivitySection]] = frozenset(
    {
        KnownActivitySection.DIVIDENDS,
        KnownActivitySection.WITHHOLDING_TAX,
        KnownActivitySection.INTEREST,
        KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY,
    }
)
# Sections we recognise well enough to KNOW we can't yet parse them
# safely. IBKR's ``Corporate Actions`` section records splits / mergers
# / spin-offs / symbol changes that silently reshape lot quantity and
# cost basis; dropping it (as we do for the ignored sections) could
# file a wrong Schedule FA figure. Refuse fail-closed when one carries
# any non-header row. Matches IBKR's English section name (the whole
# parser is English-only; a localised statement fails to match every
# listed section, so it refuses as unlisted).
# ponytail: presence-refuse only; parsing the reshapes is #106/#60.
_REFUSE_SECTIONS: Final[frozenset[str]] = frozenset({"Corporate Actions"})

# The end of every refusal of a section, row type or column the
# classification doesn't list, or of a row kind a parser doesn't read
# (#1093).
FILE_AN_ISSUE: Final[str] = (
    "If IBKR now emits this, please file an issue at "
    "https://github.com/jay-tau/ibkr-fa/issues naming it."
)
_UNEXPECTED: Final[str] = f"Refusing rather than dropping it. {FILE_AN_ISSUE}"

# IBKR's custom statement setting that adds the details below (#1195).
_HIDE_DETAILS: Final[str] = (
    "'Hide Details for Positions, Trades and Client Fees Sections?' set to Yes."
)
# The Open Positions column IBKR adds when details are shown: each lot's open
# date and time (#1195).
_OPEN_POSITIONS_DETAIL_COLUMN: Final[str] = "Open"
# The Trades row kinds IBKR adds when details are shown: one row per fill and
# one per lot a sale closed (#1195).
_TRADES_DETAIL_KINDS: Final[frozenset[str]] = frozenset({"Trade", "ClosedLot", "ClosedLot*"})

# Sections whose rows are never read, each with the reason it can't
# change a filed figure. The reason must hold for every row type in the
# section, since none of its rows is looked at.
_IGNORED_SECTIONS: Final[Mapping[str, str]] = {
    "Codes": "a key to the code letters; each row carries its own codes",
    "Notes/Legal Notes": "legal text",
    "Notes": "free text",
    "Cash Report": "a per-currency summary of cash movements itemised in other sections",
    "Deposits & Withdrawals": (
        "money moved in or out of the account, which isn't income, cost or proceeds"
    ),
    "Change in Dividend Accruals": (
        "dividends not yet paid; filed dividends are the Dividends section's paid "
        "rows. Its presence still counts as dividend evidence"
    ),
    "Interest Accruals": "interest not yet paid; filed interest counts only paid credits",
    "Fees": (
        "account-level fees aren't income, aren't part of any lot's cost or proceeds "
        "(trade commissions are in Trades), and aren't deductible against the filed "
        "figures, whatever the row type"
    ),
    "Mark-to-Market Performance Summary": (
        "IBKR's own profit-and-loss summary; filed gains are computed from Trades"
    ),
    "Total P/L for Statement Period": (
        "IBKR's own profit-and-loss total; filed gains are computed from Trades"
    ),
}
# Ignored sections IBKR prints without a ``Header`` row. Only
# these skip the header-first check; every other ignored section's
# rows must follow its header.
_HEADERLESS_SECTIONS: Final[frozenset[str]] = frozenset({"Notes", "Total P/L for Statement Period"})


@dataclass(frozen=True, slots=True)
class _BlockShape:
    """One header shape a parsed section accepts (#913).

    Attributes:
        read: The columns the section's parser reads, or, for Trades,
            the splitter's check of its totals (#1090).
        ignored: Each column the parser doesn't read, with the reason
            it can't change a filed value or how a row is classified.
    """

    read: frozenset[str]
    ignored: Mapping[str, str]

    @property
    def columns(self) -> frozenset[str]:
        """Every column this shape accepts."""
        return self.read | self.ignored.keys()


_FIELD_SHAPE: Final[_BlockShape] = _BlockShape(
    read=frozenset({"Field Name", "Field Value"}), ignored={}
)
_INCOME_COLUMNS: Final[frozenset[str]] = frozenset({"Currency", "Date", "Description", "Amount"})
_PNL_SUMMARY_REASON: Final[str] = (
    "IBKR's own gain or loss figure; filed gains are computed from Trades"
)
# The header shapes each parsed section accepts. A header matches a
# shape when every one of its columns is in the shape; a column the
# shape lists can be absent (e.g. Financial Instrument Information's
# ``ISIN``), and the parsers refuse a row missing a column they need.
_SECTION_SHAPES: Final[Mapping[KnownActivitySection, tuple[_BlockShape, ...]]] = {
    KnownActivitySection.STATEMENT: (_FIELD_SHAPE,),
    KnownActivitySection.ACCOUNT_INFORMATION: (_FIELD_SHAPE,),
    KnownActivitySection.CHANGE_IN_NAV: (_FIELD_SHAPE,),
    KnownActivitySection.NET_ASSET_VALUE: (
        _BlockShape(
            read=frozenset({"Asset Class", "Current Total"}),
            ignored={
                "Prior Total": "the prior statement's figure; Table A2 files this one's",
                "Current Long": "the long part of Current Total, which is read",
                "Current Short": "the short part of Current Total, which is read",
                "Change": "Current Total less Prior Total",
            },
        ),
        _BlockShape(
            read=frozenset(),
            ignored={"Time Weighted Rate of Return": "a return percentage, which nothing files"},
        ),
    ),
    KnownActivitySection.OPEN_POSITIONS: (
        _BlockShape(
            # Each Summary row's Value is read and checked against its
            # Quantity times Close Price, to the cent (#1315).
            read=frozenset(
                {
                    "DataDiscriminator",
                    "Asset Category",
                    "Currency",
                    "Symbol",
                    "Quantity",
                    "Mult",
                    "Close Price",
                    "Value",
                    "Code",
                }
            ),
            ignored={
                "Cost Price": "IBKR's per-share cost; filed cost comes from Trades' Basis",
                "Cost Basis": (
                    "IBKR's own cost; filed cost comes from Trades' Basis, and Quantity "
                    "is reconciled against the lots built from Trades"
                ),
                "Unrealized P/L": "an unrealized gain, which nothing files",
            },
        ),
    ),
    KnownActivitySection.TRADES: (
        _BlockShape(
            read=frozenset(
                {
                    "DataDiscriminator",
                    "Asset Category",
                    "Currency",
                    "Symbol",
                    "Date/Time",
                    "Quantity",
                    "T. Price",
                    "Proceeds",
                    "Comm/Fee",
                    "Basis",
                    "Realized P/L",
                    "MTM P/L",
                    "Code",
                }
            ),
            ignored={
                "C. Price": (
                    "the trade day's closing price; closing values use Open Positions' Close Price"
                ),
            },
        ),
    ),
    KnownActivitySection.FINANCIAL_INSTRUMENT_INFORMATION: (
        _BlockShape(
            read=frozenset(
                {
                    "Asset Category",
                    "Symbol",
                    "Description",
                    "Security ID",
                    "Underlying",
                    "ISIN",
                    "Listing Exch",
                    "Multiplier",
                    "Code",
                }
            ),
            ignored={
                "Conid": "IBKR's internal ID; instruments are keyed by Symbol and Security ID",
                "Type": (
                    "IBKR's undocumented instrument label, which no IBKR route reads (ADR 0018)"
                ),
            },
        ),
    ),
    KnownActivitySection.DIVIDENDS: (_BlockShape(read=_INCOME_COLUMNS, ignored={}),),
    KnownActivitySection.WITHHOLDING_TAX: (
        _BlockShape(read=_INCOME_COLUMNS | {"Code"}, ignored={}),
    ),
    KnownActivitySection.INTEREST: (_BlockShape(read=_INCOME_COLUMNS, ignored={}),),
    KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY: (
        _BlockShape(
            read=frozenset({"Cost Adj."}),
            ignored={
                "Asset Category": "labels the summary row; every row's Cost Adj. is checked",
                "Symbol": "names the summary row; every row's Cost Adj. is checked",
                **dict.fromkeys(
                    (
                        "Realized S/T Profit",
                        "Realized S/T Loss",
                        "Realized L/T Profit",
                        "Realized L/T Loss",
                        "Realized Total",
                        "Unrealized S/T Profit",
                        "Unrealized S/T Loss",
                        "Unrealized L/T Profit",
                        "Unrealized L/T Loss",
                        "Unrealized Total",
                        "Total",
                    ),
                    _PNL_SUMMARY_REASON,
                ),
                "Code": "codes on IBKR's summary rows; Trades' own codes are checked",
            },
        ),
    ),
}

SectionRows = list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class Section:
    """One header-led block from an Activity Statement section.

    A single section name (e.g., ``"Net Asset Value"``) may appear
    as multiple :class:`Section` blocks if IBKR emits multiple
    header rows under that name — each header-led group becomes its
    own block. Downstream consumers typically pick the block whose
    columns match their schema; see the per-section parsers.

    Attributes:
        kind: Which :class:`KnownActivitySection` this block belongs
            to.
        columns: The column tuple from the block's header row, in
            file order.
        header_line: The 1-based file line of the block's header row.
        rows: One dict per ``Data`` row, keyed by the column tuple
            above. ``SubTotal``/``Total`` rows never appear: Trades'
            are checked against these rows when the block ends (#1090),
            those of the sections ``_AGGREGATION_DROPPED_SECTIONS``
            lists are dropped, and every other section refuses them.
        row_lines: The 1-based file line each of :attr:`rows` starts
            on, in the same order.
    """

    kind: KnownActivitySection
    columns: tuple[str, ...]
    header_line: int
    rows: SectionRows = field(default_factory=list)
    row_lines: list[int] = field(default_factory=list)


def _iter_csv_rows(path: Path) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(line_number, row)`` for every non-empty row.

    ``line_number`` is the 1-based line of the file on which the row
    starts, to match what users see in their text editor when reading
    :class:`ParseError` messages. A quoted cell can hold line breaks, so
    one row can span several lines; counting rows would then fall short.

    ``encoding="utf-8-sig"`` strips a BOM if present (IBKR's CSV
    occasionally ships with one; see the equivalent comment in
    :mod:`ibkr_fa.parser.multi_section`).

    A malformed record refuses under the ``"<file>"`` section at the line it
    starts on (see :func:`~ibkr_fa.parser._csv_records.csv_records`), and a
    garbled one on line 1 at row 1
    (:func:`~ibkr_fa.parser._csv_records.refuse_garbled_bom`, #1195).
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        for line_no, row in csv_records(
            f, section="<file>", first_line=1, reexport="re-export the Activity Statement."
        ):
            refuse_garbled_bom(line_no, row, first_line=1)
            yield line_no, row


def split_activity_statement_sections(path: Path) -> dict[KnownActivitySection, list[Section]]:  # noqa: PLR0915
    """Split an IBKR Activity Statement CSV into header-led blocks.

    Returns a mapping from each parsed
    :class:`KnownActivitySection` to a **list** of :class:`Section`
    blocks. The list is in file order; for sections that emit only
    one header (the common case) it has length 1, and for sections
    like ``Net Asset Value`` that re-emit headers mid-section it
    has length 2+.

    Every section is classified (#913). An ignored section
    (:data:`_IGNORED_SECTIONS`, e.g. ``Codes``) is dropped without
    reading its rows; ``Change in Dividend Accruals`` then maps to an
    empty list, so :func:`dividend_evidence` still sees it. A
    ``_REFUSE_SECTIONS`` section (``Corporate Actions``) carrying any
    non-header row raises: v1 can't parse the lot reshaping it implies,
    so rather than drop it (and risk filing a wrong Schedule FA figure)
    the splitter refuses. That check runs before the enum lookup, so it
    stays fail-closed even if the section is later promoted into the
    enum. A section on no list raises too. A Trades block's
    ``SubTotal`` and ``Total`` rows are checked against its ``Data``
    rows when the block ends, and never enter its rows (#1090). Other
    parsed sections' are dropped only in the sections
    ``_AGGREGATION_DROPPED_SECTIONS`` lists (Open Positions); every
    other parsed section refuses them (#1012).

    Args:
        path: Filesystem path to the Activity Statement CSV.

    Returns:
        ``{KnownActivitySection: [Section, ...]}`` for every
        section present, in file order. Missing-section detection
        (e.g., "the user's statement has no Trades section because
        they didn't trade this year") is the caller's job —
        legitimately-empty sections aren't an error here.

    Raises:
        PathRefusalError: If ``path`` doesn't exist.
        ParseError: If a section is on none of the lists; if a parsed
            or ignored section emits a non-header row before its
            ``Header`` row (except the headerless ignored sections in
            ``_HEADERLESS_SECTIONS``); if a parsed section emits a row
            type other than ``Header`` / ``Data`` / ``SubTotal`` /
            ``Total``; if a parsed section's ``Header`` row declares no
            column names once trailing empty cells are stripped, or one
            of those names is empty, only whitespace or repeated, or one
            that no shape in ``_SECTION_SHAPES`` accepts; if a parsed
            section's row has fewer cells than its header declared
            columns, or a populated cell past them; if a
            ``_REFUSE_SECTIONS`` section (``Corporate Actions``)
            carries any non-header row; if a parsed section that
            ``_AGGREGATION_DROPPED_SECTIONS`` doesn't list, other than
            Trades, carries a ``SubTotal`` / ``Total`` row; if a Trades
            block's ``SubTotal`` and ``Total`` rows don't match its
            ``Data`` rows, or a block with rows lacks them
            (:func:`~ibkr_fa.parser.activity_statement._trades_totals.check_trades_totals`);
            if the statement was exported with details shown: an Open
            Positions header has an ``Open`` column, or a Trades ``Data``
            row's ``DataDiscriminator`` is ``Trade``, ``ClosedLot`` or
            ``ClosedLot*``, which refuses before the block's totals are
            checked (#1195); if the record on line 1 starts with a garbled
            byte-order mark (#1195); or if a CSV record is malformed:
            a quote is left open to the end of the file, text follows a
            closing quote, or a cell is longer than the CSV reader allows
            (#1098). No ``ParseError`` carries a
            cell value, in its fields or message: a shifted row can put
            the holder's name or account number in any cell. An unknown
            section, a malformed record and a garbled byte-order mark
            report under the ``"<file>"``
            section, and header columns are named by position. Every
            ``row_index`` is the 1-based line the row starts on.
    """
    if not exists(path):
        raise PathRefusalError("Activity Statement CSV not found", path)

    sections: dict[KnownActivitySection, list[Section]] = {}
    current_kind: KnownActivitySection | None = None
    current_columns: tuple[str, ...] = ()
    current_header_line = 0
    current_rows: SectionRows = []
    current_row_lines: list[int] = []
    # The Trades block's SubTotal and Total rows, checked when it ends (#1090).
    current_aggregates: list[AggregationRow] = []
    # The ignored section whose header (or headerless row) came last.
    open_ignored: str | None = None

    def _flush() -> None:
        nonlocal current_kind, current_columns, current_header_line
        nonlocal current_rows, current_row_lines, current_aggregates
        if current_kind is not None:
            block = Section(
                kind=current_kind,
                columns=current_columns,
                header_line=current_header_line,
                rows=current_rows,
                row_lines=current_row_lines,
            )
            if current_kind is KnownActivitySection.TRADES:
                check_trades_totals(block, current_aggregates)
            sections.setdefault(current_kind, []).append(block)
        current_kind = None
        current_columns = ()
        current_header_line = 0
        current_rows = []
        current_row_lines = []
        current_aggregates = []

    for line_no, row in _iter_csv_rows(path):
        section_name = row[0]
        discriminator = row[1] if len(row) > 1 else ""

        if section_name in _REFUSE_SECTIONS or section_name in _IGNORED_SECTIONS:
            _flush()
            open_ignored = _skip_unread_row(section_name, discriminator, line_no, open_ignored)
            if section_name == KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS:
                sections.setdefault(KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS, [])
            continue
        open_ignored = None

        kind = _parsed_section(section_name, line_no)
        if discriminator == _HEADER_DISCRIMINATOR:
            _flush()
            current_kind = kind
            current_header_line = line_no
            # Trailing empty cells in the header (IBKR's CSV is
            # padded to a fixed column count) become empty strings
            # in the column tuple; strip them so dict keys don't
            # contain literal "".
            current_columns = tuple(_strip_trailing_empty(row[2:]))
            _validate_header_has_columns(current_columns, kind, line_no)
            _validate_header_names_not_blank(current_columns, kind, line_no)
            _validate_header_columns_unique(current_columns, kind, line_no)
            _refuse_open_positions_details(current_columns, kind, line_no)
            _validate_header_columns_declared(current_columns, kind, line_no)
        elif discriminator == _DATA_DISCRIMINATOR:
            _ensure_header_seen(current_kind, kind, line_no)
            cells = _row_cells(row=row, columns=current_columns, section=kind, line_no=line_no)
            if kind is KnownActivitySection.TRADES:
                refuse_trades_detail_row(cells, row_index=line_no)
            current_rows.append(cells)
            current_row_lines.append(line_no)
        elif kind is KnownActivitySection.TRADES and discriminator in _AGGREGATION_DISCRIMINATORS:
            _ensure_header_seen(current_kind, kind, line_no)
            cells = _row_cells(
                row=row,
                columns=current_columns,
                section=kind,
                line_no=line_no,
                what=f"{discriminator!r} row",
            )
            current_aggregates.append(AggregationRow(line=line_no, kind=discriminator, cells=cells))
        else:
            _check_dropped_row(current_kind, kind, discriminator, line_no)

    _flush()

    # Postconditions (PR #124): every kept section block is well-
    # formed. The _validate_header_* checks already raise at
    # header parse time, but the post-loop assertion locks the
    # contract for downstream consumers and catches any future
    # refactor that re-routes around the inline checks.
    for kind, section_list in sections.items():
        for section in section_list:
            require(
                len(section.columns) > 0,
                what=(
                    "split_activity_statement_sections post: every section "
                    "block's columns tuple must be non-empty"
                ),
                kind=kind.value,
            )
            require(
                len(set(section.columns)) == len(section.columns),
                what=(
                    "split_activity_statement_sections post: every section "
                    "block's column names must be unique"
                ),
                kind=kind.value,
                columns=section.columns,
            )
            require(
                len(section.row_lines) == len(section.rows),
                what=(
                    "split_activity_statement_sections post: every section "
                    "block's data row must have its file line"
                ),
                kind=kind.value,
            )
            require(
                0 < section.header_line < min(section.row_lines, default=section.header_line + 1),
                what=(
                    "split_activity_statement_sections post: every section "
                    "block's header line must come before its data rows"
                ),
                kind=kind.value,
                header_line=section.header_line,
            )
    return sections


def _parsed_section(section_name: str, line_no: int) -> KnownActivitySection:
    """Return the parsed section ``section_name`` names, or refuse (#913).

    Called for a section that is neither refused nor ignored, so a name
    that isn't a parsed section is on no list: nothing says whether it
    changes a filed figure. The refusal reports under the ``"<file>"``
    section and names only the line: the unknown name is a cell, which a
    shifted row can fill with the holder's name or account number. The
    enum's error, which names it, is replaced by a content-free stand-in
    cause (#1226).
    """
    try:
        return KnownActivitySection(section_name)
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    # Refused after the handler: the ValueError names the section.
    raise ParseError(
        section="<file>",
        row_index=line_no,
        column=None,
        message=(
            f"line {line_no} belongs to a section this tool neither "
            f"parses nor lists as safe to ignore, so it can't tell "
            f"whether the section changes a filed figure. {_UNEXPECTED}"
        ),
        raw_value="",
    ) from crash


def _skip_unread_row(
    section_name: str, discriminator: str, line_no: int, open_ignored: str | None
) -> str | None:
    """Check a row of a refused or ignored section, which isn't read.

    Returns the ignored section whose header (or headerless row) now
    leads, for the next row's header-first check.

    Raises:
        ParseError: If a ``_REFUSE_SECTIONS`` section (``Corporate
            Actions``) carries a non-header row, or an ignored section's
            row comes before its header (except in the headerless
            sections, by name).
    """
    if section_name in _REFUSE_SECTIONS:
        # Fail-closed refuse-guard, checked BEFORE the enum lookup so
        # it can't be silently disabled by promoting the section into
        # KnownActivitySection (the natural first step of #106 — the
        # section must be dropped from _REFUSE_SECTIONS to lift the
        # refusal). Any NON-header row signals a real corporate action
        # — a Data row, or a SubTotal / Total on a line-item-suppressed
        # "summary" statement — so refuse on anything that isn't the
        # section's Header (a header alone means no event occurred).
        if discriminator != _HEADER_DISCRIMINATOR:
            raise ParseError(
                section=section_name,
                row_index=line_no,
                column=None,
                message=(
                    f"the Activity Statement contains a "
                    f"{section_name!r} section with corporate-action "
                    f"rows. Corporate actions (splits, mergers, "
                    f"spin-offs, symbol/CUSIP changes) silently reshape "
                    f"lot quantity / cost basis and can mis-state "
                    f"Schedule FA initial / closing / proceeds. v1 does "
                    f"not yet parse this section, so it refuses rather "
                    f"than file an unverified figure. Track support: "
                    f"https://github.com/jay-tau/ibkr-fa/issues/106 "
                    f"(parser), "
                    f"https://github.com/jay-tau/ibkr-fa/issues/60 "
                    f"(compute)."
                ),
                raw_value="",
            )
        return None
    if discriminator == _HEADER_DISCRIMINATOR or section_name in _HEADERLESS_SECTIONS:
        return section_name
    _ensure_header_seen(open_ignored, section_name, line_no)
    return open_ignored


def _check_dropped_row(
    current_kind: KnownActivitySection | None,
    kind: KnownActivitySection,
    discriminator: str,
    line_no: int,
) -> None:
    """Check a parsed section's row that isn't a ``Header``, ``Data`` or Trades total row.

    Only a ``SubTotal`` or ``Total`` row after its section's header may
    be dropped, and only in a section ``_AGGREGATION_DROPPED_SECTIONS``
    lists; Trades' are held for their check (#1090) before this runs.
    A row of any other type could carry a figure that no parser
    reads, so it refuses (#913). So does a ``SubTotal`` or ``Total`` row
    elsewhere (#1012): ``_DATA_TOTAL_SECTIONS`` print their totals as
    ``Data`` rows, which their parsers check (#908), and the other
    sections define no such rows, so dropping one could hide a row their
    parsers would refuse.
    """
    if discriminator not in _AGGREGATION_DISCRIMINATORS:
        # Echo no cell: a shifted row can put the holder's name here.
        raise ParseError(
            section=kind.value,
            row_index=line_no,
            column=None,
            message=(
                f"line {line_no} has a row type other than 'Header', 'Data', "
                f"'SubTotal' or 'Total', the only ones this section's rows may "
                f"have, so the row could hold a figure no parser reads. "
                f"{_UNEXPECTED}"
            ),
            raw_value="",
        )
    _ensure_header_seen(current_kind, kind, line_no)
    if kind in _AGGREGATION_DROPPED_SECTIONS:
        return
    if kind in _DATA_TOTAL_SECTIONS:
        raise ParseError(
            section=kind.value,
            row_index=line_no,
            column=None,
            message=(
                f"line {line_no} is a {discriminator!r} row. This "
                f"section prints its totals as 'Data' rows, which its "
                f"parser checks; a {discriminator!r} row would be "
                f"dropped unchecked. The file is malformed; re-export "
                f"the Activity Statement."
            ),
            raw_value="",
        )
    raise ParseError(
        section=kind.value,
        row_index=line_no,
        column=None,
        message=(
            f"line {line_no} is a {discriminator!r} row, but the "
            f"{kind.value!r} section defines no 'SubTotal' or 'Total' "
            f"rows, so its parser would never read it. {_UNEXPECTED}"
        ),
        raw_value="",
    )


def dividend_evidence(
    sections: Mapping[KnownActivitySection, list[Section]],
) -> DividendEvidence:
    """Whether a split Activity Statement has any dividend-related section.

    Positive evidence for the FA pipeline's PR #137 "zero-when-sure"
    inference: an empty PARSED dividend list is only safe to render as ₹0
    when the statement genuinely has NO dividend-related section (not merely
    when the parsed list came back empty — those differ if a section is
    present but its rows were dropped/unparsed). We check both recognised
    dividend sections, since either means dividend activity exists:

    * ``Dividends`` — ALL distribution kinds are parsed here (Cash / Special
      / Choice Dividend, Payment In Lieu Of Dividend, Return Of Capital,
      etc.), so its presence also covers substitute / payment-in-lieu income.
    * ``Change in Dividend Accruals`` — reversals / corrections / accruals;
      present even when no cash Dividends section was emitted. Its rows
      aren't read, so it maps to an empty block list when present.

    Returns ``PRESENT`` if EITHER section is present (caller must NOT
    infer zero), ``ABSENT`` only when both are absent.
    """
    if (
        KnownActivitySection.DIVIDENDS in sections
        or KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS in sections
    ):
        return DividendEvidence.PRESENT
    return DividendEvidence.ABSENT


def _strip_trailing_empty(cells: Iterable[str]) -> list[str]:
    """Trim trailing empty cells from a row.

    IBKR pads every row to the maximum column count of the entire
    file (often 17), so a 5-column section header has 12 trailing
    commas. Those become empty-string column names that pollute the
    dict keys. Drop them.
    """
    out = list(cells)
    while out and out[-1] == "":
        out.pop()
    return out


def _validate_header_has_columns(
    columns: tuple[str, ...],
    kind: KnownActivitySection,
    line_no: int,
) -> None:
    """Reject a Header row that declares no column names (#812).

    ``columns`` has trailing empty cells stripped, so a bare
    ``Statement,Header`` and a padded ``Statement,Header,,,,`` both
    arrive empty. Such a block can hold no readable rows; dropping it
    would make the section, or one of its blocks, look absent.
    """
    if not columns:
        raise ParseError(
            section=kind.value,
            row_index=line_no,
            column=None,
            message=(
                "section header declares no column names, so its rows "
                "can't be read. The file is malformed; re-export the "
                "Activity Statement."
            ),
            raw_value="",
        )


def _validate_header_names_not_blank(
    columns: tuple[str, ...],
    kind: KnownActivitySection,
    line_no: int,
) -> None:
    """Reject a Header row with an empty or whitespace-only column name (#819).

    Only trailing ``""`` cells are padding (:func:`_strip_trailing_empty`).
    A blank name left after that keys the value under it by ``""`` or
    ``" "``, which no section parser reads, so the value would be dropped
    without an error.
    """
    for position, name in enumerate(columns, start=1):
        if not name.strip():
            raise ParseError(
                section=kind.value,
                row_index=line_no,
                column=None,
                message=(
                    f"section header's column name {position} is empty or only "
                    f"whitespace (the first name after 'Header' is 1), so the "
                    f"values under it belong to no column. The file is "
                    f"malformed; re-export the Activity Statement."
                ),
                raw_value="",
            )


def _validate_header_columns_unique(
    columns: tuple[str, ...],
    kind: KnownActivitySection,
    line_no: int,
) -> None:
    """Reject a Header row whose column names contain duplicates.

    Activity Statement sections are identified only by the first
    cell (the section name), so unlike the Flex Query splitter we
    can't catch a duplicate-column header via signature mismatch.
    Without this guard, ``dict(zip(columns, data, strict=True))``
    in :func:`_row_cells` would silently overwrite the
    earlier value for any repeated column name — losing data.

    A duplicate-column header signals either a malformed file or
    an IBKR schema change; either way, refusing it loudly is safer
    than silently corrupting parsed rows. The error names the first
    repeat and the name it repeats by position, not by name: a header
    cell is a cell, which a shifted row can fill with the holder's name.
    """
    first_positions: dict[str, int] = {}
    for position, name in enumerate(columns, start=1):
        if name in first_positions:
            raise ParseError(
                section=kind.value,
                row_index=line_no,
                column=None,
                message=(
                    f"section header's column name {position} repeats column "
                    f"name {first_positions[name]} (the first name after "
                    f"'Header' is 1); refusing because dict(zip(...)) on data "
                    f"rows would silently overwrite the earlier value. "
                    f"Either the file is malformed or IBKR has changed "
                    f"this section's schema."
                ),
                raw_value="",
            )
        first_positions[name] = position


def _refuse_open_positions_details(
    columns: tuple[str, ...],
    kind: KnownActivitySection,
    line_no: int,
) -> None:
    """Refuse an Open Positions header with IBKR's ``Open`` detail column (#1195).

    IBKR adds it, and a ``Lot`` row per lot, when a statement is exported
    with details shown. This tool reads the standard layout only. The name
    must match exactly; any other column goes on to the declared-shape check.
    """
    if kind is KnownActivitySection.OPEN_POSITIONS and _OPEN_POSITIONS_DETAIL_COLUMN in columns:
        raise ParseError(
            section=kind.value,
            row_index=line_no,
            column=None,
            message=(
                "this Activity Statement was exported with position and trade details "
                "shown (Open Positions has an 'Open' column). This tool reads the "
                f"standard layout: re-export it with {_HIDE_DETAILS}"
            ),
            raw_value="",
        )


def refuse_trades_detail_row(row: Mapping[str, str], *, row_index: int) -> None:
    """Refuse a Trades row of a kind IBKR adds only with details shown (#1195).

    The splitter calls it after header-seen and width validation, before the
    block's totals are checked. The Trades parser calls it after its required-key
    check. The ``DataDiscriminator`` must match a detail kind exactly; a row
    without one isn't checked.
    """
    if row.get("DataDiscriminator") in _TRADES_DETAIL_KINDS:
        raise ParseError(
            section=KnownActivitySection.TRADES.value,
            row_index=row_index,
            column="DataDiscriminator",
            message=(
                "this row is one of IBKR's trade details, which this tool doesn't read. "
                f"Re-export the Activity Statement with {_HIDE_DETAILS}"
            ),
            raw_value="",
        )


def _validate_header_columns_declared(
    columns: tuple[str, ...],
    kind: KnownActivitySection,
    line_no: int,
) -> None:
    """Reject a header column its section neither reads nor ignores (#913).

    The header must fit one of the section's :data:`_SECTION_SHAPES`.
    A column outside it could carry a value that changes a filed figure
    (e.g. an ``Adjustment`` amount) while its parser reads the row as
    before. The error names, by position, the first such column of the
    shape the header shares most columns with; it doesn't print the
    name, which a shifted row can fill with the holder's name.
    """
    shapes = _SECTION_SHAPES[kind]
    if any(shape.columns.issuperset(columns) for shape in shapes):
        return
    closest = max(shapes, key=lambda shape: len(shape.columns.intersection(columns)))
    position = next(
        position for position, name in enumerate(columns, start=1) if name not in closest.columns
    )
    raise ParseError(
        section=kind.value,
        row_index=line_no,
        column=None,
        message=(
            f"the header on line {line_no} has a column name {position} (the "
            f"first name after 'Header' is 1) that this block shape neither "
            f"reads nor lists as safe to ignore. The header must fit one "
            f"declared block shape, so its values could change a filed "
            f"figure unseen. {_UNEXPECTED}"
        ),
        raw_value="",
    )


def _ensure_header_seen(
    current_section: str | None,
    row_section: str,
    line_no: int,
) -> None:
    """Raise if a non-header row appears before its section's header."""
    if current_section != row_section:
        raise ParseError(
            section=str(row_section),
            row_index=line_no,
            column=None,
            message=(
                f"row appeared before any '{_HEADER_DISCRIMINATOR}' "
                f"row was seen for section {str(row_section)!r}; "
                f"the Activity Statement format requires the Header "
                f"row to precede Data rows in each section."
            ),
            raw_value="",
        )


def _row_cells(
    *,
    row: list[str],
    columns: tuple[str, ...],
    section: KnownActivitySection,
    line_no: int,
    what: str = "data row",
) -> dict[str, str]:
    """Map a CSV row's cells to the section's column tuple.

    ``what`` names the row in refusals: a ``Data`` row, or a Trades
    ``SubTotal`` or ``Total`` row, which gets the same width rule
    (#1090).

    IBKR pads rows to a uniform width with empty cells. The
    splitter therefore tolerates trailing-empty padding past the
    declared column count, but raises if any overflow cell is
    POPULATED — that signals either a malformed file or an IBKR
    schema change, and silently dropping a non-empty value could
    corrupt downstream tax calculations (e.g., losing a tax-code
    flag at the end of a Trades row).

    A row with FEWER value cells than the header declared is
    always a malformed file — raise unconditionally.

    PII note: the offending ``row`` is intentionally NOT embedded
    in ``ParseError.raw_value``. The ``Account Information`` section's
    data rows include the IBKR account number in the value-cell
    position, so a naive ``",".join(row)`` would leak the raw
    account ID into exception strings reproduced in bug reports,
    stderr, etc. (T1-r10 quality). The error message + ``section``
    + ``row_index`` give the user enough context to locate the
    offending line in the source CSV.
    """
    value_cells = row[2:]
    if len(value_cells) < len(columns):
        raise ParseError(
            section=section.value,
            row_index=line_no,
            column=None,
            message=(
                f"{what} has {len(value_cells)} value cell(s); "
                f"the section header declared {len(columns)} column(s)."
            ),
            raw_value="",
        )
    overflow = value_cells[len(columns) :]
    if any(cell != "" for cell in overflow):
        raise ParseError(
            section=section.value,
            row_index=line_no,
            column=None,
            message=(
                f"{what} has {len(value_cells)} value cell(s) "
                f"with non-empty content past the {len(columns)} "
                f"column(s) the header declared; refusing to drop "
                f"populated overflow cells. Either the file is "
                f"malformed or IBKR has added columns to this "
                f"section's schema."
            ),
            raw_value="",
        )
    return dict(zip(columns, value_cells[: len(columns)], strict=True))
